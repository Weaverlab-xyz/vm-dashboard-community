"""
Cloud-database SQL layer for the optional Password Safe integration (AWS-only).

The dashboard-provisioned DB is private; the only dashboard component with
line-of-sight to it is the shared PRA Jumpoint EC2 host. This module builds the
per-engine SQL and wraps it in a DB-client invocation — ``docker run`` for
PostgreSQL/MySQL, the natively installed sqlcmd for SQL Server — that is run
on that host over AWS SSM Run Command (see aws_service.ssm_send_command) — the
same SSM path Password Safe's DB custom plugin uses for rotation, so no DB
drivers are added to the dashboard image and no separate jump host is needed.

Only ONE DB principal is created, from the minted admin credential: a dedicated
non-privileged **managed user** — the rotation target Password Safe owns. It
(not the powerful master) is what the PRA tunnel injects.

There is deliberately NO separate DB "functional login": rotation is driven over
AWS SSM with the IAM user acting as Password Safe's functional account, and the
managed account changes its OWN password (self-rotation), which needs no elevated
DB privilege. So the managed user is created with a login and nothing more —
operators grant it whatever application access they want out of band.

Everything here is pure string-building plus the shell command list; it never
opens a DB connection itself, so it is unit-testable without a live database.

SECURITY NOTE: the built commands embed the admin connect password (via a
container env var) and the new managed password (in the CREATE statement) — these
ride the SSM command document, which is IAM-gated but not encrypted like the
plugin's RSA path. Acceptable for a one-time onboarding the dashboard already
holds the secrets for; do not log the returned command list.
"""
import re
import secrets
import string

VALID_ENGINES = ("postgres", "mysql", "sqlserver")

# Per-engine defaults. The client image is overridable via settings so an
# air-gapped/mirrored registry can be pointed at instead of Docker Hub / MCR.
#
# SQL Server's default is deliberately EMPTY — "" means "no container, run the
# native client on the jump host". There is no sqlcmd image to default to:
# `mcr.microsoft.com/mssql-tools18` (the default until 2026-09-02) does not exist
# and never did. That string is the apt/dnf PACKAGE name, which is how the jump-host
# prep installs it; MCR publishes `mssql-tools` (sqlcmd 17, a different binary path
# and different flags) and no `mssql-tools18` repository at all, so every SQL Server
# registration died on `docker run` rc=125 ("...: not found") before a byte reached
# the database. See _mssql_command.
_ENGINE = {
    "postgres":  {"image": "postgres:16", "port": 5432},
    "mysql":     {"image": "mysql:8.4",   "port": 3306},
    "sqlserver": {"image": "",            "port": 1433},
}

# Where both clouds' jump-host prep installs the `mssql-tools18` package, and the
# path the Password Safe rotation plugins already invoke. Kept as one constant so
# the onboarding and the container fallback below cannot drift from each other.
SQLCMD_PATH = "/opt/mssql-tools18/bin/sqlcmd"

# Repositories that do not exist and never did. Changing the FIELD default is not
# enough on a live instance: the settings panel writes an `app_config` row when the
# panel is saved, and a row written by an older instance outlives the default it was
# seeded from — so the phantom image would keep being handed to `docker run` and this
# fix would be inert exactly where it is needed. Dropped on read instead.
_PHANTOM_IMAGES = ("mcr.microsoft.com/mssql-tools18",)

# DB identifiers we create — restrict hard so they are safe to interpolate into
# SQL (no quoting games) and into a shell command.
_IDENT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,62}$")
# Values we single-quote into shell/SQL. Generated passwords use only these; the
# admin password (secrets.token_urlsafe) is URL-safe base64 (also within this set).
_SAFE_VALUE_RE = re.compile(r"^[A-Za-z0-9#\-_.]+$")

# Longest account name each engine will ACCEPT. Not cosmetic, and not the same
# number: MySQL's ``mysql.user.User`` column is ``char(32)`` and a longer name is
# rejected outright with ER_WRONG_STRING_LENGTH (1470); Postgres truncates at
# NAMEDATALEN-1 = 63; SQL Server's ``sysname`` is 128.
#
# One blind cap of 63 here is what broke the MySQL ephemeral path in full, live on
# 2026-08-31: ``jit_`` + a 24-char slug + ``_`` + a 12-char request token is 41
# characters, so EVERY grant for a real email address failed on the very first
# statement. It read as an infrastructure fault rather than a naming one, because
# pymysql maps no errno it does not recognise — 1470 arrives as a bare
# ``OperationalError`` with the message nowhere in the log (see
# fnruntime/dispatch.py, which now records the driver code and detail).
#
# 63 was right for exactly one engine and silently wrong for the one being used.
_MAX_IDENT = {"postgres": 63, "mysql": 32, "sqlserver": 128}

