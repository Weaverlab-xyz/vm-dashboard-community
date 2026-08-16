"""Durable, cross-process cost cache — one last-known-good figure per (cloud, view).

Everything the ``/api/costs`` endpoints and the warmer read goes through here.
``cost_service`` queries the clouds; this module decides *whether we are allowed to ask*
and *what to show when the answer is no*.

Three properties, each of which the previous in-memory cache lacked and each of which is
load-bearing:

**A failure never overwrites a success.** ``cost_service``'s per-cloud entries degrade to
``status="unavailable"`` rather than raising, which made them indistinguishable from a
real answer to a generic cache — so one Azure 429 was stored like a good value and blanked
the tile for the full six-hour TTL. Here the payload column is written *only* on
``status="ok"``; a failure writes the error and cooldown columns and leaves the last known
figure exactly where it was.

**The state is shared and durable.** The app runs ``gunicorn -w 2`` and ``jobs_worker`` is
a third process, so a process-local dict meant each one held its own idea of the throttle
and its own copy of the data, and every image rebuild threw all of them away. Azure Cost
Management rate-limits per *subscription* — a property of the account, not of a process —
so a cold start used to fire four Cost Management POSTs at one subscription in the same
instant, and a redeploy during a throttle window re-earned the 429 with no figures left to
fall back on.

**A throttled cloud is left alone.** ``cooldown_until`` is a hard gate that even an
explicit ``?refresh=true`` does not cross. Clicking Refresh on a rate-limited page used to
issue more queries into the window that was already rejecting them — and because Refresh
invalidated before fetching, it destroyed the good value first.

Two implementation rules that are easy to get wrong here:

*Sessions never span the network call.* The per-cloud tasks run under ``asyncio.gather``,
and each opens its **own** short-lived Session to claim, closes it, awaits the query with
no session held, then opens a second one to record the result. Never share one
request-scoped Session across the fan-out — Sessions are not safe for concurrent use, and
holding a pooled connection across a 30 s HTTP call is the pool-exhaustion failure
``database.py``'s pool-sizing comment is written about.

*ORM instances never leave their session.* ``SessionLocal`` has ``expire_on_commit=True``,
so a row read here and touched after the commit or the close raises
``DetachedInstanceError``. Every decision below runs off :func:`_snap`, a plain detached
copy of the columns that matter.

On SQLite (single-process dev installs) there are no advisory locks, so single-flight
degrades to the lease check plus the primary key. That is genuinely weaker than the
Postgres path and adequate only because nothing else is running.
"""
import asyncio
import json
import logging
import os
import socket
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from . import config_service, cost_service

logger = logging.getLogger(__name__)

CLOUDS = ("aws", "azure", "gcp", "oci")
VIEW_SUMMARY = "summary"
VIEW_BREAKDOWN = "breakdown"
VIEWS = (VIEW_SUMMARY, VIEW_BREAKDOWN)

# BUMP when a per-cloud entry's shape changes. Does the job the versioned cache key
# ``cost_breakdown_v2`` used to do, but as a column: an old row becomes a MISS that
# re-queries, rather than a key nobody reads and a live 4-cloud fetch on every page load.
PAYLOAD_VERSION = 1

# Transaction-scoped advisory lock class id. 20260101 is init_db's DDL lock, 20260102 the
# audit chain, 20260103 the expiry enqueue; 20260104 was freed when the token-sync enqueue
# was deleted. MUST stay transaction-scoped: a session-scoped pg_advisory_lock leaks
# through SQLAlchemy's QueuePool — that is the incident documented in database.py's
# init_db — and holding one across a 30 s HTTP call would be strictly worse.
_COST_LOCK_ID = 20260104
_CLOUD_LOCK_KEYS = {"aws": 1, "azure": 2, "gcp": 3, "oci": 4}

# Cooldown shape. A provider-supplied Retry-After is a FLOOR that is never shortened — the
# old code capped it at 30s, which is what guaranteed the retry fired while still inside
# the throttle window. Waiting longer than asked is always safe for a rate limit; waiting
# less is what earns the next 429. Non-throttle failures back off exponentially instead, so
# a *misconfiguration* (no permission, no export table) is retried often enough to notice
# the fix, while a *throttle* is not.
_THROTTLE_BASE_S = 900     # minimum quiet period after a rate limit, whatever it asked for
_FAIL_BASE_S = 120         # first non-throttle failure
_COOLDOWN_MAX_S = 3600


