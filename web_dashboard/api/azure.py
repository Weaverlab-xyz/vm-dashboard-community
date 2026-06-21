"""
Azure API endpoints:
  GET    /api/azure/images              - List private images (gallery + managed)
  GET    /api/azure/marketplace-images  - Browse Azure Marketplace images
  GET    /api/azure/vms                 - List dashboard-deployed Azure VMs (live state)
  GET    /api/azure/network-options     - Subnets, NSGs, VM sizes for the deploy form
  GET    /api/azure/keyvault-ssh-key   - Retrieve SSH public key from Azure Key Vault
  POST   /api/azure/deploy              - Deploy an Azure VM from an image
  POST   /api/azure/bulk-deploy         - Deploy multiple Azure VMs
  DELETE /api/azure/vms/{vm_name}       - Terminate a dashboard-deployed Azure VM
  POST   /api/azure/vms/{vm_name}/create-image - Capture an image from a VM
  DELETE /api/azure/images/{image_name} - Delete a managed image
"""
import asyncio
import logging
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)
from sqlalchemy.orm import Session

from ..config import settings
from ..database import Job, User, get_db
from ..models.azure import (
    AzureBulkDeployRequest,
    AzureBulkDeployResponse,
    AzureCreateImageRequest,
    AzureDeployRequest,
    AzureDeployResponse,
    AzureImageInfo,
    AzureNetworkOptions,
    AzureSubnetInfo,
    AzureNSGInfo,
    AzureSSHKeyInfo,
    AzureVMInfo,
)
from ..services import azure_service, job_service, cache_service, workgroup_service
from ..services.azure_service import AzureError
from .auth import get_current_user, require_admin, require_permission

router = APIRouter(prefix="/api/azure", tags=["azure"])


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


def _cfg(key: str, fallback: str = "") -> str:
    """Read a value from config_service (DB/wizard) with env-var fallback."""
    from ..services import config_service
    return config_service.get(key) or getattr(settings, key, fallback)


async def _resolve_azure_aci_deploy_key() -> str:
    """Return the BeyondTrust Jumpoint Docker deploy key for Azure ACI launches.

    Resolution order:
      1. Direct DB field `azure_aci_docker_deploy_key` (preferred, backend-neutral
         — config_service resolves through whichever secrets backend the user
         picked on /secrets).
      2. Legacy Password-Safe-only fallback via `azure_aci_ps_deploy_key_title`.
    Returns empty string if neither is configured (caller decides if that's fatal).
    """
    direct = _cfg("azure_aci_docker_deploy_key")
    if direct:
        return direct
    title = _cfg("azure_aci_ps_deploy_key_title")
    if title:
        from ..services import btapi_service
        try:
            return await btapi_service.get_ps_secret(title)
        except Exception as e:
            logger.warning("Azure ACI deploy key fetch from Password Safe failed (%s)", e)
    return ""


async def _resolve_acr_credentials() -> tuple:
    """Return (acr_server, acr_username, acr_password) for the ACI Ansible runner.

    Resolution order:
      1. Direct DB fields `azure_acr_username` / `azure_acr_password` (preferred,
         backend-neutral; whichever secrets backend the user selected on /secrets
         resolves them transparently via config_service).
      2. Legacy Password-Safe-only fallback via `azure_acr_*_secret_title` →
         `btapi_service.get_ps_secret(...)`.

    If `azure_acr_server` is unset, returns ("", "", "") so callers fall back to
    an unauthenticated Docker Hub pull.
    """
    server = _cfg("azure_acr_server")
    if not server:
        return "", "", ""
    username = _cfg("azure_acr_username")
    password = _cfg("azure_acr_password")
    if username and password:
        return server, username, password
    user_title = _cfg("azure_acr_username_secret_title")
    pass_title = _cfg("azure_acr_password_secret_title")
    if user_title and pass_title:
        from ..services import btapi_service
        try:
            username = await btapi_service.get_ps_secret(user_title)
            password = await btapi_service.get_ps_secret(pass_title)
            return server, username, password
        except Exception as e:
            logger.warning("ACR credential fetch from Password Safe failed (%s) — pulling without auth", e)
    return server, "", ""


def _rg():
    return _cfg("azure_resource_group") or "vm-cli-rg"


def _loc():
    return _cfg("azure_location") or "centralus"


def _aci_rg():
    return _cfg("azure_aci_resource_group") or _rg()


async def _validate_ssh_key_override(override) -> None:
    """When the operator overrides the SSH key secret at launch, require it to be a
    Key Vault keypair JSON with a ``public_key`` (resolve_azure_ssh_public_key raises
    a detailed error otherwise). Raises HTTP 400."""
    if not override:
        return
    kv_url = _cfg("azure_key_vault_url")
    try:
        await azure_service.resolve_azure_ssh_public_key(kv_url, override, "")
    except AzureError as e:
        raise HTTPException(status_code=400, detail=f"SSH key secret '{override}' is invalid: {e}")


async def _effective_ssh_public_key(req) -> str:
    """Public key to inject at launch: from the per-launch override secret when set,
    else the key the form already resolved (``req.ssh_public_key``). Keeps the injected
    key in sync with the secret Entitle registration reads the private key from."""
    override = getattr(req, "ssh_key_secret_override", None)
    if not override:
        return req.ssh_public_key
    try:
        return await azure_service.resolve_azure_ssh_public_key(_cfg("azure_key_vault_url"), override, "")
    except AzureError:
        return req.ssh_public_key


# ── Private images (gallery + managed) ───────────────────────────────────────

@router.get("/images")
async def list_images(
    current_user: User = Depends(require_permission("azure", "read")),
):
    """List private images: Shared Image Gallery images + standalone Managed Images. Served from cache (5 min)."""
    cache_key = cache_service.key_global("azure_images")
    ttl = cache_service.TTL["azure_images"]

    async def _fetch():
        return await azure_service.list_private_images(
            _cfg("azure_shared_image_gallery"),
            _cfg("azure_gallery_resource_group"),
            _rg(),
        )

    try:
        payload, cached_at = await cache_service.get_or_refresh(cache_key, ttl, _fetch)
        images = payload.get("images", [])
        warnings = payload.get("warnings", [])
        return {
            "images": [AzureImageInfo(**i) for i in images],
            "count": len(images),
            "cached_at": cached_at,
            "warnings": warnings,
        }
    except AzureError as e:
        raise HTTPException(status_code=503, detail=str(e))


