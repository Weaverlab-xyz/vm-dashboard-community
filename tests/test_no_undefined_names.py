"""No module may reference a name it never defines.

This bug class has now bitten twice in the same week, both times silently:

  * ``api/auth.py`` called ``config_service`` without importing it. The one call site
    wrapped it in a bare ``except Exception``, so the ``NameError`` was swallowed and
    the Entitle request-access deep link simply never rendered for anyone (#487).
  * ``services/aws_service.py`` called ``logger.warning`` three times without ever
    defining ``logger``. Worse than the first: each call sits *inside* the
    ``except Exception`` that handles a CloudWatch log-fetch failure, and the async
    wrapper around it catches only ``AWSError`` / ``ClientError`` / ``BotoCoreError`` /
    ``NoCredentialsError``. So a recoverable "couldn't read the logs" degraded into an
    unhandled ``NameError`` that **discarded the ECS task's real exit code** — a
    successful Ansible run reported as a baffling failure.

Both are invisible to the test suite because the offending line only runs on an error
path, and invisible to import-time checks because Python resolves globals lazily. A
static gate is the only thing that catches them.

Two layers, deliberately:

  * a targeted AST check that always runs, covering the specific shape both bugs took
    (a bare global name used but never bound at module level);
  * the full ruff F821 sweep when ruff is available, which is stricter and understands
    real scoping. There is no lint step in CI, so without this the sweep only happens
    when someone remembers to run it.

Runs under pytest, or standalone:
    python tests/test_no_undefined_names.py
"""
import ast
import builtins
import os
import subprocess
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PKG = os.path.join(_ROOT, "web_dashboard")

_BUILTINS = set(dir(builtins)) | {"__file__", "__name__", "__doc__", "__package__"}


def _python_files():
    for dirpath, dirs, files in os.walk(_PKG):
        dirs[:] = [d for d in dirs if d not in {"__pycache__", ".venv"}]
        for name in sorted(files):
            if name.endswith(".py"):
                yield os.path.join(dirpath, name)


def _bound_names(tree: ast.Module) -> set:
    """Every name a module binds anywhere — module level, in a function, in a class.

    Deliberately generous: this check is not trying to reimplement Python's scoping.
    It only asks "does this name exist ANYWHERE in the file?", which is enough to catch
    a name that was never defined at all — the shape both real bugs took — while never
    firing on a legitimate closure or a function-local import.
    """
    bound = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            bound |= {(a.asname or a.name).split(".")[0] for a in node.names}
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bound.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
                               ast.Lambda)):
            # Lambda has args but no name — and omitting it is exactly how this check
            # first produced a page of false positives on single-letter sort keys.
            if not isinstance(node, ast.Lambda):
                bound.add(node.name)
            args = getattr(node, "args", None)
            if args:
                for a in (list(args.args) + list(args.posonlyargs) + list(args.kwonlyargs)
                          + [args.vararg, args.kwarg]):
                    if a is not None:
                        bound.add(a.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            bound |= set(node.names)
        elif isinstance(node, ast.alias):
            bound.add((node.asname or node.name).split(".")[0])
        elif isinstance(node, (ast.comprehension,)):
            for sub in ast.walk(node.target):
                if isinstance(sub, ast.Name):
                    bound.add(sub.id)
        elif isinstance(node, ast.MatchAs) and node.name:
            bound.add(node.name)
    return bound


def _used_names(tree: ast.Module) -> set:
    return {n.id for n in ast.walk(tree)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}


def test_no_module_uses_a_name_it_never_defines():
    """The general form. Catches both real bugs and anything shaped like them."""
    offenders = []
    for path in _python_files():
        with open(path, encoding="utf-8") as fh:
            source = fh.read()
        try:
            tree = ast.parse(source)
        except SyntaxError as exc:  # pragma: no cover
            offenders.append(f"{os.path.relpath(path, _ROOT)}: syntax error {exc}")
            continue
        missing = _used_names(tree) - _bound_names(tree) - _BUILTINS
        if missing:
            offenders.append(f"{os.path.relpath(path, _ROOT)}: {sorted(missing)}")
    assert not offenders, (
        "module(s) reference names that are never defined — these raise NameError on "
        "whatever code path reaches them, usually an error path nobody tests:\n  "
        + "\n  ".join(offenders))


def test_every_logger_call_has_a_logger():
    """The specific shape of the aws_service bug, called out on its own.

    A logging call is the most dangerous place for this, because it is nearly always
    inside an ``except`` — so the NameError replaces the error being handled and
    escapes a caller that was only ever prepared for the original one.
    """
    offenders = []
    for path in _python_files():
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        used = {n.value.id for n in ast.walk(tree)
                if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
                and n.value.id in ("logger", "log")}
        undefined = used - _bound_names(tree)
        if undefined:
            offenders.append(f"{os.path.relpath(path, _ROOT)}: {sorted(undefined)}")
    assert not offenders, (
        "logging calls with no logger defined in the module:\n  " + "\n  ".join(offenders))


def test_ruff_f821_is_clean():
    """The authoritative sweep, when ruff is installed.

    Stricter than the AST check above because it models real scoping. Skipped rather
    than required, since ruff is not in requirements.txt and there is no lint step in
    CI — the check above is the one that always runs.
    """
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "ruff", "check", "web_dashboard",
             "--select", "F821", "--output-format", "concise"],
            cwd=_ROOT, capture_output=True, text=True, timeout=120)
    except (FileNotFoundError, subprocess.TimeoutExpired):  # pragma: no cover
        print("     (skipped: ruff unavailable)")
        return
    if proc.returncode not in (0, 1):                        # pragma: no cover
        print(f"     (skipped: ruff exited {proc.returncode})")
        return
    assert proc.returncode == 0, f"ruff F821 findings:\n{proc.stdout}"


def test_the_guard_would_catch_the_bugs_it_was_written_for():
    """Guard the guard. A check that cannot fail is worse than no check, because it
    reads like coverage."""
    entitle_shape = ast.parse(
        "def f():\n"
        "    try:\n"
        "        return config_service.get_bool('x')\n"
        "    except Exception:\n"
        "        return None\n")
    assert "config_service" in (_used_names(entitle_shape) - _bound_names(entitle_shape))

    aws_shape = ast.parse(
        "def f():\n"
        "    try:\n"
        "        pass\n"
        "    except Exception as e:\n"
        "        logger.warning('x: %s', e)\n")
    assert "logger" in (_used_names(aws_shape) - _bound_names(aws_shape))

    # And the legitimate forms must NOT trip it.
    ok = ast.parse(
        "import logging\n"
        "logger = logging.getLogger(__name__)\n"
        "def f(items):\n"
        "    local = [i for i in items]\n"
        "    def inner():\n"
        "        return local\n"
        "    try:\n"
        "        from . import config_service\n"
        "        return config_service.get('k'), inner()\n"
        "    except Exception as exc:\n"
        "        logger.warning('%s', exc)\n")
    assert not (_used_names(ok) - _bound_names(ok) - _BUILTINS)


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
