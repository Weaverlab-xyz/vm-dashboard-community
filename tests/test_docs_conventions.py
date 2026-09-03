"""Every doc says who it is for, and every folder has a way in.

Two conventions that hold only if something checks them, because breaking either is
invisible -- a page with no audience still renders, and an unlisted page still resolves
for anyone who already knows its path.

  * **The header block.** ``docs/`` is 80-odd pages across two install profiles, and it is
    the product's live help system as much as a GitHub tree. ``install_profile`` is ``demo``
    or ``pov`` and the two are mutually exclusive, so a reader who lands on
    ``profiles/pov/wiring.md`` from the /docs index -- which is PUBLIC, and lists both
    profiles' sections to everybody -- has no way to tell whether that page describes their
    instance. Every page therefore carries one line under its H1: **Audience**, **Profile**,
    and a "read this when". A page that skips it reads as though it applies to everyone.

  * **The folder index.** Nineteen docs had zero inbound links from anywhere -- all eight
    persona pages, six cloud-identity-JIT phase runbooks, both POV design notes. They were
    reachable only by knowing the path. Every folder therefore has an index: either its
    own ``README.md``, which GitHub renders as the folder's landing page and which
    ``docs_pages.doc_index`` hangs the section heading off instead of listing as an entry
    called "Readme", or -- for a hub-and-spoke split -- the sibling ``<folder>.md`` the
    spokes were cut out of. Never both; see ``_index_of``.

Three placement rules, each learned the hard way, are asserted rather than commented:

  * **Below the H1, not above it.** ``test_persona_docs`` and
    ``test_database_terminology`` both read a doc's first line and require a heading there.
    Above the H1 the block breaks both and improves nothing, because the /docs index derives
    its link text from the filename and never from the body.

  * **Not a heading.** Heading slugs de-duplicate per document with a ``-1``/``-2`` suffix
    (``docs_pages._GitHubSlugger``), so an ``## Audience`` on a page that already had an
    Audience heading would take the base slug and silently push the existing one to ``-1``,
    breaking every link into it. A bolded line inside a blockquote mints no anchor at all.

  * **One physical line.** A single newline is a *soft* break in both renderers, so a
    three-line block collapses into one run-on paragraph unless you use trailing double
    spaces (which editors strip) or blank lines (three paragraphs of chrome).

Link resolution and anchor validity for everything under ``docs/`` are already covered by
tests/test_docs_anchors.py, which walks the tree recursively -- so the folder indexes
inherit that for free and this file does not duplicate it.

Skips the one renderer-dependent check when ``markdown`` isn't installed, matching
tests/test_docs_anchors.py; the rest are filesystem-and-regex only and always run.

Run: python tests/test_docs_conventions.py   (or under pytest)
"""
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

os.environ.setdefault("JWT_SECRET_KEY", "test-secret-docs-conventions")

_DOCS = os.path.join(_ROOT, "docs")
_DOCS_PAGES = os.path.join(_ROOT, "web_dashboard", "api", "docs_pages.py")
_README = os.path.join(_ROOT, "README.md")
_INDEX = "README.md"

_AUDIENCES = ("operator", "presenter", "customer", "contributor")
_PROFILES = ("demo", "pov", "both")

_HEADER = re.compile(
    r"^> \*\*Audience:\*\* (?P<aud>\w+)"
    r" · \*\*Profile:\*\* `(?P<prof>\w+)`"
    r" · \*\*Read this when:\*\* (?P<when>\S.*)$")

_LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")

try:
    import markdown  # noqa: F401  (presence check; the renderer imports it lazily)
    from web_dashboard.api.docs_pages import _render_markdown
except Exception as exc:  # markdown / fastapi absent outside CI
    _render_markdown = None
    _IMPORT_ERR = exc


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _docs():
    """(relpath-under-docs, abspath) for every page, index pages included."""
    for dp, _, files in os.walk(_DOCS):
        for f in sorted(files):
            if f.endswith(".md"):
                p = os.path.join(dp, f)
                yield os.path.relpath(p, _DOCS).replace("\\", "/"), p