# ── Marketplace images ────────────────────────────────────────────────────────

@router.get("/marketplace-images")
async def list_marketplace_images(
    os_filter: Optional[str] = None,
    current_user: User = Depends(require_permission("azure", "read")),
):
    """
    Browse Azure Marketplace images. Pass ?os_filter=ubuntu|rhel|debian
    to narrow results; omit for all.
    """
    import logging
    logger = logging.getLogger(__name__)
    logger.info("API: marketplace-images called with os_filter=%s", os_filter)
    try:
        images = await azure_service.list_marketplace_images(
            _loc(), os_filter or "all"
        )
        logger.info("API: returned %d marketplace images", len(images))
        return {"images": [AzureImageInfo(**i) for i in images], "count": len(images)}
    except AzureError as e:
        logger.error("API: AzureError - %s", e, exc_info=True)
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.error("API: Unexpected error - %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ── Network options for deploy form ──────────────────────────────────────────

@router.get("/network-options", response_model=AzureNetworkOptions)
async def network_options(
    bust: bool = False,
    current_user: User = Depends(require_permission("azure", "read")),
):
    """Return locations, VM sizes, subnets, NSGs. Served from cache (10 min). Pass ?bust=true to force a fresh fetch."""
    cache_key = cache_service.key_global("azure_network_opts")
    ttl = cache_service.TTL["azure_network_opts"]

    async def _fetch():
        return await azure_service.get_network_options(
            _loc(), _cfg("azure_vnet_resource_group"), _rg()
        )

    try:
        if bust:
            await cache_service.invalidate(cache_key)
        opts, cached_at = await cache_service.get_or_refresh(cache_key, ttl, _fetch)
        return AzureNetworkOptions(
            locations=opts["locations"],
            vm_sizes=opts["vm_sizes"],
            subnets=[AzureSubnetInfo(**s) for s in opts["subnets"]],
            nsgs=[AzureNSGInfo(**n) for n in opts["nsgs"]],
            ssh_keys=[AzureSSHKeyInfo(**k) for k in opts["ssh_keys"]],
            warnings=opts.get("warnings", []),
        )
    except AzureError as e:
        raise HTTPException(status_code=503, detail=str(e))


# ── Key Vault SSH key ─────────────────────────────────────────────────────────

@router.get("/keyvault-ssh-key")
async def get_keyvault_ssh_key(
    current_user: User = Depends(require_permission("azure", "read")),
):
    """Retrieve the SSH public key stored in Azure Key Vault.

    Prefers the unified `azure_ssh_keypair_secret_name` (JSON with
    public_key/private_key fields), falling back to legacy
    `azure_ssh_key_secret_name`.
    """
    kv_url           = _cfg("azure_key_vault_url")
    unified_secret   = _cfg("azure_ssh_keypair_secret_name")
    legacy_secret    = _cfg("azure_ssh_key_secret_name")
    if not kv_url:
        raise HTTPException(
            status_code=503,
            detail="Key Vault not configured. Add the Key Vault URL in Settings → Azure.",
        )
    try:
        key_text = await azure_service.resolve_azure_ssh_public_key(
            kv_url, unified_secret, legacy_secret
        )
        return {
            "secret_name": unified_secret or legacy_secret,
            "ssh_public_key": key_text,
        }
    except AzureError as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.get("/secrets/ssh-keys")
async def list_ssh_key_secret_names(
    current_user: User = Depends(require_permission("azure", "read")),
):
    """Candidate Key Vault secrets for the per-launch SSH-key-secret override picker."""
    kv_url = _cfg("azure_key_vault_url")
    if not kv_url:
        raise HTTPException(status_code=503, detail="Key Vault not configured.")
    try:
        return {"secrets": await azure_service.list_kv_secret_names(kv_url)}
    except AzureError as e:
        raise HTTPException(status_code=503, detail=str(e))


# ── VM SSH key retrieval (mirrors /api/aws/instances/{id}/ssh-key) ───────────

@router.get("/vms/{vm_name}/ssh-key")
async def get_vm_ssh_key(
    vm_name: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("azure", "read")),
):
    """Return the SSH private key for an Azure VM deployed via this dashboard.

    The private key comes from the unified keypair secret in Azure Key Vault
    (`azure_ssh_keypair_secret_name`, JSON `{public_key, private_key}`) or its
    legacy single-purpose fallback. Closes issue #7 where operators had no way
    to retrieve the private key matching the public key the dashboard had just
    injected into the VM, so SSH attempts with a locally-stored copy could
    fail silently if the local file and the KV secret had drifted.

    Includes a `keypair_matches` field with the result of verifying that the
    stored public_key actually corresponds to the stored private_key — if
    `false`, the operator knows the unified KV secret is internally
    inconsistent and SSH will not work until it's repaired.
    """
    kv_url           = _cfg("azure_key_vault_url")
    unified_secret   = _cfg("azure_ssh_keypair_secret_name")
    legacy_pubkey    = _cfg("azure_ssh_key_secret_name")
    legacy_privkey   = _cfg("azure_ssh_private_key_secret_name")
    if not kv_url:
        raise HTTPException(
            status_code=503,
            detail="Key Vault not configured. Add the Key Vault URL in Settings → Azure.",
        )

    # Pull both keys so we can run the match check below.
    try:
        public_key = await azure_service.resolve_azure_ssh_public_key(
            kv_url, unified_secret, legacy_pubkey,
        )
    except AzureError as e:
        raise HTTPException(status_code=503, detail=f"Public key fetch failed: {e}")
    try:
        private_key = await azure_service.resolve_azure_ssh_private_key(
            kv_url, unified_secret, legacy_privkey,
        )
    except AzureError as e:
        raise HTTPException(status_code=503, detail=f"Private key fetch failed: {e}")

    # Pull current IP from the deploy job so the response can include an
    # ssh-ready command. Falls back to nothing if the Job row is missing
    # (e.g. the VM was provisioned before Job.cloud_resource_id was added).
    job = db.query(Job).filter(Job.cloud_resource_id == vm_name).first()
    if job is None:
        for j in db.query(Job).filter(Job.job_type == "azure_deploy").all():
            if j.metadata_dict.get("vm_name") == vm_name:
                job = j
                break

    meta = job.metadata_dict if job else {}
    ip = meta.get("public_ip") or meta.get("private_ip")
    ssh_username = meta.get("ssh_username") or "azureuser"
    ssh_command = f"ssh -i <key-file> {ssh_username}@{ip}" if ip else None

    keypair_check = azure_service.verify_ssh_keypair(public_key, private_key)

    return {
        "vm_name": vm_name,
        "public_key": public_key,
        "private_key": private_key,
        "secret_name": unified_secret or legacy_pubkey,
        "ip": ip,
        "ssh_username": ssh_username,
        "ssh_command": ssh_command,
        "keypair_matches": keypair_check["matches"],
        "keypair_check_error": keypair_check.get("error"),
        # `derived_public_key` is the OpenSSH public string computed from the
        # private key — when keypair_matches is False this lets the operator
        # see exactly what their private key SHOULD pair with vs what's in KV.
        "derived_public_key": keypair_check.get("derived_public_key"),
    }


