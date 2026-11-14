"""A test that patches a module constant must not be patching a parameter default.

Python evaluates a default once, when the function is defined:

    POLL_INTERVAL_S = 15.0
    async def drive(..., interval_s=POLL_INTERVAL_S): ...

A test that later sets ``module.POLL_INTERVAL_S = 0`` changes the module attribute and
nothing else; ``drive`` still sleeps 15s. tests/test_clouddb_power.py did that, believing
its comment ("Never sleep for real in the runner's poll loop"), and spent 90 of its 93
seconds asleep -- the floor of the whole parallel test run.

The fix on the code side is to default to ``None`` and read the constant at call time.
This guard finds the trap anywhere: a test file assigning ``<alias>.<CONSTANT> = ...``
where ``<alias>`` is a web_dashboard module and ``<CONSTANT>`` is a parameter default
in that module.

Runs under pytest, or standalone:
    python tests/test_patched_constant_defaults.py
"""
import ast
import glob
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _defaults_by_module() -> dict:
    """``{(module, NAME): [function, ...]}`` for every UPPER_CASE name used as a default."""
    out: dict = {}
    for path in glob.glob(os.path.join(_ROOT, "web_dashboard", "**", "*.py"), recursive=True):
        with open(path, encoding="utf-8") as fh:
            try:
                tree = ast.parse(fh.read())
            except SyntaxError:
                continue
        module = os.path.relpath(path, _ROOT)[:-3].replace(os.sep, ".")
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for d in node.args.defaults + [k for k in node.args.kw_defaults if k]:
                    if isinstance(d, ast.Name) and d.id.isupper():
                        out.setdefault((module, d.id), []).append(node.name)
    return out


def _aliases(src: str) -> dict:
    """Local name -> dotted web_dashboard module, from a test file's imports."""
    names = {}
    for m in re.finditer(r"^from (web_dashboard[\w.]*) import \(?([\w, \n]+?)\)?\s*(?:#.*)?$",
                         src, re.M):
        for part in m.group(2).split(","):
            bits = part.split(" as ")
            name = bits[0].strip()
            if name:
                names[(bits[1] if len(bits) > 1 else name).strip()] = f"{m.group(1)}.{name}"
    for m in re.finditer(r"^import (web_dashboard[\w.]+) as (\w+)", src, re.M):
        names[m.group(2)] = m.group(1)
    return names


def test_no_test_patches_a_constant_that_is_only_a_default():
    defaults = _defaults_by_module()
    problems = []
    for path in sorted(glob.glob(os.path.join(_ROOT, "tests", "test_*.py"))):
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        aliases = _aliases(src)
        for m in re.finditer(r"^\s*(\w+)\.([A-Z][A-Z0-9_]*)\s*=(?!=)", src, re.M):
            module = aliases.get(m.group(1))
            funcs = defaults.get((module, m.group(2))) if module else None
            if funcs:
                problems.append(
                    f"{os.path.relpath(path, _ROOT)}: sets {module}.{m.group(2)}, which "
                    f"{', '.join(sorted(set(funcs)))} took as a default when the module "
                    f"loaded -- the patch never reaches it. Default to None and read the "
                    f"constant at call time.")
    assert not problems, "\n  " + "\n  ".join(problems)


def test_the_guard_catches_the_shape_it_was_written_for():
    src = "from web_dashboard.services import clouddb_power as cp\ncp.POLL_INTERVAL_S = 0.0\n"
    aliases = _aliases(src)
    assert aliases["cp"] == "web_dashboard.services.clouddb_power"
    m = re.search(r"^\s*(\w+)\.([A-Z][A-Z0-9_]*)\s*=(?!=)", src, re.M)
    assert m and (aliases[m.group(1)], m.group(2)) == (
        "web_dashboard.services.clouddb_power", "POLL_INTERVAL_S")
    tree = ast.parse("X = 1\ndef f(a=X): pass\n")
    fn = tree.body[1]
    assert isinstance(fn, ast.FunctionDef) and isinstance(fn.args.defaults[0], ast.Name)


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"ok   {name}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
