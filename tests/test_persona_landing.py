"""The landing page curates by persona. Neutral must render today's page, unchanged.

The whole persona layer rests on one claim -- that it only ever reorders -- and the
landing page is where that claim is easiest to break by accident. So:

  * **Neutral is a no-op.** Same sections, same order, same eight quick-deploy shortcuts in
    the same sequence. A user who never picks a persona must not be able to tell this
    shipped.
  * **The ordering data lives in Python.** No persona key string appears in any template
    or JS file, or the page and services/personas.py drift and the wizard's preview lies
    about what picking a focus will do.
  * **tileSections survives verbatim.** tests/test_dashboard_collect.py finds the tiles by
    string-locating `tileSections:` and regexing them out. Renaming it, or making it a
    getter, breaks the collector-parity suite -- and it fails with a message about missing
    collectors that sends the next reader somewhere else entirely.
  * **The persona does not touch chrome.** Colour is the tenancy signal and it is
    safety-critical; ui_theme.py must stay persona-blind and theme_for's signature intact.
  * **The empty state cannot be faked.** `visibleSections` must keep reading the unordered
    list, so a bug in the ranking can never make a working instance look bare.

Source-shape assertions parse files and import nothing. The ranking behaviour is checked by
porting rankBy to Python -- the JS is four lines and its contract, not its syntax, is what
has to hold.

Runs under pytest, or standalone:
    python tests/test_persona_landing.py
"""
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-persona-landing")

_TPL = os.path.join(_ROOT, "web_dashboard", "templates")
_DASHBOARD = os.path.join(_TPL, "dashboard.html")
_BASE = os.path.join(_TPL, "base.html")
_THEME = os.path.join(_ROOT, "web_dashboard", "services", "ui_theme.py")
_MAIN = os.path.join(_ROOT, "web_dashboard", "main.py")
_JS = os.path.join(_ROOT, "web_dashboard", "static", "js")

# Today's quick-deploy band, in today's order. Hard-coded rather than derived, because the
# point is to catch a change to the catalog, and a derived expectation would move with it.
_SHIPPED_QUICK_DEPLOY = [
    ("ec2", "EC2 instance", "/aws", "aws"),
    ("azure_vm", "Azure VM", "/azure", "azure"),
    ("gce", "GCE instance", "/gcp", "gcp"),
    ("oci", "OCI instance", "/oci", "oci"),
    ("proxmox_vm", "Proxmox VM", "/proxmox", "proxmox"),
    ("nutanix_vm", "Nutanix VM", "/nutanix", "nutanix"),
    ("database", "Database", "/databases", "cloud_database"),
    ("k8s", "Kubernetes cluster", "/k8s", "k8s_management"),
]


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _tuple_literal(src, name):
    """The text of a ``name = ( ... )`` literal, balancing parens.

    Splitting on the first ``)`` is not good enough: these tuples carry explanatory
    comments and a parenthetical in one of them silently truncated the match, which made
    this file's own assertion fail on a list that was in fact correct.
    """
    i = src.index(name + " = (") + len(name) + 4
    depth, j = 1, i
    while depth:
        if src[j] == "(":
            depth += 1
        elif src[j] == ")":
            depth -= 1
        j += 1
    return src[i:j - 1]


def _tilesections_block():
    """The tileSections literal, located the way tests/test_dashboard_collect.py locates
    it. If this raises, that suite is already broken too."""
    src = _read(_DASHBOARD)
    block = src[src.index("tileSections:"):]
    return block[:block.index("\n      ],")]


def _section_ids():
    return re.findall(r"\bid:\s*'([a-z_0-9]+)'", _tilesections_block())


def _quick_deploy_catalog():
    """The declarative quick-deploy table, in declaration order."""
    src = _read(_DASHBOARD)
    block = src[src.index("quickDeployCatalog:"):]
    block = block[:block.index("\n      ],")]
    return re.findall(
        r"\{\s*id:\s*'([a-z_0-9]+)',\s*label:\s*'([^']*)',\s*href:\s*'([^']*)',\s*"
        r"flag:\s*'([a-z_0-9]+)'\s*\}", block)


# ── neutral is a no-op ───────────────────────────────────────────────────────

def _rank_by(items, order, id_of):
    """The Python twin of dashboard.html's rankBy. Same contract: stable, and anything
    the order does not name keeps its shipped relative position and follows."""
    order = list(order or [])

    def rank(i):
        try:
            return order.index(id_of(i))
        except ValueError:
            return sys.maxsize

    return [x for _r, _i, x in sorted(
        ((rank(it), idx, it) for idx, it in enumerate(items)),
        key=lambda t: (t[0], t[1]))]