# ── tunables ─────────────────────────────────────────────────────────────────────────

def _cfg_int(key: str, default: int) -> int:
    """A config_service integer with a config.py fallback. Tolerates a blank or junk row
    rather than 500-ing the costs page over a typo in a settings value.

    Emptiness is tested explicitly rather than by truthiness: ``0`` is a meaningful value
    for every knob here (it disables pacing, or the cold wait), and ``raw or fallback``
    would silently substitute the default for it."""
    from ..config import settings

    def _blank(v):
        return v is None or str(v).strip() == ""

    raw = config_service.get(key)
    if _blank(raw):
        raw = getattr(settings, key, None)
    if _blank(raw):
        return default
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


def ttl_seconds() -> int:
    """How old a good figure may be before we try to refresh it. Long on purpose: Cost
    Explorer bills per request and Cost Management throttles, and cost data moves slowly."""
    return _cfg_int("cost_cache_ttl_seconds", 21600)           # 6 h


def min_refresh_interval_seconds() -> int:
    """Floor between two *forced* (?refresh=true) requeries of the same (cloud, view)."""
    return _cfg_int("cost_refresh_min_interval_seconds", 300)  # 5 min


def lease_seconds() -> int:
    """How long a claim survives without being finished. This is what releases a process
    that died mid-fetch — the advisory lock is long gone before the query even starts."""
    return _cfg_int("cost_query_lease_seconds", 120)


def query_gap_seconds() -> int:
    """Minimum spacing between two queries to the SAME cloud. Cost Management throttles
    per subscription, so this cloud's summary and breakdown must not overlap."""
    return _cfg_int("cost_query_gap_seconds", 2)


def cold_wait_seconds() -> int:
    """How long a caller with no last-known-good waits for whoever won the claim."""
    return _cfg_int("cost_cold_wait_seconds", 5)


def warm_interval_seconds() -> int:
    return max(60, int(ttl_seconds() * 0.8))


# ── row snapshots ────────────────────────────────────────────────────────────────────

def _utcnow() -> datetime:
    """Naive UTC, matching every DateTime column in database.py. Mixing an aware value
    into a comparison here would let the session TimeZone decide a cooldown."""
    return datetime.utcnow()


def _period(now: datetime = None) -> str:
    """The month a payload belongs to. MTD spend from last month is not stale, it is
    wrong, so a rollover has to be a miss rather than a stale serve."""
    return (now or _utcnow()).strftime("%Y-%m")


def _owner() -> str:
    return f"{socket.gethostname()[:40]}:{os.getpid()}"


def _iso(dt):
    return dt.replace(tzinfo=timezone.utc).isoformat() if dt else None


def _row(db, cloud: str, view: str):
    from ..database import CloudCostCache
    return db.query(CloudCostCache).filter(
        CloudCostCache.cloud == cloud, CloudCostCache.view == view).first()


def _snap(row) -> dict:
    """A detached copy of one row's decision-relevant columns.

    Everything below reads this rather than the ORM instance, so nothing can accidentally
    touch an expired attribute after the session commits or closes."""
    if row is None:
        return None
    return {"cloud": row.cloud, "view": row.view, "payload": row.payload,
            "payload_version": row.payload_version, "period": row.period,
            "fetched_at": row.fetched_at, "stale": bool(row.stale),
            "last_error": row.last_error, "last_attempt_at": row.last_attempt_at,
            "cooldown_until": row.cooldown_until}


def _read_snap(db, cloud: str, view: str) -> dict:
    return _snap(_row(db, cloud, view))


def _has_good(snap, now: datetime) -> bool:
    """Whether this row holds a usable last-known-good figure."""
    return bool(snap and snap["payload"]
                and snap["period"] == _period(now)
                and snap["payload_version"] == PAYLOAD_VERSION)


