"""SPIFFE for everyday AWS calls, Workload Credentials for privileged jobs, on a POV.

docs/profiles/pov/public-cloud.md → Keyless AWS → With Workload Credentials for jobs. The
lease machinery (workload_credential_lease) and the everyday-from-federation rule are
pinned in tests/test_workload_credential_lease.py; what is pinned here is what the POV
split adds:

  * a timer-driven READ-ONLY job keeps the everyday credential. pov_env_reconcile runs
    every ten minutes, and in the provision scope it would keep a METERED, write-capable
    lease alive around the clock — so it is exempt, and the exemption is a closed set;
  * every other job type still takes the provision scope, including power, which the
    suspend schedule only ENQUEUES from inside reconcile;
  * reconcile reaches no adapter write — the precondition for its exemption;
  * Test connection proves the jobs' credential with the unmetered session check and
    NEVER mints (a lease is billed, and the button is one an operator presses until green);
  * the panel's third choice writes wlc_aws_everyday_source and nothing of WLC's own.

Run: python tests/test_pov_cloud_wlc_split.py   (or under pytest)
"""
import ast
import asyncio
import os
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="pov-wlc-split-test-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDB}"
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-pov-wlc-split")

try:
    import boto3  # noqa: F401
    import pydantic  # noqa: F401
    import sqlalchemy  # noqa: F401
