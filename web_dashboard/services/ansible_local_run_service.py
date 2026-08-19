"""VM (SSH/WinRM) Config-Management run execution.

The counterpart of ``ansible_cloud_run_service``: that one drives a ``hosts: localhost``
play against a Kubernetes cluster or cloud database; this one SSHes (or WinRMs) *to* a
VM or a hypervisor inventory group.

Dispatched by ``jobs_worker`` as an ``ansible_local`` job. The run's parameters are
reconstructed from the job's persisted metadata by ``services.ansible_run_meta``, so a
run survives a restart of the process that enqueued it — see that module for what may
and may not be written to job metadata.

This lives in ``services/`` rather than in ``api/config_mgmt`` because the job runner
has to import it, and a worker reaching into the API package is backwards. The API
module keeps only what belongs to a request: the permission check, the endpoint
pre-flight validation (``_effective_runner`` / ``_validate_cloud_secret_stores``) and
the routes themselves.
"""
import logging

from fastapi import HTTPException

from ..database import Job
from . import ansible_local_service
from . import job_service
from . import storage_service
from .storage_service import StorageError

logger = logging.getLogger(__name__)


async def run(db, *, job_id: str, meta: dict) -> None:
    """Entry point for the job runner.

    Mirrors ``ansible_cloud_run_service.run`` so the dispatcher's branches are
    interchangeable. ``_run_job`` opens its own session and owns the job lifecycle, so
    the runner's ``db`` is accepted for signature symmetry and deliberately unused."""
    from . import ansible_run_meta
    await _run_job(job_id, **ansible_run_meta.run_kwargs(meta))


def _cfg(key: str) -> str:
    return ansible_local_service._cfg(key)


# Deploy job type + the config key holding the SSH keypair secret the deploy used,
# per cloud. A dashboard-built VM's own keypair (the one cloud-init injected) lives
# in that secret; a per-launch override is recorded on the deploy job metadata as
# ``ssh_key_secret_override``. This is the key the VM actually trusts — the global
# ``ansible_ssh_key_sm_name`` is only a fallback for externally-built hosts.
_CLOUD_DEPLOY_JOB_TYPE = {"aws": "ec2_deploy", "azure": "azure_deploy", "gcp": "gce_deploy"}
_VM_BUILD_KEY_DEFAULT_CFG = {
    "aws":   "ec2_ssh_key_secret",
    "gcp":   "gcp_ssh_key_secret_name",
    "azure": "azure_ssh_keypair_secret_name",
}
def _find_cloud_deploy_meta(db, cloud: str, ip: str) -> dict:
    """Metadata of the most-recent, non-destroyed deploy job for the cloud VM at
    ``ip`` — mirrors how /cloud-targets enumerates targets. Empty dict when none
    matches (externally-created VM, or a manually-typed cloud IP)."""
    job_type = _CLOUD_DEPLOY_JOB_TYPE.get(cloud)
    if not job_type or not ip:
        return {}
    jobs = (
        db.query(Job)
        .filter(Job.job_type == job_type, Job.status == "completed")
        .order_by(Job.created_at.desc())
        .all()
    )
    for job in jobs:
        meta = job.metadata_dict
        if meta.get("destroyed"):
            continue
        if (meta.get("public_ip") or meta.get("private_ip")) == ip:
            return meta
    return {}


def _managed_name_hint(db, cloud: str, target: str,
                       managed_account, managed_become) -> str:
    """The extra name a Password Safe managed-system lookup should also try.

    Cloud-native onboarding registers a managed system under the *deploy name* with a
    placeholder IP, so looking it up by address alone misses it. Only this path can supply
    that hint, because only this path has a deploy job to read it off — which is why
    ``ansible_credentials.resolve_managed_ref`` takes it as an argument rather than going
    looking. An on-prem VM behind an agent has no deploy job and passes ``""``.

    Skipped entirely unless a ref actually needs resolving: a pinned ref from the single-run
    picker never reaches the lookup, so computing the hint would be a wasted query on the
    common path.
    """
    def _needs_lookup(ref) -> bool:
        return bool(ref) and (ref.get("account_id") if isinstance(ref, dict) else None) is None

    if not (_needs_lookup(managed_account) or _needs_lookup(managed_become)):
        return ""
    meta = _find_cloud_deploy_meta(db, cloud, target)
    return meta.get("instance_name") or meta.get("vm_name") or ""


