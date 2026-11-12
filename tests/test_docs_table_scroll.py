"""A wide Markdown table has to scroll itself, not drag the whole page sideways.

A pipe table is as wide as its widest row needs to be, and a table will not lay out
narrower than its min-content width however hard you squeeze it. So on a phone the table
pushed the *page* wider than the viewport: ``docs/operations/image-management/promotion.md``
has two tables that lay out at 627px and 531px, and in a 375px viewport the whole document
scrolled sideways with them -- every paragraph on the page drifting off-screen, which is
the reader-facing bug, not the table itself.

``_table_scroll_extension`` wraps each rendered ``<table>`` in a ``<div class=
"table-scroll">`` that ``_SHELL`` gives ``overflow-x:auto``, so the table gets its own
scrollbar. That is the same shape as the hand-authored SVG figures in ``docs/`` -- put the
wide thing in its own scroll container so the figure scrolls instead of the page.

What this file pins, and why each one is a way the fix could rot:

- Every table is wrapped, including ones nested in a list or a blockquote. A table reached
  only at depth is exactly the one a naive "top-level children" walk would miss.
- The wrapper does not eat the table. Replacing a child in an ElementTree while iterating
  over its parent is how you silently drop siblings, so the content is checked, not just
  the tag.
- The table keeps ``width:100%`` behaviour, i.e. we wrapped it rather than making the
  ``<table>`` itself ``display:block`` -- the other way to get ``overflow`` to apply, which
  forfeits table layout (columns stop aligning across rows).
- ``_SHELL`` actually carries the ``.table-scroll`` rules, with its ``.format()`` braces
  doubled. A wrapper with no CSS behind it is a no-op that still passes a markup check.
- Text around a table is untouched, and a page with no table renders no wrappers.

Skips cleanly when markdown / fastapi aren't installed, matching the other docs tests.

Run: python tests/test_docs_table_scroll.py   (or under pytest)
"""
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

os.environ.setdefault("JWT_SECRET_KEY", "test-secret-docs-table-scroll")

try:
    import markdown  # noqa: F401  (presence check; the renderer imports it lazily)
    from web_dashboard.api.docs_pages import _SHELL, _render_markdown
except Exception as exc:  # markdown / fastapi absent outside CI
    _render_markdown = None
    _SHELL = None
    _IMPORT_ERR = exc

_TABLE = "| Target | Runner |\n|---|---|\n| AWS | ECS Fargate task |\n"

_WRAPPED = re.compile(r'<div class="table-scroll">\s*<table>')


def _tables(html):
    return html.count("<table")


def _wrappers(html):
    return len(_WRAPPED.findall(html))


def test_a_table_is_wrapped_in_a_scroll_container():
    html = _render_markdown(_TABLE)
    assert _tables(html) == 1, f"expected one table, got: {html}"
    assert _wrappers(html) == 1, (
        f"the table is not inside a div.table-scroll, so it will widen the page on a "
        f"phone instead of scrolling itself:\n{html}")


def test_no_table_escapes_the_wrapper():
    """Two tables and a heading between them -- the second is the one a loop that stops
    at the first match would leave bare."""
    html = _render_markdown(f"{_TABLE}\n## Then\n\n{_TABLE}")
    assert _tables(html) == 2, html
    assert _wrappers(html) == 2, f"only {_wrappers(html)} of 2 tables wrapped:\n{html}"
    assert not re.search(r'(?<!<div class="table-scroll">)<table', html), (
        f"a <table> appears outside a wrapper:\n{html}")


def test_a_nested_table_is_wrapped_too():
    """A table inside a blockquote is not a child of the root, so a walk that only looks
    at the document's top-level children would skip it and leave the bug in place on any
    page that indents a table."""
    html = _render_markdown("> | a | b |\n> |---|---|\n> | 1 | 2 |\n")
    assert _tables(html) == 1, html
    assert _wrappers(html) == 1, f"a table inside a blockquote was left bare:\n{html}"
    assert "<blockquote>" in html, html


