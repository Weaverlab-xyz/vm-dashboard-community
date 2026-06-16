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

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
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
from ..services import cache_service, job_service, workgroup_service
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


def _jumpoint_name(vm_name: str) -> str:
    """Deterministic Jumpoint VM name. Each user VM gets its own paired
    Jumpoint, mirroring the AWS ECS pattern. GCE names cap at 63 chars."""
    base = f"bt-jumpoint-{vm_name}".lower()
    return base[:63]


async def _resolve_gcp_jumpoint_deploy_key() -> str:
    """Return the BeyondTrust SRA Jumpoint deploy key for GCP launches.
    Resolves through whichever secrets backend the user picked on /secrets;
    `gcp_cloud_run_docker_deploy_key` is the historical key name."""
    from ..services import config_service
    return (
        config_service.get("gcp_cloud_run_docker_deploy_key")
        or config_service.get("gcp_jumpoint_docker_deploy_key")
        or ""
    )


def _gcp_ssh_secret() -> str:
    return _gcp_cfg("gcp_ssh_key_secret_name")


def _get_db_session():
    from ..database import SessionLocal
    return SessionLocal()


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
            # cache_service.get() wraps the payload in {"data": ..., "cached_at": ...};
            # return the inner payload so the response matches the cache-miss shape
            # (issue #5 — deploy modal dropdowns were blank on cache hit because the
            # frontend received {data, cached_at} instead of the options dict).
            return cached["data"]

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
        # Skip instances already terminated via the dashboard — the terminate
        # flow marks the deploy job destroyed=True. Without this, the (now-deleted)
        # instance is re-queried and lingers in the list showing TERMINATED, as
        # GCE briefly returns it post-delete. Mirrors api/aws.py + api/azure.py.
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
    try:
        for zone, names in by_zone.items():
            live = await gcp_service.describe_instances(project_id=project_id, zone=zone, instance_names=names)
            for inst in live:
                meta = job_meta.get(inst["instance_name"], {})
                inst["job_id"] = meta.get("job_id")
                inst["deployed_by"] = meta.get("deployed_by")
                inst["workgroup"] = meta.get("workgroup") or inst.get("workgroup")
                instances.append(inst)
    except gcp_service.GCPError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    full = GCPInstanceListResponse(instances=instances, project_id=project_id, zone=_gcp_zone())
    await cache_service.set(cache_key, full.model_dump(), ttl=60)

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
    workgroup = _validate_workgroup(db, current_user, payload.workgroup)
    payload.workgroup = workgroup

    job = job_service.create_job(
        db,
        job_type="gce_deploy",
        created_by=current_user.username,
        workgroup=workgroup,
        metadata={
            "project_id":       project_id,
            "zone":             zone,
            "instance_name":    payload.instance_name,
            "machine_type":     payload.machine_type,
            "image_self_link":  payload.image_self_link,
            "image_name":       payload.image_name,
            "workgroup":        workgroup,
        },
    )
    job_service.set_cloud_resource_id(db, job.id, payload.instance_name)
    job_service.log_audit(
        db,
        current_user.username,
        "gce_deploy",
        details={"instance_name": payload.instance_name, "zone": zone, "machine_type": payload.machine_type, "workgroup": workgroup},
    )

    background_tasks.add_task(_run_deploy, job.id, payload, project_id, zone)
    return GCPDeployResponse(job_id=job.id, status="pending", message=f"Deploying {payload.instance_name}…")