@router.get("/vms/{vm_name}/admin-password")
async def get_vm_admin_password(
    vm_name: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("azure", "read")),
):
    """Return the generated local-admin password for a Windows VM deployed via
    this dashboard.

    Windows deploys store the password in the configured secrets backend and
    keep only the (backend, ref) pair in job metadata, so this resolves the
    VM's deploy job (single/bulk via cloud_resource_id, desktop-pool seats via
    the pool job's seat_passwords map) and reads the secret back. Permission
    parity with /vms/{vm_name}/ssh-key, which returns Linux private keys."""
    from ..services import secrets_backend_service

    backend = ref = username = ip = None

    job = db.query(Job).filter(Job.cloud_resource_id == vm_name).first()
    if job is None:
        for j in db.query(Job).filter(Job.job_type == "azure_deploy").all():
            if j.metadata_dict.get("vm_name") == vm_name:
                job = j
                break
    if job is not None:
        meta = job.metadata_dict
        if meta.get("admin_password_ref"):
            backend = meta.get("admin_password_backend") or "database"
            ref = meta["admin_password_ref"]
            username = meta.get("admin_username") or meta.get("ssh_username")
            ip = meta.get("public_ip") or meta.get("private_ip")

    if ref is None:
        # Desktop-pool seats: the pool provision job records per-seat refs.
        for j in db.query(Job).filter(Job.job_type == "vdesktop_pool_provision").all():
            entry = (j.metadata_dict.get("seat_passwords") or {}).get(vm_name)
            if entry:
                backend = entry.get("backend") or "database"
                ref = entry.get("ref")
                username = entry.get("username")
                break

    if not ref:
        raise HTTPException(
            status_code=404,
            detail=f"No stored admin password for '{vm_name}' — Linux VM, or deployed outside this dashboard.",
        )

    try:
        password = await asyncio.to_thread(secrets_backend_service.read_sync, backend, ref)
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Secrets backend read failed: {e}")
    if not password:
        raise HTTPException(
            status_code=404,
            detail=f"Secret '{ref}' is empty or missing in backend '{backend}'.",
        )

    job_service.log_audit(
        db, current_user.username, "azure_vm_admin_password_read",
        details={"vm_name": vm_name, "backend": backend},
    )
    return {
        "vm_name": vm_name,
        "username": username or "azureuser",
        "password": password,
        "ip": ip,
        "backend": backend,
        "secret_ref": ref,
    }


# ── VM listing ────────────────────────────────────────────────────────────────

@router.get("/vms")
async def list_vms(
    bust: bool = False,
    workgroup: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("azure", "read")),
):
    """
    List dashboard-deployed Azure VMs with live power state.
    Served from cache (1 min TTL). Pass ?bust=true to force a fresh fetch.

    Non-admins see only VMs whose Job.workgroup (or workgroup tag) is in their
    workgroup list. `?workgroup=<name>` narrows further.
    """
    accessible = _accessible_workgroups(current_user)
    if workgroup is not None:
        canonical = workgroup.lower()
        if accessible is not None and canonical not in accessible:
            raise HTTPException(status_code=403, detail=f"No access to workgroup '{canonical}'")

    cache_key = cache_service.key_global("azure_vms")
    ttl = cache_service.TTL["azure_vms"]

    deploy_jobs = (
        db.query(Job)
        .filter(Job.job_type == "azure_deploy")
        .order_by(Job.created_at.desc())
        .all()
    )
    job_meta = {
        j.metadata_dict["vm_name"]: {
            "id": j.id,
            "created_by": j.created_by,
            "resource_group": j.metadata_dict.get("resource_group"),
            "destroyed": j.metadata_dict.get("destroyed", False),
            "workgroup": (j.workgroup or j.metadata_dict.get("workgroup") or "").lower() or None,
        }
        for j in deploy_jobs if j.metadata_dict.get("vm_name")
    }

    async def _fetch():
        live_vms = await azure_service.describe_vms(_rg())
        live_vm_names = {vm["name"] for vm in live_vms}

        for vm_name, meta in job_meta.items():
            if vm_name not in live_vm_names and not meta["destroyed"]:
                rg = meta["resource_group"] or _rg()
                try:
                    vm_data = await azure_service.get_vm(rg, vm_name)
                    if vm_data:
                        live_vms.append(vm_data)
                        live_vm_names.add(vm_name)
                except Exception:
                    pass

        result = []
        for vm in live_vms:
            meta = job_meta.get(vm["name"])
            wg = (meta or {}).get("workgroup") or vm.get("workgroup")
            result.append({
                **vm,
                "workgroup": wg,
                "job_id": meta["id"] if meta else None,
                "deployed_by": meta["created_by"] if meta else "unknown",
            })
        return result

    try:
        if bust:
            await cache_service.invalidate(cache_key)
        raw, cached_at = await cache_service.get_or_refresh(cache_key, ttl, _fetch)
        filtered = []
        for vm in raw:
            vm_wg = (vm.get("workgroup") or "").lower() or None
            vm["workgroup"] = vm_wg
            if workgroup is not None and vm_wg != workgroup.lower():
                continue
            if accessible is not None:
                if vm_wg is None or vm_wg not in accessible:
                    continue
            filtered.append(vm)
        vms = [AzureVMInfo(**v) for v in filtered]
        return {"vms": vms, "count": len(vms), "cached_at": cached_at}
    except AzureError as e:
        raise HTTPException(status_code=503, detail=str(e))


