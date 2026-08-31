"""A persona is CURATION. `install_profile` is a GATE. These pin the difference.

`install_profile` exists because of tenancy, so it is mutually exclusive, it subtracts, and
it 404s routes (see test_install_profile.py). A persona is the opposite kind of thing: a
DevOps demo and a hypervisor-admin demo run on the same estate on different days, so a
persona may only reorder, emphasise and surface.

The properties pinned here are the ones whose absence is invisible:

  * **A persona never subtracts.** Enforced structurally rather than by review:
    `feature_flags` must not mention personas, `dashboard_collect` must not, and no page
    route may gate on one. If any of those ever becomes false, a persona can hide a page.
  * **No card is ever a link to a 404.** Every card target must be a real HTML route, every
    `#fragment` a real tab in that route's template, and — the one that actually bites —
    a card needing a profile-masked feature must resolve to `masked` with NO href, on
    BOTH profiles. This is the nav-link-to-404 bug class re-entering through a new door.
  * **Unknown input degrades to neutral**, never raises: resolved on the request path, and
    `?persona=` is attacker-controlled.
  * **Every declared name is real** — flags, section ids, tile keys, docs files. This is a
    content surface, so the first stale rename is a card pointing at a /docs page that 404s
    and no way for the card to know.

Source-shape assertions parse files and import nothing. The behavioural ones import
personas, which needs only config_service, settings and feature_flags.

Runs under pytest, or standalone:
    python tests/test_personas.py
"""
import ast
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-personas")

_SVC = os.path.join(_ROOT, "web_dashboard", "services")
_TPL = os.path.join(_ROOT, "web_dashboard", "templates")
_PERSONAS = os.path.join(_SVC, "personas.py")
_FLAGS = os.path.join(_SVC, "feature_flags.py")
_COLLECT = os.path.join(_SVC, "dashboard_collect.py")
_THEME = os.path.join(_SVC, "ui_theme.py")
_MAIN = os.path.join(_ROOT, "web_dashboard", "main.py")
_DASHBOARD = os.path.join(_TPL, "dashboard.html")
_DOCS = os.path.join(_ROOT, "docs")


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _all_cards():
    from web_dashboard.services import personas as P
    return [(p, c) for p in P.all_personas() for c in p.use_cases]


# ── the persona may not become a gate ────────────────────────────────────────

def test_feature_flags_knows_nothing_about_personas():
    """The import direction is one-way, and this is the assertion that keeps it so.

    Every function in feature_flags.py is a gate. A persona value living in that module
    would sit one indent away from `if profile_masks(flag): return False`, where a
    plausible-looking one-line "optimisation" would start 404ing routes.
    """
    src = _read(_FLAGS)
    assert "persona" not in src.lower(), (
        "feature_flags.py mentions personas. The dependency runs personas -> "
        "feature_flags and never the reverse; a persona that can reach the gate can "
        "subtract, which is the one thing it must never do.")


def test_the_collector_knows_nothing_about_personas():
    """dashboard_collect must keep collecting what the dashboard COULD show.

    Persona is request-scoped so the worker has none to read — but the real reason is
    worse than that: dashboard_stat_cache is ONE shared table. A persona-aware collector
    would leave every de-emphasised tile rendering `unavailable`, which
    test_dashboard_collect calls out by name as indistinguishable from a throttled cloud
    API. A persona that manufactures fake throttling is worse than no persona.
    """
    src = _read(_COLLECT)
    assert "persona" not in src.lower(), \
        "dashboard_collect.py mentions personas — the collector must stay persona-blind"


def test_no_page_route_gates_on_a_persona():
    """A persona may not appear in any route's dependencies. Not one.

    _feature_gate and _profile_page_gate are gates and they 404. A persona in a decorator
    head — even 'harmlessly' — is a persona that has made a page unreachable.
    """
    src = _read(_MAIN)
    offenders = []
    for chunk in src.split("@app.get(")[1:]:
        head = chunk.partition("async def")[0]
        if "persona" in head.lower() and "/api/persona" not in head:
            offenders.append(head.split("\n")[0].strip())
    assert not offenders, (
        f"route decorators reference a persona: {offenders}. A persona curates; it may "
        "never gate.")


