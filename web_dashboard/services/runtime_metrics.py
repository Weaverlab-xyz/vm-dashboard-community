"""Runtime health numbers for ``GET /api/metrics``: the four that were invisible.

Every hang recorded in this project was diagnosed by exec-ing into a container, because
nothing exported the thing that was saturated:

* an ``async def`` route doing synchronous database work blocked the **event loop**;
* the pool behind sync routes and ``to_thread`` was **exhausted** (the 8-thread wedge);
* the SQLAlchemy **connection pool** ran dry under concurrent jobs;
* the **job queue** stopped draining, with nothing saying how long the oldest job had waited.

So this reports: event-loop lag, the thread pools (anyio's, the loop's default executor, and
each cloud provider's), the DB pool, and queued/running job counts with the age of the
oldest waiting one.

**Per process, deliberately.** ``gunicorn -w 2`` runs two copies of everything here, and a
request is answered by one of them. Loop lag, thread pools and the DB pool are therefore the
answering process's own, and the reply says which (``pid``); poll more than once to see
both. Job counts come from the database and are the same in every process. The job worker is
a separate process and is not covered here (it has its own ``worker_health`` endpoint).

**It must still answer when the app is wedged**, because that is when it is read. So the loop
and thread numbers are read with no I/O and no thread hop, and the one part that needs the
database runs on its OWN single-thread executor under a short deadline: if the shared pools
are starved or the DB pool is exhausted, that section reports an error and the rest of the
reply is still returned. Stdlib only, no new dependency.
"""
import asyncio
import collections
import concurrent.futures
import logging
import os
import threading
import time
from datetime import timezone
from typing import Optional

logger = logging.getLogger(__name__)

# A beat of 1s, and the last minute of beats kept, so "max over 60s" is real and a single
# long block is still visible a minute after it happened.
_BEAT_S = 1.0
_WINDOW = 60

_lock = threading.Lock()
_lags: collections.deque = collections.deque(maxlen=_WINDOW)   # seconds of overshoot
_last_beat_at: Optional[float] = None

_DB_DEADLINE_S = 3.0
_db_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="metrics-db")
_db_future: Optional[concurrent.futures.Future] = None
_db_lock = threading.Lock()


async def loop_lag_monitor() -> None:
    """Sleep ``_BEAT_S`` repeatedly and record how late each wake-up was.

    A blocked event loop cannot run this task, so a block of N seconds shows up as the next
    sample being about N seconds late. Never raises: observability must not be what takes
    the app down.
    """
    global _last_beat_at
    loop = asyncio.get_running_loop()
    while True:
        try:
            start = loop.time()
            await asyncio.sleep(_BEAT_S)
            lag = max(0.0, loop.time() - start - _BEAT_S)
            with _lock:
                _lags.append(lag)
                _last_beat_at = time.monotonic()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.debug("loop lag monitor sample failed", exc_info=True)


def _loop_section() -> dict:
    with _lock:
        samples = list(_lags)
        since = None if _last_beat_at is None else round(time.monotonic() - _last_beat_at, 3)
    if not samples:
        return {"monitor": "no samples yet"}
    return {
        "last_ms": round(samples[-1] * 1000, 1),
        "max_60s_ms": round(max(samples) * 1000, 1),
        "samples": len(samples),
        # Seconds since the monitor last got to run. Far above the beat means the loop is
        # blocked right now, which no recorded sample can say (a block ends before it is
        # recorded).
        "seconds_since_last_beat": since,
    }


def _threads_section() -> dict:
    out: dict = {}
    try:
        import anyio.to_thread
        lim = anyio.to_thread.current_default_thread_limiter()
        # Starlette runs every sync (`def`) route here.
        out["anyio_sync_routes"] = {"busy": int(lim.borrowed_tokens), "size": int(lim.total_tokens)}
    except Exception as exc:  # noqa: BLE001
        out["anyio_sync_routes"] = {"error": type(exc).__name__}
    try:
        ex = getattr(asyncio.get_running_loop(), "_default_executor", None)
        if ex is None:
            out["loop_default_executor"] = {"state": "not created yet"}
        else:   # private attributes: tolerate their absence in a future Python
            queue = getattr(ex, "_work_queue", None)
            out["loop_default_executor"] = {
                "threads": len(getattr(ex, "_threads", ())),
                "size": getattr(ex, "_max_workers", None),
                "queued": queue.qsize() if queue is not None else None,
            }
    except Exception as exc:  # noqa: BLE001
        out["loop_default_executor"] = {"error": type(exc).__name__}
    try:
        from . import cloud_executor
        out["cloud_providers"] = cloud_executor.stats()
    except Exception as exc:  # noqa: BLE001
        out["cloud_providers"] = {"error": type(exc).__name__}
    return out


def _db_pool_section() -> dict:
    try:
        from ..database import engine
        pool = engine.pool
        if not hasattr(pool, "checkedout"):      # NullPool (SQLite) pools nothing
            return {"pooled": False}
        return {"pooled": True, "size": pool.size(), "checked_out": pool.checkedout(),
                "overflow": pool.overflow(), "checked_in": pool.checkedin(),
                "max_overflow": getattr(pool, "_max_overflow", None),
                "timeout_s": getattr(pool, "_timeout", None)}
    except Exception as exc:  # noqa: BLE001
        return {"error": type(exc).__name__}