# ── Deploy ────────────────────────────────────────────────────────────────────

@router.post("/deploy", response_model=AzureDeployResponse)
async def deploy_vm(
    req: AzureDeployRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("azure", "write")),
):
    """
    Deploy an Azure VM from a private image (gallery, managed, or marketplace).
    Returns a job_id trackable at /api/jobs/{job_id} or /ws/jobs/{job_id}.
    """
    if req.os_type.lower() != "windows" and not req.ssh_public_key.strip():
        raise HTTPException(status_code=400, detail="ssh_public_key is required for Linux deploys.")
    rg = req.resource_group or _rg()
    loc = req.location or _loc()
    workgroup = _validate_workgroup(db, current_user, req.workgroup)
    req.workgroup = workgroup
    await _validate_ssh_key_override(req.ssh_key_secret_override)

    job = job_service.create_job(
        db,
        job_type="azure_deploy",
        created_by=current_user.username,
        workgroup=workgroup,
        metadata={
            "image_id": req.image_id,
            "image_publisher": req.image_publisher,
            "image_offer": req.image_offer,
            "image_sku": req.image_sku,
            "image_version": req.image_version,
            "vm_name": req.vm_name,
            "vm_size": req.vm_size,
            "location": loc,
            "resource_group": rg,
            "subnet_id": req.subnet_id,
            "nsg_ids": req.nsg_ids,
            "create_public_ip": req.create_public_ip,
            "os_type": req.os_type,
            "trusted_launch": req.trusted_launch,
            "ssh_username": req.ssh_username,  # so /vms/{name}/ssh-key + /admin-password can echo the right user
            "workgroup": workgroup,
        },
    )
    job_service.set_cloud_resource_id(db, job.id, req.vm_name)

    job_service.log_audit(
        db, current_user.username, "azure_deploy",
        details={"image_id": req.image_id, "vm_name": req.vm_name, "workgroup": workgroup},
    )

    background_tasks.add_task(_run_deploy, job.id, req, rg, loc)

    return AzureDeployResponse(
        job_id=job.id,
        vm_name=req.vm_name,
        message=f"Azure VM deployment queued: {req.vm_name}",
    )


# ── Bulk Deploy ───────────────────────────────────────────────────────────────

@router.post("/bulk-deploy", response_model=AzureBulkDeployResponse)
async def bulk_deploy_vms(
    req: AzureBulkDeployRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("azure", "write")),
):
    """
    Deploy multiple Azure VMs in one request.
    Each VM gets its own job_id. One ACI Jumpoint container is shared across the batch.
    """
    if not req.items:
        raise HTTPException(status_code=400, detail="At least one VM item is required.")
    if req.os_type.lower() != "windows" and not req.ssh_public_key.strip():
        raise HTTPException(status_code=400, detail="ssh_public_key is required for Linux deploys.")

    rg = req.resource_group or _rg()
    loc = req.location or _loc()
    workgroup = _validate_workgroup(db, current_user, req.workgroup)
    req.workgroup = workgroup
    await _validate_ssh_key_override(req.ssh_key_secret_override)

    job_items = []
    for item in req.items:
        job = job_service.create_job(
            db,
            job_type="azure_deploy",
            created_by=current_user.username,
            workgroup=workgroup,
            metadata={
                "image_id": req.image_id,
                "vm_name": item.vm_name,
                "vm_size": req.vm_size,
                "location": loc,
                "resource_group": rg,
                "subnet_id": req.subnet_id,
                "nsg_ids": req.nsg_ids,
                "create_public_ip": req.create_public_ip,
                "os_type": req.os_type,
                "trusted_launch": req.trusted_launch,
                "ssh_username": req.ssh_username,
                "workgroup": workgroup,
                "bulk": True,
            },
        )
        job_service.set_cloud_resource_id(db, job.id, item.vm_name)
        job_service.log_audit(
            db, current_user.username, "azure_deploy",
            details={"image_id": req.image_id, "vm_name": item.vm_name, "workgroup": workgroup, "bulk": True},
        )
        job_items.append((job.id, item.vm_name))

    background_tasks.add_task(_run_bulk_deploy, job_items, req, rg, loc)

    return AzureBulkDeployResponse(
        jobs=[AzureDeployResponse(job_id=jid, vm_name=vn) for jid, vn in job_items]
    )


# ── Reassign workgroup ───────────────────────────────────────────────────────

class _WorkgroupReassignRequest(BaseModel):
    workgroup: str


