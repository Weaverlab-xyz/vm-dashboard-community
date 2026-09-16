"""Durable, cross-process store for a lab platform's inventory listings.

The POV page's two collection reads — every environment on the platform, and every
template it could be built from — go through here. ``lab_platforms``' adapters talk to the
platform; this module decides *whether we are allowed to ask* and *what to show when the
answer is no*.

Deliberately the same shape as ``services/cost_cache`` and ``services/dashboard_stat_cache``
— read the first of those module docstrings before changing anything here. The three
properties it lists are load-bearing for the same reasons, and one more that is specific
to this caller:

**The credential is instance-wide, so the load scaled with the number of SEs.** Both
listings went out on the one Skytap username and API token in Settings, against the one
configured project. Every SE with ``pov:write`` therefore got a byte-identical answer, and
paid a paged platform GET for it on every page load. A hundred SEs is a hundred times the
calls for one account's worth of information — and Skytap answers a saturated account with
423 plus a Retry-After, which is a property of the ACCOUNT, not of a process. That is why
this is a table and not ``services/cache_service``: a process-local dict gives
``gunicorn -w 2`` plus ``jobs_worker`` three copies and three throttle budgets, and throws
all of them away on the image pull that every deploy here is.

**A failure never overwrites a success.** ``payload`` is written only on a listing that
came back; a failure writes the error and cooldown columns and leaves the last good
listing exactly where it was. A 423 mid-window must not be able to empty the "All
environments on the platform" table — which is what a generic cache would do, because it
cannot tell a rate-limited account from an empty one.

**A throttled platform is left alone.** ``cooldown_until`` is a hard gate that
``?refresh=true`` does not cross. Refreshing into a window that is already rejecting
queries is the incident ``cost_cache``'s docstring recounts, and the POV page has the same
button.

Two implementation rules, both easy to get wrong and both already documented in the two
sibling modules:

*Sessions never span the network call.* :func:`claim` opens its transaction, decides,
commits — releasing the advisory lock — and only then does the caller await the platform.
:func:`finish` opens a second one. A pooled connection held across a paged HTTP listing is
the exhaustion failure ``database.py``'s pool-sizing comment is about, and
``tests/test_cache_fetcher_sessions.py`` exists because it has already happened here.

*ORM instances never leave their session.* ``SessionLocal`` has ``expire_on_commit=True``,
so every decision below runs off :func:`_snap`, a plain detached dict.

On SQLite (single-process dev installs) there are no advisory locks, so single-flight
degrades to the lease check plus the primary key. Genuinely weaker, and adequate for the
same reason it is in the siblings: nothing else is running.
"""
import asyncio
import json
import logging
import os
import socket
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

logger = logging.getLogger(__name__)


KIND_ENVIRONMENTS = "environments"
KIND_TEMPLATES = "templates"
KINDS = (KIND_ENVIRONMENTS, KIND_TEMPLATES)

# BUMP when an adapter changes the shape of a listed item. Does the job a versioned cache
# key would: an old row becomes a MISS that re-lists, rather than a shape the page cannot
# render being served as though it were current.
PAYLOAD_VERSION = 1

# Transaction-scoped advisory lock class id. 20260101 is init_db's DDL lock, 20260102 the
# audit chain, 20260103 the expiry enqueue, 20260104 the cost cache, 20260105 the
# dashboard stat cache. MUST stay transaction-scoped: a session-scoped pg_advisory_lock
# leaks through SQLAlchemy's QueuePool — the incident documented in database.py::init_db —
# and holding one across a paged platform listing would be strictly worse.
_POV_PLATFORM_LOCK_ID = 20260916

# Lock keys per platform. An unknown platform gets no key and is refused the lock rather
# than sharing another platform's — see _try_platform_lock.
_PLATFORM_LOCK_KEYS = {"skytap": 1, "aws": 2, "azure": 3, "gcp": 4, "oci": 5}

