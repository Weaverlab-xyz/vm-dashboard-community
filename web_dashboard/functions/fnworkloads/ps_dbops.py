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

**The v1 contract is implemented from the plugin repo's own specification** —
``docs/PLAN-CloudRunSqlServer.md`` §2 (envelope, status/code table) and §4 (the
statement per operation), which state they implement ``IDbOpsClient`` as already
declared plugin-side in ``Shared/Services/Transports.cs``. That is a written
specification from the other side of the wire, not a guess, which is what the seam was
waiting for.

**It has still never been exercised against the real plugin.** Two things in it are
inferences the specification does not spell out, both marked ``ASSUMPTION`` at the
point they are made: which credential ``change-self`` authenticates as, and the
``statementKind`` vocabulary beyond the one example value. ``FN_DBOPS_CAPTURE=1``
turns the redacted request log back on, and comparing one real request against
:func:`_parse_credential_op` settles both in a single rotation.

**Two deliberate divergences from the plan, both supersets:**

* The plan's service serves ``sqlserver`` only. This one also serves ``mysql``,
  because ``clouddb_ps_gcp_channel`` can be forced to ``cloud-run`` for every engine
  and the statement builders and driver for it are already here. ``postgres`` is
  refused with a sentence naming its own channel.
* ``passwordFormat: "prehashed"`` is refused with ``422 UNSUPPORTED_COMBINATION``
  rather than gated behind a flag. The plan ships that flag off and says why: a wrong
  salt length or SHA-512 framing produces an ``ALTER`` that SUCCEEDS while leaving a
  login nobody can authenticate as, *after* Password Safe has recorded the new
  password as authoritative. A flag whose only setting is "off" is worse than an
  honest refusal, so the ``HASHED`` statement form is not built at all.
