"""The POV page's platform listings, served from a table instead of the platform.

The reason this exists: both listings went out on the ONE Skytap username and token in
Settings, so the answer was identical for every SE on the install and the call volume
scaled with how many of them had the page open — against an account that answers
saturation with 423 and a Retry-After.

Five properties, each of which fails quietly rather than loudly when it is wrong:

  * **A failure never overwrites a success.** The whole reason this is a table and not a
    `cache_service` key. A 423 mid-window must not be able to empty a table that was
    correct a minute ago, and an adapter that raises is indistinguishable from an empty
    account to a generic cache.
  * **An empty list is a real answer.** A project with no templates is not a miss. Getting
    this wrong means a synchronous platform read on every page load for that install —
    the failure the table exists to remove, arrived at from the other side.
  * **The project id is part of the key.** `skytap_project_id` is a Settings field and
    blank is the widest scope. A key that omitted it would serve the previous project's
    environment names, which on a shared lab account are other customers' POVs.
  * **A cooldown is not crossed by refresh.** Including an explicit one. The page shows an
    error, which makes the SE press Re-check, which would issue another listing into the
    window already refusing them.
  * **Single-flight.** Ten SEs opening the page in the same second must be one platform
    call, not ten.

Uses a real SQLite database and a fake adapter. No network, no FastAPI.

Runs under pytest, or standalone:
    python tests/test_pov_platform_cache.py
"""
import asyncio
import os
import sys
from datetime import datetime, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-pov-platform-cache")

from web_dashboard import database as d  # noqa: E402

d.Base.metadata.create_all(bind=d.engine)

from web_dashboard.services import lab_platforms, pov_platform_cache as store  # noqa: E402

_PLATFORM = "skytap"


class FakeAdapter:
    """A lab platform whose two listings are scripted."""

    def __init__(self, environments=None, templates=None, raises=None):
        self._envs = environments if environments is not None else []
        self._tpls = templates if templates is not None else []
        self.raises = raises
        self.calls = []

    def configured(self):
        return True

    async def list_environments(self):
        self.calls.append("environments")
        if self.raises:
            raise self.raises
        return list(self._envs)

    async def list_templates(self):
        self.calls.append("templates")
        if self.raises:
            raise self.raises
        return list(self._tpls)


def _install(adapter):
    original = lab_platforms.adapter
    lab_platforms.adapter = lambda platform: adapter
    return original


def _restore(original):
    """Assign back, NEVER `_install(original)` — that would wrap the real resolver in
    another lambda, so the next caller gets a function where it expects a module. Same
    pair, for the same reason, as tests/test_pov_broker.py."""
    lab_platforms.adapter = original


def _scope(value=""):
    """Pin the project scope, without writing config. The real reader goes through
    skytap_service.configured_project_id(); a test that stored one would leave a
    developer's own dashboard scoped to a project that does not exist."""
    store.scope_of = lambda platform: value


def _reset(kind="environments", project_id=""):
    db = d.SessionLocal()
    db.query(d.PovPlatformCache).filter(
        d.PovPlatformCache.kind == kind,
        d.PovPlatformCache.project_id == project_id).delete(synchronize_session=False)
    db.commit()
    db.close()


def _row(kind="environments", project_id=""):
    db = d.SessionLocal()
    try:
        return store._read_snap(db, kind, _PLATFORM, project_id)
    finally:
        db.close()


def _read(kind="environments", project_id=""):
    db = d.SessionLocal()
    try:
        return store.read(db, kind, _PLATFORM, project_id=project_id)
    finally:
        db.close()


def _run(coro):
    return asyncio.run(coro)


def _gap(seconds):
    """Set the per-platform pacing gap.

    `warm` SLEEPS this between kinds rather than skipping the second one, so the real
    5-second default would make every test that calls it five seconds slower for no
    coverage. The one test that is actually about the gap sets it back.
    """
    store.query_gap_seconds = lambda: seconds


# ── the asymmetry ────────────────────────────────────────────────────────────

