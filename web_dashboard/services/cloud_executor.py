"""Bounded, per-provider thread pools for blocking cloud SDK calls.

Answers one question: *which threads may a blocking cloud call occupy, and for how long*.
Stdlib + ``config_service`` only — no Session, no cloud SDK, no models — so it is
unit-testable by file path, mirroring ``worker_policy.py`` and ``expiry_policy.py``.

WHY THIS EXISTS
---------------
On 2026-08-12 dash.weaverlab.app was unreachable for 30 minutes, and nothing in the app
was broken. ``asyncio.to_thread`` hands work to the event loop's DEFAULT
ThreadPoolExecutor, sized ``min(32, os.cpu_count() + 4)`` — measured in the live
container: ``os.cpu_count()`` = 4 (the NODE; the cgroup quota was 1 CPU, which Python
never sees), so **8 threads for the whole process**. The dashboard home page fans out
~8 concurrent per-cloud reads (``/api/{gcp,oci,aws,azure}/dashboard-stats`` plus
``/api/containers/{rancher,portainer/node,gce-compose,gce-jumpoints,gce-cloud-run-jobs}``).
GCP and OCI went slow upstream for a few minutes; 7-8 of those calls parked all 8 threads;
and every later ``to_thread`` — including routes that touch no cloud at all — queued
behind them forever. Two gunicorn workers, one page load, whole site down. Requests that
needed nothing from any cloud died because a cloud they did not use was slow.

So the failure was never "GCP was slow". It was **one unbounded queue shared by every
provider**. This module replaces it with three properties:

  * **A pool per provider.** A wedged provider can exhaust its own threads and nothing
    else. AWS, Azure and the pure-DB routes keep running. This is the property that
    actually contains the blast radius, and the reason a single bigger pool is not the
    fix — a shared pool of any size is still a shared failure domain.
  * **Admission control instead of queueing.** When a provider's threads are all
    occupied the call is REFUSED immediately rather than queued. An unbounded queue is
    what turns a slow upstream into a permanent hang: the queue never drains, and every
    caller waits on work that is itself waiting. A fast, visible "GCP is saturated" beats
    a page that never answers.
  * **A deadline on every call.** The request gets an answer even when the SDK will not.

HONEST LIMIT, worth stating plainly: a timeout frees the *caller*, not the *thread*.
Python cannot interrupt a blocking C-level socket read, so a hung SDK call keeps its slot
until it returns on its own. That is exactly why occupancy is tracked on the underlying
``concurrent.futures.Future`` (see ``_on_done``) rather than on the asyncio wrapper —
after a timeout the wrapper is cancelled while the thread is still running, and counting
the wrapper would let us re-admit work into a pool that has no free threads and rebuild
the very queue this module exists to prevent. Reclaiming the thread itself needs a
deadline inside the SDK client; until every client carries one, the pool boundary is what
keeps one provider's bad day off the other three.
"""
import asyncio
import contextvars
import functools
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

logger = logging.getLogger(__name__)


# ── Un-configurable bounds ───────────────────────────────────────────────────
#
# Not "sensible maxima" — bounds on blast radius, as in worker_policy. Threads here are
# I/O-bound and cost little parked, but each also holds whatever the SDK call holds, and
# a pool per provider multiplies by the provider count.
POOL_SIZE_CEILING = 32
POOL_SIZE_FLOOR = 2

# A call may not be given an unbounded deadline: "no timeout" is the bug being fixed.
# The ceiling is above the longest known poller (a GCP image export runs to 7200s inside
# a single synchronous to_thread call — see jobs_worker._executor_size).
CALL_TIMEOUT_CEILING_S = 86400.0
CALL_TIMEOUT_FLOOR_S = 5.0

# Fallbacks used when config and settings both fail to produce a number.
_DEFAULT_POOL_SIZE = 8

# Request path: a dashboard tile is worth ~1s on a good day. 60s is far longer than any
# healthy read and far shorter than the 240s the ingress allowed while the site was down.
_DEFAULT_CALL_TIMEOUT_S = 60.0

# Job path: the worker is not serving a page, and its long pollers are the whole point of
# the light tier. Above the 7200s export so this bounds runaway work without capping
# legitimate work — see use_worker_defaults.
_DEFAULT_WORKER_CALL_TIMEOUT_S = 14400.0


class CloudCallError(Exception):
    """Base for "your call did not run", so a caller can catch one type.

    Deliberately NOT raised to routes as-is: each service translates it into that
    service's own error (GCPError, OCIError, ...) so the handlers that already turn those
    into a 503 or an unavailable tile keep working untouched.
    """


class CloudCallTimeout(CloudCallError):
    def __init__(self, provider: str, call: str, budget: float):
        self.provider, self.call, self.budget = provider, call, budget
        super().__init__(
            f"{provider} call {call!r} exceeded its {budget:g}s deadline")


class CloudProviderBusy(CloudCallError):
    def __init__(self, provider: str, busy: int, size: int):
        self.provider, self.busy, self.size = provider, busy, size
        super().__init__(
            f"{provider} is saturated ({busy}/{size} threads busy on calls that have not "
            f"returned) — refusing rather than queueing behind them")


# ── Process state ────────────────────────────────────────────────────────────
#
# One pool per provider, created on first use and NEVER resized: a ThreadPoolExecutor
# cannot be resized safely with work in flight, and pretending otherwise would mean
# admission control and the actual thread count disagreeing. A size change therefore
# needs a restart — the one place this module does not follow worker_policy's
# read-it-every-time rule, and the reason the size is captured in _sizes beside the pool.
_state_lock = threading.Lock()
_pools: dict = {}
_sizes: dict = {}
_busy: dict = {}

