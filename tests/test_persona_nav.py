"""The nav pins by persona. It reorders; it never removes, and it must BUY width.

Two facts make this slice delicate, and both are measurements rather than opinions:

  * ``base.html``'s ``x-ref="navRow"`` spans the brand, the links AND the user menu, and
    ``responsiveNav`` folds the ENTIRE row into the flyout drawer the moment it overflows.
    ``tests/test_profile_theme`` records 61px of headroom at 1280px on the brand side, and
    notes a variant that once left 2px. So any control added anywhere in that row is
    spending a budget that is already nearly gone. The pinned/"More" split is the one
    affordance that gives width back: six pinned links plus one button is far narrower than
    the twenty-link row.
  * ``_nav_links.html`` is rendered TWICE — inline in the bar, and in the drawer. A reorder
    scoped to ``document`` rather than to the inline ref would scramble the drawer, which is
    the guaranteed-complete escape hatch that makes "a persona can never make a page
    unreachable" true at the DOM level rather than merely by intention.

So the invariants here are: the link list is untouched, the persona never appears in the
template that decides which links exist, the reorder is ref-scoped and runs before the
width read, and neutral is byte-identical to before any of this existed.

Runs under pytest, or standalone:
    python tests/test_persona_nav.py
"""
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-persona-nav")

_TPL = os.path.join(_ROOT, "web_dashboard", "templates")
_NAV = os.path.join(_TPL, "_nav_links.html")
_BASE = os.path.join(_TPL, "base.html")
_APP_JS = os.path.join(_ROOT, "web_dashboard", "static", "js", "app.js")
_MAIN = os.path.join(_ROOT, "web_dashboard", "main.py")

# The pinned row has to stay narrower than the row it replaces or the whole slice is
# pointless.
#
# MEASURED, in a real browser at a 1280px viewport with Tailwind loaded, against the
# widest realistic case (an admin on a demo instance with every integration enabled, the
# demo/community brand lockup, 28 links). Available width inside the container: 1216px.
#
#   persona            links block   natural row   headroom   folds?
#   neutral (today)        2035px        2506px      -1290      YES
#   cloudops (7 pins)       503px        1008px       +208      no
#   devops   (6 pins)       539px        1044px       +172      no
#   itops    (6 pins)       525px        1030px       +186      no
#   hypervisor (6)          514px        1019px       +197      no
#   security (6 pins)       470px         975px       +241      no
#   sre      (5 pins)       464px         969px       +247      no
#   ot       (6 pins)       437px         942px       +274      no
#   dba      (5 pins)       434px         939px       +277      no
#
# Two things that table says. First, the split does not merely add margin: a fully-enabled
# admin instance ALREADY overflows by 1290px at 1280px today, so its inline nav is
# permanently folded into the drawer, and pinning is what makes an inline nav exist at all
# for that configuration. Second, the pinned link block is 434-539px against 2035px --
# a ~75% reduction -- which is the headroom every later slice spends from, including the
# /use-cases link.
#
# 8 pins lands around 600px, still far inside 1216. This is a budget, not a style rule:
# exceeding it starts giving the width back.
_MAX_PINS = 8


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _nav_anchors():
    """``[(data_nav_or_None, whole_tag)]`` for every ``<a>`` in the shared link list."""
    src = _read(_NAV)
    out = []
    for m in re.finditer(r"<a\b[^>]*>", src, re.S):
        tag = m.group(0)
        nav = re.search(r'data-nav="([a-z0-9_]+)"', tag)
        out.append((nav.group(1) if nav else None, tag))
    return out


def _nav_ids():
    return {nav for nav, _tag in _nav_anchors() if nav}


# ── the link list itself is untouched ────────────────────────────────────────

def test_every_nav_link_is_addressable():
    """A link with no data-nav can never be pinned, and the failure is silent: the persona
    just quietly does not pin it."""
    missing = [tag[:70] for nav, tag in _nav_anchors() if not nav]
    assert not missing, f"nav anchors with no data-nav: {missing}"
    assert len(_nav_anchors()) >= 30, "the nav link list shrank unexpectedly"


def test_data_nav_ids_are_unique():
    navs = [nav for nav, _t in _nav_anchors() if nav]
    dupes = {n for n in navs if navs.count(n) > 1}
    assert not dupes, f"duplicate data-nav ids: {dupes} — a pin would move only the first"


def test_the_nav_template_knows_nothing_about_personas():
    """The file that decides which links EXIST must never hear about personas.

    This is what makes "a persona cannot hide a link" structural rather than a promise:
    the persona is applied by moving DOM nodes afterwards, so there is no code path by
    which it could omit one.
    """
    src = _read(_NAV)
    assert "persona" not in src.lower(), (
        "_nav_links.html mentions personas. Pinning happens in responsiveNav by moving "
        "nodes; this template must only ever decide which links exist.")


