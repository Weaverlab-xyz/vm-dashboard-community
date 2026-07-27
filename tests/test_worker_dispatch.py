"""Invariants for the job runner's dispatch table.

`jobs_worker` claims a job with `Job.job_type.in_(HANDLED_TYPES)` and then routes it
through an if/elif chain. Those two lists are maintained by hand and nothing checked
they agreed, so both halves of the disagreement were silent failures:

  * a type in HANDLED_TYPES with no branch → the worker claims the job, marks it
    running, falls into the `else`, logs a warning nobody reads, and the job sits
    `running` until the stale reconciler kills it ten minutes later;
  * a branch for a type not in HANDLED_TYPES → dead code, because the claim query
    filters on the tuple. The job stays `pending` forever — and `reconcile_stale_jobs`
    skips `pending` on purpose, so nothing ever fails it either.

The third invariant is the direction of the dependency. The worker exists to run the
long jobs the API used to run inline; importing from `.api` to do it drags the FastAPI
routers and the auth dependencies into the runner process and inverts which side owns
the work. Every execution path now lives in `services/`, so this can be asserted
repo-wide with no exception list.

Static, deliberately: the worker's imports are function-local so the module can be read
without fastapi or sqlalchemy installed, which is also what makes these three checks
cheap enough to run everywhere.

Run: python tests/test_worker_dispatch.py   (or under pytest)
"""
import ast
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_WORKER = os.path.join(_ROOT, "web_dashboard", "jobs_worker.py")


def _src():
    return open(_WORKER, encoding="utf-8").read()


def _handled_types():
    """HANDLED_TYPES read from the AST, not a regex — it's a tuple of literals and
    getting it wrong here would silently weaken both coverage tests."""
    for node in ast.walk(ast.parse(_src())):
        if isinstance(node, ast.Assign) and any(
            getattr(t, "id", None) == "HANDLED_TYPES" for t in node.targets
        ):
            return {e.value for e in node.value.elts}
    raise AssertionError("HANDLED_TYPES not found in jobs_worker.py")


def _dispatched_types():
    """Every job_type literal the dispatch chain tests, in either shape it uses:
    `job_type == "x"` and `job_type in ("a", "b")`."""
    src = _src()
    types = set(re.findall(r'job_type == "([a-z0-9_]+)"', src))
    for group in re.findall(r"job_type in \(([^)]*)\)", src, re.S):
        types |= set(re.findall(r'"([a-z0-9_]+)"', group))
    return types


# ── the two halves have to agree ──────────────────────────────────────────────

def test_every_handled_type_has_a_dispatch_branch():
    """Claimed but unroutable: the job goes `running` and dies to the reconciler."""
    orphans = sorted(_handled_types() - _dispatched_types())
    assert not orphans, (
        f"claimed in HANDLED_TYPES but no dispatch branch: {orphans} — "
        "these jobs are picked up and dropped into the else warning")


def test_every_dispatched_type_is_claimable():
    """Routable but unclaimed: the branch can never run and the job never leaves
    `pending`, which reconcile_stale_jobs skips by design."""
    unreachable = sorted(_dispatched_types() - _handled_types())
    assert not unreachable, (
        f"dispatched but missing from HANDLED_TYPES: {unreachable} — "
        "the claim query filters on the tuple, so these branches are dead")


def test_the_dispatch_table_is_not_empty():
    """Both set-difference assertions above pass trivially if a parsing change makes
    one side come back empty."""
    assert len(_handled_types()) > 20
    assert len(_dispatched_types()) > 20


# ── the runner owns the work; the API only enqueues it ────────────────────────

def test_the_worker_imports_nothing_from_the_api_package():
    """Repo-wide, no exceptions: every execution path lives in services/. The worker
    reaching into request-handling code inverts the dependency and pulls the routers
    and auth into a process that serves no requests."""
    offenders = [
        line.strip() for line in _src().splitlines()
        if re.match(r"\s*from \.api[. ]|\s*from \.api import|\s*import .*\bapi\b", line)
    ]
    assert not offenders, (
        "jobs_worker imports from the api package: " + "; ".join(offenders))


# ── exactly one thing may drive a job row ─────────────────────────────────────

def _api_modules():
    api = os.path.join(_ROOT, "web_dashboard", "api")
    for name in sorted(os.listdir(api)):
        if name.endswith(".py"):
            yield os.path.join(api, name)


def _create_job_calls(fn):
    """(job_type, status) for every create_job call inside one function, with status
    defaulting to what create_job defaults to."""
    for n in ast.walk(fn):
        if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "create_job":
            def kw(name, default=None):
                for k in n.keywords:
                    if k.arg == name and isinstance(k.value, ast.Constant):
                        return k.value.value
                return default
            yield kw("job_type"), kw("status", "pending")


