"""Structural invariants for the auto-delete timer's wiring.

Everything here is a property no unit test can observe, because each one is about how the
pieces are CONNECTED rather than what a function returns:

  * the app-side loop must only ENQUEUE. The app runs `gunicorn -w 2`, so a loop that
    called sweep_once directly would run two concurrent sweeps forever — and both would
    look correct in isolation;
  * that loop must actually be launched in `lifespan`. A sweeper nobody starts is the
    silent version of the feature not existing;
  * the enqueue advisory-lock id must differ from init_db's and the audit chain's, or a
    sweep deadlocks schema init or every audit append;
  * this release must contain NO code that enqueues a destroy. That is the difference
    between "observe-only by configuration" and "observe-only by construction", and only
    a source-level check can tell them apart.

Static and stdlib-only, like tests/test_worker_dispatch.py, so it runs without fastapi or
sqlalchemy installed.

Run: python tests/test_expiry_wiring.py   (or under pytest)
"""
import ast
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MAIN = os.path.join(_ROOT, "web_dashboard", "main.py")
_WORKER = os.path.join(_ROOT, "web_dashboard", "jobs_worker.py")
_REAPER = os.path.join(_ROOT, "web_dashboard", "services", "expiry_reaper.py")
_POLICY = os.path.join(_ROOT, "web_dashboard", "services", "expiry_policy.py")
_DB = os.path.join(_ROOT, "web_dashboard", "database.py")
_JOBSVC = os.path.join(_ROOT, "web_dashboard", "services", "job_service.py")


def _src(path):
    return open(path, encoding="utf-8").read()


def _fn(path, name):
    """One function's AST node, by name."""
    for node in ast.walk(ast.parse(_src(path))):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in {os.path.basename(path)}")


def _names(fn):
    """Every identifier a function references, whether called or merely named.

    References, not just ast.Call: the loops in this codebase hand a sync function to
    ``asyncio.to_thread(fn, db)`` rather than calling it (see _ci_sweeper_loop), so a
    call-only walk would miss the very thing being asserted. Naming a function is also
    the property that matters here — passing sweep_once to to_thread runs it just as
    surely as calling it would.
    """
    out = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Attribute):
            out.add(n.attr)
        elif isinstance(n, ast.Name):
            out.add(n.id)
    return out


# ── single-flight: the loop enqueues, the worker executes ─────────────────────

def test_the_sweeper_loop_only_enqueues():
    """THE invariant. With `gunicorn -w 2` every lifespan task runs twice, so a loop
    that swept directly would run two passes concurrently — each enqueueing destroys for
    the same overdue resources. Handing the pass to the queue makes _claim_one's rowcount
    lock the arbiter instead."""
    refs = _names(_fn(_MAIN, "_expiry_sweeper_loop"))
    assert "enqueue_sweep_if_due" in refs, (
        "_expiry_sweeper_loop must call expiry_reaper.enqueue_sweep_if_due")
    assert "sweep_once" not in refs, (
        "_expiry_sweeper_loop calls sweep_once directly — with two gunicorn workers "
        "that is two concurrent sweeps. It must only enqueue.")


def test_the_sweeper_loop_is_actually_launched():
    """A loop defined and never started fails silently and completely."""
    lifespan = _src(_MAIN)
    assert "_expiry_sweeper_loop()" in lifespan, (
        "_expiry_sweeper_loop is never invoked — nothing schedules a sweep")
    assert re.search(r'create_task\(\s*_expiry_sweeper_loop\(\)', lifespan), (
        "_expiry_sweeper_loop is not started as a lifespan task")


def test_the_sweep_job_type_is_claimable_and_routed():
    """Covered generally by test_worker_dispatch, asserted by name here so a rename
    fails with a readable message instead of as a set difference."""
    worker = _src(_WORKER)
    assert '"expiry_sweep"' in worker
    assert 'job_type == "expiry_sweep"' in worker, (
        "no dispatch branch for expiry_sweep — the job would be claimed and dropped")
    assert "expiry_reaper" in worker


def test_the_enqueue_guard_checks_for_an_active_pass():
    """The advisory lock only serializes callers; this check is what actually stops a
    second pass. On SQLite it is the only protection there is."""
    fn = _fn(_REAPER, "enqueue_sweep_if_due")
    src = ast.unparse(fn)
    assert "ACTIVE_STATUSES" in src, (
        "enqueue_sweep_if_due does not check for an already-active sweep")
    assert "SWEEP_JOB_TYPE" in src or "expiry_sweep" in src


# ── advisory-lock id collision ────────────────────────────────────────────────

def _lock_ids(path):
    return set(re.findall(r"pg_advisory_xact_lock\((\d+)\)", _src(path))) | set(
        re.findall(r"_LOCK_ID\s*=\s*(\d+)", _src(path)))


