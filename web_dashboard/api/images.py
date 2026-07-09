"""Image registry API.

Operator-facing CRUD over the RegisteredImage table plus a promote
endpoint that runs the SDK-driven automated path for AWS targets
(Phase 4 — PR 3) and still returns the manual-steps walkthrough for
Azure / GCP targets (those land in PRs 4 and 5).

  GET    /api/images                    — list registered images
  POST   /api/images                    — register an image
  GET    /api/images/{id}               — fetch one
  DELETE /api/images/{id}               — remove from registry (does NOT
                                          delete derived cloud-native images)
  POST   /api/images/{id}/promote       — promote to a target cloud:
                                          - AWS target → kicks off the
                                            automated ECS runner + import
                                            path as a background Job
                                          - Azure/GCP target → returns the
                                            manual-steps payload (Phase 1
                                            shape, unchanged)
                                          Pass `?manual=1` to force manual
                                          steps even for AWS targets.
"""
import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import User, get_db
from ..models.image_registry import (
    PromoteImageRequest,
    RegisterImageRequest,
)
from ..services import image_registry_service, job_service
from ..services.image_registry_service import ImageRegistryError, VALID_CLOUDS
from ..services.promote_runner_service import PromoteRunnerError
from .auth import get_current_user, require_permission

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/images", tags=["images"])


# ── List ─────────────────────────────────────────────────────────────────────

@router.get("")
async def list_images(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return {"images": image_registry_service.list_images(db)}


# ── Register ─────────────────────────────────────────────────────────────────

@router.post("", status_code=201)
async def register_image(
    payload: RegisterImageRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("admin", "write")),
):
    try:
        return image_registry_service.register_image(
            db,
            name=payload.name,
            version=payload.version,
            description=payload.description,
            source_cloud=payload.source_cloud,
            source_image_id=payload.source_image_id,
            source_region=payload.source_region,
            artefact_url=payload.artefact_url,
            artefact_format=payload.artefact_format,
            created_by=current_user.username,
        )
    except ImageRegistryError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ── Fetch one ────────────────────────────────────────────────────────────────

@router.get("/{image_id}")
async def get_image(
    image_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    image = image_registry_service.get_image(db, image_id)
    if not image:
        raise HTTPException(status_code=404, detail=f"Image {image_id} not found.")
    return image


# ── Delete ───────────────────────────────────────────────────────────────────

@router.delete("/{image_id}")
async def delete_image(
    image_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("admin", "delete")),
):
    if not image_registry_service.delete_image(db, image_id):
        raise HTTPException(status_code=404, detail=f"Image {image_id} not found.")
    return {"ok": True}


# ── Pre-flight ───────────────────────────────────────────────────────────────

