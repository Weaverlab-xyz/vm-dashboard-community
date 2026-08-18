"""Agent-synced Workstation VMs on the /vms page.

Every row on that page now comes from a remote agent reporting its own host through
vmrest. It used to be a merge: the dashboard also scanned ITS OWN host with PowerShell,
back when it ran on the box that owned the VMs. That half is gone, and with it the
workgroup-from-VMX-path inference that refused every button on the page.

Two properties are worth guarding here.

**Permission.** An agent can report any VM it likes, so this must not become a way to
widen what a non-admin sees. Agent rows are gated by `vm_workgroup_overrides` exactly as
Proxmox and Nutanix rows are: no override means admin-only.

**Acting on a row you can see.** The workgroup an action checks has to be the same one
the listing filtered on, or the page shows a VM whose buttons refuse it — which is what
happened for every VM on every install, because the inference the action used could only
ever return "".

Real throwaway SQLite; no agent.

Runs under pytest, or standalone:  python tests/test_vms_agent_merge.py
"""
import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.environ.setdefault(
    "DATABASE_URL",
    "sqlite:///" + os.path.join(tempfile.mkdtemp(), "vms.db").replace("\\", "/"))
os.environ.setdefault("JWT_SECRET_KEY", "x" * 32)

try:
    from fastapi import HTTPException

    from web_dashboard.api import vms as vms_api
    from web_dashboard.database import (Base, HypervisorVMCache, RemoteAgent,
                                        SessionLocal, engine)
    from web_dashboard.services import hypervisor_connection_service as hcs
    from web_dashboard.services import workgroup_override_service
except Exception as exc:  # noqa: BLE001
    print(f"SKIP: {exc}")
    sys.exit(0)

Base.metadata.create_all(bind=engine)

_SYNCED_AT = datetime(2026, 8, 18, 12, 0, 0)


def _setup(db, *, synced=True, guest_os="windows9-64"):
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
            guest_os=guest_os, synced_at=_SYNCED_AT))
        db.commit()
    return conn


def _tag(db, vm_id, workgroup):
    """Tag through the real bulk-assign path, not a hand-written row."""
    from web_dashboard.database import Workgroup
    if not db.query(Workgroup.id).filter(Workgroup.name == workgroup).first():
        db.add(Workgroup(id=f"wg-{workgroup}-test", name=workgroup,
                         display_name=workgroup.title()))
        db.commit()
    workgroup_override_service.set_many(
        db, provider="workstation", vm_ids=[vm_id], workgroup=workgroup)


# ── Visibility ────────────────────────────────────────────────────────────────

def test_an_untagged_agent_vm_is_admin_only():
    """Same rule as every other hypervisor page. This is what stops an agent — which can
    report anything — widening what a non-admin sees."""
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
        _tag(db, "AB12", "dev")
        rows = vms_api._agent_workstation_vms(db, ["dev"])
        assert len(rows) == 1, "a tagged VM must reach its workgroup"
        assert rows[0].workgroup == "dev"
        assert rows[0].connection_id == conn["id"], (
            "a power op has to know which connection to dial")
        # And still not to a workgroup that does not own it.
        assert vms_api._agent_workstation_vms(db, ["finance"]) == []
    finally:
        db.close()


def test_an_agent_row_carries_everything_the_page_renders():
    db = SessionLocal()
    try:
        _setup(db)
        row = vms_api._agent_workstation_vms(db, None)[0]
        assert row.source == "desk-01", "the badge names the host the VM is really on"
        assert row.vm_id == "AB12", "vmrest's id is what a power verb needs"
        assert row.vmx_path.endswith("win11.vmx"), "the Path column"
        assert row.is_running is True
        assert row.ip_address == "192.168.72.130"
        assert row.synced_at, "the staleness line has nothing to read without this"
    finally:
        db.close()


def test_the_os_column_is_labelled_from_the_code_the_agent_reported():
    """End to end: a raw VMX code in the cache becomes a readable label on the row.

    `windows9-64` is Windows 10 — the code kept VMware's internal name when Microsoft
    skipped the number — so this also pins that the label is not derived by substring.
    """
    db = SessionLocal()
    try:
        _setup(db, guest_os="windows9-64")
        assert vms_api._agent_workstation_vms(db, None)[0].os_type == "Windows 10 (64-bit)"
    finally:
        db.close()


def test_an_agent_that_reports_no_os_leaves_the_column_empty():
    """An agent older than the guest_os key syncs fine and renders a dash, rather than
    claiming an OS it never reported."""
    db = SessionLocal()
    try:
        _setup(db, guest_os=None)
        assert vms_api._agent_workstation_vms(db, None)[0].os_type is None
    finally:
        db.close()


