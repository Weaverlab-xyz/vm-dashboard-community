"""The Demo Cell architecture page, and the profile gate on the links that point at it.

An estate instance used to show "POV architecture and workflow" in Settings > Reference and
in the About box, though a POV is something it does not run. Now a POV instance links the
POV page and an estate instance links the Demo Cell page.

Pure where it can be: the template assertions parse source and import nothing; the
rendering one imports docs_pages, which needs only the markdown library.

Runs under pytest, or standalone:
    python tests/test_demo_cell_architecture_doc.py
"""
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-demo-cell-arch")

_DOC = os.path.join(_ROOT, "docs", "profiles", "demo", "demo-cell-architecture.md")
_HUB = os.path.join(_ROOT, "docs", "profiles", "demo", "README.md")
_SETTINGS = os.path.join(_ROOT, "web_dashboard", "templates", "settings.html")
_BASE = os.path.join(_ROOT, "web_dashboard", "templates", "base.html")

_POV_HREF = 'href="/docs/architecture"'
_DEMO_HREF = 'href="/docs/profiles/demo/demo-cell-architecture"'


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _branches(template):
    """Split the template's profile gate into (pov_branch, estate_branch)."""
    m = re.search(
        r"\{%\s*if install_profile == 'pov'\s*%\}(.*?)\{%\s*else\s*%\}(.*?)\{%\s*endif\s*%\}",
        template, re.S)
    assert m, "no `if install_profile == 'pov' ... else ... endif` around the architecture link"
    return m.group(1), m.group(2)


def test_the_page_exists_and_names_the_demo_profile():
    body = _read(_DOC)
    assert body.startswith("# Demo Cell architecture and workflow")
    assert "**Profile:** `demo`" in body.splitlines()[2]


def test_the_page_never_calls_an_install_a_demo_instance():
    assert "demo instance" not in _read(_DOC).lower()


def test_the_demo_hub_links_the_page():
    assert "(demo-cell-architecture.md)" in _read(_HUB)


def test_the_diagram_is_one_unbroken_figure():
    """python-markdown stashes <figure> verbatim, but a blank line ends the raw block."""
    body = _read(_DOC)
    fig = body[body.index("<figure"):body.index("</figure>")]
    assert "<svg" in fig and "</svg>" in fig
    assert "\n\n" not in fig, "a blank line inside the figure ends the raw-HTML block"
    assert "overflow-x:auto" in fig and "min-width:720px" in fig


def test_the_diagram_survives_the_docs_renderer():
    from web_dashboard.api import docs_pages
    html = docs_pages._render_markdown(_read(_DOC), "profiles/demo/demo-cell-architecture")
    assert "<svg" in html and "&lt;svg" not in html
    svg = html[html.index("<svg"):html.index("</svg>")]
    assert "<p>" not in svg, "markdown wrapped part of the SVG in a paragraph"


def test_settings_reference_card_is_gated_on_the_profile():
    pov, estate = _branches(_read(_SETTINGS))
    assert _POV_HREF in pov and _DEMO_HREF not in pov
    assert _DEMO_HREF in estate and _POV_HREF not in estate
    assert "POV architecture and workflow" in pov
    assert "Demo Cell architecture and workflow" in estate


def test_about_box_link_is_gated_on_the_profile():
    pov, estate = _branches(_read(_BASE)[_read(_BASE).index("about.api_version"):])
    assert _POV_HREF in pov and _DEMO_HREF not in pov
    assert _DEMO_HREF in estate and _POV_HREF not in estate


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("ok  ", name)
            except Exception as exc:  # noqa: BLE001
                failed += 1
                print("FAIL", name, "-", exc)
    sys.exit(1 if failed else 0)
