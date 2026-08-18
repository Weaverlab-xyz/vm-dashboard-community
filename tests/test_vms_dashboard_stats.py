"""`GET /api/vms/dashboard-stats` on a host with no local PowerShell.

The dashboard's on-prem numbers came from this endpoint, and it had both halves of the
bug bf8b4ff fixed for the `/vms` list route one function away:

  * `total_vms` counted `VMStateCache` only — a table written exclusively by the two
    PowerShell scan paths — so it could never include the Workstation rows a remote
    agent syncs, and a hosted install reported 0 while the page behind the number
    rendered rows.
  * `running_vms` called `powershell.execute("list_running_vms")` with no `try`, so on a
    host with no local PowerShell (the Azure Container App: the wrapper path is a Windows
    path that cannot exist) the whole endpoint 500d, taking the agent-sourced counts down
    with it.

So every test here drives the endpoint rather than a helper — the helpers were already
fine, and exercising them is exactly how the first version of this bug got shipped.

Real throwaway SQLite; no PowerShell and no agent.

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
    from web_dashboard.database import (Base, HypervisorConnection, HypervisorVMCache,
                                        Job, RemoteAgent, SessionLocal, VMStateCache,
                                        VMWorkgroupOverride, Workgroup, engine)
    from web_dashboard.services import cache_service
    from web_dashboard.services import hypervisor_connection_service as hcs
    from web_dashboard.services import powershell
    from web_dashboard.services import workgroup_override_service
except Exception as exc:  # noqa: BLE001
    print(f"SKIP: {exc}")
    sys.exit(0)

Base.metadata.create_all(bind=engine)


class _Admin:
    """Enough of a User for the endpoint: it reads these three and nothing else."""
    is_admin = True
    username = "tester"
    workgroups_list: list = []


# One event loop for every call in this module. cache_service holds a module-level
# asyncio.Lock, and a Lock binds to the first loop that takes it — a fresh
# `asyncio.run` per test would make the second one raise "bound to a different event
# loop", which looks nothing like the thing under test.
_LOOP = asyncio.new_event_loop()


def _stats(db):
    """Call the endpoint the way the route does."""
    return _LOOP.run_until_complete(vms_api.dashboard_stats(db=db, current_user=_Admin()))


def _setup(db, *, agent_vm=True, local_vm=False, running=True):
    db.query(HypervisorVMCache).delete()
    db.query(HypervisorConnection).delete()
    db.query(RemoteAgent).delete()
    db.query(VMStateCache).delete()
    db.query(Job).filter(Job.created_by == _Admin.username).delete()
    db.query(VMWorkgroupOverride).filter(
        VMWorkgroupOverride.provider == "workstation").delete()
    db.commit()

    # The running count is cached per workgroup set, so a hit left by an earlier test
    # would skip the fetcher and hide whichever failure this one is about. Cleared
    # directly rather than via `invalidate` so no event loop is needed here.
    cache_service._store.clear()
    cache_service._inflight.clear()
    cache_service._pending.clear()

    if local_vm:
        # What a PowerShell scan leaves behind, including the running state it stamped.
        db.add(VMStateCache(vmx_path=r"C:\VMs\dev\local.vmx", vm_name="local-lab",
                            workgroup="dev", is_running=running))
        db.commit()

    if agent_vm:
        agent = RemoteAgent(name="desk-01", public_key="k", is_active=True,
                            enrolled_at=datetime.utcnow(), last_seen_at=datetime.utcnow())
        db.add(agent)
        db.commit()
        db.refresh(agent)
        conn = hcs.create(db, kind="workstation", name="my-ws", created_by="t",
                          agent_id=agent.id, agent_connection_name="my-ws")
        db.add(HypervisorVMCache(
            connection_id=conn["id"], vm_id="AB12", name="win11-lab",
            power_state="poweredOn" if running else "poweredOff", vcpus=4, mem_mib=8192,
            ip_addresses=json.dumps(["192.168.72.130"]),
            scope=r"C:\VMs\win11\win11.vmx", vm_type="vm", tags="[]",
            synced_at=datetime.utcnow()))
        db.commit()


def _no_local_powershell():
    async def _raise(*_a, **_kw):
        raise powershell.PowerShellError(
            r"PowerShell wrapper not found: C:\Scripts\VM_CLI\vm_cli_api_wrapper.ps1",
            "WRAPPER_NOT_FOUND")
    return _raise


def _powershell_returning(vms):
    async def _ok(*_a, **_kw):
        return {"success": True, "vms": vms}
    return _ok


def test_the_endpoint_survives_a_host_with_no_powershell():
    """The regression that matters: this used to be a 500 on every single request from a
    cloud-hosted dashboard, because nothing ever lands in `VMStateCache` there to stop
    the fetcher firing and the wrapper path can never exist."""
    db = SessionLocal()
    original = powershell.execute
    try:
        _setup(db)
        powershell.execute = _no_local_powershell()
        out = _stats(db)
        assert out["total_vms"] == 1, "the agent's rows must outlive a failed local scan"
        assert out["running_vms"] == 1, \
            "an agent row's power state comes from its sync, not from PowerShell"
    finally:
        powershell.execute = original
        db.close()


def test_the_running_count_falls_back_to_stored_state():
    """Without PowerShell the last scan's persisted `is_running` is the best answer
    available for local rows — and a far better one than no response at all."""
    db = SessionLocal()
    original = powershell.execute
    try:
        _setup(db, agent_vm=False, local_vm=True, running=True)
        powershell.execute = _no_local_powershell()
        out = _stats(db)
        assert out["total_vms"] == 1
        assert out["running_vms"] == 1, "a stored running row still counts"

        # ...and a stopped one is not invented into a running one.
        _setup(db, agent_vm=False, local_vm=True, running=False)
        powershell.execute = _no_local_powershell()
        assert _stats(db)["running_vms"] == 0
    finally:
        powershell.execute = original
        db.close()


def test_the_total_counts_both_sources():
    """`VMStateCache` is written only by the PowerShell scans, so counting it alone made
    an agent-bound Workstation invisible to the dashboard no matter how healthy it was."""
    db = SessionLocal()
    original = powershell.execute
    try:
        _setup(db, agent_vm=True, local_vm=True, running=True)
        powershell.execute = _powershell_returning(
            [{"vmx_path": r"C:\VMs\dev\local.vmx", "vm_name": "local-lab"}])
        out = _stats(db)
        assert out["total_vms"] == 2, "one local + one agent-synced"
        assert out["running_vms"] == 2, \
            "the live local count and the agent's own state, added — not one or the other"
    finally:
        powershell.execute = original
        db.close()


def test_workgroup_counts_include_agent_rows():
    """These feed the per-workgroup badges. An agent VM tagged into a workgroup belongs
    in its badge for the same reason it belongs in the total."""
    db = SessionLocal()
    original = powershell.execute
    try:
        _setup(db)
        # set_many refuses a workgroup that does not exist, so create it first.
        if not db.query(Workgroup.id).filter(Workgroup.name == "dev").first():
            db.add(Workgroup(id="wg-dev-stats", name="dev", display_name="Dev"))
            db.commit()
        workgroup_override_service.set_many(
            db, provider="workstation", vm_ids=["AB12"], workgroup="dev")
        powershell.execute = _no_local_powershell()
        out = _stats(db)
        assert out["workgroup_counts"].get("dev") == 1, \
            "a tagged agent VM must reach its workgroup badge"
    finally:
        powershell.execute = original
        db.close()


def test_a_non_powershell_failure_still_surfaces():
    """The catch is scoped to PowerShellError on purpose. Swallowing everything would
    turn a broken cache or database into a silent zero — a wrong number is worse than
    no number, and it is the same mistake in the other direction."""
    db = SessionLocal()
    original = cache_service.get_or_refresh

    async def _cache_exploded(*_a, **_kw):
        raise RuntimeError("cache backend unreachable")

    try:
        _setup(db)
        cache_service.get_or_refresh = _cache_exploded
        try:
            _stats(db)
        except RuntimeError:
            pass
        else:
            raise AssertionError("a non-PowerShell failure must not be swallowed")
    finally:
        cache_service.get_or_refresh = original
        db.close()


def test_an_untagged_agent_vm_stays_out_of_a_non_admin_total():
    """The merge must not become a way to widen what a non-admin sees. Same rule as the
    list route: no `vm_workgroup_overrides` row means admin-only."""
    db = SessionLocal()
    original = powershell.execute

    class _NonAdmin:
        is_admin = False
        username = "tester"
        workgroups_list = ["dev"]

    try:
        _setup(db)
        powershell.execute = _no_local_powershell()
        out = _LOOP.run_until_complete(
            vms_api.dashboard_stats(db=db, current_user=_NonAdmin()))
        assert out["total_vms"] == 0, "an untagged agent VM is admin-only"
        assert out["running_vms"] == 0
    finally:
        powershell.execute = original
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
