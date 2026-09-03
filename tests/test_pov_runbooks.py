"""The runbook axis — a published procedure a POV is run against, as tickable cards.

A runbook is a group of cards like a persona is, and deliberately not a persona: a persona
presets feature flags, orders dashboard tiles, pins nav items and appears in the setup
wizard, and "Password Safe POC runbook" is not a job title. What is pinned here is mostly
that the second registry obeys every rule the first one does, because it reaches the same
consumers:

  * **The two registries are disjoint by card id.** The id is the primary key of a progress
    row, so a collision would let a role's card tick a runbook's off.
  * **The registry is the allowlist for writes**, both halves of it. An id in neither is
    refused rather than stored -- the rows deliberately outlive copy edits, so the mistake
    would too.
  * **A fragment target is a real tab** on pov/detail.html. The fragment IS the
    destination; a stale one lands silently on Overview.
  * **A product mix never subtracts.** Every registered card is present for every mix; a
    card whose product this POV does not include renders ``out_of_scope`` rather than
    vanishing.
  * **Only use cases the runbook can actually teach get a card.** Its seven unfinished
    ones (12-17, 19) are documented in docs/pov-ps-runbook.md instead, and this pins that
    they stay out -- a checkbox nobody can honestly tick reads as a gap in this dashboard.
  * **A POV card declares no instance-level requirements** -- flags and clouds resolve
    through a different reader.

Runs under pytest, or standalone:
    python tests/test_pov_runbooks.py
"""
import itertools
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-pov-runbooks")

_TPL = os.path.join(_ROOT, "web_dashboard", "templates")
_DETAIL = os.path.join(_TPL, "pov", "detail.html")
_DOCS = os.path.join(_ROOT, "docs")

_ENV = "0e2f6a54-test-env"


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _all_cards():
    from web_dashboard.services import pov_runbooks as R
    return [(r, c) for r in R.all_runbooks() for c in r.use_cases]


def _mixes():
    """All eight tenant combinations, each fully wired -- the completeness assertions ask
    whether a card is PRESENT, and leaving the artifacts off would conflate "out of scope"
    with "not wired yet" in the one place that must tell them apart."""
    out = []
    for pra, ps, ent in itertools.product((False, True), repeat=3):
        out.append({"pra": pra, "password_safe": ps, "entitle": ent,
                    "wired": True, "onboarded": True, "entitle_wired": True})
    return out


# ── the registry itself ──────────────────────────────────────────────────────

def test_every_runbook_has_cards_and_names_its_source():
    from web_dashboard.services import pov_runbooks as R
    assert R.VALID_RUNBOOKS, "no runbooks are registered"
    for r in R.all_runbooks():
        assert r.use_cases, f"{r.key} has no cards"
        assert r.label and r.blurb, f"{r.key} has incomplete copy"
        # Without this a runbook is a list of demos with no provenance, and the first
        # question about somebody else's POV is "which document is this?"
        assert r.source, f"{r.key} names no source document"


def test_every_card_has_copy_and_a_duration():
    for r, c in _all_cards():
        assert c.id and c.title and c.summary and c.target, f"{r.key}: incomplete card"
        assert c.minutes > 0, f"{r.key}/{c.id} claims {c.minutes} minutes"


def test_card_ids_are_unique_across_every_runbook():
    ids = [c.id for _r, c in _all_cards()]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    assert not dupes, f"duplicate runbook card ids: {dupes}"


def test_a_runbook_card_id_never_collides_with_a_persona_card_id():
    """The id is the primary key of a progress row. A collision would let a role's card
    tick a runbook's off, and the rows outlive the copy that caused it."""
    from web_dashboard.services import personas as P, pov_runbooks as R
    persona_pov = {c.id for p in P.all_personas() for c in p.pov_use_cases}
    persona_demo = {c.id for p in P.all_personas() for c in p.use_cases}
    mine = {c.id for _r, c in _all_cards()}
    assert not (mine & persona_pov), f"shared with POV cards: {sorted(mine & persona_pov)}"
    assert not (mine & persona_demo), f"shared with demo cards: {sorted(mine & persona_demo)}"
    assert not (set(R.VALID_RUNBOOKS) & set(P.VALID_PERSONAS)), \
        "a runbook key collides with a persona key; both land in the same progress column"


