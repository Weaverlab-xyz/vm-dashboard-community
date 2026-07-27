"""The cloud deploy endpoints must gate every VM they create, and tag every batch.

Two rules, both learned from bugs that shipped.

**Admission.** `admission_service.enforce` was called on all four single-deploy routes
and on neither bulk route, so `allowed_regions`, `instance_size_caps` and `prod_window`
silently did not apply to batches — an operator who had gated `aws:ec2:deploy` had no
idea their guardrails stopped at the Bulk Deploy button. Fixing it is one line per
route; keeping it fixed is not, because deleting that line breaks nothing visible and
no functional test would notice. Hence a structural rule: any function that creates a
deploy job must enforce, and must do it *before* the first `create_job` — a 403 raised
partway through would strand `queued` children that `_claim_one` will not claim (it
filters `status='pending'`) and `reconcile_stale_jobs` will not touch (it only looks at
`running`), so they would sit in the queue forever.

**batch_id.** Both bulk endpoints minted one from the start and neither returned it, so
the pages could not link to the `/jobs?batch_id=` rollup that already existed. A parent
that names children without tagging the batch is the same latent mistake.

Static, like tests/test_worker_dispatch.py and tests/test_cloud_deploy_meta.py — the
endpoints need a database, a cloud SDK and an authenticated user to call for real, and
none of that is needed to see which calls appear in which order.

Runs under pytest, or standalone:  python tests/test_deploy_batch_endpoints.py
"""
import ast
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_API = os.path.join(_ROOT, "web_dashboard", "api")

# The four cloud VM routers, and the job types that create a VM.
_MODULES = ["aws.py", "azure.py", "gcp.py", "oci.py"]
_DEPLOY_JOB_TYPES = {
    "ec2_deploy", "ec2_bulk_deploy",
    "azure_deploy", "azure_bulk_deploy",
    "gce_deploy", "gce_bulk_deploy",
    "oci_deploy", "oci_bulk_deploy",
}


def _tree(name):
    path = os.path.join(_API, name)
    return ast.parse(open(path, encoding="utf-8").read(), path)


def _functions(tree):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def _create_job_calls(fn):
    """[(node, job_type, status, metadata_keys)] for every create_job in a function."""
    out = []
    for n in ast.walk(fn):
        if not (isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "create_job"):
            continue

        def kw(name, default=None):
            for k in n.keywords:
                if k.arg == name and isinstance(k.value, ast.Constant):
                    return k.value.value
            return default

        md = next((k.value for k in n.keywords if k.arg == "metadata"), None)
        keys = ({k.value for k in md.keys if isinstance(k, ast.Constant)}
                if isinstance(md, ast.Dict) else set())
        out.append((n, kw("job_type"), kw("status", "pending"), keys))
    return out


def _enforce_calls(fn):
    """Every admission_service.enforce(...) call in a function, however it's reached —
    directly, or through the shared deploy_batch.enforce_admission helper."""
    found = []
    for n in ast.walk(fn):
        if not isinstance(n, ast.Call):
            continue
        attr = getattr(n.func, "attr", "")
        if attr in ("enforce", "enforce_admission"):
            found.append(n)
    return found


def _deploy_functions():
    """Functions in the cloud routers that create at least one VM deploy job."""
    for mod in _MODULES:
        for fn in _functions(_tree(mod)):
            calls = _create_job_calls(fn)
            if any(jt in _DEPLOY_JOB_TYPES for _, jt, _, _ in calls):
                yield mod, fn, calls


def test_every_endpoint_that_creates_a_vm_enforces_admission():
    found, violations = 0, []
    for mod, fn, _calls in _deploy_functions():
        found += 1
        if not _enforce_calls(fn):
            violations.append(
                f"{mod}:{fn.name} creates a VM deploy job but never calls "
                "admission_service.enforce — policy guardrails do not apply to it")
    # Four single-deploy routes, four count fan-outs, four multi-select bulk routes.
    # A floor guards against the walk silently stopping to match.
    assert found >= 12, (
        f"expected the cloud routers to have at least 12 VM-creating functions, "
        f"found {found} — the walk may have stopped matching rather than started passing")
    assert not violations, "\n".join(violations)


def test_the_gate_runs_before_any_job_row_is_created():
    """Order matters as much as presence. enforce() raises 403, so a denial after the
    first create_job leaves orphan children behind — unclaimable and never reconciled."""
    violations = []
    for mod, fn, calls in _deploy_functions():
        enforces = _enforce_calls(fn)
        if not enforces:
            continue          # covered by the test above
        first_gate = min(c.lineno for c in enforces)
        first_job = min(c.lineno for c, _, _, _ in calls)
        if first_gate > first_job:
            violations.append(
                f"{mod}:{fn.name} calls create_job at line {first_job} before "
                f"admission at line {first_gate} — a denial would strand orphan rows")
    assert not violations, "\n".join(violations)


def test_every_batch_parent_tags_its_batch():
    """A parent that lists children must also pass batch_id: the /jobs rollup and
    job_service._summarize_statuses are both keyed on it, so an untagged batch is
    invisible as a batch."""
    found, violations = 0, []
    for mod in _MODULES:
        for fn in _functions(_tree(mod)):
            for node, job_type, _status, keys in _create_job_calls(fn):
                if "children" not in keys:
                    continue
                found += 1
                if not any(k.arg == "batch_id" for k in node.keywords):
                    violations.append(
                        f"{mod}:{fn.name} creates parent {job_type} with children but "
                        "no batch_id — the jobs page cannot roll it up")
    assert found >= 8, (
        f"expected 8 batch parents — a count fan-out and a multi-select bulk route per "
        f"cloud — found {found}")
    assert not violations, "\n".join(violations)


def test_batch_children_carry_the_same_batch_id_as_their_parent():
    """Children created alongside a parent must be tagged too, or the rollup counts
    only the parent and reports a 1-job batch."""
    violations = []
    for mod in _MODULES:
        for fn in _functions(_tree(mod)):
            calls = _create_job_calls(fn)
            if not any("children" in keys for _, _, _, keys in calls):
                continue
            for node, job_type, status, keys in calls:
                if "children" in keys or job_type not in _DEPLOY_JOB_TYPES:
                    continue
                if not any(k.arg == "batch_id" for k in node.keywords):
                    violations.append(
                        f"{mod}:{fn.name} creates child {job_type} without a batch_id")
    assert not violations, "\n".join(violations)


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
