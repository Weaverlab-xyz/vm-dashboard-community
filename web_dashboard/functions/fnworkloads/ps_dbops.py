"""Password Safe GCP Cloud SQL plugin — the ``cloud-run`` channel's DB-Ops service.

The "bt-dbops" service. Cloud SQL for SQL Server has no IAM database authentication,
so the plugin's control-plane channel would have to mirror the functional account's
password into Secret Manager — a second authority for a credential Password Safe
exists to own. The ``cloud-run`` channel avoids that: the credential travels in the
request body to this service, which sits inside the VPC, holds the database drivers,
and opens the actual connection. Nothing is persisted.

**This is NOT the Entitle Remote Adapter contract.** ``db_grant`` serves that one, and
its request handler is not reusable here — the plugin posts to ``DbOpsPath``
(default ``/v1/credential-op``) with ``DbOpsContractVersion`` (default 1, overridable
per managed system with the address's ``ver=`` option). What IS shared with db_grant
is everything below the handler: the vendored drivers, the packaging, the VPC
attachment and the deploy machinery.

Three things make this workload different from every other one here, and all three
are deliberate:

1. **The caller is not the dashboard.** A Password Safe Resource Broker presents a
   Google OIDC ID token, so the shared-secret inner gate cannot apply. ``AUTH_MODE``
   below selects ``fnruntime.auth.verify_gcp_oidc`` instead — a different gate, not
   the absence of one.
2. **The target comes from the REQUEST, not the environment.** db_grant takes its
   database from ``FN_DB_*`` precisely so a caller cannot redirect a grant; here the
   plugin names the instance, and that is the contract. ``FN_DBOPS_ALLOWED_INSTANCES``
   is what puts the boundary back, and it fails closed.
3. **It holds no credential of its own.** No Secret Manager secret, no staged admin
   password, nothing to rotate. That is why one service serves every database in a
   region (see docs/design/ps-dbops-cloud-run.md).

**The v1 request shape is not yet pinned.** It lives in the plugin, not in this
repository, and guessing it would produce a service that deploys cleanly and fails
every rotation. So ``_parse_credential_op`` is the one unimplemented seam, and the
service ships in CAPTURE mode: it logs the redacted request and answers 501 naming the
versions it can serve. Point one managed system at it, click *Verify Managed Account*,
and the real request is in Cloud Logging — which also proves the front door, the
invoker binding, the audience and VPC reachability before any handler logic exists.
Everything the seam calls once it is filled in (below) is implemented and tested.
"""
import os
import re

from fnruntime import logs
from fnruntime.contract import Context, Request, Response

NAME = "ps_dbops"
DESCRIPTION = ("Password Safe GCP Cloud SQL 'cloud-run' channel: changes and verifies "
               "database credentials from inside the VPC. One per region.")

# The inner gate. See fnruntime.auth.verify_gcp_oidc — the caller authenticates with a
# Google ID token that Cloud Run has already verified, so the shared secret this
# runtime otherwise requires is not something the caller could ever present.
AUTH_MODE = "gcp_oidc"

# Empty, and both omissions are deliberate.
#
# FN_DBOPS_AUDIENCE cannot be required: it IS the service's own URL, which does not
# exist until after the first apply. The deploy path stamps it in a second pass.
#
# FN_DBOPS_ALLOWED_INSTANCES is not required either, because "no instances yet" is a
# legitimate state — an operator stands the service up before onboarding the first
# database in the region, exactly as they configure the invoker bindings before the
# brokers exist. REQUIRED_ENV is for settings whose absence makes a function INERT and
# dangerous to discover late; an unset allowlist makes this one refuse every request
# and say which setting is missing, which is the opposite problem.
REQUIRED_ENV = ()

# Not an Entitle adapter. Stated rather than omitted, because "Register in Entitle" is
# offered from this flag and a live integration that can never resolve an asset is
# visible only inside Entitle — a bad place to discover a mistake.
ENTITLE_ADAPTER = False

_DEFAULT_PATH = "/v1/credential-op"
_SUPPORTED_CONTRACT_VERSIONS = (1,)
_CONNECT_TIMEOUT = 15

