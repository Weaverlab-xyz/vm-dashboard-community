"""
MCP (Model Context Protocol) server — exposes dashboard read-only tools to AI clients.

Transport: HTTP Streamable (SSE), mounted at /mcp in main.py.
Auth:      Bearer PAT (vmcli_<64hex>) validated against personal_access_tokens table.
Gate:      ``mcp_server_enabled`` (default off) — checked in :class:`_MCPAuth` before auth.

Any MCP-compatible client (Claude Desktop, Claude Code, Cursor, etc.) can connect:

    {
      "mcpServers": {
        "vm-dashboard": {
          "url": "http://localhost:8001/mcp",
          "headers": {"Authorization": "Bearer vmcli_<your-pat>"}
        }
      }
    }

WHY EVERY TOOL STARTS WITH ``_caller()``
----------------------------------------
A PAT is not an MCP credential — ``api/auth.py`` accepts the same token on the whole REST
API, where every endpoint applies its own RBAC. This surface used to be the one place a PAT
escaped that: ``_mcp_user`` was set and never read, so any active user's token listed every
job in the estate and ``get_job`` returned the raw deploy payload with it.

Two rules keep that from coming back, and both matter more than they look:

* **Fail closed on identity.** ``_MCPAuth`` sets the ContextVar and awaits the wrapped app;
  under ``mcp.sse_app()`` the session loop — and so tool dispatch — runs inside the
  authenticated ``GET /sse`` request's task, which is why the value propagates. Nothing here
  *depends* on that: a tool that cannot name its caller returns ``_UNAUTHENTICATED`` rather
  than querying. The pin is ``mcp>=1.2.0``, an unpinned floor over transports that are still
  moving, so if propagation ever breaks this server stops working loudly instead of quietly
  going unscoped.
* **Never re-derive a rule — call the one the HTTP twin calls.** Each tool below names its
  twin. The imports are function-local because ``api.aws`` and friends pull in the world.

The rule a reader is most likely to "tidy" and must not: **the app has two admin rules.**
The four cloud consoles key on ``user.is_admin``; inventory, databases, k8s, functions and
expiry key on ``user.is_effective_admin``, which is a superset that also honours a
session-permissions row and a live Entitle JIT grant. Unifying them here would silently
change somebody's access in a place nobody would look. ``tests/test_dashboard_stats_api.py``
pins the same split for the dashboard tiles, and ``tests/test_mcp_rbac.py`` pins it here.
"""
import contextvars
import hashlib
import json
import logging
from datetime import datetime
from typing import Any, Callable, Optional

from mcp.server.fastmcp import FastMCP

from ..database import (
    Job,
    PersonalAccessToken,
    SessionLocal,
    User,
)
from ..services import config_service

logger = logging.getLogger(__name__)

# ── Auth context ──────────────────────────────────────────────────────────────

_mcp_user: contextvars.ContextVar[Optional[User]] = contextvars.ContextVar(
    "mcp_user", default=None
)

FLAG = "mcp_server_enabled"

_UNAUTHENTICATED = {
    "error": "Not authenticated. The MCP server could not identify the calling user; "
             "reconnect with a valid Personal Access Token."
}


def _caller() -> Optional[User]:
    """The authenticated user for this MCP session, or None.

    None is a hard stop for every tool — see the module docstring.
    """
    return _mcp_user.get()


def _forbidden(scope: str, level: str) -> dict:
    return {"error": f"Requires '{scope}:{level}' permission."}


def _has_permission(user: User, scope: str, level: str) -> bool:
    """Mirror of ``api.auth.require_permission``'s check, as a predicate.

    Including its backward-compatibility clause: an empty ``effective_permissions_dict``
    means unrestricted (pre-OIDC / pre-admin-set users). Being stricter here than the UI
    would lock those users out of a surface they can already reach through the REST API.
    """
    if getattr(user, "is_effective_admin", False):
        return True
    perms = user.effective_permissions_dict or {}
    if not perms:
        return True
    return level in perms.get(scope, [])