def test_the_persona_endpoints_are_ungated():
    """/api/persona must carry no dependencies=[...] of its own.

    Gating the endpoint that reports the curation is the first step towards curation that
    subtracts, and it would also break the wizard, which reads the catalog before setup
    is complete.
    """
    src = _read(_MAIN)
    for chunk in src.split("@app.get(")[1:]:
        head, _, _rest = chunk.partition("async def")
        if "/api/persona" not in head:
            continue
        assert "dependencies=" not in head, \
            f"a persona endpoint is gated: {head.split(chr(10))[0]}"


def test_the_persona_endpoints_bypass_the_setup_guard():
    """The wizard's Focus step fetches the catalog while setup is still incomplete."""
    src = _read(_MAIN)
    block = src.split("_SETUP_BYPASS_PREFIXES = (", 1)[1].split(")", 1)[0]
    assert '"/api/persona"' in block, (
        "/api/persona is not in _SETUP_BYPASS_PREFIXES — the wizard's persona picker "
        "would be handed a 302 to the wizard it is already in")


def test_the_theme_is_not_persona_aware():
    """Colour is the TENANCY signal and it is safety-critical.

    ui_theme.py's own docstring names the failure it prevents: an operator with two
    identical-looking tabs open onboarding a demo deploy into a customer's Password Safe.
    A persona recolouring the chrome corrupts that signal. Its entire visual budget is a
    grey text chip, the landing subtitle, and ordering.
    """
    src = _read(_THEME)
    assert "persona" not in src.lower(), \
        "ui_theme.py mentions personas — chrome colour must carry the profile ALONE"
    assert "def theme_for(profile: str, app_env: str)" in src, \
        "theme_for's signature changed; a persona must not have become an input to it"


# ── ordering fields are hints, never filters ─────────────────────────────────

def _dashboard_section_ids():
    """The tileSections ids, parsed out of dashboard.html the way the collector test
    parses the tile keys."""
    src = _read(_DASHBOARD)
    block = src[src.index("tileSections:"):]
    block = block[:block.index("\n      ],")]
    return set(re.findall(r"\bid:\s*'([a-z_0-9]+)'", block))


def _dashboard_tile_keys():
    src = _read(_DASHBOARD)
    block = src[src.index("tileSections:"):]
    block = block[:block.index("\n      ],")]
    return set(re.findall(r"\{\s*key:\s*'([a-z_0-9]+)'", block))


def test_every_section_order_names_real_sections():
    from web_dashboard.services import personas as P
    known = _dashboard_section_ids()
    assert known, "could not parse any section ids out of dashboard.html"
    for p in P.all_personas():
        for sid in p.section_order:
            assert sid in known, \
                f"{p.key}.section_order names '{sid}', which is not a dashboard section"


def test_every_tile_emphasis_names_a_real_tile():
    from web_dashboard.services import personas as P
    known = _dashboard_tile_keys()
    assert known, "could not parse any tile keys out of dashboard.html"
    for p in P.all_personas():
        for tile in p.tile_emphasis:
            assert tile in known, \
                f"{p.key}.tile_emphasis names '{tile}', which is not a dashboard tile"


def test_section_order_is_not_a_filter():
    """A persona that names a subset of sections must still not remove the rest.

    Pinned on the DATA rather than the renderer because this is the property that makes
    the whole design safe: if section_order were ever read as a whitelist, a persona would
    be subtracting. Every persona naming ANY section must name them all, so the tuple can
    only ever be a reordering and 'is it a filter?' has no observable answer.
    """
    from web_dashboard.services import personas as P
    known = _dashboard_section_ids()
    for p in P.all_personas():
        if not p.section_order:
            continue
        missing = known - set(p.section_order)
        assert not missing, (
            f"{p.key}.section_order omits {sorted(missing)}. Name every section so the "
            "tuple is unambiguously a reordering, never a whitelist.")


# ── every declared name is real ──────────────────────────────────────────────

def test_every_required_flag_is_a_real_flag():
    from web_dashboard.services import personas as P
    from web_dashboard.services import feature_flags as ff
    # `cloud_pages` is a page group, not a flag, and entitle_request_portal_url is a
    # string. Neither is something a card can require.
    real = set(ff.flags().keys()) - {"cloud_pages", "entitle_request_portal_url"}
    for p, c in _all_cards():
        for flag in c.requires_flags + c.requires_any_flag:
            assert flag in real, f"{p.key}/{c.id} requires '{flag}', not a real flag"


def test_every_preset_flag_is_a_real_flag():
    from web_dashboard.services import personas as P
    from web_dashboard.services import feature_flags as ff
    real = set(ff.flags().keys()) - {"cloud_pages", "entitle_request_portal_url"}
    for p in P.all_personas():
        for flag in p.preset_flags:
            assert flag in real, f"{p.key}.preset_flags has '{flag}', not a real flag"