"""
import os
import re
import time

from fnruntime import logs, tds
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
# The plan says the service accepts N and N-1. N is 1 and there is no version 0, so
# this is a one-element tuple today rather than a narrowing.
_SUPPORTED_CONTRACT_VERSIONS = (1,)
_CONNECT_TIMEOUT = 15

# The request's own ``timeoutSeconds`` bounds the connection attempt, because the plugin
# is the party that knows how long it is prepared to wait (its own default is 180, and
# the managed system's Timeout can lower that). Clamped at both ends: 0 or a garbage
# value falls back to _CONNECT_TIMEOUT, and no caller gets to pin a Cloud Run request
# thread open indefinitely. Cloud Run's own request deadline is the real ceiling.
_MIN_TIMEOUT = 5
_MAX_TIMEOUT = 300

# The plan's status/code table, §2. These strings are the contract -- the plugin
# switches on ``code``, so they are not free text and not for tidying.
_CODE_OK = "OK"
_CODE_BAD_REQUEST = "BAD_REQUEST"                  # 400
_CODE_AUTH_FAILED = "DB_AUTH_FAILED"               # 401
_CODE_PERMISSION_DENIED = "DB_PERMISSION_DENIED"   # 403
_CODE_NOT_FOUND = "PRINCIPAL_NOT_FOUND"            # 404
_CODE_POLICY_REJECTED = "POLICY_REJECTED"          # 409
_CODE_UNSUPPORTED = "UNSUPPORTED_COMBINATION"      # 422
_CODE_UNREACHABLE = "DB_UNREACHABLE"               # 502
_CODE_TIMEOUT = "DB_TIMEOUT"                       # 504

# ``statementKind`` is reported so a failure is diagnosable without ever putting the
# statement -- which carries the new password -- in a response body or a log line.
# ASSUMPTION: the plan gives exactly one example value, "ALTER_LOGIN". The rest of this
# vocabulary is this module's, so treat a plugin that switches on it as unproven.
_KIND_CONNECT = "CONNECT"
_KIND_ALTER_LOGIN = "ALTER_LOGIN"
_KIND_ALTER_USER = "ALTER_USER"
_KIND_LIST_LOGINS = "LIST_LOGINS"

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
    """A contract version this build does not serve. Answers 501 naming the ones it
    does, which is what makes the address's ``ver=`` option usable rather than
    guesswork."""


class UnsupportedCombination(DbOpsError):
    """A well-formed request whose combination of fields this service will not serve —
    422, not 400. A subclass of DbOpsError so anything that only knows the parent
    still refuses it rather than proceeding."""


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name, "") or default).strip()


def _capture_enabled() -> bool:
    """Whether to log the full redacted request.

    Defaults **OFF** now that the contract is implemented. It defaulted on while
    capturing the real request was the entire purpose of this build; leaving it on
    would keep writing a request body — redacted, but still — into Cloud Logging on
    every rotation forever, which is the opposite of this channel's whole argument
    (nothing at rest, no second copy of anything).

    Turn it back on for exactly as long as it takes to compare one real request against
    :func:`_parse_credential_op`. Every credential-shaped field goes through
    fnruntime.logs.redact first, and the Authorization header is dropped rather than
    redacted.
    """
    return _env("FN_DBOPS_CAPTURE", "0").lower() not in ("0", "false", "no", "off")


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

def connect_timeout(requested: int = 0) -> int:
    """The login timeout to use, from the request's ``timeoutSeconds``.

    Clamped rather than trusted. The plugin is the party that knows how long it will
    wait, so its number is the right default — but 0, a string, or 86400 all have to
    become something a Cloud Run request thread can survive.
    """
    try:
        value = int(requested or 0)
    except (TypeError, ValueError):
        return _CONNECT_TIMEOUT
    if value <= 0:
        return _CONNECT_TIMEOUT
    return max(_MIN_TIMEOUT, min(_MAX_TIMEOUT, value))


def _connect(engine: str, *, host: str, port: int, database: str, user: str,
             password: str, ssl: bool = True, timeout: int = 0):
    """One connection, as ``user``. Never as a stored identity — this service has none.

    TLS is the default and switching it off is an explicit per-managed-system choice
    (address field 5). Cloud SQL for SQL Server and MySQL both accept encrypted
    connections without a client certificate, so there is nothing to mount.

    ``ssl`` applies to MySQL only, as it always has. A SQL Server connection is
    encrypted unconditionally because the alternative does not exist: python-tds with
    no CA file cannot complete a handshake with any managed flavor here at all — see
    fnruntime.tds.
    """
    seconds = connect_timeout(timeout)
    if engine == "mysql":
        import pymysql
        return pymysql.connect(
            host=host, port=port, user=user, password=password,
            database=database or None, connect_timeout=seconds,
            ssl={"ssl": {}} if ssl else None, autocommit=True)
    if engine == "sqlserver":
        # Via fnruntime.tds, not pytds directly: python-tds encrypts only when it is
        # handed a CA file, and every managed SQL Server here refuses a connection
        # that does not. See that module.
        return tds.connect(host=host, port=port, database=database, user=user,
                           password=password, timeout=seconds)
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


def _query(connection, statement: str) -> list:
    """Rows for one SELECT. Separate from :func:`_execute`, which counts statements —
    the two read operations need the result set, and conflating them would make a
    fetch failure look like a statement failure."""
    cursor = connection.cursor()
    try:
        cursor.execute(statement)
        return list(cursor.fetchall() or [])
    finally:
        try:
            cursor.close()
        except Exception:
            pass


def server_version_statement(engine: str) -> str:
    """What ``verify`` runs once connected.

    The plan is explicit that on ``verify`` **the connection is the proof** — this
    statement exists to return something an operator can read in the response, not to
    test anything the login attempt did not already test.
    """
    if engine == "sqlserver":
        return "SELECT @@VERSION"
    if engine == "mysql":
        return "SELECT VERSION()"
    raise DbOpsError(f"unsupported engine {engine!r}")


def list_accounts_statement(engine: str) -> str:
    """What ``list-accounts`` runs. Needs VIEW ANY DEFINITION on SQL Server, held by
    ``CustomerDbRootRole``; needs SELECT on ``mysql.user`` on MySQL."""
    if engine == "sqlserver":
        # Verbatim from plan section 4. ##MS_*## are internal certificate-mapped logins
        # and cloudsqlsa is Google's own management login: neither is a rotatable
        # account, and returning them invites an operator to onboard something Cloud
        # SQL owns and will not let them change.
        return ("SELECT name, is_disabled, type_desc, create_date, modify_date "
                "FROM sys.sql_logins WHERE name NOT LIKE '##%' "
                "AND name <> 'cloudsqlsa' ORDER BY name")
    if engine == "mysql":
        # This module's own, NOT from the plan (whose service is SQL Server only). The
        # host qualifier is selected because a MySQL account IS user@host -- returning
        # bare names would give back accounts that cannot be rotated, which is the
        # exact round-trip the plugin depends on.
        return ("SELECT user, host, account_locked FROM mysql.user "
                "WHERE user <> '' ORDER BY user, host")
    raise DbOpsError(f"unsupported engine {engine!r}")


def change_password(target: dict, *, principal: str, new_password: str,
                    as_user: str, as_password: str, old_password: str = "") -> int:
    """Connect as ``as_user`` and set ``principal``'s password. Returns statements run."""
    check_instance(target.get("connection_name", ""))
    statements = change_password_statements(
        target["engine"], principal=principal, new_password=new_password,
        old_password=old_password)
    connection = _connect(target["engine"], host=target["host"], port=target["port"],
                          database=target.get("database", ""), user=as_user,
                          password=as_password, ssl=target.get("ssl", True),
                          timeout=target.get("timeout", 0))
    try:
        return _execute(connection, statements)
    finally:
        try:
            connection.close()
        except Exception:
            pass


