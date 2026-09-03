"""Every ``/docs/...`` link the *product* renders must resolve — the page must
exist and any ``#fragment`` must land on a real heading.

``test_docs_anchors.py`` walks ``docs/`` and deliberately skips any href starting
with ``/``, because those are app routes rather than filesystem paths::

    if href.startswith(("http://", "https://", "mailto:", "/")):
        continue

That was the right call there and it left a hole: the links an operator actually
clicks are written in ``web_dashboard/templates/``, which nothing walked at all,
and they are *all* app routes. Three of them pointed at
``/docs/remote-agents.md`` — with the ``.md`` suffix. ``doc_page`` appends its
own, so the route looked for ``docs/remote-agents.md.md`` and returned 404 on
every click, from the Remote Agents page banner, its policy-file modal, and the
Settings panel warning about terminating TLS.

Nothing failed loudly, which is the whole problem: a dead docs link doesn't break
a page, it just strands the reader who was trying to follow the setup
instructions the panel told them to read. That is indistinguishable, from the
outside, from the feature being undocumented.

The fragment half matters just as much and is easier to get wrong, because the
in-app slug is not the heading. ``## Layer 2 — Password Safe (AWS + Azure)``
renders as ``#layer-2--password-safe-aws--azure--gcp``; a plausible hand-written guess
like ``#password-safe-onboarding`` resolves to nothing and silently drops the
reader at the top of a 700-line page.

Skips cleanly when ``markdown`` isn't installed, matching ``test_docs_anchors``:
the renderer degrades to a ``<pre>`` block with no ids, which would fail every
assertion for the wrong reason.

Run: python tests/test_app_docs_links.py   (or under pytest)
"""
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_DOCS = os.path.join(_ROOT, "docs")
_TEMPLATES = os.path.join(_ROOT, "web_dashboard", "templates")

try:
    import markdown  # noqa: F401  (presence check; the renderer imports it lazily)
    from web_dashboard.api.docs_pages import _render_markdown
except Exception as exc:  # markdown / fastapi absent outside CI
    _render_markdown = None
    _IMPORT_ERR = exc

# href="/docs/…" in a template, and [text](/docs/…) in a doc. Single quotes and
# Jinja-interpolated hrefs don't occur today; if either appears, widen these.
_HREF = re.compile(r'href="(/docs/[^"]*)"')
_MD_LINK = re.compile(r"\]\((/docs/[^)]+)\)")
_ID = re.compile(r'id="([^"]+)"')


def _sources():
    """(relpath, text) for every file that can emit an app-route docs link."""
    for root, pattern in ((_TEMPLATES, ".html"), (_DOCS, ".md")):
        for dp, _, files in os.walk(root):
            for f in sorted(files):
                if not f.endswith(pattern):
                    continue
                p = os.path.join(dp, f)
                yield (os.path.relpath(p, _ROOT).replace("\\", "/"),
                       open(p, encoding="utf-8").read())


def _links():
    """(source, href, page, fragment) for every ``/docs/...`` link in the app.

    ``page`` is the route path with the leading ``/docs/`` stripped — i.e. what
    ``doc_page`` receives and appends ``.md`` to.
    """
    for rel, text in _sources():
        finder = _HREF if rel.endswith(".html") else _MD_LINK
        for href in finder.findall(text):
            path, _, frag = href.partition("#")
            yield rel, href, path[len("/docs/"):].strip("/"), frag


def _resolve(page):
    """The file ``doc_page`` would serve for ``/docs/<page>``, or None for a 404.

    Mirrors the route, including the folder fallback: /docs/profiles/pov serves that
    folder's README.md, which is what lets a link name a directory the way it does on
    GitHub. Checking only ``<page>.md`` here would fail every folder URL in the app while
    the route serves them fine."""
    for candidate in (os.path.join(_DOCS, f"{page}.md"),
                      os.path.join(_DOCS, page, "README.md")):
        if os.path.isfile(candidate):
            return candidate
    return None


def _rendered_ids(page):
    """Anchor ids for the page as the dashboard actually renders it."""
    with open(_resolve(page), encoding="utf-8") as fh:
        return set(_ID.findall(_render_markdown(fh.read())))


def test_every_app_docs_link_resolves_to_a_page():
    """``doc_page`` appends ``.md`` itself, so a link that already carries the
    suffix 404s. So does a link to a doc that was renamed or never written."""
    broken = sorted({
        f"{src} -> {href}" for src, href, page, _ in _links()
        if not page or _resolve(page) is None})
    assert not broken, (
        f"{len(broken)} link(s) to a /docs page that 404s:\n  " + "\n  ".join(broken))


def test_every_app_docs_fragment_lands_on_a_real_heading():
    """A fragment the renderer never emits leaves the reader at the top of the
    page with no error — see the module docstring for how the slugs differ."""
    broken = []
    for src, href, page, frag in _links():
        if not frag or _resolve(page) is None:
            continue  # the page check above owns that failure
        if frag not in _rendered_ids(page):
            broken.append(f"{src} -> {href}")
    assert not broken, (
        f"{len(broken)} anchor(s) don't resolve in /docs — the reader lands at the "
        "top of the page instead of the section:\n  " + "\n  ".join(sorted(broken)))


def test_the_suite_is_actually_looking_at_the_templates():
    """The globs above are the kind of thing that silently stops matching after a
    directory move, and an empty sweep passes both tests above."""
    srcs = {src for src, _, _, _ in _links()}
    assert any(s.startswith("web_dashboard/templates/") for s in srcs), (
        "no /docs links found in web_dashboard/templates — the sweep is looking "
        "in the wrong place")


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
