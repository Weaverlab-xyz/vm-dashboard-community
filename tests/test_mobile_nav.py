"""The nav bar on a phone. Every control in the row has to survive the fold.

The bug this file exists to prevent: ``base.html``'s ``x-ref="navRow"`` is
``overflow-hidden`` — deliberately, because that is what makes responsiveNav's
``scrollWidth > clientWidth`` fold read meaningful — and a flex item's ``min-width`` is
``auto``, i.e. its own content. So the brand lockup ("Weaver Lab / Infrastructure
[Community]") refused to shrink, and at a 393px viewport it did not push the user menu
along: the clip simply ATE it. The settings cog and the Logout button were not shrunk,
not wrapped and not scrollable-to — they were gone, with nothing on screen to say they
had ever been there, and rotating the phone to landscape was the only way to reach MFA
or to sign out.

Two structural facts produce the fix, and both are asserted here because either one alone
leaves the clip in charge:

  * the side that MAY shrink says so (``min-w-0`` on the brand block), and
  * the side that MAY NOT says so too (``flex-shrink-0`` on the controls).

The rest of this file pins where each control lives once the row folds. The row drops the
username, the instance label and Logout to buy width; the drawer is where they land, so
"dropped from the bar" must never be able to become "dropped".

Measured in Chromium against the rendered template, admin, every integration enabled:

    viewport   brand block   cog visible   toggle visible   row clipped
    393px          153px         yes            yes             no
    375px          153px         yes            yes             no
    320px          153px         yes            yes             no

and at 1280px with a persona pinned the inline row is unchanged at 1216px — the fold
budget tests/test_persona_nav records is measuring the same row it always was.

Runs under pytest, or standalone:
    python tests/test_mobile_nav.py
"""
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

_BASE = os.path.join(_ROOT, "web_dashboard", "templates", "base.html")
_APP_JS = os.path.join(_ROOT, "web_dashboard", "static", "js", "app.js")

# Apple's HIG and Android's Material both put the floor for a control a thumb presses at
# 44px / 48dp. `p-2.5` around a `h-6 w-6` icon is 44; `py-3` around `text-sm` is 44.
_THUMB = ("p-2.5", "py-3")


def _read(path=_BASE):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _element_span(src, needle):
    """``(start, end)`` offsets of the ``<div>`` element containing ``needle``.

    A depth walk rather than a regex: "which element is this attribute on, and where does
    it close" is the whole question, and no flat pattern answers it.
    """
    at = src.index(needle)
    i = src.rindex("<div", 0, at)
    depth = 0
    for m in re.finditer(r"<div\b|</div>", src[i:]):
        depth += 1 if m.group(0).startswith("<div") else -1
        if depth == 0:
            return i, i + m.end()
    raise AssertionError(f"the element carrying {needle!r} never closes")


def _element(src, needle):
    start, end = _element_span(src, needle)
    return src[start:end]


def _open_tag(src, needle):
    """The opening tag of whichever element carries ``needle``.

    ``needle`` must be something INSIDE the tag (an attribute, a class). To read a tag by
    its own name, use :func:`_tag_named` — ``rindex`` would otherwise walk backwards past
    ``<`` characters in the Jinja comment above it and hand back the prose, which
    describes the very classes a test is trying to assert on.
    """
    at = src.index(needle)
    return src[src.rindex("<", 0, at):src.index(">", at) + 1]


def _tag_named(src, name):
    """The opening ``<name ...>`` tag itself, quote-aware so an attribute value holding a
    ``>`` cannot end it early."""
    i = src.index(f"<{name} ")
    quote = None
    for j in range(i, len(src)):
        c = src[j]
        if quote:
            if c == quote:
                quote = None
        elif c in "\"'":
            quote = c
        elif c == ">":
            return src[i:j + 1]
    raise AssertionError(f"<{name}> never closes its opening tag")


def _nav_row(src):
    return _element(src, 'x-ref="navRow"')


def _drawer(src):
    """The compact flyout. Both it and its backdrop are gated on the same expression;
    the drawer is the one that is a column of content rather than a sheet of black."""
    spans = []
    for m in re.finditer(re.escape('x-show="compact && mobileNav"'), src):
        i = src.rindex("<div", 0, m.start())
        depth = 0
        for d in re.finditer(r"<div\b|</div>", src[i:]):
            depth += 1 if d.group(0).startswith("<div") else -1
            if depth == 0:
                spans.append(src[i:i + d.end()])
                break
    drawers = [s for s in spans if "bg-black" not in s]
    assert len(drawers) == 1, (
        f"expected exactly one compact flyout drawer, found {len(drawers)}")
    return drawers[0]