def list_accounts(target: dict, *, as_user: str, as_password: str) -> list:
    """Every rotatable login on the instance, as ``[{"name": ...}, ...]``.

    ``name`` is what the plugin has to be able to hand straight back as a managed
    account, so on MySQL it is ``user@host`` rather than the bare name.
    """
    check_instance(target.get("connection_name", ""))
    engine = target["engine"]
    statement = list_accounts_statement(engine)
    connection = _connect(engine, host=target["host"], port=target["port"],
                          database=target.get("database", ""), user=as_user,
                          password=as_password, ssl=target.get("ssl", True),
                          timeout=target.get("timeout", 0))
    try:
        rows = _query(connection, statement)
    finally:
        try:
            connection.close()
        except Exception:
            pass
    accounts = []
    for row in rows:
        cells = list(row)
        if not cells:
            continue
        if engine == "mysql":
            user = str(cells[0])
            host = str(cells[1]) if len(cells) > 1 else "%"
            locked = str(cells[2] or "").upper() == "Y" if len(cells) > 2 else False
            entry = {"name": f"{user}@{host}", "disabled": locked}
        else:
            entry = {"name": str(cells[0]),
                     "disabled": bool(cells[1]) if len(cells) > 1 else False,
                     "type": str(cells[2]) if len(cells) > 2 else ""}
        accounts.append(entry)
    return accounts


def verify_credential(target: dict, *, principal: str, password: str) -> str:
    """The server version string, if ``principal`` can log in with ``password``.

    A login attempt, not a lookup, because that is the only thing that answers the
    question — and it is the reason ``cloud-run`` is the only GCP channel that can
    Verify Managed Account at all: both control-plane channels authenticate as the
    CALLER and cannot test an arbitrary principal's password.

    Returns a string rather than the bool it used to, because the plan's response
    envelope reports ``serverVersion`` on a successful verify. A REJECTED credential
    does not come back False here — it raises, and the caller maps it to
    ``401 DB_AUTH_FAILED``, which the plan is explicit is "a legitimate Verify 'false',
    not an infrastructure failure". Collapsing both into False would have thrown that
    distinction away at the one point it is cheap to keep.
    """
    check_instance(target.get("connection_name", ""))
    engine = target["engine"]
    connection = _connect(engine, host=target["host"], port=target["port"],
                          database=target.get("database", ""), user=principal,
                          password=password, ssl=target.get("ssl", True),
                          timeout=target.get("timeout", 0))
    try:
        rows = _query(connection, server_version_statement(engine))
    finally:
        try:
            connection.close()
        except Exception:
            pass
    if rows and list(rows[0]):
        return str(list(rows[0])[0] or "").strip()
    return ""


# ── The contract ─────────────────────────────────────────────────────────────

# ── Request vocabulary (plan §2) ─────────────────────────────────────────────

_OPERATIONS = ("verify", "change", "change-self", "list-accounts")

