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
    gcp_portainer_data_disk_gb=10,
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
# get_raw / is_reference are how a caller tells a stored literal from a vault
# reference (aws_sm://, bt_safe://, ...) — the mint path refuses to overwrite one.
_cfgsvc.get_raw = lambda key, default="": _CONFIG.get(key, default) or ""
_cfgsvc.is_reference = lambda raw: "://" in (raw or "")
sys.modules["web_dashboard.services.config_service"] = _cfgsvc

# ── Stub the heavy deps portainer_node_service imports at module load ────────────
for _name in ("job_service", "portainer_service"):
    sys.modules[f"web_dashboard.services.{_name}"] = types.ModuleType(f"web_dashboard.services.{_name}")
_db_mod = types.ModuleType("web_dashboard.database")
# A real-enough session: _jumpoint_cidrs opens one when the caller has none.
_db_mod.SessionLocal = lambda: types.SimpleNamespace(close=lambda: None)
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


def test_recently_seen_egress_addresses_stay_admitted():
    """One pinned /32 is a snapshot of an address that is not guaranteed stable: a host
    behind a SNAT pool with no fixed outbound address (an ACA environment with no NAT
    Gateway) egresses from whichever address the platform picks per connection. The
    readiness poll needs ONE lucky attempt and passes; the bootstrap needs several
    consecutive ones and gets dropped — "serving a second ago, unreachable now"
    (live 2026-09-17). Admitting the recent set closes that window."""
    _reset(gcp_project_id="proj", gcp_zone="us-central1-a",
           portainer_dashboard_egress_cidr="172.193.115.158/32",
           portainer_dashboard_egress_recent="172.193.115.158/32,135.237.231.126/32")
    st = portainer_node_service.firewall_status()
    assert st["merged"] == ["135.237.231.126/32", "172.193.115.158/32"], st
    # The CURRENT pin still leads the singular field the Settings panel reads.
    assert st["dashboard_egress_ip"] == "172.193.115.158/32", st


def test_recent_egress_list_is_bounded_and_most_recent_first():
    """A genuinely roaming address must not grow the allow-list without limit."""
    from web_dashboard.services import managed_node_service as mns
    _reset()
    spec = portainer_node_service._SPEC
    for i in range(7):
        mns._record_recent_egress(spec, f"198.51.100.{i}/32")
    recent = mns._recent_egress_cidrs(spec)
    assert len(recent) == mns._RECENT_EGRESS_MAX, recent
    assert recent[0] == "198.51.100.6/32", recent
    # Re-seeing an address promotes it rather than duplicating it.
    mns._record_recent_egress(spec, "198.51.100.4/32")
    recent = mns._recent_egress_cidrs(spec)
    assert recent[0] == "198.51.100.4/32", recent
    assert len(recent) == len(set(recent)) == mns._RECENT_EGRESS_MAX, recent


def test_jumpoint_cidrs_requires_the_web_jump_to_be_enabled():
    # An egress IP left over from a previous deploy must NOT open the firewall while
    # the Web Jump is off — the /32 is only justified by an active broker.
    _reset(gcp_project_id="proj", gcp_zone="us-central1-a",
           portainer_ui_jumpoint_egress_ip="198.51.100.9")
    assert portainer_node_service._jumpoint_cidrs() == []
    _CONFIG["portainer_ui_web_jump_enabled"] = "1"
    assert portainer_node_service._jumpoint_cidrs() == ["198.51.100.9/32"]


def test_jumpoint_cidrs_absent_when_ip_unknown():
    # A pre-existing operator Jumpoint can't be auto-detected — enabled but no IP
    # must stay empty rather than emitting a bogus "/32".
    _reset(gcp_project_id="proj", gcp_zone="us-central1-a",
           portainer_ui_web_jump_enabled="1")
    assert portainer_node_service._jumpoint_cidrs() == []


def test_jumpoint_cidrs_joins_the_merged_firewall_set():
    _reset(gcp_project_id="proj", gcp_zone="us-central1-a",
           portainer_ui_web_jump_enabled="1",
           portainer_ui_jumpoint_egress_ip="198.51.100.9",
           portainer_allowed_source_cidrs="10.0.0.0/8",
           portainer_dashboard_egress_cidr="203.0.113.5")
    st = portainer_node_service.firewall_status()
    assert st["merged"] == ["10.0.0.0/8", "198.51.100.9/32", "203.0.113.5/32"], st
    assert st["jumpoint_egress_ip"] == "198.51.100.9/32", st
    assert st["opened"] is True, st


