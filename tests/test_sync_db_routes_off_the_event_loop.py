"""A route that runs synchronous SQLAlchemy must not be ``async def``.

**The bug class, measured on pov.weaverlab.app 2026-09-16.** An ``async def`` endpoint
runs *on the event loop*. If its body then calls synchronous SQLAlchemy -- which every
route taking ``db: Session = Depends(get_db)`` does -- one slow or lock-blocked query
blocks the loop, and with it **every other request in that Gunicorn worker**. That day
the whole app stopped answering: ``/``, ``/login``, ``/api/jobs``, a route that does not
even exist, and ``/api/health``, which is ``async def health(): return {"status": "ok"}``
and does no I/O at all. A no-I/O route timing out is the signature of a blocked loop and
of nothing else.

``Depends(get_db)`` itself is fine -- it is a *sync* generator, so FastAPI resolves it in
the anyio worker threadpool. It is the query in the handler body that lands on the loop.

**Why plain ``def`` is the fix.** Starlette runs a sync endpoint in the threadpool
(anyio's limiter, 40 slots). Waiting for a slot is an ``await``, which *suspends* rather
than blocks, so the loop keeps serving. A blocked query then costs one thread instead of
the entire process, and the failure degrades into slow requests and at worst a
``QueuePool`` timeout on one request -- never a dead worker that answers nothing.

This is not a new convention: 68 routes taking ``db`` were already plain ``def`` before
this sweep started. It is the majority pattern for the same work.

**A route is exempt if it genuinely awaits something.** Those need the sync DB work moved
off-loop instead (``run_in_threadpool`` or a service-level executor), which is a real
change rather than dropping a keyword, so this sweep does not cover them.

**Before converting a batch, grep the tests for the literal ``async def <name>(``.** A
number of tests in this suite slice a function body out of the source with
``src.split("async def foo(")[1]``, and dropping the keyword makes that raise
``IndexError`` from a test that looks unrelated to the change. Known live example:
``tests/test_pov_instance_grants.py`` does this to ``api/users.py::update_user``, and
``tests/test_bt_tenants.py`` to ``verify_tenant`` -- both still in the backlog below, so
whoever converts those files has to update the test in the same commit. Beware the
false positive too: a test DOUBLE can define a method with the same name as a route
(``tests/test_pov_add_vms.py`` has ``async def add_vms`` on a fake Skytap adapter), which
is not a coupling at all. Read every hit before acting on it.
"""
import ast
import collections
import pathlib

PKG = pathlib.Path(__file__).resolve().parents[1] / "web_dashboard"

HTTP_METHODS = ("'get'", "'post'", "'delete'", "'put'", "'patch'")

# Routes that must STAY async even though they never await, with the reason. Keep this
# tiny and always say why -- it is the escape hatch, not a parking lot.
INTENTIONALLY_ASYNC = {
    ("web_dashboard/api/dashboard.py", "dashboard_refresh"):
        "touches asyncio directly; a sync endpoint runs in a worker thread with no "
        "running event loop, so the asyncio calls would raise there.",
}

# The remaining backlog, by file. Converting a file means LOWERING its number (and
# deleting the entry at zero). The number is exact on purpose: this is a ratchet, and
# an exact count is what makes progress visible and prevents silent regrowth.
NOT_YET_CONVERTED = {
    "web_dashboard/api/auth.py": 7,
    "web_dashboard/api/aws.py": 4,
    "web_dashboard/api/azure.py": 2,
    "web_dashboard/api/bt_tenants.py": 5,
    "web_dashboard/api/config_mgmt.py": 4,
    "web_dashboard/api/containers.py": 9,
    "web_dashboard/api/desktops.py": 6,
    "web_dashboard/api/epml.py": 1,
    "web_dashboard/api/expiry.py": 1,
    "web_dashboard/api/gateways.py": 3,
    "web_dashboard/api/gcp.py": 3,
    "web_dashboard/api/images.py": 6,
    "web_dashboard/api/mfa.py": 4,
    "web_dashboard/api/notifications.py": 6,
    "web_dashboard/api/nutanix.py": 4,
    "web_dashboard/api/oci.py": 1,
    "web_dashboard/api/ot.py": 4,
    "web_dashboard/api/packer.py": 3,
    "web_dashboard/api/pov_accessor.py": 9,
    "web_dashboard/api/pov_vendor.py": 1,
    "web_dashboard/api/proxmox.py": 3,
    "web_dashboard/api/spend.py": 3,
    "web_dashboard/api/suspend.py": 2,
    "web_dashboard/api/tokens.py": 3,
    "web_dashboard/api/users.py": 9,
    "web_dashboard/api/vms.py": 3,
}


