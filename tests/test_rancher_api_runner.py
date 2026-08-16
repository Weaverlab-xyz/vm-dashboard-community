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
_CONFIG_STORE = {}   # backs the default config_service stub


def _install_stubs():
    gcp = types.ModuleType("web_dashboard.services.gcp_service")
    gcp.run_cloud_run_k8s_task = _fake_run_cloud_run_k8s_task
    sys.modules["web_dashboard.services.gcp_service"] = gcp

    krs = types.ModuleType("web_dashboard.services.k8s_runner_service")
    krs._resolve_gcp = lambda: dict(_RESOLVED)
    sys.modules["web_dashboard.services.k8s_runner_service"] = krs

    # Stub config_service at load so the REAL one is never imported: _resolve now
    # reads gcp_rancher_zone (to pin the runner to the node's region), and if the
    # real module gets imported here it sets the package attribute
    # web_dashboard.services.config_service, which then defeats a later
    # sys.modules-only stub (from . import config_service resolves the attribute
    # first) — a hermetic default stub keeps the suite DB-free.
    cs = types.ModuleType("web_dashboard.services.config_service")
    cs.get = lambda key, default="", workgroup=None: _CONFIG_STORE.get(key, default)
    cs.get_bool = lambda key, default=False: bool(_CONFIG_STORE.get(key, default))
    sys.modules["web_dashboard.services.config_service"] = cs


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


def _b64_line(body: str, code: str = "201") -> str:
    return f"RANCHER_B64:{base64.b64encode(body.encode()).decode()}:RC:{code}\n"


def test_request_parses_status_and_body():
    # The response travels as ONE atomic RANCHER_B64 line: Cloud Logging ingests a
    # raw-JSON stdout line as jsonPayload (it VANISHES from textPayload assembly)
    # and can reorder same-instant lines — both bit live 2026-07-21.
    _reset(output=("some cloud logging preamble\n"
                   + _b64_line('{"token": "token-xyz:secret"}', "201")))
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


def test_command_pipes_stdin_into_curl():
    """Regression + protocol round-trip: the runner shell prepends
    ``printf %s "$STDIN_B64" | base64 -d |`` to the command, and a pipe binds to
    the FIRST simple command only — the command must be ONE brace group with curl
    first so `curl -K -` receives the decoded config (the ungrouped version fed
    it to echo → "no URL specified", caught live). Executes the real composition
    under sh with a curl shim that honours ``-o`` and emits a 201, then feeds the
    stdout through the real ``_parse_response`` — proving stdin reaches curl AND
    the single-line RANCHER_B64 protocol survives the shell round trip."""
    import os
    import shutil
    import subprocess
    _reset(output=_b64_line("ok"))
    asyncio.run(rar.request("GET", "https://10.1.2.3/ping", token="tok-x"))
    kw = _CALLS[0]
    assert kw["command"].lstrip().startswith("{ curl"), "curl must lead the brace group"
    sh = shutil.which("sh") or shutil.which("bash")
    if not sh:  # pragma: no cover — no POSIX shell on this host; shape assert above still ran
        return
    # curl shim: write stdin (the config) to the -o file, print the http code to
    # stdout like `-w %{http_code}` would.
    shim = (
        'curl() { out=""; prev=""; '
        'for a in "$@"; do if [ "$prev" = "-o" ]; then out="$a"; fi; prev="$a"; done; '
        'cat > "$out"; printf 201; }; '
    )
    full = shim + 'printf %s "$STDIN_B64" | base64 -d | ' + kw["command"]
    r = subprocess.run([sh, "-c", full], capture_output=True, text=True,
                       env={**os.environ, "STDIN_B64": kw["stdin_b64"]})
    assert r.returncode == 0, r.stderr
    status, body = rar._parse_response(r.stdout)
    assert status == 201
    assert 'url = "https://10.1.2.3/ping"' in body, body
    assert "Authorization: Bearer tok-x" in body


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


