"""
Cloud-database SQL layer for the optional Password Safe integration (AWS-only).

The dashboard-provisioned DB is private; the only dashboard component with
line-of-sight to it is the shared PRA Jumpoint EC2 host. This module builds the
per-engine SQL and wraps it in a ``docker run`` DB-client invocation that is run
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
_ENGINE = {
    "postgres":  {"image": "postgres:16",                     "port": 5432},
    "mysql":     {"image": "mysql:8.4",                       "port": 3306},
    "sqlserver": {"image": "mcr.microsoft.com/mssql-tools18", "port": 1433},
}

# DB identifiers we create — restrict hard so they are safe to interpolate into
# SQL (no quoting games) and into a shell command.
_IDENT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,62}$")
# Values we single-quote into shell/SQL. Generated passwords use only these; the
# admin password (secrets.token_urlsafe) is URL-safe base64 (also within this set).
_SAFE_VALUE_RE = re.compile(r"^[A-Za-z0-9#\-_.]+$")


class CloudDbSqlError(Exception):
    """Raised on invalid identifiers/values passed to the SQL builders."""


def default_port(engine: str) -> int:
    return _ENGINE[engine]["port"]


def default_client_image(engine: str) -> str:
    return _ENGINE[engine]["image"]


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

def _pg_onboard_sql(managed: str, managed_pw: str) -> list:
    return [f'CREATE ROLE "{managed}" WITH LOGIN PASSWORD \'{managed_pw}\';']


def _mysql_onboard_sql(managed: str, managed_pw: str) -> list:
    # 8.4 defaults new users to caching_sha2_password (which the PRA tunnel
    # requires — no mysql_native_password).
    return [f"CREATE USER '{managed}'@'%' IDENTIFIED BY '{managed_pw}';"]


def _mssql_onboard_sql(managed: str, managed_pw: str) -> list:
    return [f"CREATE LOGIN [{managed}] WITH PASSWORD = '{managed_pw}';"]


def _pg_teardown_sql(managed: str) -> list:
    return [f'DROP ROLE IF EXISTS "{managed}";']


def _mysql_teardown_sql(managed: str) -> list:
    return [f"DROP USER IF EXISTS '{managed}'@'%';"]


def _mssql_teardown_sql(managed: str) -> list:
    return [f"IF EXISTS (SELECT 1 FROM sys.server_principals WHERE name = '{managed}') DROP LOGIN [{managed}];"]


# ── docker-run command builders (run on the jump host via SSM) ────────────────

def _pg_command(*, host, port, database, admin_user, admin_password, image, statements) -> str:
    db = _ident(database) if database else "postgres"
    conn = f"host={host} port={int(port)} dbname={db} user={admin_user} sslmode=disable"
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
        "--ssl-mode=DISABLED", "--batch",
        "-e", f'"{batch}"',
    ]
    return " ".join(parts)


def _mssql_command(*, host, port, database, admin_user, admin_password, image, statements) -> str:
    # -b: exit non-zero on SQL error. -N o (optional encryption) -C (trust cert):
    # the mssql tunnel does its own backend TLS. Batches joined with GO.
    batch = "\nGO\n".join(statements) + "\nGO\n"
    parts = [
        "docker", "run", "--rm",
        "-e", f"SQLCMDPASSWORD='{admin_password}'",
        image, "/opt/mssql-tools18/bin/sqlcmd",
        "-S", f"{host},{int(port)}", "-U", admin_user, "-d", "master",
        "-N", "o", "-C", "-b",
        "-Q", f'"{batch}"',
    ]
    return " ".join(parts)


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
    target). Returned as an SSM ``commands`` list (one ``docker run`` line). Raises
    on unsafe identifiers/values so nothing unvalidated reaches the shell."""
    _check_engine(engine)
    managed = _ident(managed_user)
    mpw = _value(managed_password)
    _value(admin_password)
    image = client_image or default_client_image(engine)
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
    image = client_image or default_client_image(engine)
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

_MSSQL_ROLE = {"read": "db_datareader", "readwrite": "db_datawriter"}


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


def _mysql_grant(*, user: str, password: str, database: str, role: str) -> list:
    # '%' host: the function reaches the server from a VPC/VNet address that is not
    # predictable per invocation, so pinning the host would break on the next cold
    # start in a different subnet.
    privileges = "SELECT" if role == "read" else "SELECT, INSERT, UPDATE, DELETE"
    return [
        f"CREATE USER '{user}'@'%' IDENTIFIED BY '{password}';",
        f"GRANT {privileges} ON `{database}`.* TO '{user}'@'%';",
    ]


def _mysql_revoke(*, user: str, database: str) -> list:
    # Dropping the user removes its grants; no explicit REVOKE needed.
    return [f"DROP USER IF EXISTS '{user}'@'%';"]


def _pg_grant(*, user: str, password: str, database: str, role: str) -> list:
    statements = [
        f"CREATE ROLE \"{user}\" WITH LOGIN PASSWORD '{password}';",
        f'GRANT CONNECT ON DATABASE "{database}" TO "{user}";',
        f'GRANT USAGE ON SCHEMA public TO "{user}";',
    ]
    if role == "read":
        statements.append(f'GRANT SELECT ON ALL TABLES IN SCHEMA public TO "{user}";')
    else:
        statements.append(
            f'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO "{user}";')
    return statements


