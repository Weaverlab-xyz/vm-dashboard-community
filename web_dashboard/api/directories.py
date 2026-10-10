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
  POST   /api/directories/dns-link              — build a GCP DNS link to an on-prem AD
  POST   /api/directories/vyos-link             — build a GCP VyOS WireGuard site link
  POST   /api/directories/{id}/check-link       — re-apply a site link's config and probe it
  GET    /api/directories/{id}/onprem-commands  — the VyOS lines for the on-prem router
  GET    /api/directories/joinable?cloud=…      — what a Windows deploy can join
  GET    /api/directories/{id}                  — one directory
  GET    /api/directories/{id}/admin-password   — the stored admin credential (audited)
  POST   /api/directories/{id}/reset-admin-password
  DELETE /api/directories/{id}                  — destroy (built here) or unregister
  PUT    /api/directories/{id}/join-account     — Azure: pin the Entra DS join account

Cloud identity providers (Entra ID, Okta, PingOne):

  GET    /api/directories/idp/options            — providers, auth modes, regions
  POST   /api/directories/register-idp           — record one (signs in and reads first)
  PATCH  /api/directories/{id}                   — rename, toggle writes, repoint credential
  POST   /api/directories/{id}/test              — sign in afresh, read a user and a group
  GET    /api/directories/{id}/users?q=&cursor=
  GET    /api/directories/{id}/groups?q=&cursor=
  GET    /api/directories/{id}/groups/{gid}/members
  GET    /api/directories/{id}/users/{uid}/groups
  POST   /api/directories/{id}/groups/{gid}/members/{uid}   — add (writes on, audited)
  DELETE /api/directories/{id}/groups/{gid}/members/{uid}   — remove (writes on, audited)

Entitle (any directory):

  GET    /api/directories/{id}/entitle-candidates   — the tenant's integrations, likely first
  PUT    /api/directories/{id}/entitle-integration  — pin one ({"integration_id": ""} unpins)

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
    # Azure (Entra Domain Services). `edition` is the SKU, `region` the location.
    resource_group: str = ""
    vnet_resource_group: str = ""
    vnet_name: str = ""
    subnet_cidr: str = ""
    manage_vnet_dns: bool = False
    join_account: Optional["ManagedAccountRef"] = None


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
    vendor: str = ""                # LDAP only: openldap | pingdirectory | okta_ldap | …


class RegisterRequest(BaseModel):
    cloud: str
    identifier: str
    region: str = ""
    project: str = ""
    workgroup: Optional[str] = None
    join_account: Optional[ManagedAccountRef] = None   # Azure only


class JoinAccountRequest(BaseModel):
    join_account: Optional[ManagedAccountRef] = None    # None clears it


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
        if cloud == "azure":
            continue    # Entra DS stores no administrator password
        try:
            windows_admin_secret.resolve_backend(cloud)
        except windows_admin_secret.WindowsSecretError:
            # Fixed wording rather than the exception's text, so nothing from the secrets
            # layer reaches the response.
            missing.append(f"{cloud}: no place to store the administrator password — "
                           f"configure Password Safe, an external secrets backend, or "
                           f"the cloud's own vault")
    return {
        "clouds": list(directory_service.PROVISIONING_CLOUDS),
        "aws_editions": list(directory_service.AWS_EDITIONS),
        "azure_skus": list(directory_service.AZURE_SKUS),
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
            "azure_resource_group": directory_service._cfg("azure_resource_group"),
            "azure_location": directory_service._cfg("azure_location"),
            "azure_vnet_resource_group": directory_service._cfg("azure_vnet_resource_group"),
            "azure_vnet_name": directory_service._cfg("azure_vnet_name"),
        },
        "missing": missing,
    }


