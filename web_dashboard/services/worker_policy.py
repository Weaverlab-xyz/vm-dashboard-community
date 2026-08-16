"""Pure policy for background job-worker concurrency.

Answers one question: *how many jobs of each kind may ``jobs_worker`` run at once*.
Stdlib + ``config_service`` only — no Session, no cloud SDK, no models — so it is
unit-testable by file path, mirroring ``expiry_policy.py`` and ``notify_policy.py``.

The supervisor that acts on these answers lives in ``jobs_worker._run_loop``; the tier
membership (which job_type is heavy/medium/light) lives there too, next to
``HANDLED_TYPES``, because that partition is only reviewable beside the dispatch table.
This module is only the *numbers*.

Why the numbers are configurable at all, rather than a constant: the worker used to run
exactly one job at a time and "run more" meant more ``docker-compose`` replicas. On a
PaaS host that lever costs a connection pool per replica, so the concurrency has to live
inside one process — and the right value depends entirely on the install. A 1-vCore
Burstable Postgres and a 4-vCore General Purpose one are not the same deployment, and
neither is a lab running one cluster a week versus an estate exporting images all day.
Every read goes through here rather than being captured at import, so a Settings change
takes effect on the next supervisor pass without restarting the worker — see the note on
``_int`` below for why that works across containers.

Two properties matter more than anything else here:

  * **The ceilings below cannot be raised by configuration.** They bound how wrong a
    misconfiguration can be, exactly as ``expiry_policy``'s floors do. A slider that can
    be set to 500 is not a feature.
  * **No cap can be configured to 0.** Zero would look like "pause the queue" and behave
    like a queue that silently never drains, with nothing failed and nothing logged.
    Pausing a tier is not what these knobs are for; cancelling a job is a job action.

The DB pool is deliberately NOT here. ``create_engine`` runs at import in
``database.py`` — before any connection exists — so the pool that connects to the
database cannot be sized from a value stored in that database. ``DB_POOL_SIZE`` /
``DB_MAX_OVERFLOW`` stay environment-only, and ``jobs_worker._limits`` clamps whatever
these accessors return down to what that pool can actually serve.
"""
import json
import logging
import socket
from datetime import datetime

logger = logging.getLogger(__name__)


# ── Un-configurable ceilings ─────────────────────────────────────────────────
#
# Not "sensible maxima" — bounds on blast radius. Each is a different resource:
#
#   heavy  — terraform apply / packer build / `docker run`, streamed line by line. EVERY
#            output line is a JobLog INSERT plus a commit, so two concurrent streams are
#            already enough to saturate a small Postgres's IOPS. Past 4 the database is
#            the bottleneck and raising this makes every job slower, not more of them.
#   medium — a short terraform in a tempdir, or kubectl/helm. These serialize on the
#            shared plugin cache flock (terraform.plugin_cache_lock) anyway, so extra
#            slots mostly buy queueing inside the lock.
#   light  — sleep-and-poll a cloud API. Genuinely cheap in CPU, but each still holds a
#            pooled DB connection and an executor thread for its whole run, and an image
#            export runs for up to two hours.
#   total  — the backstop. The real limit is almost always the pool clamp in
#            jobs_worker._limits; this exists so a nonsense value can't get that far.
HEAVY_CAP_CEILING = 4
MEDIUM_CAP_CEILING = 4
LIGHT_CAP_CEILING = 12
TOTAL_CAP_CEILING = 16

# Threads for the worker's default executor. The stdlib default is min(32, cpu+4), which
# on a container is computed from the HOST's CPU count (os.cpu_count() is not cgroup-aware
# in 3.12) — a non-deterministic ceiling. It has to be sized explicitly because several
# cloud pollers are synchronous `while True: time.sleep(n)` loops wrapped in to_thread and
# park a thread for their entire wait, not per call.
EXECUTOR_THREADS_CEILING = 128

# Nothing longer can be saved on shutdown anyway: the platform SIGKILLs after its
# termination grace period (Azure Container Apps defaults to 30s), so a drain budget
# above that is a promise the host will not keep.
DRAIN_TIMEOUT_CEILING_S = 300

# Fallbacks used when config and settings both fail to produce a number. Mirrors the
# defaults in config.py and api/setup.py's WorkerFeatureConfig; tests pin all three equal.
_DEFAULT_HEAVY = 2
_DEFAULT_MEDIUM = 1
_DEFAULT_LIGHT = 3
_DEFAULT_TOTAL = 3
_DEFAULT_EXECUTOR_THREADS = 0        # 0 = derive from the caps
_DEFAULT_DRAIN_TIMEOUT_S = 20

# Runtime state the worker writes for the Settings panel to read back. NOT a configurable
# key — see get_runtime_status. Deliberately absent from WorkerFeatureConfig, the same way
# resource_expiry_last_sweep and notify_last_scan_at are.
_RUNTIME_STATUS_KEY = "worker_runtime_status"


# ── Config access ────────────────────────────────────────────────────────────
#
# Every read goes through here rather than being captured at import, so a Settings change
# takes effect on the next supervisor pass without restarting the worker.
#
# This works ACROSS CONTAINERS, which is the whole point: the app writes app_config, and
# config_service._ensure_loaded drops any cache older than _CACHE_TTL_SECONDS (5s), so a
# worker in a different container — or a different Container App — sees the new value
# within ~5s without being told. notification_service.drain_loop already depends on
# exactly this for notify_flush_interval_s.

