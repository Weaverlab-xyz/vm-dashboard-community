"""Per-POV use cases: a third axis that states, and still never subtracts.

`install_profile` GATES (tests/test_install_profile.py). A persona CURATES
(tests/test_personas.py). This one answers "can I run this on THIS POV?" — and the whole
risk of adding it is that it quietly becomes a fourth kind of gate, because unlike the
other two it resolves against a customer's row rather than against configuration.

The properties pinned here are the ones whose absence would be invisible:

  * **A product mix never shrinks the catalog.** Every persona and every card is present
    for all eight combinations of the three tenants. A Password-Safe-only POV is a normal
    shape, not a degraded one, and the moment a mix could remove a card this module would
    be an install_profile with extra steps.
  * **`out_of_scope` is not `masked`.** Different fact, different word, and — the half that
    actually bites — NO href and NO action link, because there is nowhere useful to send
    somebody: the fix is a tenant on the POV row, which is a decision about the evaluation.
  * **A POV card never leaks into the demo tests, and vice versa.** The two lists resolve
    through different readers. `tests/test_personas` walks `use_cases` and asserts every
    target is a real `@app.get` path; a POV card's target is a fragment on a parameterised
    route, so it would fail that test for entirely the wrong reason.
  * **`/pov/{env_id}` is declared after `/pov/templates`.** Starlette matches in declaration
    order, so the reverse turns the builder page into "No such POV environment" and nothing
    else looks wrong.
  * **`personas` still knows nothing about the database.** The POV resolvers take a dict of
    booleans precisely so that stays true; a row parameter would drag `database` into a
    module `api/docs_pages` imports deliberately.

Source-shape assertions parse files and import nothing. The behavioural ones import
personas, which needs only config_service, settings and feature_flags.

Runs under pytest, or standalone:
    python tests/test_pov_use_cases.py
"""
import ast
import itertools
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-pov-use-cases")

_SVC = os.path.join(_ROOT, "web_dashboard", "services")
_TPL = os.path.join(_ROOT, "web_dashboard", "templates")
_PERSONAS = os.path.join(_SVC, "personas.py")
_SERVICE = os.path.join(_SVC, "pov_use_cases.py")
_MAIN = os.path.join(_ROOT, "web_dashboard", "main.py")
_API = os.path.join(_ROOT, "web_dashboard", "api", "pov.py")
_DETAIL = os.path.join(_TPL, "pov", "detail.html")
_LIST = os.path.join(_TPL, "pov", "index.html")
_NAV = os.path.join(_TPL, "_nav_links.html")
_DOCS = os.path.join(_ROOT, "docs")

_ENV = "0e2f6a54-test-env"


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _all_pov_cards():
    from web_dashboard.services import personas as P
    return [(p, c) for p in P.all_personas() for c in p.pov_use_cases]


def _mixes():
    """All eight tenant combinations, each fully wired.

    Fully wired on purpose: these drive the completeness assertions, where the question is
    whether a card is PRESENT, and leaving the artifacts off would conflate "out of scope"
    with "not wired yet" in the one place that must tell them apart.
    """
    out = []
    for pra, ps, ent in itertools.product((False, True), repeat=3):
        out.append({"pra": pra, "password_safe": ps, "entitle": ent,
                    "wired": True, "onboarded": True, "entitle_wired": True})
    return out


# ── every declared name is real ──────────────────────────────────────────────

def test_every_persona_has_pov_cards_and_every_card_has_copy():
    from web_dashboard.services import personas as P
    for p in P.all_personas():
        assert p.pov_use_cases, f"{p.key} has no POV cards"
        for c in p.pov_use_cases:
            assert c.id and c.title and c.summary and c.target, f"{p.key}: incomplete card"
            assert c.minutes > 0, f"{p.key}/{c.id} claims {c.minutes} minutes"


def test_pov_card_ids_are_unique_across_every_persona():
    """The id is the primary key of a progress row, so a collision would let two roles'
    cards tick each other off."""
    ids = [c.id for _p, c in _all_pov_cards()]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    assert not dupes, f"duplicate POV card ids: {dupes}"


def test_a_pov_card_id_never_collides_with_a_demo_card_id():
    from web_dashboard.services import personas as P
    demo = {c.id for p in P.all_personas() for c in p.use_cases}
    pov = {c.id for _p, c in _all_pov_cards()}
    assert not (demo & pov), f"ids used by both catalogs: {sorted(demo & pov)}"