def test_the_entitle_adapters_subnet_joins_the_merged_firewall_set():
    """The portainer_access adapter is a VPC-attached Cloud Function that reaches this
    node at its INTERNAL IP, and a source-restricted firewall applies to intra-VPC
    ingress too — so without its range in the merged set every Entitle grant times
    out against a node that looks perfectly healthy."""
    _reset(gcp_project_id="proj", gcp_zone="us-central1-a",
           portainer_allowed_source_cidrs="10.0.0.0/8",
           portainer_dashboard_egress_cidr="203.0.113.5",
           portainer_adapter_source_cidr="10.128.0.0/20")
    st = portainer_node_service.firewall_status()
    assert "10.128.0.0/20" in st["merged"], st
    # Attributed rather than anonymous, so the Settings readout can say what it is for.
    assert st["adapter_cidrs"] == ["10.128.0.0/20"], st


def test_several_adapter_subnets_are_admitted():
    """AWS attaches a function to a LIST of subnets (one per AZ), so the key is a CSV
    and a single-value read would admit only the first."""
    _reset(gcp_project_id="proj", gcp_zone="us-central1-a",
           portainer_adapter_source_cidr="10.0.1.0/24, 10.0.2.0/24")
    st = portainer_node_service.firewall_status()
    assert st["adapter_cidrs"] == ["10.0.1.0/24", "10.0.2.0/24"], st
    assert st["merged"] == ["10.0.1.0/24", "10.0.2.0/24"], st


def test_no_adapter_leaves_the_firewall_exactly_as_it_was():
    """The key is runtime-set and blank on every install that has never paired, so it
    must not widen — or close — anything by existing."""
    _reset(gcp_project_id="proj", gcp_zone="us-central1-a",
           portainer_allowed_source_cidrs="10.0.0.0/8")
    st = portainer_node_service.firewall_status()
    assert st["adapter_cidrs"] == [], st
    assert st["merged"] == ["10.0.0.0/8"], st


def test_a_user_deployed_gateway_is_admitted_from_the_registry():
    """The gap that let a hand-deployed gateway sit outside the allow list: only the
    single remembered "shared gateway" IP was ever admitted, while every gateway in the
    cloud joins the same PRA Gateway cluster and may broker the session."""
    from web_dashboard.services import gateway_service
    _reset(gcp_project_id="proj", gcp_zone="us-central1-a",
           portainer_ui_web_jump_enabled="1")   # no remembered shared IP at all
    original = gateway_service.live_egress_ips
    gateway_service.live_egress_ips = lambda db, cloud: ["198.51.100.20", "198.51.100.21"]
    try:
        assert portainer_node_service._jumpoint_cidrs(db=object()) == [
            "198.51.100.20/32", "198.51.100.21/32"]
        # …and it merges with the legacy key rather than replacing it.
        _CONFIG["portainer_ui_jumpoint_egress_ip"] = "198.51.100.9"
        assert portainer_node_service._jumpoint_cidrs(db=object()) == [
            "198.51.100.20/32", "198.51.100.21/32", "198.51.100.9/32"]
    finally:
        gateway_service.live_egress_ips = original


def test_a_registry_read_failure_does_not_close_the_firewall():
    """Fail-closed is right for an unconfigured allow list, but not for a transient DB
    error: dropping the known sources would lock the operator out of a working node."""
    from web_dashboard.services import gateway_service
    _reset(gcp_project_id="proj", gcp_zone="us-central1-a",
           portainer_ui_web_jump_enabled="1",
           portainer_ui_jumpoint_egress_ip="198.51.100.9")
    original = gateway_service.live_egress_ips

    def _boom(db, cloud):
        raise RuntimeError("database is down")

    gateway_service.live_egress_ips = _boom
    try:
        assert portainer_node_service._jumpoint_cidrs(db=object()) == ["198.51.100.9/32"]
    finally:
        gateway_service.live_egress_ips = original


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


def test_container_spec_initializes_the_admin_at_boot():
    """Portainer shuts its first-run init window a short time after the container
    starts and then fences off the WHOLE API ("administrator initialization timeout"),
    leaving a node nobody can log into. Passing the bcrypt hash at launch is what
    removes the race: the admin exists before anything has to reach the node."""
    import yaml
    spec = yaml.safe_load(gcp_service._portainer_container_spec_yaml(
        "portainer/portainer-ce:latest", "$2b$12$abcdefghijklmnopqrstuv"))
    container = spec["spec"]["containers"][0]
    assert container["args"] == ["--admin-password", "$2b$12$abcdefghijklmnopqrstuv"], container
    # No hash (bcrypt unavailable) must not emit an empty flag — Portainer would
    # refuse to start, which is worse than falling back to the init endpoint.
    plain = yaml.safe_load(
        gcp_service._portainer_container_spec_yaml("portainer/portainer-ce:latest"))
    assert "args" not in plain["spec"]["containers"][0]


