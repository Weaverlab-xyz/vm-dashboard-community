"""Where a demo cell's code lives now, for tests that read it as text.

The OT cell forms and the network cell form used to be written out inline in each cloud
page (templates/<cloud>/index.html). They moved, so the Demo hub could render the same form
(see static/js/demo_cells.js):

  * the markup to templates/demo/_ot_cell_<cloud>.html and _net_cell.html (Jinja macros
    the page imports), and
  * the logic to static/js/demo_cells.js (otCellMixin / netCellMixin, spread into the page).

A test that asks "does the GCP page's OT form still do X" should read all three, which is
what :func:`cell_page_source` returns.
"""
import os

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_WEB = os.path.join(_ROOT, "web_dashboard")


def _read(*parts):
    with open(os.path.join(_WEB, *parts), encoding="utf-8") as fh:
        return fh.read()


def demo_cells_js():
    return _read("static", "js", "demo_cells.js")


def cell_page_source(cloud):
    """The cloud page, its cell partials, and the shared cell logic, as one string."""
    parts = [_read("templates", cloud, "index.html"),
             _read("templates", "demo", f"_ot_cell_{cloud}.html")]
    if cloud == "gcp":
        parts.append(_read("templates", "demo", "_net_cell.html"))
    parts.append(demo_cells_js())
    return "\n".join(parts)