def test_every_required_product_is_a_real_product():
    from web_dashboard.services import personas as P
    for p, c in _all_pov_cards():
        for product in c.requires_products:
            assert product in P.POV_PRODUCTS, \
                f"{p.key}/{c.id} requires '{product}', not a real product"


def test_every_product_has_a_label_an_artifact_and_a_remedy():
    """"Needs: password_safe" is not copy anyone can act on, and a product with no artifact
    key would silently resolve every card for it as ready."""
    from web_dashboard.services import personas as P
    for product in P.POV_PRODUCTS:
        assert product in P._PRODUCT_LABELS, f"{product} has no human label"
        assert product in P._PRODUCT_ARTIFACT, f"{product} names no wire-up artifact"
        assert product in P._PRODUCT_REMEDY, f"{product} has no remedy copy"


def test_every_pov_card_docs_path_exists_on_disk():
    """A card is content, and the first stale rename is a card linking to a 404."""
    for p, c in _all_pov_cards():
        if not c.docs:
            continue
        assert os.path.exists(os.path.join(_DOCS, c.docs + ".md")), \
            f"{p.key}/{c.id} names docs '{c.docs}', which is not a file under docs/"


# ── the two catalogs stay apart ──────────────────────────────────────────────

def test_a_pov_card_declares_no_flag_or_cloud():
    """The two lists resolve through different readers. A POV card carrying requires_flags
    would be half-resolved by whichever one saw it first."""
    for p, c in _all_pov_cards():
        assert not c.requires_flags and not c.requires_any_flag and not c.requires_clouds, \
            f"{p.key}/{c.id} is a POV card declaring instance-level requirements"


def test_a_demo_card_declares_no_product():
    from web_dashboard.services import personas as P
    for p in P.all_personas():
        for c in p.use_cases:
            assert not c.requires_products, \
                f"{p.key}/{c.id} is a demo card declaring POV products"


def test_the_demo_card_tests_still_walk_only_the_demo_list():
    """tests/test_personas asserts every card target is a real @app.get path. A POV card's
    is a fragment on a parameterised route, so folding the two lists into that helper would
    fail it for the wrong reason -- or, worse, get the assertion weakened to accommodate."""
    src = _read(os.path.join(_ROOT, "tests", "test_personas.py"))
    helper = src.split("def _all_cards():", 1)[1].split("\ndef ", 1)[0]
    assert "p.use_cases" in helper and "pov_use_cases" not in helper, \
        "tests/test_personas._all_cards has been widened to include POV cards"


# ── a fragment target is a real tab ──────────────────────────────────────────

def _detail_tabs():
    """The tab ids the POV detail page actually renders, from its own tab list."""
    src = _read(_DETAIL)
    block = src.split("tabs: [", 1)[1].split("],", 1)[0]
    return set(re.findall(r"id:\s*'([a-z-]+)'", block))


def test_every_pov_card_target_is_a_fragment():
    for p, c in _all_pov_cards():
        assert c.target.startswith("#"), \
            f"{p.key}/{c.id} targets {c.target!r}; POV cards carry a fragment, not a path"


def test_every_pov_card_fragment_is_a_real_tab():
    """The fragment IS the destination -- there is no second way to reach a tab -- so a
    stale one is a card that silently lands on Overview."""
    tabs = _detail_tabs()
    assert tabs, "could not parse the tab list out of pov/detail.html"
    for p, c in _all_pov_cards():
        assert c.target[1:] in tabs, \
            f"{p.key}/{c.id} targets '{c.target}', not a tab in pov/detail.html ({sorted(tabs)})"


def test_the_action_tab_is_a_real_tab():
    from web_dashboard.services import personas as P
    assert P._POV_ACTION_TAB[1:] in _detail_tabs(), \
        f"the needs_wiring action link points at {P._POV_ACTION_TAB}, not a real tab"


# ── the mix states, and never filters ────────────────────────────────────────

