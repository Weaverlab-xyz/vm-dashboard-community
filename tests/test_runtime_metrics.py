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


def _samples(text):
    """Parse exposition text into {(name, frozenset(labels)): value}, checking its grammar."""
    import re
    out = {}
    for line in text.splitlines():
        if not line:
            continue
        if line.startswith("# HELP ") or line.startswith("# TYPE "):
            continue
        m = re.fullmatch(r'([a-zA-Z_:][a-zA-Z0-9_:]*)\{(.*)\} (-?[0-9.eE+-]+)', line)
        assert m, f"not a valid sample line: {line!r}"
        labels = frozenset(re.findall(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"', m.group(2)))
        out[(m.group(1), labels)] = float(m.group(3))
    return out


def _fake_snapshot():
    return {
        "pid": 42,
        "event_loop": {"last_ms": 12.5, "max_60s_ms": 300.0, "samples": 60,
                       "seconds_since_last_beat": 0.4},
        "threads": {"anyio_sync_routes": {"busy": 3, "size": 8},
                    "loop_default_executor": {"threads": 2, "size": 16, "queued": 0},
                    "cloud_providers": {"aws": {"busy": 1, "size": 4}, "gcp": {"error": "X"}}},
        "db_pool": {"pooled": True, "size": 5, "checked_out": 2, "overflow": 0,
                    "checked_in": 3, "max_overflow": 10, "timeout_s": 30},
        "jobs": {"queued": {"count": 0, "oldest_age_s": None},
                 "pending": {"count": 2, "oldest_age_s": 90},
                 "running": {"count": 1, "oldest_age_s": 10}},
    }


def test_prometheus_text_carries_the_snapshot_with_a_pid_label():
    text = rm.render_prometheus(_fake_snapshot())
    assert text.endswith("\n") and "\r" not in text
    s = _samples(text)
    P = ("pid", "42")
    assert s[("dashboard_event_loop_lag_max_60s_seconds", frozenset([P]))] == 0.3
    assert s[("dashboard_thread_pool_busy", frozenset([P, ("pool", "anyio_sync_routes")]))] == 3
    assert s[("dashboard_thread_pool_size", frozenset([P, ("pool", "cloud_aws")]))] == 4
    assert s[("dashboard_db_pool_checked_out", frozenset([P]))] == 2
    assert s[("dashboard_jobs", frozenset([P, ("status", "pending")]))] == 2
    assert s[("dashboard_jobs_oldest_age_seconds", frozenset([P, ("status", "pending")]))] == 90
    assert s[("dashboard_jobs_up", frozenset([P]))] == 1
    assert all(P in labels for _, labels in s), "every sample must say which process"


def test_prometheus_omits_what_is_not_there_and_flags_a_failed_jobs_read():
    snap = _fake_snapshot()
    snap["event_loop"] = {"monitor": "no samples yet"}
    snap["db_pool"] = {"pooled": False}
    snap["jobs"] = {"error": "no answer from the database in 3s"}
    s = _samples(rm.render_prometheus(snap))
    names = {n for n, _ in s}
    assert "dashboard_event_loop_lag_last_seconds" not in names
    assert not any(n.startswith("dashboard_db_pool") for n in names)
    assert "dashboard_jobs" not in names
    assert s[("dashboard_jobs_up", frozenset([("pid", "42")]))] == 0
    # An error section from cloud_providers must not become a sample.
    assert not any(("pool", "cloud_gcp") in labels for _, labels in s)


def test_prometheus_escapes_label_values():
    assert rm._esc('a"b\\c\nd') == 'a\\"b\\\\c\\nd'


def test_prometheus_from_a_real_snapshot_parses():
    _samples(rm.render_prometheus(_run(rm.snapshot())))


def test_the_route_serves_json_by_default_and_prometheus_on_request():
    src = open(os.path.join(ROOT, "web_dashboard", "main.py"), encoding="utf-8").read()
    at = src.index('@app.get("/api/metrics"')
    body = src[at:src.index('@app.get("/api/about"')]
    assert "Depends(auth.require_admin)" in body.split(":\n", 1)[0]
    assert 'format: str = "json"' in body, "JSON must stay the default"
    assert "render_prometheus" in body and "PROMETHEUS_CONTENT_TYPE" in body
    assert "status_code=400" in body, "an unknown format must be refused, not guessed"


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