def test_a_failed_listing_does_not_replace_a_good_one():
    """The property the whole table exists for. `cache_service` cannot express it: a
    raising adapter and an empty account look the same to a generic store, so one 423
    emptied the page for a full TTL."""
    _scope()
    _reset()
    good = [{"id": "sky-1", "name": "poc-alpha"}]
    original = _install(FakeAdapter(environments=good))
    try:
        _run(store.refresh_one("environments", _PLATFORM))
        assert _read()["items"] == good

        # Now the platform starts refusing. The success just written leaves two gates in
        # the way of the next claim, and clearing both is the point of doing it here
        # rather than pretending a second call would go out: the per-platform pacing gap,
        # and the minimum-refresh floor under the `refresh=True` below.
        _clear_pacing()
        _set_fetched_back(seconds=store.min_refresh_interval_seconds() + 5)
        _install(FakeAdapter(raises=RuntimeError("Skytap listing failed (423); "
                                                 "rate-limited")))
        out = _run(store.refresh_one("environments", _PLATFORM, refresh=True))
    finally:
        _restore(original)

    assert out["items"] == good, "a 423 replaced the last good listing"
    snap = _row()
    assert snap["last_error"], "the failure was not recorded"
    assert snap["cooldown_until"] is not None
    # And it says so rather than presenting the old listing as current.
    assert "unavailable" in out["note"]


def test_a_rate_limit_gets_the_short_flat_cooldown():
    """A 423 means "arrived while the account was busy", not "this credential is wrong",
    so it must not earn the escalating backoff a real fault does — the client has already
    retried it to exhaustion by the time one reaches here."""
    assert store.is_busy_error(RuntimeError("Skytap GET /x -> (423)"))
    assert store.is_busy_error(RuntimeError("the account is rate-limited; try again"))
    assert not store.is_busy_error(RuntimeError("credentials were rejected (401)"))
    busy = store._cooldown_seconds(busy=True, failures=4)
    real = store._cooldown_seconds(busy=False, failures=4)
    assert busy < real, "a busy account backs off harder than a broken one"
    # Bounded, so a permanently broken platform is still retried eventually.
    assert store._cooldown_seconds(busy=False, failures=99) <= store._COOLDOWN_MAX_S


def test_an_empty_listing_is_a_real_answer_and_not_a_miss():
    """A project with no templates. If "" and [] were both misses, every page load on that
    install would take a synchronous platform read — the thing this removes."""
    _scope()
    _reset("templates")
    original = _install(FakeAdapter(templates=[]))
    try:
        _run(store.refresh_one("templates", _PLATFORM))
    finally:
        _restore(original)
    out = _read("templates")
    assert out["items"] == []
    assert out["have"] is True, "an empty listing read as 'never listed'"
    assert out["fresh"] is True
    assert out["note"] == ""


# ── the key ──────────────────────────────────────────────────────────────────

def test_the_project_id_is_part_of_the_key():
    """Two projects on one account must not serve each other's environments. On a shared
    lab account the other project's environments are another customer's POVs."""
    _reset("environments", "proj-a")
    _reset("environments", "proj-b")
    a = [{"id": "sky-a", "name": "poc-in-a"}]
    original = _install(FakeAdapter(environments=a))
    try:
        _scope("proj-a")
        _run(store.refresh_one("environments", _PLATFORM))
    finally:
        _restore(original)
        _scope()

    assert _read("environments", "proj-a")["items"] == a
    # The other project addresses a DIFFERENT row, so it is a miss rather than a stale
    # hit. That is what makes a Settings change safe without an explicit invalidation.
    other = _read("environments", "proj-b")
    assert other["items"] == []
    assert other["have"] is False


def test_the_payload_version_retires_an_old_shape():
    """An adapter that changes the shape of a listed item must produce a MISS, not a row
    the page cannot render served as though it were current."""
    _scope()
    _reset()
    original = _install(FakeAdapter(environments=[{"id": "sky-1"}]))
    try:
        _run(store.refresh_one("environments", _PLATFORM))
    finally:
        _restore(original)
    assert _read()["have"] is True

    db = d.SessionLocal()
    row = store._row(db, "environments", _PLATFORM, "")
    row.payload_version = store.PAYLOAD_VERSION + 1
    db.commit()
    db.close()
    assert _read()["have"] is False, "a superseded payload shape was served"


def test_an_unconfigured_platform_is_left_entirely_alone():
    """`run_reconcile` warms every platform in VALID_PLATFORMS and a POV instance runs one
    cloud at most. Without the configured() guard each pass wrote a failure row and a
    WARNING for three clouds nobody set up — so `snapshot()` showed four platforms
    "unavailable" and only one of them meant anything."""
    _scope()
    _reset()

    class Unconfigured(FakeAdapter):
        def configured(self):
            return False

    adapter = Unconfigured(environments=[{"id": "sky-1"}])
    original = _install(adapter)
    try:
        out = _run(store.refresh_one("environments", _PLATFORM))
    finally:
        _restore(original)
    assert adapter.calls == [], "an unconfigured platform was listed anyway"
    assert out["reason"] == "not-configured"
    assert _row() is None, "an unconfigured platform left a row behind"


