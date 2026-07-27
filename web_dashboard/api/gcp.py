"""
GCP (Google Cloud Platform) API endpoints.

Mirrors the AWS and Azure router patterns:
  - config helpers read from config_service (DB) first, fall back to settings
  - background tasks create a Job record and update progress
  - cache_service used for expensive GCP API calls
"""
import json
import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..config import settings
from ..database import Job, User, get_db
from ..models.gcp import (
    GCPCreateImageRequest,
    GCPDeployRequest,
    GCPDeployResponse,
    GCPImageListResponse,
    GCPInstanceListResponse,
    GCPNetworkOptions,
    GCPSSHKeyDetail,
)
from ..services import cache_service, cloud_stats, job_service, region_catalog, workgroup_service
from ..services import gcp_service
from .auth import require_admin, require_permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/gcp", tags=["gcp"])


# ── Config helpers ────────────────────────────────────────────────────────────

def _gcp_cfg(key: str, fallback: str = "") -> str:
    from ..services import config_service
    return config_service.get(key) or getattr(settings, key, None) or fallback


def _gcp_project() -> str:
    return _gcp_cfg("gcp_project_id")


def _gcp_zone() -> str:
    return _gcp_cfg("gcp_zone") or "us-central1-a"


def _gcp_region() -> str:
    zone = _gcp_zone()
    cfg_region = _gcp_cfg("gcp_region")
    if cfg_region:
        return cfg_region
    parts = zone.rsplit("-", 1)
    return parts[0] if len(parts) == 2 else zone


def _resolve_zone(zone: Optional[str]) -> str:
    """Resolve the effective GCE zone for a request. Format validation is delegated
    to the shared region catalog; a blank/None zone falls back to the configured
    default (``_gcp_zone()``); a malformed zone is rejected with HTTP 400 so a typo
    can't silently deploy into the default zone."""
    if zone is None or not zone.strip():
        return _gcp_zone()
    z = region_catalog.normalize("gcp", zone)
    if not region_catalog.validate_zone(z):
        raise HTTPException(status_code=400, detail=f"Invalid GCP zone '{zone}'")
    return z


def _region_from_zone(zone: str) -> str:
    """Derive the region a zone belongs to (us-central1-a → us-central1). Falls
    back to the configured default region (``_gcp_region()``, which derives from the
    configured zone when ``gcp_region`` is unset) if the zone doesn't parse."""
    parts = (zone or "").rsplit("-", 1)
    if len(parts) == 2 and region_catalog.validate("gcp", parts[0]):
        return parts[0]
    return _gcp_region()






def _gcp_ssh_secret() -> str:
    return _gcp_cfg("gcp_ssh_key_secret_name")




def _validate_workgroup(db: Session, user: User, workgroup: str) -> str:
    """Validate that `workgroup` exists and the user has access. Returns canonical name."""
    wg = workgroup_service.get(db, workgroup)
    if not wg:
        raise HTTPException(status_code=400, detail=f"Unknown workgroup '{workgroup}'")
    canonical = wg.name
    if not user.is_admin and canonical not in [w.lower() for w in user.workgroups_list]:
        raise HTTPException(status_code=403, detail=f"You do not have access to workgroup '{canonical}'")
    return canonical


def _accessible_workgroups(user: User) -> Optional[List[str]]:
    """Return the canonical workgroup names the user can see, or None for admins."""
    if user.is_admin:
        return None
    return [w.lower() for w in user.workgroups_list]


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/public-images", response_model=GCPImageListResponse)
async def list_public_images(
    os_filter: str = Query("all", description="Filter by OS: all/debian/ubuntu/rhel/rocky/centos/cos/windows"),
    current_user: User = Depends(require_permission("gcp", "read")),
):
    """List GCP public images from well-known image projects."""
    cache_key = cache_service.key_global(f"gcp_public_images_{os_filter}")
    cached = await cache_service.get(cache_key)
    if cached:
        return cached["data"]

    project_id = _gcp_project()
    if not project_id:
        raise HTTPException(status_code=400, detail="GCP project ID not configured — run the setup wizard.")

    try:
        images = await gcp_service.list_public_images(os_filter=os_filter)
    except gcp_service.GCPError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    result = GCPImageListResponse(images=images, project_id=project_id)
    await cache_service.set(cache_key, result.model_dump(), ttl=600)
    return result


