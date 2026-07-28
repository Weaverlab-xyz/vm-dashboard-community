"""Regression test: every GKE cluster must get its OWN private control-plane /28.

The module hard-coded `master_cidr = 172.16.8.0/28`, which GCP materializes as a
system-managed `gke-<cluster>-<hash>-pe-subnet` subnetwork in the cluster's VPC.
Subnet ranges may not overlap anywhere in a VPC — other regions included — so the
SECOND cluster sharing the sandbox VPC died ~40s into the apply with

    Error: Error waiting for creating GKE cluster: Conflicting IP cidr range:
    Invalid IPCidrRange: 172.16.8.0/28 conflicts with existing subnetwork
    'gke-k8s-test-f11af9c8-pe-subnet' in region 'us-central1'.

(a us-east1 cluster colliding with a us-central1 one). The fix allocates the
lowest free /28 out of `gcp_gke_master_cidr_base`, skipping ranges recorded on
this dashboard's clusters AND ranges found live in the project.

Stubs the DB / sqlalchemy / config imports so k8s_service loads without an
app/DB (same lightweight approach as test_gke_sandbox_peering). Runs under pytest
or standalone:
    python tests/test_gke_master_cidr.py
"""
import os
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# Stub the heavy module-load deps so k8s_service imports without a real DB engine.
_cfg_stub = types.ModuleType("web_dashboard.config")
_cfg_stub.settings = object()
sys.modules.setdefault("web_dashboard.config", _cfg_stub)

sys.modules.setdefault("sqlalchemy", types.ModuleType("sqlalchemy"))
_orm_stub = types.ModuleType("sqlalchemy.orm")
_orm_stub.Session = object
sys.modules.setdefault("sqlalchemy.orm", _orm_stub)

_db_stub = types.ModuleType("web_dashboard.database")
_db_stub.Job = type("Job", (), {})
_db_stub.K8sCluster = type("K8sCluster", (), {})
sys.modules.setdefault("web_dashboard.database", _db_stub)

# A controllable config_service stub — `_cfg` / `_cfg_list` read through this.
_CONFIG = {}
_cfgsvc_stub = types.ModuleType("web_dashboard.services.config_service")
_cfgsvc_stub.get = lambda key, default=None: _CONFIG.get(key, "")
sys.modules.setdefault("web_dashboard.services.config_service", _cfgsvc_stub)

_region_stub = types.ModuleType("web_dashboard.services.region_config")
_region_stub.resolve_region = lambda cloud, region: {}
_region_stub.resolve_azure_region = lambda region: {}
sys.modules["web_dashboard.services.region_config"] = _region_stub

# A controllable gcp_service stub — the allocator's live range scan reads through
# `reserved_cidrs`. Forced (not setdefault) and mirrored onto the package object,
# because `from . import gcp_service` resolves the parent attribute first.
_LIVE_CIDRS = set()
_gcpsvc_stub = types.ModuleType("web_dashboard.services.gcp_service")
_gcpsvc_stub.reserved_cidrs = lambda project="": set(_LIVE_CIDRS)
sys.modules["web_dashboard.services.gcp_service"] = _gcpsvc_stub

import web_dashboard.services as _svcpkg  # noqa: E402

_svcpkg.gcp_service = _gcpsvc_stub

from web_dashboard.services import k8s_service as k  # noqa: E402


# ── Fake ORM: only .query(Model).filter(...).all() is exercised ───────────────

class _Col:
    """A column stand-in whose comparisons are inert (the fake query ignores them)."""

    def __eq__(self, other):
        return True

    def in_(self, values):
        return True

    __hash__ = None


k.K8sCluster = type("K8sCluster", (), {"cloud": _Col(), "id": _Col()})
k.Job = type("Job", (), {"id": _Col()})


class _Cluster:
    def __init__(self, deploy_job_id="", source="provisioned"):
        self.cloud = "gcp"
        self.deploy_job_id = deploy_job_id
        self.source = source


class _Job:
    def __init__(self, job_id, tf_variables=None):
        self.id = job_id
        self.metadata_dict = {"tf_variables": tf_variables} if tf_variables is not None else {}


class _Query:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *args, **kwargs):
        return self

    def all(self):
        return list(self._rows)