# project:region:instance — the same shape ps_resource_service validates in address
# field 2, and the same shape the allowlist is written in.
_CONNECTION_NAME_RE = re.compile(r"^[^:;\s]+:[^:;\s]+:[^:;\s]+$")

# Database principal names. Broad on purpose — a managed account is whatever the
# customer already had — but control characters and NUL are refused outright: they
# cannot appear in a real principal and are how a quoting bug becomes an injection.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")

_MAX_IDENT = 128


class DbOpsError(Exception):
    """A request this service will not serve. The message reaches the caller."""


class ContractNotImplemented(Exception):
    """The seam. Raised until the v1 request shape is pinned (see the module note)."""


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name, "") or default).strip()


def _capture_enabled() -> bool:
    """Whether to log the full redacted request.

    Defaults ON, and stays on until the contract is implemented: capturing the real
    request is the entire purpose of this build of the workload. Every credential-shaped
    field goes through fnruntime.logs.redact first, and the Authorization header is
    dropped rather than redacted.
    """
    return _env("FN_DBOPS_CAPTURE", "1").lower() not in ("0", "false", "no", "off")


# ── Target admission ─────────────────────────────────────────────────────────

def allowed_instances() -> tuple:
    """The instance connection names this service will act on.

    ``*`` is an explicit, logged opt-out meaning "any instance this VPC can reach".
    Empty is NOT that — it is an unconfigured service, and it refuses everything.
    """
    raw = _env("FN_DBOPS_ALLOWED_INSTANCES")
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def check_instance(connection_name: str) -> str:
    """The connection name, if this service may act on it. Raises otherwise.

    One service serves every database in its region, so IAM authenticates the caller
    but does not scope the target: without this, a broker credential is a licence to
    change passwords on any Cloud SQL instance reachable from the VPC. The dashboard
    appends to the allowlist as it onboards, which is a new revision each time —
    accepted, because the alternative is an unbounded blast radius.
    """
    name = (connection_name or "").strip()
    if not _CONNECTION_NAME_RE.match(name):
        raise DbOpsError(
            f"instance connection name {name!r} is not of the form "
            "'project:region:instance'")
    allowed = allowed_instances()
    if not allowed:
        # Fail CLOSED, and say which setting is missing — an operator reading this in
        # a Password Safe rotation failure has no other way to tell.
        raise DbOpsError(
            "this DB-Ops service has no allowed instances configured "
            "(FN_DBOPS_ALLOWED_INSTANCES), so it will not act on any database")
    if "*" in allowed:
        return name
    if name not in allowed:
        raise DbOpsError(
            f"instance {name!r} is not in this DB-Ops service's allowlist — it serves "
            f"{len(allowed)} instance(s) in this region")
    return name


# ── Statement construction ───────────────────────────────────────────────────
#
# DDL, so most of this cannot be parameterized: no database engine accepts a bind
# parameter for a principal name in ALTER LOGIN / ALTER ROLE, and SQL Server accepts
# none for the password either. Quoting is therefore the security boundary, and each
# engine gets the escaping its own parser actually specifies — not one shared
# approximation. MySQL is the exception and is done properly, with the driver's own
# escaping via bound parameters (see _mysql_params).

def _ident(name: str) -> str:
    """A principal name that is safe to quote. Raises if it is not."""
    value = (name or "").strip()
    if not value:
        raise DbOpsError("a database principal name is required")
    if len(value) > _MAX_IDENT:
        raise DbOpsError(
            f"database principal name is {len(value)} characters, over the "
            f"{_MAX_IDENT} character limit")
    if _CONTROL_CHARS_RE.search(value):
        raise DbOpsError("database principal name contains control characters")
    return value


def _secret(value: str) -> str:
    """A password that is safe to embed. Raises if it is not.

    Same control-character rule, for the same reason. No character-set allowlist:
    Password Safe generates the password and its alphabet is the customer's policy,
    so an allowlist here would reject valid credentials — which on a rotation means
    refusing to apply a change Password Safe has already recorded.
    """
    if value is None or value == "":
        raise DbOpsError("a password is required")
    if _CONTROL_CHARS_RE.search(value):
        raise DbOpsError("password contains control characters")
    return value


