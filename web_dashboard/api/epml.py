"""
BeyondTrust EPM for Linux (EPM-L) API router (community edition).

GET  /api/epml/packages        — list available packages from BT API
GET  /api/epml/build-status    — raw build status from BT API
POST /api/epml/trigger-build   — trigger a package build
POST /api/epml/sync-packages   — download from BT + upload to asset storage (background job)
GET  /api/epml/token           — fetch a fresh installation token
"""
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.orm import Session

from .auth import get_current_user
from ..database import User, get_db
from ..services import job_service
from ..services import epml_service
from ..services.epml_service import EpmlError

router = APIRouter(prefix="/api/epml", tags=["epml"])


@router.get("/packages")
async def get_packages(current_user: User = Depends(get_current_user)):
    try:
        return await epml_service.list_packages()
    except EpmlError as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/build-status")
async def get_build_status(current_user: User = Depends(get_current_user)):
    try:
        return await epml_service.get_build_status()
    except EpmlError as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.post("/trigger-build")
async def trigger_build(current_user: User = Depends(get_current_user)):
    try:
        return await epml_service.trigger_build()
    except EpmlError as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/token")
async def get_token(
    expiry_minutes: int = 480,
    current_user: User = Depends(get_current_user),
):
    try:
        token = await epml_service.get_installation_token(expiry_minutes)
        return {"token": token}
    except EpmlError as e:
        raise HTTPException(status_code=502, detail=str(e))


async def _run_sync(job_id: str):
    from ..database import SessionLocal
    db = SessionLocal()
    try:
        job_service.update_progress(db, job_id, 10, "Checking available EPM-L packages…")
        result = await epml_service.sync_packages_to_storage()
        summary = []
        if result["rpm_uploaded"]:
            summary.append("RPM uploaded")
        if result["deb_uploaded"]:
            summary.append("DEB uploaded")
        if not summary:
            summary.append("no new packages uploaded")
        job_service.update_progress(db, job_id, 90, f"Storage sync complete — {', '.join(summary)}")
        job_service.set_completed(db, job_id, result)
    except Exception as e:
        job_service.set_failed(db, job_id, str(e))
    finally:
        db.close()


@router.post("/sync-packages")
async def sync_packages(
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    job = job_service.create_job(
        db,
        job_type="epml_sync",
        description="EPM-L: sync packages from BeyondTrust to asset storage",
        workgroup="",
        owner_id=current_user.id,
    )
    background_tasks.add_task(_run_sync, job.id)
    return {"job_id": job.id, "status": "queued"}
