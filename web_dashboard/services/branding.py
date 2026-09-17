"""The operator's own name and colours, read from ``app_config``.

``services/ui_theme`` owns what the chrome *looks like* and is a pure function of its
arguments. This module owns *where the operator's choices are stored*, and is the only half
that touches the database. The split is deliberate: the theme stays unit-testable without a
session, and this stays the one place a stored value is trusted or rejected.

Eight keys, all global rows (community is single-workgroup):

    brand_name        replaces "Weaver Lab" everywhere it renders
    brand_full        replaces "Weaver Lab Applications" on the login card
    brand_accent      a key of ui_theme._ACCENTS
    brand_env_label   short text for the banner under the nav bar; empty = no banner
    brand_env_color   #rrggbb for that banner; empty = the accent's own nav colour
    brand_primary     #rrggbb -- buttons, links, the selected menu row
    brand_secondary   #rrggbb -- the navigation bar and the flyout drawer
    brand_accent_hex  #rrggbb -- badges, focus rings, the mark's accent strand

The last three are all-or-nothing and outrank ``brand_accent``: see ``ui_theme``'s
``_custom_palette`` for why a partial trio cannot be honoured. Note ``brand_accent`` and
``brand_accent_hex`` are different types -- a preset name and a colour -- which is what
the suffix is for.

Four more rows point at an uploaded logo, and are written by ``api/setup``'s own logo
endpoints rather than by ``patch_branding``:

    brand_logo_etag   sha256 of the stored bytes; also the URL segment and the ETag
    brand_logo_mime   one of ``brand_logo._ALLOWED``
    brand_logo_w      pixel width, if the format declared one
    brand_logo_h      pixel height, if the format declared one

The image itself is a row in ``brand_asset``, not here -- see that model for why -- and
``services/brand_logo`` owns it. This module only decides whether the pointer is
trustworthy enough to render, which is the same job it does for the colours.

Validation happens on READ, not only on write, and that is the point of this module rather
than a couple of ``config_service.get`` calls at the call site. ``app_config`` is not a
closed system: ``scripts/config_migrate`` writes it, a restored dump can carry rows from an
older schema, and an operator with psql can set anything. A value that fails its check is
dropped and the stock default renders -- the alternative is a malformed colour landing in an
inline ``style`` attribute on every page of the app.
"""
import re
import unicodedata

from . import config_service, ui_theme


# Display text, not identifiers. Long enough for a real company name, short enough that the
# nav lockup and the tab title still truncate gracefully -- the drawer header gives the
# wordmark ~200px beside a chip, and the bar is the first thing that stops fitting.
_MAX_BRAND = 32
_MAX_BRAND_FULL = 48
# The banner is one line under a 64px bar. "PRODUCTION - EU-WEST-1" is 22.
_MAX_ENV_LABEL = 24

# Six hex digits with the hash, nothing else. This value is interpolated into a style
# attribute, so anything looser -- `red; background-image: url(...)`, an unclosed quote --
# is CSS injection rather than a bad colour. Jinja escapes the quotes, which stops the
# attribute being broken out of; it does not stop extra declarations inside it.
_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

# A sha256 hex digest, exactly. This value is interpolated into a URL and then into an
# ``src`` attribute -- including on the public docs shell, which is a ``.format()`` template
# with no autoescaping at all -- so the same argument that makes ``_HEX_RE`` strict applies
# here. Nothing but a digest can reach the markup.
_LOGO_ETAG_RE = re.compile(r"^[0-9a-f]{64}$")

# Mirrors ``brand_logo._ALLOWED``. Duplicated rather than imported so that a stored row
# naming a type this app no longer serves is dropped on read, the way a stale accent name
# is -- and so this module keeps its "the only thing that trusts a stored value" role.
_ALLOWED_LOGO_MIMES = ("image/png", "image/jpeg", "image/webp", "image/svg+xml")


def _clean_text(value: str, limit: int) -> str:
    """Strip, drop control characters, collapse runs of whitespace, truncate.

    Control characters are removed rather than escaped: a bidi override or a zero-width
    joiner in the wordmark is not a rendering bug, it is a way to make the brand read as
    something other than what is stored, and there is no legitimate use for one here.
    Unicode category ``C`` covers the lot (Cc, Cf, Cs, Co, Cn) in one test.
    """
    if not value:
        return ""
    stripped = "".join(c for c in value if unicodedata.category(c)[0] != "C")
    return " ".join(stripped.split())[:limit]


