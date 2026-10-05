"""
Managed Active Directory API (gated by ``directories_enabled``).

  GET    /api/directories                       — directories (creator-scoped for non-admins)
  GET    /api/directories/options               — editions, costs, what is not configured
  POST   /api/directories                       — build one (record + schedule apply)
  GET    /api/directories/discover?cloud=…      — existing directories in the account
  POST   /api/directories/register              — record an existing one
  GET    /api/directories/joinable?cloud=…      — what a Windows deploy can join
  GET    /api/directories/{id}                  — one directory
  GET    /api/directories/{id}/admin-password   — the stored admin credential (audited)
  POST   /api/directories/{id}/reset-admin-password
  DELETE /api/directories/{id}                  — destroy (built here) or unregister

The admin password route is ``directories:write`` and refuses (409) when Password Safe
manages the account — the dashboard then holds no valid copy.

``joinable`` is gated on the CLOUD's write permission rather than on this scope, because
the person picking a directory is the one deploying the VM, and joining needs no domain
credential on either cloud.
"""
import asyncio
import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import User, get_db
from ..services import directory_service, job_service
from ..services.directory_service import DirectoryError
from .auth import get_current_user, has_permission, require_explicit_permission

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/directories", tags=["directories"])


def _visible(row, user: User) -> bool:
    return bool(getattr(user, "is_effective_admin", False)) or row.created_by == user.username


def _row_or_404(db: Session, directory_id: str, user: User):
    row = directory_service.get_directory(db, directory_id)
    if not row or row.status == "deleted" or not _visible(row, user):
        raise HTTPException(status_code=404, detail="directory not found")
    return row


class BuildRequest(BaseModel):
    cloud: str
    name: str
    netbios: str = ""
    acknowledge_cost: bool = False
    register_in_passwordsafe: bool = False
    workgroup: Optional[str] = None
    # AWS
    edition: str = ""
    region: str = ""
    vpc_id: str = ""
    subnet_ids: List[str] = []
    # GCP
    project: str = ""
    locations: List[str] = []
    reserved_ip_range: str = ""
    networks: List[str] = []


class RegisterRequest(BaseModel):
    cloud: str
    identifier: str
    region: str = ""
    project: str = ""
    workgroup: Optional[str] = None


@router.get("")
def list_directories(db: Session = Depends(get_db),
                     user: User = Depends(require_explicit_permission("directories", "read"))):
    rows = [r for r in directory_service.list_directories(db) if _visible(r, user)]
    return {"directories": [directory_service.to_dict(r) for r in rows]}


@router.get("/options")
def build_options(user: User = Depends(require_explicit_permission("directories", "read"))):
    from ..services import windows_admin_secret
    missing = []
    for cloud in directory_service.PROVISIONING_CLOUDS:
        try:
            windows_admin_secret.resolve_backend(cloud)
        except windows_admin_secret.WindowsSecretError as e:
            missing.append(f"{cloud}: {e}")
    return {
        "clouds": list(directory_service.PROVISIONING_CLOUDS),
        "aws_editions": list(directory_service.AWS_EDITIONS),
        "costs": {f"{c}:{e}" if e else c: v
                  for (c, e), v in directory_service.APPROX_MONTHLY_COST.items()},
        "defaults": {
            "aws_edition": directory_service._cfg("directory_aws_default_edition", "Standard"),
            "aws_region": directory_service._cfg("aws_region", ""),
            "gcp_project": directory_service._cfg("gcp_project")
            or directory_service._cfg("gcp_project_id"),
            "gcp_region": directory_service._cfg("gcp_region", ""),
            "gcp_reserved_ip_range": directory_service._cfg("directory_gcp_reserved_ip_range"),
            "gcp_network": directory_service._cfg("gcp_network"),
        },
        "missing": missing,
    }


@router.post("")
def build_directory(req: BuildRequest, db: Session = Depends(get_db),
                    user: User = Depends(require_explicit_permission("directories", "write"))):
    try:
        out = directory_service.provision(
            db, cloud=req.cloud, name=req.name, created_by=user.username,
            acknowledge_cost=req.acknowledge_cost, netbios=req.netbios, edition=req.edition,
            region=req.region, vpc_id=req.vpc_id, subnet_ids=req.subnet_ids,
            project=req.project, locations=req.locations,
            reserved_ip_range=req.reserved_ip_range, networks=req.networks,
            register_in_passwordsafe=req.register_in_passwordsafe, workgroup=req.workgroup)
    except DirectoryError as e:
        raise HTTPException(status_code=400, detail=str(e))
    job_service.log_audit(db, user.username, "directory_provision",
                          details={"name": req.name, "cloud": req.cloud})
    return out


