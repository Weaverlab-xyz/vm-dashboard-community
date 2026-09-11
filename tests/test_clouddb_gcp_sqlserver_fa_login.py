"""Cloud SQL for SQL Server: the functional account's login is the dashboard's to mint.

Cloud SQL for SQL Server has no IAM database authentication, so Password Safe's
functional account cannot be a passwordless IAM principal the way it is for PostgreSQL
and MySQL on the ``data-api`` channel -- it is a real login with a real password, and the
engine therefore lands on ``cloud-run``, where the DB-Ops service opens a genuine TDS
connection.

Until now "create" mode satisfied that by handing Password Safe the instance's BUILT-IN
``sqlserver`` admin credential. That was wrong in two directions at once:

* it gave the rotation identity every right on the instance, when CustomerDbRootRole --
  or, under self-rotation, no privilege at all -- is enough; and
* it put the dashboard's own provisioning credential into Password Safe, so a *Change
  Functional Account* would rotate the admin out from under ``clouddb/<id>/admin`` and
  silently break the grant path, the PRA tunnel and decommission, with nothing able to
  notice.

``users.insert`` creates a Cloud SQL login without opening a database connection, which
is why the dashboard can own both halves here and cannot on the other two clouds.

What these tests pin:

- the dedicated login's NAME is derived from the managed user's and can never collide
  with one;
- its password is persisted and READ BACK, because ``create_functional_account_on_platform``
  resolves a duplicate and returns the existing account WITHOUT updating its password,
  while ``users.insert`` does reset the database's -- so a freshly generated password on
  a re-register would move the database and leave Password Safe behind;
- the admin credential is nowhere in what Password Safe is handed;
- the grant follows the minted login, and self-rotation removes it.

Runs under pytest, or standalone:  python tests/test_clouddb_gcp_sqlserver_fa_login.py
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
CALLS = []
USERS = []


class _Settings:
    def __getattr__(self, _key):
        return ""


class _CloudDatabase:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Job:
    id = None


async def _fake_ensure_prereqs(project, instance, *, iam_auth=True):
    CALLS.append(("prereqs", project, instance, iam_auth))
    return {"patched": False}


async def _fake_create_cloudsql_user(project, instance, name, *, password="",
                                     iam_service_account=False, host=""):
    # The real call RESETS an existing user's password to whatever it is handed; that is
    # the behaviour the persisted-password design exists to be safe against, so record
    # the password rather than just the name.
    CALLS.append(("create_user", name, password, iam_service_account))


async def _fake_list_cloudsql_users(project, instance):
    return list(USERS)


FAIL_DELETE = []          # push an exception to make the login drop fail


async def _fake_delete_cloudsql_user(project, instance, name, *, host=""):
    CALLS.append(("delete_user", project, instance, name))
    if FAIL_DELETE:
        raise FAIL_DELETE[0]
    # The real call returns False when the user (or the instance) is already gone.
    return name in [c[1] for c in CALLS if c[0] == "create_user"]


async def _fake_write_regional_secret(project, region, secret_id, value):
    CALLS.append(("write_secret", secret_id))
    return f"projects/{project}/locations/{region}/secrets/{secret_id}/versions/latest"


async def _fake_delete_regional_secret(project, region, secret_id):
    return True


async def _fake_execute_cloudsql_sql(project, instance, database, statement, **kw):
    CALLS.append(("execute", statement))
    return [{}]


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
    cfg.get_fresh = lambda key, default="": CONF.get(key, default)
    cfg.set = lambda key, val: CALLS.append(("config_set", key, val)) or \
        CONF.__setitem__(key, val)
    cfg.get_bool = lambda key, default=False: bool(CONF.get(key, default))
    cfg.delete = lambda key: CALLS.append(("config_delete", key)) or CONF.pop(key, None)
    sys.modules["web_dashboard.services.config_service"] = cfg

    js = types.ModuleType("web_dashboard.services.job_service")
    js.append_job_log = lambda _db, job_id, msg: LOGS.append(msg)
    js.update_progress = lambda *a, **k: None
    sys.modules["web_dashboard.services.job_service"] = js

    gs = types.ModuleType("web_dashboard.services.gcp_service")
    gs.ensure_cloudsql_rotation_prereqs = _fake_ensure_prereqs
    gs.create_cloudsql_user = _fake_create_cloudsql_user
    gs.delete_cloudsql_user = _fake_delete_cloudsql_user
    gs.list_cloudsql_users = _fake_list_cloudsql_users
    gs.write_regional_secret = _fake_write_regional_secret
    gs.delete_regional_secret = _fake_delete_regional_secret
    gs.execute_cloudsql_sql = _fake_execute_cloudsql_sql
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
    from web_dashboard.services import cloud_db_sql_service as sql
except Exception as exc:  # pragma: no cover -- skip if other app deps are missing
    try:
        import pytest
        pytest.skip(f"cloud_database_service import unavailable: {exc}",
                    allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)

DB_ID = "6583f505-4e96-4bc0-9d1a-0b2c3d4e5f60"
ADMIN_PW = "the-instance-admin-pw"


class _FakeDB:
    def commit(self):
        pass


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _reset(**conf):
    CONF.clear()
    LOGS.clear()
    CALLS.clear()
    USERS.clear()
    FAIL_DELETE.clear()
    CONF.update(conf)


def _deregister(meta=None, cloud="gcp", instance="clouddb-6583f505"):
    """Run the deregister-time drop with whatever the onboarding recorded."""
    row = _CloudDatabase(id=DB_ID, cloud=cloud, region="us-east1",
                         instance_id=instance, engine="sqlserver")
    stashed = {"ps_db_fa_db_user": svc._fa_db_user_name(DB_ID),
               "ps_db_fa_sm_project": "acme-proj"}
    stashed.update(meta or {})
    return _run(svc._drop_dedicated_fa_login(row=row, meta=stashed))


def _dropped():
    return [c[3] for c in CALLS if c[0] == "delete_user"]


def _onboard(engine="sqlserver", **tf):
    row = _CloudDatabase(id=DB_ID, cloud="gcp", region="us-east1",
                         instance_id="clouddb-6583f505", engine=engine, port=None,
                         private_host="10.0.0.5", db_name="app_db")
    tf_variables = {"project": "acme-proj", "master_username": "sqlserver",
                    "master_password": ADMIN_PW, "db_name": "app_db"}
    tf_variables.update(tf)
    return _run(svc._create_db_managed_user_gcp(
        _FakeDB(), row=row, log_job_id="job-1", engine=engine,
        tf_variables=tf_variables))


def _created():
    """(name, password) for every users.insert that carried a password."""
    return [(c[1], c[2]) for c in CALLS if c[0] == "create_user" and not c[3]]


# -- the name ----------------------------------------------------------------

def test_the_fa_login_is_derived_from_the_managed_user_name():
    """One derivation, not two spellings. sys.sql_logins then reads as "the managed
    account and its rotator", and neither name can drift from the other."""
    assert svc._fa_db_user_name(DB_ID) == f"{svc._managed_user_name(DB_ID)}_fa"
    assert svc._fa_db_user_name(DB_ID) == "psafe_6583f5054e96_fa"


