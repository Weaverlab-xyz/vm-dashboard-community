"""Unit tests for cloud_db_sql_service — the per-engine managed-user SQL and the
docker-run/SSM command builder for the optional Password Safe cloud-DB onboarding.

Covers:
- password generation is complexity-satisfying and shell/SQL-safe;
- onboard_commands creates ONLY the dedicated managed user (no functional login,
  no privilege grants) with the right client, TLS-disable flag, and admin auth env
  per engine;
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
    assert 'CREATE ROLE "psafe_ab12cd34" WITH LOGIN PASSWORD \'Managed-Pw_9\';' in c
    assert "PGPASSWORD='Admin-Pw_123'" in c
    assert "sslmode=disable" in c and " psql " in c
    # No functional login / grants — the managed account self-rotates.
    assert "CREATEROLE" not in c and "GRANT" not in c
    assert c.count("CREATE ROLE") == 1


def test_mysql_onboard_uses_ssl_disabled_and_caching_sha2_default():
    cmds = sql.onboard_commands("mysql", **{**_COMMON, "port": 3306})
    c = cmds[0]
    assert "CREATE USER 'psafe_ab12cd34'@'%' IDENTIFIED BY 'Managed-Pw_9';" in c
    assert "MYSQL_PWD='Admin-Pw_123'" in c
    assert "--ssl-mode=DISABLED" in c
    # 8.4 default auth (caching_sha2) — must NOT force the tunnel-incompatible plugin.
    assert "mysql_native_password" not in c
    assert "GRANT" not in c


def test_sqlserver_onboard_targets_master_with_tunnel_flags():
    cmds = sql.onboard_commands("sqlserver", **{**_COMMON, "port": 1433})
    c = cmds[0]
    assert "CREATE LOGIN [psafe_ab12cd34] WITH PASSWORD = 'Managed-Pw_9';" in c
    assert "SQLCMDPASSWORD='Admin-Pw_123'" in c
    assert "sqlcmd" in c and "-d master" in c
    assert "-N o -C" in c          # optional encryption + trust cert (tunnel does backend TLS)
    assert "ALTER ANY LOGIN" not in c


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
