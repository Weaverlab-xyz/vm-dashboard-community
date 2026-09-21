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

# What k8s_runner_service._resolve_aci returns: the DASHBOARD's primary placement,
# read from flat keys. The node may be somewhere else entirely.
_RESOLVED_ACI = {"rg": "rg-primary", "location": "centralus",
                 "subnet_id": "/subscriptions/s/rg-primary/subnets/aci-centralus",
                 "image": "dtzar/helm-kubectl:latest", "acr_server": "",
                 "acr_username": "", "acr_password": ""}


def _install_stubs():
    gcp = types.ModuleType("web_dashboard.services.gcp_service")
    gcp.run_cloud_run_k8s_task = _fake_run_cloud_run_k8s_task
    sys.modules["web_dashboard.services.gcp_service"] = gcp

    krs = types.ModuleType("web_dashboard.services.k8s_runner_service")
    krs._resolve_gcp = lambda: dict(_RESOLVED)
    krs._resolve_aci = lambda: dict(_RESOLVED_ACI)
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


def _stub_region_config(by_region):
    """Install a region_config stub resolving from ``by_region``; return a restore fn.

    Sets the PACKAGE ATTRIBUTE as well as sys.modules: ``_resolve_aci`` reaches it via
    ``from . import region_config``, which resolves the attribute on the already-imported
    ``web_dashboard.services`` package first — so a sys.modules-only stub is silently
    ignored (the same trap the config_service note above describes)."""
    import web_dashboard.services as pkg
    prev_mod = sys.modules.get("web_dashboard.services.region_config")
    prev_attr = getattr(pkg, "region_config", None)
    mod = types.ModuleType("web_dashboard.services.region_config")
    mod.resolve_region = lambda cloud, region: dict(by_region.get(region, {}))
    sys.modules["web_dashboard.services.region_config"] = mod
    pkg.region_config = mod

    def _restore():
        if prev_mod is not None:
            sys.modules["web_dashboard.services.region_config"] = prev_mod
        else:
            sys.modules.pop("web_dashboard.services.region_config", None)
        if prev_attr is not None:
            pkg.region_config = prev_attr
        else:
            try:
                delattr(pkg, "region_config")
            except AttributeError:
                pass
    return _restore


def test_resolve_aci_pins_container_group_to_node_region():
    """An ACI container group attaches to a VNet-DELEGATED subnet, which is regional.
    The k8s runner resolves the DASHBOARD's primary placement from flat keys, so a node
    outside the default location would put the group in the wrong VNet — where
    AllowVnetInBound does not apply and nothing is peered, so the SYN is dropped and the
    probe burns the whole readiness budget (live 2026-09-17: a westus2 node, a
    centralus runner). Location, RG and subnet must all come from the node's region."""
    restore_cfg = _stub_config({"azure_rancher_zone": "westus2"})
    restore_rc = _stub_region_config({"westus2": {
        "resource_group": "sandbox-westus2-rg",
        "aci_subnet_id": "/subscriptions/s/sandbox-westus2-rg/subnets/aci-westus2"}})
    try:
        cfg = rar._resolve_aci()
        assert cfg["location"] == "westus2", cfg["location"]
        assert cfg["rg"] == "sandbox-westus2-rg", cfg["rg"]
        assert cfg["subnet_id"].endswith("aci-westus2"), cfg["subnet_id"]
    finally:
        restore_rc(); restore_cfg()


def test_resolve_aci_without_a_region_subnet_raises():
    """Fail fast and name the key. Launching anyway puts the group in the default
    location's VNet, which cannot route — the silent-drop failure this replaces."""
    restore_cfg = _stub_config({"azure_rancher_zone": "westus2"})
    restore_rc = _stub_region_config({"westus2": {"resource_group": "sandbox-westus2-rg"}})
    try:
        rar._resolve_aci()
        raised = None
    except rar.RancherRunnerError as exc:
        raised = str(exc)
    finally:
        restore_rc(); restore_cfg()
    assert raised and "aci_subnet_id" in raised and "westus2" in raised, raised


def test_resolve_aci_in_the_default_location_is_untouched():
    """A single-region install must resolve to EXACTLY the flat keys it always did."""
    restore_cfg = _stub_config({"azure_rancher_zone": "centralus"})
    restore_rc = _stub_region_config({})
    try:
        cfg = rar._resolve_aci()
        assert cfg["location"] == "centralus", cfg["location"]
        assert cfg["rg"] == "rg-primary", cfg["rg"]
        assert cfg["subnet_id"].endswith("aci-centralus"), cfg["subnet_id"]
    finally:
        restore_rc(); restore_cfg()


def test_node_region_reads_azure_location_verbatim():
    """Azure models no zone for these nodes — resolve_placement persists the LOCATION
    in the zone key — so any split (the GCP/AWS ones) would corrupt it."""
    restore = _stub_config({"azure_rancher_zone": "westus2"})
    try:
        assert rar._node_region("azure") == "westus2"
    finally:
        restore()


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


def _swap_launcher(fn):
    """Point the stubbed Cloud Run launcher at ``fn``; return a restore callable."""
    gcp = sys.modules["web_dashboard.services.gcp_service"]
    prev = gcp.run_cloud_run_k8s_task
    gcp.run_cloud_run_k8s_task = fn
    return lambda: setattr(gcp, "run_cloud_run_k8s_task", prev)


