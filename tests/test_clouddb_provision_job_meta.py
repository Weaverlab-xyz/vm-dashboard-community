"""Regression test: cloud_database_service.provision must embed the (secret-
stripped) ``tf_variables`` in the clouddb_provision Job's metadata at creation
time.

The apply runs in a *separate* process (the dedicated job runner) that polls for
pending jobs and dispatches them reading ``meta["tf_variables"]``. An earlier
version committed the pending job with only ``{db_id, engine, cloud, name, …}``
and patched ``tf_variables`` in via a second call from the API layer — so if the
runner's poll landed between those two commits it claimed a job with no
``tf_variables`` and died with ``KeyError('tf_variables')`` ("job runner error:
'tf_variables'"). This pins the fix: provision() embeds tf_variables in the job
metadata from the single create commit, with the admin password stripped (a
secret is never written to jobs.extra_data; run_provision_apply re-injects it).

The second test pins the OTHER half of that contract — the re-injection. Because
provision() strips the password, run_provision_apply MUST put it back, and it reads
it from a store the app process wrote moments earlier. config_service answers from a
per-process snapshot up to 5s old, and the runner claims a pending job within one 2s
poll, so a cached read returns "" for a row that is certainly in app_config. The read
used to be ``config_service.get(...) or ""`` behind ``if _pw:``, so the miss was
silent: terraform ran with no master_password and the apply died on "No value for
required variable master_password" pointing at the module's variable block, which
reads as a Terraform/module bug in whichever engine and region drew the short straw.

Heavy deps (database, config, config_service, terraform, job_service, websocket) are
stubbed in sys.modules; the DB session and job_service.create_job are faked so no real
database or cloud account is needed. Runs under pytest, or standalone:
    python tests/test_clouddb_provision_job_meta.py
"""
import asyncio
import os
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# The two config stores the bug lives between: CONF is app_config (authoritative,
# what the app process writes) and CACHE is the snapshot the job runner holds. A
# separate process's set() never lands in CACHE, and the runner only reloads it every
# _CACHE_TTL_SECONDS — so a key written seconds ago is simply not in there.
CONF = {}
CACHE = {}
APPLIED = {}    # what terraform.apply was actually called with
FAILED = {}     # what job_service.set_failed recorded


class _Settings:
    def __getattr__(self, _key):
        return ""


class _CloudDatabase:
    # Class-level attributes because the runner filters on them (CloudDatabase.id ==
    # db_id); on the real model those are SQLAlchemy column descriptors.
    id = ""
    cloud = ""

    def __init__(self, **kw):
        self.__dict__.update(kw)
        self.id = "abcdef0123456789"  # DB-assigned in prod; fixed here for asserts


# captured on create_job so the test can assert what was persisted
_CAPTURED = {}


class _FakeJob:
    def __init__(self, metadata):
        self.id = "job-abc"
        self.metadata_dict = metadata


def _fake_create_job(db, *, job_type, created_by, metadata=None, **_kw):
    _CAPTURED["job_type"] = job_type
    _CAPTURED["metadata"] = metadata
    return _FakeJob(metadata)


def _install_stubs():
    confmod = types.ModuleType("web_dashboard.config")
    confmod.settings = _Settings()
    sys.modules["web_dashboard.config"] = confmod

    dbmod = types.ModuleType("web_dashboard.database")
    dbmod.CloudDatabase = _CloudDatabase
    dbmod.Job = type("Job", (), {"id": ""})
    sys.modules["web_dashboard.database"] = dbmod

    cfg = types.ModuleType("web_dashboard.services.config_service")
    # get() serves the runner's snapshot; get_fresh() goes to the table. set() writes
    # the table WITHOUT touching CACHE, because in production the writer is the app
    # process and the reader is the runner — two processes, two caches.
    cfg.get = lambda key, default="", workgroup=None: CACHE.get(key, default)
    cfg.get_fresh = lambda key, default="", workgroup=None: CONF.get(key, default)
    cfg.set = lambda key, val, workgroup=None: CONF.__setitem__(key, val)
    cfg.get_bool = lambda key, default=False: bool(CACHE.get(key, default))
    sys.modules["web_dashboard.services.config_service"] = cfg

    js = types.ModuleType("web_dashboard.services.job_service")
    js.create_job = _fake_create_job
    js.set_running = lambda *a, **k: None
    js.set_completed = lambda *a, **k: None
    js.update_progress = lambda *a, **k: None
    js.cancel_check = lambda *a, **k: None
    js.set_failed = lambda db, job_id, msg: FAILED.__setitem__("message", msg)
    sys.modules["web_dashboard.services.job_service"] = js

    tf = types.ModuleType("web_dashboard.services.terraform")

    class _TerraformError(Exception):
        pass

    async def _fake_apply(deploy_dir, variables, template_dir=None, env=None,
                          on_line=None):
        APPLIED["variables"] = dict(variables)
        return {}

    tf.TerraformError = _TerraformError
    tf.apply = _fake_apply
    sys.modules["web_dashboard.services.terraform"] = tf

    tpe = types.ModuleType("web_dashboard.services.terraform_provider_env")
    tpe.provider_env = lambda cloud: {}
    sys.modules["web_dashboard.services.terraform_provider_env"] = tpe

    # _job_stream imports this lazily, when it builds the on_line callback.
    api = types.ModuleType("web_dashboard.api")
    ws = types.ModuleType("web_dashboard.api.websocket")

    async def _broadcast(*a, **k):
        return None

    ws.broadcast_progress = _broadcast
    api.websocket = ws
    sys.modules["web_dashboard.api"] = api
    sys.modules["web_dashboard.api.websocket"] = ws


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


