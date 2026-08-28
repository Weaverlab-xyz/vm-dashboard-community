"""
In-memory cache service — stale-while-revalidate helpers.

Drop-in replacement for the previous Redis-backed implementation.
All data is stored in a process-local dict protected by asyncio.Lock.
In-memory lookups are ~1000x faster than Redis (no network hop or
serialization overhead), and the cache warmers already repopulate on
every startup, so no data is lost when a container restarts.

Public interface is identical to the Redis version — no callers change.

TWO EXPIRIES, AND WHY
---------------------
Each entry carries a SOFT expiry (``_expires_at``, at ``ttl``) meaning "refresh this" and a
HARD expiry (``_hard_expires_at``, at ``ttl + STALE_GRACE_S``) meaning "stop serving this".
Between them the entry is stale-but-servable, which is the whole point of
``get_or_refresh``.

They used to be the same instant, and that made the stale-serve branch dead code: ``get()``
evicts at the soft expiry, so by the time ``get_or_refresh`` computed ``age >= ttl`` there
was nothing left to serve stale. Every TTL boundary was therefore a SYNCHRONOUS fetch on a
user's request — a full cloud round-trip in the request path, on a 60s TTL, for the
dashboard's instance tiles.

``get()`` still evicts at the soft expiry and that is deliberate — do NOT "fix" it. Eleven
call sites in ``api/gcp.py`` and ``api/oci.py`` read this store with a bare ``get()`` and
have **no refresh path at all**; if ``get()`` started serving stale entries, each of them
would serve up to ``ttl + STALE_GRACE_S`` old data with nothing ever refreshing it. Stale
tolerance therefore lives in ``_get_entry``, which only ``get_or_refresh`` uses.

That split means a key should not have both a ``get_or_refresh`` reader and a bare-``get()``
reader: whichever ran first would decide whether the other sees the entry at all. No key
does today. If one ever gains both, the consequence is a lost stale-serve — today's
behaviour — not wrong data.
"""
import asyncio
import logging
from datetime import datetime, timezone
from time import monotonic
from typing import Any, Callable, Coroutine, Optional

logger = logging.getLogger(__name__)


# ── TTL constants (seconds) ───────────────────────────────────────────────────
TTL = {
    # AWS / boto3-backed
    "aws_amis":            300,   # 5 min
    "aws_network_opts":    600,   # 10 min
    "aws_instances":        60,   # 1 min
    "aws_community":       900,   # 15 min
    "aws_db_options":      600,   # 10 min — RDS subnet groups + SGs for the DB provision form
    "k8s_provision_opts":  600,   # 10 min — VPC subnets for the k8s provision form (AWS only)
    # Azure / azure-sdk-backed
    "azure_images":        300,   # 5 min
    "azure_network_opts":  600,   # 10 min
    "azure_vms":            60,   # 1 min
    # Portainer (proxied via Hybrid Worker in cloud mode)
    "portainer_endpoints": 300,   # 5 min
    "portainer_containers": 60,   # 1 min
    "portainer_stacks":    120,   # 2 min
    # Cost (cross-cloud MTD spend) does NOT live here. It moved to services/cost_cache.py
    # and a `cloud_cost_cache` table, because this store cannot express what that data
    # needs: a failure must not overwrite a success, the throttle state must be shared
    # across workers, and both must survive a restart. Don't add a cost key back.
    # Deployment inventory (DB aggregation) — short TTL; cheap indexed queries.
    "deployment_inventory": 60,   # 1 min
    # Password Safe database import candidates (api/cloud_databases.py). Four paged
    # collections per miss, so worth caching; 5 min because Password Safe discovery
    # runs on an hourly-plus schedule and nothing here changes faster than that.
    # Keyed on the workgroup filter — see SCOPED_CACHES in tests/test_cache_key_scoping.py.
    "ps_db_candidates":    300,   # 5 min
    # Asset listing on a share reached through a remote agent. Not a cloud API call but a
    # full agent job round trip — the agent polls every 5s, so an uncached listing costs
    # five to fifteen seconds. storage_service.list_all_assets fans out over every
    # configured backend and feeds the asset pickers on six pages, so without this every
    # one of those pickers would sit on that round trip.
    #
    # Keyed on agent + share + subpath, never globally: two shares behind one dashboard
    # would otherwise serve each other's listings for a full TTL. See SCOPED_CACHES in
    # tests/test_cache_key_scoping.py.
    "agent_storage_list":  120,   # 2 min
}

