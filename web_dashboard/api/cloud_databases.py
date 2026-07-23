"""
Cloud database infrastructure API — Phase 1 (gated by ``cloud_database_enabled``).

  POST   /api/databases                 — provision a managed DB (record + schedule apply)
  GET    /api/databases                 — list dashboard-provisioned databases
  GET    /api/databases/options         — pickers for the provision form (region-scoped)
  GET    /api/databases/{id}/connection — connection info (the PRA jump is Phase 2)
  DELETE /api/databases/{id}            — decommission

Permission-gated via the ``cloud_database`` scope (read/write/delete), mirroring
the AWS/Azure/GCP pages; list results are scoped to the caller's own rows for
non-admins. The real Terraform apply and the PRA tunnel (Phase 2, via the
``beyondtrust/sra`` provider) are later work; Phase 1 records and (with cloud
creds) drives the apply as a background task.
"""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..config import settings
from ..database import User, get_db
from ..services import aws_service, cache_service, cloud_database_service, config_service, job_service, region_catalog
from ..services.aws_service import AWSError
from .auth import require_permission

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
    # OCI (Autonomous Database) — free-tier by default (public endpoint reached via
    # the PRA tcp tunnel). Beyond free tier needs oci_is_free_tier=false + a subnet.
    oci_db_workload: Optional[str] = None       # "OLTP" (ATP) | "DW" (ADW)
    oci_is_free_tier: Optional[bool] = None
    oci_cpu_core_count: Optional[int] = None
    oci_data_storage_tbs: Optional[int] = None
    oci_subnet_ocid: Optional[str] = None
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
    # Selectable region ids for the provision-form dropdown (configured/picked
    # region first) — mirrors the k8s provision form so both draw from the shared
    # region catalog instead of a free-text box. Empty only on an unknown cloud.
    regions: list[str] = []
    instance_classes: list[str]
    db_subnet_groups: list[dict]
    security_groups: list[dict]
    vault_account_groups: list[dict] = []
    # PRA Jump Groups / Jumpoints for the per-DB tunnel pickers (cloud-agnostic —
    # PRA objects aren't region/cloud-scoped). Empty when PRA isn't configured.
    jump_groups: list[dict] = []
    jumpoints: list[dict] = []
    cached_at: Optional[str] = None


# Region validation + default resolution is centralised in services/region_catalog
# (junk input is rejected before boto/caching so it can't create unbounded cache
# keys or hang on a nonexistent endpoint). These stay: the per-cloud size pickers.
# Cloud SQL machine tiers offered in the GCP provision form (shared-core first).
_GCP_TIERS = ["db-f1-micro", "db-g1-small", "db-custom-1-3840",
              "db-custom-2-7680", "db-custom-4-15360"]
# Flexible Server SKUs offered in the Azure provision form (burstable first).
_AZURE_SKUS = ["B_Standard_B1ms", "B_Standard_B2s",
               "GP_Standard_D2s_v3", "GP_Standard_D4s_v3"]
# ADB workloads offered in the OCI provision form (ATP first). Free-tier sizing
# is fixed (1 OCPU / 20 GB), so there's no size picker — just the workload.
_OCI_WORKLOADS = ["OLTP", "DW"]


async def _pra_pickers() -> dict:
    """PRA-sourced provision-form pickers — Vault account groups, Jump Groups and
    Jumpoints — fetched concurrently. Cloud-agnostic (PRA objects aren't
    region/cloud-scoped). Best-effort: any individual failure yields an empty
    list for that picker (the dropdown just falls back to the configured default
    at broker time)."""
    from ..services import pra_api_service
    try:
        return await pra_api_service.list_pickers()
    except Exception as exc:
        logger.warning("PRA pickers fetch failed (non-fatal): %s", exc)
        return {"vault_account_groups": [], "jump_groups": [], "jumpoints": []}


def _resolve_db_region(cloud: str, region: Optional[str]) -> str:
    """Validate + default-resolve a provision-form region through the shared region
    catalog (blank → configured default; malformed → HTTP 400)."""
    try:
        return region_catalog.resolve(cloud, region)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def _region_choices(cloud: str, resolved_region: str) -> list[str]:
    """Region ids for the provision-form dropdown, with ``resolved_region`` (the
    configured default or the just-picked region) guaranteed present and first
    (order-preserving, de-duplicated). Draws from the shared ``region_catalog`` so
    the DB form mirrors the k8s form; the catalog is a convenience list, not an
    allow-list, so a custom region still shows up (it's forced in first)."""
    seen, out = set(), []
    for r in [resolved_region, *region_catalog.region_ids(cloud)]:
        r = (r or "").strip()
        if r and r not in seen:
            seen.add(r)
            out.append(r)
    return out