@router.get("/custom-images", response_model=GCPImageListResponse)
async def list_custom_images(
    current_user: User = Depends(require_permission("gcp", "read")),
):
    """List custom (private) images in the configured GCP project."""
    project_id = _gcp_project()
    if not project_id:
        raise HTTPException(status_code=400, detail="GCP project ID not configured — run the setup wizard.")

    cache_key = cache_service.key_global("gcp_custom_images")
    cached = await cache_service.get(cache_key)
    if cached:
        return cached["data"]

    try:
        images = await gcp_service.list_custom_images(project_id=project_id)
    except gcp_service.GCPError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    result = GCPImageListResponse(images=images, project_id=project_id)
    await cache_service.set(cache_key, result.model_dump(), ttl=120)
    return result


@router.get("/network-options", response_model=GCPNetworkOptions)
async def network_options(
    zone: Optional[str] = None,
    bust: bool = Query(False),
    current_user: User = Depends(require_permission("gcp", "read")),
):
    """Return zones, machine types, and subnetworks for a project/region. ``?zone=``
    scopes the lookup to a specific zone's region (defaults to the configured
    zone/region); the cache is keyed per region so regions never collide."""
    project_id = _gcp_project()
    if not project_id:
        raise HTTPException(status_code=400, detail="GCP project ID not configured — run the setup wizard.")

    # No zone param → keep the exact historical defaults (honours the gcp_region
    # config override). An explicit zone derives its own region.
    if zone:
        _zone = _resolve_zone(zone)
        _region = _region_from_zone(_zone)
    else:
        _zone = _gcp_zone()
        _region = _gcp_region()

    # The Zone dropdown must offer zones across every configured region, not just
    # the current one — otherwise multi-region setups (gcp_region_configs) can't
    # deploy outside the default region. Collect the default region plus every
    # per-region config set so the returned zone list spans all of them.
    from ..services import region_config
    _zone_regions = set(region_config.load_region_configs("gcp").keys())
    _zone_regions.add(_gcp_region())
    _zone_regions.add(_region)

    cache_key = cache_service.key_param("gcp_network_opts", region=_region)
    if not bust:
        cached = await cache_service.get(cache_key)
        if cached:
            # cache_service.get() wraps the payload in {"data": ..., "cached_at": ...};
            # return the inner payload so the response matches the cache-miss shape
            # (issue #5 — deploy modal dropdowns were blank on cache hit because the
            # frontend received {data, cached_at} instead of the options dict).
            return cached["data"]

    try:
        opts = await gcp_service.get_network_options(
            project_id=project_id,
            region=_region,
            zone=_zone,
            zone_regions=sorted(_zone_regions),
        )
    except gcp_service.GCPError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    from datetime import datetime, timezone
    opts["cached_at"] = datetime.now(timezone.utc).isoformat()
    result = GCPNetworkOptions(**opts)
    await cache_service.set(cache_key, result.model_dump(), ttl=300)
    return result


