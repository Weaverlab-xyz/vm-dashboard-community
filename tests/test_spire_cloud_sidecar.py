"""The dashboard's SPIRE server on a managed container runtime (ACA, Cloud Run, ECS, k8s).

docker-compose.spire.yml drives the server with ``docker exec``; a managed runtime has no
Docker socket and no volume the app and the worker can share. Pinned here:

  * the transport each process picks — explicit, sidecar socket, Docker socket, relay —
    and the argv the socket transport runs;
  * a stopped or unreachable sidecar reads as "is not running", which the federation
    callers rely on, and a uid mismatch says so;
  * the database relay: a worker's call is answered by an app process, stdin and output
    are encrypted at rest, the row is gone afterwards, a dead relay fails at once rather
    than after a timeout, an unclaimed call is withdrawn, and a command outside the
    allow-list is refused — and the allow-list covers every command the module sends;
  * only the socket transport serves the relay, so a compose install never polls;
  * the worker keeps its own token files only where its directory is writable;
  * an unknown kid re-reads the dashboard's OWN bundle, at most once a minute, and never
    another trust domain's;
  * the sidecar image: same pinned SPIRE as the overlay and the app image, the same
    timings, Postgres + an upstream CA, the socket path the app defaults to, and an
    entrypoint that refuses to start without its CA.

Run: python tests/test_spire_cloud_sidecar.py   (or under pytest)
"""
import ast
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="spire-cloud-test-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDB}"
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-spire-cloud-sidecar")
for _var in ("SPIRE_SERVER_TRANSPORT", "SPIRE_SERVER_SOCKET", "DOCKER_HOST"):
    os.environ.pop(_var, None)

try:
    import cryptography  # noqa: F401
    import sqlalchemy  # noqa: F401