def test_two_runner_jobs_do_not_overlap():
    """One Rancher node, so two runner jobs at once are two overlapping sequences
    (a deploy's first-run alongside a cluster import), never parallel work — each
    paying a container cold start and launching its own cloud resource. _run holds
    a lock for the whole job, so the second one starts only after the first ends."""
    events = []

    async def _slow(**kw):
        events.append("enter")
        await asyncio.sleep(0.05)
        events.append("exit")
        return 0, _b64_line("ok")

    _reset()
    restore = _swap_launcher(_slow)
    try:
        async def _both():
            await asyncio.gather(rar.request("GET", "https://10.1.2.3/ping"),
                                 rar.request("GET", "https://10.1.2.3/ping"))
        asyncio.run(_both())
    finally:
        restore()
    # Interleaved would be enter, enter, exit, exit.
    assert events == ["enter", "exit", "enter", "exit"], events


def test_the_lock_is_per_event_loop():
    """Regression pin: a module-level asyncio.Lock binds to the loop that first
    acquires it and raises 'bound to a different event loop' on any other. The
    worker has one long-lived loop, but every test here gets a fresh asyncio.run —
    so a single shared Lock would pass once and then break the whole file."""
    _reset(output=_b64_line("ok"))
    asyncio.run(rar.request("GET", "https://10.1.2.3/ping"))
    asyncio.run(rar.request("GET", "https://10.1.2.3/ping"))  # a brand-new loop
    assert len(_CALLS) == 2, len(_CALLS)


def test_a_long_runner_job_does_not_wedge_the_next_one():
    """The wait is bounded and fails OPEN. A first-run sequence can hold the lock
    for the better part of an hour; blocking a second job behind it forever would
    turn a throughput problem into a wedged job. Past the deadline the caller
    launches anyway — safe, because the cloud resources are named per invocation."""
    events = []

    async def _slow(**kw):
        events.append("enter")
        await asyncio.sleep(0.2)
        return 0, _b64_line("ok")

    _reset()
    restore = _swap_launcher(_slow)
    saved = rar._LOCK_WAIT_S
    rar._LOCK_WAIT_S = 0.01
    try:
        async def _both():
            await asyncio.gather(rar.request("GET", "https://10.1.2.3/ping"),
                                 rar.request("GET", "https://10.1.2.3/ping"))
        asyncio.run(_both())
    finally:
        rar._LOCK_WAIT_S = saved
        restore()
    # Both ran: the second gave up waiting rather than queueing behind the first.
    assert events == ["enter", "enter"], events


def test_the_lock_is_released_when_a_runner_job_raises():
    """A launcher that blows up must not leave the lock held — the next Rancher
    call would then block for the full deadline before failing open, turning one
    cloud error into a stalled queue."""
    calls = []

    async def _boom_then_ok(**kw):
        calls.append(kw)
        if len(calls) == 1:
            raise RuntimeError("the cloud said no")
        return 0, _b64_line("ok")

    _reset()
    restore = _swap_launcher(_boom_then_ok)
    try:
        async def _sequence():
            try:
                await rar.request("GET", "https://10.1.2.3/ping")
            except RuntimeError:
                pass
            # Checked in-loop, on the same lock object the next call will take:
            # a leak shows up here instead of as a 30-minute hang below.
            assert not rar._run_lock().locked(),                 "the lock survived a launcher exception — the next call would block"
            return await rar.request("GET", "https://10.1.2.3/ping")
        status, _ = asyncio.run(_sequence())
    finally:
        restore()
    assert status == 201, status


def test_rancher_service_threads_job_id_to_the_runner():
    """Every launcher names its container group / Cloud Run job / log stream after
    ``job_id``. Unthreaded it arrived empty, so two Rancher API calls in flight at
    once (two jobs, or a job plus an interactive call) targeted ONE fixed resource:
    the second create landed on the first's live container and whichever finished
    first deleted it in its finally. Nothing serialises Rancher API calls."""
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
    asyncio.run(rs._call("POST", "/v3/cluster", token="t", job_id="job-1234abcd"))
    assert _CALLS[0]["job_id"] == "job-1234abcd", _CALLS[0].get("job_id")

    # ...and through the public entry points a job drives, not just _call.
    _reset(output=_b64_line("{}", "200"))
    asyncio.run(rs.set_server_url_direct(server_url="https://34.1.2.3",
                                         api_token="t", job_id="job-1234abcd"))
    assert _CALLS[0]["job_id"] == "job-1234abcd", _CALLS[0].get("job_id")

    # All FOUR first-run calls carry it — they are the sequence that stalls.
    _reset(output=_b64_line("{}", "200"))
    asyncio.run(rs.complete_first_run_direct(
        api_token="t", server_url="https://34.1.2.3",
        current_password="bootpw-123456", new_password="adminpw-123456",
        job_id="job-1234abcd"))
    assert len(_CALLS) == 4, len(_CALLS)
    assert all(c["job_id"] == "job-1234abcd" for c in _CALLS), [c.get("job_id") for c in _CALLS]


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
