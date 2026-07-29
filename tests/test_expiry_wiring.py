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


# ── deletion reuses the endpoints' own job types ──────────────────────────────
#
# PR 1's test_this_release_cannot_enqueue_a_deletion lived here and asserted the ABSENCE
# of every destroy path. Deletion has now landed deliberately, so that test is gone and
# these took its place: the point is no longer "can't delete" but "deletes only through
# the paths a human's Destroy button already uses".

def test_the_reaper_does_not_reimplement_teardown():
    """The reaper must enqueue jobs and call start_decommission — never drive Terraform,
    a cloud SDK, or a PRA/vault cleanup itself. A second teardown implementation would
    drift from the endpoint's and leak exactly the resources this feature exists to stop
    leaking."""
    src = _src(_REAPER)
    for forbidden in ("terraform", "boto3", "ec2_client", "compute_v1",
                      "run_destroy", "run_decommission", "delete_instance"):
        assert forbidden not in src, (
            f"expiry_reaper references {forbidden} — teardown belongs to the service the "
            "destroy job dispatches to, not to the reaper")


def test_the_reaper_derives_destroy_metadata_from_the_deploy_job():
    """Never from current config. Each DELETE endpoint resolves and PERSISTS region /
    resource_group / project_id at deploy time precisely so the runner doesn't re-resolve
    them later — api/gcp.py calls a destroy aimed at the wrong project "the worst version
    of this bug"."""
    src = ast.unparse(_fn(_POLICY, "build_destroy_metadata"))
    assert "deploy_meta" in src
    for reresolved in ("config_service", "settings", "_aws_region", "_rg", "_gcp_project"):
        assert reresolved not in src, (
            f"build_destroy_metadata reads {reresolved} — it must use only what the "
            "deploy job recorded, and refuse when a key is missing")


def test_every_reapable_kind_has_a_reap_path():
    """A kind that reap_target can return but _reap_one can't act on would be reported as
    due every pass and never resolved."""
    reap = ast.unparse(_fn(_REAPER, "_reap_one"))
    assert '"vm"' in reap or "'vm'" in reap
    row = ast.unparse(_fn(_REAPER, "_reap_row"))
    for kind in ("database", "k8s"):
        assert f'"{kind}"' in row or f"'{kind}'" in row, f"_reap_row ignores {kind}"


def test_at_most_once_is_a_write_not_a_hope():
    """Enqueueing a destroy must clear expires_at in the same commit. Without that the
    next pass sees the same overdue resource and enqueues a second destroy — the failure
    mode that turns one expiry into N teardowns of the same thing."""
    for fn_name in ("_reap_vm", "_reap_row"):
        src = ast.unparse(_fn(_REAPER, fn_name))
        assert "expires_at = None" in src, (
            f"{fn_name} does not clear expires_at — a second sweep would re-enqueue")
        assert "commit" in src, f"{fn_name} does not commit its at-most-once write"


def test_reap_one_never_raises():
    """One bad resource — a 403, a missing region, a teardown already running — must not
    abandon the rest of the pass. Mirrors _reap_cloud_run_jobs, which counts a failed
    delete and moves on."""
    fn = _fn(_REAPER, "_reap_one")
    handlers = [n for n in ast.walk(fn) if isinstance(n, ast.ExceptHandler)]
    assert handlers, "_reap_one has no exception handling"
    caught = {ast.unparse(h.type) for h in handlers if h.type is not None}
    assert any("Exception" in c for c in caught), (
        "_reap_one does not catch bare Exception — an unexpected error would kill the pass")
    assert not any(isinstance(n, ast.Raise) and n.exc is not None
                   for n in ast.walk(fn)), "_reap_one raises; it must report instead"


def test_the_per_pass_cap_counts_deletions_not_attempts():
    """A resource that always refuses must not starve the cap.

    Found in an end-to-end run: a VM whose deploy job recorded no region refuses on every
    pass, and it sorts oldest-first forever, so charging a refusal against the cap let ONE
    un-reapable resource consume a deletion slot indefinitely. With cap=1 nothing else
    would ever be reaped — the feature would silently stop working, and the report would
    look busy while doing nothing.

    The fix is that the budget is spent on successful reaps only; refusals are attempted
    (they're a metadata read, no cloud call) and reported. Asserted structurally because
    the bug is in the loop's control flow, which no return value exposes.
    """
    src = ast.unparse(_fn(_REAPER, "sweep_once"))
    assert "reaped >= cap" in src, (
        "the cap is not checked against the successful-reap count — a refusal that "
        "consumes a slot lets one bad resource starve every pass")
    # And the pre-loop truncation that used to do this must be gone, or the loop check is
    # dead code and the starvation is back.
    assert "targets[:cap]" not in src, (
        "targets are still truncated before the loop; refusals inside the truncated "
        "window would consume deletion budget again")


def test_the_report_counts_everything_overdue_not_just_this_pass():
    """"due 2" while 40 resources are overdue would tell an operator the opposite of the
    truth."""
    src = ast.unparse(_fn(_REAPER, "sweep_once"))
    assert "due_total" in src, (
        "sweep_once reports len(targets) after capping rather than the true overdue count")


def test_deletion_is_gated_on_both_arming_clocks():
    """Turning report-only off must not act on a backlog accumulated while it was on."""
    src = ast.unparse(_fn(_REAPER, "sweep_once"))
    assert "deletion_active" in src, "sweep_once does not consult deletion_active"
    assert "may_delete" in src
    pol = ast.unparse(_fn(_POLICY, "deletion_active"))
    assert "ARM_DELAY_MINUTES" in pol, "deletion_active has no arming delay"
    assert "dry_run" in pol and "enforce" in pol, (
        "deletion_active must require both flags, not just one")


def test_the_reaper_actor_is_not_a_login_name():
    """created_by on a destroy job and username in the audit log both have to read
    unambiguously as machine action, and stay separable in an audit query."""
    assert "-" in reaper_actor() or reaper_actor().islower()
    assert reaper_actor() not in ("system", "admin", ""), (
        "the reaper's actor should be distinguishable from the generic system actor")


def reaper_actor() -> str:
    for node in ast.walk(ast.parse(_src(_REAPER))):
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", None) == "REAPER_ACTOR" for t in node.targets):
            return node.value.value
    raise AssertionError("REAPER_ACTOR not found")


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
