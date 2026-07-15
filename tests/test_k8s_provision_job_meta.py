"""Regression test: k8s_service.create_cluster must embed ``tf_variables`` in the
provision Job's metadata at creation time.

The apply runs in a *separate* process (the dedicated job runner) that polls for
pending jobs and dispatches them reading ``meta["tf_variables"]``. An earlier
version committed the pending job with only ``{cluster_id, cloud, name, region}``
and patched ``tf_variables`` in via a second call from the API layer — so if the
runner's poll landed between those two commits it claimed a job with no
``tf_variables`` and died with ``KeyError('tf_variables')`` ("job runner error:
'tf_variables'"). This pins the fix: the Job that ``create_cluster`` creates
carries ``tf_variables`` in its metadata from the single create commit.

Heavy deps (database, config, config_service, region_config) are stubbed in
sys.modules; the DB session and job_service.create_job are faked so no real
database or cloud account is needed. Runs under pytest, or standalone:
    python tests/test_k8s_provision_job_meta.py
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


class _Col:
    """Stand-in for a SQLAlchemy column so ``K8sCluster.name == x`` (evaluated as
    a filter argument) doesn't blow up on the bare stub class."""
    def __eq__(self, other):
        return ("eq", other)


class _K8sCluster:
    id = name = cloud = status = source = region = created_by = deploy_job_id = _Col()

    def __init__(self, **kw):
        self.__dict__.update(kw)


# ── captured on create_job so the test can assert what was persisted ──────────
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
    dbmod.Job = type("Job", (), {})
    dbmod.K8sCluster = _K8sCluster
    sys.modules["web_dashboard.database"] = dbmod

    cfg = types.ModuleType("web_dashboard.services.config_service")
    cfg.get = lambda key: ""
    sys.modules["web_dashboard.services.config_service"] = cfg

    rc = types.ModuleType("web_dashboard.services.region_config")
    rc.resolve_azure_region = lambda region: {"resource_group": "rg-test"}
    sys.modules["web_dashboard.services.region_config"] = rc

    js = types.ModuleType("web_dashboard.services.job_service")
    js.create_job = _fake_create_job
    sys.modules["web_dashboard.services.job_service"] = js


_install_stubs()
try:
    from web_dashboard.services import k8s_service as svc
except Exception as exc:  # pragma: no cover — skip if other app deps are missing
    try:
        import pytest
        pytest.skip(f"k8s_service import unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)


class _Query:
    def filter(self, *a, **k):
        return self

    def first(self):
        return None  # no existing cluster with this name


class _FakeDB:
    def query(self, *a, **k):
        return _Query()

    def add(self, *a, **k):
        pass

    def commit(self):
        pass

    def refresh(self, *a, **k):
        pass


def test_create_cluster_embeds_tf_variables_in_job_metadata():
    _CAPTURED.clear()
    result = svc.create_cluster(
        _FakeDB(), cloud="aws", name="demo", region="r1", created_by="admin")

    # The job the runner will claim must already carry tf_variables (the bug was
    # that it didn't until a second, racy commit from the API layer).
    assert _CAPTURED["job_type"] == "k8s_provision"
    meta = _CAPTURED["metadata"]
    assert "tf_variables" in meta, "tf_variables must be in the job metadata at create time"
    assert meta["tf_variables"] == result["tf_variables"]
    # Sanity: the other metadata the dispatch reads is present too.
    assert meta["cluster_id"] == result["cluster_id"]
    assert meta["cloud"] == "aws"
    # And the -var set is the real EKS shape, not empty.
    assert result["tf_variables"]["cluster_name"] == "k8s-demo"
    assert result["tf_variables"]["region"] == "r1"


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
