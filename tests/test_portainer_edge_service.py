"""Unit tests for Edge-agent registration.

An Edge agent is the only way a managed Portainer node can manage a Docker host it
cannot reach: the agent runs ON the host and polls OUT to the node's tunnel port.
The failure modes here are all silent ones, so they are what these tests pin:

  * ``EDGE_INSECURE_POLL=1`` must be in the join command. The node serves a
    self-signed cert, and without it the agent's first poll fails verification and
    the environment simply never appears — no error the operator would find.
  * ``EDGE_ID`` must never be blank. Portainer only assigns one under
    ``EnforceEdgeID``; a blank id makes the agent collide with every other blank one.
  * A missing ``EdgeKey`` must raise rather than emit a command with an empty key,
    which would fail on the operator's host with a message about the agent.
  * The node URL is baked into the key, and the managed node's external IP is
    ephemeral — so a URL change silently invalidates every existing agent.

Also pins the multipart contract for ``POST /api/endpoints``, which is the trap that
makes this call unable to use the module's JSON ``_api`` helper.

Runs under pytest or standalone:

    python tests/test_portainer_edge_service.py
"""
import asyncio
import os
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_cfg_mod = types.ModuleType("web_dashboard.config")
_cfg_mod.settings = types.SimpleNamespace()
sys.modules["web_dashboard.config"] = _cfg_mod

_CONFIG = {"portainer_url": "https://203.0.113.9:9443"}
_cfgsvc = types.ModuleType("web_dashboard.services.config_service")
_cfgsvc.get = lambda key, default=None: _CONFIG.get(key, "")
_cfgsvc.set = lambda key, val: _CONFIG.__setitem__(key, val)
_cfgsvc.get_bool = lambda key, default=False: bool(_CONFIG.get(key, default))
sys.modules["web_dashboard.services.config_service"] = _cfgsvc


class _StubError(Exception):
    pass


class FakePortainer(types.ModuleType):
    PortainerError = _StubError
    EDGE_AGENT_ENVIRONMENT = 4

    def __init__(self, response=None):
        super().__init__("web_dashboard.services.portainer_service")
        self.calls = []
        self.response = response if response is not None else {
            "Id": 3, "Name": "lab-docker", "EdgeKey": "ZWRnZS1rZXk="}

    async def create_edge_endpoint(self, name, server_url, *, group_id=1,
                                   checkin_interval=5):
        self.calls.append({"name": name, "server_url": server_url,
                           "group_id": group_id, "checkin_interval": checkin_interval})
        if isinstance(self.response, Exception):
            raise self.response
        return dict(self.response)


def _install(fake):
    sys.modules["web_dashboard.services.portainer_service"] = fake
    for mod in list(sys.modules):
        if mod.endswith("portainer_edge_service"):
            del sys.modules[mod]
    import importlib
    return importlib.import_module("web_dashboard.services.portainer_edge_service")


# ── join command ─────────────────────────────────────────────────────────────

def test_the_join_command_disables_certificate_verification():
    """The node's cert is self-signed. Without EDGE_INSECURE_POLL the agent's first
    poll fails verification and the environment never appears at all."""
    svc = _install(FakePortainer())
    cmd = svc.join_command("edge-id-1", "key-1")
    assert "-e EDGE_INSECURE_POLL=1" in cmd, cmd


def test_the_join_command_carries_the_id_key_and_socket():
    svc = _install(FakePortainer())
    cmd = svc.join_command("edge-id-1", "key-1")
    assert "-e EDGE=1" in cmd
    assert "-e EDGE_ID=edge-id-1" in cmd
    assert "-e EDGE_KEY=key-1" in cmd
    # The agent manages the host's Docker, so it needs the socket and a restart policy.
    assert "/var/run/docker.sock:/var/run/docker.sock" in cmd
    assert "--restart always" in cmd
    assert cmd.rstrip().endswith(svc.DEFAULT_AGENT_IMAGE)


def test_the_join_command_honours_a_custom_agent_image():
    svc = _install(FakePortainer())
    cmd = svc.join_command("i", "k", image="portainer/agent:2.21.0")
    assert cmd.rstrip().endswith("portainer/agent:2.21.0")
    assert svc.DEFAULT_AGENT_IMAGE not in cmd


def test_the_join_command_is_a_single_pasteable_block():
    """Every line but the last must continue, or the operator pastes a truncated
    command that silently runs without the Edge variables."""
    svc = _install(FakePortainer())
    lines = svc.join_command("i", "k").split("\n")
    assert all(line.rstrip().endswith("\\") for line in lines[:-1]), lines
    assert not lines[-1].rstrip().endswith("\\")


def test_generated_edge_ids_are_unique():
    """A blank or repeated EDGE_ID makes the agent register as a duplicate."""
    svc = _install(FakePortainer())
    ids = {svc.generate_edge_id() for _ in range(20)}
    assert len(ids) == 20
    assert all(i for i in ids)


# ── register ─────────────────────────────────────────────────────────────────

def test_register_uses_the_configured_node_url_as_the_dial_back_address():
    """The Edge key is derived from this URL — passing a stale one mints keys that can
    never check in."""
    fake = FakePortainer()
    svc = _install(fake)
    out = asyncio.run(svc.register("lab-docker"))
    assert fake.calls[0]["server_url"] == "https://203.0.113.9:9443"
    assert out["server_url"] == "https://203.0.113.9:9443"
    assert out["endpoint_id"] == 3
    assert out["tunnel_port"] == 8000, "the node firewall opens 8000 for the tunnel"
    assert "EDGE_KEY=ZWRnZS1rZXk=" in out["join_command"]


