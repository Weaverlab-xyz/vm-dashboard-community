"""Static wiring checks for the k8s Password Safe token feature.

Three lists in jobs_worker must agree or the failure is a runtime KeyError *after* a job
is claimed, which takes down the supervisor loop for every job — so it is pinned here
rather than discovered in production.

Most of this file used to pin a dashboard-side sync loop (``services/k8s_token_sync.py``:
a timer, an enqueue-only app task, an advisory lock, a singleton job type). That loop is
gone — Password Safe keeps the PRA Vault account in step natively via SyncedAccounts —
and ``test_no_trace_of_the_old_sync_loop_survives`` below is what stops it growing back
one reference at a time.

The advisory-lock scan is repo-wide and outlived the feature that motivated it: ids were
kept unique by comment alone, and a collision would wedge one pass against schema init or
every audit append — a deadlock that only appears under concurrency.

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
_TOKENSVC = os.path.join(_ROOT, "web_dashboard", "services", "ps_k8s_token_service.py")
_PSAPI = os.path.join(_ROOT, "web_dashboard", "services", "ps_api_service.py")
_K8S_API = os.path.join(_ROOT, "web_dashboard", "api", "k8s.py")

TOKEN_TYPE = "k8s_ps_token"


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

def test_the_token_job_is_claimable():
    handled = _tuple_literal(_WORKER, "HANDLED_TYPES")
    assert TOKEN_TYPE in handled, \
        f"{TOKEN_TYPE} is not in HANDLED_TYPES — the worker never claims it"


def test_the_token_job_is_in_exactly_one_tier():
    tiers = {name: _tuple_literal(_WORKER, name)
             for name in ("HEAVY_TYPES", "MEDIUM_TYPES", "LIGHT_TYPES")}
    found = [name for name, members in tiers.items() if TOKEN_TYPE in members]
    assert len(found) == 1, (
        f"{TOKEN_TYPE} is in {found or 'no tier'} — an untiered handled type raises "
        f"KeyError in the supervisor AFTER the claim, killing the loop for every job")
    assert TOKEN_TYPE in tiers["MEDIUM_TYPES"], \
        "k8s_ps_token runs kubectl plus up to two terraform applies"


def test_the_token_job_has_a_dispatch_branch():
    dispatch = _fn_code(_WORKER, "_dispatch")
    assert f'job_type == "{TOKEN_TYPE}"' in dispatch, f"{TOKEN_TYPE} has no _dispatch branch"
    assert "ps_k8s_token_service.run(" in dispatch


def test_the_token_job_is_not_a_routine_type():
    routine = _tuple_literal(_JOBSVC, "ROUTINE_JOB_TYPES")
    assert TOKEN_TYPE not in routine, \
        "the register/rotate job is operator-initiated and must stay visible on /jobs"


# ── the sync loop is gone and must stay gone ─────────────────────────────────────

def test_no_trace_of_the_old_sync_loop_survives():
    """The dashboard no longer polls Password Safe or pushes credentials into PRA.

    Every name below belonged to that loop. They are checked together because the loop
    died as a unit and would come back the same way: one helper, then a job type to call
    it, then a timer to enqueue the job. The single allowed hit is the orphan-row cleanup
    in ps_k8s_token_service.deregister, which outlives the module that wrote those rows.
    """
    # Deliberately NOT "sweep_interval_seconds" / "enqueue_sweep_if_due": the auto-delete
    # reaper has its own, and a name that matches a live feature makes this test lie.
    dead = ("k8s_token_sync", "rotate_pra_vault_token", "_set_credential",
            "token_sync_state", "syncPsToken")
    allowed = {
        # deregister() still deletes `k8s_token_sync_<cluster_id>` config rows written by
        # v26.7.7. Drop this exemption once no deployment predates synced accounts.
        os.path.join("web_dashboard", "services", "ps_k8s_token_service.py"): {"k8s_token_sync"},
    }
    offenders = {}
    for dirpath, dirnames, filenames in os.walk(os.path.join(_ROOT, "web_dashboard")):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for fname in filenames:
            if not fname.endswith((".py", ".html")):
                continue
            path = os.path.join(dirpath, fname)
            rel = os.path.relpath(path, _ROOT)
            src = _src(path)
            for name in dead:
                if name in src and name not in allowed.get(rel, set()):
                    offenders.setdefault(rel, []).append(name)
    assert not offenders, (
        f"the deleted PS→PRA sync loop is referenced again: {offenders}. Password Safe "
        f"owns this now (SyncedAccounts) — do not reintroduce a poller.")


def test_the_app_starts_no_token_sync_task():
    src = _src(_MAIN)
    assert "_k8s_token_sync_loop" not in src, \
        "the app still starts a token-sync background task"


def test_no_sync_endpoints_remain():
    src = _src(_K8S_API)
    for gone in ('"/clusters/{cluster_id}/token-sync"', '"/token-sync/sweep"'):
        assert gone not in src, f"{gone} still exists — nothing implements it any more"
    assert '"/clusters/{cluster_id}/ps-token/status"' in src, \
        "the live Password Safe status read replaced the cached sync fields; it is missing"


# ── registration hands the sync to Password Safe, in the right order ─────────────

def test_registration_links_before_it_rotates():
    """The link must be established BEFORE the one registration-time rotation.

    Rotating first and failing to link leaves Password Safe holding a token PRA will
    never receive — and in LongLived mode the value PRA still has was just revoked.
    Linking first means a failure at that point has changed nothing in the cluster."""
    code = _fn_code(_TOKENSVC, "register")
    link = code.find("link_synced_account")
    rotate = code.find("change_managed_account_password")
    assert link != -1, "register() never links the two managed accounts"
    assert rotate != -1, "register() no longer rotates once to prove the path"
    assert link < rotate, (
        "register() rotates before it links — a failed link then leaves PRA holding a "
        "revoked token with nothing to refresh it")


def test_registration_passes_the_token_account_as_the_parent():
    """Direction, statically. Both arguments are managed-account ids, so swapping them
    links successfully and syncs BACKWARDS — pushing the PRA Vault account's value onto
    the cluster's token account. Nothing downstream would notice."""
    compact = "".join(_fn_code(_TOKENSVC, "register").split())
    assert "parent_account_id=int(row.ps_token_account_id)" in compact, \
        "the ServiceAccount token account must be the PARENT of the synced pair"
    assert "synced_account_id=int(row.ps_pra_vault_account_id)" in compact, \
        "the PRA Vault account must be the SUBSCRIBER of the synced pair"


