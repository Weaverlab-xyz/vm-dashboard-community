"""Unit test: "which database?" has TWO answers for a cloud database, and they are
not interchangeable. tests/test_clouddb_row_db_name.py pins each resolver on its own;
this file pins the PAIR — that they deliberately disagree, and where.

  admin session — cloud_database_service.connection_db_name(row). Where an admin
      connects, and where SQL Server's SERVER-level principals live. `master` for
      every provisioned sqlserver row, on every cloud. Consumers are not display-only:
      the PRA protocol tunnel, the native Entitle connector, the Secrets Safe admin
      document, the managed-user creation and the Ansible connection vars.

  grant scope — cloud_db_adapter_service._database_name(...) -> FN_DB_NAME ->
      `USE [...]`. The catalog whose DATA a just-in-time account gets rights on. A
      recorded user catalog, NEVER master, because db_datareader/db_datawriter are
      database-level fixed roles.

The two genuinely differ for sqlserver on azure and gcp, whose Terraform modules do
create a user catalog (azurerm_mssql_database / google_sql_database). On aws they
cannot differ: that var set omits db_name, only `master` exists, and the grant scope
refuses rather than substituting it.

Heavy deps are stubbed in sys.modules (mirrors test_clouddb_ansible_conn_vars.py);
no real DB or cloud account is needed. Runs under pytest, or standalone:
    python tests/test_clouddb_db_name_concepts.py
"""
import os
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


class _Settings:
    def __getattr__(self, _key):
        return ""


class _CloudDatabase:
    id = None
    engine = None
    cloud = None
    source = "provisioned"
    db_name = None

    def __init__(self, **kw):
        self.__dict__.update(kw)


def _install_stubs():
    # Mirrors test_clouddb_ansible_conn_vars._install_stubs. sqlalchemy is imported at
    # module scope by both services purely for the `Session` type hint; stub it only
    # when the real library is unavailable, so CI's real one is never shadowed.
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

    # web_dashboard.database builds a real engine off settings.database_url at import,
    # so it must be stubbed rather than imported.
    dbmod = types.ModuleType("web_dashboard.database")
    dbmod.CloudDatabase = _CloudDatabase
    dbmod.Job = type("Job", (), {})
    sys.modules["web_dashboard.database"] = dbmod

    cfg = types.ModuleType("web_dashboard.services.config_service")
    cfg.get = lambda *_a, **_k: ""
    cfg.get_bool = lambda *_a, **_k: False
    cfg.set = lambda *_a, **_k: None
    cfg.resolve_reference = lambda *_a, **_k: ""
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
    from web_dashboard.services import cloud_db_adapter_service as pairing
except Exception as exc:  # pragma: no cover — skip if other app deps are missing
    try:
        import pytest
        pytest.skip(f"cloud database services unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)


# ── the two answers must stay distinct ───────────────────────────────────────

def test_the_two_answers_diverge_for_sqlserver_where_a_user_catalog_exists():
    """Azure/GCP SQL Server is the case the split exists for: one row, two different
    and both-correct answers. If these ever agree, one of them is wrong."""
    for cloud in ("azure", "gcp"):
        row = _CloudDatabase(id="db-1", engine="sqlserver", cloud=cloud, db_name="appdb")
        assert svc.connection_db_name(row) == "master"
        assert pairing._database_name({"db_name": "appdb"}, "sqlserver", row) == "appdb"
        assert svc.connection_db_name(row) != pairing._database_name({}, "sqlserver", row), (
            "these answer different questions — a tunnel/admin session vs. a grant "
            "scope — and collapsing them silently rescopes every SQL Server JIT grant "
            "on this cloud from the user catalog to the system catalog")


def test_the_two_answers_agree_for_engines_with_no_system_catalog():
    """Postgres/MySQL have no master, so both concepts resolve to the same recorded
    name — the divergence is genuinely sqlserver-only."""
    for engine in ("postgres", "mysql"):
        row = _CloudDatabase(id="db-2", engine=engine, cloud="aws", db_name="appdb")
        assert svc.connection_db_name(row) == pairing._database_name({}, engine, row)


def test_aws_sqlserver_is_the_one_place_they_cannot_disagree():
    """RDS creates no user catalog (the var set omits db_name), so the row is
    legitimately blank: the admin session still gets master, and the grant scope
    refuses rather than inheriting it."""
    row = _CloudDatabase(id="db-3", engine="sqlserver", cloud="aws", db_name=None)
    assert svc.connection_db_name(row) == "master"
    try:
        pairing._database_name({}, "sqlserver", row)
    except pairing.AdapterPairingError:
        return
    raise AssertionError("expected a refusal, not a master fallback")


# ── the tf_variables fallback is additive only ───────────────────────────────

def test_the_var_set_only_fills_a_blank_row():
    """The consumers that hold the provisioning job's var set pass it as a FALLBACK.
    It must never override a recorded row, or one of them could open a different
    database than the Databases page shows for the same row."""
    row = _CloudDatabase(id="db-4", engine="postgres", cloud="aws", db_name="from-the-row")
    assert svc.connection_db_name(row, {"db_name": "from-the-job"}) == "from-the-row"


def test_the_var_set_keeps_a_row_the_backfill_has_not_reached_resolvable():
    """A row provisioned before the column existed, on an instance that has not
    restarted since, still has a live job. Dropping that source would have regressed
    these callers to an empty database name."""
    row = _CloudDatabase(id="db-5", engine="postgres", cloud="aws", db_name=None)
    assert svc.connection_db_name(row, {"db_name": "appdb"}) == "appdb"
    # Still nothing invented when neither source has it.
    assert svc.connection_db_name(row, {}) == ""


def test_the_var_set_cannot_talk_a_provisioned_sqlserver_out_of_master():
    """Azure/GCP carry a real catalog in the var set. The admin session must ignore it,
    exactly as it ignores the same value on the row."""
    for cloud in ("azure", "gcp", "aws"):
        row = _CloudDatabase(id="db-6", engine="sqlserver", cloud=cloud, db_name=None)
        assert svc.connection_db_name(row, {"db_name": "appdb"}) == "master"


# ── the admin-session answer reaches every consumer that needs it ────────────

def test_every_admin_session_consumer_routes_through_the_one_resolver():
    """The five behavioural consumers used to carry their own copy of the sqlserver
    ternary. A resolver nothing calls cannot keep them consistent, and an inline copy
    is how they drifted apart in the first place."""
    source = open(svc.__file__, encoding="utf-8").read()
    assert source.count("connection_db_name(row") >= 8, (
        "expected the tunnel, the Entitle connector, the Secrets Safe document, both "
        "managed-user paths and the Ansible vars to all call connection_db_name")
    # The inline form is what this consolidates; none should survive.
    assert '"master" if engine == "sqlserver"' not in source, (
        "an inline sqlserver ternary is back — route it through connection_db_name")


def test_the_grant_scope_never_calls_the_admin_session_resolver():
    """The adapter must not reach for the admin catalog, however convenient."""
    source = open(pairing.__file__, encoding="utf-8").read()
    assert "connection_db_name" not in source.replace(
        "``cloud_database_service.connection_db_name``", ""), (
        "the adapter referenced the admin-session resolver outside its docstring — "
        "FN_DB_NAME must name a real catalog or nothing")


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