@router.post("/{image_id}/preflight")
async def preflight_image(
    image_id: str,
    payload: PromoteImageRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Return advisory pre-flight checks for promoting `image_id` to a target.

    Pure-Python: artefact recorded, format compat, cross-storage requirement,
    target credentials configured. Synchronous (<100ms). None of these block
    the operator from running the actual promote — they're surfaced in the
    promote modal so blockers are visible before the operator copies the
    manual import commands."""
    if payload.target_cloud not in VALID_CLOUDS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown target_cloud '{payload.target_cloud}'.",
        )
    image = image_registry_service.get_image(db, image_id)
    if not image:
        raise HTTPException(status_code=404, detail=f"Image {image_id} not found.")
    try:
        checks = image_registry_service.compute_preflight_checks(image, payload.target_cloud)
    except ImageRegistryError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"image_id": image_id, "target_cloud": payload.target_cloud, "checks": checks}


# ── Promote ──────────────────────────────────────────────────────────────────

async def _run_aws_automated_promote(
    image_id: str, target_region: str, job_id: str,
) -> None:
    """Background-task wrapper around image_registry_service.promote_to_aws_automated.
    Owns its own DB session because BackgroundTasks runs after the request
    handler returns and the request session is closed."""
    from ..database import SessionLocal
    db = SessionLocal()
    try:
        # Mark running so the job UI shows a real status + duration instead of
        # sitting at "pending" for the whole promote.
        job_service.set_running(db, job_id)

        def _progress(pct: int, msg: str) -> None:
            try:
                job_service.update_progress(db, job_id, pct, msg[:200])
            except Exception:
                logger.exception("Failed to update job %s progress", job_id)

        try:
            updated = await image_registry_service.promote_to_aws_automated(
                db, image_id, target_region=target_region, progress_cb=_progress,
            )
            promo = (updated.get("promotions") or {}).get("aws") or {}
            job_service.set_completed(db, job_id, {
                "ami_id":     promo.get("image_id"),
                "region":     promo.get("region"),
                "promotions": updated.get("promotions"),
            })
        except (ImageRegistryError, PromoteRunnerError) as e:
            # Surface the runner log tail (if any) so the operator can read
            # qemu-img output / S3 upload errors from the Job page.
            extra = ""
            if isinstance(e, PromoteRunnerError) and getattr(e, "log_output", ""):
                extra = "\n--- runner log ---\n" + e.log_output[-4000:]
            job_service.set_failed(db, job_id, f"{e}{extra}")
            # Record the failed state on the image too so the /images page
            # row reflects reality.
            try:
                image_registry_service.record_promotion(
                    db, image_id, "aws",
                    status="failed",
                    region=target_region,
                    notes=str(e),
                )
            except Exception:
                logger.exception("Failed to record promotion failure for %s", image_id)
        except Exception as e:
            logger.exception("Automated AWS promote of %s raised unexpectedly", image_id)
            job_service.set_failed(db, job_id, f"Unexpected: {e}")
    finally:
        db.close()


async def _run_gcp_automated_promote(
    image_id: str,
    target_region: str,
    job_id: str,
) -> None:
    """Background-task wrapper for GCP automated promote. Mirrors the
    AWS / Azure wrappers."""
    from ..database import SessionLocal
    db = SessionLocal()
    try:
        # Mark running so the job UI shows a real status + duration instead of
        # sitting at "pending" for the whole promote.
        job_service.set_running(db, job_id)

        def _progress(pct: int, msg: str) -> None:
            try:
                job_service.update_progress(db, job_id, pct, msg[:200])
            except Exception:
                logger.exception("Failed to update job %s progress", job_id)

        try:
            updated = await image_registry_service.promote_to_gcp_automated(
                db, image_id,
                target_region=target_region or None,
                progress_cb=_progress,
            )
            promo = (updated.get("promotions") or {}).get("gcp") or {}
            job_service.set_completed(db, job_id, {
                "self_link": promo.get("self_link") or promo.get("image_id"),
                "region":    promo.get("region"),
                "promotions": updated.get("promotions"),
            })
        except (ImageRegistryError, PromoteRunnerError) as e:
            extra = ""
            if isinstance(e, PromoteRunnerError) and getattr(e, "log_output", ""):
                extra = "\n--- runner log ---\n" + e.log_output[-4000:]
            job_service.set_failed(db, job_id, f"{e}{extra}")
            try:
                image_registry_service.record_promotion(
                    db, image_id, "gcp",
                    status="failed",
                    region=target_region,
                    notes=str(e),
                )
            except Exception:
                logger.exception("Failed to record promotion failure for %s", image_id)
        except Exception as e:
            logger.exception("Automated GCP promote of %s raised unexpectedly", image_id)
            job_service.set_failed(db, job_id, f"Unexpected: {e}")
    finally:
        db.close()


async def _run_azure_automated_promote(
    image_id: str,
    target_resource_group: str,
    target_region: str,
    job_id: str,
) -> None:
    """Background-task wrapper for Azure automated promote. Mirrors the AWS
    wrapper; separate function so each cloud's failure path can record
    state in its own promotions slot."""
    from ..database import SessionLocal
    db = SessionLocal()
    try:
        # Mark running so the job UI shows a real status + duration instead of
        # sitting at "pending" for the whole promote.
        job_service.set_running(db, job_id)

        def _progress(pct: int, msg: str) -> None:
            try:
                job_service.update_progress(db, job_id, pct, msg[:200])
            except Exception:
                logger.exception("Failed to update job %s progress", job_id)

        try:
            updated = await image_registry_service.promote_to_azure_automated(
                db, image_id,
                target_resource_group=target_resource_group,
                target_location=target_region or None,
                progress_cb=_progress,
            )
            promo = (updated.get("promotions") or {}).get("azure") or {}
            job_service.set_completed(db, job_id, {
                "resource_id": promo.get("image_id"),
                "region":      promo.get("region"),
                "promotions":  updated.get("promotions"),
            })
        except (ImageRegistryError, PromoteRunnerError) as e:
            extra = ""
            if isinstance(e, PromoteRunnerError) and getattr(e, "log_output", ""):
                extra = "\n--- runner log ---\n" + e.log_output[-4000:]
            job_service.set_failed(db, job_id, f"{e}{extra}")
            try:
                image_registry_service.record_promotion(
                    db, image_id, "azure",
                    status="failed",
                    region=target_region,
                    notes=str(e),
                )
            except Exception:
                logger.exception("Failed to record promotion failure for %s", image_id)
        except Exception as e:
            logger.exception("Automated Azure promote of %s raised unexpectedly", image_id)
            job_service.set_failed(db, job_id, f"Unexpected: {e}")
    finally:
        db.close()


@router.post("/{image_id}/promote")
async def promote_image(
    image_id: str,
    payload: PromoteImageRequest,
    background_tasks: BackgroundTasks,
    manual: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("admin", "write")),
):
    """Promote a registered image to a target cloud.

    AWS targets (no `?manual=1`):  enqueue a background Job that drives the
    ECS promote runner + ec2:ImportImage end-to-end. Endpoint returns
    `{job_id, automated: true}` and the operator polls /jobs/{id} for
    status; on success the resulting AMI ID is recorded in
    `RegisteredImage.promotions["aws"]`.

    Azure / GCP targets (and any target with `?manual=1`):  unchanged from
    Phase 1 — returns the operator-readable CLI walkthrough and writes a
    `manual`-status promotion record. Used until PRs 4 and 5 wire the
    other two clouds.
    """
    if payload.target_cloud not in VALID_CLOUDS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown target_cloud '{payload.target_cloud}'.",
        )

    image = image_registry_service.get_image(db, image_id)
    if not image:
        raise HTTPException(status_code=404, detail=f"Image {image_id} not found.")

    if payload.target_cloud == "aws" and not manual:
        if not payload.target_region:
            raise HTTPException(
                status_code=400,
                detail="target_region is required for AWS automated promote (e.g. 'us-east-2').",
            )
        if not (image.get("artefact_url") or "").strip():
            raise HTTPException(
                status_code=400,
                detail=(
                    "Image has no artefact_url — automated promote needs the hub-backed "
                    "VHD. Re-run the build so Phase 3 export populates it, or use "
                    "?manual=1 for the CLI walkthrough."
                ),
            )
        job = job_service.create_job(
            db,
            job_type="image_promote_aws",
            created_by=current_user.username,
            workgroup=None,
            metadata={
                "image_id":      image_id,
                "image_name":    image["name"],
                "image_version": image["version"],
                "target_cloud":  "aws",
                "target_region": payload.target_region,
            },
        )
        # Mark the promotion as "running" on the image right away so the
        # /images page reflects the in-flight state.
        image_registry_service.record_promotion(
            db, image_id, "aws",
            status="running",
            region=payload.target_region,
            notes=f"Automated promote in progress (job {job.id}).",
        )
        # Enqueued as a pending job; the worker container claims + runs it
        # (survives gunicorn worker recycling, unlike an in-app BackgroundTask).
        return {
            "ok":        True,
            "automated": True,
            "job_id":    job.id,
        }

    if payload.target_cloud == "azure" and not manual:
        if not (image.get("artefact_url") or "").strip():
            raise HTTPException(
                status_code=400,
                detail=(
                    "Image has no artefact_url — automated promote needs the hub-backed "
                    "VHD. Re-run the build so Phase 3 export populates it, or use "
                    "?manual=1 for the CLI walkthrough."
                ),
            )
        target_rg = payload.target_resource_group
        # target_region in the request maps to Azure location (e.g. "centralus").
        # Both fall back to existing dashboard Azure config in the service layer.
        job = job_service.create_job(
            db,
            job_type="image_promote_azure",
            created_by=current_user.username,
            workgroup=None,
            metadata={
                "image_id":              image_id,
                "image_name":            image["name"],
                "image_version":         image["version"],
                "target_cloud":          "azure",
                "target_resource_group": target_rg,
                "target_region":         payload.target_region,
            },
        )
        image_registry_service.record_promotion(
            db, image_id, "azure",
            status="running",
            region=payload.target_region,
            notes=f"Automated promote in progress (job {job.id}).",
        )
        return {
            "ok":        True,
            "automated": True,
            "job_id":    job.id,
        }

    if payload.target_cloud == "gcp" and not manual:
        if not (image.get("artefact_url") or "").strip():
            raise HTTPException(
                status_code=400,
                detail=(
                    "Image has no artefact_url — automated promote needs the hub-backed "
                    "VHD. Re-run the build so Phase 3 export populates it, or use "
                    "?manual=1 for the CLI walkthrough."
                ),
            )
        job = job_service.create_job(
            db,
            job_type="image_promote_gcp",
            created_by=current_user.username,
            workgroup=None,
            metadata={
                "image_id":      image_id,
                "image_name":    image["name"],
                "image_version": image["version"],
                "target_cloud":  "gcp",
                "target_region": payload.target_region,
            },
        )
        image_registry_service.record_promotion(
            db, image_id, "gcp",
            status="running",
            region=payload.target_region,
            notes=f"Automated promote in progress (job {job.id}).",
        )
        return {
            "ok":        True,
            "automated": True,
            "job_id":    job.id,
        }

    # Manual fallback — any explicit ?manual=1.
    notes = image_registry_service.compute_manual_steps(image, payload.target_cloud)
    updated = image_registry_service.record_promotion(
        db,
        image_id,
        payload.target_cloud,
        status="manual",
        region=payload.target_region,
        notes=notes,
    )
    return {
        "ok":           True,
        "automated":    False,
        "manual_steps": notes,
        "image":        updated,
    }
