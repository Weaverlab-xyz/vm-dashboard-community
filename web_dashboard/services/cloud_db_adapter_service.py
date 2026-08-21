"""Pair a provisioned cloud database with a Cloud Functions Entitle adapter.

Entitle's native database connectors cannot serve two of the three engines this
dashboard provisions:

  MySQL       its connector assigns persistent roles and never mints an account,
              so ``allow_creating_accounts`` is forced off — no JIT account at all.
  SQL Server  its connector needs sysadmin/CONTROL SERVER, which managed Azure SQL /
              RDS / Cloud SQL do not grant. ``cloud_database_service`` blocks
              registration outright for exactly this reason.

A REST adapter sidesteps both, because it does not use those connectors: the
``db_grant`` Cloud Function implements Entitle's Remote Adapter contract itself and
runs the SQL directly, from inside the VPC/VNet.

This module owns the pairing — pushing the admin credential to the cloud's own
secret store, deploying the adapter beside the database, and registering it — so
``cloud_database_service`` does not grow a third registration path.

Postgres is deliberately NOT paired by default: its native connector works, and
replacing a working integration with a new moving part buys nothing.
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session

from ..database import CloudDatabase
from . import config_service, job_service

logger = logging.getLogger(__name__)


class AdapterPairingError(Exception):
    pass


# Engines whose NATIVE connector cannot do just-in-time accounts, with the reason
# stated in the user's terms — these strings surface in the job log and the UI.
_ADAPTER_REQUIRED = {
    "mysql": ("Entitle's MySQL connector assigns persistent roles and never mints an "
              "account, so it cannot provide just-in-time access"),
    "sqlserver": ("Entitle's SQL Server connector requires sysadmin/CONTROL SERVER, "
                  "which managed SQL Server does not grant"),
}

# SQL Server's managed flavors are NOT interchangeable and this mapping is the crux
# of the whole feature: Azure SQL Database is a contained-database model, so the
# login goes in master and the user in the target database, over two connections.
# Getting this wrong produces SQL that runs on the wrong flavor and fails at the
# first grant. See cloud_db_sql_service.VALID_FLAVORS.
_SQLSERVER_FLAVOR = {"aws": "rds", "azure": "azure_sql", "gcp": "cloudsql"}

# Where the admin credential is staged so the FUNCTION can read it. Never passed to
# the function as a value — each cloud resolves a reference.
_SECRET_BACKEND = {"aws": "aws_sm", "azure": "azure_kv", "gcp": "gcp_sm"}

_ADAPTER_WORKLOAD = "db_grant"


def adapter_required(engine: str) -> Optional[str]:
    """Why this engine needs an adapter rather than the native connector, or None."""
    return _ADAPTER_REQUIRED.get(engine)


def flavor_for(engine: str, cloud: str) -> str:
    """The SQL dialect flavor for ``(engine, cloud)``. Empty for non-SQL-Server."""
    if engine != "sqlserver":
        return ""
    flavor = _SQLSERVER_FLAVOR.get(cloud)
    if not flavor:
        raise AdapterPairingError(
            f"no SQL Server flavor known for cloud {cloud!r} — the adapter would "
            "emit statements for the wrong dialect")
    return flavor


def adapter_name(row: CloudDatabase) -> str:
    """A deterministic function name for this database's adapter.

    Derived from the database id so re-pairing finds the same function instead of
    accumulating one per attempt.
    """
    from . import cloud_function_service
    return cloud_function_service.normalize_name(f"jit-{row.engine}-{row.id[:8]}")


def secret_key(row: CloudDatabase) -> str:
    """Key for the staged admin credential, in the cloud's secret store."""
    return f"clouddb-{row.id}-admin"


def build_environment(row: CloudDatabase, *, admin_username: str,
                      database: str, dry_run: bool = True) -> dict:
    """The NON-SECRET environment the adapter needs to find its database.

    Pure — every value is passed in. Everything the function is told about its
    target comes from here rather than from a request, which is what stops a caller
    redirecting a grant at another database.
    """
    engine = row.engine
    environment = {
        "FN_DB_ENGINE": engine,
        "FN_DB_HOST": row.private_host or "",
        "FN_DB_PORT": str(row.port or ""),
        "FN_DB_NAME": database,
        "FN_DB_ADMIN_USER": admin_username,
        # Dry run stays ON unless the operator explicitly arms the adapter. The same
        # observe-first default as notify_dry_run and resource_expiry_dry_run: the
        # first thing you get is the SQL it WOULD run, not a changed database.
        "FN_DB_DRY_RUN": "0" if not dry_run else "1",
    }
    flavor = flavor_for(engine, row.cloud)
    if flavor:
        environment["FN_DB_FLAVOR"] = flavor
    return environment


