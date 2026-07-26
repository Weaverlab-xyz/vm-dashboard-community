"""Unit tests for the managed Portainer node (deploy placement + firewall).

Covers the two pure-logic pieces of ``portainer_node_service`` that decide WHERE
the node lands and WHO may reach it:

  * ``_node_params(region, zone)`` — region/zone/subnet resolution. Mirrors the
    Rancher node's contract (see test_rancher_multiregion.py): the region pick,
    the bare-redeploy back-compat path, the "never inherit the default region's
    zone / subnet" guard, and stickiness to the persisted ``gcp_portainer_zone``.
  * ``_allowed_cidrs`` / ``_dashboard_cidr`` / ``firewall_status`` — the merged
    source set is fail-closed (empty ⇒ nothing opened) unless
    ``gcp_portainer_allow_open``, and a bare IP is normalized to /32.

Also pins ``gcp_service._portainer_container_spec_yaml`` — the konlet declaration
must keep /data on a host path and must NOT request privileged (unlike Rancher).

Uses the REAL region_config / region_catalog with a controllable config_service
stub; heavy deps are stubbed so the module imports without an app/DB. Runs under
pytest or standalone:

    python tests/test_portainer_node_service.py
"""
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
    gcp_portainer_name="portainer-server",
    gcp_portainer_image="portainer/portainer-ce:latest",
    gcp_portainer_machine_type="e2-small", gcp_portainer_boot_disk_gb=20,
    gcp_portainer_network_tag="portainer", portainer_ready_timeout_s=300,
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

# ── Stub the heavy deps portainer_node_service imports at module load ────────────
for _name in ("job_service", "portainer_service"):
    sys.modules[f"web_dashboard.services.{_name}"] = types.ModuleType(f"web_dashboard.services.{_name}")
_db_mod = types.ModuleType("web_dashboard.database")
_db_mod.SessionLocal = object
sys.modules["web_dashboard.database"] = _db_mod
sys.modules.setdefault("httpx", types.ModuleType("httpx"))

# portainer_node_service imports _generate_admin_password from rancher_node_service,
# which drags in rancher_service — stub it the same way.
sys.modules.setdefault(
    "web_dashboard.services.rancher_service",
    types.ModuleType("web_dashboard.services.rancher_service"))

from web_dashboard.services import gcp_service              # noqa: E402
from web_dashboard.services import portainer_node_service   # noqa: E402

_node_params = portainer_node_service._node_params


def _reset(**cfg):
    _CONFIG.clear()
    _CONFIG.update(cfg)


# ── _node_params ─────────────────────────────────────────────────────────────────

def test_default_region_backcompat():
    # No region arg + flat keys only → region derived from gcp_zone, and the
    # single-region install behaves exactly as before multi-region existed.
    _reset(gcp_project_id="proj", gcp_zone="us-central1-a")
    p = _node_params()
    assert p["region"] == "us-central1", p
    assert p["zone"] == "us-central1-a", p
    assert p["name"] == "portainer-server"
    assert p["machine_type"] == "e2-small"
    assert p["network_tag"] == "portainer"


def test_explicit_region_never_inherits_default_zone():
    # Picking a region with no region-config must NOT leak the default region's
    # zone (the us-east1 cross-region trap) — blank zone lets the launcher pick.
    _reset(gcp_project_id="proj", gcp_zone="us-central1-a")
    p = _node_params(region="us-east1")
    assert p["region"] == "us-east1", p
    assert p["zone"] == "", p


def test_explicit_zone_sets_region():
    _reset(gcp_project_id="proj", gcp_zone="us-central1-a")
    p = _node_params(zone="europe-west1-b")
    assert p["region"] == "europe-west1", p
    assert p["zone"] == "europe-west1-b", p


def test_out_of_region_zone_is_dropped():
    # A zone that doesn't sit in the chosen region is ignored, not obeyed.
    _reset(gcp_project_id="proj", gcp_zone="us-central1-a")
    p = _node_params(region="us-east1", zone="us-central1-a")
    assert p["region"] == "us-east1", p
    assert p["zone"] == "", p


def test_bare_redeploy_is_sticky_to_persisted_zone():
    # After a relocation the deploy persists gcp_portainer_zone; a bare redeploy
    # must stay there rather than snapping back to the default region.
    _reset(gcp_project_id="proj", gcp_zone="us-central1-a",
           gcp_portainer_zone="europe-west1-c")
    p = _node_params()
    assert p["region"] == "europe-west1", p
    assert p["zone"] == "europe-west1-c", p


def test_boot_disk_falls_back_on_garbage():
    _reset(gcp_project_id="proj", gcp_zone="us-central1-a",
           gcp_portainer_boot_disk_gb="not-a-number")
    assert _node_params()["boot_disk_gb"] == 20


