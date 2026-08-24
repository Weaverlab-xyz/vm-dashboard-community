"""Registering a database that already exists, so it can be a Config Management target.

The dashboard has always registered Kubernetes clusters it did not create
(`K8sCluster.source = registered | provisioned`). Databases had only the provisioned
half, and one function made that structural rather than incidental:
`ansible_connection_vars` read the admin credential out of the **provisioning job's**
`tf_variables`. A database the dashboard never provisioned has no such job, so it could
not produce connection variables at all — it could never be a target no matter what else
was built.

These tests pin the shape that fixes it:

  * a registered row resolves its credential from a Password Safe managed account,
    checked out at run time, with no provisioning job anywhere in the path;
  * the provisioned path is untouched — same keys, same per-cloud normalization;
  * the credential is never persisted: not on the row, not in the reference, not in
    anything the operator can read back;
  * delete means *deregister* for a registered row. The dashboard has no Terraform state
    for someone else's database, and destroying it would be the wrong verb.

Heavy deps are stubbed the way tests/test_clouddb_ansible_conn_vars.py does it, so this
runs without sqlalchemy or a Password Safe tenant.

Run: python tests/test_database_registration.py   (or under pytest)
"""
import asyncio
import json
import os
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

CONF = {}
CHECKOUTS = []          # (system_id, account_id, duration_min, uses_ssh_key)


class _Settings:
    def __getattr__(self, _key):
        return ""


class _CloudDatabase:
    # Class-level so the duplicate check's `CloudDatabase.engine == x` is a plain
    # comparison — the fake query ignores the predicate anyway.
    id = None
    engine = None
    cloud = None
    source = "provisioned"
    db_name = None
    credentials_ref = None
    private_host = None
    port = None
    region = None
    instance_id = None
    provider = None
    status = None
    created_by = None
    created_at = None
    jump_item_id = None
    entitle_integration_id = None
    ps_managed_system_id = None
    ps_managed_account_id = None

    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Q:
    def __init__(self, rows):
        self._rows = rows if isinstance(rows, list) else [rows]

    def filter(self, *a, **k):
        return self

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return self._rows


class _FakeDB:
    def __init__(self, rows=None):
        self.rows = rows if isinstance(rows, list) else ([rows] if rows else [])
        self.added, self.deleted, self.commits = [], [], 0

    def query(self, _model):
        return _Q(self.rows)

    def add(self, row):
        self.added.append(row)
        self.rows.append(row)

    def delete(self, row):
        self.deleted.append(row)

    def commit(self):
        self.commits += 1


def _install_stubs():
    try:
        # Availability, NOT `in sys.modules` — in CI the real library is installed but
        # not yet imported here, and a stub would shadow it.
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
    dbmod.Job = type("Job", (), {})
    sys.modules["web_dashboard.database"] = dbmod

    cfg = types.ModuleType("web_dashboard.services.config_service")
    cfg.get = lambda key: CONF.get(key, "")
    cfg.set = lambda key, val: CONF.__setitem__(key, val)
    sys.modules["web_dashboard.services.config_service"] = cfg

    js = types.ModuleType("web_dashboard.services.job_service")
    js.create_job = lambda *a, **k: None
    sys.modules["web_dashboard.services.job_service"] = js

    bt = types.ModuleType("web_dashboard.services.btapi_service")

    class BTAPIError(Exception):
        pass

    async def _checkout(system_id, account_id, duration_min=60, uses_ssh_key=False):
        CHECKOUTS.append((system_id, account_id, duration_min, uses_ssh_key))
        return 4242, "checked-out-secret"

    bt.BTAPIError = BTAPIError
    bt.get_ps_credential_with_request = _checkout
    sys.modules["web_dashboard.services.btapi_service"] = bt

    for name in ("terraform", "terraform_provider_env"):
        sys.modules[f"web_dashboard.services.{name}"] = types.ModuleType(
            f"web_dashboard.services.{name}")


_install_stubs()
try:
    from web_dashboard.services import cloud_database_service as svc
except Exception as exc:  # pragma: no cover
    print(f"SKIP: {exc}")
    sys.exit(0)


_ACCOUNT = {"system_id": 7, "account_id": 11, "account_name": "svc-dba", "uses_ssh_key": False}


def _registered_row(**kw):
    base = dict(id="db-reg", engine="postgres", cloud="local", source="registered",
                private_host="pg.corp.internal", port=5432, db_name="appdb",
                credentials_ref=svc._MANAGED_REF_PREFIX + json.dumps(_ACCOUNT, sort_keys=True))
    base.update(kw)
    return _CloudDatabase(**base)


# ── the blocker: connection vars without a provisioning job ───────────────────

