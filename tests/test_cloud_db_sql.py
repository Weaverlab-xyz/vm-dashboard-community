"""Unit tests for cloud_db_sql_service — the per-engine managed-user SQL and the
docker-run/SSM command builder for the optional Password Safe cloud-DB onboarding.

Covers:
- password generation is complexity-satisfying and shell/SQL-safe;
- onboard_commands creates ONLY the dedicated managed user (no functional login,
  no privilege grants) with the right client, TLS mode, and admin auth env per engine;
- teardown_commands drops the managed user;
- identifier/value validation rejects anything unsafe to interpolate.

Imports the service with a stubbed web_dashboard.config (no app deps).
Runs under pytest or standalone:  python tests/test_cloud_db_sql.py
"""
import os
import re
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_cfg_stub = types.ModuleType("web_dashboard.config")
_cfg_stub.settings = object()
sys.modules.setdefault("web_dashboard.config", _cfg_stub)

from web_dashboard.services import cloud_db_sql_service as sql  # noqa: E402

_COMMON = dict(host="db.abc.us-east-1.rds.amazonaws.com", port=5432, database="appdb",
               admin_user="dbadmin", admin_password="Admin-Pw_123",
               managed_user="psafe_ab12cd34", managed_password="Managed-Pw_9")


def test_generate_password_is_complex_and_safe():
    for _ in range(50):
        pw = sql.generate_password()
        assert len(pw) >= 8
        assert sql._SAFE_VALUE_RE.match(pw), "password must be shell/SQL safe"
        cats = sum([bool(re.search(r"[a-z]", pw)), bool(re.search(r"[A-Z]", pw)),
                    bool(re.search(r"[0-9]", pw)), bool(re.search(r"[#\-_]", pw))])
        assert cats >= 3, "must satisfy SQL Server complexity (≥3 categories)"


def test_defaults():
    assert sql.default_port("postgres") == 5432
    assert sql.default_port("mysql") == 3306
    assert sql.default_port("sqlserver") == 1433
    assert sql.default_client_image("postgres").startswith("postgres")
    assert "mssql-tools" in sql.default_client_image("sqlserver")


def test_postgres_onboard_creates_only_managed_role():
    cmds = sql.onboard_commands("postgres", **_COMMON)
    assert len(cmds) == 1
    c = cmds[0]
    assert 'CREATE ROLE "psafe_ab12cd34" WITH LOGIN PASSWORD \'\'Managed-Pw_9\'\';' in c
    assert "PGPASSWORD='Admin-Pw_123'" in c
    assert "sslmode=require" in c and " psql " in c
    # No functional login / grants — the managed account self-rotates.
    assert "CREATEROLE" not in c and "GRANT" not in c
    assert c.count("CREATE ROLE") == 1


def test_onboard_is_create_or_reset_so_the_retry_action_can_run():
    """Live regression (2026-08-27): the Password Safe half of an AWS provision failed
    AFTER the managed user existed, and the documented remedy — the Databases row's
    "Register in Password Safe" — then failed forever with `role "psafe_…" already
    exists`, because onboarding was a bare CREATE. Every engine must converge an
    existing user onto this run's password instead."""
    pg = sql.onboard_commands("postgres", **_COMMON)[0]
    # No CREATE ROLE IF NOT EXISTS in Postgres, so the branch is a DO block — and its
    # body must NOT be $$-quoted: the statement is interpolated into a double-quoted
    # shell word, where $$ becomes the shell's PID.
    assert "IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = ''psafe_ab12cd34'')" in pg
    assert 'ALTER ROLE "psafe_ab12cd34" WITH LOGIN PASSWORD \'\'Managed-Pw_9\'\';' in pg
    assert "$$" not in pg
    # Balanced shell double quotes: an odd count means the argument is unterminated.
    assert pg.count('"') % 2 == 0

    my = sql.onboard_commands("mysql", **{**_COMMON, "port": 3306})[0]
    assert "CREATE USER IF NOT EXISTS 'psafe_ab12cd34'@'%'" in my
    assert "ALTER USER 'psafe_ab12cd34'@'%' IDENTIFIED BY 'Managed-Pw_9';" in my

    ms = sql.onboard_commands("sqlserver", **{**_COMMON, "port": 1433})[0]
    assert ("IF NOT EXISTS (SELECT 1 FROM sys.server_principals WHERE "
            "name = 'psafe_ab12cd34') CREATE LOGIN [psafe_ab12cd34]") in ms
    # ALTER LOGIN alone in its own batch — the form valid whatever options it carries.
    assert "\nGO\nALTER LOGIN [psafe_ab12cd34] WITH PASSWORD = 'Managed-Pw_9';\nGO\n" in ms