# The plan spells these hyphenated. The variants are accepted because a 400 over a
# casing or separator difference would be indistinguishable, from the operator's side,
# from the service being unreachable — and a non-canonical spelling is logged, so a
# real contract drift is still visible rather than absorbed.
_OPERATION_ALIASES = {
    "verify": "verify", "change": "change",
    "change-self": "change-self", "change_self": "change-self",
    "changeself": "change-self", "self-change": "change-self",
    "self_change": "change-self",
    "list-accounts": "list-accounts", "list_accounts": "list-accounts",
    "listaccounts": "list-accounts", "discover": "list-accounts",
}

_ENGINES = ("sqlserver", "mysql")
_ENGINE_ALIASES = {"sqlserver": "sqlserver", "mssql": "sqlserver",
                   "sql-server": "sqlserver", "sql_server": "sqlserver",
                   "mysql": "mysql", "postgres": "postgres",
                   "postgresql": "postgres", "pgsql": "postgres"}

_DEFAULT_PORTS = {"sqlserver": 1433, "mysql": 3306}
_DEFAULT_DATABASES = {"sqlserver": "master", "mysql": ""}


def _field(payload: dict, *names, default=""):
    """First present, non-empty value among ``names``.

    The plan's envelope is camelCase; this also accepts snake_case for every field.
    Liberal on the way IN only — what goes back out is exactly the plan's spelling.
    """
    for name in names:
        value = (payload or {}).get(name)
        if value not in (None, ""):
            return value
    return default


