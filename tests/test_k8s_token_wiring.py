"""Static wiring checks for the k8s Password Safe token feature.

Three lists in jobs_worker must agree or the failure is a runtime KeyError *after* a job
is claimed, which takes down the supervisor loop for every job — so it is pinned here
rather than discovered in production. Same for the app-side loop: it must only ENQUEUE,
because under ``gunicorn -w 2`` every task started there runs twice and the claim
rowcount is what makes a pass single-flight.

The advisory-lock scan is repo-wide and new: three ids existed before this feature and
their uniqueness was maintained by comment alone. A collision would wedge a sync pass
against schema init or every audit append — a deadlock that only appears under
concurrency.

Reads source text only; imports nothing from the app. Runs under pytest or standalone.
"""
import ast
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_WORKER = os.path.join(_ROOT, "web_dashboard", "jobs_worker.py")
_MAIN = os.path.join(_ROOT, "web_dashboard", "main.py")
_JOBSVC = os.path.join(_ROOT, "web_dashboard", "services", "job_service.py")
_SYNC = os.path.join(_ROOT, "web_dashboard", "services", "k8s_token_sync.py")
_TOKENSVC = os.path.join(_ROOT, "web_dashboard", "services", "ps_k8s_token_service.py")
_K8S_API = os.path.join(_ROOT, "web_dashboard", "api", "k8s.py")

NEW_TYPES = ("k8s_ps_token", "k8s_token_sync")


