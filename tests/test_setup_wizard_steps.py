"""The setup wizard's panels must be addressed by step NAME, never by ordinal.

The wizard's step list is data (`steps` in ``setup.html``) but its panels used to be
addressed by hardcoded number — ``x-show="step === 4"`` and friends — and the two were
hand-maintained copies. Inserting a step shifted every panel below it, and the symptom
was never an error:

* When the OCI step was added, ``submit()`` kept assigning the literal ``6``. That had
  been the Done panel; it was now **Feature Flags**. So a successful setup bounced back
  to the toggles screen, never said it worked, and never offered ``/login``. It shipped
  and survived, because nothing here asserted the step count or where submit lands.

So these tests assert the *shape*, not the content: any panel the wizard can show is
named in ``steps`` (or is the deliberately unlisted Done panel), the counter and the
submit button derive from ``steps.length``, and no ordinal comparison against a step
number is left anywhere. Add a step and this file needs no edit; add a step *and* an
ordinal, and it fails.

Pure: parses the template, no app import. Runs under pytest, or standalone:
    python tests/test_setup_wizard_steps.py
"""
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SETUP_HTML = os.path.join(_ROOT, "web_dashboard", "templates", "setup.html")

# The Done panel is intentionally NOT in `steps`: it has no progress dot and is not
# somewhere you navigate to, so it cannot be derived from the list.
_UNLISTED_PANELS = {"done"}


def _html():
    with open(_SETUP_HTML, encoding="utf-8") as fh:
        return fh.read()


def _step_entries(html):
    """The `allSteps` entries, in order, as (key, profiles-or-None) pairs.

    `profiles` limits a step to certain install profiles; absent means every profile.
    """
    block = re.search(r"\n\s*allSteps:\s*\[(.*?)\n\s*\],", html, re.S)
    assert block, "could not find setup.html's `allSteps: [...]` array — teach this test its shape"
    out = []
    for line in block.group(1).splitlines():
        m = re.search(r"key:\s*'([^']+)'", line)
        if not m:
            continue
        prof = re.search(r"profiles:\s*\[([^\]]*)\]", line)
        out.append((m.group(1),
                    re.findall(r"'([^']+)'", prof.group(1)) if prof else None))
    assert out, "parsed allSteps but found no `key:` entries"
    return out


def _step_keys(html, profile=None):
    """Step keys, optionally filtered to one install profile the way the getter does."""
    return [k for k, profs in _step_entries(html)
            if profile is None or profs is None or profile in profs]


def _profiles(html):
    """Every profile named anywhere in allSteps, plus the wizard's own option list."""
    named = {p for _, profs in _step_entries(html) if profs for p in profs}
    opts = set(re.findall(r"value:\s*'([^']+)'", html))
    return named | (opts & {"demo", "pov"})


def _shown_panel_keys(html):
    """Every step key some panel renders on, via x-show="stepKey === '...'"."""
    return set(re.findall(r"""x-show="stepKey === '([^']+)'""", html))


def test_the_steps_array_parses_and_is_ordered():
    keys = _step_keys(_html())
    assert keys[0] == "admin", f"the first step must be the admin account, got {keys[0]!r}"
    assert keys[-1] == "features", f"the last step must be the feature flags, got {keys[-1]!r}"
    assert len(keys) == len(set(keys)), f"duplicate step keys: {keys}"


def test_the_profile_step_comes_second():
    """It has to be asked before anything it filters, and after the admin account —
    which is the only step every profile needs and the only one that validates."""
    keys = _step_keys(_html())
    assert keys[1] == "profile", f"expected the profile step second, got {keys[1]!r}"


def test_every_profile_yields_a_usable_wizard():
    """The filtered list must still start at admin and end at the feature flags, or
    `isLastStep` lands somewhere that is not the submit step."""
    html = _html()
    profiles = _profiles(html)
    assert profiles, "no install profiles found — teach this test their shape"
    for profile in sorted(profiles):
        keys = _step_keys(html, profile)
        assert keys[0] == "admin", f"{profile}: first step is {keys[0]!r}"
        assert keys[1] == "profile", f"{profile}: profile step is not second"
        assert keys[-1] == "features", f"{profile}: last step is {keys[-1]!r}"


def test_the_pov_profile_skips_every_cloud_step():
    """A POV instance never asks for credentials it will not use."""
    keys = _step_keys(_html(), "pov")
    leaked = sorted({"aws", "azure", "gcp", "oci"} & set(keys))
    assert not leaked, f"the pov profile still shows cloud steps: {leaked}"


def test_the_demo_profile_keeps_every_cloud_step():
    """The default path must be unchanged."""
    keys = _step_keys(_html(), "demo")
    missing = sorted({"aws", "azure", "gcp", "oci"} - set(keys))
    assert not missing, f"the demo profile lost cloud steps: {missing}"


def test_every_panel_is_a_named_step():
    """A panel keyed to a name that is not in `steps` can never be reached."""
    html = _html()
    orphans = _shown_panel_keys(html) - set(_step_keys(html)) - _UNLISTED_PANELS
    assert not orphans, f"panels keyed to unknown steps: {sorted(orphans)}"


def test_every_named_step_has_a_panel():
    """A step in the list with no panel renders an empty screen with a Next button."""
    html = _html()
    missing = set(_step_keys(html)) - _shown_panel_keys(html)
    assert not missing, f"steps with no panel: {sorted(missing)}"


def test_no_panel_is_addressed_by_a_step_ordinal():
    """This is the assertion the OCI insertion needed.

    Only a comparison against an integer *literal* counts. `step === i + 1` in the
    progress-dot loop compares against the loop index, and `this.step ===
    this.steps.length` in `isLastStep` derives from the list — both are fine, and both
    stay correct when a step is inserted. A bare number does not.
    """
    ordinals = re.findall(r"step\s*===\s*(\d+)", _html())
    assert not ordinals, (
        f"step compared against literal(s) {ordinals} in setup.html — "
        "address panels by stepKey, and derive bounds from steps.length")


def test_submit_lands_on_the_done_panel():
    """Not on `steps.length`, and above all not on a literal."""
    html = _html()
    assign = re.search(r"this\.step\s*=\s*([^;]+);", html.split("async submit()")[-1])
    assert assign, "submit() no longer assigns this.step — check where it lands"
    expr = assign.group(1).strip()
    assert "steps.length" in expr, (
        f"submit() lands on {expr!r}; it must be derived from steps.length so that "
        "inserting a step cannot leave it pointing at a real panel")


def test_the_step_counter_and_submit_label_are_derived():
    html = _html()
    assert "'Step 1 of ' + steps.length" in html, \
        "the step counter is hardcoded — it will lie the moment a step is added"
    assert 'x-text="isLastStep ?' in html, \
        "the submit button's label is keyed to an ordinal rather than isLastStep"


def test_advancing_is_bounded_by_the_step_list():
    html = _html()
    assert "if (!this.isLastStep) { this.step++; return; }" in html, \
        "next() compares against a literal step number instead of isLastStep"


def test_the_done_panel_sends_a_reconfiguring_operator_back_to_settings():
    """On a reconfigure the operator is already signed in, so /login is wrong."""
    html = _html()
    assert """:href="isReconfig ? '/settings' : '/login'\"""" in html, \
        "the Done panel's link does not branch on isReconfig"


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
