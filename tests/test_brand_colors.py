"""Three operator-chosen colours, resolved to CSS rather than to Tailwind.

`tests/test_branding` covers the five original branding values. This covers what became
possible when the accent stopped having to be one of eight preset names.

The mechanism is the thing worth pinning, because its failure modes are all silent:

  * A chrome slot now holds a HAND-WRITTEN class name (`brand-nav`), defined in
    templates/_brand_css.html, not a Tailwind utility. The two halves are joined by nothing
    but a matching string, so a rename on one side leaves a class that no rule matches --
    which renders as a transparent nav bar and raises nothing, anywhere. Both directions
    are asserted here, as is the `.brand-custom` scope class that every rule depends on.
  * `brand_css` is interpolated INSIDE a <style> element, where Jinja's autoescaping is
    worthless: CSS does not decode HTML entities. So the guard cannot be escaping, and is
    instead refusal -- a hex that fails the check turns custom mode off rather than
    rendering. Pinned with the payloads that motivated it.
  * The settings preview recomputes this arithmetic in JavaScript, because the chrome is
    baked in server-side and there is nothing else to preview from. The constants are
    asserted present on both sides; the numeric agreement is checked by the node harness.
  * The default output must not have moved. Every new parameter is optional, and an
    instance that never opens the Appearance panel must render byte-for-byte what it did
    before -- the same property tests/test_branding leans on.

Pure: no database, no HTTP. `branding.overrides()` is exercised with
`config_service.get_raw` monkeypatched, matching tests/test_branding's `_Stub`.

Runs under pytest, or standalone:
    python tests/test_brand_colors.py
"""
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-brand-colors")

from web_dashboard.services import branding, config_service, ui_theme  # noqa: E402

_TPL = os.path.join(_ROOT, "web_dashboard", "templates")
_CSS = os.path.join(_TPL, "_brand_css.html")
_SETUP_API = os.path.join(_ROOT, "web_dashboard", "api", "setup.py")
_THEME = os.path.join(_ROOT, "web_dashboard", "services", "ui_theme.py")

_PROFILES = (("demo", "production"), ("demo", "development"), ("pov", "production"))

# BeyondTrust, the palette this feature was built for and the one the settings card offers
# as a preset. Blue reads as primary, navy as the nav surface, orange as the accent.
P, S, A = "#1903a6", "#030973", "#ff5400"


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class _Stub:
    """Stands in for config_service.get_raw over a fixed dict of rows."""

    def __init__(self, rows):
        self.rows = rows

    def __call__(self, key, default="", workgroup=None):
        return self.rows.get(key, default)


def _with_rows(rows):
    original = config_service.get_raw
    config_service.get_raw = _Stub(rows)
    try:
        return branding.overrides()
    finally:
        config_service.get_raw = original


def _custom(**kw):
    return ui_theme.theme_for("demo", "production", primary=P, secondary=S, accent_hex=A, **kw)


def _slot_values(theme):
    """Every class string the theme hands to a template, tokenised."""
    out = set()
    for key, value in theme.items():
        if key in ("hex", "favicon", "brand_css", "brand_scope") or not isinstance(value, str):
            continue
        out.update(value.split())
    return out


# ── The default has not moved ────────────────────────────────────────────────


def test_no_custom_colours_leaves_every_key_untouched():
    """The same guarantee tests/test_branding makes for the original five parameters."""
    for profile, env in _PROFILES:
        theme = ui_theme.theme_for(profile, env)
        base = ui_theme._POV if profile == "pov" else (
            ui_theme._DEMO_PROD if env == "production" else ui_theme._DEMO_DEV
        )
        for key, value in base.items():
            assert theme[key] == value, f"{profile}/{env}: {key} moved to {theme[key]!r}"
        assert theme["brand_css"] == "", f"{profile}/{env} emitted CSS with no colours set"
        assert theme["brand_scope"] == "", f"{profile}/{env} scoped itself with no colours"


