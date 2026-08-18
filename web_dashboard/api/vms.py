"""Workstation VM operations.

Every row on this page comes from a remote agent. It did not always: this router used to
scan the dashboard's OWN host with PowerShell, because the dashboard used to run on the
Windows box that owned the VMs. That path is gone. It depended on an external wrapper
script at a fixed Windows path, which a Linux container cannot have, and it inferred a
VM's workgroup by string-matching its VMX path against `settings.workgroups` — a dict
that is empty in the community edition, so `_assert_workgroup_access` refused every
start, stop and ping with "Cannot determine workgroup from VM path". The page listed
agent-synced rows perfectly well and every button behind it dialled that dead path.

So: listing reads the synced cache, and power enqueues an agent job through the same
`agent_power_job` helper the Proxmox, vSphere, XCP-ng and Hyper-V routers use. A
workstation connection is agent-bound by construction (`AGENT_ONLY_KINDS`), so unlike
those four there is no direct path to fall back to.
"""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import User, Job, get_db
from ..models.vm import VMListResponse, VMInfo
from ..services import job_service, config_service
from ..services import hypervisor_sync_service, workgroup_override_service
from .auth import require_permission
from .hypervisor_deps import agent_power_job, conn_or_error

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/vms", tags=["vms"])


class PowerOpRequest(BaseModel):
    """A power request names the VM by the hypervisor's id, not by its path.

    vmrest addresses a VM by an opaque id; the VMX path is display-only, and the agent
    reports it as `scope`. `_clean_vm` drops empty strings, so a path is not even
    guaranteed to be present — keying an action on it is how two rows end up sharing one
    power state.
    """
    vm_id: str
    name: str = ""
    connection_id: str = ""


def _workstation_connections(db: Session) -> list:
    """Active, agent-bound Workstation connections. Best-effort by design."""
    from ..database import HypervisorConnection
    try:
        return (db.query(HypervisorConnection)
                .filter(HypervisorConnection.kind == "workstation",
                        HypervisorConnection.is_active.is_(True),
                        HypervisorConnection.agent_id.isnot(None)).all())
    except Exception:  # noqa: BLE001
        return []


def _agent_workstation_vms(db: Session, accessible: Optional[list]) -> list:
    """Workstation VMs the agents synced, as VMInfo rows.

    Workgroups come from `vm_workgroup_overrides`, exactly as they do for Proxmox and
    Nutanix: the cache has no workgroup of its own, and a VM with no override is
    admin-only. That is what stops an agent widening what a non-admin can see — an agent
    can report any VM it likes, and none of them become visible without an admin tagging
    them first.

    Best-effort per connection: one broken connection or empty cache costs the others
    nothing.
    """
    from ..database import RemoteAgent
    from ..services import hypervisor_view_service

    agent_names: dict = {}
    out = []
    for conn in _workstation_connections(db):
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
                os_type=vm.get("os_type") or None,
                source=source,
                vm_id=vm.get("vm_id"),
                connection_id=conn.id,
                synced_at=vm.get("synced_at"),
            ))
    return out


def _accessible(user: User) -> Optional[list]:
    """The workgroups this user may act on, or None for an admin.

    `is_effective_admin`, not the raw `is_admin` column: the property also honours a
    session grant and a live Entitle grant, and reading the column alone means a
    JIT-granted admin sees an empty page and cannot press a button on it.
    """
    if user.is_effective_admin:
        return None
    return [w.lower() for w in user.workgroups_list]


# ── List ──────────────────────────────────────────────────────────────────────

@router.get("", response_model=VMListResponse)
async def list_vms(
    workgroup: Optional[str] = Query(None, description="Filter by workgroup name"),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("vms", "read")),
):
    """Every Workstation VM the caller may see, from the synced cache."""
    vms = _agent_workstation_vms(db, _accessible(current_user))
    if workgroup:
        vms = [v for v in vms if v.workgroup == workgroup]

    # The OLDEST sync among the rows actually shown, and never now(). `cached_at` used to
    # be datetime.now(), so the page's "Updated 0s ago" was true of the response and said
    # nothing about the data — over rows that could be a day stale. A page merging several
    # agents is only as fresh as its stalest row.
    #
    # min() over these strings is chronological because every one of them is
    # HypervisorVMCache.synced_at.isoformat(): one format, naive UTC, uniform width.
    stamps = [v.synced_at for v in vms if v.synced_at]
    return VMListResponse(vms=vms, count=len(vms),
                          cached_at=min(stamps) if stamps else None)


