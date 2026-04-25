"""
Hyper-V API router.

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
from ..services import hyperv_service
from ..services.hyperv_service import HyperVError

router = APIRouter(prefix="/api/hyperv", tags=["hyperv"])


# ── List endpoints ────────────────────────────────────────────────────────────

@router.get("/vms")
async def get_vms(current_user: User = Depends(get_current_user)):
    """List all Hyper-V VMs on the configured host."""
    try:
        return await hyperv_service.list_vms()
    except HyperVError as e:
        raise HTTPException(status_code=502, detail=str(e))


# ── Power operations ──────────────────────────────────────────────────────────

class PowerOpRequest(BaseModel):
    vmid: str
    name: str = ""


async def _run_power_op(job_id: str, vmid: str, name: str, op: str, label: str):
    from ..database import SessionLocal
    db = SessionLocal()
    try:
        job_service.update_progress(db, job_id, 10, f"{op.capitalize()}ing {label}…")
        result = await hyperv_service.power_op(vmid, name, op)
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
        label = payload.name or payload.vmid
        job = job_service.create_job(
            db,
            job_type=f"hyperv_{op}",
            description=f"Hyper-V {op}: {label}",
            workgroup="hyperv",
            owner_id=current_user.id,
        )
        background_tasks.add_task(
            _run_power_op, job.id, payload.vmid, payload.name, op, label
        )
        return {"job_id": job.id, "status": "queued"}

    _handler.__name__ = f"hyperv_{op}"
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
    summary="Graceful shutdown (requires Integration Services)",
)
router.add_api_route(
    "/power/stop",
    _power_endpoint("stop"),
    methods=["POST"],
    summary="Force power off",
)
router.add_api_route(
    "/power/restart",
    _power_endpoint("restart"),
    methods=["POST"],
    summary="Force restart",
)
router.add_api_route(
    "/power/pause",
    _power_endpoint("pause"),
    methods=["POST"],
    summary="Pause (Suspend-VM)",
)
router.add_api_route(
    "/power/resume",
    _power_endpoint("resume"),
    methods=["POST"],
    summary="Resume a paused or saved VM",
)
router.add_api_route(
    "/power/save",
    _power_endpoint("save"),
    methods=["POST"],
    summary="Save VM state to disk",
)
