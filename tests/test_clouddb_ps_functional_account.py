"""Cloud-DB Password Safe onboarding: where the functional account comes from.

The DB path used to be the odd one out. VM onboarding (``ps_vm_hook``) and k8s
ServiceAccount token rotation (``ps_k8s_token_service``) both *reference* an account the
operator created in BeyondInsight, via ``ps_api_service.get_functional_account``, and hold
no cloud secret in dashboard config. The DB path instead minted one functional account per
database, packing the operator's IAM access key / Azure SP secret into it, and deleted it on
decommission — which the feature's own runbook called "the most common wrong turn".

``clouddb_ps_functional_account_mode`` adds the missing half. These tests pin the parts that
are easy to break:

- ``reference`` mode RESOLVES and never creates, and the managed system takes the platform
  from the functional account (as the VM path does) rather than from ``clouddb_ps_platform_*``;
- the platform-name lookup is skipped entirely in ``reference`` mode, so a stale platform
  field cannot block onboarding;
- **a referenced account is never stashed under ``ps_db_functional_account_id``**. That key
  is the only thing driving the decommission delete loop, so stashing it would make teardown
  delete an operator-owned account shared by every database;
- the mode is an explicit choice: a blank name in ``reference`` mode raises instead of
  silently falling through to ``create``, because this feature already has enough silent
  fallbacks (two-of-three IAM fields, the blank-PEM key drop).

Runs under pytest, or standalone:  python tests/test_clouddb_ps_functional_account.py
"""
import asyncio
import os
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

CONF = {}
CALLS = []


class _Settings:
    def __getattr__(self, _key):
        return ""


class _CloudDatabase:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Job:
    id = None


class _FakeJobRow:
    def __init__(self):
        self.id = "job-1"
        self.metadata_dict = {}


# ── fake Password Safe API ────────────────────────────────────────────────────

FAKE_FA = {"id": 77, "platform_id": 909,
           "platform_name": "psql SSM Custom Plugin", "account_name": "clouddb-ssm-postgres"}


async def _fake_get_functional_account(name):
    CALLS.append(("get_functional_account", name))
    if name not in ("clouddb-ssm-postgres", "pra-config-api"):
        raise AssertionError(f"unexpected functional account lookup {name!r}")
    return dict(FAKE_FA)


async def _fake_create_fa_on_platform(*, platform_id, account_name, display_name,
                                      password, description=""):
    CALLS.append(("create", platform_id, account_name, password))
    return 4242


async def _fake_get_platform_id(name):
    CALLS.append(("get_platform_id", name))
    return 111


async def _fake_get_workgroup_id(name):
    return 5


async def _fake_register_managed_system(**kw):
    CALLS.append(("register", kw["functional_account_id"], kw["platform_id"]))
    return {"tf_state_json": "{}", "managed_system_id": 1, "managed_account_id": 2}


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

    ps = types.ModuleType("web_dashboard.services.ps_api_service")
    ps.get_functional_account = _fake_get_functional_account
    ps.create_functional_account_on_platform = _fake_create_fa_on_platform
    ps.get_platform_id = _fake_get_platform_id
    ps.get_workgroup_id = _fake_get_workgroup_id
    sys.modules["web_dashboard.services.ps_api_service"] = ps

    psr = types.ModuleType("web_dashboard.services.ps_resource_service")
    psr.register_managed_system = _fake_register_managed_system
    psr.PSResourceError = type("PSResourceError", (Exception,), {})
    sys.modules["web_dashboard.services.ps_resource_service"] = psr

    # ps_vm_hook is deliberately NOT stubbed: the helper imports it lazily and must use
    # the real _platform_name_ok, since reusing that check is half the point.
    for name in ("terraform", "terraform_provider_env", "job_service"):
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


class _Query:
    def __init__(self, row):
        self._row = row

    def filter(self, *_a, **_k):
        return self

    def first(self):
        return self._row


class _FakeDB:
    def __init__(self, job_row):
        self._job = job_row

    def query(self, _model):
        return _Query(self._job)

    def commit(self):
        pass


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _reset(**conf):
    CONF.clear()
    CALLS.clear()
    CONF.update(conf)


def _resolve(**kw):
    kw.setdefault("config_key", "clouddb_ps_functional_account_postgres")
    kw.setdefault("platform_id", 0)
    kw.setdefault("platform_tokens", ("ssm",))
    kw.setdefault("label", "ssm")
    kw.setdefault("create", {"account_name": "iam-user", "display_name": "d",
                             "password": "AKIA:secret", "description": ""})
    return _run(svc._resolve_db_functional_account(**kw))


# ── the mode itself ───────────────────────────────────────────────────────────

def test_mode_defaults_to_create():
    _reset()
    assert svc._ps_fa_mode() == "create"


def test_mode_is_case_and_whitespace_tolerant():
    _reset(clouddb_ps_functional_account_mode="  Reference  ")
    assert svc._ps_fa_mode() == svc._FA_MODE_REFERENCE


# ── create mode: unchanged legacy behaviour ───────────────────────────────────

def test_create_mode_mints_an_account_and_owns_it():
    _reset()
    fa_id, platform_id, owned = _resolve(platform_id=111)
    assert (fa_id, platform_id, owned) == (4242, 111, True)
    assert ("create", 111, "iam-user", "AKIA:secret") in CALLS
    assert not any(c[0] == "get_functional_account" for c in CALLS)