@router.post("")
async def build_directory(req: BuildRequest, db: Session = Depends(get_db),
                          user: User = Depends(require_explicit_permission("directories", "write"))):
    if req.join_account:
        _require_secrets_use(user)
    if (req.cloud or "").lower() == "azure" and req.acknowledge_cost:
        # Checked before anything is recorded: each of these fails 45 minutes into a
        # build that bills from its first minute.
        try:
            await directory_service.azure_preflight(db)
        except DirectoryError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception:  # noqa: BLE001
            logger.warning("directory: Azure preflight failed", exc_info=True)
            raise HTTPException(status_code=502, detail=(
                "Could not check the Azure subscription before building — see the "
                "dashboard log and Settings → Azure."))
    try:
        out = directory_service.provision(
            db, cloud=req.cloud, name=req.name, created_by=user.username,
            acknowledge_cost=req.acknowledge_cost, netbios=req.netbios, edition=req.edition,
            region=req.region, vpc_id=req.vpc_id, subnet_ids=req.subnet_ids,
            project=req.project, locations=req.locations,
            reserved_ip_range=req.reserved_ip_range, networks=req.networks,
            register_in_passwordsafe=req.register_in_passwordsafe, workgroup=req.workgroup,
            resource_group=req.resource_group, vnet_resource_group=req.vnet_resource_group,
            vnet_name=req.vnet_name, subnet_cidr=req.subnet_cidr,
            manage_vnet_dns=req.manage_vnet_dns,
            managed_account=req.join_account.model_dump() if req.join_account else None)
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


class DNSLinkRequest(BaseModel):
    onprem_directory_id: str
    project: str = ""
    networks: List[str] = []
    dns_ips: List[str]
    workgroup: Optional[str] = None


@router.post("/dns-link")
def build_dns_link(req: DNSLinkRequest, db: Session = Depends(get_db),
                   user: User = Depends(require_explicit_permission("directories", "write"))):
    """A Cloud DNS forwarding zone so GCE Windows servers can join an on-prem AD, joined
    by the on-prem agent after deploy."""
    row = directory_service.get_directory(db, req.onprem_directory_id)
    if not row or not _visible(row, user):
        raise HTTPException(status_code=404, detail="directory not found")
    try:
        out = directory_service.provision_dns_link(
            db, onprem_directory_id=row.id, project=req.project, networks=req.networks,
            dns_ips=req.dns_ips, created_by=user.username, workgroup=req.workgroup)
    except DirectoryError as e:
        raise HTTPException(status_code=400, detail=str(e))
    job_service.log_audit(db, user.username, "directory_dns_link",
                          details={"name": row.name, "project": req.project,
                                   "onprem_directory_id": row.id})
    return out


class VyosLinkRequest(BaseModel):
    onprem_directory_id: str
    project: str = ""
    zone: str = ""
    network: str = ""
    subnetwork: str = ""
    image_name: str = ""
    image_self_link: str = ""
    vyos_release: str = "1.4"
    onprem_public_key: str
    onprem_subnets: List[str]
    cloud_networks: List[str]
    dns_ips: List[str]
    machine_type: str = ""
    wireguard_source_ranges: List[str] = []
    ssh_user: str = ""
    workgroup: Optional[str] = None


