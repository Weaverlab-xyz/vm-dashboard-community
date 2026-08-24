"""Unit test: a dashboard-built database's name is recorded on its row and shown.

Before this, `cloud_databases.db_name` was written only on the *registered* path — a
provisioned row's catalog lived solely in its provisioning job's tf_variables. So the
Databases page could not show it, and the Entitle cloud-function adapter had nothing to
fall back on when that job was gone (see tests/test_clouddb_adapter_pairing.py).

What is pinned here is the read/write split that keeps the fix from changing behaviour:

  * the column stores the RAW Terraform catalog — never an engine substitution, because
    FN_DB_NAME must name a real database or none at all
  * connection_db_name applies `master` for SQL Server on READ, for display only
  * the backfill fills NULLs from the provisioning job, skips registered rows, and
    never invents a name

Heavy deps are stubbed in sys.modules (mirrors test_clouddb_ansible_conn_vars.py); no
real DB or cloud account is needed. Runs under pytest, or standalone:
    python tests/test_clouddb_row_db_name.py
"""
import os
import sys
import types
from datetime import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

CONF = {}


class _Settings:
    def __getattr__(self, _key):
        return ""


class _Col:
    """Stands in for a mapped column so the filter/order_by EXPRESSIONS in the code
    under test parse. The fake query ignores the predicates themselves."""
    def is_(self, _other):
        return self

    def asc(self):
        return self

    def desc(self):
        return self

    def __eq__(self, _other):
        return self


class _CloudDatabase:
    id = _Col()
    db_name = _Col()          # class-level, so CloudDatabase.db_name.is_(None) parses

    def __init__(self, **kw):
        # Real instance defaults, so `row.db_name or ""` never sees the _Col sentinel.
        self.db_name = None
        self.source = "provisioned"
        self.provider = None
        self.cloud = "aws"
        self.region = ""
        self.instance_id = ""
        self.private_host = ""
        self.port = None
        self.status = "available"
        self.jump_item_id = None
        self.entitle_integration_id = None
        self.ps_managed_system_id = None
        self.ps_managed_account_id = None
        self.created_by = "admin"
        self.created_at = datetime(2026, 8, 21, 12, 0, 0)
        self.__dict__.update(kw)


class _Job:
    job_type = _Col()
    created_at = _Col()

    def __init__(self, metadata_dict):
        self.metadata_dict = metadata_dict


