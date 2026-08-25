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
# The GCP siblings, on the Cloud SQL plugin platforms. "clouddb-gcp-wrong" sits on a
# platform whose name carries neither token, so it exercises the mismatch guard.
FAKE_FA_GCP = {"id": 88, "platform_id": 808,
               "platform_name": "GCP Cloud SQL PostgreSQL",
               "account_name": "clouddb-gcp-postgres"}
FAKE_FA_GCP_MYSQL = dict(FAKE_FA_GCP, id=89, platform_id=807,
                         platform_name="GCP Cloud SQL MySQL",
                         account_name="clouddb-gcp-mysql")
FAKE_FA_WRONG = dict(FAKE_FA_GCP, id=90, platform_name="Azure Active Directory",
                     account_name="clouddb-gcp-wrong")
_FAKE_ACCOUNTS = {
    "clouddb-ssm-postgres": FAKE_FA,
    "pra-config-api": FAKE_FA,
    "clouddb-gcp-postgres": FAKE_FA_GCP,
    "clouddb-gcp-mysql": FAKE_FA_GCP_MYSQL,
    "clouddb-gcp-mssql": dict(FAKE_FA_GCP, id=91, platform_id=806,
                              platform_name="GCP Cloud SQL SQL Server",
                              account_name="clouddb-gcp-mssql"),
    "clouddb-gcp-wrong": FAKE_FA_WRONG,
}


async def _fake_get_functional_account(name):
    CALLS.append(("get_functional_account", name))
    if name not in _FAKE_ACCOUNTS:
        raise AssertionError(f"unexpected functional account lookup {name!r}")
    return dict(_FAKE_ACCOUNTS[name])


async def _fake_create_fa_on_platform(*, platform_id, account_name, display_name,
                                      password, description=""):
    CALLS.append(("create", platform_id, account_name, password))
    return 4242


async def _fake_get_platform_id(name):
    CALLS.append(("get_platform_id", name))
    return 111


async def _fake_get_workgroup_id(name):
    return 5


LAST_REGISTER = {}


async def _fake_register_managed_system(**kw):
    CALLS.append(("register", kw["functional_account_id"], kw["platform_id"]))
    LAST_REGISTER.clear()
    LAST_REGISTER.update(kw)
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
    LAST_REGISTER.clear()
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

_AWS_PORTS = {"postgres": 5432, "mysql": 3306, "sqlserver": 1433}


def _onboard(engine="postgres", **conf):
    # The certPath address segment is required (a blank one is refused up front), so
    # default it the way an operator must; a test proving the refusal passes "".
    conf.setdefault("clouddb_ps_ssm_public_key_path", r"C:\Utils\public_ssm.pem")
    _reset(**conf)
    job_row = _FakeJobRow()
    row = _CloudDatabase(id="abcdef0123456789abcd", cloud="aws",
                         private_host="db.internal", engine=engine)
    ctx = {"managed_user": "psafe_abcdef012345",
           "jump_host_id": "i-0eaa6a10886717ed", "region": "us-east-1",
           "admin_username": "sqladmin" if engine == "sqlserver" else "dbadmin",
           "client_image": "postgres:16",
           "db_name": "master" if engine == "sqlserver" else "appdb",
           "port": _AWS_PORTS[engine]}
    _run(svc._onboard_ps_managed_systems(
        _FakeDB(job_row), row=row, job_id="job-1", engine=engine,
        tf_variables={"identifier": "clouddb-abcdef01",
                      "master_password": "s3cr3t-admin-pw"}, ctx=ctx))
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



# -- the GCP branch (dbgcp = "GCP Cloud SQL {engine}", data-api channel) -------
#
# The third Layer-2 cloud, and the one shaped differently from the other two: the plugin
# reaches a private-IP instance over Google's control plane, so the address carries no
# host, no cert path and no key material, and under IAM database authentication the
# functional-account composite carries no database password either.

_GCP_PORTS = {"postgres": 5432, "mysql": 3306, "sqlserver": 1433}


