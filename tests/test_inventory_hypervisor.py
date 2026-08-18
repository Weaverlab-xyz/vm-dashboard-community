"""Synced hypervisor VMs on /inventory.

`inventory_service.collect` used to read four sources, all of them things the dashboard
either deployed or holds a record for. A VM discovered on somebody's hypervisor is
neither, and it was simply absent — so an estate could be fully synced, listed on its
hypervisor page, and invisible on the page named "inventory".

The three properties worth pinning are the ones that would be silent if wrong:

* **it cannot be auto-deleted** — there is no teardown for a discovered VM, so expiring
  one would drop a cache row and leave the VM running;
* **it does not double-count** — a Proxmox VM the dashboard deployed is already listed
  from its deploy Job, which is the row carrying the timer and the job link;
* **it does not widen visibility** — untagged means admin-only, as on every hypervisor
  page.

Real throwaway SQLite, because the join, the override lookup and the dedup only actually
run against a database. The pure mappers are unit-tested in test_inventory_service.py.

Runs under pytest, or standalone:  python tests/test_inventory_hypervisor.py
"""
import json
import os
import sys
import tempfile
from datetime import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.environ.setdefault(
    "DATABASE_URL",
    "sqlite:///" + os.path.join(tempfile.mkdtemp(), "inv.db").replace("\\", "/"))
os.environ.setdefault("JWT_SECRET_KEY", "x" * 32)

try:
    from web_dashboard.database import (Base, HypervisorConnection, HypervisorVMCache,
                                        Job, RemoteAgent, SessionLocal, Workgroup,
                                        VMWorkgroupOverride, engine)
    from web_dashboard.services import inventory_service as svc
    from web_dashboard.services import workgroup_override_service as wos
except Exception as exc:  # noqa: BLE001
    print(f"SKIP: {exc}")
    sys.exit(0)

Base.metadata.create_all(bind=engine)


def _reset(db):
    db.query(HypervisorVMCache).delete()
    db.query(HypervisorConnection).delete()
    db.query(RemoteAgent).delete()
    db.query(VMWorkgroupOverride).delete()
    db.query(Job).delete()
    db.commit()


def _agent(db):
    agent = RemoteAgent(name="desk-01", public_key="k", is_active=True,
                        enrolled_at=datetime.utcnow(), last_seen_at=datetime.utcnow())
    db.add(agent)
    db.commit()
    db.refresh(agent)
    return agent


def _conn(db, agent, *, kind, name, active=True, site=""):
    import uuid
    conn = HypervisorConnection(
        id=str(uuid.uuid4()), kind=kind, name=name, agent_id=agent.id,
        agent_connection_name=name, is_active=active, site=site,
        created_by="t", created_at=datetime.utcnow())
    db.add(conn)
    db.commit()
    return conn


# Per-kind spelling of "on". Not one string: that is the whole point of
# hypervisor_view_service._RUNNING, and a helper that hardcoded `running` would quietly
# assert that a Workstation VM is stopped.
_ON = {"workstation": "poweredOn", "vsphere": "poweredOn", "esxi": "poweredOn",
       "nutanix": "ON", "hyperv": "Running", "proxmox": "running", "xcpng": "running"}


def _vm(db, conn, *, vm_id, name, state=None, scope="", ips=()):
    state = state or _ON[(conn.kind or "").lower()]
    db.add(HypervisorVMCache(
        connection_id=conn.id, vm_id=vm_id, name=name, power_state=state,
        scope=scope, vm_type="vm", tags="[]",
        ip_addresses=json.dumps(list(ips)), synced_at=datetime.utcnow()))
    db.commit()


def _workgroup(db, name):
    """Workgroups survive _reset (other suites seed them), so create idempotently."""
    if not db.query(Workgroup.id).filter(Workgroup.name == name).first():
        db.add(Workgroup(id=f"wg-{name}-inv", name=name, display_name=name.title()))
        db.commit()


def _hv_items(db):
    return [i for i in svc.collect(db) if i["id"].startswith("hv:")]


# ── The source at all ─────────────────────────────────────────────────────────

