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


def test_a_batch_cannot_fail_children_after_the_loop_finishes():
    """`job_service.set_failed` does not check the current status, so a `set_failed`
    reached *after* the per-item loop can overwrite children that already completed —
    discarding their PRA / Entitle / Password Safe state as far as the UI is concerned.

    Per-item handlers inside the loop are fine and expected; this only rejects calls
    past the end of the loop body."""
    checked, violations = 0, []
    for module in _LAUNCHERS:
        bulk = _function(_tree(module), "_run_bulk_deploy")
        if bulk is None:
            continue          # not every provider has a batch path
        checked += 1
        loops = [n for n in ast.walk(bulk) if isinstance(n, (ast.For, ast.AsyncFor))]
        if not loops:
            continue
        loop_end = max(n.end_lineno for n in loops)
        late = [c.lineno for c in _calls_named(bulk, "set_failed") if c.lineno > loop_end]
        if late:
            violations.append(
                f"{module}:_run_bulk_deploy calls set_failed at line(s) {late}, after the "
                f"per-item loop ends at line {loop_end} — that can overwrite a child that "
                "already completed")
    assert checked >= 2, (
        f"expected at least two providers with a _run_bulk_deploy, found {checked} — "
        "the walk may have stopped matching rather than started passing")
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
