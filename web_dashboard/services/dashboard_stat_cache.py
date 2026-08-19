"""Durable, cross-process store for the dashboard's tile counts.

Everything the dashboard read endpoint and the collector touch goes through here. The
collector queries the providers; this module decides *whether we are allowed to ask* and
*what to show when the answer is no*.

Deliberately the same shape as ``services/cost_cache`` — read that module's docstring
first. The three properties it lists are load-bearing here for the same reasons:

**A failure never overwrites a success.** ``payload`` is written only on a successful
collection; a failure writes the error and cooldown columns and leaves the last known
counts exactly where they were. Several of the collectors' sources degrade rather than
raise (an unreachable hypervisor, a saturated provider pool), so "a well-formed empty
answer" and "a real answer" are indistinguishable to a generic cache. Here they are not
the same code path at all.

**The state is shared and durable.** ``gunicorn -w 2`` plus ``jobs_worker`` is three
processes. A process-local dict — which is what ``services/cache_service`` is — gives each
one its own copy, its own throttle, and a cold start on every image rebuild.

**A saturated provider is left alone.** ``cooldown_until`` is a hard gate that even an
explicit refresh does not cross.

WHY A TABLE AND NOT MORE CACHE
------------------------------
The home page fans out to ~33 endpoints, ~22 of them simultaneously, and every one holds a
pooled connection for its whole duration against ``pool_size=5 + max_overflow=5``. That is
the ``QueuePool limit ... reached`` failure of 26.8.5, and no amount of caching fixes it:
a cache hit is still a request, still a connection, still a slot. What fixes it is the page
asking ONE question. This table is the answer to that question.

TWO RULES THAT ARE EASY TO GET WRONG
------------------------------------
*Sessions never span the network call.* Each collection opens its own short-lived Session
to claim, closes it, awaits the provider with nothing held, then opens a second one to
record the result. Never a request-scoped Session — holding a pooled connection across a
30s cloud call is the exhaustion failure ``database.py``'s pool-sizing comment is written
about, and ``tests/test_cache_fetcher_sessions.py`` exists because it already happened.

*ORM instances never leave their session.* ``SessionLocal`` has ``expire_on_commit=True``,
so a row read here and touched after the commit raises ``DetachedInstanceError``. Every
decision below runs off :func:`_snap`, a plain detached dict.

On SQLite (single-process dev installs) there are no advisory locks, so single-flight
degrades to the lease check plus the primary key. Genuinely weaker, and adequate for the
same reason it is in cost_cache: nothing else is running.
"""
import json
import logging
import os
import socket
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

logger = logging.getLogger(__name__)


# BUMP when a tile's payload shape changes. Does the job a versioned cache key would: an
# old row becomes a MISS that re-collects, rather than a shape nobody can parse being
# served as if it were current.
PAYLOAD_VERSION = 1

# Transaction-scoped advisory lock class id. 20260101 is init_db's DDL lock, 20260102 the
# audit chain, 20260103 the expiry enqueue, 20260104 the cost cache. MUST stay
# transaction-scoped: a session-scoped pg_advisory_lock leaks through SQLAlchemy's
# QueuePool — the incident documented in database.py::init_db — and holding one across a
# provider call would be strictly worse.
_STAT_LOCK_ID = 20260105

# Second lock key, per PROVIDER rather than per tile. What it guards is "is any call to
# this provider in flight", and GCP alone owns seven tiles against an 8-thread pool
# (services/cloud_executor), so pacing per tile would let a single pass earn
# CloudProviderBusy from itself.
_PROVIDER_LOCK_KEYS = {
    "aws": 1, "azure": 2, "gcp": 3, "oci": 4,
    "proxmox": 5, "nutanix": 6, "vsphere": 7, "hyperv": 8, "xcpng": 9,
    "k8s": 10, "storage": 11, "clouddb": 12, "portainer": 13, "local": 14,
}

# Cooldown shape. Softer than cost_cache's, because these failures are mostly "the host is
# down" or "the pool is busy" rather than a billing API rate limit with a Retry-After.
_BUSY_COOLDOWN_S = 30      # CloudProviderBusy / CloudCallTimeout — "not now", not "broken"
_FAIL_BASE_S = 120         # first real failure, doubling from there
_COOLDOWN_MAX_S = 1800


# ── tunables ─────────────────────────────────────────────────────────────────

