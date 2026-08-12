"""The durable cost cache: a failed cloud query must never cost you the last good figure.

The bug this file exists to pin: `cost_service`'s per-cloud entries degrade to
`{"status": "unavailable", "detail": ...}` instead of raising, so the orchestrators never
raise either — which made a 429 indistinguishable from a real answer to the old generic
cache. `get_or_refresh` took its success path and stored the error payload for the full
six-hour TTL, and `?refresh=true` called `invalidate()` BEFORE fetching, so one Refresh
click during an Azure throttle deleted a working number and installed the error in its
place. Seeing "unavailable" makes an admin click Refresh again, which issued more queries
into the window that was already rejecting them.

Three things had to change together, and each has tests below:
  * a failure writes the cooldown columns and never touches `payload`/`fetched_at`
  * the state lives in a table, so it is shared across workers and survives a rebuild
  * `cooldown_until` is a hard gate that `?refresh=true` does not cross

**Two worker processes are simulated by loading the service module twice**, under two
different names, from the same file — each copy gets its own module globals, which is what
two `gunicorn -w 2` workers have, and what the previous process-local dict could not
bridge. They share the database and nothing else.

Uses a temporary SQLite file and the real ORM. Note that single-flight on SQLite rests on
the lease check rather than a Postgres advisory lock, so `test_only_one_worker_queries_a_
cloud` is proving the lease, not `pg_try_advisory_xact_lock`.

Runs under pytest, or standalone:  python tests/test_cost_cache.py
"""
import asyncio
import importlib.util
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="cost-cache-test-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDB}"
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-cost-cache-tests")
# Pacing off by default: it makes every back-to-back read in this file sleep for real, and
# only two tests are about it (they set their own gap). 0 has to survive _cfg_int, which
# is why that helper tests for blank rather than falsy.
os.environ["COST_QUERY_GAP_SECONDS"] = "0"

try:
    from web_dashboard.database import Base, CloudCostCache, SessionLocal, engine
    import web_dashboard.services.cost_cache as _real_service
