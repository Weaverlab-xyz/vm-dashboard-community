"""Sweeper loops must run their database pass OFF the event loop.

``web_dashboard/main.py`` runs six enqueue-only sweeper loops inside the gunicorn app
process (``-w 2``). Each one opens a session, calls a synchronous ``enqueue_*`` function,
and sleeps. That call is not cheap and it is not merely a query: ``spend_sweeper`` and
``suspend_sweeper`` take ``pg_advisory_xact_lock`` first, which BLOCKS until the lock is
granted, and then run two SELECTs and an INSERT. Awaited directly, that stalls every HTTP
request the worker is serving.

Four of the six had this right — ``asyncio.to_thread`` — and two did not. Both of the two
carried a docstring saying "Same shape and the same reasoning as ``_expiry_sweeper_loop``",
which is the loop that does it correctly, so the claim of parity was in the source while the
parity itself was not. Nothing failed: the app served requests slightly slower, only while
the feature was switched on, and only on the tick.

So these tests pin the property structurally AND behaviourally, in the same spirit as
``tests/test_cache_warmer_parity.py`` — which exists because warmer↔reader drift was also
silent. The behavioural one is the one that bites: it runs a real iteration and checks which
thread the pass landed on.

Run: python tests/test_sweeper_loop_parity.py   (or under pytest)
"""
import ast
import asyncio
import logging
import os
import sys
import threading

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

os.environ.setdefault("JWT_SECRET_KEY", "test-secret-sweeper-parity")

try:
    from web_dashboard import main
except Exception as exc:  # pragma: no cover — deps absent outside CI
    try:
        import pytest
        pytest.skip(f"dashboard import unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)

_SRC = open(os.path.join(_ROOT, "web_dashboard/main.py"), encoding="utf-8").read()
_TREE = ast.parse(_SRC)

# The six enqueue-only sweepers. Named rather than pattern-matched: a new one should have
# to be added here deliberately, which is the moment to ask whether it needs its own loop.
SWEEPERS = (
    "_ci_sweeper_loop",
    "_spend_sweeper_loop",
    "_suspend_sweeper_loop",
    "_expiry_sweeper_loop",
    "_pov_reconcile_loop",
    "_hypervisor_sync_loop",
)

# Coroutines that legitimately hand-roll `while True`. The two warm primitives are the
# analogous abstraction for cache warmers; the three self-looping warmers predate it and
# are out of scope here — they await async fetchers, so they do not block anything.
MAY_HAND_ROLL = {
    "_sweeper_loop", "_warm_loop", "_warm_scoped_loop",
    "_warm_cost_summary", "_warm_dashboard_stats", "_warm_portainer_containers",
}


def _fn(name):
    for node in ast.walk(_TREE):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in main.py")


# ── 1. The behavioural one: the pass must not run on the event loop ───────────

def test_the_pass_runs_off_the_event_loop():
    """The property itself, not a proxy for it.

    A `work` that records its thread, driven through one real iteration of the primitive.
    Against the pre-refactor `_spend_sweeper_loop` this is the assertion that fails.
    """
    seen = {}

    def work(db):
        seen["work_thread"] = threading.get_ident()

    def interval():
        # Long enough that the loop parks in `sleep` and the test can cancel it there,
        # rather than racing a second iteration.
        return 3600

    async def drive():
        seen["loop_thread"] = threading.get_ident()
        task = asyncio.ensure_future(
            main._sweeper_loop("parity probe", work, interval, fallback=1))
        # Yield until the first pass has been recorded, then stop. Bounded so a broken
        # primitive fails the assertion below instead of hanging CI.
        for _ in range(200):
            if "work_thread" in seen:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(drive())

    assert "work_thread" in seen, "the primitive never ran the pass"
    assert seen["work_thread"] != seen["loop_thread"], (
        "the sweep ran ON the event loop thread — it must go through asyncio.to_thread, "
        "or a blocking pg_advisory_xact_lock stalls every request this worker is serving")


def test_a_failing_pass_does_not_kill_the_loop():
    """A sweep that raises must be logged and retried next tick, not end the loop.

    Every one of the six is launched once at startup and never restarted, so an escaping
    exception silently disables that feature until the process is bounced.
    """
    calls = {"n": 0}
    # The primitive logs each failed pass at WARNING, which is the behaviour under test;
    # muted here so a passing run does not print four scary tracebacks into CI.
    logging.getLogger("web_dashboard.main").setLevel(logging.ERROR)

    def work(db):
        calls["n"] += 1
        raise RuntimeError("sweep exploded")

    def interval():
        return 0  # come straight back round

    async def drive():
        task = asyncio.ensure_future(
            main._sweeper_loop("parity probe", work, interval, fallback=1))
        for _ in range(200):
            if calls["n"] >= 2:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(drive())
    assert calls["n"] >= 2, f"loop stopped after a failing pass (ran {calls['n']}x)"


def test_a_broken_interval_falls_back_instead_of_raising():
    """`interval_fn` reads live config so a Settings change lands without a restart — which
    also means it can raise. It must degrade to the fallback, not kill the loop."""
    calls = {"n": 0}

    def work(db):
        calls["n"] += 1

    def interval():
        raise RuntimeError("config unreadable")

    async def drive():
        task = asyncio.ensure_future(
            main._sweeper_loop("parity probe", work, interval, fallback=0))
        for _ in range(200):
            if calls["n"] >= 2:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(drive())
    assert calls["n"] >= 2, "a raising interval_fn stopped the loop"


# ── 2. Structural: every sweeper goes through the primitive ───────────────────

def test_every_sweeper_delegates_to_the_primitive():
    """No sweeper may hand-roll the loop, because hand-rolling is how both bugs happened.

    Delegating IS the fix: `asyncio.to_thread` lives inside `_sweeper_loop`, so a loop that
    goes through it cannot forget.
    """
    offenders = []
    for name in SWEEPERS:
        fn = _fn(name)
        if any(isinstance(n, ast.While) for n in ast.walk(fn)):
            offenders.append(f"{name} hand-rolls `while True`")
        calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Name) and n.func.id == "_sweeper_loop"]
        if not calls:
            offenders.append(f"{name} does not call _sweeper_loop")
    assert not offenders, "; ".join(offenders)