def _onboard_gcp(engine="postgres", channel=None, **conf):
    _reset(**conf)
    job_row = _FakeJobRow()
    row = _CloudDatabase(id="abcdef0123456789abcd", cloud="gcp",
                         private_host="10.102.0.3", engine=engine,
                         region="us-central1", instance_id="clouddb-abcdef01")
    channel = channel or ("cloud-run" if engine == "sqlserver" else "data-api")
    ctx = {"managed_user": "psafe_abcdef012345", "region": "us-central1",
           "admin_username": "sqlserver" if engine == "sqlserver" else "dbadmin",
           "client_image": "", "db_name": "master" if engine == "sqlserver" else "appdb",
           "port": _GCP_PORTS[engine],
           "project": "acme-data-prod", "instance": "clouddb-abcdef01",
           "channel": channel,
           "fa_db_user": ("sqlserver" if channel == "cloud-run"
                          else "bt-rotator@acme-data-prod.iam"),
           "managed_user_host": "%" if engine == "mysql" else ""}
    # run_provision_apply re-injects the admin password into tf_variables before it
    # reaches here, which is where the cloud-run functional account's database password
    # comes from in create mode. (On the post-hoc path tf_variables is the SECRET-STRIPPED
    # copy off the job, and the same value is read from the secrets backend instead.)
    _run(svc._onboard_ps_managed_systems(
        _FakeDB(job_row), row=row, job_id="job-1", engine=engine,
        tf_variables={"identifier": "clouddb-abcdef01",
                      "master_password": "s3cr3t-admin-pw"}, ctx=ctx))
    return job_row.metadata_dict


def test_gcp_address_is_the_data_api_five_field_form():
    _onboard_gcp(clouddb_ps_platform_gcp_postgres="GCP Cloud SQL PostgreSQL")
    addr = LAST_REGISTER["dns_name"]
    fields = addr.split(";")
    assert fields[0] == "data-api", addr
    # Field 2 is the instance CONNECTION NAME, not a hostname -- project:region:instance,
    # composed from columns the row already has rather than a new terraform output.
    assert fields[1] == "acme-data-prod:us-central1:clouddb-abcdef01", addr
    assert fields[2] == "appdb", addr
    # Both control-plane channels reject an audience or an SSL flag: they are always TLS
    # and never open a database connection.
    assert fields[3] == "-" and fields[4] == "-", addr
    assert "iam=true" in fields[5:], addr
    assert LAST_REGISTER["method"] == "dbgcp"


def test_gcp_mysql_address_carries_the_host_qualifier():
    # The plugin deliberately REFUSES to default a MySQL host, because app@% and
    # app@10.0.0.5 are different accounts and rotating the wrong row is silent. The
    # dashboard created '<user>'@'%', so the address has to say so.
    _onboard_gcp(engine="mysql", clouddb_ps_platform_gcp_mysql="GCP Cloud SQL MySQL")
    assert "host=%" in LAST_REGISTER["dns_name"].split(";"), LAST_REGISTER["dns_name"]


def test_gcp_postgres_address_has_no_host_option():
    # host= applies to MySQL only; the plugin rejects an option it does not recognise
    # for the engine rather than ignoring it.
    _onboard_gcp(clouddb_ps_platform_gcp_postgres="GCP Cloud SQL PostgreSQL")
    assert "host=" not in LAST_REGISTER["dns_name"], LAST_REGISTER["dns_name"]


def _real_dbgcp_validator():
    """The REAL _validate_dbgcp_dns_name, lifted out of ps_resource_service by source.

    That module is stubbed in this file (the whole point is to drive the onboarding with
    no Terraform), so a normal import returns the fake. But the one thing worth proving
    end to end here is that the address this service BUILDS is an address that validator
    ACCEPTS — the two halves live in different modules and nothing else pins them
    together. Exec'ing just the grammar block keeps that check honest without unstubbing.
    """
    import re as _re
    src_path = os.path.join(_ROOT, "web_dashboard", "services", "ps_resource_service.py")
    with open(src_path, encoding="utf-8") as fh:
        src = fh.read()
    block = src[src.index("_DBGCP_MAX_ADDRESS = 249"):src.index("def _ssm_account_name")]

    class _Err(Exception):
        pass

    ns = {"re": _re, "PSResourceError": _Err}
    exec(block, ns)  # noqa: S102 - test-local, reads our own source
    return ns["_validate_dbgcp_dns_name"], _Err


def test_the_address_the_service_builds_is_one_the_plugin_grammar_accepts():
    validate, Err = _real_dbgcp_validator()
    for engine in ("postgres", "mysql"):
        _onboard_gcp(engine=engine,
                     **{f"clouddb_ps_platform_gcp_{engine}": "GCP Cloud SQL X"})
        addr = LAST_REGISTER["dns_name"]
        try:
            validate(addr)
        except Err as exc:
            raise AssertionError(f"{engine}: the service built {addr!r}, which the "
                                 f"plugin grammar rejects: {exc}")