def test_neutral_leaves_the_section_order_exactly_as_shipped():
    shipped = _section_ids()
    assert shipped, "could not parse section ids out of dashboard.html"
    assert _rank_by(shipped, [], lambda s: s) == shipped


def test_neutral_leaves_the_quick_deploy_band_exactly_as_shipped():
    """The refactor from eight `if (f.x) t.push(...)` lines to a table must be provably
    behaviour-preserving, or it is a silent capability change dressed as a cleanup."""
    catalog = _quick_deploy_catalog()
    assert catalog == _SHIPPED_QUICK_DEPLOY, (
        "the quick-deploy catalog no longer matches the band this replaced.\n"
        f"  got:      {catalog}\n  expected: {_SHIPPED_QUICK_DEPLOY}")
    ids = [c[0] for c in catalog]
    assert _rank_by(ids, [], lambda s: s) == ids


def test_ranking_promotes_without_ever_removing():
    """`order` is a hint, never a filter. This is the property the whole design rests on."""
    items = ["a", "b", "c", "d"]
    assert _rank_by(items, ["c"], lambda s: s) == ["c", "a", "b", "d"]
    assert _rank_by(items, ["d", "b"], lambda s: s) == ["d", "b", "a", "c"]
    # An order naming something absent is ignored, not an error.
    assert _rank_by(items, ["zzz"], lambda s: s) == items
    for order in ([], ["c"], ["d", "b"], ["zzz"], ["a", "b", "c", "d"]):
        assert sorted(_rank_by(items, order, lambda s: s)) == sorted(items), \
            f"ranking by {order} changed the SET of items, not just their order"


def test_every_persona_ordering_id_exists_on_the_page():
    """A persona naming a section, tile or shortcut that does not exist is a silent no-op
    -- the curation just quietly does nothing, which is worse than an error."""
    from web_dashboard.services import personas as P
    sections = set(_section_ids())
    tiles = set(re.findall(r"\{\s*key:\s*'([a-z_0-9]+)'", _tilesections_block()))
    shortcuts = {c[0] for c in _quick_deploy_catalog()}
    for p in P.all_personas():
        for sid in p.section_order:
            assert sid in sections, f"{p.key}.section_order: no section '{sid}'"
        for t in p.tile_emphasis:
            assert t in tiles, f"{p.key}.tile_emphasis: no tile '{t}'"
        for q in p.quick_deploy:
            assert q in shortcuts, f"{p.key}.quick_deploy: no shortcut '{q}'"


# ── the page holds no persona knowledge ──────────────────────────────────────

def test_no_template_or_script_hard_codes_a_persona_key():
    """Ordering arrives as data. The moment a template says "cloudops leads with cloud",
    it and services/personas.py can disagree, and the wizard's preview becomes a lie."""
    from web_dashboard.services import personas as P
    targets = []
    for root, _dirs, files in os.walk(_TPL):
        targets += [os.path.join(root, f) for f in files if f.endswith(".html")]
    if os.path.isdir(_JS):
        for root, _dirs, files in os.walk(_JS):
            targets += [os.path.join(root, f) for f in files if f.endswith(".js")]
    offenders = []
    for path in targets:
        src = _read(path)
        for key in P.VALID_PERSONAS:
            # A quoted key NEAR the word "persona". A bare quoted key is not enough on
            # its own: `activeTab === 'ot'` on the three cloud pages is the OT deep-link
            # tab, predates personas entirely, and has nothing to do with this. What must
            # never appear is a front-end branch ON a persona.
            k = re.escape(key)
            near = (r"""persona[^\n]{0,80}['"]%s['"]""" % k,
                    r"""['"]%s['"][^\n]{0,80}persona""" % k)
            if any(re.search(pat, src, re.I) for pat in near):
                offenders.append(f"{os.path.relpath(path, _ROOT)} -> '{key}'")
    assert not offenders, (
        f"persona keys hard-coded in the front end: {offenders}. Read them from "
        "/api/persona instead.")


