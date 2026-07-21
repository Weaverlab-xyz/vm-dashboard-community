"""
Cloud database infrastructure — the engine/cloud-agnostic service seam (community).

Provisions **private** managed databases (Postgres / MySQL / SQL Server) reached
only through a BeyondTrust PRA tunnel, and records each in the ``cloud_databases``
inventory table. Shaped like the other cloud services; drives Terraform via a
per-job deploy dir (``terraform/deployments/{job_id}``).

Implements **postgres / mysql / sqlserver across aws / azure / gcp**
end-to-end on the dashboard side (record + Terraform variables + apply/destroy
plumbing); see ``_IMPLEMENTED`` for the supported engine/cloud matrix —
anything outside it raises ``NotImplementedError``. The PRA tunnel is brokered
with the ``beyondtrust/sra`` Terraform provider (``terraform_pra_service``) —
**never ``btapi``** — so MongoDB is not offered in community until the provider
ships a resource. Credentials are stored encrypted in the DB via ``config_service``
(community has no Password Safe dependency).

``provision`` does the synchronous record-keeping and returns; the actual
``terraform apply`` runs in :func:`run_provision_apply` (scheduled as a
background task by the API). The real apply needs cloud creds — dev mocks it.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import shutil
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from ..config import settings
from ..database import CloudDatabase, Job
from . import config_service, job_service, terraform, terraform_provider_env
from .region_config import resolve_region

logger = logging.getLogger(__name__)

# Community supports the three engines the beyondtrust/sra provider can tunnel
# (no MongoDB resource yet). All engine × cloud combos are wired — see _IMPLEMENTED.
VALID_ENGINES = {"postgres", "mysql", "sqlserver", "oracle"}
VALID_CLOUDS = {"aws", "azure", "gcp", "oci"}
_IMPLEMENTED = {
    ("postgres", "aws"), ("postgres", "gcp"), ("postgres", "azure"),
    ("mysql", "aws"), ("mysql", "azure"), ("mysql", "gcp"),
    ("sqlserver", "aws"), ("sqlserver", "gcp"), ("sqlserver", "azure"),
    # OCI Autonomous Database (ATP/ADW) — a managed PaaS, unlike the RDS/Cloud
    # SQL/Flexible-Server engines; reached over a generic PRA tcp tunnel (no SSH
    # jump-host managed-user path — that's AWS-only, gated on cloud=="aws").
    ("oracle", "oci"),
}
_PROVIDER = {
    ("postgres", "aws"): "rds",
    ("postgres", "gcp"): "cloudsql",
    ("postgres", "azure"): "flexibleserver",
    ("mysql", "aws"): "rds",
    ("mysql", "azure"): "flexibleserver",
    ("mysql", "gcp"): "cloudsql",
    ("sqlserver", "aws"): "rds",
    ("sqlserver", "gcp"): "cloudsql",
    ("sqlserver", "azure"): "sql_database",
    ("oracle", "oci"): "autonomous",
}

# terraform/<dir> module per (engine, cloud) — relative to repo root (parents[2]).
_REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
_TEMPLATE_DIRS = {
    ("postgres", "aws"): os.path.join(_REPO_ROOT, "terraform", "db_postgres"),
    ("postgres", "gcp"): os.path.join(_REPO_ROOT, "terraform", "db_gcp_postgres"),
    ("postgres", "azure"): os.path.join(_REPO_ROOT, "terraform", "db_azure_postgres"),
    ("mysql", "aws"): os.path.join(_REPO_ROOT, "terraform", "db_mysql"),
    ("mysql", "azure"): os.path.join(_REPO_ROOT, "terraform", "db_azure_mysql"),
    ("mysql", "gcp"): os.path.join(_REPO_ROOT, "terraform", "db_gcp_mysql"),
    ("sqlserver", "aws"): os.path.join(_REPO_ROOT, "terraform", "db_sqlserver"),
    ("sqlserver", "gcp"): os.path.join(_REPO_ROOT, "terraform", "db_gcp_sqlserver"),
    ("sqlserver", "azure"): os.path.join(_REPO_ROOT, "terraform", "db_azure_sqlserver"),
    ("oracle", "oci"): os.path.join(_REPO_ROOT, "terraform", "db_oci_autonomous"),
}
_DEPLOYMENTS_DIR = os.path.join(_REPO_ROOT, "terraform", "deployments")

# oracle = the ADB TLS (no-wallet) listener port. mTLS would be 1522.
_DEFAULT_PORTS = {"postgres": 5432, "mysql": 3306, "sqlserver": 1433, "oracle": 1521}

# tf_variables keys that hold the admin secret — stripped before the -var set is
# persisted to the job metadata (a secret is never written to jobs.extra_data).
# run_provision_apply re-injects the password from the secrets backend. aws/gcp
# use master_password; azure's Flexible Server module uses administrator_password;
# OCI Autonomous DB uses admin_password.
_SECRET_TF_KEYS = ("master_password", "administrator_password", "admin_password")


class CloudDatabaseError(Exception):
    pass


def terraform_available() -> bool:
    return shutil.which(settings.terraform_executable) is not None


def template_dir(engine: str, cloud: str) -> str:
    return _TEMPLATE_DIRS[(engine, cloud)]


def _deploy_dir(job_id: str) -> str:
    return os.path.join(_DEPLOYMENTS_DIR, job_id)


def _db_name_from(name: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_]", "_", name).strip("_").lower() or "appdb"
    if not slug[0].isalpha():
        slug = "db_" + slug
    return slug[:63]


def _oracle_db_name(db_id: str) -> str:
    """OCI Autonomous DB ``db_name``: <=14 chars, alphanumeric, letter-led (no
    hyphens/underscores). Derived deterministically from the row id."""
    return ("adb" + re.sub(r"[^a-z0-9]", "", db_id.lower()))[:14]


def _build_tf_variables(
    *, engine: str, cloud: str, region: str, db_id: str, db_name: str,
    master_username: str, master_password: str, opts: dict,
) -> dict:
    """The Terraform -var set for the engine module (per engine/cloud branch below).

    The module itself hardcodes ``publicly_accessible = false`` — the private-only
    guarantee lives in the .tf, not in a toggle-able variable.

    Per-region resource ids (subnets, DB networks, resource group) resolve through
    ``region_config.resolve_region(cloud, region)`` so a database provisioned in a
    non-default region picks up that region's network; a blank field (or the default
    region) falls back to the flat config keys, so single-region installs are
    unchanged. An explicit value passed in ``opts`` always wins.
    """
    if (engine, cloud) == ("postgres", "aws"):
        return {
            "region": region,
            "identifier": f"clouddb-{db_id[:8]}",
            "db_name": db_name,
            "master_username": master_username,
            "master_password": master_password,
            "instance_class": opts.get("instance_class", "db.t3.micro"),
            "allocated_storage": opts.get("allocated_storage", 20),
            "db_subnet_group_name": opts.get("db_subnet_group_name")
                or resolve_region("aws", region)["db_subnet_group_name"],
            "vpc_security_group_ids": opts.get("vpc_security_group_ids", []),
            # Attach the force_ssl=0 parameter group the sandbox pre-created, so the
            # PRA protocol tunnel's cleartext jumpoint→RDS connection isn't rejected.
            # Empty config → "" → module falls back to the RDS default group.
            "parameter_group_name": _cfg("aws_db_parameter_group_name"),
            "tags": {"managed-by": "vm-dashboard", "clouddb-id": db_id},
        }

    if (engine, cloud) == ("mysql", "aws"):
        return {
            "region": region,
            "identifier": f"clouddb-{db_id[:8]}",
            "db_name": db_name,
            "master_username": master_username,
            "master_password": master_password,
            "instance_class": opts.get("instance_class", "db.t3.micro"),
            "allocated_storage": opts.get("allocated_storage", 20),
            "db_subnet_group_name": opts.get("db_subnet_group_name")
                or resolve_region("aws", region)["db_subnet_group_name"],
            "vpc_security_group_ids": opts.get("vpc_security_group_ids", []),
            # MySQL's cleartext knob is require_secure_transport=0 (not
            # rds.force_ssl) — its own mysql8.0-family group the sandbox
            # pre-creates. Empty config → "" → module falls back to RDS default.
            "parameter_group_name": _cfg("aws_db_mysql_parameter_group_name"),
            "tags": {"managed-by": "vm-dashboard", "clouddb-id": db_id},
        }

    if (engine, cloud) == ("sqlserver", "aws"):
        # RDS SQL Server (sqlserver-ex). Mirrors mysql/aws but OMITS db_name — RDS for
        # SQL Server rejects it at creation; you connect to the `master` system DB
        # instead (the tunnel targets master for sqlserver). No no-SSL parameter group
        # is needed: RDS SQL Server's rds.force_ssl defaults to optional and the PRA
        # mssql tunnel is TDS-aware (handles encryption itself), so RDS's default group
        # is fine. db.t3.small min (SQL Server needs >=2 GiB; t3.micro is too small).
        # The AWS form defaults instance_class to db.t3.micro (1 GiB) — too small for SQL
        # Server (needs >=2 GiB; micro is unsupported for sqlserver-ex) — so bump any
        # *.micro class up to db.t3.small.
        sqlserver_class = opts.get("instance_class") or "db.t3.small"
        if sqlserver_class.endswith(".micro"):
            sqlserver_class = "db.t3.small"
        return {
            "region": region,
            "identifier": f"clouddb-{db_id[:8]}",
            "master_username": master_username,
            "master_password": master_password,
            "instance_class": sqlserver_class,
            "allocated_storage": opts.get("allocated_storage", 20),
            "db_subnet_group_name": opts.get("db_subnet_group_name")
                or resolve_region("aws", region)["db_subnet_group_name"],
            "vpc_security_group_ids": opts.get("vpc_security_group_ids", []),
            "tags": {"managed-by": "vm-dashboard", "clouddb-id": db_id},
        }

    if (engine, cloud) == ("postgres", "gcp"):
        # The private_network the instance gets its private IP on; the sandbox
        # configures private-services-access on it. ssl_mode defaults inside the
        # module to ALLOW_UNENCRYPTED_AND_ENCRYPTED so the PRA tunnel's cleartext
        # jumpoint→DB connection is accepted (mirrors AWS's force_ssl=0).
        return {
            "project": _cfg("gcp_project") or _cfg("gcp_project_id"),
            "region": region,
            "identifier": f"clouddb-{db_id[:8]}",
            "db_name": db_name,
            "master_username": master_username,
            "master_password": master_password,
            "tier": opts.get("tier", "db-f1-micro"),
            "disk_size": opts.get("disk_size", 20),
            "private_network": opts.get("private_network") or resolve_region("gcp", region)["db_network"],
            "labels": {"managed-by": "vm-dashboard", "clouddb-id": db_id},
        }

    if (engine, cloud) == ("mysql", "gcp"):
        # Cloud SQL MySQL — identical wiring to postgres/gcp (same private_network +
        # ssl_mode knobs). The module pins database_version=MYSQL_8_4 so the admin is
        # created on caching_sha2_password (Cloud SQL MySQL 8.0 uses mysql_native_password,
        # which the PRA tunnel rejects) and edition=ENTERPRISE to keep db-f1-micro on 8.4.
        return {
            "project": _cfg("gcp_project") or _cfg("gcp_project_id"),
            "region": region,
            "identifier": f"clouddb-{db_id[:8]}",
            "db_name": db_name,
            "master_username": master_username,
            "master_password": master_password,
            "tier": opts.get("tier", "db-f1-micro"),
            "disk_size": opts.get("disk_size", 20),
            "private_network": opts.get("private_network") or resolve_region("gcp", region)["db_network"],
            "labels": {"managed-by": "vm-dashboard", "clouddb-id": db_id},
        }

    if (engine, cloud) == ("sqlserver", "gcp"):
        # Cloud SQL SQL Server. Mirrors postgres/gcp, but the admin login is the built-in
        # `sqlserver` account (set via the module's root_password) — force master_username
        # to "sqlserver" (Cloud SQL ignores any other name). SQL Server needs a db-custom-*
        # tier (no shared-core); the module defaults database_version=SQLSERVER_2022_STANDARD.
        # (The tunnel targets `master`, set in _broker_tunnel.)
        # The GCP form's tier picker defaults to db-f1-micro (shared-core), which Cloud SQL
        # rejects for SQL Server ("requires a custom machine type") — coerce any non-db-custom
        # tier to a db-custom one.
        sqlserver_tier = opts.get("tier") or ""
        if not sqlserver_tier.startswith("db-custom"):
            sqlserver_tier = "db-custom-2-7680"
        return {
            "project": _cfg("gcp_project") or _cfg("gcp_project_id"),
            "region": region,
            "identifier": f"clouddb-{db_id[:8]}",
            "db_name": db_name,
            "master_username": "sqlserver",
            "master_password": master_password,
            "tier": sqlserver_tier,
            "disk_size": opts.get("disk_size", 20),
            "private_network": opts.get("private_network") or resolve_region("gcp", region)["db_network"],
            "labels": {"managed-by": "vm-dashboard", "clouddb-id": db_id},
        }

    if (engine, cloud) == ("postgres", "azure"):
        # VNet-integrated private Flexible Server. The delegated subnet + private
        # DNS zone are sandbox-created; the module references them. require_secure_
        # transport=OFF (set in the module) is the force_ssl=0 analog for the tunnel.
        _az = resolve_region("azure", region)
        return {
            "resource_group_name": opts.get("resource_group_name") or _az["resource_group"],
            "location": region,
            "identifier": f"clouddb-{db_id[:8]}",
            "administrator_login": master_username,
            "administrator_password": master_password,
            "sku_name": opts.get("sku_name", "B_Standard_B1ms"),
            "storage_mb": opts.get("storage_mb", 32768),
            "db_name": db_name,
            "delegated_subnet_id": opts.get("delegated_subnet_id") or _az["db_subnet_id"],
            "private_dns_zone_id": opts.get("private_dns_zone_id") or _az["db_private_dns_zone_id"],
            "tags": {"managed-by": "vm-dashboard", "clouddb-id": db_id},
        }

    if (engine, cloud) == ("mysql", "azure"):
        # VNet-integrated private MySQL Flexible Server. Mirrors postgres/azure but
        # reads the MySQL-specific delegated subnet + DNS zone (a delegated subnet
        # hosts only one flexible-server type, so MySQL needs its own). The module's
        # require_secure_transport=OFF is the cleartext-tunnel knob; MySQL 8.0's admin
        # defaults to caching_sha2_password, which the PRA tunnel needs.
        _az = resolve_region("azure", region)
        return {
            "resource_group_name": opts.get("resource_group_name") or _az["resource_group"],
            "location": region,
            "identifier": f"clouddb-{db_id[:8]}",
            "administrator_login": master_username,
            "administrator_password": master_password,
            "sku_name": opts.get("sku_name", "B_Standard_B1ms"),
            "storage_mb": opts.get("storage_mb", 32768),
            "db_name": db_name,
            "delegated_subnet_id": opts.get("delegated_subnet_id") or _az["db_mysql_subnet_id"],
            # MySQL has its own DNS zone flat key (not a region-config field) — unchanged.
            "private_dns_zone_id": opts.get("private_dns_zone_id") or _cfg("azure_db_mysql_private_dns_zone_id"),
            "tags": {"managed-by": "vm-dashboard", "clouddb-id": db_id},
        }

    if (engine, cloud) == ("sqlserver", "azure"):
        # Azure SQL Database + Private Endpoint (no flexible-server analog for SQL).
        # Same azure credential shape (administrator_login/password) but reads the
        # SQL-Server-specific PE subnet + privatelink.database.windows.net DNS zone the
        # sandbox creates. The module forces public_network_access_enabled=false; Azure
        # SQL's forced TLS is fine because the mssql tunnel does backend TLS itself.
        # (The tunnel targets `master`, set in _broker_tunnel.)
        # The Azure form's SKU picker offers Flexible-Server SKUs (B_Standard_*, GP_Standard_*),
        # which are invalid for azurerm_mssql_database (Azure SQL DB wants Basic / S0 / P1 /
        # GP_S_Gen5_1 / …). Coerce any Flexible-Server SKU to Basic; honor a real SQL-DB SKU.
        sqlserver_sku = opts.get("sku_name") or "Basic"
        if "_Standard_" in sqlserver_sku:
            sqlserver_sku = "Basic"
        return {
            "resource_group_name": opts.get("resource_group_name") or resolve_region("azure", region)["resource_group"],
            "location": region,
            "identifier": f"clouddb-{db_id[:8]}",
            "administrator_login": master_username,
            "administrator_password": master_password,
            "sku_name": sqlserver_sku,
            "db_name": db_name,
            # SQL Server has its own PE subnet + DNS zone flat keys (not region-config
            # fields) — unchanged.
            "subnet_id": opts.get("subnet_id") or _cfg("azure_db_sqlserver_subnet_id"),
            "private_dns_zone_id": opts.get("private_dns_zone_id") or _cfg("azure_db_sqlserver_private_dns_zone_id"),
            "tags": {"managed-by": "vm-dashboard", "clouddb-id": db_id},
        }

    if (engine, cloud) == ("oracle", "oci"):
        # OCI Autonomous Database (ATP/ADW). Free-tier (default) is a PUBLIC
        # endpoint reached over the PRA tcp tunnel from the public-subnet jumpoint
        # (Always-Free ADB can't sit in a VCN); a private endpoint needs is_free_tier
        # false + a subnet. The admin login is always ADMIN; only the password is a
        # variable (mapped from the minted master_password). db_name is ADB-shaped
        # (<=14 alnum, letter-led) — distinct from the generic db_name arg.
        is_free = bool(opts.get("oci_is_free_tier", True))
        return {
            "compartment_ocid": opts.get("oci_compartment_ocid") or _cfg("oci_compartment_ocid") or _cfg("oci_tenancy_ocid"),
            "identifier": f"clouddb-{db_id[:8]}",
            "db_name": _oracle_db_name(db_id),
            "admin_password": master_password,
            "db_workload": (opts.get("oci_db_workload") or "OLTP").upper(),
            "is_free_tier": is_free,
            "cpu_core_count": int(opts.get("oci_cpu_core_count") or 1),
            "data_storage_size_in_tbs": int(opts.get("oci_data_storage_tbs") or 1),
            # Private endpoint only when explicitly paid + a subnet is given.
            "subnet_ocid": ("" if is_free else (opts.get("oci_subnet_ocid") or _cfg("oci_default_subnet_ocid") or "")),
            "is_mtls_connection_required": False,
            "freeform_tags": {"managed-by": "vm-dashboard", "clouddb-id": db_id},
        }

    raise NotImplementedError(f"{engine}/{cloud} Terraform variables not implemented")


def provision(
    db: Session, *, engine: str, cloud: str, region: str, name: str,
    created_by: str, master_username: str = "dbadmin",
    vault_account_group_id: Optional[int] = None,
    jump_group: Optional[str] = None, jumpoint_name: Optional[str] = None,
    pra_credential_ref: Optional[str] = None,
    register_in_entitle: bool = False, **opts,
) -> dict:
    """Record a new managed database: validate, mint the admin credential, write
    the ``CloudDatabase`` row + a provisioning ``Job``, and return the Terraform
    variables the apply will use. Does **not** run Terraform — the API schedules
    :func:`run_provision_apply`. Returns ``{ok, db_id, job_id, tf_variables}``.
    """
    if engine not in VALID_ENGINES:
        raise CloudDatabaseError(f"unknown engine {engine!r} (expected one of {sorted(VALID_ENGINES)})")
    if cloud not in VALID_CLOUDS:
        raise CloudDatabaseError(f"unknown cloud {cloud!r} (expected one of {sorted(VALID_CLOUDS)})")
    if not region:
        raise CloudDatabaseError("region is required")
    if (engine, cloud) not in _IMPLEMENTED:
        raise NotImplementedError(
            f"{engine} on {cloud} is not available yet"
        )

    row = CloudDatabase(
        engine=engine,
        provider=_PROVIDER.get((engine, cloud)),
        cloud=cloud,
        region=region,
        port=_DEFAULT_PORTS.get(engine),
        status="provisioning",
        created_by=created_by,
        created_at=datetime.utcnow(),
        jump_group=(jump_group or "").strip() or None,
        jumpoint_name=(jumpoint_name or "").strip() or None,
        pra_credential_ref=(pra_credential_ref or "").strip() or None,
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    # Mint the admin master credential and stash it via the encrypted config
    # store — never returned in plaintext after this point. OCI Autonomous DB
    # rejects the default token_urlsafe(24) (32 chars > ADB's 30-char cap, and no
    # guaranteed upper/lower/digit mix), so use the complexity generator there.
    if engine == "oracle":
        from . import cloud_db_sql_service as _sql
        master_password = _sql.generate_password(24)  # 24 chars, guaranteed upper/lower/digit/symbol
    else:
        master_password = secrets.token_urlsafe(24)
    config_service.set(f"clouddb/{row.id}/admin", master_password)
    row.credentials_ref = f"config://clouddb/{row.id}/admin"
    db.commit()

    job_meta = {"db_id": row.id, "engine": engine, "cloud": cloud, "name": name,
                "register_in_entitle": bool(register_in_entitle)}
    if vault_account_group_id:
        # Carried via job metadata (not tf_variables — those map 1:1 to the
        # cloud module's declared variables) for _broker_tunnel to pick up.
        job_meta["vault_account_group_id"] = int(vault_account_group_id)

    # Build the -var set BEFORE creating the job so its (secret-stripped) copy can
    # be embedded in the job metadata atomically. The apply runs in a separate
    # process (the dedicated job runner) that polls for pending jobs and dispatches
    # them reading meta["tf_variables"]. If the job were committed without
    # tf_variables and patched in by a follow-up call, the runner could claim it in
    # that gap and dispatch with no tf_variables → KeyError('tf_variables'). The
    # master password is NEVER persisted to jobs.extra_data — run_provision_apply
    # re-injects it from the secrets backend before the apply / tunnel read it back.
    tf_variables = _build_tf_variables(
        engine=engine, cloud=cloud, region=region, db_id=row.id,
        db_name=_db_name_from(name), master_username=master_username,
        master_password=master_password, opts=opts,
    )
    job_meta["tf_variables"] = {k: v for k, v in tf_variables.items()
                                if k not in _SECRET_TF_KEYS}
    job = job_service.create_job(
        db, job_type="clouddb_provision", created_by=created_by,
        metadata=job_meta,
    )

    logger.info("clouddb provisioned record db_id=%s engine=%s cloud=%s job_id=%s",
                row.id, engine, cloud, job.id)
    return {"ok": True, "db_id": row.id, "job_id": job.id, "tf_variables": tf_variables}


def _cfg(key: str) -> str:
    val = config_service.get(key)
    if val:
        return val
    return getattr(settings, key, "") or ""


# Provider credentials for the terraform subprocess moved to the shared
# services/terraform_provider_env module (reused by k8s_service); call sites use
# terraform_provider_env.provider_env(cloud).


def _pra_configured() -> bool:
    """True when a PRA/SRA appliance + Jumpoint + Jump Group are configured —
    the prerequisites for brokering a tunnel. When false, a DB is still
    provisioned/recorded; it just isn't reachable until PRA is set up."""
    return all(_cfg(k) for k in ("bt_api_host", "bt_jumpoint_name", "bt_jump_group_name"))


