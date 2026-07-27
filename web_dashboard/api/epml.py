"""
BeyondTrust EPM for Linux (EPM-L) API router (community edition).

GET  /api/epml/packages        — list available packages from BT API
GET  /api/epml/build-status    — raw build status from BT API
POST /api/epml/trigger-build   — trigger a package build
POST /api/epml/sync-packages   — download from BT + upload to asset storage (background job)
GET  /api/epml/token           — fetch a fresh installation token
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .auth import get_current_user
from ..database import User, get_db
from ..services import job_service
from ..services import epml_service
from ..services import storage_service
from ..services.epml_service import EpmlError
from ..services.storage_service import StorageError

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


class SyncPackagesRequest(BaseModel):
    # Which storage backend to upload into; empty = the active one.
    backend: str = ""


@router.post("/sync-packages")
async def sync_packages(
    payload: SyncPackagesRequest | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Queue a download-from-BeyondTrust → upload-to-storage sync.

    The packages are large and the BeyondTrust links expire, so this is a job rather
    than a request-blocking call: the client gets a job_id and polls /api/jobs/{id}.
    Execution belongs to the job runner (``services.epml_sync_service``)."""
    backend = (payload.backend if payload else "") or ""
    if backend:
        try:
            storage_service._validate_backend(backend)
        except StorageError as e:
            raise HTTPException(status_code=400, detail=str(e))
    job = job_service.create_job(
        db,
        job_type="epml_sync",
        created_by=current_user.username,
        workgroup="ansible",
        metadata={
            "description": "EPM-L: sync packages from BeyondTrust to asset storage",
            "backend": backend,
        },
    )
    return {"job_id": job.id, "status": "queued"}