async def _build_gcp_instances(db, project_id: str) -> list:
    """Query completed, non-destroyed gce_deploy jobs, fetch live per-zone state,
    cache the full list under `gcp_instances`, and return it (unfiltered). Shared
    by /instances and /dashboard-stats. Raises gcp_service.GCPError on a live-API
    failure (callers decide how to surface it)."""
    deploy_jobs = (
        db.query(Job)
        .filter(Job.job_type == "gce_deploy", Job.status == "completed")
        .order_by(Job.created_at.desc())
        .all()
    )
    by_zone: dict = {}
    job_meta: dict = {}
    for job in deploy_jobs:
        if not job.extra_data:
            continue
        try:
            data = json.loads(job.extra_data)
        except Exception:
            continue
        if data.get("destroyed"):
            continue
        name = data.get("instance_name")
        zone = data.get("zone") or _gcp_zone()
        if name:
            by_zone.setdefault(zone, []).append(name)
            job_meta[name] = {
                "job_id": job.id,
                "deployed_by": job.created_by,
                "extra": data,
                "workgroup": (job.workgroup or data.get("workgroup") or "").lower() or None,
            }

    instances = []
    for zone, names in by_zone.items():
        region = _region_from_zone(zone)
        live = await gcp_service.describe_instances(project_id=project_id, zone=zone, instance_names=names)
        for inst in live:
            meta = job_meta.get(inst["instance_name"], {})
            inst["job_id"] = meta.get("job_id")
            inst["deployed_by"] = meta.get("deployed_by")
            inst["workgroup"] = meta.get("workgroup") or inst.get("workgroup")
            inst["region"] = inst.get("region") or region
            instances.append(inst)

    full = GCPInstanceListResponse(instances=instances, project_id=project_id, zone=_gcp_zone())
    await cache_service.set(cache_service.key_global("gcp_instances"), full.model_dump(), ttl=60)
    return instances


async def _gcp_instances_unfiltered(db, project_id: str) -> list:
    """Full dashboard GCE instances, cache-aware (reads the gcp_instances cache,
    builds + caches on miss)."""
    cached = await cache_service.get(cache_service.key_global("gcp_instances"))
    if cached:
        return (cached.get("data") or {}).get("instances") or []
    return await _build_gcp_instances(db, project_id)


@router.get("/dashboard-stats")
async def gcp_dashboard_stats(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("gcp", "read")),
):
    """One-call counts for the GCP dashboard tiles (instances total+running,
    custom images total) — reuses the gcp_instances / gcp_custom_images caches +
    RBAC. A null section → the tile shows unavailable."""
    out = {"instances": None, "images": None}
    project_id = _gcp_project()
    if not project_id:
        return out
    try:
        instances = await _gcp_instances_unfiltered(db, project_id)
        accessible = _accessible_workgroups(current_user)
        out["instances"] = cloud_stats.summarize_instances(instances, accessible, "status")
        out["instances"]["by_region"] = cloud_stats.summarize_by_region(
            instances, accessible, "status", "region")
    except gcp_service.GCPError:
        pass
    try:
        cached = await cache_service.get(cache_service.key_global("gcp_custom_images"))
        if cached:
            imgs = (cached.get("data") or {}).get("images") or []
        else:
            imgs = await gcp_service.list_custom_images(project_id=project_id)
        out["images"] = {"total": len(imgs)}
    except gcp_service.GCPError:
        pass
    return out