def test_the_overflow_menu_is_not_in_the_shared_link_list():
    """_nav_links.html is included twice. A "More" container inside it would give the
    flyout drawer one too — and the drawer's job is to be the flat, complete list."""
    src = _read(_NAV)
    for token in ("navMore", "moreOpen", "moreCount", "More ▾"):
        assert token not in src, f"_nav_links.html contains {token!r}"


def test_the_drawer_still_renders_the_complete_shipped_list():
    """Persona reordering is inline-row only. The drawer is the escape hatch."""
    src = _read(_BASE)
    drawer = src.split('x-show="compact && mobileNav"', 1)[1].split("</div>", 1)[0]
    assert "_nav_links.html" in src
    assert "navInline" not in drawer and "navMore" not in drawer, \
        "the compact drawer was given the pinning refs — it must stay the full list"
    assert src.count("{% include '_nav_links.html' %}") == 2, \
        "the nav list is no longer rendered exactly twice"


# ── every pin is real, and the row stays narrow ──────────────────────────────

def test_every_persona_pin_names_a_real_nav_link():
    from web_dashboard.services import personas as P
    known = _nav_ids()
    assert known, "could not parse any data-nav ids"
    for p in P.all_personas():
        for pin in p.nav_pins:
            assert pin in known, (
                f"{p.key}.nav_pins names '{pin}', which is not a data-nav id in "
                f"_nav_links.html")


def test_no_persona_pins_more_than_the_width_budget():
    from web_dashboard.services import personas as P
    for p in P.all_personas():
        assert len(p.nav_pins) <= _MAX_PINS, (
            f"{p.key} pins {len(p.nav_pins)} links; the budget is {_MAX_PINS}. Beyond it "
            "the pinned row stops being narrower than the row it replaces and the split "
            "costs width instead of buying it.")


def test_pins_are_unique_within_a_persona():
    """A repeated pin would move the same node twice and silently reorder the rest."""
    from web_dashboard.services import personas as P
    for p in P.all_personas():
        assert len(set(p.nav_pins)) == len(p.nav_pins), f"{p.key} repeats a nav pin"


def test_every_persona_pins_the_dashboard():
    """Home is how you reach the lens that changes persona. A focus you cannot leave from
    the nav is a focus that has taken something away."""
    from web_dashboard.services import personas as P
    for p in P.all_personas():
        if p.nav_pins:
            assert "dashboard" in p.nav_pins, f"{p.key} does not pin the dashboard"


# ── the reorder is scoped, ordered, and non-destructive ──────────────────────

def _apply_pins_body():
    src = _read(_APP_JS)
    return src.split("applyPins() {", 1)[1].split("\n        },", 1)[0]


def test_the_reorder_is_scoped_to_the_inline_row():
    """Both renders of _nav_links.html are in the DOM at once. A document-wide selector
    would move the drawer's links into the inline row's overflow menu and destroy the
    complete list."""
    body = _apply_pins_body()
    assert "this.$refs.navInline" in body, "applyPins does not resolve the inline row by ref"
    assert "document.querySelector" not in body and "document.getElement" not in body, (
        "applyPins reaches for document — it would scramble the flyout drawer, which is "
        "the only guarantee that every page stays reachable")


def test_the_pins_are_applied_before_the_width_is_read():
    """Measuring first decides the fold on the UNPINNED width and throws away the entire
    width benefit for that page load."""
    src = _read(_APP_JS)
    measure = src.split("const measure = () => {", 1)[1].split("\n            };", 1)[0]
    assert "this.applyPins()" in measure, "measure() never applies the pins"
    i_pin = measure.index("this.applyPins()")
    i_read = measure.index("scrollWidth")
    assert i_pin < i_read, (
        "applyPins runs after the scrollWidth read, so the fold decision uses the "
        "pre-pin width")


def test_neutral_changes_no_dom_at_all():
    body = _apply_pins_body()
    assert re.search(r"if\s*\(\s*!pins\.length\s*\)\s*return", body), (
        "applyPins has no early return for an empty pin list — the neutral persona must "
        "leave the DOM byte-identical to before this existed")


def test_the_links_are_moved_not_cloned():
    """cloneNode would drop the Alpine x-show bindings that hide the admin-only links from
    a non-admin, so every user would see Users, Groups and Secrets in the overflow menu."""
    body = _apply_pins_body()
    assert "cloneNode" not in body and "innerHTML" not in body, \
        "applyPins clones or rewrites markup; it must MOVE the live nodes"
    assert "insertBefore" in body and "appendChild" in body