def test_the_catalog_is_complete_for_every_product_mix():
    from web_dashboard.services import personas as P
    expected_groups = len(P.VALID_PERSONAS)
    expected_cards = sum(len(p.pov_use_cases) for p in P.all_personas())
    for mix in _mixes():
        cat = P.pov_catalog(_ENV, mix)
        assert [g["persona"] for g in cat] == list(P.VALID_PERSONAS), \
            f"{mix}: the catalog drops or reorders personas"
        assert len(cat) == expected_groups
        total = sum(len(g["use_cases"]) for g in cat)
        assert total == expected_cards, \
            f"{mix}: catalog has {total} cards, expected {expected_cards}"


def test_an_out_of_scope_card_carries_no_target_and_no_action():
    """The half that bites. A link the client merely styles as inert is one middle-click
    from proving there is nothing there for this POV."""
    from web_dashboard.services import personas as P
    for mix in _mixes():
        for g in P.pov_catalog(_ENV, mix):
            for c in g["use_cases"]:
                if c["state"] == "out_of_scope":
                    assert not c["target"], f"{c['id']} is out of scope with a target"
                    assert not c["action_link"], f"{c['id']} is out of scope with an action"
                    assert c["needs"], f"{c['id']} is out of scope and names nothing"


def test_a_card_with_no_products_is_ready_on_every_mix():
    """The oversight cards are why an empty requires_products exists: a POV wired into one
    product must still have something to run."""
    from web_dashboard.services import personas as P
    unconditional = {c.id for _p, c in _all_pov_cards() if not c.requires_products}
    assert unconditional, "no POV card is product-independent; the empty mix has nothing"
    for mix in _mixes():
        for g in P.pov_catalog(_ENV, mix):
            for c in g["use_cases"]:
                if c["id"] in unconditional:
                    assert c["state"] == "ready", \
                        f"{c['id']} needs no product but is {c['state']} on {mix}"


def test_a_tenant_without_its_artifact_is_needs_wiring_and_says_what_to_run():
    from web_dashboard.services import personas as P
    mix = {"pra": True, "password_safe": True, "entitle": True,
           "wired": False, "onboarded": False, "entitle_wired": False}
    seen = 0
    for g in P.pov_catalog(_ENV, mix):
        for c in g["use_cases"]:
            if not c["products"]:
                continue
            seen += 1
            assert c["state"] == "needs_wiring", f"{c['id']} is {c['state']} with nothing wired"
            assert c["needs"], f"{c['id']} is unready but names nothing"
            assert c["action_link"] == f"/pov/{_ENV}{P._POV_ACTION_TAB}"
    assert seen, "no product-tagged cards were exercised"


def test_absence_beats_unwired_when_a_card_names_two_products():
    """A card needing PRA and Password Safe on a POV with no PRA cannot be run at all, so
    "run the wire-up" would send an operator to a button that will skip what they came for."""
    from web_dashboard.services import personas as P
    two = [(p, c) for p, c in _all_pov_cards() if len(c.requires_products) > 1]
    assert two, "no POV card names two products; this rule is untested"
    for p, c in two:
        mix = {k: True for k in ("pra", "password_safe", "entitle")}
        mix.update({"wired": True, "onboarded": True, "entitle_wired": True})
        # Drop the first product's tenant, and leave a second one wired.
        mix[c.requires_products[0]] = False
        state, _needs = P._pov_card_state(c, mix)
        assert state == "out_of_scope", f"{p.key}/{c.id} is {state} with a tenant missing"


def test_a_ready_card_targets_this_pov_and_only_this_pov():
    from web_dashboard.services import personas as P
    mix = {k: True for k in ("pra", "password_safe", "entitle",
                             "wired", "onboarded", "entitle_wired")}
    for g in P.pov_catalog(_ENV, mix):
        for c in g["use_cases"]:
            assert c["state"] == "ready", f"{c['id']} is {c['state']} on a fully wired POV"
            assert c["target"].startswith(f"/pov/{_ENV}#"), \
                f"{c['id']} targets {c['target']!r}"


def test_find_pov_card_is_the_allowlist_and_refuses_a_demo_id():
    """The registry is what stops the progress table becoming a free-text store -- and the
    demo ids must not be a back door into it, since they name pages a POV cannot reach."""
    from web_dashboard.services import personas as P
    key, card = P.find_pov_card("pov-security-who-has-access")
    assert card is not None and key == "security"
    for bogus in ("", "   ", "nope", "cloudops-three-layers"):
        assert P.find_pov_card(bogus) == ("", None), f"{bogus!r} resolved to a card"


