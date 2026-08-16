"""
Cloud Functions API — preview (gated by the ``cloud_functions_enabled`` flag).

  POST   /api/functions              — deploy a workload (record + schedule apply)
  GET    /api/functions              — list dashboard-deployed functions
  GET    /api/functions/workloads    — the workload catalog for the deploy form
  GET    /api/functions/options      — clouds/regions + which package stores are set
  GET    /api/functions/{id}         — one function
  GET    /api/functions/{id}/invoke-info  — endpoint + credentials (admin)
  POST   /api/functions/{id}/invoke  — test-invoke from the dashboard
  DELETE /api/functions/{id}         — decommission (terraform destroy)

Permission-gated via the ``cloud_function`` scope; list results are scoped to the
caller's own rows for non-admins, mirroring the cloud-database page.

``invoke-info`` returns the shared bearer secret in plaintext — it is the value an
operator has to paste into an Entitle REST integration — so it is **admin-only**,
not merely ``cloud_function:read``.
"""
import logging
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..config import settings
from ..database import User, get_db
from ..services import cloud_function_service, config_service, region_catalog
from ..services.cloud_function_service import CloudFunctionError
from .auth import require_admin, require_permission

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/functions", tags=["cloud-functions"])


def _require_enabled() -> None:
    if not config_service.get_bool("cloud_functions_enabled", settings.cloud_functions_enabled):
        raise HTTPException(status_code=403, detail="cloud functions are disabled")


class DeployRequest(BaseModel):
    cloud: str
    region: str
    name: str
    workload: str
    # "public" deploys an internet-reachable endpoint; "vpc" additionally attaches
    # it to the VPC/VNet so it can reach private resources. The per-cloud ids fall
    # back to the Settings → Cloud Functions panel when omitted.
    network_mode: str = "public"
    subnet_ids: Optional[list[str]] = None          # aws
    security_group_ids: Optional[list[str]] = None  # aws
    subnet_id: Optional[str] = None                 # azure
    vpc_connector: Optional[str] = None             # gcp
    auth_mode: Optional[str] = None
    environment: Optional[dict] = None              # NON-secret env only
    timeout_seconds: Optional[int] = None
    memory_mb: Optional[int] = None
    resource_group_name: Optional[str] = None       # azure
    sku_name: Optional[str] = None                  # azure
    service_account_email: Optional[str] = None     # gcp
    # Credential access for workloads that need one (db_grant). Each cloud resolves
    # it differently and none of them takes the secret VALUE: AWS grants the
    # function read on named Secrets Manager ARNs, GCP injects a Secret Manager
    # secret as an env var, and Azure resolves an @Microsoft.KeyVault(...) reference
    # placed in `environment` using the app's system-assigned identity.
    readable_secret_arns: Optional[list[str]] = None   # aws
    db_admin_secret: Optional[str] = None              # gcp


class InvokeRequest(BaseModel):
    payload: Optional[dict] = None


def _visible(db: Session, user: User):
    """Non-admins see only what they deployed — same rule as the DB page."""
    rows = cloud_function_service.list_functions(db)
    if getattr(user, "is_effective_admin", False):
        return rows
    return [row for row in rows if row.get("created_by") == user.username]


@router.get("")
def list_functions(db: Session = Depends(get_db),
                   user: User = Depends(require_permission("cloud_function", "read"))):
    """Every function this caller may see."""
    _require_enabled()
    return {"functions": _visible(db, user)}


@router.get("/workloads")
def list_workloads(user: User = Depends(require_permission("cloud_function", "read"))):
    """The workload catalog — name, description, and which clouds each runs on."""
    _require_enabled()
    return {"workloads": cloud_function_service.workload_catalog()}


@router.get("/options")
def deploy_options(user: User = Depends(require_permission("cloud_function", "read"))):
    """What the deploy form needs: clouds, their regions, and whether each cloud's
    package store is configured — so the form can explain a missing prerequisite
    instead of failing at deploy time."""
    _require_enabled()
    clouds = []
    for cloud in cloud_function_service.VALID_CLOUDS:
        try:
            ready = bool(cloud_function_service.package_location(cloud, "probe", "0" * 64))
            reason = ""
        except CloudFunctionError as exc:
            ready, reason = False, str(exc)
        clouds.append({
            "cloud": cloud,
            "regions": region_catalog.regions(cloud),
            "default_region": region_catalog.default_region(cloud),
            "package_store_ready": ready,
            "reason": reason,
        })
    return {
        "clouds": clouds,
        "network_modes": list(cloud_function_service.VALID_NETWORK_MODES),
        "terraform_available": cloud_function_service.terraform_available(),
    }