# How long past its TTL an entry stays SERVABLE to get_or_refresh. Additive and global,
# not a per-key table and not a multiplier:
#   * a second dict parallel to TTL is two numbers per key that must agree — the drift
#     shape tests/test_cache_warmer_parity.py exists to prevent;
#   * a multiplier scales with the TTL, which is the wrong axis. The grace only has to
#     cover HOW LONG A REFRESH CAN TAKE, and that is deadline-shaped: cloud_executor's
#     request-path budget is 60s. 300s covers two full-length failed attempts plus slack.
# So worst-case staleness is `ttl + 300` uniformly, which is one sentence rather than a
# table. A key that genuinely needs its own window passes `hard_ttl=` at the call site.
#
# The cost, stated plainly: a permanently-failing refresh now shows a stale number for up
# to ttl+300 where it used to show an unavailable tile. _refresh_task logs a WARNING on
# every failed pass, and the grace is bounded, which is what makes that trade acceptable.
STALE_GRACE_S = 300

# ── Internal store ────────────────────────────────────────────────────────────
_store: dict = {}
_lock: asyncio.Lock = asyncio.Lock()
_inflight: set = set()  # cache keys with a background refresh task in progress
_pending: dict[str, asyncio.Future] = {}  # cache keys with a synchronous fetch in progress


# ── Connection management (no-ops — kept for interface compatibility) ─────────

async def ping() -> bool:
    """Always returns True — in-memory cache has no external dependency."""
    return True


async def close_redis() -> None:
    """No-op — kept so main.py lifespan shutdown requires no changes."""
    pass


# ── Key construction ──────────────────────────────────────────────────────────

def key_global(name: str) -> str:
    """Cache key for data that is not user-specific (AWS, images, etc.)."""
    return f"vmcli:{name}"


def key_workgroups(name: str, workgroups: list) -> str:
    """
    Cache key scoped to a user's workgroup set.
    Workgroups are sorted so [Hydra, Weaverlab] == [Weaverlab, Hydra].
    """
    wg_part = ",".join(sorted(str(w) for w in workgroups))
    return f"vmcli:{name}:{wg_part}"


def key_param(name: str, **params) -> str:
    """Cache key parameterised by simple values (e.g. os_filter)."""
    param_part = ":".join(f"{k}={v}" for k, v in sorted(params.items()))
    return f"vmcli:{name}:{param_part}"


# ── Low-level get / set / delete ──────────────────────────────────────────────

async def get(cache_key: str) -> Optional[dict]:
    """
    Return the stored envelope {"data": ..., "cached_at": "ISO"} or None.
    Returns None (and evicts the entry) if the TTL has expired.

    STRICT TTL, deliberately. Eleven call sites in api/gcp.py and api/oci.py serve this
    return value directly with no refresh path of any kind, so relaxing the check here
    would silently serve them stale data forever. Stale tolerance belongs to
    ``_get_entry``/``get_or_refresh``, which have somewhere to put the refresh.
    """
    async with _lock:
        entry = _store.get(cache_key)

    if entry is None:
        return None

    if monotonic() >= entry["_expires_at"]:
        async with _lock:
            _store.pop(cache_key, None)
        return None

    return {"data": entry["data"], "cached_at": entry["cached_at"]}