def test_gcp_address_survives_the_plugins_249_character_limit():
    # ps_resource_service is stubbed here, so the limit is spelled out rather than read
    # from it; test_ps_resource.py pins the constant itself. 249 not 255, because a
    # truncated address does not error -- it becomes a different, wrong address.
    _onboard_gcp(clouddb_ps_platform_gcp_postgres="GCP Cloud SQL PostgreSQL")
    addr = LAST_REGISTER["dns_name"]
    assert len(addr) <= 249, (len(addr), addr)


def test_gcp_create_mode_composite_carries_no_database_password():
    # Under IAM database auth there is nothing to store, rotate or leak: segment 3 is
    # "-". This is the whole reason "reference" mode is the natural GCP configuration --
    # unlike Azure, the composite holds nothing per-database.
    _onboard_gcp(clouddb_ps_platform_gcp_postgres="GCP Cloud SQL PostgreSQL")
    created = [c for c in CALLS if c[0] == "create"]
    assert created, CALLS
    _, _, account_name, password = created[0]
    assert account_name == "ADC:bt-rotator@acme-data-prod.iam", account_name
    assert password == "-:-:-", password


def test_gcp_imp_mode_puts_the_target_in_the_second_segment():
    _onboard_gcp(clouddb_ps_platform_gcp_postgres="GCP Cloud SQL PostgreSQL",
                 clouddb_ps_gcp_auth_mode="IMP",
                 clouddb_ps_gcp_impersonate_target="bt-rotator@acme.iam.gserviceaccount.com")
    _, _, account_name, password = [c for c in CALLS if c[0] == "create"][0]
    assert account_name.startswith("IMP:"), account_name
    assert password == "-:bt-rotator@acme.iam.gserviceaccount.com:-", password


def test_gcp_reference_mode_inherits_the_accounts_platform():
    meta = _onboard_gcp(clouddb_ps_functional_account_mode="reference",
                        clouddb_ps_functional_account_gcp_postgres="clouddb-gcp-postgres")
    assert ("register", 88, 808) in CALLS, CALLS
    assert meta["ps_db_functional_account_ref"] == 88
    assert "ps_db_functional_account_id" not in meta


def test_gcp_reference_mode_rejects_an_account_on_the_wrong_platform():
    # The token check is ("gcp", "cloud sql"); a Password Safe account on some other
    # platform would register a managed system the plugin never sees.
    try:
        _onboard_gcp(clouddb_ps_functional_account_mode="reference",
                     clouddb_ps_functional_account_gcp_postgres="clouddb-gcp-wrong")
        raise AssertionError("expected a wrong-platform functional account to be refused")
    except svc.CloudDatabaseError:
        pass


def test_gcp_never_self_rotates_even_when_the_global_flag_is_on():
    # Self-rotation is a CLOUD-RUN action the data-api channel refuses at pre-flight,
    # but clouddb_ps_self_rotation is one global flag that AWS/Azure "reference" mode
    # REQUIRES -- so turning it on for those clouds must not reach GCP.
    _onboard_gcp(clouddb_ps_platform_gcp_postgres="GCP Cloud SQL PostgreSQL",
                 clouddb_ps_self_rotation=True)
    assert LAST_REGISTER["use_own_credentials"] is False, LAST_REGISTER


def test_the_other_clouds_still_self_rotate_when_the_flag_is_on():
    # The guard above must be scoped to dbgcp, not applied to every DB plugin.
    _onboard(clouddb_ps_self_rotation=True)
    assert LAST_REGISTER["use_own_credentials"] is True, LAST_REGISTER



# -- the cloud-run channel (SQL Server) ---------------------------------------
#
# The only channel that can verify a managed account's password or let one rotate
# itself, and the only one Cloud SQL for SQL Server can use at all -- it has no IAM
# database authentication, so there is no passwordless option.

