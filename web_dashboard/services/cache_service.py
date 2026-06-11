"""
In-memory cache service — stale-while-revalidate helpers.

Drop-in replacement for the previous Redis-backed implementation.
All data is stored in a process-local dict protected by asyncio.Lock.
In-memory lookups are ~1000x faster than Redis (no network hop or
serialization overhead), and the cache warmers already repopulate on
every startup, so no data is lost when a container restarts.

Public interface is identical to the Redis version — no callers change.
"""
import asyncio
import logging
from datetime import datetime, timezone
from time import monotonic
from typing import Any, Callable, Coroutine, Optional

logger = logging.getLogger(__name__)


# ── TTL constants (seconds) ───────────────────────────────────────────────────
TTL = {
    # PowerShell-backed
    "vms_running":         1800,  # 30 min — now only triggered by explicit user action
    "images_ovas":         300,   # 5 min
    "images_isos":         600,   # 10 min
    # AWS / boto3-backed
    "aws_amis":            300,   # 5 min
    "aws_network_opts":    600,   # 10 min
    "aws_instances":        60,   # 1 min
    "aws_community":       900,   # 15 min
    "aws_ssh_key_secrets": 300,   # 5 min
    "aws_db_options":      600,   # 10 min — RDS subnet groups + SGs for the DB provision form
    # Config management
    "cfgmgmt_instances":    60,   # 1 min
    "cfgmgmt_s3status":    120,   # 2 min
    # Azure / azure-sdk-backed
    "azure_images":        300,   # 5 min
    "azure_network_opts":  600,   # 10 min
    "azure_vms":            60,   # 1 min
    "azure_marketplace":   900,   # 15 min
    # Portainer (proxied via Hybrid Worker in cloud mode)
    "portainer_endpoints": 300,   # 5 min
    "portainer_containers": 60,   # 1 min
    "portainer_stacks":    120,   # 2 min
}

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


async def set(cache_key: str, payload: Any, ttl: int) -> None:
    """Store payload with a TTL and a cached_at timestamp."""
    entry = {
        "data": payload,
        "cached_at": datetime.now(timezone.utc).isoformat(),
        "_expires_at": monotonic() + ttl,
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
      - Cache HIT, age < ttl  → return immediately, no background work.
      - Cache HIT, age >= ttl → return stale data immediately + fire background
                                refresh so next request gets fresh data.
      - Cache MISS            → fetch synchronously, store, return.

    Set background=False to force a synchronous refresh (used by the scheduled
    warmers — they are already running in the background).
    """
    cached = await get(cache_key)

    if cached is not None:
        cached_at = cached.get("cached_at", "")
        age = _age_seconds(cached_at)

        if age < ttl:
            return cached["data"], cached_at

        # Stale — return immediately and refresh asynchronously
        if background:
            if cache_key not in _inflight:
                asyncio.create_task(_refresh_task(cache_key, ttl, fetcher))
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
) -> None:
    """Run fetcher and update cache. Swallows all exceptions."""
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
        result.append({
            "key": k,
            "cached_at": entry["cached_at"],
            "ttl_remaining_s": round(ttl_remaining, 1),
            "age_s": round(_age_seconds(entry["cached_at"]), 1),
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