def _mssql_ident(name: str) -> str:
    """``[name]`` with ``]`` doubled — T-SQL's delimited identifier rule."""
    return "[" + _ident(name).replace("]", "]]") + "]"


def _tsql_literal(value: str) -> str:
    """``'value'`` with ``'`` doubled. T-SQL has no backslash escape, so this is the
    complete rule — unlike MySQL, where it depends on NO_BACKSLASH_ESCAPES."""
    return "'" + _secret(value).replace("'", "''") + "'"


def _pg_ident(name: str) -> str:
    return '"' + _ident(name).replace('"', '""') + '"'


def _pg_literal(value: str) -> str:
    """Standard-conforming SQL string literal. ``standard_conforming_strings`` has
    been on by default since PostgreSQL 9.1 and Cloud SQL does not turn it off, so
    doubling the quote is the whole rule here too."""
    return "'" + _secret(value).replace("'", "''") + "'"


def change_password_statements(engine: str, *, principal: str, new_password: str,
                               old_password: str = "") -> list:
    """Statements that set ``principal``'s password.

    ``old_password`` selects SELF-rotation, which is the recommended configuration on
    this channel: the managed login alters itself and the functional account needs no
    privilege over it at all. It is not a nicety — it is what lets an operator skip
    the ALTER ANY LOGIN grant entirely.

    Returns MySQL statements as ``(sql, params)`` pairs so the driver does the
    escaping; the other two are plain strings, because their DDL takes no parameters.
    """
    if engine == "sqlserver":
        login = _mssql_ident(principal)
        new = _tsql_literal(new_password)
        if old_password:
            # OLD_PASSWORD is what makes this work without ALTER ANY LOGIN: SQL Server
            # lets a login change its own password when it can prove it knows the
            # current one.
            return [f"ALTER LOGIN {login} WITH PASSWORD = {new} "
                    f"OLD_PASSWORD = {_tsql_literal(old_password)};"]
        return [f"ALTER LOGIN {login} WITH PASSWORD = {new};"]

    if engine == "mysql":
        user, host = _split_mysql_account(principal)
        _secret(new_password)
        if old_password:
            # USER(), not the parsed name: on a self-rotation the session IS the
            # managed account, and MySQL resolves the authenticated account exactly.
            # Naming it again would rotate '<user>'@'%' when the session actually
            # authenticated as '<user>'@'10.0.0.5' — a different account, silently.
            return [("ALTER USER USER() IDENTIFIED BY %s", (new_password,))]
        return [("ALTER USER %s@%s IDENTIFIED BY %s", (user, host, new_password))]

    if engine == "postgres":
        return [f"ALTER ROLE {_pg_ident(principal)} WITH PASSWORD "
                f"{_pg_literal(new_password)};"]

    raise DbOpsError(f"unsupported engine {engine!r}")


def _split_mysql_account(principal: str) -> tuple:
    """``('user', 'host')`` from ``user@host`` or ``user``.

    The plugin refuses to default a MySQL host qualifier and so does this: app@% and
    app@10.0.0.5 are different accounts, and rotating the wrong row rotates the wrong
    account silently. A bare name is only accepted because the address's ``host=``
    option is what supplies the qualifier, and the caller merges the two before
    getting here.
    """
    name = _ident(principal)
    user, sep, host = name.rpartition("@")
    if not sep:
        raise DbOpsError(
            f"MySQL account {principal!r} has no host qualifier — 'app'@'%' and "
            "'app'@'10.0.0.5' are different accounts, so this is refused rather "
            "than defaulted")
    return user, host


# ── Connection + execution ───────────────────────────────────────────────────

