"""AWS EC2 deploy / destroy / image execution, run by the job runner.

Dispatched by ``jobs_worker`` as ``ec2_deploy``, ``ec2_bulk_deploy``, ``ec2_destroy``,
``ec2_create_image`` and ``ami_copy``. ``api/aws`` keeps the route handlers, which
validate, persist the request on the job and return; the worker reconstructs the call
from that metadata and runs it here, so a gunicorn recycle mid-deploy no longer strands
the job or orphans the instance.

Lives in ``services/`` because the job runner has to import it, and a worker reaching
into the API package is backwards (see services/packer_build_service for the same split
on the image-build side).
"""
import asyncio
import logging

from ..config import settings
from ..database import Job
from ..models.aws import CopyAMIRequest, CreateImageRequest
from . import aws_service, cache_service, job_service
from .aws_service import AWSError

logger = logging.getLogger(__name__)


def _aws_cfg(key: str, fallback: str = "") -> str:
    """Read a config key from config_service first, fall back to settings env var.

    Own copy rather than an import from ``api.aws`` — that is the dependency this
    module exists to remove. ``packer_build_service`` carries its ``_cfg`` the same way."""
    from ..services import config_service
    return config_service.get(key) or getattr(settings, key, None) or fallback


def _aws_region() -> str:
    """The configured default region, for the paths that don't carry one on the job.

    Own copy for the same reason as ``_aws_cfg`` above — and it has to exist here at
    all because ``_run_create_image`` and ``_run_ami_copy`` call it. ``_run_deploy``
    and ``_run_bulk_deploy`` instead bind a *local* ``_aws_region`` string from the
    job's persisted region, which is why they were unaffected while these two were
    raising NameError."""
    return _aws_cfg("aws_region") or "us-east-2"


# ── Job-runner entry point ────────────────────────────────────────────────────

async def run(job_id: str, job_type: str, meta: dict) -> None:
    """Run one AWS job. Every argument comes from the metadata the endpoint persisted —
    the worker has no request object to hand over."""
    if job_type == "ec2_deploy":
        await _run_deploy(
            job_id,
            meta["ami_id"],
            meta["instance_name"],
            meta["instance_type"],
            meta.get("subnet_id") or "",
            meta.get("security_group_ids") or [],
            meta["workgroup"],
            meta.get("jump_group") or "",
            meta.get("jumpoint_name") or "",
            meta.get("pra_credential_ref") or "",
            meta["region"],
        )
    elif job_type == "ec2_bulk_deploy":
        # Children were created ``queued`` so the runner cannot claim them alongside
        # this parent; _run_bulk_deploy drives them and owns their status.
        job_items = [(c["job_id"], _BulkItem(c["ami_id"], c["instance_name"]))
                     for c in meta["children"]]
        await _run_bulk_deploy(
            job_items,
            meta["instance_type"],
            meta.get("subnet_id") or "",
            meta.get("security_group_ids") or [],
            meta["workgroup"],
            meta["region"],
            # `.get`, not `[...]`: parent rows created before these were threaded
            # through carry none of them and must stay runnable. Same deliberate
            # optionality tests/test_cloud_deploy_meta documents for the single path.
            meta.get("jump_group") or "",
            meta.get("jumpoint_name") or "",
            meta.get("pra_credential_ref") or "",
        )
    elif job_type == "ec2_destroy":
        await _run_destroy(job_id, meta.get("deploy_job_id") or "",
                           meta["instance_id"], meta["region"])
    elif job_type == "ec2_create_image":
        await _run_create_image(job_id, meta["instance_id"], CreateImageRequest(
            name=meta["name"], description=meta.get("description") or "",
            no_reboot=meta.get("no_reboot", True)))
    elif job_type == "ami_copy":
        await _run_ami_copy(job_id, CopyAMIRequest(
            source_ami_id=meta["source_ami_id"], name=meta["name"],
            description=meta.get("description") or ""))


