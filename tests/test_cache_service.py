"""cache_service really does serve stale data while it refreshes behind it.

There was no test file for this module at all, which is how the headline behaviour came to
be dead code without anyone noticing.

``set()`` stored ``cached_at`` (wall clock) AND ``_expires_at = monotonic() + ttl``. ``get()``
returns None **and evicts** once ``monotonic() >= _expires_at``. ``get_or_refresh`` read
through ``get()`` and then took its "serve stale, refresh in background" branch only when
``_age_seconds(cached_at) >= ttl`` — which is the same instant ``get()`` had already evicted
at. So the branch never ran, and **every TTL boundary was a synchronous fetch on a user's
request**: a full cloud round-trip in the request path, on a 60s TTL, for the dashboard's
instance tiles. Measured against the pre-fix module, a caller arriving one tick after
expiry blocked for the fetcher's entire duration and received the *refetched* value; a
working stale-serve returns the cached value in ~0 ms.

Two clocks were also involved, which made it worse than merely wrong: an NTP step between
``set`` and ``get`` could make ``age >= ttl`` true while ``_expires_at`` had not elapsed, so
the branch fired occasionally and nondeterministically. Staleness is now decided by
``monotonic()`` alone.

The fix is a SOFT expiry (refresh me) plus a HARD one (stop serving me), with stale
tolerance in ``_get_entry`` — used only by ``get_or_refresh``. ``get()`` keeps its strict
TTL, and test_get_stays_strict below is the guard on that: eleven bare ``get()`` call sites
in api/gcp.py and api/oci.py have no refresh path whatsoever, so a relaxed ``get()`` would
serve them stale data forever.

cache_service imports only the stdlib and has no relative imports, so this loads the real
module by path with no fakes and no app import.

Run: python tests/test_cache_service.py   (or under pytest)
"""
import ast
import asyncio
import importlib.util
import inspect
import os
import sys
import textwrap
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MOD_PATH = os.path.join(_ROOT, "web_dashboard", "services", "cache_service.py")

# Short enough to keep the suite fast, long enough that scheduling jitter cannot be
# mistaken for a blocking fetch.
TTL = 0.30
FETCH_S = 0.60


def _fresh_module():
    """A pristine copy of the module — the store is global, so every test needs its own."""
    spec = importlib.util.spec_from_file_location("cache_service_under_test", _MOD_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run(coro_fn):
    return asyncio.run(coro_fn())


def _code_of(fn):
    """``fn``'s source with its docstring and comments removed.

    A "this name no longer appears" assertion over raw source also matches the docstring
    that explains why the name was removed — so a correct fix fails its own test. That has
    bitten the cost-cache tests and this file's first draft. Strip the prose, then assert.
    """
    src = inspect.getsource(fn)
    tree = ast.parse(textwrap.dedent(src))
    node = tree.body[0]
    if (node.body and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)):
        node.body = node.body[1:]          # drop the docstring
    return ast.unparse(node)               # unparse never emits comments


# ── the headline ──────────────────────────────────────────────────────────────

def test_a_stale_entry_is_served_immediately_and_refreshed_behind_it():
    cs = _fresh_module()
    calls = []

    async def slow_fetcher():
        calls.append(1)
        await asyncio.sleep(FETCH_S)
        return f"DATA-{len(calls)}"

    async def body():
        await cs.set("k", "DATA-0", TTL)
        await asyncio.sleep(TTL + 0.05)          # past the soft expiry, inside the grace

        t0 = time.monotonic()
        data, _ = await cs.get_or_refresh("k", TTL, slow_fetcher)
        waited = time.monotonic() - t0

        assert data == "DATA-0", (
            f"expected the cached value while the refresh runs, got {data!r} — the caller "
            "was made to wait for a fresh fetch, which is the dead-branch behaviour")
        assert waited < FETCH_S / 2, (
            f"caller blocked {waited:.2f}s on a {FETCH_S:.2f}s fetch: it is fetching "
            "synchronously, not serving stale")
        await asyncio.sleep(0)   # let the scheduled refresh actually start
        assert calls, "no background refresh was started"

        # The refresh lands, and the next caller sees it.
        await asyncio.sleep(FETCH_S + 0.15)
        data2, _ = await cs.get_or_refresh("k", TTL, slow_fetcher)
        assert data2 == "DATA-1", f"the background refresh never reached the store: {data2!r}"

    _run(body)