def test_every_required_product_is_a_real_product():
    from web_dashboard.services import personas as P
    for r, c in _all_cards():
        for product in c.requires_products:
            assert product in P.POV_PRODUCTS, \
                f"{r.key}/{c.id} requires '{product}', not a real product"


def test_a_runbook_card_declares_no_flag_or_cloud():
    for r, c in _all_cards():
        assert not c.requires_flags and not c.requires_any_flag and not c.requires_clouds, \
            f"{r.key}/{c.id} is a POV card declaring instance-level requirements"


def test_every_declared_doc_exists_on_disk():
    from web_dashboard.services import pov_runbooks as R
    for r in R.all_runbooks():
        for doc in r.docs:
            assert os.path.exists(os.path.join(_DOCS, doc + ".md")), \
                f"{r.key} names docs '{doc}', which is not a file under docs/"
    for r, c in _all_cards():
        if c.docs:
            assert os.path.exists(os.path.join(_DOCS, c.docs + ".md")), \
                f"{r.key}/{c.id} names docs '{c.docs}', which is not a file under docs/"


# ── a fragment target is a real tab ──────────────────────────────────────────

def _detail_tabs():
    src = _read(_DETAIL)
    block = src.split("tabs: [", 1)[1].split("],", 1)[0]
    return set(re.findall(r"id:\s*'([a-z-]+)'", block))


def test_every_card_fragment_is_a_real_tab():
    tabs = _detail_tabs()
    assert tabs, "could not parse the tab list out of pov/detail.html"
    for r, c in _all_cards():
        assert c.target.startswith("#"), \
            f"{r.key}/{c.id} targets {c.target!r}; POV cards carry a fragment, not a path"
        assert c.target[1:] in tabs, \
            f"{r.key}/{c.id} targets '{c.target}', not a tab in pov/detail.html"


# ── the catalog never filters ────────────────────────────────────────────────

def test_the_catalog_is_complete_for_every_product_mix():
    from web_dashboard.services import pov_runbooks as R
    expected_cards = sum(len(r.use_cases) for r in R.all_runbooks())
    for mix in _mixes():
        cat = R.catalog(_ENV, mix)
        assert [g["persona"] for g in cat] == list(R.VALID_RUNBOOKS), \
            f"{mix}: the catalog drops or reorders runbooks"
        total = sum(len(g["use_cases"]) for g in cat)
        assert total == expected_cards, \
            f"{mix}: catalog has {total} cards, expected {expected_cards}"


def test_an_out_of_scope_card_carries_no_target():
    """A link the client merely styles as inert is one middle-click from proving there is
    nothing there for this POV."""
    from web_dashboard.services import pov_runbooks as R
    seen = 0
    for mix in _mixes():
        for g in R.catalog(_ENV, mix):
            for c in g["use_cases"]:
                if c["state"] == "out_of_scope":
                    seen += 1
                    assert not c["target"], f"{c['id']} is out of scope with a target"
                    assert c["needs"], f"{c['id']} is out of scope and names nothing"
    assert seen, "no mix put a runbook card out of scope; the gating is not being exercised"


def test_the_group_shape_matches_the_persona_one():
    """Every consumer -- the detail page's group loop, `pov_summary.by_persona`,
    `pov_use_cases._summarize` -- takes both registries without knowing which is which.
    A missing key is a group that renders blank rather than one that errors."""
    from web_dashboard.services import personas as P, pov_runbooks as R
    mix = _mixes()[-1]
    persona_keys = set(P.pov_catalog(_ENV, mix)[0].keys())
    for g in R.catalog(_ENV, mix):
        assert set(g.keys()) == persona_keys, \
            f"{g.get('persona')!r} group keys differ: {sorted(set(g) ^ persona_keys)}"