def test_sqlserver_takes_the_cloud_run_channel_and_carries_the_audience():
    _onboard_gcp(engine="sqlserver",
                 clouddb_ps_platform_gcp_sqlserver="GCP Cloud SQL SQL Server",
                 clouddb_ps_gcp_dbops_audience="https://bt-dbops.acme.internal")
    fields = LAST_REGISTER["dns_name"].split(";")
    assert fields[0] == "cloud-run", fields
    assert fields[1] == "acme-data-prod:us-central1:clouddb-abcdef01", fields
    # SQL Server's admin session catalog is master, not the user database.
    assert fields[2] == "master", fields
    # Field 4 is the STABLE CUSTOM AUDIENCE, used verbatim as both the request target
    # and the token audience -- a *.run.app hostname changes when the service is
    # recreated and every revision gets its own URL.
    assert fields[3] == "https://bt-dbops.acme.internal", fields
    assert fields[4] == "sslTRUE", fields
    assert LAST_REGISTER["port"] == 1433


def test_the_ssl_flag_is_a_real_toggle_on_cloud_run():
    _onboard_gcp(engine="sqlserver",
                 clouddb_ps_platform_gcp_sqlserver="GCP Cloud SQL SQL Server",
                 clouddb_ps_gcp_dbops_audience="https://bt-dbops.acme.internal",
                 clouddb_ps_gcp_dbops_ssl=False)
    assert LAST_REGISTER["dns_name"].split(";")[4] == "sslFALSE"


def test_cloud_run_create_mode_carries_a_real_database_password():
    # No IAM database auth exists for SQL Server, so segment 3 cannot be "-" the way it
    # is on data-api. In create mode it is this database's own admin credential -- which
    # makes the composite PER-DATABASE, exactly the property that makes reference mode
    # the better answer here, as it is on Azure.
    _onboard_gcp(engine="sqlserver",
                 clouddb_ps_platform_gcp_sqlserver="GCP Cloud SQL SQL Server",
                 clouddb_ps_gcp_dbops_audience="https://bt-dbops.acme.internal")
    _, _, account_name, password = [c for c in CALLS if c[0] == "create"][0]
    assert account_name == "ADC:sqlserver", account_name
    segments = password.split(":", 2)
    assert segments[0] == "-" and segments[1] == "-", password
    assert segments[2] == "s3cr3t-admin-pw", password


def test_cloud_run_honours_self_rotation_where_data_api_refuses_it():
    # The same global flag, opposite answers, because the channels differ in what they
    # can do -- not because GCP is special. cloud-run logs in AS the managed account
    # (ALTER LOGIN ... OLD_PASSWORD), which needs no privilege over the target at all.
    _onboard_gcp(engine="sqlserver",
                 clouddb_ps_platform_gcp_sqlserver="GCP Cloud SQL SQL Server",
                 clouddb_ps_gcp_dbops_audience="https://bt-dbops.acme.internal",
                 clouddb_ps_self_rotation=True)
    assert LAST_REGISTER["use_own_credentials"] is True

    _onboard_gcp(engine="postgres",
                 clouddb_ps_platform_gcp_postgres="GCP Cloud SQL PostgreSQL",
                 clouddb_ps_self_rotation=True)
    assert LAST_REGISTER["use_own_credentials"] is False


def test_the_channel_override_moves_postgres_onto_cloud_run():
    _onboard_gcp(engine="postgres", channel="cloud-run",
                 clouddb_ps_platform_gcp_postgres="GCP Cloud SQL PostgreSQL",
                 clouddb_ps_gcp_channel="cloud-run",
                 clouddb_ps_gcp_dbops_audience="https://bt-dbops.acme.internal")
    fields = LAST_REGISTER["dns_name"].split(";")
    assert fields[0] == "cloud-run", fields
    assert fields[3] == "https://bt-dbops.acme.internal", fields
    # iam=true is a data-api option and must not ride along.
    assert not any(f.startswith("iam=") for f in fields), fields


def test_every_cloud_run_address_the_service_builds_parses():
    validate, Err = _real_dbgcp_validator()
    for engine in ("postgres", "mysql", "sqlserver"):
        _onboard_gcp(engine=engine, channel="cloud-run",
                     clouddb_ps_gcp_channel="cloud-run",
                     clouddb_ps_gcp_dbops_audience="https://bt-dbops.acme.internal",
                     **{f"clouddb_ps_platform_gcp_{engine}": "GCP Cloud SQL X"})
        addr = LAST_REGISTER["dns_name"]
        try:
            validate(addr)
        except Err as exc:
            raise AssertionError(f"{engine}: the service built {addr!r}, which the "
                                 f"plugin grammar rejects: {exc}")


