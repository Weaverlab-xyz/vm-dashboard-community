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
from typing import Optional

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
        # this parent; _run_bulk_deploy drives them and owns their status.
        req = AzureBulkDeployRequest(**meta["req"])
        job_items = [(c["job_id"], _AzureBulkItem.from_child(c, req))
                     for c in meta["children"]]
        await _run_bulk_deploy(job_items, req, meta["resource_group"], meta["location"])
        # The parent has no terminal status of its own — the worker only marks a job
        # failed when dispatch raises, so without this it stays `running` at 0%.
        _finish_parent(job_id, [c["job_id"] for c in meta["children"]])
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


class _AciRef:
    """How a deploy reached its BeyondTrust Gateway.

    ``mode`` is the part that matters, and there are three shapes:

      * ``owned``  — this deploy created its own ACI container group and must stop it
        if the VM create fails.
      * ``shared`` — a batch owns the ACI group, and one VM failing must leave it
        running for its siblings.
      * ``host``   — this deploy BORROWED the ref-counted ``clouddb-jumpoint`` VM that
        ``jumpoint_host_service`` owns on behalf of cloud databases, k8s tunnels and
        VDI seats. It must only release a reference; the teardown decision belongs to
        that service.

    Mirrors ``gcp_vm_service._JumpointRef``, where the same paired-vs-shared
    distinction is what stops a VM destroy deleting the ref-counted host.

    ``deploy_key_note`` is carried rather than dropped: the batch path used to assign it
    and never read it, so an ACI built without a deploy key left no trace anywhere an
    operator would look.
    """
    __slots__ = ("mode", "group_name", "error", "deploy_key_note", "host_id", "region")

    def __init__(self, mode: str, group_name: str = "", error: str = "",
                 deploy_key_note: str = "", host_id: str = "", region: str = ""):
        self.mode, self.group_name = mode, group_name
        self.error, self.deploy_key_note = error, deploy_key_note
        self.host_id, self.region = host_id, region

    @property
    def owned(self) -> bool:
        """True when tearing this group down on failure is this deploy's business."""
        return self.mode == "owned" and bool(self.group_name)

    def record(self, result: dict) -> None:
        """Write this reference onto the deploy job's result metadata.

        A borrowed host is NEVER recorded under ``aci_group_name``. That key is what
        drives the ACI teardown in ``_run_destroy`` — and, worse, an absent
        ``aci_group_name`` there triggers a sweep that stops *every* dashboard ACI
        group. Recording the shared host under its own key keeps both paths honest.
        """
        if self.mode == "host":
            result["jumpoint_mode"] = "host"
            if self.host_id:
                result["jumpoint_host_id"] = self.host_id
                result["jumpoint_region"] = self.region
            elif self.error:
                result["jumpoint_error"] = self.error
            return
        if self.group_name:
            result["aci_group_name"] = self.group_name
        elif self.error:
            result["aci_error"] = self.error

    def note(self) -> str:
        """The progress line describing the outcome, in any mode."""
        if self.mode == "host":
            if self.host_id:
                return (f"Using the shared BeyondTrust Gateway host ({self.host_id}), "
                        "deploying VM…")
            return (f"Shared Gateway host unavailable (non-fatal): {self.error}"
                    " — continuing with VM deploy…")
        if self.group_name:
            verb = "started" if self.mode == "owned" else "shared by this batch"
            return f"ACI Gateway {verb} ({self.group_name}){self.deploy_key_note}, deploying VM…"
        if self.error:
            return (f"ACI Gateway failed (non-fatal): {self.error}{self.deploy_key_note}"
                    " — continuing with VM deploy…")
        return "Preparing Azure VM deploy…"