@router.get("/{fn_id}")
def get_function(fn_id: str, db: Session = Depends(get_db),
                 user: User = Depends(require_permission("cloud_function", "read"))):
    _require_enabled()
    for row in _visible(db, user):
        if row["id"] == fn_id:
            return row
    raise HTTPException(status_code=404, detail="function not found")


@router.get("/{fn_id}/invoke-info")
def invoke_info(fn_id: str, db: Session = Depends(get_db),
                user: User = Depends(require_admin)):
    """Endpoint + credentials, for pasting into an Entitle REST integration.

    Admin-only: this returns the shared secret in plaintext.
    """
    _require_enabled()
    try:
        return cloud_function_service.invoke_info(db, fn_id)
    except CloudFunctionError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("")
def deploy_function(payload: DeployRequest, background: BackgroundTasks,
                    db: Session = Depends(get_db),
                    user: User = Depends(require_permission("cloud_function", "write"))):
    """Record the function, stage its package, and schedule the Terraform apply."""
    _require_enabled()
    if not cloud_function_service.terraform_available():
        raise HTTPException(
            status_code=503,
            detail={"code": "terraform_unavailable",
                    "message": "terraform is not installed in this image"})
    try:
        result = cloud_function_service.deploy(
            db, cloud=payload.cloud, region=payload.region, name=payload.name,
            workload=payload.workload, created_by=user.username,
            network_mode=payload.network_mode,
            subnet_ids=payload.subnet_ids,
            security_group_ids=payload.security_group_ids,
            subnet_id=payload.subnet_id or "",
            vpc_connector=payload.vpc_connector or "",
            auth_mode=payload.auth_mode or "",
            environment=payload.environment or {},
            timeout_seconds=payload.timeout_seconds,
            memory_mb=payload.memory_mb,
            resource_group_name=payload.resource_group_name,
            sku_name=payload.sku_name,
            service_account_email=payload.service_account_email,
            readable_secret_arns=payload.readable_secret_arns or [],
            db_admin_secret=payload.db_admin_secret or "",
        )
    except NotImplementedError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except CloudFunctionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # The jobs worker also handles cloudfn_deploy; scheduling it here too means a
    # single-container deployment (no separate worker) still works, exactly as the
    # cloud-database path does.
    background.add_task(
        _run_deploy, fn_id=result["fn_id"], job_id=result["job_id"],
        tf_variables=result["tf_variables"])
    return result


async def _run_deploy(*, fn_id: str, job_id: str, tf_variables: dict) -> None:
    """Background entry point — owns its own session, since the request's is closed
    by the time this runs."""
    from ..database import SessionLocal
    db = SessionLocal()
    try:
        await cloud_function_service.run_deploy_apply(
            db, fn_id=fn_id, job_id=job_id, tf_variables=tf_variables)
    finally:
        db.close()


@router.post("/{fn_id}/invoke")
async def invoke_function(fn_id: str, payload: InvokeRequest,
                          db: Session = Depends(get_db),
                          user: User = Depends(require_permission("cloud_function", "write"))):
    """Call the function with its credentials attached.

    The fastest way to tell "the function is broken" from "my curl was wrong", and
    what makes the workloads demoable without a terminal.
    """
    _require_enabled()
    try:
        return await cloud_function_service.invoke(db, fn_id=fn_id,
                                                   payload=payload.payload or {})
    except CloudFunctionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        # Network failures reaching the endpoint are the expected error here and
        # are the caller's problem to see — but only as a type, not a raw trace.
        logger.warning("cloudfn: test invoke of %s failed: %s", fn_id, exc)
        raise HTTPException(
            status_code=502,
            detail={"code": "invoke_failed",
                    "message": f"could not reach the function ({type(exc).__name__})"}) from exc


@router.delete("/{fn_id}")
def decommission_function(fn_id: str, background: BackgroundTasks,
                          db: Session = Depends(get_db),
                          user: User = Depends(require_permission("cloud_function", "delete"))):
    """Destroy the cloud resource and mark the record deleted."""
    _require_enabled()
    try:
        result = cloud_function_service.start_decommission(db, fn_id,
                                                           created_by=user.username)
    except CloudFunctionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    background.add_task(_run_decommission, fn_id=fn_id, job_id=result["job_id"])
    return result


async def _run_decommission(*, fn_id: str, job_id: str) -> None:
    from ..database import SessionLocal
    db = SessionLocal()
    try:
        await cloud_function_service.run_decommission(db, fn_id=fn_id, job_id=job_id)
    finally:
        db.close()
