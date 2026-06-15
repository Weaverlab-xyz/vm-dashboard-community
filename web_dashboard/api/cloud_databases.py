"""
Cloud database infrastructure API — Phase 1 (gated by ``cloud_database_enabled``).

  POST   /api/databases                 — provision a managed DB (record + schedule apply)
  GET    /api/databases                 — list dashboard-provisioned databases
  GET    /api/databases/options         — pickers for the provision form (region-scoped)
  GET    /api/databases/{id}/connection — connection info (the PRA jump is Phase 2)
  DELETE /api/databases/{id}            — decommission

Admin-only. The real Terraform apply and the PRA tunnel (Phase 2, via the
``beyondtrust/sra`` provider) are later work; Phase 1 records and (with cloud
creds) drives the apply as a background task.
"""
import logging
import re
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..config import settings
from ..database import User, get_db
from ..services import aws_service, cache_service, cloud_database_service, config_service
from ..services.aws_service import AWSError
from .auth import require_admin

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/databases", tags=["cloud-databases"])


def _require_enabled() -> None:
    if not config_service.get_bool("cloud_database_enabled", settings.cloud_database_enabled):
        raise HTTPException(status_code=403, detail="cloud database infrastructure is disabled")


class ProvisionRequest(BaseModel):
    engine: str
    cloud: str
    region: str
    name: str
    master_username: str = "dbadmin"
    instance_class: Optional[str] = None
    allocated_storage: Optional[int] = None
    db_subnet_group_name: Optional[str] = None
    vpc_security_group_ids: Optional[list[str]] = None
    # GCP (Cloud SQL) — machine tier + disk; private_network falls back to the
    # sandbox-emitted gcp_db_network config when omitted.
    tier: Optional[str] = None
    disk_size: Optional[int] = None
    private_network: Optional[str] = None
    # PRA Vault account group the injected credential lands in — an unassigned
    # vault account is injectable by nobody, so the form offers a picker.
    vault_account_group_id: Optional[int] = None
    # Per-DB PRA broker overrides — config defaults are the fallback. Values are
    # secrets-backend references (e.g. aws_sm://…), not raw secrets.
    jump_group: Optional[str] = None          # PRA Jump Group name override (else bt_jump_group_name)
    jumpoint_name: Optional[str] = None       # PRA Jumpoint name override (else bt_jumpoint_name)
    pra_credential_ref: Optional[str] = None  # secret ref → bt_client_secret override


class DatabaseOptions(BaseModel):
    region: str
    instance_classes: list[str]
    db_subnet_groups: list[dict]
    security_groups: list[dict]
    vault_account_groups: list[dict] = []
    cached_at: Optional[str] = None


# us-east-2, ap-southeast-3, us-gov-west-1 — checked before boto/caching so junk
# input can't create unbounded cache keys or hang on a nonexistent endpoint.
_REGION_RE = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d$")
# GCP regions: us-central1, europe-west4, asia-southeast1, …
_GCP_REGION_RE = re.compile(r"^[a-z]+-[a-z]+\d$")
# Cloud SQL machine tiers offered in the GCP provision form (shared-core first).
_GCP_TIERS = ["db-f1-micro", "db-g1-small", "db-custom-1-3840",
              "db-custom-2-7680", "db-custom-4-15360"]


async def _vault_account_groups() -> list:
    """PRA Vault account groups for the credential-injection picker (best-effort;
    an empty list just hides the dropdown's options). Cloud-agnostic."""
    try:
        from ..services import pra_api_service
        if pra_api_service.configured():
            return await pra_api_service.list_vault_account_groups()
    except Exception as exc:
        logger.warning("vault account-group listing failed (non-fatal): %s", exc)
    return []


def _default_region() -> str:
    return config_service.get("aws_region") or settings.aws_region or "us-east-2"


def _apply_task(db_id: str, job_id: str, engine: str, tf_variables: dict) -> None:
    """Background worker: open a fresh session and drive the Terraform apply."""
    import asyncio
    from ..database import SessionLocal
    s = SessionLocal()
    try:
        asyncio.run(cloud_database_service.run_provision_apply(
            s, db_id=db_id, job_id=job_id, engine=engine, tf_variables=tf_variables))
    finally:
        s.close()