def _clean_color(value: str) -> str:
    """``#rrggbb`` lowercased, or ``""`` if it is anything else."""
    candidate = (value or "").strip()
    return candidate.lower() if _HEX_RE.match(candidate) else ""


def _logo_override() -> dict:
    """``{"logo": {...}}`` when a valid logo pointer is stored, else ``{}``.

    Reads only the four short pointer rows, never the blob: this runs on every render, and
    ``config_service`` already holds those rows in its cache, so the cost is dict lookups.
    The bytes are fetched by their own route, once, and then cached by the browser.

    Dimensions are optional -- an SVG need not declare any -- and are carried only so the
    markup can reserve the right aspect ratio and not reflow the nav bar when the image
    lands. A non-numeric stored value is dropped rather than rendered.
    """
    etag = (config_service.get_raw("brand_logo_etag") or "").strip().lower()
    mime = (config_service.get_raw("brand_logo_mime") or "").strip().lower()
    if not _LOGO_ETAG_RE.match(etag) or mime not in _ALLOWED_LOGO_MIMES:
        return {}

    def _dim(key: str) -> int | None:
        raw = (config_service.get_raw(key) or "").strip()
        return int(raw) if raw.isdigit() and 0 < int(raw) <= 100000 else None

    return {"logo": {
        "url": f"/api/setup/branding/logo/{etag}",
        "mime": mime,
        "width": _dim("brand_logo_w"),
        "height": _dim("brand_logo_h"),
    }}


def overrides() -> dict:
    """Validated branding keyword arguments for :func:`ui_theme.theme_for`.

    Only keys the operator has actually set are returned, so the caller's splat is a no-op
    on an instance that has never opened the Appearance panel and ``theme_for`` yields its
    original output.

    Never raises. This runs inside the template context processor, i.e. on every render of
    every page including the login screen, and on the public docs shell. A database that is
    down, unmigrated, or holding rows encrypted under a rotated ``JWT_SECRET_KEY`` must cost
    the operator their custom wordmark, not the ability to reach the app at all -- the same
    rule ``api/docs_pages._shell`` states for the profile lookup.

    Cost is a handful of dict lookups: ``config_service`` caches the whole table on a 5s
    TTL. ``get_raw`` rather than ``get`` because these are display strings -- resolving an
    ``aws_sm://``-looking value would put a vault round trip on the render path.
    """
    try:
        out = {}
        brand = _clean_text(config_service.get_raw("brand_name"), _MAX_BRAND)
        if brand:
            out["brand"] = brand

        brand_full = _clean_text(config_service.get_raw("brand_full"), _MAX_BRAND_FULL)
        if brand_full:
            out["brand_full"] = brand_full
        elif brand:
            # The login card's subtitle would otherwise still read "Weaver Lab
            # Applications" under a renamed wordmark, which looks like a missed spot
            # rather than a setting nobody filled in.
            out["brand_full"] = _clean_text(f"{brand} Applications", _MAX_BRAND_FULL)

        accent = (config_service.get_raw("brand_accent") or "").strip().lower()
        if accent in ui_theme._ACCENTS:
            out["accent"] = accent

        # The operator's own three, passed only when all three survive the hex check. The
        # API rejects a partial trio at save time, so this gate is for the other ways a row
        # reaches app_config -- config_migrate, a restored dump, psql -- where half a
        # palette must be inert rather than half-applied. ui_theme refuses a partial set
        # too; this is the cheaper of the two backstops, not the only one.
        trio = {
            "primary": _clean_color(config_service.get_raw("brand_primary")),
            "secondary": _clean_color(config_service.get_raw("brand_secondary")),
            "accent_hex": _clean_color(config_service.get_raw("brand_accent_hex")),
        }
        if all(trio.values()):
            out.update(trio)

        label = _clean_text(config_service.get_raw("brand_env_label"), _MAX_ENV_LABEL)
        if label:
            out["env_label"] = label
            # Only meaningful with a label, and theme_for falls back to the accent's nav
            # colour when this is absent, so an invalid stored colour degrades to a
            # correct-looking banner rather than to no banner at all.
            color = _clean_color(config_service.get_raw("brand_env_color"))
            if color:
                out["env_color"] = color

        out.update(_logo_override())
        return out
    except Exception:
        return {}
