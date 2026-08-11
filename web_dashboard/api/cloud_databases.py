"""
Database infrastructure API — Phase 1 (gated by ``cloud_database_enabled``).

  POST   /api/databases                 — provision a managed DB (record + schedule apply)
  POST   /api/databases/register        — record a database that already exists
  GET    /api/databases                 — list databases, provisioned and registered
  GET    /api/databases/options         — pickers for the provision form (region-scoped)
  GET    /api/databases/{id}/connection — connection info (the PRA jump is Phase 2)
  DELETE /api/databases/{id}            — decommission, or deregister if registered

Provisioning is cloud-only because it needs a Terraform module; registering needs
only somewhere to reach, so it also covers on-premises (``cloud='local'``).

Permission-gated via the ``cloud_database`` scope (read/write/delete), mirroring
the AWS/Azure/GCP pages; list results are scoped to the caller's own rows for
non-admins. The real Terraform apply and the PRA tunnel (Phase 2, via the
``beyondtrust/sra`` provider) are later work; Phase 1 records and (with cloud
creds) drives the apply as a background task.
"""
import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..config import settings
from ..database import CloudDatabase, User, get_db
from ..services import (aws_service, cache_service, cloud_database_service, config_service,
                        job_service, ps_api_service, ps_database_catalog, region_catalog)
from ..services.aws_service import AWSError
from .auth import require_permission

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/databases", tags=["databases"])


def _require_enabled() -> None:
    if not config_service.get_bool("cloud_database_enabled", settings.cloud_database_enabled):
        raise HTTPException(status_code=403, detail="database infrastructure is disabled")


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


# ── Import from Password Safe ─────────────────────────────────────────────────
#
# Password Safe already runs a discovery scanner with managed credentials, so it
# knows a database's platform, port and requestable accounts authoritatively.
# These two routes read that inventory and register the rows an operator picks —
# nothing in Password Safe is created or changed.
#
# Both are declared ABOVE the /{db_id} routes so a path parameter can never
# capture them, and both demand `secrets:use` on top of the cloud_database scope
# (see _require_secrets_use).

# One batch is an operator ticking boxes in a dialog, not a migration tool.
_MAX_IMPORT_BATCH = 50

# Platform config keys naming the custom plugins THIS dashboard's own cloud-DB
# onboarding creates. A managed system on one of those is already managed here, and
# offering it for import would let a provisioned database acquire a registered twin
# whose host string happens not to match.
_DASHBOARD_PLATFORM_KEYS = (
    "clouddb_ps_platform_postgres", "clouddb_ps_platform_mysql",
    "clouddb_ps_platform_sqlserver", "clouddb_ps_platform_azure_postgres",
    "clouddb_ps_platform_azure_mysql", "clouddb_ps_platform_azure_sqlserver",
)

_PS_GENERIC_ERROR = ("Password Safe lookup failed — check the BeyondTrust "
                     "configuration and server logs.")


def _require_secrets_use(user: User) -> None:
    """Listing Password Safe systems and accounts is the ``secrets:use`` grant.

    It is the same inventory ``GET /api/config-mgmt/managed-accounts`` gates on,
    and this returns strictly *more* of it — that route needs a ``host``, this one
    enumerates the tenant. Importing then pins a managed account for later
    just-in-time checkout, which is ``secrets:use`` in durable form. Without this
    check a holder of ``cloud_database:read`` alone would have a way around the
    scope.

    Reuses config_mgmt's predicate rather than restating it, so the two routes
    cannot drift apart. Imported locally to avoid an import cycle.
    """
    from .config_mgmt import _can_use_secrets
    if not _can_use_secrets(user):
        raise HTTPException(status_code=403, detail="The 'secrets:use' permission is required.")


def _cfg(key: str):
    val = config_service.get(key)
    if val not in (None, ""):
        return val
    return getattr(settings, key, "")


def _import_settings() -> dict:
    try:
        max_systems = int(_cfg("clouddb_ps_import_max_systems"))
    except (TypeError, ValueError):
        max_systems = settings.clouddb_ps_import_max_systems
    default_cloud = str(_cfg("clouddb_ps_import_default_cloud") or "local").strip().lower()
    if default_cloud not in cloud_database_service.VALID_REGISTER_CLOUDS:
        default_cloud = "local"
    return {
        "workgroup": str(_cfg("clouddb_ps_import_workgroup") or "").strip(),
        "default_cloud": default_cloud,
        "max_systems": max(1, max_systems),
        "platform_map": ps_database_catalog.parse_platform_map(
            _cfg("clouddb_ps_import_platform_map")),
        "dashboard_platforms": tuple(
            str(_cfg(k) or "").strip() for k in _DASHBOARD_PLATFORM_KEYS),
    }


