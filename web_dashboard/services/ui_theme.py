"""Chrome and brand tokens for one instance, in one place.

Two dashboards run the same image: a **demo** instance, which resolves its BeyondTrust
tenant from the global singletons, and a **POV** instance, which resolves it from a
registry of named customer tenants. ``services/feature_flags`` enforces that split in
code. This module is the other half -- making it *visible*, because the failure mode the
split exists to prevent is silent (a demo deploy onboarding into a customer's Password
Safe, or a POV onboarding into the demo tenant) and an operator with two identical-looking
tabs open is the way it happens.

The palette lives here rather than in Jinja ternaries for two reasons: both profiles are
legible side by side, and it is unit-testable -- ``tests/test_profile_theme.py`` pins the
demo strings so a re-skin of POV can never quietly recolour the demo instance.

Every value in the shipped palettes is a literal Tailwind class string (the app is on the
Tailwind Play CDN, which scans the DOM, so composed class names are not an option) except
``hex``, which carries raw colours for the two consumers that do not speak Tailwind: the
docs browser (``api/docs_pages``, which writes plain CSS) and the favicon data URI.

An operator's own three colours are the one exception, and they are still class strings --
just hand-written ones rather than Tailwind utilities, defined in
``templates/_brand_css.html`` against CSS custom properties. See :data:`_BRAND_SCOPE` for
why that sidesteps the purge hazard instead of ignoring it.

Colour alone is deliberately NOT the whole signal -- it fails for colour-vision deficiency
and fails completely in a greyscale screenshot. The wordmark, the chip text and the
``POV -`` title prefix each carry the profile independently.
"""
import re
from urllib.parse import quote

from .feature_flags import VALID_PROFILES


BRAND = "Weaver Lab"
# Too long for the nav bar, which names the instance (the product word) and leaves the
# parent brand to the flyout drawer's header. It belongs on the login page, the one screen
# with room for it.
BRAND_FULL = "Weaver Lab Applications"

# Mark A, "the weave": two warp strands and two weft strands interlacing over/under in a
# basket weave. Split into two paths on purpose -- that split IS the brand system. The warp
# is neutral on both instances; the weft carries the profile accent. Same loom, one strand
# dyed differently.
#
# The gaps in each path are the over/under crossings, so the stroke width is load-bearing:
# much above 2.2 on a 24x24 viewBox and the round caps close the gaps, turning the weave
# into a plain grid.
MARK_WARP = "M8.5 3v10.7M8.5 17.3V21M15.5 3v3.7M15.5 10.3V21"
MARK_WEFT = "M3 8.5h3.7M10.3 8.5H21M3 15.5h10.7M17.3 15.5H21"


def _favicon(warp: str, weft: str) -> str:
    """The mark as a fully percent-encoded ``data:`` URI.

    Encoded rather than inlined raw because a hex colour's ``#`` starts a URI fragment:
    left literal, the browser truncates the SVG at the first ``stroke="#...`` and the tab
    shows nothing. ``safe=""`` escapes everything, which is longer but cannot be got wrong.
    """
    svg = (
        "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' "
        "stroke-width='2.6' stroke-linecap='round'>"
        f"<path d='{MARK_WARP}' stroke='{warp}'/>"
        f"<path d='{MARK_WEFT}' stroke='{weft}'/>"
        "</svg>"
    )
    return "data:image/svg+xml," + quote(svg, safe="")


# Six hex digits with the hash, lowercase, nothing else. ``services.branding`` applies the
# same rule on the way out of the database; this copy exists because that module is not the
# only possible caller of ``theme_for``, and the custom palette's output lands INSIDE a
# ``<style>`` element rather than in an attribute. Jinja's autoescaping is worthless there
# -- CSS does not decode entities, and ``</style>`` ends the block -- so the check has to
# live on this side of the call too. ``branding`` cannot be imported here: it imports us.
_HEX = re.compile(r"^#[0-9a-f]{6}$")


def _lin(c: float) -> float:
    """One sRGB channel, 0..1, linearised. Module-level so ``_ratio`` shares it."""
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def _luminance(hex_: str) -> float:
    """WCAG relative luminance of ``#rrggbb``. Raises on anything else."""
    r, g, b = (int(hex_[i:i + 2], 16) / 255 for i in (1, 3, 5))
    return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)


def _text_on(bg: str) -> str:
    """Near-black or white, whichever is legible on ``bg`` (``#rrggbb``).

    The environment banner is the one chrome colour an operator picks freely, so the
    foreground cannot be a constant: white on a chosen ``#fde047`` is ~1.1:1 and the label
    -- the entire point of the banner -- disappears. WCAG relative luminance with the usual
    0.179 split, which is the threshold at which black and white swap places as the better
    partner against a mid-tone.

    An unparseable value yields white, matching the dark default the pickers offer.
    """
    try:
        luminance = _luminance(bg)
    except (ValueError, IndexError):
        return "#ffffff"
    return "#0f172a" if luminance > 0.179 else "#ffffff"


