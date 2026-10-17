"""Wiring tests for the shared tab contract.

Every page built on a `{% set tabs = [...] %}` list renders its bar with the `tab_bar`
macro (templates/partials/tab_bar.html) and gets select()/pickTab() from
`window.tabState` (static/js/app.js). Nine pages used to carry their own copy of both,
each with a comment saying "same contract as remoteAgentsPage.select"; a fix to that
contract had to land nine times, and a tenth page would have made it ten. The behaviour
itself is exercised in tests/template_helpers_check.js; these pin that every page still
uses it, and that the page's own cross-links name tabs that exist.

Text checks, no app imports, so it runs on a checkout without the requirements
installed. Runs under pytest or standalone:  python tests/test_tab_contract.py
"""
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TEMPLATES = os.path.join(_ROOT, "web_dashboard", "templates")

# A slug in the page's tab list, whether the list is a literal or built with append().
_SLUG = re.compile(r"'slug':\s*'([\w-]+)'")
# Places the page names a tab by slug: a select() call, a #fragment link that calls one,
# and a cross-tab $dispatch('select-tab', ...) from an included panel.
_SELECT = re.compile(r"\bselect\('([\w-]+)'")
_DISPATCH = re.compile(r"\$dispatch\('select-tab',\s*'([\w-]+)'\)")
_INCLUDE = re.compile(r'{%\s*include\s+"([^"]+)"')


def _read(rel: str) -> str:
    with open(os.path.join(_TEMPLATES, rel), encoding="utf-8") as fh:
        return fh.read()


def _tabbed_pages():
    for root, _dirs, files in os.walk(_TEMPLATES):
        for f in files:
            if not f.endswith(".html"):
                continue
            rel = os.path.relpath(os.path.join(root, f), _TEMPLATES).replace("\\", "/")
            if rel.startswith("partials/"):
                continue
            src = _read(rel)
            if re.search(r"{%\s*set tabs\s*=", src):
                yield rel, src


def test_the_tabbed_pages_are_found():
    # A guard on the sweep itself: if the `set tabs` convention changes, every check below
    # would pass by finding nothing.
    pages = dict(_tabbed_pages())
    assert len(pages) >= 8, sorted(pages)
    assert "agents/index.html" in pages and "storage/index.html" in pages


def test_every_tabbed_page_uses_the_shared_bar_and_state():
    failures = []
    for rel, src in _tabbed_pages():
        if 'import tab_bar' not in src or "tab_bar(tabs" not in src:
            failures.append(f"{rel}: renders its own tab bar instead of the tab_bar macro")
        if "...tabState(" not in src:
            failures.append(f"{rel}: does not spread window.tabState()")
        if 'role="tablist"' in src:
            failures.append(f"{rel}: has a hand-written role=\"tablist\" beside the macro")
    assert not failures, "\n  ".join([""] + failures)


def test_no_page_reimplements_the_contract():
    """A page may wrap select() (agents dispatches agents-tab-shown on a switch), but it
    goes through selectTab(); setting activeTab or rewriting the fragment by hand is the
    copy this replaced."""
    failures = []
    for rel, src in _tabbed_pages():
        # Comments may still name the mechanism; code may not.
        code = "\n".join(line for line in src.splitlines()
                         if not line.strip().startswith("//"))
        if "replaceState" in code:
            failures.append(f"{rel}: rewrites the fragment itself")
        # The parse-and-pick idiom each page used to carry. Reading the hash for one
        # page-specific alias (POV's old #bt-tenants anchor) is fine; picking from it is not.
        if "(window.location.hash || '').replace" in code:
            failures.append(f"{rel}: picks its tab from the fragment instead of pickTab()")
        m = re.search(r"\n    select\(slug, push = true\) \{\n(.*?)\n    \},", src, re.S)
        if m and "this.selectTab(" not in m.group(1):
            failures.append(f"{rel}: overrides select() without going through selectTab()")
    assert not failures, "\n  ".join([""] + failures)


def test_every_named_tab_exists_on_its_page():
    """A link to a tab the page does not have is inert in the browser, since select()
    refuses it, so a renamed or removed slug fails silently."""
    failures = []
    for rel, src in _tabbed_pages():
        slugs = set(_SLUG.findall(src))
        named = set(_SELECT.findall(src))
        panels = [src] + [_read(inc) for inc in _INCLUDE.findall(src)
                          if os.path.exists(os.path.join(_TEMPLATES, inc))]
        for p in panels:
            named |= set(_DISPATCH.findall(p))
        for slug in sorted(named - slugs):
            failures.append(f"{rel}: names tab '{slug}', which its tab list does not have")
    assert not failures, "\n  ".join([""] + failures)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok   " + name)