def _parse_credential_op(payload: dict, version: int) -> dict:
    """Translate one plugin request into an operation this module can run.

    Implemented from the plugin repository's own specification —
    ``docs/PLAN-CloudRunSqlServer.md`` §2 for the envelope and §4 for what each
    operation runs — which states it implements ``IDbOpsClient`` as already declared in
    the plugin's ``Shared/Services/Transports.cs``.

    Returns::

        {"op": "verify" | "change" | "change-self" | "list-accounts",
         "target": {"engine", "connection_name", "host", "port", "database", "ssl",
                    "timeout"},
         "principal": str, "new_password": str, "old_password": str,
         "as_user": str, "as_password": str,
         "request_id": str, "version": int, "secrets": [str, ...]}

    ``secrets`` exists so :func:`_scrub` can guarantee no credential reaches a response
    body or a log line even if a driver embeds one in an error message. Raises
    :class:`DbOpsError` for anything malformed (→ 400) and
    :class:`ContractNotImplemented` for a version this build does not serve (→ 501).
    """
    if version < 0:
        raise DbOpsError(
            "contractVersion is not an integer — the plugin sends it as the first "
            "field of the envelope precisely so a mismatch is diagnosable")
    if version not in _SUPPORTED_CONTRACT_VERSIONS:
        raise ContractNotImplemented(
            f"contract version {version} is not implemented in this build")
    if not isinstance(payload, dict) or not payload:
        raise DbOpsError(
            "the request body is not a JSON object — expected the v1 credential-op "
            "envelope")

    raw_op = str(_field(payload, "operation", "op")).strip().lower()
    op = _OPERATION_ALIASES.get(raw_op)
    if not op:
        raise DbOpsError(
            f"operation {raw_op!r} is not one of {', '.join(_OPERATIONS)}")

    raw_engine = str(_field(payload, "engine", default="sqlserver")).strip().lower()
    engine = _ENGINE_ALIASES.get(raw_engine, raw_engine)
    if engine == "postgres":
        # Reachable only if an operator forced clouddb_ps_gcp_channel=cloud-run for
        # every engine. Named rather than left to the driver import, because "no
        # module named pg8000" is not an answer anybody can act on.
        raise DbOpsError(
            "PostgreSQL is not built into this DB-Ops service — postgres uses the "
            "'data-api' channel, which needs no service at all. Leave "
            "clouddb_ps_gcp_channel on 'auto'")
    if engine not in _ENGINES:
        raise DbOpsError(
            f"engine {raw_engine!r} is not one of {', '.join(_ENGINES)}")

    # The target. check_instance is the fail-closed boundary: IAM authenticates the
    # caller but does not scope which instance it may act on, and the target comes from
    # the REQUEST on this channel.
    connection_name = check_instance(
        str(_field(payload, "instanceConnectionName", "instance_connection_name",
                   "connectionName", "connection_name")))

    host = str(_field(payload, "privateIp", "private_ip", "host", "address")).strip()
    if not host:
        # This service CANNOT resolve it. Plan §1 and §6 keep the Cloud Run service
        # account deliberately tiny — none of roles/cloudsql.client,
        # roles/cloudsql.instanceUser or roles/cloudsql.admin — so there is no
        # instances.get available here, by design rather than by omission. Say that,
        # because "privateIp is required" alone reads like a field that could be made
        # optional.
        raise DbOpsError(
            "privateIp is required and was not sent. This service holds no Cloud SQL "
            "API permission at all (no roles/cloudsql.* — see the plan's IAM section), "
            "so it cannot resolve the address from instanceConnectionName itself; the "
            "plugin has to send it")

    try:
        port = int(_field(payload, "port", default=0) or 0)
    except (TypeError, ValueError):
        raise DbOpsError("port is not an integer")
    if not port:
        port = _DEFAULT_PORTS[engine]

    database = str(_field(payload, "database", "dbName", "db_name",
                          default=_DEFAULT_DATABASES[engine]))

    # The envelope carries no TLS field, so this defaults to ON and can only be turned
    # off by an explicit false — the same direction address field 5 defaults in.
    ssl_raw = _field(payload, "ssl", "requireSsl", "require_ssl", "encrypt",
                     default=True)
    ssl = str(ssl_raw).strip().lower() not in ("0", "false", "no", "off", "sslfalse")

    password_format = str(_field(payload, "passwordFormat", "password_format",
                                 default="plaintext")).strip().lower()

    login_user = str(_field(payload, "loginUser", "login_user")).strip()
    login_password = str(_field(payload, "loginPassword", "login_password"))
    target_user = str(_field(payload, "targetUser", "target_user")).strip()
    new_password = str(_field(payload, "newPassword", "new_password"))
    current_password = str(_field(payload, "currentPassword", "current_password"))
    timeout = _field(payload, "timeoutSeconds", "timeout_seconds", "timeout", default=0)

    if op in ("change", "change-self"):
        if password_format not in ("plaintext", ""):
            # 422, not 400: the request is well-formed and the combination is the
            # problem. The HASHED statement form is not built — see the module note.
            raise UnsupportedCombination(
                f"passwordFormat={password_format!r} is not supported. Only 'plaintext' "
                f"is built: the HASHED form skips CHECK_POLICY, and a wrong salt length "
                f"or SHA-512 framing produces an ALTER that SUCCEEDS while leaving a "
                f"login nobody can authenticate as — after Password Safe has already "
                f"recorded the new password as authoritative")
        if not target_user:
            raise DbOpsError(f"targetUser is required for operation {op!r}")
        if not new_password:
            raise DbOpsError(f"newPassword is required for operation {op!r}")

    if op == "change-self":
        # ASSUMPTION, and the one worth checking against a captured request. Plan §4
        # says change-self is "ALTER LOGIN [target] ... OLD_PASSWORD = N'old'" and that
        # OLD_PASSWORD "lets a login change its own password with no elevated
        # permission" — which is only true when the SESSION is that login. So the
        # authenticating identity defaults to the target with its current password, and
        # a request naming a different one is refused rather than sent: SQL Server would
        # answer that with a permission error, and a permission error on a self-rotation
        # is the single most misleading failure this service could return.
        login_user = login_user or target_user
        login_password = login_password or current_password
        if not current_password:
            raise DbOpsError(
                "currentPassword is required for operation 'change-self' — it is the "
                "OLD_PASSWORD that lets a login change its own password without "
                "ALTER ANY LOGIN")
        if login_user != target_user:
            raise DbOpsError(
                f"operation 'change-self' authenticates AS the account it changes, but "
                f"loginUser={login_user!r} and targetUser={target_user!r} differ. Use "
                f"operation 'change' to rotate one account as another")

    if not login_user:
        raise DbOpsError(f"loginUser is required for operation {op!r}")
    if not login_password:
        # No IAM database authentication exists for Cloud SQL for SQL Server, so there
        # is no mode in which this is legitimately absent.
        raise DbOpsError(
            f"loginPassword is required for operation {op!r} — this channel exists "
            f"because Cloud SQL has no IAM database authentication for SQL Server, so "
            f"there is no token to fall back to")

    return {
        "op": op,
        "target": {"engine": engine, "connection_name": connection_name,
                   "host": host, "port": port, "database": database, "ssl": ssl,
                   "timeout": connect_timeout(timeout)},
        "principal": target_user,
        "new_password": new_password,
        "old_password": current_password if op == "change-self" else "",
        "as_user": login_user,
        "as_password": login_password,
        "request_id": str(_field(payload, "requestId", "request_id")),
        "version": version,
        "canonical_operation": raw_op == op,
        "secrets": [v for v in (login_password, new_password, current_password) if v],
    }