def test_a_fresh_entry_starts_no_work_at_all():
    cs = _fresh_module()
    calls = []

    async def fetcher():
        calls.append(1)
        return "NEW"

    async def body():
        await cs.set("k", "OLD", TTL)
        data, _ = await cs.get_or_refresh("k", TTL, fetcher)
        assert data == "OLD"
        assert not calls, "a fresh entry must not trigger a refresh"

    _run(body)


# ── the bounds ────────────────────────────────────────────────────────────────

def test_past_the_hard_expiry_it_is_a_miss_not_an_unbounded_stale_serve():
    cs = _fresh_module()

    async def fetcher():
        return "NEW"

    async def body():
        # hard_ttl is the escape hatch for a key that needs its own window; used here to
        # reach the hard boundary without sleeping out the 300s default.
        await cs.set("k", "OLD", TTL, hard_ttl=TTL)
        await asyncio.sleep(TTL + 0.05)
        data, _ = await cs.get_or_refresh("k", TTL, fetcher)
        assert data == "NEW", (
            "past the hard expiry the entry must be gone and the fetch synchronous; "
            "serving 'OLD' here would mean staleness has no upper bound")

    _run(body)


def test_stale_grace_is_bounded_and_covers_a_refresh():
    cs = _fresh_module()
    grace = cs.STALE_GRACE_S
    assert grace > 0, "a zero grace re-creates the dead branch — soft and hard coincide again"
    # It only has to outlast a refresh attempt. cloud_executor's request-path deadline is
    # 60s and tests/test_cloud_executor.py caps it at 120s.
    assert 120 <= grace <= 1800, (
        f"STALE_GRACE_S={grace} — it must exceed cloud_executor's request-path deadline so "
        "a refresh can finish inside it, and stay small enough to bound worst-case "
        "staleness at ttl+grace")


def test_a_failing_refresh_keeps_serving_the_last_good_value():
    cs = _fresh_module()
    attempts = []

    async def broken():
        attempts.append(1)
        raise RuntimeError("upstream is down")

    async def body():
        await cs.set("k", "GOOD", TTL)
        await asyncio.sleep(TTL + 0.05)
        data, _ = await cs.get_or_refresh("k", TTL, broken)
        assert data == "GOOD", "a stale read must not raise the refresh's error at the caller"
        await asyncio.sleep(0.10)
        assert attempts, "the refresh never ran"
        # A failed refresh must not evict — otherwise one bad upstream turns a servable
        # stale number into an unavailable tile.
        data2, _ = await cs.get_or_refresh("k", TTL, broken)
        assert data2 == "GOOD", "a failed refresh evicted the last good value"

    _run(body)


# ── single-flight ─────────────────────────────────────────────────────────────

def test_concurrent_stale_reads_start_exactly_one_refresh():
    cs = _fresh_module()
    calls = []

    async def slow_fetcher():
        calls.append(1)
        await asyncio.sleep(FETCH_S)
        return "NEW"

    async def body():
        await cs.set("k", "OLD", TTL)
        await asyncio.sleep(TTL + 0.05)
        results = await asyncio.gather(*[
            cs.get_or_refresh("k", TTL, slow_fetcher) for _ in range(8)])
        assert all(r[0] == "OLD" for r in results), (
            f"every concurrent stale reader should get the cached value, got "
            f"{[r[0] for r in results]} — they were each made to fetch")
        await asyncio.sleep(0.05)
        assert len(calls) == 1, (
            f"{len(calls)} refreshes for one key. The _inflight claim must happen before "
            "create_task — a task body does not run until the loop schedules it, so a "
            "check-then-create lets every concurrent reader spawn its own cloud call")

    _run(body)


