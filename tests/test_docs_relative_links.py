"""A relative link between two docs has to work in the dashboard's own viewer, not just
on GitHub.

``docs/`` is authored and reviewed on GitHub, so a cross-reference is written the way
GitHub resolves it -- ``[Cloud VMs](cloud-vms.md)``, or ``../../cloud-vms.md`` from a
nested page. Nothing rewrote those and ``_SHELL`` has no ``<base>``, so the browser
resolved them against the current route: ``/docs/cloud-vms.md``, which makes ``doc_page``
append its own suffix, look for ``docs/cloud-vms.md.md``, and 404. Every relative link in
the tree -- 500-odd of them -- was dead in this viewer while working perfectly on GitHub.

That is the same split ``tests/test_app_docs_links.py`` was written about, one layer down,
and it has the same signature: nothing fails loudly, a browser just navigates to a 404 and
strands the reader who was following the instructions a page told them to follow.

``tests/test_docs_anchors.py`` resolves these links against the filesystem the way GitHub
does, so it already proves the *targets* exist. This file pins that the rendered ``href``
agrees with them, and that the rewrite leaves alone everything that is already correct:
external URLs, app routes like ``/settings``, and same-page fragments.

It also pins the folder fallback. Nine folders under ``docs/`` carry a ``README.md`` index,
which GitHub serves as the folder's landing page; without the fallback ``/docs/profiles/pov``
404s and the hub is reachable only at ``/docs/profiles/pov/README`` -- mixed case, on a
case-sensitive container filesystem, which is a link that passes a check on Windows and
404s in the image.

Skips cleanly when markdown / fastapi aren't installed, matching the other docs tests.

Run: python tests/test_docs_relative_links.py   (or under pytest)
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

os.environ.setdefault("JWT_SECRET_KEY", "test-secret-docs-rel-links")

try:
    import markdown  # noqa: F401  (presence check; the renderer imports it lazily)
    from web_dashboard.api.docs_pages import _render_markdown
except Exception as exc:  # markdown / fastapi absent outside CI
    _render_markdown = None
    _IMPORT_ERR = exc

# A nested page, so a link that climbs out of it exercises the depth arithmetic rather
# than a same-directory no-op.
_PAGE = "profiles/demo/ot-demo-cell"


def _render(md, page=_PAGE):
    return _render_markdown(md, page=page)


def _hrefs(html):
    import re
    return re.findall(r'<a href="([^"]+)"', html)


def test_a_relative_doc_link_becomes_a_docs_route():
    assert _hrefs(_render("[Cloud VMs](../../cloud-vms.md)")) == ["/docs/cloud-vms"]


def test_a_same_directory_link_resolves_against_the_page_not_the_root():
    """The bug this catches is an off-by-one in the base: resolving against ``docs/``
    instead of the page's own folder sends every sibling link to the wrong section."""
    assert _hrefs(_render("[OT](ot-demo-cell.md)")) == ["/docs/profiles/demo/ot-demo-cell"]


def test_the_fragment_survives_the_rewrite():
    """The fragment is the whole value of a deep link. Dropping it silently leaves the
    reader at the top of a 700-line page, which is the failure test_docs_anchors exists
    for -- reintroducing it in the renderer would be a fine joke."""
    html = _render("[k8s](../../integrations/password-safe.md#kubernetes-serviceaccount-token-rotation)")
    assert _hrefs(html) == [
        "/docs/integrations/password-safe#kubernetes-serviceaccount-token-rotation"]


def test_a_directory_link_lands_on_that_folders_index():
    """``[Use cases](personas/)`` is a real link in the tree. os.path.exists() accepts a
    directory, so it passes a target-exists check while 404ing in-app; the folder README
    fallback in doc_page is what makes it resolve."""
    assert _hrefs(_render("[Use cases](personas/)")) == ["/docs/profiles/demo/personas"]