def _annotate_imported(db: Session, candidates: list) -> None:
    """Mark the candidates the dashboard already holds a row for.

    Computed here and never baked into the cached Password Safe payload: the
    Password Safe half is tenant-wide and cacheable, this half changes the moment
    anyone imports. Same split as ``api/agent._annotate_findings``, and it means an
    import needs no cache invalidation at all.

    Two deliberate choices:

    * **Not creator-filtered**, unlike the list endpoint. A row another user
      imported must still read as already imported, or two operators race and the
      second gets a per-item failure they cannot explain from the UI.
    * **No status filter**, because ``register_database``'s duplicate check has
      none either — a decommissioned row still blocks a re-register, and greying
      the candidate is how that becomes visible instead of surprising.

    The comparison is case-insensitive while the service's is a plain ``==``, so a
    host differing only in case greys out a row the service would in fact accept.
    That direction is the safe one, and it matches _annotate_findings.
    """
    known = {((row[0] or "").strip().lower(), (row[1] or "").strip().lower())
             for row in db.query(CloudDatabase.private_host, CloudDatabase.engine).all()}
    for candidate in candidates:
        candidate["already_imported"] = (
            (candidate.get("host") or "").strip().lower(),
            (candidate.get("engine") or "").strip().lower()) in known


async def _read_candidates(db: Session) -> dict:
    """Candidate rows plus the counters the modal needs, cache-backed.

    ``get_or_refresh`` re-raises on a cache *miss* but swallows a failed background
    refresh, so after one success a Password Safe outage looks like unchanging
    data — hence ``cached_at`` travelling to the UI.
    """
    cfg = _import_settings()
    cache_key = cache_service.key_param("ps_db_candidates",
                                        workgroup=(cfg["workgroup"] or "*"))

    async def _fetch():
        return await ps_api_service.read_database_inventory(workgroup=cfg["workgroup"])

    raw, cached_at = await cache_service.get_or_refresh(
        cache_key, cache_service.TTL["ps_db_candidates"], _fetch)

    systems, truncated = ps_database_catalog.build_candidates(
        platforms=raw.get("platforms"), systems=raw.get("systems"),
        databases=raw.get("databases"), accounts=raw.get("accounts"),
        platform_map=cfg["platform_map"],
        dashboard_platforms=cfg["dashboard_platforms"],
        max_candidates=cfg["max_systems"])
    _annotate_imported(db, systems)
    return {"systems": systems, "truncated": truncated,
            "warnings": list(raw.get("warnings") or []),
            "cached_at": cached_at, "default_cloud": cfg["default_cloud"]}


@router.get("/ps-candidates")
async def ps_candidates(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("cloud_database", "read")),
):
    """Databases Password Safe knows about, shaped for the import dialog.

    Never 500s and never returns Password Safe's own error text: a ``PSApiError``
    carries a slice of the tenant's response body, which must not reach the caller
    (CodeQL ``py/stack-trace-exposure``). A disabled or unconfigured integration is
    reported as a *state* rather than an error, matching
    ``GET /api/config-mgmt/managed-accounts``, so the dialog can say which switch to
    flip instead of showing a failure.
    """
    _require_enabled()
    _require_secrets_use(current_user)

    if not config_service.get_bool("password_safe_enabled", settings.password_safe_enabled):
        return {"enabled": False, "configured": False, "systems": [],
                "default_cloud": "local", "truncated": False, "warnings": []}
    if not ps_api_service.configured():
        return {"enabled": True, "configured": False, "systems": [],
                "default_cloud": _import_settings()["default_cloud"],
                "truncated": False, "warnings": [],
                "reason": "Password Safe is not configured — set the API URL, client "
                          "id and secret in Settings → Integrations → BeyondTrust."}
    try:
        return {"enabled": True, "configured": True, **(await _read_candidates(db))}
    except ps_api_service.PSApiError as exc:
        logger.warning("Password Safe database inventory read failed: %s", exc)
        return {"enabled": True, "configured": True, "systems": [],
                "default_cloud": _import_settings()["default_cloud"],
                "truncated": False, "warnings": [], "error": _PS_GENERIC_ERROR}
    except Exception:  # noqa: BLE001
        logger.exception("Password Safe database import candidates failed")
        return {"enabled": True, "configured": True, "systems": [],
                "default_cloud": "local", "truncated": False, "warnings": [],
                "error": _PS_GENERIC_ERROR}


