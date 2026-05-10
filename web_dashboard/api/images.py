"""Image registry API.

Operator-facing CRUD over the RegisteredImage table plus a promote
endpoint that returns structured manual-steps payloads (Phase 1) — Phase
2 will replace the manual-steps return with native VM-import automation.

  GET    /api/images                    — list registered images
  POST   /api/images                    — register an image
  GET    /api/images/{id}               — fetch one
  DELETE /api/images/{id}               — remove from registry (does NOT
                                          delete derived cloud-native images)
  POST   /api/images/{id}/promote       — record a promotion to a target
                                          cloud; for cross-cloud, returns
                                          the manual-steps payload
"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import User, get_db
from ..models.image_registry import (
    PromoteImageRequest,
    RegisterImageRequest,
)
from ..services import image_registry_service
from ..services.image_registry_service import ImageRegistryError, VALID_CLOUDS
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

@router.post("/{image_id}/promote")
async def promote_image(
    image_id: str,
    payload: PromoteImageRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("admin", "write")),
):
    """Record a promotion intent. For Phase 1 this is always a manual-steps
    flow — the dashboard returns operator-readable instructions, the
    operator runs them, and updates the recorded image_id field via a
    follow-up PATCH (or by re-running this endpoint with `image_id_value`).

    Phase 2 will branch here on whether the source/target combination has an
    automated import path; the manual-steps return shape stays the same so
    the frontend doesn't need to change."""
    if payload.target_cloud not in VALID_CLOUDS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown target_cloud '{payload.target_cloud}'.",
        )

    image = image_registry_service.get_image(db, image_id)
    if not image:
        raise HTTPException(status_code=404, detail=f"Image {image_id} not found.")

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
        "ok":         True,
        "automated":  False,         # Phase 2: flips to true when we add native import
        "manual_steps": notes,
        "image":      updated,
    }