def _cfg_int(key: str, default: int) -> int:
    """A dashboard_stats_* number, from config → settings → literal default.

    Tests for BLANKNESS rather than truthiness: ``raw or getattr(settings, key)`` silently
    replaces a legitimate 0, which is a bug this repo has shipped before.
    """
    try:
        from . import config_service
        raw = config_service.get(key, "")
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


def ttl_seconds() -> int:
    """How old a tile may be before a collection pass refetches it."""
    return _cfg_int("dashboard_stats_ttl_seconds", 120)


def collect_interval_seconds() -> int:
    """How often the collector wakes."""
    return _cfg_int("dashboard_stats_interval_seconds", 60)


def lease_seconds() -> int:
    """How long a claim is honoured before another process may take over."""
    return _cfg_int("dashboard_stats_lease_seconds", 120)


def query_gap_seconds() -> int:
    """Minimum spacing between two calls to the SAME provider."""
    return _cfg_int("dashboard_stats_gap_seconds", 2)


def stale_after_seconds() -> int:
    """The app-side fallback's deference window — see ``main._warm_dashboard_stats``."""
    return _cfg_int("dashboard_stats_stale_after_seconds", 300)


def min_refresh_interval_seconds() -> int:
    """Floor between two forced refreshes, so the button cannot be mashed."""
    return _cfg_int("dashboard_refresh_min_interval_seconds", 30)


# ── row snapshots ────────────────────────────────────────────────────────────

def _utcnow() -> datetime:
    """Naive UTC, matching every DateTime column in database.py. Mixing an aware value
    into a comparison here would let the session TimeZone decide a cooldown."""
    return datetime.utcnow()


def _owner() -> str:
    return f"{socket.gethostname()[:40]}:{os.getpid()}"


def _iso(dt):
    return dt.replace(tzinfo=timezone.utc).isoformat() if dt else None


def _row(db, tile_key: str, scope: str = ""):
    from ..database import DashboardStatCache
    return db.query(DashboardStatCache).filter(
        DashboardStatCache.tile_key == tile_key,
        DashboardStatCache.scope == scope).first()


def _snap(row) -> dict:
    """A detached copy of one row's decision-relevant columns.

    Everything below reads this rather than the ORM instance, so nothing can touch an
    expired attribute after the session commits or closes.
    """
    if row is None:
        return None
    return {"tile_key": row.tile_key, "scope": row.scope, "payload": row.payload,
            "payload_version": row.payload_version, "fetched_at": row.fetched_at,
            "stale": bool(row.stale), "last_error": row.last_error,
            "last_attempt_at": row.last_attempt_at,
            "cooldown_until": row.cooldown_until}


def _read_snap(db, tile_key: str, scope: str = "") -> dict:
    return _snap(_row(db, tile_key, scope))


def _has_good(snap) -> bool:
    """Whether this row holds usable last-known-good counts."""
    return bool(snap and snap["payload"]
                and snap["payload_version"] == PAYLOAD_VERSION)


def _is_fresh(snap, now: datetime) -> bool:
    if not _has_good(snap) or snap["stale"] or not snap["fetched_at"]:
        return False
    return (now - snap["fetched_at"]).total_seconds() < ttl_seconds()


def payload_of(snap):
    """The parsed payload, or None. Public: the read endpoint needs it too."""
    if not _has_good(snap):
        return None
    try:
        return json.loads(snap["payload"])
    except (TypeError, ValueError):
        logger.warning("dashboard stats: unparseable payload for %s/%s",
                       snap["tile_key"], snap["scope"])
        return None


def _cooldown_seconds(*, busy: bool, failures: int) -> int:
    """How long to leave this tile alone after a failed collection.

    ``failures`` INCLUDES this one, so the first waits exactly ``_FAIL_BASE_S`` and each
    subsequent one doubles. A saturation refusal is not a failure of the same kind — the
    provider is fine, we just arrived while it was busy — so it gets a flat short wait
    rather than an escalating one.
    """
    if busy:
        return _BUSY_COOLDOWN_S
    return min(_FAIL_BASE_S * (2 ** min(max(failures - 1, 0), 6)), _COOLDOWN_MAX_S)


# ── claim / finish ───────────────────────────────────────────────────────────