except ModuleNotFoundError as exc:  # pragma: no cover — app deps missing
    try:
        import pytest
        pytest.skip(f"app dependencies unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)

# First-party imports UNGUARDED: a broken module must fail this file, not skip it.
from web_dashboard import jobs_worker  # noqa: E402
from web_dashboard.api.setup import PovCloudFeatureConfig  # noqa: E402
from web_dashboard.database import Base, engine  # noqa: E402
from web_dashboard.services import (aws_service, cloud_federation,  # noqa: E402
                                    job_service, pov_aws_service, pov_cloud_aws)
from web_dashboard.services import workload_credential_lease as leases  # noqa: E402
from web_dashboard.services import workload_credentials_service as wlc  # noqa: E402

Base.metadata.create_all(bind=engine)

_SETTINGS = os.path.join(_ROOT, "web_dashboard", "templates", "settings.html")
_RECONCILE = os.path.join(_ROOT, "web_dashboard", "services", "pov_reconcile.py")


class _Patch:
    def __init__(self):
        self._undo = []

    def set(self, obj, name, value):
        self._undo.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def undo(self):
        for obj, name, value in reversed(self._undo):
            setattr(obj, name, value)


# ── which jobs take write privilege ───────────────────────────────────────────

def test_the_everyday_job_set_is_exactly_reconcile():
    """A closed set. Joining it means a job runs WITHOUT write privilege, so a new member
    must be argued for in jobs_worker's comment, not added in passing."""
    assert jobs_worker.EVERYDAY_JOB_TYPES == frozenset({"pov_env_reconcile"})


def test_every_everyday_job_is_a_routine_timer_job():
    assert jobs_worker.EVERYDAY_JOB_TYPES <= set(job_service.ROUTINE_JOB_TYPES)
    assert jobs_worker.EVERYDAY_JOB_TYPES <= set(jobs_worker.HANDLED_TYPES)


def _purpose_inside(job_type):
    with jobs_worker._wlc_provisioning(job_type):
        return leases._purpose_ctx.get()


def test_reconcile_runs_outside_the_provision_scope():
    assert _purpose_inside("pov_env_reconcile") == ""


def test_every_other_pov_job_still_takes_the_provision_scope():
    for job_type in ("pov_env_provision", "pov_env_power", "pov_env_destroy",
                     "pov_env_broker", "pov_env_add_vms", "expiry_sweep", ""):
        assert _purpose_inside(job_type) == leases.PURPOSE_PROVISION, job_type


def test_reconcile_reaches_no_adapter_write():
    """The precondition for the exemption, checked on the source: every attribute
    reconcile calls on a lab-platform adapter is a read."""
    writes = {name for name, fn in vars(pov_aws_service).items()
              if callable(fn) and name in {
                  "create_environment", "create_broker_vm", "set_runstate",
                  "wait_for_runstate", "delete_environment"}}
    assert writes, "the adapter's write surface was not found — the check is vacuous"
    tree = ast.parse(open(_RECONCILE, encoding="utf-8").read())
    called = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert not (called & writes), (
        f"pov_reconcile calls {sorted(called & writes)}; it runs with the everyday "
        f"credential (jobs_worker.EVERYDAY_JOB_TYPES) and must stay read-only — enqueue a "
        f"job for the write instead, as the suspend schedule does")


# ── Test connection ───────────────────────────────────────────────────────────

def _verify_split(patch, *, configured=True, session=None):
    patch.set(pov_cloud_aws, "configured", lambda: True)
    patch.set(pov_cloud_aws, "default_region", lambda: "us-east-2")
    patch.set(leases, "dynamic_enabled", lambda cloud: True)
    patch.set(leases, "everyday_from_federation", lambda cloud: True)
    patch.set(leases, "secret_name_for", lambda cloud, purpose: "dashboard-provision")
    patch.set(cloud_federation, "source", lambda cloud: cloud_federation.SOURCE)
    patch.set(cloud_federation, "active", lambda cloud: True)
    patch.set(wlc, "configured", lambda: configured)
    patch.set(wlc, "missing_settings", lambda: ["wlc_service_name"])
    minted = []
    patch.set(leases, "credentials", lambda *a, **kw: minted.append(a) or None)
    patch.set(leases, "refresh", lambda *a, **kw: minted.append(a) or False)

    def _session():
        if session:
            raise session
        return {"ok": True, "message": "Connected"}
    patch.set(wlc, "test_connection", _session)

    async def _to_thread(fn, *args):
        if fn is pov_cloud_aws._verify_sync:
            return 1, "arn:aws:sts::1:assumed-role/pov-read/vm-dashboard"
        return fn(*args)
    patch.set(aws_service, "_to_thread", _to_thread)
    ok, msg = asyncio.run(pov_cloud_aws.verify())
    return ok, msg, minted


def test_the_split_proves_both_legs_and_mints_nothing():
    patch = _Patch()
    try:
        ok, msg, minted = _verify_split(patch)
    finally:
        patch.undo()
    assert ok, msg
    assert "assumed-role/pov-read" in msg and "SPIFFE identity" in msg, msg
    assert "dashboard-provision" in msg and "not minted" in msg, msg
    assert not minted, f"Test connection minted a billed lease: {minted}"


def test_a_refused_session_fails_the_test_even_though_everyday_works():
    patch = _Patch()
    try:
        ok, msg, _ = _verify_split(
            patch, session=wlc.WorkloadCredentialsError("HTTP 401: Access denied for this site"))
    finally:
        patch.undo()
    assert not ok
    assert "Access denied for this site" in msg, msg


def test_an_unconfigured_wlc_names_what_is_missing():
    patch = _Patch()
    try:
        ok, msg, _ = _verify_split(patch, configured=False)
    finally:
        patch.undo()
    assert not ok and "wlc_service_name" in msg, msg


def test_verify_never_names_the_metered_mint():
    # The check is shared by every driver (pov_cloud_env.verify_jobs_lease), so it is
    # pinned where it lives rather than in each driver's thin delegate.
    src = open(os.path.join(_ROOT, "web_dashboard", "services", "pov_cloud_env.py"),
               encoding="utf-8").read()
    body = src.split("async def verify_jobs_lease", 1)[1].split("\ndef ", 1)[0]
    assert "test_connection" in body, "the shared check no longer calls the session test"
    for banned in ("generate", ".refresh(", ".credentials("):
        assert banned not in body.split('"""', 2)[-1], (
            f"the provisioning check reaches {banned!r}, a billed issuance")


def test_the_label_follows_aws_kwargs_order_on_the_lease_tier():
    """Off federation-everyday, a lease answers request-path calls, not the SPIFFE role."""
    patch = _Patch()
    try:
        patch.set(leases, "dynamic_enabled", lambda cloud: True)
        patch.set(leases, "everyday_from_federation", lambda cloud: False)
        patch.set(cloud_federation, "source", lambda cloud: cloud_federation.SOURCE)
        assert pov_cloud_aws._credential_label() == "a Workload Credentials lease"
    finally:
        patch.undo()


# ── the panel ─────────────────────────────────────────────────────────────────

def test_the_panel_model_declares_the_everyday_source():
    assert "wlc_aws_everyday_source" in PovCloudFeatureConfig.model_fields
    assert PovCloudFeatureConfig.model_fields["wlc_aws_everyday_source"].default == "lease"


def test_the_third_choice_writes_the_everyday_source_and_nothing_of_wlcs_own():
    src = open(_SETTINGS, encoding="utf-8").read()
    start = src.index("    choosePovAwsSource(")
    body = src[start:src.index("\n    },", start)]
    assert "wlc_aws_everyday_source = src === 'spiffe_wlc' ? 'federation' : 'lease'" in body
    for key in ("wlc_aws_enabled", "workload_credentials_enabled", "wlc_auth_mode",
                "wlc_aws_secret_name"):
        assert key not in body, f"the POV panel's radio writes {key}; that is WLC's panel"


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
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
