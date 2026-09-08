"""GCP Cloud SQL: the name the rotator service account is REGISTERED under.

Register in Password Safe failed at 25% ("Creating the rotatable managed database
user...") against a live PostgreSQL instance, in one second, with::

    create user 'bt-rotator@project-4e93c8e3-4e96-4bc0-9d1.iam.gserviceaccount.com'
    on clouddb-6583f505 failed: HTTP 400 ... "Invalid request: User name
    "bt-rotator@...gserviceaccount.com" to be created is too long (max 63)."

Nothing about the service account was wrong -- a Postgres role name is capped at 63
characters and the email is 65, so *every* PostgreSQL onboarding failed on length
alone. Google's documented form for PostgreSQL is the email minus the
``.gserviceaccount.com`` suffix.

What these tests pin:

- PostgreSQL registers the SUFFIX-STRIPPED name, and the real service account this
  lab uses fits under the cap once stripped;
- MySQL still registers the FULL EMAIL. Cloud SQL truncates it itself there
  (``users.list`` reads back ``bt-rotator``, proven live), so pre-truncating would
  replace a working call with an untested one -- the fix must not "tidy" that;
- SQL Server never registers a rotator at all, because it has no IAM database
  authentication;
- the name SENT and the name STORED are separate questions, and the read-back still
  wins over both. That is the trap this feature has already been bitten by once, when
  GKE turned out to store a numeric uniqueId where everyone had derived an email.

Runs under pytest, or standalone:  python tests/test_clouddb_gcp_iam_db_user.py
"""
import asyncio
import os
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# The real service account from the lab project, and the exact string that 400'd.
SA = "bt-rotator@project-4e93c8e3-4e96-4bc0-9d1.iam.gserviceaccount.com"
SA_PG = "bt-rotator@project-4e93c8e3-4e96-4bc0-9d1.iam"
PG_NAME_MAX = 63          # PostgreSQL NAMEDATALEN - 1
MYSQL_NAME_MAX = 32

CONF = {}
LOGS = []
CALLS = []
USERS = []                # what users.list will report back
ENGINE = []               # the engine the fake instance runs, set by _onboard


class _Settings:
    def __getattr__(self, _key):
        return ""


class _CloudDatabase:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Job:
    id = None


# -- fake gcp_service ---------------------------------------------------------

async def _fake_ensure_prereqs(project, instance, *, iam_auth=True):
    CALLS.append(("prereqs", project, instance, iam_auth))
    return {"patched": False}


async def _fake_create_cloudsql_user(project, instance, name, *, password="",
                                     iam_service_account=False, host=""):
    CALLS.append(("create_user", project, instance, name, iam_service_account, host))
    # Mirror the Admin API's own validation, which is per-ENGINE: PostgreSQL rejects an
    # over-cap name outright (the 400 that produced this test), while MySQL accepts the
    # email and truncates it to 32 itself. Applying Postgres's rule everywhere would
    # make the fake reject a call that is proven to work live.
    if iam_service_account and (ENGINE or ["postgres"])[0] == "postgres" \
            and len(name) > PG_NAME_MAX:
        raise RuntimeError(
            f"create user {name!r} on {instance} failed: HTTP 400 Invalid request: "
            f"User name {name!r} to be created is too long (max {PG_NAME_MAX}).")


async def _fake_list_cloudsql_users(project, instance):
    CALLS.append(("list_users", project, instance))
    return list(USERS)


async def _fake_write_regional_secret(project, region, secret_id, value):
    return f"projects/{project}/locations/{region}/secrets/{secret_id}/versions/latest"


async def _fake_execute_cloudsql_sql(project, instance, database, statement, **kw):
    CALLS.append(("execute", statement))
    return [{}]


async def _fake_delete_regional_secret(project, region, secret_id):
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
    gs.ensure_cloudsql_rotation_prereqs = _fake_ensure_prereqs
    gs.create_cloudsql_user = _fake_create_cloudsql_user
    gs.list_cloudsql_users = _fake_list_cloudsql_users
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
except Exception as exc:  # pragma: no cover -- skip if other app deps are missing
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
    CALLS.clear()
    USERS.clear()
    ENGINE.clear()
    CONF["clouddb_ps_gcp_rotator_service_account"] = SA


def _onboard(engine="postgres", **kw):
    """Run the real GCP onboarding step that failed live."""
    ENGINE[:] = [engine]
    row = _CloudDatabase(id="db1", cloud="gcp", region="us-east1",
                         instance_id="clouddb-6583f505", engine=engine, port=None,
                         private_host="10.0.0.5", db_name="app_db")
    tf_variables = {"project": "project-4e93c8e3-4e96-4bc0-9d1",
                    "master_username": "dbadmin", "master_password": "s3cret",
                    "db_name": "app_db"}
    tf_variables.update(kw)
    return _run(svc._create_db_managed_user_gcp(
        _FakeDB(), row=row, job_id="job-1", engine=engine, tf_variables=tf_variables))


