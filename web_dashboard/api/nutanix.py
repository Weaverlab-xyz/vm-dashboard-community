"""
Nutanix AHV API router.

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
from ..services import nutanix_service
from ..services.nutanix_service import NutanixError

router = APIRouter(prefix="/api/nutanix", tags=["nutanix"])


@router.get("/vms")
async def get_vms(current_user: User = Depends(get_current_user)):
    """List all VMs from Prism Central."""
    try:
        return await nutanix_service.list_vms()
    except NutanixError as e:
        raise HTTPException(status_code=502, detail=str(e))


class PowerOpRequest(BaseModel):
    uuid: str
    name: str = ""
    cluster: str = ""


async def _run_power_op(job_id: str, uuid: str, name: str, op: str, label: str):
    from ..database import SessionLocal
    db = SessionLocal()
    try:
        job_service.update_progress(db, job_id, 10, f"{op.capitalize()}ing {label}…")
        result = await nutanix_service.power_op(uuid, name, op)
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
        label = payload.name or payload.uuid
        job = job_service.create_job(
            db,
            job_type=f"nutanix_{op}",
            description=f"Nutanix {op}: {label}"
            + (f" ({payload.cluster})" if payload.cluster else ""),
            workgroup=payload.cluster or "nutanix",
            owner_id=current_user.id,
        )
        background_tasks.add_task(
            _run_power_op, job.id, payload.uuid, payload.name, op, label
        )
        return {"job_id": job.id, "status": "queued"}

    _handler.__name__ = f"nutanix_{op}"
    return _handler


router.add_api_route("/power/start",    _power_endpoint("start"),    methods=["POST"], summary="Power on a VM")
router.add_api_route("/power/shutdown", _power_endpoint("shutdown"),  methods=["POST"], summary="Graceful shutdown via ACPI (requires NGT)")
router.add_api_route("/power/stop",     _power_endpoint("stop"),      methods=["POST"], summary="Force power off")
router.add_api_route("/power/reboot",   _power_endpoint("reboot"),    methods=["POST"], summary="Graceful reboot via ACPI (requires NGT)")
router.add_api_route("/power/reset",    _power_endpoint("reset"),     methods=["POST"], summary="Hard reset")
router.add_api_route("/power/pause",    _power_endpoint("pause"),     methods=["POST"], summary="Pause VM")
router.add_api_route("/power/resume",   _power_endpoint("resume"),    methods=["POST"], summary="Resume paused or suspended VM")