def _is_fresh(snap, now: datetime) -> bool:
    if not _has_good(snap, now) or snap["stale"] or not snap["fetched_at"]:
        return False
    return (now - snap["fetched_at"]).total_seconds() < ttl_seconds()


def _payload_of(snap):
    try:
        return json.loads(snap["payload"])
    except (TypeError, ValueError):
        logger.warning("cost cache: unparseable payload for %s/%s",
                       snap["cloud"], snap["view"])
        return None


def _cooldown_seconds(*, throttled: bool, retry_after, failures: int) -> int:
    """How long to leave this cloud alone after a failed attempt.

    ``failures`` is the count INCLUDING this one, so the first failure waits exactly
    ``_FAIL_BASE_S`` and each subsequent one doubles."""
    if throttled:
        try:
            asked = int(retry_after or 0)
        except (TypeError, ValueError):
            asked = 0
        return min(max(asked, _THROTTLE_BASE_S), _COOLDOWN_MAX_S)
    return min(_FAIL_BASE_S * (2 ** min(max(failures - 1, 0), 6)), _COOLDOWN_MAX_S)


def _note(snap, now: datetime) -> str:
    """The human-readable reason a figure is being shown stale. Empty when it is fresh."""
    if not snap:
        return ""
    cooldown = snap["cooldown_until"]
    if cooldown and cooldown > now:
        at = cooldown.strftime("%H:%M UTC")
        err = snap["last_error"] or ""
        if "429" in err or "rate limit" in err.lower():
            return f"{snap['cloud'].upper()} is rate limited — retrying at {at}."
        return f"{snap['cloud'].upper()} could not be refreshed — retrying at {at}."
    if snap["last_error"] and snap["last_attempt_at"] and (
            not snap["fetched_at"] or snap["last_attempt_at"] > snap["fetched_at"]):
        return f"Last refresh failed: {snap['last_error'][:200]}"
    return ""


def _decorate(entry: dict, snap, now: datetime, *, stale: bool) -> dict:
    """Attach the staleness metadata the templates render.

    ``status`` deliberately keeps its old meaning — "ok" whenever numbers exist, including
    when they are stale, and "unavailable" only when this cloud has never returned one.
    ``stale``/``note``/``as_of`` carry the age instead. A third status value would have to
    be threaded through every existing template branch and both totals to say the same
    thing, and a throttled cloud would drop out of the total and make it jump down."""
    cooldown = snap["cooldown_until"] if snap else None
    return {**entry,
            "stale": stale,
            "as_of": _iso(snap["fetched_at"]) if snap else None,
            "note": _note(snap, now) if stale else "",
            "retry_at": _iso(cooldown) if cooldown and cooldown > now else None}


def _never_had_one(cloud: str, view: str, snap) -> dict:
    """The genuinely-unavailable entry: this cloud has never returned a number.

    With last-known-good in place this is a much narrower claim than it used to be — it
    means "never", not "not right now" — so the detail string is worth surfacing."""
    detail = ((snap["last_error"] if snap and snap["last_error"] else "")
              or "No cost figure has been retrieved for this cloud yet.")
    if view == VIEW_SUMMARY:
        return {"cloud": cloud, "amount": None, "currency": None,
                "status": "unavailable", "detail": detail,
                "throttled": False, "retry_after": None}
    return cost_service._unavailable_breakdown(cloud, detail)


# ── claim / finish ───────────────────────────────────────────────────────────────────

def _try_cloud_lock(db, cloud: str) -> bool:
    """Try the per-CLOUD claim lock. Never blocks; False means another process is claiming
    right now and this caller should serve cache instead.

    Transaction-scoped, released by the commit at the end of :func:`_claim` — which
    happens BEFORE any network call. Per cloud rather than per (cloud, view) because what
    it guards is "is any query to this subscription in flight", and it is held for the few
    milliseconds of the claim, so the coarser granularity costs nothing.

    SQLite has no advisory locks and serializes writers at the database level; it relies on
    the lease check instead."""
    from ..database import _is_sqlite
    if _is_sqlite:
        return True
    return bool(db.execute(
        text("SELECT pg_try_advisory_xact_lock(:c, :k)"),
        {"c": _COST_LOCK_ID, "k": _CLOUD_LOCK_KEYS[cloud]}).scalar())


