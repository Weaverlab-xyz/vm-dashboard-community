"""Entitle Remote Adapter for ephemeral just-in-time database accounts.

Implements the Remote Adapter contract
(docs.beyondtrust.com/entitle/docs/open-api-definition) against a private MySQL or
SQL Server instance, so Entitle can mint a short-lived account, grant it a role, and
take both away again. This is the case Entitle's native connectors cannot serve: its
MySQL connector assigns persistent roles and never mints an account, and its SQL
Server connector assumes a server-level LOGIN plus USE, which is not how Azure SQL
Database works.

Routes — **the verb is the PATH**, not a body field:

    GET  /get_assets                            what can be requested
    GET  /get_actors                            the accounts that exist now
    GET  /get_all_permissions                   who currently holds what
    GET  /get_asset_permissions/{identifier}    the same, for one asset
    POST /create_actor    {actor}               mint the ephemeral account
    POST /give_access     {asset, actor_identifier, role_code}
    POST /revoke_access   {asset, actor_identifier, role_code}
    POST /delete_actor    {actor_identifier}    drop the account
    POST /check_config    {config}              configuration self-test

Note there is **no TTL in any request**. Entitle owns expiry and calls revoke and
delete when the grant ends, so this adapter is entirely stateless with respect to
time — it never schedules anything and never needs to be told a duration.

**The SQL is not written here.** It comes from ``cloud_db_sql_service``'s four pure
plan builders, which mirror the four Entitle operations one-for-one and are
exhaustively unit-tested offline (tests/test_db_grant_sql.py), including the Azure
SQL two-connection split. That module is copied into the zip verbatim as
``sqlplan``, so the SQL that runs in the cloud is byte-identical to the SQL tested.

Dry run is the DEFAULT (``FN_DB_DRY_RUN``): every write route returns the exact
statements it would execute and opens no connection, which is how the whole Entitle
path gets validated before anything touches a real database.
"""
import os
import secrets
import string

from fnruntime import logs, secretref
from fnruntime.contract import Context, Request, Response

NAME = "db_grant"
DESCRIPTION = ("Entitle Remote Adapter: ephemeral just-in-time database accounts "
               "(MySQL / SQL Server).")

# See the note in the previous revision: in the zip the packager copies
# web_dashboard/services/cloud_db_sql_service.py in as sqlplan.py, so the SQL is the
# file under test rather than a reimplementation. In-repo that name does not exist.
try:
    import sqlplan as _sqlplan  # noqa: E402
except ImportError:  # pragma: no cover - exercised in-repo, never in the zip
    from web_dashboard.services import cloud_db_sql_service as _sqlplan  # noqa: E402

_TRUTHY = ("1", "true", "yes", "on")
_CONNECT_TIMEOUT = 10

# Entitle role_code → the access level the SQL builders understand. Exposed to
# Entitle through get_assets' role_options, so an operator picks from these rather
# than typing a string that silently means nothing.
_ROLE_CODES = {
    "read": ("Read only", ["SELECT"]),
    "readwrite": ("Read / write", ["SELECT", "INSERT", "UPDATE", "DELETE"]),
}


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name, "") or default).strip()


def _dry_run() -> bool:
    """Default ON. Executing SQL against a production database is not the safe
    default for an integration still being wired up."""
    raw = os.environ.get("FN_DB_DRY_RUN")
    return True if raw is None else raw.strip().lower() in _TRUTHY


def _target() -> dict:
    """Where and what to connect to — all from the function's own configuration.

    Never from the request. Entitle sends an ``asset`` object, and honouring a host
    or database from it would make every caller of this endpoint a lateral-movement
    primitive; the asset identifier is only ever CHECKED against what we serve.
    """
    engine = _env("FN_DB_ENGINE").lower()
    if engine not in ("mysql", "sqlserver"):
        raise RuntimeError(f"FN_DB_ENGINE must be mysql or sqlserver (got {engine!r})")
    host = _env("FN_DB_HOST")
    if not host:
        raise RuntimeError("FN_DB_HOST is not set")
    try:
        port = int(_env("FN_DB_PORT") or (3306 if engine == "mysql" else 1433))
    except ValueError:
        raise RuntimeError("FN_DB_PORT is not a number") from None
    database = _env("FN_DB_NAME")
    if not database:
        raise RuntimeError("FN_DB_NAME is not set")
    return {
        "engine": engine, "host": host, "port": port, "database": database,
        "flavor": _env("FN_DB_FLAVOR"), "admin_user": _env("FN_DB_ADMIN_USER") or "dbadmin",
    }