def test_the_wrapper_keeps_the_table_whole():
    """Replacing a child under an element mid-iteration drops siblings. Check the rows
    survived, not just that a <table> tag is present."""
    html = _render_markdown(_TABLE)
    body = re.search(r'<div class="table-scroll">(.*?)</div>', html, re.S)
    assert body, f"no wrapper found:\n{html}"
    inner = body.group(1)
    for fragment in ("<thead>", "<tbody>", "<th>Target</th>", "<td>ECS Fargate task</td>"):
        assert fragment in inner, (
            f"{fragment} is missing from the wrapped table -- the wrapper ate content:"
            f"\n{inner}")


def test_text_around_a_table_is_untouched():
    html = _render_markdown(f"Before.\n\n{_TABLE}\nAfter.\n")
    assert "<p>Before.</p>" in html, html
    assert "<p>After.</p>" in html, html
    assert _wrappers(html) == 1, html


def test_a_page_with_no_table_gets_no_wrapper():
    html = _render_markdown("# Title\n\nJust a paragraph.\n")
    assert "table-scroll" not in html, (
        f"a table-less page should render no wrappers:\n{html}")


def test_the_wrapper_survives_the_relative_link_rewrite():
    """Both extensions are treeprocessors on the same tree. Wrapping must not hide the
    links inside the table's cells from the rewriter, which finds <a> by iterating the
    tree -- one extra div deep is only safe if that walk is recursive."""
    md = "| Doc |\n|---|\n| [Cloud VMs](cloud-vms.md) |\n"
    html = _render_markdown(md, page="profiles/demo/ot-demo-cell")
    assert _wrappers(html) == 1, html
    assert 'href="/docs/profiles/demo/cloud-vms"' in html, (
        f"the link inside the wrapped table was not rewritten to a /docs route:\n{html}")


def test_the_shell_styles_the_wrapper():
    """A wrapper class with no CSS behind it is a no-op that still passes a markup check.
    ``_SHELL`` is a ``.format()`` template, so these braces have to be doubled -- a single
    brace here raises at format time, which the render check below would catch."""
    assert ".table-scroll {{ overflow-x:auto;" in _SHELL, (
        "_SHELL has no .table-scroll overflow rule, so the wrapper does nothing")
    assert ".table-scroll table {{ margin:0; }}" in _SHELL, (
        "the wrapped table keeps its own vertical margin, which stacks with the "
        "wrapper's and doubles the gap above every table")


def test_the_shell_still_formats_with_the_new_rules():
    """The brace-doubling above is only correct if the template still renders."""
    from web_dashboard.api.docs_pages import _shell
    out = _shell("A page", _render_markdown(_TABLE))
    assert "{{" not in out and "}}" not in out, (
        "literal braces survived .format() -- a CSS rule is over-escaped")
    assert ".table-scroll {" in out and "overflow-x:auto" in out, out[:400]
    assert _wrappers(out) == 1, "the shell lost the table wrapper"


def test_the_table_keeps_filling_its_width():
    """We wrapped the table rather than making the <table> itself display:block, which is
    the other way to get overflow to apply and silently forfeits table layout. Pin that
    the shell still sizes tables to 100% and never sets display on them."""
    assert "table {{ border-collapse:collapse; width:100%;" in _SHELL, (
        "the table width rule changed; a wrapped table that no longer fills its "
        "container will look broken on desktop")
    # Only the rules that actually select a table -- `header img.logo` is display:block
    # and always has been, which is none of this test's business.
    for selector, body in re.findall(r"([^{}\n]+?) \{\{([^{}]*)\}\}", _SHELL):
        if "table" not in selector:
            continue
        assert "display" not in body, (
            f"the rule `{selector.strip()}` sets display ({body.strip()}) -- if that "
            f"makes the table a block box, width:100% stops filling and its columns "
            f"stop aligning across rows, which is the trap the wrapper exists to avoid")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    if _render_markdown is None:
        print(f"SKIP all: markdown/fastapi not importable ({_IMPORT_ERR})")
        raise SystemExit(0)
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {fn.__name__}: {exc}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    raise SystemExit(1 if failures else 0)
