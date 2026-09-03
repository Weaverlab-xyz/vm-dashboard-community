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
    assert sql.default_client_image("mysql").startswith("mysql")
    # SQL Server has NO default image, on purpose — see
    # test_sqlserver_runs_the_native_sqlcmd_because_no_such_image_exists.
    assert sql.default_client_image("sqlserver") == ""


def test_postgres_and_mysql_still_run_the_container_client_verbatim():
    """The two engines that are PROVEN live stay byte-for-byte what they were.

    PostgreSQL on Azure rotates end-to-end today (a Forced Reset on the "PostgreSQL
    Azure Run Command Plugin", 2026-09-02, functional account minted at build time),
    and postgres:16 / mysql:8.4 are real images — so the SQL Server client fix, which
    moved that engine off `docker run` entirely and touched the shared image resolver,
    must not have shifted a character here. Exact strings, not substrings, because the
    failure mode being guarded is a well-meaning refactor of the sibling builder.
    """
    pg = sql.onboard_commands("postgres", **_COMMON)[0]
    assert pg == (
        "docker run --rm -e PGPASSWORD='Admin-Pw_123' postgres:16 psql "
        '"host=db.abc.us-east-1.rds.amazonaws.com port=5432 dbname=appdb '
        'user=dbadmin sslmode=require" -v ON_ERROR_STOP=1 '
        '-c "DO \'BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '
        "''psafe_ab12cd34'') THEN ALTER ROLE \"psafe_ab12cd34\" WITH LOGIN PASSWORD "
        "''Managed-Pw_9''; ELSE CREATE ROLE \"psafe_ab12cd34\" WITH LOGIN PASSWORD "
        "''Managed-Pw_9''; END IF; END';\"")

    my = sql.onboard_commands("mysql", **{**_COMMON, "port": 3306})[0]
    assert my == (
        "docker run --rm -e MYSQL_PWD='Admin-Pw_123' mysql:8.4 mysql "
        "--host=db.abc.us-east-1.rds.amazonaws.com --port=3306 --user=dbadmin "
        "--ssl-mode=REQUIRED --batch "
        "-e \"CREATE USER IF NOT EXISTS 'psafe_ab12cd34'@'%' IDENTIFIED BY "
        "'Managed-Pw_9'; ALTER USER 'psafe_ab12cd34'@'%' IDENTIFIED BY "
        "'Managed-Pw_9';\"")

    # And the resolver hands these two a real image no matter what it is given —
    # they have no native mode to fall back to, so "" would break them outright.
    for engine in ("postgres", "mysql"):
        for configured in ("", "   ", "mcr.microsoft.com/mssql-tools18"):
            assert sql.resolve_client_image(engine, configured) != ""


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
    assert ("IF NOT EXISTS (SELECT 1 FROM sys.sql_logins WHERE "
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
    assert " -Nm -C " in c         # encrypt + trust cert (no CA on the host)
    assert "ALTER ANY LOGIN" not in c


def test_sqlserver_runs_the_native_sqlcmd_because_no_such_image_exists():
    """The SQL Server client is NOT a container, and this is why.

    `mcr.microsoft.com/mssql-tools18` was this builder's default image from the
    original commit until 2026-09-02, and no such repository has ever existed in MCR:
    `mssql-tools18` is the apt/dnf PACKAGE name (which is how both clouds' jump-host
    prep installs it), MCR publishes `mssql-tools` — sqlcmd 17, a different binary
    path, and `-N` there takes no argument — and nothing else. So every SQL Server
    registration failed identically on both clouds, with docker exiting 125
    ("Unable to find image ... locally / not found") before a byte reached the
    database. Observed live on Azure 2026-09-02.

    A regression that hands sqlserver a default image must fail here, not at a
    customer's registration.
    """
    c = sql.onboard_commands("sqlserver", **{**_COMMON, "port": 1433})[0]
    assert "docker" not in c
    assert c.startswith("SQLCMDPASSWORD='Admin-Pw_123' /opt/mssql-tools18/bin/sqlcmd ")
    # Same binary, same path, as the jump-host prep installs and the rotation plugins
    # invoke — the whole point of going native rather than mirroring an image.
    assert sql.SQLCMD_PATH == "/opt/mssql-tools18/bin/sqlcmd"
    assert "mssql-tools18:" not in c and "mcr.microsoft.com" not in c

    # Teardown too: a drop that cannot run leaves the login behind forever.
    t = sql.teardown_commands("sqlserver", host="h", port=1433, database="",
                              admin_user="dbadmin", admin_password="Admin-Pw_123",
                              managed_user="psafe_ab12cd34")[0]
    assert "docker" not in t and sql.SQLCMD_PATH in t


def test_sqlserver_passes_the_encryption_switch_in_the_only_form_sqlcmd_parses():
    """The encryption value is ATTACHED — `-Nm`. A space is a crash, and so is bare `-N`.

    sqlcmd's switch is `-N[s|m|o]` (strict / mandatory / optional): one token, no
    space. /opt/mssql-tools18/bin/sqlcmd rejects anything else before it ever opens a
    socket —

        Sqlcmd: Command -N: Invalid Parameters passed.

    rc=1, at 25% "Creating the rotatable managed database user…", observed live on
    Azure TWICE: with `-N t` on 2026-09-02 (`t` for "true" is the separate *Go*
    sqlcmd's vocabulary, so that one was wrong twice over), and again on 2026-09-03
    after a bare `-N` replaced it. The message names the SWITCH, never the value, so
    the second failure cannot say which candidate it was — an un-rebuilt image still
    sending `-N t`, or a bare -N swallowing the `-C` that follows it as its parameter.
    An attached value is correct under either reading.

    `-Nm` is mandatory encryption, attached, and `o|m|s` have been the accepted values
    on Linux since sqlcmd 18.0 — the only major mssql-tools18 ships. Omitting -N
    altogether would also encrypt on a current build, but defaults to `-No`
    (optional — cleartext if the server declines) on older ones, so the explicit form
    is what is pinned. Any `-N` followed by a space must fail here.
    """
    cmds = [sql.onboard_commands("sqlserver", **{**_COMMON, "port": 1433})[0],
            sql.teardown_commands("sqlserver", host="h", port=1433, database="",
                                  admin_user="dbadmin", admin_password="Admin-Pw_123",
                                  managed_user="psafe_ab12cd34")[0],
            sql.onboard_commands("sqlserver", **{**_COMMON, "port": 1433,
                                                 "client_image": "myreg/mssql:2022"})[0]]
    for c in cmds:
        assert " -Nm -C -b " in c
        # Bare `-N` is the 2026-09-03 regression: it parses as -N consuming "-C".
        assert " -N " not in c, "bare -N consumes the following -C and is rejected"
        for bad in (" -N t", " -N o", " -N m", " -N s", " -N true", " -N mandatory"):
            assert bad not in c, f"space-separated {bad.strip()!r} is a sqlcmd parse error"


def test_a_saved_phantom_image_is_dropped_rather_than_run():
    """Changing the field default does not fix a LIVE instance on its own.

    The settings panel writes an `app_config` row when the panel is saved, and a row
    written by an older instance outlives the default it was seeded from — so an
    instance that ever saved the Password Safe panel still holds
    `mcr.microsoft.com/mssql-tools18` and would keep handing it to `docker run`, with
    the fix inert exactly where it is needed. Read-side, any tag or digest of that
    phantom repository resolves to "" (native).
    """
    for configured in ("mcr.microsoft.com/mssql-tools18",
                       "mcr.microsoft.com/mssql-tools18:latest",
                       "mcr.microsoft.com/mssql-tools18@sha256:abc",
                       "  mcr.microsoft.com/mssql-tools18  ", ""):
        assert sql.resolve_client_image("sqlserver", configured) == ""
    c = sql.onboard_commands("sqlserver", **{
        **_COMMON, "port": 1433, "client_image": "mcr.microsoft.com/mssql-tools18"})[0]
    assert "docker" not in c and c.startswith("SQLCMDPASSWORD=")

    # A real mirror is NOT collateral damage, and the other engines keep their images.
    assert sql.resolve_client_image("sqlserver", "myreg/mssql-tools18:18") == \
        "myreg/mssql-tools18:18"
    assert sql.resolve_client_image("postgres") == "postgres:16"
    assert sql.resolve_client_image("mysql", "myreg/mysql:8.4") == "myreg/mysql:8.4"


def test_a_configured_sqlserver_image_goes_back_through_docker_with_an_entrypoint():
    """The mirrored-registry escape hatch, and the flag that makes it work.

    --entrypoint is mandatory: the only MCR image carrying sqlcmd 18 at SQLCMD_PATH is
    mcr.microsoft.com/mssql/server, whose ENTRYPOINT is launch_sqlservr.sh, so the
    pre-2026-09-02 form (`docker run IMAGE /opt/.../sqlcmd -S …`) would hand the whole
    command line to the SQL Server launcher and never run sqlcmd at all.
    """
    c = sql.onboard_commands("sqlserver", **{**_COMMON, "port": 1433,
                                             "client_image": "myreg/mssql:2022"})[0]
    assert c.startswith("docker run --rm -e SQLCMDPASSWORD='Admin-Pw_123' "
                        "--entrypoint /opt/mssql-tools18/bin/sqlcmd myreg/mssql:2022 -S ")
    # The binary is the entrypoint, so it must NOT also appear as the first argument.
    assert c.count(sql.SQLCMD_PATH) == 1
    # Identical SQL and connection flags on either path.
    assert " -Nm -C " in c and "-d master" in c
    assert "CREATE LOGIN [psafe_ab12cd34] WITH PASSWORD = 'Managed-Pw_9';" in c


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
    require / --ssl-mode=REQUIRED / sqlcmd -N -C all encrypt without checking the
    certificate. The verifying modes would need each cloud's root CA reachable by the
    client, and neither the stock postgres:16 / mysql:8.4 images nor the jump host
    running sqlcmd carry the Azure, RDS or Cloud SQL roots — so a verify-full here
    would fail closed on every cloud. If a CA bundle is ever staged on the jump host,
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
    # -Nm = encryption mandatory; -C = trust the cert without a CA. The value must be
    # ATTACHED, and `-No` (optional) would be the downgrade this test guards against:
    # see test_sqlserver_passes_the_encryption_switch_in_the_only_form_sqlcmd_parses.
    assert " -Nm -C " in ms
    assert "-No" not in ms and "-N o" not in ms


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
    throwaway psql/mysql/sqlcmd invocation that creates the user and exits. A
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


def test_sqlserver_login_guards_read_sys_sql_logins_not_server_principals():
    """Live regression (2026-09-03, centralus): Azure SQL Database does NOT persist SQL
    logins in sys.server_principals, so a guard reading that view matched nothing in the
    virtual master and clouddb_ps_register hit `Msg 15025 … The server principal
    'psafe_…' already exists.` at 25%. The same view made the teardown/delete_actor drop
    a silent no-op. sys.sql_logins is documented for Azure SQL Database's master and for
    SQL Server 2008+, so it is correct on all three clouds."""
    onboard = sql.onboard_commands("sqlserver", **{**_COMMON, "port": 1433})[0]
    teardown = sql.teardown_commands(
        "sqlserver", host="h", port=1433, database="", admin_user="dbadmin",
        admin_password="Admin-Pw_123", managed_user="psafe_ab12cd34")[0]
    ephemeral = " ".join(
        stmt
        for _db, statements in sql.delete_actor_plan(
            "sqlserver", username="jit_a_1", database="appdb", flavor="azure_sql")
        for stmt in statements)
    for sql_text in (onboard, teardown, ephemeral):
        assert "sys.server_principals" not in sql_text, sql_text
    assert "sys.sql_logins" in onboard and "sys.sql_logins" in teardown
    # The ephemeral DROP LOGIN is guarded on the login catalog; the contained USER it
    # drops first is a database principal and legitimately reads sys.database_principals.
    assert "sys.sql_logins" in ephemeral and "sys.database_principals" in ephemeral


# sqlcmd batch separator: GO alone on its own line.
BATCH_SEP = "\nGO\n"


def test_azure_sql_onboard_creates_the_master_user_the_login_needs_to_connect():
    """Azure SQL disables master's `guest`, so a bare login is refused at CONNECT with
    `Cannot open database "master" requested by the login` — Password Safe's rotation
    never reaches ALTER LOGIN. The contained user is what makes the account usable, and
    master is where it has to be: it is the database every SQL Server admin session
    opens, and the only one Azure SQL allows ALTER LOGIN in."""
    azure = sql.onboard_commands(
        "sqlserver", **{**_COMMON, "port": 1433, "flavor": "azure_sql"})[0]
    assert ("IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE "
            "name = 'psafe_ab12cd34') CREATE USER [psafe_ab12cd34] "
            "FOR LOGIN [psafe_ab12cd34];") in azure
    # After the login exists, not before, and in its own batch.
    assert azure.index("CREATE LOGIN") < azure.index("CREATE USER")
    assert azure.count(BATCH_SEP) >= 3, azure
    # No privileges ride along: the account exists to be rotated, not to read.
    assert "ALTER ROLE" not in azure and "GRANT" not in azure

    # A real instance keeps `guest` in master, so the login connects without one — and
    # the admin there may not be allowed to add users to master at all.
    for flavor in ("rds", "cloudsql"):
        other = sql.onboard_commands(
            "sqlserver", **{**_COMMON, "port": 1433, "flavor": flavor})[0]
        assert "CREATE USER" not in other, flavor
    default = sql.onboard_commands("sqlserver", **{**_COMMON, "port": 1433})[0]
    rds = sql.onboard_commands(
        "sqlserver", **{**_COMMON, "port": 1433, "flavor": "rds"})[0]
    assert default == rds


def test_azure_sql_teardown_drops_the_master_user_before_the_login():
    """Azure SQL refuses to drop a login a database principal still maps to, so the
    teardown has to undo exactly what onboarding made, in reverse."""
    cmd = sql.teardown_commands(
        "sqlserver", host="h", port=1433, database="", admin_user="dbadmin",
        admin_password="Admin-Pw_123", managed_user="psafe_ab12cd34",
        flavor="azure_sql")[0]
    assert "DROP USER [psafe_ab12cd34];" in cmd
    assert cmd.index("DROP USER") < cmd.index("DROP LOGIN")
    assert "sys.database_principals" in cmd and "sys.sql_logins" in cmd
    rds = sql.teardown_commands(
        "sqlserver", host="h", port=1433, database="", admin_user="dbadmin",
        admin_password="Admin-Pw_123", managed_user="psafe_ab12cd34", flavor="rds")[0]
    assert "DROP USER" not in rds


def test_onboard_rejects_a_flavor_sqlserver_does_not_have():
    for bad in ("azure", "managed_instance", "sql_database"):
        try:
            sql.onboard_commands("sqlserver", **{**_COMMON, "port": 1433, "flavor": bad})
        except sql.CloudDbSqlError as exc:
            assert "flavor" in str(exc), exc
        else:
            raise AssertionError(f"accepted bogus flavor {bad!r}")
    # Non-SQL-Server engines carry no flavor and must not start rejecting one.
    assert sql.onboard_commands("postgres", **{**_COMMON, "flavor": "azure_sql"})


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