# What to assume when the engine is not known. The SMALLEST limit, never the
# largest: a name that is too short is ugly, a name that is too long does not exist.
_MIN_MAX_IDENT = min(_MAX_IDENT.values())


def max_identifier_length(engine: str) -> int:
    """The longest account name ``engine`` accepts (conservative if unknown)."""
    return _MAX_IDENT.get(engine, _MIN_MAX_IDENT)


class CloudDbSqlError(Exception):
    """Raised on invalid identifiers/values passed to the SQL builders."""


def default_port(engine: str) -> int:
    return _ENGINE[engine]["port"]


def default_client_image(engine: str) -> str:
    """The DB-client container image for ``engine``, or ``""`` when the client runs
    natively on the jump host (SQL Server — see the ``_ENGINE`` note)."""
    return _ENGINE[engine]["image"]


def resolve_client_image(engine: str, configured: str = "") -> str:
    """The image to actually run for ``engine`` — ``configured`` if it names a real
    one, else this engine's default — or ``""`` meaning "run the client natively".

    Callers pass the raw setting and use the result for BOTH the command build and
    the decision to install a native client, so a phantom reference cannot make those
    two disagree (which would install nothing and then run nothing).

    A dropped phantom falls back to the ENGINE DEFAULT, not to ``""``. For SQL Server
    that default is "" (native), which is the point; for PostgreSQL/MySQL it keeps
    their real images, so this function can never hand `docker run` an empty image
    argument — those two builders have no native mode to fall back to."""
    image = (configured or "").strip() or default_client_image(engine)
    for phantom in _PHANTOM_IMAGES:
        if image == phantom or image.startswith((phantom + ":", phantom + "@")):
            return default_client_image(engine)
    return image


def generate_password(length: int = 24) -> str:
    """A random password that satisfies SQL Server complexity (≥3 of upper/lower/
    digit/symbol) and is safe to single-quote into both SQL and a POSIX shell —
    only ``[A-Za-z0-9#-_]``, so no escaping is ever required downstream."""
    symbols = "#-_"
    pools = [string.ascii_lowercase, string.ascii_uppercase, string.digits, symbols]
    all_chars = string.ascii_letters + string.digits + symbols
    length = max(length, 8)
    chars = [secrets.choice(p) for p in pools]
    chars += [secrets.choice(all_chars) for _ in range(length - len(chars))]
    rng = secrets.SystemRandom()
    rng.shuffle(chars)
    return "".join(chars)


def _ident(name: str) -> str:
    if not _IDENT_RE.match(name or ""):
        raise CloudDbSqlError(
            f"unsafe DB identifier {name!r} — must match {_IDENT_RE.pattern}")
    return name


def _value(val: str) -> str:
    if not _SAFE_VALUE_RE.match(val or ""):
        raise CloudDbSqlError(
            "unsafe DB value for shell/SQL interpolation (contains quotes or shell "
            "metacharacters); regenerate the credential")
    return val


# ── Per-engine SQL statement builders (pure) ──────────────────────────────────

# Onboarding is CREATE-OR-RESET, never a bare CREATE. The managed user's name is
# derived from the database row and owned by the dashboard, and the caller mints a
# fresh password on every run — so the only correct outcome is "this user exists with
# THIS password", whether or not a previous attempt got that far. A bare CREATE made
# the documented remedy unusable: the Password Safe onboarding that failed after the
# user was created (live 2026-08-27, `Bad IP value` on the managed-system create) left
# the row's "Register in Password Safe" action failing forever with
# `role "psafe_950fc41dbd7e" already exists`, with no way to make progress from the UI.
# Resetting is not a second remote resource — it is the SAME user, converged onto the
# credential this run is about to hand Password Safe.


def _pg_onboard_sql(managed: str, managed_pw: str) -> list:
    # Postgres has no CREATE ROLE IF NOT EXISTS, so the branch goes in a DO block. Its
    # body is a single-quoted string literal with the inner quotes doubled rather than
    # a $$-quoted one: the statement is interpolated into a double-quoted shell word,
    # where $$ would expand to the shell's PID before psql ever sees it.
    return [
        "DO 'BEGIN "
        f"IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = ''{managed}'') THEN "
        f"ALTER ROLE \"{managed}\" WITH LOGIN PASSWORD ''{managed_pw}''; ELSE "
        f"CREATE ROLE \"{managed}\" WITH LOGIN PASSWORD ''{managed_pw}''; END IF; END';"
    ]