def _try_provider_lock(db, provider: str) -> bool:
    """Try the per-PROVIDER claim lock. Never blocks; False means another process is
    claiming right now and this caller should skip this pass.

    Transaction-scoped, released by the commit at the end of :func:`claim` — which happens
    BEFORE any provider call. SQLite has no advisory locks and serializes writers at the
    database level; it relies on the lease check instead.
    """
    from ..database import _is_sqlite
    if _is_sqlite:
        return True
    key = _PROVIDER_LOCK_KEYS.get(provider)
    if key is None:
        # An unknown provider must not silently share another's lock key. Refusing the
        # lock degrades to "someone else is claiming", which skips this pass rather than
        # letting two processes collect the same tile.
        logger.warning("dashboard stats: no lock key for provider %r", provider)
        return False
    return bool(db.execute(
        text("SELECT pg_try_advisory_xact_lock(:c, :k)"),
        {"c": _STAT_LOCK_ID, "k": key}).scalar())


def _ensure_row(db, tile_key: str, scope: str, provider: str = ""):
    from ..database import DashboardStatCache
    row = _row(db, tile_key, scope)
    if row is None:
        row = DashboardStatCache(tile_key=tile_key, scope=scope, provider=provider,
                                 payload_version=0, consecutive_failures=0, stale=False)
        db.add(row)
        db.flush()
    elif provider and row.provider != provider:
        row.provider = provider
        db.flush()
    return row


def ensure_tiles(db, tiles) -> int:
    """Create any missing rows for ``[(tile_key, scope, provider), ...]``.

    Called once per collection pass, BEFORE any claim, and that ordering is load-bearing:
    pacing is a bulk `UPDATE ... WHERE provider = :p`, and an UPDATE cannot reach a row
    that does not exist yet. Without this the cold start — the very case the pacing is for
    — claims the first tile of a provider, paces nothing, and lets its six siblings fire at
    that provider in the same instant. The cost cache had to learn this too.
    """
    made = 0
    for tile_key, scope, provider in tiles:
        if _row(db, tile_key, scope) is None:
            _ensure_row(db, tile_key, scope, provider)
            made += 1
    db.commit()
    return made


def claim(db, tile_key: str, *, provider: str, scope: str = "", now: datetime = None,
          force: bool = False, min_age_s: int = 0) -> tuple:
    """Decide whether this process may collect ``tile_key``, and stake the claim if so.

    Returns ``(claimed, snap, reason)``. One short transaction: take the lock, re-read the
    row, decide, write the lease, commit — and the commit releases the lock, so nothing is
    held across the provider call that follows.

    ``min_age_s`` is how old the row must be before this caller will touch it. The
    collector passes 0; the app-side fallback passes a long window so that with a worker
    present it claims nothing and costs one SELECT. One code path, two levels of patience.
    """
    from ..database import DashboardStatCache
    now = now or _utcnow()

    if not _try_provider_lock(db, provider):
        db.rollback()
        return False, _read_snap(db, tile_key, scope), "contended"

    row = _ensure_row(db, tile_key, scope, provider)
    snap = _snap(row)
    blocked = None
    if snap["cooldown_until"] and snap["cooldown_until"] > now:
        # Applies to a forced refresh too. Bypassing the cooldown on an explicit refresh is
        # how one throttle becomes many: the tile shows an error, which makes the operator
        # click Refresh, which issues another call into the window already rejecting them.
        blocked = "cooldown"
    elif row.lease_until and row.lease_until > now:
        blocked = "in-flight"
    elif row.next_query_allowed_at and row.next_query_allowed_at > now:
        blocked = "paced"
    elif force and snap["fetched_at"] and (
            (now - snap["fetched_at"]).total_seconds() < min_refresh_interval_seconds()):
        blocked = "refreshed-recently"
    elif min_age_s and snap["fetched_at"] and (
            (now - snap["fetched_at"]).total_seconds() < min_age_s):
        blocked = "deferring"
    elif not force and _is_fresh(snap, now):
        blocked = "fresh"

    if blocked:
        db.commit()
        logger.debug("dashboard stats: %s/%s not claimed (%s)", tile_key, scope, blocked)
        return False, snap, blocked

    row.lease_until = now + timedelta(seconds=lease_seconds())
    row.lease_owner = _owner()
    row.updated_at = now
    db.flush()
    # Pace the whole PROVIDER, not just this tile: the next call to it, whichever tile
    # asks, waits out the gap. Reaches only rows that EXIST — see ensure_tiles.
    db.query(DashboardStatCache).filter(
        DashboardStatCache.provider == provider).update(
            {DashboardStatCache.next_query_allowed_at:
                now + timedelta(seconds=query_gap_seconds())},
            synchronize_session=False)
    db.commit()
    return True, snap, "claimed"