# Backoff after a failed listing. Flat and short for a 423, which means "arrived while the
# account was busy" rather than "this credential is wrong"; escalating otherwise, because
# a listing that has failed four times is telling us something a fifth will not fix.
_BUSY_COOLDOWN_S = 60
_FAIL_BASE_S = 120
_COOLDOWN_MAX_S = 1800


# ── tunables ─────────────────────────────────────────────────────────────────

def _cfg_int(key: str, default: int) -> int:
    """A Settings integer, read live so a change takes effect on the next pass."""
    try:
        from . import config_service
        raw = config_service.get(key)
        if raw:
            return int(raw)
    except Exception:  # noqa: BLE001 — a malformed setting must not break the read path
        pass
    return default


def ttl_seconds() -> int:
    """How long a listing is considered current.

    Defaults to the POV reconcile interval, because that sweep is what refreshes this
    table — a TTL shorter than the cadence that fills it would mark every row not-fresh
    between passes and hand the request path a fetch it was built to avoid.
    """
    return _cfg_int("pov_platform_cache_ttl_seconds", 600)


def lease_seconds() -> int:
    """Liveness bound on a claim. Must exceed a slow paged listing plus its 423 retries —
    `SkytapClient` will wait out six of them at up to 30s each."""
    return _cfg_int("pov_platform_cache_lease_seconds", 300)


def min_refresh_interval_seconds() -> int:
    """Floor under ``?refresh=true``. Without one, Re-check is a button that issues a
    paged platform listing as fast as it can be pressed."""
    return _cfg_int("pov_platform_cache_min_refresh_seconds", 30)


def query_gap_seconds() -> int:
    """Pacing between listings of the SAME platform. Both kinds go to one account, so the
    templates read waits out the gap the environments read just set."""
    return _cfg_int("pov_platform_cache_query_gap_seconds", 5)


# ── helpers ──────────────────────────────────────────────────────────────────

def _utcnow() -> datetime:
    """Naive UTC, matching every DateTime column in database.py. Mixing an aware value
    into a comparison here would let the session TimeZone decide a cooldown."""
    return datetime.utcnow()


def _owner() -> str:
    return f"{socket.gethostname()[:40]}:{os.getpid()}"


def _iso(dt):
    return dt.replace(tzinfo=timezone.utc).isoformat() if dt else None


def scope_of(platform: str) -> str:
    """The project id this platform's listings are scoped to, or "".

    Part of the primary key, not a detail: on Skytap a listing taken under project A must
    never be served for project B. Only Skytap has the concept; every other adapter is
    account-wide and answers "".
    """
    try:
        if (platform or "") == "skytap":
            from . import skytap_service
            return (skytap_service.configured_project_id() or "")[:64]
    except Exception:  # noqa: BLE001
        logger.warning("pov platform cache: could not read the project scope for %r",
                       platform, exc_info=True)
    return ""


def _row(db, kind: str, platform: str, project_id: str):
    from ..database import PovPlatformCache
    return db.query(PovPlatformCache).filter(
        PovPlatformCache.kind == kind,
        PovPlatformCache.platform == platform,
        PovPlatformCache.project_id == project_id).first()


def _snap(row) -> dict:
    """A detached copy of one row's decision-relevant columns.

    Everything below reads this rather than the ORM instance, so nothing can touch an
    expired attribute after the session commits or closes.
    """
    if row is None:
        return None
    return {"kind": row.kind, "platform": row.platform, "project_id": row.project_id,
            "payload": row.payload, "payload_version": row.payload_version,
            "fetched_at": row.fetched_at, "stale": bool(row.stale),
            "last_error": row.last_error, "last_attempt_at": row.last_attempt_at,
            "cooldown_until": row.cooldown_until}


def _read_snap(db, kind: str, platform: str, project_id: str) -> dict:
    return _snap(_row(db, kind, platform, project_id))


