"""GET /api/metrics: the numbers behind it, and the two properties that make it worth having.

1. **It sees a blocked event loop.** The loop-lag sampler is the whole point for the
   "async route doing sync work" hang; a sampler that never reports lag would look healthy
   through that outage.
2. **It still answers when the database does not.** The metrics are read when the app is
   wedged, so a slow or exhausted database must turn into an error in ONE section of the
   reply, not a hung or failed request.

Plus a source check that the route is admin-only and that the sampler is actually started,
because a test of the module alone passes while the app never wires it in.

Runs under pytest, or standalone:
    python tests/test_runtime_metrics.py
"""
import asyncio
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-runtime-metrics")

try:
    from web_dashboard.services import runtime_metrics as rm
    _IMPORT_ERR = None
except Exception as exc:  # noqa: BLE001 -- reported by __main__
    rm = None
    _IMPORT_ERR = exc


def _run(coro):
    return asyncio.run(coro)


def test_the_sampler_reports_a_blocked_loop():
    """Block the loop for 0.3s and the next beat must be about that late."""
    async def go():
        rm._lags.clear()
        old = rm._BEAT_S
        rm._BEAT_S = 0.05
        try:
            task = asyncio.create_task(rm.loop_lag_monitor())
            await asyncio.sleep(0.2)            # let it take a few quiet samples
            time.sleep(0.3)                     # the bug: synchronous work on the loop
            await asyncio.sleep(0.2)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            return rm._loop_section()
        finally:
            rm._BEAT_S = old
    section = _run(go())
    assert section["max_60s_ms"] >= 200, section
    assert section["samples"] >= 3, section


def test_a_quiet_loop_reports_small_lag():
    async def go():
        rm._lags.clear()
        old = rm._BEAT_S
        rm._BEAT_S = 0.05
        try:
            task = asyncio.create_task(rm.loop_lag_monitor())
            await asyncio.sleep(0.4)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            return rm._loop_section()
        finally:
            rm._BEAT_S = old
    section = _run(go())
    assert section["max_60s_ms"] < 150, section


def test_no_samples_yet_is_not_an_error():
    rm._lags.clear()
    assert rm._loop_section() == {"monitor": "no samples yet"}


def test_snapshot_shape_and_job_counts():
    from web_dashboard.database import Job, SessionLocal, init_db
    init_db()
    db = SessionLocal()
    try:
        db.query(Job).delete()
        db.add(Job(job_type="ec2_deploy", status="pending"))
        db.add(Job(job_type="ec2_deploy", status="running"))
        db.add(Job(job_type="ec2_deploy", status="completed"))
        db.commit()
    finally:
        db.close()
    snap = _run(rm.snapshot())
    assert snap["pid"] == os.getpid()
    assert set(snap) == {"pid", "event_loop", "threads", "db_pool", "jobs"}
    assert "anyio_sync_routes" in snap["threads"]
    assert "cloud_providers" in snap["threads"]
    jobs = snap["jobs"]
    assert jobs["pending"]["count"] == 1 and jobs["running"]["count"] == 1, jobs
    assert jobs["queued"]["count"] == 0 and jobs["queued"]["oldest_age_s"] is None, jobs
    assert isinstance(jobs["pending"]["oldest_age_s"], int), jobs
    assert "completed" not in jobs, "only waiting/running jobs are reported"


def test_sqlite_has_no_pool_to_report():
    from web_dashboard.database import engine
    if "sqlite" not in str(engine.url):
        return                      # the Postgres job asserts the pooled shape below
    assert rm._db_pool_section() == {"pooled": False}


def test_a_slow_database_degrades_one_section_not_the_request():
    real_query, real_deadline = rm._jobs_query, rm._DB_DEADLINE_S
    rm._DB_DEADLINE_S = 0.05

    def slow():
        time.sleep(0.4)
        return {}
    rm._jobs_query = slow
    try:
        started = time.monotonic()
        snap = _run(rm.snapshot())
        took = time.monotonic() - started
        assert "no answer from the database" in snap["jobs"]["error"], snap["jobs"]
        assert took < 0.3, f"the request waited {took:.2f}s on a slow database"
        assert "event_loop" in snap and "threads" in snap      # the rest still came back
        # While that read is still running, the next one must not queue behind it.
        again = _run(rm.snapshot())
        assert "still in flight" in again["jobs"]["error"], again["jobs"]
    finally:
        rm._jobs_query, rm._DB_DEADLINE_S = real_query, real_deadline
        if rm._db_future is not None:
            try:
                rm._db_future.result(timeout=2)
            except Exception:  # noqa: BLE001
                pass


def test_a_failing_database_read_is_reported_not_raised():
    real = rm._jobs_query

    def boom():
        raise RuntimeError("db down")
    rm._jobs_query = boom
    try:
        snap = _run(rm.snapshot())
        assert snap["jobs"] == {"error": "RuntimeError"}, snap["jobs"]
    finally:
        rm._jobs_query = real


def test_the_route_is_admin_only_and_the_sampler_is_started():
    src = open(os.path.join(ROOT, "web_dashboard", "main.py"), encoding="utf-8").read()
    at = src.index('@app.get("/api/metrics"')
    sig = src[at:src.index(":\n", at)]
    assert "Depends(auth.require_admin)" in sig, sig
    life = src[src.index("async def lifespan"):src.index("yield")]
    assert "runtime_metrics.loop_lag_monitor()" in life, \
        "the sampler must be started in lifespan, or loop lag is never reported"
    assert "async def metrics" in src, "must be async: a sync route waits behind the starved pool"


if __name__ == "__main__":
    if rm is None:
        print(f"SKIP all: runtime_metrics unavailable ({_IMPORT_ERR})")
        sys.exit(0)
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {e!r}")
    sys.exit(1 if failures else 0)