@router.patch("/vms/{vm_name}/workgroup")
async def reassign_vm_workgroup(
    vm_name: str,
    req: _WorkgroupReassignRequest,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    """Rewrite the `workgroup` tag on an Azure VM and update the originating Job
    row. Admin only."""
    wg = workgroup_service.get(db, req.workgroup)
    if not wg:
        raise HTTPException(status_code=400, detail=f"Unknown workgroup '{req.workgroup}'")
    canonical = wg.name

    job = db.query(Job).filter(Job.cloud_resource_id == vm_name).first()
    if job is None:
        for j in db.query(Job).filter(Job.job_type == "azure_deploy").all():
            if j.metadata_dict.get("vm_name") == vm_name:
                job = j
                break

    rg = (job.metadata_dict.get("resource_group") if job else None) or _rg()

    try:
        await azure_service.set_workgroup_tag(rg, vm_name, canonical)
    except AzureError as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    if job is not None:
        job.workgroup = canonical
        meta = job.metadata_dict
        meta["workgroup"] = canonical
        job.metadata_dict = meta
        if not job.cloud_resource_id:
            job.cloud_resource_id = vm_name
        db.commit()

    await cache_service.invalidate(cache_service.key_global("azure_vms"))
    return {"vm_name": vm_name, "workgroup": canonical, "job_id": job.id if job else None}


# ── Terminate ─────────────────────────────────────────────────────────────────

@router.delete("/vms/{vm_name}")
async def destroy_vm(
    vm_name: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("azure", "delete")),
):
    """Terminate a dashboard-deployed Azure VM and clean up NIC/PIP."""
    deploy_jobs = (
        db.query(Job)
        .filter(Job.job_type == "azure_deploy", Job.status == "completed")
        .all()
    )
    deploy_job = None
    for job in deploy_jobs:
        meta = job.metadata_dict
        if meta.get("vm_name") == vm_name and not meta.get("destroyed"):
            deploy_job = job
            break

    if not deploy_job:
        raise HTTPException(
            status_code=404,
            detail=f"No active deployment found for VM '{vm_name}'. "
                   "It may have already been terminated or was not deployed from this dashboard.",
        )

    destroy_job = job_service.create_job(
        db,
        job_type="azure_destroy",
        created_by=current_user.username,
        metadata={"vm_name": vm_name, "deploy_job_id": deploy_job.id},
    )

    job_service.log_audit(
        db, current_user.username, "azure_destroy",
        details={"vm_name": vm_name},
    )

    rg = deploy_job.metadata_dict.get("resource_group") or _rg()
    background_tasks.add_task(_run_destroy, destroy_job.id, deploy_job.id, vm_name, rg)

    return {"job_id": destroy_job.id, "status": "pending", "message": f"Azure VM '{vm_name}' termination queued"}


# ── Create image from VM ──────────────────────────────────────────────────────

@router.post("/vms/{vm_name}/create-image")
async def create_image_from_vm(
    vm_name: str,
    req: AzureCreateImageRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("azure", "write")),
):
    """
    Capture a managed image from an Azure VM.
    If generalize=True: VM will be deallocated + generalized (VM becomes unusable).
    """
    deploy_jobs = (
        db.query(Job)
        .filter(Job.job_type == "azure_deploy", Job.status == "completed")
        .all()
    )
    deploy_job = next(
        (j for j in deploy_jobs if j.metadata_dict.get("vm_name") == vm_name
         and not j.metadata_dict.get("destroyed")),
        None,
    )
    rg = deploy_job.metadata_dict.get("resource_group") if deploy_job else _rg()

    job = job_service.create_job(
        db,
        job_type="azure_create_image",
        created_by=current_user.username,
        metadata={
            "vm_name": vm_name,
            "image_name": req.name,
            "description": req.description,
            "generalize": req.generalize,
            "resource_group": rg,
        },
    )

    job_service.log_audit(
        db, current_user.username, "azure_create_image",
        details={"vm_name": vm_name, "image_name": req.name, "generalize": req.generalize},
    )

    background_tasks.add_task(_run_create_image, job.id, vm_name, rg, req)

    return {"job_id": job.id, "status": "pending", "message": f"Image capture queued for VM '{vm_name}'"}


# ── Export managed image to portable VHD on hub backend ──────────────────────

class ExportImageRequest(BaseModel):
    image_name: str  # Registry name to record the exported image under
    resource_group: Optional[str] = None  # Defaults to the configured azure_resource_group
    os_type: str = "Linux"  # Guest OS recorded on the registry row ("Linux" | "Windows")


class ExportImageResponse(BaseModel):
    job_id: str
    status: str
    message: str


@router.post("/images/{image_name}/export", response_model=ExportImageResponse)
async def export_managed_image(
    image_name: str,
    req: ExportImageRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("azure", "write")),
):
    """Manually export a managed image to VHD on the hub backend and register
    it in the image registry. Useful when the auto-export during build was
    skipped or failed."""
    from .packer import export_and_register_azure

    rg = req.resource_group or _rg()
    job = job_service.create_job(
        db,
        job_type="azure_export_image",
        created_by=current_user.username,
        metadata={"image_name": image_name, "registry_name": req.image_name, "resource_group": rg},
    )
    job_service.log_audit(
        db, current_user.username, "azure_export_image",
        details={"image_name": image_name, "registry_name": req.image_name},
    )

    # Capture scalars before defining the background closure. FastAPI closes
    # the request's DB session when this handler returns, so `current_user`
    # would be a detached ORM instance by the time _run() executes and any
    # attribute access (e.g. .username) would raise DetachedInstanceError.
    job_id = job.id
    registry_name = req.image_name
    username = current_user.username
    os_type = req.os_type

    async def _run():
        d = _get_db_session()
        try:
            job_service.set_running(d, job_id)
            result = await export_and_register_azure(
                d, job_id, registry_name, image_name, rg, username,
                os_type=os_type,
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


# ── Delete image ──────────────────────────────────────────────────────────────

@router.delete("/images/{image_name}")
async def delete_image(
    image_name: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("azure", "delete")),
):
    """Delete a standalone managed image from the resource group."""
    try:
        await azure_service.delete_image(_rg(), image_name)
    except AzureError as e:
        raise HTTPException(status_code=400, detail=str(e))

    job_service.log_audit(
        db, current_user.username, "azure_delete_image",
        details={"image_name": image_name},
    )
    await cache_service.invalidate(cache_service.key_global("azure_images"))
    return {"deleted": True, "image_name": image_name}


# ── Background task helpers ───────────────────────────────────────────────────

def _get_db_session():
    from ..database import SessionLocal
    return SessionLocal()