def _is_route(node: ast.AST) -> bool:
    for dec in node.decorator_list:
        dumped = ast.dump(dec)
        if ("router" in dumped or "app" in dumped) and any(
                m in dumped for m in HTTP_METHODS):
            return True
    return False


def _takes_db(node) -> bool:
    return any(a.arg == "db"
               for a in list(node.args.args) + list(node.args.kwonlyargs))


def _offenders() -> dict:
    """``{posix path: [(name, lineno), ...]}`` for every async+db route that never
    awaits -- i.e. every route that is on the event loop for no reason at all."""
    found = collections.defaultdict(list)
    for path in sorted(PKG.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        rel = "web_dashboard/" + path.relative_to(PKG).as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef) or not _is_route(node):
                continue
            if not _takes_db(node):
                continue
            if any(isinstance(x, (ast.Await, ast.AsyncWith, ast.AsyncFor))
                   for x in ast.walk(node)):
                continue  # genuinely async; out of scope
            if (rel, node.name) in INTENTIONALLY_ASYNC:
                continue
            found[rel].append((node.name, node.lineno))
    return dict(found)


def test_no_new_file_puts_sync_db_work_on_the_event_loop():
    """The load-bearing assertion: a file that is clean must STAY clean."""
    offenders = _offenders()
    regressions = {f: v for f, v in offenders.items() if f not in NOT_YET_CONVERTED}
    assert not regressions, (
        "These files have `async def` routes that run synchronous SQLAlchemy without "
        "awaiting anything, which puts the query on the event loop where one slow or "
        "lock-blocked call freezes the whole Gunicorn worker (including /api/health).\n"
        "Drop the `async` keyword -- Starlette will run it in the threadpool:\n"
        + "\n".join(f"  {f}: " + ", ".join(f"{n}:{ln}" for n, ln in v)
                    for f, v in sorted(regressions.items()))
    )


def test_the_backlog_only_shrinks():
    """A ratchet over the not-yet-converted files, so the sweep cannot regrow."""
    offenders = _offenders()
    problems = []
    for f, expected in sorted(NOT_YET_CONVERTED.items()):
        actual = len(offenders.get(f, []))
        if actual > expected:
            problems.append(
                f"  {f}: {actual} now vs {expected} recorded -- a NEW async+db route "
                f"was added here. Make it `def` instead.")
        elif actual < expected:
            problems.append(
                f"  {f}: {actual} now vs {expected} recorded -- progress! Lower the "
                f"number in NOT_YET_CONVERTED to {actual}"
                + (" and delete the entry." if actual == 0 else "."))
    assert not problems, "NOT_YET_CONVERTED is out of date:\n" + "\n".join(problems)


def test_the_converted_hot_paths_are_sync():
    """Named explicitly because these are the ones the outage ran through: the Jobs
    page poller that saturated every worker, and the agent lease, which is the single
    heaviest endpoint on this deployment (~1,000 hits per 2h for one agent)."""
    import ast as _ast

    for rel, names in (("api/jobs.py", ("list_jobs", "get_job", "cancel_job")),
                       ("api/agent.py", ("lease_job", "heartbeat", "push_logs",
                                         "enroll_agent"))):
        tree = _ast.parse((PKG / rel).read_text(encoding="utf-8"))
        kinds = {n.name: type(n).__name__
                 for n in _ast.walk(tree)
                 if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef))}
        for name in names:
            assert name in kinds, f"{rel}::{name} vanished -- update this test"
            assert kinds[name] == "FunctionDef", (
                f"{rel}::{name} is async again. It runs synchronous SQLAlchemy, so on "
                f"the event loop it can freeze every other request in the process."
            )


def test_sync_db_routes_are_the_established_majority():
    """Guards the premise, so nobody 'fixes' this sweep by going the other way: plain
    `def` routes taking `db` already outnumbered async ones before it began."""
    sync = 0
    for path in sorted(PKG.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and _is_route(node) and _takes_db(node):
                sync += 1
    assert sync >= 68, (
        f"only {sync} sync `def` routes take a db session; this sweep's premise is that "
        "the pattern is already the norm here (68 predated it)"
    )
