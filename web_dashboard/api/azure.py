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
import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)
from sqlalchemy.orm import Session

from ..config import settings
from ..database import Job, User, VirtualDesktop, get_db
from ..models.azure import (
    AzureBulkDeployItem,
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
from ..services import (azure_service, azure_listing, deploy_batch, job_service,
                        cache_service, cloud_stats, region_catalog, workgroup_service)
from ..services.azure_service import AzureError
from .auth import require_admin, require_permission

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






def _rg():
    return _cfg("azure_resource_group") or "vm-cli-rg"


def _loc():
    return _cfg("azure_location") or "centralus"


def _rg_for(location: str) -> str:
    """Resource group for a given region, resolved through the per-region config
    sets (PR3). Falls back per-field to the flat ``azure_resource_group`` when the
    region isn't configured or is the default — so single-region setups are
    unchanged."""
    from ..services.region_config import resolve_azure_region
    return resolve_azure_region(location)["resource_group"] or "vm-cli-rg"


def _resolve_location(location: Optional[str]) -> str:
    """Resolve the effective Azure location for a request. Format validation +
    normalisation (display names like "East US 2" → the compact form) are delegated
    to the shared region catalog; a blank/None location falls back to the configured
    default (``_loc()``); a malformed location is rejected with HTTP 400."""
    if location is None or not location.strip():
        return _loc()
    loc = region_catalog.normalize("azure", location)
    if not region_catalog.validate("azure", loc):
        raise HTTPException(status_code=400, detail=f"Invalid Azure location '{location}'")
    return loc




async def _reject_cross_region_network(subnet_id: str, nsg_ids, location: str) -> None:
    """400 when a picked subnet/NSG lives in a region other than ``location``.

    Azure catches this itself — ``azure_service._validate_deploy_consistency`` raises
    before any resource is created — but that runs in the RUNNER, per VM: on a bulk
    deploy it fires once the N child job rows already exist and fails every one of
    them. Rejecting the request instead keeps the mismatch in front of the operator,
    where it is fixable, and creates no jobs.

    The mismatch is easy to submit because a subnet's ARM id embeds the VNet's
    resource group, not its region, so a stale picker selection reads as plausible:
    the sandbox names its VNet/subnet identically in every region it is run in.

    Ids whose region can't be resolved are left alone (see ``resource_locations``) —
    the runner-side check is still behind this.
    """
    ids = [i for i in [subnet_id, *(nsg_ids or [])] if i and str(i).strip()]
    if not ids:
        return
    want = region_catalog.normalize("azure", location)
    found = await azure_service.resource_locations(ids)
    for rid in ids:
        actual = found.get(rid) or ""
        if not actual or region_catalog.normalize("azure", actual) == want:
            continue
        kind = "subnet" if "/subnets/" in rid else "NSG"
        raise HTTPException(
            status_code=400,
            detail=(f"The selected {kind} '{rid.rsplit('/', 1)[-1]}' is in {actual}, but "
                    f"the deploy location is {location}. Azure VNets and NSGs are "
                    f"regional — pick a {kind} in {location}, or deploy into {actual}."),
        )


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




# ── Private images (gallery + managed) ───────────────────────────────────────

@router.get("/images")
async def list_images(
    current_user: User = Depends(require_permission("azure", "read")),
):
    """List private images: Shared Image Gallery images + standalone Managed Images. Served from cache (5 min)."""
    cache_key = cache_service.key_global("azure_images")
    ttl = cache_service.TTL["azure_images"]

    # Gallery defaults resolve through the per-region config sets (PR3). With no
    # region map configured this returns the flat azure_shared_image_gallery /
    # azure_gallery_resource_group / azure_resource_group values verbatim.
    from ..services.region_config import resolve_azure_region
    region = resolve_azure_region(_loc())

    async def _fetch():
        return await azure_service.list_private_images(
            region["gallery_name"],
            region["gallery_resource_group"],
            region["resource_group"] or "vm-cli-rg",
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
    location: Optional[str] = None,
    bust: bool = False,
    current_user: User = Depends(require_permission("azure", "read")),
):
    """Return locations, VM sizes, subnets, NSGs. Subnets/NSGs/sizes are scoped to
    ``location`` (default: the configured ``azure_location``); pass ?location= to
    target another region. Served from a per-region cache (10 min); ?bust=true forces a refresh."""
    loc = _resolve_location(location)
    cache_key = cache_service.key_param("azure_network_opts", location=loc)
    ttl = cache_service.TTL["azure_network_opts"]

    async def _fetch():
        return await azure_service.get_network_options(
            loc, _cfg("azure_vnet_resource_group"), _rg()
        )

    try:
        if bust:
            await cache_service.invalidate(cache_key)
        opts, cached_at = await cache_service.get_or_refresh(cache_key, ttl, _fetch)
        return AzureNetworkOptions(
            location=opts.get("location", ""),
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

def _listing_resource_groups(job_meta: dict) -> set:
    """Resource groups the VM listing must cover — see services.azure_listing."""
    def _configured_regions():
        from ..services.region_config import load_region_configs
        return list(load_region_configs("azure"))

    return azure_listing.listing_resource_groups(
        job_meta, default_rg=_rg, rg_for=_rg_for,
        configured_regions=_configured_regions)


async def _fetch_vms(db: Session) -> list:
    """Dashboard-deployed Azure VMs (azure_deploy jobs) merged with live power
    state. Shared by /vms and /dashboard-stats. Workgroup is normalized to
    lowercase so both the list filter and the stats RBAC compare consistently."""
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

    resource_groups = _listing_resource_groups(job_meta)

    live_vms = []
    live_vm_names = set()
    for rg in sorted(resource_groups):
        try:
            for vm in await azure_service.describe_vms(rg):
                if vm["name"] not in live_vm_names:
                    live_vms.append(vm)
                    live_vm_names.add(vm["name"])
        except AzureError:
            # One unreachable/deleted RG shouldn't blank the whole list.
            logger.warning("Azure VM listing: describe_vms failed for resource group %s", rg,
                           exc_info=True)

    # Anything a deploy job knows about but no RG listing returned — e.g. a VM
    # excluded by tag filtering — is still fetched by name.
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
        wg = ((meta or {}).get("workgroup") or vm.get("workgroup") or "").lower() or None
        result.append({
            **vm,
            "workgroup": wg,
            "job_id": meta["id"] if meta else None,
            "deployed_by": meta["created_by"] if meta else "unknown",
        })
    return result


@router.get("/dashboard-stats")
async def azure_dashboard_stats(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("azure", "read")),
):
    """One-call counts for the Azure dashboard tiles (VMs total+running, images
    total) — reuses the same cached data + RBAC as the list endpoints. A null
    section → the tile shows unavailable."""
    out = {"instances": None, "images": None}
    try:
        raw, _ = await cache_service.get_or_refresh(
            cache_service.key_global("azure_vms"),
            cache_service.TTL["azure_vms"],
            lambda: _fetch_vms(db))
        accessible = _accessible_workgroups(current_user)
        out["instances"] = cloud_stats.summarize_instances(raw, accessible, "state")
        # Azure keys region as "location" on the VM rows.
        out["instances"]["by_region"] = cloud_stats.summarize_by_region(
            raw, accessible, "state", "location")
    except AzureError:
        pass
    try:
        from ..services.region_config import resolve_azure_region
        region = resolve_azure_region(_loc())
        payload, _ = await cache_service.get_or_refresh(
            cache_service.key_global("azure_images"),
            cache_service.TTL["azure_images"],
            lambda: azure_service.list_private_images(
                region["gallery_name"], region["gallery_resource_group"],
                region["resource_group"] or "vm-cli-rg"))
        out["images"] = {"total": len(payload.get("images", []))}
    except AzureError:
        pass
    return out


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

    try:
        if bust:
            await cache_service.invalidate(cache_key)
        raw, cached_at = await cache_service.get_or_refresh(cache_key, ttl, lambda: _fetch_vms(db))
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

async def _fan_out_batch(
    req: AzureDeployRequest, db: Session, current_user: User,
    *, loc: str, rg: str, workgroup: str,
) -> AzureDeployResponse:
    """Fan a ``count > 1`` deploy out into one ``azure_bulk_deploy`` parent plus N
    ``queued`` ``azure_deploy`` children — the same parent type the multi-select bulk
    route uses, with every child built from the one image.

    A separate module-level function, NOT an ``if`` inside ``deploy_vm``:
    ``test_worker_dispatch``'s children-are-unclaimable rule walks the AST per
    function, so a ``children``-carrying create_job in the same function as the single
    deploy's ``pending`` one reads as a violation whatever the runtime branch does.
    """
    names = deploy_batch.expand_names(req.vm_name, req.count, "azure")
    deploy_batch.reject_name_collisions(db, "azure_deploy", names)
    await deploy_batch.enforce_admission(
        "azure:vm:deploy",
        requests=deploy_batch.batch_request_docs(
            {"region": loc, "instance_type": req.vm_size, "image": req.image_id},
            names),
        actor=current_user, db=db,
    )

    # The runner reconstructs an AzureBulkDeployRequest from the parent, so build one
    # here from the single request. Fields the bulk model doesn't declare (vm_name,
    # count) are dropped; everything it shares — including the PRA overrides — carries.
    bulk = AzureBulkDeployRequest(
        items=[AzureBulkDeployItem(vm_name=n) for n in names],
        **{k: v for k, v in req.model_dump().items()
           if k in AzureBulkDeployRequest.model_fields and k != "items"},
    )

    batch_id = uuid.uuid4().hex[:12]
    children = []
    for name in names:
        job = job_service.create_job(
            db,
            job_type="azure_deploy",
            created_by=current_user.username,
            workgroup=workgroup,
            # `queued`, not `pending`: the runner claims on status='pending', so a
            # pending child would be created a second time alongside its parent.
            status="queued",
            batch_id=batch_id,
            metadata={
                "image_id": req.image_id,
                "vm_name": name,
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
                "register_in_entitle": req.register_in_entitle,
                "register_in_passwordsafe": req.register_in_passwordsafe,
                "ssh_key_secret_override": req.ssh_key_secret_override,
            },
        )
        job_service.set_cloud_resource_id(db, job.id, name)
        job_service.log_audit(
            db, current_user.username, "azure_deploy",
            details={"image_id": req.image_id, "vm_name": name,
                     "workgroup": workgroup, "bulk": True},
        )
        children.append({"job_id": job.id, "vm_name": name})

    parent = job_service.create_job(
        db,
        job_type="azure_bulk_deploy",
        created_by=current_user.username,
        workgroup=workgroup,
        batch_id=batch_id,
        metadata={
            "location": loc,
            "resource_group": rg,
            "workgroup": workgroup,
            "req": bulk.model_dump(),
            "children": children,
        },
    )
    return AzureDeployResponse(
        job_id=parent.id,
        vm_name=names[0],
        message=f"Azure deployment queued for {len(names)} VMs ({names[0]} … {names[-1]})",
        count=len(names),
        batch_id=batch_id,
        job_ids=[c["job_id"] for c in children],
        names=names,
    )


@router.post("/deploy", response_model=AzureDeployResponse)
async def deploy_vm(
    req: AzureDeployRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("azure", "write")),
):
    """
    Deploy one or more Azure VMs from a private image (gallery, managed, or
    marketplace). Returns a job_id trackable at /api/jobs/{job_id} or /ws/jobs/{job_id}.
    ``count > 1`` fans out into a batch (see ``_fan_out_batch``).
    """
    if req.os_type.lower() != "windows" and not req.ssh_public_key.strip():
        raise HTTPException(status_code=400, detail="ssh_public_key is required for Linux deploys.")
    loc = _resolve_location(req.location)
    req.location = loc            # normalise so the runner uses the resolved location
    # Resource group resolves through the chosen region's config set (PR3) so a
    # deploy in a non-default region lands in that region's RG; falls back to the
    # flat azure_resource_group for single-region setups.
    rg = req.resource_group or _rg_for(loc)
    await _reject_cross_region_network(req.subnet_id, req.nsg_ids, loc)
    workgroup = _validate_workgroup(db, current_user, req.workgroup)
    req.workgroup = workgroup
    await _validate_ssh_key_override(req.ssh_key_secret_override)

    if req.count > 1:
        return await _fan_out_batch(req, db, current_user,
                                    loc=loc, rg=rg, workgroup=workgroup)

    # Pre-action policy gate (inert unless enabled + this action is gated).
    from ..services import admission_service
    admission_service.enforce(
        "azure:vm:deploy",
        request={"region": loc, "instance_type": req.vm_size,
                 "image": req.image_id, "name": req.vm_name,
                 "count": 1, "batch": False},
        actor=current_user, db=db,
    )

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
            # Full request so the runner can rebuild the deploy call. Only secret
            # *references* live on it, resolved at deploy time — same shape as the
            # OCI and Packer jobs.
            "req": req.model_dump(),
        },
    )
    job_service.set_cloud_resource_id(db, job.id, req.vm_name)

    job_service.log_audit(
        db, current_user.username, "azure_deploy",
        details={"image_id": req.image_id, "vm_name": req.vm_name, "workgroup": workgroup},
    )

    return AzureDeployResponse(
        job_id=job.id,
        vm_name=req.vm_name,
        message=f"Azure VM deployment queued: {req.vm_name}",
    )