except ModuleNotFoundError as exc:  # pragma: no cover — app deps missing
    try:
        import pytest
        pytest.skip(f"app dependencies unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)

from web_dashboard.database import (Base, SessionLocal, SpireAdminCall,  # noqa: E402
                                    SpireAdminRelay, engine)
from web_dashboard.services import dashboard_spire, spire_relay  # noqa: E402
from _settings_src import settings_source

Base.metadata.create_all(bind=engine)


class _Env:
    """Set environment variables for one block, and put them back."""

    def __init__(self, **values):
        self.values = values
        self.saved = {}

    def __enter__(self):
        for k, v in self.values.items():
            self.saved[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return self

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _text(*parts):
    return open(os.path.join(_ROOT, *parts), encoding="utf-8").read()


# ── transport ────────────────────────────────────────────────────────────────

def test_the_transport_follows_what_the_container_has():
    real_exists = os.path.exists
    try:
        os.path.exists = lambda p: False if p == dashboard_spire.DOCKER_SOCKET else real_exists(p)
        with _Env(SPIRE_SERVER_TRANSPORT=None, SPIRE_SERVER_SOCKET=None, DOCKER_HOST=None):
            assert dashboard_spire.transport() == "relay"
        with _Env(SPIRE_SERVER_TRANSPORT=None, SPIRE_SERVER_SOCKET=None,
                  DOCKER_HOST="tcp://docker:2375"):
            assert dashboard_spire.transport() == "docker"
        os.path.exists = lambda p: True if p == dashboard_spire.DOCKER_SOCKET else real_exists(p)
        with _Env(SPIRE_SERVER_TRANSPORT=None, SPIRE_SERVER_SOCKET=None, DOCKER_HOST=None):
            assert dashboard_spire.transport() == "docker"
        # A sidecar socket outranks a Docker socket; an explicit choice outranks both.
        with _Env(SPIRE_SERVER_TRANSPORT=None, SPIRE_SERVER_SOCKET="/run/spire-server/api.sock"):
            assert dashboard_spire.transport() == "socket"
        with _Env(SPIRE_SERVER_TRANSPORT="relay", SPIRE_SERVER_SOCKET="/x"):
            assert dashboard_spire.transport() == "relay"
        with _Env(SPIRE_SERVER_TRANSPORT="nonsense", SPIRE_SERVER_SOCKET="/x"):
            assert dashboard_spire.transport() == "socket"
    finally:
        os.path.exists = real_exists


def _capture(stdout="{}", returncode=0, stderr=""):
    seen = []

    def run(argv, timeout, input=None):
        seen.append({"argv": argv, "input": input})
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr)
    return seen, run


def test_the_socket_transport_runs_the_local_binary_against_the_socket():
    seen, run = _capture('{"trust_domain": "dash.test"}')
    saved = dashboard_spire._run
    dashboard_spire._run = run
    try:
        with _Env(SPIRE_SERVER_TRANSPORT=None, SPIRE_SERVER_SOCKET="/run/s/api.sock"):
            assert dashboard_spire.trust_domain() == "dash.test"
            dashboard_spire._cli("federation", "create", input="BUNDLE")
    finally:
        dashboard_spire._run = saved
    assert seen[0]["argv"] == [dashboard_spire.LOCAL_BIN, "bundle", "show", "-output", "json",
                               "-socketPath", "/run/s/api.sock"], seen[0]
    assert seen[1]["input"] == "BUNDLE" and "docker" not in seen[1]["argv"]


def test_the_docker_transport_is_unchanged():
    seen, run = _capture('{"trust_domain": "dash.test"}')
    saved = dashboard_spire._run
    dashboard_spire._run = run
    try:
        with _Env(SPIRE_SERVER_TRANSPORT="docker"):
            dashboard_spire.trust_domain()
    finally:
        dashboard_spire._run = saved
    assert seen[0]["argv"][:3] == ["docker", "exec", dashboard_spire.container()]
    assert seen[0]["argv"][3] == dashboard_spire.SPIRE_BIN


def _fails_with(stderr):
    _, run = _capture("", 1, stderr)
    saved = dashboard_spire._run
    dashboard_spire._run = run
    try:
        with _Env(SPIRE_SERVER_TRANSPORT=None, SPIRE_SERVER_SOCKET="/run/s/api.sock"):
            dashboard_spire._cli("bundle", "show")
    except dashboard_spire.DashboardSpireError as exc:
        return str(exc)
    finally:
        dashboard_spire._run = saved
    raise AssertionError("no error raised")


def test_a_stopped_sidecar_reads_as_not_running():
    for stderr in ('Error: connection error: desc = "transport: Error while dialing: dial unix '
                   '///run/s/api.sock: connect: no such file or directory"',
                   "dial unix /run/s/api.sock: connect: connection refused"):
        msg = _fails_with(stderr)
        assert "is not running" in msg and "/run/spire-server" in msg, msg


def test_a_uid_mismatch_names_the_uid():
    msg = _fails_with("dial unix ///run/s/api.sock: connect: permission denied")
    assert "10001" in msg and "SPIRE_RUN_UID" in msg, msg


def test_a_missing_binary_says_which_image():
    def run(argv, timeout, input=None):
        raise FileNotFoundError(argv[0])
    saved = dashboard_spire._run
    dashboard_spire._run = run
    try:
        with _Env(SPIRE_SERVER_TRANSPORT=None, SPIRE_SERVER_SOCKET="/run/s/api.sock"):
            dashboard_spire._cli("bundle", "show")
        raise AssertionError("no error raised")
    except dashboard_spire.DashboardSpireError as exc:
        assert dashboard_spire.LOCAL_BIN in str(exc)
    finally:
        dashboard_spire._run = saved


# ── the relay ────────────────────────────────────────────────────────────────

def _clear_relay_tables():
    db = SessionLocal()
    try:
        db.query(SpireAdminCall).delete()
        db.query(SpireAdminRelay).delete()
        db.commit()
    finally:
        db.close()


class _Server:
    """An app process with a sidecar: claims relayed calls and answers them through a fake
    of its local transport, recording what it was asked and what the rows held."""

    def __init__(self, stdout='{"svid": {"token": "aaa.bbb.ccc"}}'):
        self.stdout = stdout
        self.seen = []
        self.rows_in_flight = []
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._loop, daemon=True)

    def _exec(self, args, timeout, input=None):
        self.seen.append({"args": args, "input": input})
        return subprocess.CompletedProcess(args, 0, self.stdout, "")

    def _loop(self):
        while not self.stop.is_set():
            db = SessionLocal()
            try:
                spire_relay.heartbeat(db, "app:1", "socket")
                spire_relay.serve_once(db, "app:1")
                for row in db.query(SpireAdminCall).filter(
                        SpireAdminCall.status == "done").all():
                    self.rows_in_flight.append((row.stdin_enc, row.stdout_enc))
            finally:
                db.close()
            time.sleep(0.05)

    def __enter__(self):
        self.saved = dashboard_spire.exec_local
        dashboard_spire.exec_local = self._exec
        self.thread.start()
        time.sleep(0.2)                       # first heartbeat
        return self

    def __exit__(self, *exc):
        self.stop.set()
        self.thread.join(5)
        dashboard_spire.exec_local = self.saved


