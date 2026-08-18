"""
VM operations API endpoints.
All mutating operations (start/stop) are dispatched as background Celery tasks.
"""
import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional
import json

from fastapi import APIRouter, Depends, HTTPException, Query, BackgroundTasks
from sqlalchemy.orm import Session

from ..config import settings
from ..database import User, Job, get_db
from ..models.vm import (
    VMListResponse,
    VMInfo,
    VMStartRequest,
    VMStopRequest,
    BulkStartRequest,
    BulkStopRequest,
    VMDecommissionRequest,
    VMOperationResponse,
    VMOnlineCheckRequest,
)
from ..services import powershell, job_service, cache_service
from ..services import vm_inventory_service
from .auth import get_current_user, require_permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/vms", tags=["vms"])


# ── List endpoints ────────────────────────────────────────────────────────────

def _agent_workstation_vms(db: Session, accessible: list) -> list:
    """Workstation VMs a remote agent synced, as VMInfo rows.

    The local path scans this host with PowerShell; a co-located agent reports its own
    host through vmrest. Both are Workstation VMs and belong on the same page, so they
    are merged here rather than given a second one — with a `source` so it is always
    clear which host a row came from.

    Workgroups come from `vm_workgroup_overrides`, exactly as they do for Proxmox and
    Nutanix: the cache has no workgroup of its own, and a VM with no override is
    admin-only. That is what stops an agent widening what a non-admin can see — an
    agent can report any VM it likes, and none of them become visible without an
    admin tagging them first.

    Best-effort: a broken connection or an empty cache costs the local list nothing.
    """
    from ..database import HypervisorConnection, RemoteAgent
    from ..services import hypervisor_sync_service, hypervisor_view_service
    from ..services import workgroup_override_service

    try:
        rows = (db.query(HypervisorConnection)
                .filter(HypervisorConnection.kind == "workstation",
                        HypervisorConnection.is_active.is_(True),
                        HypervisorConnection.agent_id.isnot(None)).all())
    except Exception:  # noqa: BLE001
        return []

    agent_names = {}
    out = []
    for conn in rows:
        try:
            cached = hypervisor_view_service.project(
                "workstation", hypervisor_sync_service.list_vms(db, conn.id))
        except Exception:  # noqa: BLE001
            logger.warning("could not read the synced inventory for %s", conn.name)
            continue
        if not cached:
            continue

        if conn.agent_id not in agent_names:
            agent = db.query(RemoteAgent).filter(
                RemoteAgent.id == conn.agent_id).first()
            agent_names[conn.agent_id] = agent.name if agent else conn.name
        source = agent_names[conn.agent_id]

        overrides = workgroup_override_service.get_many(
            db, "workstation", [vm["vm_id"] for vm in cached])
        for vm in cached:
            workgroup = overrides.get(vm["vm_id"])
            # Same rule as every other hypervisor page: no override means admin-only.
            if accessible is not None and (workgroup is None
                                           or workgroup.lower() not in accessible):
                continue
            ips = vm.get("ip_addresses") or []
            out.append(VMInfo(
                vmx_path=vm.get("vmx_path") or "",
                vm_name=vm.get("name") or "",
                workgroup=workgroup or "",
                is_running=vm.get("is_running"),
                ip_address=ips[0] if ips else None,
                source=source,
                vm_id=vm.get("vm_id"),
            ))
    return out


