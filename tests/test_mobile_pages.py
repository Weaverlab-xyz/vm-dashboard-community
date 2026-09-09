"""The pages behind the nav bar, on a phone.

Fixing the header only moved the problem one layer down. Measured in Chromium at 393px
across all 38 page templates, with every `x-show` section forced visible so the audit saw
62 tables rather than the 6 that render with no data:

    33 tables had columns nobody could reach.

Not squeezed, not wrapped, not scrollable-to: the card pattern this app uses everywhere is
`rounded-xl ... overflow-hidden` around a table, the clip is there to keep the first and
last rows inside the rounded corners, and on a phone it silently ate everything past the
fold. Inventory's table is 857px wide in a 359px box; hyperv's is 837px. Owner,
environment, cost, last-seen — gone, with no scrollbar to say a column existed.

Four more things followed from the same audit:

  * four pages made the whole document scroll sideways (xcpng 512px, hyperv 477px, aws and
    azure 413px), each from one `flex` row that would not wrap;
  * 363 <code>/<pre> elements, 15 of which said anything about wrapping — the rest hold one
    unbroken token (an ARN, a digest, a `docker run` line) and a token with no break
    opportunity sets the page's minimum width;
  * 71 of 77 non-responsive `grid-cols-*` elements contained a form control, and 70 of
    those gave the field a column under 160px — 21 of them under 120px;
  * of 106 modal overlays, exactly one did not fit, and it was a code block.

The systemic ones are fixed once, in base.html, rather than 50 times by hand: this file
pins those rules and the per-page classes, because the CSS lives nowhere near the templates
it rescues and a future table would otherwise be born broken.

Runs under pytest, or standalone:
    python tests/test_mobile_pages.py
"""
import os
import re
import sys
import glob

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

_TPL = os.path.join(_ROOT, "web_dashboard", "templates")
_BASE = os.path.join(_TPL, "base.html")


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _pages():
    """Every template that renders as a page (i.e. extends the shell)."""
    out = []
    for path in sorted(glob.glob(os.path.join(_TPL, "**", "*.html"), recursive=True)):
        src = _read(path)
        if 'extends "base.html"' in src:
            out.append((os.path.relpath(path, _TPL), src))
    return out


def _class_attrs(src):
    return [m.group(1) for m in re.finditer(r'class="([^"]*)"', src, re.S)]


# ── the shell carries the rules that rescue every page ───────────────────────

def test_a_table_in_a_clipping_card_scrolls_instead():
    """The one rule standing between ~50 tables and their missing columns."""
    src = _read(_BASE)
    rule = re.search(r"main div\.overflow-hidden:has\(> table\)\s*\{([^}]*)\}", src)
    assert rule, (
        "base.html lost the rule that turns a table card's clip into a scroll. Without it "
        "every `rounded-xl ... overflow-hidden` card silently truncates its table on a "
        "phone -- 33 of them did, inventory losing 498px of an 857px table.")
    body = rule.group(1)
    assert "overflow-x" in body, f"the rule does not set overflow-x: {body}"
    assert "overflow-y" not in body, (
        "the rule touches overflow-y. The card clips vertically on purpose -- that is what "
        "keeps the first and last rows inside the rounded corners.")


def test_the_table_rule_is_scoped_to_cards_that_are_table_cards():
    """`:has(table)` would also catch a card holding a header AND a table somewhere below,
    and scroll the header sideways along with it. A card whose OWN child is a table is a
    table card, and its clip is that table's clip."""
    src = _read(_BASE)
    assert ":has(> table)" in src, \
        "the table rule matches descendants rather than a direct child"


def test_long_unbroken_tokens_cannot_set_the_page_width():
    src = _read(_BASE)
    rule = re.search(r"main code, main pre\s*\{([^}]*)\}", src)
    assert rule, "base.html has no rule letting code spans break"
    assert "anywhere" in rule.group(1), (
        "the rule does not use `overflow-wrap: anywhere`. Only `anywhere` counts toward "
        "min-content, and min-content is what an unbroken ARN uses to widen the page; "
        f"`break-word` would let the token still claim the width: {rule.group(1)}")
    pre = re.search(r"(?m)^\s*main pre\s*\{([^}]*)\}", src)
    assert pre and "overflow-x" in pre.group(1), (
        "a <pre> under `white-space: pre` cannot wrap at all, so it needs its own scroll "
        "or it widens the page instead")


