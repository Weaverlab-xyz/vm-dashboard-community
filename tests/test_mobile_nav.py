"""The nav bar and its drawer. One navigation model, at every width.

There used to be two: an inline link row in the bar, folding into a flyout drawer once
``responsiveNav`` measured the row overflowing. ``tests/test_persona_nav`` records the
measurement that retired it — a fully-enabled admin instance overflows a 1280px viewport
by 1290px, so the inline row was already folded away for the configuration most people
run, and the drawer was already the complete list. Now it is the only one, the bar names
the instance rather than the parent brand, and there is no fold to measure.

Losing the fold changes what has to be true of the row, and this file is where that is
pinned:

  * The row no longer CLIPS. `overflow-hidden` existed to make `scrollWidth >
    clientWidth` meaningful; with nothing reading that, a clip only hides controls.
  * So the lockup must genuinely shrink — a flex item's `min-width` is `auto`, i.e. its
    own content, and without `min-w-0` + `truncate` the lockup holds full width and
    pushes the cog and the hamburger off the right edge, taking the whole DOCUMENT
    sideways with them.
  * And the controls must refuse to shrink (`flex-shrink-0`), or the lockup squeezes them
    instead.

The rest of this file pins what the drawer owes the user, because everything the bar no
longer says — who you are, which instance this is, the way out — it says.

Measured in Chromium against the running app, admin, every integration enabled:

    viewport   nav row   doc width   cog + hamburger reachable
    320px        288px      320px            yes
    393px        361px      393px            yes
    1280px      1216px     1280px            yes

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


# `<div` and not a bare attribute search: the hamburger's own close-icon <path> is also
# `x-show="mobileNav"`, and walking back from it lands on the nav row.
_OVERLAY = r'<div\b[^>]*x-show="mobileNav"'


def _drawer(src):
    """The flyout. Both it and its backdrop are gated on the same expression; the drawer
    is the one that is a column of content rather than a sheet of black."""
    spans = []
    for m in re.finditer(_OVERLAY, src):
        i = m.start()
        depth = 0
        for d in re.finditer(r"<div\b|</div>", src[i:]):
            depth += 1 if d.group(0).startswith("<div") else -1
            if depth == 0:
                spans.append(src[i:i + d.end()])
                break
    drawers = [s for s in spans if "bg-black" not in s]
    assert len(drawers) == 1, (
        f"expected exactly one flyout drawer, found {len(drawers)}")
    return drawers[0]


# ── one model: the fold is gone, and nothing may still depend on it ──────────

def test_the_hamburger_is_in_the_bar_at_every_width():
    """The drawer is the only link menu there is. A toggle that appears only below some
    measured width would make the whole nav unreachable above it."""
    tag = _open_tag(_nav_row(_read()), 'aria-label="Toggle navigation menu"')
    assert "x-show" not in tag, (
        f"the nav toggle is conditionally hidden; it is the only way into the menu: {tag}")


def test_nothing_still_reads_the_fold_state():
    """`compact` was the measured fold. It no longer exists on the component, and an
    `x-show="!compact"` left behind does not fail loudly — Alpine evaluates the undefined
    name, and the control it guards is hidden forever."""
    base = _read()
    assert "compact" not in base, (
        "base.html still refers to `compact`, which responsiveNav no longer defines. "
        "Whatever it guards is now permanently hidden or permanently shown.")
    js = _read(_APP_JS)
    body = js.split("function responsiveNav()", 1)[1].split("\nwindow.responsiveNav", 1)[0]
    for dead in ("compact", "moreOpen", "moreCount", "navInline", "navMore", "positionMore"):
        assert dead not in body, f"responsiveNav still carries {dead!r} from the folding nav"


def test_the_link_list_is_rendered_exactly_once():
    """It used to be included twice — once inline, once in the drawer — and that is what
    forced every DOM walk over it to be ref-scoped. One render, one list."""
    assert _read().count("{% include '_nav_links.html' %}") == 1, \
        "_nav_links.html is no longer rendered exactly once by base.html"


# ── the row can no longer clip its own controls ──────────────────────────────

def test_the_lockup_is_allowed_to_shrink():
    """``min-width: auto`` is the default, and it is what lets the lockup hold its full
    width and push the controls off the end of the row."""
    row = _nav_row(_read())
    block = _open_tag(row, 'class="flex items-center space-x-2.5')
    assert "min-w-0" in block, (
        "the lockup has no min-w-0, so it cannot shrink below its content width and the "
        f"settings cog and the menu toggle get pushed off the right edge. Tag: {block}")


def test_the_product_word_truncates_rather_than_wrapping_a_64px_row():
    """min-w-0 lifts the floor; something inside still has to give. `truncate` is
    overflow-hidden + ellipsis + nowrap, so a long product name ends in an ellipsis
    instead of a second line or a wider row."""
    row = _nav_row(_read())
    tag = _open_tag(row, "{{ theme.product }}")
    assert "truncate" in tag, (
        f"the product word neither truncates nor is stopped from wrapping: {tag}")


def test_the_controls_are_the_last_thing_allowed_to_give_up_width():
    """The other half of the same fix. Letting the lockup shrink is no use if the controls
    shrink first."""
    row = _nav_row(_read())
    group = row[row.index('class="flex items-center space-x-1 sm:space-x-3'):]
    group = group[:group.index(">") + 1]
    assert "flex-shrink-0" in group, (
        f"the user-menu group is not flex-shrink-0, so the lockup can squeeze it: {group}")


def test_the_row_no_longer_clips_what_it_cannot_fit():
    """`overflow-hidden` was there to make responsiveNav's `scrollWidth > clientWidth`
    read meaningful. Nothing reads it now, and a clip on a row whose controls are the last
    thing in it only removes them from the screen with no scrollbar to say so."""
    tag = _open_tag(_read(), 'x-ref="navRow"')
    assert "overflow-hidden" not in tag, (
        "the nav row clips again. There is no fold measuring it any more, so this can "
        f"only hide the cog and the hamburger: {tag}")


# ── the bar names the instance; the drawer names the app and the user ────────

def test_the_bar_names_which_instance_this_is():
    """The product word is what says WHICH of the two instances you are on, and it is the
    half of the old lockup that is not constant between them."""
    row = _nav_row(_read())
    assert "{{ theme.product }}" in row, \
        "the nav bar no longer names the instance"
    assert "{{ theme.brand }}" not in row, (
        "the parent brand is back in the bar. It is constant across both instances and "
        "says nothing about where you are; it heads the drawer instead.")


def test_the_drawer_heads_with_the_parent_brand():
    drawer = _drawer(_read())
    assert "{{ theme.brand }}" in drawer, \
        "the drawer header does not name the app"
    assert "theme.chip_label" in drawer, (
        "the drawer does not say which instance this is. The bar's chip is `hidden sm:`, "
        "so on a phone this is the only place the edition appears at all.")


def test_the_lockup_carries_no_separator_glyph():
    """"Weaver Lab / Infrastructure" was one line, and the slash was its punctuation. The
    two halves are now in two different places, where a separator is just a stray mark."""
    src = _read()
    assert "theme.slash" not in src, \
        "base.html still renders the lockup separator between two halves that no longer meet"
    assert 'aria-hidden="true">/</span>' not in src, \
        "a bare forward slash is still rendered in the nav chrome"


def test_the_drawer_says_who_is_signed_in_and_offers_the_way_out():
    """The bar sheds the username below `md` and never had Logout at all once the row was
    the only row. Each has to be in the drawer, or "moved" quietly became "dropped"."""
    drawer = _drawer(_read())
    assert 'x-text="$store.auth.username"' in drawer, \
        "the drawer does not say who is signed in"
    assert "$store.auth.logout()" in drawer, \
        "there is no way to sign out from the drawer, and the bar has no Logout button"
    assert 'href="/settings"' in drawer, (
        "the drawer has no settings link. The open drawer covers the bar's cog, so a user "
        "who opened the menu looking for it finds nothing.")


def test_settings_is_reachable_from_the_bar_at_every_width():
    """It is the only route to MFA and passkeys. Burying it behind a menu would fix
    nothing and cost a tap."""
    row = _nav_row(_read())
    tag = _open_tag(row, 'href="/settings"')
    assert "x-show" not in tag, (
        f"the settings link is conditionally hidden in the nav bar: {tag}")


def test_both_bar_controls_are_thumb_sized():
    """A 20px icon is a fine mouse target and a poor thumb one, and these two sit next to
    each other — one of them being smaller is worse than either size on its own."""
    row = _nav_row(_read())
    for needle in ('href="/settings"', 'aria-label="Toggle navigation menu"'):
        tag = _open_tag(row, needle)
        assert any(t in tag for t in _THUMB), \
            f"{needle} is smaller than a 44px thumb target: {tag}"


# ── the drawer itself ────────────────────────────────────────────────────────

def test_the_drawer_is_not_inside_the_bar_row():
    """It is a viewport overlay. It has no business inside the 64px row that lays out the
    controls it paints over."""
    row = _nav_row(_read())
    assert not re.search(_OVERLAY, row), (
        "the drawer is back inside navRow, a 64px flex row it would be laid out as an "
        "item of.")


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
    """~32 links do not fit on a phone. If the whole panel scrolls as one, Logout is at
    the bottom of that scroll rather than at the bottom of the screen."""
    drawer = _drawer(_read())
    assert "overflow-y-auto" in drawer, "the drawer's link list does not scroll"
    scroller = _open_tag(drawer, "nav-drawer")
    assert "flex-1" in scroller and "overflow-y-auto" in scroller, (
        f"the scrolling region is not the link list alone: {scroller}")
    footer = _open_tag(drawer, "$store.auth.logout()")
    assert "overflow-y-auto" not in footer


def test_the_drawer_rows_are_thumb_sized():
    """_nav_links.html's own classes (`px-2 py-2`) are sized for a mouse. In the drawer
    every link is a thumb target with a whole column to itself."""
    src = _read()
    assert "nav-drawer" in _drawer(src), "the drawer's link list is not marked for scoping"
    rule = re.search(r"\.nav-drawer a\s*\{([^}]*)\}", src)
    assert rule, "base.html has no .nav-drawer rule sizing the drawer's link rows"
    body = rule.group(1)
    assert "padding-top" in body and "padding-bottom" in body, \
        f"the .nav-drawer rule does not set a vertical size: {body}"


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
# reach a link near the bottom of a ~32-item list, the swipe lands on the backdrop or
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


# ── the bar pins, and stays under the page's own overlays ────────────────────
#
# Measured in Chromium at 393px on a dashboard 4.3 screens tall: unpinned, the nav's
# bottom edge sits at -2755px once you reach the end of the page, so the only navigation
# affordance there is sits off-screen and reaching it means scrolling all the way back up
# first. Pinned, the bar reads top 0 / bottom 64 at any scroll offset, and opening the
# menu from 2000px down leaves the reader at 2000px.
#
# Unconditional now, where it used to be the folded row's trade alone: with one navigation
# model there is no width at which the links are on screen without opening the menu.

def test_the_bar_is_pinned():
    tag = _tag_named(_read(), "nav")
    assert "sticky" in tag and "top-0" in tag, (
        f"the bar is not pinned to the top of the viewport: {tag}")


def test_the_pinned_bar_stays_under_the_pages_modal_overlays():
    """A pinned bar has to clear scrolling content and duck under a modal. The page's
    overlays are z-40 (users/, workload_lab/, functions/, inventory/), so the bar's z-index
    is bounded on both sides: high enough to beat ordinary content, below 40."""
    tag = _tag_named(_read(), "nav")
    z = re.search(r"\bz-(\d+)\b", tag)
    assert z, f"the pinned bar has no z-index and will be painted over by page content: {tag}"
    assert 0 < int(z.group(1)) < 40, (
        f"the pinned bar is at z-{z.group(1)}; the page's modal overlays are z-40 and must "
        "still be able to cover it")


def test_the_overlays_are_outside_the_nav_a_sticky_bar_would_trap_them_in():
    """`position: sticky` creates a stacking context whatever its z-index. With the drawer
    still inside <nav>, the bar would trap it at its own z-30 — and the `fixed inset-0
    z-40` modals in users/, workload_lab/, functions/ and inventory/ would paint over an open
    menu."""
    src = _read()
    nav = src[src.index("<nav "):src.index("</nav>")]
    assert not re.search(_OVERLAY, nav), (
        "the drawer or its backdrop is back inside <nav>. The sticky bar's stacking "
        "context would drag it below the page's z-40 modal overlays.")
    assert re.search(_OVERLAY, src), "the drawer left base.html entirely"


def test_the_component_root_generates_no_box():
    """Two things need this. A sticky element only sticks within its PARENT's box, so an
    ordinary wrapper div — 64px tall, since the drawer and backdrop are `fixed` and add no
    height — would give the bar 64px of travel and it would scroll away exactly as before.
    And the drawer, a sibling of <nav>, still needs `mobileNav`, so one component has to
    span all three."""
    src = _read()
    root = _open_tag(src, 'x-data="responsiveNav()"')
    assert "<nav" not in root, "responsiveNav is still rooted on <nav>; the drawer cannot leave it"
    assert "contents" in root, (
        "the component root generates a box. <nav>'s containing block is then that box "
        f"rather than <body>, and `sticky` has nothing to stick through: {root}")
    assert 'x-show="$store.auth.isLoggedIn"' in root, \
        "the nav group is no longer hidden before login"


# ── the toast stack is chrome too, and it lives in the same shell ────────────

def test_a_toast_cannot_hang_off_the_right_edge_of_a_phone():
    """`max-w-sm` is 384px; the container is inset 16px from the right, so at 393px the
    toast ran 7px past the viewport and took the document with it. One declaration, not
    two: a second max-width utility would be resolved by stylesheet order rather than by
    which value is smaller."""
    src = _read()
    holder = [l for l in src.splitlines()
              if "rounded-lg shadow-lg text-sm font-medium text-white" in l]
    assert holder, "the toast body element is no longer recognisable in base.html"
    cls = holder[0]
    assert "max-w-[min(" in cls, (
        f"the toast has no viewport-relative width cap: {cls.strip()}")
    assert "max-w-sm" not in cls, (
        "max-w-sm is back alongside the cap. Two max-width utilities on one element are "
        "resolved by stylesheet order, so the pair silently picks a winner.")


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
