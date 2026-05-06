"""
GCP (Google Cloud Platform) API endpoints.

Mirrors the AWS and Azure router patterns:
  - config helpers read from config_service (DB) first, fall back to settings
  - background tasks create a Job record and update progress
  - cache_service used for expensive GCP API calls
"""
import json
import logging
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
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
from ..services import cache_service, job_service
from ..services import gcp_service
from .auth import require_permission

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


def _gcp_ssh_secret() -> str:
    return _gcp_cfg("gcp_ssh_key_secret_name")


def _get_db_session():
    from ..database import SessionLocal
    return SessionLocal()


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
        return cached

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
        return cached

    try:
        images = await gcp_service.list_custom_images(project_id=project_id)
    except gcp_service.GCPError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    result = GCPImageListResponse(images=images, project_id=project_id)
    await cache_service.set(cache_key, result.model_dump(), ttl=120)
    return result


@router.get("/network-options", response_model=GCPNetworkOptions)
async def network_options(
    bust: bool = Query(False),
    current_user: User = Depends(require_permission("gcp", "read")),
):
    """Return zones, machine types, and subnetworks for the configured project/region."""
    project_id = _gcp_project()
    if not project_id:
        raise HTTPException(status_code=400, detail="GCP project ID not configured — run the setup wizard.")

    cache_key = cache_service.key_global("gcp_network_opts")
    if not bust:
        cached = await cache_service.get(cache_key)
        if cached:
            return cached

    try:
        opts = await gcp_service.get_network_options(
            project_id=project_id,
            region=_gcp_region(),
            zone=_gcp_zone(),
        )
    except gcp_service.GCPError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    from datetime import datetime, timezone
    opts["cached_at"] = datetime.now(timezone.utc).isoformat()
    result = GCPNetworkOptions(**opts)
    await cache_service.set(cache_key, result.model_dump(), ttl=300)
    return result


@router.get("/instances", response_model=GCPInstanceListResponse)
async def list_instances(
    bust: bool = Query(False),
    current_user: User = Depends(require_permission("gcp", "read")),
    db: Session = Depends(get_db),
):
    """List GCE instances deployed via this dashboard (derived from job records + live GCP state)."""
    project_id = _gcp_project()
    if not project_id:
        raise HTTPException(status_code=400, detail="GCP project ID not configured — run the setup wizard.")

    cache_key = cache_service.key_global("gcp_instances")
    if not bust:
        cached = await cache_service.get(cache_key)
        if cached:
            return cached

    deploy_jobs = (
        db.query(Job)
        .filter(
            Job.job_type == "gce_deploy",
            Job.status == "completed",
        )
        .order_by(Job.created_at.desc())
        .all()
    )

    by_zone: dict[str, list[str]] = {}
    job_meta: dict[str, dict] = {}
    for job in deploy_jobs:
        if not job.extra_data:
            continue
        try:
            data = json.loads(job.extra_data)
        except Exception:
            continue
        name = data.get("instance_name")
        zone = data.get("zone") or _gcp_zone()
        if name:
            by_zone.setdefault(zone, []).append(name)
            job_meta[name] = {"job_id": job.id, "deployed_by": job.created_by, "extra": data}

    instances = []
    try:
        for zone, names in by_zone.items():
            live = await gcp_service.describe_instances(project_id=project_id, zone=zone, instance_names=names)
            for inst in live:
                meta = job_meta.get(inst["instance_name"], {})
                inst["job_id"] = meta.get("job_id")
                inst["deployed_by"] = meta.get("deployed_by")
                instances.append(inst)
    except gcp_service.GCPError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    result = GCPInstanceListResponse(instances=instances, project_id=project_id, zone=_gcp_zone())
    await cache_service.set(cache_key, result.model_dump(), ttl=60)
    return result


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


@router.post("/deploy", response_model=GCPDeployResponse)
async def deploy_instance(
    payload: GCPDeployRequest,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(require_permission("gcp", "write")),
    db: Session = Depends(get_db),
):
    """Deploy a GCE instance from an image. Runs in background; returns job ID immediately."""
    project_id = _gcp_project()
    if not project_id:
        raise HTTPException(status_code=400, detail="GCP project ID not configured — run the setup wizard.")

    zone = payload.zone or _gcp_zone()

    job = job_service.create_job(
        db,
        job_type="gce_deploy",
        created_by=current_user.username,
        metadata={
            "project_id":       project_id,
            "zone":             zone,
            "instance_name":    payload.instance_name,
            "machine_type":     payload.machine_type,
            "image_self_link":  payload.image_self_link,
            "image_name":       payload.image_name,
        },
    )
    job_service.log_audit(
        db,
        current_user.username,
        "gce_deploy",
        details={"instance_name": payload.instance_name, "zone": zone, "machine_type": payload.machine_type},
    )

    background_tasks.add_task(_run_deploy, job.id, payload, project_id, zone)
    return GCPDeployResponse(job_id=job.id, status="pending", message=f"Deploying {payload.instance_name}…")