def _folders():
    """Every directory under docs/ that holds at least one .md, docs/ itself included."""
    out = set()
    for rel, _ in _docs():
        out.add(os.path.dirname(rel))
    return out


def _header(text):
    lines = text.split("\n")
    for line in lines[1:]:
        if line.strip():
            return _HEADER.match(line), line
    return None, ""


# ── the header block ─────────────────────────────────────────────────────────

def test_every_doc_opens_with_an_h1():
    """The block goes under it, so this is the precondition for everything below -- and
    two other suites already depend on it for their own pages."""
    bad = [rel for rel, p in _docs() if not _read(p).startswith("# ")]
    assert not bad, f"these docs do not open with an H1: {bad}"


def test_every_doc_carries_the_header_block():
    """...as the first non-blank line after the H1, so it is what a reader sees before the
    first paragraph and before the first heading."""
    bad = []
    for rel, p in _docs():
        m, line = _header(_read(p))
        if not m:
            bad.append(f"{rel}: {line[:70]!r}")
    assert not bad, (
        f"{len(bad)} doc(s) whose first line after the H1 is not the header block:\n  "
        + "\n  ".join(bad)
        + "\n\nExpected exactly: > **Audience:** <who> · **Profile:** `<profile>` · "
          "**Read this when:** <trigger>.")


def test_every_audience_and_profile_is_a_declared_value():
    """Free text here is how "SE", "presenter", "demo engineer" and "sales" become four
    names for one reader, and how a page ends up claiming a profile that does not exist."""
    bad = []
    for rel, p in _docs():
        m, _ = _header(_read(p))
        if not m:
            continue  # the test above owns that failure
        if m.group("aud") not in _AUDIENCES:
            bad.append(f"{rel}: audience {m.group('aud')!r} is not one of {_AUDIENCES}")
        if m.group("prof") not in _PROFILES:
            bad.append(f"{rel}: profile {m.group('prof')!r} is not one of {_PROFILES}")
    assert not bad, "\n  ".join([""] + bad)


def test_the_trigger_is_a_sentence_and_not_the_title_again():
    """The whole point of the clause is the situation that should send you here, so a
    restatement of the title is the one thing it must not be."""
    bad = []
    for rel, p in _docs():
        text = _read(p)
        m, _ = _header(text)
        if not m:
            continue
        when = m.group("when")
        title = text.split("\n", 1)[0][2:].strip()
        if not when.endswith("."):
            bad.append(f"{rel}: trigger does not end in a period")
        if when.strip(".").lower() == title.lower():
            bad.append(f"{rel}: trigger is just the title again")
    assert not bad, "\n  ".join([""] + bad)


def test_the_header_block_is_not_a_heading():
    """Belt and braces for the dedupe hazard in the module docstring: no doc may carry an
    Audience / Profile / Read-this-when HEADING at any level, because minting a duplicate
    slug moves an existing anchor to -1 and breaks every link into it, silently."""
    pattern = re.compile(r"^#{1,6}\s+(Audience|Profile|Read this when)\b", re.I | re.M)
    bad = [rel for rel, p in _docs() if pattern.search(_read(p))]
    assert not bad, f"the header block must not be a heading, in: {bad}"


def test_exactly_one_line_declares_the_audience():
    """A second, informally-worded Audience line is how a page ends up naming two different
    readers -- docs/design/entitle-user-jit.md had one that disagreed with its own block."""
    bad = []
    for rel, p in _docs():
        n = len(re.findall(r"^> \*\*Audience:\*\*", _read(p), re.M))
        if n != 1:
            bad.append(f"{rel}: {n} audience lines")
    assert not bad, "\n  ".join([""] + bad)


# ── the folder indexes ───────────────────────────────────────────────────────