def _ensure_rows(db, cloud: str):
    """Create the missing rows for BOTH views of this cloud, and return this cloud's rows.

    Both, not just the one being asked for, because pacing is a bulk UPDATE over the
    cloud's rows and cannot reach a row that does not exist yet. Without this, the very
    first pass — the cold start this whole change is about — claims the summary, paces
    nothing, and lets the breakdown fire at the same subscription immediately."""
    from ..database import CloudCostCache
    existing = {r.view: r for r in db.query(CloudCostCache).filter(
        CloudCostCache.cloud == cloud).all()}
    for v in VIEWS:
        if v not in existing:
            row = CloudCostCache(cloud=cloud, view=v, payload_version=PAYLOAD_VERSION,
                                 consecutive_failures=0, stale=False)
            db.add(row)
            existing[v] = row
    db.flush()
    return existing


def _claim(db, cloud: str, view: str, *, now: datetime, refresh: bool) -> tuple:
    """Decide whether this process may query (cloud, view), and stake the claim if so.

    Returns ``(claimed, snap, reason)``. One short transaction: take the lock, re-read the
    row, decide, write the lease, commit — and the commit releases the lock, so nothing is
    held across the network call that follows.
    """
    from ..database import CloudCostCache
    if not _try_cloud_lock(db, cloud):
        db.rollback()
        return False, _read_snap(db, cloud, view), "contended"

    row = _ensure_rows(db, cloud)[view]
    snap = _snap(row)
    blocked = None
    if snap["cooldown_until"] and snap["cooldown_until"] > now:
        # Applies to ?refresh=true too. Bypassing the cooldown on an explicit refresh is
        # exactly how one 429 became six hours of them: the page showed an error, which
        # made the admin click Refresh, which issued another query into the same window.
        blocked = "cooldown"
    elif row.lease_until and row.lease_until > now:
        blocked = "in-flight"
    elif row.next_query_allowed_at and row.next_query_allowed_at > now:
        blocked = "paced"
    elif refresh and snap["fetched_at"] and (
            (now - snap["fetched_at"]).total_seconds() < min_refresh_interval_seconds()):
        blocked = "refreshed-recently"
    elif not refresh and _is_fresh(snap, now):
        blocked = "fresh"

    if blocked:
        db.commit()
        logger.debug("cost cache: %s/%s not claimed (%s)", cloud, view, blocked)
        return False, snap, blocked

    row.lease_until = now + timedelta(seconds=lease_seconds())
    row.lease_owner = _owner()
    row.updated_at = now
    db.flush()
    # Pace the WHOLE cloud, not just this view: the next query to this subscription,
    # whichever view asks for it, waits out the gap.
    db.query(CloudCostCache).filter(CloudCostCache.cloud == cloud).update(
        {CloudCostCache.next_query_allowed_at: now + timedelta(seconds=query_gap_seconds())},
        synchronize_session=False)
    db.commit()
    return True, snap, "claimed"


def _finish(db, cloud: str, view: str, entry: dict, *, now: datetime) -> dict:
    """Record the outcome of one query, release the lease, and return the new snapshot.

    The asymmetry here is the point of the whole module: on success we write the payload;
    on failure we do NOT touch ``payload`` or ``fetched_at``. A 429 must never be able to
    replace a working number."""
    from ..database import CloudCostCache
    row = _row(db, cloud, view)
    if row is None:  # pragma: no cover — _claim created it
        row = CloudCostCache(cloud=cloud, view=view, consecutive_failures=0, stale=False)
        db.add(row)
        db.flush()

    row.lease_until = None
    row.lease_owner = None
    row.last_attempt_at = now
    row.updated_at = now

    if entry.get("status") == "ok":
        row.payload = json.dumps(entry)
        row.payload_version = PAYLOAD_VERSION
        row.period = _period(now)
        row.fetched_at = now
        row.stale = False
        row.last_error = None
        row.consecutive_failures = 0
        row.cooldown_until = None
    else:
        failures = (row.consecutive_failures or 0) + 1
        row.consecutive_failures = failures
        row.last_error = (entry.get("detail") or "")[:2000]
        secs = _cooldown_seconds(throttled=bool(entry.get("throttled")),
                                 retry_after=entry.get("retry_after"), failures=failures)
        row.cooldown_until = now + timedelta(seconds=secs)
        kept = (f"keeping the figure from {row.fetched_at}" if row.fetched_at
                else "no previous figure to fall back on")
        logger.warning("cost cache: %s/%s failed (%s) — cooling down %ds, %s",
                       cloud, view, row.last_error[:120], secs, kept)
    db.commit()
    return _read_snap(db, cloud, view)