@router.post("/vyos-link")
def build_vyos_link(req: VyosLinkRequest, db: Session = Depends(get_db),
                    user: User = Depends(require_explicit_permission("directories", "write"))):
    """A VyOS peer on GCP, WireGuard to the on-prem router: the network path a DNS link
    and an agent join need, for the price of one small VM."""
    from ..services import vyos_link_service
    row = directory_service.get_directory(db, req.onprem_directory_id)
    if not row or not _visible(row, user):
        raise HTTPException(status_code=404, detail="directory not found")
    try:
        out = vyos_link_service.provision(
            db, onprem_directory_id=row.id, project=req.project, zone=req.zone,
            network=req.network, subnetwork=req.subnetwork, image_name=req.image_name,
            image_self_link=req.image_self_link, release=req.vyos_release,
            onprem_public_key=req.onprem_public_key, onprem_subnets=req.onprem_subnets,
            cloud_networks=req.cloud_networks, dns_ips=req.dns_ips,
            machine_type=req.machine_type,
            wireguard_source_ranges=req.wireguard_source_ranges or None,
            ssh_user=req.ssh_user, created_by=user.username, workgroup=req.workgroup)
    except vyos_link_service.VyosLinkError as e:
        raise HTTPException(status_code=400, detail=str(e))
    job_service.log_audit(db, user.username, "directory_vyos_link",
                          details={"name": row.name, "project": req.project,
                                   "zone": req.zone, "onprem_directory_id": row.id})
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
    if req.join_account:
        _require_secrets_use(user)
    try:
        row = await directory_service.register(
            db, cloud=req.cloud, identifier=req.identifier, created_by=user.username,
            region=req.region, project=req.project, workgroup=req.workgroup,
            managed_account=req.join_account.model_dump() if req.join_account else None)
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
            workgroup=req.workgroup, vendor=req.vendor)
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

        def fail(msg, item=item, name=name):
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
        spec = dict(name=cand["name"], provider=cand["provider"], host=cand["host"],
                    port=cand["port"], use_ldaps=cand["use_ldaps"], base_dn=item.base_dn,
                    agent_id=item.agent_id, managed_account=ref,
                    vendor=cand.get("vendor") or "")
        # The reason comes from onprem_problem's return, never from an exception's text
        # (CodeQL py/stack-trace-exposure): the same checks register_onprem enforces.
        problem = directory_service.onprem_problem(db, **spec)
        if problem:
            fail(problem)
            continue
        try:
            # Always through register_onprem: it is what keeps the never-store-a-credential
            # property covering imported rows too.
            row = directory_service.register_onprem(
                db, **spec, created_by=user.username, workgroup=req.workgroup)
        except DirectoryError:
            logger.warning("directory import: register failed for system %s",
                           item.system_id, exc_info=True)
            fail("could not register it; see the dashboard log")
            continue
        imported.append({"system_id": item.system_id, "name": row.name,
                         "directory_id": row.id, "host": row.host})

    batch_id = str(uuid.uuid4())
    job_service.log_audit(db, user.username, "directory_ps_import", details={
        "batch_id": batch_id, "count": len(imported),
        "system_ids": [i["system_id"] for i in imported],
        "failed": [f["system_id"] for f in failed]})
    # A batch where every item failed still answers 200 with the per-item reasons, which
    # the dialog lists; folding the first reason into an error message would lose the rest.
    return {"batch_id": batch_id, "count": len(imported),
            "imported": imported, "failed": failed}


@router.get("/joinable")
def joinable(cloud: str = Query(...), region: str = "", db: Session = Depends(get_db),
             user: User = Depends(get_current_user)):
    from ..services import hybrid_join_service
    cloud = (cloud or "").lower()
    if cloud not in ("aws", "gcp", "azure"):
        raise HTTPException(status_code=400, detail="joinable covers aws, gcp and azure")
    if not has_permission(user, cloud, "write"):
        raise HTTPException(status_code=403, detail=f"{cloud}:write is required")
    rows = directory_service.joinable_for(db, cloud, region)
    return {"directories": [{"id": r.id, "name": r.name, "provider": r.provider,
                             "provider_label": directory_service.PROVIDER_LABELS.get(r.provider),
                             "region": r.region, "vpc_id": r.vpc_id,
                             "networks": directory_service._jl(r.networks),
                             # Azure joins need a pinned account; the others need none.
                             "join_ready": r.cloud != "azure" or bool(r.credentials_ref),
                             # Whether a server joined here can be Entra hybrid joined.
                             "entra_hybrid": hybrid_join_service.settings_for(db, r)["entra_hybrid"]}
                            for r in rows]}


@router.get("/{directory_id}")
def get_directory(directory_id: str, db: Session = Depends(get_db),
                  user: User = Depends(require_explicit_permission("directories", "read"))):
    row = _row_or_404(db, directory_id, user)
    out = directory_service.to_dict(row)
    out["joined_vms"] = directory_service.joined_vms(db, row.id)
    return out


@router.post("/{directory_id}/check-link")
def check_link(directory_id: str, db: Session = Depends(get_db),
               user: User = Depends(require_explicit_permission("directories", "write"))):
    from ..services import vyos_link_service
    row = _row_or_404(db, directory_id, user)
    try:
        out = vyos_link_service.start_check(db, directory_id=row.id,
                                            created_by=user.username)
    except vyos_link_service.VyosLinkError as e:
        raise HTTPException(status_code=400, detail=str(e))
    job_service.log_audit(db, user.username, "directory_vyos_link_check",
                          details={"name": row.name, "directory_id": row.id})
    return out


