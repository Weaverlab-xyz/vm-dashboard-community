"""Unit tests for the Entitle DB-registration viability gate in
``cloud_database_service`` (PR: gate Register-in-Entitle for managed SQL Server).

Covers:
- ``_entitle_viable`` truth table: every engine except SQL Server is viable, and
  SQL Server is viable only when its ``provider`` is in the viable-providers set
  (empty today → all three managed flavors are blocked);
- a **registered** row is never viable, whatever its engine: it has no provisioning
  credential to register with, so the button must be hidden and the API must refuse;
- each blocker gets its own message — the registered one names the missing
  provisioning credential rather than reusing the SQL Server connector wording;
- ``_entitle_register_core`` refuses a non-viable row up front with
  ``CloudDatabaseError`` (before any DB access);
- ``_serialize`` surfaces ``entitle_viable`` for the frontend gate, and
  ``connection_info`` surfaces ``source`` for the API's pre-flight;
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
        # Password Safe DB onboarding — unset, i.e. never onboarded.
        ps_managed_system_id=None, ps_managed_account_id=None,
    )
    defaults.update(kw)
    return types.SimpleNamespace(**defaults)


class _FakeQuery:
    """Enough of a Query for connection_info: filter() ignores its criterion (a real
    BinaryExpression in CI, a plain bool against the stub class elsewhere)."""
    def __init__(self, row):
        self._row = row

    def filter(self, *_a, **_k):
        return self

    def first(self):
        return self._row


class _FakeSession:
    def __init__(self, row):
        self._row = row

    def query(self, *_a, **_k):
        return _FakeQuery(self._row)


def test_non_sqlserver_engines_always_viable():
    for engine in ("postgres", "mysql", "oracle"):
        for provider in ("rds", "cloudsql", "flexibleserver", "autonomous", None):
            assert svc._entitle_viable(engine, provider) is True


def test_provisioned_source_is_the_default_reading():
    # An older row can carry source=None; it is a provisioned row, not a registered one.
    for source in (None, "", "provisioned"):
        assert svc._entitle_viable("postgres", "rds", source) is True


def test_registered_rows_are_never_viable():
    # register_database stamps provider="registered" + source="registered"; every engine
    # is blocked, including the ones the connector itself supports fine.
    for engine in ("postgres", "mysql", "oracle", "sqlserver"):
        assert svc._entitle_viable(engine, "registered", "registered") is False
    # Also blocked when the row happens to carry a real provider (a registered row
    # pointed at an RDS endpoint, say) — source alone decides.
    assert svc._entitle_viable("postgres", "rds", "registered") is False


def test_registered_reason_names_the_missing_credential():
    reason = svc._entitle_ineligible_reason(
        "postgres", "registered", source="registered", cloud="aws")
    assert reason and "provisioned" in reason and "credential" in reason
    # The whole point of the separate message: it must NOT reuse the SQL Server
    # connector wording, which says nothing about why a registered row can't register.
    assert "sysadmin" not in reason and "CONTROL SERVER" not in reason


def test_registered_beats_sqlserver_in_the_reason():
    # A registered SQL Server row hits the more fundamental blocker: even an
    # Entitle-compatible flavor would have no credential to register with.
    reason = svc._entitle_ineligible_reason(
        "sqlserver", "registered", source="registered", cloud="aws")
    assert reason and "sysadmin" not in reason


def test_provisioned_sqlserver_reason_still_names_the_connector():
    # Source-awareness must not blunt the existing message for provisioned rows.
    reason = svc._entitle_ineligible_reason(
        "sqlserver", "cloudsql", source="provisioned", cloud="gcp")
    assert reason and "sysadmin" in reason and "CONTROL SERVER" in reason
    assert "cloudsql" in reason           # provider named
    assert "registered" not in reason
    # provider unset → the cloud names the flavor instead of a bare blank.
    assert "gcp" in svc._entitle_ineligible_reason(
        "sqlserver", None, source="provisioned", cloud="gcp")


def test_viable_rows_have_no_reason():
    assert svc._entitle_ineligible_reason("postgres", "rds", source="provisioned") is None
    assert svc._entitle_ineligible_reason("mysql", "cloudsql") is None


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


def test_register_core_refuses_a_registered_row_before_db_access():
    # The failure this guard replaces: a registered postgres row passed the old
    # engine-only check, queued a job, and died deep in _entitle_register_core on
    # "no admin credential available". db=None proves it never gets that far now.
    row = _row(engine="postgres", provider="registered", cloud="aws",
               source="registered", port=5432)
    try:
        asyncio.run(svc._entitle_register_core(None, row=row, engine="postgres"))
        raise AssertionError("expected CloudDatabaseError for a registered row")
    except svc.CloudDatabaseError as exc:
        assert "provisioned" in str(exc)
        assert "no admin credential" not in str(exc)   # not the deep-in-the-job failure


def test_serialize_exposes_entitle_viable():
    assert svc._serialize(_row(engine="sqlserver", provider="cloudsql"))["entitle_viable"] is False
    assert svc._serialize(_row(engine="postgres", provider="cloudsql"))["entitle_viable"] is True
    # A registered row: viable engine, but the button must still be hidden.
    assert svc._serialize(_row(engine="postgres", provider="registered",
                               source="registered"))["entitle_viable"] is False


def test_connection_info_exposes_source_for_the_api_preflight():
    # The API's entitle-register gate reads `source` off connection_info; without it the
    # pre-flight silently treats every row as provisioned.
    row = _row(engine="postgres", provider="registered", source="registered")
    assert svc.connection_info(_FakeSession(row), row.id)["source"] == "registered"
    prov = _row(engine="postgres", provider="rds", source="provisioned")
    assert svc.connection_info(_FakeSession(prov), prov.id)["source"] == "provisioned"
    # An older row with a NULL source reads as provisioned, not as a missing key.
    old = _row(engine="postgres", provider="rds", source=None)
    assert svc.connection_info(_FakeSession(old), old.id)["source"] == "provisioned"


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