def _has_good(snap) -> bool:
    """Whether this row holds a usable last-known-good listing.

    An empty LIST is usable — a project with no templates is a real answer. Only a NULL
    payload, or one written under a superseded shape, is a miss.
    """
    return bool(snap and snap["payload"] is not None
                and snap["payload_version"] == PAYLOAD_VERSION)


def _is_fresh(snap, now: datetime) -> bool:
    if not _has_good(snap) or snap["stale"] or not snap["fetched_at"]:
        return False
    return (now - snap["fetched_at"]).total_seconds() < ttl_seconds()


def items_of(snap):
    """The parsed listing, or None. Public: the read path needs it too."""
    if not _has_good(snap):
        return None
    try:
        items = json.loads(snap["payload"])
    except (TypeError, ValueError):
        logger.warning("pov platform cache: unparseable payload for %s/%s/%s",
                       snap["kind"], snap["platform"], snap["project_id"])
        return None
    return items if isinstance(items, list) else None


def _cooldown_seconds(*, busy: bool, failures: int) -> int:
    """How long to leave this platform alone. ``failures`` INCLUDES this one, so the first
    waits exactly ``_FAIL_BASE_S`` and each subsequent one doubles."""
    if busy:
        return _BUSY_COOLDOWN_S
    return min(_FAIL_BASE_S * (2 ** min(max(failures - 1, 0), 6)), _COOLDOWN_MAX_S)


def note(snap, now: datetime = None) -> str:
    """Human-readable reason a listing is not current. Empty when fresh.

    Served to the page rather than kept here, for the reason ``/platforms`` gives about
    capabilities: a table that says "last read 20 minutes ago, the platform is
    rate-limited" is worth more than one that silently shows old rows.
    """
    now = now or _utcnow()
    if not _has_good(snap):
        return "not read yet"
    if snap["cooldown_until"] and snap["cooldown_until"] > now:
        return f"platform unavailable — {snap['last_error'] or 'the last attempt failed'}"
    if snap["stale"]:
        return "configuration changed — re-reading"
    if snap["fetched_at"] and (now - snap["fetched_at"]).total_seconds() >= ttl_seconds():
        return f"last read {_iso(snap['fetched_at'])}"
    return ""


# ── claim / finish ───────────────────────────────────────────────────────────

def _try_platform_lock(db, platform: str) -> bool:
    """Try the per-PLATFORM claim lock. Never blocks; False means another process is
    claiming right now and this caller should serve what is in the table.

    Transaction-scoped, released by the commit at the end of :func:`claim` — which happens
    BEFORE any platform call. SQLite has no advisory locks and serializes writers at the
    database level; it relies on the lease check instead.
    """
    from ..database import _is_sqlite
    if _is_sqlite:
        return True
    key = _PLATFORM_LOCK_KEYS.get(platform)
    if key is None:
        # An unknown platform must not silently share another's lock key. Refusing
        # degrades to "someone else is claiming", which serves cache rather than letting
        # two processes list the same account at once.
        logger.warning("pov platform cache: no lock key for platform %r", platform)
        return False
    return bool(db.execute(
        text("SELECT pg_try_advisory_xact_lock(:c, :k)"),
        {"c": _POV_PLATFORM_LOCK_ID, "k": key}).scalar())


def _ensure_row(db, kind: str, platform: str, project_id: str):
    from ..database import PovPlatformCache
    row = _row(db, kind, platform, project_id)
    if row is None:
        row = PovPlatformCache(kind=kind, platform=platform, project_id=project_id,
                               payload_version=0, consecutive_failures=0, stale=False)
        db.add(row)
        db.flush()
    return row