def test_sqlserver_reference_mode_inherits_the_cloud_sql_platform():
    meta = _onboard_gcp(engine="sqlserver",
                        clouddb_ps_functional_account_mode="reference",
                        clouddb_ps_functional_account_gcp_sqlserver="clouddb-gcp-mssql",
                        clouddb_ps_gcp_dbops_audience="https://bt-dbops.acme.internal")
    assert ("register", 91, 806) in CALLS, CALLS
    assert meta["ps_db_functional_account_ref"] == 91



# -- the AWS branch (dbssm = "{engine} SSM Custom Plugin") ----------------------
#
# The vendor plugins (v24.2.x) index the ';'-packed address at fixed PER-ENGINE
# positions -- mssql has no database segment, mysql alone carries a trailing ssl flag
# -- and split the functional-account username/password on ':' BEFORE looking at the
# auth mode. These pin the composed shapes against that parse, including the two
# crashes the old composition shipped at every rotation: a "local" assumeRole segment
# (shorter than the plugin's Substring(0,12)) and a two-part functional-account
# password.

def _real_dbssm_validator():
    """The REAL _validate_dbssm_dns_name, lifted out of ps_resource_service by source
    -- same rationale and mechanism as _real_dbgcp_validator above: that module is
    stubbed here, but the address this service BUILDS must be one the validator
    ACCEPTS, and nothing else pins the two modules together."""
    import re as _re
    src_path = os.path.join(_ROOT, "web_dashboard", "services", "ps_resource_service.py")
    with open(src_path, encoding="utf-8") as fh:
        src = fh.read()
    block = src[src.index("_DBSSM_SEGMENT_ENGINES"):
                src.index("# ── GCP Cloud SQL address grammar")]

    class _Err(Exception):
        pass

    ns = {"re": _re, "PSResourceError": _Err}
    exec(block, ns)  # noqa: S102 - test-local, reads our own source
    return ns["_validate_dbssm_dns_name"], _Err


def test_aws_postgres_address_is_the_six_field_form():
    _onboard(clouddb_ps_platform_postgres="psql SSM Custom Plugin")
    fields = LAST_REGISTER["dns_name"].split(";")
    assert fields == ["i-0eaa6a10886717ed", "us-east-1", "db.internal", "appdb",
                      r"C:\Utils\public_ssm.pem", "NoAssumeRole"], fields
    assert LAST_REGISTER["method"] == "dbssm"
    assert LAST_REGISTER["port"] == 5432


def test_aws_sqlserver_address_has_no_database_segment():
    # mssql actions always land in master and the plugin's 5-position parse has no
    # slot for a catalog -- packing one shifts certPath into the assumeRole position.
    _onboard(engine="sqlserver", clouddb_ps_platform_sqlserver="mssql SSM Custom Plugin")
    fields = LAST_REGISTER["dns_name"].split(";")
    assert len(fields) == 5, fields
    assert "master" not in fields, fields
    assert fields[3] == r"C:\Utils\public_ssm.pem", fields
    assert LAST_REGISTER["port"] == 1433


def test_aws_mysql_address_carries_the_trailing_ssl_flag():
    # mysql is the only engine with an ssl segment, and only the literal sslTRUE
    # enables TLS -- the flag must be a real toggle, in canonical spelling.
    _onboard(engine="mysql", clouddb_ps_platform_mysql="mysql SSM Custom Plugin")
    assert LAST_REGISTER["dns_name"].split(";")[6] == "sslTRUE"
    _onboard(engine="mysql", clouddb_ps_platform_mysql="mysql SSM Custom Plugin",
             clouddb_ps_ssm_ssl=False)
    assert LAST_REGISTER["dns_name"].split(";")[6] == "sslFALSE"


def test_aws_addresses_the_service_builds_pass_the_plugin_grammar():
    validate, Err = _real_dbssm_validator()
    for engine in ("postgres", "mysql", "sqlserver"):
        _onboard(engine=engine,
                 **{f"clouddb_ps_platform_{engine}": "X SSM Custom Plugin"})
        addr = LAST_REGISTER["dns_name"]
        try:
            validate(addr)
        except Err as exc:
            raise AssertionError(f"{engine}: the service built {addr!r}, which the "
                                 f"plugin grammar rejects: {exc}")


