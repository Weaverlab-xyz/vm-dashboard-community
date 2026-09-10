"""The DB/function progress bars and the GCP Cloud SQL reclaim, both fixed off one job.

Observed 2026-09-10: a GCP Postgres clouddb_provision failed after 5m29s carrying
terraform's own "Interrupt received. / Gracefully shutting down... / Error: execution
halted" wall. The apply was SIGNALLED mid-create -- GCP never refused anything, and
Cloud SQL creates on this path take ~7 minutes. Two separate defects fell out of it:

1. The bar sat at 40% "Creating the database..." for the whole five minutes.
   ``_DB_MILESTONES`` keyed on the bare phrase ``"creating..."``, which is a SUBSTRING
   of ``"still creating..."``, and the matcher takes the FIRST hit and breaks -- so
   every "Still creating" line matched the 40% row and the 55% row below it was
   unreachable. A destroy never left 40% either, for the same reason. The identical
   table shape carried the identical bug in ``cloud_function_service._FN_MILESTONES``,
   where GCP's Cloud Build makes "still creating" the normal state for a minute or two.
   Both tables now anchor every needle on the ``"<address>: "`` separator terraform
   prints, so the general phrase cannot swallow the specific one -- and the structural
   test below pins that no needle shadows a later row again.

2. An interrupted apply leaves the instance created but ABSENT from Terraform state:
   the same end state as the create-wait error the reclaim already self-heals, but the
   trigger only matched "error waiting for create instance", so an interrupt fell
   through to a plain failure and left a billable orphan (name blocked ~a week). The
   trigger is now a marker tuple covering both, and the import tolerates a resource
   already in state -- an interrupt, unlike create-wait, can commit it before stopping.

Same stubbing approach as test_clouddb_capacity_error.py: heavy app deps stood in via
sys.modules. Runs under pytest, or standalone:
    python tests/test_clouddb_progress_and_reclaim.py
"""
import asyncio
import os
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


class _Settings:
    def __getattr__(self, _key):
        return ""


class _TerraformError(Exception):
    """Mirror of terraform.TerraformError: the reclaim's except clause must see the
    same class the stubs raise with."""


_BROADCASTS = []


def _install_stubs():
    confmod = types.ModuleType("web_dashboard.config")
    confmod.settings = _Settings()
    sys.modules["web_dashboard.config"] = confmod

    dbmod = types.ModuleType("web_dashboard.database")
    for _name in ("CloudDatabase", "CloudFunction", "Job", "CloudFunctionInvocation",
                  "K8sCluster", "EC2Instance", "AzureVM", "GCEInstance"):
        setattr(dbmod, _name, type(_name, (), {}))
    sys.modules["web_dashboard.database"] = dbmod

    cfg = types.ModuleType("web_dashboard.services.config_service")
    cfg.get = lambda key: ""
    cfg.get_bool = lambda key, default=False: default
    sys.modules["web_dashboard.services.config_service"] = cfg

    tf = types.ModuleType("web_dashboard.services.terraform")
    tf.TerraformError = _TerraformError
    sys.modules["web_dashboard.services.terraform"] = tf

    tpe = types.ModuleType("web_dashboard.services.terraform_provider_env")
    tpe.provider_env = lambda cloud: {}
    sys.modules["web_dashboard.services.terraform_provider_env"] = tpe

    js = types.ModuleType("web_dashboard.services.job_service")
    js.cancel_check = lambda job_id, state, interval_s=5.0: None
    sys.modules["web_dashboard.services.job_service"] = js

    # _job_stream resolves broadcast_progress at call time; stub the package too so the
    # parent import does not drag the real FastAPI app in.
    apimod = types.ModuleType("web_dashboard.api")
    apimod.__path__ = []
    ws = types.ModuleType("web_dashboard.api.websocket")

    async def _broadcast(job_id, pct, msg, log_line=None):
        _BROADCASTS.append((pct, msg))

    ws.broadcast_progress = _broadcast
    apimod.websocket = ws
    sys.modules["web_dashboard.api"] = apimod
    sys.modules["web_dashboard.api.websocket"] = ws


_install_stubs()
try:
    from web_dashboard.services import cloud_database_service as svc
    from web_dashboard.services import cloud_function_service as fnsvc
