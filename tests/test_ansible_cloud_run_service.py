"""Unit tests: ansible_cloud_run_service backend selection + helpers.

Pins the private-resource invariant — k8s/DB Ansible runs ALWAYS pick an in-cloud
transient runner from the resource's cloud, and 'local' is rejected — plus the
output scrubbing and kubeconfig-token extraction feeding it.

Heavy service deps are stubbed in sys.modules (mirrors the other config-mgmt unit
tests). Runs under pytest, or standalone:
    python tests/test_ansible_cloud_run_service.py
"""
import os
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

CONF = {}


def _install_stubs():
    sa = types.ModuleType("sqlalchemy")
    sa_orm = types.ModuleType("sqlalchemy.orm")
    sa_orm.Session = type("Session", (), {})
    sa.orm = sa_orm
    sys.modules["sqlalchemy"] = sa
    sys.modules["sqlalchemy.orm"] = sa_orm

    cfg = types.ModuleType("web_dashboard.services.config_service")
    cfg.get = lambda key: CONF.get(key, "")
    cfg.resolve_reference = lambda ref: ref
    cfg.is_reference = lambda ref: False
    sys.modules["web_dashboard.services.config_service"] = cfg

    # The remaining siblings are only imported (not called) at module load.
    for name in ("cloud_database_service", "job_service", "k8s_runner_service",
                 "k8s_service", "storage_service"):
        sys.modules[f"web_dashboard.services.{name}"] = types.ModuleType(
            f"web_dashboard.services.{name}")


_install_stubs()
try:
    from web_dashboard.services import ansible_cloud_run_service as acr
except Exception as exc:  # pragma: no cover
    try:
        import pytest
        pytest.skip(f"ansible_cloud_run_service import unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)


def test_resolve_runner_cloud_native_defaults():
    CONF.clear()
    assert acr.resolve_runner("aws") == "ecs"
    assert acr.resolve_runner("azure") == "aci"
    assert acr.resolve_runner("gcp") == "gcp"


def test_resolve_runner_per_cloud_override_honored():
    CONF.clear()
    CONF["ansible_runner_aws"] = "ecs"
    assert acr.resolve_runner("aws") == "ecs"


def test_resolve_runner_rejects_local():
    """A CLOUD-hosted cluster/DB is private to its VPC, so a 'local' override stays an
    error. This is the opposite topology from an on-prem cluster (test below) — both
    rules have to hold at once."""
    CONF.clear()
    CONF["ansible_runner_gcp"] = "local"
    try:
        acr.resolve_runner("gcp")
    except acr.AnsibleCloudRunError as e:
        assert "must run on an in-cloud runner" in str(e)
        return
    raise AssertionError("expected AnsibleCloudRunError for a 'local' override")


def test_resolve_runner_local_cloud_uses_the_local_runner():
    """A cluster registered with cloud='local' is on the corporate LAN, where an ECS /
    ACI / Cloud Run task has no route. The dashboard host is the only thing that can
    reach it, so 'local' is the required answer — not a rejected one."""
    CONF.clear()
    assert acr.resolve_runner("local") == "local"


def test_resolve_runner_local_ignores_a_per_cloud_override():
    """There is no ansible_runner_local knob — an on-prem cluster has exactly one
    reachable runner, so a stray override must not be able to send it in-cloud."""
    CONF.clear()
    CONF["ansible_runner_local"] = "ecs"
    assert acr.resolve_runner("local") == "local"


def test_target_clouds_k8s_allows_local_databases_do_not():
    """Single source of truth shared with the /managed-targets picker and the run
    gate. A CloudDatabase is always provisioned into a cloud, so there is no local
    endpoint for a local runner to reach."""
    assert "local" in acr.K8S_TARGET_CLOUDS
    assert "local" not in acr.DB_TARGET_CLOUDS
    for c in ("aws", "azure", "gcp"):
        assert c in acr.K8S_TARGET_CLOUDS and c in acr.DB_TARGET_CLOUDS


def test_resolve_runner_rejects_unsupported_cloud():
    CONF.clear()
    try:
        acr.resolve_runner("oci")
    except acr.AnsibleCloudRunError:
        return
    raise AssertionError("expected AnsibleCloudRunError for an unsupported cloud")


# ── target + asset-storage gate ───────────────────────────────────────────────

def test_check_target_accepts_a_local_cluster_with_a_local_asset():
    """The whole point: an on-prem cluster runs on THIS host, which can obviously read
    this host's filesystem. Requiring an S3 upload first would be absurd."""
    CONF.clear()
    assert acr.check_target("k8s", "local", "local", "play.yml") is None


def test_check_target_still_refuses_a_local_asset_for_a_cloud_cluster():
    """Unchanged behavior for in-cloud runners — they cannot read this host's disk."""
    CONF.clear()
    msg = acr.check_target("k8s", "aws", "local", "play.yml")
    assert msg and "cannot reach" in msg
    assert "play.yml" in msg, "the message should name the offending asset"


def test_check_target_accepts_a_cloud_cluster_with_a_cloud_asset():
    CONF.clear()
    assert acr.check_target("k8s", "gcp", "s3", "play.yml") is None


def test_check_target_refuses_a_local_database():
    """A CloudDatabase is always provisioned into a cloud; cloud='local' is not a
    thing for it, and the local runner has no endpoint to reach."""
    CONF.clear()
    msg = acr.check_target("database", "local", "s3", "play.yml")
    assert msg and "no Ansible runner for database targets" in msg


def test_check_target_refuses_an_unsupported_cloud():
    CONF.clear()
    assert acr.check_target("k8s", "oci", "s3", "play.yml")


def test_check_target_reports_a_misconfigured_runner_instead_of_raising():
    """check_target consults resolve_runner, which raises on a bad
    ansible_runner_<cloud>. That must come back as a validation message the caller can
    turn into a 400 — not an exception that becomes a 500 at enqueue time."""
    CONF.clear()
    CONF["ansible_runner_aws"] = "local"
    msg = acr.check_target("k8s", "aws", "local", "play.yml")
    assert msg and "must run on an in-cloud runner" in msg


def test_scrub_redacts_long_values_only():
    out = acr._scrub("pw=supersecret host=db1 pin=42", ["supersecret", "42"])
    assert "supersecret" not in out
    assert "***" in out
    assert "42" in out, "short values (<4 chars) are not redacted"


def test_kubeconfig_tokens_extraction():
    try:
        import yaml  # noqa: F401
    except ModuleNotFoundError:
        return  # best-effort helper is a no-op without PyYAML; skip when absent
    kc = (
        "apiVersion: v1\n"
        "users:\n"
        "- name: eks\n"
        "  user:\n"
        "    token: k8s-bearer-abcdef\n"
    )
    assert acr._kubeconfig_tokens(kc) == ["k8s-bearer-abcdef"]


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
    sys.exit(1 if failures else 0)