async def _run_deploy(job_id: str, req: AzureDeployRequest, rg: str, loc: str):
    db = _get_db_session()
    result = {}
    is_windows = req.os_type.lower() == "windows"
    try:
        job_service.set_running(db, job_id)

        # Windows: generate + vault the admin password before any cloud
        # resources exist — a VM whose password can't be retrieved is useless.
        admin_password = ""
        if is_windows:
            job_service.update_progress(db, job_id, 5, "Generating Windows admin password…")
            admin_password = azure_service.generate_windows_admin_password()
            backend, ref = await asyncio.to_thread(
                azure_service.store_windows_admin_password, req.vm_name, job_id[:8], admin_password,
            )
            # Reference only — job metadata is visible via the jobs API.
            result["admin_username"] = req.ssh_username
            result["admin_password_backend"] = backend
            result["admin_password_ref"] = ref

        # Step 0: Quota check — fail fast before any resources are created
        job_service.update_progress(db, job_id, 10, f"Checking Azure quota in {loc}…")
        await azure_service.check_vm_quota(loc, req.vm_size)

        # Step 1: Start ACI Jumpoint container (BeyondTrust only)
        deploy_key_note = ""
        if settings.beyondtrust_enabled:
            from ..services import btapi_service
            job_service.update_progress(db, job_id, 15, "Starting BeyondTrust ACI Jumpoint container…")
            try:
                try:
                    if getattr(req, "docker_deploy_key_ref", None):
                        from ..services import config_service as _cs
                        deploy_key = _cs.resolve_reference(req.docker_deploy_key_ref.strip())
                    else:
                        deploy_key = await _resolve_azure_aci_deploy_key()
                except Exception as key_err:
                    logger.warning("ACI deploy key fetch failed (%s) — creating ACI without deploy key", key_err)
                    deploy_key = ""
                    deploy_key_note = f" [deploy key fetch failed: {key_err}]"
                # Fetch ACR credentials if configured (backend-neutral resolution).
                acr_server, acr_username, acr_password = await _resolve_acr_credentials()
                aci_group_name = await azure_service.run_aci_jumpoint_task(
                    rg=_aci_rg(),
                    location=loc,
                    subnet_id=settings.azure_aci_subnet_id,
                    image=_cfg("azure_aci_jumpoint_image"),
                    cpu=settings.azure_aci_cpu,
                    memory=settings.azure_aci_memory,
                    deploy_key=deploy_key,
                    acr_server=acr_server,
                    acr_username=acr_username,
                    acr_password=acr_password,
                    storage_account=settings.azure_aci_storage_account,
                    storage_account_rg=settings.azure_aci_storage_account_rg,
                    file_share=settings.azure_aci_file_share,
                )
                result["aci_group_name"] = aci_group_name
                job_service.update_progress(
                    db, job_id, 30,
                    f"ACI Jumpoint started ({aci_group_name}){deploy_key_note}, deploying VM…"
                )
            except Exception as e:
                result["aci_error"] = str(e)
                job_service.update_progress(
                    db, job_id, 30,
                    f"ACI Jumpoint failed (non-fatal): {e}{deploy_key_note} — continuing with VM deploy…"
                )
        else:
            job_service.update_progress(db, job_id, 30, "Preparing Azure VM deploy…")

        # Step 2: Deploy Azure VM (3-step: PIP → NIC → VM)
        job_service.update_progress(db, job_id, 35, f"Creating Azure VM '{req.vm_name}'…")
        try:
            vm_result = await azure_service.deploy_vm(
                rg=rg,
                location=loc,
                vm_name=req.vm_name,
                vm_size=req.vm_size,
                image_id=req.image_id,
                subnet_id=req.subnet_id,
                nsg_ids=req.nsg_ids,
                create_public_ip=req.create_public_ip,
                ssh_username=req.ssh_username,
                ssh_public_key=await _effective_ssh_public_key(req),
                image_publisher=req.image_publisher,
                image_offer=req.image_offer,
                image_sku=req.image_sku,
                image_version=req.image_version,
                workgroup=getattr(req, "workgroup", "") or "",
                os_type=req.os_type,
                admin_password=admin_password,
                trusted_launch=getattr(req, "trusted_launch", False),
            )
            result.update(vm_result)
        except AzureError as e:
            if result.get("aci_group_name"):
                try:
                    await azure_service.stop_aci_jumpoint_task(_aci_rg(), result["aci_group_name"])
                except Exception:
                    pass
            raise

        hostname = result.get("private_ip") or result.get("public_ip") or req.vm_name
        job_service.update_progress(
            db, job_id, 70,
            f"VM '{req.vm_name}' created ({hostname})"
            + ("…" if is_windows else ", provisioning Shell Jump…")
        )

        # Step 3: BeyondTrust PRA — Shell Jump (optional; SSH, so Linux only)
        if settings.beyondtrust_enabled and is_windows:
            job_service.update_progress(
                db, job_id, 90,
                "Windows VM deployed — Shell Jump (SSH) skipped; broker access with an "
                "RDP jump item on the Jumpoint. Password: Azure → VMs → Password."
            )
        elif settings.beyondtrust_enabled:
            from ..services import terraform_pra_service
            # Resolve from config_service (wizard/DB) first, then env-var defaults.
            # Azure-specific keys override the shared bt_* keys.
            from ..services import config_service as _cs
            jump_group = (getattr(req, "jump_group", None) or "").strip() or _cfg("azure_bt_jump_group_name") or _cfg("bt_jump_group_name")
            jumpoint_name = (getattr(req, "jumpoint_name", None) or "").strip() or _cfg("azure_jumpoint_name") or _cfg("bt_jumpoint_name")
            _cred = getattr(req, "pra_credential_ref", None)
            _client_secret = _cs.resolve_reference(_cred.strip()) if _cred else ""
            aci_note = f" (ACI: {result['aci_group_name']})" if result.get("aci_group_name") else (
                f" (ACI failed: {result['aci_error']})" if result.get("aci_error") else " (no ACI)"
            )
            try:
                bt_result = await terraform_pra_service.provision_jump(
                    vm_name=req.vm_name,
                    hostname=hostname,
                    jump_group_name=jump_group,
                    jumpoint_name=jumpoint_name,
                    tag="Azure",
                    client_secret=_client_secret,
                )
                result["bt_shell_jump_id"] = bt_result.get("shell_jump_id")
                result["bt_jump_group_name"] = bt_result.get("jump_group_name")
                result["bt_tf_state"] = bt_result.get("tf_state_json")
                job_service.update_progress(
                    db, job_id, 90,
                    f"Shell Jump created (ID: {bt_result.get('shell_jump_id')}, "
                    f"group: {jump_group}){aci_note}"
                )
            except Exception as e:
                result["bt_error"] = str(e)
                job_service.update_progress(
                    db, job_id, 90,
                    f"VM deployed but Shell Jump provisioning failed: {e}{aci_note}"
                )
        else:
            job_service.update_progress(db, job_id, 90, "VM deployed.")

        # Step 4: Entitle — register as SSH ephemeral-accounts integration (Linux
        # only; per-build opt-in). Public VM → no agent; private → shared agent.
        from ..services import entitle_vm_hook
        if (getattr(req, "register_in_entitle", False) and not is_windows
                and entitle_vm_hook.registration_enabled()):
            await entitle_vm_hook.register(db, job_id, req.vm_name, hostname,
                                           private=not req.create_public_ip,
                                           result=result, tag="Azure",
                                           ssh_key_secret=req.ssh_key_secret_override or "")

        job_service.set_completed(db, job_id, result)
        await cache_service.invalidate(cache_service.key_global("azure_vms"))

    except AzureError as e:
        job_service.set_failed(db, job_id, str(e))
    except Exception as e:
        job_service.set_failed(db, job_id, f"Unexpected error: {e}")
    finally:
        db.close()