def _rgb(hex_: str) -> tuple[int, int, int]:
    """``"#ff5400"`` -> ``(255, 84, 0)``. Caller has already matched ``_HEX``."""
    return tuple(int(hex_[i:i + 2], 16) for i in (1, 3, 5))  # type: ignore[return-value]


def _hexs(rgb: tuple[int, int, int]) -> str:
    """``(255, 84, 0)`` -> ``"#ff5400"``. Lowercase, to match ``_HEX``."""
    return "#%02x%02x%02x" % rgb


def _mix(a: str, b: str, t: float) -> str:
    """``t`` of the way from ``a`` to ``b``, per channel. ``t=0`` is ``a``.

    ``int(x + 0.5)`` rather than ``round(x)``: the builtin rounds halves to EVEN, and the
    settings preview mirrors this arithmetic in JavaScript, where ``Math.round`` rounds
    halves UP. Mixing ``#1903a6`` toward white at 0.85 lands a channel on exactly 220.5 and
    the two languages disagreed -- invisible on screen, but it makes a preview that claims
    to show the server's output into one that merely nearly does, and there is no way to
    assert the mirror holds once it is allowed to be approximately right. Channels are
    never negative here, so half-up needs no sign handling.
    """
    ra, ga, ba = _rgb(a)
    rb, gb, bb = _rgb(b)
    return _hexs((
        int(ra + (rb - ra) * t + 0.5),
        int(ga + (gb - ga) * t + 0.5),
        int(ba + (bb - ba) * t + 0.5),
    ))


def _step(bg: str, t: float) -> str:
    """``bg`` nudged ``t`` toward its OWN legible foreground -- the hover/active rule.

    Not "lighten by t" and not "darken by t". Either fixed direction is wrong for half the
    possible inputs: lightening a pale brand gives a delta nobody can see AND pushes the
    surface further out of contrast, while darkening a near-black one does the same at the
    other end. Moving toward ``_text_on(bg)`` is always visible and always in the safe
    direction, which is one rule instead of a lightness branch.
    """
    return _mix(bg, _text_on(bg), t)


def _ratio(a: str, b: str) -> float:
    """WCAG contrast ratio between two ``#rrggbb`` colours, 1.0 .. 21.0."""
    la, lb = _luminance(a), _luminance(b)
    lighter, darker = max(la, lb), min(la, lb)
    return (lighter + 0.05) / (darker + 0.05)


# WCAG AA for body text. Also the threshold the settings preview warns against, so the
# number lives in one place per language.
_AA_CONTRAST = 4.5


def _ink(color: str) -> str:
    """``color`` darkened just far enough to be readable AS TEXT on white.

    A brand colour is chosen to work as a filled surface, not as 16px type on a white card:
    BT Orange ``#ff5400`` is 3.0:1 on white, which fails AA outright. Blending toward black
    in small steps keeps the hue recognisable and stops at the first step that passes,
    rather than jumping to a near-black that no longer reads as the brand at all.

    Iterative because the relationship between a blend factor and the resulting ratio is
    not linear -- there is no closed form to solve for t. Nineteen 5% steps covers every
    input: the worst case is pure white, which needs t=0.55, and pure yellow lands at 0.55
    too. Returning the last value rather than raising keeps the "never blow up on the
    render path" contract the rest of this module holds.
    """
    out = color
    for i in range(19):
        out = _mix(color, "#000000", i * 0.05)
        if _ratio(out, "#ffffff") >= _AA_CONTRAST:
            return out
    return out


# -- Demo, production ----------------------------------------------------------------
# The nav/user/logout/body classes below reproduce base.html's pre-brand output exactly.
# They are a refactor, not a redesign: tests/test_profile_theme.py asserts on them.
_DEMO_PROD = {
    "nav_bg": "bg-blue-900",
    "nav_fg": "text-white",
    "nav_hover": "hover:bg-blue-800",
    "nav_active": "bg-blue-800",
    "user_text": "text-blue-200",
    "logout_btn": "bg-blue-700 hover:bg-blue-600",
    "brand_hover": "hover:text-blue-200",
    "body_bg": "bg-gray-50",
    "warp": "text-blue-100",
    "weft": "text-blue-400",
    "rail": "",
    "product": "Infrastructure",
    # The edition, not the profile -- see _DEMO_DEV's chip_label below for what this slot
    # is for. It said "Demo", which was the profile name leaking into an edition's place,
    # and it is the ONE variant a person running their own estate for real ever sees:
    # .env.example ships APP_ENV=development, so setting production is a deliberate act by
    # somebody whose infrastructure is not a demonstration. Telling them otherwise on every
    # page of the app is the last place the old framing survived.
    "chip_label": "Production",
    "chip_class": "bg-emerald-500 text-emerald-950",
    "login_icon_bg": "bg-blue-100",
    "login_mark_warp": "text-blue-900",
    "login_mark_weft": "text-blue-500",
    "login_ring": "focus:ring-blue-500",
    "login_btn": "bg-blue-700 hover:bg-blue-800",
    "login_alt_bg": "bg-blue-50",
    "login_alt_fg": "text-blue-700",
    "hex": {
        "nav_bg": "#1e3a8a", "nav_fg": "#ffffff",
        "nav_warp": "#dbeafe",
        "nav_weft": "#60a5fa",
        "body_bg": "#f8fafc",
        "link": "#2563eb",
        "rail": "",
    },
    "favicon": _favicon("#1e3a8a", "#60a5fa"),
}

