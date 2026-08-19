"""The durable dashboard tile snapshot: a failed collection must never cost you the counts.

Same invariants as `tests/test_cost_cache.py`, and deliberately the same harness, because
this table exists for the same three reasons: `gunicorn -w 2` plus `jobs_worker` is three
processes, a process-local dict gives each its own copy, and an image rebuild throws all of
them away.

What it adds over the cost cache is the reason the table exists at all. The dashboard home
page fans out to ~33 endpoints, ~22 of them at once, each holding a pooled connection
against `pool_size=5 + max_overflow=5`. Caching does not fix that — a cache hit is still a
request, still a connection. Only the page asking ONE question fixes it, and this table is
what it asks.

Pinned here:
  * a failed collection writes the cooldown columns and never touches `payload`/`fetched_at`
  * `payload_version` is a shape gate: an old row is a MISS, not a stale serve
  * the lease is single-flight, and expires so a dead process cannot hold a tile forever
  * `cooldown_until` is a hard gate a FORCED refresh does not cross
  * claiming one tile paces its whole provider — including on a cold start, which is the
    case the pacing is actually for
  * `min_age_s` lets the app-side fallback defer to the worker without a second code path
  * `mark_stale` keeps serving while it re-collects, rather than blanking the page

**Two worker processes are simulated by loading the module twice** under different names
from the same file: each copy gets its own globals, which is what two gunicorn workers
have. They share the database and nothing else.

Uses a temporary SQLite file and the real ORM. Single-flight on SQLite rests on the lease
check rather than a Postgres advisory lock, so the contention tests prove the lease.

Runs under pytest, or standalone:  python tests/test_dashboard_stat_cache.py
"""
import importlib.util
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="dash-stat-cache-test-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDB}"
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-dashboard-stat-cache-tests")
# Pacing off by default: it would make every back-to-back claim in this file wait for real,
# and only the pacing tests are about it (they set their own gap). 0 has to survive
# _cfg_int, which is exactly why that helper tests for blank rather than falsy.
os.environ["DASHBOARD_STATS_GAP_SECONDS"] = "0"

try:
    from web_dashboard.database import Base, DashboardStatCache, SessionLocal, engine