def _decommission_task(db_id: str, job_id: str) -> None:
    """Background worker: open a fresh session and drive the teardown."""
    import asyncio
    from ..database import SessionLocal
    s = SessionLocal()
    try:
        asyncio.run(cloud_database_service.run_decommission(s, db_id=db_id, job_id=job_id))
    finally:
        s.close()


@router.post("")
async def provision_database(
    payload: ProvisionRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    _require_enabled()
    opts = {k: v for k, v in {
        "instance_class": payload.instance_class,
        "allocated_storage": payload.allocated_storage,
        "db_subnet_group_name": payload.db_subnet_group_name,
        "vpc_security_group_ids": payload.vpc_security_group_ids,
        "tier": payload.tier,
        "disk_size": payload.disk_size,
        "private_network": payload.private_network,
    }.items() if v is not None}
    try:
        result = cloud_database_service.provision(
            db, engine=payload.engine, cloud=payload.cloud, region=payload.region,
            name=payload.name, created_by=current_user.username,
            master_username=payload.master_username,
            vault_account_group_id=payload.vault_account_group_id,
            jump_group=payload.jump_group, jumpoint_name=payload.jumpoint_name,
            pra_credential_ref=payload.pra_credential_ref, **opts,
        )
    except cloud_database_service.CloudDatabaseError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except NotImplementedError as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc

    background_tasks.add_task(
        _apply_task, result["db_id"], result["job_id"], payload.engine, result["tf_variables"])
    return {"ok": True, "db_id": result["db_id"], "job_id": result["job_id"]}


@router.get("")
async def list_databases(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    _require_enabled()
    return {"databases": cloud_database_service.list_databases(db)}


@router.get("/options", response_model=DatabaseOptions)
async def database_options(
    region: Optional[str] = None,
    cloud: str = "aws",
    current_user: User = Depends(require_admin),
):
    """Pickers for the provision form. AWS: instance classes (static) + DB subnet
    groups + security groups, cached per region. GCP: Cloud SQL machine tiers
    (the private_network comes from the sandbox's gcp_db_network config, so no
    subnet/SG pickers). Vault account groups are cloud-agnostic."""
    _require_enabled()
    cloud = (cloud or "aws").strip().lower()

    if cloud == "gcp":
        region = (region or "").strip() or (config_service.get("gcp_region") or "us-central1")
        if not _GCP_REGION_RE.fullmatch(region):
            raise HTTPException(status_code=400, detail=f"invalid GCP region {region!r}")
        return DatabaseOptions(
            region=region, instance_classes=_GCP_TIERS,
            db_subnet_groups=[], security_groups=[],
            vault_account_groups=await _vault_account_groups(), cached_at=None,
        )

    region = (region or "").strip() or _default_region()
    if not _REGION_RE.fullmatch(region):
        raise HTTPException(status_code=400, detail=f"invalid AWS region {region!r}")

    cache_key = cache_service.key_param("aws_db_options", region=region)
    ttl = cache_service.TTL["aws_db_options"]

    async def _fetch():
        return await aws_service.get_db_options(region)

    try:
        opts, cached_at = await cache_service.get_or_refresh(cache_key, ttl, _fetch)
    except AWSError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return DatabaseOptions(
        **opts, vault_account_groups=await _vault_account_groups(), cached_at=cached_at)


@router.get("/{db_id}/connection")
async def connection(
    db_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    _require_enabled()
    try:
        return cloud_database_service.connection_info(db, db_id)
    except cloud_database_service.CloudDatabaseError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete("/{db_id}")
async def decommission_database(
    db_id: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    _require_enabled()
    try:
        result = cloud_database_service.start_decommission(db, db_id, created_by=current_user.username)
        background_tasks.add_task(_decommission_task, result["db_id"], result["job_id"])
        return result
    except cloud_database_service.CloudDatabaseError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