async def _run_deploy(job_id: str, payload: GCPDeployRequest, project_id: str, zone: str) -> None:
    from ..services import config_service as _cfg_svc
    db = _get_db_session()
    try:
        job_service.set_running(db, job_id)

        # Retrieve SSH public key
        secret_name = _cfg_svc.get("gcp_ssh_key_secret_name") or ""
        ssh_username = _cfg_svc.get("gcp_ssh_username") or payload.ssh_username or "gcp-user"
        ssh_public_key = ""
        if secret_name:
            job_service.update_progress(db, job_id, 10, "Retrieving SSH public key from Secret Manager…")
            try:
                ssh_public_key = await gcp_service.get_ssh_public_key(
                    project_id=project_id, secret_name=secret_name
                )
            except Exception as exc:
                logger.warning("Could not fetch SSH key from Secret Manager: %s", exc)

        job_service.update_progress(db, job_id, 20, "Launching Compute Engine instance…")

        result = await gcp_service.launch_instance(
            project_id=project_id,
            zone=zone,
            instance_name=payload.instance_name,
            machine_type=payload.machine_type,
            image_self_link=payload.image_self_link,
            subnetwork=payload.subnetwork,
            create_external_ip=payload.create_external_ip,
            ssh_username=ssh_username,
            ssh_public_key=ssh_public_key,
            disk_size_gb=payload.disk_size_gb,
            network_tags=payload.network_tags or [],
        )

        hostname = result.get("private_ip") or result.get("public_ip") or payload.instance_name

        final_meta = {
            "instance_name": result["instance_name"],
            "zone":          result["zone"],
            "machine_type":  result["machine_type"],
            "status":        result["status"],
            "public_ip":     result.get("public_ip"),
            "private_ip":    result.get("private_ip"),
            "self_link":     result.get("self_link", ""),
            "image_self_link": payload.image_self_link,
            "image_name":    payload.image_name,
        }

        # ── BeyondTrust PRA — Shell Jump (optional) ───────────────────────────
        if _cfg_svc.get_bool("beyondtrust_enabled"):
            from ..services import terraform_pra_service
            jump_group = _cfg_svc.get("gcp_bt_jump_group_name") or _cfg_svc.get("bt_jump_group_name") or settings.bt_jump_group_name
            jumpoint_name = _cfg_svc.get("gcp_jumpoint_name") or _cfg_svc.get("bt_jumpoint_name") or settings.bt_jumpoint_name
            job_service.update_progress(db, job_id, 90, f"Instance launched ({hostname}), provisioning Shell Jump…")
            try:
                bt_result = await terraform_pra_service.provision_jump(
                    vm_name=payload.instance_name,
                    hostname=hostname,
                    jump_group_name=jump_group,
                    jumpoint_name=jumpoint_name,
                    tag="GCP",
                )
                final_meta["bt_shell_jump_id"] = bt_result.get("shell_jump_id")
                final_meta["bt_jump_group_name"] = bt_result.get("jump_group_name")
                final_meta["bt_tf_state"] = bt_result.get("tf_state_json")
                job_service.update_progress(
                    db, job_id, 95,
                    f"Shell Jump created (ID: {bt_result.get('shell_jump_id')}, group: {jump_group})"
                )
            except Exception as bt_exc:
                final_meta["bt_error"] = str(bt_exc)
                job_service.update_progress(
                    db, job_id, 95,
                    f"Instance deployed but Shell Jump provisioning failed: {bt_exc}"
                )
        else:
            job_service.update_progress(db, job_id, 95, "Instance launched.")

        job_service.set_completed(db, job_id, final_meta)
        await cache_service.invalidate(cache_service.key_global("gcp_instances"))

    except Exception as exc:
        logger.error("GCE deploy failed for job %s: %s", job_id, exc)
        job_service.set_failed(db, job_id, str(exc))
    finally:
        db.close()