def test_concurrent_cold_misses_are_coalesced():
    cs = _fresh_module()
    calls = []

    async def slow_fetcher():
        calls.append(1)
        await asyncio.sleep(FETCH_S / 2)
        return "NEW"

    async def body():
        results = await asyncio.gather(*[
            cs.get_or_refresh("cold", TTL, slow_fetcher) for _ in range(6)])
        assert all(r[0] == "NEW" for r in results), (
            f"a cold miss must fetch and return the new value, got {[r[0] for r in results]}")
        assert len(calls) == 1, f"cold-miss coalescing broke: {len(calls)} fetches"

    _run(body)


# ── the guard on get() ────────────────────────────────────────────────────────

def test_get_stays_strict():
    cs = _fresh_module()

    async def body():
        await cs.set("k", "OLD", TTL)
        await asyncio.sleep(TTL + 0.05)          # soft-expired, still inside the grace
        assert await cs.get("k") is None, (
            "get() must keep its STRICT TTL. Eleven call sites in api/gcp.py and "
            "api/oci.py serve get()'s return value with NO refresh path at all — if get() "
            "starts serving stale entries, every one of them serves up to ttl+grace old "
            "data forever. Stale tolerance belongs to _get_entry/get_or_refresh, which "
            "have somewhere to put the refresh")

    _run(body)


def test_get_entry_sees_what_get_will_not():
    cs = _fresh_module()

    async def body():
        await cs.set("k", "OLD", TTL)
        await asyncio.sleep(TTL + 0.05)
        entry = await cs._get_entry("k")
        assert entry is not None, "the stale-tolerant reader lost the entry"
        assert entry["stale"] is True, "a soft-expired entry must report itself stale"
        assert entry["data"] == "OLD"

    _run(body)


def test_staleness_is_decided_by_one_clock():
    # The old code compared a wall-clock age against the ttl while eviction used
    # monotonic(), so an NTP step could flip the branch. `cached_at` must not be an input
    # to the decision any more.
    cs = _fresh_module()
    src = _code_of(cs._get_entry)
    assert "_age_seconds" not in src, (
        "_get_entry decides staleness from the wall-clock cached_at again. Two clocks made "
        "this branch fire nondeterministically on a container clock step")
    assert "monotonic()" in src, "_get_entry must decide staleness from monotonic()"

    body = _code_of(cs.get_or_refresh)
    assert "_age_seconds" not in body, (
        "get_or_refresh is back to comparing a wall-clock age against the ttl; branch on "
        "_get_entry's `stale` flag instead")


# ── the compatibility constraint ──────────────────────────────────────────────

def test_set_is_still_callable_with_three_positional_arguments():
    cs = _fresh_module()
    sig = inspect.signature(cs.set)
    positional = [p for p in sig.parameters.values()
                  if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    assert len(positional) == 3, (
        f"set() takes {len(positional)} positional parameters. "
        "tests/test_cache_warmer_parity.py monkeypatches it with a fake declared "
        "`async def _fake_set(key, payload, ttl)`; a positional fourth parameter breaks "
        "that fake and reds out five tests there. Keep extras keyword-only with defaults")
    for name, p in sig.parameters.items():
        if name not in ("cache_key", "payload", "ttl"):
            assert p.kind == p.KEYWORD_ONLY and p.default is not p.empty, (
                f"set() parameter {name!r} must be keyword-only with a default")

    async def body():
        await cs.set("k", "v", 5)                # exactly how the warmers call it
        assert (await cs.get("k"))["data"] == "v"

    _run(body)


def test_all_entries_reports_the_new_stale_state():
    cs = _fresh_module()

    async def body():
        await cs.set("vmcli:x", "v", TTL)
        await asyncio.sleep(TTL + 0.05)
        rows = await cs.all_entries()
        assert rows, "all_entries lost the stale entry"
        row = rows[0]
        # /api/cache/status is the only place this state is visible.
        for field in ("stale", "hard_ttl_remaining_s", "refresh_in_flight"):
            assert field in row, f"all_entries no longer reports {field!r}"
        assert row["stale"] is True
        assert row["ttl_remaining_s"] == 0.0
        assert row["hard_ttl_remaining_s"] > 0

    _run(body)


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
    sys.exit(1 if failures else 0)