@router.get("/instances", response_model=GCPInstanceListResponse)
async def list_instances(
    bust: bool = Query(False),
    workgroup: Optional[str] = None,
    current_user: User = Depends(require_permission("gcp", "read")),
    db: Session = Depends(get_db),
):
    """List GCE instances deployed via this dashboard (derived from job records + live GCP state).

    Non-admins see only instances whose Job.workgroup (or `workgroup` label) is
    in their workgroup list. `?workgroup=<name>` narrows further.
    """
    project_id = _gcp_project()
    if not project_id:
        raise HTTPException(status_code=400, detail="GCP project ID not configured — run the setup wizard.")

    accessible = _accessible_workgroups(current_user)
    if workgroup is not None:
        canonical = workgroup.lower()
        if accessible is not None and canonical not in accessible:
            raise HTTPException(status_code=403, detail=f"No access to workgroup '{canonical}'")

    cache_key = cache_service.key_global("gcp_instances")
    if not bust:
        cached = await cache_service.get(cache_key)
        if cached:
            # cache_service.get() wraps the payload in {"data": ..., "cached_at": ...};
            # unwrap to get the actual response (GCPInstanceListResponse shape).
            payload = cached.get("data") or {}
            inst_list = payload.get("instances")
            if inst_list is not None:
                filtered = []
                for inst in inst_list:
                    inst_wg = (inst.get("workgroup") or "").lower() or None
                    if workgroup is not None and inst_wg != workgroup.lower():
                        continue
                    if accessible is not None:
                        if inst_wg is None or inst_wg not in accessible:
                            continue
                    filtered.append(inst)
                payload = {**payload, "instances": filtered}
            return payload

    try:
        instances = await _build_gcp_instances(db, project_id)
    except gcp_service.GCPError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    filtered = []
    for inst in instances:
        inst_wg = (inst.get("workgroup") or "").lower() or None
        if workgroup is not None and inst_wg != workgroup.lower():
            continue
        if accessible is not None:
            if inst_wg is None or inst_wg not in accessible:
                continue
        filtered.append(inst)
    return GCPInstanceListResponse(instances=filtered, project_id=project_id, zone=_gcp_zone())


@router.get("/secrets/ssh-key", response_model=GCPSSHKeyDetail)
async def get_configured_ssh_key(
    current_user: User = Depends(require_permission("gcp", "read")),
):
    """Return a preview of the SSH public key from the configured Secret Manager secret."""
    project_id = _gcp_project()
    secret_name = _gcp_ssh_secret()
    if not project_id or not secret_name:
        raise HTTPException(
            status_code=404,
            detail="SSH key secret not configured — add gcp_ssh_key_secret_name in the wizard.",
        )
    try:
        pub_key = await gcp_service.get_ssh_public_key(project_id=project_id, secret_name=secret_name)
    except gcp_service.GCPError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return GCPSSHKeyDetail(secret_name=secret_name, public_key_preview=pub_key[:80])


@router.get("/secrets/ssh-keys")
async def list_ssh_key_secret_names(
    current_user: User = Depends(require_permission("gcp", "read")),
):
    """Candidate secrets for the per-launch SSH-key-secret override picker."""
    project_id = _gcp_project()
    if not project_id:
        raise HTTPException(status_code=400, detail="GCP project ID not configured.")
    try:
        return {"secrets": await gcp_service.list_secret_names(project_id)}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/deploy", response_model=GCPDeployResponse)
async def deploy_instance(
    payload: GCPDeployRequest,
    current_user: User = Depends(require_permission("gcp", "write")),
    db: Session = Depends(get_db),
):
    """Deploy a GCE instance from an image. Runs in background; returns job ID immediately."""
    project_id = _gcp_project()
    if not project_id:
        raise HTTPException(status_code=400, detail="GCP project ID not configured — run the setup wizard.")

    zone = _resolve_zone(payload.zone)
    payload.zone = zone            # normalise so the runner uses the resolved zone
    region = _region_from_zone(zone)
    workgroup = _validate_workgroup(db, current_user, payload.workgroup)
    payload.workgroup = workgroup

    # Pre-action policy gate (inert unless enabled + this action is gated). The
    # region must come from the *requested* zone so the allowed-regions guardrail
    # checks where the VM actually lands, not the global default region.
    from ..services import admission_service
    admission_service.enforce(
        "gcp:gce:deploy",
        request={"region": region, "zone": zone,
                 "instance_type": payload.machine_type, "image": payload.image_self_link,
                 "name": payload.instance_name},
        actor=current_user, db=db,
    )

    # Validate an optional per-launch SSH-key-secret override: must be a JSON object
    # carrying a public_key, else the VM would be unreachable.
    if payload.ssh_key_secret_override:
        from ..services import ssh_key_secret
        try:
            raw = await gcp_service.get_secret(project_id, payload.ssh_key_secret_override)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=f"SSH key secret '{payload.ssh_key_secret_override}' could not be read: {e}")
        try:
            ssh_key_secret.validate_public_key_secret(raw, secret_name=payload.ssh_key_secret_override)
        except ssh_key_secret.SshKeySecretError as e:
            raise HTTPException(status_code=400, detail=str(e))

    job = job_service.create_job(
        db,
        job_type="gce_deploy",
        created_by=current_user.username,
        workgroup=workgroup,
        metadata={
            "project_id":       project_id,
            "zone":             zone,
            "region":           region,
            "instance_name":    payload.instance_name,
            "machine_type":     payload.machine_type,
            "image_self_link":  payload.image_self_link,
            "image_name":       payload.image_name,
            "workgroup":        workgroup,
            # Full request so the runner can rebuild the deploy call. Only secret
            # *references* live on it, resolved at deploy time.
            "req":              payload.model_dump(),
        },
    )
    job_service.set_cloud_resource_id(db, job.id, payload.instance_name)
    job_service.log_audit(
        db,
        current_user.username,
        "gce_deploy",
        details={"instance_name": payload.instance_name, "zone": zone, "machine_type": payload.machine_type, "workgroup": workgroup},
    )

    return GCPDeployResponse(job_id=job.id, status="pending", message=f"Deploying {payload.instance_name}…")