@router.get("", response_model=VMListResponse)
async def list_vms(
    background_tasks: BackgroundTasks,
    workgroup: Optional[str] = Query(None, description="Filter by workgroup name"),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("vms", "read")),
):
    """
    List all available VMX files filtered to the user's accessible workgroups.
    First call (empty DB): runs a full PS scan and populates the DB.
    Subsequent calls: returns DB rows instantly, queues a background delta sync
    to catch any net-new VMX files, and periodically runs a full reconciliation.
    """
    accessible = current_user.workgroups_list

    count = vm_inventory_service.count_vms_in_db(db, accessible)
    if count == 0:
        # Cold start — block until the DB is populated.
        #
        # Best-effort, and that asymmetry is the point: the local scan is one SOURCE for
        # this page, not a precondition for serving it. On a cloud-hosted dashboard there
        # is no local hypervisor to scan, so this raises on EVERY request — nothing ever
        # lands in VMStateCache to make the branch stop firing — and letting it propagate
        # took the whole page down with it, including the agent-sourced rows merged in
        # below, which need no PowerShell at all. That is the exact reverse of the rule
        # `_agent_workstation_vms` already keeps for the other direction.
        try:
            await vm_inventory_service.populate_db_from_ps(db, accessible)
        except powershell.PowerShellError as exc:
            # Every "PowerShell is not reachable from here" case funnels into this one
            # type — a missing wrapper, a launch failure, a timeout, an Automation error.
            # A database failure inside the scan is NOT caught, and should still 500.
            logger.warning(
                "the local VMX scan is unavailable, serving agent-synced rows only: %s",
                exc)
    else:
        # Warm path — return immediately, sync in background
        background_tasks.add_task(_bg_delta_sync, accessible)

    vms = vm_inventory_service.get_vms_from_db(db, accessible)
    # A co-located agent reports its own host's Workstation VMs. They are the same kind
    # of thing as the local ones, so they belong on this page rather than a second one.
    vms = vms + _agent_workstation_vms(
        db, None if current_user.is_admin else
        [w.lower() for w in current_user.workgroups_list])

    if workgroup:
        vms = [v for v in vms if v.workgroup == workgroup]

    return VMListResponse(
        vms=vms,
        count=len(vms),
        cached_at=datetime.now(timezone.utc).isoformat(),
    )


@router.post("/sync")
async def sync_vm_inventory(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("vms", "read")),
):
    """
    Trigger an immediate full PS scan and reconcile the VM inventory DB.
    Returns counts of added/updated/removed VMs.
    Wired to the Refresh button in the VM list page.
    """
    accessible = current_user.workgroups_list
    counts = await vm_inventory_service.populate_db_from_ps(db, accessible)
    counts["synced_at"] = datetime.now(timezone.utc).isoformat()
    return counts


@router.get("/running", response_model=VMListResponse)
async def list_running_vms(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("vms", "read")),
):
    """List currently running VMs accessible to the current user.
    Results are served from cache (5 min TTL, stale-while-revalidate).
    Running state is also persisted to VMStateCache so subsequent list_vms
    calls can show is_running without an extra PS call."""
    cache_key = cache_service.key_workgroups("vms_running", current_user.workgroups_list)
    ttl = cache_service.TTL["vms_running"]

    async def _fetch():
        result = await powershell.execute("list_running_vms", {})
        return result.get("vms", [])

    running, cached_at = await cache_service.get_or_refresh(cache_key, ttl, _fetch)

    accessible = current_user.workgroups_list
    filtered = [vm for vm in running if not accessible or _workgroup_from_path(vm.get("vmx_path", "")) in accessible]

    # Persist running state to DB (non-blocking — run in background thread)
    asyncio.create_task(_persist_running_state(filtered))

    return VMListResponse(
        vms=[VMInfo(vmx_path=v["vmx_path"], vm_name=v.get("vm_name", ""), workgroup=_workgroup_from_path(v["vmx_path"]), ip_address=v.get("ip_address"), os_type=v.get("os_type")) for v in filtered],
        count=len(filtered),
        cached_at=cached_at,
    )


@router.post("/running/refresh")
async def refresh_running_vms(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("vms", "read")),
):
    """
    Trigger a non-blocking background refresh of the running VM cache.
    Returns immediately; clients should poll GET /api/vms/running until cached_at changes.
    Works in both dev (SSH) and automation (Azure Hybrid Worker) modes — the background
    task runs in the asyncio event loop so it is not subject to gunicorn's worker timeout.
    """
    cache_key = cache_service.key_workgroups("vms_running", current_user.workgroups_list)
    ttl = cache_service.TTL["vms_running"]
    workgroups = current_user.workgroups_list

    async def _bg_fetch():
        import logging as _log
        _logger = _log.getLogger(__name__)
        try:
            result = await powershell.execute("list_running_vms", {})
            running = result.get("vms", [])
            filtered = [
                vm for vm in running
                if not workgroups or _workgroup_from_path(vm.get("vmx_path", "")) in workgroups
            ]
            # Write directly to cache (bypasses TTL check — this is a forced refresh)
            await cache_service.set(cache_key, filtered, ttl)
            # Persist to DB so subsequent /api/vms calls reflect current state
            await _persist_running_state(filtered)
            _logger.info("refresh_running: %d running VM(s) cached", len(filtered))
        except Exception as exc:
            _logger.warning("refresh_running bg task failed: %s", exc)

    asyncio.create_task(_bg_fetch())
    return {"status": "refresh_started", "message": "Running VM refresh queued in background"}


