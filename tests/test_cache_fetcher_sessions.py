"""No get_or_refresh fetcher may close over a request-scoped DB session.

``get_or_refresh`` can run its fetcher as a **detached background task** that outlives the
request (that is the whole point of stale-while-revalidate). A fetcher written as
``lambda: _fetch_instances(db)``, where ``db`` came from ``Depends(get_db)``, therefore runs
against a Session the request has already closed. Two interleavings, neither benign:

* **The task starts after the request ends → a silent pooled-connection leak.**
  ``Session.close()`` releases the connection and detaches instances, but the Session stays
  *usable*. The next ``db.query(...)`` quietly checks out a FRESH connection from the pool
  that nothing ever returns; it comes back only when the Session is garbage-collected. The
  pool is ``pool_size=5 + max_overflow=5`` per process and the home page fans out ~22
  concurrent requests, so this is the ``QueuePool limit of size 5 overflow 5 reached``
  failure — and it presents as "the cache refreshed correctly", which is why it is hard to
  attribute.
* **The task starts while the request is still in flight → ``IllegalStateChangeError``.**
  ``asyncio.create_task`` does not run the body immediately; the next ``await`` in the
  handler yields, the refresh begins querying on the still-open request Session, and
  SQLAlchemy 2.0 guards concurrent Session use. That can surface out of ``get_db``'s
  ``finally`` — a 500 on a response already computed.

``_refresh_task`` swallows every exception, so neither shows up as an error anyone reads.

The correct shape is a fetcher that opens and closes its **own** ``SessionLocal()``; see
``api/inventory.py``, whose comment describes this exact hazard, and the ``*_fresh()``
helpers in ``api/aws.py`` / ``api/azure.py``.

This is a static sweep, not a behavioural test: it needs no DB, no app import and no cloud
stubs, and it covers every call site including ones written after today. It is the only
thing standing between the fix and the next person writing the same lambda.

Run: python tests/test_cache_fetcher_sessions.py   (or under pytest)
"""
import ast
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PKG = os.path.join(_ROOT, "web_dashboard")


def _py_files():
    for base, dirs, files in os.walk(_PKG):
        dirs[:] = [d for d in dirs if d not in ("__pycache__", "static", "templates")]
        for name in sorted(files):
            if name.endswith(".py"):
                yield os.path.join(base, name)


def _rel(path):
    return os.path.relpath(path, _ROOT).replace(os.sep, "/")


def _session_params(fn):
    """Names of ``fn``'s parameters that hold a request-scoped Session.

    Two signals, either sufficient: a ``Depends(get_db)`` default, or a ``Session``
    annotation. The second catches a helper that takes ``db: Session`` without declaring the
    dependency itself.
    """
    names = set()
    args = fn.args
    slots = list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs)
    defaults = ([None] * (len(args.posonlyargs) + len(args.args) - len(args.defaults))
                + list(args.defaults) + list(args.kw_defaults))

    for arg, default in zip(slots, defaults):
        ann = getattr(arg, "annotation", None)
        if isinstance(ann, ast.Name) and ann.id == "Session":
            names.add(arg.arg)
        elif isinstance(ann, ast.Constant) and ann.value == "Session":
            names.add(arg.arg)
        if (isinstance(default, ast.Call) and isinstance(default.func, ast.Name)
                and default.func.id == "Depends"
                and default.args
                and isinstance(default.args[0], ast.Name)
                and default.args[0].id == "get_db"):
            names.add(arg.arg)
    return names


def _enclosing_functions(tree):
    """[(FunctionDef, {session param names})] for every function in the module, outermost
    first, so a nested def inherits its parents' session parameters."""
    out = []

    def walk(node, inherited):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                mine = inherited | _session_params(child)
                out.append((child, mine))
                walk(child, mine)
            else:
                walk(child, inherited)

    walk(tree, set())
    return out


def _fetcher_arg(call):
    """The fetcher argument of a ``get_or_refresh(...)`` call — 3rd positional, or the
    ``fetcher=`` keyword."""
    for kw in call.keywords:
        if kw.arg == "fetcher":
            return kw.value
    return call.args[2] if len(call.args) >= 3 else None


def _is_get_or_refresh(call):
    f = call.func
    if isinstance(f, ast.Attribute):
        return f.attr == "get_or_refresh"
    return isinstance(f, ast.Name) and f.id == "get_or_refresh"


