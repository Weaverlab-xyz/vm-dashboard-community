"""Operator branding: a custom name, an accent, and an environment banner.

`services/ui_theme` was a constant table; it now takes five operator-supplied values read
from `app_config` by `services/branding`. That turns two things that used to be impossible
into things that have to be pinned:

  * A stored value now reaches the browser. The brand renders into `api/docs_pages._SHELL`,
    which is a `.format()` template on a PUBLIC, unauthenticated page with no autoescaping,
    and the banner colour renders into an inline `style` attribute. Both are pinned here.
  * The default output must not have moved. Every new parameter is optional, and an
    instance that never opens the Appearance panel must render byte-for-byte what it
    rendered before this feature existed — that property is what let
    tests/test_profile_theme keep its pinned demo strings unedited, so if it breaks, that
    suite's failure is the second symptom rather than the first.

Also pinned: an accent recolours a POV instance, but the signals that survive greyscale
(the `POV -` title prefix, the product word, the chip) do not move. That is the whole
premise on which recolouring POV is safe at all — see the note in ui_theme.theme_for.

Pure: no database. `branding.overrides()` is exercised with `config_service.get_raw`
monkeypatched, which is also the honest unit under test — everything else there is I/O.

Runs under pytest, or standalone:
    python tests/test_branding.py
"""
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-branding")

from web_dashboard.services import branding, config_service, ui_theme  # noqa: E402

_TPL = os.path.join(_ROOT, "web_dashboard", "templates")
_SETUP_API = os.path.join(_ROOT, "web_dashboard", "api", "setup.py")
_DOCS = os.path.join(_ROOT, "web_dashboard", "api", "docs_pages.py")
_MAIN = os.path.join(_ROOT, "web_dashboard", "main.py")

_PROFILES = (("demo", "production"), ("demo", "development"), ("pov", "production"))

# Every key theme_for() returned before branding existed. Re-derived from the shipped
# palettes rather than typed out, so this list cannot rot into a subset and silently stop
# checking the keys somebody removed.
_PRE_BRANDING_KEYS = set(ui_theme._DEMO_PROD) | {
    "profile", "brand", "brand_full", "mark_warp_path", "mark_weft_path",
    "title_prefix", "login_bg",
}


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class _Stub:
    """Stands in for config_service.get_raw over a fixed dict of rows."""

    def __init__(self, rows, boom=False):
        self.rows, self.boom = rows, boom

    def __call__(self, key, default="", workgroup=None):
        if self.boom:
            raise RuntimeError("database is down")
        return self.rows.get(key, default)


def _with_rows(rows, boom=False):
    original = config_service.get_raw
    config_service.get_raw = _Stub(rows, boom)
    try:
        return branding.overrides()
    finally:
        config_service.get_raw = original


# ── The default has not moved ────────────────────────────────────────────────


def test_no_overrides_leaves_every_pre_branding_key_untouched():
    """The guard that keeps tests/test_profile_theme's pinned strings honest.

    Not a snapshot comparison: the point is that adding parameters changed nothing, so the
    check has to be against the palette tables themselves rather than against a copy of
    the output taken after the change.
    """
    for profile, env in _PROFILES:
        theme = ui_theme.theme_for(profile, env)
        base = ui_theme._POV if profile == "pov" else (
            ui_theme._DEMO_PROD if env == "production" else ui_theme._DEMO_DEV
        )
        for key, value in base.items():
            assert theme[key] == value, f"{profile}/{env}: {key} moved to {theme[key]!r}"
        assert theme["brand"] == ui_theme.BRAND
        assert theme["brand_full"] == ui_theme.BRAND_FULL
        assert theme["login_bg"] == ui_theme._LOGIN_BG[profile]
        assert _PRE_BRANDING_KEYS <= set(theme), \
            f"{profile}/{env}: dropped {_PRE_BRANDING_KEYS - set(theme)}"


def test_no_env_label_means_no_banner():
    """An instance that never set one must render the rail it always did."""
    for profile, env in _PROFILES:
        assert ui_theme.theme_for(profile, env)["env_banner"] is None
    # Whitespace is not a label: a stray space must not draw an empty coloured strip.
    assert ui_theme.theme_for("demo", "production", env_label="   ")["env_banner"] is None


# ── Accents ──────────────────────────────────────────────────────────────────


def test_every_accent_is_a_complete_palette():
    """A missing key is an empty class attribute, i.e. an invisible nav bar."""
    required = set(ui_theme._DEMO_PROD) - {"product", "chip_label"} | {"login_bg"}
    for key, palette in ui_theme._ACCENTS.items():
        assert required <= set(palette), f"{key}: missing {required - set(palette)}"
        assert set(palette) - required <= set(ui_theme._ACCENT_META), \
            f"{key}: unexpected keys {set(palette) - required - set(ui_theme._ACCENT_META)}"
        assert set(palette["hex"]) == set(ui_theme._DEMO_PROD["hex"]), \
            f"{key}: hex mirror does not match the shipped shape"
        assert palette["favicon"].startswith("data:image/svg+xml,"), key
        assert re.fullmatch(r"#[0-9a-f]{6}", palette["swatch"]), key
        assert palette["label"], key


