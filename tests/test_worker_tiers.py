"""Invariants for the job runner's concurrency tiers.

`jobs_worker` runs several jobs at once, capped per tier, and the tier of a job type is
looked up in `_TIER_OF` — a dict built from three hand-maintained tuples. Both halves of a
disagreement are bad, and one is very bad:

  * a HANDLED_TYPES entry in no tier → `_TIER_OF[job_type]` raises KeyError in the
    supervisor loop, AFTER the job was claimed and marked running. That kills the loop for
    EVERY job, not just the untiered one, and the claimed row sits `running` until the
    stale reconciler fails it ten minutes later;
  * a type in a tier but not in HANDLED_TYPES → dead config, because the claim query
    filters on HANDLED_TYPES.

The other tests here pin the properties the tiering exists to create, in the direction
that fails OPEN. A refactor that dumps every type into LIGHT_TYPES passes a partition test
and reintroduces exactly the problem the tiers prevent — a Packer build running four-up on
a one-core container — so the tier of the nine streamed-subprocess types is asserted by
name, and each tier is asserted non-degenerate.

Static, deliberately: the worker's imports are function-local so the module can be read
without fastapi or sqlalchemy installed.

Run: python tests/test_worker_tiers.py   (or under pytest)
"""
import ast
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from test_worker_dispatch import _handled_types, _src, fn_code  # noqa: E402


def _norm(text):
    """ast.unparse normalizes string literals to single quotes; match quote-insensitively
    so an assertion about a filter isn't really an assertion about quoting style."""
    return text.replace('"', "'")

_WORKER = os.path.join(_ROOT, "web_dashboard", "jobs_worker.py")
_SERVICES = os.path.join(_ROOT, "web_dashboard", "services")

# Every job type that streams a long LOCAL subprocess line by line. Each of these writes a
# JobLog row per output line, so their tier is what bounds DB write pressure — the one
# classification here that must not drift.
_STREAMS_A_SUBPROCESS = {
    "k8s_provision", "k8s_decommission",              # terraform apply/destroy(on_line=)
    "clouddb_provision", "clouddb_decommission",      # same
    "cloudfn_deploy", "cloudfn_decommission",         # same (cloud_function_service._job_stream)
    "packer_aws_build", "packer_azure_build",         # packer_service._stream_command
    "packer_gcp_build", "packer_oci_build",
    "ansible_local",                                  # ansible_local_service, `docker run`
}


def _tuple_of_literals(name, path=None):
    """A module-level tuple/frozenset of string literals, read from the AST rather than a
    regex — getting this wrong would silently weaken every assertion below."""
    src = open(path or _WORKER, encoding="utf-8").read()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Assign) and any(
            getattr(t, "id", None) == name for t in node.targets
        ):
            value = node.value
            # frozenset(( ... )) — unwrap the call to its single argument
            if isinstance(value, ast.Call):
                value = value.args[0]
            return [e.value for e in value.elts]
    raise AssertionError(f"{name} not found in {path or _WORKER}")


def _tiers():
    return {
        "heavy": _tuple_of_literals("HEAVY_TYPES"),
        "medium": _tuple_of_literals("MEDIUM_TYPES"),
        "light": _tuple_of_literals("LIGHT_TYPES"),
    }


# ── the partition has to hold, both directions ────────────────────────────────

def test_every_handled_type_is_in_exactly_one_tier():
    """Untiered means KeyError in the supervisor, after the claim — which takes the loop
    down for every job, not just this one."""
    tiers = _tiers()
    tiered = [t for types in tiers.values() for t in types]
    handled = _handled_types()

    untiered = sorted(handled - set(tiered))
    assert not untiered, (
        f"in HANDLED_TYPES but in no tier: {untiered} — _TIER_OF[job_type] raises "
        "KeyError in _run_loop AFTER the job is claimed and marked running")

    unclaimable = sorted(set(tiered) - handled)
    assert not unclaimable, (
        f"tiered but not in HANDLED_TYPES: {unclaimable} — dead config, the claim query "
        "filters on HANDLED_TYPES so these can never be picked up")

    dupes = sorted({t for t in tiered if tiered.count(t) > 1})
    assert not dupes, (
        f"in more than one tier (or listed twice in one): {dupes} — _TIER_OF silently "
        "keeps whichever tuple came last, so the cap that applies is not the one you read")


