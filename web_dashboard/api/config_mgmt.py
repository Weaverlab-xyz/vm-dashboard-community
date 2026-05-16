"""
Config Management API — Ansible playbook / asset runner (local Docker path).

All endpoints require authentication.  Runs are dispatched as background jobs;
the client gets a job_id immediately and can poll /api/jobs/{id} for progress
and final output.

Asset types supported:
    .yml / .yaml  — Ansible playbooks (run as-is)
    .sh           — Bash scripts (auto-wrapped in a generated playbook)
    .rpm          — RPM packages   (auto-wrapped: copy + dnf install)
    .deb          — DEB packages   (auto-wrapped: copy + apt install)

Target types:
    On-premises group key  — "proxmox", "vsphere", "hyperv", "nutanix", "xcpng"
    Bare IP / hostname     — ad-hoc; cloud field determines SSH key source
"""
import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import Job, get_db
from ..auth import get_current_user
from ..models.user import User
from ..services import job_service
from ..services import storage_service
from ..services.storage_service import StorageError
from ..services import ansible_local_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/config-mgmt", tags=["config-mgmt"])


# ── Asset / playbook listing ───────────────────────────────────────────────────

@router.get("/assets")
async def list_assets(current_user: User = Depends(get_current_user)):
    """List all available assets (.yml, .sh, .deb, .rpm) from configured storage."""
    try:
        return await storage_service.list_assets()
    except StorageError as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/playbooks")
async def list_playbooks(current_user: User = Depends(get_current_user)):
    """List playbook names (.yml/.yaml) from configured storage — back-compat alias."""
    try:
        return await storage_service.list_playbooks()
    except StorageError as e:
        raise HTTPException(status_code=502, detail=str(e))


class UploadAssetRequest(BaseModel):
    filename: str
    content_b64: str


@router.post("/upload", status_code=201)
async def upload_asset(
    req: UploadAssetRequest,
    current_user: User = Depends(get_current_user),
):
    """Upload a playbook (.yml/.yaml), shell script (.sh), or package (.rpm/.deb) to storage."""
    import base64
    try:
        data = base64.b64decode(req.content_b64)
    except Exception:
        raise HTTPException(status_code=400, detail="content_b64 is not valid base64.")
    try:
        await storage_service.upload_asset(req.filename, data)
    except StorageError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"ok": True, "filename": req.filename, "size": len(data)}


# ── Inventory ─────────────────────────────────────────────────────────────────

@router.get("/inventory")
async def get_inventory(current_user: User = Depends(get_current_user)):
    """
    Return the dynamic Ansible inventory.

    Only on-premises hypervisors that are both enabled (feature flag) and have
    a host address configured appear.  The response includes:
      targets   — simplified list for the UI target picker
      inventory — full Ansible JSON inventory (groups + hostvars)
    """
    return {
        "targets":   ansible_local_service.get_configured_targets(),
        "inventory": ansible_local_service.build_inventory(),
    }


# ── Cloud targets ─────────────────────────────────────────────────────────────