def test_a_synced_vm_appears_on_the_inventory():
    db = SessionLocal()
    try:
        _reset(db)
        conn = _conn(db, _agent(db), kind="workstation", name="my-ws")
        _vm(db, conn, vm_id="AB12", name="win11-lab",
            scope=r"C:\VMs\win11\win11.vmx", ips=["10.0.0.5"])
        items = _hv_items(db)
        assert len(items) == 1
        item = items[0]
        assert item["cloud"] == "workstation"
        assert item["kind"] == "vm"
        assert item["name"] == "win11-lab"
        assert item["state"] == "running"
        assert item["ip"] == "10.0.0.5"
        assert item["detail_href"] == "/vms"
    finally:
        db.close()


def test_an_inactive_connection_contributes_nothing():
    """A sync only touches active connections and _prune only removes rows a pass
    touched, so deactivating one FREEZES its cache rather than emptying it. Listing
    frozen rows as current inventory is the lie this filter avoids."""
    db = SessionLocal()
    try:
        _reset(db)
        agent = _agent(db)
        live = _conn(db, agent, kind="proxmox", name="live")
        dead = _conn(db, agent, kind="proxmox", name="dead", active=False)
        _vm(db, live, vm_id="100", name="alive", scope="pve1")
        _vm(db, dead, vm_id="200", name="frozen", scope="pve1")
        names = {i["name"] for i in _hv_items(db)}
        assert names == {"alive"}
    finally:
        db.close()


def test_the_power_state_is_normalised_per_product():
    """Each product spells it differently. Reading them raw would render a whole
    hypervisor as stopped after an upstream capitalisation change."""
    db = SessionLocal()
    try:
        _reset(db)
        agent = _agent(db)
        cases = {"nutanix": "ON", "vsphere": "poweredOn",
                 "hyperv": "Running", "proxmox": "running"}
        for kind, spelling in cases.items():
            conn = _conn(db, agent, kind=kind, name=f"c-{kind}")
            _vm(db, conn, vm_id=f"id-{kind}", name=f"vm-{kind}", state=spelling)
        for item in _hv_items(db):
            assert item["state"] == "running", (item["cloud"], item["state"])
    finally:
        db.close()


# ── Auto-delete safety ────────────────────────────────────────────────────────

def test_a_synced_vm_can_never_be_auto_deleted():
    db = SessionLocal()
    try:
        _reset(db)
        conn = _conn(db, _agent(db), kind="proxmox", name="pve")
        _vm(db, conn, vm_id="100", name="db-01", scope="pve1")
        item = _hv_items(db)[0]
        assert item["expires_at"] is None
        assert item["ttl_capable"] is False
        assert item["ttl_reason"], "a refusal with no reason is a disabled control"
        assert "hypervisor" in item["ttl_reason"]
    finally:
        db.close()


def test_the_id_prefix_is_one_the_reaper_cannot_resolve():
    """Belt and braces behind ttl_capable: expiry_reaper._resolve_row has a prefix
    allowlist, and `hv:` is deliberately not in it."""
    db = SessionLocal()
    try:
        from web_dashboard.services import expiry_reaper
        _reset(db)
        conn = _conn(db, _agent(db), kind="proxmox", name="pve")
        _vm(db, conn, vm_id="100", name="db-01", scope="pve1")
        assert expiry_reaper._resolve_row(db, _hv_items(db)[0]["id"]) is None
    finally:
        db.close()


# ── Dedup ─────────────────────────────────────────────────────────────────────

def test_a_vm_the_dashboard_deployed_is_listed_once_from_its_job():
    """The Job row wins: it is the one carrying the auto-delete timer and the job link,
    so dropping it in favour of the cache row would silently orphan a live timer."""
    db = SessionLocal()
    try:
        _reset(db)
        conn = _conn(db, _agent(db), kind="proxmox", name="pve")
        _vm(db, conn, vm_id="100", name="web-01", scope="pve1")

        db.add(Job(id="job-1", job_type="proxmox_deploy", status="completed",
                   created_by="admin", created_at=datetime.utcnow(),
                   extra_data=json.dumps({"connection_id": conn.id, "vmid": 100,
                                          "vm_name": "web-01", "node": "pve1"})))
        db.commit()

        items = svc.collect(db)
        web = [i for i in items if i["name"] == "web-01"]
        assert len(web) == 1, "the same VM must not appear twice"
        assert web[0]["id"].startswith("job:")
        assert web[0]["source"] == "provisioned"
    finally:
        db.close()


