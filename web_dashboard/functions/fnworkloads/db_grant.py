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

# Settings this workload cannot run without, in the form the dashboard's deploy
# validation reads: ``"A|B"`` means either satisfies the requirement. Declared here,
# beside the code that reads them, so the picker and the validator cannot drift from
# what db_grant actually needs.
#
# Without this a hand-deploy from the Cloud Functions page produced a function that
# 500-ed on every request for the life of the install — the automated pairing path
# always passes them, so nothing else caught it.
REQUIRED_ENV = ("FN_DB_ENGINE", "FN_DB_HOST", "FN_DB_NAME|FN_DB_NAMES")

# Serves the Entitle Remote Adapter contract (the eight routes in _ROUTES), so the
# dashboard may register it as a REST integration. Declared on the module rather than
# listed in the page's JavaScript, which is where this used to live and could drift.
ENTITLE_ADAPTER = True

# See the note in the previous revision: in the zip the packager copies
# web_dashboard/services/cloud_db_sql_service.py in as sqlplan.py, so the SQL is the
# file under test rather than a reimplementation. In-repo that name does not exist.
try:
    import sqlplan as _sqlplan  # noqa: E402
except ImportError:  # pragma: no cover - exercised in-repo, never in the zip
    from web_dashboard.services import cloud_db_sql_service as _sqlplan  # noqa: E402

_TRUTHY = ("1", "true", "yes", "on")
_CONNECT_TIMEOUT = 10

# Every account this adapter mints starts with this (see
# cloud_db_sql_service.ephemeral_username). Accounts that do not are somebody
# else's, and this adapter neither lists nor deletes them.
_JIT_PREFIX = "jit_"

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


