"""Agent-synced Workstation VMs merged into the /vms page.

A co-located agent reports its own host's Workstation VMs through vmrest; the local path
scans this host with PowerShell. Both are Workstation VMs, so they share a page rather
than getting a second one — with a `source` so it is always clear which host a row is on.

The property worth guarding is the permission one. An agent can report any VM it likes,
so the merge must not become a way to widen what a non-admin sees. Agent rows are gated
by `vm_workgroup_overrides` exactly as Proxmox and Nutanix rows are: no override means
admin-only.

Real throwaway SQLite; no PowerShell and no agent.

Runs under pytest, or standalone:  python tests/test_vms_agent_merge.py
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
    "sqlite:///" + os.path.join(tempfile.mkdtemp(), "vms.db").replace("\\", "/"))
os.environ.setdefault("JWT_SECRET_KEY", "x" * 32)

try:
    from web_dashboard.api import vms as vms_api
    from web_dashboard.database import (Base, HypervisorVMCache, RemoteAgent,
                                        SessionLocal, engine)
    from web_dashboard.services import hypervisor_connection_service as hcs
    from web_dashboard.services import workgroup_override_service
except Exception as exc:  # noqa: BLE001
    print(f"SKIP: {exc}")
    sys.exit(0)

Base.metadata.create_all(bind=engine)


def _setup(db, *, synced=True):
    # Overrides too: they outlive a connection, so without this the tagging test leaks
    # into the admin-only one and the isolation failure looks like a logic bug.
    from web_dashboard.database import HypervisorConnection, VMWorkgroupOverride
    db.query(HypervisorVMCache).delete()
    db.query(RemoteAgent).delete()
    db.query(HypervisorConnection).delete()
    db.query(VMWorkgroupOverride).filter(
        VMWorkgroupOverride.provider == "workstation").delete()
    db.commit()

    agent = RemoteAgent(name="desk-01", public_key="k", is_active=True,
                        enrolled_at=datetime.utcnow(), last_seen_at=datetime.utcnow())
    db.add(agent)
    db.commit()
    db.refresh(agent)

    conn = hcs.create(db, kind="workstation", name="my-ws", created_by="t",
                      agent_id=agent.id, agent_connection_name="my-ws")
    if synced:
        db.add(HypervisorVMCache(
            connection_id=conn["id"], vm_id="AB12", name="win11-lab",
            power_state="poweredOn", vcpus=4, mem_mib=8192,
            ip_addresses=json.dumps(["192.168.72.130"]),
            scope=r"C:\VMs\win11\win11.vmx", vm_type="vm", tags="[]",
            synced_at=datetime.utcnow()))
        db.commit()
    return conn


def test_an_untagged_agent_vm_is_admin_only():
    """Same rule as every other hypervisor page. This is what stops an agent — which
    can report anything — widening what a non-admin sees."""
    db = SessionLocal()
    try:
        _setup(db)
        assert vms_api._agent_workstation_vms(db, ["dev"]) == []
        admin = vms_api._agent_workstation_vms(db, None)
        assert len(admin) == 1 and admin[0].vm_name == "win11-lab"
    finally:
        db.close()


def test_a_tagged_agent_vm_reaches_the_workgroup_that_owns_it():
    db = SessionLocal()
    try:
        conn = _setup(db)
        # set_many refuses a workgroup that does not exist, so create it first — which
        # is itself worth exercising: it is what stops an override inventing one.
        from web_dashboard.database import Workgroup
        if not db.query(Workgroup.id).filter(Workgroup.name == "dev").first():
            db.add(Workgroup(id="wg-dev-test", name="dev", display_name="Dev"))
            db.commit()
        # The same call the other hypervisor pages' bulk-assign uses, so this test
        # exercises the real tagging path rather than a hand-written row.
        workgroup_override_service.set_many(
            db, provider="workstation", vm_ids=["AB12"], workgroup="dev")
        rows = vms_api._agent_workstation_vms(db, ["dev"])
        assert len(rows) == 1, "a tagged VM must reach its workgroup"
        assert rows[0].workgroup == "dev"
        assert conn["id"]
        # And still not to a workgroup that does not own it.
        assert vms_api._agent_workstation_vms(db, ["finance"]) == []
    finally:
        db.close()


def test_an_agent_row_carries_the_agent_name_as_its_source():
    db = SessionLocal()
    try:
        _setup(db)
        row = vms_api._agent_workstation_vms(db, None)[0]
        assert row.source == "desk-01", "the badge names the host the VM is really on"
        assert row.vm_id == "AB12", "vmrest's id is what a power verb needs"
        assert row.vmx_path.endswith("win11.vmx")
        assert row.is_running is True
        assert row.ip_address == "192.168.72.130"
    finally:
        db.close()


def test_a_local_row_keeps_the_default_source():
    """Every existing row and caller predates this field, so the default has to be the
    local one or the whole page starts claiming to be agent-synced."""
    from web_dashboard.models.vm import VMInfo
    assert VMInfo(vmx_path="/x.vmx", vm_name="x", workgroup="dev").source == "local"


def test_nothing_is_returned_when_no_connection_has_synced():
    db = SessionLocal()
    try:
        _setup(db, synced=False)
        assert vms_api._agent_workstation_vms(db, None) == []
    finally:
        db.close()


def test_no_workstation_connection_at_all_is_not_an_error():
    """The overwhelmingly common case: an install with no agent Workstation. It must
    cost the local list nothing."""
    db = SessionLocal()
    try:
        from web_dashboard.database import HypervisorConnection
        db.query(HypervisorConnection).delete()
        db.commit()
        assert vms_api._agent_workstation_vms(db, None) == []
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