def _cs():
    from . import config_service
    return config_service


def _int(key: str, default: int) -> int:
    """A worker_* integer, from config → settings → literal default."""
    try:
        raw = _cs().get(key, "")
    except Exception:                                  # pragma: no cover - defensive
        raw = ""
    if raw in (None, ""):
        try:
            from ..config import settings
            raw = getattr(settings, key, default)
        except Exception:                              # pragma: no cover - defensive
            raw = default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


# ── The caps ─────────────────────────────────────────────────────────────────
#
# max(1, min(CEILING, ...)) on every one: the floor stops a "pause the queue" reading
# (see the module docstring), the ceiling stops a typo or an over-eager slider.

def heavy_cap() -> int:
    """Concurrent HEAVY jobs — terraform apply/destroy, packer build, ansible_local.
    A long local subprocess whose output is streamed, and persisted, line by line."""
    return max(1, min(HEAVY_CAP_CEILING, _int("worker_heavy_concurrency", _DEFAULT_HEAVY)))


def medium_cap() -> int:
    """Concurrent MEDIUM jobs — cloud SDK work that also shells out briefly: a PRA /
    Entitle / Password-Safe terraform in a tempdir, or kubectl+helm when the k8s runner
    is ``local`` (which is the default — see k8s_runner_service.mode)."""
    return max(1, min(MEDIUM_CAP_CEILING, _int("worker_medium_concurrency", _DEFAULT_MEDIUM)))


def light_cap() -> int:
    """Concurrent LIGHT jobs — start a cloud operation, then sleep and check its API.
    Image exports/promotes/copies, gateways, epml_sync, expiry_sweep. The tier this
    whole feature exists for: a two-hour export must not hold the only slot."""
    return max(1, min(LIGHT_CAP_CEILING, _int("worker_light_concurrency", _DEFAULT_LIGHT)))


def max_concurrency() -> int:
    """Aggregate ceiling across all tiers. Lower THIS (not one tier) to shrink the
    worker's footprint — it is what the DB pool and the thread pool are sized from."""
    return max(1, min(TOTAL_CAP_CEILING, _int("worker_max_concurrency", _DEFAULT_TOTAL)))


def executor_threads() -> int:
    """Explicit size for the worker's default ThreadPoolExecutor, or 0 to derive it from
    the caps (jobs_worker._executor_size). Not clamped to a floor: 0 is meaningful here."""
    return max(0, min(EXECUTOR_THREADS_CEILING,
                      _int("worker_executor_threads", _DEFAULT_EXECUTOR_THREADS)))


def drain_timeout_s() -> int:
    """Seconds to let in-flight jobs finish after SIGTERM before abandoning them to the
    next startup reconcile. 0 = don't wait. Keep it under the platform's termination
    grace period or SIGKILL lands first and the drain never completes."""
    return max(0, min(DRAIN_TIMEOUT_CEILING_S,
                      _int("worker_drain_timeout_s", _DEFAULT_DRAIN_TIMEOUT_S)))


def caps() -> dict:
    """All three tier caps plus the aggregate, in one read. Convenience for the
    supervisor, which needs them together on every pass."""
    return {"heavy": heavy_cap(), "medium": medium_cap(), "light": light_cap(),
            "total": max_concurrency()}


# ── Runtime status readout ───────────────────────────────────────────────────
#
# The worker publishes what it ACTUALLY resolved so the Settings panel can show it. This
# is not decoration: the caps above are clamped twice (here, and again to the DB pool in
# jobs_worker._limits), and a cap that is silently clamped is worse than one refused —
# "I set light to 10 and nothing changed" is otherwise only diagnosable from a container
# log most operators will never open.
#
# One JSON key, mirroring expiry_reaper's last-sweep readout, including its never-run and
# corrupted fallbacks: an operator reading "corrupted" learns more than one staring at an
# empty card.

def get_runtime_status() -> dict:
    """The worker's last published view of its own limits, for the Settings panel and
    ``GET /api/worker/status``. Returns ``{"never_run": True}`` before any worker has
    started (a fresh install, or one still on the old image)."""
    try:
        raw = _cs().get(_RUNTIME_STATUS_KEY, "") or ""
    except Exception:                                  # pragma: no cover - defensive
        return {"never_run": True}
    if not raw:
        return {"never_run": True}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"corrupted": True, "raw_preview": raw[:200]}


def publish_runtime_status(*, configured: dict, effective: dict, clamp_reason: str = "",
                           executor_threads_used: int = 0, pool_capacity: int = 0,
                           in_flight: int = 0) -> None:
    """Best-effort save of the worker's resolved limits.

    A failure here is logged, not raised — the worker's job is to run jobs, and losing a
    status readout must never take it down (same contract as
    ``expiry_reaper._persist_result``). ``hostname`` is included so an install running
    more than one worker replica can tell the readouts apart rather than seeing one
    overwrite the other.
    """
    payload = {
        "configured": configured,
        "effective": effective,
        "clamp_reason": clamp_reason,
        "executor_threads": executor_threads_used,
        "pool_capacity": pool_capacity,
        "in_flight": in_flight,
        "hostname": socket.gethostname(),
        "published_at": datetime.utcnow().isoformat() + "Z",
    }
    try:
        _cs().set(_RUNTIME_STATUS_KEY, json.dumps(payload, sort_keys=True, default=str))
    except Exception:
        logger.exception("failed to persist %s", _RUNTIME_STATUS_KEY)
