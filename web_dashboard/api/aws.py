"""
AWS API endpoints:
  GET  /api/aws/amis                          - List all AMIs owned by the account
  GET  /api/aws/community-amis                - Browse free-tier-compatible public AMIs
  GET  /api/aws/instances                     - List dashboard-deployed EC2 instances (with live state)
  GET  /api/aws/network-options               - Subnets and SGs for the deploy form
  GET  /api/aws/secrets/ssh-keys              - List SSH public key secrets from Secrets Manager
  GET  /api/aws/secrets/ssh-keys/{name}       - Retrieve a specific SSH public key for preview
  POST /api/aws/deploy                        - Launch an EC2 instance from an AMI via boto3
  POST /api/aws/amis/copy                     - Copy a community AMI into this account as a private AMI
  DELETE /api/aws/ami/{id}                    - Deregister a private AMI and delete its snapshots
  DELETE /api/aws/instances/{id}              - Terminate a dashboard-deployed EC2 instance via boto3
"""
import asyncio
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.orm import Session

from ..config import settings
from ..database import Job, User, get_db
from ..models.aws import (
    AMIInfo,
    AMIListResponse,
    BulkDeployJobResult,
    BulkDeployRequest,
    BulkDeployResponse,
    CommunityAMIInfo,
    CommunityAMIListResponse,
    CopyAMIRequest,
    CopyAMIResponse,
    CreateImageRequest,
    CreateImageResponse,
    DeployRequest,
    DeployResponse,
    DestroyResponse,
    EC2InstanceInfo,
    EC2InstanceListResponse,
    NetworkOptions,
    SSHKeySecretDetail,
)
from ..services import aws_service, job_service, cache_service, cloud_stats, workgroup_service
from ..services.aws_service import AWSError
from .auth import get_current_user, require_admin, require_permission

from pydantic import BaseModel

router = APIRouter(prefix="/api/aws", tags=["aws"])


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


def _aws_cfg(key: str, fallback: str = "") -> str:
    """Read a config key from config_service first, fall back to settings env var."""
    from ..services import config_service
    return config_service.get(key) or getattr(settings, key, None) or fallback

def _aws_region() -> str:
    return _aws_cfg("aws_region") or "us-east-2"

def _ssh_key_secret() -> str:
    return _aws_cfg("ec2_ssh_key_secret") or ""

def _ssm_instance_profile() -> str:
    return _aws_cfg("ec2_ssm_instance_profile") or ""


async def _validate_ssh_key_override(override: Optional[str]) -> None:
    """When the operator overrides the SSH key secret at launch, require it to be a
    JSON object with a ``public_key`` (so the VM is reachable). Raises HTTP 400."""
    if not override:
        return
    from ..services import ssh_key_secret
    try:
        raw = await aws_service.get_secret(override, _aws_region())
    except AWSError as e:
        raise HTTPException(status_code=400, detail=f"SSH key secret '{override}' could not be read: {e}")
    try:
        ssh_key_secret.validate_public_key_secret(raw, secret_name=override)
    except ssh_key_secret.SshKeySecretError as e:
        raise HTTPException(status_code=400, detail=str(e))


async def _resolve_aws_ecs_deploy_key() -> str:
    """Return the BeyondTrust Jumpoint Docker deploy key for AWS ECS launches.

    Resolution order:
      1. Direct DB field `aws_ecs_docker_deploy_key` (preferred, backend-neutral
         — config_service resolves through whichever secrets backend the user
         picked on /secrets).
      2. Legacy Password-Safe-only fallback via `bt_ps_deploy_key_title`.
    Returns empty string if neither is configured (caller decides if that's fatal).
    """
    direct = _aws_cfg("aws_ecs_docker_deploy_key")
    if direct:
        return direct
    title = _aws_cfg("bt_ps_deploy_key_title")
    if title:
        from ..services import btapi_service
        try:
            return await btapi_service.get_ps_secret(title)
        except Exception as e:
            logger.warning("AWS ECS deploy key fetch from Password Safe failed (%s)", e)
    return ""


# ── AMI listing ───────────────────────────────────────────────────────────────

@router.get("/amis", response_model=AMIListResponse)
async def list_amis(
    current_user: User = Depends(require_permission("aws", "read")),
):
    """List all AMIs owned by this AWS account. Served from cache (5 min TTL)."""
    cache_key = cache_service.key_global("aws_amis")
    ttl = cache_service.TTL["aws_amis"]

    async def _fetch():
        return await aws_service.list_amis(_aws_region())

    try:
        amis, cached_at = await cache_service.get_or_refresh(cache_key, ttl, _fetch)
        return AMIListResponse(
            amis=[AMIInfo(**a) for a in amis],
            count=len(amis),
            cached_at=cached_at,
        )
    except AWSError as e:
        raise HTTPException(status_code=503, detail=str(e))


# ── Network options for deploy form ──────────────────────────────────────────

@router.get("/network-options", response_model=NetworkOptions)
async def network_options(
    current_user: User = Depends(require_permission("aws", "read")),
):
    """Return key pairs, subnets, and security groups for the deploy form. Served from cache (10 min TTL)."""
    cache_key = cache_service.key_global("aws_network_opts")
    ttl = cache_service.TTL["aws_network_opts"]

    async def _fetch():
        return await aws_service.get_network_options(_aws_region())

    try:
        opts, cached_at = await cache_service.get_or_refresh(cache_key, ttl, _fetch)
        return NetworkOptions(**opts, cached_at=cached_at)
    except AWSError as e:
        raise HTTPException(status_code=503, detail=str(e))


# ── EC2 instance listing ──────────────────────────────────────────────────────

async def _fetch_instances(db: Session) -> list:
    """Dashboard-deployed EC2 instances (completed, non-destroyed ec2_deploy jobs)
    merged with live state. Shared by /instances and /dashboard-stats so both hit
    the same cache key. Returns dicts carrying `state` + `workgroup`."""
    deploy_jobs = (
        db.query(Job)
        .filter(Job.job_type == "ec2_deploy", Job.status == "completed")
        .order_by(Job.created_at.desc())
        .all()
    )
    active_jobs = []
    instance_ids = []
    for job in deploy_jobs:
        meta = job.metadata_dict
        if meta.get("destroyed"):
            continue
        iid = meta.get("instance_id")
        if iid:
            active_jobs.append(job)
            instance_ids.append(iid)

    if not instance_ids:
        return []

    live_instances = await aws_service.describe_instances(_aws_region(), instance_ids)
    live_by_id = {inst["instance_id"]: inst for inst in live_instances}
    job_by_instance = {job.metadata_dict.get("instance_id"): job for job in active_jobs}

    result = []
    for iid in instance_ids:
        live = live_by_id.get(iid)
        if not live:
            continue
        job = job_by_instance.get(iid)
        wg = (job.workgroup or "").lower() if job and job.workgroup else None
        result.append({
            **live,
            "key_name": live.get("key_name"),
            "workgroup": wg,
            "job_id": job.id if job else None,
            "deployed_by": job.created_by if job else None,
        })
    return result


