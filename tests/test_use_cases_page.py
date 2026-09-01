"""The use-case catalog. Ungated, complete under neutral, and never a link to a 404.

Three properties, each of which would be invisible if it broke:

  * **The page carries no gate.** A persona surfaces things; the moment it can 404 a page it
    has become a second install_profile. Adding a `_feature_gate` here would look like an
    improvement -- it matches every other page in main.py -- so the absence is pinned.
  * **Neutral shows everything.** This page is where an SE who has not picked a role goes to
    see what there is. If choosing no focus showed no cards, the neutral persona would have
    subtracted the catalog.
  * **A `masked` card emits no href and no Settings link.** `needs_flag` is actionable, so it
    points at Settings. `masked` is not: api/setup.patch_feature_config refuses that enable
    with a 409 naming the profile, so a Settings link would send the operator to a switch
    that cannot move -- and a target would be a live link to a page this profile 404s.
  * **On a POV instance it LEADS with a POV, and still subtracts nothing.** The catalog
    asks feature_flags, which is an instance-wide answer, so on a POV instance most of it
    was greyed out -- correct, and useless in front of a customer. The POV lead goes on
    top; the catalog is COLLAPSED underneath, never filtered, and a demo instance renders
    exactly what it did before. The distance between "collapsed" and "removed" is the whole
    argument, so both halves are pinned.

Runs under pytest, or standalone:
    python tests/test_use_cases_page.py
"""
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-use-cases")

_TPL = os.path.join(_ROOT, "web_dashboard", "templates")
_PAGE = os.path.join(_TPL, "use_cases.html")
_DASHBOARD = os.path.join(_TPL, "dashboard.html")
_NAV = os.path.join(_TPL, "_nav_links.html")
_MAIN = os.path.join(_ROOT, "web_dashboard", "main.py")


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _route_head(path):
    """The decorator text of the ``@app.get(path, ...)`` route, or None."""
    src = _read(_MAIN)
    for chunk in src.split("@app.get(")[1:]:
        if chunk.split('"')[1] != path:
            continue
        return chunk.partition("async def")[0]
    return None


# ── the page is ungated ──────────────────────────────────────────────────────

def test_the_route_exists_and_serves_html():
    head = _route_head("/use-cases")
    assert head is not None, "/use-cases is not a route in main.py"
    assert "HTMLResponse" in head


def test_the_route_carries_no_gate():
    """Source-parsed rather than behavioural, because the mistake is a one-line addition
    that would look entirely idiomatic next to every neighbouring route."""
    head = _route_head("/use-cases")
    assert "dependencies=" not in head, (
        "/use-cases has a dependency. It must not: a persona may surface, never gate, and "
        "every card on this page already carries its own state.")
    for gate in ("_feature_gate", "_profile_page_gate"):
        assert gate not in head, f"/use-cases is gated with {gate}"


def test_the_absence_of_a_gate_is_explained_in_the_source():
    """A missing gate reads as an oversight. Without the comment the next reader adds one."""
    src = _read(_MAIN)
    before = src.split('@app.get("/use-cases"', 1)[0]
    comment = before[-900:]
    assert "UNGATED" in comment or "ungated" in comment, \
        "nothing above the /use-cases route explains why it has no gate"


def test_the_nav_link_is_unconditional():
    """The page is never empty -- under neutral it lists every card for every role -- so a
    flag on the link would only ever hide something that works."""
    src = _read(_NAV)
    m = re.search(r'<a data-nav="use_cases"[^>]*>', src)
    assert m, "no Use cases nav link with a data-nav id"
    # The link must not sit inside a {% if %} block. Check the nearest preceding tag.
    before = src[:m.start()]
    last_if = before.rfind("{% if")
    last_endif = before.rfind("{% endif %}")
    assert last_endif > last_if, \
        "the Use cases nav link is inside a {% if %} — it must be unconditional"


def test_the_nav_link_is_not_pinned_by_any_persona():
    """A pin slot spent on a reference page is a work link displaced, and the dashboard band
    already offers "All use cases" to anyone with a focus set. So it lands in "More"."""
    from web_dashboard.services import personas as P
    for p in P.all_personas():
        assert "use_cases" not in p.nav_pins, \
            f"{p.key} pins the Use cases link; it belongs in the More menu"