def test_links_that_are_already_correct_are_left_alone():
    for href, md in (
        ("https://example.com/x.md", "[x](https://example.com/x.md)"),
        ("http://example.com/y.md",  "[y](http://example.com/y.md)"),
        ("mailto:a@b.c",             "[m](mailto:a@b.c)"),
        ("/settings",                "[app](/settings)"),
        ("#the-gate",                "[same](#the-gate)"),
    ):
        assert _hrefs(_render(md)) == [href], f"{md} was rewritten to {_hrefs(_render(md))}"


def test_a_link_that_climbs_out_of_docs_is_left_as_authored():
    """It is a broken link either way; inventing a route for it would turn a visible 404
    into a link that points confidently at the wrong page."""
    assert _hrefs(_render("[esc](../../../../README.md)")) == ["../../../../README.md"]


def test_a_non_doc_relative_link_is_not_touched():
    """Only ``*.md`` and directories are docs routes. An image or a repo file is not."""
    assert _hrefs(_render("[cfg](../../../examples/k8s/app-secret.yaml)")) == [
        "../../../examples/k8s/app-secret.yaml"]


def test_rendering_without_a_page_leaves_hrefs_untouched():
    """The parameter defaults to None so the two existing callers -- test_docs_anchors and
    test_app_docs_links -- keep the call shape they were written with. If this ever
    rewrote by default, those suites would start checking the app's hrefs against
    GitHub's semantics and disagree with themselves."""
    assert _hrefs(_render_markdown("[Cloud VMs](../../cloud-vms.md)")) == [
        "../../cloud-vms.md"]


def test_the_heading_ids_still_come_from_the_github_slugifier():
    """The rewrite registers a treeprocessor next to toc's. Ordering them wrongly is how
    you lose the anchors instead of the links."""
    html = _render("## OCI (Autonomous Database) — read the caveats")
    assert 'id="oci-autonomous-database--read-the-caveats"' in html


def test_a_folder_url_serves_that_folders_readme():
    """The other half of the fallback, through the real route."""
    try:
        from fastapi.testclient import TestClient
        from web_dashboard.main import app
        from web_dashboard.services import config_service
    except Exception as exc:
        print(f"SKIP folder-url check: fastapi absent ({exc})")
        return
    import pathlib
    docs = pathlib.Path(_ROOT) / "docs"
    folders = sorted(p.parent.relative_to(docs).as_posix()
                     for p in docs.rglob("README.md") if p.parent != docs)
    if not folders:
        print("SKIP folder-url check: no folder README.md exists yet")
        return
    c = TestClient(app)
    c.__enter__()
    config_service.set("setup_complete", "1")
    config_service._setup_complete = True
    for folder in folders:
        r = c.get(f"/docs/{folder}")
        assert r.status_code == 200, f"/docs/{folder} returned {r.status_code}, not its README"


def test_every_section_heading_on_the_index_resolves():
    """The /docs index hangs each section's heading off that folder's README, so those
    hrefs are the most-clicked links on the page. They are also the easiest to get wrong:
    "General" is a synthetic section name rather than a directory, so emitting the section
    string for it produced /docs/General -- a 404 at the top of the index."""
    try:
        from fastapi.testclient import TestClient
        from web_dashboard.main import app
        from web_dashboard.services import config_service
    except Exception as exc:
        print(f"SKIP index-heading check: fastapi absent ({exc})")
        return
    import re
    c = TestClient(app)
    c.__enter__()
    config_service.set("setup_complete", "1")
    config_service._setup_complete = True
    index = c.get("/docs")
    assert index.status_code == 200
    hrefs = re.findall(r'<h2><a href="([^"]+)"', index.text)
    assert hrefs, "no section heading links on the /docs index"
    for href in hrefs:
        assert c.get(href).status_code == 200, f"{href} on the /docs index 404s"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    if _render_markdown is None:
        print(f"SKIP all: markdown/fastapi not importable ({_IMPORT_ERR})")
        raise SystemExit(0)
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