def finish(db, tile_key: str, result, *, scope: str = "", now: datetime = None,
           error: str = "", busy: bool = False) -> dict:
    """Record the outcome of one collection, release the lease, return the new snapshot.

    The asymmetry is the point of the whole module: ``result`` is written only when it is
    not None; a failure does NOT touch ``payload`` or ``fetched_at``. A provider having a
    bad minute must never be able to replace a working number with a zero.
    """
    now = now or _utcnow()
    row = _ensure_row(db, tile_key, scope)

    row.lease_until = None
    row.lease_owner = None
    row.last_attempt_at = now
    row.updated_at = now

    if result is not None:
        row.payload = json.dumps(result)
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
        kept = (f"keeping the counts from {row.fetched_at}" if row.fetched_at
                else "no previous counts to fall back on")
        logger.warning("dashboard stats: %s/%s failed (%s) — cooling down %ds, %s",
                       tile_key, scope, row.last_error[:120], secs, kept)
    db.commit()
    return _read_snap(db, tile_key, scope)


# ── read path ────────────────────────────────────────────────────────────────

def read_all(db) -> dict:
    """Every tile's snapshot, keyed by tile_key → list of per-scope snaps.

    ONE query. This is the whole database cost of a dashboard page load, and keeping it
    that way is the point of the table.
    """
    from ..database import DashboardStatCache
    out: dict = {}
    for row in db.query(DashboardStatCache).all():
        out.setdefault(row.tile_key, []).append(_snap(row))
    return out


def note(snap, now: datetime = None) -> str:
    """Human-readable reason a tile's counts are not current. Empty when fresh."""
    now = now or _utcnow()
    if not _has_good(snap):
        return "not collected yet"
    if snap["cooldown_until"] and snap["cooldown_until"] > now:
        return f"provider unavailable — {snap['last_error'] or 'last attempt failed'}"
    if snap["stale"]:
        return "configuration changed — recollecting"
    if snap["fetched_at"] and (now - snap["fetched_at"]).total_seconds() >= ttl_seconds():
        return f"last collected {_iso(snap['fetched_at'])}"
    return ""


# ── maintenance ──────────────────────────────────────────────────────────────

def mark_stale(db) -> int:
    """Mark every tile for recollection without deleting anything.

    Not a delete, deliberately: the next pass re-collects while the current counts keep
    serving. Deleting first is what turned one throttle into a blank page in the cost
    cache — the operator loses a working number in exchange for a maybe.
    """
    from ..database import DashboardStatCache
    n = db.query(DashboardStatCache).update({DashboardStatCache.stale: True},
                                            synchronize_session=False)
    db.commit()
    return n


def clear_cooldowns(db) -> int:
    """Drop every backoff. For a Setup save: the operator just changed a credential, so
    whatever we were backing off from may now be fixed."""
    from ..database import DashboardStatCache
    n = db.query(DashboardStatCache).update(
        {DashboardStatCache.cooldown_until: None,
         DashboardStatCache.consecutive_failures: 0,
         DashboardStatCache.next_query_allowed_at: None},
        synchronize_session=False)
    db.commit()
    return n


def snapshot(db) -> list:
    """Metadata for /api/cache/status — no payloads, just whether this is working.

    Comparing ``fetched_at`` across replicas is how you confirm the table is genuinely
    shared rather than each process keeping its own idea of the world.
    """
    from ..database import DashboardStatCache
    out = []
    for row in db.query(DashboardStatCache).order_by(
            DashboardStatCache.tile_key, DashboardStatCache.scope).all():
        out.append({
            "tile_key": row.tile_key,
            "scope": row.scope,
            "provider": row.provider,
            "has_payload": bool(row.payload),
            "payload_version": row.payload_version,
            "fetched_at": _iso(row.fetched_at),
            "stale": bool(row.stale),
            "last_error": row.last_error,
            "consecutive_failures": row.consecutive_failures,
            "cooldown_until": _iso(row.cooldown_until),
            "lease_owner": row.lease_owner,
            "lease_until": _iso(row.lease_until),
        })
    return out
