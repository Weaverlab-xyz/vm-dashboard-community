"""Azure VM deploy / destroy / image execution, run by the job runner.

Dispatched by ``jobs_worker`` as ``azure_deploy``, ``azure_bulk_deploy``,
``azure_destroy`` and ``azure_create_image``. ``api/azure`` keeps the route handlers,
which validate, persist the request on the job and return; the worker reconstructs the
call from that metadata and runs it here, so a gunicorn recycle mid-deploy no longer
strands the job or orphans the VM.

Lives in ``services/`` because the job runner has to import it, and a worker reaching
into the API package is backwards (see services/aws_vm_service for the AWS counterpart).
"""
import asyncio
import logging

from ..config import settings
from ..database import Job
from ..models.azure import (
    AzureBulkDeployRequest,
    AzureCreateImageRequest,
    AzureDeployRequest,
)
from . import azure_service, cache_service, job_service
from .azure_service import AzureError

logger = logging.getLogger(__name__)


def _cfg(key: str, fallback: str = "") -> str:
    """Read a value from config_service (DB/wizard) with env-var fallback.

    Own copy rather than an import from ``api.azure`` — that is the dependency this
    module exists to remove. Copied verbatim, including the ``getattr(settings, key,
    fallback)`` form: AWS/GCP/OCI use ``getattr(..., None) or fallback``, which differs
    when the setting is an empty string. Harmonizing them is a behavior change and does
    not belong inside a relocation."""
    from ..services import config_service
    return config_service.get(key) or getattr(settings, key, fallback)


def _rg():
    return _cfg("azure_resource_group") or "vm-cli-rg"


# ── Job-runner entry point ────────────────────────────────────────────────────