def test_no_handled_type_is_both_claimable_and_dispatched_in_request():
    """The double-execution bug this guards is not hypothetical — it is what bulk
    deploy would have done the moment `ec2_deploy` entered HANDLED_TYPES.

    The bulk endpoint creates one job row per instance and drives all of them from a
    single parent. Those rows carry a handled job_type, so if they are also created
    `pending`, `_claim_one` picks each one up and deploys it a second time, in parallel
    with the parent. Every instance launched twice, and nothing in the job record shows
    why. Children a parent owns must be created `queued`."""
    handled = _handled_types()
    violations = []
    for path in _api_modules():
        tree = ast.parse(open(path, encoding="utf-8").read(), path)
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            drives_it_itself = any(
                isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "add_task"
                for n in ast.walk(fn)
            )
            if not drives_it_itself:
                continue
            for job_type, status in _create_job_calls(fn):
                if job_type in handled and status == "pending":
                    violations.append(f"{os.path.basename(path)}:{fn.name} -> {job_type}")
    assert not violations, (
        "job rows that both the runner and an in-request task would execute: "
        + "; ".join(violations))


def _parent_child_endpoints():
    """Endpoints that create a parent job whose metadata carries a `children` list.

    Structural, so it needs no list of known bulk endpoints: naming child job ids in a
    parent's metadata *is* the declaration that something other than the runner will
    drive them."""
    for path in _api_modules():
        tree = ast.parse(open(path, encoding="utf-8").read(), path)
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            parents, others = [], []
            for n in ast.walk(fn):
                if not (isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "create_job"):
                    continue
                md = next((k.value for k in n.keywords if k.arg == "metadata"), None)
                keys = ({k.value for k in md.keys if isinstance(k, ast.Constant)}
                        if isinstance(md, ast.Dict) else set())
                def kw(name, default=None):
                    for k in n.keywords:
                        if k.arg == name and isinstance(k.value, ast.Constant):
                            return k.value.value
                    return default
                (parents if "children" in keys else others).append(
                    (kw("job_type"), kw("status", "pending")))
            if parents:
                yield os.path.basename(path), fn.name, parents, others


def test_children_of_a_parent_job_are_created_unclaimable():
    """The rule the in-request check above can't see any more.

    That one keys on `background_tasks.add_task` being present in the endpoint — but
    once a bulk endpoint is migrated the add_task is gone, so it stops applying exactly
    when the parent/child split becomes real. A dropped `status="queued"` would then be
    caught by nothing, and every VM in the batch gets created twice.

    Checked structurally against any endpoint that names child job ids in a parent's
    metadata, so a future bulk endpoint is covered the day it is written.

    Because the walk is per FUNCTION, the count>1 fan-out has to live in its own
    module-level `_fan_out_batch` rather than as an `if` inside the deploy route: a
    runtime branch is invisible here, so the single deploy's `pending` create_job in
    the same function would read as a violation. Nesting the helper doesn't help
    either — ast.walk descends into nested defs and re-attributes them to the parent."""
    found, violations = 0, []
    for module, fn_name, parents, others in _parent_child_endpoints():
        found += 1
        for job_type, status in others:
            if job_type in _handled_types() and status != "queued":
                violations.append(
                    f"{module}:{fn_name} creates {job_type} as {status!r} while also "
                    "creating a parent that lists it as a child")
    assert found >= 6, (
        f"expected the four count-batch paths plus the AWS and Azure multi-select bulk "
        f"endpoints to match this shape, found {found} — the rule may have stopped "
        "matching rather than started passing")
    assert not violations, "; ".join(violations)


def test_queued_is_outside_the_claim_query():
    """`queued` only protects anything because _claim_one filters on status='pending'
    exactly. A change to `!= 'completed'` or an `in_` would silently re-open it."""
    src = _src()
    claim = re.search(r"def _claim_one\(.*?\n(.*?)\n\ndef ", src, re.S).group(1)
    assert 'Job.status == "pending"' in claim, (
        "_claim_one no longer filters status == 'pending'; queued children are "
        "unprotected and bulk deploys would double-execute")


def test_the_worker_does_not_import_request_models_either():
    """Reconstructing a pydantic request model is the service's job — the worker hands
    it the stored metadata and nothing else. (`models.packer` was the last one.)"""
    assert "from .models" not in _src(), (
        "jobs_worker imports request models; the service should rebuild them")


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
