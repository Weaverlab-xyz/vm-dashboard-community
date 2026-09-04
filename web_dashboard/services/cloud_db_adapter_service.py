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

# For the staging failure message: the Secrets page panel an operator has to go fix,
# spelled as that page spells it — "gcp_sm" is the internal key, not a place.
_BACKEND_LABEL = {"aws_sm": "AWS Secrets Manager", "azure_kv": "Azure Key Vault",
                  "gcp_sm": "GCP Secret Manager"}

# The Cloud Functions workload that IS the adapter. Public: the Databases page needs
# it to find a row's existing adapter, and a second spelling of "db_grant" is how the
# lookup and the deploy drift apart.
ADAPTER_WORKLOAD = "db_grant"


def _entitle_registration_enabled() -> bool:
    """Whether the Entitle integration is switched on. Read at run time, not import
    time, because config_service is backed by the app_config table."""
    return config_service.get_bool("entitle_registration_enabled", False)


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


def adapter_ineligible_reason(row: CloudDatabase) -> Optional[str]:
    """Why this database cannot be paired with a ``db_grant`` adapter, or None.

    Single source of truth for all three consumers — the row button (via
    ``cloud_database_service._serialize``'s ``adapter_viable``), the API pre-flight, and
    :func:`start_pairing` — so the button can never offer what the endpoint refuses and
    neither can queue a job the pairing would fail.

    STRUCTURAL blockers only: facts about the row that no amount of configuration
    changes. Whether Cloud Functions is switched on, and whether the row's region has a
    per-region config set to place the function in, are configuration-time questions the
    API answers separately (409). ``status`` is likewise left out — the buttons test it
    in their own x-show, as the Password Safe and Entitle ones do.

    One deliberate strictness: the ``db_name`` check reads the COLUMN, while
    :func:`_database_name` prefers the provisioning job's tf_variables and only falls
    back to the column. In practice they agree — every ``_build_tf_variables`` branch
    emits ``db_name``, and ``provision`` stamps exactly that onto the row, so the
    ``database_name``/``initial_catalog`` spellings _database_name also accepts are
    defensive rather than reachable. The gap is legacy rows provisioned before the
    column existed, which ``backfill_provisioned_db_names`` is there to close; until it
    runs, such a row is refused here rather than offered. That is the safe direction —
    never offer a button that may refuse.
    """
    if not adapter_required(row.engine):
        return (f"{row.engine or 'this engine'} keeps Entitle's native database "
                "connector, which does just-in-time accounts on its own. An adapter "
                "would replace a working integration with a new moving part.")
    if (row.source or "provisioned") == "registered":
        return ("no admin credential for this database — it cannot be paired with an "
                "adapter (a registered, rather than provisioned, database has none)")
    if row.cloud not in _SECRET_BACKEND:
        return (f"no secret backend for cloud {row.cloud!r} — the adapter reads its "
                "admin credential from the cloud's own secret store")
    try:
        flavor_for(row.engine, row.cloud)
    except AdapterPairingError as exc:
        return str(exc)
    if not row.private_host:
        return "database has no endpoint yet — wait for provisioning to finish"
    if not row.db_name:
        return (f"cannot determine the database name for this {row.engine} instance "
                "— the adapter would have nothing to scope grants to")
    return None


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

    Retired by :func:`retire_admin_secret` when the database is decommissioned. Until
    that existed, every pairing left this password live in the cloud's store forever.
    """
    from . import secrets_backend_service

    backend = _SECRET_BACKEND.get(row.cloud)
    if not backend:
        raise AdapterPairingError(f"no secret backend for cloud {row.cloud!r}")
    key = secret_key(row)
    try:
        name = secrets_backend_service.write_sync(backend, key, admin_password)
    except Exception as exc:
        # The job detail view shows error_message and nothing else, so a bare SDK
        # error here reads as a broken secret store: GCP's is a protobuf dump naming
        # only secretmanager.googleapis.com, Azure's a 403 on a vault URL. Name the
        # stage and the backend, because staging is the one step whose prerequisite
        # (a configured secret store) is not implied by having provisioned the
        # database at all.
        raise AdapterPairingError(
            f"could not stage the admin credential in the {backend} secret store, "
            f"which the adapter reads it from — check Secrets → "
            f"{_BACKEND_LABEL.get(backend, backend)} and use its Test button: {exc}"
        ) from exc

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


# ── Teardown: the two things a pairing leaves behind ──────────────────────────
#
# Both live here rather than in cloud_database_service because this module owns the
# names: the backend + key mapping for the credential, and adapter_name/
# ADAPTER_WORKLOAD for the function. A teardown that spelled either one itself would
# be the version that drifts.


def _secret_backend_for(row: CloudDatabase) -> str:
    """The store a pairing would have staged this row's credential in, or ``""`` when
    the row could never have had one.

    The same three facts :func:`_stage_admin_secret` runs under, and all of them are
    stable properties of the row rather than of the current configuration — so this
    answers "was a credential ever staged for this database", not "could one be staged
    today". Postgres (the default, and most rows) short-circuits here, before any
    config is read or any cloud is called.
    """
    if not _SECRET_BACKEND.get(row.cloud or ""):
        return ""
    if not adapter_required(row.engine or ""):
        return ""
    if (row.source or "provisioned") == "registered":
        return ""
    return _SECRET_BACKEND[row.cloud]


def _already_gone(exc: Exception) -> bool:
    """Whether a delete failed because there was nothing there.

    Three SDKs, three shapes, and importing all three to catch them by class would
    make an absent optional dependency the reason a teardown reports a leaked
    credential. Matched structurally instead: google.api_core's NotFound carries
    ``code == 404``, azure.core's ResourceNotFoundError ``status_code == 404``, and
    botocore's ClientError the error code in its response dict. The class names are
    the belt to those braces — nothing else in these SDKs is called NotFound.
    """
    if type(exc).__name__ in ("NotFound", "ResourceNotFoundError",
                              "ResourceNotFoundException"):
        return True
    if getattr(exc, "code", None) == 404 or getattr(exc, "status_code", None) == 404:
        return True
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        code = (response.get("Error") or {}).get("Code")
        return code in ("ResourceNotFoundException", "ResourceNotFound")
    return False


def retire_admin_secret(row: CloudDatabase) -> str:
    """Delete the admin credential :func:`_stage_admin_secret` put in the cloud's own
    secret store. Returns the ref it removed, or ``""`` when there was nothing to do.

    Blocking (three cloud SDKs); call it off the event loop.

    Absence is the NORMAL outcome, not a failure: a database that was never paired has
    no staged secret, and neither has one whose secret an operator already deleted by
    hand. Anything else raises, with the ref in the message — what is left behind is a
    live database admin password in a store the database itself no longer justifies,
    and the four found orphaned in the lab project on 2026-09-04 were invisible
    precisely because nothing ever said so.
    """
    from . import secrets_backend_service

    backend = _secret_backend_for(row)
    if not backend:
        return ""
    # The ref is the WRITER's, via secrets_backend_service.ref_for: each store mangles
    # the key on the way in and the mangled form is what identifies the secret
    # afterwards. On GCP that is dashboard-clouddb-<id>-admin, not the
    # clouddb-<id>-admin this module handed to write_sync — a delete addressed at the
    # key would 404 forever while the credential stayed live.
    #
    # Derived inside the try, and the key stands in for it in the message if that
    # fails: an unaddressable secret still has to be named, or the one error an
    # operator cannot act on is the one telling them a password was left behind.
    key = secret_key(row)
    ref = ""
    try:
        ref = secrets_backend_service.ref_for(backend, key)
        secrets_backend_service.delete_sync(backend, ref)
    except Exception as exc:
        if _already_gone(exc):
            logger.info("clouddb adapter: no staged credential to retire for db_id=%s",
                        row.id)
            return ""
        raise AdapterPairingError(
            f"the adapter's staged admin credential {ref or key} in "
            f"{_BACKEND_LABEL.get(backend, backend)} was not deleted — it holds a "
            f"database password, remove it by hand: {exc}") from exc
    # Neither the ref nor the backend is logged HERE, and that is deliberate rather
    # than incidental. Both descend from a name CodeQL classifies as sensitive
    # (`secret_key`, `_SECRET_BACKEND`), so logging either makes
    # py/clear-text-logging-sensitive-data read the NAME of the secret as the secret —
    # which it is right to be suspicious of, and which suppressing would waste. Nothing
    # is lost: the caller in cloud_database_service logs the returned ref as
    # ``resource_id``, and the failure path above names it in the job, where an
    # operator with a credential to remove by hand actually looks.
    logger.info("clouddb adapter: retired the staged credential for db_id=%s", row.id)
    return ref


async def retire_adapter(db: Session, row: CloudDatabase, *,
                         created_by: str = "clouddb-decommission") -> str:
    """Deregister and destroy this row's ``db_grant`` adapter function, if it has one.
    Returns the function id it removed, or ``""`` when the row was never paired.

    An adapter outlives its database as a running, billable function that can only
    ever fail, still holding the IAM binding on the credential above. Entitle first,
    because the integration is the outward-facing half — left behind it stays in
    Entitle's catalogue and errors on every grant — but a failure there does not skip
    the destroy: the function is the part that costs money either way.
    """
    from . import cloud_function_service

    name = adapter_name(row)
    fn = cloud_function_service.find_by_names(
        db, [name], workload=ADAPTER_WORKLOAD).get(name)
    if fn is None:
        return ""
    fn_id = fn.id
    problems: list = []

    if fn.entitle_integration_id:
        try:
            job = cloud_function_service.start_entitle_register(
                db, fn_id, action="deregister", created_by=created_by)
            await cloud_function_service.run_entitle_register(
                db, fn_id=fn_id, job_id=job["job_id"], action="deregister")
        except Exception as exc:                                # pragma: no cover
            problems.append(f"Entitle deregistration raised: {exc}")
        db.refresh(fn)
        # run_entitle_register reports through the job, not by raising, and clears the
        # id only when the removal really happened — so the column is the outcome.
        if fn.entitle_integration_id:
            problems.append(
                f"the adapter's Entitle integration {fn.entitle_integration_id} was "
                f"not removed — see the cloudfn_entitle_register job")

    try:
        job = cloud_function_service.start_decommission(db, fn_id, created_by=created_by)
        await cloud_function_service.run_decommission(
            db, fn_id=fn_id, job_id=job["job_id"])
    except Exception as exc:
        problems.append(f"adapter function destroy raised: {exc}")
    db.refresh(fn)
    if fn.status != "deleted":
        problems.append(
            f"the adapter function {name} was not destroyed (status: "
            f"{fn.status or 'unknown'}) — it is still billable and can only fail now; "
            f"delete it from the Cloud Functions page")

    if problems:
        raise AdapterPairingError("; ".join(problems))
    logger.info("clouddb adapter: retired adapter %s fn_id=%s db_id=%s",
                name, fn_id, row.id)
    return fn_id


def start_pairing(db: Session, *, row: CloudDatabase, created_by: str = "",
                  dry_run: bool = True) -> dict:
    """Queue a ``clouddb_adapter_pair`` job. Validates before queuing so an
    impossible pairing fails at the click, not three minutes into a job."""
    reason = adapter_ineligible_reason(row)
    if reason:
        raise AdapterPairingError(reason)
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
            workload=ADAPTER_WORKLOAD, created_by="clouddb-pairing",
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

        # The Entitle leg is skippable, and only this leg: an adapter that is deployed,
        # VPC-attached and pointed at its database is useful on its own, and refusing to
        # deploy one because the Entitle integration happens to be switched off would
        # make the row button dead weight on exactly the installs still being set up.
        # The provision-time caller is unaffected — _pair_adapter only fires when
        # registration is already enabled.
        entitle_skipped = not _entitle_registration_enabled()
        if entitle_skipped:
            job_service.update_progress(
                db, job_id, 90,
                "Adapter deployed. Entitle registration is disabled — register it from "
                "the Cloud Functions page once entitle_registration_enabled is set.")
        else:
            job_service.update_progress(db, job_id, 75,
                                        "Registering the adapter in Entitle…")
            register = cloud_function_service.start_entitle_register(
                db, fn_id, action="register", created_by="clouddb-pairing")
            await cloud_function_service.run_entitle_register(
                db, fn_id=fn_id, job_id=register["job_id"], action="register")

            db.refresh(fn_row)
            # Only stamped when the integration really exists — the column is shared
            # with the native-connector path and a placeholder would read as registered.
            row.entitle_integration_id = fn_row.entitle_integration_id
            db.commit()

        job_service.set_completed(db, job_id, {
            "db_id": db_id, "fn_id": fn_id,
            "entitle_integration_id": fn_row.entitle_integration_id,
            "entitle_skipped": entitle_skipped,
            "dry_run": bool(dry_run),
        })
        logger.info("clouddb adapter paired db_id=%s fn_id=%s integration=%s dry_run=%s "
                    "entitle_skipped=%s", db_id, fn_id, fn_row.entitle_integration_id,
                    dry_run, entitle_skipped)
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