async def run(job_id: str, job_type: str, meta: dict) -> None:
    """Run one Azure job. Every argument comes from the metadata the endpoint
    persisted — the worker has no request object to hand over."""
    if job_type == "azure_deploy":
        req = AzureDeployRequest(**meta["req"])
        await _run_deploy(job_id, req, meta["resource_group"], meta["location"])
    elif job_type == "azure_bulk_deploy":
        # Children were created ``queued`` so the runner cannot claim them alongside
        # this parent; _run_bulk_deploy drives them and owns their status. It unpacks
        # each entry as a plain (job_id, vm_name) pair — unlike the AWS runner, which
        # takes an object per item.
        req = AzureBulkDeployRequest(**meta["req"])
        job_items = [(c["job_id"], c["vm_name"]) for c in meta["children"]]
        await _run_bulk_deploy(job_items, req, meta["resource_group"], meta["location"])
    elif job_type == "azure_destroy":
        await _run_destroy(job_id, meta.get("deploy_job_id") or "",
                           meta["vm_name"], meta["resource_group"])
    elif job_type == "azure_create_image":
        # The endpoint stores the image name under "image_name"; the model field is
        # "name". `generalize` defaults False on the model — don't invent True here.
        await _run_create_image(job_id, meta["vm_name"], meta["resource_group"],
                                AzureCreateImageRequest(
                                    name=meta["image_name"],
                                    description=meta.get("description") or "",
                                    generalize=meta.get("generalize", False)))


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
def _aci_rg():
    return _cfg("azure_aci_resource_group") or _rg()
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
                    # ACI jumpoint params come from config_service (the wizard /
                    # sandbox write them to the DB) — NOT the Pydantic settings
                    # object, whose env-var defaults are empty here. An empty
                    # subnet_id creates the jumpoint OUTSIDE the VNet, so it has no
                    # route to the VM's private IP and the SSH Shell Jump times out
                    # (credential rotation still works — that's the control plane).
                    # Same fix class as the jump-group/jumpoint-name resolution below.
                    subnet_id=_cfg("azure_aci_subnet_id") or settings.azure_aci_subnet_id,
                    image=_cfg("azure_aci_jumpoint_image"),
                    cpu=float(_cfg("azure_aci_cpu") or settings.azure_aci_cpu),
                    memory=float(_cfg("azure_aci_memory") or settings.azure_aci_memory),
                    deploy_key=deploy_key,
                    acr_server=acr_server,
                    acr_username=acr_username,
                    acr_password=acr_password,
                    storage_account=_cfg("azure_aci_storage_account") or settings.azure_aci_storage_account,
                    storage_account_rg=_cfg("azure_aci_storage_account_rg") or settings.azure_aci_storage_account_rg,
                    file_share=_cfg("azure_aci_file_share") or settings.azure_aci_file_share,
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
        from ..services import entitle_vm_hook, config_service
        if (getattr(req, "register_in_entitle", False) and not is_windows
                and entitle_vm_hook.registration_enabled()):
            # Resolved login user (config override → request field → cloud default).
            # config_service.get is store-only so it doesn't shadow a blank request
            # field via the settings default (see the GCP fix, gcp.py).
            await entitle_vm_hook.register(db, job_id, req.vm_name, hostname,
                                           private=not req.create_public_ip,
                                           result=result, tag="Azure",
                                           sudo_user=config_service.get("azure_ssh_username") or req.ssh_username or "azureuser",
                                           ssh_key_secret=req.ssh_key_secret_override or "")

        # Step 5: Password Safe — onboard as a managed system + account (Linux only).
        from ..services import ps_vm_hook
        if (getattr(req, "register_in_passwordsafe", False) and not is_windows
                and ps_vm_hook.registration_enabled()):
            await ps_vm_hook.register(db, job_id, req.vm_name, hostname,
                                      result=result, tag="Azure",
                                      ssh_key_secret=req.ssh_key_secret_override or "",
                                      resource_group=rg)

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
                    # ACI jumpoint params come from config_service (the wizard /
                    # sandbox write them to the DB) — NOT the Pydantic settings
                    # object, whose env-var defaults are empty here. An empty
                    # subnet_id creates the jumpoint OUTSIDE the VNet, so it has no
                    # route to the VM's private IP and the SSH Shell Jump times out
                    # (credential rotation still works — that's the control plane).
                    # Same fix class as the jump-group/jumpoint-name resolution below.
                    subnet_id=_cfg("azure_aci_subnet_id") or settings.azure_aci_subnet_id,
                    image=_cfg("azure_aci_jumpoint_image"),
                    cpu=float(_cfg("azure_aci_cpu") or settings.azure_aci_cpu),
                    memory=float(_cfg("azure_aci_memory") or settings.azure_aci_memory),
                    deploy_key=deploy_key,
                    acr_server=acr_server,
                    acr_username=acr_username,
                    acr_password=acr_password,
                    storage_account=_cfg("azure_aci_storage_account") or settings.azure_aci_storage_account,
                    storage_account_rg=_cfg("azure_aci_storage_account_rg") or settings.azure_aci_storage_account_rg,
                    file_share=_cfg("azure_aci_file_share") or settings.azure_aci_file_share,
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
                from ..services import entitle_vm_hook, config_service
                if (getattr(req, "register_in_entitle", False) and not is_windows
                        and entitle_vm_hook.registration_enabled()):
                    # Resolved login user (config override → request field → cloud
                    # default); config_service.get is store-only (see the GCP fix).
                    await entitle_vm_hook.register(db, job_id, vm_name, hostname,
                                                   private=not req.create_public_ip,
                                                   result=result, tag="Azure",
                                                   sudo_user=config_service.get("azure_ssh_username") or req.ssh_username or "azureuser",
                                                   ssh_key_secret=req.ssh_key_secret_override or "")

                # Step 5: Password Safe — onboard as a managed system + account.
                from ..services import ps_vm_hook
                if (getattr(req, "register_in_passwordsafe", False) and not is_windows
                        and ps_vm_hook.registration_enabled()):
                    await ps_vm_hook.register(db, job_id, vm_name, hostname,
                                              result=result, tag="Azure",
                                              ssh_key_secret=req.ssh_key_secret_override or "",
                                              resource_group=rg)

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

            # Off-board the Password Safe managed system if this deploy registered one.
            if meta.get("ps_registration_tf_state"):
                from ..services import ps_vm_hook
                await ps_vm_hook.deregister(meta, result)

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
