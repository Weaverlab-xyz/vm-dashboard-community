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

from ..database import get_db
from ..auth import get_current_user
from ..models.user import User
from ..services import job_service
from ..services import ansible_storage
from ..services.ansible_storage import AnsibleStorageError
from ..services import ansible_local_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/config-mgmt", tags=["config-mgmt"])


# ── Asset / playbook listing ───────────────────────────────────────────────────

@router.get("/assets")
async def list_assets(current_user: User = Depends(get_current_user)):
    """List all available assets (.yml, .sh, .deb, .rpm) from configured storage."""
    try:
        return await ansible_storage.list_assets()
    except AnsibleStorageError as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/playbooks")
async def list_playbooks(current_user: User = Depends(get_current_user)):
    """List playbook names (.yml/.yaml) from configured storage — back-compat alias."""
    try:
        return await ansible_storage.list_playbooks()
    except AnsibleStorageError as e:
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
        await ansible_storage.upload_asset(req.filename, data)
    except AnsibleStorageError as e:
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
async def get_cloud_targets(current_user: User = Depends(get_current_user)):
    """
    Return running cloud VM targets (EC2 + Azure VMs + GCE instances) with IPs.

    Reads from the in-memory cache populated by the cloud tabs — no extra API
    calls are made here.  Returns empty lists for any cloud that is not
    configured or has no deployed instances in the cache.

    Response shape:
        {
          "aws":   [{name, ip, instance_id}, ...],
          "azure": [{name, ip}, ...],
          "gcp":   [{name, ip, zone}, ...],
        }
    """
    from ..services import cache_service

    targets: dict = {"aws": [], "azure": [], "gcp": []}

    # AWS EC2 — populated by the AWS tab warmer
    try:
        cached = await cache_service.get(cache_service.key_global("aws_instances"))
        if cached:
            for inst in (cached.get("data") or []):
                if inst.get("state") == "running":
                    ip = inst.get("public_ip") or inst.get("private_ip")
                    if ip:
                        targets["aws"].append({
                            "name": inst.get("name") or inst.get("instance_id", ""),
                            "ip": ip,
                            "instance_id": inst.get("instance_id"),
                        })
    except Exception as exc:
        logger.debug("cloud-targets: AWS cache read failed: %s", exc)

    # Azure VMs — populated by the Azure tab warmer
    try:
        cached = await cache_service.get(cache_service.key_global("azure_vms"))
        if cached:
            for vm in (cached.get("data") or []):
                ip = vm.get("public_ip") or vm.get("private_ip")
                if ip:
                    targets["azure"].append({
                        "name": vm.get("name", ""),
                        "ip": ip,
                    })
    except Exception as exc:
        logger.debug("cloud-targets: Azure cache read failed: %s", exc)

    # GCE Instances — populated by the GCP tab warmer
    try:
        cached = await cache_service.get(cache_service.key_global("gcp_instances"))
        if cached:
            for inst in (cached.get("data") or []):
                if inst.get("status") == "RUNNING":
                    ip = inst.get("public_ip") or inst.get("private_ip")
                    if ip:
                        targets["gcp"].append({
                            "name": inst.get("instance_name", ""),
                            "ip": ip,
                            "zone": inst.get("zone", ""),
                        })
    except Exception as exc:
        logger.debug("cloud-targets: GCP cache read failed: %s", exc)

    return targets


# ── Playbook / asset run ───────────────────────────────────────────────────────

class RunRequest(BaseModel):
    asset: str           # filename of any supported type (.yml, .sh, .deb, .rpm)
    target: str          # on-prem group key OR bare IP/hostname for cloud/ad-hoc
    cloud: str = ""      # "" | "aws" | "azure" | "gcp" — drives SSH key retrieval
    extra_vars: dict = {}


async def _run_job(
    job_id: str,
    asset: str,
    target: str,
    cloud: str,
    extra_vars: dict,
) -> None:
    from ..database import SessionLocal
    db = SessionLocal()
    try:
        job_service.update_progress(db, job_id, 5, f"Fetching asset '{asset}'…")
        try:
            asset_b64 = await ansible_storage.fetch_asset_b64(asset)
        except AnsibleStorageError as e:
            job_service.set_failed(db, job_id, f"Asset storage error: {e}")
            return

        # Fetch cloud SSH key if applicable
        ssh_key_pem: str | None = None
        if cloud in ("aws", "gcp"):
            job_service.update_progress(db, job_id, 10, f"Retrieving SSH key for {cloud.upper()}…")
            try:
                ssh_key_pem = await ansible_local_service.fetch_ssh_key(cloud)
                if not ssh_key_pem:
                    logger.warning(
                        "No SSH key configured for %s (ansible_ssh_key_sm_name / gcp_ssh_key_secret_name) "
                        "— proceeding without key; ensure the target allows password auth or agent forwarding",
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
            job_service.set_failed(
                db, job_id, f"ansible-playbook exited {rc}:\n{output}"
            )
    except Exception as e:
        logger.exception("ansible-local job %s failed: %s", job_id, e)
        job_service.set_failed(db, job_id, str(e))
    finally:
        db.close()


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
        _run_job, job.id, payload.asset, payload.target, payload.cloud, payload.extra_vars
    )
    return {"job_id": job.id, "status": "queued"}