def _connect(engine: str, *, host: str, port: int, database: str, user: str,
             password: str, ssl: bool = True):
    """One connection, as ``user``. Never as a stored identity — this service has none.

    TLS is the default and switching it off is an explicit per-managed-system choice
    (address field 5). Cloud SQL for SQL Server and MySQL both accept encrypted
    connections without a client certificate, so there is nothing to mount.
    """
    if engine == "mysql":
        import pymysql
        return pymysql.connect(
            host=host, port=port, user=user, password=password,
            database=database or None, connect_timeout=_CONNECT_TIMEOUT,
            ssl={"ssl": {}} if ssl else None, autocommit=True)
    if engine == "sqlserver":
        import pytds
        return pytds.connect(
            server=host, port=port, database=database or "master", user=user,
            password=password, login_timeout=_CONNECT_TIMEOUT,
            cafile=_env("FN_DB_CAFILE") or None, validate_host=False,
            autocommit=True)
    if engine == "postgres":
        # Reachable only when an operator forces clouddb_ps_gcp_channel=cloud-run for
        # every engine. PostgreSQL's own channel (data-api) needs no service at all,
        # so no Postgres driver is vendored into this image — adding pg8000 to
        # cloud_function_package._WORKLOAD_VENDOR is the whole change if this is
        # wanted. Named explicitly so the failure is a sentence, not a NameError.
        raise DbOpsError(
            "PostgreSQL is not built into this DB-Ops service — postgres uses the "
            "'data-api' channel, which needs no service at all. Leave "
            "clouddb_ps_gcp_channel on 'auto'")
    raise DbOpsError(f"unsupported engine {engine!r}")


def _execute(connection, statements) -> int:
    """Run ``statements`` on an open connection. Returns how many ran.

    Accepts a plain string or a ``(sql, params)`` pair per statement, so the MySQL
    path can bind parameters and the DDL-only engines can pass literals.
    """
    executed = 0
    cursor = connection.cursor()
    try:
        for statement in statements:
            if isinstance(statement, tuple):
                cursor.execute(statement[0], statement[1])
            else:
                cursor.execute(statement)
            executed += 1
    finally:
        try:
            cursor.close()
        except Exception:
            pass
    return executed


def change_password(target: dict, *, principal: str, new_password: str,
                    as_user: str, as_password: str, old_password: str = "") -> int:
    """Connect as ``as_user`` and set ``principal``'s password. Returns statements run."""
    check_instance(target.get("connection_name", ""))
    statements = change_password_statements(
        target["engine"], principal=principal, new_password=new_password,
        old_password=old_password)
    connection = _connect(target["engine"], host=target["host"], port=target["port"],
                          database=target.get("database", ""), user=as_user,
                          password=as_password, ssl=target.get("ssl", True))
    try:
        return _execute(connection, statements)
    finally:
        try:
            connection.close()
        except Exception:
            pass


def verify_credential(target: dict, *, principal: str, password: str) -> bool:
    """Whether ``principal`` can log in with ``password``.

    A login attempt, not a lookup, because that is the only thing that answers the
    question — and it is the reason ``cloud-run`` is the only GCP channel that can
    Verify Managed Account at all: both control-plane channels authenticate as the
    CALLER and cannot test an arbitrary principal's password.
    """
    check_instance(target.get("connection_name", ""))
    connection = _connect(target["engine"], host=target["host"], port=target["port"],
                          database=target.get("database", ""), user=principal,
                          password=password, ssl=target.get("ssl", True))
    try:
        _execute(connection, ["SELECT 1"])
        return True
    finally:
        try:
            connection.close()
        except Exception:
            pass


# ── The contract ─────────────────────────────────────────────────────────────

def _parse_credential_op(payload: dict, version: int) -> dict:
    """THE SEAM. Translate one plugin request into an operation this module can run.

    Deliberately unimplemented: the v1 request shape is defined by the Password Safe
    plugin and is not in this repository. Writing a plausible parser would produce a
    service that deploys cleanly, passes its own tests, and fails every real rotation
    — and a rotation that fails after the change has been applied is the worst
    outcome this system can produce.

    Fill this in from a CAPTURED request (see the module note), returning::

        {"op": "change" | "self_change" | "verify",
         "target": {"engine", "connection_name", "host", "port", "database", "ssl"},
         "principal": str, "new_password": str, "old_password": str,
         "as_user": str, "as_password": str}

    then route it through :func:`change_password` / :func:`verify_credential`, which
    are implemented and covered by tests/test_ps_dbops_workload.py.
    """
    raise ContractNotImplemented(
        f"contract version {version} is not implemented in this build")