def test_the_partial_renders_nothing_without_colours():
    """An un-branded instance must not gain even an empty <style> block."""
    src = _read(_CSS)
    assert "{% if theme.brand_css %}" in src, \
        "the partial is not gated, so it emits markup on every un-branded page"


# ── All three or nothing ─────────────────────────────────────────────────────


def test_a_partial_trio_is_inert():
    """One or two colours cannot fill twenty-one slots without inventing the rest."""
    for kw in ({"primary": P}, {"secondary": S}, {"primary": P, "secondary": S},
               {"primary": P, "accent_hex": A}):
        theme = ui_theme.theme_for("demo", "production", **kw)
        assert theme["nav_bg"] == ui_theme._DEMO_PROD["nav_bg"], kw
        assert theme["brand_css"] == "", kw
        assert theme["brand_scope"] == "", kw


def test_the_reader_drops_a_partial_trio():
    """config_migrate, a restored dump and psql can all leave half a palette behind."""
    assert _with_rows({"brand_primary": P}) == {}
    assert _with_rows({"brand_primary": P, "brand_secondary": S}) == {}
    out = _with_rows({"brand_primary": P, "brand_secondary": S, "brand_accent_hex": A})
    assert out == {"primary": P, "secondary": S, "accent_hex": A}


def test_the_api_refuses_a_partial_trio():
    """A setting that saves and then does nothing is worse than a 422."""
    src = _read(_SETUP_API)
    assert "_all_three_brand_colours" in src, \
        "nothing stops the panel persisting one colour of three"
    for key in ("brand_primary", "brand_secondary", "brand_accent_hex"):
        assert f'"{key}",' in src, f"{key} is not in _BRANDING_KEYS, so it never persists"


# ── Custom outranks a preset ─────────────────────────────────────────────────


def test_custom_wins_over_a_stored_preset():
    theme = _custom(accent="teal")
    assert theme["nav_bg"] == "brand-nav"
    assert theme["accent"] == "", \
        "a stored preset still reads as selected, so the picker lies about what renders"


def test_custom_fills_every_slot_a_preset_fills():
    """A missing key is an empty class attribute, i.e. an invisible nav bar.

    Same `required` set tests/test_branding uses for the eight presets, so the two cannot
    drift apart.
    """
    required = set(ui_theme._DEMO_PROD) - {"product", "chip_label"} | {"login_bg"}
    palette = ui_theme._custom_palette(P, S, A)
    assert required <= set(palette), f"missing {required - set(palette)}"
    for key in required - {"rail", "hex", "favicon"}:
        assert palette[key], f"{key} is empty"


def test_the_rail_is_cleared_like_every_preset_clears_it():
    """The rail slot is where the environment banner renders; two strips is noise."""
    assert _custom()["rail"] == ""


def test_no_custom_slot_is_a_tailwind_utility():
    """The whole point: these are hand-written classes, and a stray utility would be
    purged by a future build step while looking fine on the CDN today."""
    for token in _slot_values(ui_theme._custom_palette(P, S, A)):
        assert token.startswith("brand-"), f"{token!r} is not a brand class"
        for prefix in ("bg-", "text-", "border-", "hover:", "focus:", "ring-"):
            assert not token.startswith(prefix), f"{token!r} looks like a Tailwind utility"


def test_the_hex_mirror_keeps_its_shape_and_carries_the_colours():
    """The only channel to the consumers that do not speak Tailwind."""
    theme = _custom()
    assert set(theme["hex"]) == set(ui_theme._DEMO_PROD["hex"])
    assert theme["hex"]["nav_bg"] == S
    assert theme["hex"]["nav_weft"] == A
    assert theme["hex"]["link"] == ui_theme._ink(P)
    assert theme["hex"]["rail"] == ""


def test_the_favicon_uses_the_custom_colours():
    favicon = _custom()["favicon"]
    assert favicon.startswith("data:image/svg+xml,")
    for colour in (S, A):
        assert colour.replace("#", "%23") in favicon, f"{colour} is not in the favicon"


# ── The class/variable contract with the partial ─────────────────────────────