async def _get_entry(cache_key: str) -> Optional[dict]:
    """Stale-tolerant read for ``get_or_refresh`` only.

    Returns ``{"data", "cached_at", "stale"}``, or None once the HARD expiry has passed.
    ``stale`` is True between the soft and hard expiries: serve it, and refresh behind it.

    ``stale`` is computed from ``monotonic()`` alone. The previous code decided staleness by
    comparing ``_age_seconds(cached_at)`` — a WALL-CLOCK age — against the ttl, while
    eviction used ``monotonic()``. Two clocks meant an NTP step could flip the branch, so
    the behaviour was not just wrong but nondeterministic. One clock decides.
    """
    async with _lock:
        entry = _store.get(cache_key)

    if entry is None:
        return None

    now = monotonic()
    # Entries written before this field existed cannot occur (the store is process-local
    # and dies with the process), but defaulting keeps a partially-reloaded module safe.
    if now >= entry.get("_hard_expires_at", entry["_expires_at"]):
        async with _lock:
            _store.pop(cache_key, None)
        return None

    return {"data": entry["data"], "cached_at": entry["cached_at"],
            "stale": now >= entry["_expires_at"]}


async def set(cache_key: str, payload: Any, ttl: int, *, hard_ttl: Optional[int] = None) -> None:
    """Store payload with a TTL and a cached_at timestamp.

    ``hard_ttl`` is how long the entry stays servable-while-stale to ``get_or_refresh``;
    it defaults to ``ttl + STALE_GRACE_S``.

    It is KEYWORD-ONLY with a default, and that is load-bearing rather than stylistic:
    ``tests/test_cache_warmer_parity.py`` monkeypatches this function with a fake declared
    ``async def _fake_set(key, payload, ttl)``. A required or positional fourth parameter
    breaks that fake and reds out five tests in that file.
    """
    now = monotonic()
    entry = {
        "data": payload,
        "cached_at": datetime.now(timezone.utc).isoformat(),
        "_expires_at": now + ttl,
        # Soft and hard must not be the same instant — see the module docstring. When they
        # were, get_or_refresh's stale-serve branch was unreachable.
        "_hard_expires_at": now + (ttl + STALE_GRACE_S if hard_ttl is None else hard_ttl),
        "_ttl": ttl,
    }
    async with _lock:
        _store[cache_key] = entry
    logger.debug("cache set key=%s ttl=%ds", cache_key, ttl)


async def invalidate(cache_key: str) -> None:
    """Delete one cache entry immediately."""
    async with _lock:
        _store.pop(cache_key, None)
    logger.debug("cache invalidated key=%s", cache_key)


async def invalidate_prefix(prefix: str) -> int:
    """Delete all keys matching vmcli:<prefix>:* — use after mutations."""
    pattern = f"vmcli:{prefix}:"
    async with _lock:
        keys = [k for k in _store if k.startswith(pattern)]
        for k in keys:
            del _store[k]
    if keys:
        logger.debug("cache invalidated %d key(s) prefix=%s", len(keys), prefix)
    return len(keys)


# ── Stale-while-revalidate primitive ─────────────────────────────────────────