def test_a_relayed_call_is_answered_by_the_app_and_leaves_nothing_behind():
    _clear_relay_tables()
    with _Env(SPIRE_SERVER_TRANSPORT="relay"), _Server() as server:
        token = dashboard_spire.mint_jwt("aud", 300, "dash.test")
        dashboard_spire._cli("federation", "create", input="SEED-BUNDLE")
    assert token == "aaa.bbb.ccc"
    assert server.seen[0]["args"][:2] == ["jwt", "mint"], server.seen[0]
    assert "-output" in server.seen[0]["args"], "the same argv the local transports run"
    assert server.seen[1]["input"] == "SEED-BUNDLE"
    assert server.rows_in_flight, "the server saw its answered rows"
    for stdin_enc, stdout_enc in server.rows_in_flight:
        assert "aaa.bbb.ccc" not in (stdout_enc or ""), "output is encrypted at rest"
        assert "SEED-BUNDLE" not in (stdin_enc or ""), "stdin is encrypted at rest"
    db = SessionLocal()
    try:
        assert db.query(SpireAdminCall).count() == 0, "the requester deletes its row"
    finally:
        db.close()


def test_a_relayed_failure_is_checked_like_a_local_one():
    """The serving process's stderr comes back as-is, so a sidecar that is down reads the
    same from the worker as from the app."""
    _clear_relay_tables()
    with _Env(SPIRE_SERVER_TRANSPORT="relay"), _Server():
        dashboard_spire.exec_local = lambda args, timeout, input=None: subprocess.CompletedProcess(
            args, 1, "", "dial unix /run/spire-server/api.sock: connect: connection refused")
        try:
            dashboard_spire.trust_domain()
            raise AssertionError("no error raised")
        except dashboard_spire.DashboardSpireError as exc:
            assert "is not running" in str(exc), exc


def test_no_live_relay_fails_at_once_and_says_why():
    _clear_relay_tables()
    with _Env(SPIRE_SERVER_TRANSPORT="relay"):
        started = time.monotonic()
        try:
            dashboard_spire.trust_domain()
            raise AssertionError("no error raised")
        except dashboard_spire.DashboardSpireError as exc:
            assert "is not running" in str(exc) and "SPIRE_SERVER_SOCKET" in str(exc)
        assert time.monotonic() - started < 2
    db = SessionLocal()
    try:
        assert db.query(SpireAdminCall).count() == 0, "nothing is queued for nobody"
    finally:
        db.close()


def test_an_unclaimed_call_is_withdrawn():
    _clear_relay_tables()
    db = SessionLocal()
    try:
        spire_relay.heartbeat(db, "app:gone", "socket")   # alive, but never serves
    finally:
        db.close()
    saved = spire_relay.CLAIM_WAIT_S
    spire_relay.CLAIM_WAIT_S = 0.5
    try:
        spire_relay.call(["bundle", "show"], 1)
        raise AssertionError("no error raised")
    except spire_relay.RelayError as exc:
        assert "not answered" in str(exc)
    finally:
        spire_relay.CLAIM_WAIT_S = saved
    db = SessionLocal()
    try:
        assert db.query(SpireAdminCall).count() == 0
    finally:
        db.close()


