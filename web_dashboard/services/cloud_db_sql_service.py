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
