"""Unit tests for rancher_api_runner — the Rancher-API-over-Cloud-Run transport
(the corp-TLS-inspection escape hatch) — plus rancher_service routing through it.

Covers the request marshalling (curl config: method/url/token/body quoting, all
delivered via stdin so secrets stay out of argv), the sentinel-based response
parse out of the job's combined log output, and the wait_ready marker handling.
``gcp_service.run_cloud_run_k8s_task`` and ``k8s_runner_service._resolve_gcp``
are stubbed in sys.modules so no GCP account is needed. Runs under pytest, or
standalone: python tests/test_rancher_api_runner.py
"""
import asyncio
import base64
import os
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# ── stubs ─────────────────────────────────────────────────────────────────────
_CALLS = []          # captured run_cloud_run_k8s_task invocations
_JOB_RESULT = {}     # (exit_code, output) the fake job returns


async def _fake_run_cloud_run_k8s_task(**kw):
    _CALLS.append(kw)
    return _JOB_RESULT.get("exit_code", 0), _JOB_RESULT.get("output", "")


_RESOLVED = {"project_id": "proj-test", "region": "us-central1",
             "image": "dtzar/helm-kubectl:latest",
             "vpc_connector": "runner-conn",
             "vpc_network": "sandbox-vpc", "vpc_subnetwork": "sandbox-subnet"}


def _install_stubs():
    gcp = types.ModuleType("web_dashboard.services.gcp_service")
    gcp.run_cloud_run_k8s_task = _fake_run_cloud_run_k8s_task
    sys.modules["web_dashboard.services.gcp_service"] = gcp

    krs = types.ModuleType("web_dashboard.services.k8s_runner_service")
    krs._resolve_gcp = lambda: dict(_RESOLVED)
    sys.modules["web_dashboard.services.k8s_runner_service"] = krs


_install_stubs()
try:
    from web_dashboard.services import rancher_api_runner as rar
except Exception as exc:  # pragma: no cover — skip if deps missing
    try:
        import pytest
        pytest.skip(f"rancher_api_runner import unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)


def _reset(exit_code=0, output=""):
    _CALLS.clear()
    _JOB_RESULT.clear()
    _JOB_RESULT["exit_code"] = exit_code
    _JOB_RESULT["output"] = output


def test_curl_config_marshalling():
    cfg = rar._curl_config("put", "https://10.1.2.3/v3/settings/server-url",
                           token="tok-a:b", json_body={"name": "server-url", "value": 'x"y'},
                           timeout_s=25)
    assert 'url = "https://10.1.2.3/v3/settings/server-url"' in cfg
    assert 'request = "PUT"' in cfg
    assert "insecure" in cfg
    assert "max-time = 25" in cfg
    assert 'header = "Authorization: Bearer tok-a:b"' in cfg
    # JSON body double quotes are backslash-escaped for the curl config format.
    assert 'data = "{\\"name\\": \\"server-url\\", \\"value\\": \\"x\\\\\\"y\\"}"' in cfg
    # No token/body → no auth or data lines.
    bare = rar._curl_config("get", "https://10.1.2.3/ping")
    assert "Authorization" not in bare and "data =" not in bare


def test_request_parses_status_and_body():
    _reset(output=(
        "some cloud logging preamble\n"
        "RANCHER_RESP_BEGIN\n"
        '{"token": "token-xyz:secret"}\n'
        "RANCHER_STATUS:201\n"
    ))
    status, body = asyncio.run(rar.request(
        "POST", "https://10.1.2.3/v3/token", token="t", json_body={"ttl": 0}))
    assert status == 201
    assert '"token-xyz:secret"' in body
    # The call rode the stubbed Cloud Run task with the config on stdin (not argv).
    kw = _CALLS[0]
    assert kw["vpc_connector"] == "runner-conn"
    # Direct-VPC-egress fields pass through so the job NIC lands in the subnet.
    assert kw["vpc_network"] == "sandbox-vpc"
    assert kw["vpc_subnetwork"] == "sandbox-subnet"
    stdin = base64.b64decode(kw["stdin_b64"]).decode()
    assert "Authorization: Bearer t" in stdin
    assert "Authorization" not in kw["command"]  # secrets not in argv


def test_resolve_requires_vpc_reach():
    """No connector AND no direct-egress subnet → fail fast with the exact keys
    (a VPC-less job launches fine but can't route to the internal IP, silently
    burning the readiness budget — the failure mode this guard prevents)."""
    global _RESOLVED
    saved = dict(_RESOLVED)
    _RESOLVED.update(vpc_connector="", vpc_network="", vpc_subnetwork="")
    try:
        try:
            asyncio.run(rar.request("GET", "https://10.1.2.3/ping"))
            raised = False
        except rar.RancherRunnerError as exc:
            raised = True
            assert "gcp_run_network" in str(exc)
        assert raised
    finally:
        _RESOLVED.clear()
        _RESOLVED.update(saved)


def test_request_no_marker_raises():
    _reset(output="job launched but curl never ran\n")
    try:
        asyncio.run(rar.request("GET", "https://10.1.2.3/ping"))
        raised = False
    except rar.RancherRunnerError:
        raised = True
    assert raised


def test_request_no_status_raises():
    _reset(output="RANCHER_RESP_BEGIN\ncurl: (7) connection refused\n")
    try:
        asyncio.run(rar.request("GET", "https://10.1.2.3/ping"))
        raised = False
    except rar.RancherRunnerError:
        raised = True
    assert raised


def test_wait_ready_markers():
    _reset(output="RANCHER_READY\n")
    assert asyncio.run(rar.wait_ready("https://10.1.2.3", 120)) == "ready"
    # The in-container loop is sized from the timeout (120s / 10s poll = 12 tries).
    assert "seq 1 12" in _CALLS[0]["command"]
    _reset(output="RANCHER_NOT_READY\n")
    assert asyncio.run(rar.wait_ready("https://10.1.2.3", 120)) == "timeout"


def test_rancher_service_runner_routing():
    """rancher_service._call must route through the runner transport, hit the
    INTERNAL url, and parse the runner's (status, text) into a dict body."""
    cfgmod = types.ModuleType("web_dashboard.services.config_service")
    store = {"rancher_api_transport": "runner",
             "rancher_internal_url": "https://10.9.8.7",
             "rancher_server_url": "https://34.1.2.3",
             "rancher_api_token": "token-cfg:secret"}
    cfgmod.get = lambda key, default="", workgroup=None: store.get(key, default)
    cfgmod.get_bool = lambda key, default=False: bool(store.get(key, default))
    sys.modules["web_dashboard.services.config_service"] = cfgmod
    from web_dashboard.services import rancher_service as rs

    _reset(output="RANCHER_RESP_BEGIN\n"
                  '{"id": "c-m-abc"}\n'
                  "RANCHER_STATUS:201\n")
    status, body = asyncio.run(rs._call("POST", "/v3/cluster",
                                        token="token-cfg:secret",
                                        json={"type": "cluster", "name": "demo"}))
    assert status == 201 and body == {"id": "c-m-abc"}
    stdin = base64.b64decode(_CALLS[0]["stdin_b64"]).decode()
    # Addressed at the INTERNAL url (the connector can't route the public IP).
    assert 'url = "https://10.9.8.7/v3/cluster"' in stdin


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