def test_every_referenced_flag_has_a_human_label():
    """"Needs: pra_enabled" is not copy anyone can act on."""
    from web_dashboard.services import personas as P
    for p, c in _all_cards():
        for flag in c.requires_flags + c.requires_any_flag:
            assert flag in P._FLAG_LABELS, \
                f"{p.key}/{c.id} requires '{flag}' with no entry in _FLAG_LABELS"


def test_every_required_cloud_is_a_real_cloud():
    from web_dashboard.services import personas as P
    for p, c in _all_cards():
        for cloud in c.requires_clouds:
            assert cloud in ("aws", "azure", "gcp", "oci"), \
                f"{p.key}/{c.id} requires cloud '{cloud}'"


def test_every_docs_path_exists_on_disk():
    """A card is content, and the first stale rename is a card linking to a 404."""
    from web_dashboard.services import personas as P
    for p in P.all_personas():
        for d in p.docs:
            assert os.path.exists(os.path.join(_DOCS, d + ".md")), \
                f"{p.key}.docs names '{d}', which is not a file under docs/"
    for p, c in _all_cards():
        if not c.docs:
            continue
        assert os.path.exists(os.path.join(_DOCS, c.docs + ".md")), \
            f"{p.key}/{c.id} names docs '{c.docs}', which is not a file under docs/"


# ── no card is ever a link to a 404 ──────────────────────────────────────────

def _html_page_routes():
    """Every @app.get(..., response_class=HTMLResponse) route, as (path, head).

    Same technique as test_install_profile._html_page_routes: parse main.py, import
    nothing. Duplicated rather than imported because CI runs each test file standalone.
    """
    src = _read(_MAIN)
    out = []
    for chunk in src.split("@app.get(")[1:]:
        path = chunk.split('"')[1]
        head, _, _rest = chunk.partition("async def")
        if "HTMLResponse" not in head:
            continue
        out.append((path, head))
    return out


_TEMPLATE_OF = {
    "/aws": "aws/index.html", "/azure": "azure/index.html",
    "/gcp": "gcp/index.html", "/oci": "oci/index.html",
    "/containers": "containers/index.html",
}


def test_every_card_target_is_a_real_page_route():
    """The single most valuable assertion here.

    A card whose target does not resolve is the nav-link-to-404 bug class arriving through
    a new door — and it is invisible until someone in front of a customer clicks it.
    """
    known = {path for path, _head in _html_page_routes()}
    assert "/aws" in known, "could not parse HTML page routes out of main.py"
    for p, c in _all_cards():
        path = c.target.split("#")[0]
        assert path in known, \
            f"{p.key}/{c.id} targets '{path}', which is not an HTML page route"


def test_every_card_fragment_is_a_real_tab():
    """`/gcp#ot` is not a convenience — api/ot.py has no page of its own, so the hash IS
    the address. A renamed tab silently lands the SE on the wrong panel."""
    for p, c in _all_cards():
        if "#" not in c.target:
            continue
        path, _, frag = c.target.partition("#")
        tpl = _TEMPLATE_OF.get(path)
        assert tpl, f"{p.key}/{c.id} deep-links into {path}, which has no entry in " \
                    "_TEMPLATE_OF — add it so the fragment can be verified"
        src = _read(os.path.join(_TPL, tpl))
        tabs = set(re.findall(r"activeTab === '([a-z_0-9]+)'", src))
        assert frag in tabs, \
            f"{p.key}/{c.id} targets '#{frag}' but {tpl} has no such tab (has {sorted(tabs)})"


def test_a_card_needing_a_cloud_page_declares_the_cloud():
    """The demo/pov interaction, pinned where it actually goes wrong.

    On a POV instance `cloud_pages` is masked and /aws /azure /gcp /oci /images 404. A card
    targeting one of them without declaring `requires_clouds` would resolve to `ready` and
    render a live href to a page that 404s — on the instance that does customer work.
    """
    gated = {"/aws", "/azure", "/gcp", "/oci", "/images"}
    for p, c in _all_cards():
        path = c.target.split("#")[0]
        if path not in gated:
            continue
        assert c.requires_clouds, (
            f"{p.key}/{c.id} targets the profile-gated page {path} but declares no "
            "requires_clouds, so it would render a live link to a 404 on a POV instance")


