"""A GCP Cloud SQL decommission that the orphan sweep finished must not wedge forever.

Live case (job d2b06e68, db f5128341): the first decommission's `terraform destroy`
failed on DROP DATABASE — Postgres answered "database app_db is being accessed by
other users", the backends of the Entitle forwarder deleted seconds earlier — and the
orphan sweep then deleted the instance directly by name. Its database stayed in
Terraform state, and the google provider can never refresh or delete a database of a
missing instance: Cloud SQL answers 403 notAuthorized there, not 404. Every retry
failed on that 403, with nothing left in the cloud to delete.

Pinned here:
  * the state-rm helper forgets ONLY the resource types it is given (never the
    instance, never a data source);
  * decommission forgets them only once the sweep has PROVEN the instance gone, and
    re-runs destroy to confirm rather than assuming success;
  * the GCP modules ABANDON the database/user, so a fresh deployment never tries the
    DROP that can fail at all.

Terraform itself is never invoked — `_run` is stubbed.
Runs under pytest, or standalone:  python tests/test_clouddb_gcp_destroy_after_sweep.py
"""
import os
import subprocess
import sys
import types
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


class _Settings:
    terraform_executable = "terraform"

    def __getattr__(self, _key):
        return ""


def _install_stubs():
    confmod = types.ModuleType("web_dashboard.config")
    confmod.settings = _Settings()
    sys.modules["web_dashboard.config"] = confmod

    st = types.ModuleType("web_dashboard.services.storage_service")
    st.active_backend = lambda: "local"
    sys.modules["web_dashboard.services.storage_service"] = st

    cfg = types.ModuleType("web_dashboard.services.config_service")
    cfg.get = lambda key: ""
    sys.modules["web_dashboard.services.config_service"] = cfg


_install_stubs()
try:
    from web_dashboard.services import terraform as tf
except Exception as exc:  # pragma: no cover — skip if other app deps are missing
    try:
        import pytest
        pytest.skip(f"terraform service import unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)

_STATE = """google_sql_database_instance.this
google_sql_database.this[0]
google_sql_user.master
data.google_compute_network.vpc
module.extra.google_sql_user.other
"""


def _fake_run(calls, list_rc=0):
    def run(cmd, cwd, timeout=600, env=None):
        calls.append(cmd)
        if cmd[:2] == ["state", "list"]:
            return subprocess.CompletedProcess(cmd, list_rc, _STATE, "boom" if list_rc else "")
        return subprocess.CompletedProcess(cmd, 0, "", "")
    return run


def test_only_the_named_types_are_forgotten():
    calls = []
    orig = tf._run
    tf._run = _fake_run(calls)
    try:
        removed = tf._forget_resource_types_sync(
            "/x", ("google_sql_database", "google_sql_user"), None)
    finally:
        tf._run = orig
    assert removed == ["google_sql_database.this[0]", "google_sql_user.master",
                       "module.extra.google_sql_user.other"]
    rm_targets = [c[-1] for c in calls if c[:2] == ["state", "rm"]]
    assert "google_sql_database_instance.this" not in rm_targets, \
        "the instance must be left for destroy to confirm gone"
    assert not any(t.startswith("data.") for t in rm_targets)


def test_a_failed_state_list_raises_rather_than_forgetting_nothing_quietly():
    orig = tf._run
    tf._run = _fake_run([], list_rc=1)
    try:
        try:
            tf._forget_resource_types_sync("/x", ("google_sql_database",), None)
        except tf.TerraformError:
            return
        raise AssertionError("a state-list failure must surface")
    finally:
        tf._run = orig


def _decommission_body():
    src = Path(_ROOT, "web_dashboard", "services", "cloud_database_service.py").read_text(
        encoding="utf-8")
    return src.split("async def run_decommission(", 1)[1].split("\ndef ", 1)[0]


def test_children_are_forgotten_only_after_the_sweep_proves_the_instance_gone():
    body = _decommission_body()
    sweep = body.index("sweep_orphan_sql_instance(")
    forget = body.index("forget_resource_types(")
    assert sweep < forget
    gate = body[sweep:forget]
    assert 'result in ("deleted", "not-found")' in gate, \
        "an unlabeled or still-present instance must never have its children forgotten"
    assert "destroy_error" in gate, "a clean destroy has nothing to forget"


def test_the_teardown_is_confirmed_by_a_second_destroy_not_assumed():
    body = _decommission_body()
    after = body.split("forget_resource_types(", 1)[1]
    assert after.index("terraform.destroy(") < after.index("destroy_error = None")


def test_a_destroy_failure_still_fails_the_job_when_unrecovered():
    body = _decommission_body()
    assert "errors.insert(0, destroy_error)" in body
    assert body.index("errors.insert(0, destroy_error)") < body.index("if errors:")


def test_the_gcp_modules_abandon_what_the_instance_delete_removes():
    for engine, has_user in (("postgres", True), ("mysql", True), ("sqlserver", False)):
        src = Path(_ROOT, "terraform", f"db_gcp_{engine}", "main.tf").read_text(
            encoding="utf-8")
        db_block = src.split('resource "google_sql_database" "this"', 1)[1].split("\n}", 1)[0]
        assert 'deletion_policy = "ABANDON"' in db_block, engine
        if has_user:
            user = src.split('resource "google_sql_user" "master"', 1)[1].split("\n}", 1)[0]
            assert 'deletion_policy = "ABANDON"' in user, engine


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {exc!r}")
    sys.exit(1 if failures else 0)