def test_no_link_is_dropped_by_the_split():
    """Every link is either pinned or moved to the overflow menu. Never neither."""
    body = _apply_pins_body()
    assert "remove()" not in body and "removeChild" not in body, \
        "applyPins removes a node — a persona may reorder, never subtract"


def test_the_pinning_runs_once():
    body = _apply_pins_body()
    assert "this._pinned" in body, \
        "applyPins is not guarded against re-running; a resize would re-walk a pinned row"


def test_the_overflow_button_counts_only_visible_links():
    """Most unpinned links are admin-only. Without a visible-count a non-admin gets a
    "More" button that opens an empty popover.

    And the visibility test must be the element's OWN computed display, not offsetParent.
    The menu is `x-show="moreOpen"`, so it is display:none when this runs, and an
    offsetParent check therefore returns 0 for EVERY link -- the button would never appear
    and ~22 links would be reachable only from the drawer. Verified in a browser with
    Alpine: inside a hidden menu, a visible link has offsetParent === null and offsetWidth
    === 0 but computed display 'inline', while one its own x-show hid computes to 'none'.
    """
    src = _read(_APP_JS)
    body = src.split("countMore() {", 1)[1].split("\n        },", 1)[0]
    assert "getComputedStyle" in body and "display" in body, \
        "countMore does not test the element's own computed display"
    assert "offsetParent" not in body, (
        "countMore tests offsetParent, which is ancestor-dependent: the overflow menu is "
        "display:none when this runs, so every link would count as hidden and the More "
        "button would never render")
    measure = src.split("const measure = () => {", 1)[1].split("\n            };", 1)[0]
    assert "this.countMore()" in measure, (
        "countMore is not called from measure(), which is the one moment the inline row is "
        "forced visible and can be measured")


def test_the_overflow_menu_hides_when_empty():
    src = _read(_BASE)
    assert 'x-show="moreCount > 0"' in src, \
        "the More button is not gated on there being visible overflow links"


# ── the popover has to escape the row's clip ─────────────────────────────────
#
# The button shipped working and the popover shipped invisible, which read as a dead
# button. navRow is `overflow-hidden` -- that is what makes responsiveNav's
# `scrollWidth > clientWidth` fold read meaningful -- and the popover was an absolutely
# positioned child of a control inside it, hanging below a 64px row. Measured in a browser:
# it opened at its full 132px and 122px of that was clipped away, leaving a sliver in the
# nav's own colour. z-index cannot help; overflow clipping is not a stacking question.

def _nav_row_span(src):
    """``(start, end)`` character offsets of the ``x-ref="navRow"`` element in base.html.

    A depth walk over ``<div``/``</div>`` rather than a regex, because "is this element
    inside that one" is the entire question and no flat pattern can answer it.
    """
    start = src.index('x-ref="navRow"')
    depth = 0
    i = src.rindex("<div", 0, start)
    for m in re.finditer(r"<div\b|</div>", src[i:]):
        depth += 1 if m.group(0).startswith("<div") else -1
        if depth == 0:
            return i, i + m.end()
    raise AssertionError("navRow never closes -- base.html div nesting is unbalanced")


def test_the_popover_is_not_inside_the_clipped_row():
    src = _read(_BASE)
    start, end = _nav_row_span(src)
    row = src[start:end]
    assert "overflow-hidden" in row, (
        "navRow lost `overflow-hidden`. That is not free: responsiveNav decides the fold "
        "with `scrollWidth > clientWidth`, which needs the clip to mean anything. If this "
        "was removed deliberately, re-measure the fold before trusting it.")
    assert 'x-ref="navMore"' not in row, (
        "the overflow popover is back inside navRow, which is `overflow-hidden`. It will "
        "open at full size and be clipped to the 64px row -- the More button will look "
        "dead. Keep the panel a child of <nav> and let positionMore() anchor it.")
    assert 'x-ref="navMore"' in src[end:], \
        "the overflow popover left base.html entirely"
    assert 'x-ref="moreBtn"' in row, \
        "the More BUTTON left the inline row; it has to be in the row it measures"


def test_the_popover_is_hidden_when_the_row_folds():
    """The button lives in navInline and vanishes with it in compact mode. The panel does
    not, so it needs its own guard or a popover left open across a resize outlives the
    trigger that opened it."""
    src = _read(_BASE)
    panel = src[src.index('x-ref="navMore"'):]
    show = re.search(r'x-show="([^"]+)"', panel).group(1)
    assert "moreOpen" in show and "!compact" in show, (
        f'the popover\'s x-show is "{show}" -- it must test !compact as well as moreOpen')