def test_every_class_the_theme_emits_is_defined_in_the_partial():
    """A renamed slot leaves a class no rule matches: a transparent nav bar, no error."""
    defined = set(re.findall(r"\.(brand-[\w-]+)", _read(_CSS)))
    emitted = _slot_values(ui_theme._custom_palette(P, S, A))
    assert emitted <= defined, f"emitted but never styled: {sorted(emitted - defined)}"


def test_every_class_the_partial_defines_is_actually_emitted():
    """The reverse direction, so the stylesheet cannot accumulate dead rules."""
    defined = set(re.findall(r"\.(brand-[\w-]+)", _read(_CSS)))
    emitted = _slot_values(ui_theme._custom_palette(P, S, A))
    dead = defined - emitted - {ui_theme._BRAND_SCOPE}
    assert not dead, f"styled but never emitted: {sorted(dead)}"


def test_every_variable_the_partial_references_is_emitted():
    """A typo'd custom property is one invisible element and nothing in any log."""
    referenced = set(re.findall(r"var\((--brand-[\w-]+)\)", _read(_CSS)))
    declared = {
        d.split(":")[0] for d in _custom()["brand_css"].split(";") if d
    }
    assert referenced <= declared, f"referenced but not emitted: {sorted(referenced - declared)}"


def test_every_variable_emitted_is_referenced():
    referenced = set(re.findall(r"var\((--brand-[\w-]+)\)", _read(_CSS)))
    declared = {d.split(":")[0] for d in _custom()["brand_css"].split(";") if d}
    assert declared <= referenced, f"emitted but unused: {sorted(declared - referenced)}"


def test_the_scope_class_matches_in_every_place_it_has_to():
    """Every rule is `.brand-custom .brand-x`. If the scope stops being applied, all of
    them stop matching at once -- and the page looks like the feature was never built."""
    scope = ui_theme._BRAND_SCOPE
    assert scope == "brand-custom"
    assert _custom()["brand_scope"] == scope
    css = _read(_CSS)
    assert f".{scope} " in css, "the partial's rules are not scoped"
    # Unscoped rules would tie with Tailwind utilities on specificity and be decided by
    # script timing, which is the failure this scope exists to make impossible.
    for rule in re.findall(r"^\s*(\.[^\s{][^{]*)\{", css, re.M):
        if rule.strip().startswith(":"):
            continue
        assert rule.strip().startswith(f".{scope}"), f"unscoped rule: {rule.strip()!r}"
    for name in ("base.html", "login.html"):
        markup = _read(os.path.join(_TPL, name))
        assert "theme.brand_scope" in markup, f"{name} never applies the scope class"
        assert "_brand_css.html" in markup, f"{name} does not include the brand CSS"


def test_the_scope_is_on_the_html_element():
    """It has to be an ANCESTOR of body, or `.brand-custom .brand-body` never matches."""
    for name in ("base.html", "login.html"):
        markup = _read(os.path.join(_TPL, name))
        html_tag = re.search(r"<html[^>]*>", markup).group(0)
        assert "theme.brand_scope" in html_tag, \
            f"{name} puts the scope somewhere other than <html>: {html_tag!r}"


# ── Injection ────────────────────────────────────────────────────────────────


def test_brand_css_cannot_break_out_of_the_style_block():
    """This value lands inside <style>, where escaping buys nothing -- CSS does not decode
    entities. So the control is refusal, not escaping: a bad hex turns the feature off."""
    payloads = (
        "#fff}</style><script>alert(1)</script>",
        "red; background-image: url(//x)",
        "#ff0000; x: y",
        "var(--x)",
        "#ff5400 }",
        "expression(alert(1))",
        "#gggggg",
        "#ff",
        "",
        "  ",
    )
    for bad in payloads:
        for kw in ("primary", "secondary", "accent_hex"):
            theme = ui_theme.theme_for(
                "demo", "production",
                **{"primary": P, "secondary": S, "accent_hex": A, kw: bad}
            )
            assert theme["brand_css"] == "", f"{bad!r} in {kw} was accepted"
            assert theme["nav_bg"] == ui_theme._DEMO_PROD["nav_bg"], f"{bad!r} in {kw}"


