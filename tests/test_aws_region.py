"""Multi-region support (Phase 1, AWS): region is a first-class deploy parameter.

Pins the behaviour added when EC2 deploys stopped taking their region solely from
the global ``aws_region`` config and started honouring a per-deploy ``region``:

  * ``_resolve_region`` — an explicit well-formed region wins, blank/None falls
    back to the configured default, and a malformed region is rejected (so a typo
    can't silently deploy into the default region).
  * ``POST /api/aws/deploy`` records the resolved region on the job metadata (the
    background runner + inventory read it back), defaulting when omitted and
    returning 400 on a bad region.
  * ``_fetch_instances`` describes each instance in the region recorded on its
    deploy job (grouped per region) and tags the result with that region, so
    instances deployed outside the default region stay listable.

Follows the hermetic TestClient pattern from test_containers_page_resilience.py.
Heavy cloud deps (fastapi/boto3/…) are only present in CI; when they're missing
the file SKIPs cleanly so the per-file runner stays green.

Run: python tests/test_aws_region.py   (or under pytest)
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient

    from web_dashboard.api import aws
    from web_dashboard.api.auth import get_current_user
    from web_dashboard.database import get_db
except Exception as exc:  # pragma: no cover — deps absent outside CI
    try:
        import pytest
        pytest.skip(f"aws api import unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)


DEFAULT_REGION = "us-east-2"


class _AdminUser:
    is_effective_admin = True
    is_admin = True
    username = "tester"
    workgroups_list: list = []
    effective_permissions_dict: dict = {}


class _Workgroup:
    name = "eng"


# ── shared stubs installed on the aws module ─────────────────────────────────

_CAPTURED: dict = {}


class _FakeJob:
    def __init__(self, metadata):
        self.id = "job-1"
        self.metadata_dict = metadata or {}


def _fake_create_job(db, *, job_type, created_by, workgroup=None, metadata=None, **_kw):
    _CAPTURED["job_type"] = job_type
    _CAPTURED["metadata"] = metadata
    return _FakeJob(metadata)


async def _noop_async(*_a, **_k):
    return None


def _install_stubs():
    aws._aws_region = lambda: DEFAULT_REGION           # deterministic default
    aws.workgroup_service.get = lambda db, name: _Workgroup()
    aws.job_service.create_job = _fake_create_job
    aws.job_service.log_audit = lambda *a, **k: None
    aws._run_deploy = _noop_async                       # keep background task harmless
    aws._run_bulk_deploy = _noop_async
    from web_dashboard.services import admission_service
    admission_service.enforce = lambda *a, **k: None    # inert policy gate


def _make_client() -> TestClient:
    app = FastAPI()
    app.include_router(aws.router)
    app.dependency_overrides[get_current_user] = lambda: _AdminUser()
    app.dependency_overrides[get_db] = lambda: object()
    return TestClient(app, raise_server_exceptions=False)


def _deploy_body(**over):
    body = {
        "ami_id": "ami-123",
        "instance_name": "vm1",
        "subnet_id": "subnet-1",
        "security_group_ids": ["sg-1"],
        "workgroup": "eng",
    }
    body.update(over)
    return body


# ── _resolve_region ──────────────────────────────────────────────────────────

def test_resolve_region_default_explicit_and_invalid():
    _install_stubs()
    assert aws._resolve_region(None) == DEFAULT_REGION
    assert aws._resolve_region("   ") == DEFAULT_REGION
    assert aws._resolve_region("us-west-2") == "us-west-2"
    assert aws._resolve_region("US-West-2") == "us-west-2"   # normalised
    assert aws._resolve_region("eu-central-1") == "eu-central-1"
    assert aws._resolve_region("us-gov-west-1") == "us-gov-west-1"
    for bad in ("bogus", "us_east_2", "useast2", "us-east"):
        try:
            aws._resolve_region(bad)
        except HTTPException as e:
            assert e.status_code == 400
        else:
            raise AssertionError(f"expected 400 for region {bad!r}")


# ── POST /api/aws/deploy ──────────────────────────────────────────────────────

def test_deploy_records_explicit_region():
    _install_stubs()
    _CAPTURED.clear()
    client = _make_client()
    r = client.post("/api/aws/deploy", json=_deploy_body(region="us-west-2"))
    assert r.status_code == 200, (r.status_code, r.text)
    assert _CAPTURED["job_type"] == "ec2_deploy"
    assert _CAPTURED["metadata"]["region"] == "us-west-2"


def test_deploy_defaults_region_when_omitted():
    _install_stubs()
    _CAPTURED.clear()
    client = _make_client()
    r = client.post("/api/aws/deploy", json=_deploy_body())
    assert r.status_code == 200, (r.status_code, r.text)
    assert _CAPTURED["metadata"]["region"] == DEFAULT_REGION


def test_deploy_rejects_invalid_region():
    _install_stubs()
    _CAPTURED.clear()
    client = _make_client()
    r = client.post("/api/aws/deploy", json=_deploy_body(region="not-a-region!"))
    assert r.status_code == 400, (r.status_code, r.text)
    assert "metadata" not in _CAPTURED, "no job should be created for a bad region"


# ── _fetch_instances region grouping ─────────────────────────────────────────

class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *a, **k):
        return self

    def order_by(self, *a, **k):
        return self

    def all(self):
        return self._rows


class _FakeDB:
    def __init__(self, rows):
        self._rows = rows

    def query(self, *_a, **_k):
        return _FakeQuery(self._rows)


class _FakeDeployJob:
    job_type = "ec2_deploy"
    status = "completed"

    def __init__(self, jid, instance_id, region, workgroup="eng"):
        self.id = jid
        self.workgroup = workgroup
        self.created_by = "tester"
        self.metadata_dict = {"instance_id": instance_id, "region": region}


def test_fetch_instances_groups_and_tags_by_region():
    _install_stubs()
    calls = []

    async def _fake_describe(region, ids):
        calls.append((region, list(ids)))
        return [{"instance_id": i, "name": i, "state": "running"} for i in ids]

    aws.aws_service.describe_instances = _fake_describe

    jobs = [
        _FakeDeployJob("j-east", "i-east", "us-east-2"),
        _FakeDeployJob("j-west", "i-west", "us-west-2"),
    ]
    result = asyncio.run(aws._fetch_instances(_FakeDB(jobs)))

    # Each instance described in its own region, ids never mixed across regions.
    by_region = {region: ids for region, ids in calls}
    assert by_region == {"us-east-2": ["i-east"], "us-west-2": ["i-west"]}

    # Every returned instance is tagged with the region it lives in.
    region_by_instance = {r["instance_id"]: r["region"] for r in result}
    assert region_by_instance == {"i-east": "us-east-2", "i-west": "us-west-2"}


def test_fetch_instances_missing_region_falls_back_to_default():
    _install_stubs()
    calls = []

    async def _fake_describe(region, ids):
        calls.append((region, list(ids)))
        return [{"instance_id": i} for i in ids]

    aws.aws_service.describe_instances = _fake_describe

    # A pre-multi-region job with no recorded region → default region.
    legacy = _FakeDeployJob("j-old", "i-old", "us-east-2")
    legacy.metadata_dict = {"instance_id": "i-old"}  # no "region" key
    result = asyncio.run(aws._fetch_instances(_FakeDB([legacy])))

    assert calls == [(DEFAULT_REGION, ["i-old"])]
    assert result[0]["region"] == DEFAULT_REGION


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
            traceback.print_exc()
    sys.exit(1 if failures else 0)