@router.get("/discover")
async def discover(cloud: str = Query(...), region: str = "", project: str = "",
                   db: Session = Depends(get_db),
                   user: User = Depends(require_explicit_permission("directories", "read"))):
    try:
        found = await directory_service.discover(db, cloud=cloud, region=region,
                                                 project=project)
    except DirectoryError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:  # noqa: BLE001 — a cloud SDK error is the caller's to see
        raise HTTPException(status_code=502, detail=f"discovery failed: {e}")
    return {"directories": found}


@router.post("/register")
async def register(req: RegisterRequest, db: Session = Depends(get_db),
                   user: User = Depends(require_explicit_permission("directories", "write"))):
    try:
        row = await directory_service.register(
            db, cloud=req.cloud, identifier=req.identifier, created_by=user.username,
            region=req.region, project=req.project, workgroup=req.workgroup)
    except DirectoryError as e:
        raise HTTPException(status_code=400, detail=str(e))
    job_service.log_audit(db, user.username, "directory_register",
                          details={"cloud": req.cloud, "identifier": req.identifier})
    return directory_service.to_dict(row)


@router.get("/joinable")
def joinable(cloud: str = Query(...), region: str = "", db: Session = Depends(get_db),
             user: User = Depends(get_current_user)):
    cloud = (cloud or "").lower()
    if cloud not in ("aws", "gcp"):
        raise HTTPException(status_code=400, detail="joinable covers aws and gcp")
    if not has_permission(user, cloud, "write"):
        raise HTTPException(status_code=403, detail=f"{cloud}:write is required")
    rows = directory_service.joinable_for(db, cloud, region)
    return {"directories": [{"id": r.id, "name": r.name, "provider": r.provider,
                             "provider_label": directory_service.PROVIDER_LABELS.get(r.provider),
                             "region": r.region, "vpc_id": r.vpc_id,
                             "networks": directory_service._jl(r.networks)} for r in rows]}


@router.get("/{directory_id}")
def get_directory(directory_id: str, db: Session = Depends(get_db),
                  user: User = Depends(require_explicit_permission("directories", "read"))):
    row = _row_or_404(db, directory_id, user)
    out = directory_service.to_dict(row)
    out["joined_vms"] = directory_service.joined_vms(db, row.id)
    return out


@router.get("/{directory_id}/admin-password")
async def get_admin_password(directory_id: str, db: Session = Depends(get_db),
                             user: User = Depends(require_explicit_permission("directories", "write"))):
    from ..services import windows_admin_secret
    row = _row_or_404(db, directory_id, user)
    if row.admin_password_custody == "passwordsafe_managed":
        raise HTTPException(
            status_code=409,
            detail=(f"Password Safe manages the administrator of {row.name} (managed "
                    f"account {row.ps_account_id}) — check the credential out there."))
    if not (row.admin_password_backend and row.admin_password_ref):
        raise HTTPException(status_code=404, detail=(
            "No administrator password is stored for this directory — it was registered "
            "rather than built here, or the post-build store failed (use Reset)."))
    try:
        pw = await asyncio.to_thread(windows_admin_secret.read, row.admin_password_backend,
                                     row.admin_password_ref)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"Secrets backend read failed: {e}")
    job_service.log_audit(db, user.username, "directory_admin_password_read",
                          details={"directory": row.name,
                                   "backend": row.admin_password_backend})
    return {"name": row.name, "username": row.admin_username, "password": pw,
            "backend": row.admin_password_backend}


@router.post("/{directory_id}/reset-admin-password")
async def reset_admin_password(directory_id: str, db: Session = Depends(get_db),
                               user: User = Depends(require_explicit_permission("directories", "write"))):
    row = _row_or_404(db, directory_id, user)
    try:
        out = await directory_service.reset_admin_password(db, directory_id=row.id)
    except DirectoryError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"reset failed: {e}")
    job_service.log_audit(db, user.username, "directory_admin_password_reset",
                          details={"directory": row.name})
    return out


@router.delete("/{directory_id}")
def delete_directory(directory_id: str, db: Session = Depends(get_db),
                     user: User = Depends(require_explicit_permission("directories", "delete"))):
    row = _row_or_404(db, directory_id, user)
    try:
        if row.source == "registered":
            directory_service.unregister(db, directory_id=row.id)
            out = {"unregistered": True}
        else:
            out = directory_service.start_decommission(db, directory_id=row.id,
                                                       created_by=user.username)
    except DirectoryError as e:
        raise HTTPException(status_code=409, detail=str(e))
    job_service.log_audit(db, user.username, "directory_delete",
                          details={"directory": row.name, "source": row.source})
    return out