def _asset_identifier(target: dict) -> str:
    """The one asset this adapter fronts. One function per database keeps the blast
    radius of a compromised endpoint to exactly that database."""
    return f"{target['engine']}:{target['host']}:{target['database']}"


def _asset(target: dict) -> dict:
    return {
        "identifier": _asset_identifier(target),
        "name": f"{target['database']} ({target['host']})",
        "type": target["engine"],
        "role_options": [
            {"code": code, "display_name": label, "available": True,
             "permissions": permissions}
            for code, (label, permissions) in _ROLE_CODES.items()
        ],
    }


def _admin_password() -> str:
    """The DB admin password, resolved without it ever being a request field.

    Always a reference, never a plaintext setting: GCP injects a Secret Manager
    secret and Azure resolves a Key Vault reference (both land in
    FN_DB_ADMIN_PASSWORD before this code runs), and on AWS the function reads
    Secrets Manager itself. ``fnruntime.secretref`` owns that fan-out.

    FN_DB_ADMIN_SECRET_ID is the id variable this workload shipped with, so it is
    still accepted ahead of the conventional name and existing deployments keep
    resolving unchanged.
    """
    password = secretref.resolve("FN_DB_ADMIN_PASSWORD", "FN_DB_ADMIN_SECRET_ID")
    if not password:
        raise RuntimeError(
            "no database admin credential available: set FN_DB_ADMIN_PASSWORD "
            "(GCP secret env var / Azure Key Vault reference) or FN_DB_ADMIN_SECRET_ID "
            "(AWS Secrets Manager)")
    return password


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
    """One TLS connection. Encryption is not optional: Azure Flexible Server and
    Azure SQL both refuse plaintext, and a JIT credential is exactly the traffic you
    least want in the clear."""
    if engine == "mysql":
        import pymysql
        return pymysql.connect(
            host=host, port=port, user=user, password=password,
            database=database or None, connect_timeout=_CONNECT_TIMEOUT,
            ssl={"ssl": {}}, autocommit=True)
    import pytds
    return pytds.connect(
        server=host, port=port, database=database or "master", user=user,
        password=password, login_timeout=_CONNECT_TIMEOUT,
        cafile=_env("FN_DB_CAFILE") or None, validate_host=False, autocommit=True)


def _execute(plan, target: dict) -> int:
    """Run a plan. Each ``(database, statements)`` pair gets its OWN connection,
    which is not an optimisation choice: Azure SQL Database has no USE, so the login
    in master and the contained user in the target database cannot share one."""
    admin_password = _admin_password()
    executed = 0
    for database, statements in plan:
        connection = _connect(target["engine"], host=target["host"], port=target["port"],
                              database=database, user=target["admin_user"],
                              password=admin_password)
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


def _plan_json(plan) -> list:
    return [{"database": db, "statements": list(stmts)} for db, stmts in plan]


def _apply(plan, target: dict, ctx: Context, extra: dict = None) -> Response:
    """Execute a plan, or describe it in dry run. Every write route ends here."""
    body = {"data": dict(extra or {})}
    if _dry_run():
        body["data"]["dry_run"] = True
        body["data"]["plan"] = _plan_json(plan)
        return Response(200, body)
    body["data"]["statements_executed"] = _execute(plan, target)
    return Response(200, body)


def _check_asset(payload: dict, target: dict):
    """``None`` if the request names the asset we serve, else the error Response.

    Entitle sends the whole asset object back; a mismatch means the integration is
    pointed at the wrong function, which is worth failing loudly rather than
    quietly granting on the only database we have.
    """
    asset = payload.get("asset") or {}
    identifier = str(asset.get("identifier") or "").strip()
    expected = _asset_identifier(target)
    if identifier and identifier != expected:
        return Response(404, {"error": f"unknown asset {identifier!r}; "
                                       f"this adapter serves {expected!r}"})
    return None