def test_no_tier_is_degenerate():
    """Mirrors test_the_dispatch_table_is_not_empty. A refactor that collapses everything
    into one tier passes the partition test above while reintroducing exactly what the
    tiers prevent, so assert each tier still carries a plausible share."""
    tiers = _tiers()
    assert len(tiers["heavy"]) >= 8, f"HEAVY_TYPES shrank to {len(tiers['heavy'])}"
    assert len(tiers["medium"]) >= 25, f"MEDIUM_TYPES shrank to {len(tiers['medium'])}"
    assert len(tiers["light"]) >= 14, f"LIGHT_TYPES shrank to {len(tiers['light'])}"


def test_the_streamed_subprocess_jobs_are_heavy():
    """The load-bearing half of the classification. These stream a local subprocess and
    persist a JobLog row PER OUTPUT LINE, so their concurrency is bounded by the database,
    not the CPU. Demoting one to medium or light would let several run four-up."""
    heavy = set(_tiers()["heavy"])
    misfiled = sorted(_STREAMS_A_SUBPROCESS - heavy)
    assert not misfiled, (
        f"these stream a local subprocess but are not in HEAVY_TYPES: {misfiled} — each "
        "writes a JobLog INSERT per output line; running them concurrently saturates a "
        "small Postgres before it saturates the CPU")


def test_the_singletons_are_all_real_job_types():
    """A typo in SINGLETON_TYPES is invisible: the name simply never matches, and the
    deployment-global write it was meant to serialize runs concurrently anyway."""
    singletons = set(_tuple_of_literals("SINGLETON_TYPES"))
    unknown = sorted(singletons - _handled_types())
    assert not unknown, (
        f"SINGLETON_TYPES names types that are not claimable: {unknown} — the entry never "
        "matches and whatever global state it guards is unguarded")


# ── the supervisor's shape ────────────────────────────────────────────────────

def test_the_supervisor_never_awaits_a_job():
    """The one-line statement of this whole change. `await _dispatch(...)` inside the loop
    is what made the worker serial; if it comes back, every cap silently becomes 1."""
    loop = fn_code("_run_loop")
    assert "await _dispatch" not in loop, (
        "_run_loop awaits the dispatch again — that serializes the worker and makes the "
        "tier caps decorative")
    assert "create_task(_run_job" in loop, "_run_loop no longer spawns jobs as tasks"


def test_correlation_is_entered_per_job_not_per_loop():
    """contextvars are snapshotted per Task at create_task time. Entering correlation in
    the loop body would tag every concurrent job's log lines with whichever job the loop
    was starting at that instant — so the ids would be wrong, not just shared."""
    assert "correlation(" not in fn_code("_run_loop"), (
        "_run_loop enters correlation() itself; with concurrent tasks that mislabels "
        "every job's log lines. It belongs inside _run_job.")
    assert "with correlation(job_id):" in fn_code("_run_job"), (
        "_run_job no longer scopes the correlation id to the job")


def test_the_failure_backstop_lives_in_the_per_job_task():
    """An exception escaping a Task surfaces only as an 'exception was never retrieved'
    warning on GC, so the row would sit `running` for ten minutes. The backstop has to be
    inside the task, not around the await the supervisor no longer does."""
    assert "_fail_backstop" in fn_code("_run_job"), (
        "_run_job has no failure backstop; a crash around the dispatch leaves the job "
        "stuck `running` until the stale reconciler")
    assert "set_failed" not in fn_code("_run_loop"), (
        "_run_loop still marks jobs failed — it no longer awaits them, so it cannot know")


def test_the_claim_retry_is_bounded():
    """The old `while True` relied on a lost race never repeating, which rests on nothing
    ever writing `pending` back onto a running row — an invariant _claim_one cannot
    enforce. A bound turns a hot loop into a logged giveup."""
    claim = fn_code("_claim_one")
    assert "while True" not in claim, (
        "_claim_one is unbounded again; a row that stays claimable-but-unclaimable would "
        "spin the supervisor at full tilt")
    assert "max_attempts" in claim