def test_an_unreadable_payload_is_a_miss_and_not_an_empty_table():
    """The version column catches a shape change somebody REMEMBERED to bump. This is the
    other half: a truncated write or a payload that is not a list at all must send the
    cold path to the platform, not report "we have a listing, it is empty" — which
    nothing would then re-list until the next reconcile pass."""
    _scope()
    _reset()
    db = d.SessionLocal()
    row = store._ensure_row(db, "environments", _PLATFORM, "")
    row.payload = '[{"id": "a"'          # truncated JSON
    row.payload_version = store.PAYLOAD_VERSION
    row.fetched_at = store._utcnow()
    db.commit()
    db.close()
    out = _read()
    assert out["have"] is False, "an unparseable payload read as a usable listing"
    assert out["items"] == []

    db = d.SessionLocal()
    row = store._row(db, "environments", _PLATFORM, "")
    row.payload = '{"id": "a"}'          # valid JSON, wrong shape
    db.commit()
    db.close()
    assert _read()["have"] is False, "a payload that is not a list read as a listing"


# ── the gates ────────────────────────────────────────────────────────────────

def _clear_pacing():
    db = d.SessionLocal()
    db.query(d.PovPlatformCache).update(
        {d.PovPlatformCache.next_query_allowed_at: None},
        synchronize_session=False)
    db.commit()
    db.close()


def test_a_cooldown_is_not_crossed_by_an_explicit_refresh():
    """The incident cost_cache's docstring recounts, with this page's Re-check button on
    it: the table shows an error, so the SE presses Re-check, which issues another listing
    into the window that is already refusing them."""
    _scope()
    _reset()
    now = store._utcnow()
    db = d.SessionLocal()
    row = store._ensure_row(db, "environments", _PLATFORM, "")
    row.cooldown_until = now + timedelta(seconds=300)
    db.commit()
    db.close()

    adapter = FakeAdapter(environments=[{"id": "sky-1"}])
    original = _install(adapter)
    try:
        _run(store.refresh_one("environments", _PLATFORM, refresh=True))
    finally:
        _restore(original)
    assert adapter.calls == [], "an explicit refresh queried a platform in cooldown"


def test_a_fresh_row_is_not_relisted_by_the_timer_pass():
    """`warm` without refresh is the 10-minute top-up. A row inside its TTL costs a
    SELECT — otherwise a quiet install with nobody looking still lists the platform on
    every pass, twice."""
    _scope()
    _gap(0)
    _reset()
    _reset("templates")
    adapter = FakeAdapter(environments=[{"id": "sky-1"}], templates=[{"id": "t1"}])
    original = _install(adapter)
    try:
        _run(store.warm(_PLATFORM))
        assert sorted(adapter.calls) == ["environments", "templates"]
        adapter.calls.clear()
        _clear_pacing()
        _run(store.warm(_PLATFORM))
    finally:
        _restore(original)
    assert adapter.calls == [], "a fresh listing was re-read by the timer pass"


def test_re_check_does_relist_a_fresh_row():
    """The other half: an operator who just fixed a token is not asking whether we think
    the listing is fresh. Still inside the cooldown gate above."""
    _scope()
    _gap(0)
    _reset()
    _reset("templates")
    adapter = FakeAdapter(environments=[{"id": "sky-1"}], templates=[])
    original = _install(adapter)
    try:
        _run(store.warm(_PLATFORM))
        adapter.calls.clear()
        _clear_pacing()
        _set_fetched_back(seconds=store.min_refresh_interval_seconds() + 5)
        _run(store.warm(_PLATFORM, refresh=True))
    finally:
        _restore(original)
    assert "environments" in adapter.calls


def _set_fetched_back(*, seconds):
    """Age every row, so the min-refresh floor does not mask what a test is checking."""
    db = d.SessionLocal()
    db.query(d.PovPlatformCache).update(
        {d.PovPlatformCache.fetched_at: store._utcnow() - timedelta(seconds=seconds)},
        synchronize_session=False)
    db.commit()
    db.close()


def test_a_lease_makes_concurrent_cold_loads_one_platform_call():
    """Ten SEs opening the page in the same second. The lease is what makes the
    synchronous cold-start path safe to have at all."""
    _scope()
    _reset()
    adapter = FakeAdapter(environments=[{"id": "sky-1"}])
    original = _install(adapter)

    async def ten_at_once():
        return await asyncio.gather(
            *[store.refresh_one("environments", _PLATFORM) for _ in range(10)])

    try:
        outs = _run(ten_at_once())
    finally:
        _restore(original)
    assert len(adapter.calls) == 1, f"{len(adapter.calls)} platform calls for one cold row"
    # The losers are told why rather than being handed a silent empty list.
    reasons = {o["reason"] for o in outs}
    assert "listed" in reasons
    assert reasons - {"listed"}, "nine callers all claimed the same row"