class PSImportItem(BaseModel):
    """One database to import, named by its Password Safe ids and nothing else.

    There is deliberately no host, port or account-name field. The server
    re-resolves every ``system_id`` from its own candidate read, which is the same
    property ``config_mgmt.run_playbook_bulk`` gets from resolving inventory ids
    server-side: a caller cannot pair an arbitrary host with an arbitrary managed
    account and register it, bypassing every eligibility rule. ``engine`` may be
    sent, but only to be checked against the server's answer — never trusted.
    """
    system_id: int
    account_id: int
    cloud: str = ""
    engine: str = ""
    db_name: Optional[str] = None


class PSImportRequest(BaseModel):
    items: list[PSImportItem] = []


@router.post("/ps-import")
async def ps_import(
    req: PSImportRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("cloud_database", "write")),
):
    """Register the selected Password Safe databases.

    Two tiers of validation, following ``run_playbook_bulk``:

    * **Selection** problems — nothing to do, too many, the same system twice, two
      systems that would collide on ``(host, engine)``, an invalid location, an
      engine that disagrees with the server's — refuse the *whole* request with a
      400 before a single row is written. A partial success there would be
      arbitrary, and letting item two fail on the uniqueness row item one just
      created reads as a Password Safe fault.
    * **Per-target** problems — the system vanished, it is no longer eligible, the
      account is not on it, the register call refused — fail that item and carry
      on, returning a partial-success envelope. 400 only if nothing succeeded.
    """
    _require_enabled()
    _require_secrets_use(current_user)

    if not config_service.get_bool("password_safe_enabled", settings.password_safe_enabled):
        raise HTTPException(
            status_code=400,
            detail="BeyondTrust is disabled, so there is nothing to import from.")

    items = req.items or []
    if not items:
        raise HTTPException(status_code=400, detail="Select at least one database to import.")
    if len(items) > _MAX_IMPORT_BATCH:
        raise HTTPException(
            status_code=400,
            detail=f"Import at most {_MAX_IMPORT_BATCH} databases at a time "
                   f"({len(items)} selected).")
    system_ids = [i.system_id for i in items]
    if len(set(system_ids)) != len(system_ids):
        raise HTTPException(status_code=400,
                            detail="The same managed system was selected more than once.")

    try:
        found = await _read_candidates(db)
    except ps_api_service.PSApiError as exc:
        logger.warning("Password Safe read failed during import: %s", exc)
        raise HTTPException(status_code=503, detail=_PS_GENERIC_ERROR) from exc

    default_cloud = found["default_cloud"]
    by_id = {c["system_id"]: c for c in found["systems"]}

    # ── selection pass: no writes ──
    seen_targets = {}
    for item in items:
        candidate = by_id.get(item.system_id)
        if candidate is None:
            continue                      # per-target below; the id may have gone
        cloud = (item.cloud or default_cloud).strip().lower()
        if cloud not in cloud_database_service.VALID_REGISTER_CLOUDS:
            raise HTTPException(
                status_code=400,
                detail=f"{cloud!r} is not a valid location. Choose one of: "
                       f"{', '.join(sorted(cloud_database_service.VALID_REGISTER_CLOUDS))}.")
        if item.engine and item.engine.strip().lower() != candidate["engine"]:
            raise HTTPException(
                status_code=400,
                detail=f"Password Safe reports {candidate['name'] or item.system_id} as "
                       f"{candidate['engine'] or 'an unsupported engine'}, not "
                       f"{item.engine!r}.")
        target = ((candidate.get("host") or "").strip().lower(), candidate["engine"])
        if target in seen_targets:
            raise HTTPException(
                status_code=400,
                detail=f"Two selected systems point at the same database "
                       f"({target[0]}, {target[1]}): "
                       f"{seen_targets[target]} and {candidate['name'] or item.system_id}. "
                       f"Import one of them.")
        seen_targets[target] = candidate["name"] or item.system_id

    # ── per-target pass ──
    batch_id = str(uuid.uuid4())
    imported, failed = [], []
    for item in items:
        candidate = by_id.get(item.system_id)
        name = (candidate or {}).get("name") or str(item.system_id)
        if candidate is None:
            failed.append({"system_id": item.system_id, "name": name,
                           "error": "no longer present in Password Safe"})
            continue
        if candidate.get("already_imported"):
            failed.append({"system_id": item.system_id, "name": name,
                           "error": "already registered in the dashboard"})
            continue
        if not candidate.get("eligible"):
            failed.append({"system_id": item.system_id, "name": name,
                           "error": candidate.get("reason") or "not importable"})
            continue
        if not ps_database_catalog.find_account(candidate, item.account_id):
            failed.append({"system_id": item.system_id, "name": name,
                           "error": "the selected account is not a requestable account "
                                    "on that managed system"})
            continue

        payload = ps_database_catalog.import_request(
            candidate, cloud=(item.cloud or default_cloud), account_id=item.account_id)
        if item.db_name is not None:
            payload["db_name"] = item.db_name.strip()
        try:
            # Always through register_database, never around it: that is what keeps
            # the credential-never-persisted property (and its test) covering
            # imported rows too.
            result = cloud_database_service.register_database(
                db, engine=payload["engine"], cloud=payload["cloud"],
                host=payload["host"], port=payload["port"], db_name=payload["db_name"],
                managed_account=payload["managed_account"],
                created_by=current_user.username,
                region=payload["region"], instance_id=payload["instance_id"])
        except cloud_database_service.CloudDatabaseError as exc:
            # The dashboard's own text — safe to pass through.
            failed.append({"system_id": item.system_id, "name": name, "error": str(exc)})
            continue
        imported.append({"system_id": item.system_id, "name": name,
                         "db_id": result.get("id"),
                         "host": payload["host"], "engine": payload["engine"]})

    job_service.log_audit(db, current_user.username, "clouddb_ps_import", details={
        "batch_id": batch_id, "count": len(imported),
        "system_ids": [i["system_id"] for i in imported],
        "failed": [f["system_id"] for f in failed]})

    if not imported:
        raise HTTPException(
            status_code=400,
            detail=f"No databases were imported. First failure: {failed[0]['error']}"
                   if failed else "No databases were imported.")
    return {"batch_id": batch_id, "count": len(imported),
            "imported": imported, "failed": failed}


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