@router.get("/dashboard-stats")
async def aws_dashboard_stats(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("aws", "read")),
):
    """One-call counts for the AWS dashboard tiles (instances total+running, AMIs
    total) — reuses the same cached data + RBAC as the list endpoints, so it adds
    no cloud calls on a warm cache. A null section → the tile shows unavailable."""
    out = {"instances": None, "images": None}
    try:
        raw, _ = await cache_service.get_or_refresh(
            cache_service.key_global("aws_instances"),
            cache_service.TTL["aws_instances"],
            lambda: _fetch_instances(db))
        out["instances"] = cloud_stats.summarize_instances(
            raw, _accessible_workgroups(current_user), "state")
    except AWSError:
        pass
    try:
        amis, _ = await cache_service.get_or_refresh(
            cache_service.key_global("aws_amis"),
            cache_service.TTL["aws_amis"],
            lambda: aws_service.list_amis(_aws_region()))
        out["images"] = {"total": len(amis)}
    except AWSError:
        pass
    return out


@router.get("/instances", response_model=EC2InstanceListResponse)
async def list_instances(
    workgroup: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("aws", "read")),
):
    """
    List dashboard-deployed EC2 instances. Served from cache (1 min TTL).
    Queries jobs DB for completed ec2_deploy jobs, then fetches live state from AWS.

    Filtering: non-admins see only instances whose `Job.workgroup` is in their
    workgroup list. Admins see all. `?workgroup=<name>` narrows further.
    """
    accessible = _accessible_workgroups(current_user)
    if workgroup is not None:
        canonical = workgroup.lower()
        if accessible is not None and canonical not in accessible:
            raise HTTPException(status_code=403, detail=f"No access to workgroup '{canonical}'")

    cache_key = cache_service.key_global("aws_instances")
    ttl = cache_service.TTL["aws_instances"]

    try:
        raw, cached_at = await cache_service.get_or_refresh(
            cache_key, ttl, lambda: _fetch_instances(db))
        filtered = []
        for inst in raw:
            inst_wg = inst.get("workgroup")
            if workgroup is not None and inst_wg != workgroup.lower():
                continue
            if accessible is not None:
                if inst_wg is None or inst_wg not in accessible:
                    continue
            filtered.append(inst)
        instances = [EC2InstanceInfo(**i) for i in filtered]
        return EC2InstanceListResponse(instances=instances, count=len(instances), cached_at=cached_at)
    except AWSError as e:
        raise HTTPException(status_code=503, detail=str(e))


# ── Community AMI browser ────────────────────────────────────────────────────

@router.get("/community-amis", response_model=CommunityAMIListResponse)
async def list_community_amis(
    os_filter: Optional[str] = None,
    current_user: User = Depends(require_permission("aws", "read")),
):
    """
    Browse free-tier-compatible public AMIs from well-known AWS owners
    (Amazon Linux, Ubuntu, Debian).  Pass ?os_filter=amazon-linux|ubuntu|debian
    to narrow results; omit for all three.
    """
    try:
        amis = await aws_service.search_community_amis(_aws_region(), os_filter)
        return CommunityAMIListResponse(
            amis=[CommunityAMIInfo(**a) for a in amis],
            count=len(amis),
        )
    except AWSError as e:
        raise HTTPException(status_code=503, detail=str(e))


# ── Copy community AMI → private ─────────────────────────────────────────────

@router.post("/amis/copy", response_model=CopyAMIResponse)
async def copy_community_ami(
    req: CopyAMIRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("aws", "write")),
):
    """
    Copy a public/community AMI into this AWS account as a private AMI.
    The copy runs as a background job (AWS typically takes 2–10 minutes).
    Track progress at /jobs/{job_id}.
    """
    job = job_service.create_job(
        db,
        job_type="ami_copy",
        created_by=current_user.username,
        metadata={
            "source_ami_id": req.source_ami_id,
            "name": req.name,
            "description": req.description,
        },
    )

    job_service.log_audit(
        db, current_user.username, "ami_copy",
        details={"source_ami_id": req.source_ami_id, "name": req.name},
    )

    background_tasks.add_task(_run_ami_copy, job.id, req)

    return CopyAMIResponse(
        job_id=job.id,
        status="pending",
        message=f"AMI copy queued: {req.source_ami_id} → {req.name}",
    )


# ── SSH key secret preview (Secrets Manager) ─────────────────────────────────

@router.get("/secrets/ssh-key", response_model=SSHKeySecretDetail)
async def get_configured_ssh_key(
    current_user: User = Depends(require_permission("aws", "read")),
):
    """Retrieve the configured SSH public key from Secrets Manager for preview in the deploy modal."""
    secret_name = _ssh_key_secret()
    if not secret_name:
        raise HTTPException(
            status_code=404,
            detail="No SSH key secret configured. Go to Setup → AWS Advanced settings."
        )
    try:
        detail = await aws_service.get_ssh_public_key_from_secret(_aws_region(), secret_name)
        return SSHKeySecretDetail(**detail)
    except AWSError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/secrets/ssh-keys")
async def list_ssh_key_secret_names(
    current_user: User = Depends(require_permission("aws", "read")),
):
    """Candidate secrets for the per-launch SSH-key-secret override picker."""
    try:
        return {"secrets": await aws_service.list_secret_names(_aws_region())}
    except AWSError as e:
        raise HTTPException(status_code=503, detail=str(e))


# ── Deploy ────────────────────────────────────────────────────────────────────

