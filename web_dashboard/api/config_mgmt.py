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

from ..database import Job, User, get_db
from .auth import get_current_user
from ..services import job_service
from ..services import storage_service
from ..services.storage_service import StorageError
from ..services import ansible_local_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/config-mgmt", tags=["config-mgmt"])


# ── Asset / playbook listing ───────────────────────────────────────────────────

@router.get("/assets")
async def list_assets(current_user: User = Depends(get_current_user)):
    """List all available assets (.yml, .sh, .deb, .rpm) across every configured
    storage backend, each item tagged with the backend it lives on. Issue #16:
    operators can now keep playbooks on local filesystem AND on a cloud backend
    side-by-side — the UI uses the per-asset backend tag to warn when a local
    asset is paired with a cloud target."""
    try:
        return await storage_service.list_all_assets()
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
    # Advisory secret scan (never blocks the upload — a heads-up only).
    findings = []
    from ..services import config_service as cs, secret_scan
    if cs.get_bool("secret_scan_enabled", True):
        findings = secret_scan.scan_bytes(data, req.filename)

    try:
        await storage_service.upload_asset(req.filename, data)
    except StorageError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"ok": True, "filename": req.filename, "size": len(data),
            "secret_findings": findings}


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

class ManagedAccountRef(BaseModel):
    """A BeyondTrust Password Safe managed account the operator picked from the
    live list. The ids drive the just-in-time credential checkout; the name is
    non-secret and becomes ``ansible_user``. Never carries a credential."""
    system_id: int
    account_id: int
    account_name: str = ""
    uses_ssh_key: bool = False   # DSSAutoManagementFlag → checkout as -t dsskey


class RunRequest(BaseModel):
    asset: str           # filename of any supported type (.yml, .sh, .deb, .rpm)
    target: str          # on-prem group key OR bare IP/hostname for cloud/ad-hoc
    cloud: str = ""      # "" | "aws" | "azure" | "gcp" — drives SSH key retrieval
    ansible_user: str = ""  # SSH user for cloud runner targets; falls back to ansible_default_user
    extra_vars: dict = {}
    # Use Secrets-Management secrets in the run WITHOUT ever seeing the value.
    # Requires the `secrets:use` permission (admins bypass). A "source" is a
    # config-secret registry key or a raw vault ref (bt_safe:// …). Resolved
    # values are scrubbed from job output and never stored on the job.
    secret_vars: dict = {}            # {ansible_var: source} — named vars; LOCAL runner only
    secret_become_source: str = ""    # source → ansible_become_password (no_log); LOCAL runner only
    secret_ssh_key_source: str = ""   # source → the connection SSH private key; LOCAL + cloud runner
    # Which storage backend the asset should be fetched from. Empty = active
    # backend (back-compat). With multi-backend support (issue #16), the UI
    # passes the backend explicitly because the same asset name may exist on
    # multiple backends.
    asset_backend: str = ""
    # BeyondTrust Password Safe managed-account checkout (LOCAL runner only). The
    # credential is checked out just-in-time at run time — the operator never sees
    # it. managed_account is the connection identity; managed_become is an optional
    # separate account for the become/sudo password.
    managed_account: ManagedAccountRef | None = None
    managed_become: ManagedAccountRef | None = None


def _cfg(key: str) -> str:
    return ansible_local_service._cfg(key)


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


def _can_use_secrets(user) -> bool:
    """True if the user may use a secret in a run (without ever seeing it): an
    admin, an unrestricted (NULL-permission) legacy user, or one granted
    ``secrets:use``."""
    if getattr(user, "is_effective_admin", False):
        return True
    perms = user.effective_permissions_dict  # {} / NULL → unrestricted (legacy)
    if not perms:
        return True
    return "use" in perms.get("secrets", [])


# ── Cloud-runner secret injection (hardened per provider) ───────────────────────
# The per-provider resolution is pure (services/cloud_ansible_secrets); here we
# inject the real config_service / secrets-backend callables and map the module's
# StoreMismatch to an actionable HTTP 400.
def _effective_runner(cloud: str) -> str:
    """The Ansible runner backend that will actually handle a run for this target
    cloud — per-cloud override (ansible_runner_<cloud>) falling back to global."""
    runner = _cfg("ansible_runner") or "local"
    if cloud in ("aws", "azure", "gcp"):
        runner = _cfg(f"ansible_runner_{cloud}") or runner
    return runner