# ── Failure classification (plan §2's status/code table) ─────────────────────
#
# The plugin switches on ``code``, so a wrong mapping does not merely mislabel a
# failure — it sends the plugin down the wrong branch. Where SQL Server itself refuses
# to distinguish two cases, that is called out rather than guessed at.

_ERROR_NUMBERS = {
    # 401 -- the database rejected the credential. Plan: "a legitimate Verify 'false',
    # not an infrastructure failure".
    18456: (401, _CODE_AUTH_FAILED),   # SQL Server: login failed for user
    18452: (401, _CODE_AUTH_FAILED),   # SQL Server: login from an untrusted domain
    1045: (401, _CODE_AUTH_FAILED),    # MySQL: access denied for user
    # 404 -- the principal does not exist. Unambiguous, unlike 15151 below.
    15007: (404, _CODE_NOT_FOUND),     # SQL Server: is not a valid login
    1396: (404, _CODE_NOT_FOUND),      # MySQL: operation ALTER USER failed for
    # 403 -- authenticated but not permitted over the target.
    15247: (403, _CODE_PERMISSION_DENIED),
    297: (403, _CODE_PERMISSION_DENIED),
    1227: (403, _CODE_PERMISSION_DENIED),   # MySQL: access denied; need privilege
    1142: (403, _CODE_PERMISSION_DENIED),   # MySQL: command denied to user
    # 409 -- the password itself was refused by policy (CHECK_POLICY / validate_password).
    15116: (409, _CODE_POLICY_REJECTED),
    15118: (409, _CODE_POLICY_REJECTED),
    1819: (409, _CODE_POLICY_REJECTED),     # MySQL: does not satisfy the policy
}

# SQL Server 15151 is "Cannot alter the login 'x', because it does not exist or you do
# not have permission" -- it CONFLATES 403 and 404 deliberately, so that an unprivileged
# caller cannot enumerate logins by probing. There is no way to tell them apart from the
# error, so it maps to 403 and says so in ``detail``: of the two, "you lack the grant" is
# the one an operator can act on, and the §4 grant is the documented remedy.
_AMBIGUOUS_NUMBERS = {15151, 15150}

_TIMEOUT_MARKERS = ("timeout", "timed out", "timeoutexpired")
_UNREACHABLE_MARKERS = ("getaddrinfo", "name or service not known", "no route to host",
                        "connection refused", "network is unreachable", "unreachable",
                        "cannot connect", "could not connect", "connection reset",
                        "handshake", "ssl", "tls", "broken pipe", "econnrefused")