def _mysql_onboard_sql(managed: str, managed_pw: str) -> list:
    # 8.4 defaults new users to caching_sha2_password (which the PRA tunnel
    # requires — no mysql_native_password). The ALTER is unconditional so a user left
    # by an earlier attempt ends on this run's password; on a fresh create it is a
    # no-op that re-sets what CREATE USER just set.
    return [f"CREATE USER IF NOT EXISTS '{managed}'@'%' IDENTIFIED BY '{managed_pw}';",
            f"ALTER USER '{managed}'@'%' IDENTIFIED BY '{managed_pw}';"]


def _mssql_onboard_sql(managed: str, managed_pw: str) -> list:
    # Two statements, so the caller's "\nGO\n" join puts ALTER LOGIN alone in its own
    # batch — the form that is valid whatever options it carries.
    return [f"IF NOT EXISTS (SELECT 1 FROM sys.server_principals WHERE name = '{managed}') "
            f"CREATE LOGIN [{managed}] WITH PASSWORD = '{managed_pw}';",
            f"ALTER LOGIN [{managed}] WITH PASSWORD = '{managed_pw}';"]


def _pg_teardown_sql(managed: str) -> list:
    return [f'DROP ROLE IF EXISTS "{managed}";']


def _mysql_teardown_sql(managed: str) -> list:
    return [f"DROP USER IF EXISTS '{managed}'@'%';"]


def _mssql_teardown_sql(managed: str) -> list:
    return [f"IF EXISTS (SELECT 1 FROM sys.server_principals WHERE name = '{managed}') DROP LOGIN [{managed}];"]


# ── DB-client command builders (run on the jump host) ───────────────────────
#
# PostgreSQL and MySQL run their client as a throwaway `docker run` (postgres:16 /
# mysql:8.4 are real images and the jump hosts on both clouds run docker). SQL Server
# runs the NATIVE sqlcmd the prep installs, because no sqlcmd container image exists
# to run — see the _ENGINE note and _mssql_command.
#
# TLS on the jumphost→DB hop. Every DB terraform module here turns the server's
# *forced*-TLS knob OFF — db_azure_postgres/db_azure_mysql set
# require_secure_transport=OFF, db_postgres attaches an rds.force_ssl=0 group,
# db_gcp_* set ssl_mode=ALLOW_UNENCRYPTED_AND_ENCRYPTED — because the PRA protocol
# tunnel proxies the *cleartext* wire protocol (that is how it injects the Vault
# credential and records the session) and has no backend-TLS option.
#
# That is a constraint on the TUNNEL, not on us. These builders open their own
# psql/mysql/sqlcmd connection, and every one of those servers still OFFERS TLS:
# "forced off" is not "unavailable", and ALLOW_UNENCRYPTED_AND_ENCRYPTED says so in
# its name. Proof it works from this exact network position — the Password Safe
# plugin's own rotation connections, to the very same servers, run with TLS on all
# three clouds (clouddb_ps_azure_ssl / clouddb_ps_ssm_ssl / clouddb_ps_gcp_dbops_ssl
# all default True, which is the trailing `sslTRUE` segment of the packed managed-
# system address). The original commit (7e89d6f) mirrored the *server's* posture into
# the client and connected cleartext, which put the admin master password on the wire
# in the clear for no reason at all.
#
# So: encrypt, but do NOT verify. `sslmode=require`, `--ssl-mode=REQUIRED` and sqlcmd
# `-N -C` all mean "this connection must be encrypted; do not check the certificate".
# Verifying instead (`verify-full` / `VERIFY_IDENTITY` / `-N` without `-C`) would need
# each cloud's root CA reachable by the client: the stock postgres:16 and mysql:8.4
# images carry only the OS trust store, as does the jump host's own, and nothing pins
# the DigiCert root Azure Flexible Server presents, the Amazon RDS roots or the
# per-instance Google Cloud SQL server CA. Shipping those bundles to the jump host is a
# separate change, out of scope here: this hop is intra-VNet/VPC to a private endpoint,
# so encrypting the admin password on the wire is the win that matters.

def _pg_command(*, host, port, database, admin_user, admin_password, image, statements) -> str:
    db = _ident(database) if database else "postgres"
    conn = f"host={host} port={int(port)} dbname={db} user={admin_user} sslmode=require"
    parts = [
        "docker", "run", "--rm",
        "-e", f"PGPASSWORD='{admin_password}'",
        image, "psql", f'"{conn}"', "-v", "ON_ERROR_STOP=1",
    ]
    for stmt in statements:
        parts += ["-c", f'"{stmt}"']
    return " ".join(parts)