def _ensure_rows(db, platform: str, project_id: str) -> dict:
    """Create the missing rows for EVERY kind of this platform, and return them all.

    Every kind, not just the one being claimed, because pacing is a bulk UPDATE over the
    platform's rows and **cannot reach a row that does not exist yet**. Without this the
    very first pass — the cold start this whole table is about — claims the environments
    listing, paces nothing, and lets the templates listing fire at the same account in the
    same instant. Which is the burst the gap exists to prevent, occurring exactly once per
    install and therefore never in a test that seeded its rows first.

    Same reasoning and same shape as ``cost_cache._ensure_rows``, which creates both views
    of a cloud for the identical reason.
    """
    return {k: _ensure_row(db, k, platform, project_id) for k in KINDS}


def claim(db, kind: str, platform: str, *, project_id: str = None,
          now: datetime = None, refresh: bool = False) -> tuple:
    """Decide whether this process may list ``kind`` off ``platform``, and stake the claim.

    Returns ``(claimed, snap, reason)``. One short transaction: take the lock, re-read the
    row, decide, write the lease, commit — and the commit releases the lock, so nothing is
    held across the listing that follows.
    """
    from ..database import PovPlatformCache
    now = now or _utcnow()
    project_id = scope_of(platform) if project_id is None else project_id

    if not _try_platform_lock(db, platform):
        db.rollback()
        return False, _read_snap(db, kind, platform, project_id), "contended"

    row = _ensure_rows(db, platform, project_id)[kind]
    snap = _snap(row)
    blocked = None
    if snap["cooldown_until"] and snap["cooldown_until"] > now:
        # Applies to ?refresh=true too. Bypassing the cooldown on an explicit refresh is
        # how one 423 becomes an hour of them: the page shows an error, which makes the SE
        # press Re-check, which issues another listing into the window already refusing.
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
        logger.debug("pov platform cache: %s/%s not claimed (%s)", platform, kind, blocked)
        return False, snap, blocked

    row.lease_until = now + timedelta(seconds=lease_seconds())
    row.lease_owner = _owner()
    row.updated_at = now
    db.flush()
    # Pace the whole PLATFORM, not just this kind: both listings go to one account, so the
    # next one — whichever kind asks — waits out the gap. Reaches only rows that exist,
    # which is why `warm` touches both kinds.
    db.query(PovPlatformCache).filter(
        PovPlatformCache.platform == platform,
        PovPlatformCache.project_id == project_id).update(
            {PovPlatformCache.next_query_allowed_at:
                now + timedelta(seconds=query_gap_seconds())},
            synchronize_session=False)
    db.commit()
    return True, snap, "claimed"


def finish(db, kind: str, platform: str, items, *, project_id: str = None,
           now: datetime = None, error: str = "", busy: bool = False) -> dict:
    """Record the outcome of one listing, release the lease, return the new snapshot.

    The asymmetry is the point of the whole module: ``items`` is written only when it is
    not None; a failure does NOT touch ``payload`` or ``fetched_at``. A rate-limited
    account must never be able to replace a working listing with an empty table.
    """
    now = now or _utcnow()
    project_id = scope_of(platform) if project_id is None else project_id
    row = _ensure_row(db, kind, platform, project_id)

    row.lease_until = None
    row.lease_owner = None
    row.last_attempt_at = now
    row.updated_at = now

    if items is not None:
        row.payload = json.dumps(items)
        row.payload_version = PAYLOAD_VERSION
        row.fetched_at = now
        row.stale = False
        row.last_error = None
        row.consecutive_failures = 0
        row.cooldown_until = None
    else:
        failures = (row.consecutive_failures or 0) + 1
        row.consecutive_failures = failures
        row.last_error = (error or "")[:2000]
        secs = _cooldown_seconds(busy=busy, failures=failures)
        row.cooldown_until = now + timedelta(seconds=secs)
        kept = (f"keeping the listing from {row.fetched_at}" if row.fetched_at
                else "no previous listing to fall back on")
        logger.warning("pov platform cache: %s/%s failed (%s) — cooling down %ds, %s",
                       platform, kind, row.last_error[:120], secs, kept)
    db.commit()
    return _read_snap(db, kind, platform, project_id)


# ── read path ────────────────────────────────────────────────────────────────

