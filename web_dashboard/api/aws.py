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
from ..services import aws_service, job_service, cache_service
from ..services.aws_service import AWSError
from .auth import get_current_user, require_permission

router = APIRouter(prefix="/api/aws", tags=["aws"])


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

@router.get("/instances", response_model=EC2InstanceListResponse)
async def list_instances(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("aws", "read")),
):
    """
    List dashboard-deployed EC2 instances. Served from cache (1 min TTL).
    Queries jobs DB for completed ec2_deploy jobs, then fetches live state from AWS.
    """
    cache_key = cache_service.key_global("aws_instances")
    ttl = cache_service.TTL["aws_instances"]

    async def _fetch():
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
            job_meta = job.metadata_dict if job else {}
            result.append({
                **live,
                # key_name comes from live EC2 describe response (populated for old key-pair
                # deployments; None for new Secrets Manager deployments — hides the SSH Key button).
                "key_name": live.get("key_name"),
                "job_id": job.id if job else None,
                "deployed_by": job.created_by if job else None,
            })
        return result

    try:
        raw, cached_at = await cache_service.get_or_refresh(cache_key, ttl, _fetch)
        instances = [EC2InstanceInfo(**i) for i in raw]
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
    job = job_service.create_job(
        db,
        job_type="ec2_deploy",
        created_by=current_user.username,
        metadata={
            "ami_id": req.ami_id,
            "instance_name": req.instance_name,
            "instance_type": req.instance_type,
            "subnet_id": req.subnet_id,
            "security_group_ids": req.security_group_ids,
        },
    )

    job_service.log_audit(
        db, current_user.username, "ec2_deploy",
        details={"ami_id": req.ami_id, "instance_name": req.instance_name},
    )

    background_tasks.add_task(
        _run_deploy,
        job.id,
        req.ami_id,
        req.instance_name,
        req.instance_type,
        req.subnet_id,
        req.security_group_ids,
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

    # Create one job per instance up front so callers get all job IDs immediately
    job_items: list[tuple[str, object]] = []
    for item in req.items:
        job = job_service.create_job(
            db,
            job_type="ec2_deploy",
            created_by=current_user.username,
            metadata={
                "ami_id": item.ami_id,
                "instance_name": item.instance_name,
                "instance_type": req.instance_type,
                "subnet_id": req.subnet_id,
                "security_group_ids": req.security_group_ids,
                "bulk": True,
            },
        )
        job_service.log_audit(
            db, current_user.username, "ec2_deploy",
            details={"ami_id": item.ami_id, "instance_name": item.instance_name, "bulk": True},
        )
        job_items.append((job.id, item))

    # One background task for the whole batch — shares a single ECS container
    background_tasks.add_task(
        _run_bulk_deploy,
        job_items,
        req.instance_type,
        req.subnet_id,
        req.security_group_ids,
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


async def _run_deploy(
    job_id: str,
    ami_id: str,
    instance_name: str,
    instance_type: str,
    subnet_id: str,
    security_group_ids: list,
):
    db = _get_db_session()
    result = {}
    try:
        job_service.set_running(db, job_id)

        # ── Step 1: Start ECS Jumpoint container first (BeyondTrust only) ─────
        from ..services import config_service as _cfg_svc
        _aws_region = _aws_cfg("aws_region") or "us-east-2"
        ssh_secret_name = _cfg_svc.get("ec2_ssh_key_secret") or ""
        if _cfg_svc.get_bool("beyondtrust_enabled"):
            from ..services import btapi_service
            job_service.update_progress(db, job_id, 15, "Starting BeyondTrust Jumpoint container…")
            try:
                deploy_key = await btapi_service.get_ps_secret(settings.bt_ps_deploy_key_title)
                ecs_task_arn = await aws_service.run_ecs_jumpoint_task(
                    region=_aws_region,
                    cluster=settings.bt_ecs_cluster,
                    task_family=settings.bt_ecs_task_family,
                    subnet_id=subnet_id,
                    security_group_ids=security_group_ids,
                    deploy_key=deploy_key,
                    cpu=settings.bt_ecs_cpu,
                    memory=settings.bt_ecs_memory,
                    execution_role_arn=settings.bt_ecs_execution_role_arn,
                    image=settings.bt_ecs_image,
                )
                result["ecs_task_arn"] = ecs_task_arn
                job_service.update_progress(
                    db, job_id, 35,
                    f"Jumpoint container started ({ecs_task_arn.split('/')[-1]}), launching EC2 instance…"
                )
            except Exception as e:
                result["ecs_error"] = str(e)
                job_service.update_progress(
                    db, job_id, 35,
                    f"Jumpoint ECS task failed (non-fatal): {e} — continuing with EC2 launch…"
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
            os_type, _ = detect_os_type(ami_info.get("name", ""))
            key_detail = await aws_service.get_ssh_public_key_from_secret(_aws_region, ssh_secret_name)
            public_key = key_detail["public_key"]
            result["ssh_secret_name"] = ssh_secret_name

        # ── Step 3: Launch EC2 instance ────────────────────────────────────────
        job_service.update_progress(db, job_id, 40, f"Launching EC2 instance ({os_type})…")
        try:
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
            )
            result.update(instance_result)
        except AWSError as e:
            # EC2 failed — stop ECS task if it was started
            if result.get("ecs_task_arn"):
                try:
                    await aws_service.stop_ecs_jumpoint_task(
                        _aws_region, settings.bt_ecs_cluster, result["ecs_task_arn"]
                    )
                except Exception:
                    pass
            raise

        instance_id = result["instance_id"]
        hostname = result.get("private_ip") or result.get("public_ip") or instance_id
        job_service.update_progress(
            db, job_id, 70,
            f"Instance {instance_id} launched ({hostname}), provisioning Shell Jump…"
        )

        # ── Step 3: BeyondTrust PRA — Shell Jump + policy (optional) ──────────
        if _cfg_svc.get_bool("beyondtrust_enabled"):
            from ..services import btapi_service
            try:
                bt_result = await btapi_service.provision_ec2_jump(
                    instance_name=instance_name,
                    hostname=hostname,
                    jump_group_name=settings.bt_jump_group_name,
                    group_policy_name=settings.bt_group_policy_name,
                    jumpoint_id=settings.bt_jumpoint_id,
                )
                result["bt_shell_jump_id"] = bt_result.get("shell_jump_id")
                result["bt_jump_group_id"] = bt_result.get("jump_group_id")
                job_service.update_progress(
                    db, job_id, 90,
                    f"Shell Jump created (ID: {bt_result.get('shell_jump_id')}, "
                    f"group: {settings.bt_jump_group_name})"
                )
            except Exception as e:
                result["bt_error"] = str(e)
                job_service.update_progress(
                    db, job_id, 90,
                    f"Instance deployed but Shell Jump provisioning failed: {e}"
                )
        else:
            job_service.update_progress(db, job_id, 90, "Instance deployed.")

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
):
    """
    Background task for bulk EC2 deployment.
    Starts ONE ECS Jumpoint container for the entire batch, then deploys each
    instance sequentially. All instance jobs share the same ecs_task_arn so
    the container stays alive until the last instance in the batch is terminated.
    """
    db = _get_db_session()
    ecs_task_arn = None
    try:
        # Mark all jobs running
        for job_id, _ in job_items:
            job_service.set_running(db, job_id)

        # Step 1: Start ONE ECS Jumpoint container for the whole batch (BT only)
        from ..services import config_service as _cfg_svc
        _aws_region = _aws_cfg("aws_region") or "us-east-2"
        ssh_secret_name = _cfg_svc.get("ec2_ssh_key_secret") or ""
        first_job_id = job_items[0][0]
        ecs_error = None
        if _cfg_svc.get_bool("beyondtrust_enabled"):
            from ..services import btapi_service
            job_service.update_progress(
                db, first_job_id, 10,
                f"Starting BeyondTrust Jumpoint container for {len(job_items)}-instance batch…"
            )
            try:
                deploy_key = await btapi_service.get_ps_secret(settings.bt_ps_deploy_key_title)
                ecs_task_arn = await aws_service.run_ecs_jumpoint_task(
                    region=_aws_region,
                    cluster=settings.bt_ecs_cluster,
                    task_family=settings.bt_ecs_task_family,
                    subnet_id=subnet_id,
                    security_group_ids=security_group_ids,
                    deploy_key=deploy_key,
                    cpu=settings.bt_ecs_cpu,
                    memory=settings.bt_ecs_memory,
                    execution_role_arn=settings.bt_ecs_execution_role_arn,
                    image=settings.bt_ecs_image,
                )
            except Exception as e:
                ecs_error = str(e)
                ecs_task_arn = None
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
            if ecs_task_arn:
                result["ecs_task_arn"] = ecs_task_arn
            elif ecs_error:
                result["ecs_error"] = ecs_error

            try:
                job_service.update_progress(
                    db, job_id, 40, f"Launching EC2 instance {item.instance_name}…"
                )
                ami_info = await aws_service.describe_ami(_aws_region, item.ami_id)
                is_windows = "windows" in (ami_info.get("platform", "") or "").lower()
                instance_result = await aws_service.launch_instance(
                    region=_aws_region,
                    ami_id=item.ami_id,
                    instance_name=item.instance_name,
                    instance_type=instance_type,
                    public_key="" if is_windows else shared_public_key,
                    subnet_id=subnet_id,
                    security_group_ids=security_group_ids,
                    iam_instance_profile=_cfg_svc.get("ec2_ssm_instance_profile") or "",
                )
                result.update(instance_result)

                instance_id = result["instance_id"]
                hostname = result.get("private_ip") or result.get("public_ip") or instance_id
                job_service.update_progress(
                    db, job_id, 70,
                    f"Instance {instance_id} launched ({hostname}), provisioning Shell Jump…"
                )

                # Step 3: BeyondTrust PRA — Shell Jump per instance (optional)
                if _cfg_svc.get_bool("beyondtrust_enabled"):
                    from ..services import btapi_service
                    try:
                        bt_result = await btapi_service.provision_ec2_jump(
                            instance_name=item.instance_name,
                            hostname=hostname,
                            jump_group_name=settings.bt_jump_group_name,
                            group_policy_name=settings.bt_group_policy_name,
                            jumpoint_id=settings.bt_jumpoint_id,
                        )
                        result["bt_shell_jump_id"] = bt_result.get("shell_jump_id")
                        result["bt_jump_group_id"] = bt_result.get("jump_group_id")
                        job_service.update_progress(
                            db, job_id, 90,
                            f"Shell Jump created (ID: {bt_result.get('shell_jump_id')}, "
                            f"group: {settings.bt_jump_group_name})"
                        )
                    except Exception as e:
                        result["bt_error"] = str(e)
                        job_service.update_progress(
                            db, job_id, 90,
                            f"Instance deployed but Shell Jump provisioning failed: {e}"
                        )
                else:
                    job_service.update_progress(db, job_id, 90, "Instance deployed.")

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

        result = await aws_service.terminate_instance(_aws_region(), instance_id)

        deploy_job = job_service.get_job(db, deploy_job_id)
        if deploy_job:
            meta = deploy_job.metadata_dict

            # Stop ECS Jumpoint task — only if no other active instances share it
            ecs_task_arn = meta.get("ecs_task_arn")
            if ecs_task_arn:
                sibling_count = sum(
                    1 for j in db.query(Job)
                    .filter(Job.job_type == "ec2_deploy", Job.status == "completed")
                    .all()
                    if j.id != deploy_job_id
                    and j.metadata_dict.get("ecs_task_arn") == ecs_task_arn
                    and not j.metadata_dict.get("destroyed")
                )
                if sibling_count == 0:
                    job_service.update_progress(
                        db, destroy_job_id, 40,
                        "Instance terminating, stopping Jumpoint ECS task…"
                    )
                    try:
                        await aws_service.stop_ecs_jumpoint_task(
                            _aws_region(), settings.bt_ecs_cluster, ecs_task_arn
                        )
                        result["ecs_task_stopped"] = ecs_task_arn
                    except AWSError as e:
                        result["ecs_error"] = f"ECS task stop failed: {e}"
                else:
                    job_service.update_progress(
                        db, destroy_job_id, 40,
                        f"Jumpoint ECS task shared with {sibling_count} other active instance(s) — leaving running…"
                    )
                    result["ecs_task_shared"] = ecs_task_arn

            # Remove BeyondTrust Shell Jump (only if BT is enabled — a deploy
            # that recorded a bt_shell_jump_id must have run with BT on; if
            # the flag has since flipped off, leave the jump in place rather
            # than crash on a missing import).
            bt_shell_jump_id = meta.get("bt_shell_jump_id")
            if bt_shell_jump_id and settings.beyondtrust_enabled:
                from ..services import btapi_service
                job_service.update_progress(
                    db, destroy_job_id, 70,
                    f"Removing BeyondTrust Shell Jump {bt_shell_jump_id}…"
                )
                try:
                    await btapi_service.remove_ec2_jump(int(bt_shell_jump_id))
                    result["bt_shell_jump_removed"] = bt_shell_jump_id
                except Exception as e:
                    result["bt_error"] = f"Shell Jump removal failed: {e}"

            # Mark original deploy job as destroyed
            meta["destroyed"] = True
            job_service.set_completed(db, deploy_job_id, meta)

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