# ── the module boundary ──────────────────────────────────────────────────────

def test_personas_still_knows_nothing_about_the_database():
    """The POV resolvers take a dict of booleans precisely so this stays true. A row
    parameter would drag `database` into a module api/docs_pages imports deliberately."""
    tree = ast.parse(_read(_PERSONAS))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            name = node.module or ""
            assert "database" not in name and not name.startswith("api"), \
                f"personas.py imports {name!r}"
            for alias in node.names:
                assert alias.name not in ("database",), \
                    f"personas.py imports {alias.name!r}"
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert "database" not in alias.name, f"personas.py imports {alias.name!r}"


def _imported_names(path):
    """Every module name a file imports, from its AST -- so the `pov_use_cases` FIELD on
    Persona is not mistaken for an import of the module that shares its name."""
    names = set()
    for node in ast.walk(ast.parse(_read(path))):
        if isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
    return names


def test_the_import_direction_is_one_way():
    """pov_use_cases imports personas. The reverse would put a database-backed module one
    indent away from a registry that must stay pure."""
    assert "personas" in _imported_names(_SERVICE), \
        "pov_use_cases does not import personas"
    assert "pov_use_cases" not in _imported_names(_PERSONAS), \
        "personas.py imports pov_use_cases; the dependency runs the other way"


def test_the_writes_live_in_the_api_layer_not_the_persona_layer():
    """services/personas opens by saying a card navigates and never starts work. A tick is
    not a deploy, but it IS a write, and it belongs behind the API's auth rather than in the
    registry."""
    src = _read(_PERSONAS)
    for token in ("db.commit", "Session", "PovUseCaseProgress"):
        assert token not in src, f"personas.py contains {token!r}; it must stay a pure registry"


# ── the route, and the order it is declared in ───────────────────────────────

def _route_positions():
    src = _read(_MAIN)
    return src.index('@app.get("/pov/templates"'), src.index('@app.get("/pov/{env_id}"')


def test_the_detail_route_exists_and_is_gated_like_the_pov_page():
    src = _read(_MAIN)
    head = src.split('@app.get("/pov/{env_id}"', 1)[1].split("async def", 1)[0]
    assert "HTMLResponse" in head
    assert '_feature_gate("pov_environments_enabled")' in head, \
        "/pov/{env_id} is not gated; it would render a POV page on a demo instance"


def test_the_detail_route_is_declared_after_the_templates_route():
    """Starlette matches in declaration order. Reversed, /pov/templates is captured as an
    environment id: the builder page 404s as "No such POV environment" and nothing else
    looks wrong."""
    templates_at, detail_at = _route_positions()
    assert templates_at < detail_at, \
        "/pov/{env_id} is declared before /pov/templates and would swallow it"


def test_the_nav_link_lights_on_a_pov_page_but_never_on_the_builder():
    src = _read(_NAV)
    m = re.search(r'<a data-nav="pov"[^>]*>', src, re.S)
    assert m, "no POV nav link"
    tag = m.group(0)
    assert "startswith('/pov/')" in tag, "the POV link does not light on a POV's own page"
    assert "not request.url.path.startswith('/pov/templates')" in tag, \
        "the POV link lights on the builder too, which reads as being in two places"


# ── the page ─────────────────────────────────────────────────────────────────

def test_the_page_renders_all_three_states():
    src = _read(_DETAIL)
    for state in ("ready", "needs_wiring", "out_of_scope"):
        assert f"'{state}'" in src, f"pov/detail.html never mentions the {state} state"


def test_only_a_ready_card_gets_an_anchor():
    src = _read(_DETAIL)
    anchors = re.findall(r"<a[^>]*:href=\"c\.target\"[^>]*>", src, re.S)
    assert anchors, "no card anchor on the page"
    for tag in anchors:
        assert "c.state === 'ready'" in tag, \
            f"a card anchor is not gated on the ready state: {tag[:90]}"


def test_an_out_of_scope_card_is_offered_no_action_on_the_page():
    src = _read(_DETAIL)
    branch = src.split("c.state === 'out_of_scope'", 1)[1].split("</p>", 1)[0]
    assert "action_link" not in branch and "href" not in branch, \
        "the out_of_scope branch offers a link; the fix is a tenant, not a click"


