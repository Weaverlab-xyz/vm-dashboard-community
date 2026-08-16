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
  * both deploy routes reject a subnet/NSG from another region up front
    (``_reject_cross_region_network``) instead of letting the runner discover it.
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

# _install_stubs() replaces this on the shared module, so keep a handle to the real
# one for the tests that exercise it directly.
_REAL_RESOURCE_LOCATIONS = azure.azure_service.resource_locations


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


def _fake_resource_locations(regions: dict):
    """Stand in for the ARM lookup behind ``_reject_cross_region_network``.

    ``regions`` maps ARM id -> the region that id lives in; anything not listed
    resolves to "" (undeterminable), which the guard must treat as "no conflict".
    """
    async def _lookup(resource_ids):
        return {rid: regions.get(rid, "") for rid in (resource_ids or [])}
    return _lookup


def _install_stubs(resource_regions: dict = None):
    azure._loc = lambda: DEFAULT_LOCATION
    azure._rg = lambda: "rg-default"
    azure._rg_for = lambda loc: f"rg-{loc}"           # deterministic per-region RG
    azure.workgroup_service.get = lambda db, name: _Workgroup()
    azure.job_service.create_job = _fake_create_job
    azure.job_service.set_cloud_resource_id = lambda *a, **k: None
    azure.job_service.log_audit = lambda *a, **k: None
    azure._run_deploy = _noop_async
    # No creds in a hermetic test, so the real lookup would fail open anyway — stub it
    # so nothing reaches out, and so a test can say where a subnet/NSG lives.
    azure.azure_service.resource_locations = _fake_resource_locations(resource_regions or {})
    azure.deploy_batch.reject_name_collisions = lambda *a, **k: None
    azure.deploy_batch.enforce_admission = _noop_async
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


# ── Cross-region subnet / NSG guard ───────────────────────────────────────────
# VNets and NSGs are regional, and an Azure subnet id names the VNet's RESOURCE
# GROUP, not its region — so a subnet carried over from a previous location looks
# perfectly plausible (the sandbox calls its VNet/subnet the same thing in every
# region). Azure only rejects the pair when the NIC is created, which on the bulk
# route is after N child job rows exist: the whole batch fails. Both routes now
# reject it before anything is created. Regression: the bulk modal reloaded the
# subnet options on a Location change but never cleared the selection.

_SUB = "/subscriptions/00000000-0000-0000-0000-000000000000"
_CENTRAL_SUBNET = (f"{_SUB}/resourceGroups/dashboard-sandbox-rg/providers"
                   "/Microsoft.Network/virtualNetworks/dashboard-sandbox-vnet"
                   "/subnets/vm-subnet")
_CENTRAL_NSG = (f"{_SUB}/resourceGroups/dashboard-sandbox-rg/providers"
                "/Microsoft.Network/networkSecurityGroups/dashboard-sandbox-vm-nsg")


def _bulk_body(**over):
    body = {
        "items": [{"vm_name": "web-01"}, {"vm_name": "web-02"}],
        "image_id": "/subscriptions/x/img-1",
        "subnet_id": _CENTRAL_SUBNET,
        "workgroup": "eng",
        "ssh_public_key": "ssh-rsa AAAAB3demo",
    }
    body.update(over)
    return body


def test_deploy_rejects_subnet_from_another_region():
    _install_stubs({_CENTRAL_SUBNET: "centralus"})
    _CAPTURED.clear()
    client = _make_client()
    r = client.post("/api/azure/deploy",
                    json=_deploy_body(location="westeurope", subnet_id=_CENTRAL_SUBNET))
    assert r.status_code == 400, (r.status_code, r.text)
    # Both regions must be named — that is the whole point over Azure's own
    # "InvalidResourceReference", which names neither.
    detail = r.json()["detail"]
    assert "centralus" in detail and "westeurope" in detail, detail
    assert "metadata" not in _CAPTURED, "no job should be created for a mismatched subnet"


def test_deploy_rejects_nsg_from_another_region():
    """NSGs are regional too, and the bulk modal's NSG checkboxes go stale the same
    way the subnet picker does."""
    _install_stubs({_CENTRAL_NSG: "centralus"})
    _CAPTURED.clear()
    client = _make_client()
    r = client.post("/api/azure/deploy", json=_deploy_body(
        location="westeurope", subnet_id=f"{_SUB}/eu-subnet", nsg_ids=[_CENTRAL_NSG]))
    assert r.status_code == 400, (r.status_code, r.text)
    assert "centralus" in r.json()["detail"], r.text
    assert "metadata" not in _CAPTURED


