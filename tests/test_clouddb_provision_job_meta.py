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

Heavy deps (database, config, config_service, terraform, job_service) are stubbed
in sys.modules; the DB session and job_service.create_job are faked so no real
database or cloud account is needed. Runs under pytest, or standalone:
    python tests/test_clouddb_provision_job_meta.py
"""
import os
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

CONF = {}


class _Settings:
    def __getattr__(self, _key):
        return ""


class _CloudDatabase:
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
    dbmod.Job = type("Job", (), {})
    sys.modules["web_dashboard.database"] = dbmod

    cfg = types.ModuleType("web_dashboard.services.config_service")
    cfg.get = lambda key: CONF.get(key, "")
    cfg.set = lambda key, val: CONF.__setitem__(key, val)
    sys.modules["web_dashboard.services.config_service"] = cfg

    js = types.ModuleType("web_dashboard.services.job_service")
    js.create_job = _fake_create_job
    sys.modules["web_dashboard.services.job_service"] = js

    # Imported at module load; provision() never touches them.
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


class _FakeDB:
    def add(self, *a, **k):
        pass

    def commit(self):
        pass

    def refresh(self, *a, **k):
        pass


def test_provision_embeds_secret_stripped_tf_variables_in_job_metadata():
    _CAPTURED.clear()
    CONF.clear()
    result = svc.provision(
        _FakeDB(), engine="postgres", cloud="aws", region="r1",
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