@router.get("/{directory_id}/onprem-commands")
def onprem_commands(directory_id: str, db: Session = Depends(get_db),
                    user: User = Depends(require_explicit_permission("directories", "read"))):
    """Public material only: the peer's public key and address, and the routes."""
    from ..services import vyos_link_service
    row = _row_or_404(db, directory_id, user)
    if row.provider != vyos_link_service.PROVIDER:
        raise HTTPException(status_code=404, detail="not a site link")
    return {"commands": vyos_link_service.onprem_commands(row)}


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


# ── Cloud identity providers (Entra ID, Okta, PingOne) ────────────────────────
#
# Reads are directories:read; membership changes and editing the row are
# directories:write, and the row must also have writes turned on (change_membership
# refuses otherwise). Every change is audited with the tenant, group and user ids.

class RegisterIdPRequest(BaseModel):
    provider: str                   # entra_id | okta | pingone
    name: str = ""
    endpoint: str = ""              # Okta org URL | PingOne region TLD
    tenant_id: str = ""             # Entra tenant | PingOne environment
    client_id: str = ""
    auth_mode: str = ""             # blank = the provider's first mode
    credentials_ref: str = ""       # a vault ref, never the secret
    managed_account: Optional[ManagedAccountRef] = None
    options: dict = {}
    writes_enabled: bool = False
    workgroup: Optional[str] = None


class UpdateIdPRequest(BaseModel):
    name: Optional[str] = None
    writes_enabled: Optional[bool] = None
    credentials_ref: Optional[str] = None
    managed_account: Optional[ManagedAccountRef] = None


def _idp_or_404(db: Session, directory_id: str, user: User):
    row = _row_or_404(db, directory_id, user)
    if row.provider not in directory_service.IDP_PROVIDERS:
        raise HTTPException(status_code=404, detail="not a cloud identity provider")
    return row


@router.get("/idp/options")
def idp_options(user: User = Depends(require_explicit_permission("directories", "read"))):
    from ..services import directory_idp
    from ..services.directory_idp import pingone
    return {
        "providers": [{"id": p, "label": directory_service.PROVIDER_LABELS[p],
                       "auth_modes": list(directory_idp.AUTH_MODES[p]),
                       "options": list(directory_idp.OPTION_KEYS[p])}
                      for p in directory_service.IDP_PROVIDERS],
        "pingone_regions": list(pingone.TLDS),
        "vault_prefixes": list(directory_idp.vault_prefixes()),
    }


@router.post("/register-idp")
async def register_idp(req: RegisterIdPRequest, db: Session = Depends(get_db),
                       user: User = Depends(require_explicit_permission("directories", "write"))):
    if req.managed_account:
        _require_secrets_use(user)
    try:
        row, result = await directory_service.register_idp(
            db, provider=req.provider, name=req.name, endpoint=req.endpoint,
            tenant_id=req.tenant_id, client_id=req.client_id, auth_mode=req.auth_mode,
            credentials_ref=req.credentials_ref,
            managed_account=req.managed_account.model_dump() if req.managed_account else None,
            options=req.options, writes_enabled=req.writes_enabled,
            created_by=user.username, workgroup=req.workgroup)
    except DirectoryError as e:
        raise HTTPException(status_code=400, detail=str(e))
    job_service.log_audit(db, user.username, "directory_register_idp",
                          details={"provider": row.provider, "name": row.name,
                                   "tenant_id": row.tenant_id, "endpoint": row.endpoint,
                                   "writes_enabled": bool(row.writes_enabled)})
    return {**directory_service.to_dict(row), "test": result}