class RegisterDatabaseRequest(BaseModel):
    """A database that already exists. No Terraform inputs — the dashboard is recording
    it, not building it."""
    engine: str
    cloud: str                       # aws | azure | gcp | oci | local ('local' = on-prem)
    host: str
    port: Optional[int] = None
    db_name: str = ""
    region: str = ""
    instance_id: str = ""
    # Password Safe system + account. The credential itself is checked out at run time
    # and never stored, so only ids and a name travel.
    managed_account: dict


@router.post("/register", status_code=201)
async def register_database(
    req: RegisterDatabaseRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("cloud_database", "write")),
):
    """Record an existing database so it can be a Config Management target — the
    database counterpart of registering a Kubernetes cluster from a kubeconfig."""
    _require_enabled()
    try:
        return cloud_database_service.register_database(
            db, engine=req.engine, cloud=req.cloud, host=req.host, port=req.port,
            db_name=req.db_name, managed_account=req.managed_account or {},
            created_by=current_user.username, region=req.region,
            instance_id=req.instance_id,
        )
    except cloud_database_service.CloudDatabaseError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/{db_id}")
async def decommission_database(
    db_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("cloud_database", "delete")),
):
    """Destroy a provisioned database, or deregister a registered one.

    The verb follows ``source``, as it does for Kubernetes clusters: the dashboard holds
    Terraform state for what it provisioned and nothing at all for what it was merely
    told about, so deleting someone else's database is never the right action here."""
    _require_enabled()
    row = cloud_database_service.get_database(db, db_id)
    if row is not None and row.get("source") == "registered":
        try:
            cloud_database_service.deregister_database(db, db_id)
            return {"id": db_id, "status": "deregistered"}
        except cloud_database_service.CloudDatabaseError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
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
        info = cloud_database_service.connection_info(db, db_id)   # 404 if unknown
    except cloud_database_service.CloudDatabaseError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    # Fast, clear rejection for a row Entitle can't onboard: a registered database (no
    # provisioning credential to register with) or a managed SQL Server flavor whose
    # connector needs sysadmin/CONTROL SERVER. The service enforces this too, but a 400
    # here beats a queued-then-failed job — and the message comes from the same function
    # that hides the button, so the two can't drift. Deregister is always allowed so a
    # previously-created integration can still be cleaned up.
    if payload.action == "register":
        reason = cloud_database_service._entitle_ineligible_reason(
            info["engine"], info.get("provider"),
            source=info.get("source"), cloud=info.get("cloud"))
        if reason:
            raise HTTPException(status_code=400, detail=reason)
    job = job_service.create_job(
        db, job_type="clouddb_entitle_register", created_by=current_user.username,
        metadata={"db_id": db_id, "action": payload.action},
    )
    return {"ok": True,
            "status": "registering" if payload.action == "register" else "deregistering",
            "db_id": db_id, "action": payload.action, "job_id": job.id}
