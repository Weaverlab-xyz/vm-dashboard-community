"""The two instances must not look alike, and the demo one must not have moved.

`install_profile` is a GATE (see tests/test_install_profile.py): a demo instance resolves
its BeyondTrust tenant from the global singletons, a POV instance from a registry of named
customer tenants, and the wrong answer is silent rather than loud. `services/ui_theme` is
the half of that split an operator can actually see, so the properties pinned here are the
ones whose absence would be invisible until someone acted on the wrong tab:

  * POV and demo differ on EVERY chrome token — not "mostly", because a single shared
    surface is the one an eye lands on.
  * The demo tokens still emit today's exact class strings. A re-skin of POV must never
    quietly recolour the instance nobody asked to change.
  * POV ignores app_env. Whose tenant it reaches is what matters, not the deploy stage.
  * An unknown profile renders demo chrome, mirroring feature_flags.install_profile().
  * The signal survives greyscale: wordmark, chip text and title prefix each carry the
    profile without colour.
  * base.html reads the tokens rather than re-deriving colour from app_env — two sources
    for one palette is how the nav bar and the flyout drawer that share nav_bg end up
    different colours.

Pure: imports ui_theme (which needs only feature_flags.VALID_PROFILES) and parses files.

Runs under pytest, or standalone:
    python tests/test_profile_theme.py
"""
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-profile-theme")

from web_dashboard.services import ui_theme  # noqa: E402

_TPL = os.path.join(_ROOT, "web_dashboard", "templates")
_MAIN = os.path.join(_ROOT, "web_dashboard", "main.py")
_DOCS = os.path.join(_ROOT, "web_dashboard", "api", "docs_pages.py")

# Every token whose whole job is to look different on the two instances. The copy strings
# (brand, mark paths) are deliberately excluded: those are the CONSTANT half of the system.
_CHROME = (
    "nav_bg", "nav_hover", "nav_active", "user_text", "logout_btn", "brand_hover",
    "body_bg", "warp", "weft", "slash", "chip_class", "login_bg", "login_icon_bg",
    "login_mark_warp", "login_mark_weft", "login_ring", "login_btn", "login_alt_bg",
    "login_alt_fg", "favicon",
)


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


# ── the two instances diverge ────────────────────────────────────────────────

def test_every_chrome_token_differs_between_the_profiles():
    demo = ui_theme.theme_for("demo", "production")
    pov = ui_theme.theme_for("pov", "production")
    same = [k for k in _CHROME if demo[k] == pov[k]]
    assert not same, (
        f"POV and demo share these chrome tokens: {same}. A shared surface is the one an "
        f"operator's eye lands on when they act on the wrong instance.")


def test_the_hex_palette_diverges_too():
    """The docs browser writes plain CSS, so it needs its own divergence check."""
    demo = ui_theme.theme_for("demo", "production")["hex"]
    pov = ui_theme.theme_for("pov", "production")["hex"]
    for key in ("nav_bg", "nav_warp", "nav_weft", "body_bg", "link"):
        assert demo[key] != pov[key], f"hex[{key!r}] is the same on both profiles"


def test_the_profile_survives_greyscale():
    """Colour fails for colour-vision deficiency and in a pasted screenshot. Three
    non-colour tokens must each name the profile on their own."""
    demo = ui_theme.theme_for("demo", "production")
    pov = ui_theme.theme_for("pov", "production")
    assert demo["product"] != pov["product"]
    assert demo["chip_label"] != pov["chip_label"]
    assert demo["title_prefix"] != pov["title_prefix"]
    assert "POV" in pov["title_prefix"], "the tab title must name POV before the brand"
    assert "POV" not in demo["title_prefix"]


def test_the_brand_is_the_constant_half():
    """One parent brand, two products — not two apps."""
    demo = ui_theme.theme_for("demo", "production")
    pov = ui_theme.theme_for("pov", "development")
    assert demo["brand"] == pov["brand"] == "Weaver Lab"
    assert demo["mark_warp_path"] == pov["mark_warp_path"]
    assert demo["mark_weft_path"] == pov["mark_weft_path"]


# ── the demo instance has not moved ──────────────────────────────────────────

def test_demo_production_still_emits_todays_classes():
    """Anti-regression. These are base.html's pre-brand strings, verbatim."""
    t = ui_theme.theme_for("demo", "production")
    assert t["nav_bg"] == "bg-blue-900"
    assert t["nav_hover"] == "hover:bg-blue-800"
    assert t["nav_active"] == "bg-blue-800"
    assert t["user_text"] == "text-blue-200"
    assert t["logout_btn"] == "bg-blue-700 hover:bg-blue-600"
    assert t["brand_hover"] == "hover:text-blue-200"
    assert t["body_bg"] == "bg-gray-50"