def test_the_persona_route_is_public_in_both_places_that_decide_that():
    """`fetch('/api/persona')` is bare on purpose, and two separate lists have to agree.

    The app authenticates off the Authorization header and sets no cookie anywhere, so a
    bare fetch is an ANONYMOUS request. tests/test_template_scripts guards against that
    and keeps an allowlist of routes deliberately reachable without a token;
    main._SETUP_BYPASS_PREFIXES is the other half, and without it the wizard's Focus step
    would be handed a 302 to the wizard it is already in.

    Nothing enforced that those two lists agree, and the failure is asymmetric and ugly:
    drop it from the allowlist and CI fails loudly, drop it from the bypass list and the
    wizard silently loses its persona step. So: both, pinned here.
    """
    guard = _read(os.path.join(_ROOT, "tests", "test_template_scripts.py"))
    allow = _tuple_literal(guard, "_PUBLIC_API")
    assert '"/api/persona"' in allow, (
        "/api/persona is not in test_template_scripts._PUBLIC_API, so the bare fetch in "
        "dashboard.html fails that guard")
    bypass = _tuple_literal(_read(_MAIN), "_SETUP_BYPASS_PREFIXES")
    assert '"/api/persona"' in bypass, (
        "/api/persona is not in main._SETUP_BYPASS_PREFIXES — the wizard's Focus step "
        "would be redirected to the wizard it is already in")


def test_the_dashboard_reads_the_persona_from_the_api():
    src = _read(_DASHBOARD)
    assert "fetch('/api/persona')" in src, \
        "dashboard.html no longer loads the persona from /api/persona"
    assert "loadPersona()" in src


def test_the_persona_load_is_awaited_before_the_first_paint():
    """Loaded in the same await as the features. Arriving late would reorder the bands
    under the operator after the page had already settled."""
    src = _read(_DASHBOARD)
    assert "Promise.all([this.loadFeatures(), this.loadPersona()])" in src, \
        "the persona is no longer loaded alongside the features in init()"


def test_a_failed_persona_load_leaves_the_page_in_its_shipped_order():
    """A persona is presentation. If /api/persona fails the page must still render."""
    src = _read(_DASHBOARD)
    body = src.split("async loadPersona()", 1)[1].split("\n      },", 1)[0]
    assert "catch" in body, "loadPersona has no catch — an API blip would blank the page"
    assert "Object.assign" in body, (
        "loadPersona assigns the payload wholesale; a response missing an ordering key "
        "would leave it undefined and take rankBy with it")


def test_no_route_hands_a_persona_name_to_templateresponse():
    """The context-processor overwrite rule, extended to the persona keys.

    Starlette applies context processors with context.update() AFTER the route's own
    context, so anything _profile_context returns overwrites what a route passed.
    tests/test_install_profile pins this for flag names, but it derives its forbidden set
    from feature_flags.flags(), so it is blind to persona keys. The failure is silent.
    """
    src = _read(_MAIN)
    forbidden = ("persona", "persona_label", "persona_source")
    offenders = []
    for chunk in src.split("TemplateResponse(")[1:]:
        call = chunk[:chunk.index(")")] if ")" in chunk else chunk
        for name in forbidden:
            if re.search(r'["\']%s["\']\s*:' % name, call) or \
                    re.search(r"\b%s\s*=" % name, call):
                offenders.append(call.strip()[:90])
    assert not offenders, (
        f"a route passes a persona name to TemplateResponse: {offenders}. The context "
        "processor overwrites it, so the value is silently discarded.")


# ── the persona does not touch chrome ────────────────────────────────────────

def test_the_theme_stays_persona_blind():
    src = _read(_THEME)
    assert "persona" not in src.lower(), \
        "ui_theme.py mentions personas — chrome colour must carry the profile ALONE"
    assert "def theme_for(profile: str, app_env: str)" in src


def test_the_lens_is_not_in_the_measured_nav_row():
    """base.html's x-ref="navRow" spans the brand, the links AND the user menu, and
    test_profile_theme records only 61px of headroom at 1280px before responsiveNav folds
    the whole nav into the drawer. So a control in the user menu costs exactly what one in
    the link row costs. The lens lives on the dashboard until the pinned/"More" split
    buys the width back."""
    src = _read(_BASE)
    row = src.split('x-ref="navRow"', 1)[1].split("</nav>", 1)[0]
    assert "lensOpen" not in row and "setPersona" not in row, (
        "the lens selector is inside the measured nav row — it would spend the same 61px "
        "of headroom the fold decision uses")


def test_the_lens_does_not_colour_itself_by_persona():
    """No per-persona accent, not even inside the content area: an emerald 'SRE' control
    on a violet POV instance is the greyscale-screenshot confusion one layer in."""
    src = _read(_DASHBOARD)
    lens = src.split("<!-- Lens selector -->", 1)[1].split("</div>\n  </div>", 1)[0]
    for token in ("theme.", "persona.color", "persona.accent", "persona.chip_class"):
        assert token not in lens, f"the lens reads {token} — it must carry no profile or " \
                                  "persona colour of its own"