class _WorkgroupReassignRequest(BaseModel):
    workgroup: str


@router.patch("/instances/{instance_name}/workgroup")
async def reassign_instance_workgroup(
    instance_name: str,
    req: _WorkgroupReassignRequest,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    """Rewrite the `workgroup` label on a GCE instance and update the originating
    Job row. Admin only."""
    project_id = _gcp_project()
    if not project_id:
        raise HTTPException(status_code=400, detail="GCP project ID not configured")

    wg = workgroup_service.get(db, req.workgroup)
    if not wg:
        raise HTTPException(status_code=400, detail=f"Unknown workgroup '{req.workgroup}'")
    canonical = wg.name

    job = db.query(Job).filter(Job.cloud_resource_id == instance_name).first()
    if job is None:
        for j in db.query(Job).filter(Job.job_type == "gce_deploy").all():
            if j.metadata_dict.get("instance_name") == instance_name:
                job = j
                break

    zone = (job.metadata_dict.get("zone") if job else None) or _gcp_zone()

    try:
        await gcp_service.set_workgroup_label(project_id, zone, instance_name, canonical)
    except gcp_service.GCPError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"GCE label update failed: {exc}")

    if job is not None:
        job.workgroup = canonical
        meta = job.metadata_dict
        meta["workgroup"] = canonical
        job.metadata_dict = meta
        if not job.cloud_resource_id:
            job.cloud_resource_id = instance_name
        db.commit()

    await cache_service.invalidate(cache_service.key_global("gcp_instances"))
    return {"instance_name": instance_name, "workgroup": canonical, "job_id": job.id if job else None}


@router.post("/instances/{instance_name}/create-image", response_model=GCPDeployResponse)
async def create_image_from_instance(
    instance_name: str,
    payload: GCPCreateImageRequest,
    current_user: User = Depends(require_permission("gcp", "write")),
    db: Session = Depends(get_db),
):
    """Capture a GCE instance as a custom image. Runs in background."""
    project_id = _gcp_project()
    if not project_id:
        raise HTTPException(status_code=400, detail="GCP project ID not configured.")
    zone = _gcp_zone()

    job = job_service.create_job(
        db,
        job_type="gce_capture_image",
        created_by=current_user.username,
        # project_id and zone are resolved here and persisted, not re-derived in the
        # runner: _gcp_project()/_gcp_zone() return whatever is configured at run time,
        # which would capture from the wrong project if the default changed.
        metadata={
            "instance_name": instance_name,
            "image_name": payload.image_name,
            "description": payload.description,
            "project_id": project_id,
            "zone": zone,
        },
    )
    return GCPDeployResponse(
        job_id=job.id, status="pending",
        message=f"Creating image {payload.image_name} from {instance_name}…"
    )




