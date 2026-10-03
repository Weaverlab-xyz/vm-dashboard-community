"""The opt-in SPIRE compose overlays: the dashboard's server, and the remote agent's sidecar.

Neither overlay has attested a real agent yet (the agent code that uses the socket is
Phase 2 of docs/design/agent-and-human-identity.md). These pin what decides whether they
CAN work and whether they stay additive: the server config cannot drift from the SPIRE
lab's, the images are the lab's pinned ones, everything runs locked down, and nothing in
either overlay changes how an existing service runs.

Run: python tests/test_spire_overlay.py   (or under pytest)
"""
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

try:
    import yaml
except ModuleNotFoundError as exc:  # pragma: no cover -- app deps missing
    try:
        import pytest
        pytest.skip(f"PyYAML unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)


def _path(*parts):
    return os.path.join(_ROOT, *parts)


def _yaml(*parts):
    return yaml.safe_load(open(_path(*parts), encoding="utf-8").read())


def _text(*parts):
    return open(_path(*parts), encoding="utf-8").read()


def _hcl_value(conf, key):
    """`key = "value"` (or `key = value`) from a SPIRE HCL config; None when absent."""
    m = re.search(r'^\s*%s\s*=\s*"?([^"\n]+?)"?\s*$' % re.escape(key), conf, re.M)
    return m.group(1) if m else None


def _active_plugins(conf):
    """`(type, name)` of every plugin block that is not commented out."""
    lines = [ln for ln in conf.splitlines() if not ln.lstrip().startswith("#")]
    return set(re.findall(r'^\s*(\w+)\s+"(\w+)"\s*\{', "\n".join(lines), re.M))


_LAB_PLAY = ("examples", "playbooks", "spire", "spire-docker-server.yml")
_SERVER_CONF = ("examples", "spire-server", "server.conf")
_DASH_OVERLAY = ("docker-compose.spire.yml",)
_AGENT_OVERLAY = ("examples", "remote-agent", "docker-compose.spire.yml")
_AGENT_CONFS = {
    "join": ("examples", "remote-agent", "spire", "agent-join-token.conf"),
    "cloud": ("examples", "remote-agent", "spire", "agent-cloud.conf"),
}


def _lab():
    return _yaml(*_LAB_PLAY)[0]


# ── the server config cannot drift from the lab's ───────────────────────────

def test_the_dashboard_server_config_matches_the_lab():
    """Everything downstream (the dashboard's SVID verification, the 5-minute assertion
    lifetime it assumes, the join-token flow) is written against the lab's settings."""
    lab = _lab()["vars"]
    conf = _text(*_SERVER_CONF)
    assert _hcl_value(conf, "bind_port") == str(lab["bind_port"])
    assert _hcl_value(conf, "ca_ttl") == lab["ca_ttl"]
    assert _hcl_value(conf, "default_x509_svid_ttl") == lab["default_x509_svid_ttl"]
    assert _hcl_value(conf, "default_jwt_svid_ttl") == lab["default_jwt_svid_ttl"]
    assert _hcl_value(conf, "database_type") == "sqlite3"
    plugins = _active_plugins(conf)
    assert ("NodeAttestor", "join_token") in plugins
    assert ("KeyManager", "disk") in plugins
    assert _hcl_value(conf, "trust_domain") == "${SPIRE_TRUST_DOMAIN}", (
        "the trust domain must come from the environment (the server runs with -expandEnv)")


def test_the_server_image_is_the_labs_pinned_digest():
    lab = _lab()["vars"]
    version = lab["spire_version"]
    want = f"ghcr.io/spiffe/spire-server:{version}@{lab['spire_image_digests'][version]['server']}"
    got = _yaml(*_DASH_OVERLAY)["services"]["spire-server"]["image"]
    assert got == want, f"the overlay runs {got!r}; the lab pins {want!r}"
    assert "-expandEnv" in _yaml(*_DASH_OVERLAY)["services"]["spire-server"]["command"]


def test_the_agent_sidecar_runs_the_same_spire_release_by_digest():
    version = _lab()["vars"]["spire_version"]
    image = _yaml(*_AGENT_OVERLAY)["services"]["spire-agent"]["image"]
    assert image.startswith(f"ghcr.io/spiffe/spire-agent:{version}@sha256:"), (
        f"the sidecar runs {image!r}: it must be SPIRE {version}, pinned by digest")


# ── locked down ─────────────────────────────────────────────────────────────

def _locked(name, svc):
    assert svc["read_only"] is True, f"{name}: root filesystem must be read-only"
    assert svc["cap_drop"] == ["ALL"], f"{name}: every capability must be dropped"
    assert "no-new-privileges:true" in svc["security_opt"], name
    assert not str(svc["user"]).startswith("0"), f"{name} runs as root"


def test_the_spire_containers_run_unprivileged_and_locked_down():
    _locked("spire-server", _yaml(*_DASH_OVERLAY)["services"]["spire-server"])
    _locked("spire-agent", _yaml(*_AGENT_OVERLAY)["services"]["spire-agent"])


def test_the_volume_init_containers_can_only_chown():
    """They run as root because a named volume starts root-owned. That is all they may do:
    no network, no capability beyond ownership, and a pinned image."""
    for overlay in (_DASH_OVERLAY, _AGENT_OVERLAY):
        init = _yaml(*overlay)["services"]["spire-volumes-init"]
        assert init["network_mode"] == "none"
        assert init["cap_drop"] == ["ALL"]
        assert set(init["cap_add"]) <= {"CHOWN", "FOWNER"}
        assert "@sha256:" in init["image"], "the init image is not pinned by digest"
        assert init["restart"] == "no"


def test_nothing_mounts_the_docker_socket():
    """The workload attestor is `unix` over a shared PID namespace precisely so that no
    container on an agent host gets /var/run/docker.sock — which is root on the host."""
    for overlay in (_DASH_OVERLAY, _AGENT_OVERLAY):
        assert "docker.sock" not in _text(*overlay), f"{overlay[-1]} mounts the Docker socket"


# ── additive ────────────────────────────────────────────────────────────────
# compose merges an overlay's keys into the base service. An overlay that set image,
# command, user, ports or environment that the base relies on would change how an
# existing install runs — the opposite of opt-in.

_ADDITIVE_KEYS = {"volumes", "pid", "environment", "depends_on"}


def test_the_dashboard_overlay_only_adds_mounts_to_existing_services():
    services = _yaml(*_DASH_OVERLAY)["services"]
    base = _yaml("docker-compose.yml")["services"]
    for name in ("app", "worker"):
        assert name in base
        assert set(services[name]) == {"volumes"}, (
            f"the overlay changes {sorted(set(services[name]) - {'volumes'})} on {name}")
        assert all(str(v).endswith(":ro") for v in services[name]["volumes"]), (
            f"{name} gets the SPIRE admin socket writable")


def test_the_agent_overlay_only_adds_to_the_agent():
    services = _yaml(*_AGENT_OVERLAY)["services"]
    agent = services["agent"]
    assert set(agent) <= _ADDITIVE_KEYS, (
        f"the overlay changes {sorted(set(agent) - _ADDITIVE_KEYS)} on the agent")
    assert set(agent["environment"]) == {"SPIFFE_ENDPOINT_SOCKET"}
    base = _yaml("examples", "remote-agent", "docker-compose.yml")["services"]["agent"]
    assert "SPIFFE_ENDPOINT_SOCKET" not in (base.get("environment") or {}), (
        "the BASE agent compose must not opt in to SPIRE — only the overlay does")
    assert any("agent_state" in str(v) for v in base["volumes"]), (
        "agent_state must stay: it holds sealing.key, which SPIRE does not replace")


# ── the agent's attestation actually matches the agent ──────────────────────

def test_the_workload_is_identified_by_the_agent_images_uid():
    """The entry the dashboard creates selects `unix:uid:10001`. If the agent image ever
    changes its uid, the sidecar silently stops issuing it anything."""
    dockerfile = _text("runners", "agent", "Dockerfile")
    assert re.search(r"^USER 10001:10001$", dockerfile, re.M), "the agent image's uid moved"
    overlay = _yaml(*_AGENT_OVERLAY)["services"]
    assert overlay["agent"]["pid"] == "service:spire-agent", (
        "without a shared PID namespace the unix attestor cannot see the agent's process")
    design = _text("docs", "design", "agent-and-human-identity.md")
    assert "uid:10001" in design


def test_each_agent_config_matches_what_it_promises_about_keys_at_rest():
    join, cloud = (_text(*_AGENT_CONFS[k]) for k in ("join", "cloud"))
    assert ("KeyManager", "disk") in _active_plugins(join), (
        "join_token cannot re-attest after a restart, so its key must persist")
    assert ("KeyManager", "memory") in _active_plugins(cloud), (
        "the cloud config exists to keep NO key at rest")
    for name, conf in (("join", join), ("cloud", cloud)):
        plugins = _active_plugins(conf)
        assert ("WorkloadAttestor", "unix") in plugins, name
        assert len([p for p in plugins if p[0] == "NodeAttestor"]) == 1, (
            f"{name}: exactly one node attestor may be active")
        assert _hcl_value(conf, "trust_bundle_path"), f"{name}: no bootstrap trust bundle"
        active = "\n".join(ln for ln in conf.splitlines() if not ln.lstrip().startswith("#"))
        assert "insecure_bootstrap" not in active, (
            f"{name}: insecure_bootstrap trusts whoever answers the first connection")
        assert _hcl_value(conf, "socket_path") == "/run/spire/sockets/agent.sock"
    sock = _yaml(*_AGENT_OVERLAY)["services"]["agent"]["environment"]["SPIFFE_ENDPOINT_SOCKET"]
    assert sock == "unix:///run/spire/sockets/agent.sock"


def test_the_server_enables_every_attestor_an_agent_config_can_use():
    """An agent attesting with a plugin the server does not run is refused at the first
    connection, with an error that reads like a network fault."""
    server = _active_plugins(_text(*_SERVER_CONF))
    join = {p for p in _active_plugins(_text(*_AGENT_CONFS["join"])) if p[0] == "NodeAttestor"}
    assert join <= server, f"the server does not enable {join - server}"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