# ── and the pages hold up their end ──────────────────────────────────────────

def test_no_page_grid_is_left_unstacked_on_a_phone():
    """`grid-cols-2` with no breakpoint is two ~180px columns on a 393px screen, and 21 of
    these were handing a form control less than 120px. `grid-cols-1 sm:grid-cols-N` is
    byte-identical at >=640px -- verified in Chromium, all 38 pages, at 640 and 1280."""
    bare = re.compile(r"(?<![\w:-])grid-cols-([234])(?![\w-])")
    offenders = []
    for rel, src in _pages():
        for cls in _class_attrs(src):
            if bare.search(cls) and "sm:grid-cols-" not in cls:
                offenders.append(f"{rel}: {cls[:60]}")
    assert not offenders, (
        "these grids keep their column count on a phone:\n  " + "\n  ".join(offenders[:12]))


def test_the_toolbars_that_widened_the_page_still_wrap():
    """Each of these was one `flex` row long enough to set the document's width -- the
    page itself scrolled sideways, which is the most obviously broken thing a phone
    browser can do. `flex-wrap` is inert while the content fits, so none of it reaches a
    desktop."""
    expected = {
        "xcpng/index.html": "flex flex-wrap items-center gap-3 mb-4",
        "hyperv/index.html": "flex flex-wrap items-center gap-3 mb-4",
        "nutanix/index.html": "flex flex-wrap items-center gap-3 mb-4",
        "azure/index.html": "flex flex-wrap items-center gap-2 mb-4",
        "aws/index.html": "flex flex-wrap gap-1 mb-6 bg-gray-100 p-1 rounded-xl w-fit",
        "settings.html": "flex flex-wrap gap-2",
        "pov/index.html": "flex flex-wrap items-start justify-between gap-4",
    }
    for rel, cls in expected.items():
        src = _read(os.path.join(_TPL, rel))
        assert f'class="{cls}"' in src, f"{rel} no longer carries {cls!r}"


def test_the_underline_tab_strips_scroll_rather_than_wrap():
    """A wrapped tab strip puts a second row of tabs below the border that marks the
    selected one. Scrolling is the phone convention and keeps the underline meaningful."""
    for rel, cls in (("containers/index.html", "flex gap-1 -mb-px overflow-x-auto"),
                     ("nutanix/index.html", "flex gap-1 mb-6 border-b border-gray-200 overflow-x-auto"),
                     ("proxmox/index.html", "flex gap-1 mb-6 border-b border-gray-200 overflow-x-auto")):
        src = _read(os.path.join(_TPL, rel))
        assert f'class="{cls}"' in src, f"{rel} tab strip no longer scrolls: expected {cls!r}"


def test_a_no_shrink_group_does_not_refuse_to_shrink_on_a_phone():
    """containers/ keeps its buttons from being squeezed by the paragraph beside them,
    which is right on a desktop. On a phone the row wraps, the group lands on its own
    line, and a group that will not shrink cannot then wrap inside it: it stayed 386px in
    a 361px column and took the page with it."""
    src = _read(os.path.join(_TPL, "containers", "index.html"))
    assert 'class="flex flex-wrap items-center gap-2 sm:flex-shrink-0 sm:ml-4"' in src, (
        "containers/ lost the breakpoint on its no-shrink button group")
    assert 'class="flex items-center gap-2 flex-shrink-0 ml-4"' not in src, \
        "the unconditional flex-shrink-0 is back"


def test_every_page_still_extends_the_shell_that_carries_the_rules():
    """The rules above live in base.html. A page that stopped extending it would quietly
    opt out of all of them."""
    pages = _pages()
    assert len(pages) >= 38, f"only {len(pages)} page templates found; expected 38+"


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