def _vm_build_key_secret(cloud: str, meta: dict) -> str:
    """The secret name holding the keypair the VM was built with: the per-launch
    override recorded on the deploy job, else the cloud's configured deploy default."""
    override = (meta.get("ssh_key_secret_override") or "").strip()
    return override or _cfg(_VM_BUILD_KEY_DEFAULT_CFG.get(cloud, ""))


async def _resolve_cloud_ssh_key(db, cloud: str, ip: str) -> str | None:
    """Best SSH private key for a cloud VM run: the keypair the VM was actually
    built with (resolved from its deploy metadata) first, then the per-cloud global
    Ansible key as a fallback. Never raises — a retrieval error yields the fallback
    (and ultimately None), matching the prior "proceed without key" behaviour."""
    build_secret = _vm_build_key_secret(cloud, _find_cloud_deploy_meta(db, cloud, ip))
    if build_secret:
        try:
            pem = await ansible_local_service.fetch_ssh_key(cloud, secret_name=build_secret)
            if pem:
                return pem
        except Exception as exc:
            logger.warning("VM build-key retrieval failed (%s) — trying the global key: %s",
                           cloud, exc)
    return await ansible_local_service.fetch_ssh_key(cloud)


def _scrub_secrets(text: str, values: list) -> str:
    """Redact resolved secret values from run output before it's stored/shown —
    defense in depth so a ``debug`` in a playbook can't leak an injected secret to
    the job log. Values shorter than 4 chars are skipped to avoid over-redaction."""
    if not text or not values:
        return text
    for v in values:
        v = str(v)
        if len(v) >= 4:
            text = text.replace(v, "***")
    return text


def _resolve_cloud_secrets(runner: str, secret_vars: dict | None,
                           secret_become_source: str) -> tuple:
    """Build ``(secret_entries, manifest_b64, inline_values)`` for a cloud run —
    ECS ``{env, arn}`` / GCP ``{env, secret_name}`` / ACI ``{env, value}``.
    ``inline_values`` (ACI only) feed the output scrub set."""
    from ..services import (config_service as cs, cloud_ansible_secrets as _cas,
                            secrets_backend_service as sbs)
    try:
        return _cas.resolve_entries(
            runner, secret_vars, secret_become_source,
            is_reference=cs.is_reference, get=cs.get, get_raw=cs.get_raw,
            resolve_reference=cs.resolve_reference, parse_ref=cs._parse_ref,
            aws_sm_arn=sbs.aws_sm_arn)
    except _cas.StoreMismatch as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def _add_ephemeral_managed_entries(runner: str, entries: list, manifest_b64: str,
                                   managed_cred_vars: dict, job_id: str) -> tuple:
    """Materialise managed-account credential vars as short-lived, RBAC-locked cloud
    store secrets (ECS → AWS SM, GCP → GCP SM) and extend the (entries, manifest)
    with them, continuing the env-index numbering. Returns
    ``(entries, manifest_b64, cleanup)`` where ``cleanup`` is ``[(provider, id)]``
    to force-delete after the run. A JIT credential can't reference a pre-existing
    store secret (#217), so we create one per run and reap it."""
    import base64 as _b64, json as _json
    from ..services import (cloud_ansible_secrets as _cas, ephemeral_secrets as _eph,
                            secrets_backend_service as sbs)
    if not managed_cred_vars:
        return entries, manifest_b64, []
    entries = list(entries)
    manifest = _json.loads(_b64.b64decode(manifest_b64)) if manifest_b64 else []
    cleanup = []
    start = len(entries)
    for i, (var, value) in enumerate(managed_cred_vars.items()):
        env = _cas.env_name(start + i)
        if runner == "ecs":
            name = _eph.aws_secret_name(job_id, start + i)
            arn = sbs.write_aws_sm_ephemeral(
                name, value, exec_role_arn=_cfg("ansible_ecs_execution_role_arn"),
                kms_key_id=_cfg("ansible_ephemeral_kms_key_id"))
            entries.append({"env": env, "arn": arn})
            cleanup.append(("aws", name))
        else:  # gcp
            sid = _eph.gcp_secret_id(job_id, start + i)
            sbs.write_gcp_sm_ephemeral(
                sid, value, runner_sa=_cfg("gcp_ansible_runner_service_account"))
            entries.append({"env": env, "secret_name": sid})
            cleanup.append(("gcp", sid))
        manifest.append({"env": env, "var": var})
    manifest_b64 = _b64.b64encode(_json.dumps(manifest).encode()).decode()
    return entries, manifest_b64, cleanup


