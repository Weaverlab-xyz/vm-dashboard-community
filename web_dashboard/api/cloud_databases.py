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

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..config import settings
from ..database import User, get_db
from ..services import aws_service, cache_service, cloud_database_service, config_service, job_service
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
    # Azure (Flexible Server) — SKU + storage; the delegated subnet, private DNS
    # zone and resource group fall back to the sandbox-emitted azure_db_* config.
    sku_name: Optional[str] = None
    storage_mb: Optional[int] = None
    # PRA Vault account group the injected credential lands in — an unassigned
    # vault account is injectable by nobody, so the form offers a picker.
    vault_account_group_id: Optional[int] = None
    # Per-DB PRA broker overrides — config defaults are the fallback. Values are
    # secrets-backend references (e.g. aws_sm://…), not raw secrets.
    jump_group: Optional[str] = None          # PRA Jump Group name override (else bt_jump_group_name)
    jumpoint_name: Optional[str] = None       # PRA Jumpoint name override (else bt_jumpoint_name)
    pra_credential_ref: Optional[str] = None  # secret ref → bt_client_secret override
    register_in_entitle: bool = False         # opt in to registering this DB as an Entitle integration


class DatabaseOptions(BaseModel):
    region: str
    instance_classes: list[str]
    db_subnet_groups: list[dict]
    security_groups: list[dict]
    vault_account_groups: list[dict] = []
    # PRA Jump Groups / Jumpoints for the per-DB tunnel pickers (cloud-agnostic —
    # PRA objects aren't region/cloud-scoped). Empty when PRA isn't configured.
    jump_groups: list[dict] = []
    jumpoints: list[dict] = []
    cached_at: Optional[str] = None


# us-east-2, ap-southeast-3, us-gov-west-1 — checked before boto/caching so junk
# input can't create unbounded cache keys or hang on a nonexistent endpoint.
_REGION_RE = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d$")
# GCP regions: us-central1, europe-west4, asia-southeast1, …
_GCP_REGION_RE = re.compile(r"^[a-z]+-[a-z]+\d$")
# Cloud SQL machine tiers offered in the GCP provision form (shared-core first).
_GCP_TIERS = ["db-f1-micro", "db-g1-small", "db-custom-1-3840",
              "db-custom-2-7680", "db-custom-4-15360"]
# Azure regions: eastus, westus2, centralus, northeurope, australiaeast …
_AZURE_REGION_RE = re.compile(r"^[a-z]{3,}\d?$")
# Flexible Server SKUs offered in the Azure provision form (burstable first).
_AZURE_SKUS = ["B_Standard_B1ms", "B_Standard_B2s",
               "GP_Standard_D2s_v3", "GP_Standard_D4s_v3"]


async def _pra_pickers() -> dict:
    """PRA-sourced provision-form pickers — Vault account groups, Jump Groups and
    Jumpoints — fetched concurrently. Cloud-agnostic (PRA objects aren't
    region/cloud-scoped). Best-effort: any individual failure yields an empty
    list for that picker (the dropdown just falls back to the configured default
    at broker time)."""
    empty = {"vault_account_groups": [], "jump_groups": [], "jumpoints": []}
    try:
        import asyncio
        from ..services import pra_api_service
        if not pra_api_service.configured():
            return empty
        vg, jg, jp = await asyncio.gather(
            pra_api_service.list_vault_account_groups(),
            pra_api_service.list_jump_groups(),
            pra_api_service.list_jumpoints(),
            return_exceptions=True,
        )

        def _ok(x, what):
            if isinstance(x, Exception):
                logger.warning("PRA %s listing failed (non-fatal): %s", what, x)
                return []
            return x

        return {
            "vault_account_groups": _ok(vg, "vault account-group"),
            "jump_groups": _ok(jg, "jump-group"),
            "jumpoints": _ok(jp, "jumpoint"),
        }
    except Exception as exc:
        logger.warning("PRA pickers fetch failed (non-fatal): %s", exc)
        return empty


def _default_region() -> str:
    return config_service.get("aws_region") or settings.aws_region or "us-east-2"


@router.post("")
async def provision_database(
    payload: ProvisionRequest,
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
        "sku_name": payload.sku_name,
        "storage_mb": payload.storage_mb,
    }.items() if v is not None}

    # Pre-action policy gate (inert unless enabled + this action is gated).
    from ..services import admission_service
    admission_service.enforce(
        "clouddb:provision",
        request={"region": payload.region, "engine": payload.engine,
                 "cloud": payload.cloud, "name": payload.name,
                 "instance_type": payload.instance_class or payload.tier or payload.sku_name or ""},
        actor=current_user, db=db,
    )
    try:
        result = cloud_database_service.provision(
            db, engine=payload.engine, cloud=payload.cloud, region=payload.region,
            name=payload.name, created_by=current_user.username,
            master_username=payload.master_username,
            vault_account_group_id=payload.vault_account_group_id,
            jump_group=payload.jump_group, jumpoint_name=payload.jumpoint_name,
            pra_credential_ref=payload.pra_credential_ref,
            register_in_entitle=payload.register_in_entitle, **opts,
        )
    except cloud_database_service.CloudDatabaseError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except NotImplementedError as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc

    # Persist the Terraform vars on the job — MINUS the master password (never store
    # a secret in jobs.extra_data; run_provision_apply re-injects it from the secrets
    # backend). The dedicated job runner claims the pending job and runs the apply.
    tf_vars = {k: v for k, v in result["tf_variables"].items()
               if k not in ("master_password", "administrator_password")}
    job_service.update_metadata(db, result["job_id"], {"tf_variables": tf_vars})
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
            cached_at=None, **(await _pra_pickers()),
        )

    if cloud == "azure":
        region = (region or "").strip() or (config_service.get("azure_location") or "eastus")
        if not _AZURE_REGION_RE.fullmatch(region):
            raise HTTPException(status_code=400, detail=f"invalid Azure location {region!r}")
        return DatabaseOptions(
            region=region, instance_classes=_AZURE_SKUS,
            db_subnet_groups=[], security_groups=[],
            cached_at=None, **(await _pra_pickers()),
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
        **opts, cached_at=cached_at, **(await _pra_pickers()))


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
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    _require_enabled()
    try:
        # start_decommission creates the pending clouddb_decommission job; the job
        # runner claims it and drives the teardown (no payload needed — the run fn
        # rebuilds the destroy vars from the row + config).
        result = cloud_database_service.start_decommission(db, db_id, created_by=current_user.username)
        return result
    except cloud_database_service.CloudDatabaseError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