def test_the_server_refuses_a_command_outside_the_allow_list():
    seen, run = _capture()
    saved = dashboard_spire._run
    dashboard_spire._run = run
    try:
        with _Env(SPIRE_SERVER_TRANSPORT=None, SPIRE_SERVER_SOCKET="/run/s/api.sock"):
            out = spire_relay._execute(["run", "-config", "/etc/x"], 5, None)
            assert out.returncode != 0 and "not a command the relay runs" in out.stderr
            out = spire_relay._execute("bundle show", 5, None)
            assert out.returncode != 0
    finally:
        dashboard_spire._run = saved
    assert seen == []


def test_the_allow_list_covers_every_command_the_module_sends():
    """A command added to dashboard_spire but not to the list works on compose and fails
    only on a managed runtime's worker — the one place nobody tests by hand."""
    tree = ast.parse(_text("web_dashboard", "services", "dashboard_spire.py"))
    sent = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id in ("_cli", "_json") and node.args
                and isinstance(node.args[0], ast.Constant)):
            sent.add(node.args[0].value)
    assert sent, "found no CLI calls — has the module changed shape?"
    assert sent <= spire_relay.ALLOWED_COMMANDS, sent - spire_relay.ALLOWED_COMMANDS


def test_only_a_socket_process_serves_the_relay():
    started = []

    class Fake:
        def __init__(self, transport):
            started.append(transport)

        def start(self):
            pass
    saved = spire_relay.RelayServer
    spire_relay.RelayServer = Fake
    try:
        for env, serves in (({"SPIRE_SERVER_TRANSPORT": "docker"}, False),
                            ({"SPIRE_SERVER_TRANSPORT": "relay"}, False),
                            ({"SPIRE_SERVER_TRANSPORT": None,
                              "SPIRE_SERVER_SOCKET": "/run/s/api.sock"}, True)):
            started.clear()
            with _Env(**env):
                result = spire_relay.start()
            assert (result is not None) == serves and bool(started) == serves, env
    finally:
        spire_relay.RelayServer = saved


def test_the_app_starts_the_relay_and_stops_it():
    main = _text("web_dashboard", "main.py")
    assert "spire_relay.start()" in main and "spire_relay_server.stop()" in main


# ── the worker's own token files ─────────────────────────────────────────────

def test_the_worker_writes_tokens_only_where_it_can():
    from web_dashboard.services import dashboard_identity
    base = tempfile.mkdtemp(prefix="spiffe-dir-")
    fresh = os.path.join(base, "made-here")
    with _Env(SPIFFE_TOKEN_DIR=fresh):
        assert dashboard_identity.writes_here() and os.path.isdir(fresh)
    if os.name == "posix" and os.geteuid() != 0:
        ro = os.path.join(base, "ro")
        os.makedirs(ro)
        os.chmod(ro, stat.S_IRUSR | stat.S_IXUSR)
        try:
            with _Env(SPIFFE_TOKEN_DIR=ro):
                assert not dashboard_identity.writes_here()
        finally:
            os.chmod(ro, stat.S_IRWXU)
    shutil.rmtree(base, ignore_errors=True)


def test_the_worker_runs_the_token_loop():
    worker = _text("web_dashboard", "jobs_worker.py")
    loop = worker.split("async def _spiffe_token_loop", 1)[1].split("\nasync def ", 1)[0]
    assert "dashboard_identity.writes_here()" in loop
    assert "asyncio.to_thread(dashboard_identity.refresh)" in loop, "off the event loop"
    main = worker.split("async def _main", 1)[1]
    assert "_spiffe_token_loop()" in main


# ── an unknown kid re-reads the dashboard's own bundle ───────────────────────

