"""
Proxmox VE API router.

All endpoints require authentication. Power operations are dispatched as
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
from ..services import proxmox_service
from ..services.proxmox_service import ProxmoxError

router = APIRouter(prefix="/api/proxmox", tags=["proxmox"])


# ── List endpoints ────────────────────────────────────────────────────────────

@router.get("/nodes")
async def get_nodes(current_user: User = Depends(get_current_user)):
    try:
        return await proxmox_service.list_nodes()
    except ProxmoxError as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/resources")
async def get_resources(
    node: str = "",
    current_user: User = Depends(get_current_user),
):
    """List all VMs and containers. Pass ?node=<name> to filter to one node."""
    try:
        nodes = [node] if node else None
        return await proxmox_service.list_resources(nodes)
    except ProxmoxError as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/nodes/{node}/{vm_type}/{vmid}")
async def get_vm_detail(
    node: str,
    vm_type: str,
    vmid: int,
    current_user: User = Depends(get_current_user),
):
    if vm_type not in ("qemu", "lxc"):
        raise HTTPException(status_code=400, detail="vm_type must be 'qemu' or 'lxc'")
    try:
        return await proxmox_service.get_vm_detail(node, vmid, vm_type)
    except ProxmoxError as e:
        raise HTTPException(status_code=502, detail=str(e))


# ── Power operations ──────────────────────────────────────────────────────────

class PowerOpRequest(BaseModel):
    node: str
    vmid: int
    vm_type: str  # "qemu" or "lxc"
    name: str = ""


async def _run_power_op(job_id: str, node: str, vmid: int, vm_type: str, op: str, db: Session):
    from ..database import SessionLocal
    db2 = SessionLocal()
    try:
        job_service.update_progress(db2, job_id, 10, f"{op.capitalize()}ing {vm_type} {vmid} on {node}...")
        result = await proxmox_service.power_op(node, vmid, vm_type, op)
        job_service.set_completed(db2, job_id, result)
    except Exception as e:
        job_service.set_failed(db2, job_id, str(e))
    finally:
        db2.close()


def _power_endpoint(op: str):
    async def _handler(
        payload: PowerOpRequest,
        background_tasks: BackgroundTasks,
        db: Session = Depends(get_db),
        current_user: User = Depends(get_current_user),
    ):
        label = payload.name or f"{payload.vm_type}/{payload.vmid}"
        job = job_service.create_job(
            db,
            job_type=f"proxmox_{op}",
            description=f"Proxmox {op}: {label} on {payload.node}",
            workgroup=payload.node,
            owner_id=current_user.id,
        )
        background_tasks.add_task(
            _run_power_op,
            job.id, payload.node, payload.vmid, payload.vm_type, op, db,
        )
        return {"job_id": job.id, "status": "queued"}

    _handler.__name__ = f"proxmox_{op}"
    return _handler


router.add_api_route(
    "/power/start",
    _power_endpoint("start"),
    methods=["POST"],
    summary="Start a VM or container",
)
router.add_api_route(
    "/power/shutdown",
    _power_endpoint("shutdown"),
    methods=["POST"],
    summary="Gracefully shut down a VM or container",
)
router.add_api_route(
    "/power/stop",
    _power_endpoint("stop"),
    methods=["POST"],
    summary="Force-stop a VM or container",
)
router.add_api_route(
    "/power/reboot",
    _power_endpoint("reboot"),
    methods=["POST"],
    summary="Reboot a VM (QEMU only)",
)
