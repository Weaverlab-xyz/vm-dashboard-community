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
import logging
import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
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
from ..services import aws_service, deploy_batch, job_service, cache_service, cloud_stats, region_catalog, workgroup_service
from ..services.aws_service import AWSError
from .auth import require_admin, require_permission

from pydantic import BaseModel

logger = logging.getLogger(__name__)

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


def _resolve_region(region: Optional[str]) -> str:
    """Resolve the effective AWS region for a request. Format validation is
    delegated to the shared region catalog; a blank/None region falls back to the
    configured default (``_aws_region()``); a malformed region is rejected with HTTP
    400 so a typo can't silently deploy into the default region."""
    if region is None or not region.strip():
        return _aws_region()
    r = region_catalog.normalize("aws", region)
    if not region_catalog.validate("aws", r):
        raise HTTPException(status_code=400, detail=f"Invalid AWS region '{region}'")
    return r

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
    region: Optional[str] = None,
    current_user: User = Depends(require_permission("aws", "read")),
):
    """Return key pairs, subnets, and security groups for the deploy form. Served from
    cache (10 min TTL). ``?region=`` scopes the lookup to a specific region (defaults to
    the configured ``aws_region``); the cache is keyed per region so regions never collide."""
    _region = _resolve_region(region)
    cache_key = cache_service.key_param("aws_network_opts", region=_region)
    ttl = cache_service.TTL["aws_network_opts"]

    async def _fetch():
        return await aws_service.get_network_options(_region)

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
    # Group instance IDs by the region recorded on their deploy job so each is
    # described in the region it actually lives in. Jobs from before multi-region
    # support carry no region and fall back to the configured default.
    default_region = _aws_region()
    active_jobs = []
    instance_ids = []
    ids_by_region: dict[str, list[str]] = {}
    for job in deploy_jobs:
        meta = job.metadata_dict
        if meta.get("destroyed"):
            continue
        iid = meta.get("instance_id")
        if iid:
            active_jobs.append(job)
            instance_ids.append(iid)
            ids_by_region.setdefault(meta.get("region") or default_region, []).append(iid)

    if not instance_ids:
        return []

    live_by_id = {}
    region_by_id = {}
    for region, ids in ids_by_region.items():
        for inst in await aws_service.describe_instances(region, ids):
            live_by_id[inst["instance_id"]] = inst
            region_by_id[inst["instance_id"]] = region
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
            "region": region_by_id.get(iid, default_region),
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
        accessible = _accessible_workgroups(current_user)
        out["instances"] = cloud_stats.summarize_instances(raw, accessible, "state")
        out["instances"]["by_region"] = cloud_stats.summarize_by_region(
            raw, accessible, "state", "region")
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

