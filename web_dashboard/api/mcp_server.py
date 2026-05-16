"""
MCP (Model Context Protocol) server — exposes dashboard read-only tools to AI clients.

Transport: HTTP Streamable (SSE), mounted at /mcp in main.py.
Auth:      Bearer PAT (vmcli_<64hex>) validated against personal_access_tokens table.

Any MCP-compatible client (Claude Desktop, Claude Code, Cursor, etc.) can connect:

    {
      "mcpServers": {
        "vm-dashboard": {
          "url": "http://localhost:8001/mcp",
          "headers": {"Authorization": "Bearer vmcli_<your-pat>"}
        }
      }
    }
"""
import contextvars
import hashlib
import json
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

# ── Auth context ──────────────────────────────────────────────────────────────

_mcp_user: contextvars.ContextVar[Optional[User]] = contextvars.ContextVar(
    "mcp_user", default=None
)

# ── FastMCP instance ──────────────────────────────────────────────────────────

mcp = FastMCP(
    "Infrastructure Dashboard",
    instructions=(
        "Read-only access to the VM Infrastructure Dashboard. "
        "Use these tools to inspect jobs, VMs, EC2 instances, and Azure VMs. "
        "All operations are non-destructive — deploy/start/stop actions must be "
        "performed through the web UI."
    ),
)

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


# ── Tools ─────────────────────────────────────────────────────────────────────


@mcp.tool()
async def dashboard_summary() -> dict:
    """
    Return a high-level summary of the dashboard: active job count, recent
    failures, and which integrations are enabled.
    """
    db = SessionLocal()
    try:
        running = db.query(Job).filter(Job.status.in_(["pending", "running"])).count()
        failed_today = (
            db.query(Job)
            .filter(
                Job.status == "failed",
                Job.created_at >= datetime.utcnow().replace(
                    hour=0, minute=0, second=0, microsecond=0
                ),
            )
            .count()
        )
        total = db.query(Job).count()
    finally:
        db.close()

    return {
        "active_jobs": running,
        "failed_today": failed_today,
        "total_jobs": total,
        "features": {
            "vmware": config_service.get_bool("vmware_enabled", False),
            "beyondtrust": config_service.get_bool("beyondtrust_enabled", False),
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
    List recent jobs. Optionally filter by status (pending/running/completed/failed/cancelled)
    and/or workgroup. Returns at most `limit` jobs (max 100), newest first.
    """
    limit = min(max(1, limit), 100)
    db = SessionLocal()
    try:
        q = db.query(Job)
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
    Return full details for a single job by its UUID.
    """
    db = SessionLocal()
    try:
        job = db.query(Job).filter(Job.id == job_id).first()
        if not job:
            return {"error": f"Job {job_id!r} not found"}
        result = _job_dict(job)
        if job.extra_data:
            try:
                result["extra_data"] = json.loads(job.extra_data)
            except Exception:
                result["extra_data"] = {}
        return result
    finally:
        db.close()


@mcp.tool()
async def list_vms(workgroup: Optional[str] = None) -> dict:
    """
    List VMware VMs via the PowerShell CLI. Returns a list of VMs with their
    state (running/stopped). Only available when VMware is enabled.
    """
    if not config_service.get_bool("vmware_enabled", False):
        return {"error": "VMware integration is not enabled on this dashboard"}
    try:
        from ..services import vm_service  # type: ignore
        vms = await vm_service.list_vms(workgroup=workgroup)
        return {"vms": vms}
    except ImportError:
        return {"error": "VMware service not available"}
    except Exception as exc:
        return {"error": str(exc)}


@mcp.tool()
async def list_ec2_instances() -> dict:
    """
    List EC2 instances deployed via this dashboard (from job tracking records).
    Returns instance IDs, state, and region.
    """
    db = SessionLocal()
    try:
        deploy_jobs = (
            db.query(Job)
            .filter(
                Job.job_type.in_(["ec2_deploy", "ec2_bulk_deploy"]),
                Job.status == "completed",
            )
            .order_by(Job.created_at.desc())
            .all()
        )
        instances = []
        for job in deploy_jobs:
            if not job.extra_data:
                continue
            try:
                data = json.loads(job.extra_data)
            except Exception:
                continue
            instance_id = data.get("instance_id")
            if instance_id:
                instances.append({
                    "instance_id": instance_id,
                    "job_id": job.id,
                    "workgroup": job.workgroup,
                    "created_by": job.created_by,
                    "deployed_at": _fmt_dt(job.completed_at),
                    "region": data.get("region", ""),
                    "ami_id": data.get("ami_id", ""),
                    "public_ip": data.get("public_ip", ""),
                })
    finally:
        db.close()

    if not instances:
        return {"instances": [], "note": "No completed EC2 deploy jobs found. Live state requires AWS credentials."}
    return {"instances": instances}


@mcp.tool()
async def list_amis(region: Optional[str] = None) -> dict:
    """
    List available AWS AMIs (from AWS Secrets Manager / Boto3). Requires AWS
    credentials to be configured in the dashboard wizard.
    """
    try:
        from ..services.aws_service import list_amis as _list_amis  # type: ignore
        amis = await _list_amis(region=region)
        return {"amis": amis}
    except Exception as exc:
        return {"error": str(exc)}


@mcp.tool()
async def list_azure_vms(resource_group: Optional[str] = None) -> dict:
    """
    List Azure VMs managed by this dashboard. Requires Azure credentials to be
    configured in the dashboard wizard.
    """
    db = SessionLocal()
    try:
        deploy_jobs = (
            db.query(Job)
            .filter(
                Job.job_type.in_(["azure_vm_deploy", "azure_bulk_deploy"]),
                Job.status == "completed",
            )
            .order_by(Job.created_at.desc())
            .all()
        )
        vms = []
        for job in deploy_jobs:
            if not job.extra_data:
                continue
            try:
                data = json.loads(job.extra_data)
            except Exception:
                continue
            vm_name = data.get("vm_name")
            if vm_name:
                rg = data.get("resource_group", "")
                if resource_group and rg != resource_group:
                    continue
                vms.append({
                    "vm_name": vm_name,
                    "resource_group": rg,
                    "job_id": job.id,
                    "workgroup": job.workgroup,
                    "created_by": job.created_by,
                    "deployed_at": _fmt_dt(job.completed_at),
                    "location": data.get("location", ""),
                    "image": data.get("image_reference", ""),
                    "public_ip": data.get("public_ip", ""),
                })
    finally:
        db.close()

    if not vms:
        return {"vms": [], "note": "No completed Azure VM deploy jobs found."}
    return {"vms": vms}


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
    Pure-ASGI authentication wrapper for the MCP app.
    Using a raw ASGI callable (not BaseHTTPMiddleware) so that SSE streams
    are not buffered by the response wrapper.
    """

    def __init__(self, app: Callable) -> None:
        self._app = app

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] == "lifespan":
            await self._app(scope, receive, send)
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
    async def _send_401(send: Callable, scope: dict) -> None:
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        await send({
            "type": "http.response.start",
            "status": 401,
            "headers": [
                [b"content-type", b"application/json"],
                [b"www-authenticate", b'Bearer realm="vm-dashboard"'],
            ],
        })
        await send({
            "type": "http.response.body",
            "body": b'{"detail":"Missing or invalid PAT. Create one at /settings."}',
        })


# ── Public factory ────────────────────────────────────────────────────────────


def get_mcp_asgi_app() -> Callable:
    """Return the MCP ASGI app wrapped with PAT authentication."""
    raw_app = mcp.sse_app()
    return _MCPAuth(raw_app)
