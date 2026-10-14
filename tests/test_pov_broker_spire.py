"""POV broker agents attest through SPIRE, and fall back to their code when they cannot.

Pinned:

  * the sidecar images are the agent overlay's, digest for digest;
  * with SPIRE off the bootstrap still REMOVES a previous run's sidecars, and the agent
    enrols with its code exactly as before;
  * with SPIRE offered the script is valid, ASCII-only shell that writes the agent's SPIRE
    config with these values, pulls both images, waits a bounded time for an SVID, and
    falls back to the enrolment code when none comes;
  * the decision really is made on the guest: run with a fake `docker`, an SVID gives the
    agent AGENT_SPIFFE_JWT_FILE and no code mount, and no SVID gives it the code;
  * every value written into the root script is held to its shape — a quote, a newline or
    a `$(` in any of them is refused, not quoted;
  * the offer is made only when attestation is on, binds the agent AFTER its code is
    issued, and a server that is down leaves the broker unbound and on its code;
  * the job summary says which way the broker came up, the POV page gets broker_auth,
    and teardown removes the broker's SPIRE entries.

Run: python tests/test_pov_broker_spire.py   (or under pytest)
"""
import asyncio
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
_TMPDB = os.path.join(tempfile.mkdtemp(prefix="pov-broker-spire-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDB}"
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-pov-broker-spire")

try:
    import sqlalchemy  # noqa: F401
except ModuleNotFoundError as exc:  # pragma: no cover
    print(f"SKIP: {exc}")
    sys.exit(0)

from web_dashboard import database as d  # noqa: E402

d.Base.metadata.create_all(bind=d.engine)

from web_dashboard.services import (agent_service, config_service, dashboard_spire,  # noqa: E402
                                    lab_platforms, pov_broker, pov_env_service)

_URL = "https://pov-agents.example.test"
_PEM = ("-----BEGIN CERTIFICATE-----\nMIIBszCCAVmgAwIBAgIUQ0FfdGVzdF9vbmx5X25vdF9hX2NlcnQwCgYIKoZIzj0E\n"
        "-----END CERTIFICATE-----")
# A join token's SHAPE (a UUID), deliberately not random-looking: a high-entropy literal
# beside the word "join_token" is exactly what the secret scanner is there to catch.
_FAKE_TOKEN = str(uuid.UUID(int=1))
_OFFER = {"server_address": "pov-agents.example.test", "server_port": 8091,
          "trust_domain": "pov.example.test",
          "join_token": _FAKE_TOKEN, "bootstrap_pem": _PEM}


def _script(spire=None):
    return pov_broker.render_bootstrap(
        env_name="poc-1", dashboard_url=_URL, enroll_code="agte_x",
        policy_yaml=pov_broker.render_policy(["10.0.0.5"]), spire=spire)


def _sh_ok(script):
    sh = shutil.which("sh")
    if not sh:
        return
    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False, encoding="utf-8",
                                     newline="\n") as fh:
        fh.write(script)
    try:
        p = subprocess.run([sh, "-n", fh.name], capture_output=True, text=True)
        assert p.returncode == 0, p.stderr[:400]
    finally:
        os.unlink(fh.name)


# ── the script ───────────────────────────────────────────────────────────────

def test_the_images_are_the_agent_overlays():
    overlay = open(os.path.join(_ROOT, "examples", "remote-agent", "docker-compose.spire.yml"),
                   encoding="utf-8").read()
    assert pov_broker.SPIRE_AGENT_IMAGE in overlay
    assert pov_broker.SPIFFE_HELPER_IMAGE in overlay


def test_without_spire_the_sidecars_are_removed_and_the_code_is_used():
    script = _script()
    _sh_ok(script)
    for name in pov_broker.SPIRE_CONTAINERS:
        assert f"docker rm -f" in script and name in script, "a previous run's sidecars go"
    assert "--name dashboard-agent-spire" not in script, "nothing SPIRE is started"
    assert pov_broker.SPIRE_AGENT_IMAGE not in script, "nor pulled"
    assert 'AGENT_AUTH="ed25519"' in script
    assert "AGENT_ENROLLMENT_CODE_FILE=/etc/dashboard-agent/enroll-code" in script


def test_with_spire_the_script_is_valid_ascii_shell_with_these_values():
    script = _script(_OFFER)
    _sh_ok(script)
    bad = sorted({c for c in script if ord(c) > 127})
    assert not bad, f"non-ASCII in the payload: {bad!r}"
    for value in ("pov-agents.example.test", '"8091"', "pov.example.test",
                  _OFFER["join_token"], f'{_URL}/api/agent/attest'):
        assert value in script, value
    assert pov_broker.SPIRE_AGENT_IMAGE in script and pov_broker.SPIFFE_HELPER_IMAGE in script
    assert "--pid container:dashboard-agent-spire" in script, "unix attestor needs the PID ns"
    assert f'-lt {pov_broker.SPIRE_WAIT_SECONDS}' in script, "the wait is bounded"
    assert "Falling back to the enrolment code" in script


def test_the_agent_config_matches_the_example_one():
    """The overlay's agent-join-token.conf is the reference; the broker's inline copy must
    carry the same plugins and the same socket path."""
    ref = open(os.path.join(_ROOT, "examples", "remote-agent", "spire",
                            "agent-join-token.conf"), encoding="utf-8").read()
    script = _script(_OFFER)
    conf = script.split("DASHBOARD_SPIRE_AGENT_EOF'\n", 1)[1].split("DASHBOARD_SPIRE_AGENT_EOF", 1)[0]
    plugins = lambda text: set(re.findall(r'^\s*(\w+)\s+"(\w+)"', text, re.M))  # noqa: E731
    assert plugins(conf) == plugins(ref) - {("plugins", "")}
    assert '"/run/spire/sockets/agent.sock"' in conf and '"/run/spire/sockets/agent.sock"' in ref


def _decide(script, svid_appears):
    """Run the SPIRE block and the auth decision with a fake `docker`, as root would."""
    sh = shutil.which("sh")
    if not sh or os.name != "posix":
        return None
    start = script.index("# SPIRE: remove whatever")
    end = script.index("# The container runs as uid 10001")
    state = tempfile.mkdtemp(prefix="broker-state-")
    block = (script[start:end]
             .replace(pov_broker.GUEST_SPIRE_DIR, os.path.join(state, "spire")))
    fake = (
        "docker() {\n"
        '  case "$*" in\n'
        "    *'test -s'*) " + ("return 0" if svid_appears else "return 1") + " ;;\n"
        "    *) return 0 ;;\n"
        "  esac\n"
        "}\n"
        "chown() { :; }\n"
        "sleep() { :; }\n")
    run = ("set -eu\n" + fake + f'STATE="{state}"\n' + block
           + 'echo "AUTH=$AGENT_AUTH"\necho "ARGS=$AGENT_AUTH_ARGS"\n')
    p = subprocess.run([sh, "-s"], input=run, capture_output=True, text=True)
    shutil.rmtree(state, ignore_errors=True)
    assert p.returncode == 0, p.stderr[:400]
    return p.stdout


def test_an_svid_gives_the_agent_its_jwt_file_and_no_code():
    out = _decide(_script(_OFFER), svid_appears=True)
    if out is None:
        return  # no POSIX sh here; CI runs it
    assert "AUTH=spiffe" in out
    assert "AGENT_SPIFFE_JWT_FILE=/run/spiffe/jwt/attest.jwt" in out
    assert "enroll-code" not in out.split("ARGS=", 1)[1]


def test_no_svid_falls_back_to_the_code():
    out = _decide(_script(_OFFER), svid_appears=False)
    if out is None:
        return
    assert "AUTH=ed25519" in out
    assert "AGENT_ENROLLMENT_CODE_FILE" in out and "AGENT_SPIFFE_JWT_FILE" not in out


def test_every_value_is_held_to_its_shape():
    for key, bad in (("server_address", 'evil"; rm -rf /'),
                     ("server_address", "host\nname"),
                     ("server_address", "$(id)"),
                     ("server_port", 0), ("server_port", 70000),
                     ("trust_domain", "Pov.Example"),
                     ("trust_domain", 'td"x'),
                     ("join_token", "abc'def-123456"),
                     ("bootstrap_pem", "not a cert"),
                     ("bootstrap_pem", _PEM + "\n'; reboot; '")):
        try:
            pov_broker.check_spire_offer({**_OFFER, key: bad})
            raise AssertionError(f"{key}={bad!r} was accepted")
        except ValueError:
            pass
    assert pov_broker.check_spire_offer(_OFFER)["server_port"] == 8091


# ── the offer ────────────────────────────────────────────────────────────────

def _agent():
    db = d.SessionLocal()
    row = d.RemoteAgent(name="pov-" + uuid.uuid4().hex[:8], is_active=True)
    db.add(row)
    db.commit()
    return db, row


class _Patch:
    def __init__(self, obj, **attrs):
        self.obj, self.attrs, self.saved = obj, attrs, {}

    def __enter__(self):
        for k, v in self.attrs.items():
            self.saved[k] = getattr(self.obj, k)
            setattr(self.obj, k, v)
        return self

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            setattr(self.obj, k, v)


def _flags(on):
    return _Patch(config_service, get_bool=lambda key, *a, **k: on
                  if key == "spire_attest_enabled" else False)


def test_no_offer_while_attestation_is_off():
    db, agent = _agent()
    try:
        with _flags(False):
            facts, note = pov_broker.spire_offer(db, agent)
        assert facts is None and "off" in note
    finally:
        db.close()


def test_the_offer_binds_the_agent_like_migrate():
    db, agent = _agent()
    calls = []

    def migrate(db_, agent_):
        calls.append(agent_.id)
        agent_service.bind_spiffe_id(db_, agent_, f"spiffe://pov.example.test/agent/{agent_.id}")
        return {"trust_domain": "pov.example.test", "join_token": _OFFER["join_token"],
                "bootstrap_pem": _PEM, "spiffe_id": agent_.spiffe_id}
    try:
        with _flags(True), _server(verdict=True), _Patch(dashboard_spire, migrate_agent=migrate):
            facts, note = pov_broker.spire_offer(db, agent)
        assert calls == [agent.id]
        assert facts["join_token"] == _OFFER["join_token"] and facts["server_port"] == 8091
        assert agent.spiffe_id.endswith(agent.id) and "falls back" in note
        assert "verified" in note
    finally:
        db.close()


def test_a_down_server_leaves_the_broker_unbound_on_its_code():
    db, agent = _agent()

    def migrate(db_, agent_):
        agent_service.bind_spiffe_id(db_, agent_, f"spiffe://pov.example.test/agent/{agent_.id}")
        raise dashboard_spire.DashboardSpireError("the SPIRE server sidecar is not running")
    try:
        with _flags(True), _server(verdict=True), _Patch(dashboard_spire, migrate_agent=migrate):
            facts, note = pov_broker.spire_offer(db, agent)
        assert facts is None and "not running" in note
        db.refresh(agent)
        assert agent.spiffe_id is None, "a half-done offer must not leave a binding"
    finally:
        db.close()


def test_the_offer_comes_after_the_code_in_ensure_broker():
    """Re-issuing a code clears the binding, so the order is load-bearing."""
    src = open(os.path.join(_ROOT, "web_dashboard", "services", "pov_broker.py"),
               encoding="utf-8").read()
    body = src.split("async def ensure_broker(", 1)[1]
    assert body.index("_mint_code(db, env)") < body.index("spire_offer, db, agent")
    assert "asyncio.to_thread(spire_offer" in body, "relayed calls must not block the loop"
    assert "spire=spire" in body


def _server(verdict=True, why="checked", td_raises=None):
    """dashboard_spire's read side, faked: trust domain, bundle, address, and the verdict
    of the address check."""
    seq = list(td_raises or [])

    def td():
        if seq:
            raise seq.pop(0)
        return "pov.example.test"
    return _Patch(dashboard_spire, trust_domain=td, bootstrap_bundle_pem=lambda: _PEM,
                  server_address=lambda: "pov-agents.example.test", server_port=lambda: 8091,
                  check_agent_address=lambda *a, **k: (verdict, why))


def test_an_address_that_is_another_server_is_never_offered():
    """The live failure: a blank address handed the broker port 8081, which in that
    environment is another dashboard's SPIRE server."""
    db, agent = _agent()
    minted = []
    try:
        with _flags(True), _server(verdict=False, why="presents another trust domain"),                 _Patch(dashboard_spire, migrate_agent=lambda *a: minted.append(1)):
            facts, note = pov_broker.spire_offer(db, agent)
        assert facts is None and minted == [], "no token is minted for the wrong server"
        assert "another trust domain" in note
    finally:
        db.close()


def test_an_address_the_dashboard_cannot_reach_is_still_offered():
    """Unreachable from the dashboard proves nothing about the guest's network."""
    db, agent = _agent()

    def migrate(db_, agent_):
        agent_service.bind_spiffe_id(db_, agent_, f"spiffe://pov.example.test/agent/{agent_.id}")
        return {"trust_domain": "pov.example.test", "join_token": _OFFER["join_token"],
                "bootstrap_pem": _PEM, "spiffe_id": agent_.spiffe_id}
    try:
        with _flags(True), _server(verdict=None, why="timed out"),                 _Patch(dashboard_spire, migrate_agent=migrate):
            facts, note = pov_broker.spire_offer(db, agent)
        assert facts is not None and "not verifiable" in note and "timed out" in note
    finally:
        db.close()


def test_a_relay_that_is_just_starting_gets_one_more_chance():
    slept = []
    err = dashboard_spire.DashboardSpireError("the SPIRE server relay is not running: …")
    with _server(td_raises=[err]):
        assert pov_broker._trust_domain_with_grace(dashboard_spire, sleep=slept.append)             == "pov.example.test"
    assert slept == [pov_broker.RELAY_GRACE_S]
    with _server(td_raises=[err, err]):
        try:
            pov_broker._trust_domain_with_grace(dashboard_spire, sleep=slept.append)
            raise AssertionError("a relay that never comes must still fail")
        except dashboard_spire.DashboardSpireError:
            pass
    other = dashboard_spire.DashboardSpireError("spire-server bundle show failed: x")
    with _server(td_raises=[other]):
        try:
            pov_broker._trust_domain_with_grace(dashboard_spire, sleep=slept.append)
            raise AssertionError("only a missing relay is waited for")
        except dashboard_spire.DashboardSpireError:
            pass
    assert len(slept) == 2


def test_the_relay_rechecks_the_switches_within_seconds():
    from web_dashboard.services import spire_relay
    assert spire_relay.IN_USE_RECHECK_S <= 5
    assert pov_broker.RELAY_GRACE_S > spire_relay.IN_USE_RECHECK_S + spire_relay.POLL_S


# ── what the operator sees ───────────────────────────────────────────────────

def test_describe_reports_how_the_broker_signs_in():
    env = d.PovEnvironment(platform="skytap", name="x", template_id="1")
    none = pov_broker._described(env, None)
    assert none["broker_auth"] == ""
    a = d.RemoteAgent(name="a", is_active=True, public_key=None)
    assert pov_broker._described(env, a)["broker_auth"] == "", "not signed in yet"
    a.public_key, a.auth_mode = "k", None
    assert pov_broker._described(env, a)["broker_auth"] == "ed25519"
    a.auth_mode = "spiffe"
    assert pov_broker._described(env, a)["broker_auth"] == "spiffe"


def test_teardown_removes_the_brokers_spire_entries():
    db = d.SessionLocal()
    env = d.PovEnvironment(platform="skytap", name="td-" + uuid.uuid4().hex[:6],
                           template_id="1", status=pov_env_service.STATUS_ACTIVE)
    db.add(env)
    db.commit()
    agent = d.RemoteAgent(name=pov_broker.agent_name(env), is_active=True)
    db.add(agent)
    db.commit()
    agent.spiffe_id = f"spiffe://pov.example.test/agent/{agent.id}"
    env.broker_agent_id = agent.id
    db.commit()
    removed = []
    try:
        with _Patch(dashboard_spire,
                    remove_workload=lambda sid, node: removed.append((sid, node)) or []):
            line = pov_broker.teardown(db, env)
        assert removed == [(f"spiffe://pov.example.test/agent/{agent.id}",
                            f"spiffe://pov.example.test/node/{agent.id}")], removed
        assert "Revoked and removed" in line
    finally:
        db.close()


def test_the_pov_page_shows_it():
    tpl = open(os.path.join(_ROOT, "web_dashboard", "templates", "pov", "index.html"),
               encoding="utf-8").read()
    assert "e.broker_auth" in tpl


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    fns.sort(key=lambda f: f.__code__.co_firstlineno)
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