def _src(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _tuple_literal(path, name):
    """The string members of a module-level tuple/frozenset assignment."""
    tree = ast.parse(_src(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return {c.value for c in ast.walk(node.value)
                            if isinstance(c, ast.Constant) and isinstance(c.value, str)}
    raise AssertionError(f"{name} not found in {os.path.basename(path)}")


def _fn_code(path, name):
    tree = ast.parse(_src(path))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(_src(path), node) or ""
    raise AssertionError(f"{name}() not found in {os.path.basename(path)}")


# ── the three worker lists agree ─────────────────────────────────────────────────

def test_both_types_are_claimable():
    handled = _tuple_literal(_WORKER, "HANDLED_TYPES")
    for t in NEW_TYPES:
        assert t in handled, f"{t} is not in HANDLED_TYPES — the worker never claims it"


def test_each_type_is_in_exactly_one_tier():
    tiers = {name: _tuple_literal(_WORKER, name)
             for name in ("HEAVY_TYPES", "MEDIUM_TYPES", "LIGHT_TYPES")}
    for t in NEW_TYPES:
        found = [name for name, members in tiers.items() if t in members]
        assert len(found) == 1, (
            f"{t} is in {found or 'no tier'} — an untiered handled type raises KeyError "
            f"in the supervisor AFTER the claim, killing the loop for every job")
    assert "k8s_ps_token" in tiers["MEDIUM_TYPES"], \
        "k8s_ps_token runs kubectl plus up to two terraform applies"
    assert "k8s_token_sync" in tiers["LIGHT_TYPES"], \
        "the sync is Password Safe REST only — no local process (why it is not ps-cli)"


def test_the_sync_pass_is_a_singleton():
    singletons = _tuple_literal(_WORKER, "SINGLETON_TYPES")
    assert "k8s_token_sync" in singletons, (
        "two concurrent passes are two credential checkouts and two credential WRITES "
        "to the same Password Safe account")


def test_both_types_have_a_dispatch_branch():
    dispatch = _fn_code(_WORKER, "_dispatch")
    for t in NEW_TYPES:
        assert f'job_type == "{t}"' in dispatch, f"{t} has no _dispatch branch"
    assert "ps_k8s_token_service.run(" in dispatch
    assert "k8s_token_sync.run(" in dispatch


def test_the_sync_pass_is_a_routine_job_type():
    routine = _tuple_literal(_JOBSVC, "ROUTINE_JOB_TYPES")
    assert "k8s_token_sync" in routine, (
        "a timer-driven type outside ROUTINE_JOB_TYPES fills /jobs with completed "
        "no-ops — 96 rows a day at the default cadence")
    assert "k8s_ps_token" not in routine, \
        "the register/rotate job is operator-initiated and must stay visible"


# ── the app loop only enqueues ───────────────────────────────────────────────────

def test_the_loop_enqueues_and_does_not_run_the_pass():
    code = _fn_code(_MAIN, "_k8s_token_sync_loop")
    assert "enqueue_sweep_if_due" in code
    for forbidden in ("sync_once", "sync_cluster", "k8s_token_sync.run"):
        assert forbidden not in code, (
            f"the app loop calls {forbidden} — under gunicorn -w 2 that runs the pass "
            f"twice; the claim rowcount is what makes it single-flight")
    assert "sweep_interval_seconds" in code, \
        "the cadence must be re-read each iteration so Settings changes apply live"


def test_the_loop_is_registered_as_a_task():
    src = _src(_MAIN)
    assert "_k8s_token_sync_loop(), name=" in src, "the loop is never started"


# ── the enqueue guard keeps all three layers ─────────────────────────────────────

def test_the_enqueue_guard_checks_active_and_recency_under_a_lock():
    code = _fn_code(_SYNC, "enqueue_sweep_if_due")
    assert "pg_advisory_xact_lock" in code
    assert "ACTIVE_STATUSES" in code
    assert "created_at >=" in code or "Job.created_at" in code, (
        "the recency term is not redundant: a pass with nothing to sync finishes in "
        "well under a second, so a liveness-only dedupe provably creates duplicates")


def test_the_force_endpoint_skips_only_the_recency_guard():
    src = _src(_K8S_API)
    assert "min_gap_seconds=0" in src, \
        "the operator force-sweep must bypass recency — a human means now"


# ── advisory-lock ids are unique repo-wide ───────────────────────────────────────

def test_no_two_modules_take_the_same_advisory_lock_id():
    """20260101 init_db's DDL lock, 20260102 the audit chain, 20260103 the expiry
    enqueue, 20260104 this feature's. Reusing one would make a sync pass block schema
    init or every audit append."""
    owners = {}
    for dirpath, dirnames, filenames in os.walk(os.path.join(_ROOT, "web_dashboard")):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for fname in filenames:
            if not fname.endswith(".py"):
                continue
            path = os.path.join(dirpath, fname)
            src = _src(path)
            ids = set(re.findall(r"pg_advisory_xact_lock\((\d+)\)", src))
            ids |= set(re.findall(r"_LOCK_ID\s*=\s*(\d+)", src))
            for lock_id in ids:
                owners.setdefault(lock_id, set()).add(os.path.relpath(path, _ROOT))
    assert owners, "the advisory-lock regex found nothing — it may have drifted"
    clashes = {k: sorted(v) for k, v in owners.items() if len(v) > 1}
    assert not clashes, f"advisory-lock ids reused across modules: {clashes}"
    assert "20260104" in owners, "the token-sync enqueue lock id is missing"


# ── dependency direction ─────────────────────────────────────────────────────────

def test_the_new_services_do_not_import_the_api_layer_at_module_scope():
    """Both run in the worker, which never builds a FastAPI app. A module-scope
    ``..api`` import would drag routers — and fastapi — into the worker process.
    (broadcast_progress is imported inside the functions that use it, which is fine.)"""
    for path in (_SYNC, _TOKENSVC):
        tree = ast.parse(_src(path))
        for node in tree.body:      # module scope only
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith(("web_dashboard.api", "..api")), \
                    f"{os.path.basename(path)} imports the api layer at module scope"
            if isinstance(node, ast.ImportFrom) and node.level and node.module == "api":
                raise AssertionError(f"{os.path.basename(path)} imports ..api at module scope")


# ── the credential never leaves ps_api_service ───────────────────────────────────

def test_the_sync_module_never_handles_a_raw_credential():
    """checkout+push are one call in ps_api_service precisely so the plaintext stays in
    one frame. The sync must not reach for the value-returning helper."""
    src = _src(_SYNC)
    assert "checkout_credential" not in src, (
        "the recurring sync must use rotate_pra_vault_token, which never returns the "
        "credential — checkout_credential exists only for the Terraform-fed tunnel path")
    assert "rotate_pra_vault_token" in src


def test_rotate_on_check_in_is_never_requested_anywhere_in_the_feature():
    for path in (_SYNC, _TOKENSVC):
        assert "rotateoncheckin" not in _src(path).lower(), (
            f"{os.path.basename(path)} references rotate-on-check-in — that would rotate "
            f"the token again on release, revoking the value just written to PRA")


if __name__ == "__main__":
    fns = [v for name, v in sorted(globals().items()) if name.startswith("test_")]
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