def _pscli_configured() -> bool:
    """True when the Password Safe OAuth client (shared by ps-cli and the
    ps_api_service REST calls) is configured — the gate for staging the DB
    admin credential as a functional account + Secrets Safe secret."""
    return all(_cfg(k) for k in ("pscli_api_url", "pscli_client_id", "pscli_client_secret"))


async def _resolve_ecs_deploy_key() -> str:
    """BeyondTrust Jumpoint Docker deploy key — same resolution as the EC2
    deploy flow (api/aws.py:_resolve_aws_ecs_deploy_key): direct config field
    first, then the legacy Password-Safe title. Empty when neither is set."""
    direct = _cfg("aws_ecs_docker_deploy_key")
    if direct:
        return direct
    title = _cfg("bt_ps_deploy_key_title")
    if title:
        from . import btapi_service
        try:
            return await btapi_service.get_ps_secret(title)
        except Exception as exc:
            logger.warning("clouddb: deploy key fetch from Password Safe failed: %s", exc)
    return ""


async def _ensure_jumpoint_node(region: str) -> None:
    """Make sure the ECS-hosted Jumpoint (the PRA gateway) has at least one
    live node before brokering a tunnel. The tunnel jump item can be created
    with zero nodes online, but it shows 'Unavailable' in PRA until a node
    registers — so check the Jumpoint cluster for a running task and start one
    (the same launch the EC2 deploy flow does) only when there is none.
    Non-fatal throughout, like the EC2 path."""
    from . import aws_service
    cluster = _cfg("bt_ecs_cluster")
    family = _cfg("bt_ecs_task_family")
    try:
        tasks = await aws_service.list_ecs_tasks(region, cluster)
    except Exception as exc:
        logger.warning("clouddb: could not list Jumpoint ECS tasks (%s) — skipping node check", exc)
        return
    live = [t for t in tasks
            if t.get("lastStatus") in ("PROVISIONING", "PENDING", "RUNNING")
            and f"task-definition/{family}:" in (t.get("taskDefinitionArn") or "")]
    if live:
        logger.info("clouddb: Jumpoint node already up (%d live task(s) in cluster %s)",
                    len(live), cluster)
        return

    launch_type = (_cfg("bt_ecs_launch_type") or "EC2").upper()
    subnet_id = _cfg("bt_ecs_jumpoint_subnet_id")
    sg_ids = [s.strip() for s in _cfg("bt_ecs_jumpoint_security_group_id").split(",") if s.strip()]
    deploy_key = await _resolve_ecs_deploy_key()
    # EC2/host-networking takes its network from the container instance (the
    # sandbox provisions it), so only the deploy key is required here; the
    # legacy FARGATE path still needs a task subnet + SG.
    missing_net = (launch_type != "EC2") and not (subnet_id and sg_ids)
    if not deploy_key or missing_net:
        need = "aws_ecs_docker_deploy_key"
        if missing_net:
            need += ", bt_ecs_jumpoint_subnet_id, bt_ecs_jumpoint_security_group_id"
        logger.warning(
            "clouddb: no live Jumpoint node and cannot auto-start one — set %s. "
            "The tunnel will show Unavailable in PRA until a Jumpoint node is online.",
            need)
        return
    try:
        arn = await aws_service.run_ecs_jumpoint_task(
            region=region,
            cluster=cluster,
            task_family=family,
            subnet_id=subnet_id,
            security_group_ids=sg_ids,
            deploy_key=deploy_key,
            cpu=_cfg("bt_ecs_cpu"),
            memory=_cfg("bt_ecs_memory"),
            execution_role_arn=_cfg("bt_ecs_execution_role_arn"),
            image=_cfg("bt_ecs_image"),
            launch_type=launch_type,
        )
        logger.info("clouddb: started Jumpoint ECS node %s (launch_type=%s) — "
                    "registers with PRA in ~1-2 min", arn.split("/")[-1], launch_type)
    except Exception as exc:
        logger.warning("clouddb: Jumpoint ECS node launch failed (non-fatal): %s", exc)