# Which defaults this PROCESS uses. The app and the worker run the same image and import
# the same services, but a 60s deadline that is right for a dashboard tile would kill a
# two-hour image export, so the process says which it is rather than the operator having
# to set an env var per container and get it wrong.
_timeout_key = "cloud_call_timeout_s"
_timeout_default = _DEFAULT_CALL_TIMEOUT_S


def use_worker_defaults() -> None:
    """Switch this process to the job-path deadline. Called once by ``jobs_worker``."""
    global _timeout_key, _timeout_default
    _timeout_key = "cloud_worker_call_timeout_s"
    _timeout_default = _DEFAULT_WORKER_CALL_TIMEOUT_S
    logger.info("cloud executor: worker deadlines (default %.0fs)", _timeout_default)


# ── Config access ────────────────────────────────────────────────────────────

def _cs():
    from . import config_service
    return config_service


def _num(key: str, default: float) -> float:
    """A cloud_* number, from config → settings → literal default. Mirrors
    ``worker_policy._int``; float because a deadline is not naturally an integer."""
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
        return float(raw)
    except (TypeError, ValueError):
        return default


def pool_size() -> int:
    """Threads per provider. Read once per provider, at pool creation."""
    return max(POOL_SIZE_FLOOR,
               min(POOL_SIZE_CEILING, int(_num("cloud_pool_size", _DEFAULT_POOL_SIZE))))


def call_timeout() -> float:
    """Default deadline for one blocking call in THIS process."""
    return max(CALL_TIMEOUT_FLOOR_S,
               min(CALL_TIMEOUT_CEILING_S, _num(_timeout_key, _timeout_default)))


# ── Occupancy ────────────────────────────────────────────────────────────────

def _release(provider: str) -> None:
    with _state_lock:
        _busy[provider] = max(0, _busy.get(provider, 0) - 1)


def _on_done(provider: str, future) -> None:
    """Runs on the WORKER thread the moment the call really finishes.

    Reading .exception() marks it retrieved: after a timeout nobody is waiting on this
    future any more, and an unretrieved exception would surface later as a bare
    "Future exception was never retrieved" with no route and no job attached to it.
    """
    try:
        future.exception()
    except BaseException:                              # noqa: BLE001 - incl. CancelledError
        pass
    _release(provider)


def _pool_for(provider: str):
    with _state_lock:
        pool = _pools.get(provider)
        if pool is None:
            size = pool_size()
            pool = ThreadPoolExecutor(max_workers=size,
                                      thread_name_prefix=f"cloud-{provider}")
            _pools[provider] = pool
            _sizes[provider] = size
            _busy.setdefault(provider, 0)
            logger.info("cloud executor: %s pool = %d threads", provider, size)
        return pool, _sizes[provider]


def stats() -> dict:
    """Per-provider ``(busy, size)``. For a health endpoint or a support dump — this is
    the number that was invisible during the outage."""
    with _state_lock:
        return {p: {"busy": _busy.get(p, 0), "size": _sizes[p]} for p in _sizes}


# ── The one entry point ──────────────────────────────────────────────────────

async def run(provider: str, fn, /, *args, deadline: Optional[float] = None, **kwargs):
    """Run blocking ``fn`` on ``provider``'s own pool, under a deadline.

    Raises ``CloudProviderBusy`` if that provider has no free thread, and
    ``CloudCallTimeout`` if the call outlives its budget. Anything ``fn`` itself raises
    propagates unchanged, so existing error handling is unaffected.

    The knob is ``deadline``, NOT ``timeout``, and that is deliberate: every ``_to_thread``
    shim forwards ``**kwargs`` straight through to ``fn``, and ``timeout`` is one of the
    commonest kwargs in a cloud SDK. Named ``timeout`` this signature would silently eat
    an argument meant for the SDK call and apply it as the deadline instead — a bug that
    would read as "the SDK ignored my timeout". Named ``deadline``, ``timeout=`` reaches
    ``fn`` exactly as written.
    """
    pool, size = _pool_for(provider)

    with _state_lock:
        busy = _busy.get(provider, 0)
        if busy >= size:
            # Refuse rather than queue. See the module docstring: the queue IS the bug.
            raise CloudProviderBusy(provider, busy, size)
        _busy[provider] = busy + 1

    # Submit under a copy of the caller's context. asyncio.to_thread — which every one
    # of these call sites was migrated FROM — copies contextvars into the thread; a bare
    # pool.submit starts the thread on a fresh context. Two things the services read are
    # context-local: the job correlation id that tags log lines, and
    # workload_credential_lease's provisioning() marker, which the credential lookup
    # resolves INSIDE the pool thread. Without this copy a provisioning job's SDK calls
    # are silently served the everyday (read-only) lease — observed live 2026-08-25 as
    # an ec2_deploy failing iam:PassRole with an explicit session-policy deny.
    ctx = contextvars.copy_context()
    try:
        future = pool.submit(ctx.run, fn, *args, **kwargs)
    except BaseException:
        # submit() never ran the work, so no done-callback will ever fire for it.
        _release(provider)
        raise
    future.add_done_callback(functools.partial(_on_done, provider))

    # An explicit budget is CODE and is trusted as written (bounded only by the ceiling):
    # a caller that knows its call is sub-second may say so. The floor exists to stop a
    # config typo making every call fail, so it applies to the configured default only.
    budget = call_timeout() if deadline is None else min(
        CALL_TIMEOUT_CEILING_S, max(0.001, float(deadline)))
    try:
        return await asyncio.wait_for(asyncio.wrap_future(future), budget)
    except asyncio.TimeoutError:
        name = getattr(fn, "__name__", repr(fn))
        logger.warning("cloud executor: %s %s exceeded %gs; thread stays busy until "
                       "the SDK returns", provider, name, budget)
        raise CloudCallTimeout(provider, name, budget) from None