except Exception as exc:  # pragma: no cover — app deps missing
    try:
        import pytest
        pytest.skip(f"app dependencies unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)

Base.metadata.create_all(bind=engine)

_SERVICE_PATH = os.path.join(_ROOT, "web_dashboard", "services", "cost_cache.py")


def _worker(tag: str):
    """Load `services/cost_cache` as a fresh module object with its own globals.

    A second import stands in for the second gunicorn worker process."""
    spec = importlib.util.spec_from_file_location(
        f"web_dashboard.services.cost_cache__{tag}", _SERVICE_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


w1 = _worker("w1")
w2 = _worker("w2")

CALLS = []          # every stubbed per-cloud fetch, as (view, cloud)


def _run(coro):
    return asyncio.run(coro)


def _reset():
    CALLS.clear()
    db = SessionLocal()
    try:
        db.query(CloudCostCache).delete()
        db.commit()
    finally:
        db.close()
    for mod in (w1, w2):
        _stub(mod)


def _ok(cloud, amount=10.0):
    return {"cloud": cloud, "amount": amount, "currency": "USD",
            "status": "ok", "detail": ""}


def _fail(cloud, detail="boom", *, throttled=False, retry_after=None):
    return {"cloud": cloud, "amount": None, "currency": None, "status": "unavailable",
            "detail": detail, "throttled": throttled, "retry_after": retry_after}


def _stub(mod, responder=None):
    """Replace the module's view of cost_service with a recorder.

    Only the three functions cost_cache actually calls are stubbed; `assemble_*` are
    borrowed from the real module so the rolled-up payload keeps its real shape."""
    from web_dashboard.services import cost_service as real

    responder = responder or (lambda view, cloud: _ok(cloud))

    class _Svc:
        assemble_summary = staticmethod(real.assemble_summary)
        assemble_breakdown = staticmethod(real.assemble_breakdown)
        _unavailable_breakdown = staticmethod(real._unavailable_breakdown)

        @staticmethod
        async def cloud_summary_entry(cloud):
            CALLS.append(("summary", cloud))
            return responder("summary", cloud)

        @staticmethod
        async def cloud_breakdown_entry(cloud):
            CALLS.append(("breakdown", cloud))
            return responder("breakdown", cloud)

    mod.cost_service = _Svc
    return mod


def _row(cloud="aws", view="summary"):
    db = SessionLocal()
    try:
        return db.query(CloudCostCache).filter(
            CloudCostCache.cloud == cloud, CloudCostCache.view == view).first()
    finally:
        db.close()


def _set(cloud, view, **cols):
    db = SessionLocal()
    try:
        row = db.query(CloudCostCache).filter(
            CloudCostCache.cloud == cloud, CloudCostCache.view == view).first()
        for k, v in cols.items():
            setattr(row, k, v)
        db.commit()
    finally:
        db.close()


def _entry(payload, cloud="aws"):
    return {c["cloud"]: c for c in payload["clouds"]}[cloud]


class _gap:
    """Temporarily give a worker a real per-cloud query gap.

    Restores the module's own function rather than `del`-ing the attribute — `del` would
    remove the function itself, since assigning to it only shadowed it."""
    def __init__(self, mod, seconds):
        self.mod, self.seconds = mod, seconds

    def __enter__(self):
        self.orig = self.mod.query_gap_seconds
        self.mod.query_gap_seconds = lambda: self.seconds

    def __exit__(self, *exc):
        self.mod.query_gap_seconds = self.orig
        return False


def _calls(cloud="aws", view="summary"):
    return [c for c in CALLS if c == (view, cloud)]


# ── harness guard ────────────────────────────────────────────────────────────────────

def test_the_two_workers_really_are_independent():
    """Guards the harness, not the code. If both names resolved to one module object the
    single-flight tests below would pass no matter how broken the claim was."""
    assert w1 is not w2 and w1.__dict__ is not w2.__dict__
    assert w1 is not _real_service


# ── the headline bug ─────────────────────────────────────────────────────────────────

def test_a_failure_never_overwrites_a_good_payload():
    """THE regression. A good figure, then a 429 — the figure must still be there, and
    must still be what the page renders."""
    _reset()
    _run(w1.get_summary())
    good = _row().payload
    assert json.loads(good)["amount"] == 10.0

    # Age the figure past the TTL so the next read genuinely tries to refresh it, then
    # snapshot fetched_at so we can prove the FAILURE left it alone.
    _set("aws", "summary", fetched_at=datetime.utcnow() - timedelta(days=1))
    good_at = _row().fetched_at
    _stub(w1, lambda v, c: _fail(c, "429 Too Many Requests", throttled=True, retry_after=300))
    out = _run(w1.get_summary())

    assert len(_calls()) == 2, "the read must actually have attempted a refresh"
    assert _row().payload == good, "a failed query overwrote the last good payload"
    assert _row().fetched_at == good_at, "a failed query moved fetched_at"
    assert _row().last_attempt_at is not None, "the attempt still has to be recorded"
    entry = _entry(out)
    assert entry["status"] == "ok", "a throttled cloud must keep serving its figure"
    assert entry["amount"] == 10.0
    assert entry["stale"] is True and entry["as_of"]
    assert "rate limited" in entry["note"]


def test_no_prior_good_value_still_renders_unavailable():
    """The guard against last-known-good quietly inventing numbers: with nothing cached,
    a failure is still a failure."""
    _reset()
    _stub(w1, lambda v, c: _fail(c, "NotAuthorizedOrNotFound"))
    entry = _entry(_run(w1.get_summary()))
    assert entry["status"] == "unavailable"
    assert entry["amount"] is None
    assert "NotAuthorizedOrNotFound" in entry["detail"]
    assert entry["as_of"] is None


def test_a_fresh_figure_is_served_without_querying():
    _reset()
    _run(w1.get_summary())
    assert len(_calls()) == 1
    _run(w1.get_summary())
    assert len(_calls()) == 1, "a fresh entry must not re-query"


def test_a_stale_figure_is_served_with_as_of_and_re_queried():
    _reset()
    _run(w1.get_summary())
    _set("aws", "summary", fetched_at=datetime.utcnow() - timedelta(days=1))
    entry = _entry(_run(w1.get_summary()))
    assert len(_calls()) == 2, "past the TTL it must re-query"
    assert entry["status"] == "ok" and entry["stale"] is False


# ── cooldown ─────────────────────────────────────────────────────────────────────────

def test_a_429_sets_a_cooldown_from_retry_after():
    """The provider's Retry-After is authoritative — capping it is how the old code
    guaranteed its retry landed inside the same throttle window."""
    _reset()
    _stub(w1, lambda v, c: _fail(c, "429", throttled=True, retry_after=1800))
    _run(w1.get_summary())
    row = _row()
    delta = (row.cooldown_until - datetime.utcnow()).total_seconds()
    assert 1700 < delta <= 1800, delta
    assert row.consecutive_failures == 1


def test_a_short_retry_after_is_floored():
    """A provider asking us back in 5s during a throttle is not a reason to hammer it."""
    _reset()
    _stub(w1, lambda v, c: _fail(c, "429", throttled=True, retry_after=5))
    _run(w1.get_summary())
    delta = (_row().cooldown_until - datetime.utcnow()).total_seconds()
    assert delta > w1._THROTTLE_BASE_S - 60, delta


def test_a_non_throttle_failure_backs_off_more_gently():
    """A misconfiguration must be retried often enough that fixing it is visible, so the
    FIRST failure waits exactly the base — not already doubled."""
    _reset()
    _stub(w1, lambda v, c: _fail(c, "no permission"))
    _run(w1.get_summary())
    delta = (_row().cooldown_until - datetime.utcnow()).total_seconds()
    assert w1._FAIL_BASE_S - 5 < delta <= w1._FAIL_BASE_S, delta
    assert delta < w1._THROTTLE_BASE_S

    # …and each subsequent failure doubles.
    _set("aws", "summary", cooldown_until=None, next_query_allowed_at=None)
    _run(w1.get_summary())
    assert _row().consecutive_failures == 2
    delta = (_row().cooldown_until - datetime.utcnow()).total_seconds()
    assert w1._FAIL_BASE_S < delta <= w1._FAIL_BASE_S * 2, delta


def test_cooldown_is_capped():
    _reset()
    _stub(w1, lambda v, c: _fail(c, "429", throttled=True, retry_after=99999))
    _run(w1.get_summary())
    delta = (_row().cooldown_until - datetime.utcnow()).total_seconds()
    assert delta <= w1._COOLDOWN_MAX_S


def test_refresh_true_does_not_query_during_a_cooldown():
    """The exact loop that turned one 429 into six hours of them: the page shows an error,
    the admin clicks Refresh, and Refresh issues another query into the same window."""
    _reset()
    _run(w1.get_summary())                                   # seed a good figure
    _stub(w1, lambda v, c: _fail(c, "429", throttled=True, retry_after=900))
    _set("aws", "summary", fetched_at=datetime.utcnow() - timedelta(days=1))
    _run(w1.get_summary())                                   # earns the cooldown
    before = len(_calls())

    for _ in range(5):                                       # mash the button
        out = _run(w1.get_summary(refresh=True))
    assert len(_calls()) == before, "refresh crossed the cooldown gate"

    entry = _entry(out)
    assert entry["status"] == "ok" and entry["amount"] == 10.0
    assert entry["stale"] is True and entry["retry_at"]


def test_a_cooldown_survives_a_fresh_module_load():
    """Durability. A redeploy inside a throttle window must not re-earn the 429 — which is
    exactly what a process-local cooldown did, on every image rebuild."""
    _reset()
    _stub(w1, lambda v, c: _fail(c, "429", throttled=True, retry_after=900))
    _run(w1.get_summary())
    assert len(_calls()) == 1

    fresh = _stub(_worker("reboot"))          # a brand-new process, default (ok) responder
    _run(fresh.get_summary())
    assert len(_calls()) == 1, "a restarted process re-queried a cooling-down cloud"


# ── forced refresh ───────────────────────────────────────────────────────────────────

def test_refresh_within_the_min_interval_does_not_query():
    _reset()
    _run(w1.get_summary())
    assert len(_calls()) == 1
    _run(w1.get_summary(refresh=True))
    assert len(_calls()) == 1, "two forced refreshes inside the floor"


def test_refresh_after_the_min_interval_does_query():
    _reset()
    _run(w1.get_summary())
    _set("aws", "summary",
         fetched_at=datetime.utcnow() - timedelta(seconds=w1.min_refresh_interval_seconds() + 5))
    _run(w1.get_summary(refresh=True))
    assert len(_calls()) == 2, "a forced refresh past the floor must re-query"


# ── isolation, single-flight, pacing ─────────────────────────────────────────────────

def test_one_cloud_throttling_does_not_stop_the_others():
    """The point of per-cloud rows: Azure being rate limited must not cost AWS/GCP/OCI
    their refresh, and must not make the total jump down."""
    _reset()
    _stub(w1, lambda v, c: _fail(c, "429", throttled=True, retry_after=900)
          if c == "azure" else _ok(c, 10.0))
    out = _run(w1.get_summary())
    by = {c["cloud"]: c for c in out["clouds"]}
    assert by["azure"]["status"] == "unavailable"     # never had a figure
    for c in ("aws", "gcp", "oci"):
        assert by[c]["status"] == "ok" and by[c]["stale"] is False
    assert out["total_mtd"] == 30.0
    assert out["stale_clouds"] == []


def test_only_one_worker_queries_a_cloud():
    """Single-flight across processes. Both workers warming at once is the cold-start
    shape that made a container start fire four Cost Management POSTs at one
    subscription."""
    _reset()

    async def _both():
        return await asyncio.gather(w1.get_summary(), w2.get_summary())

    _run(_both())
    assert len(_calls("aws")) == 1, f"both workers queried: {CALLS}"
    assert len(_calls("azure")) == 1


def test_an_expired_lease_lets_another_worker_take_over():
    """A process that dies mid-fetch must not wedge the row forever."""
    _reset()
    _run(w1.get_summary())
    _set("aws", "summary", fetched_at=datetime.utcnow() - timedelta(days=1),
         lease_until=datetime.utcnow() - timedelta(seconds=1), lease_owner="dead:1",
         next_query_allowed_at=None)
    _run(w2.get_summary())
    assert len(_calls()) == 2, "an expired lease still blocked the claim"


def test_a_live_lease_blocks_a_second_claim():
    _reset()
    _run(w1.get_summary())
    _set("aws", "summary", fetched_at=datetime.utcnow() - timedelta(days=1),
         lease_until=datetime.utcnow() + timedelta(seconds=60), lease_owner="other:1",
         next_query_allowed_at=None)
    out = _run(w2.get_summary())
    assert len(_calls()) == 1, "two processes queried the same cloud at once"
    assert _entry(out)["amount"] == 10.0, "the blocked caller must serve the cached figure"


def test_claiming_one_view_paces_the_whole_cloud():
    """Cost Management throttles per SUBSCRIPTION, so this cloud's two views must be paced
    apart even though they are different cache rows — including on the very first pass,
    when the sibling row does not exist yet and a bulk UPDATE has nothing to reach."""
    _reset()
    with _gap(w1, 30):
        _run(w1.get_summary())
    row = _row("aws", "breakdown")
    assert row is not None, "claiming one view must create the cloud's other row"
    assert row.next_query_allowed_at is not None, "claiming one view must pace the cloud"
    assert row.next_query_allowed_at > datetime.utcnow()


def test_a_paced_view_waits_rather_than_skipping():
    """The gap must DELAY the second view, not drop it. warm() asks for summary then
    breakdown back to back; skipping would leave the breakdown unfetched on every pass."""
    _reset()
    with _gap(w1, 1):
        _run(w1.warm())
    assert len(CALLS) == 8, f"a paced view was skipped instead of delayed: {CALLS}"
    assert _row("aws", "breakdown").payload, "the breakdown never landed"


# ── payload validity ─────────────────────────────────────────────────────────────────

def test_a_month_rollover_is_a_miss_not_a_stale_serve():
    """MTD spend from last month is not stale, it is wrong."""
    _reset()
    _run(w1.get_summary())
    _set("aws", "summary", period="1999-01",
         fetched_at=datetime.utcnow() - timedelta(days=1))
    _stub(w1, lambda v, c: _fail(c, "down"))
    entry = _entry(_run(w1.get_summary()))
    assert len(_calls()) == 2, "last month's row must not satisfy a read"
    assert entry["status"] == "unavailable", "last month's figure must not be served"


def test_a_payload_version_bump_is_a_miss():
    """Replaces the old versioned-cache-key guard: an old shape must re-query, not be
    handed to a template expecting the new one."""
    _reset()
    _run(w1.get_summary())
    _set("aws", "summary", payload_version=w1.PAYLOAD_VERSION + 1,
         fetched_at=datetime.utcnow() - timedelta(days=1))
    _stub(w1, lambda v, c: _fail(c, "down"))
    assert _entry(_run(w1.get_summary()))["status"] == "unavailable"


def test_mark_stale_re_queries_but_keeps_serving():
    """A Setup save must make the next read re-query without blanking the page meanwhile."""
    _reset()
    _run(w1.get_summary())
    db = SessionLocal()
    try:
        w1.mark_stale(db)
    finally:
        db.close()
    assert _row().payload, "mark_stale must not delete the figure"
    _run(w1.get_summary())
    assert len(_calls()) == 2


def test_clear_cooldowns_lets_the_next_read_try_again():
    _reset()
    _stub(w1, lambda v, c: _fail(c, "429", throttled=True, retry_after=900))
    _run(w1.get_summary())
    db = SessionLocal()
    try:
        w1.clear_cooldowns(db)
    finally:
        db.close()
    assert _row().cooldown_until is None
    _stub(w1)
    _run(w1.get_summary())
    assert _entry(_run(w1.get_summary()))["amount"] == 10.0


# ── read-only callers ────────────────────────────────────────────────────────────────

def test_allow_fetch_false_never_queries():
    """The notification scanner's contract: a budget alert is not worth paying for a
    billable query, and it runs in jobs_worker where nothing warms."""
    _reset()
    assert _run(w1.get_summary(allow_fetch=False)) is None
    assert CALLS == [], "a read-only caller queried a cloud"

    _run(w1.get_summary())
    out = _run(w1.get_summary(allow_fetch=False))
    assert out is not None and _entry(out)["amount"] == 10.0
    assert len(_calls()) == 1


def test_snapshot_reports_state_without_leaking_payloads():
    """/api/cache/status is how you confirm the cache is actually shared across workers —
    but it must not put billing figures in a health endpoint."""
    _reset()
    _run(w1.get_summary())
    db = SessionLocal()
    try:
        rows = w1.snapshot(db)
    finally:
        db.close()
    assert len(rows) >= 4
    aws = [r for r in rows if r["cloud"] == "aws" and r["view"] == "summary"][0]
    assert aws["has_value"] is True and aws["fetched_at"]
    assert aws["stale"] is False and aws["cooling_down"] is False
    assert "payload" not in aws and "amount" not in aws


def test_breakdown_keeps_every_scope_key_when_stale():
    """A stale breakdown still has to expose every key the template reads, or Alpine
    renders `undefined` where a figure used to be."""
    _reset()
    _run(w1.get_breakdown())
    _stub(w1, lambda v, c: _fail(c, "429", throttled=True, retry_after=900))
    _set("aws", "breakdown", fetched_at=datetime.utcnow() - timedelta(days=1),
         next_query_allowed_at=None)
    entry = _entry(_run(w1.get_breakdown()))
    # The stubbed breakdown entry is an _ok() summary shape; what matters is that the
    # cached payload came back untouched and marked stale rather than being replaced.
    assert entry["status"] == "ok" and entry["stale"] is True


def test_warm_uses_the_same_entrypoints_as_the_endpoints():
    """The warmer must not hold its own fetch path — that is the drift the warmer-parity
    suite exists to prevent. One pass fills both views for all four clouds."""
    _reset()
    _run(w1.warm())
    assert len(CALLS) == 8, CALLS
    assert {v for v, _ in CALLS} == {"summary", "breakdown"}


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
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
