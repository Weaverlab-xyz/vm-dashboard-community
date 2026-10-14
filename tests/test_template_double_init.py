"""No element may call ``init()`` from ``x-init`` when its own component defines ``init()``.

Alpine 3 calls a component's ``init()`` method by itself when the ``x-data`` initialises.
An ``x-init="init()"`` on the same element runs it a second time. Nothing errors and the
page looks right, but every first-load API fetch goes out twice, every ``setInterval`` /
``setTimeout`` poll it starts runs as two timers, and anything that opens a stream opens
two. jobs/detail.html is the one that showed: two WebSockets, each replaying the whole job
log, so Live Output printed its opening lines twice (#1088). About fifty other pages had
the same attribute and were quietly doubling their requests.

An ``x-init`` that calls something else (``x-init="load()"``), or an ``init()`` that the
component does not define, is left alone. The tab partials (agents/_*.html, rbac/_*.html,
workload_lab/_*.html) each carry their own ``x-data``, so their ``x-init`` called their own
``init()`` and is the same bug, not a call into the container.

Runs under pytest, or standalone:
    python tests/test_template_double_init.py
"""
import os
import pathlib
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_WEB = pathlib.Path(_ROOT) / "web_dashboard"

# Fixed by its own PR (#1088), which also reworks the lines around it. Drop this once
# that has merged.
_FIXED_ELSEWHERE = {"web_dashboard/templates/jobs/detail.html"}

_JINJA_COMMENT = re.compile(r"\{#.*?#\}", re.S)
# An opening tag that calls init() from x-init. [^<>] keeps it inside one tag; Jinja
# comments are blanked first because several root tags carry one holding < or >.
_TAG = re.compile(r"<[A-Za-z][^<>]*?\bx-init\s*=\s*([\"'])\s*init\(\)\s*;?\s*\1[^<>]*>", re.S)
_XDATA = re.compile(r"""\bx-data\s*=\s*(["'])\s*([A-Za-z_$][\w$]*)\s*\(""")
_COMPONENT = re.compile(r"^\s*(?:window\.\w+\s*=\s*)?(?:async\s+)?function\s+([\w$]+)\s*\(", re.M)
_INIT = re.compile(r"^\s*(?:async\s+)?init\s*\(\s*\)\s*\{|^\s*init\s*:\s*(?:async\s+)?function\b", re.M)


def _sources():
    yield from sorted((_WEB / "templates").rglob("*.html"))
    yield from sorted((_WEB / "static" / "js").rglob("*.js"))


def components_with_init(texts):
    """Names of every ``function name(`` component whose scope defines ``init()``.

    A scope runs to the next ``function`` line, the same split
    test_template_duplicate_methods.py uses."""
    names = set()
    for text in texts:
        starts = list(_COMPONENT.finditer(text))
        for i, m in enumerate(starts):
            hi = starts[i + 1].start() if i + 1 < len(starts) else len(text)
            if _INIT.search(text, m.end(), hi):
                names.add(m.group(1))
    return names


def double_inits(html: str, with_init):
    """[(line, component)] for each tag whose x-init re-calls its own component's init()."""
    blanked = _JINJA_COMMENT.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), html)
    out = []
    for tag in _TAG.finditer(blanked):
        xd = _XDATA.search(tag.group(0))
        if xd and xd.group(2) in with_init:
            out.append((blanked.count("\n", 0, tag.start()) + 1, xd.group(2)))
    return out


def test_scanner_catches_the_original_bug():
    js = "function jobDetail(id) {\n  return {\n    init() {\n      this.connectWs();\n    },\n  };\n}\n"
    html = "<div x-data=\"jobDetail('{{ job_id }}')\" x-init=\"init()\">"
    assert double_inits(html, components_with_init([js])) == [(1, "jobDetail")]


def test_scanner_sees_through_a_jinja_comment_and_a_split_tag():
    js = "function rbacPage(a) {\n  return {\n    async init() {\n    },\n  };\n}\n"
    html = ("<div class=\"x\"\n     {# a comment with <, > and & in it #}\n"
            "     x-data='rbacPage({{ slugs | tojson }})'\n     x-init=\"init()\">")
    assert double_inits(html, components_with_init([js])) == [(1, "rbacPage")]


def test_scanner_leaves_other_x_inits_alone():
    js = ("function a() {\n  return {\n    load() {\n    },\n  };\n}\n"
          "function b() {\n  return {\n    init() {\n    },\n  };\n}\n")
    with_init = components_with_init([js])
    # x-init calls something other than init()
    assert double_inits("<div x-data=\"b()\" x-init=\"load()\">", with_init) == []
    # the component has no init() method of its own, so x-init is its only caller
    assert double_inits("<div x-data=\"a()\" x-init=\"init()\">", with_init) == []


def test_no_element_calls_its_own_init_twice():
    paths = list(_sources())
    with_init = components_with_init(p.read_text(encoding="utf-8") for p in paths)
    problems = []
    for path in paths:
        rel = path.relative_to(_ROOT).as_posix()
        if path.suffix != ".html" or rel in _FIXED_ELSEWHERE:
            continue
        for line, name in double_inits(path.read_text(encoding="utf-8"), with_init):
            problems.append(f"{rel}:{line}: x-init=\"init()\" on {name}(), which defines init()")
    assert not problems, (
        "Alpine 3 already calls init() on its own, so these pages initialise twice: every "
        "first-load fetch doubles and every poll runs as two timers. Remove the "
        "x-init.\n" + "\n".join(problems))


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
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