def _names_in(node):
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _resolve(expr, fn):
    """The node whose free names are the fetcher's captures.

    A lambda captures directly. A bare Name is resolved to a nested ``def`` of the same name
    in ``fn`` — that is the ``*_fresh()`` shape — whose body is then what matters.
    """
    if isinstance(expr, ast.Lambda):
        return expr, "lambda"
    if isinstance(expr, ast.Name):
        for node in ast.walk(fn):
            if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.name == expr.id and node is not fn):
                return node, f"nested def {expr.id}"
        # A module-level function: it takes no session from this scope by construction.
        return None, f"module-level {expr.id}"
    return expr, "expression"


def test_no_fetcher_captures_a_request_scoped_session():
    offenders = []
    checked = 0

    for path in _py_files():
        try:
            tree = ast.parse(open(path, encoding="utf-8").read())
        except SyntaxError as exc:                      # pragma: no cover - defensive
            raise AssertionError(f"{_rel(path)} does not parse: {exc}") from exc

        for fn, session_params in _enclosing_functions(tree):
            if not session_params:
                continue
            for node in ast.walk(fn):
                if not (isinstance(node, ast.Call) and _is_get_or_refresh(node)):
                    continue
                arg = _fetcher_arg(node)
                if arg is None:
                    continue
                checked += 1
                target, shape = _resolve(arg, fn)
                if target is None:
                    continue
                captured = _names_in(target) & session_params
                if captured:
                    offenders.append(
                        f"{_rel(path)}:{node.lineno} — {fn.name}() passes a {shape} "
                        f"capturing {sorted(captured)}")

    assert checked, (
        "the sweep found no get_or_refresh call sites at all — the matcher is broken, and a "
        "test that checks nothing is worse than no test")

    assert not offenders, (
        "these get_or_refresh fetchers close over a request-scoped Session:\n  "
        + "\n  ".join(offenders)
        + "\n\nA background refresh outlives the request, so the Session is already closed: "
          "SQLAlchemy will silently check out a fresh pooled connection nothing returns "
          "(QueuePool exhaustion), or raise IllegalStateChangeError if the task starts while "
          "the request is still running. Give the fetcher its own SessionLocal() and close it "
          "in a finally — see api/inventory.py and the *_fresh() helpers in api/aws.py.")


def test_the_sweep_would_catch_the_original_bug():
    # Mutation check in-process: the exact code shape that shipped must be reported. Without
    # this, a matcher that silently stops matching passes forever.
    src = '''
from sqlalchemy.orm import Session
async def dashboard_stats(db: Session = Depends(get_db)):
    raw, _ = await cache_service.get_or_refresh(key(), 60, lambda: _fetch_instances(db))
'''
    tree = ast.parse(src)
    found = []
    for fn, session_params in _enclosing_functions(tree):
        for node in ast.walk(fn):
            if isinstance(node, ast.Call) and _is_get_or_refresh(node):
                arg = _fetcher_arg(node)
                target, _shape = _resolve(arg, fn)
                if target is not None and (_names_in(target) & session_params):
                    found.append(node.lineno)
    assert found, (
        "the sweep no longer flags `lambda: _fetch_instances(db)` on a handler declaring "
        "`db: Session = Depends(get_db)` — that is the exact code that shipped, so the "
        "matcher has regressed and the real test above is now vacuous")


def test_the_sweep_accepts_the_fixed_shape():
    # The counterpart: a fetcher that owns its session must NOT be reported, or the guard
    # becomes noise someone silences.
    src = '''
from sqlalchemy.orm import Session
async def _fetch_instances_fresh():
    s = SessionLocal()
    try:
        return await _fetch_instances(s)
    finally:
        s.close()

async def dashboard_stats(db: Session = Depends(get_db)):
    raw, _ = await cache_service.get_or_refresh(key(), 60, _fetch_instances_fresh)
'''
    tree = ast.parse(src)
    for fn, session_params in _enclosing_functions(tree):
        for node in ast.walk(fn):
            if isinstance(node, ast.Call) and _is_get_or_refresh(node):
                target, _shape = _resolve(_fetcher_arg(node), fn)
                if target is None:
                    continue
                assert not (_names_in(target) & session_params), (
                    "the sweep flags a fetcher that opens its own session — it would fail "
                    "on correct code")


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