def _stub_config(store):
    """Install a config_service stub returning from ``store``; return a restore fn."""
    prev = sys.modules.get("web_dashboard.services.config_service")
    cfgmod = types.ModuleType("web_dashboard.services.config_service")
    cfgmod.get = lambda key, default="", workgroup=None: store.get(key, default)
    cfgmod.get_bool = lambda key, default=False: bool(store.get(key, default))
    sys.modules["web_dashboard.services.config_service"] = cfgmod

    def _restore():
        if prev is not None:
            sys.modules["web_dashboard.services.config_service"] = prev
        else:
            sys.modules.pop("web_dashboard.services.config_service", None)
    return _restore


def test_resolve_pins_direct_runner_to_node_region():
    """Direct VPC egress reaches only SAME-region internal IPs, so the runner must
    run in the NODE's region (from gcp_rancher_zone) — else a cross-region node's
    internal IP is unreachable (SYN dropped → readiness timeout, diagnosed live
    2026-07-24: us-central1 runner vs us-east1 node). A bare subnet name is
    region-agnostic and stays as-is (Cloud Run resolves it in the job's region)."""
    global _RESOLVED
    saved = dict(_RESOLVED)
    _RESOLVED.update(region="us-central1", vpc_connector="",
                     vpc_network="sandbox-vpc", vpc_subnetwork="jump-subnet")
    restore = _stub_config({"gcp_rancher_zone": "us-east1-b"})
    try:
        cfg = rar._resolve()
        assert cfg["region"] == "us-east1", cfg["region"]
        assert cfg["vpc_subnetwork"] == "jump-subnet", cfg["vpc_subnetwork"]
    finally:
        restore()
        _RESOLVED.clear(); _RESOLVED.update(saved)


def test_resolve_retargets_subnet_selflink_region():
    """A region-qualified subnet self-link has its region segment rewritten to the
    node region so the job NIC lands in a subnet that exists there."""
    global _RESOLVED
    saved = dict(_RESOLVED)
    _RESOLVED.update(region="us-central1", vpc_connector="", vpc_network="sandbox-vpc",
                     vpc_subnetwork="projects/p/regions/us-central1/subnetworks/jump")
    restore = _stub_config({"gcp_rancher_zone": "us-east1-b"})
    try:
        cfg = rar._resolve()
        assert cfg["region"] == "us-east1"
        assert cfg["vpc_subnetwork"] == "projects/p/regions/us-east1/subnetworks/jump", \
            cfg["vpc_subnetwork"]
    finally:
        restore()
        _RESOLVED.clear(); _RESOLVED.update(saved)


def test_resolve_connector_only_keeps_gcp_region():
    """A VPC Access connector can reach any region in the VPC and must stay
    co-located with the Cloud Run region, so the node-region override is
    direct-egress-only — a connector-only config keeps gcp_region."""
    global _RESOLVED
    saved = dict(_RESOLVED)
    _RESOLVED.update(region="us-central1", vpc_connector="runner-conn",
                     vpc_network="", vpc_subnetwork="")
    restore = _stub_config({"gcp_rancher_zone": "us-east1-b"})
    try:
        cfg = rar._resolve()
        assert cfg["region"] == "us-central1", cfg["region"]
    finally:
        restore()
        _RESOLVED.clear(); _RESOLVED.update(saved)


def test_request_no_marker_raises():
    _reset(output="job launched but curl never ran\n")
    try:
        asyncio.run(rar.request("GET", "https://10.1.2.3/ping"))
        raised = False
    except rar.RancherRunnerError:
        raised = True
    assert raised


def test_request_no_status_raises():
    # Marker line present but the code slot is empty = curl died before an HTTP
    # status (network failure) — must raise, not return a bogus status.
    _reset(output="RANCHER_B64::RC:\ncurl: (7) connection refused\n")
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

    _reset(output=_b64_line('{"id": "c-m-abc"}', "201"))
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
