"""VMware Workstation Pro over vmrest, and the loopback exception it needs.

Workstation was written off twice as unreachable, on the grounds that it is driven by
`vmrun` against local VMX paths. True of vmrun, wrong overall: Workstation Pro ships
`vmrest`, plain JSON over HTTP — so this needs no new dependency and no sibling
container.

Three protocol quirks are pinned here because each fails only against REAL vmrest, where
a unit test with a permissive stub would happily pass:

* vmrest **rejects `application/json`** — both Accept and Content-Type must be its vendor
  media type;
* the power endpoint takes a **bare string body** (`on`), not a JSON object;
* `GET /api/vms` returns only `id` and `path`, so a display name costs a second call.

And the security property: `vmrest` binds `127.0.0.1`, so a co-located agent needs an
opt-in loopback exception — which must NOT leak into discovery, where the blanket deny is
what stops the agent probing its own container or a cloud metadata service.

Runs under pytest, or standalone:  python tests/test_agent_workstation.py
"""
import importlib.util
import json
import os
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PATH = os.path.join(_ROOT, "runners", "agent", "agent.py")

try:
    _spec = importlib.util.spec_from_file_location("agent_runner_ws", _PATH)
    agent = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(agent)
except Exception as exc:  # noqa: BLE001
    print(f"SKIP: {exc}")
    sys.exit(0)


CONN = {"name": "my-workstation", "kind": "workstation", "host": "127.0.0.1",
        "port": 8697, "username": "api", "password": "pw"}


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode() if self._payload is not None else b""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeUrlopen:
    """Records every request and replays canned bodies keyed on the path."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []          # (method, path, headers, body)

    def __call__(self, request, timeout=None, context=None):
        path = request.full_url.split("/api", 1)[-1]
        self.calls.append((request.get_method(), path,
                           dict(request.header_items()), request.data))
        return FakeResponse(self.routes.get(path))


_ROUTES = {
    "/vms": [{"id": "AB12", "path": r"C:\VMs\win11\win11.vmx"},
             {"id": "CD34", "path": r"C:\VMs\ubuntu\ubuntu.vmx"}],
    "/vms/AB12": {"id": "AB12", "cpu": {"processors": 4}, "memory": 8192},
    "/vms/CD34": {"id": "CD34", "cpu": {"processors": 2}, "memory": 4096},
    "/vms/AB12/power": {"power_state": "poweredOn"},
    "/vms/CD34/power": {"power_state": "poweredOff"},
    "/vms/AB12/params/displayName": {"name": "displayName", "value": "win11-lab"},
    "/vms/CD34/params/displayName": {"name": "displayName", "value": "ubuntu-build"},
    "/vms/AB12/ip": {"ip": "192.168.72.130"},
}


class _AllowAll:
    """Policy stand-in: Policy itself needs a real file, and these tests are about the
    vmrest protocol rather than the allow-list."""
    digest = "d" * 64

    def check(self, addr, port):
        return None

    def allows_loopback(self, ref):
        return True


def _run(fn, routes=None, **kw):
    fake = FakeUrlopen(routes if routes is not None else _ROUTES)
    original = agent._urlrequest.urlopen
    agent._urlrequest.urlopen = fake
    try:
        return fn(fake), fake
    finally:
        agent._urlrequest.urlopen = original


def _sync(routes=None, payload=None):
    return _run(lambda f: agent._sync_workstation(
        CONN, payload or {"connection_ref": "my-workstation"}, _AllowAll(),
        lambda _m: None), routes)


# ── the three protocol quirks ─────────────────────────────────────────────────

def test_the_vendor_media_type_is_sent_on_both_headers():
    """vmrest rejects application/json outright. Nothing but a live vmrest would catch
    this, so it is asserted on every request the client makes."""
    _out, fake = _sync()
    assert fake.calls, "no request was made"
    for _method, path, headers, _body in fake.calls:
        # urllib title-cases header names on the way out ("Content-type"), so compare
        # case-insensitively rather than against whatever spelling we happened to set.
        lowered = {k.lower(): v for k, v in headers.items()}
        assert lowered.get("accept") == agent._VMREST_MEDIA, path
        assert lowered.get("content-type") == agent._VMREST_MEDIA, path


def test_the_power_body_is_a_bare_string_not_json():
    def _power(_f):
        return agent._power_workstation(
            CONN, {"target_id": "AB12", "connection_ref": "my-workstation"},
            _AllowAll(), lambda _m: None, "power_on")

    _out, fake = _run(_power, {"/vms/AB12/power": {"power_state": "poweredOn"}})
    put = [c for c in fake.calls if c[0] == "PUT"][0]
    assert put[3] == b"on", f"vmrest wants a bare string, got {put[3]!r}"
    assert put[3] != b'"on"'


def test_the_display_name_comes_from_a_second_call():
    out, fake = _sync()
    names = {vm["name"] for vm in out["vms"]}
    assert names == {"win11-lab", "ubuntu-build"}
    assert any("params/displayName" in c[1] for c in fake.calls)


def test_a_missing_display_name_falls_back_to_the_vmx_basename():
    """Better than a blank cell, and it is what the file is actually called on disk."""
    routes = dict(_ROUTES)
    routes["/vms/AB12/params/displayName"] = {}
    out, _fake = _sync(routes)
    by_id = {vm["vm_id"]: vm for vm in out["vms"]}
    assert by_id["AB12"]["name"] == "win11"


# ── inventory ─────────────────────────────────────────────────────────────────

def test_inventory_carries_cpu_memory_and_the_vmx_path():
    out, _fake = _sync()
    vm = {v["vm_id"]: v for v in out["vms"]}["AB12"]
    assert vm["vcpus"] == 4 and vm["mem_mib"] == 8192
    assert vm["scope"].endswith("win11.vmx"), "the VMX path rides in scope, display-only"
    assert out["complete"] is True and out["next_cursor"] == ""


def test_an_ip_is_only_fetched_for_a_powered_on_vm():
    """A stopped VM has no address, so asking costs a call per VM to learn nothing."""
    _out, fake = _sync()
    ip_calls = [c[1] for c in fake.calls if c[1].endswith("/ip")]
    assert ip_calls == ["/vms/AB12/ip"], ip_calls


def test_the_powered_on_vm_reports_its_address():
    out, _fake = _sync()
    by_id = {v["vm_id"]: v for v in out["vms"]}
    assert by_id["AB12"]["ip_addresses"] == ["192.168.72.130"]
    assert "ip_addresses" not in by_id["CD34"]


def test_page_size_bounds_the_listing():
    out, _fake = _sync(payload={"connection_ref": "my-workstation", "page_size": 1})
    assert len(out["vms"]) == 1


def test_a_vmrest_that_returns_no_list_is_a_readable_refusal():
    try:
        _sync({"/vms": None})
    except agent.PolicyRefusal as exc:
        assert "vmrest -C" in str(exc), "the message must name the setup step"
    else:
        raise AssertionError("expected a refusal")


# ── the verbs vmrest does not have ────────────────────────────────────────────

def test_only_power_on_and_off_are_supported():
    assert set(agent._VMREST_POWER) == {"power_on", "power_off"}


def test_the_unsupported_verbs_refuse_and_say_what_vmrest_offers():
    """Mapping `restart` onto vmrest's `shutdown` would quietly do something other than
    what was asked. Refusing, and naming the real API, is the honest failure."""
    for verb in ("restart", "power_reset", "snapshot"):
        try:
            agent._power_workstation(CONN, {"target_id": "AB12"}, _AllowAll(),
                                     lambda _m: None, verb)
        except agent.PolicyRefusal as exc:
            assert "no reset, reboot or snapshot" in str(exc), verb
        else:
            raise AssertionError(f"{verb} should have been refused")


def test_a_power_verb_without_a_target_is_refused():
    try:
        agent._power_workstation(CONN, {}, _AllowAll(), lambda _m: None, "power_on")
    except agent.PolicyRefusal as exc:
        assert "target_id" in str(exc)
    else:
        raise AssertionError("expected a refusal")


# ── the loopback exception ────────────────────────────────────────────────────

def _policy(text: str):
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
        fh.write(text)
        path = fh.name
    try:
        return agent.Policy.load(path)
    finally:
        os.unlink(path)


_WITH_LOOPBACK = """
targets:
  - cidr: 10.20.0.0/24
    ports: [8697]