def _role_code(payload: dict) -> str:
    return str(payload.get("role_code") or "read").strip().lower()


def _actor_identifier(payload: dict) -> str:
    return str(payload.get("actor_identifier") or "").strip()


# ── Routes ───────────────────────────────────────────────────────────────────

def _get_assets(req, ctx, target):
    # No pagination: one function fronts exactly one database, so there is never a
    # second page. `next` is still present because the contract declares it.
    return Response(200, {"next": "", "data": {"assets": [_asset(target)]}})


def _get_actors(req, ctx, target):
    """The ephemeral accounts that currently exist.

    Derived from the account naming convention rather than a stored list — the
    adapter holds no state, so the database is the only source of truth. In dry run
    there is nothing to read, so it reports empty rather than guessing.
    """
    if _dry_run():
        return Response(200, {"next": "", "data": {"actors": []}})
    return Response(200, {"next": "", "data": {"actors": _list_actors(target)}})


def _list_actors(target: dict) -> list:
    engine = target["engine"]
    if engine == "mysql":
        query = ("SELECT user FROM mysql.user WHERE user LIKE 'jit\\_%' "
                 "AND host = '%'")
        database = target["database"]
    else:
        query = ("SELECT name FROM sys.database_principals "
                 "WHERE name LIKE 'jit[_]%' AND type IN ('S','U')")
        database = target["database"]
    connection = _connect(engine, host=target["host"], port=target["port"],
                          database=database, user=target["admin_user"],
                          password=_admin_password())
    try:
        cursor = connection.cursor()
        cursor.execute(query)
        names = [str(row[0]) for row in cursor.fetchall()]
        cursor.close()
    finally:
        try:
            connection.close()
        except Exception:
            pass
    return [{"identifier": name, "name": name, "type": "database_account",
             "email": ""} for name in names]


def _get_all_permissions(req, ctx, target):
    """Who currently holds what. Entitle reconciles against this, so an adapter that
    lies here produces drift it can never correct."""
    if _dry_run():
        return Response(200, {"next": "", "data": {"actors_permissions": [],
                                                   "assets_permissions": []}})
    actors = _list_actors(target)
    asset_id = _asset_identifier(target)
    return Response(200, {"next": "", "data": {
        # The role an account holds is not recoverable from its name, and reading it
        # back per engine is a separate piece of work; report membership without a
        # role rather than inventing one Entitle would then act on.
        "actors_permissions": [
            {"actor_id": actor["identifier"], "role_code": "", "direct_member": True}
            for actor in actors],
        "assets_permissions": [{"asset_id": asset_id, "role_code": code}
                               for code in _ROLE_CODES],
    }})


def _get_asset_permissions(req, ctx, target):
    return _get_all_permissions(req, ctx, target)


def _create_actor(req, ctx, target):
    payload = req.json()
    actor = payload.get("actor") or payload
    identity = str(actor.get("email") or actor.get("identifier")
                   or actor.get("name") or "").strip()
    if not identity:
        return Response(400, {"error": "create_actor needs an actor with an email or identifier"})
    username = _sqlplan.ephemeral_username(identity, ctx.request_id)
    password = _generate_password()
    try:
        plan = _sqlplan.create_actor_plan(
            target["engine"], username=username, password=password,
            database=target["database"], flavor=target["flavor"])
    except _sqlplan.CloudDbSqlError as exc:
        return Response(400, {"error": str(exc)})

    logs.emit("info", "create_actor", request_id=ctx.request_id,
              identity=identity, username=username, engine=target["engine"],
              dry_run=_dry_run())

    # The credentials ARE the point of create_actor — Entitle hands them to the
    # requester. In dry run nothing was created, so returning one would be a secret
    # with no account behind it.
    extra = {"identifier": username, "name": username, "type": "database_account"}
    if not _dry_run():
        extra.update({"username": username, "password": password,
                      "host": target["host"], "port": target["port"],
                      "database": target["database"]})
    return _apply(plan, target, ctx, extra)


