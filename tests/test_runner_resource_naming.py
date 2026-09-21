"""Guard: no cloud runner launcher may name its per-invocation resource with a
FIXED fallback constant when ``job_id`` is empty.

The three k8s-runner launchers each name one cloud resource after ``job_id`` —
an ACI container group, a Cloud Run job, an ECS log stream. When ``job_id`` is
empty (an interactive call, or a caller that never threaded it through) the
fallback used to be a single hard-coded name shared by every ad-hoc run. That
name is a shared resource: two Rancher API calls in flight at once (two jobs, or
a job plus an interactive call) both target it, the second create lands on the
first's LIVE container, and whichever finishes first deletes it out from under
the other in its ``finally``. On Cloud Run the same clash surfaced as a 409
"already exists" instead. Nothing in the Rancher API path serialises these calls,
so the NAME has to.

This is a source-shape check rather than a behavioural one because the launchers
need the cloud SDKs (and real credentials) to run. It parses the AST instead of
grepping so it cannot be satisfied by a comment mentioning uuid.

Runs under pytest, or standalone: python tests/test_runner_resource_naming.py
"""
import ast
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_SERVICES = os.path.join(_ROOT, "web_dashboard", "services")

# (module, launcher function, the local holding the resource name/suffix)
_LAUNCHERS = [
    ("azure_service.py", "_run_aci_k8s_sync", "group_name"),
    ("gcp_service.py", "_run_cloud_run_k8s_sync", "_suffix"),
    ("aws_service.py", "_run_ecs_k8s_sync", "log_stream_prefix"),
]


def _find_function(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def _find_assignment(fn_node, target):
    """The value expression assigned to ``target`` inside ``fn_node``."""
    for node in ast.walk(fn_node):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == target:
                    return node.value
    return None


def test_runner_fallback_names_are_unique_per_invocation():
    for module, fn_name, target in _LAUNCHERS:
        path = os.path.join(_SERVICES, module)
        tree = ast.parse(open(path, encoding="utf-8").read())
        fn = _find_function(tree, fn_name)
        assert fn is not None, f"{module}: {fn_name} not found — did the launcher move?"
        value = _find_assignment(fn, target)
        assert value is not None, f"{module}:{fn_name}: no assignment to {target}"
        # The shape is `<job_id-derived> if job_id else <fallback>`; only the
        # fallback branch is at issue (a job_id already distinguishes callers).
        assert isinstance(value, ast.IfExp), (
            f"{module}:{fn_name}: {target} is no longer a job_id/fallback conditional — "
            f"re-check that the no-job_id path is still unique per invocation")
        names = {n.id for n in ast.walk(value.orelse) if isinstance(n, ast.Name)}
        assert "uuid" in names, (
            f"{module}:{fn_name}: with job_id empty, {target} falls back to a name that is "
            f"the same on every ad-hoc run, so two concurrent ones collide on ONE cloud "
            f"resource (the second create lands on the first's live container; whichever "
            f"finishes first deletes it). Derive the fallback from uuid, per invocation.")


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_"):
            try:
                fn()
                print(f"ok   {name}")
            except Exception as e:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}: {e}")
    sys.exit(1 if failures else 0)
