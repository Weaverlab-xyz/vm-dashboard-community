"""Every intra-repo link inside ``docs/`` must point at something that exists, and
every ``#fragment`` must land on a real heading — in the dashboard's own docs viewer,
not just on GitHub.

The two renderers used to disagree. Docs are authored and reviewed on GitHub, so
anchors get written GitHub's way; ``/docs/<page>`` renders them with
python-markdown, whose stock ``toc`` slugifier collapses a run of separators to a
single hyphen where GitHub emits one per separator::

    ### OCI (Autonomous Database) — read the caveats
    GitHub          #oci-autonomous-database--read-the-caveats
    python-markdown #oci-autonomous-database-read-the-caveats

Thirty links were written the first way and rendered the second, so clicking one
in-app scrolled nowhere and left you at the top of the page. Nothing failed
loudly — a browser just ignores an anchor it can't resolve — which is why this
lasted as long as it did and why it needs a test rather than a fix alone.

``docs_pages._GitHubSlugger`` closes the gap, and this walks ``docs/`` through the
*production* renderer to keep it closed. Because that slugifier is a faithful port
of ``github-slugger``, a fragment that resolves here resolves on GitHub too, so
this doubles as a dead-link check for the repo as rendered on GitHub.

The existence half of this file covers links *without* a fragment too, which were
unchecked for a long time and are exactly as dead when wrong. ``notifications.md``
pointed the words "auto-delete timer" at ``../README.md`` — a file that exists, so
nothing was broken in the filesystem sense, but which documents no such thing. A
target-exists check can't catch that one; what it does catch is the far more common
version, a link to a doc that was renamed or never written at all.

Only inline ``[text](target)`` links exist in ``docs/`` today — no reference-style
definitions, no raw ``<a href>``. If either ever appears, widen ``_LINK``.

Skips cleanly when ``markdown`` isn't installed (the renderer degrades to a
``<pre>`` block with no ids at all, which would fail every assertion for the wrong
reason).

Run: python tests/test_docs_anchors.py   (or under pytest)
"""
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_DOCS = os.path.join(_ROOT, "docs")

try:
    import markdown  # noqa: F401  (presence check; the renderer imports it lazily)
    from web_dashboard.api.docs_pages import _GitHubSlugger, _render_markdown
except Exception as exc:  # markdown / fastapi absent outside CI
    _render_markdown = None
    _IMPORT_ERR = exc

_LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
_ID = re.compile(r'id="([^"]+)"')


def _docs():
    for dp, _, files in os.walk(_DOCS):
        for f in sorted(files):
            if f.endswith(".md"):
                p = os.path.join(dp, f)
                yield os.path.relpath(p, _ROOT).replace("\\", "/"), p


def _ids_by_doc():
    """{relpath: {anchor id, ...}} as the dashboard actually renders them."""
    return {rel: set(_ID.findall(_render_markdown(open(p, encoding="utf-8").read())))
            for rel, p in _docs()}



# ``[...](...)`` inside a code span is not a link in either renderer, and a doc that
# quotes a regex — `[a-z]([-_a-z0-9]*)?` — contains one by accident. Scanning raw text
# reported that as a link to a file named after the character class, which is a false
# positive no author can fix except by not documenting the regex.
_FENCED = re.compile(r"^```.*?^```", re.S | re.M)
_INLINE_CODE = re.compile(r"`[^`\n]*`")


def _strip_code(text: str) -> str:
    """Markdown with its code spans and fenced blocks removed.

    Deliberately narrow: it drops only what the renderers would show verbatim, so every
    real link is still checked. The alternative — a smarter link regex — would have to
    re-implement markdown's escaping rules to know the difference.
    """
    return _INLINE_CODE.sub("", _FENCED.sub("", text))


def _links():
    """(source, href, target_relpath, fragment) for every intra-repo link.

    ``fragment`` is ``""`` for a plain file link, and ``target`` is the source doc
    itself for a same-page ``#anchor`` — so the anchor check below reads naturally and
    the existence check below can't trip over one.

    A leading ``/`` is skipped along with the URL schemes: those are *app routes*
    (``/docs/integrations/entitle``), which the docs viewer serves and which are not
    filesystem paths. Resolving them against the repo root would fail a link that works
    in the product.
    """
    for rel, p in _docs():
        for href in _LINK.findall(_strip_code(open(p, encoding="utf-8").read())):
            if href.startswith(("http://", "https://", "mailto:", "/")):
                continue
            path, _, frag = href.partition("#")
            target = rel if not path else os.path.normpath(
                os.path.join(os.path.dirname(rel), path)).replace("\\", "/")
            yield rel, href, target, frag