@router.post("/check-online")
async def check_online_workgroup(
    workgroup: Optional[str] = Query(None, description="Check only this workgroup; omit for all accessible"),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("vms", "read")),
):
    """
    Check network reachability (TCP connect) for VMs in one or all accessible workgroups.
    Uses Python stdlib — no PowerShell required. Updates is_online + last_online_check_at in DB.
    """
    if workgroup:
        _assert_workgroup_access(current_user, workgroup)
        workgroups_to_check = [workgroup]
    else:
        workgroups_to_check = current_user.workgroups_list

    def _do_checks():
        results = []
        for wg in workgroups_to_check:
            results.extend(vm_inventory_service.check_workgroup_online(db, wg))
        return results

    results = await asyncio.to_thread(_do_checks)
    return {"results": results, "checked_at": datetime.now(timezone.utc).isoformat()}


@router.post("/check-online/single")
async def check_online_single(
    req: VMOnlineCheckRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("vms", "read")),
):
    """Check network reachability for a single VM by vmx_path."""
    wg = _workgroup_from_path(req.vmx_path)
    _assert_workgroup_access(current_user, wg)
    result = await asyncio.to_thread(vm_inventory_service.check_vm_online, db, req.vmx_path)
    return result


@router.get("/dashboard-stats")
async def dashboard_stats(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("vms", "read")),
):
    """Single-call endpoint for the dashboard: VM counts, active jobs, per-workgroup counts.

    Counts the same two sources `GET /api/vms` lists — the PowerShell-scanned
    `VMStateCache` rows plus the Workstation rows a remote agent syncs — so the numbers
    on the dashboard match the page they link to. Totals come from the DB (instant); the
    running count prefers the PS-backed cache and degrades to stored state without it.
    """
    accessible = current_user.workgroups_list

    # VM inventory from DB — fast
    local_vms = vm_inventory_service.get_vms_from_db(db, accessible)
    # The same merge the list route does, for the same reason: `VMStateCache` is written
    # only by the two PowerShell scan paths, so counting it alone can never include an
    # agent-synced VM — a hosted install reported a total of 0 while the page behind the
    # number rendered rows.
    agent_vms = _agent_workstation_vms(
        db, None if current_user.is_admin else [w.lower() for w in accessible])
    all_vms = local_vms + agent_vms

    # An agent row's power state arrives with its sync, so it is counted whether or not
    # there is any local PowerShell to ask about the local ones.
    agent_running = sum(1 for vm in agent_vms if vm.is_running)

    running_key = cache_service.key_workgroups("vms_running", accessible)

    async def _fetch_running():
        return (await powershell.execute("list_running_vms", {})).get("vms", [])

    try:
        running_vms, _ = await cache_service.get_or_refresh(
            running_key, cache_service.TTL["vms_running"], _fetch_running
        )
        local_running = sum(
            1 for vm in running_vms
            if not accessible or _workgroup_from_path(vm.get("vmx_path", "")) in accessible
        )
    except powershell.PowerShellError as exc:
        # The local scan is one SOURCE for this count, not a precondition for serving it —
        # the rule `list_vms` already keeps one function away. On a cloud-hosted dashboard
        # the wrapper path is a Windows path that cannot exist, so this raised on EVERY
        # request and took the whole endpoint down with it, including the agent-sourced
        # numbers that need no PowerShell at all. Fall back to the running state the last
        # scan persisted; a cache or database failure is NOT caught and should still 500.
        logger.warning(
            "the local running-VM scan is unavailable, counting stored state instead: %s",
            exc)
        local_running = sum(1 for vm in local_vms if vm.is_running)

    running_count = local_running + agent_running

    active_jobs = (
        db.query(Job)
        .filter(Job.created_by == current_user.username, Job.status.in_(["pending", "running"]))
        .count()
    )

    wg_counts: dict[str, int] = {}
    for vm in all_vms:
        wg = vm.workgroup or ""
        wg_counts[wg] = wg_counts.get(wg, 0) + 1

    return {
        "total_vms": len(all_vms),
        "running_vms": running_count,
        "active_jobs": active_jobs,
        "workgroup_counts": wg_counts,
    }


