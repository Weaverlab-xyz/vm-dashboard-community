"""No module-level name may be defined twice in the same file (pyflakes F811).

Python keeps the LAST definition, so an earlier duplicate is dead code that still
*looks* live. `secrets_backend_service.py` carried two of them: `delete_aws_sm`
(line ~276 and ~837) and `delete_gcp_sm` (line ~471 and ~896). The two copies
happened to be functionally identical, so nothing misbehaved — but the pair sat in
the module that deletes secrets, where the next person to add a confirmation guard,
an audit-log call, or a "don't force-delete production" check has a 50/50 chance of
adding it to the copy that never runs, and no test or review diff would show it.

That is the same failure shape as the NameError bugs the suite already guards
(`tests/test_service_helper_names.py`): code that reads as live, isn't, and fails
silently rather than loudly. This test is the redefinition half of that pair.

Deliberately narrow to stay quiet, in the same spirit as the helper-names test:

- Only bindings at the top level of a module body, and only methods directly in a
  class body, are compared. A def inside `try: / except ImportError:` or under an
  `if sys.version_info` is a legitimate fallback and is skipped outright.
- `@overload`, `@property`-`@x.setter`/`@x.deleter`, and `@singledispatch`-style
  `@x.register` deliberately reuse a name; all are exempt.
- Only defs and classes are reported as the *offender*. Duplicate imports (also
  F811) are out of scope; a def shadowing an earlier top-level import is in scope,
  because that one silently eats the import.

Runs under pytest, or standalone:  python tests/test_no_redefined_names.py
"""
import ast
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_APP = os.path.join(_ROOT, "web_dashboard")

# Decorators whose whole purpose is to bind a name that already exists.
_REBINDING_DECORATORS = {"overload", "setter", "getter", "deleter", "register"}


def _app_files():
    for dirpath, dirnames, filenames in os.walk(_APP):
        dirnames[:] = [d for d in dirnames
                       if d not in {"__pycache__", "node_modules", ".venv"}]
        for fn in sorted(filenames):
            if fn.endswith(".py"):
                yield os.path.join(dirpath, fn)


def _decorator_names(node):
    """Flatten decorator expressions to the trailing attribute/callable name."""
    out = set()
    for dec in getattr(node, "decorator_list", []):
        expr = dec.func if isinstance(dec, ast.Call) else dec
        if isinstance(expr, ast.Attribute):
            out.add(expr.attr)
        elif isinstance(expr, ast.Name):
            out.add(expr.id)
    return out


def _is_exempt(node):
    return bool(_decorator_names(node) & _REBINDING_DECORATORS)


def _scope_violations(body, rel, kind):
    """Report duplicate bindings among the direct statements of one scope."""
    seen: dict[str, int] = {}
    violations = []
    checked = 0
    for node in body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            # Tracked as a prior binding a later def could shadow, but never
            # reported as the offender itself.
            for alias in node.names:
                seen.setdefault(alias.asname or alias.name.split(".")[0], node.lineno)
            continue
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        checked += 1
        if _is_exempt(node):
            continue
        first = seen.get(node.name)
        if first is not None:
            violations.append(
                f"{rel}:{node.lineno} redefines `{node.name}` already bound at "
                f"line {first} ({kind}) — Python keeps this last definition, so "
                f"the earlier one is dead code that still looks live")
        seen[node.name] = node.lineno
    return checked, violations


def test_no_module_level_redefinitions():
    files, checked, violations = 0, 0, []
    for path in _app_files():
        rel = os.path.relpath(path, _ROOT).replace(os.sep, "/")
        try:
            tree = ast.parse(open(path, encoding="utf-8").read(), path)
        except SyntaxError as exc:  # a file that won't parse is a louder problem
            violations.append(f"{rel}: does not parse: {exc}")
            continue
        files += 1
        n, v = _scope_violations(tree.body, rel, "module scope")
        checked += n
        violations += v
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                n, v = _scope_violations(node.body, rel, f"class {node.name}")
                checked += n
                violations += v
    assert files >= 100, (
        f"expected to walk the whole app package, saw {files} files — the walk may "
        "have stopped finding code rather than started passing")
    assert checked >= 500, (
        f"expected plenty of top-level defs across the app, saw {checked} — the walk "
        "may have stopped matching rather than started passing")
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
