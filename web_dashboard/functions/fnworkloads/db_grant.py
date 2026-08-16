"""Ephemeral just-in-time database accounts — the Phase 2 flagship.

Give Access mints a short-lived account on a private database and returns its
credentials; Revoke Access drops it. This is the case Entitle's native connectors
cannot serve: its MySQL connector assigns persistent roles and never mints an
account, and its SQL Server connector's ephemeral accounts assume a server-level
LOGIN plus USE, which is not how Azure SQL Database works.

    POST { "action": "grant",  "user_email": "alice@example.com",
           "role": "read", "request_id": "..." }
      → 200 { "ok": true, "username": "...", "password": "...", "host": ..., ... }

    POST { "action": "revoke", "username": "jit_alice_ab12" }
      → 200 { "ok": true, "revoked": "jit_alice_ab12" }

**The SQL is not written here.** It comes from the dashboard's
``cloud_db_sql_service.grant_plan`` / ``revoke_plan``, which are pure, hard
allowlisted, and exhaustively unit-tested offline (tests/test_db_grant_sql.py) —
including the Azure SQL two-connection split. This module is copied into the zip
alongside a vendored copy of those builders (see ``_sqlplan``), so the SQL that
runs in the cloud is byte-identical to the SQL under test.

Dry run is the DEFAULT. With ``FN_DB_DRY_RUN`` unset or truthy the workload returns
the exact statements it would execute and opens no connection, which is how the
whole Entitle path gets validated before anything touches a real database.

Credentials: the admin password is never passed in the request. It is resolved
per-cloud OUTSIDE this code where the platform can do it — GCP secret env vars,
an Azure Key Vault app-setting reference — falling back to a boto3 Secrets Manager
read on AWS, which is the one cloud with no platform-resolved equivalent (and the
one whose runtime already ships an SDK).
"""
import os
import secrets
import string

from fnruntime import logs
from fnruntime.contract import Context, Request, Response

NAME = "db_grant"
DESCRIPTION = "Mint and drop ephemeral just-in-time database accounts (MySQL / SQL Server)."

# The SQL builders. In the zip the packager copies web_dashboard/services/
# cloud_db_sql_service.py in as `sqlplan.py` — the file itself, not a
# reimplementation, so the SQL that runs in the cloud is byte-identical to the SQL
# tests/test_db_grant_sql.py exercises. In-repo (the dashboard's workload catalog,
# and this module's own tests) that name does not exist, so fall back to importing
# the service directly. The two-context import is the honest expression of the two
# contexts this module genuinely runs in.
try:
    import sqlplan as _sqlplan  # noqa: E402
except ImportError:  # pragma: no cover - exercised in-repo, never in the zip
    from web_dashboard.services import cloud_db_sql_service as _sqlplan  # noqa: E402

_TRUTHY = ("1", "true", "yes", "on")
_CONNECT_TIMEOUT = 10


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name, "") or default).strip()


def _dry_run() -> bool:
    """Default ON. Executing SQL against a production database is not the safe
    default for a feature whose payload schema is still being pinned."""
    raw = os.environ.get("FN_DB_DRY_RUN")
    return True if raw is None else raw.strip().lower() in _TRUTHY


def _admin_password() -> str:
    """The DB admin password, resolved without it ever being a request field.

    GCP injects it as a secret env var and Azure resolves a Key Vault reference in
    app settings — both happen before this code runs, so both land in FN_DB_ADMIN_PASSWORD.
    AWS has no platform-resolved equivalent for Lambda, so read Secrets Manager
    directly; boto3 is already in the runtime, which is why local_account_broker
    and this workload are the only two allowed to touch a cloud SDK at all.
    """
    direct = _env("FN_DB_ADMIN_PASSWORD")
    if direct:
        return direct
    secret_id = _env("FN_DB_ADMIN_SECRET_ID")
    if not secret_id:
        raise RuntimeError(
            "no database admin credential available: set FN_DB_ADMIN_PASSWORD "
            "(GCP secret env var / Azure Key Vault reference) or FN_DB_ADMIN_SECRET_ID "
            "(AWS Secrets Manager)")
    import boto3  # noqa: PLC0415 — Lambda ships it; the other clouds never reach here
    client = boto3.client("secretsmanager", region_name=_env("AWS_REGION") or None)
    value = client.get_secret_value(SecretId=secret_id)
    payload = value.get("SecretString") or ""
    if payload.startswith("{"):
        import json
        parsed = json.loads(payload)
        return str(parsed.get("password") or parsed.get("admin_password") or "")
    return payload


def _generate_password(length: int = 24) -> str:
    """Mirrors cloud_db_sql_service.generate_password: only characters the SQL
    builders' value allowlist accepts, and enough classes for SQL Server."""
    symbols = "#-_"
    pools = [string.ascii_lowercase, string.ascii_uppercase, string.digits, symbols]
    alphabet = string.ascii_letters + string.digits + symbols
    chars = [secrets.choice(p) for p in pools]
    chars += [secrets.choice(alphabet) for _ in range(max(length, 8) - len(pools))]
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


def _connect(engine: str, *, host: str, port: int, database: str,
             user: str, password: str):
    """One TLS connection to ``database``. Encryption is not optional: Azure
    Flexible Server and Azure SQL both refuse plaintext, and a JIT credential is
    exactly the traffic you least want on the wire in the clear."""
    if engine == "mysql":
        import pymysql
        return pymysql.connect(
            host=host, port=port, user=user, password=password,
            database=database or None, connect_timeout=_CONNECT_TIMEOUT,
            ssl={"ssl": {}},          # stdlib ssl; PyMySQL needs no cryptography over TLS
            autocommit=True)
    if engine == "sqlserver":
        import pytds
        return pytds.connect(
            server=host, port=port, database=database or "master",
            user=user, password=password, login_timeout=_CONNECT_TIMEOUT,
            cafile=_env("FN_DB_CAFILE") or None,
            validate_host=False,      # managed endpoints present a platform cert chain
            autocommit=True)
    raise RuntimeError(f"unsupported engine {engine!r} (expected mysql or sqlserver)")