# ── Single-VM operations ──────────────────────────────────────────────────────

@router.post("/start", response_model=VMOperationResponse)
async def start_vm(
    req: VMStartRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("vms", "write")),
):
    """
    Start a single VM. Dispatches a background task and returns a job ID
    that can be polled via GET /api/jobs/{job_id} or tracked via WebSocket.
    """
    wg = _workgroup_from_path(req.vmx_path)
    _assert_workgroup_access(current_user, wg)

    if job_service.has_active_job_for_vm(db, req.vmx_path):
        raise HTTPException(status_code=409, detail="An operation is already running for this VM")

    job = job_service.create_job(
        db,
        job_type="vm_start",
        created_by=current_user.username,
        vm_path=req.vmx_path,
        workgroup=wg,
        metadata={"ip_wait_timeout": req.ip_wait_timeout},
    )

    job_service.log_audit(db, current_user.username, "vm_start", target_vm=req.vmx_path)

    # Dispatch background task
    background_tasks.add_task(_run_start_vm, job.id, req.vmx_path, req.ip_wait_timeout)

    return VMOperationResponse(
        job_id=job.id,
        status="pending",
        message="VM start operation queued",
    )


@router.post("/stop", response_model=VMOperationResponse)
async def stop_vm(
    req: VMStopRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("vms", "write")),
):
    """Stop a single VM."""
    wg = _workgroup_from_path(req.vmx_path)
    _assert_workgroup_access(current_user, wg)

    if job_service.has_active_job_for_vm(db, req.vmx_path):
        raise HTTPException(status_code=409, detail="An operation is already running for this VM")

    job = job_service.create_job(
        db,
        job_type="vm_stop",
        created_by=current_user.username,
        vm_path=req.vmx_path,
        workgroup=wg,
    )

    job_service.log_audit(db, current_user.username, "vm_stop", target_vm=req.vmx_path)

    background_tasks.add_task(_run_stop_vm, job.id, req.vmx_path)

    return VMOperationResponse(
        job_id=job.id,
        status="pending",
        message="VM stop operation queued",
    )


# ── Decommission ─────────────────────────────────────────────────────────────

@router.post("/decommission", response_model=VMOperationResponse)
async def decommission_vm(
    req: VMDecommissionRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("vms", "delete")),
):
    """
    Decommission a VM: stop it and mark it as decommissioned in BeyondTrust.
    Optionally schedules the VM folder for deletion the following day at 2:00 AM.
    """
    wg = _workgroup_from_path(req.vmx_path)
    _assert_workgroup_access(current_user, wg)

    if job_service.has_active_job_for_vm(db, req.vmx_path):
        raise HTTPException(status_code=409, detail="An operation is already running for this VM")

    job = job_service.create_job(
        db,
        job_type="vm_decommission",
        created_by=current_user.username,
        vm_path=req.vmx_path,
        workgroup=wg,
        metadata={"delete_folder": req.delete_folder},
    )

    job_service.log_audit(db, current_user.username, "vm_decommission", target_vm=req.vmx_path)

    background_tasks.add_task(_run_decommission_vm, job.id, req.vmx_path, req.delete_folder, req.guest_password)

    return VMOperationResponse(
        job_id=job.id,
        status="pending",
        message="Decommission job queued",
    )


# ── Bulk operations ───────────────────────────────────────────────────────────

