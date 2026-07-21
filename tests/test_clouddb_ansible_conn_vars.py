"""Unit test: cloud_database_service.ansible_connection_vars builds the connection
extra-vars an Ansible localhost play uses to reach a managed DB.

Pins the per-cloud admin-credential normalization it shares with _broker_tunnel /
_entitle_register_core:
  - user     — master_username | administrator_login, Cloud SQL SQL Server →
               'sqlserver', Oracle → 'ADMIN', else 'dbadmin'
  - password — from the encrypted config store (clouddb/{id}/admin) when the
               tf_variables copy is scrubbed
  - db_name  — 'master' for SQL Server, else the provisioned db_name
  - port     — row.port, else the engine default

Heavy deps are stubbed in sys.modules (mirrors test_clouddb_provision_job_meta.py);
no real DB or cloud account is needed. Runs under pytest, or standalone:
    python tests/test_clouddb_ansible_conn_vars.py
"""
import os
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

CONF = {}


class _Settings:
    def __getattr__(self, _key):
        return ""


class _CloudDatabase:
    # Class-level attrs so `CloudDatabase.id == x` in the filter() expression is a
    # plain comparison (the fake query ignores the predicate anyway).
    id = None

    def __init__(self, **kw):
        self.__dict__.update(kw)


def _install_stubs():
    confmod = types.ModuleType("web_dashboard.config")
    confmod.settings = _Settings()
    sys.modules["web_dashboard.config"] = confmod

    dbmod = types.ModuleType("web_dashboard.database")
    dbmod.CloudDatabase = _CloudDatabase
    dbmod.Job = type("Job", (), {})
    sys.modules["web_dashboard.database"] = dbmod

    cfg = types.ModuleType("web_dashboard.services.config_service")
    cfg.get = lambda key: CONF.get(key, "")
    cfg.set = lambda key, val: CONF.__setitem__(key, val)
    sys.modules["web_dashboard.services.config_service"] = cfg

    js = types.ModuleType("web_dashboard.services.job_service")
    js.create_job = lambda *a, **k: None
    sys.modules["web_dashboard.services.job_service"] = js

    for name in ("terraform", "terraform_provider_env"):
        sys.modules[f"web_dashboard.services.{name}"] = types.ModuleType(
            f"web_dashboard.services.{name}")


_install_stubs()
try:
    from web_dashboard.services import cloud_database_service as svc
except Exception as exc:  # pragma: no cover — skip if other app deps are missing
    try:
        import pytest
        pytest.skip(f"cloud_database_service import unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)


class _Q:
    def __init__(self, result):
        self._r = result

    def filter(self, *a, **k):
        return self

    def order_by(self, *a, **k):
        return self

    def first(self):
        return self._r


class _FakeDB:
    """query(CloudDatabase).filter(...).first() → the preset row. The Job lookup
    inside _provision_job_for is bypassed (we monkeypatch it per test)."""
    def __init__(self, row):
        self._row = row

    def query(self, _model):
        return _Q(self._row)


def _run(db, db_id, tf_variables):
    svc._provision_job_for = lambda _db, _id: types.SimpleNamespace(
        metadata_dict={"tf_variables": tf_variables})
    return svc.ansible_connection_vars(db, db_id)


def test_postgres_aws_uses_store_password_and_provisioned_db_name():
    CONF.clear()
    CONF["clouddb/db-pg/admin"] = "s3cret-pw"
    row = _CloudDatabase(id="db-pg", engine="postgres", cloud="aws",
                         private_host="pg.internal", port=5432)
    out = _run(_FakeDB(row), "db-pg",
               {"master_username": "dbadmin", "db_name": "appdb"})
    assert out == {
        "db_engine": "postgres",
        "db_login_host": "pg.internal",
        "db_login_port": 5432,
        "db_login_user": "dbadmin",
        "db_login_password": "s3cret-pw",
        "db_name": "appdb",
    }


def test_sqlserver_gcp_forces_sqlserver_user_and_master_db():
    CONF.clear()
    CONF["clouddb/db-ms/admin"] = "pw"
    row = _CloudDatabase(id="db-ms", engine="sqlserver", cloud="gcp",
                         private_host="ms.internal", port=1433)
    # tf_variables has no username → the gcp+sqlserver default applies.
    out = _run(_FakeDB(row), "db-ms", {"db_name": "ignored"})
    assert out["db_login_user"] == "sqlserver"
    assert out["db_name"] == "master"      # always master for SQL Server
    assert out["db_login_port"] == 1433


def test_sqlserver_aws_master_db_and_dbadmin_default():
    CONF.clear()
    CONF["clouddb/db-ms2/admin"] = "pw"
    row = _CloudDatabase(id="db-ms2", engine="sqlserver", cloud="aws",
                         private_host="ms2.internal", port=1433)
    out = _run(_FakeDB(row), "db-ms2", {})
    assert out["db_login_user"] == "dbadmin"
    assert out["db_name"] == "master"


def test_azure_administrator_login_key_and_port_fallback():
    CONF.clear()
    CONF["clouddb/db-az/admin"] = "pw"
    row = _CloudDatabase(id="db-az", engine="mysql", cloud="azure",
                         private_host="az.mysql", port=None)  # port unset
    out = _run(_FakeDB(row), "db-az",
               {"administrator_login": "azadmin", "db_name": "wp"})
    assert out["db_login_user"] == "azadmin"     # administrator_login key honored
    assert out["db_login_port"] == 3306          # engine default when row.port is None
    assert out["db_name"] == "wp"


def test_tf_variables_password_wins_over_store():
    CONF.clear()
    CONF["clouddb/db-pg2/admin"] = "store-pw"
    row = _CloudDatabase(id="db-pg2", engine="postgres", cloud="aws",
                         private_host="pg2", port=5432)
    out = _run(_FakeDB(row), "db-pg2",
               {"master_username": "dbadmin", "master_password": "tf-pw", "db_name": "d"})
    assert out["db_login_password"] == "tf-pw"


def test_missing_credential_raises():
    CONF.clear()  # no store entry, no tf password
    row = _CloudDatabase(id="db-x", engine="postgres", cloud="aws",
                         private_host="x", port=5432)
    try:
        _run(_FakeDB(row), "db-x", {"master_username": "dbadmin", "db_name": "d"})
    except svc.CloudDatabaseError:
        return
    raise AssertionError("expected CloudDatabaseError when no admin credential is resolvable")


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
