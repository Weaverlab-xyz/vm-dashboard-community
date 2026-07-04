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

class RunRequest(BaseModel):
    asset: str           # filename of any supported type (.yml, .sh, .deb, .rpm)
    target: str          # on-prem group key OR bare IP/hostname for cloud/ad-hoc
    cloud: str = ""      # "" | "aws" | "azure" | "gcp" — drives SSH key retrieval
    ansible_user: str = ""  # SSH user for cloud runner targets; falls back to ansible_default_user
    extra_vars: dict = {}
    # Optional: inject Secrets-Management secrets as Ansible vars — {var_name: source},
    # source = a config-secret registry key or a raw vault ref (bt_safe:// …).
    # Admin-only (injecting a secret == reading it); local runner only.
    secret_vars: dict = {}
    # Which storage backend the asset should be fetched from. Empty = active
    # backend (back-compat). With multi-backend support (issue #16), the UI
    # passes the backend explicitly because the same asset name may exist on
    # multiple backends.
    asset_backend: str = ""


def _cfg(key: str) -> str:
    return ansible_local_service._cfg(key)


async def _run_job(
    job_id: str,
    asset: str,
    target: str,
    cloud: str,
    ansible_user: str,
    extra_vars: dict,
    asset_backend: str = "",
    secret_vars: dict | None = None,
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

        # Resolve any Secrets-Management secrets JIT (never stored on the job /
        # never on the command line — passed via a 0600 tmpfile inside run_playbook).
        secret_extra_vars = {}
        if secret_vars:
            from ..services import ansible_secrets, config_service as cs
            secret_extra_vars = ansible_secrets.resolve_secret_vars(
                secret_vars, get=cs.get, resolve_reference=cs.resolve_reference,
                is_reference=cs.is_reference)

        job_service.update_progress(db, job_id, 20, f"Running {asset} against {target}…")
        output, rc = await ansible_local_service.run_playbook(
            asset_b64=asset_b64,
            target=target,
            extra_vars=extra_vars or None,
            asset_name=asset,
            ssh_key_pem=ssh_key_pem,
            secret_extra_vars=secret_extra_vars or None,
        )

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

    # Secret injection is admin-only — injecting a secret into a run you control is
    # equivalent to reading it — and local-runner only in this release.
    if payload.secret_vars:
        if not current_user.is_admin:
            raise HTTPException(
                status_code=403,
                detail="Injecting Secrets-Management secrets into a run requires an admin.")
        if runs_in_cloud_runner:
            raise HTTPException(
                status_code=400,
                detail="Secret injection is only supported on the local Ansible runner in this release.")

    atype = ansible_local_service.asset_type(payload.asset)
    description = f"Ansible ({atype}): {payload.asset} → {payload.target}"

    job = job_service.create_job(
        db,
        job_type="ansible_local",
        description=description,
        workgroup="ansible",
        owner_id=current_user.id,
    )
    if payload.secret_vars:
        # Audit the use (var names only — never the source refs or values).
        job_service.log_audit(
            db, current_user.username, "ansible_secret_inject",
            details={"vars": sorted(payload.secret_vars.keys()),
                     "count": len(payload.secret_vars),
                     "asset": payload.asset, "target": payload.target})
    background_tasks.add_task(
        _run_job, job.id, payload.asset, payload.target, payload.cloud,
        payload.ansible_user, payload.extra_vars, asset_backend, payload.secret_vars,
    )
    return {"job_id": job.id, "status": "queued"}


@router.get("/secret-options")
async def list_secret_options(current_user: User = Depends(get_current_user)):
    """Secret sources the operator can inject into a run — **names only, never
    values**. Admin-only, matching who may read secrets; the run form uses this to
    populate the secret picker."""
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Admin required to inject secrets.")
    from ..services import config_service as cs
    from .secrets import _SECRET_REGISTRY

    cs._ensure_loaded()
    out = []
    for key, desc in _SECRET_REGISTRY:
        with cs._cache_lock:
            has = bool(cs._cache.get(key, ""))
        out.append({"key": key, "description": desc, "has_value": has})
    return out


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