@router.delete("/instances/{instance_name}")
async def destroy_instance(
    instance_name: str,
    zone: str = Query("", description="Zone the instance is in; defaults to configured zone"),
    current_user: User = Depends(require_permission("gcp", "delete")),
    db: Session = Depends(get_db),
):
    """Terminate a GCE instance. Runs in background."""
    project_id = _gcp_project()
    if not project_id:
        raise HTTPException(status_code=400, detail="GCP project ID not configured.")
    resolved_zone = _resolve_zone(zone)

    # Find the original deploy job so we can retrieve bt_tf_state for Shell Jump removal
    deploy_jobs = (
        db.query(Job)
        .filter(Job.job_type == "gce_deploy", Job.status == "completed")
        .all()
    )
    deploy_job = None
    for j in deploy_jobs:
        meta = j.metadata_dict
        if meta.get("instance_name") == instance_name and not meta.get("destroyed"):
            deploy_job = j
            break

    job = job_service.create_job(
        db,
        job_type="gce_destroy",
        created_by=current_user.username,
        metadata={
            "instance_name": instance_name,
            "zone": resolved_zone,
            "deploy_job_id": deploy_job.id if deploy_job else None,
            # Persisted rather than re-derived in the runner — see create-image above.
            # A destroy aimed at the wrong project is the worst version of this bug.
            "project_id": project_id,
        },
    )
    job_service.log_audit(
        db, current_user.username, "gce_destroy",
        details={"instance_name": instance_name, "zone": resolved_zone},
    )
    return {"job_id": job.id, "status": "pending", "message": f"Terminating {instance_name}…"}




# ── Export custom image to portable VHD on hub backend ───────────────────────

class ExportImageRequest(BaseModel):
    image_name: str  # Registry name to record the exported image under


class ExportImageResponse(BaseModel):
    job_id: str
    status: str
    message: str


@router.post("/images/{image_name}/export", response_model=ExportImageResponse)
async def export_custom_image(
    image_name: str,
    req: ExportImageRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("gcp", "write")),
):
    """Manually export a custom GCE image to VHD on the hub backend and
    register it in the image registry. Useful when the auto-export during
    build was skipped or failed."""
    project_id = _gcp_project()
    if not project_id:
        raise HTTPException(status_code=400, detail="GCP project ID not configured.")

    job = job_service.create_job(
        db,
        job_type="gcp_export_image",
        created_by=current_user.username,
        metadata={"image_name": image_name, "registry_name": req.image_name,
                  "project_id": project_id, "created_by": current_user.username},
    )
    job_service.log_audit(
        db, current_user.username, "gcp_export_image",
        details={"image_name": image_name, "registry_name": req.image_name},
    )

    # Enqueued as a pending job; the worker container claims + runs it
    # (survives gunicorn worker recycling, unlike an in-app BackgroundTask).
    return ExportImageResponse(
        job_id=job.id,
        status="pending",
        message=f"Export of {image_name} queued",
    )


@router.delete("/images/{image_name}")
async def delete_image(
    image_name: str,
    current_user: User = Depends(require_permission("gcp", "delete")),
    db: Session = Depends(get_db),
):
    """Delete a custom GCP image."""
    project_id = _gcp_project()
    if not project_id:
        raise HTTPException(status_code=400, detail="GCP project ID not configured.")
    try:
        await gcp_service.delete_image(project_id=project_id, image_name=image_name)
    except gcp_service.GCPError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    job_service.log_audit(db, current_user.username, "gce_delete_image", details={"image_name": image_name})
    await cache_service.invalidate(cache_service.key_global("gcp_custom_images"))
    return {"ok": True, "image_name": image_name}