def read(db, kind: str, platform: str, *, project_id: str = None) -> dict:
    """What the table holds for one listing. One indexed query, no platform call.

    This is the whole cost of a POV page load's platform half, and keeping it that way is
    the point of the table.
    """
    project_id = scope_of(platform) if project_id is None else project_id
    snap = _read_snap(db, kind, platform, project_id)
    now = _utcnow()
    items = items_of(snap)
    return {
        "items": items if items is not None else [],
        # From the PARSED listing, not from `_has_good`. A payload that is present but
        # unreadable — a truncated write, a shape change that slipped past
        # `payload_version` — would otherwise report "we have one" and hand the page an
        # empty table that nothing re-lists until the next reconcile pass. `None` here
        # means miss, which sends the cold path out to the platform instead.
        "have": items is not None,
        "as_of": _iso(snap["fetched_at"]) if snap else None,
        "fresh": _is_fresh(snap, now),
        "note": note(snap, now),
        # The platform's own words from the last failed attempt, for a caller that has
        # NOTHING to serve and must therefore report rather than render — see
        # `api/pov._cached_listing`. Meaningless beside a good payload: a row keeps the
        # error from a failure that a later success has already superseded.
        "error": (snap or {}).get("last_error") or "",
    }


def is_busy_error(exc: Exception) -> bool:
    """Whether a failed listing was "the account was busy" rather than a real fault.

    Skytap answers a saturated account 423 and a plain rate limit 429, and
    ``SkytapClient`` has already retried both to exhaustion by the time one reaches here —
    so arriving with one means the window is wide, and the short flat cooldown is the
    right response rather than an escalating one.
    """
    text_ = str(exc)
    return "(423)" in text_ or "(429)" in text_ or "rate-limited" in text_


async def refresh_one(kind: str, platform: str, *, refresh: bool = False) -> dict:
    """Claim → list → record, for one (kind, platform). Returns the fresh :func:`read`.

    Opens its own short-lived Sessions and holds NONE across the listing — see the module
    docstring's first implementation rule. Never raises: a caller in the request path
    wants the last good listing plus a note, not a 502.
    """
    from ..database import SessionLocal
    from . import lab_platforms

    project_id = scope_of(platform)

    # Before the claim, so an unconfigured platform creates no rows and earns no cooldown.
    # `run_reconcile` warms every platform in VALID_PLATFORMS, and a POV instance runs one
    # cloud at most — so without this, every pass wrote a failure row and a WARNING for
    # three clouds nobody set up, and the next operator to read `snapshot()` would find
    # four platforms "unavailable" and one of them real.
    try:
        if not lab_platforms.adapter(platform).configured():
            db = SessionLocal()
            try:
                out = read(db, kind, platform, project_id=project_id)
            finally:
                db.close()
            out["reason"] = "not-configured"
            return out
    except Exception:  # noqa: BLE001 — an unknown platform is the caller's problem
        logger.warning("pov platform cache: cannot resolve platform %r", platform,
                       exc_info=True)

    db = SessionLocal()
    try:
        claimed, _snapshot, reason = claim(db, kind, platform, project_id=project_id,
                                          refresh=refresh)
    finally:
        db.close()

    if not claimed:
        db = SessionLocal()
        try:
            out = read(db, kind, platform, project_id=project_id)
        finally:
            db.close()
        out["reason"] = reason
        return out

    items, error, busy = None, "", False
    try:
        mod = lab_platforms.adapter(platform)
        fn = getattr(mod, f"list_{kind}")
        items = list(await fn())
    except Exception as exc:  # noqa: BLE001 — recorded on the row, not raised at a page
        error = str(exc)
        busy = is_busy_error(exc)
        logger.warning("pov platform cache: listing %s off %s failed", kind, platform,
                       exc_info=True)

    db = SessionLocal()
    try:
        finish(db, kind, platform, items, project_id=project_id, error=error, busy=busy)
        out = read(db, kind, platform, project_id=project_id)
    finally:
        db.close()
    out["reason"] = "listed" if items is not None else "failed"
    return out


