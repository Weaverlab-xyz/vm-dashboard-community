"""The job-runner services must not call module-level helpers they don't have.

When the cloud deploy/destroy work moved out of `api/*` into `services/*_vm_service`,
the functions came across but not all of their helpers: `aws_vm_service` called
`_aws_region()` at five sites while the definition stayed behind in `api/aws.py`.
Every `ec2_create_image` and `ami_copy` job raised NameError — and nothing noticed,
because the runners wrap the whole body in `except Exception` and funnel it into
`job_service.set_failed`, so the failure looked like an ordinary cloud error on a
job page rather than a broken import.

That is the gap this closes. `tests/test_cloud_deploy_meta.py` compares metadata keys
and `tests/test_worker_dispatch.py` compares job types; neither one looks at whether
the code inside a runner can actually run. A NameError in a rarely-exercised branch
is invisible until someone clicks the button.

Deliberately narrow to stay quiet: only calls to a leading-underscore bare name are
checked, and only against names the module defines, imports, or binds locally in the
enclosing function. Anything reached through an attribute (`job_service.set_failed`)
or shadowed by a local (`_run_deploy` binds `_aws_region` as a plain string) is out
of scope, which is what keeps this from crying wolf.

Runs under pytest, or standalone:  python tests/test_service_helper_names.py
"""
import ast
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SERVICES = os.path.join(_ROOT, "web_dashboard", "services")

# The runner services reached by jobs_worker._dispatch for cloud VM lifecycle.
_MODULES = [
    "aws_vm_service.py",
    "azure_vm_service.py",
    "gcp_vm_service.py",
    "oci_vm_service.py",
]


def _module_scope(tree):
    """Names available anywhere in the module: defs, classes, imports, globals."""
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                names.add(a.asname or a.name.split(".")[0])
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def _local_names(fn):
    """Everything bound inside one function: args, assignments, imports, with/for
    targets, comprehension vars, and nested defs."""
    names = set()
    args = fn.args
    for a in (list(args.args) + list(args.posonlyargs) + list(args.kwonlyargs)):
        names.add(a.arg)
    for a in (args.vararg, args.kwarg):
        if a:
            names.add(a.arg)
    for node in ast.walk(fn):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                names.add(a.asname or a.name.split(".")[0])
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
    return names


def _functions(tree):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def test_every_underscore_helper_called_in_a_runner_service_exists():
    checked, violations = 0, []
    for mod in _MODULES:
        path = os.path.join(_SERVICES, mod)
        tree = ast.parse(open(path, encoding="utf-8").read(), path)
        scope = _module_scope(tree)
        for fn in _functions(tree):
            local = _local_names(fn)
            for node in ast.walk(fn):
                if not (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Name)
                        and node.func.id.startswith("_")):
                    continue
                checked += 1
                name = node.func.id
                if name not in scope and name not in local:
                    violations.append(
                        f"{mod}:{node.lineno} {fn.name}() calls {name}() which is "
                        "neither defined nor imported in this module — the job will "
                        "raise NameError and be recorded as a generic failure")
    assert checked >= 20, (
        f"expected the runner services to call plenty of private helpers, saw {checked} — "
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