# ── read path ────────────────────────────────────────────────────────────────────────

async def _cloud_view(cloud: str, view: str, *, refresh: bool, allow_fetch: bool) -> dict:
    """Resolve one (cloud, view): serve cache, or claim → fetch → finish → serve."""
    from ..database import SessionLocal
    now = _utcnow()

    db = SessionLocal()
    try:
        if allow_fetch:
            claimed, snap, reason = _claim(db, cloud, view, now=now, refresh=refresh)
        else:
            claimed, snap, reason = False, _read_snap(db, cloud, view), "read-only"
    finally:
        db.close()

    if not claimed and reason == "paced":
        # WAIT out the per-cloud gap rather than skipping. The warmer asks for summary
        # then breakdown back to back, so skipping would leave the breakdown unfetched on
        # every single pass — the gap exists to stop the two views hitting one
        # subscription *simultaneously*, not to drop one of them.
        await asyncio.sleep(min(query_gap_seconds(), cold_wait_seconds()))
        now = _utcnow()
        db = SessionLocal()
        try:
            claimed, snap, reason = _claim(db, cloud, view, now=now, refresh=refresh)
        finally:
            db.close()

    if not claimed:
        if _has_good(snap, now):
            return _decorate(_payload_of(snap), snap, now, stale=not _is_fresh(snap, now))
        if allow_fetch:
            waited = await _wait_for_winner(cloud, view, now)
            if waited is not None:
                return waited
        return _decorate(_never_had_one(cloud, view, snap), snap, now, stale=False)

    # No session is open across this — see the module docstring.
    entry = await _fetch_entry(cloud, view)

    db = SessionLocal()
    try:
        after = _finish(db, cloud, view, entry, now=_utcnow())
    finally:
        db.close()

    now = _utcnow()
    if entry.get("status") == "ok":
        return _decorate(entry, after, now, stale=False)
    if _has_good(after, now):
        # The whole point: the query failed and the figure survived it.
        return _decorate(_payload_of(after), after, now, stale=True)
    return _decorate(_never_had_one(cloud, view, after), after, now, stale=False)


async def _fetch_entry(cloud: str, view: str) -> dict:
    """One cloud's live entry. Never raises — cost_service degrades per cloud."""
    if view == VIEW_SUMMARY:
        return await cost_service.cloud_summary_entry(cloud)
    return await cost_service.cloud_breakdown_entry(cloud)


async def _wait_for_winner(cloud: str, view: str, now: datetime):
    """Poll briefly for the result of whoever won the claim.

    Only fires on a genuine cold miss — no last-known-good AND the claim was lost. Without
    it, a millisecond-wide race at first boot makes one worker render "unavailable" for a
    cloud that is being fetched successfully right next to it."""
    from ..database import SessionLocal
    loop = asyncio.get_running_loop()
    deadline = loop.time() + cold_wait_seconds()
    while loop.time() < deadline:
        await asyncio.sleep(0.25)
        db = SessionLocal()
        try:
            snap = _read_snap(db, cloud, view)
        finally:
            db.close()
        if _has_good(snap, now):
            return _decorate(_payload_of(snap), snap, now, stale=not _is_fresh(snap, now))
        # The winner finished and failed — stop waiting, nothing more is coming.
        if snap and snap["cooldown_until"] and snap["cooldown_until"] > now:
            return _decorate(_never_had_one(cloud, view, snap), snap, now, stale=False)
    return None