async def _broker_tunnel(db: Session, *, row: CloudDatabase, job_id: str,
                         engine: str, tf_variables: dict,
                         override_cred: Optional[tuple] = None) -> None:
    """Phase 2: provision a PRA protocol-tunnel jump to the private DB via the
    beyondtrust/sra provider, record ``jump_item_id`` on the row, and stash the
    tunnel's Terraform state in the provisioning job's metadata for teardown.
    Non-fatal: a failure leaves the DB up with no tunnel (retryable).

    ``override_cred`` — when the Password Safe onboarding is active it passes
    ``(managed_user, managed_password)`` so the injected/vaulted credential is the
    dedicated managed DB user (the rotation target) rather than the master admin."""
    from . import terraform_pra_service as pra
    # The shared Jumpoint host was ensured at the start of run_provision_apply
    # (so its ~2-min boot overlaps the RDS apply); ensure again here — idempotent
    # and cheap when the host is already up — so the task is running before we
    # broker the tunnel.
    from . import jumpoint_host_service
    try:
        await jumpoint_host_service.ensure_jumpoint_host(row.cloud, _cfg(row.cloud + "_region") or row.region)
    except Exception as exc:
        logger.warning("clouddb: ensure jumpoint host (broker) failed (non-fatal): %s", exc)
    try:
        jump_name = tf_variables.get("identifier") or f"clouddb-{row.id[:8]}"
        job = db.query(Job).filter(Job.id == job_id).first()
        vault_group_id = ((job.metadata_dict or {}).get("vault_account_group_id")
                          if job is not None else None)
        # Per-DB PRA overrides win over the configured defaults.
        cred_ref = row.pra_credential_ref
        client_secret = config_service.resolve_reference(cred_ref) if cred_ref else ""
        # The admin credential's variable name differs per cloud (aws/gcp use
        # master_username/master_password; azure's Flexible Server module uses
        # administrator_login/administrator_password). Normalize so the Vault
        # account is minted for every cloud — otherwise on Azure both resolve
        # empty, want_vault is False, and the tunnel comes up with no credential
        # to inject (no warning, since the vault account is never attempted).
        admin_username = (tf_variables.get("master_username")
                          or tf_variables.get("administrator_login")
                          or ("ADMIN" if engine == "oracle" else ""))
        admin_password = (tf_variables.get("master_password")
                          or tf_variables.get("administrator_password")
                          or tf_variables.get("admin_password") or "")
        if override_cred:
            admin_username, admin_password = override_cred
        vault_account_name = f"{jump_name}-admin"
        tun = await pra.provision_db_tunnel(
            engine=engine,
            name=jump_name,
            hostname=row.private_host,
            jump_group_name=row.jump_group or _cfg("bt_jump_group_name"),
            jumpoint_name=row.jumpoint_name or _cfg("bt_jumpoint_name"),
            client_secret=client_secret,
            username=admin_username,
            database=("master" if engine == "sqlserver" else tf_variables.get("db_name", "")),
            tag="clouddb",
            # Vault account for credential injection at tunnel launch; rides in
            # the same workspace/state so decommission destroys it too. The
            # account group makes it visible to users via group policies.
            admin_password=admin_password,
            vault_account_name=vault_account_name,
            vault_account_group_id=vault_group_id,
        )
        row.jump_item_id = tun.get("tunnel_jump_id") or None
        db.commit()
        job = db.query(Job).filter(Job.id == job_id).first()
        if job is not None:
            meta = job.metadata_dict or {}
            meta["tunnel_tf_state"] = tun.get("tf_state_json")   # scrubbed of secrets
            meta["vault_account_id"] = tun.get("vault_account_id")
            meta["vault_account_name"] = vault_account_name      # pravault managed-account name
            job.metadata_dict = meta
            db.commit()
        logger.info("clouddb tunnel brokered db_id=%s jump_item_id=%s vault_account_id=%s",
                    row.id, row.jump_item_id, tun.get("vault_account_id"))
    except Exception as exc:
        logger.warning("clouddb tunnel brokering failed db_id=%s (DB is up, no tunnel): %s",
                       row.id, exc)