def _jobs_query() -> dict:
    """One grouped, indexed read: status -> (count, oldest created_at)."""
    from sqlalchemy import func
    from ..database import Job, SessionLocal
    from .job_service import ACTIVE_STATUSES
    db = SessionLocal()
    try:
        rows = (db.query(Job.status, func.count(Job.id), func.min(Job.created_at))
                .filter(Job.status.in_(ACTIVE_STATUSES)).group_by(Job.status).all())
    finally:
        db.close()
    now = time.time()
    out = {s: {"count": 0, "oldest_age_s": None} for s in ACTIVE_STATUSES}
    for status, n, oldest in rows:
        age = None
        if oldest is not None:
            # created_at is naive UTC.
            age = max(0, int(now - oldest.replace(tzinfo=timezone.utc).timestamp()))
        out[status] = {"count": int(n), "oldest_age_s": age}
    return out


async def _jobs_section() -> dict:
    """The jobs read on this module's own thread, under a deadline. Never raises."""
    global _db_future
    with _db_lock:
        if _db_future is not None and not _db_future.done():
            # The last read has not come back (DB pool exhausted, or the DB is slow).
            return {"error": "previous read still in flight: the database is not answering"}
        _db_future = _db_executor.submit(_jobs_query)
        fut = _db_future
    try:
        return await asyncio.wait_for(asyncio.wrap_future(fut), timeout=_DB_DEADLINE_S)
    except asyncio.TimeoutError:
        return {"error": f"no answer from the database in {_DB_DEADLINE_S:g}s"}
    except Exception as exc:  # noqa: BLE001
        return {"error": type(exc).__name__}


async def snapshot() -> dict:
    return {
        "pid": os.getpid(),
        "event_loop": _loop_section(),
        "threads": _threads_section(),
        "db_pool": _db_pool_section(),
        "jobs": await _jobs_section(),
    }


# ── Prometheus text exposition ───────────────────────────────────────────────

PROMETHEUS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


def _esc(value) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def render_prometheus(snap: dict) -> str:
    """The same snapshot as Prometheus exposition text (format 0.0.4), hand-rolled.

    No ``prometheus_client``: it would be a new dependency for a dozen gauges, and its
    default multiprocess story does not fit ``gunicorn -w 2`` anyway. Every sample carries
    a ``pid`` label because the values are the answering process's own; scrape twice (or
    from both) to see both workers. Sections that errored or are not applicable are simply
    absent, except ``dashboard_jobs_up`` which says whether the database read worked, so an
    alert can tell "no queue" from "could not read the queue".
    """
    pid = snap.get("pid")
    families: dict = {}      # name -> (help, [(labels, value)])

    def add(name: str, help_text: str, value, **labels) -> None:
        if value is None or isinstance(value, (str, dict, list)):
            return
        fam = families.setdefault(name, (help_text, []))
        fam[1].append(({"pid": pid, **labels}, value))

    loop = snap.get("event_loop") or {}
    add("dashboard_event_loop_lag_last_seconds", "Lateness of the most recent loop-lag sample.",
        None if "last_ms" not in loop else loop["last_ms"] / 1000)
    add("dashboard_event_loop_lag_max_60s_seconds", "Worst loop-lag sample in the last 60 samples.",
        None if "max_60s_ms" not in loop else loop["max_60s_ms"] / 1000)
    add("dashboard_event_loop_seconds_since_last_beat",
        "Seconds since the lag sampler last ran; far above 1 means the loop is blocked now.",
        loop.get("seconds_since_last_beat"))

    threads = snap.get("threads") or {}
    anyio_pool = threads.get("anyio_sync_routes") or {}
    add("dashboard_thread_pool_busy", "Threads in use.", anyio_pool.get("busy"), pool="anyio_sync_routes")
    add("dashboard_thread_pool_size", "Thread pool capacity.", anyio_pool.get("size"), pool="anyio_sync_routes")
    ex = threads.get("loop_default_executor") or {}
    add("dashboard_thread_pool_busy", "Threads in use.", ex.get("threads"), pool="loop_default_executor")
    add("dashboard_thread_pool_size", "Thread pool capacity.", ex.get("size"), pool="loop_default_executor")
    add("dashboard_thread_pool_queued", "Work waiting for a thread.", ex.get("queued"), pool="loop_default_executor")
    for provider, st in sorted((threads.get("cloud_providers") or {}).items()):
        if isinstance(st, dict):
            add("dashboard_thread_pool_busy", "Threads in use.", st.get("busy"), pool=f"cloud_{provider}")
            add("dashboard_thread_pool_size", "Thread pool capacity.", st.get("size"), pool=f"cloud_{provider}")

    db = snap.get("db_pool") or {}
    if db.get("pooled"):
        for key in ("size", "checked_out", "checked_in", "overflow", "max_overflow"):
            add(f"dashboard_db_pool_{key}", f"SQLAlchemy pool {key.replace('_', ' ')}.", db.get(key))

    jobs = snap.get("jobs") or {}
    add("dashboard_jobs_up", "1 if the job counts were read from the database, else 0.",
        0 if "error" in jobs else 1)
    for status, st in sorted(jobs.items()):
        if isinstance(st, dict) and "count" in st:
            add("dashboard_jobs", "Jobs by status.", st["count"], status=status)
            add("dashboard_jobs_oldest_age_seconds", "Age of the oldest job in this status.",
                st.get("oldest_age_s"), status=status)

    lines = []
    for name, (help_text, samples) in families.items():
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} gauge")
        for labels, value in samples:
            lab = ",".join(f'{k}="{_esc(v)}"' for k, v in labels.items() if v is not None)
            lines.append(f"{name}{{{lab}}} {float(value):g}" if isinstance(value, float)
                         else f"{name}{{{lab}}} {int(value)}")
    return "\n".join(lines) + "\n"