def _pg_revoke(*, user: str, database: str) -> list:
    # Postgres refuses to drop a role that still owns or is granted anything, so
    # reassign/drop what it holds first. Without this the revoke fails on any
    # account that was actually used, which is every account worth revoking.
    return [
        f'REASSIGN OWNED BY "{user}" TO CURRENT_USER;',
        f'DROP OWNED BY "{user}";',
        f'DROP ROLE IF EXISTS "{user}";',
    ]


def _mssql_login(*, user: str, password: str) -> list:
    return [f"CREATE LOGIN [{user}] WITH PASSWORD = '{password}';"]


def _mssql_user(*, user: str, role: str) -> list:
    return [
        f"CREATE USER [{user}] FOR LOGIN [{user}];",
        f"ALTER ROLE {_MSSQL_ROLE[role]} ADD MEMBER [{user}];",
    ]


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


def grant_plan(engine: str, *, username: str, password: str, database: str,
               role: str = "read", flavor: str = "") -> list:
    """``[(database_to_connect_to, [statements…]), …]`` creating an ephemeral account.

    A **plan**, not a flat statement list, because Azure SQL Database genuinely
    needs two connections — the login in ``master``, the user in the target
    database, with no ``USE`` to bridge them. Encoding that in the return value
    keeps the flavor difference visible in data the tests can assert on, instead of
    buried in a branch inside the executor.

    Raises on any unsafe identifier or value, so nothing unvalidated reaches SQL.
    """
    _check_engine(engine)
    user = _ident(username)
    pwd = _value(password)
    role = _check_role(role)
    flavor = _check_flavor(engine, flavor)
    db_name = _ident(database) if database else ""

    if engine == "mysql":
        if not db_name:
            raise CloudDbSqlError("mysql grants need a database name")
        return [(db_name, _mysql_grant(user=user, password=pwd,
                                       database=db_name, role=role))]

    if engine == "postgres":
        if not db_name:
            raise CloudDbSqlError("postgres grants need a database name")
        # One connection: the role is cluster-wide, and the object grants apply to
        # whichever database the connection is against.
        return [(db_name, _pg_grant(user=user, password=pwd,
                                    database=db_name, role=role))]

    # SQL Server.
    if not db_name:
        raise CloudDbSqlError("sqlserver grants need a database name")
    login = _mssql_login(user=user, password=pwd)
    db_user = _mssql_user(user=user, role=role)
    if flavor in _SPLIT_LOGIN_FLAVORS:
        # Azure SQL: two connections, and the order matters — the user cannot be
        # created until the login exists on the logical server.
        return [("master", login), (db_name, db_user)]
    # RDS / Cloud SQL: one connection to master, USE to switch.
    return [("master", login + [f"USE [{db_name}];"] + db_user)]


def revoke_plan(engine: str, *, username: str, database: str,
                flavor: str = "") -> list:
    """``[(database, [statements…]), …]`` removing an ephemeral account.

    Idempotent wherever the engine allows it: a revoke that arrives twice, or for
    an account a failed grant never finished creating, must still succeed — Entitle
    retries, and a revoke that errors leaves standing access behind, which is the
    one outcome this whole feature exists to prevent.
    """
    _check_engine(engine)
    user = _ident(username)
    flavor = _check_flavor(engine, flavor)
    db_name = _ident(database) if database else ""

    if engine == "mysql":
        return [(db_name or "", _mysql_revoke(user=user, database=db_name))]

    if engine == "postgres":
        if not db_name:
            raise CloudDbSqlError("postgres revokes need a database name")
        return [(db_name, _pg_revoke(user=user, database=db_name))]

    if not db_name:
        raise CloudDbSqlError("sqlserver revokes need a database name")
    if flavor in _SPLIT_LOGIN_FLAVORS:
        # Drop the contained user first: on Azure SQL the login cannot be dropped
        # while a database principal is still mapped to it.
        return [(db_name, _mssql_drop_user(user=user)),
                ("master", _mssql_drop_login(user=user))]
    return [("master", [f"USE [{db_name}];"] + _mssql_drop_user(user=user)
             + ["USE [master];"] + _mssql_drop_login(user=user))]


def ephemeral_username(prefix: str, token: str) -> str:
    """A safe, collision-resistant account name for one grant.

    Derived from the requester rather than random so an operator looking at
    ``sys.database_principals`` (or ``mysql.user``) can tell WHO an account belongs
    to without a lookup — the first question asked of any unexpected account.
    """
    slug = re.sub(r"[^A-Za-z0-9]", "_", (prefix or "").strip().lower()).strip("_")
    slug = re.sub(r"_{2,}", "_", slug)[:24] or "jit"
    suffix = re.sub(r"[^A-Za-z0-9]", "", token or "")[:12].lower() or "x"
    name = f"jit_{slug}_{suffix}"
    if not name[0].isalpha():
        name = "j" + name
    return name[:63]