def test_the_admin_password_hash_round_trips():
    """A hash Portainer can't verify gives a node that boots fine and rejects the only
    password we have, so the hash is checked before it can reach a VM."""
    try:
        import bcrypt  # noqa: F401
    except ImportError:
        return  # bcrypt-less environment takes the documented fallback path
    pw = portainer_node_service._generate_admin_password()
    hashed = portainer_node_service._admin_password_hash(pw)
    assert hashed.startswith("$2"), hashed
    import bcrypt as _b
    assert _b.checkpw(pw.encode(), hashed.encode())
    assert not _b.checkpw(b"wrong-password", hashed.encode())


def test_the_deploy_settles_the_credential_before_launching():
    """Order matters: the hash has to exist before the VM does, and the password has to
    be persisted for a fresh node because the container is already using it."""
    src = open(os.path.join(_ROOT, "web_dashboard", "services",
                            "portainer_node_service.py"), encoding="utf-8").read()
    body = src[src.index("async def run_deploy"):]
    hash_at = body.index("_admin_password_hash(password)")
    launch_at = body.index("_launch_node(")
    assert hash_at < launch_at, "the admin hash is computed after the VM is launched"
    assert "admin_password_hash=pw_hash" in body, "the launcher never receives the hash"
    persist_at = body.index('config_service.set("portainer_admin_password", password)')
    ready_at = body.index("wait_ready(")
    assert launch_at < persist_at < ready_at, (
        "a fresh node's password must be persisted between launch and the readiness "
        "poll — the container is already initialized with it")


def test_a_locked_node_fails_the_job_with_the_remedy():
    """The old code reported this as 'already had an admin user' and completed the job,
    so a bricked node looked like a successful deploy."""
    src = open(os.path.join(_ROOT, "web_dashboard", "services",
                            "portainer_node_service.py"), encoding="utf-8").read()
    assert "_LOCKED_NODE_REMEDY" in src
    assert "PortainerInitWindowClosed" in src, (
        "the deploy does not distinguish a closed init window from an existing admin")
    body = src[src.index("async def run_deploy"):]
    for guard in ("except portainer_service.PortainerInitWindowClosed as exc:",
                  "set_failed(db, job_id, f\"{exc} {_LOCKED_NODE_REMEDY}\")"):
        assert guard in body, f"run_deploy is missing: {guard}"
    remedy = src[src.index("_LOCKED_NODE_REMEDY = ("):src.index("def _firewall_name")]
    assert "Delete the node" in remedy, (
        "the remedy must say the VM has to go — a redeploy reuses the RUNNING VM and "
        "konlet only reads the container declaration at boot")


def test_the_init_timeout_is_not_mistaken_for_an_existing_admin():
    """Same HTTP 403, opposite meaning: an existing admin is recoverable with a PAT,
    a closed window means no admin exists at all."""
    src = open(os.path.join(_ROOT, "web_dashboard", "services",
                            "portainer_service.py"), encoding="utf-8").read()
    assert "class PortainerInitWindowClosed" in src
    init = src[src.index("async def init_admin"):src.index("def _is_init_timeout")]
    assert init.index("_is_init_timeout(resp)") < init.index("resp.status_code in (403, 409)"), (
        "the timeout check must come first, or the 403 is swallowed as 'already "
        "initialized'")
    detect = src[src.index("def _is_init_timeout"):]
    detect = detect[:detect.index("\n@") if "\n@" in detect else len(detect)]
    assert "administrator initialization timeout" in detect, (
        "the timeout is matched on the message, because the status code differs per "
        "endpoint (403 on init, 303 elsewhere)")


def test_portainer_url_uses_9443():
    assert gcp_service._portainer_url("203.0.113.9") == "https://203.0.113.9:9443"
    assert gcp_service._portainer_url("") == ""


# ── Durable state: the optional persistent data disk ─────────────────────────────

def _node_service_src() -> str:
    return open(os.path.join(_ROOT, "web_dashboard", "services",
                             "portainer_node_service.py"), encoding="utf-8").read()


def _gcp_service_src() -> str:
    return open(os.path.join(_ROOT, "web_dashboard", "services",
                             "gcp_service.py"), encoding="utf-8").read()