def test_everything_emitted_is_a_hex_colour():
    """The property the injection argument rests on: nothing but #rrggbb reaches the CSS."""
    for declaration in _custom()["brand_css"].split(";"):
        if not declaration:
            continue
        name, _, value = declaration.partition(":")
        assert name.startswith("--brand-"), declaration
        assert re.fullmatch(r"#[0-9a-f]{6}", value), declaration


def test_the_reader_drops_a_bad_stored_colour():
    """app_config is reachable from config_migrate and from psql, not just the panel."""
    for bad in ("red", "red; background-image: url(//x)", "#ff", "#gggggg",
                "javascript:alert(1)", "#ff0000; x: y"):
        out = _with_rows({"brand_primary": bad, "brand_secondary": S, "brand_accent_hex": A})
        assert out == {}, f"{bad!r} was accepted"


# ── The colour maths ─────────────────────────────────────────────────────────


def test_hover_and_active_differ_visibly_from_their_surface():
    """A fixed lighten is invisible on a pale brand; a fixed darken is invisible on a dark
    one. _step moves toward the surface's own foreground, so it works for both ends."""
    for surface in ("#ffffff", "#000000", "#030973", "#ff5400", "#808080"):
        for amount in (0.10, 0.12):
            stepped = ui_theme._step(surface, amount)
            assert stepped != surface, f"{surface} at {amount} did not move"
            distance = sum(
                abs(a - b) for a, b in zip(ui_theme._rgb(surface), ui_theme._rgb(stepped))
            )
            assert distance >= 8, f"{surface} at {amount} moved only {distance}"


def test_ink_is_readable_on_white():
    """A colour chosen to work as a filled surface is usually too light to read as text.
    BT Orange is 3.0:1 on white, which fails AA outright."""
    for colour in ("#ff5400", "#ffff00", "#ffffff", "#1903a6", "#030973", "#00ff00"):
        ink = ui_theme._ink(colour)
        ratio = ui_theme._ratio(ink, "#ffffff")
        assert ratio >= ui_theme._AA_CONTRAST, f"{colour} -> {ink} is only {ratio:.2f}:1"


def test_ink_leaves_an_already_dark_colour_alone():
    """Darkening a colour that already passes would discard brand fidelity for nothing."""
    for colour in ("#1903a6", "#030973"):
        assert ui_theme._ink(colour) == colour


def test_a_computed_foreground_is_always_the_more_legible_one():
    for surface in ("#ffffff", "#000000", "#030973", "#ff5400", "#fde047", "#808080"):
        chosen = ui_theme._text_on(surface)
        other = "#0f172a" if chosen == "#ffffff" else "#ffffff"
        assert ui_theme._ratio(chosen, surface) >= ui_theme._ratio(other, surface), surface


def test_mix_rounds_halves_up_so_the_javascript_preview_can_agree():
    """Python's round() goes to even and Math.round goes up. Mixing #1903a6 toward white
    at 0.85 lands a channel on exactly 220.5, which is where the two used to disagree."""
    assert ui_theme._mix("#1903a6", "#ffffff", 0.85) == "#ddd9f2"
    assert ui_theme._mix("#000000", "#ffffff", 0.5) == "#808080"


def test_mix_endpoints_are_exact():
    assert ui_theme._mix(P, "#ffffff", 0.0) == P
    assert ui_theme._mix(P, "#ffffff", 1.0) == "#ffffff"


def test_every_palette_agrees_with_text_on_about_its_nav_foreground():
    """nav_fg was added to all eleven hex dicts as a literal. It is only inert because
    every shipped nav colour is dark; this is the assertion that keeps that true."""
    palettes = [("demo_prod", ui_theme._DEMO_PROD), ("demo_dev", ui_theme._DEMO_DEV),
                ("pov", ui_theme._POV)] + list(ui_theme._ACCENTS.items())
    assert len(palettes) == 11
    for name, palette in palettes:
        expected = ui_theme._text_on(palette["hex"]["nav_bg"])
        assert palette["hex"]["nav_fg"] == expected, \
            f"{name}: nav_fg is {palette['hex']['nav_fg']} but should be {expected}"


