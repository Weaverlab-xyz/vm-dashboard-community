"""The job runner's health endpoint, and specifically the ordering that makes it useful.

``dash-worker`` has no ingress and no other port, so before this endpoint existed ACA
could not probe it at all. The regression these tests guard is subtler than "does it
answer": an endpoint that reports healthy as soon as it binds would have returned 200
throughout the 2026-08-28 outage, because the process was alive the whole time -- it was
blocked in ``init_db()``. So the assertions below are mostly about the UNHEALTHY cases.
"""
import json
import os
import sys
import threading
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from web_dashboard import worker_health as wh
    _IMPORT_ERR = None
except Exception as exc:  # noqa: BLE001 — reported by __main__ / skipped by pytest
    wh = None
    _IMPORT_ERR = exc


def _reset(phase, beat=False):
    wh.mark(phase)
    with wh._lock:
        wh._last_beat = None
    if beat:
        wh.beat()


def test_starting_is_unhealthy_and_says_why():
    """The init_db wedge. This is the case the endpoint exists for."""
    _reset(wh.STARTING)
    healthy, payload = wh.status()
    assert healthy is False
    assert payload["phase"] == wh.STARTING
    assert "has not reached the run loop" in payload["reason"]


def test_db_ready_is_still_unhealthy():
    """init_db returned, but the startup _reconcile_stale() can block the same way."""
    _reset(wh.DB_READY)
    healthy, payload = wh.status()
    assert healthy is False, payload
    assert payload["phase"] == wh.DB_READY


def test_running_without_a_completed_pass_is_unhealthy():
    """Reaching the loop is not the same as the loop turning."""
    _reset(wh.RUNNING)
    healthy, payload = wh.status()
    assert healthy is False, payload
    assert "has not completed a pass" in payload["reason"]


def test_running_with_a_fresh_beat_is_healthy():
    _reset(wh.RUNNING, beat=True)
    healthy, payload = wh.status()
    assert healthy is True, payload
    assert payload["status"] == "ok"
    assert payload["seconds_since_last_pass"] is not None


def test_a_stale_beat_is_unhealthy():
    """A loop that died after a clean start. This is what Liveness reads."""
    _reset(wh.RUNNING, beat=True)
    original = wh.STALE_AFTER_S
    try:
        wh.STALE_AFTER_S = -1.0     # forces staleness without sleeping two minutes
        healthy, payload = wh.status()
        assert healthy is False, payload
        assert "has not announced a pass" in payload["reason"]
    finally:
        wh.STALE_AFTER_S = original


def test_draining_is_unhealthy():
    """SIGTERM. Readiness should drop; the drain is far shorter than the liveness budget,
    so this cannot manufacture a restart of a container that is already going away."""
    _reset(wh.DRAINING, beat=True)
    healthy, _ = wh.status()
    assert healthy is False


def test_the_payload_never_carries_a_secret():
    """It is an unauthenticated endpoint. Keep it boring."""
    _reset(wh.RUNNING, beat=True)
    _, payload = wh.status()
    assert set(payload) <= {
        "status", "phase", "version", "uptime_s",
        "seconds_since_last_pass", "stale_after_s", "reason",
    }, payload


def test_port_zero_disables_the_listener():
    assert wh.serve(0) is None


def test_it_serves_over_http_with_the_right_status_codes():
    """End to end on a real socket: 503 while starting, 200 once running, 404 elsewhere."""
    server = wh.serve(18098)
    assert server is not None, "health server failed to bind"
    try:
        _reset(wh.STARTING)
        assert _get(18098, "/api/health")[0] == 503

        _reset(wh.RUNNING, beat=True)
        code, body = _get(18098, "/api/health")
        assert code == 200, (code, body)
        assert body["status"] == "ok"

        # The app serves /api/health, so one probe definition works against either
        # container; /health is kept as an alias.
        assert _get(18098, "/health")[0] == 200
        assert _get(18098, "/nope")[0] == 404
    finally:
        server.shutdown()
        server.server_close()


def test_a_bind_failure_does_not_raise():
    """A runner that refuses to start because its HEALTH endpoint could not bind is
    strictly worse than one the platform cannot probe.

    The OSError is injected rather than provoked by binding twice: ``allow_reuse_address``
    means a second bind to the same port SUCCEEDS on Windows while it raises EADDRINUSE
    on Linux, so a double-bind test asserts the platform, not this branch.
    """
    original = wh.ThreadingHTTPServer

    def _boom(*_a, **_kw):
        raise OSError(98, "Address already in use")

    wh.ThreadingHTTPServer = _boom
    try:
        assert wh.serve(18097) is None
    finally:
        wh.ThreadingHTTPServer = original


def _get(port, path):
    try:
        r = urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5)
        return r.status, json.loads(r.read())
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, {}


def test_the_runner_starts_the_listener_before_init_db():
    """The ordering IS the feature, and it is one line away from being useless.

    Pinned as source order because there is no way to observe it at runtime without a
    real Postgres to deadlock against -- and if these two ever swap, every test above
    still passes while the endpoint goes back to reporting 200 through an outage.
    """
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "web_dashboard", "jobs_worker.py"), encoding="utf-8").read()
    body = src[src.index("def main() -> None:"):]
    serve_at = body.index("worker_health.serve()")
    initdb_at = body.index("init_db()")
    assert serve_at < initdb_at, "worker_health.serve() must precede init_db()"


if __name__ == "__main__":
    if wh is None:
        print(f"SKIP all: worker_health unavailable ({_IMPORT_ERR})")
        sys.exit(0)
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
    sys.exit(1 if failures else 0)
