"""The agent's one-shot sibling runner: the Engine client and its refusals.

This is the capability with a real cost — launching a container needs the Docker
socket, and the socket is root on the host. The tests that matter are therefore not
"does it work" but "what can a job ask for", and the answer has to stay: a verb and a
connection name, and nothing else.

The strongest assertion here is
:func:`test_no_hostconfig_field_comes_from_the_job`. A fully hostile dashboard cannot
get `--privileged`, a bind mount or host networking out of this agent, because the
create spec is built from constants and policy — there is no field through which to ask.

No Docker: the Engine API is driven through a stubbed transport.

Runs under pytest, or standalone:  python tests/test_agent_sibling_runner.py
"""
import ast
import importlib.util
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PATH = os.path.join(_ROOT, "runners", "agent", "agent.py")

try:
    _spec = importlib.util.spec_from_file_location("agent_runner_sib", _PATH)
    agent = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(agent)
except Exception as exc:  # noqa: BLE001
    print(f"SKIP: {exc}")
    sys.exit(0)


class FakePolicy:
    """Just the sibling knobs — Policy itself needs a real file."""

    def __init__(self, enabled=True, image="chrweav/hypervisor-runner:latest",
                 network="bridge"):
        self.sibling_enabled = enabled
        self.sibling_image = image
        self.sibling_network = network
        self.digest = "d" * 64


def _framed(text: str) -> bytes:
    """Docker's non-TTY log framing: 8-byte header then the payload."""
    body = text.encode()
    return b"\x01\x00\x00\x00" + len(body).to_bytes(4, "big") + body


class FakeEngine:
    """Records every Engine call and replays canned responses."""

    def __init__(self, *, create_status=201, logs="", start_status=204):
        self.calls = []
        self.created = None
        self._create_status = create_status
        self._logs = logs
        self._start_status = start_status
        self.removed = []

    def __call__(self, method, path, body=None, timeout=60.0):
        self.calls.append((method, path))
        if path == "/containers/create":
            self.created = body
            return self._create_status, {"Id": "c0ffee"}
        if path.endswith("/start"):
            return self._start_status, {}
        if path.endswith("/wait"):
            return 200, {"StatusCode": 0}
        if "/logs" in path:
            return 200, _framed(self._logs)
        if method == "DELETE":
            self.removed.append(path)
            return 204, {}
        if path.startswith("/containers/json"):
            return 200, [{"Id": "orphan1"}, {"Id": "orphan2"}]
        return 200, {}


def _run(engine, policy=None, env=None, cancelled=lambda: False):
    original = agent._engine
    agent._engine = engine
    try:
        return agent._run_sibling(policy or FakePolicy(), env or {"HV_KIND": "hyperv"},
                                  lambda _m: None, cancelled)
    finally:
        agent._engine = original


_OK = json.dumps({"ok": True, "vms": [{"vm_id": "1", "name": "web01"}],
                  "next_cursor": "", "complete": True})


# ── the create spec is the security boundary ──────────────────────────────────

def test_no_hostconfig_field_comes_from_the_job():
    """The assertion this whole file exists for.

    Every HostConfig value is a constant or comes from policy. If any of them were ever
    derived from the payload, a compromised dashboard could ask for a privileged
    container with / bind-mounted, and the socket would give it that.
    """
    engine = FakeEngine(logs=_OK)
    _run(engine)
    host = engine.created["HostConfig"]
    assert host["Privileged"] is False
    assert host["Binds"] == []
    assert host["CapDrop"] == ["ALL"]
    assert host["ReadonlyRootfs"] is True
    assert "no-new-privileges:true" in host["SecurityOpt"]
    assert host["NetworkMode"] != "host"
    assert host["PidsLimit"] and host["Memory"]


def test_the_image_comes_from_policy_not_the_job():
    engine = FakeEngine(logs=_OK)
    _run(engine, FakePolicy(image="my-registry/hv:1.2"))
    assert engine.created["Image"] == "my-registry/hv:1.2"


def test_the_network_is_policy_controlled():
    engine = FakeEngine(logs=_OK)
    _run(engine, FakePolicy(network="agent-net"))
    assert engine.created["HostConfig"]["NetworkMode"] == "agent-net"


def test_the_credential_rides_in_env_not_argv():
    # argv is visible in `ps` on the host; Env in the create body is not.
    engine = FakeEngine(logs=_OK)
    _run(engine, env={"HV_KIND": "hyperv", "HV_PASSWORD": "s3cret"})
    assert "HV_PASSWORD=s3cret" in engine.created["Env"]
    assert "Cmd" not in engine.created and "Entrypoint" not in engine.created


def test_the_container_is_labelled_so_orphans_can_be_swept():
    engine = FakeEngine(logs=_OK)
    _run(engine)
    assert engine.created["Labels"] == {agent.SIBLING_LABEL: "1"}


def test_autoremove_is_off_so_the_log_read_is_not_raced():
    engine = FakeEngine(logs=_OK)
    _run(engine)
    assert engine.created["HostConfig"]["AutoRemove"] is False


# ── lifecycle ─────────────────────────────────────────────────────────────────

