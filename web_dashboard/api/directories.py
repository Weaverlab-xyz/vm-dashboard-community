"""
Managed Active Directory API (gated by ``directories_enabled``).

  GET    /api/directories                       — directories (creator-scoped for non-admins)
  GET    /api/directories/options               — editions, costs, what is not configured
  POST   /api/directories                       — build one (record + schedule apply)
  GET    /api/directories/discover?cloud=…      — existing directories in the account
  POST   /api/directories/register              — record an existing one
  POST   /api/directories/register-onprem       — record an on-prem AD/LDAP via an agent
  GET    /api/directories/ps-candidates         — directories Password Safe manages
  POST   /api/directories/ps-import             — register the chosen ones via an agent
  POST   /api/directories/ad-connector          — build an AWS AD Connector to an on-prem AD
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


class ManagedAccountRef(BaseModel):
    system_id: int
    account_id: int
    account_name: str = ""


class RegisterOnpremRequest(BaseModel):
    name: str                       # AD domain (corp.example.com) or a label for LDAP
    provider: str = "onprem_ad"     # onprem_ad | ldap
    host: str                       # a DC / LDAP server the agent can reach
    port: int = 0                   # 0 = 636 with LDAPS, 389 without
    use_ldaps: bool = True
    base_dn: str = ""               # blank for AD = derived from the domain
    agent_id: str
    managed_account: ManagedAccountRef
    workgroup: Optional[str] = None


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


class ADConnectorRequest(BaseModel):
    onprem_directory_id: str
    region: str = ""
    vpc_id: str
    subnet_ids: List[str]
    dns_ips: List[str]
    size: str = "Small"
    netbios: str = ""
    acknowledge_cost: bool = False
    workgroup: Optional[str] = None


@router.post("/ad-connector")
def build_ad_connector(req: ADConnectorRequest, db: Session = Depends(get_db),
                       user: User = Depends(require_explicit_permission("directories", "write"))):
    """An AWS AD Connector for a registered on-prem AD. The service account is the on-prem
    directory's own Password Safe account, checked out once by the worker."""
    row = directory_service.get_directory(db, req.onprem_directory_id)
    if not row or not _visible(row, user):
        raise HTTPException(status_code=404, detail="directory not found")
    try:
        out = directory_service.provision_ad_connector(
            db, onprem_directory_id=row.id, region=req.region, vpc_id=req.vpc_id,
            subnet_ids=req.subnet_ids, dns_ips=req.dns_ips, size=req.size,
            netbios=req.netbios, acknowledge_cost=req.acknowledge_cost,
            created_by=user.username, workgroup=req.workgroup)
    except DirectoryError as e:
        raise HTTPException(status_code=400, detail=str(e))
    job_service.log_audit(db, user.username, "directory_ad_connector",
                          details={"name": row.name, "region": req.region,
                                   "onprem_directory_id": row.id})
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


@router.post("/register-onprem")
def register_onprem(req: RegisterOnpremRequest, db: Session = Depends(get_db),
                    user: User = Depends(require_explicit_permission("directories", "write"))):
    try:
        row = directory_service.register_onprem(
            db, name=req.name, provider=req.provider, host=req.host, port=req.port,
            use_ldaps=req.use_ldaps, base_dn=req.base_dn, agent_id=req.agent_id,
            managed_account=req.managed_account.model_dump(), created_by=user.username,
            workgroup=req.workgroup)
    except DirectoryError as e:
        raise HTTPException(status_code=400, detail=str(e))
    job_service.log_audit(db, user.username, "directory_register_onprem",
                          details={"name": row.name, "host": row.host,
                                   "agent_id": row.agent_id})
    return directory_service.to_dict(row)


# ── Import from Password Safe ─────────────────────────────────────────────────
#
# Password Safe already manages these directories and their accounts, so it knows the
# domain, port, SSL setting and requestable accounts authoritatively. Same shape as the
# database import (api/cloud_databases ps-candidates / ps-import): read the inventory,
# let the operator pick, and register through register_onprem so every rule it enforces
# (active agent, version, managed account, no duplicate) applies to imported rows too.
# Nothing in Password Safe is created or changed.

_MAX_IMPORT_BATCH = 50
_PS_GENERIC_ERROR = ("Password Safe lookup failed — check the BeyondTrust "
                     "configuration and server logs.")


def _require_secrets_use(user: User) -> None:
    """Listing Password Safe systems and pinning an account for later checkout is the
    ``secrets:use`` grant, as for the database import. Reuses config_mgmt's predicate."""
    from .config_mgmt import _can_use_secrets
    if not _can_use_secrets(user):
        raise HTTPException(status_code=403, detail="The 'secrets:use' permission is required.")


def _ps_ready() -> str:
    """Why Password Safe cannot be read, or "" when it can."""
    from ..config import settings
    from ..services import config_service, ps_api_service
    if not config_service.get_bool("password_safe_enabled", settings.password_safe_enabled):
        return "BeyondTrust Password Safe is disabled in Settings."
    if not ps_api_service.configured():
        return ("Password Safe is not configured — set the API URL, client id and secret "
                "in Settings → Integrations → BeyondTrust.")
    return ""