@router.post("/deploy", response_model=DeployResponse)
async def deploy_ami(
    req: DeployRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("aws", "write")),
):
    """
    Launch an EC2 instance from an AMI using the AWS API.
    Returns a job_id trackable at /api/jobs/{job_id} or /api/ws/jobs/{job_id}.
    """
    workgroup = _validate_workgroup(db, current_user, req.workgroup)
    await _validate_ssh_key_override(req.ssh_key_secret_override)

    job = job_service.create_job(
        db,
        job_type="ec2_deploy",
        created_by=current_user.username,
        workgroup=workgroup,
        metadata={
            "ami_id": req.ami_id,
            "instance_name": req.instance_name,
            "instance_type": req.instance_type,
            "subnet_id": req.subnet_id,
            "security_group_ids": req.security_group_ids,
            "workgroup": workgroup,
            "register_in_entitle": req.register_in_entitle,
            "register_in_passwordsafe": req.register_in_passwordsafe,
            "ssh_key_secret_override": req.ssh_key_secret_override,
        },
    )

    job_service.log_audit(
        db, current_user.username, "ec2_deploy",
        details={"ami_id": req.ami_id, "instance_name": req.instance_name, "workgroup": workgroup},
    )

    background_tasks.add_task(
        _run_deploy,
        job.id,
        req.ami_id,
        req.instance_name,
        req.instance_type,
        req.subnet_id,
        req.security_group_ids,
        workgroup,
        req.jump_group,
        req.jumpoint_name,
        req.pra_credential_ref,
    )

    return DeployResponse(
        job_id=job.id,
        status="pending",
        message=f"EC2 deployment queued for AMI {req.ami_id}",
    )


# ── Bulk Deploy ───────────────────────────────────────────────────────────────

@router.post("/bulk-deploy", response_model=BulkDeployResponse)
async def bulk_deploy_amis(
    req: BulkDeployRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("aws", "write")),
):
    """
    Launch multiple EC2 instances from a list of AMIs in one request.
    Each AMI gets its own job_id. All share the same instance type, key pair,
    subnet, and security groups. A single ECS Jumpoint container is started for
    the entire batch (instead of one per instance). Returns a list of job IDs.
    """
    if not req.items:
        raise HTTPException(status_code=400, detail="At least one AMI item is required.")

    workgroup = _validate_workgroup(db, current_user, req.workgroup)
    await _validate_ssh_key_override(req.ssh_key_secret_override)

    # Create one job per instance up front so callers get all job IDs immediately
    job_items: list[tuple[str, object]] = []
    for item in req.items:
        job = job_service.create_job(
            db,
            job_type="ec2_deploy",
            created_by=current_user.username,
            workgroup=workgroup,
            metadata={
                "ami_id": item.ami_id,
                "instance_name": item.instance_name,
                "instance_type": req.instance_type,
                "subnet_id": req.subnet_id,
                "security_group_ids": req.security_group_ids,
                "workgroup": workgroup,
                "bulk": True,
                "register_in_entitle": req.register_in_entitle,
                "register_in_passwordsafe": req.register_in_passwordsafe,
                "ssh_key_secret_override": req.ssh_key_secret_override,
            },
        )
        job_service.log_audit(
            db, current_user.username, "ec2_deploy",
            details={"ami_id": item.ami_id, "instance_name": item.instance_name, "workgroup": workgroup, "bulk": True},
        )
        job_items.append((job.id, item))

    # One background task for the whole batch — shares a single ECS container
    background_tasks.add_task(
        _run_bulk_deploy,
        job_items,
        req.instance_type,
        req.subnet_id,
        req.security_group_ids,
        workgroup,
    )

    results = [
        BulkDeployJobResult(
            ami_id=item.ami_id,
            instance_name=item.instance_name,
            job_id=job_id,
            status="pending",
        )
        for job_id, item in job_items
    ]
    return BulkDeployResponse(jobs=results, count=len(results))


# ── Deregister AMI ────────────────────────────────────────────────────────────

@router.post("/ami/{ami_id}/enable-ena")
async def enable_ami_ena(
    ami_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("aws", "write")),
):
    """Enable ENA (Enhanced Networking) on a private AMI.
    OVA-imported AMIs lack ENA by default; this is required for t3/m5/c5/r5+ instance types.
    This is a metadata-only change — no snapshot modification needed."""
    try:
        new_ami_id = await aws_service.enable_ena_support(_aws_region(), ami_id)
    except AWSError as e:
        raise HTTPException(status_code=400, detail=str(e))

    job_service.log_audit(
        db, current_user.username, "enable_ena",
        details={"source_ami_id": ami_id, "new_ami_id": new_ami_id},
    )
    await cache_service.invalidate(cache_service.key_global("aws_amis"))
    return {"success": True, "source_ami_id": ami_id, "new_ami_id": new_ami_id}


@router.delete("/ami/{ami_id}")
async def deregister_ami(
    ami_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("aws", "delete")),
):
    """Deregister a private AMI and delete its backing EBS snapshots."""
    try:
        deleted_snapshots = await aws_service.deregister_ami(_aws_region(), ami_id)
    except AWSError as e:
        raise HTTPException(status_code=400, detail=str(e))

    job_service.log_audit(
        db, current_user.username, "deregister_ami",
        details={"ami_id": ami_id, "deleted_snapshots": deleted_snapshots},
    )
    # Invalidate the private AMI list cache
    await cache_service.invalidate(cache_service.key_global("aws_amis"))

    return {
        "deregistered": True,
        "ami_id": ami_id,
        "deleted_snapshots": deleted_snapshots,
    }


# ── Reassign workgroup ───────────────────────────────────────────────────────

class _WorkgroupReassignRequest(BaseModel):
    workgroup: str