def _mysql_command(*, host, port, database, admin_user, admin_password, image, statements) -> str:
    batch = " ".join(statements)
    parts = [
        "docker", "run", "--rm",
        "-e", f"MYSQL_PWD='{admin_password}'",
        image, "mysql",
        f"--host={host}", f"--port={int(port)}", f"--user={admin_user}",
        "--ssl-mode=REQUIRED", "--batch",
        "-e", f'"{batch}"',
    ]
    return " ".join(parts)


def _mssql_command(*, host, port, database, admin_user, admin_password, image, statements) -> str:
    """sqlcmd invoking ``statements`` against ``host``, NATIVELY on the jump host.

    Native rather than containerised because there is nothing to containerise:
    `mcr.microsoft.com/mssql-tools18` — the default image this builder carried until
    2026-09-02 — is not a registry repository, so `docker run` exited 125 without
    connecting, and SQL Server registration could never have succeeded on either
    cloud. `mssql-tools18` is the apt/dnf package name, and both clouds' jump-host
    prep installs it (cloud_database_service._AZURE_CLIENT_INSTALL /
    _SSM_CLIENT_INSTALL) for the rotation plugin, which invokes this same binary at
    this same path — so onboarding and rotation now use one client, not two.

    ``image`` is the opt-in escape hatch (a mirrored/air-gapped registry, or a jump
    host where the package install is not wanted): a non-empty value goes back
    through docker.
    """
    # -b: exit non-zero on SQL error. -N (encrypt) -C (trust the server cert without
    # checking it against a CA) — see the TLS note above. Batches joined with GO.
    #
    # `-N` is passed BARE, and that is the whole point of this line. sqlcmd's encryption
    # switch is documented as `-N[s|m|o]`: the value is attached to the switch with no
    # space (`-Ns`, `-Nm`, `-No`) and the only accepted letters are strict/mandatory/
    # optional. `t` (for "true") belongs to the *Go* sqlcmd's `true|false|disable` set —
    # a different binary. So `-N t` and the earlier `-N o` were BOTH malformed for
    # /opt/mssql-tools18/bin/sqlcmd, and it rejected the command line before opening a
    # socket: `Sqlcmd: Command -N: Invalid Parameters passed.`, rc=1, observed live on
    # Azure 2026-09-02 at 25% "Creating the rotatable managed database user…".
    # Bare `-N` means "encrypt" in every sqlcmd — the boolean flag of 17, and the
    # documented `-Nm` (mandatory) default of the value-taking 18+ — so it is the one
    # form that cannot be version-dependent, and `-C` then waives cert validation.
    batch = "\nGO\n".join(statements) + "\nGO\n"
    args = [
        "-S", f"{host},{int(port)}", "-U", admin_user, "-d", "master",
        "-N", "-C", "-b",
        "-Q", f'"{batch}"',
    ]
    if image:
        # --entrypoint is required, not stylistic: the only MCR image that carries
        # sqlcmd 18 at SQLCMD_PATH is mcr.microsoft.com/mssql/server, whose
        # ENTRYPOINT is launch_sqlservr.sh — a bare `docker run IMAGE sqlcmd …` hands
        # the whole command line to the SQL Server launcher and sqlcmd never runs.
        return " ".join(["docker", "run", "--rm",
                         "-e", f"SQLCMDPASSWORD='{admin_password}'",
                         "--entrypoint", SQLCMD_PATH, image] + args)
    # The password rides an env assignment scoped to this one command rather than the
    # shell's environment — same exposure as the `docker run -e` form it replaces.
    return " ".join([f"SQLCMDPASSWORD='{admin_password}'", SQLCMD_PATH] + args)


_ONBOARD_SQL = {"postgres": _pg_onboard_sql, "mysql": _mysql_onboard_sql, "sqlserver": _mssql_onboard_sql}
_TEARDOWN_SQL = {"postgres": _pg_teardown_sql, "mysql": _mysql_teardown_sql, "sqlserver": _mssql_teardown_sql}
_COMMAND = {"postgres": _pg_command, "mysql": _mysql_command, "sqlserver": _mssql_command}


def _check_engine(engine: str) -> None:
    if engine not in VALID_ENGINES:
        raise CloudDbSqlError(f"unsupported engine {engine!r} (supported: {', '.join(VALID_ENGINES)})")


def onboard_commands(engine: str, *, host: str, port: int, database: str,
                     admin_user: str, admin_password: str,
                     managed_user: str, managed_password: str,
                     client_image: str = "") -> list:
    """Shell command(s) that create the dedicated managed DB user (the rotation
    target). Returned as an SSM ``commands`` list (one client-invocation line). Raises
    on unsafe identifiers/values so nothing unvalidated reaches the shell."""
    _check_engine(engine)
    managed = _ident(managed_user)
    mpw = _value(managed_password)
    _value(admin_password)
    image = resolve_client_image(engine, client_image)
    statements = _ONBOARD_SQL[engine](managed, mpw)
    return [_COMMAND[engine](host=host, port=port, database=database,
                             admin_user=_ident(admin_user), admin_password=admin_password,
                             image=image, statements=statements)]