# ── Bulk Deploy ───────────────────────────────────────────────────────────────

@router.post("/bulk-deploy", response_model=AzureBulkDeployResponse)
async def bulk_deploy_vms(
    req: AzureBulkDeployRequest,
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

    loc = _resolve_location(req.location)
    req.location = loc            # normalise so the runner uses the resolved location
    # Region-aware RG resolution (PR3) — see deploy_vm above.
    rg = req.resource_group or _rg_for(loc)
    await _reject_cross_region_network(req.subnet_id, req.nsg_ids, loc)
    workgroup = _validate_workgroup(db, current_user, req.workgroup)
    req.workgroup = workgroup
    await _validate_ssh_key_override(req.ssh_key_secret_override)

    # Bulk means one VM per selected IMAGE, so every item has to resolve to one —
    # either its own or the batch-level default.
    for item in req.items:
        if not (item.image_id or req.image_id):
            raise HTTPException(
                status_code=400,
                detail=f"VM '{item.vm_name}' has no image: set image_id on the item "
                       "or supply a request-level image_id.")

    # Names here are hand-typed per row in the bulk modal, so unlike the count path
    # they can genuinely repeat. Checked before anything is created.
    deploy_batch.reject_name_collisions(
        db, "azure_deploy", [i.vm_name for i in req.items])

    # Policy gate every item BEFORE the first create_job. This route had no gate at
    # all, so allowed_regions / instance_size_caps / prod_window silently did not
    # apply to batches. Per item because each carries its own image.
    await deploy_batch.enforce_admission(
        "azure:vm:deploy",
        requests=[{"region": loc, "instance_type": req.vm_size,
                   "image": item.image_id or req.image_id, "name": item.vm_name,
                   "count": len(req.items), "batch": True}
                  for item in req.items],
        actor=current_user, db=db,
    )

    # One job row per VM so callers get all job IDs immediately.
    #
    # Created ``queued``, not ``pending``: the parent azure_bulk_deploy job below drives
    # them, and the runner claims on `status='pending' AND job_type IN HANDLED_TYPES`.
    # Left pending they would be claimed and deployed a second time, concurrently with
    # the parent — every VM created twice.
    batch_id = uuid.uuid4().hex[:12]
    job_items = []
    children = []
    for item in req.items:
        # Resolve the per-item image once, here, so the runner never has to re-derive
        # the fallback and the child row records what was actually used.
        resolved = {
            "image_id":        item.image_id or req.image_id,
            "image_publisher": item.image_publisher if item.image_publisher is not None else req.image_publisher,
            "image_offer":     item.image_offer if item.image_offer is not None else req.image_offer,
            "image_sku":       item.image_sku if item.image_sku is not None else req.image_sku,
            "image_version":   item.image_version if item.image_version is not None else req.image_version,
            "os_type":         item.os_type or req.os_type,
            "trusted_launch":  req.trusted_launch if item.trusted_launch is None else item.trusted_launch,
        }
        job = job_service.create_job(
            db,
            job_type="azure_deploy",
            created_by=current_user.username,
            workgroup=workgroup,
            status="queued",
            batch_id=batch_id,
            metadata={
                "image_id": resolved["image_id"],
                "vm_name": item.vm_name,
                "vm_size": req.vm_size,
                "location": loc,
                "resource_group": rg,
                "subnet_id": req.subnet_id,
                "nsg_ids": req.nsg_ids,
                "create_public_ip": req.create_public_ip,
                "os_type": resolved["os_type"],
                "trusted_launch": resolved["trusted_launch"],
                "ssh_username": req.ssh_username,
                "workgroup": workgroup,
                "bulk": True,
                # Parity with the AWS children. The runner reads these off the parent's
                # req blob today, so this changes no behaviour — it is what the jobs UI
                # and any future per-child rerun would read.
                "register_in_entitle": req.register_in_entitle,
                "register_in_passwordsafe": req.register_in_passwordsafe,
                "ssh_key_secret_override": req.ssh_key_secret_override,
            },
        )
        job_service.set_cloud_resource_id(db, job.id, item.vm_name)
        job_service.log_audit(
            db, current_user.username, "azure_deploy",
            details={"image_id": resolved["image_id"], "vm_name": item.vm_name,
                     "workgroup": workgroup, "bulk": True},
        )
        job_items.append((job.id, item.vm_name))
        children.append({"job_id": job.id, "vm_name": item.vm_name, **resolved})

    # One parent job for the whole batch — the runner claims this, and it drives the
    # queued children above, sharing a single ACI Jumpoint container across them.
    job_service.create_job(
        db,
        job_type="azure_bulk_deploy",
        created_by=current_user.username,
        workgroup=workgroup,
        batch_id=batch_id,
        metadata={
            "location": loc,
            "resource_group": rg,
            "workgroup": workgroup,
            "req": req.model_dump(),
            "children": children,
        },
    )

    return AzureBulkDeployResponse(
        jobs=[AzureDeployResponse(job_id=jid, vm_name=vn) for jid, vn in job_items],
        batch_id=batch_id,
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
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("azure", "delete")),
):
    """Terminate a dashboard-deployed Azure VM and clean up NIC/PIP.

    The normal path matches the VM's completed ``azure_deploy`` job (and marks it
    ``destroyed``). VDI **pool seats** (deployed via the desktop-pool path, no
    ``azure_deploy`` job) and cloud-recovered VMs ("deployed by: unknown") have no
    such job — for those we FALL BACK: confirm the VM still exists in Azure and
    terminate it anyway, so the Azure-tab Destroy button isn't a dead 404."""
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
        return await _destroy_without_deploy_job(vm_name, db, current_user)

    # Resolve the resource group here and persist it: the runner rebuilds the call from
    # metadata, and re-deriving it there would read whatever `azure_resource_group` is
    # configured at run time rather than the group the VM was actually deployed into.
    rg = deploy_job.metadata_dict.get("resource_group") or _rg()

    destroy_job = job_service.create_job(
        db,
        job_type="azure_destroy",
        created_by=current_user.username,
        metadata={"vm_name": vm_name, "deploy_job_id": deploy_job.id, "resource_group": rg},
    )

    job_service.log_audit(
        db, current_user.username, "azure_destroy",
        details={"vm_name": vm_name},
    )

    return {"job_id": destroy_job.id, "status": "pending", "message": f"Azure VM '{vm_name}' termination queued"}