def _error_number(exc) -> int:
    """The driver's own error number, or 0. pytds exposes ``number``/``msg_no``, pymysql
    puts it in ``args[0]`` — read all of them rather than importing either driver, which
    is not installed outside the deployed image."""
    for attr in ("number", "msg_no", "errno", "code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int) and value:
            return value
    args = getattr(exc, "args", ()) or ()
    if args and isinstance(args[0], int):
        return args[0]
    return 0


def _classify(exc) -> tuple:
    """``(status, code, detail)`` for a driver exception."""
    text = str(exc) or exc.__class__.__name__
    number = _error_number(exc)
    if number in _ERROR_NUMBERS:
        status, code = _ERROR_NUMBERS[number]
        return status, code, text
    if number in _AMBIGUOUS_NUMBERS:
        return 403, _CODE_PERMISSION_DENIED, (
            f"{text} — SQL Server reports 'does not exist or you do not have "
            f"permission' as one error and does not distinguish them, so this may also "
            f"be a missing login. Confirm the grant first: ALTER SERVER ROLE "
            f"CustomerDbRootRole ADD MEMBER [<functional account>]")
    low = text.lower()
    if any(marker in low for marker in _TIMEOUT_MARKERS):
        return 504, _CODE_TIMEOUT, text
    if isinstance(exc, OSError) or any(m in low for m in _UNREACHABLE_MARKERS):
        return 502, _CODE_UNREACHABLE, text
    # Not in the plan's table, and deliberately not forced into it: borrowing a
    # documented code for an unrecognised failure sends the plugin down a branch the
    # failure does not justify. An unknown code degrades to "it failed", which is true.
    return 500, "DB_ERROR", text


def _scrub(detail, secrets) -> str:
    """``detail`` with every credential value replaced.

    Structural, not best-effort (plan §8): the drivers are third-party and an error
    message that embeds the value it rejected is exactly the kind of thing discovered
    after it has been written to Cloud Logging for a month. The passwords are known
    here, so removing them is a substring replace rather than a pattern guess.
    """
    text = "" if detail is None else str(detail)
    for secret in sorted({s for s in (secrets or []) if s}, key=len, reverse=True):
        if len(secret) >= 4:
            text = text.replace(secret, "***")
    return text


def _envelope(status: int, code: str, *, version: int, request_id: str, started: float,
              detail=None, success: bool = False, secrets=None, **extra) -> Response:
    """One response shape for every outcome, per plan §2. ``detail`` is scrubbed and
    ``statementKind`` is reported in place of the statement — never the statement, which
    carries the new password."""
    body = {
        "contractVersion": version if version in _SUPPORTED_CONTRACT_VERSIONS else 1,
        "requestId": request_id or "",
        "success": bool(success),
        "code": code,
        "detail": (_scrub(detail, secrets) or None) if detail is not None else None,
        "elapsedMs": int(max(0.0, (time.monotonic() - started)) * 1000),
    }
    body.update(extra)
    return Response(status, body)


def _credential_op(req: Request, ctx: Context) -> Response:
    started = time.monotonic()
    payload = req.json()
    version = _contract_version(payload)
    request_id = str(_field(payload, "requestId", "request_id")) or ctx.request_id or ""

    if _capture_enabled():
        # Off by default now the contract is implemented; this is the switch to flip
        # when comparing one real request against the parser. Headers minus
        # Authorization — dropped, not redacted, because an ID token is a bearer
        # credential and "***" in a log is still an invitation to log it somewhere
        # else. Everything else goes through redact(), which masks every
        # password/secret/token-shaped key.
        logs.emit("info", "dbops_contract_capture",
                  request_id=request_id, method=req.method, path=req.path,
                  query=logs.redact(dict(req.query or {})),
                  headers=logs.redact({k: v for k, v in (req.headers or {}).items()
                                       if k != "authorization"}),
                  body=logs.redact(payload),
                  body_bytes=len(req.body or b""))

    # Read straight off the payload rather than off the parsed dict, because these are
    # the failure paths where parsing did NOT succeed. No parser message interpolates a
    # password today; this is here so that adding one later cannot become a leak.
    raw_secrets = [str(_field(payload, *names)) for names in (
        ("loginPassword", "login_password"),
        ("newPassword", "new_password"),
        ("currentPassword", "current_password"))]

    def _fail(status, code, detail, **extra):
        return _envelope(status, code, version=version, request_id=request_id,
                         started=started, detail=detail, secrets=raw_secrets, **extra)

    try:
        parsed = _parse_credential_op(payload, version)
    except ContractNotImplemented as exc:
        # 501, not 400: the request is fine and this service is the thing that does not
        # serve that version. Saying which versions it CAN serve is what makes the
        # address's ver= option usable rather than guesswork.
        logs.emit("warning", "dbops_contract_not_implemented",
                  request_id=request_id, contract_version=version)
        return _fail(501, "UNSUPPORTED_CONTRACT_VERSION", str(exc),
                     supported_contract_versions=list(_SUPPORTED_CONTRACT_VERSIONS))
    except UnsupportedCombination as exc:
        return _fail(422, _CODE_UNSUPPORTED, str(exc))
    except DbOpsError as exc:
        return _fail(400, _CODE_BAD_REQUEST, str(exc))

    if not parsed.get("canonical_operation", True):
        # Accepted, but visible: a non-canonical spelling arriving from the real plugin
        # means this module's alias table is papering over a contract difference.
        logs.emit("warning", "dbops_operation_spelling",
                  request_id=request_id, operation=parsed.get("op"))

    return _run(parsed, ctx, started=started)


# The statement kind reported per operation. Never the statement: on a change it
# carries the new password, and plan §8 is explicit that only the kind goes out.
_STATEMENT_KINDS = {
    "verify": lambda engine: _KIND_CONNECT,
    "list-accounts": lambda engine: _KIND_LIST_LOGINS,
    "change": lambda engine: _KIND_ALTER_LOGIN if engine == "sqlserver" else _KIND_ALTER_USER,
    "change-self": lambda engine: _KIND_ALTER_LOGIN if engine == "sqlserver" else _KIND_ALTER_USER,
}


def _run(parsed: dict, ctx: Context, *, started: float) -> Response:
    """Execute a parsed operation and answer in the plan's envelope.

    Separate from parsing so each half is testable alone: the parser needs no database
    and this needs no HTTP.
    """
    op = parsed["op"]
    target = parsed["target"]
    engine = target["engine"]
    secrets = parsed.get("secrets") or []
    kind = _STATEMENT_KINDS[op](engine)

    def _reply(status, code, *, success=False, detail=None, **extra):
        return _envelope(status, code, version=parsed["version"],
                         request_id=parsed["request_id"], started=started,
                         detail=detail, success=success, secrets=secrets,
                         statementKind=kind, **extra)

    def _done(outcome, code, error_code=""):
        # Plan §8's explicit field whitelist. Never the body, never a password, and
        # never an exception object — logger.LogError(ex, ...) inside a
        # credential-handling path is the pattern that section forbids.
        logs.emit("info" if outcome == "ok" else "warning", "dbops_credential_op",
                  request_id=parsed["request_id"], operation=op, engine=engine,
                  instance=target["connection_name"], target_user=parsed["principal"],
                  outcome=outcome, code=code, error_code=error_code,
                  statement_kind=kind,
                  elapsed_ms=int(max(0.0, time.monotonic() - started) * 1000))

    try:
        if op == "verify":
            # The connection IS the proof (plan §4). A rejected credential raises and
            # is classified as 401 DB_AUTH_FAILED, which the plan calls a legitimate
            # Verify "false" rather than an infrastructure failure.
            version_text = verify_credential(
                target, principal=parsed["as_user"], password=parsed["as_password"])
            _done("ok", _CODE_OK)
            return _reply(200, _CODE_OK, success=True, serverVersion=version_text)

        if op == "list-accounts":
            accounts = list_accounts(target, as_user=parsed["as_user"],
                                     as_password=parsed["as_password"])
            _done("ok", _CODE_OK)
            return _reply(200, _CODE_OK, success=True, accounts=accounts)

        if op in ("change", "change-self"):
            executed = change_password(
                target, principal=parsed["principal"],
                new_password=parsed["new_password"],
                as_user=parsed["as_user"], as_password=parsed["as_password"],
                old_password=parsed.get("old_password", ""))
            _done("ok", _CODE_OK)
            return _reply(200, _CODE_OK, success=True,
                          statementsExecuted=executed)

        # Unreachable: the parser only emits names from _OPERATIONS. Kept so a new
        # operation added to the parser and not here fails loudly instead of falling
        # through to a 200.
        raise DbOpsError(f"operation {op!r} is not implemented in this service")

    except UnsupportedCombination as exc:
        _done("refused", _CODE_UNSUPPORTED)
        return _reply(422, _CODE_UNSUPPORTED, detail=str(exc))
    except DbOpsError as exc:
        _done("refused", _CODE_BAD_REQUEST)
        return _reply(400, _CODE_BAD_REQUEST, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        # Everything the drivers raise lands here and is mapped to the plan's table.
        # Caught broadly on purpose: an unclassified driver exception escaping would be
        # rendered by the runtime as a 500 with a stack trace, and a stack trace from a
        # credential-handling path is the one thing §8 rules out.
        status, code, detail = _classify(exc)
        _done("failed", code, error_code=str(_error_number(exc) or ""))
        return _reply(status, code, detail=detail)


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
            "contract_implemented": True,
            "operations": list(_OPERATIONS),
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