def test_accent_meta_never_reaches_the_theme():
    """`label` and `swatch` are for the picker. Rendered as classes they are garbage."""
    theme = ui_theme.theme_for("demo", "production", accent="teal")
    for meta in ui_theme._ACCENT_META:
        assert meta not in theme, f"{meta} leaked into the theme dict"


def test_accent_recolours_pov_but_not_its_greyscale_signals():
    """The premise that makes recolouring a POV instance safe."""
    theme = ui_theme.theme_for("pov", "production", accent="teal")
    assert theme["nav_bg"] == "bg-teal-900"
    assert theme["product"] == "POV"
    assert theme["chip_label"] == "Customer tenants"
    assert theme["title_prefix"].startswith("POV")


def test_an_unknown_accent_renders_stock_chrome():
    """A stale config row must not 500 every page. Mirrors the unknown-profile branch."""
    for bad in ("chartreuse", "", "   ", "bg-red-900"):
        theme = ui_theme.theme_for("demo", "production", accent=bad)
        assert theme["nav_bg"] == ui_theme._DEMO_PROD["nav_bg"], bad
        assert theme["accent"] == "", bad


def test_accent_names_are_case_and_space_insensitive():
    assert ui_theme.theme_for("demo", "production", accent="  TEAL ")["nav_bg"] == "bg-teal-900"


def test_accent_choices_covers_the_table():
    choices = ui_theme.accent_choices()
    assert [c["key"] for c in choices] == list(ui_theme._ACCENTS)
    assert all(c["swatch"] and c["label"] for c in choices)


# ── The banner ───────────────────────────────────────────────────────────────


def test_banner_text_stays_legible_on_any_background():
    """White on a pale pick would erase the label, which is the banner's entire job."""
    assert ui_theme._text_on("#ffffff") == "#0f172a"
    assert ui_theme._text_on("#fde047") == "#0f172a"   # amber-300, the classic failure
    assert ui_theme._text_on("#000000") == "#ffffff"
    assert ui_theme._text_on("#1e3a8a") == "#ffffff"
    # Unparseable falls back to white, matching the dark colours the picker defaults to.
    assert ui_theme._text_on("nonsense") == "#ffffff"
    assert ui_theme._text_on("") == "#ffffff"


def test_banner_falls_back_to_the_accent_colour():
    """A label on its own is enough; the operator should not have to pick twice."""
    banner = ui_theme.theme_for("demo", "production", accent="rose", env_label="DEV")["env_banner"]
    assert banner["bg"] == ui_theme._ACCENTS["rose"]["hex"]["nav_bg"]
    assert banner["label"] == "DEV"
    # ...and to the profile's own nav colour when no accent is set either.
    plain = ui_theme.theme_for("demo", "production", env_label="DEV")["env_banner"]
    assert plain["bg"] == ui_theme._DEMO_PROD["hex"]["nav_bg"]


def test_the_banner_replaces_the_rail_rather_than_stacking_with_it():
    """Two coloured strips under the bar read as a rendering fault, not as two facts."""
    base = _read(os.path.join(_TPL, "base.html"))
    assert "theme.env_banner" in base, "base.html never renders the banner"
    assert re.search(r"\{%\s*elif theme\.rail\s*%\}", base), \
        "the rail is not an elif of the banner; both can render at once"
    assert ui_theme.theme_for("pov", "production", accent="teal")["rail"] == "", \
        "an accented instance keeps a rail that the banner will stack on top of"


def test_both_templates_render_the_banner():
    """login.html owns its own head and inherits nothing from base.html."""
    for name in ("base.html", "login.html"):
        markup = _read(os.path.join(_TPL, name))
        assert "theme.env_banner.label" in markup, f"{name} does not render the label"
        assert "theme.env_banner.fg" in markup, \
            f"{name} hardcodes a text colour instead of the computed one"


# ── Reading stored values ────────────────────────────────────────────────────


def test_unset_rows_produce_no_overrides():
    """The splat at the call site must be a no-op on a fresh instance."""
    assert _with_rows({}) == {}
    assert _with_rows({k: "" for k in (
        "brand_name", "brand_full", "brand_accent",
        "brand_env_label", "brand_env_color")}) == {}


def test_a_brand_alone_renames_the_login_subtitle_too():
    """Otherwise the card reads 'Weaver Lab Applications' under a renamed wordmark."""
    out = _with_rows({"brand_name": "Contoso"})
    assert out["brand"] == "Contoso"
    assert out["brand_full"] == "Contoso Applications"
    # An explicit full name still wins.
    assert _with_rows({"brand_name": "Contoso", "brand_full": "Contoso Cloud"})["brand_full"] \
        == "Contoso Cloud"