def test_only_the_primitives_hand_roll_a_loop():
    """Catches a SEVENTH sweeper added beside the six rather than through them."""
    offenders = []
    for node in ast.walk(_TREE):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        if node.name in MAY_HAND_ROLL:
            continue
        if any(isinstance(n, ast.While) for n in ast.walk(node)):
            offenders.append(node.name)
    assert not offenders, (
        "these coroutines hand-roll a loop instead of using a primitive: "
        f"{offenders} — add to MAY_HAND_ROLL only with a reason")


def _own_frame(node):
    """Walk `node`, but do NOT descend into a nested plain ``def``.

    A nested sync function handed to ``asyncio.to_thread`` runs in a worker thread, so an
    ``enqueue_*`` inside one is the CORRECT pattern rather than a violation — which is
    exactly what each sweeper's ``work`` closure is. Only what executes in the coroutine's
    own frame is on the event loop.
    """
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.FunctionDef):
            continue
        yield child
        yield from _own_frame(child)


def test_no_enqueue_runs_directly_inside_a_coroutine():
    """The assertion that would have caught the original bug where it was written.

    An `enqueue_*` call executing in an `async def`'s own frame — rather than inside a
    closure the primitive hands to `asyncio.to_thread` — is synchronous database work on
    the event loop.
    """
    offenders = []
    for node in ast.walk(_TREE):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        own = list(_own_frame(node))

        # Directly-wrapped form: asyncio.to_thread(fn, db) names the callable, so exempt it.
        wrapped = {ast.unparse(c.args[0])
                   for c in own
                   if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                   and c.func.attr == "to_thread" and c.args}

        for call in [n for n in own if isinstance(n, ast.Call)]:
            f = call.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
            if name.startswith("enqueue") and ast.unparse(f) not in wrapped:
                offenders.append(
                    f"{node.name} line {call.lineno}: {ast.unparse(f)}(...)")

        # Closes the hole the exemption above would otherwise open: a closure holding the
        # enqueue is only safe while it is PASSED to the primitive. Called directly, it is
        # back on the event loop with the nesting hiding it from the check above.
        closures = {c.name for c in ast.iter_child_nodes(node)
                    if isinstance(c, ast.FunctionDef)}
        for call in [n for n in own if isinstance(n, ast.Call)]:
            if isinstance(call.func, ast.Name) and call.func.id in closures:
                offenders.append(
                    f"{node.name} line {call.lineno}: calls its own {call.func.id}() "
                    f"instead of handing it to the primitive")

    assert not offenders, (
        "synchronous enqueue work on the event loop: " + "; ".join(offenders))


# ── 3. The refactor must not re-time anything ─────────────────────────────────

# What each loop falls back to when its live interval read fails. Pinned because a refactor
# that quietly changed one would change how often a sweep runs in production, and nothing
# else in the tree would notice.
EXPECTED_FALLBACKS = {
    "_ci_sweeper_loop": 60 * 60,
    "_expiry_sweeper_loop": 30 * 60,
    "_hypervisor_sync_loop": 300,
    "_suspend_sweeper_loop": 600,
    "_spend_sweeper_loop": 600,
}


def test_the_fallback_cadence_is_unchanged():
    """`_pov_reconcile_loop` is absent deliberately: its fallback is
    `pov_reconcile.DEFAULT_INTERVAL_S`, a named constant rather than a literal, and pinning
    a copy of it here would be the drift this file exists to prevent."""
    wrong = []
    for name, expected in EXPECTED_FALLBACKS.items():
        fn = _fn(name)
        found = None
        for call in [n for n in ast.walk(fn) if isinstance(n, ast.Call)]:
            if isinstance(call.func, ast.Name) and call.func.id == "_sweeper_loop":
                for kw in call.keywords:
                    if kw.arg == "fallback":
                        found = eval(compile(ast.Expression(kw.value), "<f>", "eval"))
        if found != expected:
            wrong.append(f"{name}: fallback {found!r}, expected {expected!r}")
    assert not wrong, "; ".join(wrong)


def _run():
    tests = [(n, o) for n, o in sorted(globals().items())
             if n.startswith("test_") and callable(o)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"ok   {name}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {name}: {exc}")
        except Exception as exc:                       # noqa: BLE001
            failed += 1
            print(f"ERROR {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run())