def test_a_successful_run_returns_the_parsed_result_and_removes_the_container():
    engine = FakeEngine(logs=_OK)
    out = _run(engine)
    assert out["vms"][0]["name"] == "web01"
    assert "ok" not in out, "the transport flag must not leak into the result"
    assert engine.removed, "the container was not removed"


def test_the_container_is_removed_even_when_the_run_fails():
    engine = FakeEngine(logs=json.dumps({"ok": False, "error": "WinRM refused"}))
    try:
        _run(engine)
    except agent.PolicyRefusal as exc:
        assert "WinRM refused" in str(exc)
    else:
        raise AssertionError("expected a refusal")
    assert engine.removed, "a failed run must still clean up"


def test_log_framing_is_stripped():
    engine = FakeEngine(logs=_OK)
    assert _run(engine)["complete"] is True


def test_output_that_is_not_json_is_a_refusal_not_a_crash():
    engine = FakeEngine(logs="Traceback (most recent call last):\n  boom")
    try:
        _run(engine)
    except agent.PolicyRefusal:
        pass
    else:
        raise AssertionError("expected a refusal")


def test_empty_output_says_so():
    engine = FakeEngine(logs="")
    try:
        _run(engine)
    except agent.PolicyRefusal as exc:
        assert "no result" in str(exc)
    else:
        raise AssertionError("expected a refusal")


def test_a_missing_image_says_how_to_get_it():
    """The agent never pulls: a pull is a network fetch of executable content, which is
    the operator's decision rather than a job's."""
    engine = FakeEngine(create_status=404)
    try:
        _run(engine)
    except agent.PolicyRefusal as exc:
        assert "docker pull" in str(exc)
    else:
        raise AssertionError("expected a refusal")


# ── the three grants ──────────────────────────────────────────────────────────

def test_a_policy_that_does_not_enable_the_runner_refuses():
    engine = FakeEngine(logs=_OK)
    for policy in (FakePolicy(enabled=False), FakePolicy(image="")):
        try:
            _run(engine, policy)
        except agent.PolicyRefusal as exc:
            assert "policy.yaml" in str(exc)
        else:
            raise AssertionError("expected a refusal")
    assert engine.created is None, "nothing may be created before the policy check"


def test_the_sibling_is_off_by_default_in_the_policy_parser():
    """A policy.yaml with no `sibling:` block must not enable it."""
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
        fh.write("targets:\n  - cidr: 10.0.0.0/24\n")
        path = fh.name
    try:
        policy = agent.Policy.load(path)
        assert policy.sibling_enabled is False
        assert policy.sibling_image == ""
    finally:
        os.unlink(path)


def test_an_unmounted_socket_says_which_overlay_to_apply():
    def _boom(*a, **kw):
        raise OSError("No such file or directory")

    original = agent._UnixHTTP.connect
    agent._UnixHTTP.connect = _boom
    try:
        agent._engine("GET", "/_ping")
    except agent.PolicyRefusal as exc:
        assert "docker-compose.sibling.yml" in str(exc)
    else:
        raise AssertionError("expected a refusal")
    finally:
        agent._UnixHTTP.connect = original


# ── orphan sweep ──────────────────────────────────────────────────────────────

def test_the_orphan_sweep_is_scoped_by_label():
    engine = FakeEngine()
    original = agent._engine
    agent._engine = engine
    try:
        assert agent.sweep_orphans() == 2
    finally:
        agent._engine = original
    listing = [p for m, p in engine.calls if p.startswith("/containers/json")][0]
    assert agent.SIBLING_LABEL in listing, (
        "the sweep must filter by label, or it could remove a container this agent "
        "did not create")


def test_the_orphan_sweep_is_quiet_when_there_is_no_socket():
    def _boom(*a, **kw):
        raise agent.PolicyRefusal("no socket")

    original = agent._engine
    agent._engine = _boom
    try:
        assert agent.sweep_orphans() == 0
    finally:
        agent._engine = original


# ── routing ───────────────────────────────────────────────────────────────────

def test_only_hyperv_and_esxi_go_through_the_sibling():
    """Everything else has an in-agent transport, and a second implementation of the
    same thing is a second thing to keep correct."""
    assert set(agent._SIBLING_KINDS) == {"hyperv", "esxi"}
    for kind in ("vsphere", "proxmox", "nutanix", "xcpng"):
        assert kind not in agent._SIBLING_KINDS


def test_esxi_satisfies_a_vsphere_job_but_not_the_reverse():
    """The dashboard has no `esxi` connection kind — both are 'vsphere' to it — so the
    agent accepts the specialisation. The reverse would send SOAP work to a vCenter."""
    assert agent._kind_matches("esxi", "vsphere") is True
    assert agent._kind_matches("vsphere", "vsphere") is True
    assert agent._kind_matches("vsphere", "esxi") is False
    assert agent._kind_matches("proxmox", "vsphere") is False


def test_the_agent_still_imports_no_execution_machinery():
    """The socket is a capability, not a licence to grow one. The supervisor must stay
    inert: no subprocess, no docker SDK, no shell."""
    with open(_PATH, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    for banned in ("subprocess", "docker", "pty", "shlex"):
        assert banned not in imported, f"the agent imports {banned}"


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