def test_demo_non_production_still_emits_todays_classes():
    t = ui_theme.theme_for("demo", "development")
    assert t["nav_bg"] == "bg-emerald-700"
    assert t["nav_hover"] == "hover:bg-emerald-600"
    assert t["nav_active"] == "bg-emerald-600"
    assert t["user_text"] == "text-emerald-200"
    assert t["logout_btn"] == "bg-emerald-600 hover:bg-emerald-500"
    assert t["body_bg"] == "bg-gray-50"


def test_the_community_signal_survived_the_rebrand():
    """The old app_env pill said "Community". Folding it into the profile chip must not
    drop it — that pill is what tells a public-release user which edition they are on."""
    assert "Community" in ui_theme.theme_for("demo", "development")["chip_label"]
    assert "Community" not in ui_theme.theme_for("demo", "production")["chip_label"]


# ── resolution rules ─────────────────────────────────────────────────────────

def test_pov_ignores_app_env():
    """A POV instance is violet whether it runs as production or development: what
    matters is whose tenant it reaches, not the deployment stage."""
    assert ui_theme.theme_for("pov", "production") == ui_theme.theme_for("pov", "development")


def test_an_unknown_profile_falls_back_to_demo():
    """Mirrors feature_flags.install_profile(). This runs on the request path, so a typo
    in one config row must render today's chrome, never a blank nav bar."""
    baseline = ui_theme.theme_for("demo", "production")
    for bogus in ("", None, "POV_", "both", "  "):
        assert ui_theme.theme_for(bogus, "production") == baseline, \
            f"{bogus!r} did not fall back to demo"


def test_the_profile_names_match_the_gate():
    """theme_for must not accept a profile the gate rejects, or vice versa."""
    from web_dashboard.services import feature_flags
    for p in feature_flags.VALID_PROFILES:
        assert ui_theme.theme_for(p, "production")["profile"] == p


def test_case_and_whitespace_resolve_like_the_gate():
    assert ui_theme.theme_for("  POV  ", "production")["profile"] == "pov"


# ── the favicon is a data URI, not a broken one ──────────────────────────────

def test_the_favicon_has_no_literal_hash():
    """A literal '#' starts a URI fragment: the browser truncates the SVG at the first
    stroke colour and the tab renders empty."""
    for p in ("demo", "pov"):
        fav = ui_theme.theme_for(p, "production")["favicon"]
        assert fav.startswith("data:image/svg+xml,")
        assert "#" not in fav, f"{p} favicon carries an unencoded hash"
        assert "%23" in fav, f"{p} favicon lost its colours entirely"


def test_every_hex_value_is_a_real_colour():
    """The docs shell interpolates these straight into CSS, where a typo fails silently."""
    for p in ("demo", "pov"):
        for key, val in ui_theme.theme_for(p, "production")["hex"].items():
            if not val:
                continue          # rail is deliberately empty on demo — no rail renders
            assert re.fullmatch(r"#[0-9a-f]{6}", val), f"{p} hex[{key!r}] = {val!r}"


def test_the_hex_and_tailwind_palettes_agree():
    """Two spellings of one palette drift. Spot-check the pairs that would show it."""
    pov = ui_theme.theme_for("pov", "production")
    assert pov["nav_bg"] == "bg-violet-900" and pov["hex"]["nav_bg"] == "#4c1d95"
    assert pov["body_bg"] == "bg-violet-50" and pov["hex"]["body_bg"] == "#f5f3ff"
    demo = ui_theme.theme_for("demo", "production")
    assert demo["nav_bg"] == "bg-blue-900" and demo["hex"]["nav_bg"] == "#1e3a8a"


def test_the_chip_stays_narrow_enough_not_to_fold_the_nav():
    """static/js/app.js:responsiveNav folds the ENTIRE nav into the flyout drawer the
    moment the row overflows, so the brand block's width is a functional constraint, not
    a cosmetic one. Measured at a 1280px viewport, the widest variant (demo/community)
    leaves 61px of headroom at these lengths; "Demo · Community" left 2px. Anything much
    longer here silently costs every user at that width their inline nav."""
    for p in ("demo", "pov"):
        for app_env in ("production", "development"):
            label = ui_theme.theme_for(p, app_env)["chip_label"]
            assert len(label) <= 18, f"{p}/{app_env} chip {label!r} is {len(label)} chars"


def test_the_chip_is_not_uppercased_in_the_nav():
    """uppercase + tracking-wide cost 17px of bar width for no legibility gain."""
    src = _read(os.path.join(_TPL, "base.html"))
    chip_line = [l for l in src.splitlines() if "theme.chip_label" in l][0]
    assert "uppercase" not in chip_line and "tracking-wide" not in chip_line, chip_line


def test_only_pov_gets_a_rail():
    assert ui_theme.theme_for("demo", "production")["rail"] == ""
    assert ui_theme.theme_for("demo", "development")["rail"] == ""
    assert ui_theme.theme_for("pov", "production")["rail"]