def test_deploy_accepts_matching_or_unresolvable_subnet():
    # Same region as the deploy.
    _install_stubs({_CENTRAL_SUBNET: "centralus"})
    _CAPTURED.clear()
    client = _make_client()
    r = client.post("/api/azure/deploy",
                    json=_deploy_body(location="centralus", subnet_id=_CENTRAL_SUBNET))
    assert r.status_code == 200, (r.status_code, r.text)
    assert _CAPTURED["metadata"]["subnet_id"] == _CENTRAL_SUBNET
    # Azure reports the region as its display name in places ("Central US"); the
    # comparison is on the canonical compact form, so this must NOT be a mismatch.
    _install_stubs({_CENTRAL_SUBNET: "Central US"})
    _CAPTURED.clear()
    r = client.post("/api/azure/deploy",
                    json=_deploy_body(location="centralus", subnet_id=_CENTRAL_SUBNET))
    assert r.status_code == 200, (r.status_code, r.text)
    # A subnet whose region can't be determined (no Reader on the VNet's RG, a
    # malformed id) must fail OPEN — the runner's own check still stands behind the
    # guard, and blocking here would break deploys the dashboard can't introspect.
    _install_stubs({})
    _CAPTURED.clear()
    r = client.post("/api/azure/deploy",
                    json=_deploy_body(location="westeurope", subnet_id=_CENTRAL_SUBNET))
    assert r.status_code == 200, (r.status_code, r.text)


def test_bulk_deploy_rejects_subnet_from_another_region():
    _install_stubs({_CENTRAL_SUBNET: "centralus"})
    _CAPTURED.clear()
    client = _make_client()
    r = client.post("/api/azure/bulk-deploy", json=_bulk_body(location="westeurope"))
    assert r.status_code == 400, (r.status_code, r.text)
    detail = r.json()["detail"]
    assert "centralus" in detail and "westeurope" in detail, detail
    # The batch must be rejected before any child row exists — two queued children
    # that the runner then fails one by one is exactly what this replaces.
    assert "metadata" not in _CAPTURED, "no child job should be created for the batch"


def test_bulk_deploy_still_queues_a_matching_batch():
    """The contrast to the test above: same route, matching region, rows created."""
    _install_stubs({_CENTRAL_SUBNET: "centralus"})
    _CAPTURED.clear()
    client = _make_client()
    r = client.post("/api/azure/bulk-deploy", json=_bulk_body(location="centralus"))
    assert r.status_code == 200, (r.status_code, r.text)
    assert len(r.json()["jobs"]) == 2, r.text
    assert _CAPTURED["job_type"] == "azure_bulk_deploy"   # the parent, created last


# ── azure_service.resource_locations (the lookup behind the guard) ────────────

def test_resource_locations_reads_the_region_off_the_right_resource():
    """An ARM id carries the resource group, not the region — so subnet ids resolve
    through the VNet and NSG ids through the NSG. Anything that is neither has no
    region to compare, and must report "" rather than guess."""
    from web_dashboard.services import azure_service as svc

    class _Net:
        virtual_networks = type("V", (), {
            "get": staticmethod(lambda rg, name: type("R", (), {"location": "Central US"})())})()
        network_security_groups = type("N", (), {
            "get": staticmethod(lambda rg, name: type("R", (), {"location": "westeurope"})())})()

    original = svc._get_network
    svc._get_network = lambda cred, sub_id: _Net()
    try:
        out = svc._resource_locations_sync(
            None, "sub-1", [_CENTRAL_SUBNET, _CENTRAL_NSG, "/subscriptions/x/nonsense"])
    finally:
        svc._get_network = original
    assert out[_CENTRAL_SUBNET] == "Central US"
    assert out[_CENTRAL_NSG] == "westeurope"
    assert out["/subscriptions/x/nonsense"] == ""


def test_resource_locations_fails_open_when_the_lookup_cannot_run():
    """No creds / no Reader on the VNet's resource group must not block deploys: the
    guard treats "" as "no conflict", and the runner-side check still stands."""
    from web_dashboard.services import azure_service as svc

    async def _boom():
        raise RuntimeError("credentials not configured")

    original = svc._ensure_creds
    svc._ensure_creds = _boom
    try:
        assert asyncio.run(_REAL_RESOURCE_LOCATIONS([_CENTRAL_SUBNET])) == {}
        # Nothing to look up short-circuits before creds are touched at all.
        assert asyncio.run(_REAL_RESOURCE_LOCATIONS(["", None])) == {}
    finally:
        svc._ensure_creds = original


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
