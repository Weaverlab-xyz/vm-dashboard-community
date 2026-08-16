"""Unit tests for rancher_service — the direct-HTTPS Rancher v3 API client.

The central Rancher runs on a public GCE COS node, so the dashboard calls the v3
API directly over HTTPS (httpx). We exercise the orchestration + JSON parsing
against an ``httpx.MockTransport`` (no live Rancher, no cluster) by monkeypatching
``rs._client`` to bind a mock transport. config_service is stubbed so the
connection resolves without pydantic settings.

Runs under pytest, or standalone: python tests/test_rancher_service.py
"""
import asyncio
import os
import sys
import types

import httpx

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _install_stubs():
    cfg = types.ModuleType("web_dashboard.services.config_service")
    store = {
        "rancher_server_url": "https://rancher.example",
        "rancher_api_token": "token-cfg:secret",
    }
    cfg.get = lambda key, default="", workgroup=None: store.get(key, default)
    cfg.get_bool = lambda key, default=False: bool(store.get(key, default))
    sys.modules["web_dashboard.services.config_service"] = cfg


_install_stubs()
try:
    from web_dashboard.services import rancher_service as rs
except Exception as exc:  # pragma: no cover — skip if deps missing
    try:
        import pytest
        pytest.skip(f"rancher_service import unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)


def _mock(handler):
    """Monkeypatch rs._client to return an AsyncClient wired to a MockTransport,
    preserving the real Authorization/base_url behaviour so tests can assert them."""
    def fake_client(token="", *, base_url=""):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        url = base_url or rs._server_url()
        return httpx.AsyncClient(base_url=url, headers=headers,
                                 transport=httpx.MockTransport(handler))
    rs._client = fake_client


def test_bootstrap_direct_orchestration():
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path, request.headers.get("authorization", "")))
        if request.url.path == "/v3-public/localProviders/local":
            return httpx.Response(201, json={"token": "login-abc"})
        if request.url.path == "/v3/settings/server-url":
            return httpx.Response(200, json={"name": "server-url"})
        if request.url.path == "/v3/token":
            return httpx.Response(201, json={"token": "token-xyz:secret"})
        return httpx.Response(404, json={})

    _mock(handler)
    tok = asyncio.run(rs.bootstrap_direct(bootstrap_password="bootpw",
                                          server_url="https://rancher.example"))
    assert tok == "token-xyz:secret"
    # login (no auth) → PUT server-url (session token) → mint token (session token)
    assert seen[0][:2] == ("POST", "/v3-public/localProviders/local")
    assert seen[1][:2] == ("PUT", "/v3/settings/server-url")
    assert seen[1][2] == "Bearer login-abc"
    assert seen[2][:2] == ("POST", "/v3/token")


def test_create_import_cluster_direct():
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path, request.headers.get("authorization", "")))
        if request.url.path == "/v3/cluster":
            return httpx.Response(201, json={"id": "c-m-abc", "type": "cluster"})
        if request.url.path == "/v3/clusterregistrationtoken":
            return httpx.Response(201, json={"manifestUrl": "https://rancher.example/reg/xyz.yaml"})
        return httpx.Response(404, json={})

    _mock(handler)
    cid, url = asyncio.run(rs.create_import_cluster_direct(name="demo"))
    assert cid == "c-m-abc"
    assert url == "https://rancher.example/reg/xyz.yaml"
    assert seen[0][:2] == ("POST", "/v3/cluster")
    # token falls back to config (token-cfg:secret) when not passed explicitly
    assert seen[0][2] == "Bearer token-cfg:secret"
    assert seen[1][:2] == ("POST", "/v3/clusterregistrationtoken")


def test_delete_cluster_direct_ignores_404():
    def handler(request):
        return httpx.Response(404, json={})
    _mock(handler)
    # 404 = already gone → no exception.
    asyncio.run(rs.delete_cluster_direct(cluster_id="c-m-gone"))


def test_complete_first_run_direct():
    seen = []
    bodies = {}

    def handler(request):
        seen.append((request.method, request.url.path))
        import json as _j
        bodies[request.url.path] = _j.loads(request.content or b"{}")
        return httpx.Response(200, json={})

    _mock(handler)
    result = asyncio.run(rs.complete_first_run_direct(
        api_token="token-cfg:secret", server_url="https://rancher.example",
        current_password="bootpw-123456", new_password="adminpw-123456"))
    assert result["password_changed"] is True
    # changepassword first (clears the Welcome gate), then the 3 wizard settings.
    assert ("POST", "/v3/users") in seen
    assert bodies["/v3/users"] == {"currentPassword": "bootpw-123456", "newPassword": "adminpw-123456"}
    for s in ("eula-agreed", "telemetry-opt", "first-login"):
        assert ("PUT", f"/v3/settings/{s}") in seen


def test_complete_first_run_changepassword_failure_is_non_fatal():
    def handler(request):
        if request.url.path == "/v3/users":
            return httpx.Response(422, json={"message": "Password must be at least 12 characters"})
        return httpx.Response(200, json={})  # settings still succeed

    _mock(handler)
    # Must NOT raise — the node is usable regardless; the reason is surfaced.
    result = asyncio.run(rs.complete_first_run_direct(
        api_token="t", server_url="https://rancher.example",
        current_password="short", new_password="short"))
    assert result["password_changed"] is False
    assert "12 characters" in result["reason"]


def test_not_configured_raises():
    # Force _cfg to resolve empty (skips the pydantic-settings env fallback, which
    # isn't importable in this bare test env) so server_url/api_token are missing.
    orig_cfg = rs._cfg
    rs._cfg = lambda key, default="": default
    try:
        raised = False
        try:
            asyncio.run(rs.create_import_cluster_direct(name="x"))
        except rs.RancherNotConfigured:
            raised = True
        assert raised, "expected RancherNotConfigured when server_url/api_token unset"
    finally:
        rs._cfg = orig_cfg


if __name__ == "__main__":
    test_bootstrap_direct_orchestration()
    test_create_import_cluster_direct()
    test_complete_first_run_direct()
    test_complete_first_run_changepassword_failure_is_non_fatal()
    test_delete_cluster_direct_ignores_404()
    test_not_configured_raises()
    print("ok")