def test_a_job_that_predates_the_id_merge_still_dedups_on_name_and_node():
    """Older deploy jobs recorded no vmid, so the casefolded name+node key is the
    fallback — deliberately the same join api/proxmox.py uses to inherit a workgroup."""
    db = SessionLocal()
    try:
        _reset(db)
        conn = _conn(db, _agent(db), kind="proxmox", name="pve")
        _vm(db, conn, vm_id="101", name="Web-02", scope="pve1")

        db.add(Job(id="job-2", job_type="proxmox_deploy", status="completed",
                   created_by="admin", created_at=datetime.utcnow(),
                   extra_data=json.dumps({"vm_name": "web-02", "node": "pve1"})))
        db.commit()

        assert [i["name"] for i in _hv_items(db)] == []
    finally:
        db.close()


def test_a_vm_nobody_deployed_survives_the_dedup():
    db = SessionLocal()
    try:
        _reset(db)
        conn = _conn(db, _agent(db), kind="proxmox", name="pve")
        _vm(db, conn, vm_id="100", name="web-01", scope="pve1")
        _vm(db, conn, vm_id="999", name="hand-made", scope="pve1")

        db.add(Job(id="job-3", job_type="proxmox_deploy", status="completed",
                   created_by="admin", created_at=datetime.utcnow(),
                   extra_data=json.dumps({"connection_id": conn.id, "vmid": 100,
                                          "vm_name": "web-01", "node": "pve1"})))
        db.commit()
        assert [i["name"] for i in _hv_items(db)] == ["hand-made"]
    finally:
        db.close()


# ── RBAC ──────────────────────────────────────────────────────────────────────

def test_an_untagged_synced_vm_is_admin_only():
    db = SessionLocal()
    try:
        _reset(db)
        conn = _conn(db, _agent(db), kind="workstation", name="my-ws")
        _vm(db, conn, vm_id="AB12", name="win11-lab")
        item = _hv_items(db)[0]
        assert item["workgroup"] is None
        assert item["deployed_by"] is None, (
            "load-bearing: visible_to falls back to comparing this against the caller")
        assert svc.visible_to(item, None, "admin") is True
        assert svc.visible_to(item, ["dev"], "dev") is False
        assert svc.visible_to(item, [], "dev") is False
    finally:
        db.close()


def test_a_tagged_synced_vm_follows_its_workgroup():
    db = SessionLocal()
    try:
        _reset(db)
        conn = _conn(db, _agent(db), kind="workstation", name="my-ws")
        _vm(db, conn, vm_id="AB12", name="win11-lab")
        _workgroup(db, "dev")
        wos.set_many(db, provider="workstation", vm_ids=["AB12"], workgroup="dev")

        item = _hv_items(db)[0]
        assert item["workgroup"] == "dev"
        assert svc.visible_to(item, ["dev"], "anyone") is True
        assert svc.visible_to(item, ["finance"], "anyone") is False
    finally:
        db.close()


def test_a_proxmox_override_is_read_with_the_composite_key():
    """api/proxmox.py keys overrides on `node/vmid`, because a vmid is not unique across
    a cluster. Reading the bare vmid here would silently ignore every Proxmox
    assignment an admin has ever made."""
    db = SessionLocal()
    try:
        _reset(db)
        conn = _conn(db, _agent(db), kind="proxmox", name="pve")
        _vm(db, conn, vm_id="100", name="web-01", scope="pve1")
        _workgroup(db, "dev")
        wos.set_many(db, provider="proxmox", vm_ids=["pve1/100"], workgroup="dev")
        assert _hv_items(db)[0]["workgroup"] == "dev"
    finally:
        db.close()


# ── Cost ──────────────────────────────────────────────────────────────────────

def test_the_override_lookup_is_one_query_per_kind_not_per_vm():
    """The N+1 guard. collect() is cached for 60s and runs a handful of indexed queries;
    a per-row lookup over a large vCenter would turn that into thousands."""
    db = SessionLocal()
    try:
        _reset(db)
        agent = _agent(db)
        for kind in ("proxmox", "vsphere"):
            conn = _conn(db, agent, kind=kind, name=f"c-{kind}")
            for n in range(25):
                _vm(db, conn, vm_id=f"{kind}-{n}", name=f"vm-{kind}-{n}", scope="s1")

        calls = []
        original = wos.get_many
        wos.get_many = lambda db_, provider, vm_ids: (
            calls.append(provider) or original(db_, provider, vm_ids))
        try:
            assert len(_hv_items(db)) == 50
        finally:
            wos.get_many = original
        assert len(calls) == 2, f"one bulk lookup per kind, got {calls}"
    finally:
        db.close()


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
