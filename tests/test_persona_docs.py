"""One narrative doc per role, surfaced in /docs — and /docs stays persona-blind.

This slice is content, which is the kind of thing that rots quietly. So:

  * **Every persona has a doc, and every doc has a persona.** An orphan file in
    docs/personas/ is a page nobody can reach from the app; a persona with no doc is a card
    set with no story behind it.
  * **Every card title in the registry appears in its role's doc.** Otherwise the doc drifts
    from the catalog and an SE reading one gets a demo the other does not offer.
  * **/docs must not become persona-aware.** That shell is PUBLIC and unauthenticated, and
    it already leaks `install_profile` deliberately. Ordering the index by the instance's
    chosen focus would leak the focus to anyone who asks. The persona-aware view of the same
    material is /use-cases, behind the auth shell.

Link resolution and anchor validity are already covered for every file under docs/ by
tests/test_docs_anchors.py, which walks the tree recursively — so these new pages inherit
that for free and this file does not duplicate it.

Runs under pytest, or standalone:
    python tests/test_persona_docs.py
"""
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-persona-docs")

_DOCS = os.path.join(_ROOT, "docs")
_PERSONA_DOCS = os.path.join(_DOCS, "profiles", "demo", "personas")
_DOCS_PAGES = os.path.join(_ROOT, "web_dashboard", "api", "docs_pages.py")
_README = os.path.join(_ROOT, "README.md")


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _doc_files():
    """README.md is the folder index, not a role -- docs_pages hangs the /docs section
    heading off it rather than listing it, and the orphan check below would otherwise
    read it as a persona named "README"."""
    return sorted(f for f in os.listdir(_PERSONA_DOCS)
                  if f.endswith(".md") and f != "README.md")


# ── every persona has a doc, and every doc has a persona ─────────────────────

def test_the_persona_docs_directory_exists():
    assert os.path.isdir(_PERSONA_DOCS), "docs/profiles/demo/personas/ is missing"


def test_every_persona_declares_exactly_one_doc():
    from web_dashboard.services import personas as P
    for p in P.all_personas():
        assert len(p.docs) == 1, \
            f"{p.key} declares {len(p.docs)} docs; expected exactly one narrative page"
        assert p.docs[0] == f"profiles/demo/personas/{p.key}", \
            f"{p.key} declares docs={p.docs!r}; expected ('profiles/demo/personas/{p.key}',)"


def test_every_declared_doc_exists_on_disk():
    """The card-level equivalent is already pinned in test_personas; this is the role level.
    A stale rename here is a /docs link that 404s with no way for the page to know."""
    from web_dashboard.services import personas as P
    for p in P.all_personas():
        for d in p.docs:
            assert os.path.exists(os.path.join(_DOCS, d + ".md")), \
                f"{p.key} declares '{d}', which is not a file under docs/"


def test_there_are_no_orphan_persona_docs():
    from web_dashboard.services import personas as P
    on_disk = {f[:-3] for f in _doc_files()}
    declared = {p.key for p in P.all_personas()}
    orphans = on_disk - declared
    assert not orphans, (
        f"docs/personas/ has files no persona claims: {sorted(orphans)}. They would render "
        "in the /docs index with nothing in the app linking to them.")
    assert on_disk == declared, f"missing docs for: {sorted(declared - on_disk)}"


def test_every_doc_leads_with_the_role_name():
    """The /docs index derives its link text from the first heading, so a doc whose H1 is
    something else lists under a name the app never uses."""
    from web_dashboard.services import personas as P
    for p in P.all_personas():
        first = _read(os.path.join(_PERSONA_DOCS, p.key + ".md")).lstrip().split("\n", 1)[0]
        assert first.startswith("# "), f"{p.key}.md does not open with an H1"
        assert len(first) > 3, f"{p.key}.md has an empty H1"


# ── the docs and the card catalog say the same thing ─────────────────────────

def test_every_card_title_appears_in_its_role_doc():
    """The doc is the narrative and the cards are the catalog; they must not drift.

    Matched on the title text, so renaming a card without touching its doc fails here —
    which is the direction drift actually happens.
    """
    from web_dashboard.services import personas as P
    missing = []
    for p in P.all_personas():
        text = _read(os.path.join(_PERSONA_DOCS, p.key + ".md"))
        for c in p.use_cases:
            if c.title not in text:
                missing.append(f"{p.key}: {c.title!r}")
    assert not missing, (
        "card titles with no matching section in the role's doc:\n  " + "\n  ".join(missing))


def test_no_doc_promises_a_card_that_does_not_exist():
    """The other direction: a heading under "Use cases" must correspond to a real card.

    Catches the copy-paste that leaves a deleted demo documented as if it still shipped.
    """
    from web_dashboard.services import personas as P
    stray = []
    for p in P.all_personas():
        text = _read(os.path.join(_PERSONA_DOCS, p.key + ".md"))
        if "## Use cases" not in text:
            stray.append(f"{p.key}: no '## Use cases' section")
            continue
        section = text.split("## Use cases", 1)[1]
        section = section.split("\n## ", 1)[0]
        titles = {c.title for c in p.use_cases}
        for h in re.findall(r"^### (.+)$", section, re.M):
            if h.strip() not in titles:
                stray.append(f"{p.key}: '### {h.strip()}' is not a card")
    assert not stray, "docs describe demos the registry does not offer:\n  " + "\n  ".join(stray)