class _FakeQuery:
    def __init__(self, result):
        self._result = result

    def filter(self, *a, **k):
        return self

    def order_by(self, *a, **k):
        return self

    def first(self):
        return self._result

    def all(self):
        return [self._result] if self._result is not None else []


class _FakeDB:
    def __init__(self, row=None):
        self._row = row
        self.added = []

    def add(self, row, *a, **k):
        self.added.append(row)

    def commit(self):
        pass

    def refresh(self, *a, **k):
        pass

    def query(self, model):
        # Only the CloudDatabase row matters; the Job lookup (register_in_entitle)
        # is allowed to come back empty.
        return _FakeQuery(self._row if model is _CloudDatabase else None)


def test_provision_embeds_secret_stripped_tf_variables_in_job_metadata():
    _CAPTURED.clear()
    CONF.clear()
    fake_db = _FakeDB()
    result = svc.provision(
        fake_db, engine="postgres", cloud="aws", region="r1",
        name="appdb", created_by="admin")

    # The job the runner will claim must already carry tf_variables (the bug was
    # that it didn't until a second, racy commit from the API layer).
    assert _CAPTURED["job_type"] == "clouddb_provision"
    meta = _CAPTURED["metadata"]
    assert "tf_variables" in meta, "tf_variables must be in the job metadata at create time"

    # The persisted copy must NOT contain the admin secret …
    persisted = meta["tf_variables"]
    assert "master_password" not in persisted
    assert "administrator_password" not in persisted
    # … but must keep the non-secret vars the apply/tunnel need.
    assert persisted["master_username"] == "dbadmin"
    assert persisted["db_name"] == "appdb"
    assert persisted["region"] == "r1"

    # The full set returned to the caller still carries the secret (run_provision_
    # apply re-injects from the secrets backend, but the return value is the source).
    assert result["tf_variables"]["master_password"], "returned tf_variables keeps the secret"
    # Other metadata the dispatch reads is present.
    assert meta["db_id"] == result["db_id"]
    assert meta["engine"] == "postgres"
    assert meta["cloud"] == "aws"

    # The catalog is stamped on the ROW too, so neither the Databases page nor the
    # Entitle adapter depends on this job surviving. Exactly the -var, no engine
    # substitution — see cloud_database_service.connection_db_name.
    assert fake_db.added[0].db_name == persisted["db_name"]


def _provisioned_gcp_mysql():
    """provision() a GCP MySQL DB and hand back (result, row) — the exact state the
    runner wakes up to."""
    _CAPTURED.clear()
    CONF.clear()
    CACHE.clear()
    APPLIED.clear()
    FAILED.clear()
    result = svc.provision(
        _FakeDB(), engine="mysql", cloud="gcp", region="us-east1",
        name="appdb", created_by="admin")
    row = _CloudDatabase(cloud="gcp", engine="mysql", region="us-east1",
                         status="provisioning", private_host="", instance_id="",
                         port=3306, created_by="admin", jump_item_id=None)
    return result, row


def test_run_provision_apply_reinjects_the_password_the_runner_cannot_see_yet():
    result, row = _provisioned_gcp_mysql()
    db_id = result["db_id"]
    secret_key = f"clouddb/{db_id}/admin"
    assert CONF[secret_key], "provision() must have written the admin secret"
    # The runner's snapshot predates that write — CACHE stays empty, which is exactly
    # what a cached read sees when the job is claimed ~2s after it was created.
    assert secret_key not in CACHE

    asyncio.run(svc.run_provision_apply(
        _FakeDB(row=row), db_id=db_id, job_id="job-abc", engine="mysql",
        tf_variables=dict(_CAPTURED["metadata"]["tf_variables"])))

    assert "variables" in APPLIED, (
        "terraform.apply was never reached; the job failed with: "
        f"{FAILED.get('message')!r}")
    assert APPLIED["variables"].get("master_password") == CONF[secret_key], (
        "run_provision_apply must re-inject the admin password even though the "
        "runner's config cache predates it — otherwise terraform apply dies on "
        "'No value for required variable master_password'")
    assert not FAILED, f"the apply should not have failed: {FAILED.get('message')!r}"