def _validate_cloud_secret_stores(runner: str, secret_vars: dict | None,
                                  secret_become_source: str) -> None:
    """For the ECS/GCP runners, require every named/become secret to reference that
    cloud's store. Raises HTTPException(400) otherwise; no-op for ACI/local. Pure
    prefix check (no backend I/O) so it can gate the request synchronously."""
    from ..services import config_service as cs, cloud_ansible_secrets as _cas
    try:
        _cas.validate_stores(runner, secret_vars, secret_become_source,
                             is_reference=cs.is_reference, get_raw=cs.get_raw)
    except _cas.StoreMismatch as exc:
        raise HTTPException(status_code=400, detail=str(exc))


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
            # Log the provider + traceback only — the resource id is a loop-derived
            # value CodeQL taints from the credential dict, and it isn't needed here
            # (the GC sweeper reaps by tag; the traceback names the failing call).
            logger.warning("ephemeral %s cleanup failed (GC will reap it)",
                           provider, exc_info=True)


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
) -> None:
    import base64
    from ..database import SessionLocal
    db = SessionLocal()
    try:
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

        # Resolve requested Secrets-Management secrets ONCE, just-in-time — never
        # stored on the job, never on the command line; values are scrubbed from
        # output below. Named vars + become password apply to the local runner;
        # the SSH-key secret applies to both (used as the connection key).
        secret_extra_vars: dict = {}
        secret_ssh_pem = None
        secret_values: list = []
        if secret_vars or secret_become_source or secret_ssh_key_source:
            from ..services import ansible_secrets, config_service as cs

            def _resolve_source(src: str) -> str:
                src = (src or "").strip()
                if not src:
                    return ""
                return cs.resolve_reference(src) if cs.is_reference(src) else cs.get(src)

            secret_extra_vars = ansible_secrets.resolve_secret_vars(
                secret_vars, get=cs.get, resolve_reference=cs.resolve_reference,
                is_reference=cs.is_reference)
            if secret_become_source:
                _bp = _resolve_source(secret_become_source)
                if _bp:
                    secret_extra_vars["ansible_become_password"] = _bp
            if secret_ssh_key_source:
                secret_ssh_pem = _resolve_source(secret_ssh_key_source) or None
            secret_values = [v for v in list(secret_extra_vars.values())
                             + ([secret_ssh_pem] if secret_ssh_pem else []) if v]

        # Managed-account checkout (BeyondTrust Password Safe) — check out the
        # credential just-in-time. The account is the connection identity;
        # managed_become is an optional separate account for the sudo/become
        # password. Tracked separately so each runner can route it correctly:
        #   • local / ACI → inline (merged into secret_extra_vars below)
        #   • ECS / GCP   → the password vars become ephemeral store secrets, the
        #                   SSH key rides SSH_KEY_B64, ansible_user is a plain var.
        managed_cred_vars: dict = {}   # cred vars needing a secure channel on cloud
        managed_plain_vars: dict = {}    # non-secret vars (ansible_user)
        managed_request_ids: list = []   # PS request ids — checked in (rotate-on-release) after a cloud run
        if managed_account or managed_become:
            from ..services import btapi_service
            # Long enough that the request is still open after the run for the
            # rotate-on-check-in + check-in below (best-effort mitigation).
            _req_dur = int(_cfg("ansible_managed_request_duration_min") or 60)
            try:
                if managed_account:
                    req_id, cred = await btapi_service.get_ps_credential_with_request(
                        managed_account["system_id"], managed_account["account_id"],
                        duration_min=_req_dur,
                        uses_ssh_key=managed_account.get("uses_ssh_key", False))
                    managed_request_ids.append(req_id)
                    if managed_account.get("account_name"):
                        managed_plain_vars["ansible_user"] = managed_account["account_name"]
                    if managed_account.get("uses_ssh_key"):
                        secret_ssh_pem = cred                     # connection key (SSH_KEY_B64)
                    else:
                        managed_cred_vars["ansible_ssh_pass"] = cred  # SSH password (sshpass)
                        managed_cred_vars["ansible_password"] = cred  # WinRM targets
                    secret_values.append(cred)
                if managed_become:
                    breq_id, bcred = await btapi_service.get_ps_credential_with_request(
                        managed_become["system_id"], managed_become["account_id"],
                        duration_min=_req_dur, uses_ssh_key=False)
                    managed_request_ids.append(breq_id)
                    managed_cred_vars["ansible_become_password"] = bcred
                    secret_values.append(bcred)
            except btapi_service.BTAPIError as e:
                job_service.set_failed(db, job_id, f"Password Safe checkout failed: {e}")
                return
            # Local / ACI runners consume everything inline via secret_extra_vars.
            secret_extra_vars.update(managed_cred_vars)
            secret_extra_vars.update(managed_plain_vars)

        # Per-target-cloud runner backend: an AWS-target job uses
        # ansible_runner_aws, Azure → ansible_runner_azure, GCP → ansible_runner_gcp,
        # each falling back to the global ansible_runner. The target cloud is the
        # run request's `cloud` field (operator-set for cloud targets; "" on-prem).
        runner = _cfg("ansible_runner") or "local"
        if cloud in ("aws", "azure", "gcp"):
            runner = _cfg(f"ansible_runner_{cloud}") or runner
        is_adhoc = "." in target or ":" in target
        is_playbook = ansible_local_service.asset_type(asset) == "playbook"

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
                    ssh_key_pem = await ansible_local_service.fetch_ssh_key(key_cloud)
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
                ssh_key_pem = await ansible_local_service.fetch_ssh_key(cloud)
                if not ssh_key_pem:
                    logger.warning("No SSH key configured for %s — proceeding without key", cloud)
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
) -> tuple:
    """Route to the configured cloud Ansible runner. Returns (exit_code, output).

    secret_entries/manifest_b64 (when present) carry per-provider secret refs — the
    runner injects each via the provider's secret channel and the container builds a
    0600 vars file from the manifest before running ansible-playbook."""
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
            secret_entries=secret_entries,
            manifest_b64=manifest_b64,
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
            secret_entries=secret_entries,
            manifest_b64=manifest_b64,
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
            service_account=_cfg("gcp_ansible_runner_service_account") or "",
            secret_entries=secret_entries,
            manifest_b64=manifest_b64,
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

    # Issue #16: with multi-backend storage, the same asset name can exist on
    # local *and* on a cloud backend. Cloud-side ansible runners (ECS task,
    # ACI, Cloud Run) cannot reach the dashboard's local filesystem, so refuse
    # the local-asset + cloud-target combo up front with an actionable error
    # rather than letting the runner blow up partway through.
    asset_backend = payload.asset_backend or storage_service.active_backend()
    is_cloud_target = bool(payload.cloud) or (is_adhoc and not payload.target.startswith(("10.", "192.168.", "172.")))
    runner = _cfg("ansible_runner") or "local"
    runs_in_cloud_runner = runner in ("ecs", "aci", "gcp")
    if asset_backend == "local" and (is_cloud_target or runs_in_cloud_runner):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Asset '{payload.asset}' lives on local filesystem storage, "
                f"which the cloud-side ansible runner cannot reach. Open the "
                f"Storage page and use the Move action to copy this asset to "
                f"a cloud backend (S3 / Azure Blob / GCS), then re-run the job."
            ),
        )

    # Using a Secrets-Management secret in a run requires the `secrets:use`
    # permission (admins bypass) — the operator never sees the value. Named-var and
    # become-password secrets work on both the local and the cloud runners; on the
    # cloud they are injected via the provider's secret channel (ECS valueFrom /
    # Cloud Run secret-env / ACI secure_value).
    has_managed = bool(payload.managed_account or payload.managed_become)
    wants_secret = bool(payload.secret_vars or payload.secret_become_source
                        or payload.secret_ssh_key_source or has_managed)
    if wants_secret and not _can_use_secrets(current_user):
        raise HTTPException(
            status_code=403,
            detail="Using a Secrets-Management secret in a run requires the 'secrets:use' permission.")

    # A managed-account checkout needs BeyondTrust Password Safe enabled.
    if has_managed:
        from ..services import config_service as cs
        if not cs.get_bool("beyondtrust_enabled"):
            raise HTTPException(
                status_code=400,
                detail="Managed-account checkout requires BeyondTrust Password Safe to be enabled in Settings.")

    atype = ansible_local_service.asset_type(payload.asset)

    # A cloud run only actually uses the cloud runner for bare-IP playbook targets;
    # otherwise it falls back to local. When it will run on ECS/GCP, every named/
    # become secret must already live in that cloud's store (fail fast with an
    # actionable move-it message rather than a mid-job failure). ACI takes the value
    # inline, so no store requirement.
    eff_runner = _effective_runner(payload.cloud)
    if (wants_secret and eff_runner in ("ecs", "aci", "gcp")
            and is_adhoc and atype == "playbook"):
        _validate_cloud_secret_stores(
            eff_runner, payload.secret_vars, payload.secret_become_source)

    # Managed-account checkout works on the local and ACI runners (both inject the
    # credential inline). ECS / Cloud Run reference a store secret, so a JIT-checked-
    # out credential needs an ephemeral, RBAC-locked store copy — gated behind an
    # explicit opt-in (it copies a PAM-vaulted credential into the cloud store for
    # the run). Rejected up front when that isn't enabled.
    from ..services import managed_accounts as _ma, config_service as _cs2
    if _ma.requires_ephemeral_store(has_managed, eff_runner, is_adhoc, atype == "playbook"):
        if not _cs2.get_bool("ansible_cloud_ephemeral_secrets_enabled"):
            raise HTTPException(
                status_code=400,
                detail=("Managed-account checkout on the ECS / Cloud Run runners requires "
                        "'Ephemeral cloud secrets' to be enabled in Settings (it briefly copies "
                        "the credential into the cloud store, RBAC-locked). Otherwise use the "
                        "local or Azure (ACI) runner."))
        if eff_runner == "gcp" and not _cfg("gcp_ansible_runner_service_account"):
            raise HTTPException(
                status_code=400,
                detail=("GCP ephemeral secrets require 'gcp_ansible_runner_service_account' to be "
                        "set — the Cloud Run job runs as that SA and read access to the ephemeral "
                        "secret is locked to it."))
    description = f"Ansible ({atype}): {payload.asset} → {payload.target}"

    job = job_service.create_job(
        db,
        job_type="ansible_local",
        description=description,
        workgroup="ansible",
        owner_id=current_user.id,
    )
    if wants_secret:
        # Audit the use — kinds + var names only, never the source refs or values.
        kinds = []
        if payload.secret_vars:
            kinds.append(f"{len(payload.secret_vars)} var(s)")
        if payload.secret_become_source:
            kinds.append("become-password")
        if payload.secret_ssh_key_source:
            kinds.append("ssh-key")
        # Managed-account use — record kind + account name(s) + system, never the credential.
        managed_accts = []
        if payload.managed_account:
            kinds.append("managed-account (checkout)")
            managed_accts.append({"role": "connection",
                                  "account": payload.managed_account.account_name,
                                  "system_id": payload.managed_account.system_id})
        if payload.managed_become:
            kinds.append("managed-account become (checkout)")
            managed_accts.append({"role": "become",
                                  "account": payload.managed_become.account_name,
                                  "system_id": payload.managed_become.system_id})
        job_service.log_audit(
            db, current_user.username, "ansible_secret_use",
            details={"kinds": kinds, "vars": sorted(payload.secret_vars.keys()),
                     "managed_accounts": managed_accts,
                     "asset": payload.asset, "target": payload.target})
    background_tasks.add_task(
        _run_job, job.id, payload.asset, payload.target, payload.cloud,
        payload.ansible_user, payload.extra_vars, asset_backend, payload.secret_vars,
        payload.secret_become_source, payload.secret_ssh_key_source,
        payload.managed_account.model_dump() if payload.managed_account else None,
        payload.managed_become.model_dump() if payload.managed_become else None,
    )
    return {"job_id": job.id, "status": "queued"}