def teardown_commands(engine: str, *, host: str, port: int, database: str,
                      admin_user: str, admin_password: str,
                      managed_user: str, client_image: str = "") -> list:
    """Shell command(s) that drop the managed DB user (best-effort teardown)."""
    _check_engine(engine)
    managed = _ident(managed_user)
    _value(admin_password)
    image = resolve_client_image(engine, client_image)
    statements = _TEARDOWN_SQL[engine](managed)
    return [_COMMAND[engine](host=host, port=port, database=database,
                             admin_user=_ident(admin_user), admin_password=admin_password,
                             image=image, statements=statements)]


# ── Ephemeral just-in-time accounts (Cloud Functions db_grant workload) ───────
#
# Everything above serves the Password Safe managed-user path, which creates ONE
# long-lived login per database and ships it over SSM. What follows is a different
# lifecycle for a different caller: a short-lived account minted per access grant
# and dropped on revoke, executed by the db_grant function over a direct database
# connection (see web_dashboard/functions/fnworkloads/db_grant.py).
#
# Why this exists at all: Entitle's native connectors cannot deliver it. Its MySQL
# connector assigns persistent roles only, and its SQL Server connector's ephemeral
# accounts do not work against the managed SQL flavors this dashboard provisions.
#
# Still pure string-building — no connection is opened here — so the whole matrix
# is unit-testable with no database and no driver.

# Access levels an ephemeral account can be granted. Deliberately tiny: these map
# onto an Entitle role/permission name, and every additional level is a privilege
# escalation path somebody has to reason about.
VALID_ROLES = ("read", "readwrite")

# SQL Server comes in three managed flavors and they are NOT interchangeable.
#   rds / cloudsql — a real SQL Server instance. A server-level LOGIN plus a
#                    database USER mapped to it, and USE works to switch database.
#   azure_sql      — Azure SQL Database is a CONTAINED database model. The login
#                    lives in `master` on the logical server, the user lives in the
#                    target database, and there is NO USE statement: each database
#                    needs its own connection. That is precisely why Entitle's
#                    stock MSSQL ephemeral accounts do not work here.
VALID_FLAVORS = ("rds", "azure_sql", "cloudsql")
_DEFAULT_FLAVOR = {"sqlserver": "rds", "mysql": "", "postgres": ""}

# Which flavors need the login created on a SEPARATE connection to `master`.
_SPLIT_LOGIN_FLAVORS = ("azure_sql",)

# SQL Server's fixed database roles do NOT nest: db_datawriter grants INSERT,
# UPDATE and DELETE and no SELECT at all, so a "readwrite" account that got only
# that role could write rows it could not read back. MySQL's readwrite is
# SELECT+INSERT+UPDATE+DELETE and db_grant advertises exactly that to Entitle, so
# both roles are needed here for the two engines to mean the same thing by the same
# role_code. A tuple per code, because a role that maps to one is the special case.
_MSSQL_ROLES = {
    "read": ("db_datareader",),
    "readwrite": ("db_datareader", "db_datawriter"),
}


def _check_role(role: str) -> str:
    if role not in VALID_ROLES:
        raise CloudDbSqlError(
            f"unsupported role {role!r} (supported: {', '.join(VALID_ROLES)})")
    return role


def _check_flavor(engine: str, flavor: str) -> str:
    """Normalise the flavor for ``engine``; only SQL Server actually branches."""
    if engine != "sqlserver":
        return ""
    flavor = flavor or _DEFAULT_FLAVOR["sqlserver"]
    if flavor not in VALID_FLAVORS:
        raise CloudDbSqlError(
            f"unsupported sqlserver flavor {flavor!r} (supported: {', '.join(VALID_FLAVORS)})")
    return flavor


# Entitle's Remote Adapter models an ephemeral account as FOUR operations, not two
# (docs.beyondtrust.com/entitle/docs/open-api-definition):
#
#   create_actor   mint the account, with no privileges
#   give_access    grant it a role on an asset
#   revoke_access  take the role away
#   delete_actor   drop the account
#
# Splitting them here rather than in the adapter is what makes the SQL Server case
# fall out cleanly: the login/user split and the role-membership change are already
# separate statements against different databases, so the four operations map onto
# statement groups that already existed. ``grant_plan``/``revoke_plan`` remain as
# the composed pair, for the standalone (non-Entitle) path.