def _execute(plan, *, engine: str, host: str, port: int, admin_user: str,
             admin_password: str) -> int:
    """Run a plan. Each ``(database, statements)`` pair gets its OWN connection —
    which is not an optimisation choice: Azure SQL Database has no USE, so the
    login in ``master`` and the contained user in the target database genuinely
    cannot share one."""
    executed = 0
    for database, statements in plan:
        connection = _connect(engine, host=host, port=port, database=database,
                              user=admin_user, password=admin_password)
        try:
            cursor = connection.cursor()
            for statement in statements:
                cursor.execute(statement)
                executed += 1
            cursor.close()
        finally:
            try:
                connection.close()
            except Exception:
                pass
    return executed


def _target() -> dict:
    """Where and what to connect to, all from the function's own configuration —
    never from the request, so a caller cannot redirect the grant at another
    database."""
    engine = _env("FN_DB_ENGINE").lower()
    if engine not in ("mysql", "sqlserver"):
        raise RuntimeError(
            f"FN_DB_ENGINE must be mysql or sqlserver (got {engine!r})")
    host = _env("FN_DB_HOST")
    if not host:
        raise RuntimeError("FN_DB_HOST is not set")
    try:
        port = int(_env("FN_DB_PORT") or (3306 if engine == "mysql" else 1433))
    except ValueError:
        raise RuntimeError("FN_DB_PORT is not a number") from None
    return {
        "engine": engine,
        "host": host,
        "port": port,
        "database": _env("FN_DB_NAME"),
        "flavor": _env("FN_DB_FLAVOR"),
        "admin_user": _env("FN_DB_ADMIN_USER") or "dbadmin",
    }


def _plan_as_json(plan) -> list:
    return [{"database": db, "statements": list(stmts)} for db, stmts in plan]


def handle(req: Request, ctx: Context) -> Response:
    payload = req.json()
    action = str(payload.get("action") or "").strip().lower()
    if action in ("give", "give_access", "giveaccess", "grant"):
        action = "grant"
    elif action in ("revoke", "revoke_access", "revokeaccess", "remove"):
        action = "revoke"
    else:
        return Response(400, {"error": "action must be 'grant' or 'revoke'"})

    target = _target()
    engine, flavor = target["engine"], target["flavor"]
    dry_run = _dry_run()

    if action == "grant":
        identity = str(payload.get("user_email") or payload.get("userEmail")
                       or payload.get("user") or "").strip()
        if not identity:
            return Response(400, {"error": "grant needs user_email"})
        role = str(payload.get("role") or "read").strip().lower()
        token = str(payload.get("request_id") or payload.get("requestId")
                    or ctx.request_id)
        username = _sqlplan.ephemeral_username(identity, token)
        password = _generate_password()
        try:
            plan = _sqlplan.grant_plan(
                engine, username=username, password=password,
                database=target["database"], role=role, flavor=flavor)
        except _sqlplan.CloudDbSqlError as exc:
            return Response(400, {"error": str(exc)})

        logs.emit("info", "grant", request_id=ctx.request_id, action=action,
                  identity=identity, username=username, role=role,
                  engine=engine, flavor=flavor, dry_run=dry_run)

        if dry_run:
            # The password is NOT echoed in dry run: nothing was created, so a
            # credential in the response would be a secret with no account.
            return Response(200, {
                "ok": True, "dry_run": True, "action": "grant",
                "username": username, "role": role,
                "plan": _plan_as_json(plan), "request_id": ctx.request_id,
            })

        executed = _execute(plan, engine=engine, host=target["host"],
                            port=target["port"], admin_user=target["admin_user"],
                            admin_password=_admin_password())
        return Response(200, {
            "ok": True, "action": "grant",
            "username": username,
            "password": password,        # the only time this is ever returned
            "host": target["host"], "port": target["port"],
            "database": target["database"], "role": role,
            "statements_executed": executed, "request_id": ctx.request_id,
        })

    # Revoke. Entitle retries, and a revoke that errors leaves standing access
    # behind — the one outcome this feature exists to prevent — so the SQL is
    # existence-guarded and a missing account is a success, not a 404.
    username = str(payload.get("username") or "").strip()
    if not username:
        identity = str(payload.get("user_email") or payload.get("userEmail") or "").strip()
        token = str(payload.get("request_id") or payload.get("requestId") or "")
        if not identity or not token:
            return Response(400, {
                "error": "revoke needs username, or user_email plus the original request_id"})
        username = _sqlplan.ephemeral_username(identity, token)

    try:
        plan = _sqlplan.revoke_plan(engine, username=username,
                                    database=target["database"], flavor=flavor)
    except _sqlplan.CloudDbSqlError as exc:
        return Response(400, {"error": str(exc)})

    logs.emit("info", "revoke", request_id=ctx.request_id, action=action,
              username=username, engine=engine, flavor=flavor, dry_run=dry_run)

    if dry_run:
        return Response(200, {
            "ok": True, "dry_run": True, "action": "revoke",
            "username": username, "plan": _plan_as_json(plan),
            "request_id": ctx.request_id,
        })

    executed = _execute(plan, engine=engine, host=target["host"],
                        port=target["port"], admin_user=target["admin_user"],
                        admin_password=_admin_password())
    return Response(200, {
        "ok": True, "action": "revoke", "revoked": username,
        "statements_executed": executed, "request_id": ctx.request_id,
    })