@router.get("/dashboard-stats")
async def dashboard_stats(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("vms", "read")),
):
    """Counts for the dashboard tile, from the same source the page lists."""
    all_vms = _agent_workstation_vms(db, _accessible(current_user))

    active_jobs = (
        db.query(Job)
        .filter(Job.created_by == current_user.username,
                Job.status.in_(["pending", "queued", "running"]))
        .count()
    )

    wg_counts: dict = {}
    for vm in all_vms:
        wg_counts[vm.workgroup or ""] = wg_counts.get(vm.workgroup or "", 0) + 1

    return {
        "total_vms": len(all_vms),
        "running_vms": sum(1 for vm in all_vms if vm.is_running),
        "active_jobs": active_jobs,
        "workgroup_counts": wg_counts,
    }


# ── Sync ──────────────────────────────────────────────────────────────────────

@router.post("/sync")
async def sync_inventory(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("vms", "read")),
):
    """Ask every Workstation agent to re-read its host now.

    Enqueues the same job the scheduled pass enqueues, minus the cadence check — the
    dashboard has no route to vmrest, so the agent is the only thing that can look.
    Returns immediately; the page polls the listing until `cached_at` moves.
    """
    if not config_service.get_bool("remote_agents_enabled", False):
        raise HTTPException(
            status_code=409,
            detail=("Remote agents are disabled, so nothing can sync this page. Enable "
                    "them in Settings."))

    conns = _workstation_connections(db)
    if not conns:
        raise HTTPException(
            status_code=409,
            detail=("No active Workstation connection is bound to an agent. Add one on "
                    "the Connections page."))

    queued, skipped = [], []
    for conn in conns:
        job, reason = hypervisor_sync_service.sync_now(db, conn)
        if job is not None:
            queued.append({"connection": conn.name, "job_id": job.id})
        else:
            # Both lists, always. A response that says "0 queued" and nothing else is
            # precisely the silence this endpoint exists to break — the operator needs to
            # know it was the agent being offline, not the button being broken.
            skipped.append({"connection": conn.name,
                            "reason": reason or "a sync is already in flight"})
    return {"queued": queued, "skipped": skipped}


# ── Power ─────────────────────────────────────────────────────────────────────

def _workstation_workgroup(db: Session, vm_id: str) -> str:
    """The workgroup this VM is tagged into, or "".

    Read from `vm_workgroup_overrides` — the same table `_agent_workstation_vms` reads to
    decide who may SEE the row, so what you can act on and what you can see cannot
    disagree.
    """
    return workgroup_override_service.get(db, "workstation", vm_id) or ""


def _assert_workgroup_access(user: User, workgroup: str) -> None:
    accessible = _accessible(user)
    if accessible is None:
        return
    if not workgroup:
        raise HTTPException(
            status_code=403,
            detail=("This VM is not assigned to a workgroup, so only an admin can act "
                    "on it. An admin can tag it with Assign Workgroup."))
    if workgroup.lower() not in accessible:
        raise HTTPException(
            status_code=403, detail=f"Access denied to workgroup: {workgroup}")


def _power_endpoint(op: str):
    """One power route.

    The page op travels as-is; the per-kind translation to an agent verb belongs to
    `agent_power_job`, which reads the single table in `agent_hypervisor_meta.PAGE_OPS`
    rather than a copy kept here. Three routers once kept their own copy, and it mapped
    every page's graceful stop onto an op that hard-reset the guest.
    """

    async def _handler(
        payload: PowerOpRequest,
        db: Session = Depends(get_db),
        current_user: User = Depends(require_permission("vms", "write")),
    ):
        conn = conn_or_error(db, "workstation", payload.connection_id or None)
        # Before the job, not after: a refusal must leave nothing on /jobs for the
        # operator to wonder about.
        _assert_workgroup_access(
            current_user, _workstation_workgroup(db, payload.vm_id))

        label = payload.name or payload.vm_id
        job = agent_power_job(
            db, conn, op=op, target_id=payload.vm_id,
            target_scope="", target_type="vm",
            created_by=current_user.username,
            description=f"{op} {label}")
        if job is None:
            # `agent_power_job` returns None for a connection the dashboard could dial
            # itself. A Workstation connection can never be one — AGENT_ONLY_KINDS — so
            # this is a corrupt row, not a fallback worth writing.
            raise HTTPException(
                status_code=409,
                detail=(f"Connection '{conn.name}' is not bound to an agent. A "
                        f"Workstation connection has to be: the dashboard has no "
                        f"transport to vmrest."))

        job_service.log_audit(db, current_user.username, f"vm_{op}", target_vm=label)
        return {"job_id": job.id, "status": job.status}

    _handler.__name__ = f"workstation_{op}"
    return _handler


router.add_api_route("/power/start", _power_endpoint("start"), methods=["POST"],
                     summary="Power on a Workstation VM through its agent")
router.add_api_route("/power/stop", _power_endpoint("stop"), methods=["POST"],
                     summary="Power off a Workstation VM through its agent")