def _install_stubs():
    try:
        from sqlalchemy.orm import Session as _RealSession  # noqa: F401
    except Exception:
        sa = types.ModuleType("sqlalchemy")
        orm = types.ModuleType("sqlalchemy.orm")
        orm.Session = type("Session", (), {})
        sa.orm = orm
        sys.modules["sqlalchemy"] = sa
        sys.modules["sqlalchemy.orm"] = orm

    confmod = types.ModuleType("web_dashboard.config")
    confmod.settings = _Settings()
    sys.modules["web_dashboard.config"] = confmod

    dbmod = types.ModuleType("web_dashboard.database")
    dbmod.CloudDatabase = _CloudDatabase
    dbmod.Job = _Job
    sys.modules["web_dashboard.database"] = dbmod

    cfg = types.ModuleType("web_dashboard.services.config_service")
    cfg.get = lambda key: CONF.get(key, "")
    cfg.get_bool = lambda key, default=False: bool(CONF.get(key, default))
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
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *a, **k):
        return self

    def order_by(self, *a, **k):
        return self

    def all(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None


class _FakeDB:
    def __init__(self, rows=(), jobs=()):
        self.rows, self.jobs = list(rows), list(jobs)
        self.commits = 0

    def query(self, model):
        return _Q(self.jobs if model is _Job else self.rows)

    def commit(self):
        self.commits += 1


# -- connection_db_name: the read-time resolver -------------------------------

def test_the_recorded_catalog_is_what_you_connect_to():
    for engine in ("postgres", "mysql"):
        row = _CloudDatabase(id="db-1", engine=engine, db_name="appdb")
        assert svc.connection_db_name(row) == "appdb"


def test_a_provisioned_sqlserver_always_reports_master():
    """RDS creates no user database at all; the Azure/GCP admin paths target master on
    purpose. Same answer as _broker_tunnel, _entitle_register_core and
    ansible_connection_vars, which is the point of having one resolver for the page."""
    for cloud, stored in (("aws", None), ("azure", "appdb"), ("gcp", "appdb")):
        row = _CloudDatabase(id="db-ms", engine="sqlserver", cloud=cloud, db_name=stored)
        assert svc.connection_db_name(row) == "master"


def test_the_azure_gcp_sqlserver_catalog_is_not_overwritten_by_master():
    """The substitution is applied on READ only. Storing it would take FN_DB_NAME with
    it, and the adapter would scope grants at the instance instead of the database."""
    row = _CloudDatabase(id="db-ms", engine="sqlserver", cloud="azure", db_name="appdb")
    assert svc.connection_db_name(row) == "master"
    assert row.db_name == "appdb"       # untouched


def test_a_registered_sqlserver_keeps_the_operators_entry():
    """Somebody else's server: what they recorded wins, master is only the fallback —
    the asymmetry _registered_connection_vars already had."""
    named = _CloudDatabase(id="r1", engine="sqlserver", source="registered", db_name="SQL22")
    blank = _CloudDatabase(id="r2", engine="sqlserver", source="registered", db_name=None)
    assert svc.connection_db_name(named) == "SQL22"
    assert svc.connection_db_name(blank) == "master"


def test_oracle_stays_resolvable_without_the_provisioning_job():
    """The ADB name is derived from the row id, so a row the backfill could not reach
    still shows the right thing — but only a provisioned one. Inventing adb<uuid> for a
    registered Oracle database would name something that does not exist."""
    row = _CloudDatabase(id="db-or", engine="oracle", cloud="oci", db_name=None)
    assert svc.connection_db_name(row) == svc._oracle_db_name("db-or")
    reg = _CloudDatabase(id="db-or2", engine="oracle", source="registered", db_name=None)
    assert svc.connection_db_name(reg) == ""


def test_an_unresolvable_row_reports_nothing_rather_than_guessing():
    row = _CloudDatabase(id="db-old", engine="postgres", db_name=None)
    assert svc.connection_db_name(row) == ""


# -- The backfill -------------------------------------------------------------

def _job(db_id, tf_variables):
    return _Job({"db_id": db_id, "tf_variables": tf_variables})


def test_the_backfill_stamps_the_catalog_from_the_provisioning_job():
    row = _CloudDatabase(id="db-1", engine="postgres", db_name=None)
    db = _FakeDB([row], [_job("db-1", {"db_name": "appdb"})])
    assert svc.backfill_provisioned_db_names(db) == 1
    assert row.db_name == "appdb"


def test_the_backfill_reads_the_newest_job_for_a_database():
    """Matches _provision_job_for's newest-first pick: a redeployed row must not get
    the name from a superseded apply."""
    row = _CloudDatabase(id="db-1", engine="mysql", db_name=None)
    db = _FakeDB([row], [_job("db-1", {"db_name": "old"}),
                         _job("db-1", {"db_name": "current"})])
    svc.backfill_provisioned_db_names(db)
    assert row.db_name == "current"


def test_the_backfill_is_idempotent():
    row = _CloudDatabase(id="db-1", engine="postgres", db_name=None)
    jobs = [_job("db-1", {"db_name": "appdb"})]
    assert svc.backfill_provisioned_db_names(_FakeDB([row], jobs)) == 1
    # Second boot: the row is no longer a candidate, so the query returns nothing.
    assert svc.backfill_provisioned_db_names(_FakeDB([], jobs)) == 0


def test_the_backfill_never_touches_a_registered_row():
    """A blank name there is the operator's own choice, and inventing one would change
    what a Config-Management run connects to."""
    row = _CloudDatabase(id="r1", engine="mysql", source="registered", db_name=None)
    db = _FakeDB([row], [_job("r1", {"db_name": "invented"})])
    assert svc.backfill_provisioned_db_names(db) == 0
    assert row.db_name is None


def test_a_row_whose_job_is_gone_stays_null_without_raising():
    """The failure mode this whole change exists to soften must not become a crash on
    startup — init_db calls this before the app serves a request."""
    row = _CloudDatabase(id="db-gone", engine="postgres", db_name=None)
    db = _FakeDB([row], [])
    assert svc.backfill_provisioned_db_names(db) == 0
    assert row.db_name is None
    assert db.commits == 0


def test_rds_sqlserver_stays_null_because_no_user_database_exists():
    """Its module omits db_name (tests/test_cloud_db_tf_vars.py pins that), so there is
    nothing to record. `master` is added on read, not written here."""
    row = _CloudDatabase(id="db-ms", engine="sqlserver", cloud="aws", db_name=None)
    db = _FakeDB([row], [_job("db-ms", {"identifier": "clouddb-abc12345"})])
    assert svc.backfill_provisioned_db_names(db) == 0
    assert row.db_name is None
    assert svc.connection_db_name(row) == "master"


def test_the_backfill_reads_the_same_keys_the_adapter_does():
    """SQL Server's modules name it differently and Azure SQL's is the database
    resource, so a single-key read would silently skip those rows."""
    for key in ("db_name", "database_name", "initial_catalog"):
        row = _CloudDatabase(id="db-1", engine="sqlserver", cloud="azure", db_name=None)
        svc.backfill_provisioned_db_names(_FakeDB([row], [_job("db-1", {key: "appdb"})]))
        assert row.db_name == "appdb", key


# -- What the page is given ---------------------------------------------------

def test_both_names_are_projected_so_the_page_can_show_the_pair():
    """The template does no engine arithmetic of its own — that is what stops the cell
    drifting from what the tunnel and the adapter actually use."""
    row = _CloudDatabase(id="db-ms", engine="sqlserver", cloud="azure",
                         provider="azure_sql", db_name="appdb")
    out = svc._serialize(row)
    assert out["db_name"] == "appdb"            # the catalog Terraform created
    assert out["connect_db_name"] == "master"   # what a session opens against


def test_the_connection_dialog_is_told_the_database():
    row = _CloudDatabase(id="db-1", engine="postgres", private_host="pg.internal",
                         port=5432, db_name="appdb")
    out = svc.connection_info(_FakeDB([row]), "db-1")
    assert out["connect_db_name"] == "appdb"
    assert out["db_name"] == "appdb"


def test_the_page_renders_the_server_resolved_values():
    """An engine ternary in Alpine would be a seventh copy of the rule, free to drift
    from the six already in the service."""
    path = os.path.join(_ROOT, "web_dashboard", "templates", "databases", "index.html")
    source = open(path, encoding="utf-8").read()
    assert ">Database</th>" in source
    assert "d.db_name || d.connect_db_name" in source
    assert "conn?.connect_db_name" in source
    assert "d.engine === 'sqlserver'" not in source


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
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