def test_nothing_is_returned_when_no_connection_has_synced():
    db = SessionLocal()
    try:
        _setup(db, synced=False)
        assert vms_api._agent_workstation_vms(db, None) == []
    finally:
        db.close()


def test_no_workstation_connection_at_all_is_not_an_error():
    """The overwhelmingly common case: an install with no agent Workstation."""
    db = SessionLocal()
    try:
        from web_dashboard.database import HypervisorConnection
        db.query(HypervisorConnection).delete()
        db.commit()
        assert vms_api._agent_workstation_vms(db, None) == []
    finally:
        db.close()


# ── The endpoint ──────────────────────────────────────────────────────────────

class _Admin:
    """Enough of a User for the endpoint. Note the EMPTY workgroups list: that is the
    normal state for an admin, and reading it instead of the admin flag is what used to
    refuse them every button on the page."""
    is_admin = True
    is_effective_admin = True
    username = "admin"
    workgroups_list: list = []


class _Dev:
    is_admin = False
    is_effective_admin = False
    username = "dev"
    workgroups_list = ["dev"]


def _list_vms(db, user=None):
    return asyncio.run(vms_api.list_vms(
        workgroup=None, db=db, current_user=user or _Admin()))


def test_cached_at_is_the_oldest_sync_not_the_time_of_the_response():
    """The staleness line's whole job. It used to be datetime.now(), so the page read
    "Updated 0s ago" over data that could be a day old — and the sync button's poll
    watched a value that changed on every request whether anything synced or not."""
    db = SessionLocal()
    try:
        conn = _setup(db)
        db.add(HypervisorVMCache(
            connection_id=conn["id"], vm_id="CD34", name="older-vm",
            power_state="poweredOff", ip_addresses="[]", tags="[]",
            scope="", vm_type="vm", synced_at=_SYNCED_AT - timedelta(hours=6)))
        db.commit()
        out = _list_vms(db)
        assert out.count == 2
        assert out.cached_at == (_SYNCED_AT - timedelta(hours=6)).isoformat(), (
            "a page merging several agents is only as fresh as its stalest row")
    finally:
        db.close()


def test_an_empty_page_reports_no_sync_time_rather_than_now():
    db = SessionLocal()
    try:
        _setup(db, synced=False)
        assert _list_vms(db).cached_at is None
    finally:
        db.close()


# ── Acting on a row ───────────────────────────────────────────────────────────

def test_the_workgroup_an_action_checks_is_the_one_the_listing_filtered_on():
    """The bug behind every dead button, in the form it actually took.

    The old code inferred a workgroup by matching the VMX path against
    `settings.workgroups`, which is {} in the community edition — so it returned "" for
    every VM and `_assert_workgroup_access` raised before anything ran. Reading the same
    override table the listing reads is the only way the two can agree.
    """
    db = SessionLocal()
    try:
        _setup(db)
        assert vms_api._workstation_workgroup(db, "AB12") == "", "untagged"
        _tag(db, "AB12", "dev")
        assert vms_api._workstation_workgroup(db, "AB12") == "dev"
    finally:
        db.close()


def test_an_admin_with_no_workgroups_may_act():
    """`_accessible` must read the admin flag, not the (normally empty) workgroups list.
    It also has to be `is_effective_admin`, so a session- or Entitle-granted admin is not
    left looking at a page of buttons that refuse them."""
    vms_api._assert_workgroup_access(_Admin(), "")          # must not raise
    vms_api._assert_workgroup_access(_Admin(), "anything")  # must not raise


def test_a_non_admin_is_refused_an_untagged_vm_with_a_usable_reason():
    try:
        vms_api._assert_workgroup_access(_Dev(), "")
    except HTTPException as exc:
        assert exc.status_code == 403
        assert "Assign Workgroup" in exc.detail, (
            "the refusal has to name the fix — it is the operator's whole diagnosis")
    else:
        raise AssertionError("an untagged VM must be refused to a non-admin")


def test_a_non_admin_is_refused_a_workgroup_they_do_not_hold():
    try:
        vms_api._assert_workgroup_access(_Dev(), "finance")
    except HTTPException as exc:
        assert exc.status_code == 403
    else:
        raise AssertionError("a foreign workgroup must be refused")


def test_workgroup_access_is_case_insensitive():
    """Overrides are stored canonical-lowercase by set_many, but a user's list is
    whatever it was typed as. Comparing raw would refuse the holder of their own
    workgroup."""
    class _Shouty:
        is_admin = False
        is_effective_admin = False
        username = "dev"
        workgroups_list = ["DEV"]

    vms_api._assert_workgroup_access(_Shouty(), "dev")  # must not raise


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