# ── the row can no longer clip its own controls ──────────────────────────────

def test_the_brand_block_is_allowed_to_shrink():
    """``min-width: auto`` is the default, and it is what let the lockup hold its full
    width inside an `overflow-hidden` row until the controls fell off the end."""
    row = _nav_row(_read())
    brand = _open_tag(row, "_brand_mark.html")
    # The mark's include is inside the brand block; walk out to the block itself.
    block = _open_tag(row, 'class="flex items-center space-x-2.5')
    assert "min-w-0" in block, (
        "the brand block has no min-w-0, so it cannot shrink below its content width. "
        "Inside navRow's `overflow-hidden` that does not push the user menu aside — it "
        "clips it, and the settings cog and Logout disappear with no scrollbar and no "
        f"ellipsis to say so. Tag: {block}")
    assert brand  # the mark is still in the block we just asserted on


def test_the_wordmark_does_not_wrap_inside_a_64px_row():
    row = _nav_row(_read())
    link = _open_tag(row, "{{ theme.brand }}")
    assert "whitespace-nowrap" in link, (
        f"the brand wordmark may wrap; in a 64px row that is a clip, not a second line: {link}")


def test_the_controls_are_the_last_thing_allowed_to_give_up_width():
    """The other half of the same fix. Letting the brand shrink is no use if the controls
    shrink first."""
    row = _nav_row(_read())
    menu = _open_tag(row, 'x-text="$store.auth.username"')
    # The username span is inside the controls group; assert on the group itself.
    group = row[row.rindex("<div", 0, row.index('x-text="$store.auth.username"')):]
    group = group[:group.index(">") + 1]
    assert "flex-shrink-0" in group, (
        f"the user-menu group is not flex-shrink-0, so the brand can squeeze it: {group}")
    assert menu


def test_the_row_still_clips_because_the_fold_depends_on_it():
    """Anti-regression in the other direction: "stop the clip eating the cog" must not be
    fixed by removing the clip. responsiveNav decides the fold with
    ``scrollWidth > clientWidth``, which needs the overflow to be hidden to mean anything.
    """
    assert "overflow-hidden" in _open_tag(_read(), 'x-ref="navRow"')


# ── every control survives the fold, somewhere ───────────────────────────────

def test_settings_is_reachable_from_the_bar_at_every_width():
    """It is the only route to MFA and passkeys. Burying it behind a menu at exactly the
    width where it was already unreachable would fix the symptom and keep the bug."""
    row = _nav_row(_read())
    tag = _open_tag(row, 'href="/settings"')
    assert "x-show" not in tag, (
        f"the settings link is conditionally hidden in the nav bar: {tag}")


def test_the_settings_target_is_thumb_sized_once_the_row_folds():
    """A 20px icon is a fine mouse target and a poor thumb one. The padding is state-driven
    rather than a `sm:` breakpoint because measure() reads the row with compact false — so
    the wide row keeps exactly the width the fold budget was measured against."""
    tag = _open_tag(_nav_row(_read()), 'href="/settings"')
    assert ":class" in tag and "compact" in tag, (
        f"the settings link has no compact-only touch padding: {tag}")
    assert not re.search(r'\bsm:p-', tag), (
        "the touch padding is on a breakpoint. `compact` is a measured fold, not a "
        "viewport width, and a breakpoint would also widen the row while it is measured.")


def test_the_toggle_is_a_thumb_sized_target():
    tag = _open_tag(_nav_row(_read()), 'aria-label="Toggle navigation menu"')
    assert any(t in tag for t in _THUMB), (
        f"the nav toggle is smaller than a 44px thumb target: {tag}")


def test_what_the_bar_drops_the_drawer_picks_up():
    """The bar sheds the username, the instance label and Logout to buy width when it
    folds. Each one has to reappear in the drawer, or "dropped from the bar" quietly
    became "dropped"."""
    drawer = _drawer(_read())
    assert 'x-text="$store.auth.username"' in drawer, \
        "the drawer does not say who is signed in, and the bar stops saying it when compact"
    assert "theme.product" in drawer and "theme.chip_label" in drawer, \
        "the drawer does not say which instance this is; the bar stops saying it when compact"
    assert "$store.auth.logout()" in drawer, \
        "there is no way to sign out from the drawer, and the bar's Logout is hidden when compact"
    assert 'href="/settings"' in drawer, (
        "the drawer has no settings link. The open drawer covers the bar's cog, so a user "
        "who opened the menu looking for it finds nothing.")