# -- Demo, anything else -------------------------------------------------------------
_DEMO_DEV = dict(
    _DEMO_PROD,
    nav_bg="bg-emerald-700",
    nav_hover="hover:bg-emerald-600",
    nav_active="bg-emerald-600",
    user_text="text-emerald-200",
    logout_btn="bg-emerald-600 hover:bg-emerald-500",
    brand_hover="hover:text-emerald-200",
    warp="text-emerald-100",
    weft="text-emerald-300",
    # The lockup's product word already says which profile this is ("Infrastructure" vs
    # "POV"), so the chip carries the one fact the lockup does not: the edition. This is
    # also the pre-brand pill's exact wording, kept rather than reinvented.
    #
    # It is short on purpose. The chip sits beside the product word in a 64px bar that
    # also has to hold the username, the settings cog and the menu toggle, and it is the
    # first thing that stops fitting: "Demo · Community" left 2px of headroom at 1280px
    # where this leaves 61px. tests/test_profile_theme pins the length.
    chip_label="Community",
    hex={
        "nav_bg": "#047857", "nav_fg": "#ffffff",
        "nav_warp": "#d1fae5",
        "nav_weft": "#6ee7b7",
        "body_bg": "#f8fafc",
        "link": "#059669",
        "rail": "",
    },
)

# -- POV -----------------------------------------------------------------------------
# Flat violet, not a gradient: base.html reuses nav_bg on the 288px-wide vertical flyout
# drawer, where a horizontal gradient reads as a rendering bug rather than a choice.
#
# The chip says "Customer tenants" rather than "POV" because the product word beside it
# already says POV. The chip's job is to name the consequence, not repeat the label.
_POV = {
    "nav_bg": "bg-violet-900",
    "nav_fg": "text-white",
    "nav_hover": "hover:bg-violet-800",
    "nav_active": "bg-fuchsia-700",
    "user_text": "text-violet-200",
    "logout_btn": "bg-fuchsia-700 hover:bg-fuchsia-600",
    "brand_hover": "hover:text-fuchsia-200",
    "body_bg": "bg-violet-50",
    "warp": "text-violet-100",
    "weft": "text-fuchsia-400",
    "rail": "h-1 bg-fuchsia-500",
    "product": "POV",
    "chip_label": "Customer tenants",
    "chip_class": "bg-fuchsia-500 text-fuchsia-950",
    "login_icon_bg": "bg-violet-100",
    "login_mark_warp": "text-violet-900",
    "login_mark_weft": "text-fuchsia-500",
    "login_ring": "focus:ring-fuchsia-500",
    "login_btn": "bg-violet-800 hover:bg-violet-700",
    "login_alt_bg": "bg-violet-50",
    "login_alt_fg": "text-violet-700",
    "hex": {
        "nav_bg": "#4c1d95", "nav_fg": "#ffffff",
        "nav_warp": "#ede9fe",
        "nav_weft": "#e879f9",
        "body_bg": "#f5f3ff",
        "link": "#7c3aed",
        "rail": "#d946ef",
    },
    "favicon": _favicon("#4c1d95", "#d946ef"),
}