async def get_or_refresh(
    cache_key: str,
    ttl: int,
    fetcher: Callable[[], Coroutine[Any, Any, Any]],
    background: bool = True,
) -> tuple:
    """
    Core stale-while-revalidate helper.

    Returns (payload, cached_at_iso_string).

    Behaviour:
      - Cache HIT, fresh      → return immediately, no background work.
      - Cache HIT, stale      → return stale data immediately + fire background
                                refresh so next request gets fresh data.
      - Cache MISS            → fetch synchronously, store, return.

    "Stale" means past the soft expiry but inside STALE_GRACE_S; past that it is a MISS.
    Reads through ``_get_entry``, not ``get()`` — ``get()`` evicts at the soft expiry, which
    is what made this branch unreachable and turned every TTL boundary into a blocking
    cloud call on a user's request.

    Set background=False to force a synchronous refresh (used by the scheduled
    warmers — they are already running in the background).
    """
    cached = await _get_entry(cache_key)

    if cached is not None:
        cached_at = cached.get("cached_at", "")

        if not cached["stale"]:
            return cached["data"], cached_at

        # Stale — return immediately and refresh asynchronously
        if background:
            # Claim BEFORE create_task, not inside _refresh_task. The task body does not
            # run until the loop next schedules it, so a check-then-create here let N
            # concurrent stale readers each spawn a refresh for the same key — N cloud
            # calls and N sessions off one page load. Harmless while this branch was dead;
            # a fan-out amplifier the moment it is live.
            if cache_key not in _inflight:
                _inflight.add(cache_key)
                asyncio.create_task(_refresh_task(cache_key, ttl, fetcher, claimed=True))
        else:
            await _refresh_task(cache_key, ttl, fetcher)
        return cached["data"], cached_at

    # Cache miss — coalesce concurrent fetches so only one Automation job fires
    if cache_key in _pending:
        logger.debug("cache miss coalesced key=%s (waiting on existing fetch)", cache_key)
        data = await _pending[cache_key]
        # Re-read cached_at from the store (set by the first fetcher)
        cached = await get(cache_key)
        return data, (cached["cached_at"] if cached else datetime.now(timezone.utc).isoformat())

    loop = asyncio.get_running_loop()
    future: asyncio.Future = loop.create_future()
    _pending[cache_key] = future
    try:
        data = await fetcher()
        await set(cache_key, data, ttl)
        future.set_result(data)
        return data, datetime.now(timezone.utc).isoformat()
    except Exception as exc:
        future.set_exception(exc)
        raise
    finally:
        _pending.pop(cache_key, None)


async def _refresh_task(
    cache_key: str,
    ttl: int,
    fetcher: Callable[[], Coroutine[Any, Any, Any]],
    claimed: bool = False,
) -> None:
    """Run fetcher and update cache. Swallows all exceptions.

    ``claimed=True`` means the caller already added the key to ``_inflight`` (it must, to
    close the check-then-create race — see get_or_refresh). Either way the key is
    discarded here, so ownership of the release stays in one place.
    """
    if not claimed:
        _inflight.add(cache_key)
    try:
        data = await fetcher()
        await set(cache_key, data, ttl)
        logger.debug("cache refreshed key=%s", cache_key)
    except Exception as exc:
        logger.warning("cache refresh failed key=%s: %s", cache_key, exc)
    finally:
        _inflight.discard(cache_key)


# ── Cache status (for /api/cache/status) ─────────────────────────────────────

async def all_entries() -> list:
    """Return metadata for every vmcli:* key in the in-memory store."""
    now = monotonic()
    async with _lock:
        snapshot = list(_store.items())

    result = []
    for k, entry in sorted(snapshot):
        if not k.startswith("vmcli:"):
            continue
        ttl_remaining = max(0.0, entry["_expires_at"] - now)
        hard_expires_at = entry.get("_hard_expires_at", entry["_expires_at"])
        result.append({
            "key": k,
            "cached_at": entry["cached_at"],
            "ttl_remaining_s": round(ttl_remaining, 1),
            "age_s": round(_age_seconds(entry["cached_at"]), 1),
            # Past its TTL but still being served while a refresh runs behind it. This is
            # the only place that state is visible, and it is what you want the first time
            # someone reports a number that will not move.
            "stale": now >= entry["_expires_at"],
            "hard_ttl_remaining_s": round(max(0.0, hard_expires_at - now), 1),
            "refresh_in_flight": k in _inflight,
        })
    return result


# ── Helpers ───────────────────────────────────────────────────────────────────

def _age_seconds(iso_str: str) -> float:
    """Return elapsed seconds since the given UTC ISO timestamp."""
    if not iso_str:
        return float("inf")
    try:
        ts = datetime.fromisoformat(iso_str)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - ts).total_seconds()
    except Exception:
        return float("inf")