def test_the_username_and_logout_leave_the_bar_when_it_folds():
    """The other direction: if they stayed, they would be back under the clip."""
    row = _nav_row(_read())
    for needle in ('x-text="$store.auth.username"', "$store.auth.logout()"):
        tag = _open_tag(row, needle)
        assert 'x-show="!compact"' in tag, (
            f"{needle} is still rendered in the bar when the row folds: {tag}")


# ── the drawer itself ────────────────────────────────────────────────────────

def test_the_drawer_is_not_inside_the_clipped_row():
    """It is a viewport overlay. It has no business inside the 64px `overflow-hidden` row
    whose width decides whether it is ever shown."""
    src = _read()
    row = _nav_row(src)
    assert 'x-show="compact && mobileNav"' not in row, (
        "the compact drawer is back inside navRow. It is a full-height overlay measured as "
        "part of a 64px row that clips its children.")


def test_the_drawer_carries_its_own_close_control():
    """It is full height, so it paints over the bar's own toggle: without this the X you
    press to dismiss the drawer sits underneath the drawer, and the backdrop is the only
    way out of a menu that shows no way out."""
    drawer = _drawer(_read())
    assert 'aria-label="Close navigation menu"' in drawer, \
        "the drawer has no close button, and it covers the toggle that opened it"
    tag = _open_tag(drawer, 'aria-label="Close navigation menu"')
    assert any(t in tag for t in _THUMB), \
        f"the drawer's close button is smaller than a 44px thumb target: {tag}"


def test_the_drawer_scrolls_its_links_without_losing_its_footer():
    """~28 links do not fit on a phone. If the whole panel scrolls as one, Logout is at the
    bottom of that scroll rather than at the bottom of the screen."""
    drawer = _drawer(_read())
    assert "overflow-y-auto" in drawer, "the drawer's link list does not scroll"
    scroller = _open_tag(drawer, "nav-drawer")
    assert "flex-1" in scroller and "overflow-y-auto" in scroller, (
        f"the scrolling region is not the link list alone: {scroller}")
    footer = _open_tag(drawer, "$store.auth.logout()")
    assert "overflow-y-auto" not in footer


def test_the_drawer_rows_are_thumb_sized_without_widening_the_bar():
    """_nav_links.html is rendered twice from ONE set of classes, and one of those renders
    is the row whose width decides the fold. So the taller row is CSS scoped to the
    drawer; putting it in the shared classes would spend the width the fold exists to
    save."""
    src = _read()
    assert "nav-drawer" in _drawer(src), "the drawer's link list is not marked for scoping"
    rule = re.search(r"\.nav-drawer a\s*\{([^}]*)\}", src)
    assert rule, "base.html has no .nav-drawer rule sizing the drawer's link rows"
    body = rule.group(1)
    assert "padding-top" in body and "padding-bottom" in body, \
        f"the .nav-drawer rule does not set a vertical size: {body}"
    row = _nav_row(src)
    assert "nav-drawer" not in row, \
        "the drawer's sizing class leaked onto the inline row it is scoped away from"


def test_the_drawer_leaves_the_backdrop_reachable_on_a_narrow_phone():
    """A drawer that covers the whole screen at 320px gives a first-time user nothing to
    tap to dismiss it and no sense that a page is still behind it."""
    tag = _open_tag(_drawer(_read()), 'class="fixed inset-y-0 right-0')
    assert "max-w-[" in tag, \
        f"the drawer has no width cap; at 320px it covers the viewport: {tag}"


# ── the page must not move underneath the open drawer ────────────────────────
#
# Measured in Chromium at 393px on a 4.3-screen dashboard: with the drawer open, a
# 800px wheel over the backdrop moved the document from 1200 to 1800. The user swipes to
# reach a link near the bottom of a ~28-item list, the swipe lands on the backdrop or
# runs past the end of the list, and the page behind moves instead — then they dismiss
# the menu and are somewhere they did not choose. Three separate paths chain, so three
# separate guards.

def test_the_body_is_locked_while_the_drawer_is_open():
    src = _read(_APP_JS)
    assert "init() {" in src
    body = src.split("init() {", 1)[1].split("\n        },", 1)[0]
    assert "$watch('mobileNav'" in body, (
        "responsiveNav does not watch mobileNav, so nothing locks the page while the "
        "drawer is open and a swipe on the backdrop scrolls it")
    assert "document.body.classList" in body and "overflow-hidden" in body, (
        "the mobileNav watcher does not lock body scroll")


def test_the_lock_does_not_throw_the_reader_back_to_the_top():
    """`position: fixed` is the other way to lock a page and it loses the scroll
    position — you open the menu, change your mind, and the article you were reading is
    back at its first line. html's overflow is `visible`, so `overflow: hidden` on body
    propagates to the viewport and pins the page exactly where it stands."""
    body = _read(_APP_JS).split("init() {", 1)[1].split("\n        },", 1)[0]
    watcher = body.split("$watch('mobileNav'", 1)[1].split("});", 1)[0]
    assert "position" not in watcher and "scrollTo" not in watcher, (
        f"the scroll lock moves the page rather than pinning it: {watcher}")


