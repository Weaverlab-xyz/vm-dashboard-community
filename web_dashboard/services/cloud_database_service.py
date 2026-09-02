"""
Database infrastructure — the engine/cloud-agnostic service seam (community).

Provisions **private** managed databases (Postgres / MySQL / SQL Server) reached
only through a BeyondTrust PRA tunnel, and records each in the ``cloud_databases``
inventory table. Shaped like the other cloud services; drives Terraform via a
per-job deploy dir (``terraform/deployments/{job_id}``).

Also **registers** databases it did not provision (``source='registered'``) — on-premises
(``cloud='local'``) or in a cloud — so they can be Config Management targets. Provisioning
needs a Terraform module and is therefore cloud-only; registering needs only somewhere to
reach, which is why ``VALID_REGISTER_CLOUDS`` is wider than ``VALID_CLOUDS``.

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
from . import (cloud_db_adapter_service, config_service, job_service, terraform,
               terraform_provider_env)
from .region_config import resolve_region

logger = logging.getLogger(__name__)

# Community supports the three engines the beyondtrust/sra provider can tunnel
# (no MongoDB resource yet). All engine × cloud combos are wired — see _IMPLEMENTED.
VALID_ENGINES = {"postgres", "mysql", "sqlserver", "oracle"}
VALID_CLOUDS = {"aws", "azure", "gcp", "oci"}
# Clouds a database may be REGISTERED from. Wider than VALID_CLOUDS because
# provisioning needs a Terraform module and registering needs only somewhere to reach:
# 'local' is an on-prem database, run against by the local runner (the same shape a
# kubeconfig-registered k8s cluster already has).
VALID_REGISTER_CLOUDS = VALID_CLOUDS | {"local"}
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

# SQL Server managed offerings that CAN satisfy Entitle's Microsoft SQL Server
# connector, which requires sysadmin (standard mode) or CONTROL SERVER + a fixed
# permission set (least-privilege mode). NONE of the managed SQL Server flavors this
# dashboard provisions today qualify: GCP Cloud SQL (provider "cloudsql") and AWS RDS
# (provider "rds") reserve sysadmin/CONTROL SERVER for the platform, and Azure SQL
# Database (provider "sql_database") is a logical server with no server-level grants.
# Empty today; PR2's Entitle-compatible offerings add "rds_custom" (AWS RDS Custom)
# and "sql_managed_instance" (Azure SQL Managed Instance), which DO grant sysadmin.
_ENTITLE_VIABLE_SQLSERVER_PROVIDERS: frozenset = frozenset()


def _entitle_ineligible_reason(engine: str, provider: Optional[str], *,
                               source: Optional[str] = None,
                               cloud: Optional[str] = None) -> Optional[str]:
    """``None`` if this cloud DB can be onboarded to Entitle, else the reason it can't,
    worded for the user. Single source of truth for all three gates — the button (via
    :func:`_serialize`'s ``entitle_viable``), the API pre-flight, and
    :func:`_entitle_register_core` — so they can't disagree about what is offerable.

    Two independent blockers, each with its own message:

    * a **registered** database (``source="registered"``) was never provisioned here, so
      there is no provisioning job, no ``tf_variables`` and no ``config://clouddb/<id>/
      admin`` entry for :func:`_entitle_register_core` to build the connector's admin
      credential from. Its Password Safe managed account is checked out at run time and
      never stored on the row, so registration can only ever fail. Checked first: it
      holds for every engine, and it is the more fundamental of the two.
    * **SQL Server**'s connector needs sysadmin/CONTROL SERVER, which the managed
      flavors this dashboard provisions can't grant (see
      ``_ENTITLE_VIABLE_SQLSERVER_PROVIDERS``).

    ``cloud`` only sharpens the SQL Server message for a row whose ``provider`` is unset;
    omitting it is safe.
    """
    if (source or "provisioned") == "registered":
        return ("Register in Entitle is only supported on dashboard-provisioned "
                "databases. This database was registered, so there is no provisioning "
                "credential to give the Entitle connector — its Password Safe managed "
                "account is checked out at run time and never stored.")
    if engine == "sqlserver" and (provider or "") not in _ENTITLE_VIABLE_SQLSERVER_PROVIDERS:
        return (f"Entitle's Microsoft SQL Server connector requires sysadmin/CONTROL "
                f"SERVER, which managed {provider or cloud or 'cloud'} SQL Server does "
                f"not grant. Register is only supported on Entitle-compatible SQL Server "
                f"(Azure SQL Managed Instance / AWS RDS Custom).")
    return None


def _entitle_viable(engine: str, provider: Optional[str],
                    source: Optional[str] = None) -> bool:
    """Whether this cloud DB can be managed by its Entitle DB connector — the boolean
    face of :func:`_entitle_ineligible_reason` (see there for the blockers)."""
    return _entitle_ineligible_reason(engine, provider, source=source) is None


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


async def _to_thread(fn, /, *args, **kwargs):
    """Run a blocking cloud-database call on clouddb's OWN bounded thread pool.

    These calls used to go to the event loop's default ThreadPoolExecutor — one unbounded
    queue shared by every provider, about 8 threads in-container, with no deadline. That is
    the shape that took the whole dashboard down for 30 minutes on 2026-08-12 when two
    clouds went slow upstream: requests that needed nothing from clouddb queued behind
    calls that never returned. See services/cloud_executor.py, which is explicit that a
    bigger shared pool is not the fix, because a shared pool of any size is still a shared
    failure domain.

    Refusals become CloudDatabaseError so every existing ``except CloudDatabaseError`` — which is what turns a
    failure into a 503 or an unavailable tile — keeps working unchanged. Anything cloud-database
    itself raises propagates untouched.

    ``cloud_executor`` is imported INSIDE the function on purpose: this module is loaded by
    file path under a non-dotted name in its own tests, and a top-level relative import
    fails there with "attempted relative import with no known parent package".
    """
    from . import cloud_executor
    try:
        return await cloud_executor.run("clouddb", fn, *args, **kwargs)
    except cloud_executor.CloudCallError as exc:
        raise CloudDatabaseError(str(exc)) from exc


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


def connection_db_name(row, tf_variables: Optional[dict] = None) -> str:
    """The database an admin session actually opens against this row — what the
    Databases page and the Connection modal show, and what every admin-session
    consumer connects to: the PRA protocol tunnel (:func:`_broker_tunnel`), the
    native Entitle connector (:func:`_entitle_register_core`), the Secrets Safe
    admin document, the managed-user creation and the Ansible connection vars.
    Not display-only — changing it changes what those sessions open.

    Deliberately distinct from the *catalog* stored in ``row.db_name``: for SQL Server
    you always connect to the ``master`` system database. RDS creates no user database
    at all (the module omits ``db_name``), and the Azure/GCP admin paths target
    ``master`` on purpose even though their modules do create a catalog alongside it —
    so the substitution is applied on read and never stored. Storing it would poison
    the Entitle adapter, whose ``FN_DB_NAME`` must name a real catalog or nothing.

    Pure: no ``Session``, no job lookup, because :func:`_serialize` calls it per row.
    ``tf_variables`` is an optional *fallback* source for the callers that already hold
    the provisioning job's var set. It only fills a blank ``row.db_name``, so passing it
    can never change an answer the row already gives — it keeps a row the backfill has
    not reached yet (provisioned before the column existed, on an instance that has not
    restarted since) resolvable, which is where these callers used to read from.
    """
    engine = row.engine or ""
    registered = (getattr(row, "source", None) or "provisioned") == "registered"
    if engine == "sqlserver":
        # A registered row is somebody else's server, so the operator's entry wins and
        # master is only the fallback — the same asymmetry _registered_connection_vars
        # already has. A provisioned one is always reached through master.
        return (row.db_name or "master") if registered else "master"
    # Oracle's ADB name is derived from the row id, so it stays resolvable even for a
    # row the backfill could not reach. Never invent one for a registered Oracle row.
    if engine == "oracle" and not row.db_name and not registered:
        return _oracle_db_name(row.id)
    return row.db_name or (tf_variables or {}).get("db_name", "") or ""


def _aws_db_security_groups(regional: dict, opts: dict) -> list[str]:
    """Security groups to attach to an RDS instance. ``regional`` is that region's
    already-resolved config set (``resolve_region("aws", region)``).

    An explicit selection from the provision form always wins. When the caller
    picks none, fall back to the sandbox's DB-tier security group for that region
    (``aws_db_security_group_id`` / ``aws_region.<region>.db_security_group_id``)
    instead of letting Terraform attach the VPC *default* SG — which allows no
    ingress on 5432/3306/1433 from the Gateway, so the PRA tunnel to the new
    database times out. That group exists precisely to carry those rules.

    Blank config (no sandbox run) still yields ``[]`` → the VPC default group, the
    historical behaviour.
    """
    chosen = [sg for sg in (opts.get("vpc_security_group_ids") or []) if sg]
    if chosen:
        return chosen
    configured = regional["db_security_group_id"]
    return [configured] if configured else []


def _gcp_iam_auth_wanted() -> bool:
    """Whether a NEW Cloud SQL instance should come up with IAM database
    authentication on — i.e. whether GCP Password Safe onboarding is
    configured at all. Reads the same two switches as :func:`_ps_db_onboarding_enabled`
    minus the per-row parts, because tf variables are built before there is a row to
    inspect. Harmless when onboarding is later turned off; the flag only ever *permits*
    IAM authentication, it does not require it."""
    return (config_service.get_bool("clouddb_ps_onboarding_enabled", False)
            and (config_service.get("passwordsafe_gcp_db_registration_method")
                 or "off").lower() != "off")


def _build_tf_variables(
    *, engine: str, cloud: str, region: str, db_id: str, db_name: str,
    master_username: str, master_password: str, opts: dict,
) -> dict:
    """The Terraform -var set for the engine module (per engine/cloud branch below).

    The module itself hardcodes ``publicly_accessible = false`` — the private-only
    guarantee lives in the .tf, not in a toggle-able variable.

    Per-region resource ids (subnets, security groups, DB networks, resource group)
    resolve through ``region_config.resolve_region(cloud, region)`` so a database
    provisioned in a non-default region picks up that region's network; a blank
    field (or the default region) falls back to the flat config keys, so
    single-region installs are unchanged. An explicit value passed in ``opts``
    always wins.
    """
    if (engine, cloud) == ("postgres", "aws"):
        _aws = resolve_region("aws", region)
        return {
            "region": region,
            "identifier": f"clouddb-{db_id[:8]}",
            "db_name": db_name,
            "master_username": master_username,
            "master_password": master_password,
            "instance_class": opts.get("instance_class", "db.t3.micro"),
            "allocated_storage": opts.get("allocated_storage", 20),
            "db_subnet_group_name": opts.get("db_subnet_group_name")
                or _aws["db_subnet_group_name"],
            "vpc_security_group_ids": _aws_db_security_groups(_aws, opts),
            # Attach the force_ssl=0 parameter group the sandbox pre-created, so the
            # PRA protocol tunnel's cleartext gateway→RDS connection isn't rejected.
            # Per-region: the sandbox creates one group per region and emits it as
            # aws_region.<r>.db_parameter_group_name. Empty config → "" → module
            # falls back to the RDS default group.
            "parameter_group_name": _aws["db_parameter_group_name"],
            "tags": {"managed-by": "vm-dashboard", "clouddb-id": db_id},
        }

    if (engine, cloud) == ("mysql", "aws"):
        _aws = resolve_region("aws", region)
        return {
            "region": region,
            "identifier": f"clouddb-{db_id[:8]}",
            "db_name": db_name,
            "master_username": master_username,
            "master_password": master_password,
            "instance_class": opts.get("instance_class", "db.t3.micro"),
            "allocated_storage": opts.get("allocated_storage", 20),
            "db_subnet_group_name": opts.get("db_subnet_group_name")
                or _aws["db_subnet_group_name"],
            "vpc_security_group_ids": _aws_db_security_groups(_aws, opts),
            # MySQL's cleartext knob is require_secure_transport=0 (not
            # rds.force_ssl) — its own mysql8.0-family group the sandbox
            # pre-creates per region. Empty config → "" → module falls back to
            # the RDS default.
            "parameter_group_name": _aws["db_mysql_parameter_group_name"],
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
        _aws = resolve_region("aws", region)
        return {
            "region": region,
            "identifier": f"clouddb-{db_id[:8]}",
            "master_username": master_username,
            "master_password": master_password,
            "instance_class": sqlserver_class,
            "allocated_storage": opts.get("allocated_storage", 20),
            "db_subnet_group_name": opts.get("db_subnet_group_name")
                or _aws["db_subnet_group_name"],
            "vpc_security_group_ids": _aws_db_security_groups(_aws, opts),
            "tags": {"managed-by": "vm-dashboard", "clouddb-id": db_id},
        }

    if (engine, cloud) == ("postgres", "gcp"):
        # The private_network the instance gets its private IP on; the sandbox
        # configures private-services-access on it. ssl_mode defaults inside the
        # module to ALLOW_UNENCRYPTED_AND_ENCRYPTED so the PRA tunnel's cleartext
        # gateway→DB connection is accepted (mirrors AWS's force_ssl=0).
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
            "iam_authentication": _gcp_iam_auth_wanted(),
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
            "iam_authentication": _gcp_iam_auth_wanted(),
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
            "private_dns_zone_id": opts.get("private_dns_zone_id") or _az["db_mysql_private_dns_zone_id"],
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
        _az = resolve_region("azure", region)
        return {
            "resource_group_name": opts.get("resource_group_name") or _az["resource_group"],
            "location": region,
            "identifier": f"clouddb-{db_id[:8]}",
            "administrator_login": master_username,
            "administrator_password": master_password,
            "sku_name": sqlserver_sku,
            "db_name": db_name,
            # SQL Server gets its own PE subnet + privatelink zone, per region.
            "subnet_id": opts.get("subnet_id") or _az["db_sqlserver_subnet_id"],
            "private_dns_zone_id": opts.get("private_dns_zone_id") or _az["db_sqlserver_private_dns_zone_id"],
            "tags": {"managed-by": "vm-dashboard", "clouddb-id": db_id},
        }

    if (engine, cloud) == ("oracle", "oci"):
        # OCI Autonomous Database (ATP/ADW). Free-tier (default) is a PUBLIC
        # endpoint reached over the PRA tcp tunnel from the public-subnet gateway
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


# The Terraform variables that name a REGION-SCOPED network resource, per cloud.
# Only AWS and Azure appear: GCP's ``private_network`` is a *global* VPC (Cloud SQL
# takes its private IP from a global private-services-access range, so any region
# works), and OCI's Autonomous DB free tier is a public endpoint with no network of
# ours. Those two clouds therefore cannot be cross-region, and have nothing to check.
_REGIONAL_NETWORK_VARS: dict[str, tuple[str, ...]] = {
    "azure": ("delegated_subnet_id", "subnet_id"),
    "aws": ("db_subnet_group_name", "vpc_security_group_ids"),
}


def regional_network_ids(*, engine: str, cloud: str, region: str,
                         opts: Optional[dict] = None) -> dict:
    """The region-scoped network resources a provision would attach to, as
    ``{terraform var: value}`` with blanks dropped (lists stay lists).

    Answers "which subnet / DB subnet group / security groups is this database about
    to land on?" so a caller can confirm they really live in ``region`` *before* any
    row, credential or job exists. That check matters because
    :func:`region_config.resolve_region` falls every field back to the flat config
    keys: a region with no config set of its own — or with the DB subnet field left
    blank — silently resolves to the DEFAULT region's subnet, and Azure only says so
    ~90 seconds into the apply ("VnetWithDifferentLocationNotSupported").

    Built by running the real :func:`_build_tf_variables` and picking the network keys
    out of it, so the resolution order (opts override → region entry → flat key) has
    exactly ONE implementation and the guard can never drift from what the apply does.
    The placeholder identity/secret arguments below feed no network variable.

    Returns ``{}`` for a cloud with nothing to check or an unimplemented combo — the
    caller's job here is to validate a network, not the engine/cloud matrix.
    """
    keys = _REGIONAL_NETWORK_VARS.get((cloud or "").strip().lower())
    if not keys:
        return {}
    try:
        tf = _build_tf_variables(
            engine=engine, cloud=cloud, region=region,
            db_id="0" * 36, db_name="probe", master_username="probe",
            master_password="probe", opts=dict(opts or {}),
        )
    except (NotImplementedError, CloudDatabaseError, ValueError):
        return {}
    out: dict = {}
    for key in keys:
        val = tf.get(key)
        if isinstance(val, (list, tuple)):
            items = [str(v).strip() for v in val if v and str(v).strip()]
            if items:
                out[key] = items
        elif val and str(val).strip():
            out[key] = str(val).strip()
    return out


def provision(
    db: Session, *, engine: str, cloud: str, region: str, name: str,
    created_by: str, master_username: str = "dbadmin",
    vault_account_group_id: Optional[int] = None,
    jump_group: Optional[str] = None, jumpoint_name: Optional[str] = None,
    pra_credential_ref: Optional[str] = None,
    register_in_entitle: bool = False,
    register_in_passwordsafe: Optional[bool] = None, **opts,
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

    from . import expiry_policy
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
        # Auto-delete timer from the global default; None (no timer) unless the feature
        # is on AND a default is configured. Only this PROVISION path stamps one —
        # register_database deliberately does not, since deleting a registered row only
        # deregisters it. See expiry_policy.default_expiry_for_kind.
        expires_at=expiry_policy.default_expiry_for_kind("database", source="provisioned"),
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
    # Password Safe onboarding is a THREE-state choice, and absent is the important
    # one: True/False are the operator's explicit answer from the provision form's
    # checkbox, and ABSENT means "whatever the Password Safe DB onboarding setting says
    # when the apply gets there". Every caller that predates the checkbox — the API
    # without the field, the tests, any script — sends absent, so their behaviour is
    # exactly what it was. Only written when the caller actually chose.
    if register_in_passwordsafe is not None:
        job_meta["register_in_passwordsafe"] = bool(register_in_passwordsafe)
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
    # Record the catalog on the row as well, so nothing depends on this job surviving:
    # the Databases page shows it and the Entitle cloud-function adapter scopes its
    # grants to it. Exactly the -var, with no engine substitution — that is what makes
    # the column definitionally equal to what Terraform was handed. RDS SQL Server
    # leaves it NULL because its module creates no user database (see the branch in
    # _build_tf_variables); `master` is applied on read by connection_db_name instead.
    row.db_name = tf_variables.get("db_name") or None
    db.commit()
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
    """True when a PRA/SRA appliance + Gateway + Jump Group are configured —
    the prerequisites for brokering a tunnel. When false, a DB is still
    provisioned/recorded; it just isn't reachable until PRA is set up."""
    return all(_cfg(k) for k in ("bt_api_host", "bt_jumpoint_name", "bt_jump_group_name"))