async def warm(platform: str, *, refresh: bool = False) -> None:
    """Re-list every kind for one platform. Called from the POV reconcile pass.

    Sequential and it WAITS OUT the pacing gap between kinds rather than tripping over
    it. That distinction is the whole of this function's difficulty, so it is worth
    stating: ``claim`` paces the whole platform, so the gap the environments listing sets
    applies to the templates listing a millisecond later. A loop that simply called
    ``refresh_one`` twice therefore got one listing and one "paced" per pass — and since
    the skipped kind is whichever ran second, the templates row would sit empty forever
    while the log said the warm succeeded. Skipping is the right answer for an unrelated
    caller who can serve cache; it is the wrong answer for the only writer this table has.

    The wait only happens after a kind that actually went out — a pass that found both
    rows fresh costs two SELECTs and no sleep, which is every pass on a quiet install.

    ``refresh`` is what separates the two callers. The timer pass leaves it False, so a
    row still inside its TTL is skipped and a quiet install costs nothing; Re-check sets
    it True, because an operator pressing it has just changed something and "it is still
    fresh" is not the answer they are after. Neither crosses ``cooldown_until``.
    """
    went_out = False
    for kind in KINDS:
        if went_out:
            await asyncio.sleep(query_gap_seconds())
        try:
            out = await refresh_one(kind, platform, refresh=refresh)
            # "failed" counts: the platform was called, it just did not answer. Not
            # pacing after a failure is how a broken account gets asked twice in a row.
            went_out = out.get("reason") in ("listed", "failed")
        except Exception:  # noqa: BLE001 — one kind failing must not skip the other
            went_out = False
            logger.warning("pov platform cache: warming %s off %s failed", kind, platform,
                           exc_info=True)


# ── maintenance ──────────────────────────────────────────────────────────────

def mark_stale(db) -> int:
    """Mark every listing not-fresh, without deleting anything. Called from a Settings
    save.

    A mark rather than a delete, for the reason ``cost_cache`` uses one: the current
    listing keeps serving while the next pass re-reads. Deleting first trades a working
    answer for a maybe, which is what made one throttle into a blank page there.

    ``cooldown_until`` is deliberately NOT cleared — a saturated account stays left alone
    however many times Settings is saved. :func:`clear_cooldowns` is the explicit,
    separate act.
    """
    from ..database import PovPlatformCache
    n = db.query(PovPlatformCache).update({PovPlatformCache.stale: True},
                                          synchronize_session=False)
    db.commit()
    return n


def clear_cooldowns(db) -> int:
    """Drop every backoff. For an operator who has just fixed the credential and should
    not have to wait out a cooldown earned by the broken one."""
    from ..database import PovPlatformCache
    n = db.query(PovPlatformCache).filter(
        PovPlatformCache.cooldown_until.isnot(None)).update(
            {PovPlatformCache.cooldown_until: None,
             PovPlatformCache.consecutive_failures: 0},
            synchronize_session=False)
    db.commit()
    return n


def snapshot(db) -> list:
    """Every row, for diagnostics. Payloads excluded — a listing is large and nobody
    debugging a throttle needs it."""
    from ..database import PovPlatformCache
    out = []
    for row in db.query(PovPlatformCache).all():
        snap = _snap(row)
        items = items_of(snap)
        out.append({"kind": row.kind, "platform": row.platform,
                    "project_id": row.project_id,
                    "count": len(items) if items is not None else None,
                    "as_of": _iso(row.fetched_at),
                    "stale": bool(row.stale),
                    "last_error": row.last_error,
                    "consecutive_failures": row.consecutive_failures or 0,
                    "cooldown_until": _iso(row.cooldown_until),
                    "lease_owner": row.lease_owner,
                    "lease_until": _iso(row.lease_until)})
    return out