async def _destroy_without_deploy_job(
    vm_name: str, db: Session, current_user: User,
) -> dict:
    """Fallback destroy for VMs that have no completed ``azure_deploy`` job — VDI
    pool seats and cloud-recovered ("unknown") VMs.

    Resolve the resource group (prefer the RG parsed from a matching desktop-seat's
    ARM ``vm_resource_id``; else the configured ``azure_resource_group`` — the
    sandbox keeps every VM in one RG), confirm the VM actually exists in Azure
    (keep the 404 when it's genuinely gone), then terminate it via the same
    ``_run_destroy`` task with no deploy job. If a desktop-seat row backs this VM,
    drop it (and its PRA RDP jump) so destroying the VM here doesn't strand the
    seat row + jump."""
    from ..services import vdesktop_service

    # Prefer the seat's full ARM id for the RG (more reliable than the flat config).
    seat_rg = None
    seat = (
        db.query(VirtualDesktop)
        .filter(
            (VirtualDesktop.vm_resource_id == vm_name)
            | VirtualDesktop.vm_resource_id.like("%/" + vm_name)
        )
        .first()
    )
    if seat is not None and seat.vm_resource_id and "/" in seat.vm_resource_id:
        seat_rg, _ = vdesktop_service._parse_vm_id(seat.vm_resource_id)
    rg = seat_rg or _rg()

    try:
        vm = await azure_service.get_vm(rg, vm_name)
    except AzureError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    if vm is None:
        raise HTTPException(
            status_code=404,
            detail=f"No active deployment found for VM '{vm_name}'. "
                   "It may have already been terminated or was not deployed from this dashboard.",
        )

    # Best-effort: drop the desktop-seat row (+ its PRA RDP jump) before terminating
    # the VM, so the Azure-tab Destroy doesn't leave an orphaned seat / stranded jump.
    try:
        await vdesktop_service.drop_seat_by_vm(db, vm_name)
    except Exception as exc:
        logger.warning("desktop seat cleanup failed for Azure-tab destroy vm=%s: %s", vm_name, exc)

    destroy_job = job_service.create_job(
        db,
        job_type="azure_destroy",
        created_by=current_user.username,
        metadata={"vm_name": vm_name, "resource_group": rg, "deploy_job_id": None},
    )
    job_service.log_audit(
        db, current_user.username, "azure_destroy",
        details={"vm_name": vm_name, "fallback": True},
    )
    # No deploy job to fetch/mark-destroyed: _run_destroy treats a missing
    # deploy_job_id as "nothing extra to clean up" and still terminates the VM.
    return {"job_id": destroy_job.id, "status": "pending", "message": f"Azure VM '{vm_name}' termination queued"}


# ── Create image from VM ──────────────────────────────────────────────────────

@router.post("/vms/{vm_name}/create-image")
async def create_image_from_vm(
    vm_name: str,
    req: AzureCreateImageRequest,
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
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("azure", "write")),
):
    """Manually export a managed image to VHD on the hub backend and register
    it in the image registry. Useful when the auto-export during build was
    skipped or failed."""
    rg = req.resource_group or _rg()
    job = job_service.create_job(
        db,
        job_type="azure_export_image",
        created_by=current_user.username,
        metadata={"image_name": image_name, "registry_name": req.image_name,
                  "resource_group": rg, "os_type": req.os_type,
                  "created_by": current_user.username},
    )
    job_service.log_audit(
        db, current_user.username, "azure_export_image",
        details={"image_name": image_name, "registry_name": req.image_name},
    )

    # Enqueued as a pending job; the worker container claims + runs it
    # (survives gunicorn worker recycling, unlike an in-app BackgroundTask).
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









