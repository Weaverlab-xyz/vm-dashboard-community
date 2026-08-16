"""Unit tests for multi-region Rancher management-node placement.

Covers the two pieces that decide WHERE the node lands:

  * ``rancher_node_service._node_params(region, zone)`` — region/zone/subnet
    resolution. Exercises the region pick, the bare-redeploy back-compat path
    (single-region installs unchanged), the "never inherit the default region's
    zone / subnet" guards (the us-east1 cross-region-leak trap), and stickiness to
    the persisted ``gcp_rancher_zone`` after a relocation.
  * ``gcp_service._rancher_candidate_zones`` — preferred-first, same-region-only
    UP-zone ordering + the enumeration-failure fallback.
  * ``gcp_service._is_zone_capacity_error`` — only ZONE_RESOURCE_POOL_EXHAUSTED /
    "does not have enough resources" count as retryable.

Uses the REAL region_config / region_catalog with a controllable config_service
stub; the heavy service deps (job_service, rancher_service, database, httpx) are
stubbed so the module imports without an app/DB, and ``google.auth`` is faked so the
zone lister is deterministic offline. Runs under pytest or standalone:

    python tests/test_rancher_multiregion.py
"""
import json
import os
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# ── Stub settings (attrs read via `X or settings.X`; others fall back to "") ──────
_settings = types.SimpleNamespace(
    gcp_project_id="", gcp_network="", gcp_region="us-central1", gcp_zone="us-central1-a",
    gcp_subnetwork="", gcp_jumpoint_subnetwork="",
    gcp_rancher_name="rancher-server", gcp_rancher_image="rancher/rancher:latest",
    gcp_rancher_machine_type="e2-medium", gcp_rancher_boot_disk_gb=30,
    gcp_rancher_network_tag="rancher",
)
_cfg_mod = types.ModuleType("web_dashboard.config")
_cfg_mod.settings = _settings
sys.modules["web_dashboard.config"] = _cfg_mod

# ── Controllable config_service (real region_config / region_catalog read this) ──
_CONFIG: dict = {}
_cfgsvc = types.ModuleType("web_dashboard.services.config_service")
_cfgsvc.get = lambda key, default=None: _CONFIG.get(key, "")
_cfgsvc.set = lambda key, val: _CONFIG.__setitem__(key, val)
_cfgsvc.get_bool = lambda key, default=False: str(_CONFIG.get(key, default)).lower() in ("1", "true", "yes")
sys.modules["web_dashboard.services.config_service"] = _cfgsvc

# ── Stub the heavy service deps rancher_node_service imports at module load ───────
for _name in ("job_service", "rancher_service"):
    sys.modules[f"web_dashboard.services.{_name}"] = types.ModuleType(f"web_dashboard.services.{_name}")
_db_mod = types.ModuleType("web_dashboard.database")
_db_mod.SessionLocal = object
sys.modules["web_dashboard.database"] = _db_mod
sys.modules.setdefault("httpx", types.ModuleType("httpx"))


# ── Fake google.auth so gcp_service._rancher_candidate_zones is deterministic ─────
class _FakeResp:
    def __init__(self, data):
        self._d = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._d


class _FakeSession:
    payload: dict = {"items": []}
    raise_on_get: bool = False

    def __init__(self, creds):
        pass

    def get(self, url, timeout=None):
        if _FakeSession.raise_on_get:
            raise RuntimeError("boom")
        return _FakeResp(_FakeSession.payload)


_g = types.ModuleType("google")
_ga = types.ModuleType("google.auth")
_gat = types.ModuleType("google.auth.transport")
_gatr = types.ModuleType("google.auth.transport.requests")
_gatr.AuthorizedSession = _FakeSession
sys.modules["google"] = _g
sys.modules["google.auth"] = _ga
sys.modules["google.auth.transport"] = _gat
sys.modules["google.auth.transport.requests"] = _gatr

from web_dashboard.services import gcp_service           # noqa: E402
from web_dashboard.services import rancher_node_service  # noqa: E402

_node_params = rancher_node_service._node_params


# ── _node_params ─────────────────────────────────────────────────────────────────

def test_default_region_backcompat():
    # No region arg + flat keys only → identical to the pre-multi-region behavior:
    # region derived from gcp_zone, jumpoint subnet preferred over the VM subnet.
    _CONFIG.clear()
    _CONFIG.update({
        "gcp_project_id": "proj", "gcp_zone": "us-central1-a",
        "gcp_network": "sb-vpc", "gcp_subnetwork": "sb-vm",
        "gcp_jumpoint_subnetwork": "sb-jump",
    })
    p = _node_params()
    assert p["region"] == "us-central1"
    assert p["zone"] == "us-central1-a"
    assert p["network"] == "sb-vpc"
    assert p["subnetwork"] == "sb-jump"   # jumpoint preferred


def test_region_pick_uses_region_config():
    _CONFIG.clear()
    _CONFIG.update({
        "gcp_project_id": "proj", "gcp_zone": "us-central1-a", "gcp_network": "sb-vpc",
        "gcp_jumpoint_subnetwork": "sb-jump-central",   # must NOT leak into us-east1
        "gcp_region_configs": json.dumps({
            "us-east1": {"zone": "us-east1-b", "subnetwork": "sb-vm-east",
                         "jumpoint_subnetwork": "sb-jump-east"},
        }),
    })
    p = _node_params(region="us-east1")
    assert p["region"] == "us-east1"
    assert p["zone"] == "us-east1-b"
    assert p["subnetwork"] == "sb-jump-east"   # region's jumpoint subnet
    assert p["network"] == "sb-vpc"            # VPC name is global