def _index_of(folder):
    """The page that indexes a folder, or None.

    Either the folder's own README.md, or -- for a hub-and-spoke split -- the sibling
    ``<folder>.md`` the spokes were cut out of. The sibling case is not a shortcut: a
    folder cannot have BOTH, because ``doc_page`` resolves ``<page>.md`` before falling
    back to ``<page>/README.md``, so docs/databases.md and docs/databases/README.md would
    both answer /docs/databases and the README would be the one you could never reach.
    The hub keeps the filename because operator-facing strings name it -- eight scripts
    and six agent error messages name ONBOARDING.md and remote-agents.md."""
    own = os.path.join(_DOCS, folder, _INDEX)
    if os.path.isfile(own):
        return own
    if not folder:
        return None
    # Case-insensitively, but by LISTING rather than by os.path.isfile: two of these hubs
    # are SCREAMING_SNAKE (ONBOARDING.md), and isfile() on an NTFS checkout would match
    # "onboarding.md" while the same call on CI's Linux would not -- a test that passes
    # here and fails there, over a filename.
    parent = os.path.join(_DOCS, os.path.dirname(folder))
    want = f"{os.path.basename(folder)}.md".lower()
    for f in os.listdir(parent):
        if f.lower() == want and os.path.isfile(os.path.join(parent, f)):
            return os.path.join(parent, f)
    return None


def test_every_folder_with_docs_has_an_index():
    missing = sorted(f for f in _folders() if _index_of(f) is None)
    assert not missing, (
        f"these folders have neither a {_INDEX} nor a sibling hub, so their pages are "
        f"reachable only by path: {[f or 'docs/' for f in missing]}")


def test_no_folder_has_both_a_readme_and_a_sibling_hub():
    """Both is worse than either: /docs/<folder> serves the hub, so the README becomes a
    page nothing can navigate to -- the exact problem the indexes exist to fix."""
    both = sorted(f for f in _folders()
                  if f and os.path.isfile(os.path.join(_DOCS, f, _INDEX))
                  and os.path.isfile(os.path.join(_DOCS, f"{f}.md")))
    assert not both, (
        f"these folders have a {_INDEX} AND a sibling <folder>.md: {both}. Keep one.")


def test_every_index_links_every_doc_in_its_own_folder():
    """An index that lists nine of ten pages is worse than no index: the missing one now
    looks deliberately unlisted rather than forgotten."""
    bad = []
    for folder in sorted(_folders()):
        index = _index_of(folder)
        if index is None:
            continue  # the test above owns that failure
        base = os.path.dirname(os.path.relpath(index, _DOCS)).replace("\\", "/")
        linked = set()
        for href in _LINK.findall(_read(index)):
            href = href.split("#")[0].rstrip("/")
            # hrefs in a sibling hub are relative to docs/, not to the folder
            if base != folder and href.lower().startswith(
                    f"{os.path.basename(folder).lower()}/"):
                href = href.split("/", 1)[1]
            linked.add(href)
        here = os.path.join(_DOCS, folder)
        for f in sorted(os.listdir(here)):
            if not f.endswith(".md") or f == _INDEX:
                continue
            if f not in linked:
                bad.append(f"{folder or '.'}/{_INDEX} does not link {f}")
    assert not bad, "\n  ".join([""] + bad)


def test_every_index_links_its_immediate_subfolders():
    """So the tree is walkable from docs/README.md down, rather than each index being an
    island that happens to exist."""
    bad = []
    for folder in sorted(_folders()):
        index = _index_of(folder)
        if index is None:
            continue
        linked = {h.split("#")[0].rstrip("/") for h in _LINK.findall(_read(index))}
        children = {f for f in _folders()
                    if os.path.dirname(f) == folder and f != folder}
        for child in sorted(children):
            name = os.path.basename(child)
            # Whatever indexes the child counts: its own README, or the sibling hub a
            # split cut its spokes out of -- under that hub's real name, which is not
            # always the folder's case (ONBOARDING.md indexes onboarding/).
            child_index = _index_of(child)
            accept = {name, f"{name}/{_INDEX}"}
            if child_index:
                accept.add(os.path.relpath(child_index, os.path.join(_DOCS, folder))
                           .replace("\\", "/"))
            if not (accept & linked):
                bad.append(f"{folder or '.'}/{_INDEX} does not link the {name}/ folder")
    assert not bad, "\n  ".join([""] + bad)


