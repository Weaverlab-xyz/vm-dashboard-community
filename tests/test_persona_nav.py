"""The nav pins by persona. It reorders; it never removes.

The nav used to be two things — an inline link row in the bar, folding into a flyout
drawer once the row overflowed — and pinning existed as much to BUY the bar width as to
curate: six pinned links plus a "More" button is far narrower than a twenty-link row.
The measurement in ``_MAX_PINS`` below is what retired that arrangement: a fully-enabled
admin instance overflows a 1280px viewport by 1290px, so the inline row was already
folded away for the configuration most people run, and the drawer was already the
complete list. There is one nav now, and pinning is purely what it always claimed to be.

That makes the central invariant STRUCTURAL rather than merely intended. There is no
second container to move an unpinned link into and no width pressure to move it for: the
links are reordered inside the one list that exists, so "a persona can never make a page
unreachable" needs no escape hatch to be true. The rest of this file pins that — the link
list is untouched, the persona never appears in the template that decides which links
exist, the reorder is ref-scoped and runs once, and neutral is byte-identical to before
any of this existed.

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

# The pinned block is a SHORTLIST at the top of a ~32-link drawer, above a divider. Past
# eight it stops reading as "the handful this role reaches for" and starts reading as a
# second copy of the list, which is the one thing it must not become.
#
# The number is inherited from the width budget the old folding nav had, and that
# measurement is worth keeping because it is why there is no folding nav any more.
# MEASURED, in a real browser at a 1280px viewport with Tailwind loaded, against the
# widest realistic case (an admin on a demo instance with every integration enabled, 28
# links). Available width inside the container: 1216px.
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
# The top row is the one that mattered: a fully-enabled admin instance overflowed by
# 1290px, i.e. its inline nav was permanently folded into the drawer, and pinning was
# what made an inline nav exist at all for that configuration. Rather than keep two
# navigation models so that a minority of configurations could have the narrower one, the
# app now ships the drawer for everybody.
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


def test_the_shared_link_list_carries_no_nav_machinery():
    """It is a flat, complete list of links and nothing else. Every container, ref and
    piece of state belongs to base.html, which is what makes the list safe to render
    anywhere — including the second render this file used to have to defend against."""
    src = _read(_NAV)
    for token in ("navDrawer", "navMore", "moreOpen", "moreCount", "More ▾", "data-nav-pins"):
        assert token not in src, f"_nav_links.html contains {token!r}"


def test_the_drawer_renders_the_complete_shipped_list_once():
    """One render, one list. The reorder happens inside it, so there is nowhere for a
    link to go that is not still in it."""
    src = _read(_BASE)
    assert src.count("{% include '_nav_links.html' %}") == 1, \
        "the nav list is no longer rendered exactly once"
    assert "navMore" not in src, \
        "base.html grew a second link container; the drawer must stay the whole list"


# ── every pin is real, and the shortlist stays short ─────────────────────────

def test_every_persona_pin_names_a_real_nav_link():
    from web_dashboard.services import personas as P
    known = _nav_ids()
    assert known, "could not parse any data-nav ids"
    for p in P.all_personas():
        for pin in p.nav_pins:
            assert pin in known, (
                f"{p.key}.nav_pins names '{pin}', which is not a data-nav id in "
                f"_nav_links.html")


def test_no_persona_pins_more_than_the_shortlist_budget():
    from web_dashboard.services import personas as P
    for p in P.all_personas():
        assert len(p.nav_pins) <= _MAX_PINS, (
            f"{p.key} pins {len(p.nav_pins)} links; the budget is {_MAX_PINS}. Beyond it "
            "the pinned block stops reading as a shortlist and becomes a second copy of "
            "the list below it.")


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


def _code_only(js):
    """``js`` with its ``//`` comments removed.

    The assertions below that a token is ABSENT are about what the function DOES, and the
    comments in responsiveNav name the very techniques they rule out ("cloning would drop
    the x-show bindings", "createElement, not innerHTML"). Grepping the raw text makes
    explaining a decision indistinguishable from taking it.
    """
    return re.sub(r"^\s*//.*$", "", js, flags=re.M)


def test_the_reorder_is_scoped_to_the_drawer_by_ref():
    """The drawer is the only render of _nav_links.html today. A document-wide selector
    would still be wrong — the docs shell and any future second render would be scrambled
    by it, silently — and being wrong here is what "a persona cannot hide a page" rests
    on."""
    body = _code_only(_apply_pins_body())
    assert "this.$refs.navDrawer" in body, "applyPins does not resolve the link list by ref"
    assert "document.querySelector" not in body and "document.getElement" not in body, (
        "applyPins reaches for document rather than for its own list")


def test_the_pins_are_applied_after_alpine_has_walked_the_children():
    """A component's init() runs BEFORE Alpine walks its children, so $refs.navDrawer is
    undefined there and applyPins() returns having done nothing — silently, leaving the
    pins in the attribute and the list in shipped order. That shipped once."""
    src = _read(_APP_JS)
    init = src.split("        init() {", 1)[1].split("\n        },", 1)[0]
    assert "applyPins" in init, "nothing ever applies the pins"
    assert re.search(r"\$nextTick\(\s*\(\)\s*=>\s*this\.applyPins\(\)\s*\)", init), (
        "applyPins is called straight from init(), where $refs is not populated yet: "
        f"{init.strip()[-200:]}")


def test_neutral_changes_no_dom_at_all():
    body = _apply_pins_body()
    assert re.search(r"if\s*\(\s*!pins\.length\s*\)\s*return", body), (
        "applyPins has no early return for an empty pin list — the neutral persona must "
        "leave the DOM byte-identical to before this existed")


def test_the_links_are_moved_not_cloned():
    """cloneNode would drop the Alpine x-show bindings that hide the admin-only links from
    a non-admin, so every user would see Users, Groups and Secrets pinned."""
    body = _code_only(_apply_pins_body())
    assert "cloneNode" not in body and "innerHTML" not in body, \
        "applyPins clones or rewrites markup; it must MOVE the live nodes"
    assert "insertBefore" in body


def test_no_link_is_dropped_by_the_reorder():
    """A persona may reorder. It may never subtract."""
    body = _code_only(_apply_pins_body())
    assert "remove()" not in body and "removeChild" not in body, \
        "applyPins removes a node — a persona may reorder, never subtract"


def test_the_pinning_runs_once():
    body = _apply_pins_body()
    assert "this._pinned" in body, \
        "applyPins is not guarded against re-running; it would re-walk a pinned list"


def test_the_divider_only_appears_when_something_actually_moved():
    """A pin set naming nothing this instance has (POV pins on a demo instance, a cloud
    pin with cloud_pages off) must not leave a rule floating at the top of an untouched
    list."""
    body = _apply_pins_body()
    assert "createElement('hr')" in body, \
        "applyPins draws no line between the persona's links and the rest"
    hr = body[body.index("createElement('hr')"):]
    guard = body[:body.index("createElement('hr')")]
    assert re.search(r"if\s*\(\s*cursor\s*\)", guard), (
        "the divider is inserted unconditionally; with no pin resolving, it lands at the "
        f"top of a list nothing moved in. {hr[:80]}")


def test_the_divider_is_styled_where_the_drawer_is():
    """`hr` under Tailwind's preflight takes the default grey border, which on a dark
    drawer reads as a rendering artefact rather than a separator."""
    src = _read(_BASE)
    assert re.search(r"\.nav-drawer hr\s*\{[^}]*border-color", src), \
        "base.html does not colour the drawer's persona divider"


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


def test_the_pin_attribute_is_on_the_list_the_reorder_reads():
    """applyPins reads `list.dataset.navPins` off the same element it queries links from.
    Split across two elements, the pins arrive and nothing moves."""
    src = _read(_BASE)
    i = src.index('x-ref="navDrawer"')
    tag = src[src.rindex("<div", 0, i):src.index(">", i) + 1]
    assert "data-nav-pins" in tag, (
        f"the pin list is not on the element carrying x-ref=navDrawer: {tag}")


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