def test_mysql_onboard_uses_tls_and_caching_sha2_default():
    cmds = sql.onboard_commands("mysql", **{**_COMMON, "port": 3306})
    c = cmds[0]
    assert "CREATE USER IF NOT EXISTS 'psafe_ab12cd34'@'%' IDENTIFIED BY 'Managed-Pw_9';" in c
    assert "MYSQL_PWD='Admin-Pw_123'" in c
    assert "--ssl-mode=REQUIRED" in c
    # 8.4 default auth (caching_sha2) — must NOT force the tunnel-incompatible plugin.
    assert "mysql_native_password" not in c
    assert "GRANT" not in c


def test_sqlserver_onboard_targets_master_with_tunnel_flags():
    cmds = sql.onboard_commands("sqlserver", **{**_COMMON, "port": 1433})
    c = cmds[0]
    assert "CREATE LOGIN [psafe_ab12cd34] WITH PASSWORD = 'Managed-Pw_9';" in c
    assert "SQLCMDPASSWORD='Admin-Pw_123'" in c
    assert "sqlcmd" in c and "-d master" in c
    assert "-N t -C" in c          # encryption mandatory + trust cert (no CA in the image)
    assert "ALTER ANY LOGIN" not in c


def test_client_connections_encrypt_but_do_not_verify():
    """Pins the TLS posture of the jumphost→DB client hop, and why it is what it is.

    Every DB module in terraform/ turns the server's *forced*-TLS knob off
    (require_secure_transport=OFF on the Azure flexible servers, rds.force_ssl=0 on
    RDS, ssl_mode=ALLOW_UNENCRYPTED_AND_ENCRYPTED on Cloud SQL) because the PRA
    protocol tunnel proxies the cleartext wire protocol and has no backend-TLS
    option. The original commit (7e89d6f) copied that server posture into THIS
    client and connected with sslmode=disable / --ssl-mode=DISABLED, sending the DB
    master password over the wire in the clear. Nothing required that: these builders
    are not the tunnel, and the same servers accept TLS today — the Password Safe
    plugin's own rotation connections to them run with the packed address's
    `sslTRUE` segment on all three clouds.

    ENCRYPT-BUT-DO-NOT-VERIFY is the deliberate choice, not an oversight. sslmode=
    require / --ssl-mode=REQUIRED / sqlcmd -N t -C all encrypt without checking the
    certificate. The verifying modes would need each cloud's root CA inside the
    client container, and the stock postgres:16 / mysql:8.4 / mssql-tools18 images
    do not carry the Azure, RDS or Cloud SQL roots — so a verify-full here would
    fail closed on every cloud. If a CA bundle is ever mounted onto the jump host,
    tighten these three flags together and update this test.

    A regression that flips any of them back to a disabling value must fail here.
    """
    pg = sql.onboard_commands("postgres", **_COMMON)[0]
    assert "sslmode=require" in pg
    assert "sslmode=disable" not in pg and "sslmode=prefer" not in pg
    # Not verify-*: no cloud root CA ships in postgres:16.
    assert "sslmode=verify" not in pg and "sslrootcert" not in pg

    my = sql.onboard_commands("mysql", **{**_COMMON, "port": 3306})[0]
    assert "--ssl-mode=REQUIRED" in my
    assert "DISABLED" not in my and "PREFERRED" not in my
    assert "VERIFY_CA" not in my and "VERIFY_IDENTITY" not in my and "--ssl-ca" not in my

    ms = sql.onboard_commands("sqlserver", **{**_COMMON, "port": 1433})[0]
    # -N t = encryption mandatory (the mssql-tools18 default); -C = trust the cert.
    assert "-N t -C" in ms
    assert "-N o" not in ms


# Statement-flag per engine: the flag that introduces SQL, as opposed to the
# connection flags. Needed because `--ssl-mode=REQUIRED` legitimately contains
# "REQUIRE" and would otherwise trip the sweep below.
_STMT_FLAG = {"postgres": "-c", "mysql": "-e", "sqlserver": "-Q"}

# Clauses that pin TLS onto the DB PRINCIPAL rather than onto one connection.
_TLS_PINNING_CLAUSES = ("REQUIRE SSL", "REQUIRE X509", "REQUIRE SUBJECT",
                        "REQUIRE ISSUER", "REQUIRE CIPHER", "HOSTSSL", "CLIENTCERT")


def _statements(engine: str, **over) -> str:
    """The SQL half of a built onboard command, with the connection flags dropped."""
    kw = {**_COMMON, "port": sql.default_port(engine), **over}
    cmd = sql.onboard_commands(engine, **kw)[0]
    parts = cmd.split(_STMT_FLAG[engine] + ' "')
    assert len(parts) > 1, f"no {_STMT_FLAG[engine]} statement in: {cmd}"
    return " ".join(parts[1:]).upper()