@router.patch("/{directory_id}")
def update_idp(directory_id: str, req: UpdateIdPRequest, db: Session = Depends(get_db),
               user: User = Depends(require_explicit_permission("directories", "write"))):
    row = _idp_or_404(db, directory_id, user)
    if req.managed_account:
        _require_secrets_use(user)
    try:
        row = directory_service.update_idp(
            db, row, name=req.name, writes_enabled=req.writes_enabled,
            credentials_ref=req.credentials_ref,
            managed_account=req.managed_account.model_dump() if req.managed_account else None)
    except DirectoryError as e:
        raise HTTPException(status_code=400, detail=str(e))
    job_service.log_audit(db, user.username, "directory_update_idp", details={
        "directory": row.name, "provider": row.provider,
        "writes_enabled": bool(row.writes_enabled),
        "credential_changed": req.credentials_ref is not None or bool(req.managed_account)})
    return directory_service.to_dict(row)


@router.post("/{directory_id}/test")
async def test_idp(directory_id: str, db: Session = Depends(get_db),
                   user: User = Depends(require_explicit_permission("directories", "read"))):
    row = _idp_or_404(db, directory_id, user)
    try:
        return await directory_service.idp_test(row)
    except DirectoryError as e:
        raise HTTPException(status_code=502, detail=str(e))


async def _idp_read(row, fn: str, *args):
    try:
        return await directory_service.idp_call(row, fn, *args)
    except DirectoryError as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/{directory_id}/users")
async def idp_users(directory_id: str, q: str = "", cursor: str = "",
                    db: Session = Depends(get_db),
                    user: User = Depends(require_explicit_permission("directories", "read"))):
    return await _idp_read(_idp_or_404(db, directory_id, user), "list_users", q, cursor)


@router.get("/{directory_id}/groups")
async def idp_groups(directory_id: str, q: str = "", cursor: str = "",
                     db: Session = Depends(get_db),
                     user: User = Depends(require_explicit_permission("directories", "read"))):
    return await _idp_read(_idp_or_404(db, directory_id, user), "list_groups", q, cursor)


@router.get("/{directory_id}/groups/{group_id}/members")
async def idp_group_members(directory_id: str, group_id: str, cursor: str = "",
                            db: Session = Depends(get_db),
                            user: User = Depends(require_explicit_permission("directories", "read"))):
    return await _idp_read(_idp_or_404(db, directory_id, user), "group_members",
                           group_id, cursor)


@router.get("/{directory_id}/users/{user_id}/groups")
async def idp_user_groups(directory_id: str, user_id: str, db: Session = Depends(get_db),
                          user: User = Depends(require_explicit_permission("directories", "read"))):
    return await _idp_read(_idp_or_404(db, directory_id, user), "user_groups", user_id)


async def _change_membership(directory_id, group_id, user_id, action, db, user):
    row = _idp_or_404(db, directory_id, user)
    try:
        out = await directory_service.change_membership(row, group_id=group_id,
                                                        user_id=user_id, action=action)
    except DirectoryError as e:
        raise HTTPException(status_code=400, detail=str(e))
    job_service.log_audit(db, user.username, f"directory_member_{action}", details={
        "directory": row.name, "provider": row.provider, "tenant_id": row.tenant_id,
        "endpoint": row.endpoint, "group_id": out["group_id"],
        "group_name": out["group_name"], "user_id": user_id, "changed": out["changed"]})
    return out


@router.post("/{directory_id}/groups/{group_id}/members/{user_id}")
async def idp_add_member(directory_id: str, group_id: str, user_id: str,
                         db: Session = Depends(get_db),
                         user: User = Depends(require_explicit_permission("directories", "write"))):
    return await _change_membership(directory_id, group_id, user_id, "add", db, user)


@router.delete("/{directory_id}/groups/{group_id}/members/{user_id}")
async def idp_remove_member(directory_id: str, group_id: str, user_id: str,
                            db: Session = Depends(get_db),
                            user: User = Depends(require_explicit_permission("directories", "write"))):
    return await _change_membership(directory_id, group_id, user_id, "remove", db, user)


# ── Entitle: which integration governs this directory ─────────────────────────
#
# Pinned by an operator, never inferred. Read-only against Entitle: nothing is created
# there, and the pin is only a label on this row.