def test_the_refresh_floor_bounds_the_re_check_button():
    """Without it, Re-check is a button that issues a paged platform listing as fast as it
    can be pressed."""
    _scope()
    _reset()
    adapter = FakeAdapter(environments=[{"id": "sky-1"}])
    original = _install(adapter)
    try:
        _run(store.refresh_one("environments", _PLATFORM))
        adapter.calls.clear()
        _clear_pacing()
        _run(store.refresh_one("environments", _PLATFORM, refresh=True))
    finally:
        _restore(original)
    assert adapter.calls == [], "refresh ignored the minimum interval"


def test_pacing_holds_the_second_listing_off_the_same_account():
    """Both kinds go to one account, so the templates read waits out the gap the
    environments read just set. Pacing is per PLATFORM, not per kind."""
    _scope()
    _gap(30)
    _reset()
    _reset("templates")
    adapter = FakeAdapter(environments=[{"id": "sky-1"}], templates=[{"id": "t1"}])
    original = _install(adapter)
    try:
        _run(store.refresh_one("environments", _PLATFORM))
        # No _clear_pacing() here, deliberately: this is the gap under test.
        _run(store.refresh_one("templates", _PLATFORM))
    finally:
        _restore(original)
    assert adapter.calls == ["environments"], "the gap did not reach the platform's other kind"


# ── maintenance ──────────────────────────────────────────────────────────────

def test_mark_stale_keeps_serving_while_it_waits():
    """A mark and not a delete. Deleting first trades a working listing for a maybe, which
    is what made one throttle into a blank page in cost_cache."""
    _scope()
    _reset()
    good = [{"id": "sky-1"}]
    original = _install(FakeAdapter(environments=good))
    try:
        _run(store.refresh_one("environments", _PLATFORM))
    finally:
        _restore(original)

    db = d.SessionLocal()
    store.mark_stale(db)
    db.close()
    out = _read()
    assert out["items"] == good, "mark_stale emptied the table"
    assert out["fresh"] is False
    assert "configuration changed" in out["note"]


def test_a_settings_save_does_not_clear_a_cooldown_by_accident():
    """`mark_stale` and `clear_cooldowns` are separate acts. A saturated account stays left
    alone however many times Settings is saved; dropping the backoff is explicit."""
    _scope()
    _reset()
    now = store._utcnow()
    db = d.SessionLocal()
    row = store._ensure_row(db, "environments", _PLATFORM, "")
    row.cooldown_until = now + timedelta(seconds=300)
    db.commit()
    store.mark_stale(db)
    db.close()
    assert _row()["cooldown_until"] is not None, "mark_stale dropped a cooldown"

    db = d.SessionLocal()
    store.clear_cooldowns(db)
    db.close()
    assert _row()["cooldown_until"] is None


def test_the_snapshot_reports_counts_and_not_payloads():
    """For diagnostics. A listing is large and nobody debugging a throttle needs it."""
    _scope()
    _reset()
    original = _install(FakeAdapter(environments=[{"id": "a"}, {"id": "b"}]))
    try:
        _run(store.refresh_one("environments", _PLATFORM))
    finally:
        _restore(original)
    # Filtered on the project scope too, not just the kind: another test leaves a row for
    # `proj-a` behind, and the whole point of that key is that it is a different row.
    rows = [r for r in store_snapshot()
            if r["kind"] == "environments" and r["project_id"] == ""]
    assert len(rows) == 1 and rows[0]["count"] == 2, rows
    assert all("payload" not in r for r in store_snapshot())


def store_snapshot():
    db = d.SessionLocal()
    try:
        return store.snapshot(db)
    finally:
        db.close()


# ── the wiring ───────────────────────────────────────────────────────────────

def test_the_ttl_outlasts_the_sweep_that_fills_it():
    """A TTL equal to the reconcile cadence expires every row in the instant before the
    pass that renews it, so a sweep running a little late paints "last read 10 minutes
    ago" over a table about to be correct. Same looseness, same reason, as the page's own
    STALE_AFTER_MS."""
    from web_dashboard.services import pov_reconcile
    assert store.ttl_seconds() > pov_reconcile.DEFAULT_INTERVAL_S, (
        "the listing TTL is not longer than the sweep interval that refreshes it")