# -- Operator-chosen accents ---------------------------------------------------------
# What an admin picks in Settings -> Appearance. Each entry is a complete chrome override
# applied ON TOP of the profile theme above, so the three built-in palettes stay exactly as
# they are for an instance that never sets one.
#
# Presets rather than a colour picker, and every string written out in full rather than
# composed from a hue name. Two reasons, and the second is the one that bites later:
#
#   1. Each palette is hand-checked for contrast. A free hex lets an operator pick something
#      that renders the nav unreadable, and the nav is how you leave the page you broke.
#   2. These are Tailwind class names on the Play CDN today. The CDN scans the DOM, so an
#      f-string would in fact work right now -- but a build step would purge by scanning
#      SOURCE, and every composed name would vanish silently. Literals survive that move.
#      (The banner below is free-hex precisely because it is an inline style, not a class.)
#
# `rail` is cleared on every accent: the rail slot in base.html is where the environment
# banner renders, and two coloured strips stacked there is noise. An accented POV therefore
# gives up its fuchsia rail -- it keeps the `POV -` title prefix, the POV product word and
# the "Customer tenants" chip, which is the signal that survives greyscale anyway.
#
# `label` and `swatch` exist so the settings UI renders its buttons from this table instead
# of keeping a second copy of the palette in a template.
_ACCENTS = {
    "blue": {
        "label": "Blue",
        "swatch": "#1e3a8a",
        "nav_bg": "bg-blue-900",
        "nav_fg": "text-white",
        "nav_hover": "hover:bg-blue-800",
        "nav_active": "bg-blue-800",
        "user_text": "text-blue-200",
        "logout_btn": "bg-blue-700 hover:bg-blue-600",
        "brand_hover": "hover:text-blue-200",
        "body_bg": "bg-gray-50",
        "warp": "text-blue-100",
        "weft": "text-blue-400",
        "rail": "",
        "chip_class": "bg-emerald-500 text-emerald-950",
        "login_bg": "bg-gradient-to-br from-blue-900 to-blue-700",
        "login_icon_bg": "bg-blue-100",
        "login_mark_warp": "text-blue-900",
        "login_mark_weft": "text-blue-500",
        "login_ring": "focus:ring-blue-500",
        "login_btn": "bg-blue-700 hover:bg-blue-800",
        "login_alt_bg": "bg-blue-50",
        "login_alt_fg": "text-blue-700",
        "hex": {"nav_bg": "#1e3a8a", "nav_fg": "#ffffff", "nav_warp": "#dbeafe", "nav_weft": "#60a5fa",
                "body_bg": "#f8fafc", "link": "#2563eb", "rail": ""},
        "favicon": _favicon("#1e3a8a", "#60a5fa"),
    },
    "emerald": {
        "label": "Emerald",
        "swatch": "#064e3b",
        "nav_bg": "bg-emerald-900",
        "nav_fg": "text-white",
        "nav_hover": "hover:bg-emerald-800",
        "nav_active": "bg-emerald-800",
        "user_text": "text-emerald-200",
        "logout_btn": "bg-emerald-700 hover:bg-emerald-600",
        "brand_hover": "hover:text-emerald-200",
        "body_bg": "bg-gray-50",
        "warp": "text-emerald-100",
        "weft": "text-emerald-400",
        "rail": "",
        # Amber, not emerald: a chip in the nav's own hue reads as part of the bar rather
        # than as a separate badge. Every chip below is a deliberate step off its nav.
        "chip_class": "bg-amber-400 text-amber-950",
        "login_bg": "bg-gradient-to-br from-emerald-900 to-emerald-700",
        "login_icon_bg": "bg-emerald-100",
        "login_mark_warp": "text-emerald-900",
        "login_mark_weft": "text-emerald-500",
        "login_ring": "focus:ring-emerald-500",
        "login_btn": "bg-emerald-700 hover:bg-emerald-800",
        "login_alt_bg": "bg-emerald-50",
        "login_alt_fg": "text-emerald-700",
        "hex": {"nav_bg": "#064e3b", "nav_fg": "#ffffff", "nav_warp": "#d1fae5", "nav_weft": "#34d399",
                "body_bg": "#f8fafc", "link": "#059669", "rail": ""},
        "favicon": _favicon("#064e3b", "#34d399"),
    },
    "teal": {
        "label": "Teal",
        "swatch": "#134e4a",
        "nav_bg": "bg-teal-900",
        "nav_fg": "text-white",
        "nav_hover": "hover:bg-teal-800",
        "nav_active": "bg-teal-800",
        "user_text": "text-teal-200",
        "logout_btn": "bg-teal-700 hover:bg-teal-600",
        "brand_hover": "hover:text-teal-200",
        "body_bg": "bg-gray-50",
        "warp": "text-teal-100",
        "weft": "text-teal-400",
        "rail": "",
        "chip_class": "bg-amber-400 text-amber-950",
        "login_bg": "bg-gradient-to-br from-teal-900 to-teal-700",
        "login_icon_bg": "bg-teal-100",
        "login_mark_warp": "text-teal-900",
        "login_mark_weft": "text-teal-500",
        "login_ring": "focus:ring-teal-500",
        "login_btn": "bg-teal-700 hover:bg-teal-800",
        "login_alt_bg": "bg-teal-50",
        "login_alt_fg": "text-teal-700",
        "hex": {"nav_bg": "#134e4a", "nav_fg": "#ffffff", "nav_warp": "#ccfbf1", "nav_weft": "#2dd4bf",
                "body_bg": "#f8fafc", "link": "#0d9488", "rail": ""},
        "favicon": _favicon("#134e4a", "#2dd4bf"),
    },
    "indigo": {
        "label": "Indigo",
        "swatch": "#312e81",
        "nav_bg": "bg-indigo-900",
        "nav_fg": "text-white",
        "nav_hover": "hover:bg-indigo-800",
        "nav_active": "bg-indigo-800",
        "user_text": "text-indigo-200",
        "logout_btn": "bg-indigo-700 hover:bg-indigo-600",
        "brand_hover": "hover:text-indigo-200",
        "body_bg": "bg-gray-50",
        "warp": "text-indigo-100",
        "weft": "text-indigo-400",
        "rail": "",
        "chip_class": "bg-emerald-500 text-emerald-950",
        "login_bg": "bg-gradient-to-br from-indigo-900 to-indigo-700",
        "login_icon_bg": "bg-indigo-100",
        "login_mark_warp": "text-indigo-900",
        "login_mark_weft": "text-indigo-500",
        "login_ring": "focus:ring-indigo-500",
        "login_btn": "bg-indigo-700 hover:bg-indigo-800",
        "login_alt_bg": "bg-indigo-50",
        "login_alt_fg": "text-indigo-700",
        "hex": {"nav_bg": "#312e81", "nav_fg": "#ffffff", "nav_warp": "#e0e7ff", "nav_weft": "#818cf8",
                "body_bg": "#f8fafc", "link": "#4f46e5", "rail": ""},
        "favicon": _favicon("#312e81", "#818cf8"),
    },
    "violet": {
        "label": "Violet",
        "swatch": "#4c1d95",
        "nav_bg": "bg-violet-900",
        "nav_fg": "text-white",
        "nav_hover": "hover:bg-violet-800",
        "nav_active": "bg-violet-800",
        "user_text": "text-violet-200",
        "logout_btn": "bg-violet-700 hover:bg-violet-600",
        "brand_hover": "hover:text-violet-200",
        "body_bg": "bg-gray-50",
        "warp": "text-violet-100",
        "weft": "text-violet-400",
        "rail": "",
        "chip_class": "bg-fuchsia-500 text-fuchsia-950",
        "login_bg": "bg-gradient-to-br from-violet-900 to-violet-700",
        "login_icon_bg": "bg-violet-100",
        "login_mark_warp": "text-violet-900",
        "login_mark_weft": "text-violet-500",
        "login_ring": "focus:ring-violet-500",
        "login_btn": "bg-violet-700 hover:bg-violet-800",
        "login_alt_bg": "bg-violet-50",
        "login_alt_fg": "text-violet-700",
        "hex": {"nav_bg": "#4c1d95", "nav_fg": "#ffffff", "nav_warp": "#ede9fe", "nav_weft": "#a78bfa",
                "body_bg": "#f8fafc", "link": "#7c3aed", "rail": ""},
        "favicon": _favicon("#4c1d95", "#a78bfa"),
    },
    "rose": {
        "label": "Rose",
        "swatch": "#881337",
        "nav_bg": "bg-rose-900",
        "nav_fg": "text-white",
        "nav_hover": "hover:bg-rose-800",
        "nav_active": "bg-rose-800",
        "user_text": "text-rose-200",
        "logout_btn": "bg-rose-700 hover:bg-rose-600",
        "brand_hover": "hover:text-rose-200",
        "body_bg": "bg-gray-50",
        "warp": "text-rose-100",
        "weft": "text-rose-400",
        "rail": "",
        "chip_class": "bg-amber-400 text-amber-950",
        "login_bg": "bg-gradient-to-br from-rose-900 to-rose-700",
        "login_icon_bg": "bg-rose-100",
        "login_mark_warp": "text-rose-900",
        "login_mark_weft": "text-rose-500",
        "login_ring": "focus:ring-rose-500",
        "login_btn": "bg-rose-700 hover:bg-rose-800",
        "login_alt_bg": "bg-rose-50",
        "login_alt_fg": "text-rose-700",
        "hex": {"nav_bg": "#881337", "nav_fg": "#ffffff", "nav_warp": "#ffe4e6", "nav_weft": "#fb7185",
                "body_bg": "#f8fafc", "link": "#e11d48", "rail": ""},
        "favicon": _favicon("#881337", "#fb7185"),
    },
    "amber": {
        "label": "Amber",
        "swatch": "#78350f",
        "nav_bg": "bg-amber-900",
        "nav_fg": "text-white",
        "nav_hover": "hover:bg-amber-800",
        "nav_active": "bg-amber-800",
        "user_text": "text-amber-200",
        "logout_btn": "bg-amber-700 hover:bg-amber-600",
        "brand_hover": "hover:text-amber-200",
        "body_bg": "bg-gray-50",
        "warp": "text-amber-100",
        "weft": "text-amber-400",
        "rail": "",
        "chip_class": "bg-sky-400 text-sky-950",
        "login_bg": "bg-gradient-to-br from-amber-900 to-amber-700",
        "login_icon_bg": "bg-amber-100",
        "login_mark_warp": "text-amber-900",
        "login_mark_weft": "text-amber-500",
        "login_ring": "focus:ring-amber-500",
        "login_btn": "bg-amber-700 hover:bg-amber-800",
        "login_alt_bg": "bg-amber-50",
        "login_alt_fg": "text-amber-700",
        "hex": {"nav_bg": "#78350f", "nav_fg": "#ffffff", "nav_warp": "#fef3c7", "nav_weft": "#fbbf24",
                "body_bg": "#f8fafc", "link": "#b45309", "rail": ""},
        "favicon": _favicon("#78350f", "#fbbf24"),
    },
    "slate": {
        "label": "Slate",
        "swatch": "#0f172a",
        "nav_bg": "bg-slate-900",
        "nav_fg": "text-white",
        "nav_hover": "hover:bg-slate-800",
        "nav_active": "bg-slate-800",
        "user_text": "text-slate-300",
        "logout_btn": "bg-slate-700 hover:bg-slate-600",
        "brand_hover": "hover:text-slate-300",
        "body_bg": "bg-gray-50",
        "warp": "text-slate-100",
        "weft": "text-slate-400",
        "rail": "",
        "chip_class": "bg-emerald-500 text-emerald-950",
        "login_bg": "bg-gradient-to-br from-slate-900 to-slate-700",
        "login_icon_bg": "bg-slate-100",
        "login_mark_warp": "text-slate-900",
        "login_mark_weft": "text-slate-500",
        "login_ring": "focus:ring-slate-500",
        "login_btn": "bg-slate-700 hover:bg-slate-800",
        "login_alt_bg": "bg-slate-50",
        "login_alt_fg": "text-slate-700",
        "hex": {"nav_bg": "#0f172a", "nav_fg": "#ffffff", "nav_warp": "#f1f5f9", "nav_weft": "#94a3b8",
                "body_bg": "#f8fafc", "link": "#475569", "rail": ""},
        "favicon": _favicon("#0f172a", "#94a3b8"),
    },
}