class EntitleLinkRequest(BaseModel):
    integration_id: str = ""        # "" unpins


@router.get("/{directory_id}/entitle-candidates")
async def entitle_candidates(directory_id: str, db: Session = Depends(get_db),
                             user: User = Depends(require_explicit_permission("directories", "write"))):
    from ..services import entitle_directory_link as link
    row = _row_or_404(db, directory_id, user)
    if not link.configured():
        return {"configured": False, "integrations": [],
                "reason": "Entitle is not configured — set its API URL and token in "
                          "Settings → Integrations → Entitle."}
    try:
        items = await link.list_integrations(row.provider)
    except link.EntitleLinkError as e:
        return {"configured": True, "integrations": [], "error": str(e)}
    return {"configured": True, "integrations": items,
            "pinned": row.entitle_integration_id or ""}


@router.put("/{directory_id}/entitle-integration")
async def pin_entitle_integration(directory_id: str, req: EntitleLinkRequest,
                                  db: Session = Depends(get_db),
                                  user: User = Depends(require_explicit_permission("directories", "write"))):
    """Pin (or with "" unpin) the Entitle integration that governs this directory. The id
    is checked against Entitle's own list, and the NAME recorded is Entitle's."""
    from datetime import datetime
    from ..services import entitle_directory_link as link
    row = _row_or_404(db, directory_id, user)
    wanted = (req.integration_id or "").strip()
    name = ""
    if wanted:
        try:
            items = await link.list_integrations(row.provider)
        except link.EntitleLinkError as e:
            raise HTTPException(status_code=503, detail=str(e))
        found = next((i for i in items if i["id"] == wanted), None)
        if not found:
            raise HTTPException(status_code=400,
                                detail="That integration is not in this Entitle tenant.")
        name = found["name"]
    row.entitle_integration_id = wanted or None
    row.entitle_integration_name = name or None
    row.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(row)
    job_service.log_audit(db, user.username, "directory_entitle_pin", details={
        "directory": row.name, "integration_id": wanted, "integration_name": name})
    return directory_service.to_dict(row)


@router.put("/{directory_id}/join-account")
def set_join_account(directory_id: str, req: JoinAccountRequest, db: Session = Depends(get_db),
                     user: User = Depends(require_explicit_permission("directories", "write"))):
    """Pin (or clear) the AAD DC Administrators account Azure VMs join Entra Domain
    Services as. Only its Password Safe ids and name are stored; the password is checked
    out per join."""
    row = _row_or_404(db, directory_id, user)
    if req.join_account:
        _require_secrets_use(user)
    try:
        row = directory_service.set_join_account(
            db, row, req.join_account.model_dump() if req.join_account else None)
    except DirectoryError as e:
        raise HTTPException(status_code=400, detail=str(e))
    job_service.log_audit(db, user.username, "directory_join_account", details={
        "directory": row.name, "account_name": row.admin_username or "",
        "cleared": not req.join_account})
    return directory_service.to_dict(row)


class HybridJoinRequest(BaseModel):
    entra_hybrid: bool
    hybrid_ou: str = ""


@router.put("/{directory_id}/hybrid")
def set_hybrid_join(directory_id: str, req: HybridJoinRequest, db: Session = Depends(get_db),
                    user: User = Depends(require_explicit_permission("directories", "write"))):
    """Declare that Entra Connect syncs this on-prem domain with hybrid join configured,
    and the OU (in its sync scope) servers should join. Only a declaration: nothing in
    Entra Connect or the domain is changed."""
    from ..services import hybrid_join_service
    row = _row_or_404(db, directory_id, user)
    try:
        row = hybrid_join_service.set_settings(db, row, entra_hybrid=req.entra_hybrid,
                                               hybrid_ou=req.hybrid_ou)
    except hybrid_join_service.HybridError as e:
        raise HTTPException(status_code=400, detail=str(e))
    job_service.log_audit(db, user.username, "directory_hybrid_join", details={
        "directory": row.name, "entra_hybrid": req.entra_hybrid, "hybrid_ou": req.hybrid_ou})
    return directory_service.to_dict(row)