def test_the_lock_id_is_not_shared_with_another_store():
    """A shared class id would serialize two unrelated stores against each other for no
    reason — the note every module carrying one of these repeats."""
    from web_dashboard.services import cost_cache, dashboard_stat_cache, expiry_reaper
    taken = {20260101, cost_cache._COST_LOCK_ID, dashboard_stat_cache._STAT_LOCK_ID,
             expiry_reaper._ENQUEUE_LOCK_ID}
    assert store._POV_PLATFORM_LOCK_ID not in taken


def test_an_unknown_platform_is_refused_the_lock_rather_than_sharing_one():
    """Degrading to "someone else is claiming" serves cache. Falling back to another
    platform's key would let two processes list the same account at once."""
    assert set(store._PLATFORM_LOCK_KEYS) <= set(lab_platforms.VALID_PLATFORMS), (
        "a lock key names a platform that does not exist")
    assert len(set(store._PLATFORM_LOCK_KEYS.values())) == len(store._PLATFORM_LOCK_KEYS), (
        "two platforms share a lock key")


def test_every_kind_names_a_real_adapter_call():
    """`refresh_one` resolves the adapter method as f"list_{kind}". A kind that named no
    method would fail only at runtime, recorded as a platform failure on the row — which
    reads as "the platform is broken"."""
    mod = lab_platforms.adapter("skytap")
    for kind in store.KINDS:
        assert callable(getattr(mod, f"list_{kind}", None)), kind


def test_the_reconcile_pass_warms_both_kinds():
    """The table is only useful if something fills it. The reconcile job is the writer;
    without this call every page load pays the cold path forever."""
    with open(os.path.join(_ROOT, "web_dashboard", "services", "pov_reconcile.py"),
              encoding="utf-8") as fh:
        src = fh.read()
    body = src.split("async def run_reconcile(")[1]
    assert "pov_platform_cache.warm(" in body, (
        "the POV reconcile pass no longer warms the platform listings")


def test_reconcile_itself_still_lists_live():
    """Do NOT point `reconcile` at the cache. It decides whether a POV is GONE from the
    platform, and a ten-minute-old listing would flag an environment created since the
    last pass as missing."""
    with open(os.path.join(_ROOT, "web_dashboard", "services", "pov_reconcile.py"),
              encoding="utf-8") as fh:
        src = fh.read()
    body = src.split("async def reconcile(")[1].split("\nasync def ")[0]
    assert "await mod.list_environments()" in body, (
        "reconcile is reading a cached listing to decide what is missing")


def test_the_managed_rows_do_not_read_their_runstate_out_of_the_cached_listing():
    """The trap this change walked into and had to walk back out of.

    `/pov` renders two tables. The managed one showed a runstate labelled `live` when it
    could find the same environment in the second, read-only table — which was sound while
    that table was a live platform read taken on the same page load. It is a CACHE now,
    written by the same sweep as the rows, so a hit there is a second remembered value
    with no per-row timestamp, wearing the word "live".

    The consequence was not just a wrong label. It fed `staleRunstate`, which gates the
    power buttons: a remembered `running` hides Start from a POV the platform has already
    suspended, which is the incident docs/profiles/pov/lifecycle.md records.
    """
    with open(os.path.join(_ROOT, "web_dashboard", "templates", "pov", "index.html"),
              encoding="utf-8") as fh:
        src = fh.read()
    for helper in ("runstateOf(e) {", "staleRunstate(e) {", "seenAgo(e) {",
                   "rateLimited(e) {"):
        body = src.split(helper)[1].split("\n    },")[0]
        assert "this.environments" not in body and "liveFor" not in body, (
            f"{helper.split('(')[0]} reads the cached platform listing again — that is a "
            f"remembered runstate presented as a live one")
    # And the helper that did it is gone rather than merely unused, so there is nothing
    # for the next reader to reconnect.
    assert "liveFor" not in src


def test_the_page_routes_do_not_call_the_adapter_directly():
    """The whole change, pinned at the route. Either listing going back to `mod.list_*()`
    puts a paged platform GET back in the request path for every SE."""
    with open(os.path.join(_ROOT, "web_dashboard", "api", "pov.py"),
              encoding="utf-8") as fh:
        src = fh.read()
    for fn in ("async def list_environments(", "async def list_templates("):
        body = src.split(fn)[1].split("\n@router")[0]
        assert "_cached_listing(" in body, f"{fn} is not served from the cache"
        assert "mod.list_" not in body, f"{fn} calls the adapter directly again"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
