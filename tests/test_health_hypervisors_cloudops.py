"""Health: hypervisor connections, and the dashboard's own cloud APIs and cost controls.

What these pin:

- a direct connection is dialled and the outcome stamped on the connection; an
  agent-bound one is never dialled, and reads as its agent plus whether its sync keeps up;
- vSphere hosts disconnected or in maintenance, Proxmox nodes offline, Proxmox storage
  under 15% / 5% free;
- a cloud provider whose tile reads keep failing is failing (every read) or degraded
  (some); cost data the same;
- spend caps and the auto-delete timer's last pass;
- only connections whose hypervisor is enabled are checked.

Uses a real temp SQLite database; every hypervisor call is stubbed.

Run: python tests/test_health_hypervisors_cloudops.py   (or under pytest)
"""
import asyncio
import os
import sys
import tempfile
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="health-hv-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDB}"
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-health-hv-tests")

from web_dashboard.database import (Base, HypervisorConnection, RemoteAgent,  # noqa: E402
                                    SessionLocal, engine)
from web_dashboard.services import config_service, feature_flags  # noqa: E402
from web_dashboard.services.health.collectors import cloud_ops, hypervisors as HV  # noqa: E402

Base.metadata.create_all(bind=engine)
NOW = datetime(2026, 10, 12, 12, 0, 0)


# ── hypervisors: pure ─────────────────────────────────────────────────────────

def test_sync_freshness():
    ok = HV.sync_check(NOW - timedelta(minutes=10), 30, now=NOW)
    assert ok["status"] == "ok"
    late = HV.sync_check(NOW - timedelta(hours=2), 30, now=NOW)
    assert late["status"] == "warn"
    failed = HV.sync_check(NOW - timedelta(minutes=10), 30, last_error="agent refused",
                           last_ok_at=NOW - timedelta(hours=1), now=NOW)
    assert failed["status"] == "fail" and "agent refused" in failed["detail"]
    recovered = HV.sync_check(NOW - timedelta(minutes=5), 30, last_error="old error",
                              last_ok_at=NOW - timedelta(minutes=1), now=NOW)
    assert recovered["status"] == "ok", "an error older than the last success still failed"
    assert HV.sync_check(None, 30, now=NOW)["status"] == "unknown"


def test_vsphere_hosts():
    assert HV.vsphere_host_checks([{"name": "esx1", "status": "connected"}])[0]["status"] == "ok"
    maint = HV.vsphere_host_checks([{"name": "esx1", "status": "connected",
                                     "in_maintenance": True}])
    assert maint[0]["status"] == "warn" and "esx1" in maint[0]["detail"]
    down = HV.vsphere_host_checks([{"name": "esx2", "status": "notResponding"}])
    assert down[0]["status"] == "fail"


def test_proxmox_nodes_and_storage():
    nodes = [{"node": "pve1", "status": "online"}, {"node": "pve2", "status": "offline"}]
    checks = {c["key"]: c for c in HV.proxmox_node_checks(nodes, {
        "pve1": [{"storage": "local-lvm", "avail": 10, "total": 100}]})}
    assert checks["nodes"]["status"] == "fail" and "pve2" in checks["nodes"]["detail"]
    assert checks["storage"]["status"] == "warn" and "10% free" in checks["storage"]["detail"]
    tight = HV.proxmox_node_checks(nodes[:1], {"pve1": [{"storage": "s", "avail": 3,
                                                          "total": 100}]})
    assert tight[1]["status"] == "fail"
    roomy = HV.proxmox_node_checks(nodes[:1], {"pve1": [{"storage": "s", "avail": 60,
                                                          "total": 100}]})
    assert [c["status"] for c in roomy] == ["ok", "ok"]


# ── hypervisors: collection ───────────────────────────────────────────────────

class _Flags:
    def __init__(self, *on):
        self.on = set(on)

    def __enter__(self):
        self.real = feature_flags.enabled
        feature_flags.enabled = lambda flag, default=False: flag in self.on

    def __exit__(self, *exc):
        feature_flags.enabled = self.real