def test_the_fa_login_can_never_collide_with_a_managed_user_name():
    """Disjoint BY LENGTH -- 18 against 21 -- which survives a later tidy-up of either
    prefix in a way "the prefixes differ" would not. Rotating the wrong principal is
    the failure this forecloses."""
    ids = [DB_ID, "00000000-0000-0000-0000-000000000000",
           "ffffffffffffffffffffffffffffffff", "abcdef0123456789abcd"]
    managed = {svc._managed_user_name(i) for i in ids}
    fa = {svc._fa_db_user_name(i) for i in ids}
    assert not (managed & fa)
    assert {len(n) for n in managed} == {18}
    assert {len(n) for n in fa} == {21}


def test_the_fa_login_is_a_legal_database_identifier():
    """It is interpolated into SQL by _fa_grant_statement, so it has to satisfy the same
    rule every dashboard-generated identifier does -- and fit SQL Server's sysname."""
    name = svc._fa_db_user_name(DB_ID)
    assert sql._IDENT_RE.match(name), name
    assert len(name) <= sql.max_identifier_length("sqlserver")
    # It also clears the tighter MySQL and PostgreSQL caps, so a forced-channel variant
    # of this path on another engine would not need a second naming scheme.
    assert len(name) <= sql.max_identifier_length("mysql")


def test_the_fa_login_is_not_spelled_like_an_operator_owned_account():
    """psfa_pg / psfa_mysql / psfa_mssql are the reference-mode convention. A
    dashboard-owned login that looked like one of those would invite someone to point
    clouddb_ps_functional_account_gcp_sqlserver at it."""
    assert not svc._fa_db_user_name(DB_ID).startswith("psfa_")


