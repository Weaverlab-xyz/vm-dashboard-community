"""Resilience smoke test for the Containers page data endpoints.

Validates that the page's data sources behave whether or not a cloud provider
or Portainer is configured: each endpoint returns a clean result (200) or a
handled 503 the frontend shows per-section — never an unhandled 500 that would
break the page. Auth is bypassed with an admin stub.

Run: python tests/test_containers_page_resilience.py   (or under pytest)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI
from fastapi.testclient import TestClient

from web_dashboard.api import containers
from web_dashboard.api.auth import get_current_user
from web_dashboard.services.aws_service import AWSError
from web_dashboard.services.azure_service import AzureError
from web_dashboard.services.gcp_service import GCPError


class _AdminUser:
    is_effective_admin = True
    username = "tester"
    effective_permissions_dict = {}


def _make_client() -> TestClient:
    app = FastAPI()
    app.include_router(containers.router)
    app.dependency_overrides[get_current_user] = lambda: _AdminUser()
    return TestClient(app, raise_server_exceptions=False)


def _patch_unconfigured(monkey):
    """Every provider raises its typed 'not configured' error."""
    async def aws_boom(*a, **k):
        raise AWSError("AWS credentials not configured.")

    async def az_boom(*a, **k):
        raise AzureError("Azure not configured.")

    async def gcp_jumpoints_boom(*a, **k):
        raise GCPError("GCP not reachable.")

    monkey["aws"] = containers.aws_service.list_ecs_tasks
    monkey["az"] = containers.azure_service.list_aci_container_instances
    containers.aws_service.list_ecs_tasks = aws_boom
    containers.azure_service.list_aci_container_instances = az_boom
    # _aci_rg() reads the resource group via config_service (a DB-backed cache).
    # Stub it so the ACI path stays hermetic — mirrors the _gcp_project_id stub
    # below; otherwise an uninitialised DB turns the handled-503 path into a 500.
    monkey["aci_rg"] = containers._aci_rg
    containers._aci_rg = lambda: "test-rg"

    from web_dashboard.services import gcp_service
    monkey["gcp_j"] = gcp_service.list_gce_jumpoints
    monkey["gcp_c"] = gcp_service.list_gce_compose
    monkey["gcp_cr"] = gcp_service.list_cloud_run_jobs
    gcp_service.list_gce_jumpoints = gcp_jumpoints_boom
    gcp_service.list_gce_compose = gcp_jumpoints_boom
    gcp_service.list_cloud_run_jobs = gcp_jumpoints_boom
    # GCE endpoints first check project config — exercise the "configured but
    # unreachable" path by returning a project id.
    monkey["proj"] = containers._gcp_project_id
    containers._gcp_project_id = lambda: "test-project"


def _patch_configured(monkey):
    """Every provider is configured and returns empty inventories."""
    async def ok_list(*a, **k):
        return []

    monkey["aws"] = containers.aws_service.list_ecs_tasks
    monkey["az"] = containers.azure_service.list_aci_container_instances
    containers.aws_service.list_ecs_tasks = ok_list
    containers.azure_service.list_aci_container_instances = ok_list
    monkey["aci_rg"] = containers._aci_rg
    containers._aci_rg = lambda: "test-rg"

    from web_dashboard.services import gcp_service
    monkey["gcp_j"] = gcp_service.list_gce_jumpoints
    monkey["gcp_c"] = gcp_service.list_gce_compose
    monkey["gcp_cr"] = gcp_service.list_cloud_run_jobs
    gcp_service.list_gce_jumpoints = ok_list
    gcp_service.list_gce_compose = ok_list
    gcp_service.list_cloud_run_jobs = ok_list
    monkey["proj"] = containers._gcp_project_id
    containers._gcp_project_id = lambda: "test-project"


def _restore(monkey):
    from web_dashboard.services import gcp_service
    containers.aws_service.list_ecs_tasks = monkey["aws"]
    containers.azure_service.list_aci_container_instances = monkey["az"]
    containers._aci_rg = monkey["aci_rg"]
    gcp_service.list_gce_jumpoints = monkey["gcp_j"]
    gcp_service.list_gce_compose = monkey["gcp_c"]
    gcp_service.list_cloud_run_jobs = monkey["gcp_cr"]
    containers._gcp_project_id = monkey["proj"]


_CLOUD_ENDPOINTS = [
    "/api/containers/ecs-tasks",
    "/api/containers/aci-containers",
    "/api/containers/gce-jumpoints",
    "/api/containers/gce-compose",
    "/api/containers/gce-cloud-run-jobs",
]


def test_cloud_endpoints_handled_when_unconfigured():
    client = _make_client()
    saved = {}
    _patch_unconfigured(saved)
    try:
        for path in _CLOUD_ENDPOINTS:
            r = client.get(path)
            assert r.status_code == 503, f"{path} → {r.status_code} (expected handled 503)"
            assert r.status_code != 500
    finally:
        _restore(saved)


def test_cloud_endpoints_ok_when_configured():
    client = _make_client()
    saved = {}
    _patch_configured(saved)
    try:
        for path in _CLOUD_ENDPOINTS:
            r = client.get(path)
            assert r.status_code == 200, f"{path} → {r.status_code} (expected 200)"
            assert r.json()["count"] == 0
    finally:
        _restore(saved)


def test_gce_endpoints_503_when_project_missing():
    client = _make_client()
    saved = {"proj": containers._gcp_project_id}
    containers._gcp_project_id = lambda: ""
    try:
        for path in ("/api/containers/gce-jumpoints", "/api/containers/gce-compose",
                     "/api/containers/gce-cloud-run-jobs"):
            r = client.get(path)
            assert r.status_code == 503, f"{path} → {r.status_code}"
    finally:
        containers._gcp_project_id = saved["proj"]


def _patch_config_service(saved, gcp_project: str = ""):
    """Stub config_service reads used by the Portainer-node routes.

    ``get_portainer_node`` reads portainer_url / portainer_pat BEFORE the "is this
    cloud configured" check, and ``firewall_status`` reads the CIDR keys — both would
    hit an uninitialised DB here and turn a handled path into a 500. Same reason this
    file stubs ``_aci_rg`` above.

    ``gcp_project`` is how a test says "GCP IS configured": the node routes resolve
    their placement through ``managed_node_service``, which reads ``gcp_project_id``
    from config — so config is the only seam that decides it."""
    from web_dashboard.services import config_service
    saved["cfg_get"] = config_service.get
    saved["cfg_get_bool"] = config_service.get_bool
    config_service.get = lambda key, default=None: (
        gcp_project if key == "gcp_project_id" else "")
    config_service.get_bool = lambda key, default=False: False


def _restore_config_service(saved):
    from web_dashboard.services import config_service
    config_service.get = saved["cfg_get"]
    config_service.get_bool = saved["cfg_get_bool"]


_PORTAINER_NODE_ENDPOINTS = [
    "/api/containers/portainer/node",
    "/api/containers/portainer/node/firewall",
    "/api/containers/portainer/node/deploy-options",
]


def test_portainer_node_endpoints_handled_when_gcp_unreachable():
    """A configured project whose GCE list blows up must surface as a handled 503
    (or a clean 200 for the routes that make no cloud call) — never a 500."""
    client = _make_client()
    saved = {}
    _patch_config_service(saved, gcp_project="test-project")

    from web_dashboard.services import gcp_service
    saved["list_portainer"] = gcp_service.list_gce_portainer
    saved["proj"] = containers._gcp_project_id

    async def gcp_boom(*a, **k):
        raise GCPError("GCP not reachable.")

    gcp_service.list_gce_portainer = gcp_boom
    containers._gcp_project_id = lambda: "test-project"
    try:
        for path in _PORTAINER_NODE_ENDPOINTS:
            r = client.get(path)
            assert r.status_code in (200, 503), f"{path} → {r.status_code} (expected 200 or handled 503)"
            assert r.status_code != 500, f"{path} returned an unhandled 500"
        # The listing route is the one that touches GCE, so it must be the 503.
        assert client.get("/api/containers/portainer/node").status_code == 503
    finally:
        gcp_service.list_gce_portainer = saved["list_portainer"]
        containers._gcp_project_id = saved["proj"]
        _restore_config_service(saved)


def test_portainer_node_returns_shell_when_project_missing():
    """Deliberate contract: with no GCP project the listing route returns 200 with
    configured=false (NOT 503), so the Containers tab can render its setup card."""
    client = _make_client()
    saved = {}
    _patch_config_service(saved)
    saved["proj"] = containers._gcp_project_id
    containers._gcp_project_id = lambda: ""
    try:
        r = client.get("/api/containers/portainer/node")
        assert r.status_code == 200, f"{r.status_code} (expected the not-configured shell)"
        body = r.json()
        assert body["configured"] is False, body
        assert body["nodes"] == [] and body["count"] == 0, body
        # The other two make no cloud call, so they stay clean too.
        for path in _PORTAINER_NODE_ENDPOINTS[1:]:
            assert client.get(path).status_code in (200, 503)
    finally:
        containers._gcp_project_id = saved["proj"]
        _restore_config_service(saved)


def test_portainer_node_ok_when_configured():
    client = _make_client()
    saved = {}
    _patch_config_service(saved, gcp_project="test-project")

    from web_dashboard.services import gcp_service
    saved["list_portainer"] = gcp_service.list_gce_portainer
    saved["proj"] = containers._gcp_project_id

    async def ok_list(*a, **k):
        return []

    gcp_service.list_gce_portainer = ok_list
    containers._gcp_project_id = lambda: "test-project"
    try:
        r = client.get("/api/containers/portainer/node")
        assert r.status_code == 200, r.status_code
        body = r.json()
        assert body["count"] == 0 and body["configured"] is True, body
    finally:
        gcp_service.list_gce_portainer = saved["list_portainer"]
        containers._gcp_project_id = saved["proj"]
        _restore_config_service(saved)


def test_deploy_compose_validation():
    client = _make_client()
    # bad provider → 400 (before any DB/job work)
    r = client.post("/api/containers/deploy-compose", json={
        "provider": "k8s", "name": "x", "compose_backend": "s3", "compose_file": "a.yml",
    })
    assert r.status_code == 400, r.status_code
    # missing file reference → 400
    r = client.post("/api/containers/deploy-compose", json={
        "provider": "ecs", "name": "x", "compose_backend": "", "compose_file": "",
    })
    assert r.status_code == 400, r.status_code


if __name__ == "__main__":
    import sys, traceback
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