def _database_name(tf_variables: dict, engine: str,
                   row: Optional[CloudDatabase] = None) -> str:
    """The database grants are scoped to.

    The provisioning job's tf_variables stay the first source, so every pairing that
    resolves today resolves to exactly the same catalog. ``cloud_databases.db_name``
    is the fallback for when that job is gone — it now holds the same value, stamped at
    provision time. SQL Server's modules name it differently, and Azure SQL's is the
    database resource rather than an initial-catalog setting.

    Every source is a *recorded* value. There is deliberately no engine default: RDS
    SQL Server creates no user database, and ``master`` is the system catalog, so
    substituting it would scope grants at the whole instance instead of refusing.

    Deliberately **not** ``cloud_database_service.connection_db_name``, which is the
    other half of the same question and answers ``master`` for every provisioned SQL
    Server. That one is the admin-session catalog (where an admin connects, and where
    server-level principals live); this one is the grant scope. They diverge on Azure
    and GCP, whose modules really do create a user catalog, and unifying them would
    rescope every SQL Server grant there from the user's database to the system
    catalog: db_datareader/db_datawriter are DATABASE-level fixed roles. The ``master``
    half is not lost — for the azure_sql contained-database model, cloud_db_sql_service
    derives the master connection it needs to CREATE LOGIN itself (_SPLIT_LOGIN_FLAVORS).
    """
    for key in ("db_name", "database_name", "initial_catalog"):
        value = (tf_variables or {}).get(key)
        if value:
            return str(value)
    if row is not None and row.db_name:
        return str(row.db_name)
    raise AdapterPairingError(
        f"cannot determine the database name for this {engine} instance from its "
        "provisioning job — the adapter would have nothing to scope grants to")


def _stage_admin_secret(row: CloudDatabase, admin_password: str) -> dict:
    """Write the admin credential to the cloud's own secret store and return the
    ``secret_environment`` entry the function resolves it through.

    The dashboard holds this password encrypted in ``app_config``, which the function
    cannot read — it is in a different trust domain by design. Staging it in the
    cloud's store is what lets each platform resolve it for the function without the
    value ever passing through Terraform state, the job record, or the function's own
    settings page.

    Only the REFERENCE differs per cloud, and turning it into the right mechanism is
    ``cloud_function_service._secret_environment``'s job, not this one's.
    """
    from . import secrets_backend_service

    backend = _SECRET_BACKEND.get(row.cloud)
    if not backend:
        raise AdapterPairingError(f"no secret backend for cloud {row.cloud!r}")
    key = secret_key(row)
    name = secrets_backend_service.write_sync(backend, key, admin_password)

    if row.cloud == "aws":
        # The ARN, not the name: the role's policy names ARNs, and AWS appends a
        # random six-character suffix to every one, so the name cannot be turned into
        # one by string concatenation. Ask for it rather than wildcarding the policy.
        return {"FN_DB_ADMIN_PASSWORD": _aws_secret_arn(name)}
    # GCP takes the Secret Manager secret id; Azure takes the Key Vault secret name.
    return {"FN_DB_ADMIN_PASSWORD": name}


def _aws_secret_arn(name: str) -> str:
    import boto3
    from . import aws_service
    client = boto3.client("secretsmanager",
                          **aws_service._aws_kwargs(config_service.get("aws_region") or ""))
    return str(client.describe_secret(SecretId=name)["ARN"])


def start_pairing(db: Session, *, row: CloudDatabase, created_by: str = "",
                  dry_run: bool = True) -> dict:
    """Queue a ``clouddb_adapter_pair`` job. Validates before queuing so an
    impossible pairing fails at the click, not three minutes into a job."""
    if not row.private_host:
        raise AdapterPairingError(
            "database has no endpoint yet — wait for provisioning to finish")
    flavor_for(row.engine, row.cloud)          # raises on an unknown combination
    job = job_service.create_job(
        db, job_type="clouddb_adapter_pair", created_by=created_by,
        metadata={"db_id": row.id, "engine": row.engine, "cloud": row.cloud,
                  "dry_run": bool(dry_run)})
    return {"ok": True, "db_id": row.id, "job_id": job.id}


