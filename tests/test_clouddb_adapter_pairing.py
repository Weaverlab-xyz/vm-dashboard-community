"""Pairing a provisioned cloud database with its Entitle adapter.

The pairing itself deploys a function and talks to three clouds, so what is pinned
here is the pure decision logic that decides WHAT gets deployed — which is where a
mistake is silent rather than loud:

  * routing MySQL/SQL Server to an adapter and Postgres to the native connector
  * the SQL Server flavor per cloud, which is the crux of the whole feature
  * the environment the adapter is told about its target
  * that the credential is staged as a REFERENCE, never a value
"""
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

CONF = {}


def _stub(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module


# Stubbed unconditionally, not gated on whether a dependency happens to be
# installed: gating on that made a previous suite pass locally and fail in CI.
_stub("web_dashboard.services.config_service",
      get=lambda key, default="": CONF.get(key, default),
      get_bool=lambda key, default=False: bool(CONF.get(key, default)),
      set=lambda key, value: CONF.__setitem__(key, value),
      delete=lambda key: CONF.pop(key, None))
_stub("web_dashboard.services.job_service", create_job=None, set_running=None,
      set_completed=None, set_failed=None, update_progress=None)

try:
    import pydantic  # noqa: F401
except ImportError:
    _stub("web_dashboard.config", settings=types.SimpleNamespace())

try:
    import sqlalchemy  # noqa: F401
except ImportError:
    print("SKIP: sqlalchemy not installed")
    sys.exit(0)

# The module imports CloudDatabase for a type hint only; database.py drags in bcrypt
# and the whole ORM, none of which the pure functions under test touch.
try:
    import bcrypt  # noqa: F401
except ImportError:
    _stub("web_dashboard.database", CloudDatabase=object, Job=object,
          CloudFunction=object, SessionLocal=None)

from web_dashboard.services import cloud_db_adapter_service as pairing


class _Row:
    def __init__(self, engine="mysql", cloud="aws", **kw):
        self.id = "abcdef12-3456-7890-abcd-ef1234567890"
        self.engine = engine
        self.cloud = cloud
        self.region = "us-east-1"
        self.private_host = "db.internal"
        self.port = 3306
        self.entitle_integration_id = None
        for key, value in kw.items():
            setattr(self, key, value)


# ── Which engines need an adapter ────────────────────────────────────────────

def test_mysql_and_sqlserver_need_an_adapter_and_say_why():
    """Both reasons surface in the job log and the UI, so they are worded for a
    person rather than as an enum."""
    mysql = pairing.adapter_required("mysql")
    assert mysql and "persistent roles" in mysql
    mssql = pairing.adapter_required("sqlserver")
    assert mssql and "sysadmin" in mssql


def test_postgres_keeps_the_native_connector():
    """It works today. Replacing a working integration with a new moving part buys
    nothing, so the provision path must not route it to an adapter."""
    assert pairing.adapter_required("postgres") is None


def test_an_unknown_engine_is_not_silently_adapted():
    assert pairing.adapter_required("oracle") is None
    assert pairing.adapter_required("") is None


# ── The SQL Server flavor: the crux ──────────────────────────────────────────

def test_the_sqlserver_flavor_is_correct_per_cloud():
    """Getting this wrong emits SQL for the wrong dialect and fails at the first
    grant. Azure is the one that differs: a contained-database model needing the
    login in master and the user in the target database."""
    assert pairing.flavor_for("sqlserver", "azure") == "azure_sql"
    assert pairing.flavor_for("sqlserver", "aws") == "rds"
    assert pairing.flavor_for("sqlserver", "gcp") == "cloudsql"


def test_every_flavor_is_one_the_sql_builders_accept():
    """A flavor this module invents but cloud_db_sql_service rejects would fail at
    the first grant, in the cloud."""
    from web_dashboard.services import cloud_db_sql_service
    for cloud in ("aws", "azure", "gcp"):
        assert pairing.flavor_for("sqlserver", cloud) in cloud_db_sql_service.VALID_FLAVORS


def test_non_sqlserver_engines_carry_no_flavor():
    for engine in ("mysql", "postgres"):
        assert pairing.flavor_for(engine, "azure") == ""


def test_an_unknown_cloud_is_refused_rather_than_defaulted():
    """Defaulting to 'rds' on an unrecognised cloud would emit the wrong dialect
    silently — the single worst outcome available here."""
    try:
        pairing.flavor_for("sqlserver", "oci")
    except pairing.AdapterPairingError as exc:
        assert "flavor" in str(exc)
    else:
        raise AssertionError("accepted an unknown cloud")


# ── The environment the adapter is given ─────────────────────────────────────

def test_the_environment_pins_the_target_completely():
    env = pairing.build_environment(_Row(), admin_username="dbadmin",
                                    database="appdb")
    assert env["FN_DB_ENGINE"] == "mysql"
    assert env["FN_DB_HOST"] == "db.internal"
    assert env["FN_DB_PORT"] == "3306"
    assert env["FN_DB_NAME"] == "appdb"
    assert env["FN_DB_ADMIN_USER"] == "dbadmin"


def test_dry_run_is_on_unless_explicitly_armed():
    """Observe-first, like notify_dry_run and resource_expiry_dry_run: a newly
    paired adapter reports the SQL it WOULD run rather than changing a database."""
    assert pairing.build_environment(_Row(), admin_username="a",
                                     database="d")["FN_DB_DRY_RUN"] == "1"
    assert pairing.build_environment(_Row(), admin_username="a", database="d",
                                     dry_run=False)["FN_DB_DRY_RUN"] == "0"


def test_the_flavor_reaches_the_adapter_for_sqlserver_only():
    azure = pairing.build_environment(_Row(engine="sqlserver", cloud="azure"),
                                      admin_username="a", database="d")
    assert azure["FN_DB_FLAVOR"] == "azure_sql"
    assert "FN_DB_FLAVOR" not in pairing.build_environment(
        _Row(engine="mysql"), admin_username="a", database="d")


def test_the_environment_carries_no_secret():
    """The credential is staged in the cloud's own store and resolved by reference;
    a password here would sit in the function's configuration in plaintext."""
    env = pairing.build_environment(_Row(), admin_username="dbadmin", database="appdb")
    blob = repr(env).lower()
    for token in ("password", "secret", "credential"):
        assert token not in blob, f"{token} leaked into the adapter environment: {env}"


# ── Naming ───────────────────────────────────────────────────────────────────

def test_the_adapter_name_is_deterministic_and_cloud_safe():
    """Deterministic so re-pairing finds the same function instead of accumulating
    one per attempt."""
    from web_dashboard.services import cloud_function_service
    row = _Row()
    first, second = pairing.adapter_name(row), pairing.adapter_name(row)
    assert first == second
    assert cloud_function_service._NAME_RE.match(first), first


def test_different_databases_get_different_adapters():
    a, b = _Row(), _Row()
    b.id = "99999999-3456-7890-abcd-ef1234567890"
    assert pairing.adapter_name(a) != pairing.adapter_name(b)


def test_the_secret_key_is_scoped_to_the_database():
    assert _Row().id in pairing.secret_key(_Row())


# ── The database name ────────────────────────────────────────────────────────

def test_the_database_name_comes_from_the_provisioning_variables():
    for key in ("db_name", "database_name", "initial_catalog"):
        assert pairing._database_name({key: "appdb"}, "mysql") == "appdb"


def test_a_missing_database_name_is_refused_not_guessed():
    """Guessing would scope every grant to the wrong database."""
    try:
        pairing._database_name({}, "mysql")
    except pairing.AdapterPairingError as exc:
        assert "database name" in str(exc)
    else:
        raise AssertionError("invented a database name")


def test_the_provisioning_variables_still_win_over_the_row():
    """Ordering, not preference: it makes the row a pure fallback, so adding it cannot
    rescope a pairing that already resolves."""
    row = _Row(db_name="from-the-row")
    assert pairing._database_name({"db_name": "from-the-job"}, "mysql", row) == "from-the-job"


def test_the_row_carries_the_name_when_the_provisioning_job_is_gone():
    """The whole point of stamping cloud_databases.db_name at provision time: a pruned
    job used to take FN_DB_NAME with it and the pairing could not be made at all."""
    assert pairing._database_name({}, "mysql", _Row(db_name="appdb")) == "appdb"


def test_a_missing_database_name_is_still_refused_when_a_row_is_supplied():
    """RDS SQL Server creates no user database, so its row is legitimately blank.
    Substituting `master` there would scope grants at the whole instance instead of
    refusing — the fallback must be a RECORDED value, never an engine default."""
    for row in (_Row(engine="sqlserver", db_name=None),
                _Row(engine="sqlserver", db_name="")):
        try:
            pairing._database_name({}, "sqlserver", row)
        except pairing.AdapterPairingError as exc:
            assert "database name" in str(exc)
        else:
            raise AssertionError("invented a database name")


def test_the_pairing_passes_the_row_to_the_resolver():
    """A fallback nothing reaches is not a fallback."""
    source = open(pairing.__file__, encoding="utf-8").read()
    assert "_database_name(tf_variables, row.engine, row)" in source


# ── Wiring ───────────────────────────────────────────────────────────────────

def test_the_provision_path_routes_by_adapter_required():
    """The branch in cloud_database_service must key off this module, so the two
    cannot disagree about which engines the native connector can serve."""
    source = open(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                     "web_dashboard", "services", "cloud_database_service.py"),
        encoding="utf-8").read()
    assert "cloud_db_adapter_service.adapter_required(engine)" in source
    assert "_pair_adapter(" in source


def test_the_adapter_workload_exists():
    from web_dashboard.services import cloud_function_package
    assert pairing._ADAPTER_WORKLOAD in cloud_function_package.available_workloads()


def test_pairing_is_always_vpc_attached():
    """A public adapter deploys fine and fails every grant — the database has no
    public endpoint."""
    source = open(pairing.__file__, encoding="utf-8").read()
    assert 'network_mode="vpc"' in source


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as exc:
            failures += 1
            print(f"FAIL {fn.__name__}: {exc}")
    sys.exit(1 if failures else 0)