class _BulkItem:
    """The per-instance fields ``_run_bulk_deploy`` reads off each item. The endpoint
    persists these two; the pydantic model they came from is not reconstructable from
    the job metadata and is not needed — nothing else on it is touched."""
    __slots__ = ("ami_id", "instance_name")

    def __init__(self, ami_id: str, instance_name: str):
        self.ami_id = ami_id
        self.instance_name = instance_name


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
    region: str = "",
):
    db = _get_db_session()
    result = {}
    try:
        job_service.set_running(db, job_id)

        # ── Step 1: Start ECS Jumpoint container first (BeyondTrust only) ─────
        from ..services import config_service as _cfg_svc
        from ..services.region_config import resolve_region
        # Per-deploy region (falls back to the configured default for older callers).
        _aws_region = region or _aws_cfg("aws_region") or "us-east-2"
        # Per-region SSH key secret + SSM instance profile (blank fields / the default
        # region fall back to the flat ec2_* config keys).
        _rc = resolve_region("aws", _aws_region)
        _meta = (db.query(Job).filter(Job.id == job_id).first().metadata_dict or {})
        ssh_secret_name = _meta.get("ssh_key_secret_override") or _rc["ssh_key_secret"]
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

        # ── Step 1b: Ensure the shared on-demand NAT instance (independent of
        # BeyondTrust) so the VM's private subnet gets outbound internet. ────────
        if _cfg_svc.get_bool("aws_nat_instance_enabled"):
            try:
                from ..services import nat_instance_service
                nat_id = await nat_instance_service.ensure_nat_instance(_aws_region)
                if nat_id:
                    result["nat_instance_id"] = nat_id
                    job_service.update_progress(db, job_id, 36, "NAT instance ready (VM egress enabled)…")
            except Exception as e:
                result["nat_error"] = str(e)
                job_service.update_progress(
                    db, job_id, 36,
                    f"NAT ensure failed (non-fatal): {e} — VM may lack outbound internet…")

        # ── Step 1c: Ensure the shared on-demand SSM interface endpoints so a
        # private-subnet VM can reach the SSM control plane (Password Safe SSM
        # onboarding). Best-effort — a failure never blocks the deploy. ─────────
        if _cfg_svc.get_bool("aws_ssm_endpoints_enabled"):
            try:
                from ..services import ssm_endpoint_service
                ssm_eps = await ssm_endpoint_service.ensure_ssm_endpoints(_aws_region)
                if ssm_eps:
                    result["ssm_endpoint_ids"] = ssm_eps
                    job_service.update_progress(db, job_id, 37, "SSM interface endpoints ready…")
            except Exception as e:
                result["ssm_endpoint_error"] = str(e)

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
                    iam_instance_profile=_rc["ssm_instance_profile"],
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
    region: str = "",
    jump_group: str = "",
    jumpoint_name: str = "",
    pra_credential_ref: str = "",
):
    """
    Background task for bulk EC2 deployment.
    Ensures the shared Jumpoint host is up once for the whole batch, then deploys
    each instance sequentially. The host is ref-counted across all EC2 instances
    and databases and reclaimed when the last one is removed.

    The three PRA arguments default to blank and fall through to config, which is what
    the multi-select bulk route passed implicitly before it could send them. They exist
    because this function resolved the jump group and jumpoint from config *only*,
    while _run_deploy honoured the per-deploy overrides — so a batch silently ignored
    them and reported success with the VM registered against the wrong jump group.
    """
    db = _get_db_session()
    try:
        # NB: children are deliberately NOT all marked running up front. They deploy
        # sequentially, so the last one in a large batch would sit `running` with no
        # progress write for the whole run — and reconcile_stale_jobs fails any
        # `running` job whose heartbeat is 10 minutes cold, so an app restart mid-batch
        # would fail it out from under this parent, which then deploys it anyway.
        # Left `queued` until its turn, reconcile skips it. set_running now happens at
        # the top of the loop below.

        # Step 1: Start ONE ECS Jumpoint container for the whole batch (BT only)
        from ..services import config_service as _cfg_svc
        from ..services.region_config import resolve_region
        # Shared per-batch region (falls back to the configured default).
        _aws_region = region or _aws_cfg("aws_region") or "us-east-2"
        # Per-region SSH key secret + SSM instance profile (flat fallback for the
        # default region / blank fields).
        _rc = resolve_region("aws", _aws_region)
        first_job_id = job_items[0][0]
        _bmeta = (db.query(Job).filter(Job.id == first_job_id).first().metadata_dict or {})
        ssh_secret_name = _bmeta.get("ssh_key_secret_override") or _rc["ssh_key_secret"]
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

        # Step 1b: Ensure ONE shared NAT instance for the whole batch (independent
        # of BeyondTrust) so the VMs' private subnet gets outbound internet.
        nat_instance_id = None
        nat_error = None
        if _cfg_svc.get_bool("aws_nat_instance_enabled"):
            try:
                from ..services import nat_instance_service
                nat_instance_id = await nat_instance_service.ensure_nat_instance(_aws_region)
            except Exception as e:
                nat_error = str(e)

        # Step 1c: Ensure ONE shared set of SSM interface endpoints for the batch
        # (private-subnet SSM reach). Best-effort — never blocks the deploy.
        ssm_endpoint_ids = None
        ssm_endpoint_error = None
        if _cfg_svc.get_bool("aws_ssm_endpoints_enabled"):
            try:
                from ..services import ssm_endpoint_service
                ssm_endpoint_ids = await ssm_endpoint_service.ensure_ssm_endpoints(_aws_region)
            except Exception as e:
                ssm_endpoint_error = str(e)

        # Step 2: Fetch SSH public key once for the whole batch
        job_service.update_progress(db, first_job_id, 18, "Fetching SSH public key from Secrets Manager…")
        key_detail = await aws_service.get_ssh_public_key_from_secret(_aws_region, ssh_secret_name)
        shared_public_key = key_detail["public_key"]

        # PRA targets are batch-wide, so resolve them once rather than per instance —
        # in particular pra_credential_ref, which is a secrets-backend lookup.
        _bulk_jump_group = ((jump_group or "").strip()
                            or _cfg_svc.get("bt_jump_group_name") or settings.bt_jump_group_name)
        _bulk_jumpoint_name = ((jumpoint_name or "").strip()
                               or _cfg_svc.get("bt_jumpoint_name") or settings.bt_jumpoint_name)
        _bulk_client_secret = (_cfg_svc.resolve_reference(pra_credential_ref.strip())
                               if pra_credential_ref else "")

        # Step 3: Deploy each instance, all sharing the same ECS task ARN
        for job_id, item in job_items:
            # Claim this child only as its turn comes — see the note above.
            job_service.set_running(db, job_id)
            result: dict = {"ssh_secret_name": ssh_secret_name}
            if jumpoint_host_id:
                result["jumpoint_host_id"] = jumpoint_host_id
            elif ecs_error:
                result["ecs_error"] = ecs_error
            if nat_instance_id:
                result["nat_instance_id"] = nat_instance_id
            elif nat_error:
                result["nat_error"] = nat_error
            if ssm_endpoint_ids:
                result["ssm_endpoint_ids"] = ssm_endpoint_ids
            elif ssm_endpoint_error:
                result["ssm_endpoint_error"] = ssm_endpoint_error

            try:
                job_service.update_progress(
                    db, job_id, 40, f"Launching EC2 instance {item.instance_name}…"
                )
                ami_info = await aws_service.describe_ami(_aws_region, item.ami_id)
                is_windows = "windows" in (ami_info.get("platform", "") or "").lower()
                os_type = ""
                if not is_windows:
                    from ..services.os_detection import detect_os_type
                    os_type, result["ssh_user"] = detect_os_type(ami_info.get("name", ""))
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
                        iam_instance_profile=_rc["ssm_instance_profile"],
                        # Load-bearing, and it was missing: _build_userdata branches
                        # entirely on os_type, so "" emits no runcmd and the instance
                        # comes up with NO SSM agent — no Session Manager, and the
                        # Password Safe SSM plugin this same path onboards it into has
                        # nothing to talk to.
                        os_type=os_type,
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
                            # Override-then-config, matching _run_deploy. This read
                            # config only, so a batch silently ignored the jump group
                            # and jumpoint the operator picked on the form.
                            jump_group_name=_bulk_jump_group,
                            jumpoint_name=_bulk_jumpoint_name,
                            tag="AWS",
                            client_secret=_bulk_client_secret,
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