def test_a_registered_database_resolves_vars_with_no_provisioning_job():
    """The whole point. Before this, ansible_connection_vars raised
    'no admin credential available … (provisioning job pruned?)' for any row without a
    provisioning job, which is every registered one."""
    CHECKOUTS.clear()

    def _boom(*_a, **_k):
        raise AssertionError("the registered path must not look for a provisioning job")

    svc._provision_job_for = _boom
    out = asyncio.run(svc.ansible_connection_vars(_FakeDB(_registered_row()), "db-reg"))
    assert out["db_login_host"] == "pg.corp.internal"
    assert out["db_login_port"] == 5432
    assert out["db_login_user"] == "svc-dba"
    assert out["db_login_password"] == "checked-out-secret"
    assert out["db_name"] == "appdb"
    assert out["db_engine"] == "postgres"


def test_the_credential_is_checked_out_against_the_pinned_account():
    CHECKOUTS.clear()
    svc._provision_job_for = lambda *_a, **_k: None
    asyncio.run(svc.ansible_connection_vars(_FakeDB(_registered_row()), "db-reg"))
    assert len(CHECKOUTS) == 1, "expected exactly one Password Safe checkout"
    system_id, account_id, duration, uses_key = CHECKOUTS[0]
    assert (system_id, account_id) == (7, 11)
    assert duration > 0, "a checkout with no duration would never expire"
    assert uses_key is False


def test_the_vars_shape_matches_the_provisioned_path():
    """The runner and the sample playbooks read these keys; a registered database that
    returned a different shape would need its own playbook."""
    CHECKOUTS.clear()
    svc._provision_job_for = lambda *_a, **_k: None
    out = asyncio.run(svc.ansible_connection_vars(_FakeDB(_registered_row()), "db-reg"))
    assert set(out) == {"db_engine", "db_login_host", "db_login_port",
                        "db_login_user", "db_login_password", "db_name"}


def test_sqlserver_falls_back_to_master_like_the_provisioned_path():
    CHECKOUTS.clear()
    svc._provision_job_for = lambda *_a, **_k: None
    row = _registered_row(engine="sqlserver", db_name=None, port=1433)
    out = asyncio.run(svc.ansible_connection_vars(_FakeDB(row), "db-reg"))
    assert out["db_name"] == "master"


def test_a_scope_suffix_is_stripped_from_the_login_user():
    """Cloud-native Password Safe plugins register an account as `user;scope`. The
    suffix is a PS naming detail, not part of the database username — the SSH path
    already strips it and this must agree."""
    CHECKOUTS.clear()
    svc._provision_job_for = lambda *_a, **_k: None
    ref = dict(_ACCOUNT, account_name="svc-dba;local")
    row = _registered_row(credentials_ref=svc._MANAGED_REF_PREFIX + json.dumps(ref, sort_keys=True))
    out = asyncio.run(svc.ansible_connection_vars(_FakeDB(row), "db-reg"))
    assert out["db_login_user"] == "svc-dba"


def test_a_registered_row_without_an_account_fails_with_a_fix():
    CHECKOUTS.clear()
    svc._provision_job_for = lambda *_a, **_k: None
    row = _registered_row(credentials_ref=None)
    try:
        asyncio.run(svc.ansible_connection_vars(_FakeDB(row), "db-reg"))
    except svc.CloudDatabaseError as e:
        assert "re-register" in str(e), "the error should say what to do about it"
    else:
        raise AssertionError("a registered row with no managed account was accepted")


# ── nothing sensitive is persisted ────────────────────────────────────────────

def test_registration_stores_ids_and_a_name_but_never_a_credential():
    db = _FakeDB()
    svc.register_database(db, engine="postgres", cloud="local", host="pg.corp.internal",
                          port=5432, db_name="appdb", managed_account=_ACCOUNT,
                          created_by="alice")
    row = db.added[0]
    blob = json.dumps({k: v for k, v in row.__dict__.items()
                       if isinstance(v, (str, int, bool, type(None)))})
    assert "checked-out-secret" not in blob
    assert "password" not in blob.lower(), f"a credential-shaped field was persisted: {blob}"
    ref = svc.managed_account_ref(row)
    assert ref["system_id"] == 7 and ref["account_id"] == 11
    assert set(ref) == {"system_id", "account_id", "account_name", "uses_ssh_key"}


# ── registration validation ───────────────────────────────────────────────────

def test_on_prem_databases_may_be_registered():
    """cloud='local' is the on-prem case and is exactly what provisioning cannot do —
    there is no Terraform module for someone else's database."""
    assert "local" in svc.VALID_REGISTER_CLOUDS
    assert "local" not in svc.VALID_CLOUDS, (
        "'local' must not become provisionable — there is no module to provision with")


