"""The single and batch deploy paths must not drift apart.

Every cloud has a `_run_deploy` and a `_run_bulk_deploy`. Where those are two separate
bodies rather than one calling the other, they drift — and the drift is invisible,
because a batch deploy still reports success.

The bug that prompted this file: `aws_vm_service._run_bulk_deploy` computed `os_type`
via `detect_os_type` and then dropped it, never passing it to `launch_instance`. It
defaults to `""`, and `aws_service._build_userdata` branches entirely on it, so `""`
emits no `runcmd` and the instance comes up with **no SSM agent** — breaking Session
Manager on private-subnet VMs and the Password Safe SSM plugin that the very same batch
path then onboards them into. Nothing failed. Nothing logged. The single path had passed
`os_type` all along.

These are AST checks because nothing executes these functions: they need a database, a
cloud SDK and a live job row. What can be checked statically is that the two paths hand
the cloud the same arguments, and that the batch path delegates rather than
reimplementing.

Runs under pytest, or standalone:  python tests/test_deploy_runner_parity.py
"""
import ast
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SERVICES = os.path.join(_ROOT, "web_dashboard", "services")

# service module -> the cloud-SDK call that actually creates a VM
_LAUNCHERS = {
    "aws_vm_service.py": "launch_instance",
    "azure_vm_service.py": "deploy_vm",
    "gcp_vm_service.py": "launch_instance",
    "oci_vm_service.py": "launch_instance",
}


def _tree(name):
    path = os.path.join(_SERVICES, name)
    return ast.parse(open(path, encoding="utf-8").read(), path)


def _function(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def _calls_named(node, attr):
    """Every Call in `node` reaching an attribute called `attr` (e.g. x.launch_instance)."""
    return [n for n in ast.walk(node)
            if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == attr]


def test_every_aws_launch_passes_os_type():
    """The regression this file is named for.

    `os_type` decides whether cloud-init installs the SSM agent. Omitting it is not a
    syntax error, not a runtime error, and not visible on the job page — the VM simply
    comes up unmanaged."""
    tree = _tree("aws_vm_service.py")
    calls = _calls_named(tree, "launch_instance")
    assert calls, "no launch_instance call found — the walk stopped matching"
    missing = [c.lineno for c in calls
               if not any(k.arg == "os_type" for k in c.keywords)]
    assert not missing, (
        f"aws_service.launch_instance called without os_type= at line(s) {missing}. "
        "_build_userdata branches on os_type, so the default '' means no SSM agent is "
        "installed — Session Manager and the Password Safe SSM plugin both break silently.")


def test_a_batch_recovery_handler_cannot_reach_children_it_already_deployed():
    """`job_service.set_failed` does not check the current status.

    So a handler shaped `except Exception: for job in job_items: set_failed(job)` is
    only safe while nothing has been created yet. Wrapped around the per-item deploy
    loop it becomes a footgun: anything raised *after* the loop — `cache_service.invalidate`
    was the live example — marks every already-completed child failed and discards its
    PRA / Entitle / Password Safe state as far as the UI is concerned.

    The rule is therefore about nesting, not line order: a fail-them-all handler is
    rejected when its own `try` body contains a loop, i.e. when it wraps the deploying.
    The same handler guarding only pre-loop setup is fine and stays allowed. Per-item
    handlers inside the loop are likewise fine — they fail exactly one child."""
    checked, violations = 0, []
    for module in _LAUNCHERS:
        bulk = _function(_tree(module), "_run_bulk_deploy")
        if bulk is None:
            continue          # not every provider has a batch path
        checked += 1
        for node in ast.walk(bulk):
            if not isinstance(node, ast.Try):
                continue
            guards_the_loop = any(
                isinstance(n, (ast.For, ast.AsyncFor))
                for stmt in node.body for n in ast.walk(stmt))
            if not guards_the_loop:
                continue      # setup-only handler — failing every child is correct there
            for handler in node.handlers:
                for n in ast.walk(handler):
                    if (isinstance(n, (ast.For, ast.AsyncFor))
                            and _calls_named(n, "set_failed")):
                        violations.append(
                            f"{module}:_run_bulk_deploy has a fail-every-child handler at "
                            f"line {n.lineno} wrapped around the per-item loop — anything "
                            "raised after the loop overwrites children that already "
                            "completed")
    assert checked >= 2, (
        f"expected at least two providers with a _run_bulk_deploy, found {checked} — "
        "the walk may have stopped matching rather than started passing")
    assert not violations, "\n".join(violations)


def test_the_gcp_runners_use_the_jobs_project_and_zone_not_current_config():
    """`gcp_vm_service.run()`'s docstring states the rule: project and zone come from
    the job, never from `_gcp_project()` / `_gcp_zone()`, because those return whatever
    is configured *now*. A default changed after a deploy would then aim the work at the
    wrong project.

    It has been broken twice. First for destroy and capture; then, after that fix, the
    Password Safe onboarding call still read the live default — and there it is worse
    than aiming wrong, because the managed-system address is
    `projectId/zone/instanceName`, so the VM gets onboarded under an address that
    resolves to nothing and rotation silently targets a non-existent instance.

    The docstring could not enforce itself. This can."""
    tree = _tree("gcp_vm_service.py")
    checked, violations = 0, []
    for node in ast.walk(tree):
        if not (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name.startswith("_run_")):
            continue
        checked += 1
        for call in ast.walk(node):
            if (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                    and call.func.id in ("_gcp_project", "_gcp_zone")):
                violations.append(
                    f"gcp_vm_service.py:{call.lineno} {node.name}() calls "
                    f"{call.func.id}() — use the project/zone persisted on the job")
    assert checked >= 3, (
        f"expected several _run_* functions in gcp_vm_service, found {checked}")
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