def test_aws_short_assume_role_is_coerced_not_shipped():
    # "local" was this key's own pre-spec default, and anything under 12 characters
    # crashes the plugin's Substring(0,12) at every action -- rows persisted under the
    # old default are healed on read rather than crashed at the first rotation.
    _onboard(clouddb_ps_ssm_account_suffix="local")
    assert LAST_REGISTER["dns_name"].split(";")[5] == "NoAssumeRole"


def test_aws_a_real_assume_role_arn_is_kept():
    arn = "arn:aws:iam::123456789012:role/psafe-broker"
    _onboard(clouddb_ps_ssm_account_suffix=arn)
    assert LAST_REGISTER["dns_name"].split(";")[5] == arn


def test_aws_ec2_mode_packs_mode_admin_user_and_placeholder_keys():
    # The plugin splits the FA password into three ':'-parts BEFORE the mode check, so
    # EC2 mode still ships the two placeholders; part 3 is the DB admin credential
    # Verify FA logs in with (the old composition was a bare random token, which
    # mis-split at every action).
    _onboard()
    _, _, account_name, password = [c for c in CALLS if c[0] == "create"][0]
    assert account_name == "EC2:dbadmin", account_name
    assert password == "x:x:s3cr3t-admin-pw", password


def test_aws_iam_mode_is_selected_by_the_key_pair_alone():
    # The IAM username never reaches the plugin, so it plays no part in mode selection
    # (the old code required all three fields and silently fell back to EC2 on two).
    _onboard(clouddb_ps_ssm_access_key_id="AKIAXXXX",
             clouddb_ps_ssm_secret_access_key="sekret")
    _, _, account_name, password = [c for c in CALLS if c[0] == "create"][0]
    assert account_name == "IAM:dbadmin", account_name
    assert password == "AKIAXXXX:sekret:s3cr3t-admin-pw", password


def test_aws_a_colon_in_fa_material_is_refused():
    # ':' is the plugin's functional-account field delimiter; a value carrying one
    # would mis-split at every verify/change action, silently.
    try:
        svc._dbssm_fa_fields(admin_user="dbadmin", admin_password="a:b",
                             access_key_id="", secret_access_key="")
        raise AssertionError("expected a ':' in the DB admin password to be refused")
    except svc.CloudDatabaseError as exc:
        assert "':'" in str(exc)


def test_aws_registration_defers_the_ip_to_the_resource_service():
    # The register call picks no ip at all: register_managed_system owns the per-plugin
    # fill -- the packed address itself for dbssm (its platform refuses a create with no
    # IPAddress, "The field 'IPAddress' is required.", while its plugins crash parsing a
    # bare one) and the 127.0.0.1 placeholder for dbazure/dbgcp, whose plugins skip
    # non-parsing candidates. test_ps_resource pins the fills themselves; this pins that
    # the caller leaves the choice there instead of re-encoding it per cloud -- the two
    # copies of that rule are how the live "IPAddress is required" rejection happened.
    _onboard()
    assert "ip_address" not in LAST_REGISTER, LAST_REGISTER
    _onboard_gcp(clouddb_ps_platform_gcp_postgres="GCP Cloud SQL PostgreSQL")
    assert "ip_address" not in LAST_REGISTER, LAST_REGISTER


def test_aws_blank_cert_path_fails_loudly_before_any_object_is_created():
    # The old composition packed ';;' silently and the plugin failed at the first
    # rotation; now onboarding refuses up front, naming the config key -- and before
    # the functional account is minted, so the refusal strands nothing remote.
    try:
        _onboard(clouddb_ps_ssm_public_key_path="")
        raise AssertionError("expected a blank public-key path to be refused")
    except svc.CloudDatabaseError as exc:
        assert "clouddb_ps_ssm_public_key_path" in str(exc)
    assert not any(c[0] == "create" for c in CALLS), CALLS


def _gate(engine, **conf):
    _reset(pscli_api_url="https://ps.example/BeyondTrust/api/public/v3",
           pscli_client_id="cid", pscli_client_secret="sec",
           clouddb_ps_onboarding_enabled=True,
           passwordsafe_gcp_db_registration_method="dataapi", **conf)
    return svc._ps_db_onboarding_enabled(
        _CloudDatabase(id="d", cloud="gcp", engine=engine))