def _registration_enabled() -> bool:
    """Global Entitle-registration capability flag (per-build choice is separate)."""
    return config_service.get_bool("entitle_registration_enabled", False)


# Actions accepted by the post-provision entitle-register endpoint / job.
VALID_ENTITLE_DB_ACTIONS = ("register", "deregister")


def _provision_job_for(db: Session, db_id: str) -> Optional[Job]:
    """The most recent ``clouddb_provision`` Job for this DB — where a DB's mutable
    operational metadata lives (``tf_variables`` minus secrets, the tunnel/Entitle
    TF state). Mirrors the lookup :func:`run_decommission` uses so registration
    state is stashed exactly where teardown reads it."""
    jobs = (db.query(Job)
              .filter(Job.job_type == "clouddb_provision")
              .order_by(Job.created_at.desc()).all())
    return next((j for j in jobs if (j.metadata_dict or {}).get("db_id") == db_id), None)


async def _entitle_register_core(db: Session, *, row: CloudDatabase, engine: str,
                                 tf_variables: Optional[dict] = None) -> None:
    """Register the managed DB as an Entitle integration (PostgreSQL / MySQL /
    SQL Server) so users can request JIT access. Records ``entitle_integration_id``
    on the row and stashes the registration's Terraform state in the DB's
    **provisioning-job** metadata — where :func:`run_decommission` reads
    ``entitle_registration_tf_state`` for teardown, regardless of which job
    triggered the registration. Private (PRA-only) DB → attaches the shared
    Entitle agent. **Raises** on failure (the caller decides whether that's fatal).

    ``tf_variables`` is supplied on the provision path (password already re-injected);
    the post-hoc path passes ``None`` and we reconstruct the admin credential from
    the provisioning job metadata + the encrypted config store."""
    from . import entitle_registration_service as ent
    prov_job = _provision_job_for(db, row.id)
    tfv = tf_variables if tf_variables is not None else \
        ((prov_job.metadata_dict or {}).get("tf_variables") if prov_job else None) or {}

    # Per-cloud admin credential key normalization (mirrors _broker_tunnel). The
    # password is never in job metadata (scrubbed) — fall back to the config store
    # (still present until a clean decommission). Cloud SQL SQL Server forces the
    # 'sqlserver' admin login; everything else defaults to 'dbadmin'.
    default_user = ("ADMIN" if engine == "oracle"
                    else "sqlserver" if (engine == "sqlserver" and row.cloud == "gcp")
                    else "dbadmin")
    admin_username = (tfv.get("master_username")
                      or tfv.get("administrator_login") or default_user)
    admin_password = (tfv.get("master_password")
                      or tfv.get("administrator_password")
                      or tfv.get("admin_password")
                      or config_service.get(f"clouddb/{row.id}/admin") or "")
    if not admin_password:
        raise CloudDatabaseError(
            f"no admin credential available for db_id={row.id} "
            f"(provisioning job pruned?) — cannot register in Entitle")

    # Entitle's Microsoft SQL Server connector requires a `version` field — its
    # connection schema lists version/user/password/server/database as mandatory,
    # so omitting it fails schema matching with API 400 "Didn't find matching
    # connection schema" (the same class of failure the postgres `username`/
    # `database` bug caused). Entitle documents 2017/2019; default to "2019", which
    # is compatible with the SQL Server 2022 Cloud SQL provisions for the login/role
    # DDL the connector runs. Override per-tenant via the `entitle_sqlserver_version`
    # config key. Postgres/MySQL don't take a version on this path (postgres has no
    # version field; MySQL registration isn't a current target — see note below).
    version = ""
    if engine == "sqlserver":
        version = _cfg("entitle_sqlserver_version") or "2019"

    # GCP Cloud SQL's private IP is unreachable from the Entitle agent's own GKE VPC
    # (non-transitive peering). Stand up an on-demand socat forwarder in the sandbox
    # VPC and point Entitle at it. Returns None (no override) for non-GCP DBs or when
    # gcp_entitle_db_proxy_enabled is off; AWS RDS is reachable directly. Raises on a
    # hard failure so a post-hoc register job fails clearly (the provision-path
    # wrapper swallows it).
    from . import entitle_db_proxy_service
    reg_host, reg_port = row.private_host, row.port or 0
    fwd = await entitle_db_proxy_service.ensure_db_forwarder(db, row)
    if fwd:
        reg_host, reg_port = fwd

    result = await ent.register_database(
        engine=engine,
        name=tfv.get("identifier") or f"clouddb-{row.id[:8]}",
        host=reg_host,
        port=reg_port,
        username=admin_username,
        password=admin_password,
        database=("master" if engine == "sqlserver" else tfv.get("db_name", "")),
        version=version,
        private=True,   # dashboard-built DBs are private (publicly_accessible=false)
        tag="clouddb",
    )
    row.entitle_integration_id = result.get("integration_id") or None
    db.commit()
    if prov_job is not None:
        j = db.query(Job).filter(Job.id == prov_job.id).first()
        if j is not None:
            meta = j.metadata_dict or {}
            meta["entitle_registration_tf_state"] = result.get("tf_state_json")
            j.metadata_dict = meta
            db.commit()
    logger.info("clouddb registered in Entitle db_id=%s integration_id=%s",
                row.id, row.entitle_integration_id)


async def _register_entitle(db: Session, *, row: CloudDatabase, engine: str,
                            tf_variables: Optional[dict] = None) -> None:
    """Non-fatal wrapper used on the provision path: register the DB in Entitle but
    never let a registration failure fail the provision (the DB is up regardless)."""
    try:
        await _entitle_register_core(db, row=row, engine=engine, tf_variables=tf_variables)
    except Exception as exc:
        logger.warning("clouddb Entitle registration failed db_id=%s (DB is up): %s",
                       row.id, exc)


async def _deregister_entitle_core(db: Session, *, row: CloudDatabase) -> None:
    """Destroy the DB's Entitle integration using the state stashed on its
    provisioning job, then clear ``entitle_integration_id`` + the state key.
    **Raises** on a real destroy failure."""
    from . import entitle_registration_service as ent, entitle_db_proxy_service
    prov_job = _provision_job_for(db, row.id)
    ent_state = ((prov_job.metadata_dict or {}).get("entitle_registration_tf_state")
                 if prov_job else None)
    if ent_state:
        await ent.deregister(ent_state)   # raises on a real failure — surfaced by the caller
        row.entitle_integration_id = None
        db.commit()
        j = db.query(Job).filter(Job.id == prov_job.id).first()
        if j is not None:
            meta = j.metadata_dict or {}
            meta.pop("entitle_registration_tf_state", None)
            j.metadata_dict = meta
            db.commit()
        logger.info("clouddb Entitle integration deregistered db_id=%s", row.id)
    else:
        # Nothing recorded to destroy — just clear any stale id so the UI recovers.
        if row.entitle_integration_id:
            row.entitle_integration_id = None
            db.commit()
        logger.info("clouddb Entitle deregister: no stored state for db_id=%s "
                    "(nothing to destroy)", row.id)
    # Tear down the on-demand GCP reachability forwarder (best-effort; no-op for
    # non-GCP DBs or when none was created). Only reached after a successful
    # deregister above, so a failed destroy leaves the forwarder for retry.
    await entitle_db_proxy_service.teardown_db_forwarder(db, row)


