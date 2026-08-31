"""GCP Cloud SQL: the dashboard issues the Password Safe rotation grant itself.

Onboarding used to create the managed user and then print the functional account's
GRANT on the job for someone to run by hand — the last manual step on a path that is
otherwise a button. ``executeSql`` authenticates as the CALLER and the dashboard's own
service account is not a privileged database principal, so it could not simply run the
statement as itself. The way through is the Data API's other authentication mode: a
built-in ``user`` whose password comes from Secret Manager, and the dashboard already
holds the admin credential it minted for this database.

What these tests pin, all of it learned the hard way against a live instance:

- the secret is **regional**. The Data API rejects the global
  ``projects/*/secrets/*/versions/*`` form outright — which is also the exact shape the
  plugin article's own ``fasecret=`` example prints — demanding
  ``projects/*/locations/*/secrets/*/versions/*``;
- the staged admin credential is **deleted afterwards**, including when the statement
  fails, so a database's master password is not parked in Secret Manager forever;
- its secret id is **not** the db_grant adapter's ``clouddb-<id>-admin``, or onboarding
  a database would revoke a paired adapter's credential;
- a failure **falls back to reporting the statement** rather than unwinding the
  onboarding: a grant the operator can still paste is better than a half-created system;
- the call really does use built-in-user auth, not ``autoIamAuthn`` — getting that wrong
  reintroduces the exact "authenticates as the caller" problem this replaces.

Runs under pytest, or standalone:  python tests/test_clouddb_gcp_grant.py
"""
import asyncio
import os
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

CONF = {}
LOGS = []
SECRETS = {}
CALLS = []


class _Settings:
    def __getattr__(self, _key):
        return ""


class _CloudDatabase:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Job:
    id = None


# ── fake gcp_service ──────────────────────────────────────────────────────────

FAIL_EXECUTE = []      # push an exception to make executeSql fail
FAIL_WRITE = []        # push an exception to make the secret write fail


async def _fake_write_regional_secret(project, region, secret_id, value):
    if FAIL_WRITE:
        raise FAIL_WRITE[0]
    CALLS.append(("write_secret", project, region, secret_id, value))
    SECRETS[(project, region, secret_id)] = value
    return f"projects/{project}/locations/{region}/secrets/{secret_id}/versions/latest"


async def _fake_execute_cloudsql_sql(project, instance, database, statement, *,
                                     auto_iam_authn=True, user="",
                                     password_secret_version=""):
    CALLS.append(("execute", project, instance, database, statement,
                  auto_iam_authn, user, password_secret_version))
    if FAIL_EXECUTE:
        raise FAIL_EXECUTE[0]
    return [{}]


async def _fake_delete_regional_secret(project, region, secret_id):
    CALLS.append(("delete_secret", project, region, secret_id))
    SECRETS.pop((project, region, secret_id), None)
    return True


def _install_stubs():
    confmod = types.ModuleType("web_dashboard.config")
    confmod.settings = _Settings()
    sys.modules["web_dashboard.config"] = confmod

    dbmod = types.ModuleType("web_dashboard.database")
    dbmod.CloudDatabase = _CloudDatabase
    dbmod.Job = _Job
    sys.modules["web_dashboard.database"] = dbmod

    cfg = types.ModuleType("web_dashboard.services.config_service")
    cfg.get = lambda key: CONF.get(key, "")
    cfg.set = lambda key, val: CONF.__setitem__(key, val)
    cfg.get_bool = lambda key, default=False: bool(CONF.get(key, default))
    sys.modules["web_dashboard.services.config_service"] = cfg

    js = types.ModuleType("web_dashboard.services.job_service")
    js.append_job_log = lambda _db, job_id, msg: LOGS.append((job_id, msg))
    js.update_progress = lambda *a, **k: None
    sys.modules["web_dashboard.services.job_service"] = js

    gs = types.ModuleType("web_dashboard.services.gcp_service")
    gs.write_regional_secret = _fake_write_regional_secret
    gs.execute_cloudsql_sql = _fake_execute_cloudsql_sql
    gs.delete_regional_secret = _fake_delete_regional_secret
    sys.modules["web_dashboard.services.gcp_service"] = gs

    for name in ("terraform", "terraform_provider_env", "ps_api_service",
                 "ps_resource_service"):
        sys.modules[f"web_dashboard.services.{name}"] = types.ModuleType(
            f"web_dashboard.services.{name}")
    sys.modules["web_dashboard.services.ps_resource_service"].PSResourceError = type(
        "PSResourceError", (Exception,), {})


_install_stubs()
try:
    from web_dashboard.services import cloud_database_service as svc
except Exception as exc:  # pragma: no cover — skip if other app deps are missing
    try:
        import pytest
        pytest.skip(f"cloud_database_service import unavailable: {exc}",
                    allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)


class _FakeDB:
    def commit(self):
        pass


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _reset():
    CONF.clear()
    LOGS.clear()
    SECRETS.clear()
    CALLS.clear()
    FAIL_EXECUTE.clear()
    FAIL_WRITE.clear()