def test_sql_server_stays_off_until_the_cloud_run_audience_is_configured():
    # Not a structural refusal any more, but there is no address to build without the
    # audience -- field 4 IS the audience, and it is what the OIDC token is minted for.
    # Off is the honest answer; half-configured would fail at the first rotation.
    assert _gate("sqlserver") is False
    assert _gate("sqlserver",
                 clouddb_ps_gcp_dbops_audience="https://bt-dbops.acme.internal") is True


def test_postgres_needs_no_audience_because_data_api_needs_no_service():
    assert _gate("postgres") is True
    assert _gate("mysql") is True


def test_forcing_every_engine_onto_cloud_run_makes_the_audience_universal():
    assert _gate("postgres", clouddb_ps_gcp_channel="cloud-run") is False
    assert _gate("postgres", clouddb_ps_gcp_channel="cloud-run",
                 clouddb_ps_gcp_dbops_audience="https://bt-dbops.acme.internal") is True


def test_an_unknown_engine_is_still_refused():
    assert _gate("oracle") is False


# ── staging the plugin key material on the jump host ─────────────────────────
#
# The AWS side used to be manual ("the dashboard does not stage the AWS key material"),
# so an operator had to place private.pem/passphrase.txt in the jump host's key
# directory by hand. It now mirrors the Azure drop. Both halves fail the same way when
# absent -- provisioning goes green and only the FIRST ROTATION fails to decrypt -- so
# what these pin is that the commands are actually emitted, and that a blank config is
# a logged no-op rather than a silent one.

def test_ssm_prep_drops_the_key_material_with_tight_permissions():
    _reset(clouddb_ps_ssm_plugin_private_key="-----BEGIN ENCRYPTED PRIVATE KEY-----\nabc\n-----END ENCRYPTED PRIVATE KEY-----",
           clouddb_ps_ssm_plugin_passphrase="s3cret",
           clouddb_ps_ssm_key_directory="/home/ssm-user")
    cmds = svc._ssm_jump_prep_commands()
    joined = "\n".join(cmds)
    assert "mkdir -p /home/ssm-user" in joined
    assert "chmod 700 /home/ssm-user" in joined
    assert "/home/ssm-user/private.pem" in joined
    assert "/home/ssm-user/passphrase.txt" in joined
    assert "chmod 600 /home/ssm-user/private.pem /home/ssm-user/passphrase.txt" in joined
    # The PEM is base64'd, never inlined: a literal newline or quote in a shell command
    # would break the drop, and the key would land truncated rather than not at all.
    assert "BEGIN ENCRYPTED PRIVATE KEY" not in joined
    assert "s3cret" not in joined


def test_ssm_prep_is_a_no_op_without_a_key_or_a_directory():
    # Not an error: an operator who staged the files by hand should not be forced to
    # paste the key here. The caller logs it, so it is visible either way.
    _reset()
    assert svc._ssm_jump_prep_commands() == []
    _reset(clouddb_ps_ssm_plugin_private_key="pem")           # no directory
    assert svc._ssm_jump_prep_commands() == []
    _reset(clouddb_ps_ssm_key_directory="/home/ssm-user")     # no key
    assert svc._ssm_jump_prep_commands() == []


def test_a_trailing_slash_does_not_produce_a_double_slash_path():
    _reset(clouddb_ps_ssm_plugin_private_key="pem", clouddb_ps_ssm_key_directory="/opt/psplugin/")
    joined = "\n".join(svc._ssm_jump_prep_commands())
    assert "/opt/psplugin/private.pem" in joined
    assert "//" not in joined


def test_the_two_clouds_use_separate_key_pairs():
    # The AWS private key lands on the ECS gateway host and the Azure one on
    # clouddb-jumpoint, so they must come from different config keys -- sharing one pair
    # would mean compromising either host also decrypts the other cloud's payloads.
    _reset(clouddb_ps_ssm_plugin_private_key="aws-key",
           clouddb_ps_ssm_key_directory="/home/ssm-user",
           clouddb_ps_azure_plugin_private_key="azure-key")
    import base64
    aws = "\n".join(svc._ssm_jump_prep_commands())
    azure = "\n".join(svc._azure_jump_prep_commands())
    assert base64.b64encode(b"aws-key").decode() in aws
    assert base64.b64encode(b"azure-key").decode() in azure
    assert base64.b64encode(b"azure-key").decode() not in aws
    assert base64.b64encode(b"aws-key").decode() not in azure
    # And they write to their own host's directory.
    assert "/root/psplugin/private.pem" in azure
    assert "/home/ssm-user/private.pem" in aws