async def run_entitle_register(db: Session, *, db_id: str, job_id: str,
                               action: str = "register") -> None:
    """Worker entry for a ``clouddb_entitle_register`` job: register or deregister
    the DB as an Entitle integration with Job tracking. Mirrors
    ``k8s_service.run_entitle_register``. Marks the job failed on error (unlike the
    provision path's non-fatal wrapper, a post-hoc request should surface failures)."""
    row = db.query(CloudDatabase).filter(CloudDatabase.id == db_id).first()
    if not row:
        job_service.set_failed(db, job_id, f"cloud database {db_id} not found")
        return
    job_service.set_running(db, job_id)
    try:
        if action == "deregister":
            job_service.update_progress(db, job_id, 30, "Removing Entitle integration…")
            await _deregister_entitle_core(db, row=row)
        else:
            if not _registration_enabled():
                raise CloudDatabaseError(
                    "Entitle registration is disabled (set entitle_registration_enabled)")
            if not row.private_host:
                raise CloudDatabaseError(
                    "database has no private host yet — wait for provisioning to finish")
            job_service.update_progress(db, job_id, 30, "Registering database in Entitle…")
            await _entitle_register_core(db, row=row, engine=row.engine, tf_variables=None)
            if not row.entitle_integration_id:
                raise CloudDatabaseError("Entitle registration returned no integration id")
        job_service.set_completed(db, job_id, {
            "db_id": db_id, "action": action,
            "entitle_integration_id": row.entitle_integration_id,
        })
        logger.info("clouddb entitle %s complete db_id=%s integration_id=%s",
                    action, db_id, row.entitle_integration_id)
    except Exception as exc:
        job_service.set_failed(db, job_id, str(exc))
        logger.exception("clouddb entitle %s job failed db_id=%s: %s", action, db_id, exc)


async def _store_ps_credentials(db: Session, *, row: CloudDatabase, job_id: str,
                                tf_variables: dict) -> None:
    """Stage the admin credential in BeyondTrust Password Safe:

    1. A FUNCTIONAL ACCOUNT — the privileged account a future Ansible playbook
       will hand to Password Safe when it onboards this DB as a managed system
       and creates a managed account on it.
    2. A Secrets Safe TEXT SECRET holding the connection document — what that
       playbook (and humans) read for the actual account-creation step.

    Both best-effort, independent of each other and of PRA; ids/refs land in
    the provisioning job's metadata so decommission can retire them."""
    if not _pscli_configured():
        logger.info("clouddb: Password Safe (pscli_*) not configured — skipping "
                    "functional-account + Secrets Safe staging for db_id=%s", row.id)
        return
    name = f"{tf_variables.get('identifier') or f'clouddb-{row.id[:8]}'}-admin"
    stash: dict = {}
    # Per-cloud credential key normalization (see _broker_tunnel): aws/gcp use
    # master_*, azure's Flexible Server module uses administrator_*.
    admin_username = (tf_variables.get("master_username")
                      or tf_variables.get("administrator_login")
                      or ("ADMIN" if row.engine == "oracle" else "dbadmin"))
    admin_password = (tf_variables.get("master_password")
                      or tf_variables.get("administrator_password")
                      or tf_variables.get("admin_password") or "")

    try:
        from . import ps_api_service
        fa_id = await ps_api_service.create_functional_account(
            engine=row.engine,
            account_name=admin_username,
            display_name=name,
            password=admin_password,
            description=(
                f"Admin credential for dashboard-provisioned database "
                f"{tf_variables.get('identifier', '')} (db_id={row.id}); used as the "
                f"functional account when the DB is onboarded as a PS managed system."
            ),
        )
        stash["ps_functional_account_id"] = fa_id
        stash["ps_functional_account_name"] = name
        logger.info("clouddb: Password Safe functional account %r created (id=%s) db_id=%s",
                    name, fa_id, row.id)
    except Exception as exc:
        logger.warning("clouddb: functional-account creation failed db_id=%s (non-fatal): %s",
                       row.id, exc)

    try:
        from . import secrets_backend_service
        secret_doc = json.dumps({
            "engine": row.engine,
            "host": row.private_host,
            "port": row.port,
            "database": tf_variables.get("db_name", ""),
            "username": admin_username,
            "password": admin_password,
        })
        ref = await asyncio.to_thread(
            secrets_backend_service.write_bt_secrets_safe, name, secret_doc)
        stash["bt_secret_ref"] = ref
        logger.info("clouddb: Secrets Safe secret stored at %r db_id=%s", ref, row.id)
    except Exception as exc:
        logger.warning("clouddb: Secrets Safe write failed db_id=%s (non-fatal): %s",
                       row.id, exc)

    if stash:
        job = db.query(Job).filter(Job.id == job_id).first()
        if job is not None:
            meta = job.metadata_dict or {}
            meta.update(stash)
            job.metadata_dict = meta
            db.commit()


# ── Optional Password Safe DB onboarding (AWS-only, opt-in) ───────────────────

def _ps_db_onboarding_enabled(row: CloudDatabase) -> bool:
    """Gate for the full Password Safe DB onboarding: AWS-only, the Password Safe
    OAuth client configured, and the operator opt-in flag set. When off, the DB
    still provisions and the legacy admin-credential staging runs instead."""
    return (row.cloud == "aws" and _pscli_configured()
            and config_service.get_bool("clouddb_ps_onboarding_enabled", False))


def _managed_user_name(db_id: str) -> str:
    """A safe, per-database DB identifier for the dedicated managed user
    (letter-led, ``[A-Za-z0-9_]`` — see cloud_db_sql_service._IDENT_RE)."""
    return f"psafe_{db_id.replace('-', '')[:12]}"


async def _create_db_managed_user(db: Session, *, row: CloudDatabase, job_id: str,
                                  engine: str, tf_variables: dict) -> dict:
    """Create the dedicated managed DB user from the admin credential by running
    the DB client on the shared Jumpoint host over AWS SSM. Returns the onboarding
    context (managed user + password, jump host id, region, db name, admin user,
    client image). Raises on failure so the caller falls back to admin staging."""
    from . import aws_service, jumpoint_host_service
    from . import cloud_db_sql_service as sql
    region = _cfg(row.cloud + "_region") or row.region
    host_id = await jumpoint_host_service.ensure_jumpoint_host(row.cloud, region)
    if not host_id:
        raise CloudDatabaseError(
            "no SSM jump host available — the shared Jumpoint host must be up to run "
            "the DB client (check aws_ecs_docker_deploy_key + jumpoint config)")
    admin_username = (tf_variables.get("master_username")
                      or tf_variables.get("administrator_login") or "dbadmin")
    admin_password = (config_service.get(f"clouddb/{row.id}/admin")
                      or tf_variables.get("master_password") or "")
    db_name = "master" if engine == "sqlserver" else tf_variables.get("db_name", "")
    managed_user = _managed_user_name(row.id)
    managed_pw = sql.generate_password()
    image = _cfg(f"clouddb_db_client_image_{engine}") or sql.default_client_image(engine)
    port = row.port or sql.default_port(engine)
    cmds = sql.onboard_commands(
        engine, host=row.private_host, port=port,
        database=db_name, admin_user=admin_username, admin_password=admin_password,
        managed_user=managed_user, managed_password=managed_pw, client_image=image)
    result = await aws_service.ssm_send_command(region, host_id, cmds, timeout=300)
    if result.get("status") != "Success" or int(result.get("response_code", -1)) != 0:
        detail = (result.get("stderr") or result.get("stdout") or "")[:400]
        raise CloudDatabaseError(
            f"managed-user creation on the jump host failed "
            f"(status={result.get('status')}, rc={result.get('response_code')}): {detail}")
    logger.info("clouddb: managed DB user %r created via SSM on %s db_id=%s",
                managed_user, host_id, row.id)
    return {"managed_user": managed_user, "managed_pw": managed_pw, "jump_host_id": host_id,
            "region": region, "db_name": db_name, "admin_username": admin_username,
            "client_image": image, "port": port}