def _delete_actor(req, ctx, target):
    payload = req.json()
    username = _actor_identifier(payload) or str(payload.get("identifier") or "").strip()
    if not username:
        return Response(400, {"error": "delete_actor needs actor_identifier"})
    try:
        plan = _sqlplan.delete_actor_plan(
            target["engine"], username=username, database=target["database"],
            flavor=target["flavor"])
    except _sqlplan.CloudDbSqlError as exc:
        return Response(400, {"error": str(exc)})
    logs.emit("info", "delete_actor", request_id=ctx.request_id,
              username=username, engine=target["engine"], dry_run=_dry_run())
    return _apply(plan, target, ctx, {"identifier": username})


def _give_access(req, ctx, target):
    payload = req.json()
    mismatch = _check_asset(payload, target)
    if mismatch:
        return mismatch
    username = _actor_identifier(payload)
    if not username:
        return Response(400, {"error": "give_access needs actor_identifier"})
    role = _role_code(payload)
    try:
        plan = _sqlplan.give_access_plan(
            target["engine"], username=username, database=target["database"],
            role=role, flavor=target["flavor"])
    except _sqlplan.CloudDbSqlError as exc:
        return Response(400, {"error": str(exc)})
    logs.emit("info", "give_access", request_id=ctx.request_id,
              username=username, role_code=role, engine=target["engine"],
              dry_run=_dry_run())
    return _apply(plan, target, ctx, {"actor_identifier": username, "role_code": role})


def _revoke_access(req, ctx, target):
    payload = req.json()
    mismatch = _check_asset(payload, target)
    if mismatch:
        return mismatch
    username = _actor_identifier(payload)
    if not username:
        return Response(400, {"error": "revoke_access needs actor_identifier"})
    role = _role_code(payload)
    try:
        plan = _sqlplan.revoke_access_plan(
            target["engine"], username=username, database=target["database"],
            role=role, flavor=target["flavor"])
    except _sqlplan.CloudDbSqlError as exc:
        return Response(400, {"error": str(exc)})
    logs.emit("info", "revoke_access", request_id=ctx.request_id,
              username=username, role_code=role, engine=target["engine"],
              dry_run=_dry_run())
    return _apply(plan, target, ctx, {"actor_identifier": username, "role_code": role})


def _check_config(req, ctx, target):
    """Configuration self-test. Reports the target it resolved so a misconfigured
    integration is caught at setup rather than at the first real grant."""
    problems = []
    if not _dry_run():
        try:
            _admin_password()
        except Exception as exc:
            problems.append(str(exc))
    return Response(200, {"data": {
        "valid": not problems,
        "asset": _asset_identifier(target),
        "engine": target["engine"],
        "flavor": target["flavor"] or "rds",
        "dry_run": _dry_run(),
        "problems": problems,
    }})


_ROUTES = {
    ("GET", "/get_assets"): _get_assets,
    ("GET", "/get_actors"): _get_actors,
    ("GET", "/get_all_permissions"): _get_all_permissions,
    ("POST", "/create_actor"): _create_actor,
    ("POST", "/delete_actor"): _delete_actor,
    ("POST", "/give_access"): _give_access,
    ("POST", "/revoke_access"): _revoke_access,
    ("POST", "/check_config"): _check_config,
}


def handle(req: Request, ctx: Context) -> Response:
    target = _target()
    path = (req.path or "/").rstrip("/") or "/"

    handler = _ROUTES.get((req.method, path))
    if handler is None and path.startswith("/get_asset_permissions/") and req.method == "GET":
        handler = _get_asset_permissions
    if handler is None:
        return Response(404, {
            "error": f"no route for {req.method} {path}",
            # Entitle's paths are configurable per operation, so a 404 here usually
            # means the integration's *_path fields disagree with these.
            "routes": sorted(f"{method} {route}" for method, route in _ROUTES),
        })
    return handler(req, ctx, target)
