"""The wizard's Focus step. A preset is a starting point, and that has to be enforced.

The dangerous shape here is not a broken step, it is a working one that quietly does more
than it says. Four properties, each of which fails silently if it breaks:

  * **Only ever to true.** The profile mask rule inverted: a focus may suggest a feature,
    never remove one. Switching focus must not untick anything, and unticking must win.
  * **Never on reconfigure.** ``_apply_config`` writes every feature flag unconditionally
    ("explicit true/false is meaningful"), so re-ticking a preset during a reconfigure would
    PERSIST a flag the operator turned off months ago, while they were changing something
    unrelated.
  * **Absent means leave it alone.** ``persona: PersonaSetup | None = None``, not a default
    instance — an older client or a script that omits the block must not clear a chosen
    focus, exactly as it must not reset ``install_profile`` to demo.
  * **Every preset flag is a toggle the wizard renders.** Naming one it does not offer makes
    the preset a silent no-op.

And one divergence worth pinning because it looks like an inconsistency: an unknown persona
COERCES to neutral where an unknown profile RAISES. Getting install_profile wrong is a
cross-tenant hazard; a display preference is not, and failing a whole wizard save over one
is the wrong trade.

Runs under pytest, or standalone:
    python tests/test_persona_wizard.py
"""
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-persona-wizard")

_SETUP_PY = os.path.join(_ROOT, "web_dashboard", "api", "setup.py")
_SETUP_HTML = os.path.join(_ROOT, "web_dashboard", "templates", "setup.html")


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _wizard_toggles():
    """The feature keys the Features step actually renders."""
    block = _read(_SETUP_HTML).split("    featureFlags: [", 1)[1].split("\n    ],", 1)[0]
    return set(re.findall(r"key:\s*'([a-z_0-9]+)'", block))


# ── the step exists and is orthogonal to the profile ─────────────────────────

def test_the_focus_step_sits_after_purpose():
    """Purpose drives `get steps()`, so Focus has to come after it. allSteps is keyed by
    name rather than ordinal precisely so this is a one-line insert."""
    src = _read(_SETUP_HTML)
    block = src.split("allSteps: [", 1)[1].split("\n    ],", 1)[0]
    keys = re.findall(r"key:\s*'([a-z_]+)'", block)
    assert "persona" in keys, "the wizard has no Focus step"
    assert keys.index("persona") == keys.index("profile") + 1, \
        f"Focus is not immediately after Purpose: {keys}"


def test_the_focus_step_is_not_restricted_to_a_profile():
    """Every focus is valid on both install profiles. A `profiles:` entry here would be the
    first crack in the two axes being orthogonal."""
    src = _read(_SETUP_HTML)
    block = src.split("allSteps: [", 1)[1].split("\n    ],", 1)[0]
    line = [ln for ln in block.split("\n") if "'persona'" in ln][0]
    assert "profiles" not in line, f"the Focus step is profile-restricted: {line.strip()}"


def test_no_focus_is_an_option_and_it_is_first():
    """A profile is mandatory; a focus is not. "Show everything" has to be as easy to choose
    as it is to leave, or the step has made the plain dashboard harder to get."""
    src = _read(_SETUP_HTML)
    body = src.split("async loadPersonaOptions()", 1)[1].split("\n    },", 1)[0]
    assert "key: ''" in body, "the neutral option is missing from the picker"
    i_neutral = body.index("key: ''")
    i_concat = body.index(".concat(")
    assert i_neutral < i_concat, "the neutral option is not prepended"
    assert "form.persona.default_persona: ''" in src or \
           "persona:  { default_persona: '' }" in src, \
           "the form does not default to neutral"


def test_the_picker_reads_the_registry_rather_than_embedding_it():
    src = _read(_SETUP_HTML)
    assert "/api/persona/catalog" in src, \
        "the wizard does not fetch the persona catalog"
    from web_dashboard.services import personas as P
    for key in P.VALID_PERSONAS:
        k = re.escape(key)
        near = (r"""persona[^\n]{0,80}['"]%s['"]""" % k,
                r"""['"]%s['"][^\n]{0,80}persona""" % k)
        assert not any(re.search(pat, src, re.I) for pat in near), \
            f"setup.html hard-codes the persona key {key!r}"