def _mysql_create(*, user: str, password: str) -> list:
    # '%' host: the function reaches the server from a VPC/VNet address that is not
    # predictable per invocation, so pinning the host would break on the next cold
    # start in a different subnet.
    return [f"CREATE USER '{user}'@'%' IDENTIFIED BY '{password}';"]


def _mysql_delete(*, user: str) -> list:
    # Dropping the user removes its grants; no explicit REVOKE needed.
    return [f"DROP USER IF EXISTS '{user}'@'%';"]


def _mysql_privileges(role: str) -> str:
    return "SELECT" if role == "read" else "SELECT, INSERT, UPDATE, DELETE"


def _mysql_give(*, user: str, database: str, role: str) -> list:
    return [f"GRANT {_mysql_privileges(role)} ON `{database}`.* TO '{user}'@'%';"]


def _mysql_take(*, user: str, database: str, role: str) -> list:
    return [f"REVOKE {_mysql_privileges(role)} ON `{database}`.* FROM '{user}'@'%';"]


def _pg_create(*, user: str, password: str) -> list:
    return [f"CREATE ROLE \"{user}\" WITH LOGIN PASSWORD '{password}';"]


def _pg_delete(*, user: str) -> list:
    # Postgres refuses to drop a role that still owns or is granted anything, so
    # reassign/drop what it holds first. Without this the delete fails on any
    # account that was actually used, which is every account worth deleting.
    return [
        f'REASSIGN OWNED BY "{user}" TO CURRENT_USER;',
        f'DROP OWNED BY "{user}";',
        f'DROP ROLE IF EXISTS "{user}";',
    ]


def _pg_dml(role: str) -> str:
    return "SELECT" if role == "read" else "SELECT, INSERT, UPDATE, DELETE"


def _pg_give(*, user: str, database: str, role: str) -> list:
    return [
        f'GRANT CONNECT ON DATABASE "{database}" TO "{user}";',
        f'GRANT USAGE ON SCHEMA public TO "{user}";',
        f'GRANT {_pg_dml(role)} ON ALL TABLES IN SCHEMA public TO "{user}";',
    ]


def _pg_take(*, user: str, database: str, role: str) -> list:
    return [
        f'REVOKE {_pg_dml(role)} ON ALL TABLES IN SCHEMA public FROM "{user}";',
        f'REVOKE USAGE ON SCHEMA public FROM "{user}";',
        f'REVOKE CONNECT ON DATABASE "{database}" FROM "{user}";',
    ]


def _mssql_login(*, user: str, password: str) -> list:
    return [f"CREATE LOGIN [{user}] WITH PASSWORD = '{password}';"]


def _mssql_user(*, user: str) -> list:
    return [f"CREATE USER [{user}] FOR LOGIN [{user}];"]


def _mssql_add_role(*, user: str, role: str) -> list:
    return [f"ALTER ROLE {name} ADD MEMBER [{user}];" for name in _MSSQL_ROLES[role]]


def _mssql_drop_role(*, user: str, role: str) -> list:
    """Reversed, so a revoke undoes a grant in the opposite order it was made, and
    guarded, so it is idempotent.

    The guard is not only for Entitle's retries. An account granted `readwrite`
    before that code mapped to two roles holds only db_datawriter, and its revoke
    now names db_datareader as well — an unguarded DROP MEMBER for a role the
    account was never in would fail the whole revoke and leave the access it *does*
    hold in place. Same reason _mssql_drop_user and _mssql_drop_login are guarded.
    """
    return [f"IF IS_ROLEMEMBER('{name}', '{user}') = 1 "
            f"ALTER ROLE {name} DROP MEMBER [{user}];"
            for name in reversed(_MSSQL_ROLES[role])]


def _mssql_drop_login(*, user: str) -> list:
    return [
        f"IF EXISTS (SELECT 1 FROM sys.server_principals WHERE name = '{user}') "
        f"DROP LOGIN [{user}];"
    ]


def _mssql_drop_user(*, user: str) -> list:
    return [
        f"IF EXISTS (SELECT 1 FROM sys.database_principals WHERE name = '{user}') "
        f"DROP USER [{user}];"
    ]


def _merge(plan: list) -> list:
    """Coalesce adjacent entries for the same database into one connection.

    Composition produces runs like ``[(master, …), (appdb, …), (appdb, …)]``; each
    entry costs a real connection, and on Azure SQL that is a full TLS handshake to
    a separate endpoint. Merging is purely an efficiency and clarity win — the
    statement ORDER within a database is never changed.
    """
    merged: list = []
    for database, statements in plan:
        if merged and merged[-1][0] == database:
            merged[-1] = (database, merged[-1][1] + list(statements))
        else:
            merged.append((database, list(statements)))
    return merged