def test_the_enqueue_lock_id_is_unique():
    """20260101 is init_db's DDL lock and 20260102 the audit chain's. Reusing either
    would make a sweep block schema init or every audit append — a deadlock that only
    shows up under concurrency."""
    reaper_ids = _lock_ids(_REAPER)
    assert reaper_ids, "expiry_reaper declares no advisory-lock id"
    taken = _lock_ids(_DB) | _lock_ids(_JOBSVC)
    assert taken, "could not read the existing lock ids — the regex may have drifted"
    clash = reaper_ids & taken
    assert not clash, f"expiry_reaper reuses an advisory-lock id already in use: {clash}"


# ── dependency direction ──────────────────────────────────────────────────────

def test_the_reaper_imports_nothing_from_the_api_package():
    """Same rule test_worker_dispatch enforces on the worker: the reaper runs in the
    worker process, so reaching into request-handling code inverts the dependency."""
    offenders = [
        line.strip() for line in _src(_REAPER).splitlines()
        if re.match(r"\s*from \.\.api[. ]|\s*from \.\.api import", line)
    ]
    assert not offenders, "expiry_reaper imports from api: " + "; ".join(offenders)


def test_the_policy_module_stays_pure():
    """expiry_policy is loaded by file path in the unit tests, which only works while it
    imports nothing but stdlib, config_service and config.settings."""
    tree = ast.parse(_src(_POLICY))
    allowed = {"config_service", "settings", "json"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                assert alias.name in allowed or node.module in ("datetime", "typing"), (
                    f"expiry_policy imports {alias.name} from {node.module} — it must "
                    "stay loadable by file path with no app deps")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name in ("logging", "json"), (
                    f"expiry_policy imports {alias.name}; keep it stdlib-only")


# ── observe-only by construction, not by configuration ────────────────────────

DESTROY_TYPES = ("ec2_destroy", "azure_destroy", "gce_destroy", "oci_destroy",
                 "clouddb_decommission", "k8s_decommission")


def test_this_release_cannot_enqueue_a_deletion():
    """The reaper reports and nothing more. Deletion is a separate change, so the
    guarantee is the ABSENCE of the code path rather than a flag someone could flip —
    which is a stronger property than any amount of dry-run defaulting.

    When deletion does land, this test should be deleted in the same commit, deliberately.
    """
    src = _src(_REAPER)
    for jt in DESTROY_TYPES:
        assert jt not in src, (
            f"expiry_reaper references {jt} — this release is meant to be observe-only "
            "by construction. If deletion is intentional, remove this test in the same "
            "commit so the change is explicit.")
    assert "start_decommission" not in src, (
        "expiry_reaper calls start_decommission — see above")


def test_the_sweep_reports_zero_reaped():
    """The summary shape must already carry `reaped` so adding deletion doesn't change
    the report's schema — and in this release it is hardcoded 0."""
    src = ast.unparse(_fn(_REAPER, "sweep_once"))
    assert "'reaped': 0" in src or '"reaped": 0' in src, (
        "sweep_once should report reaped=0 explicitly while deletion is absent")


# ── the stamp seam ────────────────────────────────────────────────────────────

def test_create_job_stamps_the_default_expiry():
    """One funnel covers every cloud VM deploy — single, count fan-out and bulk child.
    If this stamp moves into the API layer instead, a new provider silently gets no
    timer."""
    fn = _fn(_JOBSVC, "create_job")
    src = ast.unparse(fn)
    assert "expires_at" in src, "create_job does not accept/set expires_at"
    assert "default_expiry_for" in src, (
        "create_job does not consult expiry_policy.default_expiry_for — a per-provider "
        "stamp would have to be repeated for every cloud")
    # An explicit argument must win over the default, or a per-deploy override is
    # impossible to add later.
    assert any(a.arg == "expires_at" for a in fn.args.kwonlyargs + fn.args.args), (
        "create_job has no expires_at parameter for callers to override with")


def test_registered_resources_are_not_stamped_at_their_register_seam():
    """Deleting a registered database/cluster only deregisters it, so stamping one would
    eventually make the dashboard forget somebody else's production resource. Only the
    provision paths may stamp."""
    for name in ("cloud_database_service.py", "k8s_service.py"):
        path = os.path.join(_ROOT, "web_dashboard", "services", name)
        tree = ast.parse(_src(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not node.name.startswith("register_"):
                continue
            src = ast.unparse(node)
            assert "default_expiry_for" not in src, (
                f"{name}:{node.name} stamps an auto-delete timer on a REGISTERED "
                "resource — deleting one only drops the dashboard's own record")


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
