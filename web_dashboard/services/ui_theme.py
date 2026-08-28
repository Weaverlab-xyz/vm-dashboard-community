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

Every value is a literal Tailwind class string (the app is on the Tailwind Play CDN, which
scans the DOM, so composed class names are not an option) except ``hex``, which carries
raw colours for the two consumers that do not speak Tailwind: the docs browser
(``api/docs_pages``, which writes plain CSS) and the favicon data URI.

Colour alone is deliberately NOT the whole signal -- it fails for colour-vision deficiency
and fails completely in a greyscale screenshot. The wordmark, the chip text and the
``POV -`` title prefix each carry the profile independently.
"""
from urllib.parse import quote

from .feature_flags import VALID_PROFILES


BRAND = "Weaver Lab"
# Too long for the nav bar, which is why the lockup there is BRAND + product. It belongs
# on the login page, the one screen with room for it.
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


# -- Demo, production ----------------------------------------------------------------
# The nav/user/logout/body classes below reproduce base.html's pre-brand output exactly.
# They are a refactor, not a redesign: tests/test_profile_theme.py asserts on them.
_DEMO_PROD = {
    "nav_bg": "bg-blue-900",
    "nav_hover": "hover:bg-blue-800",
    "nav_active": "bg-blue-800",
    "user_text": "text-blue-200",
    "logout_btn": "bg-blue-700 hover:bg-blue-600",
    "brand_hover": "hover:text-blue-200",
    "body_bg": "bg-gray-50",
    "warp": "text-blue-100",
    "weft": "text-blue-400",
    "slash": "text-blue-400",
    "rail": "",
    "product": "Infrastructure",
    "chip_label": "Demo",
    "chip_class": "bg-emerald-500 text-emerald-950",
    "login_icon_bg": "bg-blue-100",
    "login_mark_warp": "text-blue-900",
    "login_mark_weft": "text-blue-500",
    "login_ring": "focus:ring-blue-500",
    "login_btn": "bg-blue-700 hover:bg-blue-800",
    "login_alt_bg": "bg-blue-50",
    "login_alt_fg": "text-blue-700",
    "hex": {
        "nav_bg": "#1e3a8a",
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
    slash="text-emerald-300",
    # The lockup's product word already says which profile this is ("Infrastructure" vs
    # "POV"), so the chip carries the one fact the lockup does not: the edition. This is
    # also the pre-brand pill's exact wording, kept rather than reinvented.
    #
    # It is short on purpose. static/js/app.js:responsiveNav folds the ENTIRE nav into the
    # flyout drawer when the row overflows, and at a 1280px viewport the demo/community
    # bar is the widest variant -- measured, "Demo · Community" here left 2px of headroom
    # and this leaves 61px. tests/test_profile_theme pins the length.
    chip_label="Community",
    hex={
        "nav_bg": "#047857",
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
# The chip says "Customer tenants" rather than "POV" because the wordmark beside it
# already says POV. The chip's job is to name the consequence, not repeat the label.
_POV = {
    "nav_bg": "bg-violet-900",
    "nav_hover": "hover:bg-violet-800",
    "nav_active": "bg-fuchsia-700",
    "user_text": "text-violet-200",
    "logout_btn": "bg-fuchsia-700 hover:bg-fuchsia-600",
    "brand_hover": "hover:text-fuchsia-200",
    "body_bg": "bg-violet-50",
    "warp": "text-violet-100",
    "weft": "text-fuchsia-400",
    "slash": "text-fuchsia-400",
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
        "nav_bg": "#4c1d95",
        "nav_warp": "#ede9fe",
        "nav_weft": "#e879f9",
        "body_bg": "#f5f3ff",
        "link": "#7c3aed",
        "rail": "#d946ef",
    },
    "favicon": _favicon("#4c1d95", "#d946ef"),
}

# The login page is pre-auth chrome and is blue on every demo instance today, regardless of
# app_env. Kept that way deliberately: "demo keeps today's look" includes the front door.
_LOGIN_BG = {
    "demo": "bg-gradient-to-br from-blue-900 to-blue-700",
    "pov": "bg-gradient-to-br from-violet-950 via-violet-900 to-fuchsia-800",
}

_TITLE_PREFIX = {"demo": BRAND + " · ", "pov": "POV · " + BRAND + " · "}


def theme_for(profile: str, app_env: str) -> dict:
    """Chrome and brand tokens for one instance.

    POV overrides ``app_env`` entirely -- a POV instance is violet whether it runs as
    production or development, because the tenant it reaches is what matters, not the
    deployment stage. Demo still defers to ``app_env`` for blue vs emerald.

    An unrecognised profile resolves to ``demo``, mirroring
    :func:`feature_flags.install_profile`. Same reason: this runs on the request path, and
    a typo in one config row must render today's chrome rather than a blank nav bar.
    """
    profile = (profile or "").strip().lower()
    if profile not in VALID_PROFILES:
        profile = "demo"

    if profile == "pov":
        theme = dict(_POV)
    else:
        theme = dict(_DEMO_PROD if app_env == "production" else _DEMO_DEV)

    theme["profile"] = profile
    theme["brand"] = BRAND
    theme["brand_full"] = BRAND_FULL
    theme["mark_warp_path"] = MARK_WARP
    theme["mark_weft_path"] = MARK_WEFT
    theme["title_prefix"] = _TITLE_PREFIX[profile]
    theme["login_bg"] = _LOGIN_BG[profile]
    return theme