def test_an_unknown_runbook_key_yields_an_empty_group_never_none():
    from web_dashboard.services import pov_runbooks as R
    g = R.describe("no-such-runbook", _ENV, _mixes()[-1])
    assert g["use_cases"] == [] and g["label"] == ""


def test_the_blurb_carries_the_source_document():
    """So an SE opening somebody else's POV can find the procedure being followed."""
    from web_dashboard.services import pov_runbooks as R
    for r in R.all_runbooks():
        g = R.describe(r.key, _ENV, _mixes()[-1])
        assert r.source in g["blurb"], f"{r.key}'s blurb does not name its source"


# ── find_card is the write allowlist ────────────────────────────────────────

def test_find_card_resolves_every_registered_card():
    from web_dashboard.services import pov_runbooks as R
    for r, c in _all_cards():
        key, found = R.find_card(c.id)
        assert key == r.key and found is c, f"{c.id} did not resolve to {r.key}"


def test_find_card_refuses_what_it_does_not_know():
    from web_dashboard.services import pov_runbooks as R
    for bad in ("", "   ", "pspoc-nope", "pov-security-who-has-access"):
        key, found = R.find_card(bad)
        assert found is None and key == "", f"{bad!r} resolved to {key!r}"


# ── the Password Safe POC runbook, against its document ──────────────────

def _numbers(cards):
    """Each card's runbook number, from its own title. The title is the only place the
    number lives, which is deliberate: an SE matches card to page by reading it."""
    out = []
    for c in cards:
        m = re.match(r"^UC(\d+[AB]?) " + "\u00b7" + " ", c.title)
        assert m, f"{c.id} does not lead with its runbook number: {c.title!r}"
        out.append(m.group(1))
    return out


def test_the_password_safe_poc_runbook_carries_only_the_use_cases_it_can_teach():
    """The document numbers its use cases to 20 and splits 11 into A and B. Seven of them
    are unwritten, unQA'd since 2022, or the author's personal notes, and a card for one of
    those is a checkbox no SE can honestly tick -- so 14 cards, in the document's own order.

    Pinned as an exact list rather than a count: the point is WHICH are absent, and a count
    alone would let a future edit swap a real use case for an unfinished one.
    """
    from web_dashboard.services import pov_runbooks as R
    rb = R.get("ps-poc-skytap")
    assert rb is not None, "the Password Safe POC runbook is not registered"
    assert _numbers(rb.use_cases) == [
        "1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11A", "11B", "18", "20"]


def test_the_runbooks_unfinished_use_cases_have_no_card():
    """12-17 and 19. Their absence IS the decision, so it is pinned rather than left to
    whoever next reads the document and wonders why they are missing."""
    from web_dashboard.services import pov_runbooks as R
    unfinished = {"12", "13", "14", "15", "16", "17", "19"}
    present = set(_numbers(R.get("ps-poc-skytap").use_cases))
    overlap = sorted(present & unfinished)
    assert not overlap, f"the runbook cannot teach {overlap}; see docs/pov-ps-runbook.md"


def test_the_doc_accounts_for_every_use_case_the_checklist_omits():
    """With the cards gone the doc is the ONLY record that the runbook has holes, so an SE
    looking for use case 15 has somewhere to find out why it is absent."""
    doc = _read(os.path.join(_DOCS, "pov-ps-runbook.md"))
    for n in ("12", "13", "14", "15", "16", "17", "19"):
        missing = f"docs/pov-ps-runbook.md does not account for omitted use case {n}"
        assert f"| {n} " in doc, missing



def test_the_pra_integration_card_needs_both_products():
    """UC20 arrived in rev 7.0 and is the only card that is not Password Safe alone. A POV
    with no PRA tenant must show it out of scope rather than ready."""
    from web_dashboard.services import pov_runbooks as R
    card = next(c for c in R.get("ps-poc-skytap").use_cases
                if c.id == "pspoc-uc20-pws-pra-integration")
    assert set(card.requires_products) == {"pra", "password_safe"}


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