def test_the_data_disk_is_off_by_default():
    """Durability is opt-in: the disk outlives the node and bills until deleted, and
    every pre-existing install must keep the ephemeral behaviour it was built with."""
    _reset(gcp_project_id="proj", gcp_zone="us-central1-a")
    assert _node_params()["data_disk_name"] == ""
    assert _node_params()["data_disk_gb"] == 10


def test_the_data_disk_name_derives_from_the_node_name():
    _reset(gcp_project_id="proj", gcp_zone="us-central1-a",
           portainer_data_disk_enabled="1")
    assert _node_params()["data_disk_name"] == "portainer-server-data"
    # A renamed node gets its own disk rather than silently adopting another's.
    _reset(gcp_project_id="proj", gcp_zone="us-central1-a",
           portainer_data_disk_enabled="1", gcp_portainer_name="lab-portainer")
    assert _node_params()["data_disk_name"] == "lab-portainer-data"


def test_container_spec_uses_konlets_persistent_disk_keys_when_durable():
    """konlet's schema, not Kubernetes': gcePersistentDisk/pdName/fsType, and NO
    readOnly inside it. A misspelled key is not a validation error — konlet mounts
    nothing and Portainer writes its DB to the boot disk instead, which looks like a
    working deploy right up until the teardown loses everything."""
    import yaml
    spec = yaml.safe_load(gcp_service._portainer_container_spec_yaml(
        "portainer/portainer-ce:latest", data_pd_name="portainer-data"))
    vol = spec["spec"]["volumes"][0]
    assert set(vol) == {"name", "gcePersistentDisk"}, vol
    assert vol["gcePersistentDisk"] == {"pdName": "portainer-data", "fsType": "ext4"}, vol
    assert "hostPath" not in vol, "the durable spec must not also carry a host path"
    # The mount point Portainer actually reads is unchanged.
    assert spec["spec"]["containers"][0]["volumeMounts"][0]["mountPath"] == "/data"


def test_the_pd_name_matches_the_attached_device_name():
    """konlet resolves a gcePersistentDisk as /dev/disk/by-id/google-<pdName>, so the
    disk MUST be attached with device_name == pdName or the container never starts."""
    src = _gcp_service_src()
    launcher = src[src.index("def _run_gce_portainer_sync"):]
    assert "data_disk.device_name = _PORTAINER_DATA_DEVICE" in launcher, launcher[:0]
    spec_fn = src[src.index("def _portainer_container_spec_yaml"):
                  src.index("def _ensure_portainer_firewall_sync")]
    assert 'data_pd_name=_PORTAINER_DATA_DEVICE' in launcher or \
           '"pdName": data_pd_name' in spec_fn, "pdName is not wired to the device name"


def test_the_data_disk_outlives_the_vm():
    """auto_delete=False on the data disk is the whole feature — the boot disk keeps
    auto_delete=True so the OS is still disposable."""
    src = _gcp_service_src()
    launcher = src[src.index("def _run_gce_portainer_sync"):]
    assert "data_disk.auto_delete = False" in launcher
    assert "disk.auto_delete = True" in launcher, "the boot disk should stay ephemeral"


def test_a_preexisting_data_disk_pins_the_zone():
    """A persistent disk is zonal. Falling back to a sibling zone on capacity would
    launch a node with a fresh empty disk and strand the real state next door, so an
    existing disk collapses the candidate list to exactly its own zone."""
    src = _gcp_service_src()
    launcher = src[src.index("def _run_gce_portainer_sync"):]
    find_at = launcher.index("_find_portainer_data_disk_sync(project_id, data_disk_name)")
    cand_at = launcher.index("_rancher_candidate_zones(")
    assert find_at < cand_at, (
        "the disk lookup must precede zone selection — it overrides the region pick")
    assert "candidate_zones = [pinned]" in launcher


def test_an_empty_disk_is_removed_when_its_zone_is_exhausted():
    """Retrying the next zone would otherwise leave a blank disk behind in every
    exhausted zone — but a PRE-EXISTING disk must never be deleted, because it is the
    state we are trying to keep."""
    src = _gcp_service_src()
    launcher = src[src.index("def _run_gce_portainer_sync"):]
    assert "if disk_created_here:" in launcher, (
        "the cleanup is not gated on the disk having been created by this attempt")
    guard_at = launcher.index("if disk_created_here:")
    del_at = launcher.index("_delete_portainer_data_disk_sync(project_id, cand, data_disk_name)")
    assert guard_at < del_at