def test_azure_prep_still_installs_clients_and_aws_prep_does_not():
    # Deliberate asymmetry: the dashboard's own managed-user creation runs the client as
    # a docker run, and the ECS gateway host is shared with the gateway workload, so what
    # the SSM plugin needs on its PATH is the operator's call.
    _reset(clouddb_ps_azure_plugin_private_key="k",
           clouddb_ps_ssm_plugin_private_key="k", clouddb_ps_ssm_key_directory="/d")
    azure = "\n".join(svc._azure_jump_prep_commands())
    aws = "\n".join(svc._ssm_jump_prep_commands())
    assert "postgresql-client" in azure and "mssql-tools18" in azure
    assert "apt-get" not in aws


# ── the teardown record must survive a later failure ─────────────────────────
#
# Observed live 2026-08-24: an AWS database was onboarded (managed system 168 and managed
# account 203 both present in Password Safe), then decommissioned, and the destroy log
# showed ONLY Terraform -- no Password Safe step at all -- leaving both orphaned.
#
# Teardown is gated on ids in the PROVISIONING job's metadata, and those used to be
# written once at the very end of onboarding. So anything failing after the managed
# system was registered lost the only pointer to it, and the teardown then skipped it in
# silence. _stash_on_job now commits after each external resource is created; this pins
# that, because the fix is invisible on a happy path and easy to undo by moving a line.

def test_the_managed_system_id_is_committed_before_the_pra_vault_half():
    _reset(bt_api_host="pra.example.com",
           clouddb_ps_ssm_public_key_path=r"C:\Utils\public_ssm.pem")
    job_row = _FakeJobRow()
    job_row.metadata_dict = {"vault_account_name": "va-1"}   # makes the PRA Vault block run
    row = _CloudDatabase(id="abcdef0123456789abcd", cloud="aws",
                         private_host="db.internal", engine="postgres")
    ctx = {"managed_user": "psafe_abcdef012345", "jump_host_id": "i-0eaa6a10886717ed",
           "region": "us-east-1", "admin_username": "dbadmin",
           "client_image": "postgres:16", "db_name": "appdb", "port": 5432}

    calls = {"n": 0}

    async def _second_registration_fails(**kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"tf_state_json": "{}", "managed_system_id": 168, "managed_account_id": 203}
        raise RuntimeError("PRA Vault registration failed")

    psr = sys.modules["web_dashboard.services.ps_resource_service"]
    real = psr.register_managed_system
    psr.register_managed_system = _second_registration_fails
    try:
        _run(svc._onboard_ps_managed_systems(
            _FakeDB(job_row), row=row, job_id="job-1", engine="postgres",
            tf_variables={"identifier": "clouddb-abcdef01"}, ctx=ctx))
    except Exception:
        pass          # onboarding is best-effort by design; not what this asserts
    finally:
        psr.register_managed_system = real

    assert calls["n"] == 2, "the PRA Vault registration should have been attempted"
    # Without the incremental commit this is None: the managed system exists in Password
    # Safe with nothing pointing at it, and decommission skips it without a word.
    assert job_row.metadata_dict.get("ps_db_registration_tf_state") is not None, (
        "the managed system was created but its teardown record was lost - "
        "decommission would silently orphan it")
    assert job_row.metadata_dict.get("ps_db_system_id") == 168


def test_a_teardown_with_nothing_recorded_says_so():
    # The other half of the same failure: silence here reads exactly like "there was
    # nothing to remove", so an orphan leaves no trace in the job the operator reads.
    logged = []
    js = sys.modules["web_dashboard.services.job_service"]
    prev = getattr(js, "append_job_log", None)
    js.append_job_log = lambda db, job_id, line: logged.append(line)
    prev_up = getattr(js, "update_progress", None)
    js.update_progress = lambda *a, **k: None
    try:
        errs = _run(svc._teardown_ps_onboarding(
            _FakeDB(_FakeJobRow()), row=None, prov_job=None, progress_job_id="job-9"))
    finally:
        if prev is not None: js.append_job_log = prev
        if prev_up is not None: js.update_progress = prev_up
    assert errs == []
    assert logged, "a teardown that removed nothing must leave a line in the job log"
    assert "orphan" in logged[0].lower()


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
