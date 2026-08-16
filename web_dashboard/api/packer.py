"""
Packer image-builder API endpoints.

POST /api/packer/aws/build    — build an AMI from a source AMI
POST /api/packer/azure/build  — build an Azure Managed Image
POST /api/packer/gcp/build    — build a GCP Custom Image
POST /api/packer/oci/build    — build an OCI Custom Image

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
    OCIPackerBuildRequest,
    PackerBuildResponse,
)
from ..services import job_service, oci_freetier, oci_service
from ..services.oci_service import OCIError
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


# ── OCI build ─────────────────────────────────────────────────────────────────

def _oci_cfg(key: str, fallback: str = "") -> str:
    from ..services import config_service
    from ..config import settings
    return config_service.get(key) or getattr(settings, key, None) or fallback


@router.post("/oci/build", response_model=PackerBuildResponse)
async def build_oci_image(
    req: OCIPackerBuildRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("oci", "write")),
):
    """Build an OCI Custom Image using Packer (oracle-oci builder)."""
    if not req.base_image_ocid:
        raise HTTPException(status_code=400, detail="base_image_ocid is required.")
    if not req.image_name:
        raise HTTPException(status_code=400, detail="image_name is required.")
    if not req.availability_domain:
        raise HTTPException(status_code=400, detail="availability_domain is required.")

    # ── Placement precheck (hard gate) ────────────────────────────────────────
    # Runs before the free-tier prompt on purpose: a shape that cannot launch at
    # all must not be waved through an "acknowledge charges" dialog first. This
    # catches the shape/region and shape/image mismatches that LaunchInstance
    # reports only as an unattributed 404 NotAuthorizedOrNotFound — notably the
    # Always-Free VM.Standard.E2.1.Micro default, which no newer OCI region
    # offers. Advisory-by-design: it fails open if OCI can't be reached.
    try:
        await oci_service.check_launch_placement(
            availability_domain=req.availability_domain,
            image_ocid=req.base_image_ocid,
            shape=req.shape,
        )
    except OCIError as exc:
        raise HTTPException(status_code=400, detail={
            "code": "shape_not_launchable",
            "message": str(exc),
        }) from exc

    # ── Free-tier guardrail (warn + confirm) ──────────────────────────────────
    # Same gate as the deploy form, with one difference: a build instance is
    # transient (minutes), so the request is evaluated on its own rather than
    # folded in with the VMs this dashboard already has running. instance_count is
    # 1 — a build launches exactly one temporary instance.
    if _oci_cfg("oci_freetier_enforce", "1") not in ("0", "false", "False", ""):
        within, warnings = oci_freetier.evaluate(
            shape=req.shape, ocpus=req.ocpus, memory_gb=req.memory_gb,
            boot_volume_gb=req.boot_volume_gb, instance_count=1,
        )
        if not within and not req.acknowledge_charges:
            # `code` is preserved onto the Error by the frontend API helper
            # (app.js); the build form keys off it to reveal the acknowledgment.
            raise HTTPException(status_code=400, detail={
                "code": "free_tier_exceeded",
                "message": "This build instance is outside the OCI Always-Free tier and may incur charges: "
                           + " ".join(warnings),
                "warnings": warnings,
            })

    job = job_service.create_job(
        db,
        job_type="packer_oci_build",
        created_by=current_user.username,
        metadata={
            "image_name": req.image_name,
            "base_image_ocid": req.base_image_ocid,
            "shape": req.shape,
            "req": req.model_dump(),
            "created_by": current_user.username,
        },
    )
    job_service.log_audit(
        db, current_user.username, "packer_oci_build",
        details={"image_name": req.image_name, "base_image_ocid": req.base_image_ocid},
    )
    return PackerBuildResponse(
        job_id=job.id,
        status="pending",
        message=f"Packer OCI build queued: {req.image_name} from {req.base_image_ocid}",
    )