@router.get("/cloud-targets")
async def get_cloud_targets(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Return cloud VM targets (EC2 + Azure VMs + GCE instances) with IPs for the
    Config Mgmt page's target picker.

    Source of truth is the ``jobs`` table: every successful cloud deploy lands
    a completed Job whose ``metadata_dict`` carries ``instance_id``/``vm_name``,
    ``private_ip``, and ``public_ip``. We enumerate those directly instead of
    relying on the cache populated by the cloud tabs — the cache may be empty
    on a freshly-restarted server, after a cache-invalidation following a
    deploy, or when the user has never opened the relevant cloud tab. Previously
    those cases left this endpoint returning empty lists even though the
    instances clearly existed (issue #12).

    Destroyed instances are excluded (``metadata_dict['destroyed'] == True``
    after the destroy job runs).

    Response shape:
        {
          "aws":   [{name, ip, instance_id}, ...],
          "azure": [{name, ip}, ...],
          "gcp":   [{name, ip, zone}, ...],
        }
    """
    targets: dict = {"aws": [], "azure": [], "gcp": []}

    # Pull completed deploys for all three clouds in one trip.
    deploy_jobs = (
        db.query(Job)
        .filter(
            Job.job_type.in_(("ec2_deploy", "azure_deploy", "gce_deploy")),
            Job.status == "completed",
        )
        .order_by(Job.created_at.desc())
        .all()
    )

    for job in deploy_jobs:
        meta = job.metadata_dict
        if meta.get("destroyed"):
            continue
        ip = meta.get("public_ip") or meta.get("private_ip")
        if not ip:
            continue

        if job.job_type == "ec2_deploy":
            iid = meta.get("instance_id")
            name = meta.get("instance_name") or iid or ""
            targets["aws"].append({"name": name, "ip": ip, "instance_id": iid})
        elif job.job_type == "azure_deploy":
            targets["azure"].append({"name": meta.get("vm_name", ""), "ip": ip})
        elif job.job_type == "gce_deploy":
            targets["gcp"].append({
                "name": meta.get("instance_name", ""),
                "ip": ip,
                "zone": meta.get("zone", ""),
            })

    # Per-cloud default SSH user — surfaced as a *suggestion* the run-asset
    # form pre-fills when the operator picks a cloud target. Not a secret;
    # logged-in user is sufficient auth.
    default_user = _cfg("ansible_default_user") or "ec2-user"
    return {
        **targets,
        "default_users": {
            "aws":   _cfg("ansible_aws_user")   or default_user,
            "azure": _cfg("ansible_azure_user") or default_user,
            "gcp":   _cfg("ansible_gcp_user")   or default_user,
        },
    }


# ── Playbook / asset run ───────────────────────────────────────────────────────

class RunRequest(BaseModel):
    asset: str           # filename of any supported type (.yml, .sh, .deb, .rpm)
    target: str          # on-prem group key OR bare IP/hostname for cloud/ad-hoc
    cloud: str = ""      # "" | "aws" | "azure" | "gcp" — drives SSH key retrieval
    ansible_user: str = ""  # SSH user for cloud runner targets; falls back to ansible_default_user
    extra_vars: dict = {}


def _cfg(key: str) -> str:
    return ansible_local_service._cfg(key)


async def _run_job(
    job_id: str,
    asset: str,
    target: str,
    cloud: str,
    ansible_user: str,
    extra_vars: dict,
) -> None:
    import base64
    from ..database import SessionLocal
    db = SessionLocal()
    try:
        job_service.update_progress(db, job_id, 5, f"Fetching asset '{asset}'…")
        try:
            asset_b64 = await storage_service.fetch_asset_b64(asset)
        except StorageError as e:
            job_service.set_failed(db, job_id, f"Asset storage error: {e}")
            return

        runner = _cfg("ansible_runner") or "local"
        is_adhoc = "." in target or ":" in target
        is_playbook = ansible_local_service.asset_type(asset) == "playbook"

        # Cloud runners only support bare-IP targets and .yml playbooks.
        # Fall back to local for group targets or non-playbook assets.
        if runner != "local" and is_adhoc and is_playbook:
            key_cloud = cloud or runner  # "ecs"→"aws", "aci"→"azure", etc.
            if runner == "ecs":
                key_cloud = "aws"
            elif runner == "aci":
                key_cloud = "azure"
            elif runner == "gcp":
                key_cloud = "gcp"

            # SSH user: explicit ansible_user from the run request wins,
            # else the per-cloud config key, else the global fallback.
            cloud_user_keys = {
                "aws":   "ansible_aws_user",
                "azure": "ansible_azure_user",
                "gcp":   "ansible_gcp_user",
            }
            cloud_default = {
                "aws":   "ec2-user",
                "azure": "azureuser",
                "gcp":   "gcp-user",
            }.get(key_cloud, "ec2-user")
            resolved_user = (
                ansible_user
                or _cfg(cloud_user_keys.get(key_cloud, ""))
                or _cfg("ansible_default_user")
                or cloud_default
            )

            job_service.update_progress(db, job_id, 10, f"Retrieving SSH key for {key_cloud.upper()}…")
            ssh_key_pem: str | None = None
            try:
                ssh_key_pem = await ansible_local_service.fetch_ssh_key(key_cloud)
            except Exception as exc:
                logger.warning("SSH key retrieval failed (%s) — proceeding without key: %s", key_cloud, exc)

            ssh_key_b64 = base64.b64encode(ssh_key_pem.encode()).decode() if ssh_key_pem else ""

            job_service.update_progress(db, job_id, 20, f"Launching {runner.upper()} runner for {asset}…")
            exit_code, output = await _dispatch_cloud_runner(
                runner=runner,
                target_ip=target,
                ansible_user=resolved_user,
                playbook_b64=asset_b64,
                ssh_key_b64=ssh_key_b64,
                job_id=job_id,
            )

            if exit_code == 0:
                job_service.set_completed(db, job_id, {"output": output, "returncode": exit_code})
            else:
                job_service.set_failed(db, job_id, f"ansible-playbook exited {exit_code}:\n{output}")
            return

        # ── Local Docker runner (original path) ───────────────────────────────
        if runner != "local" and not is_adhoc:
            logger.debug("ansible_runner=%s ignored for group target %r — using local runner", runner, target)
        if runner != "local" and not is_playbook:
            logger.debug("ansible_runner=%s ignored for non-playbook asset %r — using local runner", runner, asset)

        ssh_key_pem = None
        if cloud in ("aws", "gcp", "azure"):
            job_service.update_progress(db, job_id, 10, f"Retrieving SSH key for {cloud.upper()}…")
            try:
                ssh_key_pem = await ansible_local_service.fetch_ssh_key(cloud)
                if not ssh_key_pem:
                    logger.warning(
                        "No SSH key configured for %s — proceeding without key",
                        cloud,
                    )
            except Exception as exc:
                logger.warning("Failed to retrieve SSH key for %s: %s — proceeding without key", cloud, exc)

        job_service.update_progress(db, job_id, 20, f"Running {asset} against {target}…")
        output, rc = await ansible_local_service.run_playbook(
            asset_b64=asset_b64,
            target=target,
            extra_vars=extra_vars or None,
            asset_name=asset,
            ssh_key_pem=ssh_key_pem,
        )

        if rc == 0:
            job_service.set_completed(db, job_id, {"output": output, "returncode": rc})
        else:
            job_service.set_failed(db, job_id, f"ansible-playbook exited {rc}:\n{output}")
    except Exception as e:
        logger.exception("ansible job %s failed: %s", job_id, e)
        job_service.set_failed(db, job_id, str(e))
    finally:
        db.close()


async def _dispatch_cloud_runner(
    runner: str,
    target_ip: str,
    ansible_user: str,
    playbook_b64: str,
    ssh_key_b64: str,
    job_id: str,
) -> tuple:
    """Route to the configured cloud Ansible runner. Returns (exit_code, output)."""
    if runner == "ecs":
        from ..services import aws_service
        region = _cfg("aws_region") or "us-east-1"
        sg_raw = _cfg("ansible_ecs_security_group_ids") or ""
        sg_ids = [s.strip() for s in sg_raw.split(",") if s.strip()]
        return await aws_service.run_ecs_ansible_task(
            region=region,
            cluster=_cfg("ansible_ecs_cluster") or "bt-jumpoint",
            task_family=_cfg("ansible_ecs_task_family") or "ansible-config-mgmt",
            image=_cfg("ansible_ecs_image") or "willhallonline/ansible:latest",
            cpu=_cfg("ansible_ecs_cpu") or "256",
            memory=_cfg("ansible_ecs_memory") or "512",
            subnet_id=_cfg("ansible_ecs_subnet_id") or "",
            security_group_ids=sg_ids,
            execution_role_arn=_cfg("ansible_ecs_execution_role_arn") or "",
            target_ip=target_ip,
            ansible_user=ansible_user,
            playbook_b64=playbook_b64,
            ssh_key_b64=ssh_key_b64,
            job_id=job_id,
        )

    if runner == "aci":
        from ..services import azure_service
        from ..services import config_service as cs
        from ..config import settings
        rg = cs.get("azure_resource_group") or settings.azure_resource_group
        location = cs.get("azure_location") or settings.azure_location
        return await azure_service.run_aci_ansible_task(
            rg=rg,
            location=location,
            subnet_id=_cfg("ansible_aci_subnet_id") or "",
            image=_cfg("ansible_aci_image") or "willhallonline/ansible:latest",
            target_ip=target_ip,
            ansible_user=ansible_user,
            playbook_b64=playbook_b64,
            ssh_key_b64=ssh_key_b64,
            job_id=job_id,
            acr_server=_cfg("ansible_aci_acr_server") or "",
            acr_username=_cfg("ansible_aci_acr_username") or "",
            acr_password=_cfg("ansible_aci_acr_password") or "",
        )

    if runner == "gcp":
        from ..services import gcp_service
        region = _cfg("gcp_ansible_cloud_run_region") or _cfg("gcp_region") or ""
        return await gcp_service.run_cloud_run_ansible_task(
            project_id=_cfg("gcp_project_id"),
            region=region,
            image=_cfg("gcp_ansible_image") or "willhallonline/ansible:latest",
            target_ip=target_ip,
            ansible_user=ansible_user,
            playbook_b64=playbook_b64,
            ssh_key_b64=ssh_key_b64,
            job_id=job_id,
            vpc_connector=_cfg("gcp_ansible_vpc_connector") or "",
        )

    raise ValueError(f"Unknown ansible_runner: {runner!r}")


@router.post("/run")
async def run_playbook(
    payload: RunRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Run an asset against a target as a background job.

    target must be one of the configured hypervisor group keys returned by
    /api/config-mgmt/inventory, or a bare IP / hostname for ad-hoc cloud runs.
    For cloud targets, set cloud="aws"|"azure"|"gcp" to enable SSH key retrieval.
    """
    targets = ansible_local_service.get_configured_targets()
    valid_keys = {t["key"] for t in targets}

    # Bare IP/hostname targets (contain a dot or colon) are allowed ad-hoc.
    is_adhoc = "." in payload.target or ":" in payload.target
    if not is_adhoc and payload.target not in valid_keys:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Target '{payload.target}' is not a configured hypervisor. "
                f"Configured: {sorted(valid_keys) or '(none — enable integrations in Settings)'}."
            ),
        )

    atype = ansible_local_service.asset_type(payload.asset)
    description = f"Ansible ({atype}): {payload.asset} → {payload.target}"

    job = job_service.create_job(
        db,
        job_type="ansible_local",
        description=description,
        workgroup="ansible",
        owner_id=current_user.id,
    )
    background_tasks.add_task(
        _run_job, job.id, payload.asset, payload.target, payload.cloud,
        payload.ansible_user, payload.extra_vars,
    )
    return {"job_id": job.id, "status": "queued"}