def test_registration_requires_a_managed_account():
    db = _FakeDB()
    for bad in ({}, {"system_id": 7}, {"account_id": 11}):
        try:
            svc.register_database(db, engine="postgres", cloud="local", host="h",
                                  port=None, db_name="", managed_account=bad,
                                  created_by="alice")
        except svc.CloudDatabaseError as e:
            assert "managed account" in str(e)
        else:
            raise AssertionError(f"registration accepted an incomplete account: {bad}")


def test_registration_rejects_an_unknown_cloud_and_engine():
    db = _FakeDB()
    for kw in ({"cloud": "digitalocean"}, {"engine": "mongodb"}):
        args = dict(engine="postgres", cloud="local", host="h", port=None, db_name="",
                    managed_account=_ACCOUNT, created_by="alice")
        args.update(kw)
        try:
            svc.register_database(db, **args)
        except svc.CloudDatabaseError:
            pass
        else:
            raise AssertionError(f"registration accepted {kw}")


def test_registration_requires_a_host():
    """The host is the only way any runner reaches it — there is no cloud resource id
    to look up for a database the dashboard didn't create."""
    try:
        svc.register_database(_FakeDB(), engine="postgres", cloud="local", host="  ",
                              port=None, db_name="", managed_account=_ACCOUNT,
                              created_by="alice")
    except svc.CloudDatabaseError as e:
        assert "host is required" in str(e)
    else:
        raise AssertionError("registration accepted a blank host")


# ── delete means deregister, not destroy ──────────────────────────────────────

def test_deregister_removes_the_row_without_destroying_anything():
    row = _registered_row()
    db = _FakeDB(row)
    svc.deregister_database(db, "db-reg")
    assert db.deleted == [row]


def test_deregister_refuses_a_provisioned_row():
    """Provisioned databases carry Terraform state; dropping the row would orphan the
    real database and its state directory."""
    row = _CloudDatabase(id="db-prov", engine="postgres", cloud="aws", source="provisioned")
    db = _FakeDB(row)
    try:
        svc.deregister_database(db, "db-prov")
    except svc.CloudDatabaseError as e:
        assert "decommissioned" in str(e)
    else:
        raise AssertionError("a provisioned database was deregistered instead of destroyed")
    assert not db.deleted


# ── targeting ─────────────────────────────────────────────────────────────────

def test_local_databases_are_a_valid_config_management_target():
    src = open(os.path.join(_ROOT, "web_dashboard", "services",
                            "ansible_cloud_run_service.py"), encoding="utf-8").read()
    import re
    tup = re.search(r"DB_TARGET_CLOUDS = \(([^)]*)\)", src).group(1)
    assert '"local"' in tup, (
        "DB_TARGET_CLOUDS has no 'local' — a registered on-prem database would be "
        "rejected by the run gate before the runner is ever chosen")


# ── the ref-count: a registered row must not pin the shared Gateway ───────────

class _Col:
    """Enough of a SQLAlchemy column for _active_db_count to BUILD its predicates.

    The fake session discards them, so these tests assert the Python-side ``source``
    filter specifically — not the status/cloud scoping, which stays plain SQL."""

    def in_(self, _values):
        return self

    def __eq__(self, _other):
        return self

    __hash__ = object.__hash__


class _Cols:
    """Stands in for the CloudDatabase *class* while the counter builds predicates.
    Kept off _CloudDatabase so no other test sees a column where it expects None."""
    status = _Col()
    cloud = _Col()
    source = _Col()


def _gateway_ref_count(rows, cloud="aws"):
    import web_dashboard.database as dbmod
    from web_dashboard.services import jumpoint_host_service as jhs
    prev = dbmod.CloudDatabase
    dbmod.CloudDatabase = _Cols          # the counter imports it per-call
    try:
        return jhs._active_db_count(_FakeDB(rows), cloud)
    finally:
        dbmod.CloudDatabase = prev


def test_a_registered_database_does_not_pin_the_shared_gateway():
    """The leak. A registered row is born status='available' with no Terraform state, no
    PRA tunnel and no jump item — it never brokers through the shared Gateway host. The
    ref-count keyed on status alone, so registering one AWS database kept that host up
    forever, and with it the three SSM interface endpoints (the ~$10/mo PR #402 fixed)."""
    rows = [_CloudDatabase(id="db-reg", cloud="aws", status="available",
                           source="registered")]
    assert _gateway_ref_count(rows) == 0, (
        "a registered database still holds a Gateway reference: the idle teardown and "
        "the SSM endpoint reclaim can never fire")