def test_the_docs_shell_takes_its_header_colour_from_the_theme():
    """The public docs page hardcoded color:#fff, which is wrong for a pale brand -- and
    it is the one surface with no Tailwind and no scope class to fix it."""
    src = _read(os.path.join(_ROOT, "web_dashboard", "api", "docs_pages.py"))
    assert "color:{nav_fg}" in src, "the docs header still hardcodes its text colour"
    assert "color:#fff;" not in src


# ── The settings preview ─────────────────────────────────────────────────────


def test_the_panel_declares_and_binds_the_three_new_fields():
    """A field bound with x-model but missing from the state object is dropped from the
    PATCH silently -- the setting saves, reports success, and changes nothing."""
    markup = _read(os.path.join(_TPL, "settings.html"))
    bound = set(re.findall(r'x-model="branding\.(\w+)"', markup))
    declared = set(re.findall(r"(brand_\w+):\s*''", markup))
    for key in ("brand_primary", "brand_secondary", "brand_accent_hex"):
        assert key in bound, f"{key} is not bound to an input"
        assert key in declared, f"{key} is bound but not declared"


def test_the_three_colour_wells_are_not_two_way_bound():
    """input[type=color] coerces anything it cannot parse to #000000, so x-model on one of
    these would rewrite a half-typed hex to black on every keystroke of the text box
    beside it.

    Scoped to the three brand colours on purpose. The banner's own colour well IS x-model
    bound and should stay that way: it has no companion text input, so there is no
    half-typed state for the coercion to destroy.
    """
    markup = _read(os.path.join(_TPL, "settings.html"))
    wells = re.findall(r'<input type="color"[^>]*>', markup, re.S)
    checked = 0
    for block in wells:
        for key in ("brand_primary", "brand_secondary", "brand_accent_hex"):
            if key in block:
                checked += 1
                assert "x-model" not in block, f"{key}'s well is two-way bound"
                assert ":value" in block and "@input" in block, \
                    f"{key}'s well does not read and write explicitly"
    assert checked == 3, f"expected three brand colour wells, found {checked}"


def test_the_preview_mirrors_the_server_maths():
    """The chrome is rendered server-side, so the only way to preview a colour before the
    reload is to recompute it in the browser. The numeric agreement is exercised by the
    node harness; this pins that the helpers and their constants exist at all."""
    markup = _read(os.path.join(_TPL, "settings.html"))
    for helper in ("mixHex", "stepHex", "inkOn", "contrastRatio", "lumin"):
        assert helper + "(" in markup, f"{helper} is missing from the preview"
    assert "0.179" in markup, "the luminance split does not match ui_theme._text_on"
    assert "4.5" in markup, "the contrast target does not match ui_theme._AA_CONTRAST"
    assert str(ui_theme._AA_CONTRAST) == "4.5"


def test_the_panel_offers_the_beyondtrust_palette():
    """The instance this was built for. One click beats three pasted hexes."""
    markup = _read(os.path.join(_TPL, "settings.html"))
    assert "brandPresets" in markup
    for colour in (P, S, A):
        assert colour in markup, f"{colour} is not offered as a preset"


def test_the_checklist_constants_were_left_alone():
    """_checklist_styles.html's orange is a deliberate constant, and its header says it is
    only safe while it stays one. It is one digit off the brand orange; that is not a bug
    to fix here."""
    src = _read(os.path.join(_TPL, "_checklist_styles.html"))
    assert "--bt-orange: #FF5500" in src, \
        "the checklist palette was changed; see its header comment before doing that"


def test_ui_theme_never_says_persona():
    """tests/test_personas asserts it, and a comment saying 'not per-persona' trips it."""
    assert "persona" not in _read(_THEME).lower()


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
