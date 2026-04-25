"""
VMware vSphere API router.

All endpoints require authentication.  Power operations are dispatched as
background jobs so the client gets a job ID immediately and can poll
/api/jobs/{id} for progress.
"""
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_db
from ..auth import get_current_user
from ..models.user import User
from ..services import job_service
from ..services import vsphere_service
from ..services.vsphere_service import VSphereError

router = APIRouter(prefix="/api/vsphere", tags=["vsphere"])


# ── List endpoints ────────────────────────────────────────────────────────────

@router.get("/datacenters")
async def get_datacenters(current_user: User = Depends(get_current_user)):
    """List all vSphere datacenters (returns ['ha-datacenter'] for standalone ESXi)."""
    try:
        return await vsphere_service.list_datacenters()
    except VSphereError as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/hosts")
async def get_hosts(current_user: User = Depends(get_current_user)):
    """List all ESXi hosts with resource summary."""
    try:
        return await vsphere_service.list_hosts()
    except VSphereError as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/vms")
async def get_vms(
    datacenter: str = "",
    current_user: User = Depends(get_current_user),
):
    """List all VMs. Pass ?datacenter=<name> to filter to one datacenter."""
    try:
        return await vsphere_service.list_vms(datacenter)
    except VSphereError as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/vms/{moref}")
async def get_vm_detail(
    moref: str,
    current_user: User = Depends(get_current_user),
):
    """Get full detail for one VM by its managed object reference ID."""
    try:
        return await vsphere_service.get_vm(moref)
    except VSphereError as e:
        raise HTTPException(status_code=502, detail=str(e))


# ── Power operations ──────────────────────────────────────────────────────────

class PowerOpRequest(BaseModel):
    moref: str
    name: str = ""
    host: str = ""


async def _run_power_op(job_id: str, moref: str, op: str, label: str):
    from ..database import SessionLocal
    db = SessionLocal()
    try:
        job_service.update_progress(db, job_id, 10, f"{op.capitalize()}ing {label}…")
        result = await vsphere_service.power_op(moref, op)
        job_service.set_completed(db, job_id, result)
    except Exception as e:
        job_service.set_failed(db, job_id, str(e))
    finally:
        db.close()


def _power_endpoint(op: str):
    async def _handler(
        payload: PowerOpRequest,
        background_tasks: BackgroundTasks,
        db: Session = Depends(get_db),
        current_user: User = Depends(get_current_user),
    ):
        label = payload.name or payload.moref
        job = job_service.create_job(
            db,
            job_type=f"vsphere_{op}",
            description=f"vSphere {op}: {label}" + (f" on {payload.host}" if payload.host else ""),
            workgroup=payload.host or "vsphere",
            owner_id=current_user.id,
        )
        background_tasks.add_task(_run_power_op, job.id, payload.moref, op, label)
        return {"job_id": job.id, "status": "queued"}

    _handler.__name__ = f"vsphere_{op}"
    return _handler


router.add_api_route(
    "/power/start",
    _power_endpoint("start"),
    methods=["POST"],
    summary="Power on a VM",
)
router.add_api_route(
    "/power/shutdown",
    _power_endpoint("shutdown"),
    methods=["POST"],
    summary="Gracefully shut down a VM (requires VMware Tools)",
)
router.add_api_route(
    "/power/stop",
    _power_endpoint("stop"),
    methods=["POST"],
    summary="Force power off a VM",
)
router.add_api_route(
    "/power/reset",
    _power_endpoint("reset"),
    methods=["POST"],
    summary="Reset (hard reboot) a VM",
)
router.add_api_route(
    "/power/suspend",
    _power_endpoint("suspend"),
    methods=["POST"],
    summary="Suspend a VM to memory",
)