@router.patch("/instances/{instance_id}/workgroup")
async def reassign_instance_workgroup(
    instance_id: str,
    req: _WorkgroupReassignRequest,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    """Rewrite the `Workgroup` tag on an EC2 instance and update the originating
    Job row. Admin only."""
    wg = workgroup_service.get(db, req.workgroup)
    if not wg:
        raise HTTPException(status_code=400, detail=f"Unknown workgroup '{req.workgroup}'")
    canonical = wg.name

    region = _aws_cfg("aws_region") or "us-east-2"
    try:
        await aws_service.set_workgroup_tag(region, instance_id, canonical)
    except AWSError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    job = db.query(Job).filter(Job.cloud_resource_id == instance_id).first()
    if job is None:
        for j in db.query(Job).filter(Job.job_type == "ec2_deploy").all():
            if j.metadata_dict.get("instance_id") == instance_id:
                job = j
                break

    if job is not None:
        job.workgroup = canonical
        meta = job.metadata_dict
        meta["workgroup"] = canonical
        job.metadata_dict = meta
        if not job.cloud_resource_id:
            job.cloud_resource_id = instance_id
        db.commit()

    await cache_service.invalidate(cache_service.key_global("aws_instances"))
    return {"instance_id": instance_id, "workgroup": canonical, "job_id": job.id if job else None}


# ── Terminate ─────────────────────────────────────────────────────────────────

@router.delete("/instances/{instance_id}", response_model=DestroyResponse)
async def destroy_instance(
    instance_id: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("aws", "delete")),
):
    """
    Terminate a dashboard-deployed EC2 instance via the AWS API.
    Only instances tracked in the dashboard DB can be terminated here.
    """
    # Find the deploy job for this instance
    deploy_jobs = (
        db.query(Job)
        .filter(Job.job_type == "ec2_deploy", Job.status == "completed")
        .all()
    )

    deploy_job = None
    for job in deploy_jobs:
        meta = job.metadata_dict
        if meta.get("instance_id") == instance_id and not meta.get("destroyed"):
            deploy_job = job
            break

    if not deploy_job:
        raise HTTPException(
            status_code=404,
            detail=f"No active deployment found for instance {instance_id}. "
                   "It may have already been terminated or was not deployed from this dashboard.",
        )

    destroy_job = job_service.create_job(
        db,
        job_type="ec2_destroy",
        created_by=current_user.username,
        metadata={
            "instance_id": instance_id,
            "deploy_job_id": deploy_job.id,
        },
    )

    job_service.log_audit(
        db, current_user.username, "ec2_destroy",
        details={"instance_id": instance_id},
    )

    background_tasks.add_task(_run_destroy, destroy_job.id, deploy_job.id, instance_id)

    return DestroyResponse(
        job_id=destroy_job.id,
        status="pending",
        message=f"EC2 instance {instance_id} termination queued",
    )


# ── Export AMI to portable VHD on hub backend ────────────────────────────────

class ExportImageRequest(BaseModel):
    image_name: str  # Registry name to record the exported image under


class ExportImageResponse(BaseModel):
    job_id: str
    status: str
    message: str


@router.post("/amis/{ami_id}/export", response_model=ExportImageResponse)
async def export_ami(
    ami_id: str,
    req: ExportImageRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("aws", "write")),
):
    """Manually export an existing AMI to VHD on the hub backend and register
    it in the image registry. Useful when the auto-export in the Packer build
    flow was skipped or failed but the AMI itself is fine."""
    from .packer import export_and_register_aws  # local import: avoid cycle

    job = job_service.create_job(
        db,
        job_type="aws_export_image",
        created_by=current_user.username,
        metadata={"ami_id": ami_id, "image_name": req.image_name, "region": _aws_region()},
    )
    job_service.log_audit(
        db, current_user.username, "aws_export_image",
        details={"ami_id": ami_id, "image_name": req.image_name},
    )

    # Capture scalars before defining the background closure. FastAPI closes
    # the request's DB session when this handler returns, so `current_user`
    # would be a detached ORM instance by the time _run() executes and any
    # attribute access (e.g. .username) would raise DetachedInstanceError.
    job_id = job.id
    image_name = req.image_name
    region = _aws_region()
    username = current_user.username

    async def _run():
        d = _get_db_session()
        try:
            job_service.set_running(d, job_id)
            result = await export_and_register_aws(
                d, job_id, image_name, ami_id, region, username,
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
        message=f"Export of {ami_id} queued",
    )


# ── Create image from instance ────────────────────────────────────────────────

@router.post("/instances/{instance_id}/create-image", response_model=CreateImageResponse)
async def create_image_from_instance(
    instance_id: str,
    req: CreateImageRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("aws", "write")),
):
    """
    Create an AMI from a running EC2 instance (AWS CreateImage API).
    By default the instance is NOT rebooted (no_reboot=True), so the image
    may have filesystem inconsistencies — suitable for most Linux workloads.
    The image creation runs as a background job; AWS typically takes 5–20 minutes.
    """
    job = job_service.create_job(
        db,
        job_type="ec2_create_image",
        created_by=current_user.username,
        metadata={
            "instance_id": instance_id,
            "name": req.name,
            "description": req.description,
            "no_reboot": req.no_reboot,
        },
    )

    job_service.log_audit(
        db, current_user.username, "ec2_create_image",
        details={"instance_id": instance_id, "name": req.name},
    )

    background_tasks.add_task(_run_create_image, job.id, instance_id, req)

    return CreateImageResponse(
        job_id=job.id,
        status="pending",
        message=f"Image creation queued for instance {instance_id}",
    )


# ── SSH key retrieval from Secrets Manager ────────────────────────────────────

@router.get("/instances/{instance_id}/ssh-key")
async def get_instance_ssh_key(
    instance_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("aws", "read")),
):
    """
    Retrieve the private key for the EC2 key pair used by this instance.
    Looks up the key name from the deploy job record, then fetches the PEM
    contents from Secrets Manager using the naming convention:
        ec2/keypairs/<key-name>

    Store the private key there once (via AWS Console or CLI) and this
    endpoint will surface it whenever you need to SSH into the instance.
    """
    # Find the deploy job to get the key_name
    deploy_jobs = (
        db.query(Job)
        .filter(Job.job_type == "ec2_deploy", Job.status == "completed")
        .all()
    )
    key_name = None
    for job in deploy_jobs:
        meta = job.metadata_dict
        if meta.get("instance_id") == instance_id and not meta.get("destroyed"):
            key_name = meta.get("key_name")
            break

    if not key_name:
        raise HTTPException(
            status_code=404,
            detail="No active deployment record found for this instance, or key name is unknown.",
        )

    try:
        private_key = await aws_service.get_keypair_private_key(_aws_region(), key_name)
    except AWSError as e:
        raise HTTPException(status_code=404, detail=str(e))

    # Get current IP from live instance state
    instances = await aws_service.describe_instances(_aws_region(), [instance_id])
    ip = None
    if instances:
        ip = instances[0].get("public_ip") or instances[0].get("private_ip")

    secret_name = f"ec2/keypairs/{key_name}"
    ssh_command = f"ssh -i <key-file> ec2-user@{ip}" if ip else None

    return {
        "instance_id": instance_id,
        "key_name": key_name,
        "secret_name": secret_name,
        "private_key": private_key,
        "ip": ip,
        "ssh_command": ssh_command,
    }


# ── Background task runners ───────────────────────────────────────────────────

def _get_db_session():
    from ..database import SessionLocal
    return SessionLocal()