def test_every_doc_names_what_to_enable():
    """A story with no prerequisites is a story that fails in front of a customer."""
    from web_dashboard.services import personas as P
    for p in P.all_personas():
        text = _read(os.path.join(_PERSONA_DOCS, p.key + ".md"))
        assert "## What to enable" in text, f"{p.key}.md has no 'What to enable' section"


def test_a_demo_only_focus_says_so():
    """hypervisor, dba and sre lean on demo-owned features, so most of their cards are masked
    on a POV instance. The doc has to say that plainly -- an SE who discovers it live reads
    it as broken rather than as a deliberate tenancy split."""
    from web_dashboard.services import personas as P
    from web_dashboard.services import feature_flags as ff
    real = ff.install_profile
    try:
        ff.install_profile = lambda: "pov"
        for entry in P.catalog():
            masked = sum(1 for c in entry["use_cases"] if c["state"] == "masked")
            if masked < len(entry["use_cases"]) / 2:
                continue
            text = _read(os.path.join(_PERSONA_DOCS, entry["persona"] + ".md"))
            assert "pov/README.md" in text, (
                f"{entry['persona']} has {masked} masked cards on a POV instance but its doc "
                "never mentions the POV/demo split")
    finally:
        ff.install_profile = real


# ── /docs surfaces them, and stays persona-blind ─────────────────────────────

def test_the_docs_index_surfaces_the_personas_section():
    src = _read(_DOCS_PAGES)
    block = src.split("_INDEX_SECTIONS = {", 1)[1].split("}", 1)[0]
    assert '"profiles/demo/personas"' in block, \
        "the personas folder is not in _INDEX_SECTIONS, so the pages render only at a known path"


def test_the_index_titles_match_the_persona_labels():
    """The /docs index derives its link text from the filename, and these files are named
    after the persona key so the URL matches the app's vocabulary -- which lists them as
    "Ot", "Dba" and "Sre". docs_pages carries an explicit override map instead.

    It is a literal there rather than an import, because that module is deliberately
    independent of the persona registry. This is the test that stops the two drifting, and
    it is the reason the duplication is acceptable.
    """
    from web_dashboard.services import personas as P
    src = _read(_DOCS_PAGES)
    block = src.split("_TITLE_OVERRIDES = {", 1)[1].split("\n}", 1)[0]
    pairs = dict(re.findall(r'"([^"]+)":\s*"([^"]+)"', block))
    for p in P.all_personas():
        key = f"profiles/demo/personas/{p.key}"
        assert key in pairs, f"docs_pages._TITLE_OVERRIDES has no entry for {key}"
        assert pairs[key] == p.label, (
            f"{key} is titled {pairs[key]!r} in the docs index but the persona's label is "
            f"{p.label!r} — the index and the app would name the same role differently")
    # Scoped to the persona prefix. The map legitimately carries other pages whose
    # filename is an identifier rather than a phrase ("Ot Demo Cell"), and those answer
    # to no persona label.
    prefix = "profiles/demo/personas/"
    extra = ({k for k in pairs if k.startswith(prefix)}
             - {prefix + p.key for p in P.all_personas()})
    assert not extra, f"_TITLE_OVERRIDES has entries for unknown personas: {sorted(extra)}"


def test_the_override_is_not_a_general_h1_rewrite():
    """Guards the decision, not just the code. Using each doc's H1 everywhere was measured
    across the 53 indexed pages: it changes 40 titles and several for the worse -- one H1 is
    a 78-character sentence. An index entry is a link, not a headline."""
    src = _read(_DOCS_PAGES)
    # Bounded to doc_index itself. An unbounded slice runs to end of file and picks up
    # doc_page's read_text, which is how a page's CONTENT is legitimately served — the first
    # draft of this test failed on exactly that.
    idx = src.split("async def doc_index", 1)[1].split("\n@router.", 1)[0]
    # H1-EXTRACTION, not the string "h1" — the index legitimately emits its own
    # <h1>Documentation</h1>, and the first draft of this test tripped on exactly that.
    for token in ("startswith('# ')", 'startswith("# ")', "_first_heading",
                  "read_text", ".open(", "readlines"):
        assert token not in idx, \
            f"the docs index reads document bodies to derive titles ({token})"
    assert "_TITLE_OVERRIDES.get(" in idx, \
        "the index no longer consults the explicit override map"


def test_the_docs_shell_stays_persona_blind():
    """PUBLIC and unauthenticated. It already leaks install_profile on purpose; the
    instance's chosen FOCUS is a different thing and must not join it."""
    src = _read(_DOCS_PAGES)
    assert "personas.resolve" not in src and "default_persona" not in src, (
        "docs_pages reads the active persona. That shell is public — the focus would leak "
        "to any anonymous caller. Do the persona-aware view on /use-cases instead.")
    assert "import personas" not in src and "services import personas" not in src, \
        "docs_pages imports the personas service; it must not need to"


def test_the_docs_index_is_not_reordered_by_anything():
    """The index sorts by section then title, for everybody, always. A persona-ranked index
    would be the leak above wearing a different hat."""
    src = _read(_DOCS_PAGES)
    idx = src.split("def docs_index", 1)[1] if "def docs_index" in src else src
    assert "sorted(groups)" in idx, "the /docs index no longer sorts its sections plainly"


def test_the_readme_documents_the_personas_pages():
    """README.md's table is the entry point for someone who has not opened the app yet."""
    text = _read(_README)
    assert "docs/profiles/demo/personas" in text, "README does not link the personas docs"
    assert "Personas" in text


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