async def run_pairing(db: Session, *, db_id: str, job_id: str,
                      dry_run: bool = True) -> None:
    """Stage the credential, deploy the adapter beside the database, register it.

    One job for all three stages: they are useless individually, and an operator
    watching a half-finished pairing cannot tell whether to retry or clean up.
    """
    from . import cloud_function_service

    row = db.query(CloudDatabase).filter(CloudDatabase.id == db_id).first()
    if not row:
        job_service.set_failed(db, job_id, f"database {db_id} not found")
        return
    job_service.set_running(db, job_id)
    try:
        admin_username, admin_password, tf_variables = _admin_credentials(db, row)
        database = _database_name(tf_variables, row.engine, row)

        job_service.update_progress(db, job_id, 15, "Staging the admin credential…")
        secret_environment = _stage_admin_secret(row, admin_password)

        environment = build_environment(row, admin_username=admin_username,
                                        database=database, dry_run=dry_run)

        job_service.update_progress(db, job_id, 30, "Deploying the adapter function…")
        deployed = cloud_function_service.deploy(
            db, cloud=row.cloud, region=row.region, name=adapter_name(row),
            workload=_ADAPTER_WORKLOAD, created_by="clouddb-pairing",
            # vpc, always: the whole point is reaching a database that has no public
            # endpoint. A public adapter would deploy fine and fail every grant.
            network_mode="vpc",
            environment=environment,
            secret_environment=secret_environment)
        fn_id = deployed["fn_id"]
        await cloud_function_service.run_deploy_apply(
            db, fn_id=fn_id, job_id=deployed["job_id"],
            tf_variables=deployed["tf_variables"])

        fn_row = cloud_function_service.get_function(db, fn_id)
        if not fn_row or fn_row.status != "available":
            raise AdapterPairingError(
                f"adapter function did not deploy (status: "
                f"{getattr(fn_row, 'status', 'missing')}) — see its job for the "
                "terraform output")

        job_service.update_progress(db, job_id, 75, "Registering the adapter in Entitle…")
        register = cloud_function_service.start_entitle_register(
            db, fn_id, action="register", created_by="clouddb-pairing")
        await cloud_function_service.run_entitle_register(
            db, fn_id=fn_id, job_id=register["job_id"], action="register")

        db.refresh(fn_row)
        row.entitle_integration_id = fn_row.entitle_integration_id
        db.commit()

        job_service.set_completed(db, job_id, {
            "db_id": db_id, "fn_id": fn_id,
            "entitle_integration_id": fn_row.entitle_integration_id,
            "dry_run": bool(dry_run),
        })
        logger.info("clouddb adapter paired db_id=%s fn_id=%s integration=%s dry_run=%s",
                    db_id, fn_id, fn_row.entitle_integration_id, dry_run)
    except Exception as exc:
        logger.error("clouddb adapter pairing failed db_id=%s: %s", db_id, exc)
        job_service.set_failed(db, job_id, str(exc))


def _admin_credentials(db: Session, row: CloudDatabase) -> tuple:
    """``(username, password, tf_variables)`` for the provisioned admin.

    Mirrors ``cloud_database_service._entitle_register_core``'s resolution, including
    the Cloud SQL SQL Server quirk where the admin login is forced to 'sqlserver'.
    """
    from .cloud_database_service import _provision_job_for
    job = _provision_job_for(db, row.id)
    tfv = ((job.metadata_dict or {}).get("tf_variables") if job else None) or {}
    default_user = "sqlserver" if (row.engine == "sqlserver" and row.cloud == "gcp") else "dbadmin"
    username = (tfv.get("master_username") or tfv.get("administrator_login")
                or default_user)
    password = (tfv.get("master_password") or tfv.get("administrator_password")
                or tfv.get("admin_password")
                or config_service.get(f"clouddb/{row.id}/admin") or "")
    if not password:
        raise AdapterPairingError(
            "no admin credential for this database — it cannot be paired with an "
            "adapter (a registered, rather than provisioned, database has none)")
    return username, password, tfv