@router.get("/secret-options")
async def list_secret_options(current_user: User = Depends(get_current_user)):
    """Secret sources the operator can use in a run — **names only, never
    values**. Requires ``secrets:use`` (admins bypass); the run form uses this to
    populate the secret picker."""
    if not _can_use_secrets(current_user):
        raise HTTPException(status_code=403, detail="The 'secrets:use' permission is required.")
    from ..services import config_service as cs
    from .secrets import _SECRET_REGISTRY

    cs._ensure_loaded()
    out = []
    for key, desc in _SECRET_REGISTRY:
        with cs._cache_lock:
            has = bool(cs._cache.get(key, ""))
        out.append({"key": key, "description": desc, "has_value": has})
    return out


@router.get("/managed-accounts")
async def list_managed_accounts(
    host: str,
    current_user: User = Depends(get_current_user),
):
    """Live BeyondTrust Password Safe managed-account list for a target host —
    **ids + names only, never credentials**. Requires ``secrets:use`` (using a
    managed account = checking out a credential without seeing it). The run form
    calls this on target change to populate the account picker.

    Returns ``{"enabled": false, "systems": []}`` when BeyondTrust is off (no
    ps-cli call), and never 500s a lookup — a ps-cli error yields an ``error`` note
    with an empty list so the UI can surface it inline."""
    if not _can_use_secrets(current_user):
        raise HTTPException(status_code=403, detail="The 'secrets:use' permission is required.")

    from ..services import config_service as cs, btapi_service, managed_accounts as ma

    # ephemeral_enabled tells the UI that managed accounts can run on ECS/GCP (via
    # the ephemeral store copy) and to nudge on change-after-release for those.
    ephemeral_enabled = cs.get_bool("ansible_cloud_ephemeral_secrets_enabled")
    if not cs.get_bool("beyondtrust_enabled"):
        return {"enabled": False, "ephemeral_enabled": ephemeral_enabled, "systems": []}

    host = (host or "").strip()
    if not host:
        return {"enabled": True, "ephemeral_enabled": ephemeral_enabled, "systems": []}

    ip = host if ma.host_is_ip(host) else ""
    name = "" if ma.host_is_ip(host) else host
    try:
        systems = await btapi_service.list_ps_managed_systems_by_ip_or_name(ip, name)
        accounts_by_system: dict = {}
        for s in systems:
            sid = s.get("ManagedSystemID") or s.get("SystemId") or s.get("SystemID")
            if sid is None:
                continue
            accounts_by_system[int(sid)] = \
                await btapi_service.list_ps_managed_accounts_with_fallback(int(sid))
        return {"enabled": True, "ephemeral_enabled": ephemeral_enabled,
                "systems": ma.normalize_managed_systems(systems, accounts_by_system)}
    except btapi_service.BTAPIError as exc:
        # Log the real ps-cli error server-side; return a generic reason. A raw
        # BTAPIError string carries ps-cli stderr, so returning it here would leak
        # internal detail to the caller — CodeQL py/stack-trace-exposure.
        logger.warning("managed-account lookup for %r failed: %s", host, exc)
        return {"enabled": True, "ephemeral_enabled": ephemeral_enabled, "systems": [],
                "error": "Password Safe lookup failed — check the BeyondTrust configuration and server logs."}


