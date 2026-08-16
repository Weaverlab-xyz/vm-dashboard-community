"""Test files that stub a missing dependency must not shadow a present one.

Several test files import real service modules and stub the heavy dependencies those
services pull in, so they can run outside CI. That is a good pattern — a file that can
only run in CI gives no local signal at all — but it has one sharp edge, and this repo
has been cut by it:

    if "sqlalchemy" not in sys.modules:      # WRONG
        sys.modules["sqlalchemy"] = <thin stub>

`in sys.modules` asks whether the library has been **imported**, not whether it is
**installed**. In CI sqlalchemy is installed but has not been imported yet when the test
module runs, so the check passes and the stub is installed *over* the real library. The
next real import then fails:

    ImportError: cannot import name 'create_engine' from 'sqlalchemy' (unknown location)

The correct question is availability:

    try:
        from sqlalchemy.orm import Session   # or whatever the module actually needs
    except Exception:
        ... install the stub ...

The difference is invisible locally — both spellings work when the dependency really is
absent — which is why it needs a test rather than care.

Run: python tests/test_stub_guards.py   (or under pytest)
"""
import ast
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TESTS = os.path.join(_ROOT, "tests")


def _test_files():
    for name in sorted(os.listdir(_TESTS)):
        if name.startswith("test_") and name.endswith(".py") and name != os.path.basename(__file__):
            yield os.path.join(_TESTS, name)


def test_no_file_gates_a_stub_on_sys_modules_membership():
    """The bug itself: `"<dep>" not in sys.modules` as the condition for stubbing."""
    offenders = []
    for path in _test_files():
        src = open(path, encoding="utf-8").read()
        for m in re.finditer(r'if\s+["\'](\w+)["\']\s+not\s+in\s+sys\.modules\s*:', src):
            line = src[:m.start()].count("\n") + 1
            offenders.append(f"{os.path.basename(path)}:{line} gates a stub on "
                             f"'{m.group(1)}' not being imported yet")
    assert not offenders, (
        "a stub gated on sys.modules membership will shadow the real dependency in CI, "
        "where it is installed but not yet imported:\n  " + "\n  ".join(offenders))


def test_the_rule_is_not_vacuous():
    """The assertion above passes trivially if no test file stubs anything at all."""
    stubbing = [os.path.basename(p) for p in _test_files()
                if "sys.modules[" in open(p, encoding="utf-8").read()]
    assert len(stubbing) >= 3, (
        f"expected several files to install stubs, found {stubbing} — the checks above "
        "may have stopped matching rather than started passing")


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