def test_run_provision_apply_refuses_to_apply_without_the_admin_password():
    """An unreadable credential must fail the job with a message that says so.

    Handing terraform a var set with the password missing turns a secrets-store
    problem into a Terraform diagnostic about a variable block, and the job page
    shows nothing but error_message — so the reason has to be in that string.
    """
    result, row = _provisioned_gcp_mysql()
    db_id = result["db_id"]
    CONF.pop(f"clouddb/{db_id}/admin")      # the credential is genuinely gone

    asyncio.run(svc.run_provision_apply(
        _FakeDB(row=row), db_id=db_id, job_id="job-abc", engine="mysql",
        tf_variables=dict(_CAPTURED["metadata"]["tf_variables"])))

    assert "variables" not in APPLIED, "must not apply without the admin password"
    assert row.status == "failed"
    message = FAILED.get("message") or ""
    assert "admin credential" in message and db_id in message, (
        f"the failure must name the missing credential, got {message!r}")


def test_a_failed_ps_onboarding_fails_the_job_but_keeps_the_row_available():
    """The live shape of 2026-08-25: the RDS apply succeeded, then Password Safe
    rejected the managed-system create ("The field 'IPAddress' is required.") — and the
    job page said *completed*. The operator opted in, so the job must FAIL and its
    error_message (the only detail a failed job shows) must carry both the cause and
    the remedy. The ROW must stay "available": the database is real and reachable, and
    run_ps_register — the remedy — refuses any row that is not."""
    _CAPTURED.clear()
    CONF.clear()
    CACHE.clear()
    APPLIED.clear()
    FAILED.clear()
    result = svc.provision(
        _FakeDB(), engine="postgres", cloud="aws", region="us-east-2",
        name="appdb", created_by="admin")
    row = _CloudDatabase(cloud="aws", engine="postgres", region="us-east-2",
                         status="provisioning", private_host="", instance_id="",
                         port=5432, created_by="admin", jump_item_id=None)
    # The runner's snapshot: Password Safe onboarding configured and switched on.
    CACHE.update({"pscli_api_url": "https://ps", "pscli_client_id": "id",
                  "pscli_client_secret": "sec", "clouddb_ps_onboarding_enabled": True})
    # The AWS path best-effort-imports this before the apply; keep it inert.
    ssm = types.ModuleType("web_dashboard.services.ssm_endpoint_service")

    async def _no_endpoints(region):
        return None

    ssm.ensure_ssm_endpoints = _no_endpoints
    sys.modules["web_dashboard.services.ssm_endpoint_service"] = ssm

    tf = sys.modules["web_dashboard.services.terraform"]

    async def _apply_returns_a_live_db(deploy_dir, variables, template_dir=None,
                                       env=None, on_line=None):
        APPLIED["variables"] = dict(variables)
        return {"instance_id": "db-X", "private_host": "h.rds.amazonaws.com",
                "port": 5432}

    async def _user_created(*a, **k):
        return {"managed_user": "psafe_x", "managed_pw": "pw", "jump_host_id": "i-j",
                "region": "us-east-2", "db_name": "appdb", "admin_username": "dbadmin",
                "port": 5432}

    async def _onboarding_rejected(*a, **k):
        raise RuntimeError(
            "terraform apply failed: Error: Error creating managed system by workgroup "
            "Id … { The field 'IPAddress' is required.")

    originals = (tf.apply, svc._create_db_managed_user, svc._onboard_ps_managed_systems)
    tf.apply = _apply_returns_a_live_db
    svc._create_db_managed_user = _user_created
    svc._onboard_ps_managed_systems = _onboarding_rejected
    try:
        asyncio.run(svc.run_provision_apply(
            _FakeDB(row=row), db_id=result["db_id"], job_id="job-abc",
            engine="postgres",
            tf_variables=dict(_CAPTURED["metadata"]["tf_variables"])))
    finally:
        tf.apply, svc._create_db_managed_user, svc._onboard_ps_managed_systems = originals

    message = FAILED.get("message") or ""
    assert "Password Safe onboarding failed" in message, (
        f"the job must fail — a green job with no PS artifacts was the bug; got {message!r}")
    assert "IPAddress" in message and "Register in Password Safe" in message, (
        f"the failure must carry the cause and the remedy, got {message!r}")
    assert row.status == "available", (
        f"the row must stay available (run_ps_register refuses any other status), "
        f"got {row.status!r}")


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