@router.get("/drift")
async def config_drift_report(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Per-target config-drift signals for the Ansible stream: **unverified**
    (last apply older than ``config_drift_stale_days``) and **changed** (the
    stored playbook's current content differs from what was applied). Read-only —
    computed from the ``config_apply_state`` rows recorded on each successful run."""
    import base64
    from ..services import config_drift, config_service as cs
    from ..config import settings
    from ..database import ConfigApplyState

    try:
        stale_days = int(cs.get("config_drift_stale_days")
                         or getattr(settings, "config_drift_stale_days", 14) or 14)
    except (TypeError, ValueError):
        stale_days = 14

    rows = db.query(ConfigApplyState).all()
    row_dicts = [{
        "target": r.target, "playbook_ref": r.playbook_ref,
        "content_hash": r.content_hash, "applied_at": r.applied_at, "job_id": r.job_id,
    } for r in rows]

    # Current content hash per distinct playbook (for change detection). Best-effort
    # — an asset that's since been deleted/unreadable just yields no change signal.
    current: dict = {}
    for ref in {r.playbook_ref for r in rows}:
        try:
            b64 = await storage_service.fetch_asset_b64(ref)
            current[ref] = config_drift.content_hash(base64.b64decode(b64))
        except Exception:
            pass

    return config_drift.evaluate(row_dicts, current, stale_days)