def _credential_op(req: Request, ctx: Context) -> Response:
    payload = req.json()
    version = _contract_version(payload)

    if _capture_enabled():
        # The whole point of this build. Headers minus Authorization — dropped, not
        # redacted, because an ID token is a bearer credential and "***" in a log is
        # still an invitation to log it somewhere else. Everything else goes through
        # redact(), which masks every password/secret/token-shaped key.
        logs.emit("info", "dbops_contract_capture",
                  request_id=ctx.request_id, method=req.method, path=req.path,
                  query=logs.redact(dict(req.query or {})),
                  headers=logs.redact({k: v for k, v in (req.headers or {}).items()
                                       if k != "authorization"}),
                  body=logs.redact(payload),
                  body_bytes=len(req.body or b""))

    try:
        parsed = _parse_credential_op(payload, version)
    except ContractNotImplemented as exc:
        # 501, not 400: the request is almost certainly fine and this service is the
        # thing that is incomplete. Saying which versions it CAN serve is what makes
        # the address's ver= option usable rather than guesswork.
        logs.emit("warning", "dbops_contract_not_implemented",
                  request_id=ctx.request_id, contract_version=version)
        return Response(501, {"error": str(exc),
                              "supported_contract_versions":
                                  list(_SUPPORTED_CONTRACT_VERSIONS),
                              "request_id": ctx.request_id})
    except DbOpsError as exc:
        return Response(400, {"error": str(exc), "request_id": ctx.request_id})

    return _run(parsed, ctx)


def _run(parsed: dict, ctx: Context) -> Response:
    """Execute a parsed operation. Separate from parsing so the seam above is the
    only thing left to write, and so this half is testable without a contract."""
    op = parsed.get("op")
    try:
        if op == "verify":
            ok = verify_credential(parsed["target"], principal=parsed["principal"],
                                   password=parsed["new_password"])
            return Response(200, {"data": {"verified": bool(ok)}})
        if op in ("change", "self_change"):
            executed = change_password(
                parsed["target"], principal=parsed["principal"],
                new_password=parsed["new_password"],
                as_user=parsed["as_user"], as_password=parsed["as_password"],
                old_password=parsed.get("old_password", "") if op == "self_change" else "")
            return Response(200, {"data": {"changed": True,
                                           "statements_executed": executed}})
        raise DbOpsError(f"unknown operation {op!r}")
    except DbOpsError as exc:
        return Response(400, {"error": str(exc), "request_id": ctx.request_id})


def _contract_version(payload: dict) -> int:
    """The contract version this request claims, defaulting to 1.

    Read from the body rather than assumed, because the address's ``ver=`` option
    exists precisely so one service can be asked for a different one.
    """
    for key in ("contractVersion", "contract_version", "version", "ver"):
        raw = (payload or {}).get(key)
        if raw not in (None, ""):
            try:
                return int(raw)
            except (TypeError, ValueError):
                return -1
    return 1


def handle(req: Request, ctx: Context) -> Response:
    """Route one request. Auth already happened in fnruntime.dispatch."""
    path = (req.path or "/").rstrip("/") or "/"
    op_path = (_env("FN_DBOPS_PATH", _DEFAULT_PATH)).rstrip("/") or _DEFAULT_PATH

    # A reachability probe that touches no database. This is what an operator curls
    # (with an identity token) to prove the invoker binding, the audience and the
    # region placement before wiring a managed system to it.
    if req.method == "GET" and path in ("/", "/healthz", "/v1/health"):
        return Response(200, {
            "service": "bt-dbops",
            "implementation": "vm-dashboard ps_dbops",
            "contract_path": op_path,
            "supported_contract_versions": list(_SUPPORTED_CONTRACT_VERSIONS),
            "contract_implemented": False,
            "capture": _capture_enabled(),
            "region": ctx.region,
            "allowed_instances": len(allowed_instances()),
        })

    if path == op_path:
        if req.method != "POST":
            return Response(405, {"error": f"{op_path} accepts POST"})
        return _credential_op(req, ctx)

    # Named, because a 404 from a service reached over a custom audience is otherwise
    # indistinguishable from the audience pointing at the wrong service entirely.
    return Response(404, {"error": f"no route {req.method} {path} — this service "
                                   f"serves {op_path}"})