def test_a_preexisting_db_never_gets_a_regenerated_password():
    """Portainer ignores --admin-password once its DB holds an admin. A fresh VM back on
    an existing data disk therefore comes up with the OLD password, so persisting a
    newly generated one would store a credential that was never real and the deploy
    would fail to sign in."""
    src = _node_service_src()
    body = src[src.index("async def run_deploy"):]
    assert "state_preexisting = bool(res.get(\"reused\") or res.get(\"data_disk_reused\"))" in body, (
        "the deploy does not treat a reattached data disk as pre-existing state")
    assert 'if pw_hash and not state_preexisting:' in body, (
        "the password is persisted without checking for a pre-existing DB")
    # And it must refuse BEFORE launching when it has no password for an existing disk.
    # Anchored on the cloud-agnostic launch seam rather than the GCE call, so the
    # ordering stays pinned whichever cloud the node is deployed to.
    guard_at = body.index("already exists in")
    launch_at = body.index("_launch_node(")
    assert guard_at < launch_at, (
        "the missing-password guard must run before the VM is created")


def test_a_preexisting_db_reuses_its_token_instead_of_reminting():
    """The PAT lives in Portainer's DB, so it survives with the disk. Re-minting under
    the same fixed description on every durable redeploy is the avoidable risk."""
    body = _node_service_src()
    deploy = body[body.index("async def run_deploy"):]
    assert "if state_preexisting and existing_pat:" in deploy, (
        "a fresh VM on an existing disk (reused=False) would re-mint needlessly")
    boot = body[body.index("async def _bootstrap"):body.index("async def run_deploy")]
    assert "state_preexisting" in boot and "description=f\"vm-dashboard-" in boot, (
        "a forced re-mint must not collide with the token already in the restored DB")


def test_teardown_keeps_the_credential_when_the_disk_survives():
    """This is the coupling that silently bricks the feature: clear the password while
    keeping the disk and the NEXT deploy can never log in."""
    src = _node_service_src()
    body = src[src.index("async def run_teardown"):]
    assert "state_survives = bool(data_disk_name) and not delete_data_disk" in body
    keep_at = body.index("state_survives = ")
    clear_at = body.index('cleared += ["portainer_pat", "portainer_admin_password"')
    assert keep_at < clear_at
    assert "if not state_survives:" in body, (
        "the credential keys are cleared unconditionally")


def test_teardown_preserves_the_data_disk_unless_asked():
    """Deleting the only copy of the node's users and environments must be an explicit
    act, not the default a routine teardown takes."""
    gsrc = _gcp_service_src()
    sig = gsrc[gsrc.index("async def stop_gce_portainer"):]
    sig = sig[:sig.index('"""')]
    assert "delete_data_disk: bool = False" in sig, sig
    nsrc = _node_service_src()
    body = nsrc[nsrc.index("async def run_teardown"):]
    assert 'delete_data_disk = bool(meta.get("delete_data_disk"))' in body
    # The disk delete has to follow the VM delete — an attached disk can't be removed.
    order = gsrc[gsrc.index("async def stop_gce_portainer"):]
    assert order.index("_terminate_instance_sync") < order.index("_delete_portainer_data_disk_sync")


def test_a_region_move_is_refused_when_state_is_durable():
    """The launcher pins to the disk's zone, so a region pick would silently not happen.
    Failing loudly beats a deploy that reports a region it didn't move to."""
    body = _node_service_src()
    deploy = body[body.index("async def run_deploy"):]
    assert 'elif p["data_disk_name"]:' in deploy
    assert "cannot be attached in another region" in deploy


# ── Minting an API token on demand ───────────────────────────────────────────
# Portainer shows a token's value exactly once, so before this the ONLY way to get
# one was to deploy a node: a revoked token, or a DB an ephemeral node threw away,
# left no supported repair.

_portainer_stub = sys.modules["web_dashboard.services.portainer_service"]


class _StubPortainerError(Exception):
    pass


class _StubNotConfigured(_StubPortainerError):
    pass


_portainer_stub.PortainerError = _StubPortainerError
_portainer_stub.PortainerNotConfigured = _StubNotConfigured


def _await(coro):
    import asyncio
    return asyncio.new_event_loop().run_until_complete(coro)


def _stub_portainer(**calls):
    """Record what the mint path asks Portainer for. Returns the log."""
    log = []

    async def _login(url, username, password, verify=False):
        log.append(("login", url, username, password, verify))
        return calls.get("jwt", "jwt-abc")

    async def _create(url, jwt, password, description="vm-dashboard", verify=False):
        log.append(("token", url, jwt, password, description, verify))
        return calls.get("pat", "ptr_minted")

    _portainer_stub.login = _login
    _portainer_stub.create_access_token = _create
    return log