def _delete_ephemeral(cleanup: list) -> None:
    """Best-effort force-delete of the ephemeral store secrets created for a run.
    A failure here is non-fatal — the GC sweeper reaps anything left behind."""
    if not cleanup:
        return
    from ..services import secrets_backend_service as sbs
    for provider, sid in cleanup:
        try:
            (sbs.delete_aws_sm if provider == "aws" else sbs.delete_gcp_sm)(sid)
        except Exception:
            # Static message + traceback only, no interpolated data. Everything
            # unpacked from `cleanup` — even the "aws"/"gcp" provider literal — is
            # tainted by CodeQL because the list is built in the credential-handling
            # loop, so logging any of it trips py/clear-text-logging. The traceback
            # still names the failing delete call; the GC sweeper reaps by tag.
            logger.warning("an ephemeral secret cleanup failed (GC will reap it)",
                           exc_info=True)
async def _run_job(
    job_id: str,
    asset: str,
    target: str,
    cloud: str,
    ansible_user: str,
    extra_vars: dict,
    asset_backend: str = "",
    secret_vars: dict | None = None,
    secret_become_source: str = "",
    secret_ssh_key_source: str = "",
    managed_account: dict | None = None,
    managed_become: dict | None = None,
    epml_token_var: str = "",
) -> None:
    """Execute one VM (SSH/WinRM) Config-Management run.

    Dispatched by ``jobs_worker`` from the job's persisted metadata — NOT from a
    request. It owns its own ``SessionLocal`` and the job lifecycle, so the runner
    hands it no session. Arguments are reconstructed by
    ``services.ansible_run_meta.run_kwargs``; that module's key set is asserted
    against this signature by tests/test_ansible_local_meta.py, because a name that
    drifts is a TypeError inside a background worker where nobody is watching.
    """
    import base64
    from ..database import SessionLocal
    db = SessionLocal()
    try:
        # Re-stamp 'running' (the runner's atomic claim already set it) so the row
        # heartbeats from the moment work actually starts — reconcile_stale_jobs
        # only reconciles 'running' jobs, and uses that heartbeat to tell a live run
        # from one whose worker died. Mirrors ansible_cloud_run_service.run.
        job_service.set_running(db, job_id)
        job_service.update_progress(db, job_id, 5, f"Fetching asset '{asset}'…")
        try:
            if asset_backend:
                raw = await storage_service.fetch_asset_in(asset_backend, asset)
                asset_b64 = base64.b64encode(raw).decode()
            else:
                # Back-compat: caller didn't specify a backend → fall back to
                # the active backend's copy.
                asset_b64 = await storage_service.fetch_asset_b64(asset)
        except StorageError as e:
            job_service.set_failed(db, job_id, f"Asset storage error: {e}")
            return

        # Resolve every credential ref into a value ONCE, just-in-time — named
        # Secrets-Management vars, the become password, the SSH key, an EPM-L token and a
        # Password Safe managed-account checkout. Shared with the agent-executed path
        # (services/ansible_credentials), which needs the identical resolution to build its
        # sealed bundle; two copies of this would use the wrong credential rather than fail.
        #
        # The cloud-native name hint stays HERE because only this path has a deploy job to
        # read it off: cloud onboarding registers a managed system under the deploy name
        # with a placeholder IP, so an IP-only lookup misses it.
        from ..services import ansible_credentials
        try:
            _creds = await ansible_credentials.resolve(
                db,
                secret_vars=secret_vars,
                secret_become_source=secret_become_source,
                secret_ssh_key_source=secret_ssh_key_source,
                epml_token_var=epml_token_var,
                managed_account=managed_account,
                managed_become=managed_become,
                target=target, cloud=cloud,
                name_hint=_managed_name_hint(db, cloud, target,
                                            managed_account, managed_become),
            )
        except ansible_credentials.CredentialError as e:
            job_service.set_failed(db, job_id, str(e))
            return
        secret_extra_vars = _creds.extra_vars
        secret_ssh_pem = _creds.ssh_pem
        managed_cred_vars = _creds.managed_cred_vars
        managed_plain_vars = _creds.managed_plain_vars
        managed_request_ids = _creds.request_ids
        secret_values = _creds.scrub

        # Per-target-cloud runner backend: an AWS-target job uses
        # ansible_runner_aws, Azure → ansible_runner_azure, GCP → ansible_runner_gcp,
        # each falling back to the global ansible_runner. The target cloud is the
        # run request's `cloud` field (operator-set for cloud targets; "" on-prem).
        runner = _cfg("ansible_runner") or "local"
        if cloud in ("aws", "azure", "gcp"):
            runner = _cfg(f"ansible_runner_{cloud}") or runner
        is_adhoc = "." in target or ":" in target
        is_playbook = ansible_local_service.asset_type(asset) == "playbook"

        # Auto-inject the configured Password Safe OAuth creds as PASSWORD_SAFE_* env so
        # an in-playbook beyondtrust.secrets_safe lookup works with no per-run setup. Rides
        # the same connection-credential channel as the SSH key on each runner (no cloud
        # store). {} when BeyondTrust is disabled / unconfigured. Scrub the client secret.
        from ..services import password_safe_runner as _psr
        ps_env = _psr.runner_env()
        _ps_secret = ps_env.get(_psr.SECRET_KEY)
        if _ps_secret and _ps_secret not in secret_values:
            secret_values.append(_ps_secret)

        # Same treatment for the Portainer connection (PORTAINER_* env), so an
        # in-playbook API call works with no per-run setup. Merged into the same
        # runner env channel — it's an opaque key/value dict at every backend. {} when
        # Portainer is disabled / no server configured. Scrub the PAT.
        from ..services import portainer_runner as _ptr
        _pt_env = _ptr.runner_env()
        _pt_secret = _pt_env.get(_ptr.SECRET_KEY)
        if _pt_secret and _pt_secret not in secret_values:
            secret_values.append(_pt_secret)
        ps_env = {**ps_env, **_pt_env}

        # Cloud runners only support bare-IP targets and .yml playbooks.
        # Fall back to local for group targets or non-playbook assets.
        if runner != "local" and is_adhoc and is_playbook:
            # key_cloud is the target cloud (drives SSH key + user lookup). The
            # run request's `cloud` wins; fall back to inferring it from the
            # runner backend for the legacy global path (no `cloud` supplied).
            key_cloud = cloud or {"ecs": "aws", "aci": "azure", "gcp": "gcp"}.get(runner, runner)

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
            # A managed account is the login identity — its name wins as the SSH user.
            if managed_plain_vars.get("ansible_user"):
                resolved_user = managed_plain_vars["ansible_user"]

            # A Secrets-Management SSH-key secret (if supplied) overrides the
            # configured key — this is the only secret kind the cloud runner takes.
            ssh_key_pem: str | None = secret_ssh_pem
            if ssh_key_pem is None:
                job_service.update_progress(db, job_id, 10, f"Retrieving SSH key for {key_cloud.upper()}…")
                try:
                    ssh_key_pem = await _resolve_cloud_ssh_key(db, key_cloud, target)
                except Exception as exc:
                    logger.warning("SSH key retrieval failed (%s) — proceeding without key: %s", key_cloud, exc)

            ssh_key_b64 = base64.b64encode(ssh_key_pem.encode()).decode() if ssh_key_pem else ""

            # Secret injection → per-provider secret channel. ACI injects inline
            # (secure_value), so it carries local semantics: deliver the full
            # resolved var set — #216/#217 named vars + become AND any managed-
            # account credential (already merged into secret_extra_vars above) — as
            # inline vars. The SSH key rides SSH_KEY_B64 (above). ECS/Cloud Run
            # reference a store secret, so they resolve per-provider store refs and
            # a managed-account run never reaches here (rejected at the endpoint).
            from ..services import cloud_ansible_secrets as _cas
            ephemeral_cleanup: list = []
            if runner == "aci":
                cloud_secret_entries, cloud_manifest_b64 = _cas.inline_entries(secret_extra_vars)
            else:
                cloud_secret_entries, cloud_manifest_b64, cloud_inline_values = (
                    _resolve_cloud_secrets(runner, secret_vars, secret_become_source))
                for _v in cloud_inline_values:
                    if _v and _v not in secret_values:
                        secret_values.append(_v)
                # Managed-account creds → ephemeral, RBAC-locked store secrets (the
                # ECS/GCP secret channel references a store secret; a JIT credential
                # has none, so we mint one per run and reap it). Sweep leaked ones
                # first (belt-and-braces with the startup GC).
                if managed_cred_vars:
                    try:
                        from ..services import ephemeral_gc
                        ephemeral_gc.sweep()
                    except Exception:
                        logger.warning("ephemeral GC pre-sweep failed (non-fatal)", exc_info=True)
                    cloud_secret_entries, cloud_manifest_b64, ephemeral_cleanup = (
                        _add_ephemeral_managed_entries(
                            runner, cloud_secret_entries, cloud_manifest_b64,
                            managed_cred_vars, job_id))
                    # Best-effort: flag the PS requests to rotate on check-in, so the
                    # copied-to-store credential is rotated (dead) once we check in
                    # below — even if the store cleanup is missed. Not enforceable
                    # (rotation depends on the account being auto-managed).
                    from ..services import btapi_service as _bt
                    for _rid in managed_request_ids:
                        await _bt.rotate_ps_request_on_checkin(_rid)

            job_service.update_progress(db, job_id, 20, f"Launching {runner.upper()} runner for {asset}…")
            try:
                exit_code, output = await _dispatch_cloud_runner(
                    runner=runner,
                    target_ip=target,
                    ansible_user=resolved_user,
                    playbook_b64=asset_b64,
                    ssh_key_b64=ssh_key_b64,
                    job_id=job_id,
                    secret_entries=cloud_secret_entries,
                    manifest_b64=cloud_manifest_b64,
                    ps_env=ps_env,
                )
            finally:
                # Value already fetched by the task identity at launch — safe to reap
                # the store copy and check the PS requests in (rotates on release when
                # flagged above). Both best-effort; the GC sweeper backstops leaks.
                _delete_ephemeral(ephemeral_cleanup)
                if ephemeral_cleanup and managed_request_ids:
                    from ..services import btapi_service as _bt
                    for _rid in managed_request_ids:
                        await _bt.checkin_ps_request(_rid)

            output = _scrub_secrets(output, secret_values)
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

        # A Secrets-Management SSH-key secret (if supplied) overrides the key.
        ssh_key_pem = secret_ssh_pem
        if ssh_key_pem is None and cloud in ("aws", "gcp", "azure"):
            job_service.update_progress(db, job_id, 10, f"Retrieving SSH key for {cloud.upper()}…")
            try:
                ssh_key_pem = await _resolve_cloud_ssh_key(db, cloud, target)
                if not ssh_key_pem:
                    logger.warning("No SSH key resolved for %s — proceeding without key", cloud)
            except Exception as exc:
                logger.warning("Failed to retrieve SSH key for %s: %s — proceeding without key", cloud, exc)

        job_service.update_progress(db, job_id, 20, f"Running {asset} against {target}…")
        output, rc = await ansible_local_service.run_playbook(
            asset_b64=asset_b64,
            target=target,
            extra_vars=extra_vars or None,
            asset_name=asset,
            ssh_key_pem=ssh_key_pem,
            secret_extra_vars=secret_extra_vars or None,
            ps_env=ps_env or None,
            # The inventory is built from hypervisor_connections now, so the runner
            # needs a session to see more than the legacy singletons.
            db=db,
        )

        output = _scrub_secrets(output, secret_values)
        if rc == 0:
            job_service.set_completed(db, job_id, {"output": output, "returncode": rc})
            # Config-drift: record the per-target fingerprint of this apply (passive,
            # best-effort — never let a tracking hiccup fail the job).
            try:
                from ..services import config_drift, config_service as cs
                if cs.get_bool("config_drift_tracking_enabled", True):
                    content = base64.b64decode(asset_b64) if asset_b64 else b""
                    config_drift.record_apply(
                        db, target=target, playbook_ref=asset,
                        content_hash=config_drift.content_hash(content),
                        inputs_hash=config_drift.inputs_hash(extra_vars),
                        job_id=job_id)
            except Exception:
                logger.warning("config-drift record failed for job %s", job_id, exc_info=True)
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
    secret_entries: list | None = None,
    manifest_b64: str = "",
    ps_env: dict | None = None,
) -> tuple:
    """Route to the configured cloud Ansible runner. Returns (exit_code, output).

    secret_entries/manifest_b64 (when present) carry per-provider secret refs — the
    runner injects each via the provider's secret channel and the container builds a
    0600 vars file from the manifest before running ansible-playbook.

    ps_env (when present) is the auto-injected credential env (PASSWORD_SAFE_* and/or
    PORTAINER_*) for an in-playbook
    beyondtrust.secrets_safe lookup; it rides the same connection-credential channel as
    the SSH key on each runner (no cloud store)."""
    if runner == "ecs":
        from ..services import aws_service
        region = _cfg("aws_region") or "us-east-1"
        sg_raw = _cfg("ansible_ecs_security_group_ids") or ""
        sg_ids = [s.strip() for s in sg_raw.split(",") if s.strip()]
        return await aws_service.run_ecs_ansible_task(
            region=region,
            cluster=_cfg("ansible_ecs_cluster") or "bt-jumpoint",
            task_family=_cfg("ansible_ecs_task_family") or "ansible-config-mgmt",
            image=_cfg("ansible_ecs_image") or "chrweav/ansible-winrm:latest",
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
            secret_entries=secret_entries,
            manifest_b64=manifest_b64,
            ps_env=ps_env,
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
            # Fall back to the jumpoint's VNet-delegated subnet (azure_aci_subnet_id)
            # when the runner's own subnet is unset: it already has line-of-sight to
            # the target VM subnet and outbound egress for the image pull. With no
            # subnet at all the container group is public and can't reach private IPs
            # (SSH to the VM times out → UNREACHABLE).
            subnet_id=_cfg("ansible_aci_subnet_id") or _cfg("azure_aci_subnet_id") or "",
            image=_cfg("ansible_aci_image") or "chrweav/ansible-winrm:latest",
            target_ip=target_ip,
            ansible_user=ansible_user,
            playbook_b64=playbook_b64,
            ssh_key_b64=ssh_key_b64,
            job_id=job_id,
            acr_server=_cfg("ansible_aci_acr_server") or "",
            acr_username=_cfg("ansible_aci_acr_username") or "",
            acr_password=_cfg("ansible_aci_acr_password") or "",
            secret_entries=secret_entries,
            manifest_b64=manifest_b64,
            ps_env=ps_env,
        )

    if runner == "gcp":
        from ..services import gcp_service
        region = _cfg("gcp_ansible_cloud_run_region") or _cfg("gcp_region") or ""
        return await gcp_service.run_cloud_run_ansible_task(
            project_id=_cfg("gcp_project_id"),
            region=region,
            image=_cfg("gcp_ansible_image") or "chrweav/ansible-winrm:latest",
            target_ip=target_ip,
            ansible_user=ansible_user,
            playbook_b64=playbook_b64,
            ssh_key_b64=ssh_key_b64,
            job_id=job_id,
            # Reach a private SSH target via direct VPC egress (the job NIC lands
            # in the subnet — no standing infra) when configured, else the legacy
            # Serverless-VPC-Access connector. Direct wins when both are set.
            vpc_connector=_cfg("gcp_ansible_vpc_connector") or "",
            vpc_network=_cfg("gcp_run_network") or "",
            vpc_subnetwork=_cfg("gcp_run_subnetwork") or "",
            service_account=_cfg("gcp_ansible_runner_service_account") or "",
            secret_entries=secret_entries,
            manifest_b64=manifest_b64,
            ps_env=ps_env,
        )

    raise ValueError(f"Unknown ansible_runner: {runner!r}")