def test_stored_text_is_cleaned_and_capped():
    """app_config is reachable from config_migrate and from psql, not just the panel."""
    out = _with_rows({"brand_name": "  Contoso‮Evil  "})
    assert out["brand"] == "ContosoEvil", "a bidi override survived into the wordmark"
    assert "\n" not in _with_rows({"brand_name": "Con\ntoso"})["brand"]
    assert len(_with_rows({"brand_name": "C" * 200})["brand"]) == branding._MAX_BRAND
    assert len(_with_rows({"brand_env_label": "E" * 200})["env_label"]) == branding._MAX_ENV_LABEL


def test_a_bad_colour_is_dropped_not_rendered():
    """This value lands inside a style attribute; anything looser is CSS injection."""
    for bad in ("red", "red; background-image: url(//x)", "#ff", "#gggggg",
                "javascript:alert(1)", "#ff0000; x: y"):
        out = _with_rows({"brand_env_label": "DEV", "brand_env_color": bad})
        assert "env_color" not in out, f"{bad!r} was accepted"
    good = _with_rows({"brand_env_label": "DEV", "brand_env_color": "#B91C1C"})
    assert good["env_color"] == "#b91c1c"


def test_a_colour_without_a_label_is_ignored():
    """Nothing renders the colour on its own, so carrying it would be a dead override."""
    assert _with_rows({"brand_env_color": "#b91c1c"}) == {}


def test_an_unknown_stored_accent_is_dropped():
    assert "accent" not in _with_rows({"brand_accent": "chartreuse"})
    assert _with_rows({"brand_accent": "TEAL"})["accent"] == "teal"


def test_a_dead_database_costs_the_wordmark_not_the_app():
    """This runs in the context processor, i.e. on every render of every page."""
    assert _with_rows({}, boom=True) == {}


# ── The public docs shell ────────────────────────────────────────────────────


def test_the_docs_shell_escapes_the_operator_supplied_brand():
    """/docs is public, and _SHELL is .format() into raw HTML with no autoescaping.

    Safe while the brand was a constant in ui_theme; stored XSS the moment an admin can
    set it in Settings. The caps in services/branding are defence in depth, not the fix.
    """
    from web_dashboard.api import docs_pages

    original = config_service.get_raw
    config_service.get_raw = _Stub({"brand_name": "<script>alert(1)</script>"})
    try:
        html = docs_pages._shell("Title", "<p>body</p>")
    finally:
        config_service.get_raw = original

    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html, "the brand was not escaped into the shell"


def test_the_docs_shell_still_reads_operator_branding():
    """A rebranded instance whose public docs page still says Weaver Lab is a missed spot."""
    src = _read(_DOCS)
    assert "branding.overrides()" in src, "_shell() ignores the operator's branding"


# ── Wiring ───────────────────────────────────────────────────────────────────


def test_the_context_processor_supplies_branding_to_every_page():
    """One hook covers every render including /login, which extends nothing."""
    src = _read(_MAIN)
    assert re.search(
        r"theme_for\(\s*profile,\s*settings\.app_env,\s*\*\*branding\.overrides\(\)\s*\)", src
    ), "main._profile_context does not pass branding into theme_for"
    assert "templates.env.globals[\"theme\"]" not in src, \
        "theme moved to env.globals, which is read once at import and cannot see a DB write"


def test_the_settings_panel_declares_every_field_it_binds():
    """A bound-but-undeclared field is dropped from the PATCH body without a word."""
    markup = _read(os.path.join(_TPL, "settings.html"))
    bound = set(re.findall(r"x-model=\"branding\.(\w+)\"", markup))
    assert bound, "the Appearance card binds nothing"
    declared = set(re.findall(r"(brand_\w+):\s*''", markup))
    assert bound <= declared, f"bound but not declared: {bound - declared}"


def test_the_panel_and_the_api_agree_on_the_field_list():
    """The card writing a key the endpoint does not persist would save into nothing."""
    markup = _read(os.path.join(_TPL, "settings.html"))
    api = _read(_SETUP_API)
    bound = set(re.findall(r"x-model=\"branding\.(\w+)\"", markup))
    persisted = set(re.findall(r"\"(brand_\w+)\",", api))
    assert bound <= persisted, f"the API does not persist: {bound - persisted}"


def test_branding_keys_match_the_reader():
    """api/setup writes these rows; services/branding is the only thing that reads them."""
    api = _read(_SETUP_API)
    reader = _read(os.path.join(_ROOT, "web_dashboard", "services", "branding.py"))
    written = set(re.findall(r"\"(brand_\w+)\",", api))
    for key in written:
        assert f'"{key}"' in reader, f"{key} is written by the API and read by nobody"


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