async def _fan_out_batch(
    req: DeployRequest, db: Session, current_user: User,
    *, region: str, workgroup: str,
) -> DeployResponse:
    """Fan a ``count > 1`` deploy out into one ``ec2_bulk_deploy`` parent plus N
    ``queued`` ``ec2_deploy`` children — the same parent type the multi-select bulk
    route already uses, with every child sharing the one AMI.

    Repeating the same ``ami_id`` across the children is the correct degenerate case
    for ``_BulkItem``: only ``instance_name`` varies, so the runner's existing
    reconstruction needs no change at all.

    A separate module-level function, NOT an ``if`` inside ``deploy_ami``:
    ``test_worker_dispatch``'s children-are-unclaimable rule walks the AST per
    function, so a ``children``-carrying create_job in the same function as the single
    deploy's ``pending`` one reads as a violation whatever the runtime branch does.
    """
    names = deploy_batch.expand_names(req.instance_name, req.count, "aws")
    deploy_batch.reject_name_collisions(db, "ec2_deploy", names)
    await deploy_batch.enforce_admission(
        "aws:ec2:deploy",
        requests=deploy_batch.batch_request_docs(
            {"region": region, "instance_type": req.instance_type, "image": req.ami_id},
            names),
        actor=current_user, db=db,
    )

    batch_id = uuid.uuid4().hex[:12]
    children = []
    for name in names:
        job = job_service.create_job(
            db,
            job_type="ec2_deploy",
            created_by=current_user.username,
            workgroup=workgroup,
            # `queued`, not `pending`: the runner claims on status='pending', so a
            # pending child would be launched a second time alongside its parent.
            status="queued",
            batch_id=batch_id,
            metadata={
                "ami_id": req.ami_id,
                "instance_name": name,
                "instance_type": req.instance_type,
                "region": region,
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
            details={"ami_id": req.ami_id, "instance_name": name,
                     "workgroup": workgroup, "bulk": True},
        )
        children.append({"job_id": job.id, "ami_id": req.ami_id, "instance_name": name})

    parent = job_service.create_job(
        db,
        job_type="ec2_bulk_deploy",
        created_by=current_user.username,
        workgroup=workgroup,
        batch_id=batch_id,
        metadata={
            "instance_type": req.instance_type,
            "region": region,
            "subnet_id": req.subnet_id,
            "security_group_ids": req.security_group_ids,
            "workgroup": workgroup,
            # The runner rebuilds the whole PRA call from the parent, so anything
            # omitted here is silently skipped rather than failing loudly.
            "jump_group": req.jump_group,
            "jumpoint_name": req.jumpoint_name,
            "pra_credential_ref": req.pra_credential_ref,
            "children": children,
        },
    )
    return DeployResponse(
        job_id=parent.id,
        status="pending",
        message=f"EC2 deployment queued for {len(names)} instances ({names[0]} … {names[-1]})",
        count=len(names),
        batch_id=batch_id,
        job_ids=[c["job_id"] for c in children],
        names=names,
    )


@router.post("/deploy", response_model=DeployResponse)
async def deploy_ami(
    req: DeployRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("aws", "write")),
):
    """
    Launch one or more EC2 instances from an AMI using the AWS API.
    Returns a job_id trackable at /api/jobs/{job_id} or /api/ws/jobs/{job_id}.
    ``count > 1`` fans out into a batch (see ``_fan_out_batch``).
    """
    workgroup = _validate_workgroup(db, current_user, req.workgroup)
    region = _resolve_region(req.region)
    await _validate_ssh_key_override(req.ssh_key_secret_override)

    if req.count > 1:
        return await _fan_out_batch(req, db, current_user,
                                    region=region, workgroup=workgroup)

    # Pre-action policy gate (inert unless enabled + this action is gated).
    from ..services import admission_service
    admission_service.enforce(
        "aws:ec2:deploy",
        request={"region": region, "instance_type": req.instance_type,
                 "image": req.ami_id, "name": req.instance_name,
                 "count": 1, "batch": False},
        actor=current_user, db=db,
    )

    job = job_service.create_job(
        db,
        job_type="ec2_deploy",
        created_by=current_user.username,
        workgroup=workgroup,
        metadata={
            "ami_id": req.ami_id,
            "instance_name": req.instance_name,
            "instance_type": req.instance_type,
            "region": region,
            "subnet_id": req.subnet_id,
            "security_group_ids": req.security_group_ids,
            "workgroup": workgroup,
            "register_in_entitle": req.register_in_entitle,
            "register_in_passwordsafe": req.register_in_passwordsafe,
            "ssh_key_secret_override": req.ssh_key_secret_override,
            # PRA jump-group fields: the runner rebuilds the whole call from this
            # metadata, so anything omitted here is silently skipped at deploy time
            # rather than failing loudly. All three are *references*, not secrets.
            "jump_group": req.jump_group,
            "jumpoint_name": req.jumpoint_name,
            "pra_credential_ref": req.pra_credential_ref,
        },
    )

    job_service.log_audit(
        db, current_user.username, "ec2_deploy",
        details={"ami_id": req.ami_id, "instance_name": req.instance_name, "workgroup": workgroup},
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
    region = _resolve_region(req.region)
    await _validate_ssh_key_override(req.ssh_key_secret_override)

    # Names here are hand-typed per row in the bulk modal, so unlike the count path
    # they can genuinely repeat. Checked before anything is created.
    deploy_batch.reject_name_collisions(
        db, "ec2_deploy", [i.instance_name for i in req.items])

    # Policy gate every item BEFORE the first create_job. This route had no gate at
    # all, so allowed_regions / instance_size_caps / prod_window silently did not
    # apply to batches. Per item because each carries its own AMI.
    await deploy_batch.enforce_admission(
        "aws:ec2:deploy",
        requests=[{"region": region, "instance_type": req.instance_type,
                   "image": item.ami_id, "name": item.instance_name,
                   "count": len(req.items), "batch": True}
                  for item in req.items],
        actor=current_user, db=db,
    )

    # Create one job per instance up front so callers get all job IDs immediately.
    #
    # These are created ``queued``, not ``pending``: the parent ec2_bulk_deploy job
    # below drives them, and the runner claims on `status='pending' AND job_type IN
    # HANDLED_TYPES`. Left pending they would be claimed and deployed a second time,
    # concurrently with the parent — every instance launched twice.
    batch_id = uuid.uuid4().hex[:12]
    job_items: list[tuple[str, object]] = []
    for item in req.items:
        job = job_service.create_job(
            db,
            job_type="ec2_deploy",
            created_by=current_user.username,
            workgroup=workgroup,
            status="queued",
            batch_id=batch_id,
            metadata={
                "ami_id": item.ami_id,
                "instance_name": item.instance_name,
                "instance_type": req.instance_type,
                "region": region,
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

    # One parent job for the whole batch — the runner claims this, and it drives the
    # queued children above, sharing a single ECS Jumpoint container across them.
    job_service.create_job(
        db,
        job_type="ec2_bulk_deploy",
        created_by=current_user.username,
        workgroup=workgroup,
        batch_id=batch_id,
        metadata={
            "instance_type": req.instance_type,
            "region": region,
            "subnet_id": req.subnet_id,
            "security_group_ids": req.security_group_ids,
            "workgroup": workgroup,
            # The runner rebuilds the whole PRA call from the parent, so anything
            # omitted here is silently skipped rather than failing loudly — which is
            # exactly what used to happen to these three.
            "jump_group": req.jump_group,
            "jumpoint_name": req.jumpoint_name,
            "pra_credential_ref": req.pra_credential_ref,
            "children": [
                {"job_id": job_id, "ami_id": item.ami_id,
                 "instance_name": item.instance_name}
                for job_id, item in job_items
            ],
        },
    )

    results = [
        BulkDeployJobResult(
            ami_id=item.ami_id,
            instance_name=item.instance_name,
            job_id=job_id,
            status="queued",
        )
        for job_id, item in job_items
    ]
    return BulkDeployResponse(jobs=results, count=len(results), batch_id=batch_id)


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

    # Terminate in the region the instance was deployed into (fall back to default
    # for instances deployed before multi-region support recorded a region).
    region = deploy_job.metadata_dict.get("region") or _aws_region()

    destroy_job = job_service.create_job(
        db,
        job_type="ec2_destroy",
        created_by=current_user.username,
        metadata={
            "instance_id": instance_id,
            "deploy_job_id": deploy_job.id,
            "region": region,
        },
    )

    job_service.log_audit(
        db, current_user.username, "ec2_destroy",
        details={"instance_id": instance_id},
    )

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
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("aws", "write")),
):
    """Manually export an existing AMI to VHD on the hub backend and register
    it in the image registry. Useful when the auto-export in the Packer build
    flow was skipped or failed but the AMI itself is fine."""
    job = job_service.create_job(
        db,
        job_type="aws_export_image",
        created_by=current_user.username,
        metadata={"ami_id": ami_id, "image_name": req.image_name,
                  "region": _aws_region(), "created_by": current_user.username},
    )
    job_service.log_audit(
        db, current_user.username, "aws_export_image",
        details={"ami_id": ami_id, "image_name": req.image_name},
    )

    # Enqueued as a pending job; the worker container claims + runs it
    # (survives gunicorn worker recycling, unlike an in-app BackgroundTask).
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