def test_an_unconfirmed_link_is_fatal():
    code = _fn_code(_TOKENSVC, "register")
    assert 'link.get("confirmed")' in code, \
        "register() does not check that the link actually took"
    assert "raise PSK8sTokenError" in code.split('link.get("confirmed")')[1][:400], (
        "an unconfirmed link must abort registration — completing would report success "
        "with rotations that never reach PRA")


def test_deregister_unlinks_before_offboarding():
    code = _fn_code(_TOKENSVC, "deregister")
    unlink = code.find("unlink_synced_account")
    destroy = code.find("ps_resource_service.deregister")
    assert unlink != -1, "deregister() leaves the synced pair linked"
    assert destroy != -1, "deregister() no longer off-boards the managed systems"
    assert unlink < destroy, (
        "deregister() destroys the managed systems before unlinking — that can leave "
        "Password Safe syncing into an account that no longer exists")


# ── advisory-lock ids are unique repo-wide ───────────────────────────────────────

def test_no_two_modules_take_the_same_advisory_lock_id():
    """20260101 init_db's DDL lock, 20260102 the audit chain, 20260103 the expiry
    enqueue. 20260104 belonged to the deleted token-sync enqueue and is now free —
    reusing an id would make one pass block schema init or every audit append."""
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


# ── dependency direction ─────────────────────────────────────────────────────────

def test_the_token_service_does_not_import_the_api_layer_at_module_scope():
    """It runs in the worker, which never builds a FastAPI app. A module-scope ``..api``
    import would drag routers — and fastapi — into the worker process.
    (broadcast_progress is imported inside the functions that use it, which is fine.)"""
    tree = ast.parse(_src(_TOKENSVC))
    for node in tree.body:      # module scope only
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith(("web_dashboard.api", "..api")), \
                "ps_k8s_token_service imports the api layer at module scope"
            if node.level and node.module == "api":
                raise AssertionError(
                    "ps_k8s_token_service imports ..api at module scope")


# ── rotate-on-release stays forbidden ────────────────────────────────────────────

def test_rotate_on_check_in_is_never_requested_anywhere_in_the_feature():
    """Truer under synced accounts than before: a credential change on EITHER member of
    a synced pair re-rotates both, so rotate-on-release would rotate the real cluster
    token every time the tunnel reads it."""
    for path in (_TOKENSVC, _PSAPI):
        assert "rotateoncheckin" not in _src(path).lower(), (
            f"{os.path.basename(path)} references rotate-on-check-in — on a synced pair "
            f"that rotates the cluster's token on every release")


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