class _DB:
    def __init__(self, clusters=(), jobs=()):
        self._clusters, self._jobs = list(clusters), list(jobs)

    def query(self, model):
        return _Query(self._jobs if model is k.Job else self._clusters)


def _gcp_vars(db=None, **opts):
    _CONFIG.setdefault("gcp_project", "sandbox-proj")
    return k._build_cluster_tf_variables(
        cloud="gcp", cluster_id="c-123", name="gke-demo",
        region="us-east1", opts=opts, db=db)


def _reset():
    _CONFIG.clear()
    _LIVE_CIDRS.clear()


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_first_cluster_takes_the_lowest_slot():
    _reset()
    tf = _gcp_vars(db=_DB())
    assert tf["master_cidr"] == "172.16.0.0/28"


def test_skips_the_range_another_cluster_recorded():
    _reset()
    db = _DB(clusters=[_Cluster("job-1")],
             jobs=[_Job("job-1", {"master_cidr": "172.16.0.0/28"})])
    assert _gcp_vars(db=db)["master_cidr"] == "172.16.0.16/28"


def test_legacy_cluster_without_a_recorded_range_still_holds_the_old_default():
    """The reported failure: `k8s-test` was provisioned when the module default was
    the only value, so its job records no master_cidr — but it owns 172.16.8.0/28
    in the shared VPC and a second cluster must not be handed that range."""
    _reset()
    db = _DB(clusters=[_Cluster("job-legacy")], jobs=[_Job("job-legacy", {})])
    allocated = _gcp_vars(db=db)["master_cidr"]
    assert allocated != "172.16.8.0/28"
    assert allocated == "172.16.0.0/28"


def test_registered_clusters_do_not_reserve_the_legacy_default():
    # Not dashboard-provisioned → its range is unknown here and the live scan owns
    # that case; reserving the legacy default on its behalf would be a guess.
    _reset()
    db = _DB(clusters=[_Cluster("", source="registered")])
    used = k._gke_recorded_master_cidrs(db)
    assert used == set()


def test_live_ranges_are_skipped():
    # An orphaned/hand-made cluster's pe-subnet shows up only in the live scan.
    _reset()
    _LIVE_CIDRS.update({"172.16.0.0/24", "10.99.3.0/24"})
    assert _gcp_vars(db=_DB())["master_cidr"] == "172.16.1.0/28"


def test_base_block_is_configurable():
    _reset()
    _CONFIG["gcp_gke_master_cidr_base"] = "192.168.240.0/24"
    assert _gcp_vars(db=_DB())["master_cidr"] == "192.168.240.0/28"


def test_unusable_base_falls_back_to_the_default_pool():
    _reset()
    _CONFIG["gcp_gke_master_cidr_base"] = "not-a-cidr"
    assert _gcp_vars(db=_DB())["master_cidr"] == "172.16.0.0/28"
    # Narrower than a /28 can't hold a control plane either.
    _CONFIG["gcp_gke_master_cidr_base"] = "172.20.0.0/30"
    assert _gcp_vars(db=_DB())["master_cidr"] == "172.16.0.0/28"


def test_explicit_master_cidr_wins():
    # The destroy path replays the /28 the apply recorded instead of allocating.
    _reset()
    tf = _gcp_vars(db=_DB(), master_cidr="172.16.8.0/28")
    assert tf["master_cidr"] == "172.16.8.0/28"


def test_exhausted_pool_raises_instead_of_colliding():
    _reset()
    _CONFIG["gcp_gke_master_cidr_base"] = "192.168.5.0/28"
    _LIVE_CIDRS.add("192.168.5.0/28")
    try:
        _gcp_vars(db=_DB())
    except k.K8sError as exc:
        assert "no free /28" in str(exc)
    else:
        raise AssertionError("an exhausted pool must raise, not hand out a taken range")


if __name__ == "__main__":
    failures = 0
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            try:
                _fn()
                print(f"PASS {_name}")
            except Exception as _exc:  # noqa: BLE001
                failures += 1
                print(f"FAIL {_name}: {_exc}")
    print("OK" if not failures else f"{failures} failure(s)")
    sys.exit(1 if failures else 0)