def test_an_unknown_kid_resyncs_only_the_dashboards_own_bundle():
    from web_dashboard.services import spiffe_assertion
    calls = []

    class Db:
        def refresh(self, row):
            row.refreshed = True
    saved = dashboard_spire.sync_trust_domain
    dashboard_spire.sync_trust_domain = lambda db, td: calls.append(td)
    spiffe_assertion._last_resync.clear()
    try:
        own = types.SimpleNamespace(trust_domain="dash.test", jwks_url=None,
                                    created_by=dashboard_spire.OWNER)
        held = [{"kid": "old"}]
        assert not spiffe_assertion._resync_own_bundle(Db(), own, "old", held), "known kid"
        assert spiffe_assertion._resync_own_bundle(Db(), own, "new", held)
        assert own.refreshed and calls == ["dash.test"]
        # sync_trust_domain clears the JWKS state; the cap must survive that.
        spiffe_assertion.clear_state()
        assert not spiffe_assertion._resync_own_bundle(Db(), own, "newer", held), "capped"
        other = types.SimpleNamespace(trust_domain="lab.test", jwks_url=None,
                                      created_by="admin")
        assert not spiffe_assertion._resync_own_bundle(Db(), other, "new", held)
        assert calls == ["dash.test"]
    finally:
        dashboard_spire.sync_trust_domain = saved
        spiffe_assertion._last_resync.clear()


def test_verify_asks_for_the_resync():
    src = _text("web_dashboard", "services", "spiffe_assertion.py")
    body = src.split("def verify(", 1)[1].split("\ndef ", 1)[0]
    assert "_resync_own_bundle(db, row" in body


# ── the agent-facing address ─────────────────────────────────────────────────

def test_the_agent_address_setting():
    from web_dashboard.services import agent_service, config_service
    saved = config_service.get
    try:
        for value, host, port in (("", "agents.test", 8081),
                                  ("spire.example.com", "spire.example.com", 8081),
                                  ("nlb-1.elb.amazonaws.com:443", "nlb-1.elb.amazonaws.com", 443),
                                  ("tcp://10.0.0.5:9081/", "10.0.0.5", 9081),
                                  ("[2001:db8::1]:8081", "2001:db8::1", 8081)):
            cfg = {"spire_server_address": value,
                   agent_service.AUDIENCE_CONFIG: "https://agents.test"}
            config_service.get = lambda key, *a, **k: cfg.get(key, "")
            assert dashboard_spire.server_address() == host, (value, dashboard_spire.server_address())
            assert dashboard_spire.server_port() == port, (value, dashboard_spire.server_port())
    finally:
        config_service.get = saved


def test_the_address_setting_is_declared_where_the_panel_saves_it():
    """Bound but undeclared is silently discarded on save."""
    assert "spire_server_address: str" in _text("web_dashboard", "config.py")
    assert "spire_server_address: str" in _text("web_dashboard", "api", "setup.py")
    assert 'x-model="panelCfg.spire_server_address"' in settings_source()


# ── the sidecar image ────────────────────────────────────────────────────────

_DIGEST = re.compile(r"ghcr\.io/spiffe/spire-server:[\w.]+@sha256:[0-9a-f]{64}")


def test_one_spire_image_everywhere():
    overlay = _DIGEST.findall(_text("docker-compose.spire.yml"))
    sidecar = _DIGEST.findall(_text("examples", "cloud-spire", "Dockerfile"))
    app = _DIGEST.findall(_text("Dockerfile"))
    assert overlay and sidecar and app
    assert set(overlay) == set(sidecar) == set(app), (overlay, sidecar, app)


def test_the_app_image_carries_the_cli_and_a_token_dir():
    dockerfile = _text("Dockerfile")
    assert "COPY --from=spire /opt/spire/bin/spire-server /usr/local/bin/spire-server" in dockerfile
    assert dashboard_spire.LOCAL_BIN == "/usr/local/bin/spire-server"
    assert "/run/spiffe-tokens" in dockerfile


def _hcl(conf, key):
    m = re.search(r'^\s*%s\s*=\s*"?([^"\n]+?)"?\s*$' % re.escape(key), conf, re.M)
    return m.group(1) if m else None