def test_minting_stores_the_token_and_never_returns_it():
    _reset(portainer_url="https://10.0.0.4:9443/",
           portainer_admin_password="hunter2hunter2", portainer_verify_ssl="0")
    log = _stub_portainer()
    out = _await(portainer_node_service.mint_api_token())
    assert _CONFIG["portainer_pat"] == "ptr_minted", _CONFIG
    # The response is what a page may render. A token Portainer shows once belongs
    # in config, not in an HTTP response that gets logged on the way out.
    assert "ptr_minted" not in repr(out), out
    assert out["token_configured"] is True and out["username"] == "admin", out
    # The trailing slash is stripped: every Portainer path is appended to this.
    assert log[0][1] == "https://10.0.0.4:9443", log


def test_the_jwt_is_a_means_and_is_never_stored():
    """Storing the session token instead would produce an integration that works
    this afternoon and 401s tomorrow — Portainer's JWT expires in hours and nothing
    here can refresh one on an integration's behalf."""
    _reset(portainer_url="https://p", portainer_admin_password="pw")
    _stub_portainer(jwt="jwt-that-expires")
    _await(portainer_node_service.mint_api_token())
    assert "jwt-that-expires" not in str(_CONFIG.values()), _CONFIG


def test_the_description_is_unique_per_mint():
    """Portainer refuses a second token with a description the user already has, and
    the node bootstrap has already taken the plain 'vm-dashboard' — so a fixed name
    would fail on the first RE-mint, which is the case this exists for."""
    _reset(portainer_url="https://p", portainer_admin_password="pw")
    log = _stub_portainer()
    first = _await(portainer_node_service.mint_api_token())["description"]
    assert first != "vm-dashboard", first
    assert first.startswith("vm-dashboard-"), first
    # An explicit one still wins, for an operator who wants a recognisable label.
    named = _await(portainer_node_service.mint_api_token(description="entitle"))
    assert named["description"] == "entitle", named
    assert log[-1][4] == "entitle", log


def test_the_verify_flag_follows_the_configured_value():
    """A managed node serves a self-signed certificate on :9443 and the deploy sets
    this to 0 for that reason; an operator's own Portainer keeps verification on."""
    _reset(portainer_url="https://p", portainer_admin_password="pw",
           portainer_verify_ssl="1")
    log = _stub_portainer()
    _await(portainer_node_service.mint_api_token())
    assert log[0][4] is True, log
    _reset(portainer_url="https://p", portainer_admin_password="pw",
           portainer_verify_ssl="0")
    log = _stub_portainer()
    _await(portainer_node_service.mint_api_token())
    assert log[0][4] is False, log


def test_minting_without_a_password_says_which_one_is_missing():
    """Portainer re-checks the password on the token call even with a valid session,
    so 'sign in somehow' is not enough and the error has to name the setting."""
    _reset(portainer_url="https://p")
    _stub_portainer()
    try:
        _await(portainer_node_service.mint_api_token())
    except _StubNotConfigured as exc:
        assert "portainer_admin_password" in str(exc), str(exc)
    else:
        raise AssertionError("minted a token with no credential to sign in with")


def test_minting_without_a_url_is_refused_before_any_call():
    _reset(portainer_admin_password="pw")
    log = _stub_portainer()
    try:
        _await(portainer_node_service.mint_api_token())
    except _StubNotConfigured as exc:
        assert "portainer_url" in str(exc), str(exc)
    else:
        raise AssertionError("minted a token against no Portainer at all")
    assert log == [], log


def test_minting_refuses_to_overwrite_a_vault_reference():
    """An operator who keeps the token in their own vault said where it lives.
    Writing a literal over that reference leaves the dashboard working and the vault
    stale, so the next rotation there does nothing and nothing says why."""
    _reset(portainer_url="https://p", portainer_admin_password="pw",
           portainer_pat="bt_safe://Portainer_PAT")
    log = _stub_portainer()
    try:
        _await(portainer_node_service.mint_api_token())
    except _StubNotConfigured as exc:
        assert "bt_safe://Portainer_PAT" in str(exc), str(exc)
    else:
        raise AssertionError("a vault reference was replaced with a literal token")
    assert _CONFIG["portainer_pat"] == "bt_safe://Portainer_PAT", _CONFIG
    assert log == [], "Portainer was asked for a token that could not be stored"


def test_a_supplied_credential_is_used_and_not_persisted():
    """For a Portainer this dashboard did not deploy: the admin is not 'admin' and
    its password is not in config. The password is for the sign-in only."""
    _reset(portainer_url="https://p", portainer_admin_password="stored-one")
    log = _stub_portainer()
    _await(portainer_node_service.mint_api_token(username="ops", password="theirs"))
    assert log[0][2] == "ops" and log[0][3] == "theirs", log
    assert _CONFIG["portainer_admin_password"] == "stored-one", _CONFIG