def test_no_doc_is_reachable_only_by_knowing_its_path():
    """The orphan check the folder indexes exist to satisfy. This is the assertion that
    keeps them honest as pages are added -- an index is only useful while it is complete."""
    linked = set()
    for rel, p in _docs():
        base = os.path.dirname(rel)
        for href in _LINK.findall(_read(p)):
            href = href.split("#")[0]
            if not href or href.startswith(("http://", "https://", "mailto:", "/")):
                continue
            linked.add(os.path.normpath(os.path.join(base, href)).replace("\\", "/"))
    for href in _LINK.findall(_read(_README)):
        href = href.split("#")[0]
        if href.startswith("docs/"):
            linked.add(href[len("docs/"):].rstrip("/"))
    orphans = sorted(rel for rel, _ in _docs()
                     if rel not in linked
                     and os.path.dirname(rel) not in linked
                     and os.path.basename(rel) != _INDEX)
    assert not orphans, (
        f"{len(orphans)} doc(s) that nothing links to:\n  " + "\n  ".join(orphans))


# ── docs_pages agrees with the tree ──────────────────────────────────────────

def test_the_docs_index_keeps_the_folder_indexes_out_of_its_item_lists():
    """Source-parsed, the idiom tests/test_persona_docs.py uses for this same module so it
    need not import fastapi. Without the skip, /docs lists an entry titled "Readme" for
    every folder, and docs/README.md lists as a link to a second copy of the index."""
    src = _read(_DOCS_PAGES)
    idx = src.split("async def doc_index", 1)[1].split("\n@router.", 1)[0]
    assert 'rel.name == "README"' in idx, (
        "doc_index no longer skips folder README.md files, so the /docs index lists one "
        '"Readme" entry per folder')


def test_every_indexed_section_has_a_heading_label():
    """A section is a directory path, so an unlabelled one renders
    <h2>profiles/demo/personas</h2> -- a path where the index wants a phrase."""
    src = _read(_DOCS_PAGES)
    sections = set(re.findall(
        r'"([^"]+)"', src.split("_INDEX_SECTIONS = {", 1)[1].split("}", 1)[0]))
    labels = set(re.findall(
        r'"([^"]+)":', src.split("_SECTION_LABELS = {", 1)[1].split("\n}", 1)[0]))
    missing = sorted(sections - labels)
    assert not missing, f"_SECTION_LABELS has no entry for: {missing}"


def test_every_indexed_section_is_a_real_folder():
    """A stale member is a section that silently never renders, which looks exactly like
    the folder being deliberately hidden."""
    src = _read(_DOCS_PAGES)
    sections = set(re.findall(
        r'"([^"]+)"', src.split("_INDEX_SECTIONS = {", 1)[1].split("}", 1)[0]))
    sections.discard("General")
    missing = sorted(s for s in sections if not os.path.isdir(os.path.join(_DOCS, s)))
    assert not missing, f"_INDEX_SECTIONS names folders that do not exist: {missing}"


def test_the_header_block_mints_no_anchor():
    """The only renderer-dependent check: rendered through the production renderer, the
    block must add no id= of its own to the page."""
    if _render_markdown is None:
        print(f"SKIP anchor check: renderer unavailable ({_IMPORT_ERR})")
        return
    sample = ("# A Page\n\n> **Audience:** operator · **Profile:** `both` · "
              "**Read this when:** something happens.\n\n## Real Heading\n")
    ids = set(re.findall(r'id="([^"]+)"', _render_markdown(sample)))
    assert ids == {"a-page", "real-heading"}, (
        f"the header block minted or displaced an anchor: {sorted(ids)}")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {fn.__name__}: {exc}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    raise SystemExit(1 if failures else 0)