async def _acquire_aci(db, progress_job_id: str, req, loc: str, *,
                       count: int = 1, mode: str = "owned") -> _AciRef:
    """Start one ACI Gateway container group, for a single deploy or a whole batch.

    ``mode`` says who is responsible for stopping it: ``owned`` for a single deploy,
    ``shared`` for a batch, where one VM's failure must leave the group running for its
    siblings.

    Best-effort: a failure is recorded and the VM deploy continues, because the operator
    may already have a Gateway reaching that VNet."""
    scope = f" for {count}-VM batch" if count > 1 else " container"
    job_service.update_progress(
        db, progress_job_id, 10, f"Starting BeyondTrust ACI Gateway{scope}…")
    deploy_key_note = ""
    try:
        try:
            # Per-request override wins, as on every other cloud. The batch path used to
            # skip this and quietly build its ACI with the configured default key.
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
        group = await azure_service.run_aci_jumpoint_task(
            rg=_aci_rg(),
            location=loc,
            # ACI gateway params come from config_service (the wizard / sandbox write
            # them to the DB) — NOT the Pydantic settings object, whose env-var defaults
            # are empty here. An empty subnet_id creates the gateway OUTSIDE the VNet,
            # so it has no route to the VM's private IP and the SSH Shell Jump times out
            # (credential rotation still works — that's the control plane).
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
        ref = _AciRef(mode, group_name=group, deploy_key_note=deploy_key_note)
    except Exception as e:
        ref = _AciRef(mode, error=str(e), deploy_key_note=deploy_key_note)
    job_service.update_progress(db, progress_job_id, 15, ref.note())
    return ref


def _aci_requested(req) -> bool:
    """Whether this single deploy should start its own ACI Gateway container group.

    Default is False — singles borrow the shared ``clouddb-jumpoint`` VM instead, which
    mirrors ``gcp_vm_service._paired_requested`` and fixes two things ACI can't:

      * ACI is serverless and cannot do protocol tunneling (no NET_ADMIN / NET_RAW /
        IPC_LOCK, no ``/dev/net/tun``), so an ACI-brokered VM gets a Shell Jump but
        never a Protocol Tunnel. The shared VM host runs the container privileged.
      * Every ACI group gets a random name but they all mount ONE ``/jpt`` Azure File
        share, which is the Gateway's persistent identity store. Successive groups
        fought over that one install; once the ``.installed-<key-hash>`` marker and the
        install on disk disagreed, the container crash-looped (ExitCode 1, no output)
        and never registered with PRA at all.

    A request carrying its own Gateway deploy key still gets an ACI group: the shared
    host serves many resources and resolves its key from config, so there is nowhere to
    honour a per-deploy override on it. Silently ignoring the form field would be the
    "succeeds with a side effect missing" failure this codebase keeps writing tests
    against, so the override wins and that VM gets its own container."""
    if getattr(req, "docker_deploy_key_ref", None):
        logger.info("Azure deploy carries a docker_deploy_key_ref — using a dedicated ACI "
                    "Gateway so the per-deploy key is honoured")
        return True
    return (_cfg("azure_vm_jumpoint_mode") or "shared").strip().lower() == "aci"


async def _acquire_shared_host(db, progress_job_id: str, loc: str) -> _AciRef:
    """Borrow the ref-counted Azure VM Gateway that cloud databases, k8s tunnels and
    VDI seats already share — one host for every VM instead of one container per deploy.

    Best-effort like the ACI path: the VM still launches when the gateway is
    unavailable, it just has no Shell Jump until one exists."""
    from ..services import jumpoint_host_service
    job_service.update_progress(
        db, progress_job_id, 10, "Ensuring the shared BeyondTrust Gateway host…")
    try:
        host = await jumpoint_host_service.ensure_jumpoint_host("azure", loc)
    except Exception as e:
        logger.warning("Shared Azure Gateway host unavailable (non-fatal): %s", e)
        ref = _AciRef("host", region=loc, error=str(e))
    else:
        ref = _AciRef("host", host_id=host or "", region=loc,
                      error="" if host else
                            "shared Gateway host unavailable — check azure_resource_group, "
                            "azure_jumpoint_subnet_id and the ACI deploy key in the wizard")
    job_service.update_progress(db, progress_job_id, 15, ref.note())
    return ref


def _child_request(item: "_AzureBulkItem", req: AzureBulkDeployRequest) -> AzureDeployRequest:
    """Project one bulk item onto the single-deploy model.

    Everything the two models share is copied straight over; the per-item image fields
    and the VM name override it. That keeps ``_run_deploy`` taking exactly one request
    type, so there is only ever one body deploying an Azure VM.

    The item's values have already been resolved against the batch request by
    ``_AzureBulkItem.from_child``, which is the back-compat hinge for count fan-out
    children — those carry only ``vm_name`` and inherit the rest."""
    shared = {k: v for k, v in req.model_dump().items()
              if k in AzureDeployRequest.model_fields and k != "vm_name"}
    shared.update(
        vm_name=item.vm_name,
        image_id=item.image_id,
        image_publisher=item.image_publisher,
        image_offer=item.image_offer,
        image_sku=item.image_sku,
        image_version=item.image_version,
        os_type=item.os_type,
        trusted_launch=item.trusted_launch,
        count=1,
    )
    return AzureDeployRequest(**shared)


class _AzureBulkItem:
    """The per-VM fields ``_run_bulk_deploy`` reads off each child.

    Mirrors ``aws_vm_service._BulkItem``. An object rather than a widened tuple
    because ``(job_id, vm_name)`` was unpacked in four places, and because bulk now
    genuinely means one VM *per selected image* — the image is per item, not per batch.
    """
    __slots__ = ("vm_name", "image_id", "image_publisher", "image_offer",
                 "image_sku", "image_version", "os_type", "trusted_launch")

    def __init__(self, vm_name: str, image_id: str = "", image_publisher=None,
                 image_offer=None, image_sku=None, image_version=None,
                 os_type: str = "Linux", trusted_launch: bool = False):
        self.vm_name, self.image_id = vm_name, image_id
        self.image_publisher, self.image_offer = image_publisher, image_offer
        self.image_sku, self.image_version = image_sku, image_version
        self.os_type, self.trusted_launch = os_type, trusted_launch

    @classmethod
    def from_child(cls, child: dict, req) -> "_AzureBulkItem":
        """Build from a child metadata entry, falling back to the batch-level request.

        The fallbacks are the back-compat hinge: an ``azure_bulk_deploy`` parent
        created before per-item images existed carries children with only ``vm_name``,
        and those still resolve every field off ``req`` and run to completion."""
        def pick(key, default=None):
            value = child.get(key)
            return getattr(req, key, default) if value is None else value

        return cls(
            vm_name=child["vm_name"],
            image_id=pick("image_id", "") or "",
            image_publisher=pick("image_publisher"),
            image_offer=pick("image_offer"),
            image_sku=pick("image_sku"),
            image_version=pick("image_version"),
            os_type=pick("os_type", "Linux") or "Linux",
            trusted_launch=bool(pick("trusted_launch", False)),
        )


async def _resolve_azure_aci_deploy_key() -> str:
    """Return the BeyondTrust Gateway Docker deploy key for Azure ACI launches.

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


def _finish_parent(parent_job_id: str, child_job_ids: list) -> None:
    """Give the batch parent a terminal status once its children are done.

    Own session: `run()` holds none, and the per-child sessions are opened and closed
    inside `_run_deploy`. Best-effort — a parent left `running` is cosmetic next to a
    batch whose VMs all exist."""
    db = _get_db_session()
    try:
        job_service.finish_batch_parent(db, parent_job_id, child_job_ids)
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not close out batch parent %s: %s", parent_job_id, exc)
    finally:
        db.close()


async def _run_deploy(job_id: str, req: AzureDeployRequest, rg: str, loc: str, *,
                      aci: Optional[_AciRef] = None,
                      ssh_public_key: Optional[str] = None,
                      quota_checked: bool = False):
    """Deploy one Azure VM.

    The keyword-only arguments are injected by ``_run_bulk_deploy`` so a batch does the
    quota check, the ACI Gateway and the Key Vault read once instead of once per VM.
    Left at their defaults — the single-deploy path — this does all three itself.

    This is the only place an Azure VM is created. The batch path used to be a second
    copy of this body, and drifted: it ignored ``docker_deploy_key_ref``, dropped the
    ACI outcome message, and never surfaced a failed deploy-key fetch."""
    db = _get_db_session()
    result = {}
    is_windows = req.os_type.lower() == "windows"
    try:
        job_service.set_running(db, job_id)

        # Step 0: Quota check — fail fast before anything is created, including the
        # Windows password below. (The single path used to vault a password first and
        # orphan it when the quota check then failed.)
        if not quota_checked:
            job_service.update_progress(db, job_id, 5, f"Checking Azure quota in {loc}…")
            await azure_service.check_vm_quota(loc, req.vm_size)

        # Step 1: BeyondTrust Gateway — the shared ref-counted VM host by default, or a
        # dedicated ACI container group when the operator asked for one (or supplied a
        # per-deploy key). See _aci_requested for why shared is the default.
        if settings.pra_enabled:
            if aci is None:
                aci = (await _acquire_aci(db, job_id, req, loc) if _aci_requested(req)
                       else await _acquire_shared_host(db, job_id, loc))
            else:
                # The batch started one group for the whole run.
                job_service.update_progress(db, job_id, 15, aci.note())
            aci.record(result)
        else:
            aci = _AciRef("none")
            job_service.update_progress(db, job_id, 15, "Preparing Azure VM deploy…")

        # Step 2: Windows gets a generated admin password, vaulted before the VM is
        # created — a VM whose password can't be retrieved is useless.
        admin_password = ""
        if is_windows:
            job_service.update_progress(db, job_id, 30, "Generating Windows admin password…")
            admin_password = azure_service.generate_windows_admin_password()
            backend, ref = await asyncio.to_thread(
                azure_service.store_windows_admin_password, req.vm_name, job_id[:8], admin_password,
            )
            # Reference only — job metadata is visible via the jobs API.
            result["admin_username"] = req.ssh_username
            result["admin_password_backend"] = backend
            result["admin_password_ref"] = ref

        # Step 3: Deploy Azure VM (3-step: PIP → NIC → VM)
        job_service.update_progress(db, job_id, 35, f"Creating Azure VM '{req.vm_name}'…")
        if ssh_public_key is None:
            ssh_public_key = await _effective_ssh_public_key(req)
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
                ssh_public_key=ssh_public_key,
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
            # Only tear the group down if this deploy created it. In a batch it is
            # shared, and stopping it here would strand every sibling still to come.
            if aci.owned:
                try:
                    await azure_service.stop_aci_jumpoint_task(_aci_rg(), aci.group_name)
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
        if settings.pra_enabled and is_windows:
            job_service.update_progress(
                db, job_id, 90,
                "Windows VM deployed — Shell Jump (SSH) skipped; broker access with an "
                "RDP jump item on the Gateway. Password: Azure → VMs → Password."
            )
        elif settings.pra_enabled:
            from ..services import terraform_pra_service
            # Resolve from config_service (wizard/DB) first, then env-var defaults.
            # Azure-specific keys override the shared bt_* keys.
            from ..services import config_service as _cs
            jump_group = (getattr(req, "jump_group", None) or "").strip() or _cfg("azure_bt_jump_group_name") or _cfg("bt_jump_group_name")
            jumpoint_name = (getattr(req, "jumpoint_name", None) or "").strip() or _cfg("azure_jumpoint_name") or _cfg("bt_jumpoint_name")
            _cred = getattr(req, "pra_credential_ref", None)
            _client_secret = _cs.resolve_reference(_cred.strip()) if _cred else ""
            if result.get("jumpoint_host_id"):
                aci_note = f" (shared Gateway host: {result['jumpoint_host_id']})"
            elif result.get("jumpoint_error"):
                aci_note = f" (shared Gateway host failed: {result['jumpoint_error']})"
            elif result.get("aci_group_name"):
                aci_note = f" (ACI: {result['aci_group_name']})"
            elif result.get("aci_error"):
                aci_note = f" (ACI failed: {result['aci_error']})"
            else:
                aci_note = " (no Gateway)"
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
    """Deploy a batch of Azure VMs behind one ACI Gateway.

    Checks the quota, starts the container group and reads the Key Vault key ONCE for
    the whole run, then hands all three to ``_run_deploy`` per VM.

    Thin on purpose. This was a 210-line near-copy of ``_run_deploy`` and had drifted:
    it ignored ``docker_deploy_key_ref``, assigned a ``deploy_key_note`` it never read,
    emitted no ACI outcome at all, and wrapped the run in a handler that could mark
    completed VMs failed. ``_run_deploy`` owns set_running / set_completed / set_failed
    and cache invalidation per VM, so one failure fails that row alone."""
    # job_items[0] below is otherwise an IndexError on an empty batch, which the setup
    # handler would then swallow into a loop over nothing — leaving the parent job
    # running with no explanation. Matches oci_vm_service._run_bulk_deploy.
    if not job_items:
        return

    db = _get_db_session()
    try:
        first_job_id = job_items[0][0]

        # Quota is a batch-level question: vm_size is shared, so one check covers the
        # run and fails it before anything is created.
        job_service.update_progress(db, first_job_id, 5, f"Checking Azure quota in {loc}…")
        await azure_service.check_vm_quota(loc, req.vm_size)

        # mode="shared": one VM failing must not stop the container the rest still need.
        aci = (await _acquire_aci(db, first_job_id, req, loc,
                                  count=len(job_items), mode="shared")
               if settings.pra_enabled else _AciRef("none"))

        # One Key Vault round trip for the batch, not one per VM.
        batch_ssh_public_key = await _effective_ssh_public_key(req)
    except Exception as e:
        # Batch SETUP failed, so nothing has been created — failing every child is the
        # right answer here, and only here. Past this point children have completed,
        # and job_service.set_failed has no status guard.
        for job_id, _ in job_items:
            job_service.set_failed(db, job_id, f"Bulk deploy error: {e}")
        return
    finally:
        db.close()

    for job_id, item in job_items:
        await _run_deploy(
            job_id,
            _child_request(item, req),
            rg,
            loc,
            aci=aci,
            ssh_public_key=batch_ssh_public_key,
            quota_checked=True,
        )

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

            # Stop ACI Gateway — only if no other active VMs share this container group.
            # A shared-host deploy carries no aci_group_name (see _AciRef.record), so it
            # falls straight through to the release below.
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
                        db, destroy_job_id, 50, "Stopping Gateway ACI container…"
                    )
                    try:
                        await azure_service.stop_aci_jumpoint_task(_aci_rg(), aci_group_name)
                        result["aci_group_stopped"] = aci_group_name
                    except AzureError as e:
                        result["aci_error"] = str(e)
                else:
                    job_service.update_progress(
                        db, destroy_job_id, 50,
                        f"ACI Gateway shared with {sibling_count} other active VM(s) — leaving running…"
                    )
                    result["aci_group_shared"] = aci_group_name

            # Fallback: if no metadata-tracked ACI and no other active VMs remain,
            # enumerate and stop all dashboard ACI gateways (covers untracked
            # containers). Only ever stops ACI groups — the shared clouddb-jumpoint VM
            # is not an ACI group and list_aci_tasks cannot see it, so a shared-host
            # deploy landing here sweeps orphans without touching its own Gateway.
            if not aci_group_name and not active_sibling_jobs:
                job_service.update_progress(
                    db, destroy_job_id, 50, "No active VMs remain — checking for orphaned ACI Gateways…"
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

            # Release the borrowed shared Gateway host, if that is how this deploy
            # reached PRA. Deliberately AFTER the `destroyed` flag above:
            # teardown_jumpoint_host_if_idle counts live rows and takes no "exclude me"
            # argument, so releasing first would let the row being destroyed count
            # itself and the host would never be reclaimed. gcp_vm_service and
            # aws_vm_service order it the same way for the same reason.
            #
            # Rows written before jumpoint_mode existed can only be ACI: nothing ever
            # wrote jumpoint_host_id on an azure_deploy, and the only writer of
            # aci_group_name was the ACI path. The inference is total, so this needs no
            # backfill and existing VMs tear down exactly as they do today.
            if meta.get("jumpoint_mode") == "host":
                # Drop this VM's reference and let jumpoint_host_service decide. It
                # counts Azure VMs, cloud databases, k8s tunnels and VDI seats, so the
                # host survives as long as anything still needs it.
                region = meta.get("jumpoint_region") or _cfg("azure_location")
                job_service.update_progress(
                    db, destroy_job_id, 95,
                    "Releasing the shared BeyondTrust Gateway host…")
                try:
                    from ..services import jumpoint_host_service
                    await jumpoint_host_service.teardown_jumpoint_host_if_idle(
                        db, "azure", region)
                except Exception as e:
                    logger.warning("Shared Gateway host release failed (non-fatal): %s", e)
                    result["jumpoint_host_teardown_error"] = str(e)

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
        await cache_service.invalidate_prefix("azure_images")

    except AzureError as e:
        job_service.set_failed(db, job_id, str(e))
    except Exception as e:
        job_service.set_failed(db, job_id, f"Unexpected error: {e}")
    finally:
        db.close()