def test_a_failed_catalog_fetch_still_leaves_a_usable_step():
    src = _read(_SETUP_HTML)
    body = src.split("async loadPersonaOptions()", 1)[1].split("\n    },", 1)[0]
    assert "catch" in body, "loadPersonaOptions has no catch"
    tail = body.split("catch", 1)[1]
    assert "key: ''" in tail, \
        "the catch branch leaves no options at all; it must still offer 'No focus'"


# ── the preset only ever adds, and never on reconfigure ──────────────────────

def _select_body():
    return _read(_SETUP_HTML).split("selectPersona(key) {", 1)[1].split("\n    },", 1)[0]


def test_the_preset_only_ever_sets_a_flag_true():
    body = _select_body()
    assert "= true" in body, "selectPersona never ticks anything"
    assert "= false" not in body, \
        "selectPersona sets a flag false — a focus may suggest a feature, never remove one"
    assert "delete " not in body


def test_the_preset_is_skipped_on_reconfigure():
    """The one that would corrupt real configuration. _apply_config writes every flag
    unconditionally, so re-ticking here persists a toggle the operator turned off."""
    body = _select_body()
    assert re.search(r"if\s*\(\s*this\.isReconfig\s*\)\s*return", body), (
        "selectPersona does not bail out on reconfigure — it would re-tick, and "
        "_apply_config would then PERSIST, a flag the operator had turned off")
    i_guard = body.index("isReconfig")
    i_set = body.index("= true")
    assert i_guard < i_set, "the reconfigure guard runs after the pre-tick"


def test_the_preset_only_touches_toggles_the_wizard_renders():
    body = _select_body()
    assert "featureFlags" in body, \
        "selectPersona does not restrict itself to the wizard's own toggle list"


def test_every_preset_flag_is_a_toggle_the_wizard_renders():
    """The data half of the above. A preset naming a flag the Features step does not offer
    is a silent no-op — which is why vdesktops_enabled, notifications_enabled and
    resource_expiry_enabled are deliberately NOT in any preset: the cards report those as
    `needs_flag` and point at Settings instead."""
    from web_dashboard.services import personas as P
    settable = _wizard_toggles()
    assert settable, "could not parse the wizard's feature toggles"
    offenders = []
    for p in P.all_personas():
        for flag in p.preset_flags:
            if flag not in settable:
                offenders.append(f"{p.key} -> {flag}")
    assert not offenders, (
        "presets name flags the wizard's Features step does not render, so selecting that "
        f"focus would silently do nothing for them: {offenders}")


def test_the_preset_is_shown_to_the_operator():
    """"Preset, not lock" has to be observable rather than asserted: the step names the
    toggles it ticked, and the Features step is still there to untick them on."""
    src = _read(_SETUP_HTML)
    assert "presetNames()" in src, "nothing tells the operator what was pre-ticked"
    panel = src.split("stepKey === 'persona'", 1)[1].split("<!-- ── AWS ── -->", 1)[0]
    assert "presetNames()" in panel, "the pre-ticked list is not shown on the Focus step"
    assert "!isReconfig" in panel, \
        "the pre-ticked panel is not hidden on reconfigure, where nothing is pre-ticked"


def test_the_focus_step_raises_no_alarm():
    """Purpose carries an amber warning because choosing wrong is a cross-tenant hazard.
    Nothing here is switched off, so borrowing that colour would misrepresent the stakes."""
    src = _read(_SETUP_HTML)
    panel = src.split("stepKey === 'persona'", 1)[1].split("<!-- ── AWS ── -->", 1)[0]
    assert "amber" not in panel, "the Focus step borrows Purpose's alarm colour"
    assert "change focus at any time" in panel or "change focus" in panel, \
        "the step does not say the choice is changeable later"


# ── absent means leave it alone ──────────────────────────────────────────────

def test_the_payload_field_is_optional_and_defaults_to_none():
    """Mirrors the install_profile reasoning: absent must mean "leave it alone", never
    "reset it". A default PersonaSetup() would clear a chosen focus on any reconfigure from
    a client that does not know about this field."""
    src = _read(_SETUP_PY)
    assert re.search(r"persona:\s*PersonaSetup\s*\|\s*None\s*=\s*None", src), (
        "SetupPayload.persona is not `PersonaSetup | None = None`")
    assert "persona: PersonaSetup = PersonaSetup()" not in src


