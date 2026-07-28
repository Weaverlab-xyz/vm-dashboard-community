"""Unit tests for the Entitle DB-registration viability gate in
``cloud_database_service`` (PR: gate Register-in-Entitle for managed SQL Server).

Covers:
- ``_entitle_viable`` truth table: every engine except SQL Server is viable, and
  SQL Server is viable only when its ``provider`` is in the viable-providers set
  (empty today → all three managed flavors are blocked);
- ``_entitle_register_core`` refuses a non-viable row up front with
  ``CloudDatabaseError`` (before any DB access);
- ``_serialize`` surfaces ``entitle_viable`` for the frontend gate;
- the forward-compat contract: adding a provider to the viable set flips it True.

Imports the real service. It used to need the full app environment, which meant it ran
only in CI — and a change to ``_serialize`` broke it there after passing everything a
developer could run locally. ``cloud_database_service`` imports sqlalchemy at module
scope purely for the ``Session`` type hint, so stubbing that is enough to run here too.

    python tests/test_entitle_viability.py     (or under pytest)
"""
import asyncio
import os
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

try:
    # Availability, NOT `"sqlalchemy" in sys.modules`: in CI the real library is
    # installed but not yet imported when this module runs, so the sys.modules check
    # is False and the thin stub below would shadow it — which is exactly how this
    # file broke CI once already ("cannot import name 'create_engine'").
    from sqlalchemy.orm import Session as _RealSession  # noqa: F401
except Exception:
    _sa = types.ModuleType("sqlalchemy")
    _orm = types.ModuleType("sqlalchemy.orm")
    _orm.Session = type("Session", (), {})
    _sa.orm = _orm
    sys.modules["sqlalchemy"] = _sa
    sys.modules["sqlalchemy.orm"] = _orm

try:  # the real app deps exist in CI; stub the module surface elsewhere
    import pydantic_settings  # noqa: F401
except ModuleNotFoundError:
    _conf = types.ModuleType("web_dashboard.config")
    _conf.settings = type("_Settings", (), {"__getattr__": lambda self, _k: ""})()
    sys.modules["web_dashboard.config"] = _conf

    # database.py builds real ORM models, which the thin sqlalchemy stub above can't
    # support. The tests here use SimpleNamespace rows anyway — the service only needs
    # the names importable. Same approach as tests/test_clouddb_ansible_conn_vars.py.
    _db = types.ModuleType("web_dashboard.database")
    _db.CloudDatabase = type("CloudDatabase", (), {"id": None})
    _db.Job = type("Job", (), {})
    sys.modules["web_dashboard.database"] = _db

    # config_service pulls in cryptography, whose native extension can't load here.
    # These four are only needed for the import to succeed — nothing under test calls
    # them.
    _cfgsvc = types.ModuleType("web_dashboard.services.config_service")
    _cfgsvc.get = lambda *_a, **_k: ""
    _cfgsvc.set = lambda *_a, **_k: None
    _cfgsvc.get_bool = lambda *_a, **_k: False
    sys.modules["web_dashboard.services.config_service"] = _cfgsvc
    _jobsvc = types.ModuleType("web_dashboard.services.job_service")
    _jobsvc.create_job = lambda *_a, **_k: None
    sys.modules["web_dashboard.services.job_service"] = _jobsvc
    for _name in ("terraform", "terraform_provider_env"):
        sys.modules[f"web_dashboard.services.{_name}"] = types.ModuleType(
            f"web_dashboard.services.{_name}")

from web_dashboard.services import cloud_database_service as svc  # noqa: E402


def _row(**kw):
    """A minimal stand-in for a CloudDatabase row carrying just the attributes the
    functions under test read."""
    defaults = dict(
        id="db-test01", engine="sqlserver", provider="cloudsql", cloud="gcp",
        region="us-east1", instance_id="i-1", private_host="h", port=1433,
        status="available", jump_item_id=None, entitle_integration_id=None,
        created_by="tester", created_at=None,
        # Registered vs dashboard-provisioned. _serialize surfaces it so the page can
        # pick the right delete verb; these rows are all the provisioned kind.
        source="provisioned", db_name=None,
    )
    defaults.update(kw)
    return types.SimpleNamespace(**defaults)


def test_non_sqlserver_engines_always_viable():
    for engine in ("postgres", "mysql", "oracle"):
        for provider in ("rds", "cloudsql", "flexibleserver", "autonomous", None):
            assert svc._entitle_viable(engine, provider) is True


def test_managed_sqlserver_flavors_are_blocked_today():
    # The three managed SQL Server offerings the dashboard provisions today.
    for provider in ("rds", "cloudsql", "sql_database"):
        assert svc._entitle_viable("sqlserver", provider) is False
    assert svc._entitle_viable("sqlserver", None) is False


def test_viable_set_is_empty_today():
    assert svc._ENTITLE_VIABLE_SQLSERVER_PROVIDERS == frozenset()


def test_adding_a_provider_flips_viability():
    # Forward-compat contract: PR2's Entitle-compatible offerings become viable
    # simply by joining the set — no other change.
    original = svc._ENTITLE_VIABLE_SQLSERVER_PROVIDERS
    try:
        svc._ENTITLE_VIABLE_SQLSERVER_PROVIDERS = frozenset(
            {"rds_custom", "sql_managed_instance"})
        assert svc._entitle_viable("sqlserver", "sql_managed_instance") is True
        assert svc._entitle_viable("sqlserver", "rds_custom") is True
        # Still-managed flavors remain blocked.
        assert svc._entitle_viable("sqlserver", "cloudsql") is False
    finally:
        svc._ENTITLE_VIABLE_SQLSERVER_PROVIDERS = original


def test_register_core_refuses_non_viable_before_db_access():
    # db=None proves the guard runs before any DB work: a non-viable row raises
    # without ever touching the session.
    row = _row(engine="sqlserver", provider="cloudsql", cloud="gcp")
    try:
        asyncio.run(svc._entitle_register_core(None, row=row, engine="sqlserver"))
        raise AssertionError("expected CloudDatabaseError for non-viable SQL Server")
    except svc.CloudDatabaseError as exc:
        assert "sysadmin" in str(exc) and "CONTROL SERVER" in str(exc)


def test_serialize_exposes_entitle_viable():
    assert svc._serialize(_row(engine="sqlserver", provider="cloudsql"))["entitle_viable"] is False
    assert svc._serialize(_row(engine="postgres", provider="cloudsql"))["entitle_viable"] is True


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