async def _onboard_ps_managed_systems(db: Session, *, row: CloudDatabase, job_id: str,
                                      engine: str, tf_variables: dict, ctx: dict) -> None:
    """Onboard the DB into Password Safe: a managed system + managed account on the
    "{engine} SSM Custom Plugin" platform (functional account = the AWS IAM user for
    SSM), and — when a PRA Vault account exists for this DB — a managed system +
    managed account on the "PRA Vault Username Password" platform so Password Safe
    propagates rotations into the vaulted credential the tunnel injects. Ids +
    teardown state are stashed on the provisioning job's metadata. Best-effort."""
    from . import ps_api_service, ps_resource_service
    name = tf_variables.get("identifier") or f"clouddb-{row.id[:8]}"
    workgroup_id = await ps_api_service.get_workgroup_id(
        _cfg("clouddb_ps_workgroup") or _cfg("passwordsafe_workgroup"))
    stash: dict = {
        "ps_db_managed_user": ctx["managed_user"],
        "ps_db_jump_host_id": ctx["jump_host_id"],
        "ps_db_region": ctx["region"],
        "ps_db_admin_username": ctx["admin_username"],
        "ps_db_client_image": ctx["client_image"],
        "ps_db_name": ctx["db_name"],
    }

    # ── DB managed system (dbssm) ──
    db_platform_id = await ps_api_service.get_platform_id(_cfg(f"clouddb_ps_platform_{engine}"))
    iam_user = _cfg("clouddb_ps_ssm_iam_username")
    akid = _cfg("clouddb_ps_ssm_access_key_id")
    secret = _cfg("clouddb_ps_ssm_secret_access_key")
    if iam_user and akid and secret:
        fa_username, fa_password = iam_user, f"{akid}:{secret}"   # IAM-user mode
    else:
        fa_username, fa_password = "EC2", secrets.token_urlsafe(16)  # EC2 mode: role-based; PS still stores a value
    db_fa_id = await ps_api_service.create_functional_account_on_platform(
        platform_id=db_platform_id, account_name=fa_username,
        display_name=f"{name}-ssm-fa", password=fa_password,
        description=f"AWS SSM functional account for dashboard database {name} (db_id={row.id})")
    stash["ps_db_functional_account_id"] = db_fa_id
    # DNS name: {instance};{region};{db endpoint};{db name};{public key path};{suffix}
    dns_name = ";".join([
        ctx["jump_host_id"], ctx["region"], row.private_host, ctx["db_name"] or "",
        _cfg("clouddb_ps_ssm_public_key_path"), _cfg("clouddb_ps_ssm_account_suffix") or "local"])
    reg = await ps_resource_service.register_managed_system(
        name=f"{name}-db", host_name=row.private_host, ip_address="127.0.0.1",
        port=ctx["port"], functional_account_id=db_fa_id, platform_id=db_platform_id,
        workgroup_id=workgroup_id, managed_account_name=ctx["managed_user"],
        method="dbssm", dns_name=dns_name)
    stash["ps_db_registration_tf_state"] = reg.get("tf_state_json")
    stash["ps_db_system_id"] = reg.get("managed_system_id")
    stash["ps_db_account_id"] = reg.get("managed_account_id")
    logger.info("clouddb: onboarded DB managed system db_id=%s system_id=%s account_id=%s",
                row.id, reg.get("managed_system_id"), reg.get("managed_account_id"))

    # ── PRA Vault managed system (pravault) — only if the tunnel minted a vault account ──
    job = db.query(Job).filter(Job.id == job_id).first()
    vault_account_name = (job.metadata_dict or {}).get("vault_account_name") if job else None
    if vault_account_name and _cfg("bt_api_host"):
        pv_platform_id = await ps_api_service.get_platform_id(_cfg("clouddb_ps_pravault_platform"))
        pra_url = _cfg("bt_api_host")
        if not pra_url.lower().startswith("http"):
            pra_url = f"https://{pra_url}"
        pv_fa_id = await ps_api_service.create_functional_account_on_platform(
            platform_id=pv_platform_id,
            account_name=(_cfg("pra_config_api_client_id") or _cfg("bt_client_id")),
            display_name=f"{name}-pravault-fa",
            password=(_cfg("pra_config_api_client_secret") or _cfg("bt_client_secret")),
            description=f"PRA Config API functional account for dashboard database {name} (db_id={row.id})")
        stash["ps_pravault_functional_account_id"] = pv_fa_id
        reg2 = await ps_resource_service.register_managed_system(
            name=f"{name}-pravault", host_name=pra_url, ip_address="127.0.0.1", port=443,
            functional_account_id=pv_fa_id, platform_id=pv_platform_id,
            workgroup_id=workgroup_id, managed_account_name=vault_account_name, method="pravault")
        stash["ps_pravault_registration_tf_state"] = reg2.get("tf_state_json")
        stash["ps_pravault_system_id"] = reg2.get("managed_system_id")
        stash["ps_pravault_account_id"] = reg2.get("managed_account_id")
        logger.info("clouddb: onboarded PRA Vault managed system db_id=%s system_id=%s (account=%r)",
                    row.id, reg2.get("managed_system_id"), vault_account_name)
    else:
        logger.info("clouddb: no PRA Vault account for db_id=%s — skipping PRA Vault onboarding", row.id)

    job = db.query(Job).filter(Job.id == job_id).first()
    if job is not None:
        meta = job.metadata_dict or {}
        meta.update(stash)
        job.metadata_dict = meta
        db.commit()


# Generic terraform line → (pct, message) milestones for the DB job's progress bar
# (engine-agnostic phrases, since the resource type varies by cloud).
_DB_MILESTONES = [
    ("plan:",                20, "Planning…"),
    ("creating...",          40, "Creating the database…"),
    ("still creating",       55, "Creating the database (this can take several minutes)…"),
    ("creation complete",    85, "Database created; brokering access…"),
    ("destroying...",        40, "Destroying the database…"),
    ("still destroying",     60, "Destroying the database…"),
    ("destruction complete", 90, "Cleaning up…"),
]


def _job_stream(job_id: str, start_pct: int, start_msg: str):
    """Build an async ``on_line`` callback for ``terraform.apply``/``destroy`` that
    streams each line to the job's Live Output + advances a coarse progress bar.
    The per-line ``broadcast_progress`` also heartbeats the job row, which the
    startup reconcile uses to distinguish a live job from a dead one."""
    from ..api.websocket import broadcast_progress
    state = {"pct": start_pct, "msg": start_msg}

    async def on_line(line: str) -> None:
        job_service.cancel_check(job_id, state)  # stop terraform if the job was cancelled
        low = line.lower()
        for needle, pct, msg in _DB_MILESTONES:
            if needle in low:
                state["pct"], state["msg"] = max(state["pct"], pct), msg
                break
        await broadcast_progress(job_id, state["pct"], state["msg"], log_line=line)

    return on_line


async def _reclaim_gcp_create_wait_instance(
    *, row: CloudDatabase, job_id: str, engine: str, tf_variables: dict, exc: Exception,
) -> Optional[dict]:
    """GCP-only self-heal for the transient Cloud SQL *create-wait* failure. The
    google provider clears the resource id (``d.SetId("")``) when the create
    operation-wait errors, so the instance is dropped from Terraform state even
    though GCP finishes creating it — the apply raises "Error waiting for Create
    Instance:" and, left alone, orphans a RUNNABLE instance (which
    :func:`run_decommission` later has to sweep, wasting the instance and blocking
    the name for ~a week).

    Instead: poll GCP for the instance (guarded on our ``clouddb-id`` label) until
    it is RUNNABLE, ``terraform import`` it back into state, then re-apply to
    converge (create the database + user, read outputs). Returns the outputs dict
    on success, or ``None`` when this isn't that failure or the instance can't be
    reclaimed — the caller then fails the job as before."""
    if row.cloud != "gcp" or "error waiting for create instance" not in str(exc).lower():
        return None
    from . import gcp_service
    project = (tf_variables.get("project")
               or _cfg("gcp_project") or _cfg("gcp_project_id"))
    name = tf_variables.get("identifier") or f"clouddb-{row.id[:8]}"
    logger.warning("clouddb apply: transient GCP create-wait error for %s — checking "
                   "whether GCP created the instance anyway", name)
    body = await gcp_service.wait_sql_instance_runnable(project, name, row.id)
    if not body:
        logger.warning("clouddb apply: %s not reclaimable (absent / not ours / not "
                       "RUNNABLE) — failing the provision", name)
        return None
    logger.warning("clouddb apply: %s is RUNNABLE despite the create-wait error — "
                   "importing it into state and re-applying to converge", name)
    await terraform.import_resource(
        _deploy_dir(job_id), "google_sql_database_instance.this", f"{project}/{name}",
        env=terraform_provider_env.provider_env(row.cloud),
        template_dir=template_dir(engine, row.cloud), variables=tf_variables)
    return await terraform.apply(
        _deploy_dir(job_id), tf_variables, template_dir=template_dir(engine, row.cloud),
        env=terraform_provider_env.provider_env(row.cloud),
        on_line=_job_stream(job_id, 40, "Reclaiming the created instance…"),
    )