def test_lowering_a_cap_cannot_cancel_a_running_job():
    """Capacity is only ever consulted to decide whether to CLAIM. If the supervisor
    gained a cancel path driven by the caps, moving a Settings slider would kill a
    40-minute apply — which is why the caps are safe to expose in the UI at all."""
    loop = fn_code("_run_loop")
    assert ".cancel()" not in loop, (
        "_run_loop cancels tasks; lowering a cap in Settings must drain, never cancel "
        "(the drain on shutdown is _drain's job, and it is explicit about it)")


def test_the_stale_cutoff_is_shared():
    """Two different numbers means either a zombie that still looks alive, or a row failed
    out from under a live sibling worker."""
    src = _src()
    assert "STALE_AFTER_MINUTES = " in src, "the cutoff constant is gone"
    assert "stale_after_minutes=STALE_AFTER_MINUTES" in src, (
        "reconcile_stale_jobs is called with a different cutoff than the drain backdates "
        "past — the two must be one constant")
    expire = _norm(fn_code("_expire_heartbeats"))
    assert "STALE_AFTER_MINUTES" in expire, (
        "_expire_heartbeats hard-codes its backdate instead of deriving it from the cutoff")
    assert "Job.status == 'running'" in expire, (
        "_expire_heartbeats is not guarded on status='running' — it could backdate a job "
        "that finished cleanly, or one a sibling worker owns")


def test_the_heartbeat_write_is_off_the_event_loop():
    """The heartbeat shares its loop with every in-flight job now. A synchronous session
    open/commit/close here would delay their progress writes and their sleep timers — and
    a late heartbeat is what makes a sibling's reconcile fail a live job."""
    assert "to_thread(_beat_once" in fn_code("_heartbeat"), (
        "the heartbeat writes to the DB synchronously on the event loop")


# ── the shared plugin cache ───────────────────────────────────────────────────

def test_no_terraform_init_runs_outside_the_plugin_cache_lock():
    """Terraform's plugin cache is explicitly not concurrency-safe: parallel inits race to
    place the same provider binary and fail with ETXTBSY. Every service that shells out to
    terraform works in its own tempdir but points TF_PLUGIN_CACHE_DIR at the ONE cache
    baked into the image, so they must all serialize on terraform.plugin_cache_lock.

    Checked by module rather than by call site: each of these funnels its init through a
    single `_run_tf`, which is where the lock belongs — nine call sites in one module
    cannot each be trusted to remember."""
    offenders = []
    for module in ("terraform.py", "terraform_pra_service.py",
                   "entitle_registration_service.py", "ps_resource_service.py"):
        path = os.path.join(_SERVICES, module)
        src = open(path, encoding="utf-8").read()
        if not re.search(r'["\']init["\']', src):
            continue                                   # module runs no init at all
        assert "plugin_cache_lock" in src, (
            f"{module} runs `terraform init` but never takes plugin_cache_lock — "
            "concurrent inits will fail with ETXTBSY 'text file busy'")
        # The lock must guard the runner, not merely be imported somewhere.
        if "with plugin_cache_lock():" not in src:
            offenders.append(module)
    assert not offenders, (
        f"{offenders} reference plugin_cache_lock but never enter it as a context manager")


def test_the_pool_clamp_reserves_headroom():
    """The clamp is the only thing standing between a config-driven cap and opaque
    QueuePool timeouts, so its two constants must stay honest: a job costs more than one
    connection, and the process needs some for itself."""
    src = _src()
    assert "_SESSIONS_PER_JOB = " in src and "_POOL_RESERVED = " in src
    import ast as _ast
    vals = {}
    for node in _ast.parse(src).body:
        if isinstance(node, _ast.Assign) and isinstance(node.targets[0], _ast.Name):
            if node.targets[0].id in ("_SESSIONS_PER_JOB", "_POOL_RESERVED"):
                vals[node.targets[0].id] = node.value.value
    assert vals["_SESSIONS_PER_JOB"] >= 2, (
        "a job holds the dispatcher's session plus at least one the service opens itself")
    assert vals["_POOL_RESERVED"] >= 1, (
        "the claim query, the heartbeat and the notification drain all need a connection")


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