def _pscli_configured() -> bool:
    """True when the Password Safe OAuth client (shared by ps-cli and the
    ps_api_service REST calls) is configured — the gate for staging the DB
    admin credential as a functional account + Secrets Safe secret."""
    return all(_cfg(k) for k in ("pscli_api_url", "pscli_client_id", "pscli_client_secret"))


async def _resolve_ecs_deploy_key() -> str:
    """BeyondTrust Gateway Docker deploy key — same resolution as the EC2
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
    """Make sure the ECS-hosted Gateway (the PRA gateway) has at least one
    live node before brokering a tunnel. The tunnel jump item can be created
    with zero nodes online, but it shows 'Unavailable' in PRA until a node
    registers — so check the Gateway cluster for a running task and start one
    (the same launch the EC2 deploy flow does) only when there is none.
    Non-fatal throughout, like the EC2 path."""
    from . import aws_service
    cluster = _cfg("bt_ecs_cluster")
    family = _cfg("bt_ecs_task_family")
    try:
        tasks = await aws_service.list_ecs_tasks(region, cluster)
    except Exception as exc:
        logger.warning("clouddb: could not list Gateway ECS tasks (%s) — skipping node check", exc)
        return
    live = [t for t in tasks
            if t.get("lastStatus") in ("PROVISIONING", "PENDING", "RUNNING")
            and f"task-definition/{family}:" in (t.get("taskDefinitionArn") or "")]
    if live:
        logger.info("clouddb: Gateway node already up (%d live task(s) in cluster %s)",
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
            "clouddb: no live Gateway node and cannot auto-start one — set %s. "
            "The tunnel will show Unavailable in PRA until a Gateway node is online.",
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
        logger.info("clouddb: started Gateway ECS node %s (launch_type=%s) — "
                    "registers with PRA in ~1-2 min", arn.split("/")[-1], launch_type)
    except Exception as exc:
        logger.warning("clouddb: Gateway ECS node launch failed (non-fatal): %s", exc)


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
    # The shared Gateway host was ensured at the start of run_provision_apply
    # (so its ~2-min boot overlaps the RDS apply); ensure again here — idempotent
    # and cheap when the host is already up — so the task is running before we
    # broker the tunnel.
    from . import jumpoint_host_service
    try:
        await jumpoint_host_service.ensure_jumpoint_host(row.cloud, _cfg(row.cloud + "_region") or row.region)
    except Exception as exc:
        logger.warning("clouddb: ensure gateway host (broker) failed (non-fatal): %s", exc)
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
            # The admin session catalog, resolved from the row by the same function
            # the Databases page and the Connection dialog use — NOT the Entitle
            # grant scope (cloud_db_adapter_service._database_name).
            database=connection_db_name(row, tf_variables),
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

# Actions accepted by the post-provision ps-register endpoint / job.
VALID_PS_DB_ACTIONS = ("register", "deregister")


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
    # Refuse an un-onboardable row up front, before any Entitle or Terraform work:
    # a registered DB has no provisioning credential to register with, and managed SQL
    # Server would yield an integration that then fails Entitle's resource sync
    # ("missing required server permissions"). See _entitle_ineligible_reason. On the
    # provision path _register_entitle swallows this, so non-viable rows simply never
    # auto-register; on the user-initiated path run_entitle_register surfaces it as a
    # failed job — and the API rejects it with a 400 before the job is even queued.
    reason = _entitle_ineligible_reason(engine, row.provider, source=row.source,
                                        cloud=row.cloud)
    if reason:
        raise CloudDatabaseError(reason)
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
        # The connector logs in as the admin and mints server-level principals, so
        # this is the admin-session catalog, not the grant scope. Unreachable for
        # sqlserver today: _entitle_ineligible_reason above rejects every managed
        # flavor (_ENTITLE_VIABLE_SQLSERVER_PROVIDERS is empty), so the sqlserver
        # branch only starts running when RDS Custom / SQL MI are added — both real
        # instances where master is right and `USE` works.
        database=connection_db_name(row, tfv),
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


async def _pair_adapter(db: Session, *, row: CloudDatabase, engine: str,
                        job_id: str) -> None:
    """Queue the adapter pairing for an engine the native connector can't serve.

    Non-fatal, exactly like :func:`_register_entitle`: a provisioned database that
    works is the primary deliverable, and a failed Entitle wiring should never fail
    the provision that produced it. Queued rather than run inline because pairing
    itself deploys a function (a full terraform apply) — doing that inside the DB's
    own apply would put two streamed terraform runs in one job.
    """
    from . import cloud_db_adapter_service
    try:
        result = cloud_db_adapter_service.start_pairing(
            db, row=row, created_by="clouddb-provision",
            # Dry run: the adapter is deployed and registered, but its first act is
            # to report the SQL it WOULD run. Arming it is a deliberate second step.
            dry_run=True)
        logger.info("clouddb: adapter pairing queued db_id=%s job_id=%s (%s)",
                    row.id, result["job_id"],
                    cloud_db_adapter_service.adapter_required(engine))
    except Exception as exc:
        logger.warning("clouddb: adapter pairing could not be queued db_id=%s "
                       "(non-fatal): %s", row.id, exc)


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
        job_service.set_failed(db, job_id, f"database {db_id} not found")
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
            # This document records the ADMIN credential, so it must carry the
            # catalog that credential actually connects to — the same one the tunnel
            # targets. The raw tf db_name was wrong for ("sqlserver", "aws"), whose
            # var set omits db_name: it stored an empty database on an otherwise
            # usable credential record.
            "database": connection_db_name(row, tf_variables),
            "username": admin_username,
            "password": admin_password,
        })
        ref = await _to_thread(
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


# ── Optional Password Safe DB onboarding (AWS / Azure / GCP, opt-in) ──────────

def _dbops_audience(row: CloudDatabase, db=None) -> str:
    """Address field 4 for a GCP ``cloud-run`` managed system, or ``""``.

    Resolution order, and the order is the whole point:

    1. The **dashboard-deployed DB-Ops service in this database's own region**, as
       recorded on its ``CloudFunction`` row. Needs a session, so callers that have
       one pass it.
    2. ``clouddb_ps_gcp_dbops_audience`` — the flat config key, for a service the
       operator deployed, or one behind a custom domain / Private Service Connect.

    The instinct is to let the explicit config key win, and that instinct is the trap
    this repo has already been bitten by (see the note on the key in config.py). A
    flat key answers "which service" globally, but a Cloud Run service on Direct VPC
    egress is REGION-LOCKED. An operator who sets the key for a us-east1 service and
    then onboards a database in europe-west1 would address every rotation for it at a
    service that cannot reach the instance — and a rotation that times out may already
    have applied the change. A per-region recorded fact must beat a global setting.
    """
    if db is not None:
        try:
            from . import clouddb_dbops_service
            recorded = clouddb_dbops_service.audience_for_region(db, row.region or "")
        except Exception as exc:
            # Never let this be the thing that breaks onboarding: the fallback below
            # is exactly the behaviour that shipped before the service existed.
            logger.warning("dbops audience lookup failed for %s: %s", row.id, exc)
            recorded = ""
        if recorded:
            return recorded
    return _cfg("clouddb_ps_gcp_dbops_audience")


def _ps_db_onboarding_enabled(row: CloudDatabase, db=None) -> bool:
    """Gate for the full Password Safe DB onboarding: the Password Safe OAuth
    client configured, the operator opt-in flag set, and a supported cloud —
    AWS (SSM plugin), Azure (Run Command plugins, unless the Azure method is set to
    "off") or GCP (Cloud SQL plugins on the data-api channel, off by default). When
    off, the DB still provisions and the legacy admin-credential staging runs instead."""
    if not (_pscli_configured()
            and config_service.get_bool("clouddb_ps_onboarding_enabled", False)):
        return False
    if row.cloud == "aws":
        return True
    if row.cloud == "azure":
        return (config_service.get("passwordsafe_azure_db_registration_method")
                or "runcommand").lower() != "off"
    if row.cloud == "gcp":
        if (config_service.get("passwordsafe_gcp_db_registration_method")
                or "off").lower() == "off":
            return False
        if row.engine not in _GCP_CHANNEL_DEFAULTS:
            return False
        # The cloud-run channel talks to a Cloud Run service inside the VPC, and the
        # managed-system address carries that service's audience. The dashboard can now
        # DEPLOY that service itself (clouddb_dbops_service) — one per region — so
        # either a deployed service in this row's region or the config-key override
        # satisfies this. With neither there is no address to build, so the channel is
        # off rather than broken.
        channel = _gcp_channel(row.engine)
        if channel == "cloud-run" and not _dbops_audience(row, db):
            return False
        # data-api + SQL Server needs the address's fasecret= option, and there is no
        # sensible default for it: with no IAM database authentication the Data API has
        # to be given the functional account's password out of Secret Manager. Off
        # rather than broken, exactly as the cloud-run audience is above — an address
        # with an empty fasecret= is refused by the plugin's own pre-flight, and finding
        # that out from a rotation failure is the outcome this gate exists to avoid.
        if (channel == "data-api" and not _iam_db_auth(row.engine, channel)
                and not _dbgcp_fa_secret_available(row)):
            return False
        return True
    return False


# Clouds whose DB plugin the dashboard can actually drive. AWS = the "{engine} SSM
# Custom Plugin", reached with SendCommand to the shared Gateway host; Azure = the
# "{engine} Azure Run Command Plugin", reached with Run Command on the ref-counted
# clouddb-jumpoint VM; GCP = the "GCP Cloud SQL {engine}" plugins, which need no
# transport into the VPC at all — they reach a private-IP instance through Google's
# control plane (the Cloud SQL Data API). OCI has no equivalent, so there is nothing to
# onboard it WITH — not a switch that is off.
_PS_ONBOARDING_CLOUDS = ("aws", "azure", "gcp")

# Engines each cloud's plugin set can actually manage, as a STRUCTURAL fact. GCP used to
# restrict this to postgres+mysql because Cloud SQL for SQL Server has no IAM database
# authentication and the data-api channel would have needed the functional account's
# password mirrored into Secret Manager. The cloud-run channel removes that: the
# credential travels in the request body and Password Safe stays the sole authority. So
# SQL Server is now a CONFIGURATION question — is the Cloud Run service deployed and its
# audience known? — which _ps_db_onboarding_enabled answers, not this function.
_PS_ONBOARDING_ENGINES: dict = {}

# Which plugin channel drives each GCP engine. These are the plugin's own recommended
# defaults: postgres/mysql on data-api (zero infrastructure, and under IAM database auth
# zero stored secrets), sqlserver on cloud-run (no IAM database auth exists for it, so
# data-api would need Secret Manager as a second authority for the credential). An
# operator can override with clouddb_ps_gcp_channel.
_GCP_CHANNEL_DEFAULTS = {"postgres": "data-api", "mysql": "data-api",
                         "sqlserver": "cloud-run"}
_GCP_CHANNELS = ("data-api", "cloud-run")


def _gcp_channel(engine: str) -> str:
    """The plugin channel to drive for ``engine`` on GCP.

    ``clouddb_ps_gcp_channel`` is ``auto`` (per-engine default above) or an explicit
    channel.

    ``admin-api`` is a third channel the plugin implements and ``ps_resource_service``
    validates, but the dashboard does not EMIT it, and that is a considered choice
    rather than a gap: it needs ``cloudsql.users.update``, which among predefined roles
    lives only in the very broad ``roles/cloudsql.admin`` (against
    ``roles/cloudsql.instanceUser`` for data-api), it performs no database login at all
    so Verify proves only the GCP identity, and it reaches only the instance's user
    REGISTRY — a principal created inside the database with ``CREATE ROLE`` can be
    invisible to it.

    An unrecognised value is logged rather than silently swapped for the default. A
    channel name that parses everywhere else in the stack and then quietly does not
    apply is how an operator concludes a setting is broken."""
    choice = (_cfg("clouddb_ps_gcp_channel") or "auto").strip().lower()
    if choice in _GCP_CHANNELS:
        return choice
    default = _GCP_CHANNEL_DEFAULTS.get(engine, "data-api")
    if choice and choice != "auto":
        logger.warning(
            "clouddb: clouddb_ps_gcp_channel=%r is not a channel the dashboard can "
            "build an address for (%s) — using %r for engine %r. 'admin-api' is a valid "
            "plugin channel but is not emitted here; see _gcp_channel.",
            choice, "/".join(_GCP_CHANNELS), default, engine)
    return default


def _ps_ineligible_reason(row: CloudDatabase) -> Optional[str]:
    """``None`` if this database can be onboarded into Password Safe, else the reason it
    can't, worded for the user. One source of truth for the button (via
    :func:`_serialize`'s ``ps_viable``), the API pre-flight and the job — the same
    arrangement :func:`_entitle_ineligible_reason` has, and for the same reason: three
    places deciding independently is how a button ends up offering what the API rejects.

    STRUCTURAL blockers only — facts about the row that no amount of configuration
    changes. Whether the feature is switched on is the separate, configuration-time
    question :func:`_ps_db_onboarding_enabled` answers."""
    if (row.source or "provisioned") == "registered":
        return ("Password Safe onboarding is only supported on dashboard-provisioned "
                "databases. A registered database has no admin credential stored here "
                "to create the rotatable managed user with — onboard it in Password "
                "Safe directly, then use Import from Password Safe to record it.")
    if (row.cloud or "") not in _PS_ONBOARDING_CLOUDS:
        return (f"Password Safe onboarding needs a way into the private database — AWS "
                f"Systems Manager, Azure Run Command, or the GCP Cloud SQL Data API. "
                f"{(row.cloud or 'this cloud').upper()} has none of them, so there is no "
                f"supported path for it yet.")
    engines = _PS_ONBOARDING_ENGINES.get(row.cloud or "")
    if engines and (row.engine or "") not in engines:
        return (f"{(row.cloud or '').upper()} Password Safe onboarding covers "
                f"{' and '.join(engines)} only. Cloud SQL for SQL Server has no IAM "
                f"database authentication, so the functional account would need a stored "
                f"password mirrored into Secret Manager — a second authority for the "
                f"credential Password Safe exists to own.")
    return None


def _ps_onboarding_opted(meta: dict) -> bool:
    """The per-database Password Safe choice recorded on the provisioning job.

    Absent means "follow the configured default", which is simply the capability flag —
    i.e. exactly the behaviour before the provision form offered a checkbox. Resolved
    HERE, at apply time, rather than at provision time, so the answer reflects the
    settings as they are when the work actually runs."""
    opted = (meta or {}).get("register_in_passwordsafe")
    return True if opted is None else bool(opted)


def _managed_user_name(db_id: str) -> str:
    """A safe, per-database DB identifier for the dedicated managed user
    (letter-led, ``[A-Za-z0-9_]`` — see cloud_db_sql_service._IDENT_RE)."""
    return f"psafe_{db_id.replace('-', '')[:12]}"


async def _create_db_managed_user(db: Session, *, row: CloudDatabase, job_id: str,
                                  engine: str, tf_variables: dict) -> dict:
    """Create the dedicated managed DB user from the admin credential by running
    the DB client on the shared Gateway host over AWS SSM. Returns the onboarding
    context (managed user + password, jump host id, region, db name, admin user,
    client image). Raises on failure so the caller falls back to admin staging."""
    from . import aws_service, jumpoint_host_service
    from . import cloud_db_sql_service as sql
    region = _cfg(row.cloud + "_region") or row.region
    host_id = await jumpoint_host_service.ensure_jumpoint_host(row.cloud, region)
    if not host_id:
        raise CloudDatabaseError(
            "no SSM jump host available — the shared Gateway host must be up to run "
            "the DB client (check aws_ecs_docker_deploy_key + gateway config)")
    admin_username = (tf_variables.get("master_username")
                      or tf_variables.get("administrator_login") or "dbadmin")
    admin_password = (config_service.get(f"clouddb/{row.id}/admin")
                      or tf_variables.get("master_password") or "")
    # Admin-session catalog. For sqlserver this is doubly required: the statement
    # is CREATE LOGIN (server-level), and cloud_db_sql_service._mssql_command
    # hardcodes `-d master` anyway, so any other value here would be discarded.
    db_name = connection_db_name(row, tf_variables)
    managed_user = _managed_user_name(row.id)
    managed_pw = sql.generate_password()
    image = _cfg(f"clouddb_db_client_image_{engine}") or sql.default_client_image(engine)
    port = row.port or sql.default_port(engine)
    # Make the jump host plugin-ready. A separate SSM call from the onboard batch, as
    # on Azure, so a staging failure is distinguishable from a managed-user failure.
    # Short timeout: unlike Azure this installs nothing.
    prep = _ssm_jump_prep_commands()
    if prep:
        prep_res = await aws_service.ssm_send_command(region, host_id, prep, timeout=120)
        if prep_res.get("status") != "Success" or int(prep_res.get("response_code", -1)) != 0:
            raise CloudDatabaseError(
                f"jump-host plugin prep failed (status={prep_res.get('status')}, "
                f"rc={prep_res.get('response_code')}): {_run_detail(prep_res)}")
        logger.info("clouddb: staged plugin key material on %s db_id=%s", host_id, row.id)
    else:
        logger.warning("clouddb: clouddb_ps_ssm_plugin_private_key / "
                       "clouddb_ps_ssm_key_directory is blank — NOT staging plugin key "
                       "material on the jump host; the first AWS rotation will fail to "
                       "decrypt unless you placed it there by hand")
    cmds = sql.onboard_commands(
        engine, host=row.private_host, port=port,
        database=db_name, admin_user=admin_username, admin_password=admin_password,
        managed_user=managed_user, managed_password=managed_pw, client_image=image)
    result = await aws_service.ssm_send_command(region, host_id, cmds, timeout=300)
    if result.get("status") != "Success" or int(result.get("response_code", -1)) != 0:
        raise CloudDatabaseError(
            f"managed-user creation on the jump host failed "
            f"(status={result.get('status')}, rc={result.get('response_code')}): "
            f"{_run_detail(result)}")
    logger.info("clouddb: managed DB user %r created (or reset to this run's password) "
                "via SSM on %s db_id=%s", managed_user, host_id, row.id)
    return {"managed_user": managed_user, "managed_pw": managed_pw, "jump_host_id": host_id,
            "region": region, "db_name": db_name, "admin_username": admin_username,
            "client_image": image, "port": port}


def _run_detail(res: dict) -> str:
    """The reportable tail of an ssm_send_command / vm_run_command result.

    Never returns "", because a failure message that ends at the colon is the one
    shape an operator cannot act on or even search for — it says a remote script
    failed and then declines to say anything else."""
    return ((res.get("stderr") or res.get("stdout") or "").strip()[:400]
            or "the remote command returned no output")


def _plugin_key_drop_commands(kdir: str, private_key: str, passphrase: str) -> list:
    """Shell commands that drop the plugin RSA key material into ``kdir`` (dir 700,
    files 600) so a custom plugin can decrypt the RSA-wrapped login password.

    Shared by both clouds. The key and passphrase are base64-encoded here and decoded
    on the host, so no PEM newline or quote ever hits the shell literally."""
    import base64
    b64key = base64.b64encode(private_key.encode()).decode()
    b64pass = base64.b64encode(passphrase.encode()).decode()
    return [
        f"mkdir -p {kdir}",
        f"chmod 700 {kdir}",
        f"printf '%s' '{b64key}' | base64 -d > {kdir}/private.pem",
        f"printf '%s' '{b64pass}' | base64 -d > {kdir}/passphrase.txt",
        f"chmod 600 {kdir}/private.pem {kdir}/passphrase.txt",
    ]


def _ssm_jump_prep_commands() -> list:
    """Commands (run as root on the shared Gateway host over SSM) that make it ready
    for the "{engine} SSM Custom Plugin": drop the plugin RSA key material where the
    plugin reads it. The AWS counterpart of :func:`_azure_jump_prep_commands`.

    Unlike Azure this installs no DB clients. The dashboard's own managed-user creation
    runs the client as a ``docker run``, and the ECS gateway host is shared with the
    gateway workload, so what the plugin needs on its own PATH is the operator's call
    rather than something to guess at here.

    Returns ``[]`` when there is nothing to stage; the caller logs that, because an
    absent drop is a failure that only ever surfaces at the first rotation."""
    priv = _cfg("clouddb_ps_ssm_plugin_private_key")
    kdir = _cfg("clouddb_ps_ssm_key_directory")
    if not priv or not kdir:
        return []
    return _plugin_key_drop_commands(
        kdir.rstrip("/"), priv, _cfg("clouddb_ps_ssm_plugin_passphrase"))


_AZURE_CLIENT_INSTALL = {
    "postgres": ["command -v psql >/dev/null 2>&1 || "
                 "{ apt-get update && apt-get install -y postgresql-client; }"],
    "mysql": ["command -v mysql >/dev/null 2>&1 || "
              "{ apt-get update && apt-get install -y mysql-client; }"],
    "sqlserver": [
        "[ -x /opt/mssql-tools18/bin/sqlcmd ] || { "
        "curl -fsSL https://packages.microsoft.com/keys/microsoft.asc -o /etc/apt/trusted.gpg.d/microsoft.asc; "
        "curl -fsSL https://packages.microsoft.com/config/ubuntu/22.04/prod.list -o /etc/apt/sources.list.d/mssql-release.list; "
        "apt-get update; ACCEPT_EULA=Y apt-get install -y mssql-tools18 unixodbc-dev; }"],
}


def _azure_jump_prep_commands(engine: str = "") -> list:
    """Shell commands (run as root on the jump VM over Azure Run Command) that make
    it plugin-ready: ensure the native DB client THIS engine's plugin invokes is
    installed — idempotent, so an already-prepped or fresh (cloud-init) VM is a fast
    no-op, and a reused VM the head start missed gets it — and drop the plugin RSA
    key material to /root/psplugin so the "{engine} Azure Run Command Plugin" can
    decrypt the RSA-wrapped login password.

    Only this engine's client, because the script runs under ``set -e`` on a SHARED
    VM: installing all three made a PostgreSQL onboarding depend on the SQL Server
    toolchain — an external Microsoft apt repo, a GPG key and an EULA — and a
    stumble anywhere in that leg aborted the run before the key material was even
    staged. No engine named (no caller does that today) keeps the old all-three
    behaviour, matching the fresh-VM cloud-init head start."""
    cmds = list(_AZURE_CLIENT_INSTALL.get(engine) or
                [c for e in ("postgres", "mysql", "sqlserver")
                 for c in _AZURE_CLIENT_INSTALL[e]])
    priv = config_service.get("clouddb_ps_azure_plugin_private_key") or ""
    if priv:
        cmds += _plugin_key_drop_commands(
            "/root/psplugin", priv,
            config_service.get("clouddb_ps_azure_plugin_passphrase") or "")
    else:
        # Historically silent, and the single worst trap in this feature: prep reports
        # success, the DB provisions, the managed system registers, and only the first
        # rotation fails, with nothing in the job pointing here.
        logger.warning("clouddb: clouddb_ps_azure_plugin_private_key is blank — NOT "
                       "staging plugin key material on the jump VM; the first Azure "
                       "rotation will fail to decrypt")
    return cmds


async def _create_db_managed_user_azure(db: Session, *, row: CloudDatabase, job_id: str,
                                        engine: str, tf_variables: dict) -> dict:
    """Azure counterpart of :func:`_create_db_managed_user`: prep the shared
    ``clouddb-jumpoint`` VM for the plugin (native clients + RSA key material) and
    create the dedicated managed DB user from the admin credential by running the DB
    client there over Azure VM Run Command. Returns the onboarding context. Raises
    on failure so the caller falls back to admin staging."""
    from . import azure_service, jumpoint_host_service
    from . import cloud_db_sql_service as sql
    # The DATABASE's own region first, and the resource group resolved FROM it — the
    # flat `azure_resource_group` is the default region's, and both clouddb jump VMs
    # carry the SAME name, so a flat group silently addresses the default region's VM.
    # Proven live 2026-09-02: a westus2 MySQL onboarding ran its client on the centralus
    # jumpoint and failed with "Unknown MySQL server host … (-2)" — the database's
    # private DNS zone and VNet only exist in its own region. `rg` also rides into the
    # plugin address that _onboard_ps_managed_systems registers, so the same slip aims
    # every later rotation at the wrong VM as well.
    region = row.region or _cfg("azure_location")
    host = await jumpoint_host_service.ensure_jumpoint_host(row.cloud, region)
    if not host:
        raise CloudDatabaseError(
            "no Azure jump VM available — the shared clouddb-jumpoint VM must be up to "
            "run the DB client (check azure_aci_deploy_key + azure_jumpoint_subnet_id)")
    rg = resolve_region("azure", region)["resource_group"]
    admin_username = (tf_variables.get("administrator_login")
                      or tf_variables.get("master_username") or "dbadmin")
    admin_password = (config_service.get(f"clouddb/{row.id}/admin")
                      or tf_variables.get("administrator_password") or "")
    # Admin-session catalog. For sqlserver this is doubly required: the statement
    # is CREATE LOGIN (server-level), and cloud_db_sql_service._mssql_command
    # hardcodes `-d master` anyway, so any other value here would be discarded.
    db_name = connection_db_name(row, tf_variables)
    managed_user = _managed_user_name(row.id)
    managed_pw = sql.generate_password()
    image = _cfg(f"clouddb_db_client_image_{engine}") or sql.default_client_image(engine)
    port = row.port or sql.default_port(engine)
    # Make the jump VM plugin-ready (clients + key material). Longer timeout: a first
    # run on a VM the cloud-init head start missed installs packages.
    prep = _azure_jump_prep_commands(engine)
    prep_res = await azure_service.vm_run_command(rg, host, prep, timeout=600)
    if prep_res.get("status") != "Success":
        raise CloudDatabaseError(
            f"jump-VM plugin prep failed (status={prep_res.get('status')}, "
            f"rc={prep_res.get('response_code')}): {_run_detail(prep_res)}")
    cmds = sql.onboard_commands(
        engine, host=row.private_host, port=port,
        database=db_name, admin_user=admin_username, admin_password=admin_password,
        managed_user=managed_user, managed_password=managed_pw, client_image=image)
    result = await azure_service.vm_run_command(rg, host, cmds, timeout=300)
    if result.get("status") != "Success" or int(result.get("response_code", -1)) != 0:
        raise CloudDatabaseError(
            f"managed-user creation on the jump VM failed "
            f"(status={result.get('status')}, rc={result.get('response_code')}): "
            f"{_run_detail(result)}")
    logger.info("clouddb: managed DB user %r created via Azure Run Command on %s db_id=%s",
                managed_user, host, row.id)
    return {"managed_user": managed_user, "managed_pw": managed_pw, "jump_vm_name": host,
            "resource_group": rg, "region": region, "db_name": db_name,
            "admin_username": admin_username, "client_image": image, "port": port}


def _observed_iam_db_user(users: list, sa_email: str) -> str:
    """Pick, from a ``users.list`` response, the in-database name Cloud SQL actually
    stored for IAM service account ``sa_email``.

    Read rather than derive. The documented transform is "drop
    ``.gserviceaccount.com``" on PostgreSQL and "local part, lowercased, truncated to
    32" on MySQL, but there is in-house precedent for GCP surfacing an unexpected
    principal name: on GKE the Kubernetes username for a key-based service account
    turned out to be the numeric ``uniqueId`` rather than the email, which silently
    nullified a role binding. A functional account naming a principal the database
    does not have fails Verify with an unhelpful message, so trust the catalog.

    Ranked: exact email, then equal local parts, then a stored name that is a prefix
    of the local part (MySQL's 32-character cap)."""
    want = (sa_email or "").strip().lower()
    local = want.split("@")[0]
    exact = same_local = truncated = ""
    for u in users or []:
        if str(u.get("type") or "").upper() != "CLOUD_IAM_SERVICE_ACCOUNT":
            continue
        name = str(u.get("name") or "").strip()
        if not name:
            continue
        low = name.lower()
        if low == want:
            exact = name
        elif low.split("@")[0] == local:
            same_local = same_local or name
        elif local and local.startswith(low.split("@")[0]):
            truncated = truncated or name
    return exact or same_local or truncated


def _fa_grant_statement(engine: str, *, fa_db_user: str, managed_user: str,
                        managed_host: str) -> str:
    """The GRANT the functional account needs to rotate ``managed_user``.

    PostgreSQL 16 — which is the module default, not an edge case — dropped the rule
    that ``CREATEROLE`` alone lets a role administer another, so this is per-role
    ``ADMIN OPTION``. That is also the safer grant: a compromised functional account
    can reset only the accounts Password Safe manages."""
    if engine == "postgres":
        return f'GRANT "{managed_user}" TO "{fa_db_user}" WITH ADMIN OPTION;'
    if engine == "mysql":
        # No per-user equivalent of ADMIN OPTION here. Do NOT grant UPDATE on mysql.* —
        # Cloud SQL restricts DML on mysql.user.
        return f"GRANT CREATE USER ON *.* TO '{fa_db_user}'@'%';"
    if engine == "sqlserver":
        # ALTER ANY LOGIN is exactly enough and is what CustomerDbRootRole carries;
        # sysadmin is unavailable on Cloud SQL. Only needed when the functional account
        # drives the change — under self-rotation the login alters itself with
        # OLD_PASSWORD and needs no permission at all.
        return f"ALTER SERVER ROLE CustomerDbRootRole ADD MEMBER [{fa_db_user}];"
    return ""


def _fa_login_statement(engine: str, *, fa_db_user: str) -> str:
    """The statement that CREATES the functional account's own database login, or ``""``.

    The one credential in this feature the dashboard cannot issue: the login's password
    is the third ``:``-segment of the functional account's password in Password Safe, and
    Password Safe never returns a functional account's password over the API. So the
    dashboard can name the statement but not run it, which is why this is a job-log line
    and not a call."""
    if engine == "postgres":
        return (f'CREATE ROLE "{fa_db_user}" LOGIN PASSWORD '
                f"'<the functional account password's third :-segment>';")
    if engine == "mysql":
        return (f"CREATE USER '{fa_db_user}'@'%' IDENTIFIED BY "
                f"'<the functional account password's third :-segment>';")
    if engine == "sqlserver":
        return (f"CREATE LOGIN [{fa_db_user}] WITH PASSWORD = "
                f"'<the functional account password's third :-segment>';")
    return ""


def _fa_discovery_grant_statement(engine: str, *, fa_db_user: str,
                                  fa_host: str = "%") -> str:
    """The extra grant ACCOUNT DISCOVERY needs on MySQL, or ``""``.

    ``CREATE USER`` confers no read of the account catalogue, so a functional account
    that can rotate every account on the instance still cannot ENUMERATE them: Discovery
    comes back as MySQL 1142. This is the one grant the rotation ladder does not already
    cover, and it is read-only — it adds "and can list account names" to "a compromised
    rotation identity can change passwords and nothing else", which is the weakest
    widening available.

    There is no privilege-free substitute. ``information_schema.USER_PRIVILEGES`` is
    derived from ``mysql.user`` and shows other principals' rows only to a caller that
    already has SELECT on it, and ``performance_schema.accounts`` sees only accounts that
    have CONNECTED and reports the client's connection host rather than the account's
    host qualifier — which breaks the ``user@host`` round-trip that makes a discovered
    account rotatable in the first place.

    PostgreSQL needs nothing: ``pg_roles`` is world-readable. SQL Server needs nothing
    either — ``sys.server_principals`` is readable by any login for its own rows and by
    ``CustomerDbRootRole`` for all of them, which the rotation grant already joins.
    """
    if engine != "mysql" or not fa_db_user:
        return ""
    # Do NOT reach for UPDATE ON mysql.* as a shortcut: Cloud SQL restricts DML on
    # mysql.user, and it would not help — this is a read.
    return f"GRANT SELECT ON mysql.user TO '{fa_db_user}'@'{fa_host or '%'}';"


def _iam_db_auth(engine: str, channel: str) -> bool:
    """Whether this (engine, channel) authenticates the DATABASE session with an IAM
    token rather than a stored password.

    The single fact three separate decisions used to spell out independently, and got
    wrong together for SQL Server: whether to enable the IAM-database-authentication
    flag, whether the functional account's database user is an IAM principal or a real
    login, and whether the address carries ``iam=true`` or ``fasecret=``.

    Cloud SQL for SQL Server supports IAM authentication for instance and backup
    operations only, **never for database operations** — so on the control plane it is
    the one engine that must take the stored-password route, and the plugin *rejects*
    ``iam=`` on it rather than ignoring it.
    """
    return channel == "data-api" and engine != "sqlserver"


def _fa_secret_resource_name(row_id: str) -> str:
    """The NAME of the regional secret holding the functional account's database
    password — not the password.

    Distinct from :func:`_grant_secret_id`, and the difference is lifetime: that one
    holds the master credential for the length of a single statement and is deleted in
    a ``finally``, while this one has to OUTLIVE the onboarding — the plugin reads it on
    every rotation. Sharing the id would have the grant's cleanup delete the credential
    the managed system depends on, on the way out of a successful onboarding.

    Named ``..._resource_name`` because the teardown path logs the value, and a name
    containing "secret" makes CodeQL read the log line as leaking the credential (see
    the note in :func:`_teardown_ps_onboarding`).
    """
    return f"clouddb-{row_id}-psfa"


def _fa_secret_version_configured() -> str:
    """The operator-staged functional-account secret version, if any."""
    return _cfg("clouddb_ps_gcp_fa_secret_version")


def _dbgcp_fa_secret_available(row: CloudDatabase) -> bool:
    """Whether a ``fasecret=`` value can be produced for this row.

    Cheap and side-effect-free, so the onboarding gate can ask before anything is
    created. It answers "is there a route to one", not "does one exist yet": in
    ``create`` mode the dashboard stages the credential it minted, which is a route
    even before the secret is written.
    """
    if _fa_secret_version_configured():
        return True
    # "reference" mode names an account whose password the dashboard has never seen, so
    # there is nothing to stage and the config key is the only source. A registered
    # database has no minted admin credential either.
    return (_ps_fa_mode(row.engine) != _FA_MODE_REFERENCE
            and (getattr(row, "source", None) or "provisioned") != "registered")


async def _stage_fa_secret_gcp(db: Session, *, row: CloudDatabase, job_id: str,
                               project: str, region: str, password: str) -> str:
    """Mirror the functional account's database password into a REGIONAL secret and
    return the version resource name for ``fasecret=``. ``""`` on failure.

    This is the mirror the ``cloud-run`` channel exists to avoid, and it is written
    down as such on the job rather than done quietly: Password Safe is meant to be the
    sole authority for this credential, and after this there are two. Nothing re-syncs
    the copy, so rotating the functional account itself breaks every subsequent
    rotation of the accounts it manages until the secret is updated by hand.

    It is still the right thing to offer, because the alternative for SQL Server on the
    control plane is no address at all. The recommendation stays cloud-run.
    """
    from . import gcp_service

    if not (project and region and password):
        return ""
    resource_id = _fa_secret_resource_name(row.id)
    try:
        version = await gcp_service.write_regional_secret(
            project, region, resource_id, password)
    except Exception as exc:  # noqa: BLE001
        logger.warning("clouddb: could not stage the functional-account secret for %s: %s",
                       row.id, exc)
        job_service.append_job_log(
            db, job_id,
            f"Could not stage the functional account's password in a regional Secret "
            f"Manager secret ({exc}) — the data-api SQL Server address needs one "
            f"(fasecret=), so this database cannot be onboarded on that channel. Use "
            f"the cloud-run channel, or stage the secret yourself and set "
            f"clouddb_ps_gcp_fa_secret_version.")
        return ""
    job_service.append_job_log(
        db, job_id,
        f"Mirrored the functional account's password into the regional secret "
        f"{resource_id} ({region}) for the data-api address's fasecret= option. "
        f"NOTE: Password Safe is no longer the sole authority for this credential — "
        f"nothing re-syncs the copy, so rotating the functional account itself will "
        f"break rotations until you update the secret. The cloud-run channel avoids "
        f"this entirely.")
    return version


def _grant_secret_id(row_id: str) -> str:
    """Regional-secret id for the one-shot admin credential the grant runs as.

    Deliberately NOT ``cloud_db_adapter_service.secret_key``'s ``clouddb-<id>-admin``:
    that secret is the db_grant adapter's long-lived credential, and this one is deleted
    the moment the statement returns. Sharing the id would make onboarding a database
    revoke a paired adapter's password."""
    return f"clouddb-{row_id}-psgrant"


async def _apply_fa_grant_gcp(db: Session, *, row: CloudDatabase, job_id: str,
                              engine: str, project: str, instance: str, region: str,
                              database: str, admin_username: str, admin_password: str,
                              grant: str, purpose: str = "rotation grant") -> bool:
    """Issue the functional account's GRANT ourselves, as the built-in admin, over the
    Data API. Returns whether it was applied.

    ``executeSql`` authenticates as the *caller*, and the dashboard's own service
    account is not a privileged database principal — which is why this cannot simply be
    an ``autoIamAuthn`` call, and why this step used to be reported for an operator to
    run by hand. The way through is the Data API's other authentication mode: a built-in
    ``user`` whose password comes from Secret Manager. The dashboard already holds this
    database's admin credential (it minted it), so it can stage it, execute one
    statement as that admin, and take it back out again.

    The secret is REGIONAL and is deleted in ``finally``. Both matter:

    * The Data API rejects the global secret form outright — including the exact
      ``fasecret=projects/<p>/secrets/<s>/versions/latest`` shape the plugin article
      documents — with "does not match the expected format
      [projects/*/locations/*/secrets/*/versions/*]".
    * Leaving it behind would park the database's master password in Secret Manager
      permanently to save one API call on a path that runs once per database.

    Never raises. A failure here leaves the database and the managed user exactly as
    they were and falls back to reporting the statement on the job, because a grant the
    operator can still run by hand is a far better outcome than an onboarding that
    unwinds itself over a permissions problem on one statement."""
    from . import gcp_service

    if not (grant and admin_password and admin_username and region):
        return False
    secret_id = _grant_secret_id(row.id)
    version = ""
    try:
        version = await gcp_service.write_regional_secret(
            project, region, secret_id, admin_password)
        await gcp_service.execute_cloudsql_sql(
            project, instance, database, grant,
            auto_iam_authn=False, user=admin_username,
            password_secret_version=version)
    except Exception as exc:  # noqa: BLE001
        # The statement, not the exception, is what the operator needs: this is the one
        # step whose manual fallback is a single line they can paste.
        logger.warning("clouddb: could not apply the PS %s on %s db_id=%s: %s",
                       purpose, instance, row.id, exc)
        job_service.append_job_log(
            db, job_id,
            f"Could not apply the Password Safe {purpose} automatically ({exc}) — "
            f"run it as an admin on {database}: {grant}")
        return False
    finally:
        if version:
            await gcp_service.delete_regional_secret(project, region, secret_id)

    job_service.append_job_log(
        db, job_id,
        f"Applied the Password Safe {purpose} as {admin_username} on "
        f"{database}: {grant}")
    logger.info("clouddb: applied PS %s on %s db_id=%s engine=%s",
                purpose, instance, row.id, engine)
    return True


async def _create_db_managed_user_gcp(db: Session, *, row: CloudDatabase, job_id: str,
                                      engine: str, tf_variables: dict) -> dict:
    """GCP counterpart of :func:`_create_db_managed_user`, and much the simplest of the
    three: **there is no jump host.**

    AWS runs the DB client on the ECS gateway over SSM and Azure runs it on the shared
    jump VM over Run Command, because neither has line-of-sight to a private database
    any other way. GCP does: ``users.insert`` on the Cloud SQL Admin API creates the
    dedicated managed user with no database connection at all, so a private-IP instance
    needs no relay, no peering and no client image. This also means the whole jump-host
    apparatus the other two carry — client images, the RSA key pair, the key-drop
    commands — has no counterpart here.

    Returns the onboarding context. Raises on failure so the caller falls back to admin
    staging."""
    from . import gcp_service
    from . import cloud_db_sql_service as sql
    project = (tf_variables.get("project") or _cfg("gcp_project")
               or _cfg("gcp_project_id"))
    instance = row.instance_id
    if not project or not instance:
        raise CloudDatabaseError(
            f"GCP Cloud SQL onboarding needs both a project ({project!r}) and an "
            f"instance name ({instance!r}) — the instance connection name is built from "
            f"them plus the region")
    region = row.region or _cfg("gcp_region")
    db_name = connection_db_name(row, tf_variables)
    admin_username = tf_variables.get("master_username") or "dbadmin"
    port = row.port or sql.default_port(engine)
    channel = _gcp_channel(engine)

    # Which authentication the DATABASE session uses on this channel. One fact, three
    # consumers below — see _iam_db_auth for why SQL Server is the exception.
    iam_db_auth = _iam_db_auth(engine, channel)

    # 1. Per-instance prerequisites, but ONLY for the control-plane channel. cloud-run
    #    opens a real database connection from a service inside the VPC, so it needs
    #    neither the Data API nor IAM database authentication. On data-api the Data API
    #    itself is always needed; the IAM flag is passed through iam_db_auth, because
    #    Cloud SQL for SQL Server has no IAM database auth to enable in the first place
    #    and asking for it is a patch that cannot succeed. Patching an instance for a
    #    channel that will not use it is a change we have no reason to make.
    if channel == "data-api":
        await gcp_service.ensure_cloudsql_rotation_prereqs(project, instance,
                                                           iam_auth=iam_db_auth)

    # 2. The dedicated managed user (the rotation target — never the master admin).
    #    A MySQL account is identified by user@host, and cloud_db_sql_service's own
    #    MySQL path creates '<user>'@'%', so match it here and carry the host forward:
    #    the plugin refuses to assume one.
    managed_user = _managed_user_name(row.id)
    managed_pw = sql.generate_password()
    managed_host = "%" if engine == "mysql" else ""
    await gcp_service.create_cloudsql_user(project, instance, managed_user,
                                           password=managed_pw, host=managed_host)

    # 3. Who the functional account authenticates to the DATABASE as.
    #
    #    On data-api that is an IAM database user, so the functional account needs no
    #    stored database password at all — its credential is a short-lived OAuth token
    #    minted per connection. Register the rotator, then read back the name the
    #    database actually stored (see _observed_iam_db_user).
    #
    #    Everywhere else the functional account is a real database login with a real
    #    password: on cloud-run because the service opens a genuine connection, and on
    #    data-api + SQL SERVER because that engine has no IAM database authentication at
    #    all. In "create" mode that login is the built-in admin this database was
    #    provisioned with — which does mean the composite carries a PER-DATABASE
    #    password, exactly the property that makes "reference" mode the better answer
    #    here, as it is on Azure.
    #
    #    The two differ in WHERE the password goes. cloud-run puts it in the composite,
    #    so Password Safe stays the sole authority. data-api cannot: the Data API reads
    #    it from Secret Manager, named by the address's fasecret= option — a second
    #    authority for the credential, staged in step 5.
    rotator = _cfg("clouddb_ps_gcp_rotator_service_account")
    fa_db_user = ""
    if not iam_db_auth:
        fa_db_user = admin_username
    elif rotator:
        await gcp_service.create_cloudsql_user(project, instance, rotator,
                                               iam_service_account=True)
        try:
            fa_db_user = _observed_iam_db_user(
                await gcp_service.list_cloudsql_users(project, instance), rotator)
        except Exception as exc:
            logger.warning("clouddb: could not read back the IAM database user for %s "
                           "on %s: %s", rotator, instance, exc)
        if not fa_db_user:
            fa_db_user = rotator.split(".gserviceaccount.com")[0]
            logger.warning("clouddb: IAM database user for %s not found in users.list on "
                           "%s — falling back to the derived name %r, which Verify will "
                           "reject if the database stored something else",
                           rotator, instance, fa_db_user)

    # 4. The functional account still needs rights over the managed principal. That is
    #    SQL, and executeSql authenticates as the CALLER, so the dashboard cannot issue
    #    it as itself. It CAN issue it as the built-in admin whose credential it minted,
    #    which is what _apply_fa_grant_gcp does — one statement, over a regional secret
    #    that is deleted again immediately. Reporting the statement for an operator to
    #    run by hand stays as the fallback, never as the first answer.
    #    Self-rotation needs none of it: the managed account authenticates as itself and
    #    alters itself, which is the strongest argument for turning it on.
    admin_password = (tf_variables.get("master_password")
                      or tf_variables.get("administrator_password")
                      or tf_variables.get("admin_password")
                      or config_service.get(f"clouddb/{row.id}/admin") or "")
    self_rotating = (channel == "cloud-run"
                     and config_service.get_bool("clouddb_ps_self_rotation", False))
    # Nothing to grant when the functional account IS the built-in admin: it already
    # carries every right over every principal on the instance, and issuing the
    # statement anyway asks the admin to add itself to a role it is already in.
    fa_is_admin = bool(fa_db_user) and fa_db_user == admin_username
    grant = "" if (self_rotating or fa_is_admin) else _fa_grant_statement(
        engine, fa_db_user=fa_db_user, managed_user=managed_user,
        managed_host=managed_host)
    # 4a. Before the grant, the prerequisite the grant silently assumes: the functional
    #     account's own DB login has to EXIST on this server, with the password Password
    #     Safe holds. The dashboard creates only the managed user and cannot create this
    #     one — it never learns that password (§0 of the runbook). Reported whether or
    #     not there is a grant to go with it, because Verify Functional Account logs in
    #     as this login on every managed system, self-rotation included: under
    #     self-rotation the grant is skipped and this line would otherwise be the only
    #     thing an operator never gets told. Live 2026-09-02, Azure postgres: a missing
    #     login fails Verify as "FATAL: password authentication failed for user
    #     '<fa>'" — identical to a wrong password, because PostgreSQL and MySQL both
    #     answer that for a role that does not exist. So the error cannot distinguish
    #     the two, and naming both here is the only place that can.
    login_stmt = _fa_login_statement(engine, fa_db_user=fa_db_user)
    if login_stmt and fa_db_user and not fa_is_admin:
        job_service.append_job_log(
            db, job_id,
            f"Password Safe's functional account signs in to this database as "
            f"{fa_db_user!r}, which the dashboard cannot create — it does not have that "
            f"password. Create it as an admin, with the password matching the third "
            f"':'-segment of the functional account's password in Password Safe: "
            f"{login_stmt} Until then every action on this managed system fails as "
            f"\"password authentication failed for user '{fa_db_user}'\" — the same "
            f"error a wrong password gives, so it does not tell you which it is.")
    if grant and fa_db_user:
        applied = False
        if channel == "data-api":
            # Only here is the Data API known to be on — step 1 enables it for this
            # channel and deliberately does not for cloud-run. Turning it on merely to
            # issue the grant would change the instance for a channel that will never
            # use it, which is the change that comment declines to make.
            applied = await _apply_fa_grant_gcp(
                db, row=row, job_id=job_id, engine=engine, project=project,
                instance=instance, region=region,
                # db_name is already connection_db_name's answer, which resolves SQL
                # Server to master on its own — ALTER SERVER ROLE is server-scoped and
                # lives there. Re-deciding that here would be a sixth inline copy of the
                # ternary that resolver exists to have exactly one of.
                database=db_name,
                admin_username=admin_username, admin_password=admin_password,
                grant=grant)
        if not applied:
            job_service.append_job_log(
                db, job_id,
                f"Password Safe rotation needs one grant on this database, which the "
                f"dashboard could not issue itself — run it as an admin: {grant}")

    # 4b. ACCOUNT DISCOVERY on MySQL needs a SECOND grant, and it is issued as a second
    #     statement rather than appended to the one above. Deliberately: whether Cloud
    #     SQL permits SELECT on mysql.user at all is still unconfirmed against a live
    #     instance, and packing the two into one call would let that open question take
    #     the ROTATION grant down with it. Rotation is the feature; Discovery is the
    #     convenience. So this runs last, its failure is reported as its own remedy, and
    #     onboarding continues either way.
    #     Nothing to do when the functional account is the built-in admin, which already
    #     reads the catalogue, and nothing to do off data-api — cloud-run enumerates
    #     accounts through the DB-Ops service as its own login.
    discovery_grant = "" if fa_is_admin else _fa_discovery_grant_statement(
        engine, fa_db_user=fa_db_user, fa_host="%")
    if discovery_grant and fa_db_user and channel == "data-api":
        if not await _apply_fa_grant_gcp(
                db, row=row, job_id=job_id, engine=engine, project=project,
                instance=instance, region=region, database=db_name,
                admin_username=admin_username, admin_password=admin_password,
                grant=discovery_grant, purpose="account-discovery grant"):
            job_service.append_job_log(
                db, job_id,
                f"Account Discovery on this MySQL instance needs one further grant, "
                f"which the dashboard could not issue itself. Rotation and Verify are "
                f"unaffected and work without it; Discovery returns MySQL 1142 until it "
                f"is applied — run it as an admin: {discovery_grant}")

    # 5. data-api + SQL Server only: the address's fasecret= option. With no IAM
    #    database authentication the Data API has to be handed the functional account's
    #    password out of Secret Manager, so unlike every other GCP path there is a
    #    credential to stage. An operator-supplied version wins — in "reference" mode it
    #    is the only possible source, because the dashboard has never seen that
    #    account's password.
    fa_secret_version = ""
    if channel == "data-api" and not iam_db_auth:
        fa_secret_version = _fa_secret_version_configured()
        if not fa_secret_version and _ps_fa_mode(engine) != _FA_MODE_REFERENCE:
            fa_secret_version = await _stage_fa_secret_gcp(
                db, row=row, job_id=job_id, project=project, region=region,
                password=admin_password)

    logger.info("clouddb: managed DB user %r created via Cloud SQL users.insert on %s "
                "db_id=%s channel=%s iam_db_auth=%s (no jump host)",
                managed_user, instance, row.id, channel, iam_db_auth)
    return {"managed_user": managed_user, "managed_pw": managed_pw,
            "managed_user_host": managed_host, "project": project, "instance": instance,
            "fa_db_user": fa_db_user, "region": region, "db_name": db_name,
            "admin_username": admin_username, "client_image": "", "port": port,
            "channel": channel, "iam_db_auth": iam_db_auth,
            "fa_secret_version": fa_secret_version}


_FA_MODE_REFERENCE = "reference"

# The "{engine} SSM Custom Plugin" address's assumeRole segment is Substring(0,12)'d
# unconditionally by the plugin (that is how an "arn:aws:iam:" prefix is detected), so
# anything shorter crashes every credential action. The placeholder is exactly 12
# characters, which is also the minimum ps_resource_service._DBSSM_MIN_ASSUME_ROLE_LEN
# enforces — the round-trip test pins the two modules together.
_DBSSM_ASSUME_ROLE_PLACEHOLDER = "NoAssumeRole"


def _dbssm_assume_role() -> str:
    """The address's assumeRole segment: a full IAM role ARN for the cross-account
    EC2-broker mode, else the placeholder. A configured value too short for the
    plugin's ``Substring(0,12)`` is coerced rather than shipped — ``local`` was this
    key's own pre-spec default, so rows persisted under it are healed here instead of
    crashing the first rotation with "Index and length must refer to a location
    within the string"."""
    role = (_cfg("clouddb_ps_ssm_account_suffix") or "").strip()
    if len(role) >= len(_DBSSM_ASSUME_ROLE_PLACEHOLDER):
        return role
    if role and role.lower() != "local":
        logger.warning(
            "clouddb: clouddb_ps_ssm_account_suffix %r is shorter than 12 characters, "
            "which crashes the SSM DB plugin (Substring(0,12)) — using %r; set a full "
            "IAM role ARN or leave the field blank", role,
            _DBSSM_ASSUME_ROLE_PLACEHOLDER)
    return _DBSSM_ASSUME_ROLE_PLACEHOLDER


def _dbssm_fa_fields(*, admin_user: str, admin_password: str,
                     access_key_id: str, secret_access_key: str) -> tuple:
    """Functional-account ``(username, password)`` for the "{engine} SSM Custom Plugin".

    Username is ``<mode>:<dbAdminUser>`` and the password is ALWAYS the three-part
    ``<accessKeyId>:<secretAccessKey>:<dbAdminPassword>`` — the plugin splits both
    BEFORE it looks at the mode, so EC2 mode (no access-key pair configured) still
    ships the two ``x`` placeholders. The mode is selected by the key pair's presence:
    with both, parts 1–2 are the credentials Password Safe calls AWS with (IAM mode);
    without, the broker host's own AWS credentials are used and parts 1–2 are ignored.
    The DB admin credential rides part 3 in both modes — Verify Functional Account
    logs into the database with it, and the mssql/mysql managed-account change ships
    ``<newMaPwd>;<faDbPwd>`` through it."""
    mode = "IAM" if (access_key_id and secret_access_key) else "EC2"
    for label, value in (("DB admin username", admin_user),
                         ("AWS access key id", access_key_id),
                         ("AWS secret access key", secret_access_key),
                         ("DB admin password", admin_password)):
        if ":" in (value or ""):
            raise CloudDatabaseError(
                f"the {label} contains ':', the SSM DB plugin's functional-account "
                f"field delimiter — the credential would mis-split at every "
                f"verify/change action")
    return (f"{mode}:{admin_user}",
            f"{access_key_id or 'x'}:{secret_access_key or 'x'}:{admin_password}")


# ── Referenced functional-account name grammar ────────────────────────────────
#
# Every DB plugin parses its functional account POSITIONALLY, exactly the way it parses
# the managed-system address. All four "{engine} SSM Custom Plugin" v24.2.x actions open
# with the same three unguarded splits:
#
#   accountName.Split(':')[1]  -> the DB login the plugin exports as PGUSER
#   password.Split(':')[0..1]  -> the AWS access key pair (ignored in EC2 mode)
#   password.Split(':')[2]     -> that DB login's password
#
# so a functional account whose NAME carries no ':' fails EVERY action — Verify FA,
# Verify MA, Change FA, Change MA — with "Index was outside the bounds of the array",
# thrown inside the plugin long after onboarding reported success. Seen live 2026-08-27:
# a reference-mode account named 'psafe-clouddb-ssm' (a perfectly good IAM user name,
# and nothing else) onboarded green and then failed every credential action on the
# managed system it was attached to.
#
# "create" mode composes both fields itself (_dbssm_fa_fields above), so it cannot get
# this wrong. "reference" mode takes the operator's account as-is, which is exactly why
# the name is checked here — before Terraform runs, while the error can still name the
# field to fix. The PASSWORD cannot be checked: Password Safe never returns a functional
# account's password over the API, so its contract is stated in the error text instead.
_FA_NAME_SHAPES = {
    # label -> (format the plugin parses, prefixes it compares with EXACT case)
    "ssm":   ("<EC2|iamUserName>:<dbLogin>", ("EC2",)),
    "azure": ("<SP|MSI>:<dbLogin>", ("SP", "MSI")),
    "gcp":   ("<ADC|IMP|SA>:<dbLogin>", ("ADC", "IMP", "SA")),
}
_FA_PASSWORD_SHAPES = {
    "ssm": "<accessKeyId>:<secretAccessKey>:<dbLoginPassword>' (EC2 mode uses "
           "'x:x:<dbLoginPassword>')",
    "azure": "<clientId>:<clientSecret>:<dbLoginPassword>' (MSI mode uses "
             "'-:-:<dbLoginPassword>')",
    "gcp": "-:<impersonationTarget|->:<dbLoginPassword|->'",
}


def _validate_referenced_fa_name(account_name: str, *, label: str, config_key: str) -> None:
    """Raise CloudDatabaseError unless ``account_name`` is a name the plugin will parse.

    Grammar only, and only for the three DB plugin families — the PRA Vault platform
    takes a plain account name and is deliberately absent from ``_FA_NAME_SHAPES``.

    The DB login half is required for ``ssm``/``azure`` because the plugin exports it as
    the DB client's user: empty, ``psql``/``mysql``/``sqlcmd`` silently falls back to the
    jump host's OS user and the rotation fails as an authentication error somewhere far
    from its cause. GCP's IAM-auth channels legitimately carry no DB login, so only the
    delimiter and the mode prefix are required there."""
    shape = _FA_NAME_SHAPES.get(label)
    if not shape:
        return
    fmt, exact_prefixes = shape
    name = (account_name or "").strip()
    prefix, sep, db_login = name.partition(":")
    if not sep or not prefix or (not db_login.strip() and label != "gcp"):
        raise CloudDatabaseError(
            f"Password Safe functional account {name!r} is not a name the {label} "
            f"database plugin can parse: it must be '{fmt}'. The plugin reads the DB "
            f"login as accountName.Split(':')[1] with no bounds check, so this account "
            f"fails every credential action — Verify Functional Account included — with "
            f"\"Index was outside the bounds of the array\". Rename the account in "
            f"BeyondInsight (its password must be "
            f"'{_FA_PASSWORD_SHAPES.get(label, '<part1>:<part2>:<dbLoginPassword>')}), "
            f"or point {config_key} at one that already has the right shape.")
    for want in exact_prefixes:
        if prefix != want and prefix.upper() == want.upper():
            raise CloudDatabaseError(
                f"Password Safe functional account {name!r} spells its mode prefix "
                f"{prefix!r}, but the {label} database plugin compares it to {want!r} "
                f"with exact case. {prefix!r} therefore selects a DIFFERENT mode "
                f"silently — no error, just the wrong credential — so rename the "
                f"account to {want}:{db_login} (or set {config_key} to an account that "
                f"spells the prefix correctly).")


def _ps_fa_mode(engine: str = "") -> str:
    """Where the DB plugin's functional account comes from: ``"reference"`` (the
    operator created it and named it in config -- VM/k8s parity) or ``"create"``
    (mint one per database from the configured credential material -- legacy default).
    An explicit choice, never inferred from a blank field.

    Resolved PER ENGINE, falling back to the global key. The two modes are not a
    preference, they are a consequence of whether the functional account carries a
    PER-DATABASE secret:

    * Where it does not -- GCP ``data-api`` under IAM database authentication, whose
      composite is ``-:-:-`` -- one operator-owned account serves every database, and
      ``reference`` is the better answer for the same reason it is on the VM and k8s
      paths: nothing per-database is stored, and decommission deletes nothing.
    * Where it does -- GCP ``cloud-run``, which opens a real database connection and
      needs a real login, and the Azure Run Command plugins -- a single shared account
      cannot hold N databases' passwords, so the dashboard must mint one per database.

    SQL Server on Cloud SQL is the case that forces this to be per-engine: it has no
    IAM database authentication, so it lands on ``cloud-run`` and needs ``create``,
    while PostgreSQL and MySQL on the same cloud want ``reference``. A single global
    switch cannot express that, and picking either one breaks the other engines."""
    engine = (engine or "").strip().lower()
    per_engine = _cfg(f"clouddb_ps_functional_account_mode_{engine}") if engine else ""
    return (per_engine or _cfg("clouddb_ps_functional_account_mode")
            or "create").strip().lower()


async def _resolve_db_functional_account(*, mode: str, config_key: str, platform_id: int,
                                         platform_tokens: tuple, label: str,
                                         create: dict) -> tuple:
    """Resolve the functional account to onboard against ->
    ``(functional_account_id, platform_id, owned)``.

    ``mode`` is passed in rather than read here, because the two accounts this resolves
    do not share one: the DB account's mode is per-engine (see :func:`_ps_fa_mode`),
    while the PRA Vault account belongs to the appliance and has no engine at all.
    Reading a per-engine key in here would silently make the Vault account follow SQL
    Server's answer.

    ``reference`` mode RESOLVES an account the operator created in BeyondInsight,
    exactly as ``ps_vm_hook.register`` and ``ps_k8s_token_service`` do -- the account is
    never created and never deleted, and the managed system takes ITS platform, because
    the functional account is the thing that binds the plugin. ``owned`` comes back
    False, and the caller must not stash an id the decommission path would then delete.

    ``create`` mode mints one per database from ``create`` (the credential material off
    the settings panel) and ``owned`` is True.

    ``platform_id`` is therefore only consulted in ``create`` mode; callers pass 0 in
    ``reference`` mode rather than resolving a platform name they will not use.

    A misconfigured name raises. The caller's onboarding block is best-effort, so the
    raise only warns and leaves the job green -- hence the ``logger.error``, which is
    the one durable, greppable trace of an operator error that produced no artifacts.
    """
    from . import ps_api_service, ps_vm_hook
    if (mode or "").strip().lower() != _FA_MODE_REFERENCE:
        fa_id = await ps_api_service.create_functional_account_on_platform(
            platform_id=platform_id, **create)
        return int(fa_id), int(platform_id), True

    name = _cfg(config_key)
    if not name:
        logger.error("clouddb: functional-account mode is %r but %s is blank -- no %s "
                     "functional account to onboard against",
                     _FA_MODE_REFERENCE, config_key, label)
        raise CloudDatabaseError(
            f"the functional-account mode for this database is {_FA_MODE_REFERENCE!r} "
            f"but no {label} functional account is configured -- set {config_key} to the "
            f"name of the account you created in Password Safe, or switch the mode back "
            f"to 'create' to have the dashboard mint one per database. The mode comes "
            f"from clouddb_ps_functional_account_mode_<engine> if that is set, otherwise "
            f"clouddb_ps_functional_account_mode")
    fa = await ps_api_service.get_functional_account(name)
    pname = fa.get("platform_name") or ""
    if not ps_vm_hook._platform_name_ok(pname, *platform_tokens):
        logger.error("clouddb: %s functional account %r is on platform %r, not a %r "
                     "platform (%s)", label, name, pname, " ".join(platform_tokens), config_key)
        raise CloudDatabaseError(
            f"functional account {name!r} is on platform {pname!r}, not a "
            f"{' '.join(platform_tokens)!r} platform -- the managed system would land on "
            f"the wrong platform. Point {config_key} at the functional account you "
            f"created on your {label} plugin platform.")
    # The platform check above proves the account is attached to the right PLUGIN; this
    # proves the plugin can actually parse it. Both are needed, and only this one catches
    # the failure that survives onboarding: a name with no ':' registers fine and then
    # fails every credential action from inside the plugin (see _FA_NAME_SHAPES).
    _validate_referenced_fa_name(fa.get("account_name") or "",
                                 label=label, config_key=config_key)
    logger.info("clouddb: using operator-created %s functional account %r "
                "(id=%s name=%r platform=%r) -- not created here, not deleted on "
                "decommission", label, name, fa["id"], fa.get("account_name"), pname)
    return int(fa["id"]), int(fa["platform_id"]), False


def _stash_on_job(db: Session, job_id: str, update: dict) -> None:
    """Merge ``update`` into a job's metadata and commit. The Password Safe onboarding
    calls this after EACH remote object it creates rather than once at the end, so a
    later failure can never strand an object with nothing recorded to remove it."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if job is None:
        logger.warning("clouddb: job %s is gone — cannot record %s",
                       job_id, sorted(update))
        return
    meta = job.metadata_dict or {}
    meta.update(update)
    job.metadata_dict = meta
    db.commit()


async def _admit_instance_to_dbops(db: Session, *, row: CloudDatabase, job_id: str,
                                   conn_name: str) -> None:
    """Add this instance to the region's DB-Ops allowlist, in place.

    Applied INLINE rather than queued, for the same reason the adapter pairing drives
    its deploy apply inline: the managed system this onboarding is about to create is
    unusable until the service will act on the instance, and two jobs that must happen
    in order are one job.

    Non-fatal on failure, and deliberately so. The service may be an operator's own
    (the config-key audience), in which case there is nothing here to update and
    nothing wrong; or the update may fail for a reason that has no bearing on whether
    the managed system is correct. What must not happen is silence — the remedy goes
    on the job log, because a failed job shows ``error_message`` and nothing else.
    """
    try:
        from . import clouddb_dbops_service, cloud_function_service

        if clouddb_dbops_service.find_for_region(db, row.region or "") is None:
            return      # a BYO service: its allowlist is not ours to manage
        result = clouddb_dbops_service.refresh_allowlist(
            db, region=row.region or "", created_by="clouddb-ps-onboarding")
        if not result.get("changed"):
            return
        await cloud_function_service.run_update_apply(
            db, fn_id=result["fn_id"], job_id=result["job_id"],
            tf_variables=result["tf_variables"])
        job_service.append_job_log(
            db, job_id, f"Admitted {conn_name} to the DB-Ops service allowlist.")
    except Exception as exc:
        logger.warning("clouddb: dbops allowlist update failed for %s: %s", row.id, exc)
        job_service.append_job_log(
            db, job_id,
            f"Could not add {conn_name} to the {row.region} DB-Ops service's allowed "
            f"instances ({exc}). Rotations for this database will be refused by the "
            f"service until you redeploy it from Settings → Password Safe, or add the "
            f"instance to FN_DBOPS_ALLOWED_INSTANCES on the function.")


async def _onboard_ps_managed_systems(db: Session, *, row: CloudDatabase, job_id: str,
                                      engine: str, tf_variables: dict, ctx: dict) -> None:
    """Onboard the DB into Password Safe: a managed system + managed account on the
    cloud-specific DB plugin platform — AWS "{engine} SSM Custom Plugin" (functional
    account = "<EC2|IAM>:<dbAdmin>" packing the AWS SSM credential and the DB admin
    password), Azure "{engine} Azure Run Command Plugin"
    (functional account = the Azure SP + the privileged DB admin login) or GCP
    "GCP Cloud SQL {engine}" (functional account = a GCP identity and, under IAM
    database authentication, no database password at all) — and, when a
    PRA Vault account exists for this DB, a managed system + managed account on the
    "PRA Vault Username Password" platform so Password Safe propagates rotations into
    the vaulted credential the tunnel injects. Ids + teardown state are stashed on the
    provisioning job's metadata the moment each half exists, so whatever a part-way
    failure created is still tracked for teardown. Failures propagate to the caller."""
    from . import ps_api_service, ps_resource_service
    name = tf_variables.get("identifier") or f"clouddb-{row.id[:8]}"
    workgroup_id = await ps_api_service.get_workgroup_id(
        _cfg("clouddb_ps_workgroup") or _cfg("passwordsafe_workgroup"))
    stash: dict = {
        "ps_db_managed_user": ctx["managed_user"],
        "ps_db_jump_host_id": ctx.get("jump_host_id") or ctx.get("jump_vm_name"),
        "ps_db_region": ctx["region"],
        "ps_db_admin_username": ctx["admin_username"],
        "ps_db_client_image": ctx.get("client_image", ""),
        "ps_db_name": ctx["db_name"],
        # Only the data-api + SQL Server path stages one, and only when the DASHBOARD
        # staged it — an operator-supplied clouddb_ps_gcp_fa_secret_version is shared by
        # every database on that channel and is not ours to delete. Recorded so teardown
        # can remove it: it holds a real database password, and a decommission that left
        # it behind would park that credential in Secret Manager for good.
        "ps_db_fa_sm_resource": (_fa_secret_resource_name(row.id)
                                 if ctx.get("fa_secret_version")
                                 and not _fa_secret_version_configured() else ""),
        "ps_db_fa_sm_project": ctx.get("project", ""),
    }

    # ── DB managed system (cloud-specific custom plugin) ──
    # Per-engine: SQL Server on Cloud SQL has no IAM database authentication, so it runs
    # on cloud-run and its functional account carries THIS database's login — which one
    # shared operator-owned account cannot do. PostgreSQL and MySQL on the same cloud
    # want the opposite. See _ps_fa_mode.
    fa_mode = _ps_fa_mode(engine)
    if row.cloud == "gcp":
        # "GCP Cloud SQL {engine}" on the data-api channel. No jump host, no broker
        # cert, no RSA key pair: the plugin reaches the private-IP instance through the
        # Cloud SQL Data API. Under IAM database authentication the functional account
        # has NO database password — the composite's third segment is "-" — so unlike
        # Azure nothing per-database is packed into it, which is why one operator-owned
        # account per engine ("reference" mode) is the natural fit here.
        db_platform_id = 0 if fa_mode == _FA_MODE_REFERENCE else (
            await ps_api_service.get_platform_id(_cfg(f"clouddb_ps_platform_gcp_{engine}")))
        fa_label, db_method = "gcp", "dbgcp"
        fa_key = f"clouddb_ps_functional_account_gcp_{engine}"
        fa_tokens = ("gcp", "cloud sql")
        channel = ctx.get("channel") or _gcp_channel(engine)
        fa_username = fa_password = ""
        if fa_mode != _FA_MODE_REFERENCE:
            auth_mode = (_cfg("clouddb_ps_gcp_auth_mode") or "ADC").upper()
            # On data-api the database user is the IAM principal name the DATABASE
            # stored, read back from the catalog rather than derived (see
            # _observed_iam_db_user); on cloud-run it is a real login, because SQL Server
            # has no IAM database authentication.
            fa_username = f"{auth_mode}:{ctx.get('fa_db_user') or ''}"
            impersonate = (_cfg("clouddb_ps_gcp_impersonate_target")
                           if auth_mode == "IMP" else "")
            # Segment 1 is the base64 service-account key, and only SA: mode has one —
            # a ~2.4 KB key base64s to ~3.2 KB, over Password Safe's 1000-character
            # credential limit, which is why ADC:/IMP: are the supported modes. Segment
            # 2 is the impersonation target. Segment 3 is the database password: absent
            # under IAM auth, but REQUIRED on cloud-run, where in "create" mode it is
            # this database's own admin credential.
            fa_db_password = "-"
            if channel == "cloud-run":
                fa_db_password = (config_service.get(f"clouddb/{row.id}/admin")
                                  or tf_variables.get("master_password") or "-")
            fa_password = f"-:{impersonate or '-'}:{fa_db_password}"
        # Address: channel;project:region:instance;dbName;audience;ssl[;key=value]
        conn_name = f"{ctx['project']}:{row.region}:{row.instance_id}"
        if channel == "cloud-run":
            # Field 4 is the Cloud Run service's audience, used verbatim as both the
            # request target and the token audience. When the dashboard deployed the
            # service the audience simply IS its URL — a custom audience exists to
            # DECOUPLE those two, and there is nothing here to decouple. The URL is
            # stable across revisions on Cloud Run v2; what can change it is recreating
            # the service, and the dashboard now owns that and re-stamps rather than
            # leaving an operator to find out at the next rotation.
            ssl_flag = ("sslTRUE" if config_service.get_bool("clouddb_ps_gcp_dbops_ssl", True)
                        else "sslFALSE")
            addr = [channel, conn_name, ctx["db_name"] or "",
                    _dbops_audience(row, db), ssl_flag]
            # The DB-Ops service refuses an instance it was not told about
            # (FN_DBOPS_ALLOWED_INSTANCES fails closed), so admit this one BEFORE the
            # managed system exists. Doing it after would register a system whose very
            # first rotation is refused by our own service — a failure that reads like
            # a permissions problem and is not.
            await _admit_instance_to_dbops(db, row=row, job_id=job_id,
                                           conn_name=conn_name)
        else:
            # Both control-plane fields are "-": the Cloud SQL APIs are always TLS and
            # never open a database connection, and the plugin rejects a value in either
            # rather than letting anyone believe they disabled something.
            #
            # MySQL names information_schema rather than the application catalog. The
            # rotation grant is GLOBAL (CREATE USER ON *.*) and confers no rights on any
            # schema, so a MySQL connection that opens the application database as its
            # default fails before running anything:
            #   Error 1044 (42000): Access denied for user '<fa>'@'%' to database '<db>'
            # The alternative — granting the rotation identity SELECT on the application
            # database — would buy a working connection with read access to customer
            # data, destroying the property that a compromised rotation identity can
            # change passwords and nothing else. information_schema is readable by every
            # principal, and ALTER USER is schema-independent, so nothing is lost.
            # Postgres needs none of this: it grants CONNECT to PUBLIC by default.
            #
            # What this does NOT fix is Discovery. MySQL permits fully-qualified
            # cross-database reads, so "SELECT ... FROM mysql.user" works from any
            # default database — the blocker there is purely the missing SELECT grant
            # (see _fa_discovery_grant_statement), never field 3. If Discovery comes
            # back empty or 1142, do not come and change this line.
            control_db = ("information_schema" if engine == "mysql"
                          else ctx["db_name"] or "")
            addr = [channel, conn_name, control_db, "-", "-"]
            # How the Data API authenticates the DATABASE session, and the two options
            # are alternatives rather than a pair. `iam=true` mints an OAuth token per
            # connection; `fasecret=` names the Secret Manager version holding the
            # functional account's password. SQL Server has no IAM database
            # authentication at all — the plugin REJECTS `iam=` on it — so it takes the
            # second, and this used to append `iam=true` unconditionally and emit
            # `fasecret=` nowhere, which made a forced data-api SQL Server address
            # unparseable in both directions at once.
            if ctx.get("iam_db_auth", _iam_db_auth(engine, channel)):
                addr.append("iam=true")
            else:
                addr.append(f"fasecret={ctx.get('fa_secret_version') or ''}")
        if engine == "mysql":
            # The plugin deliberately REFUSES to default a MySQL host qualifier, because
            # app@% and app@10.0.0.5 are different accounts and rotating the wrong row
            # rotates the wrong account silently. We created '<user>'@'%', so say so.
            addr.append(f"host={ctx.get('managed_user_host') or '%'}")
        dns_name = ";".join(addr)
    elif row.cloud == "azure":
        # "{engine} Azure Run Command Plugin". In "create" mode the functional account
        # bundles the Azure control-plane SP with a privileged DB login (the minted
        # admin), which rotates the dedicated managed user; in "reference" mode the
        # operator's own account carries whatever the plugin needs, so none of that
        # credential material is read here. Address is eight ;-separated fields.
        db_platform_id = 0 if fa_mode == _FA_MODE_REFERENCE else (
            await ps_api_service.get_platform_id(_cfg(f"clouddb_ps_platform_azure_{engine}")))
        fa_label, db_method = "azure", "dbazure"
        fa_key = f"clouddb_ps_functional_account_azure_{engine}"
        fa_tokens = ("azure", "run command")
        fa_username = fa_password = ""
        if fa_mode != _FA_MODE_REFERENCE:
            auth_mode = (_cfg("clouddb_ps_azure_auth_mode") or "SP").upper()
            admin_password = config_service.get(f"clouddb/{row.id}/admin") or ""
            fa_username = f"{auth_mode}:{ctx['admin_username']}"
            if auth_mode == "MSI":
                fa_password = f"-:-:{admin_password}"
            else:
                client_id = _cfg("clouddb_ps_azure_sp_client_id") or _cfg("azure_client_id")
                client_secret = (_cfg("clouddb_ps_azure_sp_client_secret")
                                 or _cfg("azure_client_secret"))
                fa_password = f"{client_id}:{client_secret}:{admin_password}"
        ssl_flag = "sslTRUE" if config_service.get_bool("clouddb_ps_azure_ssl", True) else "sslFALSE"
        # Address: vmName;resourceGroup;subscriptionId;tenantId;dbHost;dbName;certPath;ssl
        dns_name = ";".join([
            ctx["jump_vm_name"], ctx["resource_group"], _cfg("azure_subscription_id"),
            _cfg("azure_tenant_id"), row.private_host, ctx["db_name"] or "",
            _cfg("clouddb_ps_azure_cert_path"), ssl_flag])
    else:
        # "{engine} SSM Custom Plugin" (vendor v24.2.x). The plugin indexes ';'-packed
        # host fields at fixed per-engine positions — mssql has NO database segment and
        # mysql alone carries a trailing ssl flag; ps_resource_service holds the full
        # grammar and validates the composed address at registration. In "create" mode
        # the functional account is "<EC2|IAM>:<dbAdminUser>" over the always-three-part
        # "<akid>:<secret>:<dbAdminPassword>": the AWS pair is the SSM transport
        # credential (or two ignored-but-required placeholders in EC2 mode), and the DB
        # admin credential rides part 3 — Verify FA logs into the database with it, and
        # the via-functional-account change works because the RDS master user IS
        # privileged. In "reference" mode the operator's own account carries all of
        # that, so none of the credential material is read here.
        db_platform_id = 0 if fa_mode == _FA_MODE_REFERENCE else (
            await ps_api_service.get_platform_id(_cfg(f"clouddb_ps_platform_{engine}")))
        fa_label, db_method = "ssm", "dbssm"
        fa_key = f"clouddb_ps_functional_account_{engine}"
        fa_tokens = ("ssm",)
        fa_username = fa_password = ""
        if fa_mode != _FA_MODE_REFERENCE:
            admin_password = (config_service.get(f"clouddb/{row.id}/admin")
                              or tf_variables.get("master_password") or "")
            if not admin_password:
                logger.warning(
                    "clouddb: no admin credential available for db_id=%s — the SSM "
                    "functional account's dbAdminPassword segment will be empty, so "
                    "Verify Functional Account and rotation will fail until it is "
                    "corrected in Password Safe", row.id)
            fa_username, fa_password = _dbssm_fa_fields(
                admin_user=ctx["admin_username"], admin_password=admin_password,
                access_key_id=_cfg("clouddb_ps_ssm_access_key_id"),
                secret_access_key=_cfg("clouddb_ps_ssm_secret_access_key"))
        cert_path = _cfg("clouddb_ps_ssm_public_key_path")
        if not cert_path:
            raise CloudDatabaseError(
                "clouddb_ps_ssm_public_key_path is blank — it is packed into the "
                "managed-system address as the plugin's certPath segment (the RSA "
                "public certificate path on the Resource Broker host), and an empty "
                "positional field fails inside the plugin at the first rotation "
                "rather than here")
        # Per-engine address (grammar + rationale in ps_resource_service):
        #   sqlserver: instanceId;region;dbEndpoint;certPath;assumeRole
        #   postgres:  instanceId;region;dbEndpoint;databaseName;certPath;assumeRole
        #   mysql:     …;databaseName;certPath;assumeRole;sslTRUE|sslFALSE
        addr = [ctx["jump_host_id"], ctx["region"], row.private_host]
        if engine != "sqlserver":
            # The psql/mysql actions connect to this catalog; mssql has no segment for
            # it (its actions always land in master). connection_db_name can be blank
            # on an old un-backfilled row, so fall back to a catalog every RDS instance
            # of the engine is guaranteed to have.
            addr.append(ctx["db_name"] or ("postgres" if engine == "postgres" else "mysql"))
        addr += [cert_path, _dbssm_assume_role()]
        if engine == "mysql":
            addr.append("sslTRUE" if config_service.get_bool("clouddb_ps_ssm_ssl", True)
                        else "sslFALSE")
        dns_name = ";".join(addr)
    db_fa_id, db_platform_id, db_fa_owned = await _resolve_db_functional_account(
        mode=fa_mode,
        config_key=fa_key, platform_id=db_platform_id, platform_tokens=fa_tokens,
        label=fa_label, create={
            "account_name": fa_username, "display_name": f"{name}-{fa_label}-fa",
            "password": fa_password,
            "description": f"Cloud-DB functional account for dashboard database {name} (db_id={row.id})"})
    # Only an account WE minted may be deleted at decommission. The teardown loop is
    # driven purely by the PRESENCE of "ps_db_functional_account_id", so a referenced
    # (operator-owned) account goes under a key that loop never reads.
    stash["ps_db_functional_account_id" if db_fa_owned
          else "ps_db_functional_account_ref"] = db_fa_id
    # Self-rotation ("change password using own credentials") is a CLOUD-RUN action:
    # it needs to log in AS the managed account, which only that channel can do. The
    # control-plane channels authenticate as the caller and refuse it at pre-flight,
    # before any network call. clouddb_ps_self_rotation is one global flag that AWS/Azure
    # "reference" mode REQUIRES, so on GCP it has to be honoured per channel rather than
    # inherited wholesale — otherwise turning it on for AWS breaks every GCP data-api
    # rotation. On data-api the functional account is granted rights over the managed
    # principal instead.
    self_rotate = config_service.get_bool("clouddb_ps_self_rotation", False)
    if db_method == "dbgcp" and self_rotate and (
            (ctx.get("channel") or _gcp_channel(engine)) != "cloud-run"):
        logger.info("clouddb: not emitting use_own_credentials for db_id=%s — self-"
                    "rotation needs the cloud-run channel and this managed system is on "
                    "%s, which refuses it at pre-flight; the functional account rotates "
                    "the managed user instead", row.id, _gcp_channel(engine))
        self_rotate = False
    reg = await ps_resource_service.register_managed_system(
        name=f"{name}-db", host_name=row.private_host,
        # ip_address is deliberately NOT passed: register_managed_system owns the
        # per-plugin fill, which is the 127.0.0.1 placeholder for every DB plugin.
        # Password Safe refuses a create with no ip ("The field 'IPAddress' is
        # required.") and equally refuses one that is not an IP ("Bad IP value"), so
        # the packed address rides DnsName alone.
        port=ctx["port"], functional_account_id=db_fa_id, platform_id=db_platform_id,
        workgroup_id=workgroup_id, managed_account_name=ctx["managed_user"],
        method=db_method, dns_name=dns_name,
        use_own_credentials=self_rotate)
    stash["ps_db_registration_tf_state"] = reg.get("tf_state_json")
    stash["ps_db_system_id"] = reg.get("managed_system_id")
    stash["ps_db_account_id"] = reg.get("managed_account_id")
    # Commit what exists NOW, before the PRA Vault half is attempted. Everything used to
    # be written in one go at the end, so a PRA Vault failure — which the caller treats
    # as non-fatal — left a real managed system and a real functional account in the
    # customer's Password Safe with nothing recorded to tear them down. The row columns
    # ride the same commit as the metadata stash they mirror, so the page's "onboarded"
    # badge cannot disagree with what teardown will find.
    row.ps_managed_system_id = str(reg.get("managed_system_id") or "") or None
    row.ps_managed_account_id = str(reg.get("managed_account_id") or "") or None
    _stash_on_job(db, job_id, stash)
    logger.info("clouddb: onboarded DB managed system db_id=%s system_id=%s account_id=%s",
                row.id, reg.get("managed_system_id"), reg.get("managed_account_id"))

    # ── PRA Vault managed system (pravault) — only if the tunnel minted a vault account ──
    job = db.query(Job).filter(Job.id == job_id).first()
    vault_account_name = (job.metadata_dict or {}).get("vault_account_name") if job else None
    if vault_account_name and _cfg("bt_api_host"):
        pv_platform_id = 0 if fa_mode == _FA_MODE_REFERENCE else (
            await ps_api_service.get_platform_id(_cfg("clouddb_ps_pravault_platform")))
        pra_url = _cfg("bt_api_host")
        if not pra_url.lower().startswith("http"):
            pra_url = f"https://{pra_url}"
        # The GLOBAL mode, deliberately, not fa_mode: this account is the PRA Config API
        # credential for the appliance, shared by every database and tied to no engine.
        # Following the engine's mode would let a SQL Server setting decide how the Vault
        # account for a Postgres database is resolved.
        pv_mode = _ps_fa_mode()
        pv_account = pv_secret = ""
        if pv_mode != _FA_MODE_REFERENCE:
            pv_account = _cfg("pra_config_api_client_id") or _cfg("bt_client_id")
            pv_secret = _cfg("pra_config_api_client_secret") or _cfg("bt_client_secret")
        pv_fa_id, pv_platform_id, pv_fa_owned = await _resolve_db_functional_account(
            mode=pv_mode,
            config_key="clouddb_ps_pravault_functional_account",
            platform_id=pv_platform_id, platform_tokens=("pra vault",), label="PRA Vault",
            create={"account_name": pv_account, "display_name": f"{name}-pravault-fa",
                    "password": pv_secret,
                    "description": f"PRA Config API functional account for dashboard database {name} (db_id={row.id})"})
        stash["ps_pravault_functional_account_id" if pv_fa_owned
              else "ps_pravault_functional_account_ref"] = pv_fa_id
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

    _stash_on_job(db, job_id, stash)


async def _ps_onboard_post_hoc(db: Session, *, row: CloudDatabase, job_id: str) -> None:
    """Onboard an already-provisioned database into Password Safe: create the dedicated
    managed DB user, re-point the PRA tunnel at it, then register the managed systems.

    The same three steps :func:`run_provision_apply` interleaves with the apply, run
    against a database that already exists — so Password Safe can be turned on for one
    built before the feature was configured, or with the provision checkbox cleared.

    Everything is recorded on the PROVISIONING job's metadata, not on this action's job,
    because that is the single place :func:`run_decommission` looks for it. This job
    carries the progress and the error.

    **Re-pointing the tunnel is not optional.** Password Safe rotates the managed user,
    and the "PRA Vault Username Password" mirror pushes each rotation into the vaulted
    credential the tunnel injects — so leaving that credential as the master admin means
    the first rotation overwrites an admin password with the managed user's and
    credential injection quietly breaks. The existing jump + Vault account are destroyed
    from their stored Terraform state FIRST: the sra provider offers no import, so
    re-brokering without the destroy would strand the old jump and Vault account in the
    appliance with nothing tracking them."""
    engine = row.engine
    prov_job = _provision_job_for(db, row.id)
    if prov_job is None:
        raise CloudDatabaseError(
            "no provisioning job recorded for this database — Password Safe onboarding "
            "needs its Terraform variables (the admin login and catalog) and has "
            "nowhere to record the ids it creates")
    tf_variables = dict((prov_job.metadata_dict or {}).get("tf_variables") or {})

    job_service.update_progress(db, job_id, 25,
                                "Creating the rotatable managed database user…")
    if row.cloud == "azure":
        ctx = await _create_db_managed_user_azure(
            db, row=row, job_id=prov_job.id, engine=engine, tf_variables=tf_variables)
    elif row.cloud == "gcp":
        ctx = await _create_db_managed_user_gcp(
            db, row=row, job_id=prov_job.id, engine=engine, tf_variables=tf_variables)
    else:
        ctx = await _create_db_managed_user(
            db, row=row, job_id=prov_job.id, engine=engine, tf_variables=tf_variables)

    if _pra_configured():
        job_service.update_progress(db, job_id, 55,
                                    "Re-pointing the PRA tunnel at the managed user…")
        old_state = (prov_job.metadata_dict or {}).get("tunnel_tf_state")
        if old_state:
            from . import terraform_pra_service as pra
            # Raises on failure, deliberately: stopping here leaves the database exactly
            # as it was, where carrying on would put a second jump for the same database
            # in the appliance and orphan the first.
            await pra.remove_db_tunnel(old_state)
            row.jump_item_id = None
            fresh = dict(prov_job.metadata_dict or {})
            for key in ("tunnel_tf_state", "vault_account_id", "vault_account_name"):
                fresh.pop(key, None)
            prov_job.metadata_dict = fresh
            db.commit()
            logger.info("clouddb: removed the admin-credential tunnel for db_id=%s "
                        "before re-brokering against %s", row.id, ctx["managed_user"])
        await _broker_tunnel(db, row=row, job_id=prov_job.id, engine=engine,
                             tf_variables=tf_variables,
                             override_cred=(ctx["managed_user"], ctx["managed_pw"]))
        # _broker_tunnel is non-fatal by design (a provision keeps the database when the
        # tunnel fails). Here it is fatal: the old jump is already gone, so a silent
        # failure would leave the database unreachable with a green job.
        if not row.jump_item_id:
            raise CloudDatabaseError(
                "the PRA tunnel could not be brokered against the managed user, and the "
                "previous jump was removed first — this database currently has no "
                "tunnel. Check the app log for the sra provider error, then run "
                "Register in Password Safe again.")

    job_service.update_progress(db, job_id, 75,
                                "Registering the Password Safe managed systems…")
    await _onboard_ps_managed_systems(db, row=row, job_id=prov_job.id, engine=engine,
                                      tf_variables=tf_variables, ctx=ctx)
    if not row.ps_managed_system_id:
        raise CloudDatabaseError("Password Safe returned no managed system id")


async def run_ps_register(db: Session, *, db_id: str, job_id: str,
                          action: str = "register") -> None:
    """Worker entry for a ``clouddb_ps_register`` job: onboard the database into
    Password Safe, or remove that onboarding again. The post-provision counterpart of
    the checkbox on the provision form, and the sibling of
    :func:`run_entitle_register` — like it, failures are FATAL to the job (a post-hoc
    request the operator is watching should not go green on a skipped step)."""
    row = db.query(CloudDatabase).filter(CloudDatabase.id == db_id).first()
    if not row:
        job_service.set_failed(db, job_id, f"database {db_id} not found")
        return
    job_service.set_running(db, job_id)
    try:
        if action == "deregister":
            prov_job = _provision_job_for(db, row.id)
            if not _has_ps_onboarding((prov_job.metadata_dict or {}) if prov_job else {}):
                raise CloudDatabaseError(
                    "this database has no Password Safe onboarding recorded — nothing "
                    "to remove. An onboarding done outside the dashboard has to be "
                    "removed in Password Safe.")
            errors = await _teardown_ps_onboarding(
                db, row=row, prov_job=prov_job, progress_job_id=job_id, progress=40)
            if errors:
                raise CloudDatabaseError("; ".join(errors))
            # The managed DB user outlives a deregister — unlike a decommission, the
            # database is still here. Say so rather than leaving a login nobody expects.
            leftover = (prov_job.metadata_dict or {}).get("ps_db_managed_user") if prov_job else None
            job_service.append_job_log(
                db, job_id,
                f"The managed database user {leftover or _managed_user_name(row.id)!r} was "
                f"left in place — it is what the PRA tunnel injects. Drop it by hand if "
                f"you want the database back on its admin login.")
        else:
            reason = _ps_ineligible_reason(row)
            if reason:
                raise CloudDatabaseError(reason)
            if not _ps_db_onboarding_enabled(row, db):
                raise CloudDatabaseError(
                    "Password Safe database onboarding is not enabled for this cloud — "
                    "see Settings → Integrations → Password Safe "
                    "(clouddb_ps_onboarding_enabled, and for Azure and GCP the "
                    "per-cloud onboarding method must not be 'off')")
            if row.status != "available":
                raise CloudDatabaseError(
                    f"database is {row.status!r} — Password Safe onboarding needs a "
                    f"database that has finished provisioning")
            if not row.private_host:
                raise CloudDatabaseError("database has no endpoint yet")
            if row.ps_managed_system_id:
                raise CloudDatabaseError(
                    f"already onboarded as Password Safe managed system "
                    f"{row.ps_managed_system_id} — deregister it first to redo it")
            await _ps_onboard_post_hoc(db, row=row, job_id=job_id)
        job_service.set_completed(db, job_id, {
            "db_id": db_id, "action": action,
            "ps_managed_system_id": row.ps_managed_system_id,
            "ps_managed_account_id": row.ps_managed_account_id,
        })
        logger.info("clouddb Password Safe %s complete db_id=%s system_id=%s",
                    action, db_id, row.ps_managed_system_id)
    except Exception as exc:
        job_service.set_failed(db, job_id, str(exc))
        logger.exception("clouddb Password Safe %s job failed db_id=%s: %s",
                         action, db_id, exc)


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


# Create-time capacity stockouts, keyed by cloud: (error code the cloud embeds in
# the raw apply output, the tf variable naming the size that sold out, a sizing
# hint for the retry). Azure polls the create for many minutes before admitting
# CapacityNotAvailable ("Capacity is not available in this region/zone"), so the
# raw TerraformError is hundreds of plan/"Still creating…" lines with the one
# actionable line at the bottom. GCP is deliberately absent: its create-wait
# failure mode is handled by _reclaim_gcp_create_wait_instance, and Cloud SQL has
# no comparable stockout code to key on.
_DB_CAPACITY_STOCKOUTS = {
    "azure": ("Azure", "CapacityNotAvailable", "sku_name",
              "Burstable B-series SKUs stock out most often; a General Purpose "
              "SKU (e.g. GP_Standard_D2s_v3) usually has capacity."),
    "aws": ("AWS", "InsufficientDBInstanceCapacity", "instance_class",
            "Small burstable classes (db.t3.*) stock out most often; a larger "
            "class usually has capacity."),
}


def _distill_provision_failure(row: CloudDatabase, tf_variables: dict,
                               exc: Exception) -> str:
    """The ``set_failed`` string for a provision failure. The failed-job detail
    shows ``error_message`` ONLY, so a known failure must carry its cause and
    remedy here — and for a capacity stockout the raw TerraformError buries that
    one actionable line under the whole streamed apply (which the job's Live
    Output already persists verbatim). Unknown failures pass through unchanged.
    """
    if not isinstance(exc, terraform.TerraformError):
        return str(exc)
    sig = _DB_CAPACITY_STOCKOUTS.get(row.cloud)
    if not sig or sig[1].lower() not in str(exc).lower():
        return str(exc)
    cloud_name, code, size_var, hint = sig
    size = tf_variables.get(size_var) or "the requested size"
    region = row.region or "the requested region"
    return (
        f"{cloud_name} has no {size} capacity in {region} right now ({code}: a "
        f"capacity stockout on the cloud side, not a quota or configuration "
        f"problem). Delete this database — that also cleans up the failed create "
        f"attempt — then provision again with a different compute size, or in a "
        f"different region that has database networking configured. {hint} "
        f"The full Terraform output is in this job's Live Output."
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
    job_service.set_running(db, job_id)
    try:
        # The job's persisted tf_variables OMIT the master password — a secret is never
        # written to jobs.extra_data. Re-inject it from the secrets backend into the key
        # the engine module (and the downstream PRA tunnel + credential staging, which
        # read it back out of tf_variables) expect.
        #
        # get_fresh, not get: provision() wrote this key from the APP process moments
        # ago, and the runner claims the job within one POLL_INTERVAL (2s) — well inside
        # config_service's 5s cache TTL, which the supervisor pass keeps permanently
        # warm by re-reading worker_policy every poll. A cached read therefore returns
        # "" for a row that is certainly in app_config, most of the time.
        #
        # And an empty read must fail HERE. Skipping the injection (what this used to
        # do) handed terraform a var set with no master_password, so the apply died on
        # "No value for required variable" pointing at the module's variable block —
        # a missing-secret bug wearing a Terraform bug's clothes, and one that looked
        # like it belonged to whichever engine/region happened to draw the short straw.
        _pw_key = {"azure": "administrator_password", "oci": "admin_password"}.get(row.cloud, "master_password")
        _pw = config_service.get_fresh(f"clouddb/{db_id}/admin")
        if not _pw:
            raise CloudDatabaseError(
                f"the admin credential for this database is unreadable: "
                f"config://clouddb/{db_id}/admin holds no value, so Terraform would run "
                f"with no {_pw_key}. It is minted once at provision and cannot be "
                f"re-derived — delete this database and provision a new one."
            )
        tf_variables[_pw_key] = _pw

        # Kick the shared Gateway host EARLY (only when PRA is configured) so its
        # ~2-min boot overlaps the 5-10-min RDS apply instead of stacking after it.
        if _pra_configured():
            try:
                from . import jumpoint_host_service
                await jumpoint_host_service.ensure_jumpoint_host(row.cloud, _cfg(row.cloud + "_region") or row.region)
            except Exception as exc:
                logger.warning("clouddb: ensure gateway host (pre-apply) failed (non-fatal): %s", exc)

        # On-demand SSM interface endpoints for the AWS dbssm onboarding path so a
        # private-subnet target reaches the SSM control plane. Ref-counted; torn
        # down with the last EC2/DB. Independent of PRA. Best-effort.
        if row.cloud == "aws":
            try:
                from . import ssm_endpoint_service
                await ssm_endpoint_service.ensure_ssm_endpoints(_cfg(row.cloud + "_region") or row.region)
            except Exception as exc:
                logger.warning("clouddb: ensure SSM endpoints (pre-apply) failed (non-fatal): %s", exc)

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

        # Optional Password Safe DB onboarding (AWS / Azure / GCP, opt-in). Create
        # the dedicated managed user FIRST so the tunnel/vault injects it, then let PS
        # own its rotation. Any failure falls back to the legacy admin staging. The
        # three clouds reach the private DB differently: AWS runs the client on the ECS
        # gateway over SSM, Azure on the jump VM over Run Command, and GCP not at all —
        # it uses the Cloud SQL Admin API, which needs no network path.
        #
        # Two gates, and they answer different questions: _ps_db_onboarding_enabled is
        # "is this deployment set up for it", _ps_onboarding_opted is "did the operator
        # ask for it on THIS database" (absent → yes, see there). The choice covers THIS
        # onboarding only — the legacy admin-credential staging in the else branch is a
        # separate thing on its own gate (pscli), and quietly switching it off here would
        # make a checkbox labelled "managed system + managed account" do more than it says.
        _ps_job = db.query(Job).filter(Job.id == job_id).first()
        _ps_choice = _ps_onboarding_opted((_ps_job.metadata_dict or {}) if _ps_job else {})
        onboard_ctx = None
        if row.private_host and _ps_choice and _ps_db_onboarding_enabled(row, db):
            try:
                if row.cloud == "azure":
                    onboard_ctx = await _create_db_managed_user_azure(
                        db, row=row, job_id=job_id, engine=engine, tf_variables=tf_variables)
                elif row.cloud == "gcp":
                    onboard_ctx = await _create_db_managed_user_gcp(
                        db, row=row, job_id=job_id, engine=engine, tf_variables=tf_variables)
                else:
                    onboard_ctx = await _create_db_managed_user(
                        db, row=row, job_id=job_id, engine=engine, tf_variables=tf_variables)
            except Exception as exc:
                logger.warning("clouddb: PS managed-user creation failed db_id=%s "
                               "(falling back to admin staging): %s", db_id, exc)
                # The fallback keeps the database reachable (the tunnel is brokered
                # with the admin credential), but it must be visible on the job page —
                # a warning only in the app log is invisible to the operator who
                # ticked the box.
                job_service.append_job_log(
                    db, job_id,
                    f"Password Safe managed-user creation failed — the tunnel keeps "
                    f"the admin credential and onboarding was skipped: {exc}. Use "
                    f"'Register in Password Safe' on the Databases page to retry.")
                onboard_ctx = None

        # Phase 2: broker the PRA tunnel (only when PRA is configured + we have a host).
        # With PS onboarding active, inject the managed user; otherwise the admin cred.
        if row.private_host and _pra_configured():
            override = (onboard_ctx["managed_user"], onboard_ctx["managed_pw"]) if onboard_ctx else None
            await _broker_tunnel(db, row=row, job_id=job_id, engine=engine,
                                 tf_variables=tf_variables, override_cred=override)

        if onboard_ctx:
            # Full Password Safe onboarding (managed systems + accounts + PRA Vault sync).
            # A failure here FAILS the job. It shipped once as best-effort: the managed-
            # system create was rejected ("The field 'IPAddress' is required.") and the
            # job page said completed — the operator opted in, got no Password Safe
            # artifacts, and only a mid-log line said so. This branch has no fallback,
            # so a green job here is simply false. The ROW stays "available": the
            # database exists and the tunnel is brokered, and run_ps_register — the
            # remedy — refuses any row that is not.
            try:
                await _onboard_ps_managed_systems(
                    db, row=row, job_id=job_id, engine=engine, tf_variables=tf_variables, ctx=onboard_ctx)
            except Exception as exc:
                logger.warning("clouddb: PS managed-system onboarding failed db_id=%s: %s",
                               db_id, exc)
                raise CloudDatabaseError(
                    f"the database was created and is available, but Password Safe "
                    f"onboarding failed: {exc}. Fix the cause, then use 'Register in "
                    f"Password Safe' on the Databases page to finish onboarding — the "
                    f"database does not need to be re-provisioned.") from exc
        else:
            if not _ps_choice:
                logger.info("clouddb: Password Safe onboarding skipped for db_id=%s — the "
                            "provision opted out; the Databases page's Register in Password "
                            "Safe action onboards it later", db_id)
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
            # Two routes to Entitle, chosen by whether the NATIVE connector can
            # actually do just-in-time accounts for this engine. MySQL's assigns
            # persistent roles and never mints an account; SQL Server's needs
            # sysadmin, which managed flavors do not grant (and which
            # _entitle_ineligible_reason blocks outright). Both are served by a REST
            # adapter instead — a db_grant Cloud Function deployed beside the
            # database. Postgres keeps the native connector, which works.
            from . import cloud_db_adapter_service
            if cloud_db_adapter_service.adapter_required(engine):
                await _pair_adapter(db, row=row, engine=engine, job_id=job_id)
            else:
                await _register_entitle(db, row=row, engine=engine,
                                        tf_variables=tf_variables)

        job_service.set_completed(db, job_id)
        logger.info("clouddb apply complete db_id=%s host=%s tunnel=%s",
                    db_id, row.private_host, row.jump_item_id)
    except Exception as exc:
        # Only a failure BEFORE the database reached "available" demotes the row. After
        # that point (Password Safe onboarding, Entitle registration) the database is
        # real and usable, and the retry actions for those steps (run_ps_register,
        # run_entitle_register) refuse any row that is not "available" — demoting it
        # would lock the operator out of the very remedy the failure message names.
        if row.status != "available":
            row.status = "failed"
        db.commit()
        job_service.set_failed(db, job_id,
                               _distill_provision_failure(row, tf_variables, exc))
        logger.exception("clouddb apply failed db_id=%s: %s", db_id, exc)


# Password Safe DB onboarding teardown, as (managed-system state key, functional-account
# key, label) triples. PRA Vault first: it is the mirror, and removing the DB system
# first would leave the mirror pointing at nothing for the width of the teardown.
_PS_TEARDOWN_STEPS = (
    ("ps_pravault_registration_tf_state", "ps_pravault_functional_account_id", "PRA Vault"),
    ("ps_db_registration_tf_state", "ps_db_functional_account_id", "DB"),
)
# Everything else the onboarding records: context for the plugins and the ids an
# operator reads back. Nothing to delete remotely, so these are cleared once the
# managed systems they describe are gone.
_PS_CONTEXT_KEYS = (
    "ps_db_managed_user", "ps_db_jump_host_id", "ps_db_region", "ps_db_admin_username",
    "ps_db_client_image", "ps_db_name", "ps_db_system_id", "ps_db_account_id",
    "ps_pravault_system_id", "ps_pravault_account_id",
    "ps_db_functional_account_ref", "ps_pravault_functional_account_ref",
    "ps_db_fa_sm_resource", "ps_db_fa_sm_project",
)


def _has_ps_onboarding(meta: dict) -> bool:
    """Whether a provisioning job's metadata records Password Safe objects to remove."""
    return any((meta or {}).get(k) for step in _PS_TEARDOWN_STEPS for k in step[:2])


async def _teardown_ps_onboarding(db: Session, *, row: Optional[CloudDatabase],
                                  prov_job: Optional[Job], progress_job_id: str,
                                  progress: int = 50) -> list[str]:
    """Remove the Password Safe DB onboarding artifacts recorded on the provisioning
    job's metadata: each managed system (from its stored Terraform state) and then the
    functional account it used — in that order, because a managed system that still
    references a functional account blocks the account's delete.

    Shared by :func:`run_decommission`, where the database goes away too, and by the
    standalone Deregister in :func:`run_ps_register`, where it does not. That second
    caller is why each key is cleared INDIVIDUALLY, on the success of its own step: a
    key left behind after a successful removal has the next decommission destroy
    something that is already gone, and a key cleared after a FAILED removal is an
    object in the customer's Password Safe that nothing will ever retry.

    Only accounts the dashboard MINTED are deleted. In ``reference`` mode the id was
    stashed under ``ps_*_functional_account_ref`` — a key this loop never reads — so an
    operator-created functional account survives every teardown.

    Returns the error strings; empty means everything recorded was removed."""
    meta = (prov_job.metadata_dict or {}) if prov_job else {}
    if not _has_ps_onboarding(meta):
        # Say so, rather than returning in silence. A teardown that skips because nothing
        # was recorded is indistinguishable, in the job log, from one that had nothing to
        # do -- which is how an orphaned managed system goes unnoticed: the destroy log
        # shows only Terraform and the job still goes green. Observed live 2026-08-24.
        logger.info("clouddb: no Password Safe onboarding recorded on provision job %s "
                    "for db_id=%s - skipping PS teardown",
                    getattr(prov_job, "id", None), row.id if row is not None else "?")
        job_service.append_job_log(
            db, progress_job_id,
            "No Password Safe onboarding was recorded for this database, so there is "
            "nothing to remove. If it WAS onboarded, its managed system is orphaned and "
            "needs removing by hand.")
        return []
    db_id = row.id if row is not None else "?"
    errors: list[str] = []
    done: list[str] = []
    job_service.update_progress(db, progress_job_id, progress,
                                "Removing Password Safe managed systems…")
    from . import ps_resource_service, ps_api_service
    for state_key, fa_key, label in _PS_TEARDOWN_STEPS:
        state = meta.get(state_key)
        if state:
            try:
                await ps_resource_service.deregister(state)
                done.append(state_key)
                logger.info("clouddb %s managed system removed db_id=%s", label, db_id)
            except Exception as exc:
                errors.append(f"{label} managed system: {exc}")
                logger.warning("clouddb %s managed system removal for %s failed: %s",
                               label, db_id, exc)
                continue   # its functional account is still referenced — leave it
        fa = meta.get(fa_key)
        if fa:
            try:
                await ps_api_service.delete_functional_account(int(fa))
                done.append(fa_key)
                logger.info("clouddb %s functional account %s deleted db_id=%s",
                            label, fa, db_id)
            except Exception as exc:
                errors.append(f"{label} functional account: {exc}")
                logger.warning("clouddb %s functional-account delete for %s failed: %s",
                               label, db_id, exc)

    # The one context key with something to DELETE remotely: the regional secret
    # mirroring the functional account's database password on the data-api + SQL Server
    # path. Removed after the managed system, because the plugin reads it on every
    # rotation and deleting it first would break the last one. Best-effort and never
    # fatal — but noisy on failure, since what is left behind is a live credential.
    #
    # ``resource_id`` / ``ps_db_fa_sm_*``, not ``secret_id``, and the reason is the same
    # one gcp_service._write_regional_secret_sync spells out: this value is the secret's
    # NAME, the failure path has to log it so an operator knows which one to delete by
    # hand, and a name containing "secret" makes CodeQL's clear-text-logging query treat
    # it as the credential itself (py/clear-text-logging-sensitive-data). Naming it for
    # what it actually is beats suppressing a query that is right to be suspicious. The
    # credential never appears here at all.
    resource_id = meta.get("ps_db_fa_sm_resource")
    secret_gone = True
    if resource_id and "ps_db_registration_tf_state" in done:
        from . import gcp_service
        project = meta.get("ps_db_fa_sm_project") or _cfg("gcp_project")
        region = meta.get("ps_db_region") or ""
        secret_gone = bool(project and region) and await gcp_service.delete_regional_secret(
            project, region, resource_id)
        if not secret_gone:
            errors.append(
                f"the functional account's regional Secret Manager entry {resource_id} "
                f"in {region or '?'} was not deleted — it holds a database password, "
                f"remove it by hand")
        else:
            logger.info("clouddb: functional-account regional SM entry %s removed "
                        "db_id=%s", resource_id, db_id)

    if prov_job is not None and done:
        # Clear only what came off cleanly; the context keys go with the DB managed
        # system, since they exist to describe it. The secret keys are the exception:
        # they name something that still EXISTS when its delete failed, so clearing them
        # would leave a live database password in Secret Manager that nothing ever
        # retries — the same rule the per-step clearing above follows.
        if "ps_db_registration_tf_state" in done:
            keep = () if secret_gone else ("ps_db_fa_sm_resource", "ps_db_fa_sm_project")
            done += [k for k in _PS_CONTEXT_KEYS if k in meta and k not in keep]
        fresh = dict(prov_job.metadata_dict or {})
        for key in done:
            fresh.pop(key, None)
        prov_job.metadata_dict = fresh
        db.commit()
    if row is not None and "ps_db_registration_tf_state" in done:
        row.ps_managed_system_id = None
        row.ps_managed_account_id = None
        db.commit()
    return errors


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
        raise CloudDatabaseError(f"database {db_id} not found")

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
        job_service.set_failed(db, job_id, f"database {db_id} not found")
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
            await _to_thread(secrets_backend_service.delete_bt_secrets_safe, secret_ref)
            logger.info("clouddb Secrets Safe secret %r deleted db_id=%s", secret_ref, db_id)
        except Exception as exc:
            errors.append(f"Secrets Safe secret: {exc}")
            logger.warning("clouddb secrets-safe delete for %s failed: %s", db_id, exc)

    # 3b. Password Safe DB onboarding artifacts (managed systems + functional
    #     accounts). The managed DB user itself dies with the database instance
    #     (step 4), so no DB-side drop is needed here. Shared with the standalone
    #     Deregister action, which runs the identical teardown on its own.
    errors += await _teardown_ps_onboarding(
        db, row=row, prov_job=deploy_job, progress_job_id=job_id, progress=50)
    meta = (deploy_job.metadata_dict or {}) if deploy_job else {}

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

    # Terminate the shared Gateway host if nothing is left using it (best-effort;
    # the row is no longer active, so it's excluded from the count).
    job_service.update_progress(db, job_id, 90, "Reclaiming idle Gateway host…")
    try:
        from . import jumpoint_host_service
        await jumpoint_host_service.teardown_jumpoint_host_if_idle(db, row.cloud, _cfg(row.cloud + "_region") or row.region)
    except Exception as exc:
        warnings.append(f"Gateway host teardown: {exc}")
        logger.warning("clouddb: gateway host idle-teardown failed (non-fatal): %s", exc)

    # Reclaim the shared SSM interface endpoints if no EC2 instance / AWS cloud DB
    # is left (this row is already inactive, so it's excluded from the count).
    if row.cloud == "aws":
        try:
            from . import ssm_endpoint_service
            await ssm_endpoint_service.reclaim_ssm_endpoints(db, _cfg(row.cloud + "_region") or row.region)
        except Exception as exc:
            warnings.append(f"SSM endpoints teardown: {exc}")
            logger.warning("clouddb: SSM endpoints idle-teardown failed (non-fatal): %s", exc)

    job_service.set_completed(db, job_id, {"db_id": db_id, **({"warnings": warnings} if warnings else {})})
    logger.info("clouddb decommissioned db_id=%s", db_id)


def list_databases(db: Session) -> list[dict]:
    # Hide cleanly-decommissioned rows so old endpoints don't linger; keep
    # available/provisioning/decommissioning and `failed` (a failed decommission
    # is an orphan the operator still needs to see).
    rows = (db.query(CloudDatabase)
              .filter(CloudDatabase.status != "decommissioned")
              .order_by(CloudDatabase.created_at.desc()).all())
    out = [_serialize(r) for r in rows]
    _fill_adapter_state(db, rows, out)
    return out


def _fill_adapter_state(db: Session, rows: list, serialized: list[dict]) -> None:
    """Stamp each row's existing db_grant adapter, if it has one, onto its projection.

    ONE query for the whole page (the adapter name is derived from the database id, and
    cloud_functions.name is indexed), which is why this is here rather than in the pure
    per-row :func:`_serialize`. Non-fatal: the adapter badge is not worth 500ing the
    Databases page over, and a blank adapter_fn_id merely re-offers a button the API
    would then refuse with a 409 naming the function.
    """
    from . import cloud_function_service
    pairs = [(r, s) for r, s in zip(rows, serialized) if s.get("adapter_viable")]
    if not pairs:
        return
    try:
        names = {r.id: cloud_db_adapter_service.adapter_name(r) for r, _ in pairs}
        found = cloud_function_service.find_by_names(
            db, names.values(), workload=cloud_db_adapter_service.ADAPTER_WORKLOAD)
    except Exception as exc:                                   # pragma: no cover
        logger.warning("clouddb: adapter lookup failed (non-fatal): %s", exc)
        return
    for row, payload in pairs:
        fn = found.get(names[row.id])
        if fn is not None:
            payload["adapter_fn_id"] = fn.id
            payload["adapter_status"] = fn.status or ""


def backfill_provisioned_db_names(db: Session) -> int:
    """One-time: copy each provisioned row's catalog out of its provisioning job into
    ``cloud_databases.db_name``. Returns the number of rows written.

    Rows created before :func:`provision` started stamping the column carry their name
    only in ``jobs.extra_data["tf_variables"]``, so the Databases page has nothing to
    show and the Entitle adapter has nothing to fall back on. Idempotent and
    convergent — it only ever writes a NULL column, and two processes computing the
    same value for the same row is a benign last-write-wins, so it needs no advisory
    lock of its own (the one thing that could reintroduce the init_db deadlock class).

    A registered row is skipped: a blank name there is the operator's own choice, and
    inventing one would change what a Config-Management run connects to. A row whose
    job is gone stays NULL rather than getting a guessed value.
    """
    rows = db.query(CloudDatabase).filter(CloudDatabase.db_name.is_(None)).all()
    rows = [r for r in rows if (r.source or "provisioned") != "registered"]
    if not rows:
        return 0

    # One indexed pass over the jobs rather than _provision_job_for per row (that scans
    # every clouddb_provision job on each call). Ascending, so the last write into the
    # map is the newest job — the same row _provision_job_for would have picked.
    meta_by_db: dict[str, dict] = {}
    for job in (db.query(Job)
                  .filter(Job.job_type == "clouddb_provision")
                  .order_by(Job.created_at.asc()).all()):
        meta = job.metadata_dict or {}
        db_id = meta.get("db_id")
        if db_id:
            meta_by_db[db_id] = meta

    written = 0
    for row in rows:
        tfv = (meta_by_db.get(row.id) or {}).get("tf_variables") or {}
        # The same three keys the adapter reads: the SQL Server modules name it
        # differently, and Azure SQL's is the database resource, not an initial catalog.
        name = next((str(tfv[k]) for k in ("db_name", "database_name", "initial_catalog")
                     if tfv.get(k)), "")
        if name:
            row.db_name = name
            written += 1
    if written:
        db.commit()
    logger.info("clouddb db_name backfill: %s of %s candidate row(s) resolved",
                written, len(rows))
    return written


def connection_info(db: Session, db_id: str) -> dict:
    row = db.query(CloudDatabase).filter(CloudDatabase.id == db_id).first()
    if not row:
        raise CloudDatabaseError(f"database {db_id} not found")
    # jump_item_id is the PRA protocol-tunnel jump a user opens to reach the
    # private DB (populated once the tunnel is brokered; null if PRA is unset).
    return {
        "db_id": row.id, "engine": row.engine, "cloud": row.cloud,
        "provider": row.provider,
        # source lets a caller tell a provisioned row from a registered one without
        # reaching for the ORM — the Entitle pre-flight in the API needs it.
        "source": row.source or "provisioned",
        "status": row.status, "private_host": row.private_host, "port": row.port,
        "jump_item_id": row.jump_item_id,
        # The database to name in a connection string / after sqlcmd's -d.
        "db_name": row.db_name, "connect_db_name": connection_db_name(row),
    }


_MANAGED_REF_PREFIX = "psmanaged:"


def register_database(db: Session, *, engine: str, cloud: str, host: str,
                      port: int | None, db_name: str, managed_account: dict,
                      created_by: str, region: str = "", instance_id: str = "",
                      agent_id: str = "") -> dict:
    """Record a database that already exists, so it can be a Config Management target.

    The sibling of :func:`k8s_service.register_cluster`: no Terraform, no provisioning
    job, just an inventory row plus the reference needed to reach it. ``cloud='local'``
    is an on-prem database — the local runner reaches it directly, the same shape a
    kubeconfig-registered cluster already has.

    ``agent_id`` names the remote agent that can reach it, and is what makes an on-prem
    database usable from a **cloud-hosted** dashboard: without it the run needs a sibling
    container on the dashboard's own host, which has neither a Docker socket on ECS/ACI/
    Container Apps nor a route to the LAN. Only meaningful with ``cloud='local'`` — a
    cloud database is reached by its own in-cloud runner — so it is refused elsewhere
    rather than stored and silently ignored.

    ``managed_account`` is a Password Safe system/account pair. The credential itself is
    never stored: it is checked out at run time by
    :func:`_registered_connection_vars`, so this row holds only ids and a name."""
    engine = (engine or "").strip().lower()
    cloud = (cloud or "").strip().lower()
    host = (host or "").strip()
    if engine not in VALID_ENGINES:
        raise CloudDatabaseError(
            f"unknown engine {engine!r} (expected one of {sorted(VALID_ENGINES)})")
    if cloud not in VALID_REGISTER_CLOUDS:
        raise CloudDatabaseError(
            f"unknown cloud {cloud!r} (expected one of {sorted(VALID_REGISTER_CLOUDS)})")
    if not host:
        raise CloudDatabaseError("a host is required — it is how the runner reaches the database")
    for key in ("system_id", "account_id"):
        if not (managed_account or {}).get(key):
            raise CloudDatabaseError(
                "a Password Safe managed account is required: the dashboard checks the "
                "credential out at run time rather than storing one")
    if db.query(CloudDatabase).filter(CloudDatabase.private_host == host,
                                      CloudDatabase.engine == engine).first():
        raise CloudDatabaseError(
            f"a {engine} database at {host!r} is already registered")

    agent_id = (agent_id or "").strip()
    if agent_id and cloud != "local":
        raise CloudDatabaseError(
            f"a remote agent can only broker a cloud='local' database; this one is "
            f"{cloud!r}, which is reached by its own in-cloud Ansible runner.")
    if agent_id:
        from ..database import RemoteAgent
        if not db.query(RemoteAgent).filter(RemoteAgent.id == agent_id,
                                            RemoteAgent.is_active.is_(True)).first():
            raise CloudDatabaseError("that remote agent is not registered.")

    row = CloudDatabase(
        agent_id=agent_id or None,
        engine=engine, cloud=cloud, source="registered",
        provider="registered", region=region or None, instance_id=instance_id or None,
        private_host=host, port=port or _DEFAULT_PORTS.get(engine),
        db_name=(db_name or "").strip() or None,
        status="available",
        created_at=datetime.utcnow(),
        credentials_ref=_MANAGED_REF_PREFIX + json.dumps({
            "system_id": managed_account["system_id"],
            "account_id": managed_account["account_id"],
            "account_name": managed_account.get("account_name") or "",
            "uses_ssh_key": bool(managed_account.get("uses_ssh_key")),
        }, sort_keys=True),
        created_by=created_by,
    )
    db.add(row)
    db.commit()
    logger.info("Registered %s database %s (%s) at %s", engine, row.id, cloud, host)
    return _serialize(row)


def get_database(db: Session, db_id: str) -> Optional[dict]:
    """One serialized row, or None. Lets a caller branch on ``source`` without reaching
    for the ORM itself."""
    row = db.query(CloudDatabase).filter(CloudDatabase.id == db_id).first()
    return _serialize(row) if row else None


def deregister_database(db: Session, db_id: str) -> None:
    """Drop a registered database from the inventory.

    Deliberately not a decommission: the dashboard did not create this database and has
    no Terraform state for it, so there is nothing to destroy and destroying would be
    the wrong verb for someone else's data. :func:`run_decommission` stays the path for
    provisioned rows."""
    row = db.query(CloudDatabase).filter(CloudDatabase.id == db_id).first()
    if not row:
        raise CloudDatabaseError(f"database {db_id} not found")
    if row.source != "registered":
        raise CloudDatabaseError(
            f"database {db_id} was provisioned by the dashboard — it is decommissioned "
            "(Terraform destroy), not deregistered")
    db.delete(row)
    db.commit()
    logger.info("Deregistered database %s (%s)", db_id, row.private_host)


def managed_account_ref(row) -> dict:
    """The Password Safe system/account this registered row was registered with."""
    raw = (row.credentials_ref or "")
    if not raw.startswith(_MANAGED_REF_PREFIX):
        raise CloudDatabaseError(
            f"database {row.id} has no Password Safe managed account recorded — "
            "re-register it with an account so the credential can be checked out")
    return json.loads(raw[len(_MANAGED_REF_PREFIX):])


async def _registered_connection_vars(row) -> dict:
    """Connection vars for a registered database, credential checked out just-in-time.

    Mirrors the VM path in ``ansible_local_run_service``: check out against the pinned
    system/account, use the value inline, and let the request expire on its duration.
    That path only checks a request back in when it made an ephemeral store copy — the
    inline runners (local, ACI) leave expiry to Password Safe, and this is one of those.
    """
    from . import btapi_service
    ref = managed_account_ref(row)
    duration = int(_cfg("ansible_managed_request_duration_min") or 60)
    try:
        _req_id, credential = await btapi_service.get_ps_credential_with_request(
            ref["system_id"], ref["account_id"], duration_min=duration,
            uses_ssh_key=ref.get("uses_ssh_key", False))
    except btapi_service.BTAPIError as exc:
        raise CloudDatabaseError(
            f"Password Safe checkout failed for database {row.id}: {exc}") from exc
    if not credential:
        raise CloudDatabaseError(
            f"Password Safe returned an empty credential for database {row.id}")

    engine = row.engine
    # connection_db_name already encodes the registered asymmetry: the operator's
    # own entry wins and master is only the fallback.
    db_name = connection_db_name(row)
    from . import managed_accounts as ma
    return {
        "db_engine": engine,
        "db_login_host": row.private_host or "",
        "db_login_port": row.port or _DEFAULT_PORTS.get(engine),
        # The account name is the login identity, minus any cloud-plugin scope suffix —
        # the same normalization the SSH path applies before using it as ansible_user.
        "db_login_user": ma.ssh_login_user(ref.get("account_name") or ""),
        "db_login_password": credential,
        "db_name": db_name,
    }


async def ansible_connection_vars(db: Session, db_id: str) -> dict:
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
      - db_name  — :func:`connection_db_name`: ``master`` for SQL Server (you connect
                   to ``master``; RDS omits a db_name), the ADB name for Oracle, else
                   the recorded catalog. **Not** the Entitle grant scope — see
                   ``cloud_db_adapter_service._database_name``.

    The returned keys are engine-independent so one sample playbook maps them onto any
    module's args (``login_host: "{{ db_login_host }}"`` …). Raises
    :class:`CloudDatabaseError` when the row or its admin credential can't be resolved."""
    row = db.query(CloudDatabase).filter(CloudDatabase.id == db_id).first()
    if not row:
        raise CloudDatabaseError(f"database {db_id} not found")
    # A registered database has no Terraform state and no provisioning job, so there is
    # no tf_variables to read a credential out of. It carries a Password Safe managed
    # account instead, checked out at run time. Everything below this line is the
    # provisioned path, unchanged.
    if row.source == "registered":
        return await _registered_connection_vars(row)
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

    # The play connects as the admin, so this is the admin-session catalog — the same
    # one the tunnel targets. connection_db_name already carries the Oracle fallback.
    db_name = connection_db_name(row, tfv)

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
        # Drives the delete verb (deregister vs decommission) and the UI badge.
        "source": r.source or "provisioned", "db_name": r.db_name,
        # db_name is the raw catalog (NULL where no user database exists);
        # connect_db_name is what you actually open a session against. Both are
        # projected because the page shows the pair when they differ — see
        # connection_db_name for why the substitution is never stored.
        "connect_db_name": connection_db_name(r),
        "entitle_integration_id": r.entitle_integration_id,
        "entitle_viable": _entitle_viable(r.engine, r.provider, r.source),
        # Password Safe DB onboarding. The ids are not secrets — they are what an
        # operator reads back in the Password Safe UI — and ps_viable is the STRUCTURAL
        # half of the button's gate (see _ps_ineligible_reason); whether the feature is
        # switched on is answered separately, by the page's password_safe_enabled and
        # then by the API.
        "ps_managed_system_id": r.ps_managed_system_id or "",
        "ps_managed_account_id": r.ps_managed_account_id or "",
        "ps_onboarded": bool(r.ps_managed_system_id),
        "ps_viable": _ps_ineligible_reason(r) is None,
        # The db_grant Entitle adapter (a Cloud Function deployed beside the database).
        # adapter_viable is the structural half of the row button's gate, from the same
        # function the API rejects with; adapter_fn_id is DECLARED here and filled in by
        # list_databases, because answering "does one already exist?" needs a Session and
        # this projection is pure by contract (see connection_db_name). A key the row
        # template reads must exist here either way.
        "adapter_viable": cloud_db_adapter_service.adapter_ineligible_reason(r) is None,
        "adapter_fn_id": "",
        "adapter_status": "",
        "created_by": r.created_by,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        # Which remote agent brokers Config-Management runs against this database, or None
        # for one the dashboard reaches itself. An id, not a credential — safe to project.
        "agent_id": getattr(r, "agent_id", None),
    }