def test_apply_config_writes_the_key_only_when_it_was_sent():
    src = _read(_SETUP_PY)
    body = src.split("def _apply_config", 1)[1].split("\ndef ", 1)[0]
    assert 'pairs["default_persona"] = payload.persona.default_persona' in body
    guard = body.split('pairs["default_persona"]', 1)[0]
    assert "if payload.persona is not None:" in guard.split("\n")[-2] + guard.split("\n")[-1], \
        "the default_persona write is not guarded on the block being present"


def test_apply_config_writes_no_feature_flag_on_behalf_of_the_persona():
    """The preset lives entirely in the browser. If the server also applied it, unticking a
    pre-ticked box would be overridden — which is the difference between a starting point
    and a lock."""
    src = _read(_SETUP_PY)
    body = src.split("def _apply_config", 1)[1].split("\ndef ", 1)[0]
    seg = body.split("default_persona", 1)[1].split("# Feature flags", 1)[0]
    assert "preset_flags" not in body, \
        "_apply_config applies preset_flags server-side; the preset must only pre-tick"
    assert "_enabled" not in seg


def test_the_config_row_is_named_default_persona():
    """Both axes live in app_config and both get called "profile" in speech. The row name is
    the only thing keeping them apart in a grep."""
    src = _read(_SETUP_PY)
    assert 'pairs["default_persona"]' in src
    assert 'pairs["persona"]' not in src and 'pairs["profile"]' not in src


# ── validation: coerce, do not raise ────────────────────────────────────────

def test_an_unknown_persona_coerces_rather_than_failing_the_save():
    from web_dashboard.api.setup import PersonaSetup
    for bad in ("nope", "DEMO", "  ", "<script>", "it", "pov"):
        assert PersonaSetup(default_persona=bad).default_persona == "", \
            f"{bad!r} did not coerce to neutral"


def test_a_known_persona_survives_and_is_normalised():
    from web_dashboard.api.setup import PersonaSetup
    from web_dashboard.services import personas as P
    for key in P.VALID_PERSONAS:
        assert PersonaSetup(default_persona=key).default_persona == key
    assert PersonaSetup(default_persona="  OT ").default_persona == "ot"


def test_the_default_is_neutral():
    from web_dashboard.api.setup import PersonaSetup
    assert PersonaSetup().default_persona == ""


def test_the_validator_imports_lazily_like_its_sibling():
    """Several test files stub web_dashboard.services in sys.modules; a module-scope sibling
    import turns those suites into a SKIP. ProfileSetup._known_profile records this."""
    src = _read(_SETUP_PY)
    body = src.split("def _known_persona", 1)[1].split("\nclass ", 1)[0]
    assert "from ..services import personas" in body, \
        "the persona validator does not import lazily inside the function"


def test_the_divergence_from_the_profile_validator_is_deliberate():
    """One raises, one coerces. That asymmetry is a decision and has to read as one."""
    src = _read(_SETUP_PY)
    body = src.split("def _known_persona", 1)[1].split("\nclass ", 1)[0]
    # Statements, not prose. The first draft of this test failed on the validator's own
    # comment, which says "_known_profile, which raises" while explaining the divergence —
    # a guard that cannot tell code from a comment about code is worse than none.
    stmts = [ln.strip() for ln in body.split("\n")
             if ln.strip() and not ln.strip().startswith("#")]
    assert not any(ln.startswith("raise ") for ln in stmts), \
        "the persona validator raises; it should coerce"
    assert any(ln.startswith("return ") for ln in stmts)
    profile = src.split("def _known_profile", 1)[1].split("\nclass ", 1)[0]
    assert "raise ValueError" in profile, \
        "the PROFILE validator no longer raises — this test's premise is stale"


# ── hydration ────────────────────────────────────────────────────────────────

def test_the_stored_focus_is_hydrated_after_the_profile():
    """The profile drives `get steps()`, so nothing may be sequenced ahead of it."""
    src = _read(_SETUP_HTML)
    i_profile = src.index("this.form.profile.install_profile = cfg.install_profile")
    i_persona = src.index("this.form.persona.default_persona = cfg.default_persona")
    assert i_profile < i_persona, "the focus is hydrated before the profile"


def test_the_focus_is_submitted():
    src = _read(_SETUP_HTML)
    body = src.split("const body = {", 1)[1].split("};", 1)[0]
    assert "persona:" in body, "the wizard never sends the focus"


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