# ── The node's ingress is managed on a MINT, not only on a deploy ────────────
# The deploy writes the allow-list from one egress detection and then never revisits
# it, so by the time anyone clicks "mint a token" the dashboard's own outbound address
# may have moved (a worker rescheduled behind a different SNAT address, a corp proxy
# egressing from a pool) and the node's rule still admits the old one. Every packet is
# dropped, the mint fails with a ConnectTimeout — and that is the exact failure the
# mint button exists to REPAIR, so it repairs it instead of reporting it.

_portainer_stub.is_unreachable = lambda exc: bool(getattr(exc, "dropped", False))

#: What the cloud says is deployed, and whether it will answer at all.
_NODES: list = []
_LIST_FAILS = [False]


async def _fake_list_nodes(cloud, spec, placement):
    if _LIST_FAILS[0]:
        raise RuntimeError("compute API unavailable")
    return list(_NODES)


portainer_node_service.managed_node_service.list_nodes = _fake_list_nodes


def _dropped():
    """A PortainerError raised because the SYN went unanswered — a firewall, since a
    closed port would answer with a reset."""
    exc = _StubPortainerError("Cannot reach Portainer: ConnectTimeout: the TCP connect "
                              "got no answer")
    exc.dropped = True
    return exc


def _stub_mint(drops=0):
    """Portainer whose sign-in drops the connection the first ``drops`` times."""
    log = []
    state = {"drops": drops}

    async def _login(url, username, password, verify=False):
        if state["drops"] > 0:
            state["drops"] -= 1
            log.append(("dropped", url))
            raise _dropped()
        log.append(("login", url, username, password, verify))
        return "jwt-abc"

    async def _create(url, jwt, password, description="vm-dashboard", verify=False):
        log.append(("token", url, description))
        return "ptr_minted"

    _portainer_stub.login = _login
    _portainer_stub.create_access_token = _create
    return log


def _stub_firewall(detect="203.0.113.9", applied=None):
    """Replace the two cloud-touching steps of the re-admit, and record them.

    Detection writes the same config key the real one persists, so the REAL merge
    decides what the allow-list looked like either side of it.
    """
    seen = {"detected": 0, "applied": []}

    async def _detect():
        seen["detected"] += 1
        if detect:
            _CONFIG["portainer_dashboard_egress_cidr"] = f"{detect}/32"
        return detect

    async def _refresh(db=None, placement=None):
        seen["applied"].append(portainer_node_service.firewall_status(db)["merged"])
        return dict(applied or {"name": "portainer-server-allow-mgmt", "opened": True})

    portainer_node_service._ensure_dashboard_egress_cidr = _detect
    portainer_node_service.refresh_portainer_firewall = _refresh
    return seen


def _managed_node_config(**extra):
    cfg = dict(gcp_project_id="proj", gcp_zone="us-central1-a",
               portainer_url="https://34.10.0.7:9443", portainer_admin_password="pw")
    cfg.update(extra)
    _reset(**cfg)
    _NODES[:] = [{"name": "portainer-server", "external_ip": "34.10.0.7",
                  "internal_ip": "10.99.1.5", "status": "RUNNING"}]
    _LIST_FAILS[0] = False


def test_a_dropped_mint_re_admits_the_dashboard_egress_and_mints_again():
    """Telling the operator to REDEPLOY THE NODE to fix a token is the dead end this
    button replaced — so a stale allow-list must not be able to reinstate it."""
    _managed_node_config(portainer_dashboard_egress_cidr="198.51.100.2/32")
    log = _stub_mint(drops=1)
    seen = _stub_firewall(detect="203.0.113.9")

    out = _await(portainer_node_service.mint_api_token())

    assert out["token_configured"] is True, out
    assert _CONFIG["portainer_pat"] == "ptr_minted", _CONFIG
    assert seen["detected"] == 1, seen
    # The address actually in use now is in the rule that gets applied.
    assert "203.0.113.9/32" in seen["applied"][0], seen
    # One drop, one repair, one retry — and the retry signs in again, because a JWT
    # from a session that could not be used is worth nothing.
    assert [row[0] for row in log] == ["dropped", "login", "token"], log