@router.post("/instances/{instance_name}/create-image", response_model=GCPDeployResponse)
async def create_image_from_instance(
    instance_name: str,
    payload: GCPCreateImageRequest,
    background_tasks: BackgroundTasks,
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
        metadata={"instance_name": instance_name, "image_name": payload.image_name},
    )
    background_tasks.add_task(
        _run_capture, job.id, project_id, zone, instance_name, payload.image_name, payload.description
    )
    return GCPDeployResponse(
        job_id=job.id, status="pending",
        message=f"Creating image {payload.image_name} from {instance_name}…"
    )


async def _run_capture(
    job_id: str,
    project_id: str,
    zone: str,
    instance_name: str,
    image_name: str,
    description: str,
) -> None:
    db = _get_db_session()
    try:
        job_service.set_running(db, job_id)
        job_service.update_progress(db, job_id, 20, "Creating image from instance disk…")
        result = await gcp_service.create_image_from_instance(
            project_id=project_id, zone=zone,
            instance_name=instance_name, image_name=image_name, description=description,
        )
        job_service.set_completed(db, job_id, result)
        await cache_service.invalidate(cache_service.key_global("gcp_custom_images"))
    except Exception as exc:
        logger.error("GCE image capture failed for job %s: %s", job_id, exc)
        job_service.set_failed(db, job_id, str(exc))
    finally:
        db.close()


@router.delete("/instances/{instance_name}")
async def destroy_instance(
    instance_name: str,
    zone: str = Query("", description="Zone the instance is in; defaults to configured zone"),
    background_tasks: BackgroundTasks = None,
    current_user: User = Depends(require_permission("gcp", "delete")),
    db: Session = Depends(get_db),
):
    """Terminate a GCE instance. Runs in background."""
    project_id = _gcp_project()
    if not project_id:
        raise HTTPException(status_code=400, detail="GCP project ID not configured.")
    resolved_zone = zone or _gcp_zone()

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
        },
    )
    job_service.log_audit(
        db, current_user.username, "gce_destroy",
        details={"instance_name": instance_name, "zone": resolved_zone},
    )
    background_tasks.add_task(
        _run_destroy, job.id, project_id, resolved_zone, instance_name,
        deploy_job.id if deploy_job else None,
    )
    return {"job_id": job.id, "status": "pending", "message": f"Terminating {instance_name}…"}


async def _run_destroy(
    job_id: str, project_id: str, zone: str, instance_name: str,
    deploy_job_id: Optional[str] = None,
) -> None:
    db = _get_db_session()
    try:
        job_service.set_running(db, job_id)
        result = {"instance_name": instance_name, "zone": zone}

        # Remove BeyondTrust Shell Jump before terminating the instance
        deploy_meta = {}
        if deploy_job_id:
            deploy_job = job_service.get_job(db, deploy_job_id)
            if deploy_job:
                deploy_meta = deploy_job.metadata_dict

        bt_shell_jump_id = deploy_meta.get("bt_shell_jump_id")
        if bt_shell_jump_id and settings.beyondtrust_enabled:
            job_service.update_progress(
                db, job_id, 20,
                f"Removing BeyondTrust Shell Jump {bt_shell_jump_id}…"
            )
            try:
                tf_state = deploy_meta.get("bt_tf_state")
                if tf_state:
                    from ..services import terraform_pra_service
                    await terraform_pra_service.remove_jump(tf_state)
                else:
                    logger.warning(
                        "bt_shell_jump_id %s has no tf_state — was provisioned before "
                        "Terraform migration. Remove Shell Jump manually from PRA console.",
                        bt_shell_jump_id,
                    )
                    result["bt_error"] = (
                        f"Shell Jump {bt_shell_jump_id} requires manual removal from PRA "
                        "(provisioned before Terraform migration)"
                    )
                result["bt_shell_jump_removed"] = bt_shell_jump_id
            except Exception as e:
                result["bt_error"] = f"Shell Jump removal failed: {e}"

        job_service.update_progress(db, job_id, 50, f"Deleting instance {instance_name}…")
        await gcp_service.terminate_instance(project_id=project_id, zone=zone, instance_name=instance_name)

        if deploy_job_id:
            deploy_meta["destroyed"] = True
            deploy_job = job_service.get_job(db, deploy_job_id)
            if deploy_job:
                job_service.set_completed(db, deploy_job_id, deploy_meta)

        job_service.set_completed(db, job_id, result)
        await cache_service.invalidate(cache_service.key_global("gcp_instances"))
    except Exception as exc:
        logger.error("GCE destroy failed for job %s: %s", job_id, exc)
        job_service.set_failed(db, job_id, str(exc))
    finally:
        db.close()


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