# ── reference mode: resolve, never create ─────────────────────────────────────

def test_reference_mode_resolves_and_never_creates():
    _reset(clouddb_ps_functional_account_mode="reference",
           clouddb_ps_functional_account_postgres="clouddb-ssm-postgres")
    fa_id, platform_id, owned = _resolve()
    assert (fa_id, owned) == (77, False)
    # The platform comes from the functional account, not from clouddb_ps_platform_*.
    assert platform_id == 909
    assert ("get_functional_account", "clouddb-ssm-postgres") in CALLS
    assert not any(c[0] == "create" for c in CALLS), "reference mode must not POST an account"


def test_reference_mode_blank_name_raises_and_names_the_key():
    _reset(clouddb_ps_functional_account_mode="reference")
    try:
        _resolve()
    except svc.CloudDatabaseError as exc:
        # Never a silent fall-through to create mode.
        assert "clouddb_ps_functional_account_postgres" in str(exc)
        assert not any(c[0] == "create" for c in CALLS)
    else:
        raise AssertionError("a blank name in reference mode must raise")


def test_reference_mode_rejects_an_account_on_the_wrong_platform():
    _reset(clouddb_ps_functional_account_mode="reference",
           clouddb_ps_functional_account_postgres="clouddb-ssm-postgres")
    try:
        # An Azure Run Command account pointed at the AWS field: the managed system would
        # silently land on the wrong platform, because it inherits the account's.
        _resolve(platform_tokens=("azure", "run command"))
    except svc.CloudDatabaseError as exc:
        assert "clouddb_ps_functional_account_postgres" in str(exc)
        assert "psql SSM Custom Plugin" in str(exc)
    else:
        raise AssertionError("a platform mismatch must raise")


def test_reference_mode_fails_open_on_an_unknown_platform_name():
    # _platform_name_ok treats a blank name as "lookup failed, do not block" — the DB path
    # inherits that rather than hard-failing on a best-effort reverse lookup.
    _reset(clouddb_ps_functional_account_mode="reference",
           clouddb_ps_functional_account_postgres="clouddb-ssm-postgres")

    async def _blank_platform(name):
        CALLS.append(("get_functional_account", name))
        return dict(FAKE_FA, platform_name="")

    sys.modules["web_dashboard.services.ps_api_service"].get_functional_account = _blank_platform
    try:
        fa_id, _platform_id, owned = _resolve()
    finally:
        sys.modules["web_dashboard.services.ps_api_service"].get_functional_account = \
            _fake_get_functional_account
    assert (fa_id, owned) == (77, False)


# ── the teardown-safety half, through the real onboarding entry point ─────────

def _onboard(**conf):
    _reset(**conf)
    job_row = _FakeJobRow()
    row = _CloudDatabase(id="abcdef0123456789abcd", cloud="aws",
                         private_host="db.internal", engine="postgres")
    ctx = {"managed_user": "psafe_abcdef012345", "jump_host_id": "i-123", "region": "us-east-1",
           "admin_username": "dbadmin", "client_image": "postgres:16", "db_name": "appdb",
           "port": 5432}
    _run(svc._onboard_ps_managed_systems(
        _FakeDB(job_row), row=row, job_id="job-1", engine="postgres",
        tf_variables={"identifier": "clouddb-abcdef01"}, ctx=ctx))
    return job_row.metadata_dict


def test_a_referenced_account_is_never_stashed_under_the_deleting_key():
    meta = _onboard(clouddb_ps_functional_account_mode="reference",
                    clouddb_ps_functional_account_postgres="clouddb-ssm-postgres")
    # decommission's delete loop is driven purely by the presence of this key. An
    # operator-owned account shared by every database must survive every teardown.
    assert "ps_db_functional_account_id" not in meta
    assert meta["ps_db_functional_account_ref"] == 77


def test_a_minted_account_is_still_stashed_for_deletion():
    meta = _onboard()
    assert meta["ps_db_functional_account_id"] == 4242
    assert "ps_db_functional_account_ref" not in meta


def test_reference_mode_skips_the_platform_name_lookup():
    # get_platform_id raises on an unknown name, so resolving a platform field the runbook
    # tells you to leave blank would be a needless way to fail onboarding.
    _onboard(clouddb_ps_functional_account_mode="reference",
             clouddb_ps_functional_account_postgres="clouddb-ssm-postgres")
    assert not any(c[0] == "get_platform_id" for c in CALLS)


def test_create_mode_still_resolves_the_configured_platform():
    # settings is stubbed here, so name the platform explicitly rather than leaning on
    # the config.py default: what matters is that create mode reads the configured name.
    _onboard(clouddb_ps_platform_postgres="psql SSM Custom Plugin")
    assert ("get_platform_id", "psql SSM Custom Plugin") in CALLS


def test_the_managed_system_is_registered_against_the_referenced_account():
    _onboard(clouddb_ps_functional_account_mode="reference",
             clouddb_ps_functional_account_postgres="clouddb-ssm-postgres")
    assert ("register", 77, 909) in CALLS, "the system must inherit the account's platform"


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
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