# ── neutral shows the whole catalog ──────────────────────────────────────────

def test_the_page_reads_the_whole_catalog_not_just_the_active_persona():
    src = _read(_PAGE)
    assert "/api/persona/catalog" in src, \
        "use_cases.html does not fetch the catalog, so it cannot show every role"


def test_the_grouping_reorders_and_never_filters():
    """The active persona's group goes first. Every other group is still present -- which is
    the difference between "your focus leads" and "your focus is all there is"."""
    src = _read(_PAGE)
    body = src.split("this.groups =", 1)[1].split(";", 1)[0]
    assert ".sort(" in body, "the groups are not ordered"
    assert ".filter(" not in body, (
        "the group list is filtered. Every role must always be present; the active one is "
        "merely first.")


def test_the_catalog_covers_every_persona_and_every_card():
    from web_dashboard.services import personas as P
    cat = P.catalog()
    assert [e["persona"] for e in cat] == list(P.VALID_PERSONAS)
    for entry, persona in zip(cat, P.all_personas()):
        assert len(entry["use_cases"]) == len(persona.use_cases), \
            f"{persona.key}: the catalog drops cards"


def test_the_catalog_is_complete_on_both_profiles():
    """A POV instance masks a lot. It must still SEE the whole catalog -- a masked card is
    rendered and explained, not withheld."""
    from web_dashboard.services import personas as P
    from web_dashboard.services import feature_flags as ff
    real = ff.install_profile
    try:
        for profile in ff.VALID_PROFILES:
            ff.install_profile = lambda _p=profile: _p
            cat = P.catalog()
            assert len(cat) == len(P.VALID_PERSONAS), f"{profile}: personas missing"
            total = sum(len(e["use_cases"]) for e in cat)
            expected = sum(len(p.use_cases) for p in P.all_personas())
            assert total == expected, \
                f"{profile}: catalog has {total} cards, expected {expected}"
    finally:
        ff.install_profile = real


# ── the three states render correctly ────────────────────────────────────────

def test_all_three_states_are_rendered():
    src = _read(_PAGE)
    for state in ("ready", "needs_flag", "masked"):
        assert f"'{state}'" in src, f"use_cases.html never mentions the {state} state"


def test_only_a_ready_card_gets_an_anchor():
    """A masked card must not be an <a> at all. An <a> with no href is still focusable and
    still announces as a link, and the point of masked is that there is nowhere to go."""
    for path in (_PAGE, _DASHBOARD):
        src = _read(path)
        for m in re.finditer(r"<a[^>]*:href=\"c\.target\"[^>]*>", src, re.S):
            tag = m.group(0)
            assert "c.state === 'ready'" in tag, (
                f"{os.path.basename(path)}: a card anchor is not gated on the ready "
                f"state: {tag[:90]}")


def test_a_masked_card_offers_no_settings_link():
    src = _read(_PAGE)
    masked = src.split("c.state === 'masked'", 1)[1].split("</p>", 1)[0]
    assert "settings_link" not in masked and "/settings" not in masked, (
        "the masked branch links to Settings, where patch_feature_config would 409 the "
        "enable — the switch cannot move")


def test_the_needs_flag_card_does_link_to_settings():
    """The mirror of the above: needs_flag IS actionable, and hiding the fix is the bug."""
    src = _read(_PAGE)
    needs = src.split("c.state === 'needs_flag'", 1)[1].split("</p>", 1)[0]
    assert "settings_link" in needs, \
        "the needs_flag branch does not point at Settings, so the card hides its own fix"


def test_the_word_unavailable_is_not_reused_for_a_masked_card():
    """dashboard.html already uses "unavailable" to mean "the provider did not answer".
    Reusing it here would make two unrelated states look identical."""
    src = _read(_PAGE)
    assert "unavailable" not in src.lower(), \
        "use_cases.html says 'unavailable', which already means a throttled provider"