# Keys an accent is allowed to carry that are NOT chrome -- stripped before the override is
# applied so they cannot leak into the theme dict and end up rendered as a class.
_ACCENT_META = ("label", "swatch")


def accent_choices() -> list[dict]:
    """``[{key, label, swatch}, ...]`` for the settings picker, in display order."""
    return [
        {"key": key, "label": palette["label"], "swatch": palette["swatch"]}
        for key, palette in _ACCENTS.items()
    ]


# -- Operator-chosen colours ---------------------------------------------------------
# The presets above are a closed set because a chrome slot holds a literal Tailwind class
# and an arbitrary hex cannot be one (see the note over _ACCENTS). A customer's brand is
# not in that set, so this is the other half: the slot still holds a class name, but a
# HAND-WRITTEN one, defined in templates/_brand_css.html against CSS custom properties
# computed from three operator hexes.
#
# That sidesteps the purge hazard rather than working around it. `brand-nav` is a literal
# string in this file and a literal selector in that partial; it is not a Tailwind utility,
# so a build step that scans source for utilities neither purges it nor needs to know it
# exists. And because every slot keeps holding a class, base.html's
# `class="{{ theme.nav_bg }}"` is untouched -- all 21 slots and the drawer keep working
# through the indirection that is already there.
#
# Every selector in the partial is written `.brand-custom .brand-nav`, and `brand_scope`
# below is what puts `brand-custom` on <html>. That is not decoration. A bare `.brand-nav`
# is specificity (0,1,0) -- a TIE with any Tailwind utility on the same element setting the
# same property, broken by source order, which for a CDN that injects its sheet at runtime
# is script timing. base.html:114 carries a hardcoded `text-white` next to `theme.nav_bg`,
# and login.html has two more. The descendant selector makes it (0,2,0), which wins
# deterministically. _checklist_styles.html's header states the same rule for the same
# reason.
_BRAND_SCOPE = "brand-custom"