# -- the password ------------------------------------------------------------

def test_the_generated_password_carries_no_colon():
    """The dbgcp composite is ':'-delimited and the plugin splits it before it looks at
    anything, so one ':' mis-splits every credential action. generate_password's charset
    is what makes that structurally impossible."""
    for _ in range(200):
        assert ":" not in sql.generate_password()


def test_the_fa_password_is_minted_and_persisted():
    _reset()
    ctx = _onboard()
    assert ctx["fa_db_password"]
    assert CONF[f"clouddb/{DB_ID}/psfa"] == ctx["fa_db_password"]
    # The login was created WITH that password, not with a placeholder.
    assert (svc._fa_db_user_name(DB_ID), ctx["fa_db_password"]) in _created()


def test_the_fa_password_is_never_the_admin_password():
    """The point of the change. The admin credential stays the dashboard's."""
    _reset()
    ctx = _onboard()
    assert ctx["fa_db_password"] != ADMIN_PW
    assert CONF[f"clouddb/{DB_ID}/psfa"] != ADMIN_PW


def test_a_second_onboarding_REUSES_the_persisted_password():
    """The headline. create_functional_account_on_platform resolves a duplicate by
    (platform, account name, display name) -- all row-derived, so stable across attempts
    -- and returns the existing account WITHOUT touching its password. users.insert does
    reset the database's. So a freshly generated password on the second run would move
    the database and leave Password Safe holding the old one: every action then fails
    18456 -> 401 DB_AUTH_FAILED, with no remedy reachable from the UI.

    Both retry shapes come through here: the "functional account created, register
    failed" retry, and a whole deregister/re-register cycle (which deletes the account
    but not this key)."""
    _reset()
    first = _onboard()["fa_db_password"]
    CALLS.clear()
    second = _onboard()["fa_db_password"]
    assert second == first
    # and the second run pushed that same value at the database rather than a new one
    assert (svc._fa_db_user_name(DB_ID), first) in _created()
    # written once: the second run read it back instead of overwriting it
    assert not [c for c in CALLS
                if c[0] == "config_set" and c[1] == f"clouddb/{DB_ID}/psfa"], CALLS


def test_an_operator_staged_password_is_honoured_as_is():
    """Same mechanism, read from the other direction: whatever is in the store wins, so
    a credential recovered by hand after a failed run is not thrown away."""
    _reset(**{f"clouddb/{DB_ID}/psfa": "Recovered-by-hand-1"})
    ctx = _onboard()
    assert ctx["fa_db_password"] == "Recovered-by-hand-1"


# -- the grant follows the minted login --------------------------------------

def test_self_rotation_off_reports_the_grant_for_the_MINTED_login():
    """_apply_fa_grant_gcp only runs on data-api (executeSql), so on cloud-run the
    statement is reported for an operator -- and it must name the login the dashboard
    created, not the admin it used to borrow (which needed no grant, being the admin)."""
    _reset(clouddb_ps_self_rotation=False)
    _onboard()
    grants = [m for m in LOGS if "CustomerDbRootRole" in m]
    assert grants, LOGS
    assert f"[{svc._fa_db_user_name(DB_ID)}]" in grants[0], grants[0]
    assert "[sqlserver]" not in grants[0], grants[0]
    # Reported, never issued: there is no Data API on this channel to issue it with.
    assert not [c for c in CALLS if c[0] == "execute"], CALLS


def test_self_rotation_on_needs_no_grant_at_all():
    """The managed login alters itself with OLD_PASSWORD, so the functional account
    needs no privilege over it -- only the ability to sign in."""
    _reset(clouddb_ps_self_rotation=True)
    _onboard()
    assert not [m for m in LOGS if "rotation needs the following" in m], LOGS


def test_the_minted_login_is_named_on_the_job_either_way():
    """Whichever way self-rotation is set, the job has to say which login Password Safe
    signs in as: its password lives only in Password Safe, so an operator who cannot
    find the name cannot act on it at all."""
    for self_rotation in (True, False):
        _reset(clouddb_ps_self_rotation=self_rotation)
        _onboard()
        named = [m for m in LOGS if svc._fa_db_user_name(DB_ID) in m
                 and "dedicated login" in m]
        assert named, (self_rotation, LOGS)
        # and it says the admin credential was NOT shared
        assert "sqlserver" in named[0]