# ── the collector's parse survives ───────────────────────────────────────────

def test_tilesections_is_still_a_literal_the_collector_can_parse():
    """Guards tests/test_dashboard_collect.py's string-locate + regex, which is load-bearing
    and easy to break from this file without noticing."""
    src = _read(_DASHBOARD)
    assert "tileSections: [" in src, "tileSections is no longer a literal array"
    assert "get tileSections" not in src, "tileSections became a getter — the collector " \
                                          "parity test locates it as a literal"
    block = _tilesections_block()
    keys = re.findall(r"\{\s*key:\s*'([a-z_0-9]+)'", block)
    assert len(keys) == len(set(keys)), "duplicate tile keys"
    assert len(keys) >= 29, f"the collector test expects ~29 tile keys, parsed {len(keys)}"


def test_the_quick_deploy_catalog_is_not_inside_the_tilesections_block():
    """A second 6-space-indented array between `tileSections:` and its closing bracket
    would end the collector's parse early and silently shorten its tile list."""
    block = _tilesections_block()
    assert "quickDeployCatalog" not in block, \
        "quickDeployCatalog is inside the tileSections block — it truncates the parse"


def test_the_quick_deploy_catalog_uses_id_not_key():
    """`{ key: ...` in this file is what the collector regex looks for. A shortcut using
    that name would be scanned as a dashboard tile with no collector behind it."""
    src = _read(_DASHBOARD)
    block = src[src.index("quickDeployCatalog:"):]
    block = block[:block.index("\n      ],")]
    assert "key:" not in block, "the quick-deploy catalog uses `key:`; use `id:`"
    assert block.count("id:") == len(_SHIPPED_QUICK_DEPLOY)


# ── the empty state cannot be faked ──────────────────────────────────────────

def test_the_empty_state_reads_the_unordered_sections():
    """visibleSections decides whether the page shows "No integrations configured yet".
    Pointing it at the ordered list would let a ranking bug make a working instance look
    like a bare install."""
    src = _read(_DASHBOARD)
    body = src.split("get visibleSections()", 1)[1].split("},", 1)[0]
    assert "this.tileSections" in body, \
        "visibleSections no longer reads tileSections directly"
    assert "orderedSections" not in body, \
        "visibleSections reads the persona-ordered list — a ranking bug could then " \
        "render the empty state on a fully configured instance"


def test_tiles_are_never_moved_between_sections():
    """tile_emphasis ranks WITHIN a section. The band labels are what tell an operator
    where a number came from, so a tile that jumped bands would be a lie about its
    source."""
    src = _read(_DASHBOARD)
    body = src.split("orderedTiles(section) {", 1)[1].split("\n      },", 1)[0]
    assert "visibleTiles(section.tiles)" in body, \
        "orderedTiles no longer ranks within the section's own visible tiles"


def test_the_options_list_offers_neutral_first():
    """"No focus" must be as easy to get back to as it was to leave, or the persona has
    effectively subtracted the plain dashboard."""
    from web_dashboard.services import personas as P
    opts = P.options()
    assert opts[0]["key"] == P.NEUTRAL and opts[0]["label"], \
        "the neutral option is not first, or has no label"
    assert [o["key"] for o in opts[1:]] == list(P.VALID_PERSONAS)
    assert len(opts) == len(P.VALID_PERSONAS) + 1


def test_the_active_persona_payload_carries_the_options():
    """So the lens needs no second request, and so no template hard-codes a key."""
    from web_dashboard.services import personas as P
    for key in (P.NEUTRAL, "ot"):
        d = P.describe(key)
        assert d["options"] == P.options(), f"describe({key!r}) omits the option list"


def test_switching_persona_strips_the_url_override():
    """`?persona=` wins over the cookie by design. Switching away from a URL-sourced
    persona without stripping the parameter would let the URL immediately win again, so
    the control would appear to do nothing."""
    src = _read(_DASHBOARD)
    body = src.split("setPersona(key)", 1)[1].split("\n      },", 1)[0]
    assert "searchParams.delete('persona')" in body, \
        "setPersona does not strip ?persona= — switching away from a URL-set persona " \
        "would silently have no effect"
    assert "document.cookie" in body and "samesite=lax" in body.lower()


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