# Slot -> class. Fifteen classes for twenty-one slots: `brand-btn`, `brand-tint` and
# `brand-ink` each serve two slots that are the same colour job in two places.
#
# `rail` is cleared, exactly as every preset clears it, for the reason given over _ACCENTS:
# the rail slot in base.html is where the environment banner renders, and two coloured
# strips stacked there is noise.
_CUSTOM_SLOTS = {
    "nav_bg": "brand-nav",
    # `brand-nav` already sets `color` on the bar itself, and that beats the `text-white`
    # sitting beside it. It does NOT reach a descendant carrying its own `text-white`,
    # though -- a declaration outranks inheritance however specific the ancestor's rule is.
    # The drawer's wordmark is exactly that, so the foreground is a slot of its own, and
    # every shipped palette fills it with the literal the markup used to hardcode.
    "nav_fg": "brand-nav-fg",
    "nav_hover": "brand-nav-hover",
    "nav_active": "brand-nav-active",
    "user_text": "brand-nav-muted",
    "logout_btn": "brand-btn",
    "brand_hover": "brand-lockup-hover",
    "body_bg": "brand-body",
    "warp": "brand-warp",
    "weft": "brand-weft",
    "rail": "",
    "chip_class": "brand-chip",
    "login_bg": "brand-login-bg",
    "login_icon_bg": "brand-tint",
    "login_mark_warp": "brand-ink",
    "login_mark_weft": "brand-accent-ink",
    "login_ring": "brand-ring",
    "login_btn": "brand-btn",
    "login_alt_bg": "brand-tint",
    "login_alt_fg": "brand-ink",
}