except Exception as exc:  # pragma: no cover -- skip if other app deps are missing
    try:
        import pytest
        pytest.skip(f"service import unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)


# ── Real terraform output, trimmed from the failed job ────────────────────────

_CREATE_STREAM = [
    "Plan: 3 to add, 0 to change, 0 to destroy.",
    "google_sql_database_instance.this: Creating...",
    "google_sql_database_instance.this: Still creating... [10s elapsed]",
    "google_sql_database_instance.this: Still creating... [5m0s elapsed]",
]
_DESTROY_STREAM = [
    "google_sql_database_instance.this: Destroying... [id=clouddb-05950d12]",
    "google_sql_database_instance.this: Still destroying... [id=clouddb-05950d12, 10s elapsed]",
]


def _drive(stream, start_pct=5, start_msg="Provisioning"):
    """Push lines through the REAL _job_stream on_line and return the (pct, msg) trail."""
    _BROADCASTS.clear()
    on_line = svc._job_stream("job-1", start_pct, start_msg)

    async def _go():
        for line in stream:
            await on_line(line)

    asyncio.run(_go())
    return list(_BROADCASTS)


# ── 1. The progress bar ───────────────────────────────────────────────────────

def test_still_creating_reaches_the_long_wait_milestone():
    trail = _drive(_CREATE_STREAM)
    assert trail[0][0] == 20, trail          # Plan:
    assert trail[1][0] == 40, trail          # Creating...
    # The regression: these were 40 with the bare-phrase needle.
    assert trail[2][0] == 55, trail
    assert trail[3][0] == 55, trail
    assert "several minutes" in trail[3][1]


def test_still_destroying_reaches_its_own_milestone():
    trail = _drive(_DESTROY_STREAM, start_pct=5, start_msg="Decommissioning")
    assert trail[0][0] == 40, trail          # Destroying...
    assert trail[1][0] == 60, trail          # Still destroying... (was 40)


def test_function_table_still_creating_reaches_the_build_milestone():
    # GCP Cloud Build dominates this timeline, so the shadowed row was the one that
    # mattered most: "Building and deploying" is what the operator needs to see.
    matched = []
    for line in ["google_cloudfunctions2_function.this: Creating...",
                 "google_cloudfunctions2_function.this: Still creating... [1m0s elapsed]"]:
        low = line.lower()
        for needle, pct, msg in fnsvc._FN_MILESTONES:
            if needle in low:
                matched.append((pct, msg))
                break
    assert matched[0][0] == 35, matched
    assert matched[1][0] == 55, matched
    assert "Building and deploying" in matched[1][1]


def test_plain_lines_do_not_move_the_bar():
    trail = _drive(["google_sql_database_instance.this: Still creating... [10s elapsed]",
                    "The plugin.(*GRPCProvider).ApplyResourceChange request was cancelled."])
    assert trail[0][0] == 55
    assert trail[1] == (55, trail[0][1]), trail   # unmatched line keeps pct AND msg


def test_no_needle_shadows_a_later_row():
    """Structural guard: the bug was a needle that also matches a LATER row's phrase,
    which the first-hit-wins matcher then makes unreachable. Pin it for both tables so
    a future edit cannot reintroduce it by reordering."""
    for table_name, table in (("_DB_MILESTONES", svc._DB_MILESTONES),
                              ("_FN_MILESTONES", fnsvc._FN_MILESTONES)):
        needles = [n for n, _pct, _msg in table]
        assert len(set(needles)) == len(needles), f"{table_name}: duplicate needle"
        for i, earlier in enumerate(needles):
            for later in needles[i + 1:]:
                assert earlier not in later, (
                    f"{table_name}: {earlier!r} is a substring of the later {later!r}, "
                    f"so the {later!r} row can never match")


def test_every_milestone_row_is_reachable_from_real_terraform_output():
    """The complement of the structural test: each row must actually be the winner for
    some real terraform line, or it is dead weight."""
    lines = _CREATE_STREAM + _DESTROY_STREAM + [
        "google_sql_database_instance.this: Creation complete after 6m1s [id=x]",
        "google_sql_database_instance.this: Destruction complete after 1m2s",
    ]
    won = set()
    for line in lines:
        low = line.lower()
        for needle, _pct, _msg in svc._DB_MILESTONES:
            if needle in low:
                won.add(needle)
                break
    assert won == {n for n, _p, _m in svc._DB_MILESTONES}, (
        f"unreachable rows: {{n for n, _p, _m in svc._DB_MILESTONES}} - {won}")


# ── 2. The GCP reclaim trigger ────────────────────────────────────────────────

# The tail of the real failed apply, as terraform.apply raises it.
_INTERRUPT_WALL = _TerraformError(
    "terraform apply failed:\n"
    "google_sql_database_instance.this: Still creating... [5m0s elapsed]\n"
    "\nInterrupt received.\n"
    "Please wait for Terraform to exit or data loss may occur.\n"
    "Gracefully shutting down...\n"
    "\nStopping operation...\n"
    "\nError: execution halted\n"
    "\nError: Request cancelled\n"
    "\n  with google_sql_database_instance.this,\n"
    "  on main.tf line 125, in resource \"google_sql_database_instance\" \"this\":\n"
    "\nThe plugin.(*GRPCProvider).ApplyResourceChange request was cancelled.\n"
)
_CREATE_WAIT = _TerraformError(
    "terraform apply failed:\nError: Error waiting for Create Instance: \n")


def _gcp_row():
    return types.SimpleNamespace(cloud="gcp", id="05950d12-e55e-42ff-a102-d46e92344254")


def _reclaim(row, exc, *, runnable=None, import_exc=None, apply_result=None):
    """Run the reclaim with gcp_service/terraform stubbed; returns (result, calls)."""
    calls = {"waited": False, "imported": False, "applied": False}

    async def _wait(project, name, clouddb_id):
        calls["waited"] = True
        calls["wait_args"] = (project, name, clouddb_id)
        return runnable

    async def _import(*_a, **_kw):
        calls["imported"] = True
        if import_exc:
            raise import_exc

    async def _apply(*_a, **_kw):
        calls["applied"] = True
        return apply_result or {}

    gcp = types.ModuleType("web_dashboard.services.gcp_service")
    gcp.wait_sql_instance_runnable = _wait
    sys.modules["web_dashboard.services.gcp_service"] = gcp
    svc.terraform.import_resource = _import
    svc.terraform.apply = _apply

    result = asyncio.run(svc._reclaim_gcp_sql_instance(
        row=row, job_id="job-1", engine="postgres",
        tf_variables={"project": "p-1", "identifier": "clouddb-05950d12"}, exc=exc))
    return result, calls


def test_interrupted_apply_now_triggers_the_reclaim():
    # The regression: this error text fell straight through to a failed job + orphan.
    _result, calls = _reclaim(_gcp_row(), _INTERRUPT_WALL, runnable=None)
    assert calls["waited"], "an interrupted apply must be checked for a created instance"
    assert calls["wait_args"] == (
        "p-1", "clouddb-05950d12", "05950d12-e55e-42ff-a102-d46e92344254")


def test_create_wait_error_still_triggers_the_reclaim():
    _result, calls = _reclaim(_gcp_row(), _CREATE_WAIT, runnable=None)
    assert calls["waited"]


def test_absent_instance_gives_up_without_importing():
    result, calls = _reclaim(_gcp_row(), _INTERRUPT_WALL, runnable=None)
    assert result is None
    assert not calls["imported"] and not calls["applied"]


def test_runnable_instance_is_imported_and_reapplied():
    result, calls = _reclaim(_gcp_row(), _INTERRUPT_WALL,
                             runnable={"state": "RUNNABLE"},
                             apply_result={"instance_id": "clouddb-05950d12", "port": 5432})
    assert calls["imported"] and calls["applied"]
    assert result["instance_id"] == "clouddb-05950d12"


def test_already_in_state_skips_the_import_and_still_converges():
    # An interrupt can commit the instance to state before stopping, unlike create-wait.
    # Without the tolerance this turned a recoverable interrupt into a second failure.
    already = _TerraformError(
        "terraform import failed:\nError: Resource already managed by Terraform\n"
        "Terraform is already managing a remote object for "
        "google_sql_database_instance.this.")
    result, calls = _reclaim(_gcp_row(), _INTERRUPT_WALL, runnable={"state": "RUNNABLE"},
                             import_exc=already, apply_result={"port": 5432})
    assert calls["applied"], "a resource already in state only needed the re-apply"
    assert result == {"port": 5432}


def test_a_real_import_failure_still_propagates():
    boom = _TerraformError("terraform import failed:\nError: Cannot import non-existent "
                           "remote object")
    try:
        _reclaim(_gcp_row(), _INTERRUPT_WALL, runnable={"state": "RUNNABLE"},
                 import_exc=boom)
    except _TerraformError as exc:
        assert "non-existent" in str(exc)
    else:
        raise AssertionError("an unrelated import failure must not be swallowed")


def test_non_gcp_clouds_are_never_reclaimed():
    row = types.SimpleNamespace(cloud="aws", id="05950d12")
    result, calls = _reclaim(row, _INTERRUPT_WALL, runnable={"state": "RUNNABLE"})
    assert result is None
    assert not calls["waited"], "the reclaim is GCP-only (only GCP drops state)"


def test_unrelated_gcp_failure_is_not_reclaimed():
    # A quota/config error means nothing was created; polling for an instance and
    # importing would be wrong (and the marker match must not be that loose).
    exc = _TerraformError("terraform apply failed:\nError: googleapi: Error 403: "
                          "Cloud SQL Admin API has not been used in project")
    result, calls = _reclaim(_gcp_row(), exc, runnable={"state": "RUNNABLE"})
    assert result is None
    assert not calls["waited"]


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