def _server() -> dict:
    """The ONE server this adapter fronts — engine, endpoint, admin login.

    One function, one server, and that is a security boundary rather than a
    limitation. The admin credential a JIT adapter needs (CREATE/DROP USER) is
    already server-level on every managed engine here, so several databases on one
    server share a blast radius whether or not they share a function. Two servers do
    not, and giving one endpoint credentials for both would genuinely widen it —
    which is why ``FN_DB_HOST`` stays singular.
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
    return {
        "engine": engine, "host": host, "port": port,
        "flavor": _env("FN_DB_FLAVOR"), "admin_user": _env("FN_DB_ADMIN_USER") or "dbadmin",
    }


def _database_names() -> list:
    """The databases this adapter serves, in configuration order.

    ``FN_DB_NAMES`` is the allowlist; ``FN_DB_NAME`` is the single-database spelling
    and stays exactly as it was. A request can only ever SELECT from this list — it
    can never add to it. That is the whole safety property: the asset identifier
    picks a target, it does not describe one, so no caller can point a grant at a
    database (or a host) the operator did not configure.
    """
    raw = _env("FN_DB_NAMES") or _env("FN_DB_NAME")
    names = []
    for part in raw.split(","):
        name = part.strip()
        if name and name not in names:
            names.append(name)
    if not names:
        raise RuntimeError("set FN_DB_NAME (one database) or FN_DB_NAMES (several)")
    return names


def _targets() -> dict:
    """``{asset identifier: target}`` — everything this adapter may touch."""
    server = _server()
    return {_asset_identifier(dict(server, database=name)): dict(server, database=name)
            for name in _database_names()}


def _asset_identifier(target: dict) -> str:
    """One asset per database served. Distinct per database, so a request naming one
    can never be satisfied against another."""
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


def _resolve_target(identifier: str, targets: dict) -> tuple:
    """``(target, None)`` for the asset a request names, else ``(None, Response)``.

    An unknown identifier is a 404 — the integration is pointed at the wrong
    function, which is worth failing loudly rather than granting on some database we
    happen to have.

    A MISSING identifier is the case that matters. With one database it is
    unambiguous, and every existing deployment calls this way, so it resolves. With
    several, choosing one for the caller would be precisely the silent mis-grant
    this adapter exists to prevent, so it is a 400 that lists what it serves.
    """
    identifier = str(identifier or "").strip()
    if not identifier:
        if len(targets) == 1:
            return next(iter(targets.values())), None
        return None, Response(400, {
            "error": "asset.identifier is required: this adapter serves "
                     f"{len(targets)} databases, so there is no default",
            "assets": sorted(targets)})
    target = targets.get(identifier)
    if target is None:
        return None, Response(404, {"error": f"unknown asset {identifier!r}",
                                    "assets": sorted(targets)})
    return target, None


def _asset_target(payload: dict, targets: dict) -> tuple:
    """:func:`_resolve_target` for the ``asset`` object Entitle sends on a grant."""
    return _resolve_target((payload.get("asset") or {}).get("identifier"), targets)


def _is_minted(username: str) -> bool:
    """Whether this adapter made the account, by the naming convention it mints
    with (``cloud_db_sql_service.ephemeral_username``).

    The only thing standing between ``delete_actor`` and the database's own admin
    login, so it is checked on every destructive route that takes a name from a
    request. ``portainer_access`` draws the same line for the same reason: a grant
    integration must not be able to remove an operator's account.
    """
    return str(username or "").startswith(_JIT_PREFIX)


def _role_code(payload: dict) -> str:
    return str(payload.get("role_code") or "read").strip().lower()


def _actor_identifier(payload: dict) -> str:
    return str(payload.get("actor_identifier") or "").strip()


# ── Routes ───────────────────────────────────────────────────────────────────

def _get_assets(req, ctx, targets):
    # No pagination: the asset list is the operator's own FN_DB_NAMES, which is a
    # handful of databases on one server, not a discovered catalogue. `next` is
    # still present because the contract declares it.
    return Response(200, {"next": "", "data": {
        "assets": [_asset(target) for target in targets.values()]}})


def _get_actors(req, ctx, targets):
    """The ephemeral accounts that currently exist.

    Derived from the account naming convention rather than a stored list — the
    adapter holds no state, so the database is the only source of truth. In dry run
    there is nothing to read, so it reports empty rather than guessing.
    """
    if _dry_run():
        return Response(200, {"next": "", "data": {"actors": []}})
    return Response(200, {"next": "", "data": {"actors": _list_all_actors(targets)}})


def _query(target: dict, query: str, params=None) -> list:
    """One column of one query, against one database. Connection-scoped: the
    database is chosen by connecting to it, never by interpolating its name."""
    connection = _connect(target["engine"], host=target["host"], port=target["port"],
                          database=target["database"], user=target["admin_user"],
                          password=_admin_password())
    try:
        cursor = connection.cursor()
        cursor.execute(query, params) if params else cursor.execute(query)
        names = [str(row[0]) for row in cursor.fetchall()]
        cursor.close()
    finally:
        try:
            connection.close()
        except Exception:
            pass
    return names


def _actors(names) -> list:
    return [{"identifier": name, "name": name, "type": "database_account",
             "email": ""} for name in sorted(set(names)) if _is_minted(name)]


def _list_actors_in(target: dict) -> list:
    """Accounts holding access **in one database**.

    The per-asset answer, and the reason it is not the server-wide one: on MySQL an
    account is a server-level principal and only the GRANT is per-database, so
    reading ``mysql.user`` would report every database's accounts against every
    asset. ``mysql.db`` holds the database-scoped grants, which is the actual
    question. On SQL Server ``sys.database_principals`` is already per-database.
    """
    if target["engine"] == "mysql":
        return _actors(_query(
            target,
            "SELECT User FROM mysql.db WHERE Db = %s AND User LIKE %s",
            (target["database"], _JIT_PREFIX.replace("_", "\\_") + "%")))
    return _actors(_query(
        target,
        "SELECT name FROM sys.database_principals "
        "WHERE name LIKE 'jit[_]%' AND type IN ('S','U')"))


def _list_all_actors(targets: dict) -> list:
    """Every account this adapter minted on this server.

    MySQL can answer directly — the adapter owns the whole ``jit_`` namespace on the
    server it fronts — and answering that way also lists an account whose
    ``give_access`` never arrived, which Entitle needs in order to clean it up. SQL
    Server has no equivalent readable from a user database, so it is the union of
    the per-database users, which ``create_actor`` puts in every served database.
    """
    first = next(iter(targets.values()))
    if first["engine"] == "mysql":
        return _actors(_query(
            first, "SELECT User FROM mysql.user WHERE User LIKE %s AND Host = %s",
            (_JIT_PREFIX.replace("_", "\\_") + "%", "%")))
    found = []
    for target in targets.values():
        found += [actor["identifier"] for actor in _list_actors_in(target)]
    return _actors(found)


def _permissions_body(targets: dict) -> dict:
    """The reconciliation payload for a set of assets.

    Entitle reconciles against this, so an adapter that lies here produces drift it
    can never correct — which is why each actor is attributed only to the databases
    it actually holds a grant in, read per database rather than inferred from the
    name.
    """
    actors_permissions = []
    seen = set()
    for identifier, target in targets.items():
        for actor in _list_actors_in(target):
            if actor["identifier"] in seen:
                continue
            seen.add(actor["identifier"])
            # The role an account holds is not recoverable from its name, and
            # reading it back per engine is a separate piece of work; report
            # membership without a role rather than inventing one Entitle would
            # then act on.
            actors_permissions.append({"actor_id": actor["identifier"],
                                       "role_code": "", "direct_member": True})
    return {
        "actors_permissions": actors_permissions,
        "assets_permissions": [{"asset_id": identifier, "role_code": code}
                               for identifier in targets for code in _ROLE_CODES],
    }


def _get_all_permissions(req, ctx, targets):
    """Who currently holds what, across every database served."""
    if _dry_run():
        return Response(200, {"next": "", "data": {"actors_permissions": [],
                                                   "assets_permissions": []}})
    return Response(200, {"next": "", "data": _permissions_body(targets)})


def _get_asset_permissions(req, ctx, targets):
    """The same, for ONE asset named in the path.

    Not an alias for the all-assets route any more: with several databases served,
    answering an asset-scoped question with every asset's actors would report people
    as holding access they do not have.
    """
    identifier = (req.path or "").rstrip("/").rsplit("/", 1)[-1]
    target, refusal = _resolve_target(identifier, targets)
    if refusal:
        return refusal
    if _dry_run():
        return Response(200, {"next": "", "data": {"actors_permissions": [],
                                                   "assets_permissions": []}})
    scoped = {_asset_identifier(target): target}
    return Response(200, {"next": "", "data": _permissions_body(scoped)})


def _create_actor(req, ctx, targets):
    """Mint the account, with no privileges anywhere.

    Entitle's ``create_actor`` carries **no asset** — an actor belongs to the
    adapter, not to an asset — so this cannot know which database the grant will be
    for, and creates a role-less account across every database served. That is the
    same "useless and harmless until give_access" guarantee as the single-database
    case, just applied N times.
    """
    payload = req.json()
    actor = payload.get("actor") or payload
    identity = str(actor.get("email") or actor.get("identifier")
                   or actor.get("name") or "").strip()
    if not identity:
        return Response(400, {"error": "create_actor needs an actor with an email or identifier"})
    server = next(iter(targets.values()))
    databases = [target["database"] for target in targets.values()]
    username = _sqlplan.ephemeral_username(identity, ctx.request_id)
    password = _generate_password()
    try:
        plan = _sqlplan.create_actor_plan(
            server["engine"], username=username, password=password,
            database=databases[0], flavor=server["flavor"], databases=databases)
    except _sqlplan.CloudDbSqlError as exc:
        return Response(400, {"error": str(exc)})

    logs.emit("info", "create_actor", request_id=ctx.request_id,
              identity=identity, username=username, engine=server["engine"],
              databases=len(databases), dry_run=_dry_run())

    # The credentials ARE the point of create_actor — Entitle hands them to the
    # requester. In dry run nothing was created, so returning one would be a secret
    # with no account behind it.
    extra = {"identifier": username, "name": username, "type": "database_account"}
    if not _dry_run():
        extra.update({"username": username, "password": password,
                      "host": server["host"], "port": server["port"]})
        # One database has an unambiguous answer; several do not, and naming one
        # would tell the requester they have access somewhere they do not.
        extra.update({"database": databases[0]} if len(databases) == 1
                     else {"databases": databases})
    return _apply(plan, server, ctx, extra)


def _delete_actor(req, ctx, targets):
    payload = req.json()
    username = _actor_identifier(payload) or str(payload.get("identifier") or "").strip()
    if not username:
        return Response(400, {"error": "delete_actor needs actor_identifier"})
    if not _is_minted(username):
        # Without this, delete_actor is a DROP USER for any name the caller likes —
        # the database's own admin login included. Entitle only ever returns names
        # this adapter minted, so a name that fails here did not come from it.
        return Response(403, {"error": f"{username!r} was not created by this adapter; "
                                       "it only manages accounts it minted"})
    server = next(iter(targets.values()))
    databases = [target["database"] for target in targets.values()]
    try:
        plan = _sqlplan.delete_actor_plan(
            server["engine"], username=username, database=databases[0],
            flavor=server["flavor"], databases=databases)
    except _sqlplan.CloudDbSqlError as exc:
        return Response(400, {"error": str(exc)})
    logs.emit("info", "delete_actor", request_id=ctx.request_id,
              username=username, engine=server["engine"], dry_run=_dry_run())
    return _apply(plan, server, ctx, {"identifier": username})


def _give_access(req, ctx, targets):
    payload = req.json()
    target, refusal = _asset_target(payload, targets)
    if refusal:
        return refusal
    username = _actor_identifier(payload)
    if not username:
        return Response(400, {"error": "give_access needs actor_identifier"})
    if not _is_minted(username):
        # Otherwise this grants a role to any EXISTING account named in a request —
        # an application login, say — which is a privilege-escalation primitive
        # rather than a JIT grant. Entitle only ever passes back names from
        # create_actor or get_actors, and both are minted-only.
        return Response(403, {"error": f"{username!r} was not created by this adapter; "
                                       "it only manages accounts it minted"})
    role = _role_code(payload)
    try:
        plan = _sqlplan.give_access_plan(
            target["engine"], username=username, database=target["database"],
            role=role, flavor=target["flavor"])
    except _sqlplan.CloudDbSqlError as exc:
        return Response(400, {"error": str(exc)})
    logs.emit("info", "give_access", request_id=ctx.request_id,
              username=username, role_code=role, engine=target["engine"],
              asset=_asset_identifier(target), dry_run=_dry_run())
    return _apply(plan, target, ctx, {"actor_identifier": username, "role_code": role})


def _revoke_access(req, ctx, targets):
    payload = req.json()
    target, refusal = _asset_target(payload, targets)
    if refusal:
        return refusal
    username = _actor_identifier(payload)
    if not username:
        return Response(400, {"error": "revoke_access needs actor_identifier"})
    if not _is_minted(username):
        # Otherwise this grants a role to any EXISTING account named in a request —
        # an application login, say — which is a privilege-escalation primitive
        # rather than a JIT grant. Entitle only ever passes back names from
        # create_actor or get_actors, and both are minted-only.
        return Response(403, {"error": f"{username!r} was not created by this adapter; "
                                       "it only manages accounts it minted"})
    role = _role_code(payload)
    try:
        plan = _sqlplan.revoke_access_plan(
            target["engine"], username=username, database=target["database"],
            role=role, flavor=target["flavor"])
    except _sqlplan.CloudDbSqlError as exc:
        return Response(400, {"error": str(exc)})
    logs.emit("info", "revoke_access", request_id=ctx.request_id,
              username=username, role_code=role, engine=target["engine"],
              asset=_asset_identifier(target), dry_run=_dry_run())
    return _apply(plan, target, ctx, {"actor_identifier": username, "role_code": role})


def _check_config(req, ctx, targets):
    """Configuration self-test. Reports every asset it resolved so a misconfigured
    integration is caught at setup rather than at the first real grant."""
    problems = []
    if not _dry_run():
        try:
            _admin_password()
        except Exception as exc:
            problems.append(str(exc))
    server = next(iter(targets.values()))
    return Response(200, {"data": {
        "valid": not problems,
        "assets": sorted(targets),
        "engine": server["engine"],
        "flavor": server["flavor"] or "rds",
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


def _unconfigured(exc: Exception, ctx: Context, *, reporting: bool) -> Response:
    """What to answer when the ``FN_DB_*`` settings cannot be resolved.

    :func:`_targets` used to run BEFORE routing, which made this unreachable: any
    raise out of ``handle`` becomes dispatch's opaque
    ``500 {"error": "internal error"}`` on EVERY route — including
    ``/check_config``, whose entire job is to name what is missing. A function
    deployed without ``FN_DB_ENGINE``/``FN_DB_HOST``/``FN_DB_NAME`` therefore had
    no way to say so, on any path.

    The detail is safe in the body here, unlike auth's deliberately blank
    ``function not configured``: dispatch verifies the shared secret before it
    calls ``handle``, so only an authenticated caller ever reads this.
    """
    problem = str(exc) or type(exc).__name__
    if reporting:
        # check_config's contract is to REPORT problems, so settings it cannot
        # resolve are a valid answer rather than a failure. Same body shape as the
        # success path, so a consumer needs no special case for it.
        return Response(200, {"data": {
            "valid": False, "assets": [], "engine": "", "flavor": "",
            "dry_run": _dry_run(), "problems": [problem],
        }})
    return Response(500, {"error": "function not configured",
                          "problem": problem, "request_id": ctx.request_id})


def handle(req: Request, ctx: Context) -> Response:
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

    # AFTER routing, so an unknown path still gets the route list and a
    # misconfiguration still gets named — see _unconfigured.
    try:
        targets = _targets()
    except Exception as exc:
        return _unconfigured(exc, ctx, reporting=handler is _check_config)
    return handler(req, ctx, targets)