def _custom_palette(primary: str | None, secondary: str | None, accent: str | None) -> dict:
    """A complete chrome override from three operator hexes, or ``{}``.

    All three or nothing. A partial palette cannot fill twenty-one slots without inventing
    the colours it was not given, and inventing them is what the presets are for -- so one
    or two colours is inert and the preset (or the profile) renders unchanged. The API
    raises on a partial set so the operator sees it at the moment of saving; this returning
    ``{}`` is the backstop for a row pair that reached ``app_config`` some other way.

    Refuses rather than raises on a bad hex, matching the unrecognised-accent branch in
    ``theme_for``: this runs on every render, and a stale or hand-edited config row must
    yield stock chrome rather than a 500 on every page of the app.

    The refusal is also the security control. ``brand_css`` is interpolated INSIDE a
    ``<style>`` element, where Jinja's autoescaping buys nothing: CSS does not decode HTML
    entities, so a closing style tag in the stored text still ends the block and what
    follows is markup. Every value is the output of ``_mix``/``_text_on``/``_ink`` or a hex that
    matched ``_HEX``, so the emitted string is ``#rrggbb`` and separators by construction --
    there is no path by which operator text reaches it. Do not add a variable here whose
    value is not derived from those three, and do not relax ``_HEX``.
    """
    trio = [(c or "").strip().lower() for c in (primary, secondary, accent)]
    if not all(_HEX.match(c) for c in trio):
        return {}
    p, s, a = trio

    # Emitted as literal hex rather than as CSS `color-mix()` for three reasons: the same
    # derived values have to appear in the `hex` mirror and in the favicon, neither of
    # which can call CSS; browser support for `color-mix` is still uneven; and a string of
    # literal hex is trivially provable to be injection-free, which the docstring above
    # depends on.
    var = {
        "primary": p,
        "primary-fg": _text_on(p),
        "primary-hover": _step(p, 0.12),
        "primary-tint": _mix(p, "#ffffff", 0.90),
        "primary-ink": _ink(p),
        "secondary": s,
        "secondary-fg": _text_on(s),
        "secondary-hover": _step(s, 0.10),
        "secondary-tint": _mix(s, "#ffffff", 0.94),
        "secondary-deep": _mix(s, "#000000", 0.35),
        "accent": a,
        "accent-fg": _text_on(a),
        "accent-ink": _ink(a),
        # The neutral strand of the mark. Derived from the nav's own foreground rather than
        # fixed near-white, so it stays visible on a light nav as well as a dark one -- the
        # warp is the strand that is NOT supposed to carry the brand colour.
        "warp": _mix(s, _text_on(s), 0.85),
    }

    theme = dict(_CUSTOM_SLOTS)
    theme["brand_scope"] = _BRAND_SCOPE
    theme["brand_css"] = "".join(f"--brand-{k}:{v};" for k, v in var.items())
    # The mirror for the consumers that do not speak Tailwind -- the public docs shell and
    # the favicon. Same seven keys as every shipped palette; tests/test_branding pins that
    # shape across the table.
    theme["hex"] = {
        "nav_bg": s,
        "nav_fg": var["secondary-fg"],
        "nav_warp": var["warp"],
        "nav_weft": a,
        "body_bg": var["secondary-tint"],
        "link": var["primary-ink"],
        "rail": "",
    }
    theme["favicon"] = _favicon(s, a)
    return theme


# The login page is pre-auth chrome and is blue on every demo instance today, regardless of
# app_env. Kept that way deliberately: "demo keeps today's look" includes the front door.
_LOGIN_BG = {
    "demo": "bg-gradient-to-br from-blue-900 to-blue-700",
    "pov": "bg-gradient-to-br from-violet-950 via-violet-900 to-fuchsia-800",
}

def _title_prefix(profile: str, brand: str) -> str:
    """``<brand> - `` on demo, ``POV - <brand> - `` on POV.

    The POV marker leads because a tab title truncates from the RIGHT, and which instance
    you are on is the half worth keeping when it does.
    """
    return ("POV · " + brand + " · ") if profile == "pov" else (brand + " · ")