def _apply(**kw):
    row = _CloudDatabase(id="db1", cloud="gcp", region="us-east1")
    kw.setdefault("engine", "mysql")
    kw.setdefault("project", "proj")
    kw.setdefault("instance", "inst")
    kw.setdefault("region", "us-east1")
    kw.setdefault("database", "app_db")
    kw.setdefault("admin_username", "dbadmin")
    kw.setdefault("admin_password", "s3cret")
    kw.setdefault("grant", "GRANT CREATE USER ON *.* TO 'bt-rotator'@'%';")
    return _run(svc._apply_fa_grant_gcp(_FakeDB(), row=row, job_id="job-1", **kw))


def _of(kind):
    return [c for c in CALLS if c[0] == kind]


# ── the grant actually runs ───────────────────────────────────────────────────

def test_grant_is_applied_and_reported():
    _reset()
    assert _apply() is True
    execs = _of("execute")
    assert len(execs) == 1
    assert execs[0][4] == "GRANT CREATE USER ON *.* TO 'bt-rotator'@'%';"
    assert any("Applied the Password Safe rotation grant" in m for _j, m in LOGS)


def test_uses_builtin_user_auth_not_caller_identity():
    """autoIamAuthn would authenticate as the DASHBOARD's service account, which is not
    a privileged database principal — the whole reason this could not be done before."""
    _reset()
    _apply()
    _proj, _inst, _db, _stmt, auto_iam, user, version = _of("execute")[0][1:]
    assert auto_iam is False
    assert user == "dbadmin"
    assert version


def test_secret_version_is_regional_not_global():
    """The Data API rejects projects/*/secrets/*/versions/* — including the exact shape
    the plugin article documents for fasecret=."""
    _reset()
    _apply()
    version = _of("execute")[0][7]
    assert version == ("projects/proj/locations/us-east1/secrets/"
                       "clouddb-db1-psgrant/versions/latest")
    assert "/locations/us-east1/" in version


def test_sqlserver_grant_runs_in_master():
    _reset()
    _apply(engine="sqlserver", database="master",
           grant="ALTER SERVER ROLE CustomerDbRootRole ADD MEMBER [sqlserver];")
    assert _of("execute")[0][3] == "master"


# ── the staged credential does not outlive the statement ──────────────────────

def test_secret_deleted_after_success():
    _reset()
    _apply()
    assert _of("delete_secret"), "the staged admin credential was left in Secret Manager"
    assert SECRETS == {}


def test_secret_deleted_even_when_the_statement_fails():
    _reset()
    FAIL_EXECUTE.append(RuntimeError("permission denied"))
    assert _apply() is False
    assert _of("delete_secret"), "a failed grant left the master password staged"
    assert SECRETS == {}


def test_secret_id_does_not_collide_with_the_adapter_credential():
    """clouddb-<id>-admin is the db_grant adapter's long-lived secret. Sharing the id
    would make onboarding a database delete a paired adapter's password."""
    _reset()
    assert svc._grant_secret_id("db1") == "clouddb-db1-psgrant"
    assert svc._grant_secret_id("db1") != "clouddb-db1-admin"


# ── failure falls back, it does not unwind ────────────────────────────────────

def test_failure_reports_the_statement_and_never_raises():
    _reset()
    FAIL_EXECUTE.append(RuntimeError("nope"))
    assert _apply() is False
    assert any("GRANT CREATE USER" in m for _j, m in LOGS), \
        "the operator was not told which statement to run by hand"


def test_staging_failure_is_also_survivable():
    _reset()
    FAIL_WRITE.append(RuntimeError("secret manager down"))
    assert _apply() is False
    assert not _of("execute"), "ran the statement without a staged credential"
    assert any("GRANT CREATE USER" in m for _j, m in LOGS)


# ── preconditions ─────────────────────────────────────────────────────────────

def test_no_admin_password_means_no_attempt():
    """Without the minted credential there is nothing to authenticate as — fall back
    rather than staging an empty secret and failing at the API."""
    _reset()
    assert _apply(admin_password="") is False
    assert not _of("write_secret")
    assert not _of("execute")


def test_empty_grant_is_a_no_op():
    _reset()
    assert _apply(grant="") is False
    assert not _of("write_secret")


# ── the statement builder itself ──────────────────────────────────────────────

def test_mysql_grant_is_global_and_avoids_mysql_dml():
    stmt = svc._fa_grant_statement("mysql", fa_db_user="bt-rotator",
                                   managed_user="psafe_x", managed_host="%")
    assert stmt == "GRANT CREATE USER ON *.* TO 'bt-rotator'@'%';"
    assert "mysql." not in stmt, "Cloud SQL restricts DML on mysql.user"


def test_postgres_grant_is_per_role_admin_option():
    stmt = svc._fa_grant_statement("postgres", fa_db_user="fa", managed_user="psafe_x",
                                   managed_host="")
    assert "ADMIN OPTION" in stmt and "CREATEROLE" not in stmt


def _tests():
    return [(n, f) for n, f in sorted(globals().items())
            if n.startswith("test_") and callable(f)]


if __name__ == "__main__":
    failed = 0
    for name, fn in _tests():
        try:
            fn()
            print(f"PASS {name}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {name}: {exc}")
    print(f"\n{len(_tests()) - failed}/{len(_tests())} passed")
    sys.exit(1 if failed else 0)