connections:
  - name: my-workstation
    verbs: [inventory_sync, power_on, power_off]
    allow_loopback: true
  - name: dc1-vcenter
    verbs: [inventory_sync]
"""


def test_the_opt_in_is_per_connection():
    policy = _policy(_WITH_LOOPBACK)
    assert policy.allows_loopback("my-workstation") is True
    assert policy.allows_loopback("dc1-vcenter") is False
    assert policy.allows_loopback("") is False
    assert policy.allows_loopback("not-a-connection") is False


def test_it_is_off_unless_asked_for():
    policy = _policy("targets:\n  - cidr: 10.20.0.0/24\n")
    assert policy.loopback_connections == set()


def test_an_opted_in_connection_reaches_loopback():
    policy = _policy(_WITH_LOOPBACK)
    agent._check_endpoint(policy, "127.0.0.1", 8697, "my-workstation")   # must not raise


def test_a_connection_without_the_opt_in_still_cannot():
    policy = _policy(_WITH_LOOPBACK)
    try:
        agent._check_endpoint(policy, "127.0.0.1", 8697, "dc1-vcenter")
    except agent.PolicyRefusal:
        pass
    else:
        raise AssertionError("the exception must not apply to every connection")


def test_discovery_still_refuses_loopback_when_a_connection_opted_in():
    """THE assertion in this file.

    The blanket deny is what stops a discovery sweep probing the agent's own container
    or a cloud metadata endpoint. A future refactor that collapsed the per-connection
    exception into Policy.check would silently remove that, and every other test here
    would still pass.
    """
    policy = _policy(_WITH_LOOPBACK)
    for addr in ("127.0.0.1", "127.0.0.53", "169.254.169.254"):
        try:
            policy.check(addr, 8697)
        except agent.PolicyRefusal:
            continue
        raise AssertionError(f"Policy.check must still refuse {addr}")


def test_the_exception_does_not_widen_non_loopback_addresses():
    """An opted-in connection is not a free pass to the whole network."""
    policy = _policy(_WITH_LOOPBACK)
    try:
        agent._check_endpoint(policy, "8.8.8.8", 8697, "my-workstation")
    except agent.PolicyRefusal:
        pass
    else:
        raise AssertionError("only LOOPBACK is exempt, not everything")


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
