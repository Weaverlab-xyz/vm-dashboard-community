"""No Alpine component may define the same method twice.

Written after the POV detail page's Setup ladder went dead. The component had two
`async runStep(...)` methods: the ladder's (one per step: Create & install, Configure,
Wire up, ...) and, further down, the Guest-step panel's "run a staged file on a guest".
An object literal keeps the LAST duplicate key without complaint, so every ladder button
called the guest-file handler instead. That handler POSTed /guest-step with no VM, and the
refusal landed in the error banner at the top of the page, scrolled out of view, so the
button looked like it did nothing at all. Nothing parses wrong, nothing 404s, and
test_template_scripts.py passes, because a duplicate key is perfectly legal JavaScript.

The scan is per component: each `function name(` starts a new scope, so two components in
one file may share a name like `load()`. A method is a line that is only
`[async] name(args) {` and the scope key includes the indentation, so a nested helper at a
deeper level is not compared with a top-level method.

Runs under pytest, or standalone:
    python tests/test_template_duplicate_methods.py
"""
import collections
import os
import pathlib
import re

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_WEB = pathlib.Path(_ROOT) / "web_dashboard"

_COMPONENT = re.compile(r"^\s*(?:window\.\w+\s*=\s*)?function\s+\w+\s*\(", re.M)
_METHOD = re.compile(r"^( +)(?:async +)?([A-Za-z_$][\w$]*)\s*\([^)\n]*\)\s*\{\s*$", re.M)
_KEYWORDS = {"if", "for", "while", "switch", "catch", "function", "return", "else", "with"}


def _sources():
    yield from sorted((_WEB / "templates").rglob("*.html"))
    yield from sorted((_WEB / "static" / "js").rglob("*.js"))


def find_duplicates(text: str):
    """[(name, [line, ...])] for every method defined twice in one component."""
    starts = [m.start() for m in _COMPONENT.finditer(text)]
    bounds = list(zip([0] + starts, starts + [len(text)]))
    out = []
    for lo, hi in bounds:
        seen = collections.defaultdict(list)
        for m in _METHOD.finditer(text, lo, hi):
            if m.group(2) in _KEYWORDS:
                continue
            seen[(len(m.group(1)), m.group(2))].append(text.count("\n", 0, m.start()) + 1)
        out += [(name, lines) for (_, name), lines in seen.items() if len(lines) > 1]
    return out


def test_scanner_catches_the_original_bug():
    src = (
        "function povDetail() {\n  return {\n"
        "      async runStep(s) {\n        a();\n      },\n"
        "      async runStep() {\n        b();\n      },\n  };\n}\n"
    )
    assert find_duplicates(src) == [("runStep", [3, 6])]


def test_scanner_scopes_by_component():
    src = (
        "function a() {\n  return {\n      load() {\n      },\n  };\n}\n"
        "function b() {\n  return {\n      load() {\n      },\n  };\n}\n"
    )
    assert find_duplicates(src) == []


def test_no_component_defines_a_method_twice():
    problems = []
    for path in _sources():
        for name, lines in find_duplicates(path.read_text(encoding="utf-8")):
            problems.append(f"{path.relative_to(_ROOT)}: {name}() at lines {lines}")
    assert not problems, (
        "A method defined twice in one component: the last definition silently wins and "
        "every caller of the first runs the wrong code.\n" + "\n".join(problems))


if __name__ == "__main__":
    test_scanner_catches_the_original_bug()
    test_scanner_scopes_by_component()
    test_no_component_defines_a_method_twice()
    print("ok")