def test_a_provisioned_database_still_pins_the_shared_gateway():
    """The other direction: the fix must not turn the reference off wholesale. A
    provisioned database really does broker its tunnel through the host."""
    rows = [_CloudDatabase(id="db-reg", cloud="aws", status="available",
                           source="registered"),
            _CloudDatabase(id="db-prov", cloud="aws", status="available",
                           source="provisioned")]
    assert _gateway_ref_count(rows) == 1


def test_a_null_source_counts_as_provisioned():
    """Rows predating registration migrate with DEFAULT 'provisioned', but the read stays
    NULL-tolerant because under-counting is the dangerous direction: it would reap the
    gateway from under a live database's tunnel."""
    rows = [_CloudDatabase(id="db-old", cloud="aws", status="available", source=None)]
    assert _gateway_ref_count(rows) == 1


# ── the ephemeral-store gate does not apply to a database target ──────────────
# A registered AWS database + a Password Safe managed account was believed to be
# unrunnable: the theory was that `requires_ephemeral_store` refuses a managed-account
# run on the ECS / Cloud Run runners, so AWS and GCP databases could be registered but
# not run against. It was never true. The gate lives in the SSH/ad-hoc branch of /run,
# and a database target returns to _run_cloud_localhost dozens of lines earlier, so the
# gate is not merely passed — it is unreachable. A live run settled it: an AWS
# registered database resolved its credential from Password Safe just-in-time and the
# play completed on the ECS runner (rc=0).
#
# The mechanism is the difference between two transport channels, and that is what
# these tests pin. A database run's connection vars ride the runner's INLINE env
# (`CONN_VARS_B64` — an ECS override-env `value`, ACI `secure_value`, a Cloud Run env
# var), so nothing needs to pre-exist in a cloud secret store. The SSH path's
# named/become secrets ride the provider's secret-REFERENCE channel (`valueFrom`) on
# ECS and Cloud Run, which is the only reason that opt-in exists.

def _config_mgmt_src():
    return open(os.path.join(_ROOT, "web_dashboard", "api", "config_mgmt.py"),
                encoding="utf-8").read()


def test_database_target_returns_before_the_ephemeral_store_gate():
    """The early return must stay ahead of the gate. If someone moves the gate above
    it — or moves the dispatch below it — every AWS/GCP database run starts 400ing
    unless an unrelated cloud-secrets opt-in is enabled."""
    src = _config_mgmt_src()
    dispatch = src.find('target_kind in ("k8s", "database")')
    gate = src.find("requires_ephemeral_store(")
    assert dispatch != -1, "the k8s/database early return is gone from /run"
    assert gate != -1, "requires_ephemeral_store call is gone — did the gate move?"
    assert dispatch < gate, (
        "the ephemeral-store gate now precedes the k8s/database dispatch, so a database "
        "target is subject to a rule written for the VM SSH path")


def test_run_cloud_localhost_never_consults_the_ephemeral_store_gate():
    """The path a database target actually takes must not grow the gate either."""
    src = _config_mgmt_src()
    start = src.find("async def _run_cloud_localhost(")
    assert start != -1, "_run_cloud_localhost is gone"
    end = src.find("\n@router.post(\"/run\")", start)
    body = src[start:end if end != -1 else len(src)]
    assert "requires_ephemeral_store" not in body, (
        "_run_cloud_localhost consults the ephemeral-store gate; a database run's "
        "credential is delivered inline, so there is no store-residency requirement")


def test_database_conn_vars_ride_the_inline_env_on_every_runner():
    """CONN_VARS_B64 must be an inline value on ECS, not a store reference. `valueFrom`
    here would be the thing that actually made the old claim true."""
    src = open(os.path.join(_ROOT, "web_dashboard", "services", "aws_service.py"),
               encoding="utf-8").read()
    idx = src.find('"name": "CONN_VARS_B64"')
    assert idx != -1, "CONN_VARS_B64 is no longer put on the ECS task override env"
    entry = src[idx:idx + 200]
    assert '"value": conn_vars_b64' in entry, (
        "CONN_VARS_B64 is no longer passed by inline value — if it became a "
        "`valueFrom` store reference, a just-in-time credential really would need an "
        "ephemeral store copy and the database run path would need a gate")


def test_register_form_does_not_claim_aws_gcp_runs_are_refused():
    """The register form carried an amber warning saying a Config Management run against
    an AWS or GCP database 'is refused today'. The live run disproved it; keep it gone."""
    tpl = open(os.path.join(_ROOT, "web_dashboard", "templates", "databases", "index.html"),
               encoding="utf-8").read()
    assert "is refused today" not in tpl, (
        "the register form again claims AWS/GCP database runs are refused — they are "
        "not; both run on their in-cloud runner with an inline credential")


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