# ── Scoping helpers — each delegates, none re-derives ──────────────────────────


def _cloud_workgroups(user: User):
    """Workgroups for the four cloud consoles, or None for admins.

    Keyed on ``is_admin``, matching ``api/aws.py``, ``api/azure.py``, ``api/gcp.py`` and
    ``api/oci.py``. NOT ``is_effective_admin`` — see the module docstring.
    """
    if getattr(user, "is_admin", False):
        return None
    return [w.lower() for w in (user.workgroups_list or [])]


def _effective_workgroups(user: User):
    """Workgroups for inventory and expiry, or None for effective admins. Delegates."""
    from ..services import inventory_service
    return inventory_service.accessible_workgroups(user)


def _creator_scoped(rows: list, user: User) -> list:
    """The rule ``api/cloud_databases.py``, ``api/k8s.py`` and ``api/cloud_functions.py``
    all apply: non-effective-admins see only what they created."""
    if getattr(user, "is_effective_admin", False):
        return rows
    return [r for r in rows if r.get("created_by") == user.username]


# ── extra_data redaction ──────────────────────────────────────────────────────
#
# An ALLOWLIST, deliberately. `extra_data` is the deploy result dict and it grows every time
# an integration is added — a denylist fails open on the next one. What it carries today
# includes `bt_tf_state` (the Terraform state of the VM's PRA Shell Jump),
# `ps_registration_tf_state`, `ssh_secret_name`, `admin_password_ref` and
# `admin_password_backend`. None of that belongs in an AI client's context window.
#
# Keys not listed here are dropped silently. An operator who needs the full payload has the
# REST API, where the same PAT is subject to the same permission check.

_SAFE_EXTRA_KEYS = frozenset({
    # identity / naming
    "instance_id", "instance_name", "display_name", "vm_name", "name", "ocid",
    # placement
    "region", "zone", "location", "resource_group", "project_id", "compartment_id",
    # shape + image
    "machine_type", "instance_type", "ami_id", "image_id", "image_reference",
    # addressing (the twins return these on their own list endpoints)
    "public_ip", "private_ip", "hostname",
    # state
    "status", "state", "os_type", "ssh_user",
    # non-VM resources
    "cluster_name", "db_identifier", "engine", "endpoint",
})

# Belt and braces over the allowlist above: a key added carelessly in future that reads as
# a credential is dropped anyway. tests/test_mcp_rbac.py asserts the two never disagree —
# i.e. that no member of _SAFE_EXTRA_KEYS matches one of these.
_SENSITIVE_SUBSTRINGS = (
    "password", "secret", "token", "credential", "tf_state", "private_key", "passphrase",
)


def _is_sensitive_key(key: str) -> bool:
    low = key.lower()
    return any(bit in low for bit in _SENSITIVE_SUBSTRINGS)


def _safe_extra(data: Any) -> dict:
    """Allowlist-filter a deploy payload. Anything unrecognised is dropped."""
    if not isinstance(data, dict):
        return {}
    return {
        k: v for k, v in data.items()
        if k in _SAFE_EXTRA_KEYS and not _is_sensitive_key(k)
    }


def _load_extra(job: Job) -> dict:
    if not job.extra_data:
        return {}
    try:
        parsed = json.loads(job.extra_data)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


# ── Helpers ───────────────────────────────────────────────────────────────────