def test_register_supplies_its_own_edge_id_when_portainer_does_not():
    """Portainer sets EdgeID only under EnforceEdgeID, so the common case is blank."""
    fake = FakePortainer({"Id": 3, "Name": "n", "EdgeKey": "k"})
    svc = _install(fake)
    out = asyncio.run(svc.register("n"))
    assert out["edge_id"], "a blank EDGE_ID collides with every other blank one"
    assert f"EDGE_ID={out['edge_id']}" in out["join_command"]


def test_register_prefers_an_enforced_edge_id():
    fake = FakePortainer({"Id": 3, "Name": "n", "EdgeKey": "k",
                          "EdgeID": "server-assigned-id"})
    svc = _install(fake)
    assert asyncio.run(svc.register("n"))["edge_id"] == "server-assigned-id"


def test_register_refuses_to_return_a_command_with_no_key():
    """The environment exists but cannot be joined. Emitting a command with an empty
    EDGE_KEY would fail on the operator's host with a message about the agent."""
    fake = FakePortainer({"Id": 9, "Name": "n", "EdgeKey": ""})
    svc = _install(fake)
    try:
        asyncio.run(svc.register("n"))
    except _StubError as exc:
        assert "no Edge key" in str(exc) and "9" in str(exc), exc
    else:
        raise AssertionError("a keyless registration must raise")


def test_register_passes_the_checkin_interval_through():
    fake = FakePortainer()
    svc = _install(fake)
    asyncio.run(svc.register("n", checkin_interval=30))
    assert fake.calls[0]["checkin_interval"] == 30


# ── stale key detection ──────────────────────────────────────────────────────

def test_a_changed_node_url_warns_that_existing_agents_are_orphaned():
    """The managed node takes an EPHEMERAL external IP, so a recreate changes the URL
    and every previously joined agent stops checking in — with no error, just
    environments quietly going offline."""
    svc = _install(FakePortainer())
    msg = svc.stale_keys_warning([{"url": "https://198.51.100.4:9443"}])
    assert "no longer check in" in msg, msg
    assert "203.0.113.9" in msg and "198.51.100.4" in msg


def test_a_matching_node_url_is_silent():
    svc = _install(FakePortainer())
    assert svc.stale_keys_warning([{"url": "https://203.0.113.9:9443/"}]) == ""


def test_no_nodes_or_no_config_is_silent():
    svc = _install(FakePortainer())
    assert svc.stale_keys_warning([]) == ""
    _CONFIG["portainer_url"] = ""
    try:
        assert svc.stale_keys_warning([{"url": "https://x:9443"}]) == ""
    finally:
        _CONFIG["portainer_url"] = "https://203.0.113.9:9443"


# ── the multipart contract on POST /api/endpoints ────────────────────────────

def test_the_endpoint_create_call_is_multipart_not_json():
    """Portainer's endpointCreate reads every field with RetrieveMultiPartFormValue.
    A JSON body arrives as "invalid environment name", which points nowhere near the
    real cause — so this call cannot go through the module's JSON _api helper."""
    src = open(os.path.join(_ROOT, "web_dashboard", "services",
                            "portainer_service.py"), encoding="utf-8").read()
    fn = src[src.index("async def create_edge_endpoint"):]
    fn = fn[:fn.index("\n# ── Registries")] if "\n# ── Registries" in fn else fn
    assert "_api_form(" in fn, "create_edge_endpoint must use the multipart helper"
    assert "_api(" not in fn.replace("_api_form(", ""), (
        "create_edge_endpoint must not use the JSON helper")
    form = src[src.index("async def _api_form"):src.index("async def create_edge_endpoint")]
    # httpx only emits multipart when `files` is populated; (None, value) makes a
    # plain form FIELD rather than a file part.
    assert "files=parts" in form and "(None, str(v))" in form, form


def test_the_edge_creation_type_and_field_names_match_portainers_handler():
    """Three exact-match traps: creation type 4, the tag field spelled TagIds (not
    TagIDs as in the Go struct), and no TLS field at all — Portainer rejects TLS
    outright for an Edge environment."""
    src = open(os.path.join(_ROOT, "web_dashboard", "services",
                            "portainer_service.py"), encoding="utf-8").read()
    assert "EDGE_AGENT_ENVIRONMENT = 4" in src
    fn = src[src.index("async def create_edge_endpoint"):]
    fn = fn[:fn.index("# ── Registries")]
    for field in ('"Name"', '"EndpointCreationType"', '"URL"', '"TagIds"'):
        assert field in fn, f"missing form field {field}"
    assert '"TagIDs"' not in fn, "Portainer's form field is TagIds, not TagIDs"
    assert '"TLS"' not in fn, "TLS is rejected for an Edge environment"


def test_a_registration_without_a_node_url_explains_which_url_is_missing():
    """Portainer's own error is just "URL cannot be empty"."""
    src = open(os.path.join(_ROOT, "web_dashboard", "services",
                            "portainer_service.py"), encoding="utf-8").read()
    fn = src[src.index("async def create_edge_endpoint"):]
    fn = fn[:fn.index("# ── Registries")]
    assert "the agent will dial" in fn


if __name__ == "__main__":
    _tests = [v for k, v in sorted(globals().items())
              if k.startswith("test_") and callable(v)]
    _failures = 0
    for _t in _tests:
        try:
            _t()
            print(f"PASS {_t.__name__}")
        except Exception as _e:  # noqa: BLE001
            _failures += 1
            print(f"FAIL {_t.__name__}: {_e!r}")
    print(f"\n{len(_tests) - _failures}/{len(_tests)} passed")
    sys.exit(1 if _failures else 0)
