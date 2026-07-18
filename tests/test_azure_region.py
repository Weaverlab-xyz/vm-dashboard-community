"""Multi-region support (Phase 1, Azure): location honoured from the request.

Azure VM deploys were already location-aware (per-request ``location``, admission
gate on that location, per-region resource-group resolution via
``resolve_azure_region``, location-scoped ``/network-options``). Phase 1 adds the
missing input validation and pins the multi-region behaviour so it can't regress:

  * ``_resolve_location`` — an explicit well-formed location wins (normalised to
    the canonical compact form), blank/None falls back to the configured default,
    and a malformed location is rejected with 400.
  * ``POST /api/azure/deploy`` feeds the admission gate the *requested* location
    and records location + the region-resolved resource group on the job.
  * ``_fetch_vms`` lists VMs deployed into a non-default region's resource group
    (via the per-job ``resource_group`` fallback), so multi-region VMs stay
    listable.

Follows the hermetic TestClient pattern from test_containers_page_resilience.py.
Heavy cloud deps (fastapi/azure-sdk/…) are only present in CI; when missing the
file SKIPs cleanly so the per-file runner stays green.

Run: python tests/test_azure_region.py   (or under pytest)
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient

    from web_dashboard.api import azure
    from web_dashboard.api.auth import get_current_user
    from web_dashboard.database import get_db
except Exception as exc:  # pragma: no cover — deps absent outside CI
    try:
        import pytest
        pytest.skip(f"azure api import unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)


DEFAULT_LOCATION = "centralus"


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
    azure._loc = lambda: DEFAULT_LOCATION
    azure._rg = lambda: "rg-default"
    azure._rg_for = lambda loc: f"rg-{loc}"           # deterministic per-region RG
    azure.workgroup_service.get = lambda db, name: _Workgroup()
    azure.job_service.create_job = _fake_create_job
    azure.job_service.set_cloud_resource_id = lambda *a, **k: None
    azure.job_service.log_audit = lambda *a, **k: None
    azure._run_deploy = _noop_async
    from web_dashboard.services import admission_service
    admission_service.enforce = _fake_enforce


def _make_client() -> TestClient:
    app = FastAPI()
    app.include_router(azure.router)
    app.dependency_overrides[get_current_user] = lambda: _AdminUser()
    app.dependency_overrides[get_db] = lambda: object()
    return TestClient(app, raise_server_exceptions=False)


def _deploy_body(**over):
    body = {
        "image_id": "/subscriptions/x/img-1",
        "vm_name": "vm1",
        "subnet_id": "/subscriptions/x/subnet-1",
        "workgroup": "eng",
        "ssh_public_key": "ssh-rsa AAAAB3demo",
    }
    body.update(over)
    return body


# ── _resolve_location ─────────────────────────────────────────────────────────

def test_resolve_location_default_normalise_and_invalid():
    _install_stubs()
    assert azure._resolve_location(None) == DEFAULT_LOCATION
    assert azure._resolve_location("   ") == DEFAULT_LOCATION
    assert azure._resolve_location("westeurope") == "westeurope"
    assert azure._resolve_location("East US 2") == "eastus2"      # normalised
    assert azure._resolve_location("EastUS2") == "eastus2"
    for bad in ("bad-loc", "east!us", "us/east"):
        try:
            azure._resolve_location(bad)
        except HTTPException as e:
            assert e.status_code == 400
        else:
            raise AssertionError(f"expected 400 for location {bad!r}")


# ── POST /api/azure/deploy ────────────────────────────────────────────────────

def test_deploy_uses_requested_location_and_region_rg():
    _install_stubs()
    _CAPTURED.clear()
    client = _make_client()
    r = client.post("/api/azure/deploy", json=_deploy_body(location="westeurope"))
    assert r.status_code == 200, (r.status_code, r.text)
    # Admission sees the requested region, and the VM lands in that region's RG.
    assert _CAPTURED["admission_request"]["region"] == "westeurope"
    assert _CAPTURED["metadata"]["location"] == "westeurope"
    assert _CAPTURED["metadata"]["resource_group"] == "rg-westeurope"


def test_deploy_defaults_location_when_omitted():
    _install_stubs()
    _CAPTURED.clear()
    client = _make_client()
    r = client.post("/api/azure/deploy", json=_deploy_body())
    assert r.status_code == 200, (r.status_code, r.text)
    assert _CAPTURED["metadata"]["location"] == DEFAULT_LOCATION
    assert _CAPTURED["metadata"]["resource_group"] == f"rg-{DEFAULT_LOCATION}"
    assert _CAPTURED["admission_request"]["region"] == DEFAULT_LOCATION


def test_deploy_rejects_invalid_location():
    _install_stubs()
    _CAPTURED.clear()
    client = _make_client()
    r = client.post("/api/azure/deploy", json=_deploy_body(location="west/europe"))
    assert r.status_code == 400, (r.status_code, r.text)
    assert "metadata" not in _CAPTURED, "no job should be created for a bad location"


# ── _fetch_vms multi-region listing ──────────────────────────────────────────

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
    job_type = "azure_deploy"

    def __init__(self, jid, vm_name, resource_group, location, workgroup="eng"):
        self.id = jid
        self.workgroup = workgroup
        self.created_by = "tester"
        self.metadata_dict = {
            "vm_name": vm_name,
            "resource_group": resource_group,
            "location": location,
        }


def test_fetch_vms_lists_non_default_region_rg():
    _install_stubs()

    async def _describe_vms(rg):
        return []  # nothing in the default RG

    async def _get_vm(rg, vm_name):
        # The VM lives in the non-default region's RG, fetched individually.
        return {"vm_id": "1", "name": vm_name, "state": "running",
                "location": "westeurope", "resource_group": rg}

    azure.azure_service.describe_vms = _describe_vms
    azure.azure_service.get_vm = _get_vm

    job = _FakeDeployJob("j-eu", "vm-eu", "rg-westeurope", "westeurope")
    result = asyncio.run(azure._fetch_vms(_FakeDB([job])))

    names = {v["name"]: v.get("location") for v in result}
    assert names == {"vm-eu": "westeurope"}, names


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