def test_every_docs_anchor_link_resolves_in_the_dashboard_renderer():
    ids = _ids_by_doc()
    broken = [f"{src} -> {href}" for src, href, target, frag in _links()
              if frag and target in ids and frag not in ids[target]]
    assert not broken, (
        f"{len(broken)} anchor link(s) don't resolve in /docs — the reader lands at "
        "the top of the page instead of the section:\n  " + "\n  ".join(broken))


def test_docs_links_point_at_files_that_exist():
    """A link to a missing file is dead in both renderers, and the check above can't
    see it — that one only validates targets it was able to render.

    Fragment or not: a bare ``[text](renamed-doc.md)`` is just as broken, and skipping
    those is how ``docs/multi-tenancy-execution-plan.md`` stayed linked from
    ``design/entitle-user-jit.md`` while never having existed in the repo at all.

    A handful of links reach outside ``docs/`` (``../CONTRIBUTING.md#…``, a
    ``#L15`` line anchor into a template) and some point at directories
    (``integrations/``, ``../scripts/sandbox/Linux``). Those resolve on GitHub, and a
    fragment among them is dead in the in-app viewer either way since ``/docs`` only
    serves ``docs/*.md`` — so check the path is there and leave the fragment alone."""
    missing = sorted({f"{src} -> {href}" for src, href, target, _ in _links()
                      if not os.path.exists(os.path.join(_ROOT, target))})
    assert not missing, (
        f"{len(missing)} link(s) to a file that isn't there:\n  " + "\n  ".join(missing))


# ── the slugifier itself ──────────────────────────────────────────────────────
# Pinned against real github-slugger output, so a well-meaning "simplify this
# regex" can't quietly restore the collapsing behaviour.

def test_slugifier_emits_one_hyphen_per_separator_like_github():
    slug = _GitHubSlugger()
    cases = {
        # the em dash: deleted, and the spaces that flanked it each become a hyphen
        "OCI (Autonomous Database) — read the caveats":
            "oci-autonomous-database--read-the-caveats",
        # ampersand, same shape
        "Kubernetes-cluster & cloud-database targets (localhost runs)":
            "kubernetes-cluster--cloud-database-targets-localhost-runs",
        # plus sign, and a hyphen that is part of a word survives untouched
        "6.7 Audit trail + agent-revoke sweeper":
            "67-audit-trail--agent-revoke-sweeper",
        # slash: deleted like any other punctuation, hyphens come from the spaces
        "AWS / Systems Manager": "aws--systems-manager",
        "either/or": "eitheror",
        # parentheses vanish without leaving a separator behind
        "Part D — Run the dashboard": "part-d--run-the-dashboard",
    }
    for heading, expected in cases.items():
        got = slug(heading, "-")
        assert got == expected, f"{heading!r} -> {got!r}, expected {expected!r}"


def test_slugifier_dedupes_with_a_hyphen_not_an_underscore():
    """python-markdown's own de-duplication appends ``_1``; GitHub appends ``-1``.
    ONBOARDING.md has nine "Enable the integration" headings, so this is the
    difference between eight working anchors and eight broken ones."""
    slug = _GitHubSlugger()
    assert [slug("Prerequisites", "-") for _ in range(3)] == [
        "prerequisites", "prerequisites-1", "prerequisites-2"]


def test_each_page_gets_a_fresh_dedupe_counter():
    """The counter is per-document. A shared slugifier would number headings by how
    many pages had been served since the last restart."""
    assert _GitHubSlugger()("Prerequisites", "-") == "prerequisites"
    assert _GitHubSlugger()("Prerequisites", "-") == "prerequisites"


def test_the_renderer_actually_uses_the_github_slugifier():
    """Rendering through the real entry point — the extension list is easy to
    "tidy" back to a plain ``"toc"``, which would silently undo all of this."""
    html = _render_markdown("## Alpha — beta\n\ntext\n")
    assert 'id="alpha--beta"' in html, html


if __name__ == "__main__":
    if _render_markdown is None:
        print(f"SKIP all: renderer unavailable ({_IMPORT_ERR})")
        sys.exit(0)
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
    sys.exit(1 if failures else 0)