async def _run_bulk_deploy(job_items: list, req: AzureBulkDeployRequest, rg: str, loc: str):
    """Start ONE ACI Jumpoint for the batch, then deploy each VM sequentially."""
    db = _get_db_session()
    aci_group_name = None
    is_windows = req.os_type.lower() == "windows"
    try:
        for job_id, _ in job_items:
            job_service.set_running(db, job_id)

        first_job_id = job_items[0][0]

        # Step 0: Quota check — fail fast before any resources are created
        job_service.update_progress(db, first_job_id, 5, f"Checking Azure quota in {loc}…")
        await azure_service.check_vm_quota(loc, req.vm_size)

        aci_error = None
        deploy_key_note = ""
        if settings.beyondtrust_enabled:
            from ..services import btapi_service
            job_service.update_progress(
                db, first_job_id, 10,
                f"Starting ACI Jumpoint for {len(job_items)}-VM batch…"
            )
            try:
                try:
                    deploy_key = await _resolve_azure_aci_deploy_key()
                except Exception as key_err:
                    logger.warning("ACI deploy key fetch failed (%s) — creating ACI without deploy key", key_err)
                    deploy_key = ""
                    deploy_key_note = f" [deploy key fetch failed: {key_err}]"
                # Fetch ACR credentials if configured (backend-neutral resolution).
                acr_server, acr_username, acr_password = await _resolve_acr_credentials()
                aci_group_name = await azure_service.run_aci_jumpoint_task(
                    rg=_aci_rg(),
                    location=loc,
                    subnet_id=settings.azure_aci_subnet_id,
                    image=_cfg("azure_aci_jumpoint_image"),
                    cpu=settings.azure_aci_cpu,
                    memory=settings.azure_aci_memory,
                    deploy_key=deploy_key,
                    acr_server=acr_server,
                    acr_username=acr_username,
                    acr_password=acr_password,
                    storage_account=settings.azure_aci_storage_account,
                    storage_account_rg=settings.azure_aci_storage_account_rg,
                    file_share=settings.azure_aci_file_share,
                )
            except Exception as e:
                aci_error = str(e)
                aci_group_name = None
        else:
            job_service.update_progress(
                db, first_job_id, 10,
                f"Preparing {len(job_items)}-VM batch…"
            )

        for job_id, vm_name in job_items:
            result: dict = {}
            if aci_group_name:
                result["aci_group_name"] = aci_group_name
            elif aci_error:
                result["aci_error"] = aci_error

            try:
                # Windows: per-VM password, vaulted before that VM is created.
                admin_password = ""
                if is_windows:
                    job_service.update_progress(db, job_id, 30, "Generating Windows admin password…")
                    admin_password = azure_service.generate_windows_admin_password()
                    backend, ref = await asyncio.to_thread(
                        azure_service.store_windows_admin_password, vm_name, job_id[:8], admin_password,
                    )
                    result["admin_username"] = req.ssh_username
                    result["admin_password_backend"] = backend
                    result["admin_password_ref"] = ref

                job_service.update_progress(db, job_id, 35, f"Creating Azure VM '{vm_name}'…")
                vm_result = await azure_service.deploy_vm(
                    rg=rg,
                    location=loc,
                    vm_name=vm_name,
                    vm_size=req.vm_size,
                    image_id=req.image_id,
                    subnet_id=req.subnet_id,
                    nsg_ids=req.nsg_ids,
                    create_public_ip=req.create_public_ip,
                    ssh_username=req.ssh_username,
                    ssh_public_key=await _effective_ssh_public_key(req),
                    image_publisher=req.image_publisher,
                    image_offer=req.image_offer,
                    image_sku=req.image_sku,
                    image_version=req.image_version,
                    workgroup=getattr(req, "workgroup", "") or "",
                    os_type=req.os_type,
                    admin_password=admin_password,
                    trusted_launch=getattr(req, "trusted_launch", False),
                )
                result.update(vm_result)

                hostname = result.get("private_ip") or result.get("public_ip") or vm_name
                job_service.update_progress(
                    db, job_id, 70,
                    f"VM '{vm_name}' created ({hostname})"
                    + ("…" if is_windows else ", provisioning Shell Jump…")
                )

                if settings.beyondtrust_enabled and is_windows:
                    job_service.update_progress(
                        db, job_id, 90,
                        "Windows VM deployed — Shell Jump (SSH) skipped; broker access with an "
                        "RDP jump item on the Jumpoint. Password: Azure → VMs → Password."
                    )
                elif settings.beyondtrust_enabled:
                    from ..services import terraform_pra_service
                    jump_group = _cfg("azure_bt_jump_group_name") or _cfg("bt_jump_group_name")
                    jumpoint_name = _cfg("azure_jumpoint_name") or _cfg("bt_jumpoint_name")
                    aci_note = f" (ACI: {result['aci_group_name']})" if result.get("aci_group_name") else (
                        f" (ACI failed: {result['aci_error']})" if result.get("aci_error") else " (no ACI)"
                    )
                    try:
                        bt_result = await terraform_pra_service.provision_jump(
                            vm_name=vm_name,
                            hostname=hostname,
                            jump_group_name=jump_group,
                            jumpoint_name=jumpoint_name,
                            tag="Azure",
                        )
                        result["bt_shell_jump_id"] = bt_result.get("shell_jump_id")
                        result["bt_jump_group_name"] = bt_result.get("jump_group_name")
                        result["bt_tf_state"] = bt_result.get("tf_state_json")
                        job_service.update_progress(
                            db, job_id, 90,
                            f"Shell Jump created (ID: {bt_result.get('shell_jump_id')}, "
                            f"group: {jump_group}){aci_note}"
                        )
                    except Exception as e:
                        result["bt_error"] = str(e)
                        job_service.update_progress(
                            db, job_id, 90, f"VM deployed but Shell Jump failed: {e}{aci_note}"
                        )
                else:
                    job_service.update_progress(db, job_id, 90, "VM deployed.")

                # Step 4: Entitle — register as SSH integration (Linux only; opt-in).
                from ..services import entitle_vm_hook
                if (getattr(req, "register_in_entitle", False) and not is_windows
                        and entitle_vm_hook.registration_enabled()):
                    await entitle_vm_hook.register(db, job_id, vm_name, hostname,
                                                   private=not req.create_public_ip,
                                                   result=result, tag="Azure",
                                                   ssh_key_secret=req.ssh_key_secret_override or "")

                job_service.set_completed(db, job_id, result)

            except AzureError as e:
                job_service.set_failed(db, job_id, str(e))
            except Exception as e:
                job_service.set_failed(db, job_id, f"Unexpected error: {e}")

        await cache_service.invalidate(cache_service.key_global("azure_vms"))

    except Exception as e:
        for job_id, _ in job_items:
            job_service.set_failed(db, job_id, f"Bulk deploy error: {e}")
    finally:
        db.close()