def _database_list(databases, fallback: str) -> list:
    """Validated database names, in order, deduplicated, never empty.

    Order is preserved rather than sorted: on SQL Server these become a statement
    sequence on one connection, and a stable order keeps a dry-run plan diffable.
    """
    names = []
    for raw in (databases or [fallback]):
        name = _ident(str(raw or "").strip())
        if name not in names:
            names.append(name)
    if not names:
        raise CloudDbSqlError("no database names given")
    return names


def _prepare(engine: str, *, username: str, database: str, flavor: str,
             role: str = "", password: str = "") -> tuple:
    """Validate every input once and return the normalised pieces."""
    _check_engine(engine)
    user = _ident(username)
    # _IDENT_RE bounds the name at 63 for SAFETY (what is legal to interpolate);
    # this bounds it at what the engine will actually store. Refusing here turns a
    # too-long name into a 400 that names the problem, on the route that asked for
    # it, instead of the driver error that becomes an opaque 500 — the same reason
    # every other validation in this function is a CloudDbSqlError.
    limit = max_identifier_length(engine)
    if len(user) > limit:
        raise CloudDbSqlError(
            f"account name {user!r} is {len(user)} characters; {engine} accepts at "
            f"most {limit}")
    pwd = _value(password) if password else ""
    checked_role = _check_role(role) if role else ""
    checked_flavor = _check_flavor(engine, flavor)
    if not database:
        raise CloudDbSqlError(f"{engine} plans need a database name")
    return user, _ident(database), checked_role, checked_flavor, pwd


def create_actor_plan(engine: str, *, username: str, password: str,
                      database: str, flavor: str = "", databases=None) -> list:
    """``[(database, [statements…])]`` creating the account with NO privileges.

    Entitle's ``create_actor``. An account with no grants is useless and harmless,
    which is exactly the point: if the subsequent ``give_access`` never arrives,
    what is left behind can reach nothing.

    ``databases`` is for an adapter serving several databases on ONE server. Entitle's
    ``create_actor`` carries no asset — the actor is bound to the adapter, not to an
    asset — so the account has to be made before anything knows which database the
    grant is for. On MySQL that is already how it works (``CREATE USER`` is
    server-scoped and only the GRANT is per-database); on SQL Server the login is
    server-scoped but the USER is per-database, so a role-less user is created in
    each. Role-less is the same "no privileges" guarantee as the single-database
    case — ``give_access`` remains the only thing that grants anything.
    """
    # Explicit, because _prepare only validates a password it was actually given —
    # and a login created with an empty one is a far worse hole than a rejected
    # request. The other three operations legitimately pass no password.
    if not password:
        raise CloudDbSqlError("creating an account requires a password")
    user, db_name, _role, flavor, pwd = _prepare(
        engine, username=username, database=database, flavor=flavor, password=password)
    if engine == "mysql":
        # Server-scoped principal; db_name only selects the connection.
        return [(db_name, _mysql_create(user=user, password=pwd))]
    if engine == "postgres":
        return [(db_name, _pg_create(user=user, password=pwd))]
    names = _database_list(databases, db_name)
    login, db_user = _mssql_login(user=user, password=pwd), _mssql_user(user=user)
    if flavor in _SPLIT_LOGIN_FLAVORS:
        # Azure SQL: a connection per database, and the order matters — no user can
        # be created until the login exists on the logical server.
        return [("master", login)] + [(name, db_user) for name in names]
    statements = list(login)
    for name in names:
        statements += [f"USE [{name}];"] + db_user
    return [("master", statements)]


def delete_actor_plan(engine: str, *, username: str, database: str,
                      flavor: str = "", databases=None) -> list:
    """``[(database, [statements…])]`` dropping the account.

    Entitle's ``delete_actor``. Idempotent wherever the engine allows it: Entitle
    retries, and a delete that errors for an account a failed create never finished
    making would leave the caller retrying forever while access looks un-revoked.

    ``databases`` mirrors :func:`create_actor_plan` — it must undo exactly what that
    made, or a multi-database adapter leaves ORPHANED USERS behind: a database
    principal whose login is gone, which a later login of the same name silently
    re-adopts along with whatever it was granted.
    """
    user, db_name, _role, flavor, _pwd = _prepare(
        engine, username=username, database=database, flavor=flavor)
    if engine == "mysql":
        # DROP USER is server-scoped and takes every grant with it.
        return [(db_name, _mysql_delete(user=user))]
    if engine == "postgres":
        return [(db_name, _pg_delete(user=user))]
    names = _database_list(databases, db_name)
    if flavor in _SPLIT_LOGIN_FLAVORS:
        # Drop the contained users first: on Azure SQL the login cannot be dropped
        # while a database principal is still mapped to it.
        return ([(name, _mssql_drop_user(user=user)) for name in names]
                + [("master", _mssql_drop_login(user=user))])
    statements = []
    for name in names:
        statements += [f"USE [{name}];"] + _mssql_drop_user(user=user)
    return [("master", statements + ["USE [master];"] + _mssql_drop_login(user=user))]


