"""
XCP-ng / XenServer API router.

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
from ..services import xcpng_service
from ..services.xcpng_service import XcpNgError

router = APIRouter(prefix="/api/xcpng", tags=["xcpng"])


@router.get("/vms")
async def get_vms(current_user: User = Depends(get_current_user)):
    """List all VMs from the XCP-ng / XenServer host or pool."""
    try:
        return await xcpng_service.list_vms()
    except XcpNgError as e:
        raise HTTPException(status_code=502, detail=str(e))


class PowerOpRequest(BaseModel):
    uuid: str
    name: str = ""


async def _run_power_op(job_id: str, uuid: str, name: str, op: str, label: str):
    from ..database import SessionLocal
    db = SessionLocal()
    try:
        job_service.update_progress(db, job_id, 10, f"{op.replace('_', ' ').capitalize()}ing {label}…")
        result = await xcpng_service.power_op(uuid, name, op)
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
            job_type=f"xcpng_{op}",
            description=f"XCP-ng {op.replace('_', ' ')}: {label}",
            workgroup="xcpng",
            owner_id=current_user.id,
        )
        background_tasks.add_task(
            _run_power_op, job.id, payload.uuid, payload.name, op, label
        )
        return {"job_id": job.id, "status": "queued"}

    _handler.__name__ = f"xcpng_{op}"
    return _handler


router.add_api_route("/power/start",      _power_endpoint("start"),      methods=["POST"], summary="Start a halted VM")
router.add_api_route("/power/shutdown",   _power_endpoint("shutdown"),   methods=["POST"], summary="Graceful shutdown (requires xe-guest-utilities)")
router.add_api_route("/power/stop",       _power_endpoint("stop"),       methods=["POST"], summary="Force power off (hard shutdown)")
router.add_api_route("/power/reboot",     _power_endpoint("reboot"),     methods=["POST"], summary="Graceful reboot (requires xe-guest-utilities)")
router.add_api_route("/power/hard_reboot",_power_endpoint("hard_reboot"),methods=["POST"], summary="Force reboot (hard reboot)")
router.add_api_route("/power/suspend",    _power_endpoint("suspend"),    methods=["POST"], summary="Suspend VM to disk")
router.add_api_route("/power/resume",     _power_endpoint("resume"),     methods=["POST"], summary="Resume a suspended VM")
router.add_api_route("/power/pause",      _power_endpoint("pause"),      methods=["POST"], summary="Pause VM in memory")
router.add_api_route("/power/unpause",    _power_endpoint("unpause"),    methods=["POST"], summary="Unpause a paused VM")