except Exception as exc:  # pragma: no cover — app deps missing
    try:
        import pytest
        pytest.skip(f"app dependencies unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)

Base.metadata.create_all(bind=engine)

_MODULE_PATH = os.path.join(_ROOT, "web_dashboard", "services", "dashboard_stat_cache.py")


def _worker(tag: str):
    """Load the store as a fresh module object with its own globals — a stand-in for the
    second gunicorn worker process."""
    spec = importlib.util.spec_from_file_location(
        f"web_dashboard.services.dashboard_stat_cache__{tag}", _MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


w1 = _worker("w1")
w2 = _worker("w2")

COUNTS = {"value": 7, "running": 4}


def _reset():
    db = SessionLocal()
    try:
        db.query(DashboardStatCache).delete()
        db.commit()
    finally:
        db.close()


def _db():
    return SessionLocal()


def _row(tile_key="aws_instances", scope=""):
    db = _db()
    try:
        return w1._read_snap(db, tile_key, scope)
    finally:
        db.close()


def _seed_good(mod=None, tile_key="aws_instances", provider="aws", counts=None):
    """Claim then finish successfully, leaving a normal last-known-good row."""
    mod = mod or w1
    db = _db()
    try:
        ok, _snap, reason = mod.claim(db, tile_key, provider=provider)
        assert ok, f"seed claim was refused: {reason}"
        mod.finish(db, tile_key, counts or COUNTS)
    finally:
        db.close()


# ── the headline invariant ────────────────────────────────────────────────────

def test_a_failed_collection_never_overwrites_good_counts():
    _reset()
    _seed_good()
    before = _row()
    assert json.loads(before["payload"]) == COUNTS

    db = _db()
    try:
        # Force past the freshness check, then fail.
        w1.finish(db, "aws_instances", None, error="host unreachable")
    finally:
        db.close()

    after = _row()
    assert json.loads(after["payload"]) == COUNTS, (
        "a failed collection replaced the last known counts — this is the entire reason "
        "the table exists rather than a cache entry with a TTL")
    assert after["fetched_at"] == before["fetched_at"], (
        "fetched_at moved on a failure, so the tile will claim counts are newer than they "
        "are")
    assert after["last_error"] == "host unreachable"
    assert after["cooldown_until"] is not None


def test_no_prior_counts_still_reads_as_nothing_rather_than_zero():
    _reset()
    db = _db()
    try:
        w1.claim(db, "gcp_instances", provider="gcp")
        w1.finish(db, "gcp_instances", None, error="never worked")
    finally:
        db.close()
    snap = _row("gcp_instances")
    assert snap["payload"] is None
    assert w1.payload_of(snap) is None, (
        "a tile that has never been collected must read as absent, not as 0 — 0 is a "
        "plausible number and renders as one")


def test_a_payload_version_bump_is_a_miss_not_a_stale_serve():
    _reset()
    _seed_good()
    snap = _row()
    assert w1.payload_of(snap) == COUNTS

    db = _db()
    try:
        db.query(DashboardStatCache).update({DashboardStatCache.payload_version: 999})
        db.commit()
    finally:
        db.close()

    snap = _row()
    assert w1.payload_of(snap) is None, (
        "a row written under a different payload shape was served anyway — bumping "
        "PAYLOAD_VERSION must make old rows a MISS that re-collects")


# ── single-flight ─────────────────────────────────────────────────────────────

def test_only_one_worker_claims_a_tile():
    _reset()
    d1, d2 = _db(), _db()
    try:
        ok1, _s, _r = w1.claim(d1, "azure_vms", provider="azure")
        ok2, _s, reason = w2.claim(d2, "azure_vms", provider="azure")
    finally:
        d1.close()
        d2.close()
    assert ok1 is True
    assert ok2 is False, "both workers claimed the same tile — they will both call Azure"
    assert reason == "in-flight", reason


def test_an_expired_lease_lets_another_worker_take_over():
    _reset()
    db = _db()
    try:
        assert w1.claim(db, "azure_vms", provider="azure")[0] is True
        # The first worker died mid-collection: its lease is the only thing releasing it.
        db.query(DashboardStatCache).update(
            {DashboardStatCache.lease_until: datetime.utcnow() - timedelta(seconds=1)})
        db.commit()
        ok, _s, _r = w2.claim(db, "azure_vms", provider="azure")
        assert ok is True, (
            "an expired lease still blocked a second worker — a process that dies "
            "mid-collection would wedge that tile permanently")
    finally:
        db.close()


def test_a_fresh_tile_is_not_reclaimed():
    _reset()
    _seed_good()
    db = _db()
    try:
        ok, _s, reason = w1.claim(db, "aws_instances", provider="aws")
    finally:
        db.close()
    assert ok is False and reason == "fresh", reason


def test_a_tile_past_its_ttl_is_reclaimed():
    _reset()
    _seed_good()
    db = _db()
    try:
        old = datetime.utcnow() - timedelta(seconds=w1.ttl_seconds() + 5)
        db.query(DashboardStatCache).update({DashboardStatCache.fetched_at: old})
        db.commit()
        ok, _s, reason = w1.claim(db, "aws_instances", provider="aws")
    finally:
        db.close()
    assert ok is True, f"a stale tile was not reclaimed ({reason})"


# ── the hard gate ─────────────────────────────────────────────────────────────

def test_cooldown_blocks_even_a_forced_refresh():
    _reset()
    _seed_good()
    db = _db()
    try:
        w1.finish(db, "aws_instances", None, error="provider is throttling")
        ok, _s, reason = w1.claim(db, "aws_instances", provider="aws", force=True)
    finally:
        db.close()
    assert ok is False and reason == "cooldown", (
        f"a forced refresh crossed the cooldown ({reason}) — that is how one throttle "
        "becomes many: the tile errors, the operator clicks Refresh, and we query into "
        "the window already rejecting us")


def test_a_saturation_refusal_backs_off_more_gently_than_a_real_failure():
    _reset()
    db = _db()
    try:
        w1.claim(db, "gcp_images", provider="gcp")
        w1.finish(db, "gcp_images", None, error="gcp is saturated", busy=True)
        busy_until = _row("gcp_images")["cooldown_until"]

        w1.claim(db, "oci_images", provider="oci")
        w1.finish(db, "oci_images", None, error="credentials rejected")
        fail_until = _row("oci_images")["cooldown_until"]
    finally:
        db.close()
    assert busy_until < fail_until, (
        "a pool-saturation refusal is not a broken provider — we simply arrived while it "
        "was busy, so it must not earn the same escalating backoff as a real failure")


def test_repeated_failures_escalate_but_stay_capped():
    _reset()
    db = _db()
    try:
        waits = []
        for _ in range(10):
            db.query(DashboardStatCache).update(
                {DashboardStatCache.cooldown_until: None})
            db.commit()
            w1.claim(db, "oci_instances", provider="oci", force=True,
                     now=datetime.utcnow() + timedelta(days=1))
            snap = w1.finish(db, "oci_instances", None, error="still down")
            waits.append((snap["cooldown_until"] - datetime.utcnow()).total_seconds())
    finally:
        db.close()
    assert waits[1] > waits[0], "backoff does not escalate"
    assert max(waits) <= w1._COOLDOWN_MAX_S + 5, (
        f"backoff exceeded the cap ({max(waits)}s) — an unbounded cooldown means a tile "
        "that never recovers on its own")


# ── pacing ────────────────────────────────────────────────────────────────────

def _with_gap(mod, seconds):
    """Override the pacing gap at runtime.

    NOT via os.environ: `settings` is built from the environment at IMPORT time, so a
    later os.environ write is silently ignored — which is how the first draft of the
    cold-start test below passed while pacing was doing nothing at all.
    """
    original = mod.query_gap_seconds
    mod.query_gap_seconds = lambda: seconds
    return original


def test_claiming_one_tile_paces_its_whole_provider():
    _reset()
    db = _db()
    original = _with_gap(w1, 60)
    try:
        # GCP owns seven tiles against an 8-thread pool, so pacing has to be per provider.
        w1.ensure_tiles(db, [("gcp_instances", "", "gcp"), ("gcp_images", "", "gcp"),
                             ("aws_instances", "", "aws")])
        assert w1.claim(db, "gcp_instances", provider="gcp")[0] is True
        ok, _s, reason = w1.claim(db, "gcp_images", provider="gcp")
        assert ok is False and reason == "paced", (
            f"a sibling GCP tile was not paced ({reason}) — one collection pass would "
            "hand itself CloudProviderBusy")
        # A different provider is unaffected: the whole point of per-provider pools.
        ok2, _s2, _r2 = w1.claim(db, "aws_instances", provider="aws")
        assert ok2 is True, "pacing one provider blocked another"
    finally:
        w1.query_gap_seconds = original
        db.close()


def test_ensure_tiles_is_what_makes_cold_start_pacing_work():
    # A bulk UPDATE cannot reach a row that does not exist, so without ensure_tiles the
    # very first pass — the cold start pacing is FOR — paces nothing. Both halves are
    # asserted, because the "absent row" half alone passes just as well when pacing is
    # switched off entirely, and that is how this test first passed vacuously.
    _reset()
    db = _db()
    original = _with_gap(w1, 60)
    try:
        assert w1.claim(db, "gcp_instances", provider="gcp")[0] is True
        ok, _s, reason = w1.claim(db, "gcp_images", provider="gcp")
        assert ok is True and reason == "claimed", (
            "a row absent at pacing time should still be claimable; if this starts "
            "failing, pacing can reach absent rows and ensure_tiles may be redundant")

        # Same provider, same gap — but now the row exists when the pacing UPDATE runs.
        _reset()
        w1.ensure_tiles(db, [("gcp_instances", "", "gcp"), ("gcp_images", "", "gcp")])
        assert w1.claim(db, "gcp_instances", provider="gcp")[0] is True
        ok2, _s2, reason2 = w1.claim(db, "gcp_images", provider="gcp")
        assert ok2 is False and reason2 == "paced", (
            f"pre-creating the rows changed nothing ({reason2}) — pacing is not working, "
            "so the contrast above proves nothing either")
    finally:
        w1.query_gap_seconds = original
        db.close()

    _reset()
    db = _db()
    try:
        made = w1.ensure_tiles(db, [("gcp_instances", "", "gcp"), ("gcp_images", "", "gcp")])
        assert made == 2
        assert w1.ensure_tiles(db, [("gcp_instances", "", "gcp")]) == 0, (
            "ensure_tiles is not idempotent — it runs every pass")
    finally:
        db.close()


# ── the app-side fallback ─────────────────────────────────────────────────────

def test_min_age_lets_the_app_defer_to_the_worker():
    _reset()
    _seed_good()
    db = _db()
    try:
        # Past the TTL, so the WORKER would claim it...
        old = datetime.utcnow() - timedelta(seconds=w1.ttl_seconds() + 5)
        db.query(DashboardStatCache).update({DashboardStatCache.fetched_at: old})
        db.commit()

        ok, _s, reason = w1.claim(db, "aws_instances", provider="aws",
                                  min_age_s=w1.stale_after_seconds())
        assert ok is False and reason == "deferring", (
            f"the app-side fallback claimed a tile the worker is still keeping current "
            f"({reason}) — with a worker running it should cost one SELECT and nothing else")

        # ...and once nothing has touched it for the whole window, the app takes over.
        older = datetime.utcnow() - timedelta(seconds=w1.stale_after_seconds() + 5)
        db.query(DashboardStatCache).update({DashboardStatCache.fetched_at: older})
        db.commit()
        ok2, _s2, _r2 = w1.claim(db, "aws_instances", provider="aws",
                                 min_age_s=w1.stale_after_seconds())
        assert ok2 is True, (
            "with no worker present the app must eventually collect, or a bare install "
            "never paints")
    finally:
        db.close()


# ── maintenance ───────────────────────────────────────────────────────────────

def test_mark_stale_keeps_serving_while_it_recollects():
    _reset()
    _seed_good()
    db = _db()
    try:
        assert w1.mark_stale(db) >= 1
    finally:
        db.close()
    snap = _row()
    assert w1.payload_of(snap) == COUNTS, (
        "mark_stale deleted the counts. It must not: the operator changed a setting, and "
        "trading a working number for a blank tile is the cost-cache mistake")
    assert snap["stale"] is True

    db = _db()
    try:
        ok, _s, _r = w1.claim(db, "aws_instances", provider="aws")
        assert ok is True, "a stale-marked tile must be re-collectable immediately"
    finally:
        db.close()


def test_clear_cooldowns_reopens_every_provider():
    _reset()
    _seed_good()
    db = _db()
    try:
        w1.finish(db, "aws_instances", None, error="bad credential")
        assert _row()["cooldown_until"] is not None
        assert w1.clear_cooldowns(db) >= 1
        assert _row()["cooldown_until"] is None, (
            "a Setup save must reopen the provider — the operator just changed the "
            "credential we were backing off from")
        ok, _s, _r = w1.claim(db, "aws_instances", provider="aws", force=True,
                              now=datetime.utcnow() + timedelta(days=1))
        assert ok is True
    finally:
        db.close()


def test_read_all_groups_by_tile_and_costs_one_query():
    _reset()
    _seed_good(tile_key="aws_instances", provider="aws")
    _seed_good(tile_key="azure_vms", provider="azure")
    db = _db()
    try:
        w1.ensure_tiles(db, [("hyperv_vms", "conn-a", "hyperv"),
                             ("hyperv_vms", "conn-b", "hyperv")])
        out = w1.read_all(db)
    finally:
        db.close()
    assert set(out) >= {"aws_instances", "azure_vms", "hyperv_vms"}
    assert len(out["hyperv_vms"]) == 2, (
        "per-connection scopes must come back grouped under one tile — summing them is "
        "how an install with two Proxmox clusters stops describing only the default one")
    assert w1.payload_of(out["aws_instances"][0]) == COUNTS


def test_snapshot_reports_health_without_payloads():
    _reset()
    _seed_good()
    db = _db()
    try:
        rows = w1.snapshot(db)
    finally:
        db.close()
    assert rows and rows[0]["tile_key"] == "aws_instances"
    assert rows[0]["provider"] == "aws"
    assert rows[0]["has_payload"] is True
    # /api/cache/status is an operator view, not a data feed; comparing fetched_at across
    # replicas is how you prove the table is genuinely shared.
    assert "payload" not in rows[0], "snapshot must not ship tile payloads"
    assert rows[0]["fetched_at"], "fetched_at is the whole point of the operator view"


def test_the_lock_id_does_not_collide():
    # 20260101 init_db DDL, 20260102 audit chain, 20260103 expiry enqueue, 20260104 cost.
    assert w1._STAT_LOCK_ID == 20260105
    assert w1._STAT_LOCK_ID not in (20260101, 20260102, 20260103, 20260104)
    # Every provider needs its own key, or two providers share a claim lock and one of
    # them silently never collects while the other is working.
    keys = list(w1._PROVIDER_LOCK_KEYS.values())
    assert len(keys) == len(set(keys)), "duplicate provider lock keys"


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
            traceback.print_exc()
    sys.exit(1 if failures else 0)