async def _gather(view: str, *, refresh: bool, allow_fetch: bool) -> list:
    return list(await asyncio.gather(
        *(_cloud_view(c, view, refresh=refresh, allow_fetch=allow_fetch) for c in CLOUDS)))


def _roll_up(payload: dict, clouds: list) -> dict:
    """Top-level staleness, so the page can show one banner instead of four."""
    stales = [c for c in clouds if c.get("stale")]
    as_ofs = [c["as_of"] for c in clouds if c.get("as_of")]
    return {**payload,
            "stale": bool(stales),
            "stale_clouds": [c["cloud"] for c in stales],
            "oldest_as_of": min(as_ofs) if as_ofs else None,
            # Kept for existing consumers; now means "the newest figure on this page".
            "cached_at": max(as_ofs) if as_ofs else None}


async def get_summary(*, refresh: bool = False, allow_fetch: bool = True):
    """Per-cloud account/subscription MTD spend, served from the durable cache.

    ``allow_fetch=False`` never queries a cloud and returns ``None`` when nothing has ever
    been cached — for callers like the notification scanner, where an alert is not worth
    paying for a billable query."""
    clouds = await _gather(VIEW_SUMMARY, refresh=refresh, allow_fetch=allow_fetch)
    if not allow_fetch and not any(c.get("as_of") for c in clouds):
        return None
    return _roll_up(cost_service.assemble_summary(clouds), clouds)


async def get_breakdown(*, refresh: bool = False, allow_fetch: bool = True):
    """Per-cloud, per-service MTD spend by scope, served from the durable cache."""
    clouds = await _gather(VIEW_BREAKDOWN, refresh=refresh, allow_fetch=allow_fetch)
    if not allow_fetch and not any(c.get("as_of") for c in clouds):
        return None
    return _roll_up(cost_service.assemble_breakdown(clouds), clouds)


async def warm() -> None:
    """One warmer pass. Deliberately the SAME entrypoints /api/costs/{summary,breakdown}
    call, so there is no second fetch path and no key for a warmer to drift onto — the
    failure mode main.py's warmer comment block exists to prevent.

    Sequential, not gathered: the two views hit the same subscription."""
    await get_summary()
    await get_breakdown()


# ── maintenance / introspection ──────────────────────────────────────────────────────

def clear_cooldowns(db) -> int:
    """Drop every cooldown, lease and pacing gate. Called after a Setup save: the operator
    has just changed a credential or a billing export, so whatever we were backing off
    from may be fixed. Deliberately leaves the payload alone."""
    from ..database import CloudCostCache
    n = db.query(CloudCostCache).update(
        {CloudCostCache.cooldown_until: None, CloudCostCache.consecutive_failures: 0,
         CloudCostCache.lease_until: None, CloudCostCache.next_query_allowed_at: None},
        synchronize_session=False)
    db.commit()
    return n


def mark_stale(db) -> int:
    """Mark every payload not-fresh so the next read re-queries.

    Not a delete: fixing a GCP billing export should make the next read re-query, not blank
    the page in the meantime."""
    from ..database import CloudCostCache
    n = db.query(CloudCostCache).update({CloudCostCache.stale: True},
                                        synchronize_session=False)
    db.commit()
    return n


def snapshot(db) -> list:
    """Row metadata for /api/cache/status. No payloads — this is billing data."""
    from ..database import CloudCostCache
    now = _utcnow()
    out = []
    for row in db.query(CloudCostCache).order_by(CloudCostCache.cloud,
                                                 CloudCostCache.view).all():
        snap = _snap(row)
        out.append({
            "cloud": row.cloud, "view": row.view,
            "has_value": bool(row.payload), "period": row.period,
            "fetched_at": _iso(row.fetched_at),
            "age_s": (round((now - row.fetched_at).total_seconds(), 1)
                      if row.fetched_at else None),
            "stale": not _is_fresh(snap, now),
            "consecutive_failures": row.consecutive_failures or 0,
            "cooldown_until": _iso(row.cooldown_until),
            "cooling_down": bool(row.cooldown_until and row.cooldown_until > now),
            "last_error": (row.last_error or "")[:200],
        })
    return out