@router.post("/bulk/start", response_model=VMOperationResponse)
async def bulk_start(
    req: BulkStartRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("vms", "write")),
):
    """Start all VMs in a workgroup (3-phase: start → wait for IPs → update BeyondTrust)."""
    _assert_workgroup_access(current_user, req.workgroup)
    if req.workgroup not in settings.workgroups:
        raise HTTPException(status_code=400, detail=f"Unknown workgroup: {req.workgroup}")

    job = job_service.create_job(
        db,
        job_type="bulk_start",
        created_by=current_user.username,
        workgroup=req.workgroup,
    )
    job_service.log_audit(db, current_user.username, "bulk_start", details={"workgroup": req.workgroup})

    background_tasks.add_task(_run_bulk_start, job.id, req.workgroup)

    return VMOperationResponse(
        job_id=job.id,
        status="pending",
        message=f"Bulk start queued for workgroup '{req.workgroup}'",
    )


@router.post("/bulk/stop", response_model=VMOperationResponse)
async def bulk_stop(
    req: BulkStopRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("vms", "write")),
):
    """Stop all VMs in a workgroup."""
    _assert_workgroup_access(current_user, req.workgroup)
    if req.workgroup not in settings.workgroups:
        raise HTTPException(status_code=400, detail=f"Unknown workgroup: {req.workgroup}")

    job = job_service.create_job(
        db,
        job_type="bulk_stop",
        created_by=current_user.username,
        workgroup=req.workgroup,
    )
    job_service.log_audit(db, current_user.username, "bulk_stop", details={"workgroup": req.workgroup})

    background_tasks.add_task(_run_bulk_stop, job.id, req.workgroup)

    return VMOperationResponse(
        job_id=job.id,
        status="pending",
        message=f"Bulk stop queued for workgroup '{req.workgroup}'",
    )


@router.post("/inject-ips", response_model=VMOperationResponse)
async def inject_vm_ips(
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("vms", "write")),
):
    """
    Inject guestinfo.ip into VMX files for all VMs matched in Password Safe.
    Handles running and stopped VMs. Useful for Linux VMs where vmrun
    getGuestIPAddress never responds (e.g. OpenSUSE).
    """
    job = job_service.create_job(
        db,
        job_type="inject_vm_ips",
        created_by=current_user.username,
    )
    job_service.log_audit(db, current_user.username, "inject_vm_ips")
    background_tasks.add_task(_run_inject_vm_ips, job.id)
    return VMOperationResponse(
        job_id=job.id,
        status="pending",
        message="IP injection job queued",
    )


# ── Inventory background helpers ──────────────────────────────────────────────

async def _bg_delta_sync(workgroups: list[str]) -> None:
    """Background task: delta sync + periodic full reconciliation."""
    db = vm_inventory_service.get_fresh_db()
    try:
        await vm_inventory_service.delta_sync(db, workgroups)
        await vm_inventory_service.maybe_full_sync(db, workgroups)
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("bg delta sync failed: %s", exc)
    finally:
        db.close()


async def _persist_running_state(running_vms: list[dict]) -> None:
    """Background coroutine: stamp is_running + ip_address on VMStateCache rows."""
    db = vm_inventory_service.get_fresh_db()
    try:
        import asyncio as _asyncio
        await _asyncio.to_thread(vm_inventory_service.sync_running_state, db, running_vms)
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("persist running state failed: %s", exc)
    finally:
        db.close()


# ── Background task runners ───────────────────────────────────────────────────
# These run in FastAPI's built-in background task thread pool.
# For truly long-running ops (ISO build, AWS import) Phase 3 will use Celery.

def _get_db_session():
    """Get a direct DB session for use in background tasks."""
    from ..database import SessionLocal
    return SessionLocal()