async def _run_destroy(destroy_job_id: str, deploy_job_id: str, instance_id: str, region: str = ""):
    db = _get_db_session()
    # Region the instance lives in (falls back to the configured default).
    _region = region or _aws_region()
    try:
        job_service.set_running(db, destroy_job_id)
        job_service.update_progress(db, destroy_job_id, 20, f"Terminating instance {instance_id}…")

        # Cloud-identity JIT Phase 2: gate the terminate behind elevate().
        # EC2 TerminateInstances doesn't accept tags, so cloud-side
        # correlation has to come from the activation row (joined by
        # instance_id) instead of an inline tag.
        from ..services.cloud_identity_service import elevate, CloudIdentityError
        _destroy_job = job_service.get_job(db, destroy_job_id)
        _terminate_region = _region
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
            await jumpoint_host_service.teardown_jumpoint_host_if_idle(db, "aws", _region)
        except Exception as e:
            result["jumpoint_host_teardown_error"] = str(e)

        # Reclaim the shared on-demand NAT instance if no EC2 instance is left
        # (same exclusion — this deploy job is already marked destroyed).
        try:
            from ..services import nat_instance_service
            await nat_instance_service.reclaim_nat_instance(db, _region)
        except Exception as e:
            result["nat_teardown_error"] = str(e)

        # Reclaim the shared SSM interface endpoints if no EC2 instance / AWS cloud
        # DB is left (same exclusion — this deploy job is already marked destroyed).
        try:
            from ..services import ssm_endpoint_service
            await ssm_endpoint_service.reclaim_ssm_endpoints(db, _region)
        except Exception as e:
            result["ssm_endpoint_teardown_error"] = str(e)

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