async def run_provision_apply(
    db: Session, *, db_id: str, job_id: str, engine: str, tf_variables: dict,
) -> None:
    """Background task: drive ``terraform apply`` for the engine module, fill the
    live fields on the ``CloudDatabase`` row, then broker the PRA tunnel so the
    private DB is reachable (Phase 2). Marks the job + row failed on apply error.
    Mocked in dev."""
    row = db.query(CloudDatabase).filter(CloudDatabase.id == db_id).first()
    if not row:
        logger.warning("clouddb apply: row %s vanished", db_id)
        return
    # The job's persisted tf_variables OMIT the master password — a secret is never
    # written to jobs.extra_data. Re-inject it from the secrets backend into the key
    # the engine module (and the downstream PRA tunnel + credential staging, which
    # read it back out of tf_variables) expect.
    _pw = config_service.get(f"clouddb/{db_id}/admin") or ""
    if _pw:
        _pw_key = {"azure": "administrator_password", "oci": "admin_password"}.get(row.cloud, "master_password")
        tf_variables[_pw_key] = _pw
    job_service.set_running(db, job_id)
    try:
        # Kick the shared Jumpoint host EARLY (only when PRA is configured) so its
        # ~2-min boot overlaps the 5-10-min RDS apply instead of stacking after it.
        if _pra_configured():
            try:
                from . import jumpoint_host_service
                await jumpoint_host_service.ensure_jumpoint_host(row.cloud, _cfg(row.cloud + "_region") or row.region)
            except Exception as exc:
                logger.warning("clouddb: ensure jumpoint host (pre-apply) failed (non-fatal): %s", exc)

        try:
            outputs = await terraform.apply(
                _deploy_dir(job_id), tf_variables, template_dir=template_dir(engine, row.cloud),
                env=terraform_provider_env.provider_env(row.cloud),
                on_line=_job_stream(job_id, 5, "Provisioning the database…"),
            )
        except terraform.TerraformError as exc:
            # GCP Cloud SQL create-wait self-heal: on the transient "Error waiting for
            # Create Instance" the google provider drops the (still-created) instance
            # from state. Try to reclaim it via import + re-apply rather than failing.
            outputs = await _reclaim_gcp_create_wait_instance(
                row=row, job_id=job_id, engine=engine, tf_variables=tf_variables, exc=exc)
            if outputs is None:
                raise
        row.instance_id = str(outputs.get("instance_id") or "")
        row.private_host = str(outputs.get("private_host") or "")
        if outputs.get("port"):
            row.port = int(outputs["port"])
        row.status = "available"
        db.commit()

        # Optional Password Safe DB onboarding (AWS-only, opt-in). Create the
        # dedicated managed user FIRST so the tunnel/vault injects it, then let PS
        # own its rotation. Any failure falls back to the legacy admin staging.
        onboard_ctx = None
        if row.private_host and _ps_db_onboarding_enabled(row):
            try:
                onboard_ctx = await _create_db_managed_user(
                    db, row=row, job_id=job_id, engine=engine, tf_variables=tf_variables)
            except Exception as exc:
                logger.warning("clouddb: PS managed-user creation failed db_id=%s "
                               "(falling back to admin staging): %s", db_id, exc)
                onboard_ctx = None

        # Phase 2: broker the PRA tunnel (only when PRA is configured + we have a host).
        # With PS onboarding active, inject the managed user; otherwise the admin cred.
        if row.private_host and _pra_configured():
            override = (onboard_ctx["managed_user"], onboard_ctx["managed_pw"]) if onboard_ctx else None
            await _broker_tunnel(db, row=row, job_id=job_id, engine=engine,
                                 tf_variables=tf_variables, override_cred=override)

        if onboard_ctx:
            # Full Password Safe onboarding (managed systems + accounts + PRA Vault sync).
            try:
                await _onboard_ps_managed_systems(
                    db, row=row, job_id=job_id, engine=engine, tf_variables=tf_variables, ctx=onboard_ctx)
            except Exception as exc:
                logger.warning("clouddb: PS managed-system onboarding failed db_id=%s "
                               "(non-fatal): %s", db_id, exc)
        else:
            # Legacy: stage the admin credential (functional account + Secrets Safe doc)
            # — independent of PRA (gated only on pscli_* config). Non-fatal.
            try:
                await _store_ps_credentials(db, row=row, job_id=job_id, tf_variables=tf_variables)
            except Exception as exc:
                logger.warning("clouddb credential staging failed db_id=%s (non-fatal): %s",
                               db_id, exc)

        # Register the DB as an Entitle integration (opt-in, non-fatal). Gated by
        # the global capability flag AND the per-build choice (on the job metadata).
        _job = db.query(Job).filter(Job.id == job_id).first()
        _reg_choice = bool((_job.metadata_dict or {}).get("register_in_entitle")) if _job else False
        if row.private_host and _reg_choice and _registration_enabled():
            await _register_entitle(db, row=row, engine=engine, tf_variables=tf_variables)

        job_service.set_completed(db, job_id)
        logger.info("clouddb apply complete db_id=%s host=%s tunnel=%s",
                    db_id, row.private_host, row.jump_item_id)
    except Exception as exc:
        row.status = "failed"
        db.commit()
        job_service.set_failed(db, job_id, str(exc))
        logger.exception("clouddb apply failed db_id=%s: %s", db_id, exc)


def start_decommission(db: Session, db_id: str, created_by: str = "") -> dict:
    """Synchronously record the intent to decommission and schedule the work:
    flip the row to ``decommissioning`` and create a ``clouddb_decommission``
    Job. The actual teardown (PRA tunnel+vault, Password Safe, RDS
    ``terraform destroy``) runs in :func:`run_decommission` as a background task
    — it's minutes long and must not block the HTTP request (doing so timed out
    the browser mid-destroy and silently orphaned the Vault account). Returns
    ``{ok, db_id, job_id}``; mirrors :func:`provision`."""
    row = db.query(CloudDatabase).filter(CloudDatabase.id == db_id).first()
    if not row:
        raise CloudDatabaseError(f"cloud database {db_id} not found")

    # Already in flight — return the existing job rather than starting a second.
    # Only short-circuit on an ACTIVE (pending/running) job; a cancelled/failed
    # prior decommission would otherwise wedge the row at "decommissioning" forever
    # (re-Delete becomes a silent no-op). Fall through to start a fresh teardown.
    if row.status == "decommissioning":
        existing = (db.query(Job)
                      .filter(Job.job_type == "clouddb_decommission",
                              Job.status.in_(("pending", "running")))
                      .order_by(Job.created_at.desc()).all())
        job = next((j for j in existing if (j.metadata_dict or {}).get("db_id") == db_id), None)
        if job:
            return {"ok": True, "db_id": db_id, "job_id": job.id}

    row.status = "decommissioning"
    db.commit()
    job = job_service.create_job(
        db, job_type="clouddb_decommission", created_by=created_by or row.created_by or "system",
        metadata={"db_id": db_id, "engine": row.engine, "cloud": row.cloud},
    )
    return {"ok": True, "db_id": db_id, "job_id": job.id}


