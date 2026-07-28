"""Regression test: a failed k8s provision must tear down what Terraform created.

``terraform apply`` failing is NOT a no-op in the cloud — the provider keeps every
resource it finished before the error. Confirmed live on 2026-07-28: a GKE apply
died because the initial node pool hit GCE_STOCKOUT, the dashboard row went
``failed``, and the GKE cluster object survived in ERROR state with 0 nodes — still
holding its private control-plane /28. That subnetwork is invisible to ``gcloud
compute networks subnets list``, so nobody noticed until the *next* provision died
with "Conflicting IP cidr range … conflicts with existing subnetwork".

``run_provision_apply`` now rolls the partial deployment back before marking the row
failed. This pins the properties that matter:
  * apply failure  → terraform destroy over the same deploy dir / template / -vars;
  * rollback failure → the ORIGINAL apply error stays the primary error message,
    with a manual-cleanup note appended (a broken rollback must not mask it);
  * apply success  → no destroy, ever.

No Terraform, database, or cloud account needed. ``web_dashboard.database`` is
stubbed in sys.modules so k8s_service imports at all; everything the apply path
resolves *lazily* (config_service, job_service, terraform, terraform_provider_env,
the websocket broadcaster) is stubbed per-test and restored, so this module doesn't
poison the modules that import them for real. Runs under pytest, or standalone:
    python tests/test_k8s_provision_rollback.py
"""
import asyncio
import contextlib
import os
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


class _Settings:
    def __getattr__(self, _key):
        return ""


class _Col:
    """Stand-in for a SQLAlchemy column so ``K8sCluster.id == x`` (evaluated as a
    filter argument) doesn't blow up on the bare stub class."""
    def __eq__(self, other):
        return ("eq", other)


class _K8sCluster:
    id = name = cloud = status = source = region = created_by = deploy_job_id = _Col()

    def __init__(self, **kw):
        self.__dict__.update(kw)


def _install_import_stubs():
    """Only what k8s_service needs at *import* time (it does ``from ..database
    import Job, K8sCluster`` at module level and ``from ..config import settings``
    inside ``_cfg``)."""
    confmod = types.ModuleType("web_dashboard.config")
    confmod.settings = _Settings()
    sys.modules.setdefault("web_dashboard.config", confmod)

    dbmod = types.ModuleType("web_dashboard.database")
    dbmod.Job = type("Job", (), {})
    dbmod.K8sCluster = _K8sCluster
    sys.modules["web_dashboard.database"] = dbmod


_install_import_stubs()
try:
    from web_dashboard.services import k8s_service as svc
except Exception as exc:  # pragma: no cover — skip if other app deps are missing
    try:
        import pytest
        pytest.skip(f"k8s_service import unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)


# ── recorders + knobs the tests drive the fakes with ─────────────────────────
CALLS = {"apply": [], "destroy": [], "failed": [], "completed": []}
GOOD_OUTPUTS = {"endpoint": "https://1.2.3.4", "ca_certificate": "Y2E=",
                "cluster_name": "k8s-test", "nat_public_ip": "203.0.113.7"}
BEHAVIOUR = {"apply_exc": None, "destroy_exc": None, "outputs": GOOD_OUTPUTS}


class _TerraformError(Exception):
    pass


class _JobCancelled(Exception):
    pass


async def _fake_apply(deploy_dir, variables, template_dir=None, env=None, on_line=None):
    CALLS["apply"].append({"deploy_dir": deploy_dir, "variables": variables,
                           "template_dir": template_dir, "env": env})
    if on_line:
        await on_line("google_container_cluster.this: creating...")
    if BEHAVIOUR["apply_exc"]:
        raise BEHAVIOUR["apply_exc"]
    return dict(BEHAVIOUR["outputs"])


async def _fake_destroy(deploy_dir, env=None, template_dir=None, variables=None, on_line=None):
    CALLS["destroy"].append({"deploy_dir": deploy_dir, "variables": variables,
                             "template_dir": template_dir, "env": env})
    if on_line:
        # The rollback's on_line must not re-check cancellation: a cancelled apply
        # is one of the ways we get here, and aborting on the first streamed line
        # would leave behind the very orphan the rollback exists to remove.
        await on_line("google_container_cluster.this: destroying...")
    if BEHAVIOUR["destroy_exc"]:
        raise BEHAVIOUR["destroy_exc"]


def _lazy_stubs():
    """Fresh stubs for every module the provision path imports at call time."""
    mods = {}

    cfg = types.ModuleType("web_dashboard.services.config_service")
    cfg.get = lambda key: ""
    cfg.set = lambda key, value: None
    mods["web_dashboard.services.config_service"] = cfg

    js = types.ModuleType("web_dashboard.services.job_service")
    js.set_running = lambda db, job_id: None
    js.set_completed = lambda db, job_id, result=None: CALLS["completed"].append(job_id)
    js.set_failed = lambda db, job_id, error: CALLS["failed"].append(error)
    js.update_progress = lambda db, job_id, pct, msg: None
    # The apply's on_line cancel-checks; the rollback's must not (see _fake_destroy).
    js.cancel_check = lambda job_id, state, interval_s=5.0: None
    mods["web_dashboard.services.job_service"] = js

    tf = types.ModuleType("web_dashboard.services.terraform")
    tf.TerraformError, tf.JobCancelled = _TerraformError, _JobCancelled
    tf.apply, tf.destroy = _fake_apply, _fake_destroy
    mods["web_dashboard.services.terraform"] = tf

    tpe = types.ModuleType("web_dashboard.services.terraform_provider_env")
    tpe.provider_env = lambda cloud: {"PROVIDER": cloud}
    mods["web_dashboard.services.terraform_provider_env"] = tpe

    # Only the success tail reaches this one (a best-effort GCE firewall refresh).
    rns = types.ModuleType("web_dashboard.services.rancher_node_service")

    async def _refresh(db):
        return None
    rns.refresh_rancher_firewall = _refresh
    mods["web_dashboard.services.rancher_node_service"] = rns

    ws = types.ModuleType("web_dashboard.api.websocket")

    async def _broadcast(job_id, pct, message, log_line=None):
        return None
    ws.broadcast_progress = _broadcast
    mods["web_dashboard.api.websocket"] = ws

    return mods


@contextlib.contextmanager
def _stubbed(mods):
    """Install sys.modules stubs for one call, then put the real entries back —
    these are resolved lazily inside the function under test, so they must be live
    while it runs, and gone afterwards so a whole-suite pytest run isn't poisoned."""
    prev = {name: sys.modules.get(name) for name in mods}
    sys.modules.update(mods)
    try:
        yield
    finally:
        for name, mod in prev.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod


class _Query:
    def __init__(self, row):
        self._row = row

    def filter(self, *a, **k):
        return self

    def first(self):
        return self._row


class _FakeDB:
    def __init__(self, row):
        self._row = row

    def query(self, *a, **k):
        return _Query(self._row)

    def commit(self):
        pass


TF_VARS = {"cluster_name": "k8s-test", "project": "proj-x", "region": "us-central1"}


def _run(cloud="gcp", apply_exc=None, destroy_exc=None, outputs=GOOD_OUTPUTS):
    """Drive run_provision_apply once; return (row, job_error_or_None)."""
    for v in CALLS.values():
        v.clear()
    BEHAVIOUR.update(apply_exc=apply_exc, destroy_exc=destroy_exc, outputs=outputs)
    row = _K8sCluster(id="cid-1", cloud=cloud, name="test", region="us-central1",
                      status="provisioning", deploy_job_id="job-1")
    with _stubbed(_lazy_stubs()):
        asyncio.run(svc.run_provision_apply(
            _FakeDB(row), cluster_id="cid-1", job_id="job-1", cloud=cloud,
            tf_variables=dict(TF_VARS)))
    return row, (CALLS["failed"][0] if CALLS["failed"] else None)


# ── the bug this file guards ─────────────────────────────────────────────────

def test_apply_failure_destroys_the_partial_deployment():
    boom = _TerraformError("Error waiting for creation of NodePool: GCE_STOCKOUT")
    row, error = _run(apply_exc=boom)

    assert len(CALLS["destroy"]) == 1, "a failed apply must roll back what it created"
    d = CALLS["destroy"][0]
    # Same deploy dir the apply used (that's where the state referencing the
    # half-built cluster lives), the cloud's module, and the same -var set —
    # terraform destroy evaluates the config and errors on a missing required var.
    assert d["deploy_dir"] == svc._deploy_dir("job-1")
    assert d["deploy_dir"] == CALLS["apply"][0]["deploy_dir"]
    assert d["template_dir"] == svc._cluster_template_dir("gcp")
    assert d["variables"] == TF_VARS
    assert d["env"] == {"PROVIDER": "gcp"}
    # The row still ends failed — it's the operator's record of the attempt and the
    # handle Delete uses to re-run the teardown.
    assert row.status == "failed"
    assert "GCE_STOCKOUT" in error


def test_rollback_success_is_reported_but_keeps_the_apply_error_primary():
    row, error = _run(apply_exc=_TerraformError("GCE_STOCKOUT"))
    assert error.startswith("GCE_STOCKOUT"), "the apply error must lead the message"
    assert "rollback" in error.lower()
    assert "destroyed" in error.lower()
    assert row.status == "failed"


def test_rollback_failure_is_non_fatal_and_flags_manual_cleanup():
    # A destroy that itself fails must not mask the apply error, and must not
    # escape run_provision_apply (the job would otherwise never be marked failed).
    row, error = _run(apply_exc=_TerraformError("GCE_STOCKOUT"),
                      destroy_exc=_TerraformError("quota exceeded on delete"))
    assert error.startswith("GCE_STOCKOUT"), "the apply error must stay primary"
    assert "quota exceeded on delete" in error, "the rollback failure must be surfaced"
    assert "MANUAL CLEANUP REQUIRED" in error
    assert row.status == "failed"


def test_rollback_runs_when_the_operator_cancels_the_apply():
    # Cancel raises JobCancelled out of the apply's on_line — the partial cluster
    # is just as orphaned as on a hard failure, so it must be rolled back too.
    row, error = _run(apply_exc=_JobCancelled("job job-1 cancelled"))
    assert len(CALLS["destroy"]) == 1
    assert row.status == "failed"
    assert "cancelled" in error


def test_missing_outputs_also_roll_back():
    # The apply "succeeded" but the module returned no endpoint/CA: the cluster is
    # real and billing, but unusable — tear it down rather than orphan it.
    row, error = _run(outputs={})
    assert len(CALLS["destroy"]) == 1
    assert row.status == "failed"
    assert "endpoint" in error


def test_rollback_is_per_cloud():
    # The destroy must target the failing cloud's module + provider env, not GCP's.
    _run(cloud="aws", apply_exc=_TerraformError("InsufficientCapacity"))
    d = CALLS["destroy"][0]
    assert d["template_dir"] == svc._cluster_template_dir("aws")
    assert d["env"] == {"PROVIDER": "aws"}


# ── the regression the rollback could introduce ──────────────────────────────

def test_successful_provision_never_destroys():
    row, error = _run()
    assert error is None and CALLS["completed"] == ["job-1"]
    assert CALLS["destroy"] == [], "a cluster that came up must never be rolled back"
    assert row.status == "registered"
    assert row.api_server == "https://1.2.3.4"
    assert row.egress_ip == "203.0.113.7"


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