def test_a_masked_card_never_carries_a_target_on_either_profile():
    """The behavioural half. Walks every persona x every profile."""
    from web_dashboard.services import personas as P
    from web_dashboard.services import feature_flags as ff
    real = ff.install_profile
    try:
        for profile in ff.VALID_PROFILES:
            ff.install_profile = lambda _p=profile: _p
            for entry in P.catalog():
                for c in entry["use_cases"]:
                    if c["state"] == "masked":
                        assert not c["target"], \
                            f"{entry['persona']}/{c['id']} masked on {profile} with a target"
                        assert not c["settings_link"]
                    if c["state"] == "needs_flag":
                        assert c["settings_link"] == "/settings"
                        assert c["needs"], f"{c['id']} is unready but names nothing"
    finally:
        ff.install_profile = real


# ── the POV lead ─────────────────────────────────────────────────────────────

def test_a_demo_instance_renders_what_it_always_did():
    """The lead is additive. Everything new is behind `isPov`, and the catalog opens by
    default anywhere that is not a POV instance."""
    src = _read(_PAGE)
    for gated in ('x-show="loaded && isPov"',):
        assert gated in src, f"the POV lead is not gated on the profile ({gated})"
    body = src.split("this.showCatalog = ", 1)[1].split(";", 1)[0]
    assert "!this.isPov" in body, \
        "the catalog does not open by default on a demo instance"


def test_the_catalog_is_collapsed_not_filtered():
    """The distinction this whole page rests on. Collapsing changes what leads; filtering
    would make the persona/profile layer able to remove something, which is the one thing
    services/personas exists to forbid."""
    src = _read(_PAGE)
    assign = src.split("this.groups =", 1)[1].split(";", 1)[0]
    assert ".filter(" not in assign, "the catalog groups are filtered"
    # Every group still renders — behind x-show, which keeps it in the DOM.
    block = src.split('<template x-for="g in groups"', 1)[1][:200]
    assert 'x-show="showCatalog"' in block, "the catalog is not collapsible"
    assert "x-if" not in block, \
        "the catalog uses x-if, which REMOVES the groups from the DOM rather than hiding " \
        "them — that is filtering with extra steps"


def test_the_collapse_says_what_is_behind_it():
    """A closed section with no explanation reads as something missing."""
    src = _read(_PAGE)
    assert "showCatalog = !showCatalog" in src, "there is no way to open the catalog"
    assert "Everything this dashboard can demo" in src, \
        "the collapsed section is unlabelled"
    assert "nothing is hidden from you" in src, \
        "the page does not say why the whole catalog is still there"


def test_the_pov_lead_reuses_the_per_pov_endpoint():
    """No new backend: the lead reads exactly what the POV's own page reads, so the two
    can never disagree about what that POV can run."""
    src = _read(_PAGE)
    assert "/api/pov/managed/${this.povId}/use-cases" in src, \
        "the lead does not read the per-POV catalog"
    assert "/api/pov/managed" in src


def test_the_pov_fetch_carries_the_token_and_fails_silently():
    """/api/pov needs auth, unlike the two public endpoints beside it — and every way it
    can fail (a demo instance 404s the router, an expired session 401s) is a non-event on
    a page whose main content loaded fine."""
    src = _read(_PAGE)
    fetcher = src.split("async apiFetch(url)", 1)[1].split("\n      },", 1)[0]
    assert "Authorization" in fetcher, "the POV fetch is anonymous and would 401"
    loader = src.split("async loadPovs()", 1)[1].split("\n      },", 1)[0]
    assert "if (!res.ok) return;" in loader, \
        "a failed POV read is turned into a page error"
    assert "this.error" not in loader, \
        "a POV instance with no POVs, or a demo instance, would show an error"


def test_the_lead_never_offers_an_action():
    """Everything that CHANGES a POV lives on the POV's own page, which is also where every
    other action on it lives. This page reads."""
    src = _read(_PAGE)
    for verb in ("method: 'POST'", "method: 'DELETE'", "mark(", "mintAccessor"):
        assert verb not in src, f"the use-cases page performs a write ({verb})"


def test_a_destroyed_pov_is_not_offered():
    src = _read(_PAGE)
    loader = src.split("async loadPovs()", 1)[1].split("\n      },", 1)[0]
    assert "destroyed" in loader, "the picker offers POVs that have been reaped"


