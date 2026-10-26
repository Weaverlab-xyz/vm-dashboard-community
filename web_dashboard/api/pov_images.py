"""The POV image builder: standard images a cloud POV template boots.

  GET    /api/pov/images            — the catalog images usable on the POV's cloud
  GET    /api/pov/images/recipes    — the standard recipes, and their options
  POST   /api/pov/images/build      — build one recipe in a region (a Packer job)
  DELETE /api/pov/images/{id}       — remove a catalog row (never the cloud image itself)

**Why not open ``/api/packer`` and ``/api/images`` on a POV instead.** Those stay behind
``cloud_pages`` (main.py): they take any source image and any script, need ``aws:write``,
and come with the cloud consoles a POV instance does not have. A POV template builder needs
two images built one known way, so this router offers exactly that — the recipes in
``services/pov_image_recipes`` — over the same engine and the same catalog table, gated
like every other POV route and authorized on the scope template authoring already uses,
``pov_templates``. No new scope: adding one silently revokes it from every explicit map.

Storage-backed options (an extra script, an installer to stage) also need ``storage:read``
and honour asset scoping, exactly as the Storage page would: holding ``pov_templates`` is
not a way to read a file you could not open there.
"""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator
from sqlalchemy.orm import Session

from ..database import (Job, PovCloudTemplate, PovCloudTemplateVM, RegisteredImage, User,
                        get_db)
from ..services import (asset_scope_service, image_registry_service, job_service,
                        lab_platforms, pov_cloud_env, pov_image_recipes, storage_service)
from .auth import (get_current_user, has_permission, require_explicit_permission,
                   require_permission)

logger = logging.getLogger(__name__)
router = APIRouter(
    prefix="/api/pov/images",
    tags=["pov"],
    dependencies=[Depends(require_permission("pov_templates", "read"))],
)

# The Packer job each POV cloud's recipes queue — the same ones the demo Build Image tab
# queues, so the worker runs them unchanged.
_BUILD_JOB_TYPES = {
    "aws": "packer_aws_build",
    "azure": "packer_azure_build",
}

# An operator script is pasted or loaded into the build. Generous for a provisioner,
# small enough that a job row stays a job row.
_MAX_SCRIPT = 64 * 1024


def _selected_cloud() -> str:
    cloud = lab_platforms.selected_cloud()
    if not cloud:
        raise HTTPException(
            status_code=409,
            detail=("no POV cloud provider is selected on this instance. Choose one in "
                    "Settings → Integrations → POV cloud provider first."))
    return cloud


def _usable_on(img: dict, cloud: str) -> Optional[dict]:
    """``{image_id, region}`` an image resolves to on ``cloud``, or None — the same rule
    ``pov_cloud_env.resolve_image`` applies at provision time, so the picker never offers
    an image the build would then refuse."""
    if (img.get("source_cloud") or "").lower() == cloud:
        return {"image_id": img.get("source_image_id") or "",
                "region": img.get("source_region") or ""}
    promo = (img.get("promotions") or {}).get(cloud) or {}
    found = promo.get("image_id") or promo.get("self_link") or ""
    return {"image_id": found, "region": promo.get("region") or ""} if found else None


@router.get("")
def list_pov_images(db: Session = Depends(get_db),
                    current_user: User = Depends(get_current_user)):
    cloud = lab_platforms.selected_cloud()
    images = []
    for img in image_registry_service.list_images(db) if cloud else []:
        where = _usable_on(img, cloud)
        if not where:
            continue
        images.append({
            "id": img["id"], "name": img["name"], "version": img["version"],
            "recipe": img.get("recipe"), "os_type": img.get("os_type") or "Linux",
            "os_family": (img.get("os_type") or "Linux").lower(),
            "image_id": where["image_id"], "region": where["region"],
            "created_at": img.get("created_at"), "created_by": img.get("created_by"),
        })
    return {"selected_cloud": cloud,
            "default_region": pov_cloud_env.driver(cloud).default_region() if cloud else "",
            "images": images,
            "builds": _recent_builds(db)}


def _recent_builds(db: Session, limit: int = 10) -> list:
    """The last standard builds, newest first — the ones queued from this tab, told apart
    from a demo Build Image run by the ``recipe`` the job metadata carries."""
    rows = (db.query(Job).filter(Job.job_type.in_(tuple(_BUILD_JOB_TYPES.values())))
              .order_by(Job.created_at.desc()).limit(50).all())
    out = []
    for job in rows:
        meta = job.metadata_dict
        if not meta.get("recipe"):
            continue
        out.append({
            "job_id": job.id, "status": job.status, "recipe": meta.get("recipe"),
            "image_name": meta.get("image_name"), "region": meta.get("region"),
            "progress_pct": job.progress_pct or 0,
            "progress_message": job.progress_message or "",
            "error_message": job.error_message or "",
            "created_at": job.created_at.isoformat() if job.created_at else "",
            "created_by": job.created_by,
        })
        if len(out) >= limit:
            break
    return out


@router.get("/recipes")
def list_recipes(current_user: User = Depends(get_current_user)):
    cloud = lab_platforms.selected_cloud()
    return {
        "selected_cloud": cloud,
        "recipes": pov_image_recipes.describe(cloud),
        "default_region": pov_cloud_env.driver(cloud).default_region() if cloud else "",
        # Whether the storage-backed options can work for this user; the tab greys them
        # out rather than letting a build fail on a 403 at the end of the form.
        "storage_options": has_permission(current_user, "storage", "read"),
    }