@router.post("")
async def provision_database(
    payload: ProvisionRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("cloud_database", "write")),
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
        "oci_db_workload": payload.oci_db_workload,
        "oci_is_free_tier": payload.oci_is_free_tier,
        "oci_cpu_core_count": payload.oci_cpu_core_count,
        "oci_data_storage_tbs": payload.oci_data_storage_tbs,
        "oci_subnet_ocid": payload.oci_subnet_ocid,
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

    # The secret-stripped Terraform vars are embedded in the job metadata atomically
    # by provision() (the master password is never written to jobs.extra_data;
    # run_provision_apply re-injects it from the secrets backend). Embedding at
    # create time means the dedicated job runner can't claim the pending job in a
    # window before tf_variables is persisted → dispatch with no tf_variables.
    return {"ok": True, "db_id": result["db_id"], "job_id": result["job_id"]}


@router.get("")
async def list_databases(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("cloud_database", "read")),
):
    _require_enabled()
    # These rows carry no workgroup — only a creator — so mirror the ownerless
    # branch of inventory_service.visible_to: admins see all, everyone else sees
    # only the databases they provisioned.
    rows = cloud_database_service.list_databases(db)
    if not current_user.is_effective_admin:
        rows = [r for r in rows if r.get("created_by") == current_user.username]
    return {"databases": rows}


@router.get("/options", response_model=DatabaseOptions)
async def database_options(
    region: Optional[str] = None,
    cloud: str = "aws",
    current_user: User = Depends(require_permission("cloud_database", "read")),
):
    """Pickers for the provision form. AWS: instance classes (static) + DB subnet
    groups + security groups, cached per region. GCP: Cloud SQL machine tiers
    (the private_network comes from the sandbox's gcp_db_network config, so no
    subnet/SG pickers). Vault account groups are cloud-agnostic."""
    _require_enabled()
    cloud = (cloud or "aws").strip().lower()

    if cloud == "gcp":
        region = _resolve_db_region("gcp", region)
        return DatabaseOptions(
            region=region, regions=_region_choices("gcp", region),
            instance_classes=_GCP_TIERS,
            db_subnet_groups=[], security_groups=[],
            cached_at=None, **(await _pra_pickers()),
        )

    if cloud == "azure":
        region = _resolve_db_region("azure", region)
        return DatabaseOptions(
            region=region, regions=_region_choices("azure", region),
            instance_classes=_AZURE_SKUS,
            db_subnet_groups=[], security_groups=[],
            cached_at=None, **(await _pra_pickers()),
        )

    if cloud == "oci":
        # Autonomous DB: workload picker (no size — free tier is fixed 1 OCPU/20 GB).
        # The compartment/subnet come from the sandbox-emitted oci_* config.
        region = _resolve_db_region("oci", region)
        return DatabaseOptions(
            region=region, regions=_region_choices("oci", region),
            instance_classes=_OCI_WORKLOADS,
            db_subnet_groups=[], security_groups=[],
            cached_at=None, **(await _pra_pickers()),
        )

    region = _resolve_db_region("aws", region)

    cache_key = cache_service.key_param("aws_db_options", region=region)
    ttl = cache_service.TTL["aws_db_options"]

    async def _fetch():
        return await aws_service.get_db_options(region)

    try:
        opts, cached_at = await cache_service.get_or_refresh(cache_key, ttl, _fetch)
    except AWSError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return DatabaseOptions(
        **opts, regions=_region_choices("aws", region),
        cached_at=cached_at, **(await _pra_pickers()))


@router.get("/{db_id}/connection")
async def connection(
    db_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("cloud_database", "read")),
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
    current_user: User = Depends(require_permission("cloud_database", "delete")),
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


class EntitleDatabaseRegisterRequest(BaseModel):
    action: str = "register"   # register | deregister


@router.post("/{db_id}/entitle-register", status_code=202)
async def register_database_in_entitle(
    db_id: str,
    payload: EntitleDatabaseRegisterRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("cloud_database", "write")),
):
    """Register (or deregister) a provisioned database as an Entitle integration
    (PostgreSQL / MySQL / SQL Server) so users request JIT access in Entitle. The
    private DB is reached by the shared Entitle agent; the PRA tunnel the dashboard
    brokers is the separate path the user's client connects through. Async —
    enqueues a ``clouddb_entitle_register`` job; open the job for status/error.
    Mirrors the k8s cluster ``entitle-register`` endpoint."""
    _require_enabled()
    if payload.action not in cloud_database_service.VALID_ENTITLE_DB_ACTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"unknown action {payload.action!r} (expected one of "
                   f"{', '.join(cloud_database_service.VALID_ENTITLE_DB_ACTIONS)})",
        )
    if payload.action == "register" and not config_service.get_bool("entitle_registration_enabled", False):
        raise HTTPException(
            status_code=409,
            detail="Entitle registration is disabled (set entitle_registration_enabled)")
    try:
        cloud_database_service.connection_info(db, db_id)   # 404 if unknown
    except cloud_database_service.CloudDatabaseError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    job = job_service.create_job(
        db, job_type="clouddb_entitle_register", created_by=current_user.username,
        metadata={"db_id": db_id, "action": payload.action},
    )
    return {"ok": True,
            "status": "registering" if payload.action == "register" else "deregistering",
            "db_id": db_id, "action": payload.action, "job_id": job.id}
