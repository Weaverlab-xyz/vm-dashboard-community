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
        r, g, b = (int(bg[i:i + 2], 16) / 255 for i in (1, 3, 5))
    except (ValueError, IndexError):
        return "#ffffff"

    def _lin(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    luminance = 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)
    return "#0f172a" if luminance > 0.179 else "#ffffff"


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
# The chip says "Customer tenants" rather than "POV" because the product word beside it
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
        "hex": {"nav_bg": "#1e3a8a", "nav_warp": "#dbeafe", "nav_weft": "#60a5fa",
                "body_bg": "#f8fafc", "link": "#2563eb", "rail": ""},
        "favicon": _favicon("#1e3a8a", "#60a5fa"),
    },
    "emerald": {
        "label": "Emerald",
        "swatch": "#064e3b",
        "nav_bg": "bg-emerald-900",
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
        "hex": {"nav_bg": "#064e3b", "nav_warp": "#d1fae5", "nav_weft": "#34d399",
                "body_bg": "#f8fafc", "link": "#059669", "rail": ""},
        "favicon": _favicon("#064e3b", "#34d399"),
    },
    "teal": {
        "label": "Teal",
        "swatch": "#134e4a",
        "nav_bg": "bg-teal-900",
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
        "hex": {"nav_bg": "#134e4a", "nav_warp": "#ccfbf1", "nav_weft": "#2dd4bf",
                "body_bg": "#f8fafc", "link": "#0d9488", "rail": ""},
        "favicon": _favicon("#134e4a", "#2dd4bf"),
    },
    "indigo": {
        "label": "Indigo",
        "swatch": "#312e81",
        "nav_bg": "bg-indigo-900",
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
        "hex": {"nav_bg": "#312e81", "nav_warp": "#e0e7ff", "nav_weft": "#818cf8",
                "body_bg": "#f8fafc", "link": "#4f46e5", "rail": ""},
        "favicon": _favicon("#312e81", "#818cf8"),
    },
    "violet": {
        "label": "Violet",
        "swatch": "#4c1d95",
        "nav_bg": "bg-violet-900",
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
        "hex": {"nav_bg": "#4c1d95", "nav_warp": "#ede9fe", "nav_weft": "#a78bfa",
                "body_bg": "#f8fafc", "link": "#7c3aed", "rail": ""},
        "favicon": _favicon("#4c1d95", "#a78bfa"),
    },
    "rose": {
        "label": "Rose",
        "swatch": "#881337",
        "nav_bg": "bg-rose-900",
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
        "hex": {"nav_bg": "#881337", "nav_warp": "#ffe4e6", "nav_weft": "#fb7185",
                "body_bg": "#f8fafc", "link": "#e11d48", "rail": ""},
        "favicon": _favicon("#881337", "#fb7185"),
    },
    "amber": {
        "label": "Amber",
        "swatch": "#78350f",
        "nav_bg": "bg-amber-900",
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
        "hex": {"nav_bg": "#78350f", "nav_warp": "#fef3c7", "nav_weft": "#fbbf24",
                "body_bg": "#f8fafc", "link": "#b45309", "rail": ""},
        "favicon": _favicon("#78350f", "#fbbf24"),
    },
    "slate": {
        "label": "Slate",
        "swatch": "#0f172a",
        "nav_bg": "bg-slate-900",
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
        "hex": {"nav_bg": "#0f172a", "nav_warp": "#f1f5f9", "nav_weft": "#94a3b8",
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

    theme["profile"] = profile
    theme["accent"] = accent if palette else ""
    theme["brand"] = brand or BRAND
    theme["brand_full"] = brand_full or BRAND_FULL
    theme["mark_warp_path"] = MARK_WARP
    theme["mark_weft_path"] = MARK_WEFT
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