def theme_for(
    profile: str,
    app_env: str,
    *,
    brand: str | None = None,
    brand_full: str | None = None,
    accent: str | None = None,
    env_label: str | None = None,
    env_color: str | None = None,
    primary: str | None = None,
    secondary: str | None = None,
    accent_hex: str | None = None,
    logo: dict | None = None,
) -> dict:
    """Chrome and brand tokens for one instance.

    POV overrides ``app_env`` entirely -- a POV instance is violet whether it runs as
    production or development, because the tenant it reaches is what matters, not the
    deployment stage. Demo still defers to ``app_env`` for blue vs emerald.

    An unrecognised profile resolves to ``demo``, mirroring
    :func:`feature_flags.install_profile`. Same reason: this runs on the request path, and
    a typo in one config row must render today's chrome rather than a blank nav bar.

    The keyword arguments are the operator's own branding, read from ``app_config`` by
    :mod:`services.branding` and splatted in by the caller. They are keyword-only and all
    default to ``None``, and with none of them supplied this function returns exactly what
    it returned before they existed -- that equality is what ``tests/test_branding`` pins,
    and it is why ``tests/test_profile_theme``'s pinned demo strings needed no edit.

    This stays a pure function of its arguments on purpose: it is called on every render,
    from the context processor and from the public docs shell, and a config read buried in
    here would make it untestable from the outside and un-cacheable from the inside.

    Note ``accent`` recolours a POV instance too. Colour was never the whole profile signal
    -- the module docstring says so, and the title prefix, the product word and the chip all
    survive the override -- so an operator branding their POV does not lose the guard.

    ``primary``/``secondary``/``accent_hex`` are the operator's own three colours and
    outrank ``accent`` when all three are valid: an instance that has been given a
    customer's palette is not also asking for a preset. ``accent_hex`` sits beside
    ``accent`` rather than replacing it because they are different types -- one is a key
    into :data:`_ACCENTS`, the other a ``#rrggbb`` -- and the suffix says so where a reader
    will actually see it. See :func:`_custom_palette` for why a partial trio is inert.

    ``logo`` is ``{"url", "mime", "width", "height"}`` for an uploaded image, validated by
    :func:`services.branding._logo_override`, or ``None`` for the built-in mark. Passed
    through rather than interpreted: this module decides what the chrome *looks like*, and
    which of the two marks renders is ``_brand_mark.html``'s branch. The favicon is
    deliberately unaffected -- there is no image library here to produce a 16px raster, a
    wordmark is unreadable at that size, and the tab icon is the one profile signal visible
    on an unfocused tab.
    """
    profile = (profile or "").strip().lower()
    if profile not in VALID_PROFILES:
        profile = "demo"

    if profile == "pov":
        theme = dict(_POV)
    else:
        theme = dict(_DEMO_PROD if app_env == "production" else _DEMO_DEV)

    theme["login_bg"] = _LOGIN_BG[profile]

    # Before the brand keys below, so an accent can never overwrite one of them. Unknown
    # names are ignored rather than raising, matching the unrecognised-profile branch
    # above: a stale config row must render stock chrome, not a 500 on every page.
    palette = _ACCENTS.get((accent or "").strip().lower())
    if palette:
        theme.update({k: v for k, v in palette.items() if k not in _ACCENT_META})

    # After the preset, so three operator colours outrank a stored preset name rather than
    # fighting it -- and still before the brand keys, so neither can overwrite those.
    custom = _custom_palette(primary, secondary, accent_hex)
    theme.update(custom)
    # Always present, so no template has to guard on the key existing. Empty is the signal
    # that custom mode is off: _brand_css.html renders nothing at all for an empty
    # brand_css, which is what keeps an un-branded instance's HTML byte-identical.
    theme.setdefault("brand_css", "")
    theme.setdefault("brand_scope", "")

    theme["profile"] = profile
    # Zeroed when the custom trio won, because no preset was applied and saying otherwise
    # would make the settings picker show a selected swatch that is not what renders.
    theme["accent"] = "" if custom else (accent if palette else "")
    theme["brand"] = brand or BRAND
    theme["brand_full"] = brand_full or BRAND_FULL
    theme["mark_warp_path"] = MARK_WARP
    theme["mark_weft_path"] = MARK_WEFT
    # Always present so no template has to guard on the key. Falsy means the built-in mark.
    theme["logo"] = logo or None
    theme["title_prefix"] = _title_prefix(profile, theme["brand"])

    # The one free-form colour in the app. It is an inline style rather than a class, which
    # is what lets it be arbitrary -- and is also why callers must have validated the hex
    # before it gets here (services.branding does; nothing else may call this with a raw
    # config value). Falls back to the resolved nav colour so a label alone is enough.
    label = (env_label or "").strip()
    if label:
        background = env_color or theme["hex"]["nav_bg"]
        theme["env_banner"] = {
            "label": label,
            "bg": background,
            "fg": _text_on(background),
        }
    else:
        theme["env_banner"] = None
    return theme
