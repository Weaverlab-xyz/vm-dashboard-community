"""Unit tests for rancher_service — the runner-executed Rancher API client.

Every Rancher call runs as a `kubectl run … curl` pod via an injected `run`
coroutine, so we exercise the orchestration + JSON parsing with a fake `run`
that returns canned responses — no live Rancher, no cluster. config_service is
stubbed so `_cfg` never falls through to pydantic settings.

Runs under pytest, or standalone: python tests/test_rancher_service.py
"""
import asyncio
import os
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _install_stubs():
    # Stub only config_service (leave the real web_dashboard.services package so
    # the real rancher_service submodule resolves) — mirrors test_k8s_tf_vars.py.
    cfg = types.ModuleType("web_dashboard.services.config_service")
    store = {"rancher_namespace": "cattle-system"}
    cfg.get = lambda key, default="", workgroup=None: store.get(key, default)
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


def test_extract_json_from_noisy_output():
    assert rs._extract_json('pod noise\n{"token":"t-1:secret"}\ndeleted') == {"token": "t-1:secret"}
    try:
        rs._extract_json("no json here")
    except rs.RancherError:
        pass
    else:
        raise AssertionError("expected RancherError on non-JSON output")


def test_curl_pod_targets_incluster_service():
    cmd = rs._curl_pod("-X GET https://rancher.cattle-system/v3/x")
    assert "kubectl run rancher-api-" in cmd
    assert "curlimages/curl" in cmd
    assert "curl -sk" in cmd
    assert "-n cattle-system" in cmd


def test_bootstrap_orchestration():
    calls = []
    responses = [
        '{"token":"login-abc"}',              # login
        '{"name":"server-url","value":"x"}',  # PUT server-url (not parsed)
        '{"token":"token-xyz:secret"}',       # mint api token
    ]

    async def fake_run(command):
        calls.append(command)
        return responses[len(calls) - 1]

    tok = asyncio.run(rs.bootstrap(fake_run, bootstrap_password="bootpw",
                                   server_url="https://rancher.lab.internal"))
    assert tok == "token-xyz:secret"
    assert "/v3-public/localProviders/local?action=login" in calls[0]
    assert "bootpw" in calls[0]
    assert "/v3/settings/server-url" in calls[1]
    assert "https://rancher.lab.internal" in calls[1]
    assert "Authorization: Bearer login-abc" in calls[1]
    assert "/v3/token" in calls[2]
    assert "Authorization: Bearer login-abc" in calls[2]


def test_create_import_cluster_orchestration():
    calls = []
    responses = ['{"id":"c-m-abc","type":"cluster"}', '{"manifestUrl":"https://rancher/reg/xyz.yaml"}']

    async def fake_run(command):
        calls.append(command)
        return responses[len(calls) - 1]

    cid, url = asyncio.run(rs.create_import_cluster(fake_run, api_token="token-xyz:secret", name="demo"))
    assert cid == "c-m-abc"
    assert url == "https://rancher/reg/xyz.yaml"
    assert "/v3/cluster " in calls[0] and '"name": "demo"' in calls[0]
    assert "Authorization: Bearer token-xyz:secret" in calls[0]
    assert "/v3/clusterregistrationtoken" in calls[1] and "c-m-abc" in calls[1]


if __name__ == "__main__":
    test_extract_json_from_noisy_output()
    test_curl_pod_targets_incluster_service()
    test_bootstrap_orchestration()
    test_create_import_cluster_orchestration()
    print("ok")