def test_the_needs_wiring_card_does_link_to_the_wire_up():
    src = _read(_DETAIL)
    branch = src.split("c.state === 'needs_wiring'", 1)[1].split("</p>", 1)[0]
    assert "action_link" in branch, \
        "the needs_wiring branch hides its own fix"


def test_the_page_never_filters_the_groups():
    """Every role is always present -- the same promise /use-cases keeps one layer up."""
    src = _read(_DETAIL)
    block = src.split("async loadUseCases()", 1)[1].split("\n      },", 1)[0]
    assert ".filter(" not in block, \
        "loadUseCases filters the groups; a product mix may state a card, never remove it"


def test_no_template_hard_codes_a_persona_key_or_a_card_id():
    from web_dashboard.services import personas as P
    src = _read(_DETAIL)
    for key in P.VALID_PERSONAS:
        assert not re.search(r"""persona[^\n]{0,80}['"]%s['"]""" % re.escape(key), src, re.I), \
            f"pov/detail.html branches on the persona key {key!r}"
    for _p, c in _all_pov_cards():
        assert c.id not in src, f"pov/detail.html hard-codes the card id {c.id!r}"


def test_the_page_requires_auth_client_side_like_every_other_page():
    assert "requireAuth()" in _read(_DETAIL), \
        "pov/detail.html does not call requireAuth(), unlike every other authed page"


def test_the_deep_link_selects_the_tab():
    """A ready card links to /pov/<id>#wired. Landing on Overview instead would make every
    card on the page point at the same screen."""
    src = _read(_DETAIL)
    assert "window.location.hash" in src, "pov/detail.html ignores the fragment it is sent"


def test_the_list_page_links_each_row_to_its_own_page():
    src = _read(_LIST)
    assert "'/pov/' + e.id" in src, "the POV list does not link a row to its own page"


def test_the_list_row_counts_only_what_this_pov_can_run():
    """A denominator of the whole catalog would make a correctly scoped evaluation read as
    one going badly."""
    src = _read(_LIST)
    assert "use_cases" in src, "the POV list does not show a use-case count"
    assert "(e.use_cases || {}).total" in src, \
        "the list's denominator is not the in-scope total"


# ── the API ──────────────────────────────────────────────────────────────────

def test_the_endpoints_exist_and_carry_the_same_auth_as_their_neighbours():
    src = _read(_API)
    for decorator in ('@router.get("/managed/{env_id}/use-cases")',
                      '@router.post("/managed/{env_id}/use-cases/{card_id}")',
                      '@router.delete("/managed/{env_id}/use-cases/{card_id}")'):
        assert decorator in src, f"missing route {decorator}"
        body = src.split(decorator, 1)[1].split("\n@router.", 1)[0]
        assert "Depends(get_current_user)" in body, \
            f"{decorator} does not authenticate like every other POV route"


def test_an_unknown_card_id_is_refused_rather_than_stored():
    src = _read(_API)
    body = src.split('@router.post("/managed/{env_id}/use-cases/{card_id}")', 1)[1]
    body = body.split("\n@router.", 1)[0]
    assert "UseCaseError" in body and "status_code=400" in body, \
        "the write route does not refuse an unknown card id"


def test_the_list_endpoint_does_not_pay_for_the_wire_up_twice():
    """_serialize already computes pov_wireup.describe per row. Recomputing it for the
    summary would put a second per-VM query on the list endpoint for numbers it holds."""
    src = _read(_API)
    block = src.split("def _serialize(", 1)[1].split("\ndef ", 1)[0]
    assert block.count("pov_wireup.describe(") == 1, \
        "_serialize computes the wire-up state more than once"
    assert "summary_for(_db_of(env), env, wireup)" in block, \
        "_serialize does not hand the wire-up state it already has to the summary"


def test_the_destroy_path_keeps_the_record_and_reports_it():
    """The POV row is marked destroyed rather than deleted because it is the record of
    something that existed. Its use-case history is that record's contents."""
    svc = _read(os.path.join(_SVC, "pov_env_service.py"))
    assert "pov_use_cases.destroy_note(db, env)" in svc, \
        "run_env_destroy does not report the use-case record"
    use_cases = _read(_SERVICE)
    note = use_cases.split("def destroy_note(", 1)[1]
    assert ".delete()" not in note, \
        "destroy_note deletes the record it exists to preserve"


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