class BuildRequest(BaseModel):
    recipe: str
    region: str = ""
    image_name: str = ""
    toggles: dict = {}
    # The operator's own script: pasted text, or a storage asset whose text is read now.
    extra_script: str = ""
    extra_script_asset: str = ""
    stage_asset: str = ""

    @field_validator("extra_script")
    @classmethod
    def _script_size(cls, v: str) -> str:
        if len(v or "") > _MAX_SCRIPT:
            raise ValueError(f"the extra script is over {_MAX_SCRIPT // 1024} KB")
        return v or ""


def _storage_check(db: Session, user: User, name: str) -> str:
    """The active backend, once ``user`` may read ``name`` there; else an HTTP refusal."""
    if not has_permission(user, "storage", "read"):
        raise HTTPException(status_code=403,
                            detail="storage-backed options need the storage:read permission")
    backend = storage_service.active_backend()
    if not backend:
        raise HTTPException(status_code=409,
                            detail="no active storage backend — pick one on the Storage page")
    try:
        asset_scope_service.check(db, user, backend, name)
    except asset_scope_service.AssetScopeDenied as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return backend


@router.post("/build", status_code=202)
async def build_pov_image(payload: BuildRequest, db: Session = Depends(get_db),
                          current_user: User = Depends(
                              require_explicit_permission("pov_templates", "write"))):
    """Queue one standard build. Spends money in the POV account for the length of the
    build (one instance, usually 15-40 minutes) and leaves an image (an AMI, an Azure
    managed image) that bills for its storage until someone deletes it."""
    cloud = _selected_cloud()
    job_type = _BUILD_JOB_TYPES.get(cloud)
    if job_type is None:
        raise HTTPException(
            status_code=422,
            detail=f"standard images are built on AWS and Azure only so far, not {cloud}")
    region = (payload.region or "").strip() or pov_cloud_env.driver(cloud).default_region()

    script = payload.extra_script
    if payload.extra_script_asset.strip():
        name = payload.extra_script_asset.strip()
        backend = _storage_check(db, current_user, name)
        try:
            data = await storage_service.fetch_asset_in(backend, name)
        except storage_service.StorageError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if len(data) > _MAX_SCRIPT:
            raise HTTPException(status_code=422,
                                detail=f"{name} is over {_MAX_SCRIPT // 1024} KB")
        script = data.decode("utf-8", errors="replace")
    if payload.stage_asset.strip():
        _storage_check(db, current_user, payload.stage_asset.strip())

    try:
        req = pov_image_recipes.build_request(
            cloud, payload.recipe, region=region, image_name=payload.image_name,
            toggles=payload.toggles, extra_script=script,
            stage_asset=payload.stage_asset)
    except (pov_image_recipes.RecipeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # The two clouds' requests name the base image and the build size differently; the
    # job row records each under the keys its own Build Image tab already uses.
    if cloud == "azure":
        source = {"image_publisher": req.image_publisher, "image_offer": req.image_offer,
                  "image_sku": req.image_sku, "os_type": req.os_type}
    else:
        source = {"source_ami": req.source_ami_family, "instance_type": req.instance_type}
    job = job_service.create_job(
        db, job_type=job_type, created_by=current_user.username,
        metadata={
            "image_name": req.image_name,
            **source,
            "recipe": req.recipe,
            "region": region,
            "req": req.model_dump(),
            "created_by": current_user.username,
        })
    job_service.log_audit(
        db, current_user.username, "pov_image_build",
        details={"recipe": req.recipe, "region": region, "image_name": req.image_name})
    return {"job_id": job.id, "status": "pending", "image_name": req.image_name,
            "region": region,
            "message": f"{req.image_name} queued in {region}"}


@router.delete("/{image_id}")
def delete_pov_image(image_id: str, db: Session = Depends(get_db),
                     current_user: User = Depends(
                         require_explicit_permission("pov_templates", "delete"))):
    """Remove the catalog row. The AMI stays: deregistering it is a cloud action with its
    own consequences (a POV still running from it, snapshots), and this is a catalog.

    Refused while a cloud template names it, because that template would then fail at its
    next build with "no longer exists" — which reads like somebody else's mistake.
    """
    row = db.get(RegisteredImage, image_id)
    if row is None:
        raise HTTPException(status_code=404, detail="No such image")
    users = (db.query(PovCloudTemplate.name)
               .join(PovCloudTemplateVM, PovCloudTemplateVM.template_id == PovCloudTemplate.id)
               .filter(PovCloudTemplateVM.image_ref == image_id)
               .distinct().all())
    if users:
        names = ", ".join(sorted(n for (n,) in users))
        raise HTTPException(
            status_code=409,
            detail=f"cloud template(s) {names} still use this image; pick another there first")
    name, kept = row.name, row.source_image_id
    image_registry_service.delete_image(db, image_id)
    job_service.log_audit(db, current_user.username, "pov_image_delete",
                          details={"image_id": image_id, "name": name})
    return {"deleted": name, "ami_kept": kept}