def test_onboard_never_pins_tls_onto_the_managed_account_itself():
    """The PRA protocol tunnel logs in AS the managed account, in CLEARTEXT.

    This is the one TLS mistake in this module that would actually break tunneling,
    and it is deliberately NOT the same thing as the connection flags pinned by
    test_client_connections_encrypt_but_do_not_verify. Those flags govern one
    throwaway `docker run psql/mysql/sqlcmd` that creates the user and exits. A
    REQUIRE clause governs the ACCOUNT, permanently, for every later login —
    including the tunnel's.

    `CREATE USER ... REQUIRE SSL` (or X509/SUBJECT/ISSUER/CIPHER) would leave the
    managed account un-tunnelable no matter what the server permits: the
    sra_my_sql_tunnel_jump proxies the cleartext MySQL wire protocol (that is how it
    injects the Vault credential and records the session) and has no backend-TLS
    option, which is the whole reason every DB module in terraform/ sets
    require_secure_transport=OFF / rds.force_ssl=0 / ssl_mode=
    ALLOW_UNENCRYPTED_AND_ENCRYPTED. Turning the server knob back on is a visible
    terraform change; slipping a REQUIRE into the CREATE is a one-word edit here that
    looks like hardening and reads, in production, as a broken tunnel.

    MySQL is the real hazard — it is the only engine of the three with a per-principal
    TLS clause. Postgres has no per-role equivalent (it is pg_hba `hostssl`, which is
    server-side and not ours to write) and SQL Server has no per-login encryption
    requirement at all, so their sweeps are trivially satisfied today; they are here so
    a future hardening pass that reaches for one trips this test rather than the tunnel.
    """
    for engine in sql.VALID_ENGINES:
        stmts = _statements(engine)
        for clause in _TLS_PINNING_CLAUSES:
            assert clause not in stmts, f"{engine}: {clause!r} would break the PRA tunnel"

    # MySQL: no REQUIRE at all. A bare `REQUIRE NONE` would be harmless but is still
    # the tunnel's business to assume, not this module's to state.
    assert "REQUIRE" not in _statements("mysql")

    # Guard the guard: the sweep must be reading SQL, not the whole command line.
    # If _statements ever returned the connection flags, --ssl-mode=REQUIRED would
    # make the assertion above vacuously fragile rather than meaningful.
    assert "SSL-MODE" not in _statements("mysql")
    assert "SSLMODE" not in _statements("postgres")


def test_client_image_override():
    c = sql.onboard_commands("postgres", **{**_COMMON, "client_image": "myregistry/pg:16"})[0]
    assert "myregistry/pg:16" in c


def test_teardown_drops_managed_user():
    assert 'DROP ROLE IF EXISTS "psafe_ab12cd34";' in sql.teardown_commands(
        "postgres", host="h", port=5432, database="appdb", admin_user="dbadmin",
        admin_password="Admin-Pw_123", managed_user="psafe_ab12cd34")[0]
    assert "DROP USER IF EXISTS 'psafe_ab12cd34'@'%';" in sql.teardown_commands(
        "mysql", host="h", port=3306, database="", admin_user="dbadmin",
        admin_password="Admin-Pw_123", managed_user="psafe_ab12cd34")[0]
    assert "DROP LOGIN [psafe_ab12cd34]" in sql.teardown_commands(
        "sqlserver", host="h", port=1433, database="", admin_user="dbadmin",
        admin_password="Admin-Pw_123", managed_user="psafe_ab12cd34")[0]


def test_rejects_unsafe_identifier_and_value():
    for bad_user in ("bad-user", "1abc", "drop;table", ""):
        try:
            sql.onboard_commands("postgres", **{**_COMMON, "managed_user": bad_user})
            raise AssertionError("expected CloudDbSqlError for user=%r" % bad_user)
        except sql.CloudDbSqlError:
            pass
    # A value with a shell/SQL metacharacter (e.g. a quote) must be rejected.
    try:
        sql.onboard_commands("postgres", **{**_COMMON, "managed_password": "pw'; DROP"})
        raise AssertionError("expected CloudDbSqlError for unsafe password")
    except sql.CloudDbSqlError:
        pass


def test_rejects_unsupported_engine():
    try:
        sql.onboard_commands("oracle", **_COMMON)
        raise AssertionError("expected CloudDbSqlError for unsupported engine")
    except sql.CloudDbSqlError:
        pass


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
    sys.exit(1 if failures else 0)