def test_the_remembered_pov_survives_a_browser_that_refuses_storage():
    """localStorage throws on the READ in a browser with site data blocked, not only on
    the write — and this page must render for somebody who has that set."""
    src = _read(_PAGE)
    for fragment in ("getItem('vm_cli_use_cases_pov')", "setItem('vm_cli_use_cases_pov'"):
        assert fragment in src, f"the picker does not remember its POV ({fragment})"
    for line in src.splitlines():
        if "getItem('vm_cli_use_cases_pov')" in line or \
                "setItem('vm_cli_use_cases_pov'" in line:
            assert line.strip().startswith("try {") and "catch" in line, \
                f"an unguarded localStorage call: {line.strip()[:70]}"


def test_the_lead_still_hard_codes_no_persona_key():
    """The same rule the catalog below it keeps."""
    from web_dashboard.services import personas as P
    src = _read(_PAGE)
    lead = src.split("For one POV", 1)[1].split("Everything this dashboard can demo", 1)[0]
    for key in P.VALID_PERSONAS:
        assert f"'{key}'" not in lead and f'"{key}"' not in lead, \
            f"the POV lead names the persona key {key!r}"


# ── the dashboard band ───────────────────────────────────────────────────────

def test_the_band_is_absent_under_the_neutral_persona():
    """"A user who never picks a focus sees today's page" has to stay literally true."""
    src = _read(_DASHBOARD)
    band = src.split("<!-- Use cases for the active persona", 1)
    assert len(band) == 2, "the dashboard use-case band is gone"
    assert "persona.persona &&" in band[1][:500], \
        "the band is not gated on a persona being set, so it would render under neutral"


def test_the_band_shows_at_most_three_cards():
    src = _read(_DASHBOARD)
    body = src.split("topUseCases() {", 1)[1].split("\n      },", 1)[0]
    assert ".slice(0, 3)" in body, "the band is not capped at three cards"


def test_the_band_ranks_rather_than_filters():
    """Reuses rankBy, so "promote, never remove" is one code path with the tiles. A card one
    Settings toggle away is worth seeing, so unready cards are ordered down, not dropped."""
    src = _read(_DASHBOARD)
    body = src.split("topUseCases() {", 1)[1].split("\n      },", 1)[0]
    assert "this.rankBy(" in body, "topUseCases does not use rankBy"
    # The only filter allowed is the one building the rank list, not one shrinking output.
    assert body.count(".filter(") == 1, \
        "topUseCases filters its output; it must rank and then slice"
    assert "return this.rankBy(cards" in body


def test_the_band_is_outside_the_tilesections_block():
    """tests/test_dashboard_collect.py locates tileSections by string and regexes the tiles
    out of it. Anything inserted inside that block truncates the parse."""
    src = _read(_DASHBOARD)
    block = src[src.index("tileSections:"):]
    block = block[:block.index("\n      ],")]
    for token in ("topUseCases", "use_cases", "All use cases"):
        assert token not in block, f"{token} is inside the tileSections literal"


def test_the_band_uses_no_key_field():
    """`{ key: '...' }` in this file is exactly what the collector's regex looks for, and a
    card scanned as a tile fails that suite with a message about a missing collector."""
    src = _read(_DASHBOARD)
    band = src.split("<!-- Use cases for the active persona", 1)[1].split("<!-- Sections", 1)[0]
    assert "key:" not in band, "the use-case band uses `key:`; cards are keyed by `id`"


def test_no_template_hard_codes_a_persona_key():
    from web_dashboard.services import personas as P
    for path in (_PAGE, _DASHBOARD, _NAV):
        src = _read(path)
        for key in P.VALID_PERSONAS:
            k = re.escape(key)
            near = (r"""persona[^\n]{0,80}['"]%s['"]""" % k,
                    r"""['"]%s['"][^\n]{0,80}persona""" % k)
            assert not any(re.search(pat, src, re.I) for pat in near), \
                f"{os.path.basename(path)} branches on the persona key {key!r}"


def test_the_page_requires_auth_client_side_like_every_other_page():
    src = _read(_PAGE)
    assert "requireAuth()" in src, \
        "use_cases.html does not call requireAuth(), unlike every other authed page"


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