def test_a_mint_against_someone_elses_portainer_never_touches_the_node_firewall():
    """A Portainer this dashboard merely points at has a firewall that is the
    operator's. Rewriting an ingress rule in their cloud on its behalf would be a side
    effect nobody asked for, aimed at a firewall that is not the one dropping the
    packets."""
    _managed_node_config(portainer_url="https://portainer.corp.example:9443")
    log = _stub_mint(drops=1)
    seen = _stub_firewall()
    try:
        _await(portainer_node_service.mint_api_token())
    except _StubPortainerError as exc:
        assert "not a Portainer node this dashboard deployed" in str(exc), str(exc)
    else:
        raise AssertionError("a dropped connect to an unmanaged Portainer was swallowed")
    assert seen == {"detected": 0, "applied": []}, seen
    assert [row[0] for row in log] == ["dropped"], log


def test_a_cloud_that_cannot_be_asked_leaves_the_firewall_alone():
    """"Cannot confirm" is not "it is ours": a compute API that will not answer must
    not authorise rewriting an ingress rule."""
    _managed_node_config()
    _LIST_FAILS[0] = True
    _stub_mint(drops=1)
    seen = _stub_firewall()
    try:
        _await(portainer_node_service.mint_api_token())
    except _StubPortainerError as exc:
        assert "could not be reached to check" in str(exc), str(exc)
    else:
        raise AssertionError("an unconfirmable node still had its ingress rewritten")
    assert seen["applied"] == [], seen


def test_a_mint_still_dropped_after_re_admitting_says_nothing_changed():
    """The allow-list already named the detected address, so a retry would dial from
    the same address into the same rule. Say so instead of burning another timeout."""
    _managed_node_config(portainer_dashboard_egress_cidr="203.0.113.9/32")
    log = _stub_mint(drops=1)
    _stub_firewall(detect="203.0.113.9")
    try:
        _await(portainer_node_service.mint_api_token())
    except _StubPortainerError as exc:
        assert "produced no change" in str(exc), str(exc)
        # Naming what IS allowed is the whole diagnosis — the operator compares it
        # with the address their worker actually egresses from.
        assert "203.0.113.9/32" in str(exc), str(exc)
    else:
        raise AssertionError("a mint that never connected reported success")
    assert [row[0] for row in log] == ["dropped"], "a second connect was made for nothing"
    assert not _CONFIG.get("portainer_pat"), _CONFIG


def test_a_firewall_rule_that_had_to_be_recreated_counts_as_a_change():
    """A hand-deleted rule computes the same source set it always did, so comparing
    the sets alone would give up on the very repair that just happened."""
    _managed_node_config(portainer_dashboard_egress_cidr="203.0.113.9/32")
    log = _stub_mint(drops=1)
    _stub_firewall(detect="203.0.113.9",
                   applied={"name": "portainer-server-allow-mgmt", "opened": True,
                            "created": True})
    out = _await(portainer_node_service.mint_api_token())
    assert out["token_configured"] is True, out
    assert [row[0] for row in log] == ["dropped", "login", "token"], log


def test_a_retry_that_is_dropped_too_says_the_ingress_was_already_widened():
    """Otherwise the operator reads an identical ConnectTimeout twice and has no way
    to know the allow-list was widened on their behalf — the one fact that tells them
    a single /32 is the wrong shape for a host that egresses from a pool."""
    _managed_node_config(portainer_dashboard_egress_cidr="198.51.100.2/32")
    log = _stub_mint(drops=2)
    _stub_firewall(detect="203.0.113.9")
    try:
        _await(portainer_node_service.mint_api_token())
    except _StubPortainerError as exc:
        assert "ingress was re-applied first" in str(exc), str(exc)
        assert "203.0.113.9/32" in str(exc), str(exc)
    else:
        raise AssertionError("a mint that never connected reported success")
    assert [row[0] for row in log] == ["dropped", "dropped"], log


def test_an_http_failure_is_not_a_firewall_problem():
    """Bad credentials ANSWER; dropped packets do not. Only the second is the
    dashboard's to repair, and re-applying ingress over a 401 would hide it."""
    _managed_node_config()
    seen = _stub_firewall()
    calls = []

    async def _login(url, username, password, verify=False):
        calls.append(url)
        raise _StubPortainerError("login failed: Invalid JWT token")

    _portainer_stub.login = _login
    try:
        _await(portainer_node_service.mint_api_token())
    except _StubPortainerError as exc:
        assert "Invalid JWT token" in str(exc), str(exc)
        assert "firewall" not in str(exc).lower(), str(exc)
    else:
        raise AssertionError("a rejected credential was reported as success")
    assert seen == {"detected": 0, "applied": []}, seen
    assert len(calls) == 1, calls


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