def test_a_card_declares_the_flag_its_target_page_is_gated_on():
    """Derived from main.py, so the card list cannot drift away from the real gates.

    If /databases is gated on cloud_database_enabled, a `dba` card pointing there must
    require that flag — otherwise the card reads `ready` on an instance where the page
    404s.
    """
    gate_of = {}
    for path, head in _html_page_routes():
        m = re.search(r'_feature_gate\(\s*"(\w+)"\s*\)', head)
        if m:
            gate_of[path] = m.group(1)
    assert gate_of, "could not parse any _feature_gate() page gates out of main.py"

    for p, c in _all_cards():
        path = c.target.split("#")[0]
        flag = gate_of.get(path)
        if not flag:
            continue
        assert flag in c.requires_flags, (
            f"{p.key}/{c.id} targets {path}, which main.py gates on '{flag}', but the "
            f"card does not require it (requires {list(c.requires_flags)})")


def test_a_card_declares_an_inline_any_of_page_guard():
    """The gap that the _feature_gate test above cannot see, and it was a live defect.

    Not every page guard is a dependency in the decorator head. `/connections` 404s unless
    ANY of six hypervisor flags is on, and it does that with a plain `if not any(...)` in
    the route body. So a card requiring only `pra_enabled` resolved to `ready` and offered
    a live link to a page that 404s on every POV instance — all six of those flags are
    _DEMO_ONLY.

    This parses the body form, so the next inline guard someone adds is covered too.
    """
    src = _read(_MAIN)
    guards = {}
    for chunk in src.split("@app.get(")[1:]:
        path = chunk.split('"')[1]
        head, _, rest = chunk.partition("async def")
        if "HTMLResponse" not in head:
            continue
        body = rest.split("\n@app.")[0]
        if "status_code=404" not in body:
            continue
        m = re.search(r'any\(\s*flags\.get\(f"\{(\w+)\}_enabled"\)\s*for\s+\1\s+in\s*\(([^)]*)\)',
                      body, re.S)
        if m:
            guards[path] = {n + "_enabled" for n in re.findall(r'"(\w+)"', m.group(2))}
    assert "/connections" in guards, (
        "could not parse the /connections inline any-of guard — if that route changed "
        "shape, this test is now blind and must be updated")

    for p, c in _all_cards():
        path = c.target.split("#")[0]
        needed = guards.get(path)
        if not needed:
            continue
        covered = (set(c.requires_any_flag) & needed) or (set(c.requires_flags) & needed)
        assert covered, (
            f"{p.key}/{c.id} targets {path}, which 404s unless one of "
            f"{sorted(needed)} is on, but the card declares none of them — it would "
            "render a live link to a 404")


def test_no_card_offers_an_href_when_masked():
    """Walk every persona x every profile. A masked card must emit NO target.

    Withheld rather than merely dimmed: a link the client only styles as inert is one
    stray middle-click from proving the page 404s.
    """
    from web_dashboard.services import personas as P
    from web_dashboard.services import feature_flags as ff
    real_profile = ff.install_profile
    try:
        for profile in ff.VALID_PROFILES:
            ff.install_profile = lambda _p=profile: _p
            for entry in P.catalog():
                for card in entry["use_cases"]:
                    if card["state"] == "masked":
                        assert card["target"] == "", (
                            f"{entry['persona']}/{card['id']} is masked on '{profile}' "
                            "but still carries a target")
                        assert card["settings_link"] == "", (
                            f"{entry['persona']}/{card['id']} is masked on '{profile}' "
                            "but links to Settings, where the enable would 409")
    finally:
        ff.install_profile = real_profile


def test_a_persona_whose_cards_are_all_masked_is_still_selectable():
    """`hypervisor` on a POV instance has nothing — all six hypervisor flags are
    _DEMO_ONLY. Refusing to offer the persona would be the persona subtracting, and the
    honest answer is a page that says so."""
    from web_dashboard.services import personas as P
    from web_dashboard.services import feature_flags as ff
    real_profile = ff.install_profile
    try:
        ff.install_profile = lambda: "pov"
        keys = [e["persona"] for e in P.catalog()]
    finally:
        ff.install_profile = real_profile
    assert "hypervisor" in keys and len(keys) == len(P.VALID_PERSONAS), \
        "a persona disappeared from the catalog on a POV instance"


# ── resolution ───────────────────────────────────────────────────────────────

class _FakeRequest:
    def __init__(self, query=None, cookies=None):
        self.query_params = query or {}
        self.cookies = cookies or {}