def test_the_popover_caps_its_height_and_scrolls():
    """A persona pins 5-7 links, so the other ~25 land in here. Measured at a 720px
    viewport that popover is 1012px tall: without a cap its bottom third leaves the screen
    and nothing scrolls it, which loses those links exactly as thoroughly as the clip did.
    """
    src = _read(_BASE)
    panel = src[src.index('x-ref="navMore"'):]
    panel = panel[:panel.index("</div>")]
    assert "max-h-" in panel and "overflow-y-auto" in panel, (
        "the overflow popover has no height cap or no scroll; with ~25 links it runs off "
        "the bottom of the viewport")


def test_the_anchoring_does_not_read_el():
    """``$el`` is per-expression, not per-component.

    positionMore() is called from the More button's own ``@click``, and in that evaluation
    ``$el`` is the BUTTON -- so ``$el.right - btn.right`` is 0 and the anchor silently
    bails every single time the user presses the thing. That reproduced the original
    symptom one layer down: the popover opened, unpositioned, at the nav's left edge.
    Read the containing block off the panel itself instead.
    """
    src = _read(_APP_JS)
    assert "positionMore() {" in src, (
        "responsiveNav has no positionMore(). The popover lives outside navRow to escape "
        "its clip, so something has to supply the `right` offset that keeps it under its "
        "button; without it the panel opens at the nav's left edge.")
    body = src.split("positionMore() {", 1)[1].split("\n        },", 1)[0]
    assert "$el" not in body, (
        "positionMore reads $el, which resolves to whichever element the caller was "
        "evaluated on -- the More button, when called from its @click")
    assert "parentElement" in body, \
        "positionMore does not resolve the popover's containing block"
    assert "this.$refs.moreBtn" in body, "positionMore does not measure the button by ref"


def test_the_anchor_is_recomputed_when_the_popover_opens():
    src = _read(_APP_JS)
    assert "toggleMore() {" in src, \
        "responsiveNav has no toggleMore(), so nothing re-anchors the popover on open"
    body = src.split("toggleMore() {", 1)[1].split("\n        },", 1)[0]
    assert "this.positionMore()" in body, (
        "toggleMore does not re-anchor on open. The button moves whenever the row is "
        "re-laid out, so a position computed once at init goes stale.")
    assert 'x-ref="moreBtn"' in _read(_BASE) and "toggleMore()" in _read(_BASE), \
        "base.html does not route the More button through toggleMore()"


# ── the pins reach the page without a per-navigation request ─────────────────

def test_the_context_processor_supplies_the_pins():
    """The nav renders on every page. Fetching /api/persona from base.html would be a
    request per navigation for a value the server already holds."""
    src = _read(_MAIN)
    body = src.split("def _profile_context(", 1)[1].split("\ntemplates = ", 1)[0]
    assert "personas.resolve(request)" in body, \
        "_profile_context does not resolve the persona from the request"
    for key in ("persona_nav_pins", "persona_label", "persona"):
        assert f'"{key}"' in body, f"_profile_context does not supply {key}"


def test_the_pins_reach_the_template_as_data_not_as_a_key():
    """base.html must read the joined pin list, never branch on which persona it is."""
    src = _read(_BASE)
    assert "{{ persona_nav_pins }}" in src, \
        "base.html does not emit the pin list for responsiveNav to read"
    from web_dashboard.services import personas as P
    for k in P.VALID_PERSONAS:
        assert f"'{k}'" not in src and f'"{k}"' not in src, \
            f"base.html hard-codes the persona key {k!r}"


def test_the_context_processor_still_supplies_the_theme_and_flags():
    """Regression guard: the persona is additive. #664 was three routes rendering a nav
    with fifteen links missing because a flag never reached the template."""
    src = _read(_MAIN)
    body = src.split("def _profile_context(", 1)[1].split("\ntemplates = ", 1)[0]
    assert '"install_profile"' in body and '"theme"' in body and "**_feature_flags()" in body


def test_the_nav_pins_are_a_string_not_a_list():
    """It lands in an HTML attribute. A Python list would render as "['a', 'b']" and every
    id would arrive with quotes and brackets attached."""
    from web_dashboard.services import personas as P
    src = _read(_MAIN)
    body = src.split("def _profile_context(", 1)[1].split("\ntemplates = ", 1)[0]
    assert '",".join(persona.nav_pins)' in body, \
        "persona_nav_pins is not comma-joined into a string"
    # And the JS splits on the same separator.
    assert "split(',')" in _apply_pins_body()
    for p in P.all_personas():
        for pin in p.nav_pins:
            assert "," not in pin and '"' not in pin, f"pin {pin!r} would break the attribute"


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