async def _run_start_vm(job_id: str, vmx_path: str, ip_wait_timeout: int):
    from .websocket import broadcast_progress
    db = _get_db_session()
    try:
        job_service.set_running(db, job_id)
        current_pct, current_msg = 5, "Starting VM…"
        await broadcast_progress(job_id, current_pct, current_msg)

        result_data = None
        async for event in powershell.execute_streaming("start_vm", {
            "vmx_path": vmx_path,
            "ip_wait_timeout": ip_wait_timeout,
        }):
            if event["type"] == "progress":
                current_pct, current_msg = event["pct"], event["message"]
                await broadcast_progress(job_id, current_pct, current_msg)
            elif event["type"] == "log":
                await broadcast_progress(job_id, current_pct, current_msg, log_line=event["line"])
            elif event["type"] == "result":
                result_data = event["data"]

        if not result_data or not result_data.get("success"):
            raise powershell.PowerShellError(
                (result_data or {}).get("error", "No result returned from wrapper"), "PS_ERROR"
            )

        job_service.set_completed(db, job_id, result_data)
        await cache_service.invalidate_prefix("vms_running")
        # Stamp DB so the next page load shows is_running=True without a PS call
        ip = result_data.get("ip_address")
        vm_inventory_service.set_vm_running_in_db(db, vmx_path, ip_address=ip)
    except powershell.PowerShellError as e:
        job_service.set_failed(db, job_id, str(e))
    except Exception as e:
        job_service.set_failed(db, job_id, f"Unexpected error: {e}")
    finally:
        db.close()


async def _run_stop_vm(job_id: str, vmx_path: str):
    from .websocket import broadcast_progress
    db = _get_db_session()
    try:
        job_service.set_running(db, job_id)
        current_pct, current_msg = 5, "Stopping VM…"
        await broadcast_progress(job_id, current_pct, current_msg)

        result_data = None
        async for event in powershell.execute_streaming("stop_vm", {
            "vmx_path": vmx_path,
        }):
            if event["type"] == "progress":
                current_pct, current_msg = event["pct"], event["message"]
                await broadcast_progress(job_id, current_pct, current_msg)
            elif event["type"] == "log":
                await broadcast_progress(job_id, current_pct, current_msg, log_line=event["line"])
            elif event["type"] == "result":
                result_data = event["data"]

        if not result_data or not result_data.get("success"):
            raise powershell.PowerShellError(
                (result_data or {}).get("error", "No result returned from wrapper"), "PS_ERROR"
            )

        job_service.set_completed(db, job_id, result_data)
        await cache_service.invalidate_prefix("vms_running")
        # Stamp DB so the next page load shows is_running=False without a PS call
        vm_inventory_service.set_vm_stopped_in_db(db, vmx_path)
    except powershell.PowerShellError as e:
        job_service.set_failed(db, job_id, str(e))
    except Exception as e:
        job_service.set_failed(db, job_id, f"Unexpected error: {e}")
    finally:
        db.close()


async def _run_bulk_start(job_id: str, workgroup: str):
    from .websocket import broadcast_progress
    db = _get_db_session()
    try:
        job_service.set_running(db, job_id)
        current_pct, current_msg = 5, f"Starting bulk operation for {workgroup}..."
        await broadcast_progress(job_id, current_pct, current_msg)

        result_data = None
        async for event in powershell.execute_streaming("bulk_start", {"workgroup": workgroup}):
            if event["type"] == "progress":
                current_pct, current_msg = event["pct"], event["message"]
                await broadcast_progress(job_id, current_pct, current_msg)
            elif event["type"] == "log":
                await broadcast_progress(job_id, current_pct, current_msg, log_line=event["line"])
            elif event["type"] == "result":
                result_data = event["data"]

        if not result_data or not result_data.get("success"):
            raise powershell.PowerShellError(
                (result_data or {}).get("error", "No result returned from wrapper"), "PS_ERROR"
            )

        job_service.set_completed(db, job_id, result_data)
        await cache_service.invalidate_prefix("vms_running")
    except powershell.PowerShellError as e:
        job_service.set_failed(db, job_id, str(e))
    except Exception as e:
        job_service.set_failed(db, job_id, f"Unexpected error: {e}")
    finally:
        db.close()