def test_precedence_is_query_then_cookie_then_default():
    from web_dashboard.services import personas as P
    real_default = P.default_persona
    try:
        P.default_persona = lambda: "sre"
        assert P.resolve(_FakeRequest(query={"persona": "ot"},
                                      cookies={"persona": "dba"})) == ("ot", "url")
        assert P.resolve(_FakeRequest(cookies={"persona": "dba"})) == ("dba", "cookie")
        assert P.resolve(_FakeRequest()) == ("sre", "default")
        P.default_persona = lambda: P.NEUTRAL
        assert P.resolve(_FakeRequest()) == (P.NEUTRAL, "none")
        assert P.resolve(None) == (P.NEUTRAL, "none")
    finally:
        P.default_persona = real_default


def test_an_unknown_persona_resolves_to_neutral_and_never_raises():
    """`?persona=` is attacker-controlled and reflected into HTML. resolve() must return a
    VALID_PERSONAS member or NEUTRAL — never a pass-through."""
    from web_dashboard.services import personas as P
    for bad in ("", "  ", "nope", "OT'; DROP", "<script>", "it", "demo", "pov"):
        key, _src = P.resolve(_FakeRequest(query={"persona": bad}))
        assert key in P.VALID_PERSONAS or key == P.NEUTRAL, f"{bad!r} -> {key!r}"
        assert key != bad or key == P.NEUTRAL


def test_a_persona_key_is_matched_case_insensitively():
    from web_dashboard.services import personas as P
    assert P.resolve(_FakeRequest(query={"persona": "  OT "}))[0] == "ot"


def test_demo_and_pov_are_not_persona_keys():
    """The two axes are orthogonal, and sharing a vocabulary is how they stop being."""
    from web_dashboard.services import personas as P
    from web_dashboard.services import feature_flags as ff
    assert not (set(P.VALID_PERSONAS) & set(ff.VALID_PROFILES))


def test_the_config_row_is_not_called_profile():
    """Both axes live in app_config and both get called "profile" in speech. The row name
    is the only thing that keeps them apart in a grep."""
    src = _read(_PERSONAS)
    assert 'config_service.get("default_persona")' in src, \
        "the persona config row is no longer read as 'default_persona'"
    assert 'config_service.get("profile' not in src


# ── content invariants ───────────────────────────────────────────────────────

def test_every_persona_has_cards_and_every_card_has_copy():
    from web_dashboard.services import personas as P
    assert len(P.all_personas()) == len(P.VALID_PERSONAS)
    for p in P.all_personas():
        assert p.use_cases, f"{p.key} has no use cases"
        assert p.label and p.blurb, f"{p.key} is missing label/blurb"
        for c in p.use_cases:
            assert c.title and c.summary and c.target, f"{p.key}/{c.id} is incomplete"
            assert c.minutes > 0, f"{p.key}/{c.id} has no duration"


def test_card_ids_are_unique_and_namespaced():
    from web_dashboard.services import personas as P
    seen = {}
    for p, c in _all_cards():
        assert c.id not in seen, f"duplicate card id {c.id} ({p.key} and {seen.get(c.id)})"
        seen[c.id] = p.key
        assert c.id.startswith(p.key + "-"), \
            f"{c.id} is not namespaced under its persona '{p.key}'"


def test_the_card_dataclass_does_not_use_the_name_key():
    """tests/test_dashboard_collect.py finds dashboard tiles by regexing `{ key: '...'`.
    A card rendered into that page carrying a `key:` would be scanned as a tile and fail
    that suite with a message about a missing collector — sending the next reader
    somewhere else entirely."""
    src = _read(_PERSONAS)
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "UseCase":
            names = [n.target.id for n in node.body
                     if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)]
            assert "key" not in names, "UseCase has a `key` field; use `id`"
            assert "id" in names
            return
    raise AssertionError("UseCase class not found")


def test_the_neutral_payload_curates_nothing():
    from web_dashboard.services import personas as P
    d = P.describe(P.NEUTRAL)
    assert d["persona"] == P.NEUTRAL and d["label"] == ""
    for k in ("section_order", "tile_emphasis", "nav_pins", "quick_deploy", "use_cases"):
        assert d[k] == [], f"neutral payload curates {k}"


def test_the_catalog_covers_every_persona():
    from web_dashboard.services import personas as P
    assert [e["persona"] for e in P.catalog()] == list(P.VALID_PERSONAS)


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