async def _run_deploy(job_id: str, payload: GCPDeployRequest, project_id: str, zone: str) -> None:
    from ..services import config_service as _cfg_svc
    db = _get_db_session()
    bt_enabled = _cfg_svc.get_bool("beyondtrust_enabled")
    jumpoint_name = ""
    jumpoint_zone = zone
    jumpoint_meta: dict = {}
    try:
        job_service.set_running(db, job_id)

        # ── Step 1: Start BT Jumpoint on COS-on-GCE first (BeyondTrust only) ──
        if bt_enabled:
            jumpoint_name = _jumpoint_name(payload.instance_name)
            jumpoint_image = _cfg_svc.get("gcp_jumpoint_image") or "beyondtrust/sra-jumpoint:latest"
            jumpoint_machine = _cfg_svc.get("gcp_jumpoint_machine_type") or "e2-micro"
            jumpoint_zone = _cfg_svc.get("gcp_jumpoint_zone") or zone
            job_service.update_progress(db, job_id, 5, f"Starting BeyondTrust Jumpoint {jumpoint_name}…")
            try:
                if getattr(payload, "docker_deploy_key_ref", None):
                    deploy_key = _cfg_svc.resolve_reference(payload.docker_deploy_key_ref.strip())
                else:
                    deploy_key = await _resolve_gcp_jumpoint_deploy_key()
                if not deploy_key:
                    raise RuntimeError(
                        "Jumpoint deploy key not configured "
                        "(gcp_cloud_run_docker_deploy_key) — set it in the wizard."
                    )
                jumpoint_meta = await gcp_service.run_gce_jumpoint(
                    project_id=project_id,
                    zone=jumpoint_zone,
                    name=jumpoint_name,
                    container_image=jumpoint_image,
                    deploy_key=deploy_key,
                    subnetwork=payload.subnetwork or "",
                    machine_type=jumpoint_machine,
                    create_external_ip=True,
                )
                job_service.update_progress(
                    db, job_id, 15,
                    f"Jumpoint {jumpoint_name} {'reused' if jumpoint_meta.get('reused') else 'started'}, launching VM…"
                )
            except Exception as e:
                # Non-fatal — continue to VM launch; user may already have a Jumpoint elsewhere.
                jumpoint_meta = {"error": str(e)}
                logger.warning("GCP Jumpoint provisioning failed (non-fatal): %s", e)
                job_service.update_progress(
                    db, job_id, 15,
                    f"Jumpoint provisioning failed (non-fatal): {e} — continuing with VM launch…"
                )

        # Retrieve SSH public key
        secret_name = _cfg_svc.get("gcp_ssh_key_secret_name") or ""
        ssh_username = _cfg_svc.get("gcp_ssh_username") or payload.ssh_username or "gcp-user"
        ssh_public_key = ""
        if secret_name:
            job_service.update_progress(db, job_id, 18, "Retrieving SSH public key from Secret Manager…")
            try:
                ssh_public_key = await gcp_service.get_ssh_public_key(
                    project_id=project_id, secret_name=secret_name
                )
            except Exception as exc:
                logger.warning("Could not fetch SSH key from Secret Manager: %s", exc)

        job_service.update_progress(db, job_id, 20, "Launching Compute Engine instance…")

        # Merge config-driven default network tags (used by sandbox firewall
        # rules) with any tags the user supplied on the deploy form.
        default_tag_csv = _cfg_svc.get("gcp_default_network_tag") or ""
        default_tags = [t.strip() for t in default_tag_csv.split(",") if t.strip()]
        merged_tags = list(dict.fromkeys((payload.network_tags or []) + default_tags))

        wg = getattr(payload, "workgroup", "") or ""
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
            network_tags=merged_tags,
            labels={"workgroup": wg} if wg else None,
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
        if bt_enabled:
            if jumpoint_meta.get("error"):
                final_meta["jumpoint_error"] = jumpoint_meta["error"]
            elif jumpoint_meta.get("name"):
                final_meta["jumpoint_name"] = jumpoint_meta["name"]
                final_meta["jumpoint_zone"] = jumpoint_meta.get("zone", jumpoint_zone)

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
        if bt_shell_jump_id:
            job_service.update_progress(
                db, job_id, 20,
                f"Removing BeyondTrust Shell Jump {bt_shell_jump_id}…"
            )
            try:
                tf_state = deploy_meta.get("bt_tf_state")
                if tf_state:
                    from ..services import terraform_pra_service
                    await terraform_pra_service.remove_jump(tf_state)
                    result["bt_shell_jump_removed"] = bt_shell_jump_id
                    job_service.update_progress(
                        db, job_id, 35,
                        f"Shell Jump {bt_shell_jump_id} removed from PRA."
                    )
                else:
                    msg = (
                        f"Shell Jump {bt_shell_jump_id} requires manual removal from PRA "
                        "(provisioned before Terraform migration — no tf_state stored)"
                    )
                    logger.warning(msg)
                    result["bt_error"] = msg
                    job_service.update_progress(db, job_id, 35, msg)
            except Exception as e:
                err = f"Shell Jump removal failed: {e}"
                logger.error("bt_shell_jump_id=%s destroy error: %s", bt_shell_jump_id, e)
                result["bt_error"] = err
                job_service.update_progress(db, job_id, 35, err)

        job_service.update_progress(db, job_id, 50, f"Deleting instance {instance_name}…")
        await gcp_service.terminate_instance(project_id=project_id, zone=zone, instance_name=instance_name)

        # Clean up paired Jumpoint VM, but only if no other live deploy still references it
        # (multiple VMs may share the same Jumpoint via deploy_key — sibling-aware cleanup).
        jumpoint_name = deploy_meta.get("jumpoint_name") if deploy_job_id else None
        if jumpoint_name:
            sibling_count = sum(
                1 for j in db.query(Job).filter(
                    Job.job_type == "gce_deploy", Job.status == "completed"
                ).all()
                if j.id != deploy_job_id
                and not j.metadata_dict.get("destroyed")
                and j.metadata_dict.get("jumpoint_name") == jumpoint_name
            )
            if sibling_count == 0:
                jumpoint_zone = deploy_meta.get("jumpoint_zone", zone)
                job_service.update_progress(
                    db, job_id, 75, f"Stopping paired Jumpoint {jumpoint_name}…"
                )
                try:
                    await gcp_service.stop_gce_jumpoint(
                        project_id=project_id, zone=jumpoint_zone, name=jumpoint_name
                    )
                    result["jumpoint_stopped"] = jumpoint_name
                except Exception as e:
                    logger.warning("Jumpoint cleanup failed for %s: %s", jumpoint_name, e)
                    result["jumpoint_error"] = f"cleanup failed: {e}"
            else:
                result["jumpoint_shared"] = jumpoint_name
                logger.info(
                    "Leaving Jumpoint %s running — %d other active deploy(s) reference it",
                    jumpoint_name, sibling_count,
                )

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
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("gcp", "write")),
):
    """Manually export a custom GCE image to VHD on the hub backend and
    register it in the image registry. Useful when the auto-export during
    build was skipped or failed."""
    from .packer import export_and_register_gcp

    project_id = _gcp_project()
    if not project_id:
        raise HTTPException(status_code=400, detail="GCP project ID not configured.")

    job = job_service.create_job(
        db,
        job_type="gcp_export_image",
        created_by=current_user.username,
        metadata={"image_name": image_name, "registry_name": req.image_name, "project_id": project_id},
    )
    job_service.log_audit(
        db, current_user.username, "gcp_export_image",
        details={"image_name": image_name, "registry_name": req.image_name},
    )

    # Capture scalars before defining the background closure. FastAPI closes
    # the request's DB session when this handler returns, so `current_user`
    # would be a detached ORM instance by the time _run() executes and any
    # attribute access (e.g. .username) would raise DetachedInstanceError.
    job_id = job.id
    registry_name = req.image_name
    username = current_user.username

    async def _run():
        d = _get_db_session()
        try:
            job_service.set_running(d, job_id)
            result = await export_and_register_gcp(
                d, job_id, registry_name, image_name, project_id, username,
            )
            if result.get("export_error") or result.get("export_skipped"):
                job_service.set_failed(d, job_id, result.get("export_error") or result["export_skipped"])
            else:
                job_service.set_completed(d, job_id, result)
        except Exception as e:
            job_service.set_failed(d, job_id, f"Export failed: {e}")
        finally:
            d.close()

    background_tasks.add_task(_run)
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