# ── the templates actually read the tokens ───────────────────────────────────

def test_base_html_no_longer_derives_colour_from_app_env():
    """Two sources for one palette is how the nav bar and its flyout drawer — which share
    nav_bg — end up different colours."""
    src = _read(os.path.join(_TPL, "base.html"))
    assert "app_env" not in src, \
        "base.html still branches on app_env; the palette must come from theme.* only"


def test_base_html_renders_the_lockup_and_the_favicon():
    src = _read(os.path.join(_TPL, "base.html"))
    for needed in ("theme.title_prefix", "theme.favicon", "theme.body_bg", "theme.nav_bg",
                   "theme.brand", "theme.product", "theme.chip_label", "theme.rail",
                   "_brand_mark.html"):
        assert needed in src, f"base.html lost {needed}"


def test_login_page_is_themed_rather_than_hardcoded_blue():
    """The login page is the first screen on either instance, and used to be identical."""
    src = _read(os.path.join(_TPL, "login.html"))
    assert not re.search(r"\b(bg|text|from|to|ring)-blue-\d", src), \
        "login.html still hardcodes a blue; a POV login must not look like the demo one"
    assert "theme.login_bg" in src and "theme.brand_full" in src


def test_the_mark_partial_is_shared_not_copied():
    """One definition of the logo. base.html and login.html both include it."""
    assert os.path.isfile(os.path.join(_TPL, "_brand_mark.html"))
    for page in ("base.html", "login.html"):
        assert "_brand_mark.html" in _read(os.path.join(_TPL, page)), \
            f"{page} does not include the shared mark"


def test_the_mark_paths_live_in_one_place():
    """The docs shell cannot use the Jinja partial, so it reads the same constants."""
    partial = _read(os.path.join(_TPL, "_brand_mark.html"))
    assert "theme.mark_warp_path" in partial and "theme.mark_weft_path" in partial
    docs = _read(_DOCS)
    assert 'theme["mark_warp_path"]' in docs and 'theme["mark_weft_path"]' in docs
    assert "M8.5 3v10.7" not in partial and "M8.5 3v10.7" not in docs, \
        "path data was copied out of ui_theme; the two marks will drift"


def test_page_titles_do_not_repeat_the_brand():
    """The title prefix supplies the brand, so a page carrying its own suffix reads
    'Weaver Lab - Dashboard - Infrastructure Management'."""
    offenders = []
    for root, _dirs, files in os.walk(_TPL):
        for name in files:
            if not name.endswith(".html") or name == "base.html":
                continue
            path = os.path.join(root, name)
            for m in re.finditer(r"\{% block title %\}(.*?)\{% endblock %\}", _read(path)):
                if "Infrastructure Management" in m.group(1):
                    offenders.append(os.path.relpath(path, _ROOT))
    assert not offenders, f"page titles still repeat the old brand: {offenders}"


# ── the injection point ──────────────────────────────────────────────────────

def test_the_theme_is_injected_by_a_context_processor():
    """Not env.globals: install_profile is DB-backed and the setup wizard writes it, so a
    global read at import time shows the old brand until a restart. Not _feature_flags()
    either: /users, /groups and /workgroups do not spread it."""
    src = _read(_MAIN)
    assert "context_processors=[_profile_context]" in src, \
        "the theme is no longer injected by a context processor"
    assert 'templates.env.globals["app_env"]' in src, \
        "app_env global was dropped; other call sites still read it"
    proc = src.split("def _profile_context", 1)[1].split("\ntemplates = ", 1)[0]
    assert "feature_flags.install_profile()" in proc, \
        "the processor must resolve through the same reader as the gate"


def test_the_pov_page_is_gated():
    """It used to render on a DEMO instance to anyone who typed the URL — a page whose
    whole premise is a registry of customer tenants a demo instance does not have."""
    src = _read(_MAIN)
    route = src.split('@app.get("/pov"', 1)[1].split("async def", 1)[0]
    assert '_feature_gate("pov_environments_enabled")' in route, "/pov has no feature gate"


def test_the_docs_shell_cannot_be_taken_down_by_the_database():
    """This module promises to degrade rather than take the dashboard down. The profile
    lookup reads config_service, so it must be best-effort."""
    src = _read(_DOCS)
    body = src.split("def _shell(", 1)[1].split("\n@router", 1)[0]
    assert "except Exception" in body and "settings.install_profile" in body, \
        "_shell() has no fallback; a dead database would 500 the docs browser"


def test_the_docs_shell_has_one_formatter():
    """Ten fields threaded through two call sites is how one ends up a version behind."""
    src = _read(_DOCS)
    assert len(re.findall(r"_SHELL\.format\(", src)) == 1, \
        "more than one place formats the docs shell"


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