def test_region_pick_never_leaks_default_subnet():
    # Region entry has ONLY a VM subnet; the default region's jumpoint subnet must
    # NOT bleed in (it wouldn't exist in us-east1 → insert would fail).
    _CONFIG.clear()
    _CONFIG.update({
        "gcp_project_id": "proj", "gcp_jumpoint_subnetwork": "sb-jump-central",
        "gcp_region_configs": json.dumps({"us-east1": {"subnetwork": "sb-vm-east"}}),
    })
    p = _node_params(region="us-east1")
    assert p["subnetwork"] == "sb-vm-east"


def test_region_pick_without_zone_autopicks():
    # No region-config zone → blank zone (launcher auto-picks a valid us-east1 zone);
    # the default region's gcp_zone must NOT be inherited (the -a trap / wrong region).
    _CONFIG.clear()
    _CONFIG.update({
        "gcp_project_id": "proj", "gcp_zone": "us-central1-a",
        "gcp_region_configs": json.dumps({"us-east1": {"subnetwork": "sb-vm-east"}}),
    })
    p = _node_params(region="us-east1")
    assert p["region"] == "us-east1"
    assert p["zone"] == ""


def test_explicit_zone_in_region_wins():
    _CONFIG.clear()
    _CONFIG.update({
        "gcp_project_id": "proj",
        "gcp_region_configs": json.dumps({"us-east1": {"zone": "us-east1-b",
                                                       "subnetwork": "sb-vm-east"}}),
    })
    p = _node_params(region="us-east1", zone="us-east1-c")
    assert p["zone"] == "us-east1-c"


def test_bare_redeploy_sticky_to_persisted_zone():
    # After a relocation the deployed zone is persisted to gcp_rancher_zone; a later
    # bare redeploy stays in that region (and uses that region's subnet).
    _CONFIG.clear()
    _CONFIG.update({
        "gcp_project_id": "proj", "gcp_rancher_zone": "us-east1-b",
        "gcp_zone": "us-central1-a",
        "gcp_region_configs": json.dumps({"us-east1": {"subnetwork": "sb-vm-east"}}),
    })
    p = _node_params()
    assert p["region"] == "us-east1"
    assert p["zone"] == "us-east1-b"
    assert p["subnetwork"] == "sb-vm-east"


# ── _rancher_candidate_zones ───────────────────────────────────────────────────────

def test_candidate_zones_preferred_first_same_region_only():
    _FakeSession.raise_on_get = False
    _FakeSession.payload = {"items": [
        {"name": "us-east1-b", "status": "UP"},
        {"name": "us-east1-c", "status": "UP"},
        {"name": "us-east1-d", "status": "DOWN"},     # excluded (not UP)
        {"name": "us-central1-a", "status": "UP"},    # excluded (other region)
    ]}
    zones = gcp_service._rancher_candidate_zones("proj", "us-east1", "us-east1-c", creds=object())
    assert zones == ["us-east1-c", "us-east1-b"]


def test_candidate_zones_blank_preferred_sorted():
    _FakeSession.raise_on_get = False
    _FakeSession.payload = {"items": [
        {"name": "us-east1-c", "status": "UP"},
        {"name": "us-east1-b", "status": "UP"},
    ]}
    zones = gcp_service._rancher_candidate_zones("proj", "us-east1", "", creds=object())
    assert zones == ["us-east1-b", "us-east1-c"]


def test_candidate_zones_enumeration_failure_fallback():
    _FakeSession.raise_on_get = True
    assert gcp_service._rancher_candidate_zones("proj", "us-east1", "us-east1-b", creds=object()) == ["us-east1-b"]
    assert gcp_service._rancher_candidate_zones("proj", "us-east1", "", creds=object()) == []
    _FakeSession.raise_on_get = False


# ── _is_zone_capacity_error ────────────────────────────────────────────────────────

def test_is_zone_capacity_error():
    assert gcp_service._is_zone_capacity_error(Exception(
        "503 SERVICE UNAVAILABLE ZONE_RESOURCE_POOL_EXHAUSTED: the zone ..."))
    assert gcp_service._is_zone_capacity_error(Exception(
        "The zone 'us-central1-c' does not have enough resources"))
    assert not gcp_service._is_zone_capacity_error(Exception("QUOTA_EXCEEDED: quota 'CPUS'"))
    assert not gcp_service._is_zone_capacity_error(Exception(
        "Required 'compute.instances.create' permission"))


if __name__ == "__main__":
    _tests = [v for k, v in sorted(globals().items())
              if k.startswith("test_") and callable(v)]
    _failures = 0
    for _t in _tests:
        try:
            _t()
            print(f"PASS {_t.__name__}")
        except Exception as _e:  # noqa: BLE001
            _failures += 1
            print(f"FAIL {_t.__name__}: {_e!r}")
    print(f"\n{len(_tests) - _failures}/{len(_tests)} passed")
    sys.exit(1 if _failures else 0)
