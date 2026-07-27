"""
Packer image-builder API endpoints.

POST /api/packer/aws/build    — build an AMI from a source AMI
POST /api/packer/azure/build  — build an Azure Managed Image
POST /api/packer/gcp/build    — build a GCP Custom Image

Each route validates, persists the request on a job and returns; the build itself
belongs to the job runner (``services.packer_build_service``), so it survives a
gunicorn worker recycle.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import User, get_db
from ..models.packer import (
    AWSPackerBuildRequest,
    AzurePackerBuildRequest,
    GCPPackerBuildRequest,
    PackerBuildResponse,
)
from ..services import job_service
from .auth import require_permission

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/packer", tags=["packer"])


# ── AWS build ─────────────────────────────────────────────────────────────────

@router.post("/aws/build", response_model=PackerBuildResponse)
async def build_aws_image(
    req: AWSPackerBuildRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("aws", "write")),
):
    """Build an AMI from a source AMI using Packer (amazon-ebs builder)."""
    if not req.source_ami:
        raise HTTPException(status_code=400, detail="source_ami is required.")
    if not req.image_name:
        raise HTTPException(status_code=400, detail="image_name is required.")

    job = job_service.create_job(
        db,
        job_type="packer_aws_build",
        created_by=current_user.username,
        metadata={
            "image_name": req.image_name,
            "source_ami": req.source_ami,
            "instance_type": req.instance_type,
            # Full request + creator so the worker can reconstruct and run the
            # build. Only secret *references* live in the request (resolved at
            # build launch), so nothing sensitive is persisted here.
            "req": req.model_dump(),
            "created_by": current_user.username,
        },
    )
    job_service.log_audit(
        db, current_user.username, "packer_aws_build",
        details={"image_name": req.image_name, "source_ami": req.source_ami},
    )
    # Enqueued as a pending job; the worker container claims + runs it (survives
    # gunicorn worker recycling, unlike an in-app BackgroundTask).
    return PackerBuildResponse(
        job_id=job.id,
        status="pending",
        message=f"Packer AWS build queued: {req.image_name} from {req.source_ami}",
    )


# ── Azure build ───────────────────────────────────────────────────────────────

@router.post("/azure/build", response_model=PackerBuildResponse)
async def build_azure_image(
    req: AzurePackerBuildRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("azure", "write")),
):
    """Build an Azure Managed Image using Packer (azure-arm builder)."""
    if not req.image_name:
        raise HTTPException(status_code=400, detail="image_name is required.")

    job = job_service.create_job(
        db,
        job_type="packer_azure_build",
        created_by=current_user.username,
        metadata={
            "image_name": req.image_name,
            "image_publisher": req.image_publisher,
            "image_offer": req.image_offer,
            "image_sku": req.image_sku,
            "os_type": req.os_type,
            "req": req.model_dump(),
            "created_by": current_user.username,
        },
    )
    job_service.log_audit(
        db, current_user.username, "packer_azure_build",
        details={"image_name": req.image_name, "image_sku": req.image_sku},
    )
    return PackerBuildResponse(
        job_id=job.id,
        status="pending",
        message=f"Packer Azure build queued: {req.image_name}",
    )


# ── GCP build ─────────────────────────────────────────────────────────────────

@router.post("/gcp/build", response_model=PackerBuildResponse)
async def build_gcp_image(
    req: GCPPackerBuildRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("gcp", "write")),
):
    """Build a GCP Custom Image using Packer (googlecompute builder)."""
    if not req.source_image:
        raise HTTPException(status_code=400, detail="source_image is required.")
    if not req.image_name:
        raise HTTPException(status_code=400, detail="image_name is required.")

    job = job_service.create_job(
        db,
        job_type="packer_gcp_build",
        created_by=current_user.username,
        metadata={
            "image_name": req.image_name,
            "source_image": req.source_image,
            "machine_type": req.machine_type,
            "req": req.model_dump(),
            "created_by": current_user.username,
        },
    )
    job_service.log_audit(
        db, current_user.username, "packer_gcp_build",
        details={"image_name": req.image_name, "source_image": req.source_image},
    )
    return PackerBuildResponse(
        job_id=job.id,
        status="pending",
        message=f"Packer GCP build queued: {req.image_name} from {req.source_image}",
    )