async def run_decommission(db: Session, *, db_id: str, job_id: str) -> None:
    """Background teardown for a managed database. Removes the PRA tunnel + its
    Vault account, the staged Password Safe artifacts, and the RDS instance.
    Each step's failure is ACCUMULATED (not swallowed): any real teardown error
    leaves the row ``failed`` and the job ``failed`` with the details, so an
    orphaned Vault account / tunnel / instance is visible rather than hidden
    behind a false ``decommissioned``. Steps that simply didn't apply (PRA/PS
    never configured) are skips, not failures."""
    row = db.query(CloudDatabase).filter(CloudDatabase.id == db_id).first()
    if not row:
        job_service.set_failed(db, job_id, f"cloud database {db_id} not found")
        return
    job_service.set_running(db, job_id)
    errors: list[str] = []
    warnings: list[str] = []

    jobs = (db.query(Job)
              .filter(Job.job_type == "clouddb_provision")
              .order_by(Job.created_at.desc()).all())
    deploy_job = next((j for j in jobs if (j.metadata_dict or {}).get("db_id") == db_id), None)
    meta = (deploy_job.metadata_dict or {}) if deploy_job else {}

    # 1. PRA tunnel + Vault account (the vault account rides in the tunnel's
    #    Terraform state and is destroyed with it).
    job_service.update_progress(db, job_id, 10, "Removing PRA tunnel + Vault account…")
    tun_state = meta.get("tunnel_tf_state")
    if tun_state:
        try:
            from . import terraform_pra_service as pra
            await pra.remove_db_tunnel(tun_state)
            logger.info("clouddb tunnel + vault removed db_id=%s", db_id)
        except Exception as exc:
            errors.append(f"PRA tunnel/Vault removal: {exc}")
            logger.warning("clouddb tunnel removal for %s failed: %s", db_id, exc)

    # 1b. Entitle integration (if this DB was registered).
    ent_state = meta.get("entitle_registration_tf_state")
    if ent_state:
        job_service.update_progress(db, job_id, 20, "Removing Entitle integration…")
        try:
            from . import entitle_registration_service as ent
            await ent.deregister(ent_state)
            logger.info("clouddb Entitle integration removed db_id=%s", db_id)
        except Exception as exc:
            warnings.append(f"Entitle integration removal: {exc}")
            logger.warning("clouddb Entitle deregister for %s failed (non-fatal): %s", db_id, exc)

    # 1c. On-demand Entitle DB reachability forwarder (GCP-only; no-op otherwise / if none).
    try:
        from . import entitle_db_proxy_service
        await entitle_db_proxy_service.teardown_db_forwarder(db, row)
    except Exception as exc:
        warnings.append(f"Entitle DB forwarder teardown: {exc}")
        logger.warning("clouddb forwarder teardown for %s failed (non-fatal): %s", db_id, exc)

    # 2. Password Safe functional account.
    fa_id = meta.get("ps_functional_account_id")
    if fa_id:
        job_service.update_progress(db, job_id, 35, "Removing Password Safe functional account…")
        try:
            from . import ps_api_service
            await ps_api_service.delete_functional_account(int(fa_id))
            logger.info("clouddb functional account %s deleted db_id=%s", fa_id, db_id)
        except Exception as exc:
            errors.append(f"Password Safe functional account: {exc}")
            logger.warning("clouddb functional-account delete for %s failed: %s", db_id, exc)

    # 3. Secrets Safe secret.
    secret_ref = meta.get("bt_secret_ref")
    if secret_ref:
        job_service.update_progress(db, job_id, 45, "Removing Secrets Safe secret…")
        try:
            from . import secrets_backend_service
            await asyncio.to_thread(secrets_backend_service.delete_bt_secrets_safe, secret_ref)
            logger.info("clouddb Secrets Safe secret %r deleted db_id=%s", secret_ref, db_id)
        except Exception as exc:
            errors.append(f"Secrets Safe secret: {exc}")
            logger.warning("clouddb secrets-safe delete for %s failed: %s", db_id, exc)

    # 3b. Password Safe DB onboarding artifacts (managed systems + functional
    #     accounts). Deregister each managed system BEFORE deleting its functional
    #     account — a managed system that still references the functional account
    #     blocks the delete. The managed DB user itself dies with the RDS instance
    #     (step 4), so no DB-side drop is needed here.
    if any(meta.get(k) for k in ("ps_db_registration_tf_state", "ps_pravault_registration_tf_state",
                                 "ps_db_functional_account_id", "ps_pravault_functional_account_id")):
        job_service.update_progress(db, job_id, 50, "Removing Password Safe managed systems…")
        from . import ps_resource_service, ps_api_service
        for key, label in (("ps_pravault_registration_tf_state", "PRA Vault managed system"),
                           ("ps_db_registration_tf_state", "DB managed system")):
            state = meta.get(key)
            if state:
                try:
                    await ps_resource_service.deregister(state)
                    logger.info("clouddb %s removed db_id=%s", label, db_id)
                except Exception as exc:
                    errors.append(f"{label}: {exc}")
                    logger.warning("clouddb %s removal for %s failed: %s", label, db_id, exc)
        for key, label in (("ps_pravault_functional_account_id", "PRA Vault functional account"),
                           ("ps_db_functional_account_id", "DB functional account")):
            fa = meta.get(key)
            if fa:
                try:
                    await ps_api_service.delete_functional_account(int(fa))
                    logger.info("clouddb %s %s deleted db_id=%s", label, fa, db_id)
                except Exception as exc:
                    errors.append(f"{label}: {exc}")
                    logger.warning("clouddb %s delete for %s failed: %s", label, db_id, exc)

    # 4. The RDS instance itself (the long step).
    job_service.update_progress(db, job_id, 60, "Destroying the database instance…")
    if deploy_job:
        try:
            # terraform destroy still evaluates the module config, so it needs the
            # same -var set apply used (without it: "No value for required
            # variable"). The values don't change what's destroyed — resources
            # come from state — but every declared var must be set and provider
            # vars (e.g. the google provider's project/region) must be right.
            # Reconstruct from the row + config; the minted admin password is still
            # in the config store (deleted only after a clean decommission, below).
            destroy_vars = _build_tf_variables(
                engine=row.engine, cloud=row.cloud, region=row.region, db_id=db_id,
                db_name=_db_name_from(meta.get("name") or "appdb"),
                master_username="dbadmin",
                master_password=config_service.get(f"clouddb/{db_id}/admin") or "unused-on-destroy",
                opts={},
            )
            # State lives in the active storage backend, so destroy recovers even
            # if the deploy dir was lost to a container recreate — pass template_dir
            # so terraform.destroy rebuilds the module from it + the remote state.
            await terraform.destroy(
                _deploy_dir(deploy_job.id), variables=destroy_vars,
                env=terraform_provider_env.provider_env(row.cloud),
                template_dir=template_dir(row.engine, row.cloud),
                on_line=_job_stream(job_id, 60, "Destroying the database…"),
            )
            logger.info("clouddb instance destroyed db_id=%s cloud=%s", db_id, row.cloud)
        except Exception as exc:
            errors.append(f"DB destroy: {exc}")
            logger.warning("clouddb destroy for %s failed: %s", db_id, exc)
    else:
        errors.append("no provisioning job recorded for this database — the instance "
                      "may need manual termination in the cloud console")

    # 4b. Orphan safety net (GCP Cloud SQL only). The google provider drops a Cloud
    #     SQL instance from Terraform state when the create operation-wait errors
    #     (d.SetId("")), even though GCP finishes creating it — so a mid-create apply
    #     failure leaves a RUNNABLE instance the destroy above (empty state) can't
    #     reclaim. Delete it directly by name, guarded on the clouddb-id label so we
    #     never touch anything we didn't create. No-ops (404) after a clean destroy.
    #     (AWS/Azure providers taint the resource in state on the same error, so their
    #     destroy already covers it; only GCP exhibits the state-drop.)
    if row.cloud == "gcp":
        job_service.update_progress(db, job_id, 80, "Checking for an orphaned instance…")
        try:
            from . import gcp_service
            project = _cfg("gcp_project") or _cfg("gcp_project_id")
            result = await gcp_service.sweep_orphan_sql_instance(
                project, f"clouddb-{db_id[:8]}", db_id)
            if result == "deleted":
                logger.warning("clouddb decommission: swept orphaned GCP instance "
                               "clouddb-%s (Terraform state was lost to a create-wait "
                               "failure)", db_id[:8])
        except Exception as exc:
            errors.append(f"GCP orphan sweep: {exc}")
            logger.warning("clouddb GCP orphan sweep for %s failed: %s", db_id, exc)

    if errors:
        row.status = "failed"
        db.commit()
        job_service.set_failed(db, job_id, "; ".join(errors + warnings))
        logger.error("clouddb decommission db_id=%s ended with errors: %s", db_id, errors)
        return

    row.status = "decommissioned"
    db.commit()
    # Retire the minted admin credential from the encrypted config store too.
    config_service.delete(f"clouddb/{db_id}/admin")

    # Terminate the shared Jumpoint host if nothing is left using it (best-effort;
    # the row is no longer active, so it's excluded from the count).
    job_service.update_progress(db, job_id, 90, "Reclaiming idle Jumpoint host…")
    try:
        from . import jumpoint_host_service
        await jumpoint_host_service.teardown_jumpoint_host_if_idle(db, row.cloud, _cfg(row.cloud + "_region") or row.region)
    except Exception as exc:
        warnings.append(f"Jumpoint host teardown: {exc}")
        logger.warning("clouddb: jumpoint host idle-teardown failed (non-fatal): %s", exc)

    job_service.set_completed(db, job_id, {"db_id": db_id, **({"warnings": warnings} if warnings else {})})
    logger.info("clouddb decommissioned db_id=%s", db_id)


def list_databases(db: Session) -> list[dict]:
    # Hide cleanly-decommissioned rows so old endpoints don't linger; keep
    # available/provisioning/decommissioning and `failed` (a failed decommission
    # is an orphan the operator still needs to see).
    rows = (db.query(CloudDatabase)
              .filter(CloudDatabase.status != "decommissioned")
              .order_by(CloudDatabase.created_at.desc()).all())
    return [_serialize(r) for r in rows]


def connection_info(db: Session, db_id: str) -> dict:
    row = db.query(CloudDatabase).filter(CloudDatabase.id == db_id).first()
    if not row:
        raise CloudDatabaseError(f"cloud database {db_id} not found")
    # jump_item_id is the PRA protocol-tunnel jump a user opens to reach the
    # private DB (populated once the tunnel is brokered; null if PRA is unset).
    return {
        "db_id": row.id, "engine": row.engine, "cloud": row.cloud,
        "status": row.status, "private_host": row.private_host, "port": row.port,
        "jump_item_id": row.jump_item_id,
    }


def ansible_connection_vars(db: Session, db_id: str) -> dict:
    """Connection variables an Ansible ``localhost`` play uses to reach this managed
    DB over the network. Resolved server-side and injected as **scrubbed secret
    extra-vars** — the operator never sees them.

    The per-cloud admin-credential normalization mirrors :func:`_broker_tunnel` /
    :func:`_entitle_register_core` exactly:
      - user     — ``master_username`` | ``administrator_login`` from the provisioning
                   job's tf_variables, with the Cloud SQL SQL Server (``sqlserver``) /
                   Oracle (``ADMIN``) overrides, else ``dbadmin``.
      - password — the encrypted config store (``clouddb/{id}/admin``); tf_variables
                   never carry it (scrubbed).
      - db_name  — ``master`` for SQL Server (you connect to ``master``; RDS omits a
                   db_name), the ADB name for Oracle, else the provisioned db_name.

    The returned keys are engine-independent so one sample playbook maps them onto any
    module's args (``login_host: "{{ db_login_host }}"`` …). Raises
    :class:`CloudDatabaseError` when the row or its admin credential can't be resolved."""
    row = db.query(CloudDatabase).filter(CloudDatabase.id == db_id).first()
    if not row:
        raise CloudDatabaseError(f"cloud database {db_id} not found")
    engine = row.engine
    prov_job = _provision_job_for(db, db_id)
    tfv = ((prov_job.metadata_dict or {}).get("tf_variables") if prov_job else None) or {}

    default_user = ("ADMIN" if engine == "oracle"
                    else "sqlserver" if (engine == "sqlserver" and row.cloud == "gcp")
                    else "dbadmin")
    admin_username = (tfv.get("master_username")
                      or tfv.get("administrator_login") or default_user)
    admin_password = (tfv.get("master_password")
                      or tfv.get("administrator_password")
                      or tfv.get("admin_password")
                      or config_service.get(f"clouddb/{row.id}/admin") or "")
    if not admin_password:
        raise CloudDatabaseError(
            f"no admin credential available for db_id={row.id} "
            f"(provisioning job pruned?) — cannot build Ansible connection vars")

    if engine == "sqlserver":
        db_name = "master"
    elif engine == "oracle":
        db_name = _oracle_db_name(row.id)
    else:
        db_name = tfv.get("db_name", "")

    return {
        "db_engine": engine,
        "db_login_host": row.private_host or "",
        "db_login_port": row.port or _DEFAULT_PORTS.get(engine),
        "db_login_user": admin_username,
        "db_login_password": admin_password,
        "db_name": db_name,
    }


def _serialize(r: CloudDatabase) -> dict:
    return {
        "id": r.id, "engine": r.engine, "provider": r.provider, "cloud": r.cloud,
        "region": r.region, "instance_id": r.instance_id, "private_host": r.private_host,
        "port": r.port, "status": r.status, "jump_item_id": r.jump_item_id,
        "entitle_integration_id": r.entitle_integration_id,
        "created_by": r.created_by,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }
