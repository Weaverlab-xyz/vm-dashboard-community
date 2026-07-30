"""The deploy forms' PRA pickers, and the one way they come apart silently.

All four VM deploy forms (AWS/Azure/GCP/OCI) fill their Jump group and Gateway
dropdowns from one endpoint, ``/api/pra/pickers``. Unlike the cloud-DB and k8s
modals — which assign the response wholesale (``this.options = await API.get(...)``,
so the payload's own keys land in state — these four **map** it field by field,
because the state also carries a ``loaded`` flag the fetch owns:

    praPickers: { configured: false, jump_groups: [], gateways: [], loaded: false }
    this.praPickers = { ..., gateways: r.jumpoints || [], loaded: true }

A hand-written mapping is a second place a key is spelled, and that is the whole
defect class this file guards. The Gateway dropdown shipped empty on all four forms
because the two spellings drifted: the markup kept reading ``praPickers.jumpoints``
while the fetch wrote ``gateways``. Reading an absent key is not an error in
JavaScript — ``x-for="j in praPickers.jumpoints || []"`` renders zero options and
``:disabled="!praPickers.jumpoints.length"`` throws where only the console sees it,
so the form looked exactly like a PRA install with no Gateways: one
"(configured default)" entry, no way to pick anything, and the deploy silently
falling back to ``bt_jumpoint_name``.

Nothing was going to catch it. The mismatch is invisible to Python, the Jinja render
succeeds, and it looks identical to the legitimate unconfigured-PRA state.

Worth knowing *how* the two spellings drifted, because the same edit will be
attempted again: the Jumpoint→Gateway prose rename (see
test_gateway_terminology.py) skips a word glued to identifier, path or dot
characters. ``.jumpoints`` is dot-preceded, so every **reader** was protected —
but ``jumpoints:`` as an object-literal key is preceded by a space, so every
**writer** was rewritten. The rule protected one half of each pair and not the
other. That is why the fix renamed the readers to ``gateways`` rather than
restoring ``jumpoints``: the same terminology test forbids a space-preceded
``jumpoints`` in a template, so the state key cannot go back.

Three things are pinned:

  * every ``praPickers.<key>`` the markup reads is a key the state actually has,
  * the mapping still reads the API's own ``jumpoints`` key — that half must *not*
    be renamed, and pra_api_service is checked so the two cannot drift,
  * the select still posts ``jumpoint_name``, the field the Pydantic deploy models
    accept.

Templates are read as text; no DB, no cloud SDK, no browser.

Run: python tests/test_pra_picker_bindings.py   (or under pytest)
"""
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TEMPLATES = os.path.join(_ROOT, "web_dashboard", "templates")

# `praPickers: {...}` (initial state) and `this.praPickers = {...}` (the fetch).
_INIT = re.compile(r"(?<!\.)praPickers:\s*\{([^}]*)\}")
_ASSIGN = re.compile(r"this\.praPickers\s*=\s*\{([^}]*)\}")
_KEY = re.compile(r"(\w+)\s*:")
_READ = re.compile(r"praPickers\.(\w+)")
# The Gateway dropdown: the select that posts the jumpoint_name field.
_GATEWAY_SELECT = re.compile(r'<select x-model="[^"]*\.jumpoint_name"(.*?)</select>', re.S)


def _read(path):
    return open(path, encoding="utf-8").read()


def _forms():
    """Every template with PRA picker dropdowns, discovered rather than listed — a
    fifth cloud page gets these guards for free."""
    found = []
    for dp, _, files in os.walk(_TEMPLATES):
        for f in sorted(files):
            if f.endswith(".html"):
                p = os.path.join(dp, f)
                src = _read(p)
                if "praPickers" in src:
                    found.append((os.path.relpath(p, _ROOT).replace("\\", "/"), src))
    return found


def test_the_four_deploy_forms_are_all_present():
    """If a page loses its pickers entirely the guards below would pass vacuously."""
    names = {p for p, _ in _forms()}
    for cloud in ("aws", "azure", "gcp", "oci"):
        expected = f"web_dashboard/templates/{cloud}/index.html"
        assert expected in names, f"{cloud} deploy form no longer has PRA pickers"


# ── the readers and the writer agree ──────────────────────────────────────────

def test_every_pra_picker_read_is_a_key_the_state_defines():
    """The guard that would have caught the empty Gateway dropdown. An absent key
    reads as undefined, so the dropdown renders empty instead of failing."""
    offenders = []
    for path, src in _forms():
        declared = set(_KEY.findall(_INIT.search(src).group(1)))
        for m in _READ.finditer(src):
            if m.group(1) not in declared:
                line = src.count("\n", 0, m.start()) + 1
                offenders.append(
                    f"{path}:{line} reads praPickers.{m.group(1)}, "
                    f"state has {sorted(declared)}")
    assert not offenders, (
        "a deploy form reads a PRA picker key its state never defines — the dropdown "
        "renders empty and the deploy falls back to the configured default:\n  "
        + "\n  ".join(offenders))


def test_the_fetch_fills_every_key_the_initial_state_declares():
    """The mirror case: the fetch replaces the whole object, so a key it forgets is
    a key that disappears the moment the pickers load."""
    for path, src in _forms():
        declared = set(_KEY.findall(_INIT.search(src).group(1)))
        assigned = set(_KEY.findall(_ASSIGN.search(src).group(1)))
        assert declared == assigned, (
            f"{path}: the pickers fetch writes {sorted(assigned)} over a state of "
            f"{sorted(declared)} — the difference is silently undefined")


def test_the_gateway_dropdown_renders_the_list_the_fetch_fills():
    """Tied to the select that posts jumpoint_name, so this cannot be satisfied by
    some other dropdown on the page happening to be wired correctly."""
    for path, src in _forms():
        assigned = set(_KEY.findall(_ASSIGN.search(src).group(1)))
        blocks = _GATEWAY_SELECT.findall(src)
        assert blocks, f"{path}: no <select> posts jumpoint_name any more"
        for block in blocks:
            keys = set(_READ.findall(block))
            assert keys, (
                f"{path}: the Gateway select renders no praPickers list — it is a "
                "text box or a hardcoded option list again")
            assert keys <= assigned, (
                f"{path}: the Gateway select reads {sorted(keys - assigned)}, which "
                f"the pickers fetch never writes")


# ── the half that must NOT be renamed ─────────────────────────────────────────

def test_the_mapping_still_reads_the_apis_own_jumpoints_key():
    """``r.jumpoints`` is the payload's key, not prose. Renaming it to match the
    state key reads a field the endpoint never sends — the same empty dropdown, from
    the other direction."""
    for path, src in _forms():
        rhs = _ASSIGN.search(src).group(1)
        assert "r.jumpoints" in rhs, (
            f"{path}: the pickers fetch no longer reads the payload's jumpoints key")


def test_the_pickers_endpoint_still_returns_jumpoints():
    """So the assertion above can't drift from what the endpoint actually sends."""
    src = _read(os.path.join(_ROOT, "web_dashboard", "services", "pra_api_service.py"))
    assert '"jumpoints":' in src, (
        "list_pickers() renamed its jumpoints key — every deploy form maps r.jumpoints "
        "and would now read undefined")


def test_the_select_still_posts_jumpoint_name():
    """The field name is the wire contract with the Pydantic deploy models; the label
    above it is what says Gateway."""
    for path, src in _forms():
        cloud = path.split("/")[-2]
        model = os.path.join(_ROOT, "web_dashboard", "models", f"{cloud}.py")
        assert "jumpoint_name" in _read(model), (
            f"models/{cloud}.py no longer accepts jumpoint_name, but the form posts it")
        assert ">Gateway</label>" in src, f"{path}: the dropdown's label stopped saying Gateway"


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