async def _run_decommission_vm(job_id: str, vmx_path: str, delete_folder: bool, guest_password: str = ""):
    from .websocket import broadcast_progress
    db = _get_db_session()
    try:
        job_service.set_running(db, job_id)
        current_pct, current_msg = 5, "Starting VM decommission…"
        await broadcast_progress(job_id, current_pct, current_msg)

        result_data = None
        async for event in powershell.execute_streaming("decommission_vm", {
            "vmx_path": vmx_path,
            "delete_folder": delete_folder,
            "guest_password": guest_password,
        }):
            if event["type"] == "progress":
                current_pct, current_msg = event["pct"], event["message"]
                await broadcast_progress(job_id, current_pct, current_msg)
            elif event["type"] == "log":
                await broadcast_progress(job_id, current_pct, current_msg, log_line=event["line"])
            elif event["type"] == "result":
                result_data = event["data"]

        if not result_data or not result_data.get("success"):
            raise powershell.PowerShellError(
                (result_data or {}).get("error", "No result returned from wrapper"), "PS_ERROR"
            )

        job_service.set_completed(db, job_id, result_data)

    except powershell.PowerShellError as e:
        job_service.set_failed(db, job_id, str(e))
    except Exception as e:
        job_service.set_failed(db, job_id, f"Unexpected error: {e}")
    finally:
        db.close()


async def _run_bulk_stop(job_id: str, workgroup: str):
    from .websocket import broadcast_progress
    db = _get_db_session()
    try:
        job_service.set_running(db, job_id)
        current_pct, current_msg = 5, f"Stopping workgroup {workgroup}..."
        await broadcast_progress(job_id, current_pct, current_msg)

        result_data = None
        async for event in powershell.execute_streaming("bulk_stop", {"workgroup": workgroup}):
            if event["type"] == "progress":
                current_pct, current_msg = event["pct"], event["message"]
                await broadcast_progress(job_id, current_pct, current_msg)
            elif event["type"] == "log":
                await broadcast_progress(job_id, current_pct, current_msg, log_line=event["line"])
            elif event["type"] == "result":
                result_data = event["data"]

        if not result_data or not result_data.get("success"):
            raise powershell.PowerShellError(
                (result_data or {}).get("error", "No result returned from wrapper"), "PS_ERROR"
            )

        job_service.set_completed(db, job_id, result_data)
        await cache_service.invalidate_prefix("vms_running")
    except powershell.PowerShellError as e:
        job_service.set_failed(db, job_id, str(e))
    except Exception as e:
        job_service.set_failed(db, job_id, f"Unexpected error: {e}")
    finally:
        db.close()


async def _run_inject_vm_ips(job_id: str):
    from .websocket import broadcast_progress
    db = _get_db_session()
    try:
        job_service.set_running(db, job_id)
        current_pct, current_msg = 5, "Starting IP injection…"
        await broadcast_progress(job_id, current_pct, current_msg)

        result_data = None
        async for event in powershell.execute_streaming("inject_vm_ips", {}):
            if event["type"] == "progress":
                current_pct, current_msg = event["pct"], event["message"]
                await broadcast_progress(job_id, current_pct, current_msg)
            elif event["type"] == "log":
                await broadcast_progress(job_id, current_pct, current_msg, log_line=event["line"])
            elif event["type"] == "result":
                result_data = event["data"]

        if not result_data or not result_data.get("success"):
            raise powershell.PowerShellError(
                (result_data or {}).get("error", "No result from IP injection"), "PS_ERROR"
            )

        job_service.set_completed(db, job_id, result_data)
    except powershell.PowerShellError as e:
        job_service.set_failed(db, job_id, str(e))
    except Exception as e:
        job_service.set_failed(db, job_id, f"Unexpected error: {e}")
    finally:
        db.close()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _workgroup_from_path(vmx_path: str) -> str:
    """
    Infer workgroup from a VMX path by matching against configured workgroup paths.
    Falls back to empty string if no match found.
    """
    path_lower = vmx_path.replace("\\", "/").lower()
    for wg, wg_path in settings.workgroups.items():
        if wg_path.replace("\\", "/").lower() in path_lower:
            return wg
    return ""


def _assert_workgroup_access(user: User, workgroup: str):
    if not workgroup:
        raise HTTPException(status_code=400, detail="Cannot determine workgroup from VM path")
    if workgroup not in user.workgroups_list:
        raise HTTPException(
            status_code=403, detail=f"Access denied to workgroup: {workgroup}"
        )
