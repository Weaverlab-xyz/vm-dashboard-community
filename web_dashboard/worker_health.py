"""A health endpoint for the job runner, which otherwise has no port to probe.

``dash-worker`` runs ``python -m web_dashboard.jobs_worker`` with no HTTP server, so on
Azure Container Apps it had no ``ingress`` and therefore nothing a probe could target --
ACA probes need an ``httpGet`` or ``tcpSocket`` port and offer no exec probe. The result
was a container the platform could not check at all: on 2026-08-28 the runner deadlocked
with the app inside ``init_db()`` and sat at 0% CPU for ~50 minutes while ACA reported it
`Healthy` with ``restartCount: 0``, because with no probe there is nothing to fail.

**The ordering is the whole design.** ``jobs_worker.main`` starts this server BEFORE it
calls ``init_db()``, and this module reports unhealthy until the run loop is actually
turning. A listener started after startup, or one that answers 200 as soon as it binds,
would have returned 200 straight through that outage and bought nothing: the process was
alive the entire time. What was not true is that it was *working*.

So there are two independent failure signals here, and they catch different things:

* **Phase** -- 503 until the startup sequence reaches the run loop. This is what catches
  a wedge in ``init_db()`` or in the startup ``_reconcile_stale()``, and it is what a
  Startup probe reads. It needs no heartbeat and no timing guess.
* **Heartbeat staleness** -- 503 once the loop has stopped announcing passes. This is
  what catches a loop that dies or blocks *after* a clean start, and it is what a
  Liveness probe reads.

Deliberately stdlib-only and importing nothing from ``web_dashboard`` except
``config.settings``: the runner must not import ``main`` (that would build the whole
FastAPI app in a process that serves no requests -- the same rule
``services/feature_flags`` exists for), and a health check that can fail on an import is
not a health check.

The socket is not published anywhere. The ACA app has no ingress and ``docker-compose``
gives the worker no ``ports:`` mapping, so this is reachable from the pod/host network
only -- which is where probes come from. It exposes a phase name, two timestamps and the
API version; nothing authenticated and nothing secret.
"""
import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .config import settings

logger = logging.getLogger(__name__)

# Phases, in order. Only RUNNING is healthy.
STARTING = "starting"      # process up; init_db not finished
DB_READY = "db_ready"      # init_db returned; loop not yet turning
RUNNING = "running"         # the run loop is claiming work
DRAINING = "draining"       # SIGTERM received; finishing in-flight jobs

# How long the loop may go without announcing a pass before we call it stalled. The idle
# loop beats every POLL_INTERVAL (2s); a pass that hits the periodic sweep also does a
# reconcile, a credential release and a lease refresh off-thread, and RECONCILE_INTERVAL
# is 60s. 120s is two of those, chosen so a slow sweep on a large jobs table can never
# manufacture a restart -- a false restart during a 30-minute terraform apply costs far
# more than detecting a genuine stall a minute later.
STALE_AFTER_S = 120.0

_lock = threading.Lock()
_phase = STARTING
_started_at = time.time()
_last_beat: float | None = None
_server: ThreadingHTTPServer | None = None


def mark(phase: str) -> None:
    """Record a startup/shutdown phase transition."""
    global _phase
    with _lock:
        _phase = phase


def beat() -> None:
    """Called once per run-loop pass. Cheap on purpose: this is the hot path."""
    global _last_beat
    with _lock:
        _last_beat = time.monotonic()


def status() -> tuple[bool, dict]:
    """``(healthy, payload)``. Split out from the handler so tests need no socket."""
    with _lock:
        phase, last_beat, started_at = _phase, _last_beat, _started_at

    stale_for = None if last_beat is None else round(time.monotonic() - last_beat, 1)
    healthy = phase == RUNNING and stale_for is not None and stale_for <= STALE_AFTER_S

    payload = {
        "status": "ok" if healthy else "unhealthy",
        "phase": phase,
        "version": settings.api_version,
        "uptime_s": round(time.time() - started_at, 1),
        "seconds_since_last_pass": stale_for,
        "stale_after_s": STALE_AFTER_S,
    }
    if not healthy:
        # Say WHY, so a failing probe is self-describing in the replica listing rather
        # than sending whoever is paged back to the logs -- which is the one thing a
        # corp TLS proxy can take away (see docs/cloud-hosting.md).
        if phase != RUNNING:
            payload["reason"] = f"startup has not reached the run loop (phase={phase})"
        elif stale_for is None:
            payload["reason"] = "the run loop has not completed a pass yet"
        else:
            payload["reason"] = (f"the run loop has not announced a pass for "
                                 f"{stale_for}s (limit {STALE_AFTER_S}s)")
    return healthy, payload


class _Handler(BaseHTTPRequestHandler):
    # Match the app's path so one probe definition works against either container.
    _PATHS = ("/api/health", "/health")

    def do_GET(self) -> None:  # noqa: N802 -- BaseHTTPRequestHandler's name
        if self.path.split("?")[0] not in self._PATHS:
            self.send_error(404, "Not found")
            return
        healthy, payload = status()
        body = json.dumps(payload).encode()
        self.send_response(200 if healthy else 503)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        # BaseHTTPRequestHandler logs every request to stderr. A 10s readiness probe
        # would put ~8600 lines a day in the runner's log for no information.
        logger.debug("worker health: " + fmt, *args)


def serve(port: int | None = None) -> ThreadingHTTPServer | None:
    """Start the health server on a daemon thread. ``port=0`` disables it.

    Never raises: a runner that will not start because its *health* endpoint could not
    bind is strictly worse than one the platform cannot probe.
    """
    global _server
    port = settings.worker_health_port if port is None else port
    if not port:
        logger.info("job runner: health endpoint disabled (worker_health_port=0)")
        return None
    try:
        server = ThreadingHTTPServer(("0.0.0.0", port), _Handler)
    except OSError as exc:
        logger.warning("job runner: could not bind health endpoint on :%s (%s); "
                       "the runner will start but cannot be probed", port, exc)
        return None
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="worker-health",
                     daemon=True).start()
    _server = server
    logger.info("job runner: health endpoint on :%d/api/health", port)
    return server