async def _run_destroy(destroy_job_id: str, deploy_job_id: str, vm_name: str, rg: str):
    db = _get_db_session()
    try:
        job_service.set_running(db, destroy_job_id)
        job_service.update_progress(db, destroy_job_id, 20, f"Terminating Azure VM '{vm_name}'…")

        await azure_service.terminate_vm(rg, vm_name)

        result = {"vm_name": vm_name, "terminated": True}
        deploy_job = job_service.get_job(db, deploy_job_id)
        if deploy_job:
            meta = deploy_job.metadata_dict

            # Stop ACI Jumpoint — only if no other active VMs share this container group
            aci_group_name = meta.get("aci_group_name")
            active_sibling_jobs = [
                j for j in db.query(Job)
                .filter(Job.job_type == "azure_deploy", Job.status == "completed")
                .all()
                if j.id != deploy_job_id
                and not j.metadata_dict.get("destroyed")
            ]
            if aci_group_name:
                sibling_count = sum(
                    1 for j in active_sibling_jobs
                    if j.metadata_dict.get("aci_group_name") == aci_group_name
                )
                if sibling_count == 0:
                    job_service.update_progress(
                        db, destroy_job_id, 50, "Stopping Jumpoint ACI container…"
                    )
                    try:
                        await azure_service.stop_aci_jumpoint_task(_aci_rg(), aci_group_name)
                        result["aci_group_stopped"] = aci_group_name
                    except AzureError as e:
                        result["aci_error"] = str(e)
                else:
                    job_service.update_progress(
                        db, destroy_job_id, 50,
                        f"ACI Jumpoint shared with {sibling_count} other active VM(s) — leaving running…"
                    )
                    result["aci_group_shared"] = aci_group_name

            # Fallback: if no metadata-tracked ACI and no other active VMs remain,
            # enumerate and stop all dashboard ACI jumpoints (covers untracked containers)
            if not aci_group_name and not active_sibling_jobs:
                job_service.update_progress(
                    db, destroy_job_id, 50, "No active VMs remain — checking for orphaned ACI Jumpoints…"
                )
                try:
                    running_acis = await azure_service.list_aci_tasks(_aci_rg())
                    stopped_acis = []
                    for aci in running_acis:
                        try:
                            await azure_service.stop_aci_jumpoint_task(_aci_rg(), aci["group_name"])
                            stopped_acis.append(aci["group_name"])
                        except AzureError as e:
                            result.setdefault("aci_errors", []).append(f"{aci['group_name']}: {e}")
                    if stopped_acis:
                        result["aci_groups_stopped"] = stopped_acis
                except AzureError as e:
                    result["aci_error"] = str(e)

            # Remove BeyondTrust Shell Jump if this deploy provisioned one.
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

            # Mark original deploy job as destroyed (mirrors AWS pattern)
            meta["destroyed"] = True
            job_service.set_completed(db, deploy_job_id, meta)

        job_service.set_completed(db, destroy_job_id, result)
        await cache_service.invalidate(cache_service.key_global("azure_vms"))

    except AzureError as e:
        job_service.set_failed(db, destroy_job_id, str(e))
    except Exception as e:
        job_service.set_failed(db, destroy_job_id, f"Unexpected error: {e}")
    finally:
        db.close()


async def _run_create_image(
    job_id: str, vm_name: str, rg: str, req: AzureCreateImageRequest
):
    db = _get_db_session()
    try:
        job_service.set_running(db, job_id)
        if req.generalize:
            job_service.update_progress(
                db, job_id, 20,
                f"Deallocating and generalizing VM '{vm_name}' (VM will be unusable after this)…"
            )
        else:
            job_service.update_progress(db, job_id, 20, f"Capturing image from VM '{vm_name}'…")

        result = await azure_service.create_image_from_vm(rg, vm_name, req.name, req.generalize)

        job_service.update_progress(db, job_id, 90, f"Image '{req.name}' created successfully.")
        job_service.set_completed(db, job_id, result)
        await cache_service.invalidate(cache_service.key_global("azure_images"))

    except AzureError as e:
        job_service.set_failed(db, job_id, str(e))
    except Exception as e:
        job_service.set_failed(db, job_id, f"Unexpected error: {e}")
    finally:
        db.close()