def test_the_reference_mode_prerequisite_reporter_is_not_used_here():
    """_report_fa_db_prereqs exists for the login an OPERATOR owns; every clause of its
    message ("which the dashboard cannot create -- it does not have that password") is
    false for one the dashboard just minted."""
    _reset()
    _onboard()
    assert not [m for m in LOGS if "the dashboard cannot create" in m], LOGS


# -- the drop on deregister --------------------------------------------------
#
# A DECOMMISSION needs none of this: the login dies with the instance, which is also why
# the psafe_* managed user has no drop there. A DEREGISTER leaves the database up, and
# the login exists only to serve a Password Safe rotation that no longer exists.

def test_a_deregister_drops_the_login_and_retires_its_password():
    _reset()
    _onboard()
    key = f"clouddb/{DB_ID}/psfa"
    assert CONF[key]
    said = _deregister()
    assert _dropped() == [svc._fa_db_user_name(DB_ID)]
    assert "was dropped too" in said, said
    # Dead the moment the login is — Password Safe's copy went with the functional
    # account, so nothing anywhere can use it.
    assert key not in CONF
    assert ("config_delete", key) in CALLS, CALLS


def test_the_managed_user_is_NOT_dropped_alongside_it():
    """It is what the PRA tunnel injects, and the database survives a deregister — so
    dropping it would break a tunnel the deregister was not asked to touch."""
    _reset()
    _onboard()
    _deregister()
    assert svc._managed_user_name(DB_ID) not in _dropped()


def test_only_the_login_THIS_dashboard_minted_is_ever_dropped():
    """A recorded fa_db_user is the built-in ADMIN on the data-api channel and the
    rotator IAM principal under IAM database auth. Dropping either would be the
    dashboard deleting a principal it does not own — one of them the instance's own
    administrator."""
    for recorded in ("sqlserver", "dbadmin", "bt-rotator@acme-proj.iam",
                     svc._managed_user_name(DB_ID), ""):
        _reset()
        said = _deregister(meta={"ps_db_fa_db_user": recorded})
        assert said == "", recorded
        assert _dropped() == [], (recorded, CALLS)


def test_a_non_gcp_row_is_left_alone():
    """AWS and Azure never mint a login — there is nothing of ours to drop, and their
    recorded fa_db_user is the minted admin."""
    _reset()
    said = _deregister(cloud="aws")
    assert said == "" and _dropped() == []


def test_an_already_gone_login_is_reported_as_such_not_as_a_failure():
    """users.delete returns False for a user (or instance) that is already gone, so a
    second deregister, or a login someone dropped by hand, converges quietly."""
    _reset()
    said = _deregister()          # nothing was ever created in this run
    assert "already gone" in said, said
    assert f"clouddb/{DB_ID}/psfa" not in CONF


def test_a_failed_drop_KEEPS_the_password_and_names_the_statement():
    """While the login survives, that key is the only record of its credential anywhere
    — Password Safe's copy went with the functional account. And the failure cannot be
    fatal: the deregister's Password Safe work has already succeeded, and a retry is
    refused with "no Password Safe onboarding recorded", so the statement to paste is
    the only thing an operator can act on."""
    _reset()
    _onboard()
    key = f"clouddb/{DB_ID}/psfa"
    FAIL_DELETE.append(RuntimeError("HTTP 403 permission denied"))
    said = _deregister()
    assert "could NOT be dropped" in said, said
    assert "permission denied" in said, said
    assert "can no longer be used or rotated" in said, said
    assert f"DROP LOGIN [{svc._fa_db_user_name(DB_ID)}]" in said, said
    assert CONF[key], "the credential's only remaining record was deleted"


def test_no_project_or_instance_is_reported_rather_than_guessed():
    _reset()
    said = _deregister(meta={"ps_db_fa_sm_project": ""}, instance="")
    assert "could NOT be dropped" in said, said
    assert "DROP LOGIN" in said, said
    assert _dropped() == [], CALLS


def test_the_drop_statement_is_guarded_so_a_paste_is_idempotent():
    """Reuses cloud_db_sql_service's builder rather than spelling DROP LOGIN here, so
    the remedy carries the same IF EXISTS guard the product path uses."""
    _reset()
    FAIL_DELETE.append(RuntimeError("boom"))
    said = _deregister()
    assert "IF EXISTS" in said, said


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
