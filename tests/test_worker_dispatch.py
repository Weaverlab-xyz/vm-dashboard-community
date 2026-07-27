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
    return open(_WORKER).read()


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
