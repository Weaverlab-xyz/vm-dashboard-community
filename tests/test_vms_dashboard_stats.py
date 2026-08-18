"""`GET /api/vms/dashboard-stats` — the numbers behind the dashboard's Workstation tile.

This endpoint's whole history is a counting bug: it counted `vm_state_cache`, the local
PowerShell scan, and nothing else. On a hosted install that table is empty and the scan
raises on every request, so the tile reported a total of 0 — and then 500d outright —
while the page behind the number rendered rows perfectly well.

There is one source now, so the class of bug is gone rather than fixed. What is left to
pin is that the tile counts the same rows the page lists, under the same RBAC, and that
an install with nothing synced gets zeros rather than an error.

Real throwaway SQLite; no agent.

Runs under pytest, or standalone:  python tests/test_vms_dashboard_stats.py
"""
import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.environ.setdefault(
    "DATABASE_URL",
    "sqlite:///" + os.path.join(tempfile.mkdtemp(), "stats.db").replace("\\", "/"))
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


def _setup(db, vms=(("AB12", "win11-lab", "poweredOn"),
                    ("CD34", "ubuntu-lab", "poweredOff"))):
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
    for vm_id, name, state in vms:
        db.add(HypervisorVMCache(
            connection_id=conn["id"], vm_id=vm_id, name=name, power_state=state,
            ip_addresses=json.dumps([]), scope="", vm_type="vm", tags="[]",
            synced_at=datetime.utcnow()))
    db.commit()
    return conn


def _tag(db, vm_id, workgroup):
    from web_dashboard.database import Workgroup
    if not db.query(Workgroup.id).filter(Workgroup.name == workgroup).first():
        db.add(Workgroup(id=f"wg-{workgroup}-stats", name=workgroup,
                         display_name=workgroup.title()))
        db.commit()
    workgroup_override_service.set_many(
        db, provider="workstation", vm_ids=[vm_id], workgroup=workgroup)


class _Admin:
    is_admin = True
    is_effective_admin = True
    username = "admin"
    workgroups_list: list = []


class _Dev:
    is_admin = False
    is_effective_admin = False
    username = "dev"
    workgroups_list = ["dev"]


def _stats(db, user=None):
    return asyncio.run(vms_api.dashboard_stats(db=db, current_user=user or _Admin()))


def test_an_install_with_nothing_synced_reports_zeros_rather_than_failing():
    """The shape of the original regression, kept: whatever is missing, the tile must
    render a number. A 500 here takes out the whole dashboard, not just this tile."""
    db = SessionLocal()
    try:
        from web_dashboard.database import HypervisorConnection
        db.query(HypervisorConnection).delete()
        db.query(HypervisorVMCache).delete()
        db.commit()
        out = _stats(db)
        assert out["total_vms"] == 0
        assert out["running_vms"] == 0
    finally:
        db.close()


def test_the_totals_count_the_synced_rows():
    db = SessionLocal()
    try:
        _setup(db)
        out = _stats(db)
        assert out["total_vms"] == 2
        assert out["running_vms"] == 1, "power state rides in with the sync"
    finally:
        db.close()


def test_the_tile_counts_exactly_what_the_page_lists():
    """The property the old endpoint broke: the number and the page behind it came from
    different sources, so a click on '0 VMs' opened a page full of rows."""
    db = SessionLocal()
    try:
        _setup(db)
        listed = vms_api._agent_workstation_vms(db, None)
        assert _stats(db)["total_vms"] == len(listed)
    finally:
        db.close()


def test_workgroup_counts_key_untagged_rows_under_the_empty_string():
    db = SessionLocal()
    try:
        _setup(db)
        _tag(db, "AB12", "dev")
        counts = _stats(db)["workgroup_counts"]
        assert counts.get("dev") == 1
        assert counts.get("") == 1, "an untagged VM still has to be counted somewhere"
    finally:
        db.close()


def test_an_untagged_agent_vm_stays_out_of_a_non_admin_total():
    """Same RBAC as the listing. A count is a disclosure too: telling a non-admin there
    are five VMs they cannot see is still telling them there are five VMs."""
    db = SessionLocal()
    try:
        _setup(db)
        _tag(db, "AB12", "dev")
        assert _stats(db, _Dev())["total_vms"] == 1
        assert _stats(db, _Admin())["total_vms"] == 2
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