# ── firewall source set ──────────────────────────────────────────────────────────

def test_firewall_closed_by_default():
    # Fail closed: no manual CIDRs and no detected dashboard egress ⇒ nothing opened.
    _reset(gcp_project_id="proj", gcp_zone="us-central1-a")
    st = portainer_node_service.firewall_status()
    assert st["merged"] == [], st
    assert st["opened"] is False, st
    assert st["ports"] == ["9443", "8000"], st


def test_allow_open_opens_world_only_when_opted_in():
    _reset(gcp_project_id="proj", gcp_zone="us-central1-a",
           gcp_portainer_allow_open="1")
    st = portainer_node_service.firewall_status()
    assert st["merged"] == ["0.0.0.0/0"], st
    assert st["opened"] is True, st


def test_manual_cidrs_and_dashboard_cidr_merge_dedup_sorted():
    _reset(gcp_project_id="proj", gcp_zone="us-central1-a",
           portainer_allowed_source_cidrs=" 10.0.0.0/8 , 203.0.113.5/32 ,10.0.0.0/8 ",
           portainer_dashboard_egress_cidr="203.0.113.5")  # bare IP → /32, dedupes
    st = portainer_node_service.firewall_status()
    assert st["merged"] == ["10.0.0.0/8", "203.0.113.5/32"], st
    assert st["dashboard_egress_ip"] == "203.0.113.5/32", st
    # manual_cidrs echoes the CSV verbatim (duplicate included) so the operator sees
    # exactly what they typed; only `merged` is deduped. Same as Rancher's contract.
    assert st["manual_cidrs"] == ["10.0.0.0/8", "203.0.113.5/32", "10.0.0.0/8"], st


def test_jumpoint_cidr_requires_the_web_jump_to_be_enabled():
    # An egress IP left over from a previous deploy must NOT open the firewall while
    # the Web Jump is off — the /32 is only justified by an active broker.
    _reset(gcp_project_id="proj", gcp_zone="us-central1-a",
           portainer_ui_jumpoint_egress_ip="198.51.100.9")
    assert portainer_node_service._jumpoint_cidr() == []
    _CONFIG["portainer_ui_web_jump_enabled"] = "1"
    assert portainer_node_service._jumpoint_cidr() == ["198.51.100.9/32"]


def test_jumpoint_cidr_absent_when_ip_unknown():
    # A pre-existing operator Jumpoint can't be auto-detected — enabled but no IP
    # must stay empty rather than emitting a bogus "/32".
    _reset(gcp_project_id="proj", gcp_zone="us-central1-a",
           portainer_ui_web_jump_enabled="1")
    assert portainer_node_service._jumpoint_cidr() == []


def test_jumpoint_cidr_joins_the_merged_firewall_set():
    _reset(gcp_project_id="proj", gcp_zone="us-central1-a",
           portainer_ui_web_jump_enabled="1",
           portainer_ui_jumpoint_egress_ip="198.51.100.9",
           portainer_allowed_source_cidrs="10.0.0.0/8",
           portainer_dashboard_egress_cidr="203.0.113.5")
    st = portainer_node_service.firewall_status()
    assert st["merged"] == ["10.0.0.0/8", "198.51.100.9/32", "203.0.113.5/32"], st
    assert st["jumpoint_egress_ip"] == "198.51.100.9/32", st
    assert st["opened"] is True, st


def test_manual_cidrs_beat_allow_open():
    # allow_open only applies when the CSV is empty; an explicit list wins.
    _reset(gcp_project_id="proj", gcp_zone="us-central1-a",
           gcp_portainer_allow_open="1",
           portainer_allowed_source_cidrs="198.51.100.0/24")
    assert portainer_node_service._allowed_cidrs() == ["198.51.100.0/24"]


# ── konlet container declaration ─────────────────────────────────────────────────

def test_container_spec_is_unprivileged_with_data_volume():
    import yaml
    spec = yaml.safe_load(
        gcp_service._portainer_container_spec_yaml("portainer/portainer-ce:latest"))
    container = spec["spec"]["containers"][0]
    assert container["image"] == "portainer/portainer-ce:latest"
    # Unlike Rancher, Portainer must NOT run privileged.
    assert "securityContext" not in container, container
    # /data must be backed by a host path so a container restart keeps state.
    assert container["volumeMounts"][0]["mountPath"] == "/data"
    vol = spec["spec"]["volumes"][0]
    assert vol["hostPath"]["path"] == gcp_service._PORTAINER_DATA_HOSTPATH
    assert spec["spec"]["restartPolicy"] == "Always"


def test_portainer_url_uses_9443():
    assert gcp_service._portainer_url("203.0.113.9") == "https://203.0.113.9:9443"
    assert gcp_service._portainer_url("") == ""


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