def _registered():
    """The IAM principals handed to users.insert, in order."""
    return [c[3] for c in CALLS if c[0] == "create_user" and c[4]]


# -- the bug: PostgreSQL onboarding died on the rotator's name ----------------

def test_postgres_onboarding_no_longer_fails_on_the_rotator_name():
    """The live failure, end to end. The fake users.insert enforces the same 63-char
    cap the Admin API does, so sending the email again raises here too."""
    _reset()
    ctx = _onboard("postgres")
    assert _registered() == [SA_PG]
    assert ctx["managed_user"] == svc._managed_user_name("db1")


def test_the_email_itself_is_over_the_postgres_cap():
    """Not a contrived name -- the lab's own service account overflows, so every
    PostgreSQL onboarding failed, not an unlucky one."""
    assert len(SA) > PG_NAME_MAX
    assert len(SA_PG) <= PG_NAME_MAX


def test_postgres_registers_the_suffix_stripped_form():
    assert svc._iam_db_user_to_register("postgres", SA) == SA_PG
    assert not svc._iam_db_user_to_register("postgres", SA).endswith(
        ".gserviceaccount.com")


def test_a_long_local_part_is_still_reported_rather_than_silently_cut():
    """Stripping the suffix buys 20 characters, not immunity. A service account whose
    LOCAL part is itself over the cap must still fail loudly at users.insert -- quietly
    truncating would register a principal the functional account cannot name."""
    _reset()
    long_sa = ("a" * 60) + "@project-4e93c8e3-4e96-4bc0-9d1.iam.gserviceaccount.com"
    CONF["clouddb_ps_gcp_rotator_service_account"] = long_sa
    try:
        _onboard("postgres")
    except Exception as exc:
        assert "too long" in str(exc)
    else:
        raise AssertionError("an over-length rotator name was accepted")


# -- MySQL is proven live and must not be "tidied" ---------------------------

def test_mysql_still_registers_the_full_email():
    """Cloud SQL for MySQL accepts the email and truncates it itself -- confirmed
    against a live instance. Pre-truncating here would trade a working call for an
    untested one."""
    _reset()
    _onboard("mysql")
    assert _registered() == [SA]


def test_mysql_registration_is_unchanged_by_the_postgres_fix():
    assert svc._iam_db_user_to_register("mysql", SA) == SA


# -- SQL Server never has a rotator to register ------------------------------

def test_sqlserver_registers_no_iam_principal_at_all():
    """No IAM database authentication exists for Cloud SQL for SQL Server, so the
    functional account is the built-in admin and there is nothing to name."""
    _reset()
    ctx = _onboard("sqlserver")
    assert _registered() == []
    assert ctx["fa_db_user"] == "dbadmin"
    assert svc._iam_db_auth("sqlserver", "data-api") is False


# -- sent name vs stored name are different questions ------------------------

def test_the_read_back_beats_the_derivation():
    """users.list is authoritative. GCP has surprised this codebase before by storing
    a principal name nobody derived."""
    _reset()
    USERS.append({"name": "something-else@project-4e93c8e3-4e96-4bc0-9d1.iam",
                  "type": "CLOUD_IAM_SERVICE_ACCOUNT"})
    USERS.append({"name": SA_PG, "type": "CLOUD_IAM_SERVICE_ACCOUNT"})
    ctx = _onboard("postgres")
    assert ctx["fa_db_user"] == SA_PG


def test_mysql_derives_the_truncated_name_not_the_one_it_sent():
    """The fallback when users.list cannot be read. On MySQL the sent name and the
    stored name genuinely differ, and naming the sent one fails Verify."""
    assert svc._derived_iam_db_user("mysql", SA) == "bt-rotator"
    assert svc._derived_iam_db_user("mysql", SA) != svc._iam_db_user_to_register(
        "mysql", SA)
    assert len(svc._derived_iam_db_user("mysql", SA)) <= MYSQL_NAME_MAX


def test_postgres_derives_what_it_registered():
    assert svc._derived_iam_db_user("postgres", SA) == SA_PG


def test_a_blank_rotator_registers_nothing():
    """The setting is optional; an empty one must not insert a nameless principal."""
    _reset()
    CONF["clouddb_ps_gcp_rotator_service_account"] = ""
    ctx = _onboard("postgres")
    assert _registered() == []
    assert ctx["fa_db_user"] == ""


if __name__ == "__main__":
    failures = 0
    tests = sorted(
        (name, obj) for name, obj in list(globals().items())
        if name.startswith("test_") and callable(obj))
    for name, fn in tests:
        try:
            fn()
            print(f"PASS {name}")
        except Exception as exc:
            failures += 1
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