def test_direct_and_agent_bound_connections():
    from web_dashboard.services import proxmox_service
    db = SessionLocal()
    agent = RemoteAgent(id=str(uuid.uuid4()), name="site-a", agent_version="2.8.0",
                        is_active=True, public_key="pk", last_seen_at=None)
    direct = HypervisorConnection(id=str(uuid.uuid4()), kind="proxmox", name="lab-pve",
                                  host="pve.example.com", username="root@pam",
                                  secret_enc=config_service.encrypt_value("pw"),
                                  is_active=True)
    bound = HypervisorConnection(id=str(uuid.uuid4()), kind="proxmox", name="site-pve",
                                 host="10.0.0.9", username="root@pam", agent_id=agent.id,
                                 is_active=True)
    off = HypervisorConnection(id=str(uuid.uuid4()), kind="vsphere", name="vc",
                               host="vc.example.com", username="u", is_active=True)
    db.add_all([agent, direct, bound, off])
    db.commit()
    calls = []
    real = (proxmox_service.list_nodes, proxmox_service.list_storage)

    async def nodes(conn):
        calls.append(conn.host)
        return [{"node": "pve1", "status": "online"}]

    async def storage(conn, node):
        return [{"storage": "local", "avail": 50, "total": 100}]
    try:
        proxmox_service.list_nodes, proxmox_service.list_storage = nodes, storage
        with _Flags("proxmox_enabled"):
            items = {i.name: i for i in asyncio.run(HV.collect(db))}
    finally:
        proxmox_service.list_nodes, proxmox_service.list_storage = real
    dialled = sorted(set(calls) & {direct.host, bound.host})
    assert dialled == [direct.host], f"only the direct connection should be dialled: {calls}"
    assert "vc" not in items, "a disabled hypervisor was checked"
    assert items["lab-pve"].status == "ok"
    db.refresh(direct)
    assert direct.last_ok_at is not None, "the outcome was not stamped on the connection"
    site = {c["key"]: c["status"] for c in items["site-pve"].checks}
    assert site["agent"] == "fail" and site["sync"] == "unknown", site
    db.close()


def test_an_unreachable_connection_fails_and_is_recorded():
    from web_dashboard.services import proxmox_service
    db = SessionLocal()
    row = HypervisorConnection(id=str(uuid.uuid4()), kind="proxmox", name="dead-pve",
                               host="dead.example.com", username="root@pam",
                               secret_enc=config_service.encrypt_value("pw"), is_active=True)
    db.add(row)
    db.commit()
    real = proxmox_service.list_nodes

    async def boom(conn):
        raise proxmox_service.ProxmoxError("401 authentication failure")
    try:
        proxmox_service.list_nodes = boom
        with _Flags("proxmox_enabled"):
            items = {i.name: i for i in asyncio.run(HV.collect(db))}
    finally:
        proxmox_service.list_nodes = real
    assert items["dead-pve"].status == "fail"
    db.refresh(row)
    assert "401" in (row.last_error or "")
    db.close()


# ── cloud APIs and spend ──────────────────────────────────────────────────────

def _row(provider, failures=0, error=None, fetched=NOW):
    return SimpleNamespace(provider=provider, cloud=None, consecutive_failures=failures,
                           last_error=error, fetched_at=fetched)


def test_cloud_api_health_from_the_tile_cache():
    items = {i.object_id: i for i in cloud_ops.provider_items([
        _row("aws"), _row("aws"),
        _row("azure", 3, "AADSTS7000215: invalid client secret"), _row("azure", 2, "x"),
        _row("gcp", 1, "quota"), _row("gcp"),
    ], kind="api", area_label="API", now=NOW)}
    assert items["api:aws"].status == "ok"
    azure = items["api:azure"]
    assert azure.status == "fail" and "AADSTS7000215" in azure.checks[0]["detail"]
    assert items["api:gcp"].status == "warn"


def test_spend_and_expiry():
    assert cloud_ops.spend_item(0, 0, 0) is None
    assert cloud_ops.spend_item(0, 0, 4).status == "ok"
    assert cloud_ops.spend_item(1, 0, 4).status == "warn"
    assert cloud_ops.expiry_item({}, enabled=False) is None
    assert cloud_ops.expiry_item({"never_run": True}, enabled=True).status == "unknown"
    ok = {"ended_at": "2026-10-12T11:00:00", "due": 0, "reaped": 2, "failed": [],
          "deleting": True}
    assert cloud_ops.expiry_item(ok, enabled=True).status == "ok"
    assert cloud_ops.expiry_item(dict(ok, failed=[{"id": "x"}]), enabled=True).status == "fail"
    disarmed = dict(ok, due=5, deleting=False)
    item = cloud_ops.expiry_item(disarmed, enabled=True)
    assert item.status == "warn" and "not armed" in item.checks[0]["detail"]


def test_both_areas_are_registered():
    from web_dashboard.services.health import registry
    assert registry.get("cloud_ops").flags == ()
    assert "proxmox_enabled" in registry.get("hypervisors").flags


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
