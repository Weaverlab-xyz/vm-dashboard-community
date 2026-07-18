"""Multi-region support (Phase 1, GCP): zone/region honoured from the request.

GCE deploys already carried a per-request ``zone``, but the admission guardrail
checked the *global* region and a malformed zone was accepted silently. This pins
the Phase-1 fixes:

  * ``_resolve_zone`` — an explicit well-formed zone wins, blank/None falls back
    to the configured default, and a malformed zone is rejected (so a typo can't
    silently deploy into the default zone).
  * ``_region_from_zone`` — derives the region a zone belongs to.
  * ``POST /api/gcp/deploy`` feeds the admission gate the region derived from the
    *requested* zone (not the global default) and records zone+region on the job.
  * ``_build_gcp_instances`` tags each instance with its region.

Follows the hermetic TestClient pattern from test_containers_page_resilience.py.
Heavy cloud deps (fastapi/google-cloud/…) are only present in CI; when missing
the file SKIPs cleanly so the per-file runner stays green.

Run: python tests/test_gcp_region.py   (or under pytest)
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient

    from web_dashboard.api import gcp
    from web_dashboard.api.auth import get_current_user
    from web_dashboard.database import get_db
except Exception as exc:  # pragma: no cover — deps absent outside CI
    try:
        import pytest
        pytest.skip(f"gcp api import unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)


DEFAULT_ZONE = "us-central1-a"
DEFAULT_REGION = "us-central1"


class _AdminUser:
    is_effective_admin = True
    is_admin = True
    username = "tester"
    workgroups_list: list = []
    effective_permissions_dict: dict = {}


class _Workgroup:
    name = "eng"


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


def _fake_enforce(*_a, **kw):
    _CAPTURED["admission_request"] = kw.get("request")


def _install_stubs():
    gcp._gcp_project = lambda: "proj-1"
    gcp._gcp_zone = lambda: DEFAULT_ZONE
    gcp._gcp_region = lambda: DEFAULT_REGION
    gcp.workgroup_service.get = lambda db, name: _Workgroup()
    gcp.job_service.create_job = _fake_create_job
    gcp.job_service.set_cloud_resource_id = lambda *a, **k: None
    gcp.job_service.log_audit = lambda *a, **k: None
    gcp._run_deploy = _noop_async
    from web_dashboard.services import admission_service
    admission_service.enforce = _fake_enforce


def _make_client() -> TestClient:
    app = FastAPI()
    app.include_router(gcp.router)
    app.dependency_overrides[get_current_user] = lambda: _AdminUser()
    app.dependency_overrides[get_db] = lambda: object()
    return TestClient(app, raise_server_exceptions=False)


def _deploy_body(**over):
    body = {
        "image_self_link": "projects/x/global/images/img-1",
        "instance_name": "vm1",
        "workgroup": "eng",
    }
    body.update(over)
    return body


# ── _resolve_zone / _region_from_zone ────────────────────────────────────────

def test_resolve_zone_default_explicit_and_invalid():
    _install_stubs()
    assert gcp._resolve_zone(None) == DEFAULT_ZONE
    assert gcp._resolve_zone("   ") == DEFAULT_ZONE
    assert gcp._resolve_zone("europe-west1-b") == "europe-west1-b"
    assert gcp._resolve_zone("US-Central1-A") == "us-central1-a"   # normalised
    for bad in ("bogus", "us-central1", "uscentral1a", "us-central1-ab"):
        try:
            gcp._resolve_zone(bad)
        except HTTPException as e:
            assert e.status_code == 400
        else:
            raise AssertionError(f"expected 400 for zone {bad!r}")


def test_region_from_zone():
    _install_stubs()
    assert gcp._region_from_zone("us-central1-a") == "us-central1"
    assert gcp._region_from_zone("europe-west1-b") == "europe-west1"
    assert gcp._region_from_zone("asia-southeast1-c") == "asia-southeast1"
    # Unparseable → configured default region.
    assert gcp._region_from_zone("garbage") == DEFAULT_REGION


# ── POST /api/gcp/deploy ──────────────────────────────────────────────────────

def test_deploy_admission_region_derived_from_requested_zone():
    _install_stubs()
    _CAPTURED.clear()
    client = _make_client()
    r = client.post("/api/gcp/deploy", json=_deploy_body(zone="europe-west1-b"))
    assert r.status_code == 200, (r.status_code, r.text)
    # The guardrail must see the region where the VM actually lands, not the default.
    assert _CAPTURED["admission_request"]["region"] == "europe-west1"
    assert _CAPTURED["admission_request"]["zone"] == "europe-west1-b"
    assert _CAPTURED["metadata"]["zone"] == "europe-west1-b"
    assert _CAPTURED["metadata"]["region"] == "europe-west1"


def test_deploy_defaults_zone_when_omitted():
    _install_stubs()
    _CAPTURED.clear()
    client = _make_client()
    r = client.post("/api/gcp/deploy", json=_deploy_body())
    assert r.status_code == 200, (r.status_code, r.text)
    assert _CAPTURED["metadata"]["zone"] == DEFAULT_ZONE
    assert _CAPTURED["metadata"]["region"] == DEFAULT_REGION
    assert _CAPTURED["admission_request"]["region"] == DEFAULT_REGION


def test_deploy_rejects_invalid_zone():
    _install_stubs()
    _CAPTURED.clear()
    client = _make_client()
    r = client.post("/api/gcp/deploy", json=_deploy_body(zone="not-a-zone"))
    assert r.status_code == 400, (r.status_code, r.text)
    assert "metadata" not in _CAPTURED, "no job should be created for a bad zone"


# ── _build_gcp_instances region tagging ──────────────────────────────────────

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
    def __init__(self, jid, instance_name, zone, workgroup="eng"):
        import json
        self.id = jid
        self.workgroup = workgroup
        self.created_by = "tester"
        self.extra_data = json.dumps({"instance_name": instance_name, "zone": zone})


def test_build_instances_tags_region_from_zone():
    _install_stubs()
    gcp.cache_service.set = _noop_async

    async def _fake_describe(project_id, zone, instance_names):
        return [{"instance_name": n, "zone": zone, "status": "RUNNING"} for n in instance_names]

    gcp.gcp_service.describe_instances = _fake_describe

    jobs = [
        _FakeDeployJob("j-us", "vm-us", "us-central1-a"),
        _FakeDeployJob("j-eu", "vm-eu", "europe-west1-b"),
    ]
    instances = asyncio.run(gcp._build_gcp_instances(_FakeDB(jobs), "proj-1"))

    region_by_name = {i["instance_name"]: i["region"] for i in instances}
    assert region_by_name == {"vm-us": "us-central1", "vm-eu": "europe-west1"}


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