def _fmt_dt(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def _job_dict(job: Job) -> dict:
    return {
        "id": job.id,
        "type": job.job_type,
        "status": job.status,
        "workgroup": job.workgroup,
        "vm_path": job.vm_path,
        "progress_pct": job.progress_pct,
        "progress_message": job.progress_message,
        "created_by": job.created_by,
        "created_at": _fmt_dt(job.created_at),
        "started_at": _fmt_dt(job.started_at),
        "completed_at": _fmt_dt(job.completed_at),
        "error_message": job.error_message,
        "duration_seconds": job.duration_seconds,
    }


def _deploy_rows(db, job_types: tuple, accessible, mapper) -> list:
    """Completed deploy jobs of the given types, workgroup-scoped, mapped to rows.

    The four cloud instance tools all derive from job records rather than calling the
    provider, which is what their HTTP twins do only as a fallback. That is deliberate and
    predates this file: an MCP tool that needs live credentials fails for reasons the caller
    cannot act on, and the job row is the dashboard's own record of what it built. The
    workgroup check lives here so all four clouds cannot drift apart.
    """
    jobs = (
        db.query(Job)
        .filter(Job.job_type.in_(job_types), Job.status == "completed")
        .order_by(Job.created_at.desc())
        .all()
    )
    rows = []
    for job in jobs:
        if accessible is not None and (job.workgroup or "").lower() not in accessible:
            continue
        item = mapper(job, _load_extra(job))
        if item:
            rows.append(item)
    return rows


# ── Tools: jobs ───────────────────────────────────────────────────────────────

mcp = FastMCP(
    "Infrastructure Dashboard",
    instructions=(
        "Read-only access to the VM Infrastructure Dashboard, scoped to the permissions of "
        "the user whose Personal Access Token is in use. Results are filtered exactly as the "
        "web UI filters them for that user, so a tool returning nothing may mean 'none "
        "visible to you' rather than 'none exist'. All operations are non-destructive — "
        "deploy/start/stop actions must be performed through the web UI or REST API."
    ),
)


@mcp.tool()
async def dashboard_summary() -> dict:
    """
    Return a high-level summary of the dashboard: active job count, recent
    failures, and which integrations are enabled.
    """
    user = _caller()
    if user is None:
        return _UNAUTHENTICATED

    from .auth import can_audit_jobs
    owner = None if can_audit_jobs(user) else user.username

    db = SessionLocal()
    try:
        def _scoped(q):
            return q if owner is None else q.filter(Job.created_by == owner)

        running = _scoped(
            db.query(Job).filter(Job.status.in_(["pending", "running"]))
        ).count()
        failed_today = _scoped(
            db.query(Job).filter(
                Job.status == "failed",
                Job.created_at >= datetime.utcnow().replace(
                    hour=0, minute=0, second=0, microsecond=0
                ),
            )
        ).count()
        total = _scoped(db.query(Job)).count()
    finally:
        db.close()

    return {
        "active_jobs": running,
        "failed_today": failed_today,
        "total_jobs": total,
        "scope": "all jobs" if owner is None else f"jobs created by {owner}",
        # Integration state matches GET /api/features, which is unauthenticated, so this
        # discloses nothing the product does not already publish.
        "features": {
            "vmware": config_service.get_bool("vmware_enabled", False),
            # Three keys, never an OR of them: an agent told "beyondtrust: true"
            # because only EPM-L is on would go on to attempt a Password Safe
            # checkout and get a 400 back.
            "password_safe": config_service.get_bool("password_safe_enabled", False),
            "pra": config_service.get_bool("pra_enabled", False),
            "epml": config_service.get_bool("epml_enabled", False),
            "portainer": config_service.get_bool("portainer_enabled", False),
            "ansible": config_service.get_bool("ansible_enabled", False),
            "aws": bool(config_service.get("aws_access_key_id") or config_service.get("aws_region")),
            "azure": bool(config_service.get("azure_client_id") or config_service.get("azure_subscription_id")),
        },
    }


@mcp.tool()
async def list_jobs(
    status: Optional[str] = None,
    workgroup: Optional[str] = None,
    limit: int = 20,
) -> list[dict]:
    """
    List recent jobs visible to you. Optionally filter by status
    (pending/running/completed/failed/cancelled) and/or workgroup. Returns at most `limit`
    jobs (max 100), newest first.
    """
    user = _caller()
    if user is None:
        return [_UNAUTHENTICATED]
    if not _has_permission(user, "jobs", "read"):
        return [_forbidden("jobs", "read")]

    # Twin: api/jobs.py — owner_filter = None if can_audit_jobs(user) else user.username.
    from .auth import can_audit_jobs
    owner = None if can_audit_jobs(user) else user.username

    limit = min(max(1, limit), 100)
    db = SessionLocal()
    try:
        q = db.query(Job)
        if owner is not None:
            q = q.filter(Job.created_by == owner)
        if status:
            q = q.filter(Job.status == status)
        if workgroup:
            q = q.filter(Job.workgroup == workgroup)
        jobs = q.order_by(Job.created_at.desc()).limit(limit).all()
        return [_job_dict(j) for j in jobs]
    finally:
        db.close()


@mcp.tool()
async def get_job(job_id: str) -> dict:
    """
    Return details for a single job by its UUID. Deploy payloads are filtered to a
    non-sensitive subset — use the REST API if you need the full record.
    """
    user = _caller()
    if user is None:
        return _UNAUTHENTICATED
    if not _has_permission(user, "jobs", "read"):
        return _forbidden("jobs", "read")

    from .auth import can_audit_jobs
    owner = None if can_audit_jobs(user) else user.username

    db = SessionLocal()
    try:
        q = db.query(Job).filter(Job.id == job_id)
        if owner is not None:
            q = q.filter(Job.created_by == owner)
        job = q.first()
        # Same answer for "does not exist" and "not yours", so this cannot be used to
        # probe which job ids exist — the reasoning api/jobs.py applies to batch summaries.
        if not job:
            return {"error": f"Job {job_id!r} not found"}
        result = _job_dict(job)
        extra = _safe_extra(_load_extra(job))
        if extra:
            result["extra_data"] = extra
        return result
    finally:
        db.close()


# ── Tools: cloud instances (job-derived, workgroup-scoped) ────────────────────


def _ec2_row(job, data):
    if not data.get("instance_id"):
        return None
    return {
        "instance_id": data["instance_id"], "job_id": job.id, "workgroup": job.workgroup,
        "created_by": job.created_by, "deployed_at": _fmt_dt(job.completed_at),
        "region": data.get("region", ""), "ami_id": data.get("ami_id", ""),
        "public_ip": data.get("public_ip", ""),
    }


@mcp.tool()
async def list_ec2_instances() -> dict:
    """List EC2 instances deployed via this dashboard that are visible to you."""
    user = _caller()
    if user is None:
        return _UNAUTHENTICATED
    if not _has_permission(user, "aws", "read"):
        return _forbidden("aws", "read")

    db = SessionLocal()
    try:
        instances = _deploy_rows(
            db, ("ec2_deploy", "ec2_bulk_deploy"), _cloud_workgroups(user), _ec2_row)
    finally:
        db.close()
    if not instances:
        return {"instances": [], "note": "No completed EC2 deploy jobs visible to you."}
    return {"instances": instances}


@mcp.tool()
async def list_azure_vms(resource_group: Optional[str] = None) -> dict:
    """List Azure VMs deployed via this dashboard that are visible to you."""
    user = _caller()
    if user is None:
        return _UNAUTHENTICATED
    if not _has_permission(user, "azure", "read"):
        return _forbidden("azure", "read")

    def _row(job, data):
        if not data.get("vm_name"):
            return None
        rg = data.get("resource_group", "")
        if resource_group and rg != resource_group:
            return None
        return {
            "vm_name": data["vm_name"], "resource_group": rg, "job_id": job.id,
            "workgroup": job.workgroup, "created_by": job.created_by,
            "deployed_at": _fmt_dt(job.completed_at), "location": data.get("location", ""),
            "image": data.get("image_reference", ""), "public_ip": data.get("public_ip", ""),
        }

    db = SessionLocal()
    try:
        # "azure_vm_deploy" was never a job type — it appeared only here, so every
        # single-VM Azure deploy was invisible to this tool. The real type is azure_deploy.
        vms = _deploy_rows(
            db, ("azure_deploy", "azure_bulk_deploy"), _cloud_workgroups(user), _row)
    finally:
        db.close()
    if not vms:
        return {"vms": [], "note": "No completed Azure VM deploy jobs visible to you."}
    return {"vms": vms}


@mcp.tool()
async def list_gcp_instances() -> dict:
    """List GCE instances deployed via this dashboard that are visible to you."""
    user = _caller()
    if user is None:
        return _UNAUTHENTICATED
    if not _has_permission(user, "gcp", "read"):
        return _forbidden("gcp", "read")

    def _row(job, data):
        name = data.get("instance_name") or data.get("name")
        if not name:
            return None
        return {
            "instance_name": name, "job_id": job.id, "workgroup": job.workgroup,
            "created_by": job.created_by, "deployed_at": _fmt_dt(job.completed_at),
            "zone": data.get("zone", ""), "machine_type": data.get("machine_type", ""),
            "public_ip": data.get("public_ip", ""),
        }

    db = SessionLocal()
    try:
        rows = _deploy_rows(
            db, ("gce_deploy", "gce_bulk_deploy"), _cloud_workgroups(user), _row)
    finally:
        db.close()
    if not rows:
        return {"instances": [], "note": "No completed GCE deploy jobs visible to you."}
    return {"instances": rows}


@mcp.tool()
async def list_oci_instances() -> dict:
    """List OCI compute instances deployed via this dashboard that are visible to you."""
    user = _caller()
    if user is None:
        return _UNAUTHENTICATED
    if not _has_permission(user, "oci", "read"):
        return _forbidden("oci", "read")

    def _row(job, data):
        ident = data.get("ocid") or data.get("instance_id")
        if not ident:
            return None
        return {
            "ocid": ident, "display_name": data.get("display_name", ""), "job_id": job.id,
            "workgroup": job.workgroup, "created_by": job.created_by,
            "deployed_at": _fmt_dt(job.completed_at), "region": data.get("region", ""),
            "public_ip": data.get("public_ip", ""),
        }

    db = SessionLocal()
    try:
        rows = _deploy_rows(
            db, ("oci_deploy", "oci_bulk_deploy"), _cloud_workgroups(user), _row)
    finally:
        db.close()
    if not rows:
        return {"instances": [], "note": "No completed OCI deploy jobs visible to you."}
    return {"instances": rows}


@mcp.tool()
async def list_amis(region: Optional[str] = None) -> dict:
    """
    List AWS AMIs owned by the configured account. Requires AWS credentials to be
    configured in the dashboard wizard.
    """
    user = _caller()
    if user is None:
        return _UNAUTHENTICATED
    if not _has_permission(user, "aws", "read"):
        return _forbidden("aws", "read")
    try:
        from ..services.aws_service import list_amis as _list_amis
        # aws_service.list_amis takes region as a required positional; passing the caller's
        # None straight through was a TypeError waiting for the first argument-less call.
        from .aws import _aws_region
        amis = await _list_amis(region or _aws_region())
        return {"amis": amis}
    except Exception as exc:
        return {"error": str(exc)}


# ── Tools: on-prem ────────────────────────────────────────────────────────────


@mcp.tool()
async def list_vms(workgroup: Optional[str] = None) -> dict:
    """
    List VMware Workstation VMs a remote agent has synced, with their power state.
    Only available when VMware is enabled.
    """
    user = _caller()
    if user is None:
        return _UNAUTHENTICATED
    if not _has_permission(user, "vms", "read"):
        return _forbidden("vms", "read")
    if not config_service.get_bool("vmware_enabled", False):
        return {"error": "VMware integration is not enabled on this dashboard"}

    # This used to import `..services.vm_service`, which has never existed in this repo —
    # so `except ImportError` returned "VMware service not available" on every call and
    # the tool has never once listed a VM. It reads the synced cache now, the same source
    # /api/vms lists from.
    db = SessionLocal()
    try:
        from ..services import (hypervisor_sync_service, hypervisor_view_service,
                                workgroup_override_service)
        from ..database import HypervisorConnection

        conns = (db.query(HypervisorConnection)
                 .filter(HypervisorConnection.kind == "workstation",
                         HypervisorConnection.is_active.is_(True)).all())
        vms = []
        for conn in conns:
            for vm in hypervisor_view_service.project(
                    "workstation", hypervisor_sync_service.list_vms(db, conn.id)):
                vms.append({
                    "vm_id": vm.get("vm_id"),
                    "name": vm.get("name"),
                    "state": "running" if vm.get("is_running") else "stopped",
                    "os_type": vm.get("os_type") or "",
                    "ip_addresses": vm.get("ip_addresses") or [],
                    "vmx_path": vm.get("vmx_path") or "",
                    "connection": conn.name,
                })

        # A workgroup on these rows is an admin-assigned override, which is also what
        # decides who may SEE the row: api/vms.py refuses to act on an untagged VM for
        # anyone but an admin, so an untagged VM must not be listed to one either. An
        # agent can report any VM it likes; none of them widen a non-admin's view until
        # someone tags them.
        overrides = workgroup_override_service.get_many(
            db, "workstation", [v["vm_id"] for v in vms])
        accessible = _cloud_workgroups(user)
        if accessible is not None:
            vms = [v for v in vms
                   if (overrides.get(v["vm_id"]) or "").lower() in accessible]
        if workgroup:
            wanted = workgroup.strip().lower()
            vms = [v for v in vms
                   if (overrides.get(v["vm_id"]) or "").lower() == wanted]
        return {"vms": vms, "count": len(vms)}
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        db.close()


@mcp.tool()
async def list_containers(endpoint_id: int, all_containers: bool = True) -> dict:
    """
    List containers cached for one Portainer endpoint. `endpoint_id` is required, exactly
    as it is on GET /api/containers.
    """
    user = _caller()
    if user is None:
        return _UNAUTHENTICATED
    if not _has_permission(user, "containers", "read"):
        return _forbidden("containers", "read")

    db = SessionLocal()
    try:
        from ..services import container_inventory_service
        rows = container_inventory_service.get_containers_from_db(
            db, endpoint_id, all_containers=all_containers)
        # ContainerInfo carries `names` (Docker's list), not `name`.
        return {"containers": [
            {"id": c.short_id, "names": c.names, "image": c.image, "state": c.state,
             "status": c.status, "ports": c.ports} for c in rows], "count": len(rows)}
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        db.close()


# ── Tools: creator-scoped resources ───────────────────────────────────────────


@mcp.tool()
async def list_databases() -> dict:
    """List cloud databases visible to you (non-admins see only what they created)."""
    user = _caller()
    if user is None:
        return _UNAUTHENTICATED
    if not _has_permission(user, "cloud_database", "read"):
        return _forbidden("cloud_database", "read")

    db = SessionLocal()
    try:
        from ..services import cloud_database_service
        rows = _creator_scoped(cloud_database_service.list_databases(db), user)
        return {"databases": rows, "count": len(rows)}
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        db.close()


@mcp.tool()
async def list_k8s_clusters() -> dict:
    """List Kubernetes clusters visible to you (non-admins see only what they created)."""
    user = _caller()
    if user is None:
        return _UNAUTHENTICATED
    if not _has_permission(user, "k8s", "read"):
        return _forbidden("k8s", "read")

    db = SessionLocal()
    try:
        from ..services import k8s_service
        rows = _creator_scoped(k8s_service.list_clusters(db), user)
        return {"clusters": rows, "count": len(rows)}
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        db.close()


@mcp.tool()
async def list_functions() -> dict:
    """List cloud functions visible to you (non-admins see only what they deployed)."""
    user = _caller()
    if user is None:
        return _UNAUTHENTICATED
    if not _has_permission(user, "cloud_function", "read"):
        return _forbidden("cloud_function", "read")

    db = SessionLocal()
    try:
        from ..services import cloud_function_service
        rows = _creator_scoped(cloud_function_service.list_functions(db), user)
        return {"functions": rows, "count": len(rows)}
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        db.close()


# ── Tools: cross-provider inventory + expiry ──────────────────────────────────


@mcp.tool()
async def list_inventory(provider: Optional[str] = None,
                         kind: Optional[str] = None) -> dict:
    """
    Every resource the dashboard knows about that is visible to you, normalised across
    providers. Filter by `provider` (aws, azure, gcp, oci, proxmox, nutanix, vsphere,
    xcpng, hyperv, workstation) and/or `kind` (vm, database, k8s, desktop).
    """
    user = _caller()
    if user is None:
        return _UNAUTHENTICATED

    db = SessionLocal()
    try:
        from ..services import inventory_service
        # inventory_service's own docstring: "RBAC filtering is the API layer's job (see
        # visible_to), not the collector's." For this surface, that layer is here.
        accessible = inventory_service.accessible_workgroups(user)
        items = [i for i in inventory_service.collect(db)
                 if inventory_service.visible_to(i, accessible, user.username)]
        if provider:
            items = [i for i in items if i.get("cloud") == provider.lower()]
        if kind:
            items = [i for i in items if i.get("kind") == kind.lower()]
        return {"items": items, "count": len(items)}
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        db.close()


@mcp.tool()
async def list_expiring() -> dict:
    """Resources carrying an auto-delete timer that are visible to you, and the
    current state of the timer's gates."""
    user = _caller()
    if user is None:
        return _UNAUTHENTICATED

    db = SessionLocal()
    try:
        from ..services import expiry_policy, expiry_reaper, inventory_service
        accessible = inventory_service.accessible_workgroups(user)
        items = [i for i in inventory_service.collect(db)
                 if i.get("expires_at")
                 and inventory_service.visible_to(i, accessible, user.username)]
        return {
            "items": items,
            "count": len(items),
            "expiry": {
                "enabled": expiry_policy.enabled(),
                "enforce": expiry_policy.enforce(),
                "dry_run": expiry_policy.dry_run(),
                "warn_hours": expiry_policy.warn_hours(),
                "deleting": expiry_reaper.status()["deleting"],
            },
        }
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        db.close()


@mcp.tool()
async def config_drift() -> dict:
    """Per-target config-drift signals: targets unverified since their last apply, and
    targets whose stored playbook now differs from what was applied."""
    user = _caller()
    if user is None:
        return _UNAUTHENTICATED

    db = SessionLocal()
    try:
        from ..services import config_drift as _drift
        return await _drift.collect(db)
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        db.close()


# ── Tools: admin-only ─────────────────────────────────────────────────────────


def _admin_only(user: User) -> Optional[dict]:
    """Twins: api/agent.py, api/costs.py and api/secrets.py all use require_admin."""
    if not getattr(user, "is_effective_admin", False):
        return {"error": "Admin access required."}
    return None


@mcp.tool()
async def list_agents() -> dict:
    """Every registered remote agent, with derived status and running-job count.
    Admin only."""
    user = _caller()
    if user is None:
        return _UNAUTHENTICATED
    denied = _admin_only(user)
    if denied:
        return denied

    db = SessionLocal()
    try:
        from ..database import RemoteAgent
        from .agent import _agent_row
        agents = db.query(RemoteAgent).order_by(RemoteAgent.created_at.desc()).all()
        counts: dict = {}
        for (agent_id,) in db.query(Job.agent_id).filter(
                Job.agent_id.isnot(None), Job.status == "running").all():
            counts[agent_id] = counts.get(agent_id, 0) + 1
        return {"agents": [_agent_row(a, counts.get(a.id, 0)) for a in agents]}
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        db.close()


@mcp.tool()
async def cost_summary() -> dict:
    """Per-cloud month-to-date spend plus budget alerts. Admin only.

    Reads the durable cost cache — it does not force a live requery, because the billing
    APIs behind it are metered (AWS Cost Explorer bills per request) and an AI client
    polling a tool is exactly the traffic shape that runs a bill up.
    """
    user = _caller()
    if user is None:
        return _UNAUTHENTICATED
    denied = _admin_only(user)
    if denied:
        return denied
    try:
        from ..services import cost_cache, cost_service
        data = await cost_cache.get_summary(refresh=False)
        return cost_service.apply_budget_alerts(data)
    except Exception as exc:
        return {"error": str(exc)}


@mcp.tool()
async def secret_staleness() -> dict:
    """Per-secret age and staleness for the config-secret registry. Admin only."""
    user = _caller()
    if user is None:
        return _UNAUTHENTICATED
    denied = _admin_only(user)
    if denied:
        return denied

    db = SessionLocal()
    try:
        from ..services import secret_hygiene
        return secret_hygiene.collect(db)
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        db.close()


# ── Auth middleware (pure ASGI — no BaseHTTPMiddleware to avoid SSE buffering) ─


def _validate_pat(raw_token: str) -> Optional[User]:
    """Synchronous PAT validation — runs in a thread via the ASGI wrapper."""
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    db = SessionLocal()
    try:
        pat = (
            db.query(PersonalAccessToken)
            .filter(
                PersonalAccessToken.token_hash == token_hash,
                PersonalAccessToken.is_active == True,  # noqa: E712
            )
            .first()
        )
        if not pat:
            return None
        if pat.expires_at and pat.expires_at < datetime.utcnow():
            return None
        pat.last_used_at = datetime.utcnow()
        db.commit()
        user = (
            db.query(User)
            .filter(User.id == pat.user_id, User.is_active == True)  # noqa: E712
            .first()
        )
        return user
    finally:
        db.close()


class _MCPAuth:
    """
    Pure-ASGI feature gate + authentication wrapper for the MCP app.
    Using a raw ASGI callable (not BaseHTTPMiddleware) so that SSE streams
    are not buffered by the response wrapper.

    The gate lives here rather than as a ``_feature_gate`` dependency because ``/mcp`` is
    an ``app.mount()`` of a raw ASGI app, and a mount takes no dependencies. It still
    resolves through ``feature_flags.enabled``, so there is one reader of the flag — which
    is the whole point of ``_feature_gate``'s existence.
    """

    def __init__(self, app: Callable) -> None:
        self._app = app

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] == "lifespan":
            await self._app(scope, receive, send)
            return

        # Gate before auth: a disabled server should not even tell you whether a token
        # is valid.
        from ..services import feature_flags
        if not feature_flags.enabled(FLAG):
            await self._send_404(send, scope)
            return

        # Extract Authorization header
        headers = dict(scope.get("headers", []))
        auth = headers.get(b"authorization", b"").decode("utf-8", errors="replace")

        if not auth.startswith("Bearer "):
            await self._send_401(send, scope)
            return

        raw_token = auth[7:]
        if not raw_token.startswith("vmcli_"):
            await self._send_401(send, scope)
            return

        import asyncio
        user = await asyncio.get_event_loop().run_in_executor(None, _validate_pat, raw_token)
        if not user:
            await self._send_401(send, scope)
            return

        _mcp_user.set(user)
        await self._app(scope, receive, send)

    @staticmethod
    async def _send_json(send: Callable, scope: dict, status: int, body: bytes,
                         extra_headers: Optional[list] = None) -> None:
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [[b"content-type", b"application/json"]] + (extra_headers or []),
        })
        await send({"type": "http.response.body", "body": body})

    @classmethod
    async def _send_401(cls, send: Callable, scope: dict) -> None:
        await cls._send_json(
            send, scope, 401,
            b'{"detail":"Missing or invalid PAT. Create one at /settings."}',
            [[b"www-authenticate", b'Bearer realm="vm-dashboard"']],
        )

    @classmethod
    async def _send_404(cls, send: Callable, scope: dict) -> None:
        await cls._send_json(
            send, scope, 404,
            b'{"detail":"The MCP server is not enabled on this dashboard."}',
        )


# ── Public factory ────────────────────────────────────────────────────────────


def get_mcp_asgi_app() -> Callable:
    """Return the MCP ASGI app wrapped with the feature gate and PAT authentication."""
    raw_app = mcp.sse_app()
    return _MCPAuth(raw_app)