async def _read_ps_candidates(db: Session) -> dict:
    from ..database import ManagedDirectory
    from ..services import ps_api_service, ps_directory_catalog
    raw = await ps_api_service.read_directory_inventory()
    rows, truncated = ps_directory_catalog.build_candidates(
        platforms=raw.get("platforms"), systems=raw.get("systems"),
        directories=raw.get("directories"), accounts=raw.get("accounts"))
    # Computed per request, like api/cloud_databases._annotate_imported: not
    # creator-filtered and not status-filtered, matching register_onprem's own check.
    known = {((h or "").strip().lower(), int(p or 0)) for h, p in
             db.query(ManagedDirectory.host, ManagedDirectory.port)
             .filter(ManagedDirectory.cloud == "local").all()}
    for row in rows:
        row["already_registered"] = ((row["host"] or "").lower(), int(row["port"] or 0)) in known
    return {"systems": rows, "truncated": truncated,
            "warnings": list(raw.get("warnings") or [])}


@router.get("/ps-candidates")
async def ps_candidates(db: Session = Depends(get_db),
                        user: User = Depends(require_explicit_permission("directories", "write"))):
    """Directories Password Safe manages, shaped for the import dialog. A disabled or
    unconfigured integration is a state, not an error, and Password Safe's own error text
    never reaches the caller."""
    from ..services import ps_api_service
    _require_secrets_use(user)
    reason = _ps_ready()
    if reason:
        return {"configured": False, "reason": reason, "systems": [],
                "truncated": False, "warnings": []}
    try:
        return {"configured": True, **(await _read_ps_candidates(db))}
    except ps_api_service.PSApiError as exc:
        logger.warning("Password Safe directory inventory read failed: %s", exc)
    except Exception:  # noqa: BLE001
        logger.exception("Password Safe directory import candidates failed")
    return {"configured": True, "systems": [], "truncated": False, "warnings": [],
            "error": _PS_GENERIC_ERROR}


class PSDirectoryImportItem(BaseModel):
    """One directory to import, named by Password Safe ids plus the agent that reaches
    it. No host, port or account name: the server re-resolves those from its own read,
    so a caller cannot pair an arbitrary host with an arbitrary managed account."""
    system_id: int
    account_id: int
    agent_id: str
    base_dn: str = ""               # LDAP only, when Password Safe records none


class PSDirectoryImportRequest(BaseModel):
    items: List[PSDirectoryImportItem] = []
    workgroup: Optional[str] = None


@router.post("/ps-import")
async def ps_import(req: PSDirectoryImportRequest, db: Session = Depends(get_db),
                    user: User = Depends(require_explicit_permission("directories", "write"))):
    """Register the selected Password Safe directories. A selection problem refuses the
    whole request before anything is written; a per-item problem fails that item and the
    rest carry on. 400 only when nothing was imported."""
    import uuid
    from ..services import ps_api_service, ps_directory_catalog
    _require_secrets_use(user)
    reason = _ps_ready()
    if reason:
        raise HTTPException(status_code=400, detail=reason)
    items = req.items or []
    if not items:
        raise HTTPException(status_code=400, detail="Select at least one directory to import.")
    if len(items) > _MAX_IMPORT_BATCH:
        raise HTTPException(status_code=400,
                            detail=f"Import at most {_MAX_IMPORT_BATCH} directories at a "
                                   f"time ({len(items)} selected).")
    ids = [i.system_id for i in items]
    if len(set(ids)) != len(ids):
        raise HTTPException(status_code=400,
                            detail="The same managed system was selected more than once.")
    try:
        found = await _read_ps_candidates(db)
    except ps_api_service.PSApiError as exc:
        logger.warning("Password Safe read failed during directory import: %s", exc)
        raise HTTPException(status_code=503, detail=_PS_GENERIC_ERROR) from exc
    by_id = {c["system_id"]: c for c in found["systems"]}

    imported, failed = [], []
    for item in items:
        cand = by_id.get(item.system_id)
        name = (cand or {}).get("name") or str(item.system_id)

        def fail(msg):
            failed.append({"system_id": item.system_id, "name": name, "error": msg})

        if cand is None:
            fail("no longer present in Password Safe")
            continue
        if cand.get("already_registered"):
            fail("already registered in the dashboard")
            continue
        if not cand.get("eligible"):
            fail(cand.get("reason") or "not importable")
            continue
        ref = ps_directory_catalog.managed_account(cand, item.account_id)
        if not ref:
            fail("the selected account is not a requestable account on that directory")
            continue
        try:
            # Always through register_onprem: it is what keeps the never-store-a-credential
            # property, and every refusal it makes, covering imported rows too.
            row = directory_service.register_onprem(
                db, name=cand["name"], provider=cand["provider"], host=cand["host"],
                port=cand["port"], use_ldaps=cand["use_ldaps"], base_dn=item.base_dn,
                agent_id=item.agent_id, managed_account=ref, created_by=user.username,
                workgroup=req.workgroup)
        except DirectoryError as exc:
            fail(str(exc))
            continue
        imported.append({"system_id": item.system_id, "name": row.name,
                         "directory_id": row.id, "host": row.host})

    batch_id = str(uuid.uuid4())
    job_service.log_audit(db, user.username, "directory_ps_import", details={
        "batch_id": batch_id, "count": len(imported),
        "system_ids": [i["system_id"] for i in imported],
        "failed": [f["system_id"] for f in failed]})
    if not imported:
        raise HTTPException(
            status_code=400,
            detail=(f"No directories were imported. First failure: {failed[0]['error']}"
                    if failed else "No directories were imported."))
    return {"batch_id": batch_id, "count": len(imported),
            "imported": imported, "failed": failed}


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