def give_access_plan(engine: str, *, username: str, database: str,
                     role: str = "read", flavor: str = "") -> list:
    """``[(database, [statements…])]`` granting ``role`` to an EXISTING account.

    Entitle's ``give_access``. Assumes ``create_actor`` already ran — which is the
    order Entitle calls them in for an ephemeral integration.
    """
    user, db_name, role, flavor, _pwd = _prepare(
        engine, username=username, database=database, flavor=flavor, role=role)
    if engine == "mysql":
        return [(db_name, _mysql_give(user=user, database=db_name, role=role))]
    if engine == "postgres":
        return [(db_name, _pg_give(user=user, database=db_name, role=role))]
    add = _mssql_add_role(user=user, role=role)
    if flavor in _SPLIT_LOGIN_FLAVORS:
        return [(db_name, add)]
    return [("master", [f"USE [{db_name}];"] + add)]


def revoke_access_plan(engine: str, *, username: str, database: str,
                       role: str = "read", flavor: str = "") -> list:
    """``[(database, [statements…])]`` removing ``role`` but LEAVING the account.

    Entitle's ``revoke_access``. The account itself goes away on ``delete_actor``;
    keeping the two separate is what lets Entitle revoke one role on one asset
    without disturbing any other access the same actor holds.
    """
    user, db_name, role, flavor, _pwd = _prepare(
        engine, username=username, database=database, flavor=flavor, role=role)
    if engine == "mysql":
        return [(db_name, _mysql_take(user=user, database=db_name, role=role))]
    if engine == "postgres":
        return [(db_name, _pg_take(user=user, database=db_name, role=role))]
    drop = _mssql_drop_role(user=user, role=role)
    if flavor in _SPLIT_LOGIN_FLAVORS:
        return [(db_name, drop)]
    return [("master", [f"USE [{db_name}];"] + drop)]


def grant_plan(engine: str, *, username: str, password: str, database: str,
               role: str = "read", flavor: str = "") -> list:
    """create_actor + give_access, as one plan.

    The standalone (non-Entitle) path: one call that mints a usable account. Entitle
    drives the two halves separately, so this is a composition of them rather than a
    third implementation — there is only ever one copy of each statement.
    """
    return _merge(
        create_actor_plan(engine, username=username, password=password,
                          database=database, flavor=flavor)
        + give_access_plan(engine, username=username, database=database,
                           role=role, flavor=flavor))


def revoke_plan(engine: str, *, username: str, database: str,
                flavor: str = "") -> list:
    """Drop the account outright, which takes its grants with it.

    The standalone counterpart of :func:`grant_plan`. Entitle's revoke_access is a
    narrower operation — see :func:`revoke_access_plan`.
    """
    return _merge(delete_actor_plan(engine, username=username,
                                    database=database, flavor=flavor))


def ephemeral_username(prefix: str, token: str, engine: str = "") -> str:
    """A safe, collision-resistant account name for one grant, within ``engine``'s
    identifier limit.

    Derived from the requester rather than random so an operator looking at
    ``sys.database_principals`` (or ``mysql.user``) can tell WHO an account belongs
    to without a lookup — the first question asked of any unexpected account.

    ``engine`` is what makes the name USABLE, and omitting it is not free: the
    budget is spent on the collision-resistant suffix FIRST and the readable slug
    second, because two grants sharing a name is a correctness bug while a clipped
    slug is only a legibility one. With no engine the smallest limit is assumed —
    the result is short enough for every engine here, which is the only assumption
    that cannot mint a name the target will refuse.
    """
    limit = max_identifier_length(engine)
    slug = re.sub(r"[^A-Za-z0-9]", "_", (prefix or "").strip().lower()).strip("_")
    slug = re.sub(r"_{2,}", "_", slug)
    suffix = re.sub(r"[^A-Za-z0-9]", "", token or "")[:12].lower() or "x"
    # 24 stays the PREFERRED slug length, so the names this already generates on
    # Postgres and SQL Server do not change; the engine budget only ever shortens it.
    # len("jit_") + len("_") = 5 of that budget is structural.
    slug = slug[:max(min(24, limit - len(suffix) - 5), 1)].strip("_") or "jit"
    name = f"jit_{slug}_{suffix}"
    if not name[0].isalpha():
        name = "j" + name
    return name[:limit]