def test_the_sidecar_config_matches_the_compose_one_where_it_must():
    compose = _text("examples", "spire-server", "server.conf")
    cloud = _text("examples", "cloud-spire", "server.conf")
    for key in ("bind_port", "trust_domain", "ca_ttl", "default_x509_svid_ttl",
                "agent_ttl", "default_jwt_svid_ttl", "jwt_issuer"):
        assert _hcl(cloud, key) == _hcl(compose, key), key
    assert _hcl(cloud, "socket_path") == dashboard_spire.DEFAULT_SOCKET
    assert _hcl(cloud, "database_type") == "postgres"
    assert _hcl(cloud, "connection_string") == "${SPIRE_DATASTORE_URL}"
    # It shares the dashboard's PostgreSQL; an unbounded pool could starve the app.
    assert 0 < int(_hcl(cloud, "max_open_conns")) <= 10
    plugins = set(re.findall(r'^\s*(\w+)\s+"(\w+)"\s*\{', cloud, re.M))
    assert ("UpstreamAuthority", "disk") in plugins
    assert ("KeyManager", "memory") in plugins
    assert ("NodeAttestor", "join_token") in plugins
    assert re.search(r"health_checks\s*\{[^}]*listener_enabled\s*=\s*true", cloud, re.S)
    assert dashboard_spire.FEDERATION_PORT == int(_hcl(cloud, "port"))


def test_the_sidecar_runs_as_the_apps_uid():
    dockerfile = _text("examples", "cloud-spire", "Dockerfile")
    entry = _text("examples", "cloud-spire", "entrypoint.sh")
    assert "spire:x:10001:10001" in dockerfile
    assert "SPIRE_RUN_UID:-10001" in entry
    assert "setuidgid spire" in entry


def test_the_server_gets_a_home_it_can_read():
    """With sslmode=require the Postgres driver stats $HOME/.postgresql/postgresql.crt.
    Left at root's 0700 /root, that stat is "permission denied" and the server dies on
    start — found on the first live ACA deploy. HOME must be set before the exec."""
    entry = _text("examples", "cloud-spire", "entrypoint.sh")
    assert 'export HOME="$DATA_DIR"' in entry
    assert entry.index('export HOME="$DATA_DIR"') < entry.index("exec setuidgid")


def _entrypoint(env):
    sh = shutil.which("sh")
    if not sh or os.name != "posix":
        return None
    script = os.path.join(_ROOT, "examples", "cloud-spire", "entrypoint.sh")
    return subprocess.run([sh, script], env={"PATH": os.environ.get("PATH", ""), **env},
                          capture_output=True, text=True, timeout=10)


def test_the_entrypoint_refuses_to_start_without_its_state():
    base = {"SPIRE_TRUST_DOMAIN": "dash.test",
            "SPIRE_DATASTORE_URL": "postgresql://u:p@h/spire",
            "SPIRE_UPSTREAM_CA_CERT": "-----BEGIN CERTIFICATE-----\nx\n-----END CERTIFICATE-----",
            "SPIRE_UPSTREAM_CA_KEY": "-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----"}
    for drop, says in (("SPIRE_DATASTORE_URL", "PostgreSQL"),
                       ("SPIRE_UPSTREAM_CA_CERT", "SPIRE_UPSTREAM_CA_CERT"),
                       ("SPIRE_UPSTREAM_CA_KEY", "SPIRE_UPSTREAM_CA_KEY"),
                       ("SPIRE_TRUST_DOMAIN", "SPIRE_TRUST_DOMAIN")):
        env = {k: v for k, v in base.items() if k != drop}
        out = _entrypoint(env)
        if out is None:
            return  # no POSIX sh here; CI runs it
        assert out.returncode == 64 and says in out.stderr, (drop, out.returncode, out.stderr)
    out = _entrypoint({**base, "SPIRE_UPSTREAM_CA_KEY_FILE": "/nonexistent"})
    assert out.returncode == 64 and "not both" in out.stderr, out.stderr


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