def test_the_link_list_does_not_chain_its_scroll_to_the_page():
    """`overflow: hidden` on body does not stop a scroll that STARTED inside the drawer
    from continuing into the document once the list hits its end."""
    scroller = _open_tag(_drawer(_read()), "nav-drawer")
    assert "overscroll-contain" in scroller, (
        f"the drawer's link list chains its overscroll to the page behind it: {scroller}")


def test_the_backdrop_does_not_scroll_the_page_behind_it():
    """The backdrop is `fixed` with nothing of its own to scroll, so a drag on it goes
    straight to the document. iOS honours touch-action here where it is unreliable about
    body overflow."""
    src = _read()
    backdrop = _open_tag(src, "bg-black/40")
    assert "touch-none" in backdrop, (
        f"a swipe on the drawer's backdrop still scrolls the page behind it: {backdrop}")


# ── the bar pins on a phone, and only there ──────────────────────────────────
#
# Measured in Chromium at 393px on a dashboard 4.3 screens tall: unpinned, the nav's
# bottom edge sits at -2755px once you reach the end of the page, so the only navigation
# affordance a phone has is off-screen and reaching it means scrolling all the way back
# up first. Pinned, the bar reads top 0 / bottom 64 at any scroll offset, and opening the
# menu from 2000px down leaves the reader at 2000px.
#
# The pin costs 64px of a ~850px viewport, which is why it is the FOLDED row's trade and
# not the wide row's: at 1440px the inline row measures 1376px either way.

def test_the_bar_pins_only_while_the_row_is_folded():
    tag = _tag_named(_read(), "nav")
    assert ":class" in tag and "compact" in tag, (
        f"the bar's positioning does not depend on the fold: {tag}")
    assert "sticky" in tag and "top-0" in tag, (
        f"the folded bar is not pinned to the top of the viewport: {tag}")
    assert "relative" in tag, (
        "the inline row lost `relative`. The overflow popover is absolutely positioned "
        f"against <nav> and would escape to the nearest positioned ancestor: {tag}")


def test_the_pinned_bar_stays_under_the_pages_modal_overlays():
    """A pinned bar has to clear scrolling content and duck under a modal. The page's
    overlays are z-40 (users/, cert_lab/, functions/, inventory/), so the bar's z-index
    is bounded on both sides: high enough to beat ordinary content, below 40."""
    tag = _tag_named(_read(), "nav")
    z = re.search(r"\bz-(\d+)\b", tag)
    assert z, f"the pinned bar has no z-index and will be painted over by page content: {tag}"
    assert 0 < int(z.group(1)) < 40, (
        f"the pinned bar is at z-{z.group(1)}; the page's modal overlays are z-40 and must "
        "still be able to cover it")


def test_the_overlays_are_outside_the_nav_a_sticky_bar_would_trap_them_in():
    """`position: sticky` creates a stacking context whatever its z-index. With the drawer
    still inside <nav>, a pinned bar would trap it at the bar's own z-30 — and the
    `fixed inset-0 z-40` modals in users/, cert_lab/, functions/ and inventory/ would paint
    over an open menu."""
    src = _read()
    nav = src[src.index("<nav "):src.index("</nav>")]
    assert 'x-show="compact && mobileNav"' not in nav, (
        "the drawer or its backdrop is back inside <nav>. A sticky bar's stacking context "
        "would drag it below the page's z-40 modal overlays.")
    assert 'x-show="compact && mobileNav"' in src, "the drawer left base.html entirely"


def test_the_component_root_generates_no_box():
    """Two things need this. A sticky element only sticks within its PARENT's box, so an
    ordinary wrapper div — 64px tall, since the drawer and backdrop are `fixed` and add no
    height — would give the bar 64px of travel and it would scroll away exactly as before.
    And the drawer, now a sibling of <nav>, still needs `compact` and `mobileNav`, so one
    component has to span all three."""
    src = _read()
    root = _open_tag(src, 'x-data="responsiveNav()"')
    assert "<nav" not in root, "responsiveNav is still rooted on <nav>; the drawer cannot leave it"
    assert "contents" in root, (
        "the component root generates a box. <nav>'s containing block is then that box "
        f"rather than <body>, and `sticky` has nothing to stick through: {root}")
    assert 'x-show="$store.auth.isLoggedIn"' in root, \
        "the nav group is no longer hidden before login"


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