def _aws_deploy_payload_hash(**fields) -> str:
    """Stable SHA-256 over the deploy parameters that determine blast radius.

    Used as the elevation request's payload_hash so a granted Entitle
    activation is bound to *this* deploy intent — an attacker who replays
    the activation against a different AMI / subnet / SG list would compute
    a different hash and the audit row no longer matches.
    """
    import hashlib
    import json as _json
    blob = _json.dumps(fields, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _aws_terminate_payload_hash(region: str, instance_id: str) -> str:
    """Payload hash for the EC2 terminate elevation."""
    import hashlib
    blob = f"terminate:{region}:{instance_id}".encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


async def _register_vm_in_entitle(db, job_id: str, vm_name: str, hostname: str,
                                  result: dict, private: bool = True) -> None:
    """Thin wrapper around the shared VM registration hook (tag=AWS). The chosen SSH
    key secret (override or default, recorded on ``result``) drives the private-key
    resolution so registration uses the VM's own keypair. The SSH ``sudo_user`` is the
    image's cloud-default login user (``result['ssh_user']``, from ``detect_os_type``);
    the hook falls back to the configured ``entitle_ssh_sudo_user`` override when blank."""
    from ..services import entitle_vm_hook
    await entitle_vm_hook.register(db, job_id, vm_name, hostname,
                                   private=private, result=result, tag="AWS",
                                   sudo_user=result.get("ssh_user") or "",
                                   ssh_key_secret=result.get("ssh_secret_name") or "")


async def _register_vm_in_passwordsafe(db, job_id: str, vm_name: str, hostname: str,
                                       result: dict, *, instance_id: str = "",
                                       region: str = "") -> None:
    """Thin wrapper around the shared Password Safe VM hook (tag=AWS). Onboards the VM as
    a managed system + its baked-in adminuser account. AWS defaults to the cloud-native
    AWS Systems Manager plugin (managed system DNS = ``{instance_id}:{region}``); the SSH
    method falls back to the VM's own keypair (the deploy / Entitle registration secret)."""
    from ..services import ps_vm_hook
    await ps_vm_hook.register(db, job_id, vm_name, hostname, result=result, tag="AWS",
                              ssh_key_secret=result.get("ssh_secret_name") or "",
                              instance_id=instance_id, region=region)


async def _run_deploy(
    job_id: str,
    ami_id: str,
    instance_name: str,
    instance_type: str,
    subnet_id: str,
    security_group_ids: list,
    workgroup: str = "",
    jump_group: str = None,
    jumpoint_name: str = None,
    pra_credential_ref: str = None,
):
    db = _get_db_session()
    result = {}
    try:
        job_service.set_running(db, job_id)

        # ── Step 1: Start ECS Jumpoint container first (BeyondTrust only) ─────
        from ..services import config_service as _cfg_svc
        _aws_region = _aws_cfg("aws_region") or "us-east-2"
        _meta = (db.query(Job).filter(Job.id == job_id).first().metadata_dict or {})
        ssh_secret_name = _meta.get("ssh_key_secret_override") or _cfg_svc.get("ec2_ssh_key_secret") or ""
        if _cfg_svc.get_bool("beyondtrust_enabled"):
            job_service.update_progress(db, job_id, 15, "Ensuring the shared BeyondTrust Jumpoint host…")
            try:
                from ..services import jumpoint_host_service
                host_id = await jumpoint_host_service.ensure_jumpoint_host("aws", _aws_region)
                if host_id:
                    result["jumpoint_host_id"] = host_id
                job_service.update_progress(db, job_id, 35, "Jumpoint host ready, launching EC2 instance…")
            except Exception as e:
                result["ecs_error"] = str(e)
                job_service.update_progress(
                    db, job_id, 35,
                    f"Jumpoint host ensure failed (non-fatal): {e} — continuing with EC2 launch…"
                )
        else:
            job_service.update_progress(db, job_id, 35, "Preparing EC2 launch…")

        # ── Step 2: Fetch SSH public key from Secrets Manager ──────────────────
        job_service.update_progress(db, job_id, 38, "Fetching SSH public key from Secrets Manager…")
        ami_info = await aws_service.describe_ami(_aws_region, ami_id)
        is_windows = "windows" in (ami_info.get("platform", "") or "").lower()
        if is_windows:
            public_key = ""
            os_type = "windows"
        else:
            from ..services.os_detection import detect_os_type
            os_type, ssh_user = detect_os_type(ami_info.get("name", ""))
            key_detail = await aws_service.get_ssh_public_key_from_secret(_aws_region, ssh_secret_name)
            public_key = key_detail["public_key"]
            result["ssh_secret_name"] = ssh_secret_name
            result["ssh_user"] = ssh_user   # image's cloud-default login user → Entitle sudo_user

        # ── Step 3: Launch EC2 instance ────────────────────────────────────────
        job_service.update_progress(db, job_id, 40, f"Launching EC2 instance ({os_type})…")
        # Cloud-identity JIT Phase 2: bracket the EC2 write in elevate().
        # When the gate is off (default) this is a no-op and the deploy
        # proceeds on baseline creds. When on, Entitle's auto-approve
        # policy decides whether to issue a short-lived grant; failure
        # to grant aborts the deploy before AWS is touched.
        from ..services.cloud_identity_service import elevate, CloudIdentityError
        deploy_payload_hash = _aws_deploy_payload_hash(
            region=_aws_region, ami_id=ami_id, instance_type=instance_type,
            subnet_id=subnet_id, security_group_ids=security_group_ids,
            workgroup=workgroup, instance_name=instance_name,
        )
        _job = job_service.get_job(db, job_id)
        try:
            async with elevate(
                "aws", "aws:ec2:deploy",
                duration_minutes=15,
                payload_hash=deploy_payload_hash,
                requester_user_id=_job.created_by if _job else None,
                workgroup=workgroup or None,
            ) as _elev:
                instance_result = await aws_service.launch_instance(
                    region=_aws_region,
                    ami_id=ami_id,
                    instance_name=instance_name,
                    instance_type=instance_type,
                    public_key=public_key,
                    subnet_id=subnet_id,
                    security_group_ids=security_group_ids,
                    iam_instance_profile=_cfg_svc.get("ec2_ssm_instance_profile") or "",
                    os_type=os_type,
                    workgroup=workgroup,
                    correlation_tag=_elev.correlation_tag,
                )
            result.update(instance_result)
            if instance_result.get("instance_id"):
                job_service.set_cloud_resource_id(db, job_id, instance_result["instance_id"])
        except CloudIdentityError as e:
            job_service.set_failed(db, job_id, f"Cloud-identity elevation refused EC2 deploy: {e}")
            return
        except AWSError as e:
            # EC2 failed. The shared Jumpoint host is ref-counted and may serve
            # other resources, so we don't tear it down here — an idle host is
            # reclaimed on the next destroy/decommission.
            raise

        instance_id = result["instance_id"]
        hostname = result.get("private_ip") or result.get("public_ip") or instance_id
        job_service.update_progress(
            db, job_id, 70,
            f"Instance {instance_id} launched ({hostname}), provisioning Shell Jump…"
        )

        # ── Step 3: BeyondTrust PRA — Shell Jump (optional) ───────────────────
        if _cfg_svc.get_bool("beyondtrust_enabled"):
            from ..services import terraform_pra_service
            try:
                _client_secret = _cfg_svc.resolve_reference(pra_credential_ref.strip()) if pra_credential_ref else ""
                bt_result = await terraform_pra_service.provision_jump(
                    vm_name=instance_name,
                    hostname=hostname,
                    jump_group_name=(jump_group or "").strip() or _cfg_svc.get("bt_jump_group_name") or settings.bt_jump_group_name,
                    jumpoint_name=(jumpoint_name or "").strip() or _cfg_svc.get("bt_jumpoint_name") or settings.bt_jumpoint_name,
                    tag="AWS",
                    client_secret=_client_secret,
                )
                result["bt_shell_jump_id"] = bt_result.get("shell_jump_id")
                result["bt_jump_group_name"] = bt_result.get("jump_group_name")
                result["bt_tf_state"] = bt_result.get("tf_state_json")
                job_service.update_progress(
                    db, job_id, 90,
                    f"Shell Jump created (ID: {bt_result.get('shell_jump_id')}, "
                    f"group: {bt_result.get('jump_group_name')})"
                )
            except Exception as e:
                result["bt_error"] = str(e)
                job_service.update_progress(
                    db, job_id, 90,
                    f"Instance deployed but Shell Jump provisioning failed: {e}"
                )
        else:
            job_service.update_progress(db, job_id, 90, "Instance deployed.")

        # ── Step 4: Entitle — register as SSH ephemeral-accounts integration (optional)
        # Gated by the global capability flag AND the per-build opt-in (job metadata).
        from ..services import entitle_vm_hook
        _job = db.query(Job).filter(Job.id == job_id).first()
        _reg = bool((_job.metadata_dict or {}).get("register_in_entitle")) if _job else False
        if _reg and not is_windows and entitle_vm_hook.registration_enabled():
            await _register_vm_in_entitle(db, job_id, instance_name, hostname,
                                          result, private=not bool(result.get("public_ip")))

        # ── Step 5: Password Safe — onboard as a managed system + account (optional)
        from ..services import ps_vm_hook
        _psreg = bool((_job.metadata_dict or {}).get("register_in_passwordsafe")) if _job else False
        if _psreg and not is_windows and ps_vm_hook.registration_enabled():
            await _register_vm_in_passwordsafe(db, job_id, instance_name, hostname, result,
                                               instance_id=instance_id, region=_aws_region)

        job_service.set_completed(db, job_id, result)
        await cache_service.invalidate(cache_service.key_global("aws_instances"))
        await cache_service.invalidate(cache_service.key_global("cfgmgmt_instances"))

    except AWSError as e:
        job_service.set_failed(db, job_id, str(e))
    except Exception as e:
        job_service.set_failed(db, job_id, f"Unexpected error: {e}")
    finally:
        db.close()


async def _run_bulk_deploy(
    job_items: list,
    instance_type: str,
    subnet_id: str,
    security_group_ids: list,
    workgroup: str = "",
):
    """
    Background task for bulk EC2 deployment.
    Ensures the shared Jumpoint host is up once for the whole batch, then deploys
    each instance sequentially. The host is ref-counted across all EC2 instances
    and databases and reclaimed when the last one is removed.
    """
    db = _get_db_session()
    try:
        # Mark all jobs running
        for job_id, _ in job_items:
            job_service.set_running(db, job_id)

        # Step 1: Start ONE ECS Jumpoint container for the whole batch (BT only)
        from ..services import config_service as _cfg_svc
        _aws_region = _aws_cfg("aws_region") or "us-east-2"
        first_job_id = job_items[0][0]
        _bmeta = (db.query(Job).filter(Job.id == first_job_id).first().metadata_dict or {})
        ssh_secret_name = _bmeta.get("ssh_key_secret_override") or _cfg_svc.get("ec2_ssh_key_secret") or ""
        ecs_error = None
        jumpoint_host_id = None
        if _cfg_svc.get_bool("beyondtrust_enabled"):
            job_service.update_progress(
                db, first_job_id, 10,
                f"Ensuring the shared BeyondTrust Jumpoint host for {len(job_items)}-instance batch…"
            )
            try:
                from ..services import jumpoint_host_service
                jumpoint_host_id = await jumpoint_host_service.ensure_jumpoint_host("aws", _aws_region)
            except Exception as e:
                ecs_error = str(e)
        else:
            job_service.update_progress(
                db, first_job_id, 10,
                f"Preparing {len(job_items)}-instance batch…"
            )

        # Step 2: Fetch SSH public key once for the whole batch
        job_service.update_progress(db, first_job_id, 18, "Fetching SSH public key from Secrets Manager…")
        key_detail = await aws_service.get_ssh_public_key_from_secret(_aws_region, ssh_secret_name)
        shared_public_key = key_detail["public_key"]

        # Step 3: Deploy each instance, all sharing the same ECS task ARN
        for job_id, item in job_items:
            result: dict = {"ssh_secret_name": ssh_secret_name}
            if jumpoint_host_id:
                result["jumpoint_host_id"] = jumpoint_host_id
            elif ecs_error:
                result["ecs_error"] = ecs_error

            try:
                job_service.update_progress(
                    db, job_id, 40, f"Launching EC2 instance {item.instance_name}…"
                )
                ami_info = await aws_service.describe_ami(_aws_region, item.ami_id)
                is_windows = "windows" in (ami_info.get("platform", "") or "").lower()
                if not is_windows:
                    from ..services.os_detection import detect_os_type
                    _, result["ssh_user"] = detect_os_type(ami_info.get("name", ""))
                # Cloud-identity JIT Phase 2: per-instance elevation in
                # the bulk batch. One Entitle activation per EC2 launch
                # so a denial of one row doesn't poison the others.
                from ..services.cloud_identity_service import elevate, CloudIdentityError
                _bulk_job = job_service.get_job(db, job_id)
                _bulk_payload = _aws_deploy_payload_hash(
                    region=_aws_region, ami_id=item.ami_id,
                    instance_type=instance_type, subnet_id=subnet_id,
                    security_group_ids=security_group_ids, workgroup=workgroup,
                    instance_name=item.instance_name,
                )
                async with elevate(
                    "aws", "aws:ec2:deploy",
                    duration_minutes=15,
                    payload_hash=_bulk_payload,
                    requester_user_id=_bulk_job.created_by if _bulk_job else None,
                    workgroup=workgroup or None,
                ) as _bulk_elev:
                    instance_result = await aws_service.launch_instance(
                        region=_aws_region,
                        ami_id=item.ami_id,
                        instance_name=item.instance_name,
                        instance_type=instance_type,
                        public_key="" if is_windows else shared_public_key,
                        subnet_id=subnet_id,
                        security_group_ids=security_group_ids,
                        iam_instance_profile=_cfg_svc.get("ec2_ssm_instance_profile") or "",
                        workgroup=workgroup,
                        correlation_tag=_bulk_elev.correlation_tag,
                    )
                result.update(instance_result)
                if instance_result.get("instance_id"):
                    job_service.set_cloud_resource_id(db, job_id, instance_result["instance_id"])

                instance_id = result["instance_id"]
                hostname = result.get("private_ip") or result.get("public_ip") or instance_id
                job_service.update_progress(
                    db, job_id, 70,
                    f"Instance {instance_id} launched ({hostname}), provisioning Shell Jump…"
                )

                # Step 3: BeyondTrust PRA — Shell Jump per instance (optional)
                if _cfg_svc.get_bool("beyondtrust_enabled"):
                    from ..services import terraform_pra_service
                    try:
                        bt_result = await terraform_pra_service.provision_jump(
                            vm_name=item.instance_name,
                            hostname=hostname,
                            jump_group_name=_cfg_svc.get("bt_jump_group_name") or settings.bt_jump_group_name,
                            jumpoint_name=_cfg_svc.get("bt_jumpoint_name") or settings.bt_jumpoint_name,
                            tag="AWS",
                        )
                        result["bt_shell_jump_id"] = bt_result.get("shell_jump_id")
                        result["bt_jump_group_name"] = bt_result.get("jump_group_name")
                        result["bt_tf_state"] = bt_result.get("tf_state_json")
                        job_service.update_progress(
                            db, job_id, 90,
                            f"Shell Jump created (ID: {bt_result.get('shell_jump_id')}, "
                            f"group: {bt_result.get('jump_group_name')})"
                        )
                    except Exception as e:
                        result["bt_error"] = str(e)
                        job_service.update_progress(
                            db, job_id, 90,
                            f"Instance deployed but Shell Jump provisioning failed: {e}"
                        )
                else:
                    job_service.update_progress(db, job_id, 90, "Instance deployed.")

                # Step 4: Entitle — register as SSH integration (per-build opt-in)
                from ..services import entitle_vm_hook
                _bjob = db.query(Job).filter(Job.id == job_id).first()
                _breg = bool((_bjob.metadata_dict or {}).get("register_in_entitle")) if _bjob else False
                if _breg and not is_windows and entitle_vm_hook.registration_enabled():
                    await _register_vm_in_entitle(db, job_id, item.instance_name, hostname,
                                                  result, private=not bool(result.get("public_ip")))

                # Step 5: Password Safe — onboard as a managed system + account (per-build opt-in)
                from ..services import ps_vm_hook
                _bpsreg = bool((_bjob.metadata_dict or {}).get("register_in_passwordsafe")) if _bjob else False
                if _bpsreg and not is_windows and ps_vm_hook.registration_enabled():
                    await _register_vm_in_passwordsafe(db, job_id, item.instance_name, hostname, result,
                                                       instance_id=instance_id, region=_aws_region)

                job_service.set_completed(db, job_id, result)

            except AWSError as e:
                # EC2 launch failed — mark this job failed but continue the batch
                job_service.set_failed(db, job_id, str(e))
            except Exception as e:
                job_service.set_failed(db, job_id, f"Unexpected error: {e}")

        await cache_service.invalidate(cache_service.key_global("aws_instances"))
        await cache_service.invalidate(cache_service.key_global("cfgmgmt_instances"))

    except Exception as e:
        for job_id, _ in job_items:
            job_service.set_failed(db, job_id, f"Bulk deploy error: {e}")
    finally:
        db.close()


async def _run_destroy(destroy_job_id: str, deploy_job_id: str, instance_id: str):
    db = _get_db_session()
    try:
        job_service.set_running(db, destroy_job_id)
        job_service.update_progress(db, destroy_job_id, 20, f"Terminating instance {instance_id}…")

        # Cloud-identity JIT Phase 2: gate the terminate behind elevate().
        # EC2 TerminateInstances doesn't accept tags, so cloud-side
        # correlation has to come from the activation row (joined by
        # instance_id) instead of an inline tag.
        from ..services.cloud_identity_service import elevate, CloudIdentityError
        _destroy_job = job_service.get_job(db, destroy_job_id)
        _terminate_region = _aws_region()
        try:
            async with elevate(
                "aws", "aws:ec2:terminate",
                duration_minutes=10,
                payload_hash=_aws_terminate_payload_hash(_terminate_region, instance_id),
                requester_user_id=_destroy_job.created_by if _destroy_job else None,
            ):
                result = await aws_service.terminate_instance(_terminate_region, instance_id)
        except CloudIdentityError as e:
            job_service.set_failed(db, destroy_job_id, f"Cloud-identity elevation refused EC2 terminate: {e}")
            return

        deploy_job = job_service.get_job(db, deploy_job_id)
        if deploy_job:
            meta = deploy_job.metadata_dict

            # Remove BeyondTrust Shell Jump if this deploy provisioned one.
            # Check bt_shell_jump_id — not settings.beyondtrust_enabled — so
            # the cleanup still runs even if the feature flag was toggled off
            # after deployment (the jump exists in PRA regardless of the flag).
            bt_shell_jump_id = meta.get("bt_shell_jump_id")
            if bt_shell_jump_id:
                job_service.update_progress(
                    db, destroy_job_id, 70,
                    f"Removing BeyondTrust Shell Jump {bt_shell_jump_id}…"
                )
                try:
                    tf_state = meta.get("bt_tf_state")
                    if tf_state:
                        from ..services import terraform_pra_service
                        await terraform_pra_service.remove_jump(tf_state)
                        result["bt_shell_jump_removed"] = bt_shell_jump_id
                        job_service.update_progress(
                            db, destroy_job_id, 85,
                            f"Shell Jump {bt_shell_jump_id} removed from PRA."
                        )
                    else:
                        # Provisioned before the Terraform migration — no state to destroy with.
                        # btapi is no longer in the container; log and skip.
                        msg = (
                            f"Shell Jump {bt_shell_jump_id} requires manual removal from PRA "
                            "(provisioned before Terraform migration — no tf_state stored)"
                        )
                        logger.warning(msg)
                        result["bt_error"] = msg
                        job_service.update_progress(db, destroy_job_id, 85, msg)
                except Exception as e:
                    err = f"Shell Jump removal failed: {e}"
                    logger.error("bt_shell_jump_id=%s destroy error: %s", bt_shell_jump_id, e)
                    result["bt_error"] = err
                    job_service.update_progress(db, destroy_job_id, 85, err)

            # Remove the Entitle SSH integration if this deploy registered one.
            if meta.get("entitle_registration_tf_state"):
                from ..services import entitle_vm_hook
                await entitle_vm_hook.deregister(meta, result)
                job_service.update_progress(db, destroy_job_id, 88, "Entitle integration removed.")

            # Off-board the Password Safe managed system if this deploy registered one.
            if meta.get("ps_registration_tf_state"):
                from ..services import ps_vm_hook
                await ps_vm_hook.deregister(meta, result)
                job_service.update_progress(db, destroy_job_id, 89, "Password Safe system off-boarded.")

            # Mark original deploy job as destroyed
            meta["destroyed"] = True
            job_service.set_completed(db, deploy_job_id, meta)

        # Terminate the shared Jumpoint host if nothing is left using it (this
        # deploy job is now marked destroyed, so it's excluded from the count).
        try:
            from ..services import jumpoint_host_service
            await jumpoint_host_service.teardown_jumpoint_host_if_idle(db, "aws", _aws_region())
        except Exception as e:
            result["jumpoint_host_teardown_error"] = str(e)

        job_service.set_completed(db, destroy_job_id, result)
        await cache_service.invalidate(cache_service.key_global("aws_instances"))
        await cache_service.invalidate(cache_service.key_global("cfgmgmt_instances"))

    except AWSError as e:
        job_service.set_failed(db, destroy_job_id, str(e))
    except Exception as e:
        job_service.set_failed(db, destroy_job_id, f"Unexpected error: {e}")
    finally:
        db.close()


async def _run_create_image(job_id: str, instance_id: str, req: CreateImageRequest):
    """
    Background task: create an AMI from a running instance then poll until available.
    AWS CreateImage typically takes 5–20 minutes.
    """
    db = _get_db_session()
    try:
        job_service.set_running(db, job_id)
        job_service.update_progress(db, job_id, 10, f"Initiating image creation from instance {instance_id}…")

        new_ami_id = await aws_service.create_image_from_instance(
            region=_aws_region(),
            instance_id=instance_id,
            name=req.name,
            description=req.description,
            no_reboot=req.no_reboot,
        )

        job_service.update_progress(
            db, job_id, 25,
            f"Image {new_ami_id} is pending. Waiting for it to become available…"
        )

        # Poll up to 30 minutes (120 × 15s)
        progress = 25
        for attempt in range(120):
            await asyncio.sleep(15)
            status = await aws_service.get_ami_status(_aws_region(), new_ami_id)
            state = status.get("state", "")

            if state == "available":
                job_service.set_completed(
                    db, job_id,
                    {
                        "new_ami_id": new_ami_id,
                        "instance_id": instance_id,
                        "name": req.name,
                    },
                )
                await cache_service.invalidate(cache_service.key_global("aws_amis"))
                return

            if state == "failed":
                reason = status.get("state_reason", "unknown reason")
                job_service.set_failed(db, job_id, f"Image creation failed: {reason}")
                return

            progress = min(90, 25 + int(attempt / 120 * 65))
            job_service.update_progress(
                db, job_id, progress,
                f"AMI {new_ami_id} state: {state} (attempt {attempt + 1}/120)…"
            )

        job_service.set_failed(
            db, job_id,
            f"Timed out waiting for {new_ami_id} to become available. "
            "Check the AWS console — the image may still be in progress."
        )

    except AWSError as e:
        job_service.set_failed(db, job_id, str(e))
    except Exception as e:
        job_service.set_failed(db, job_id, f"Unexpected error during image creation: {e}")
    finally:
        db.close()


async def _run_ami_copy(job_id: str, req: CopyAMIRequest):
    """
    Background task: copy a public AMI into the account then poll until available.
    AWS copy typically takes 2–10 minutes.
    """
    db = _get_db_session()
    try:
        job_service.set_running(db, job_id)
        job_service.update_progress(db, job_id, 10, f"Initiating AMI copy from {req.source_ami_id}…")

        new_ami_id = await aws_service.copy_ami(
            region=_aws_region(),
            source_ami_id=req.source_ami_id,
            name=req.name,
            description=req.description,
        )

        job_service.update_progress(
            db, job_id, 30,
            f"Copy started — new AMI {new_ami_id} is pending. Waiting for it to become available…"
        )

        # Poll up to 20 minutes (80 × 15s)
        progress = 30
        for attempt in range(80):
            await asyncio.sleep(15)
            status = await aws_service.get_ami_status(_aws_region(), new_ami_id)
            state = status.get("state", "")

            if state == "available":
                job_service.set_completed(
                    db, job_id,
                    {
                        "new_ami_id": new_ami_id,
                        "source_ami_id": req.source_ami_id,
                        "name": req.name,
                    },
                )
                await cache_service.invalidate(cache_service.key_global("aws_amis"))
                return

            if state == "failed":
                reason = status.get("state_reason", "unknown reason")
                job_service.set_failed(db, job_id, f"AMI copy failed: {reason}")
                return

            # Advance progress from 30 → 90 gradually
            progress = min(90, 30 + int(attempt / 80 * 60))
            job_service.update_progress(
                db, job_id, progress,
                f"AMI {new_ami_id} state: {state} (attempt {attempt + 1}/80)…"
            )

        # Timed out but copy may still be running in AWS — record what we know
        job_service.set_failed(
            db, job_id,
            f"Timed out waiting for {new_ami_id} to become available. "
            "Check the AWS console — the copy may still be in progress."
        )

    except AWSError as e:
        job_service.set_failed(db, job_id, str(e))
    except Exception as e:
        job_service.set_failed(db, job_id, f"Unexpected error during AMI copy: {e}")
    finally:
        db.close()
