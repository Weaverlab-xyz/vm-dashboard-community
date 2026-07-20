"""Serve the in-repo ``docs/*.md`` as rendered HTML at ``/docs/<page>``.

Backs the "guide" links in Settings (Action Guardrails, and the integration
guides) so an operator doesn't need the GitHub repo open. Public + read-only;
renders Markdown server-side (no CDN, works air-gapped).

``/docs`` is now wholly the documentation browser: an index at ``/docs`` plus the
rendered pages beneath it. The API explorer moved to ``/swagger`` (see main.py) —
previously FastAPI owned the exact ``/docs`` path, which made the two collide
confusingly.
"""
import html as _html
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse


def _render_markdown(text: str) -> str:
    """Render Markdown → HTML. Imported lazily so a missing ``markdown`` lib can't
    crash app startup — the docs renderer is non-essential; it degrades to a
    readable <pre> fallback rather than taking the whole dashboard down."""
    try:
        import markdown as _md
        return _md.markdown(text, extensions=["fenced_code", "tables", "toc", "sane_lists"])
    except ModuleNotFoundError:
        return f"<pre>{_html.escape(text)}</pre>"

router = APIRouter(tags=["docs"])

# repo_root/docs (parents[2] of this file is the app root; docs/ sits beside it,
# and is COPYed into the image — see Dockerfile).
_DOCS_DIR = (Path(__file__).resolve().parents[2] / "docs").resolve()

# Only these sections are surfaced on the /docs index. Other subdirectories
# (design/, notes/, runbooks/) still render if you know the path — they're just
# internal enough that we don't want them cluttering the operator-facing index.
# "General" is the synthetic name for docs that live at the docs/ root.
_INDEX_SECTIONS = {"General", "integrations"}

_SHELL = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} · Docs</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ margin:0; background:#f8fafc; color:#0f172a;
         font:16px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }}
  main {{ max-width:820px; margin:0 auto; padding:2.5rem 1.25rem 4rem; }}
  a {{ color:#2563eb; }}
  h1,h2,h3 {{ line-height:1.25; margin-top:2rem; }}
  h1 {{ font-size:1.9rem; }} h2 {{ font-size:1.4rem; }} h3 {{ font-size:1.15rem; }}
  code {{ background:#eef2f7; padding:.1em .35em; border-radius:4px; font-size:.9em; }}
  pre {{ background:#0f172a; color:#e2e8f0; padding:1rem; border-radius:8px; overflow:auto; }}
  pre code {{ background:none; padding:0; color:inherit; }}
  table {{ border-collapse:collapse; width:100%; margin:1rem 0; }}
  th,td {{ border:1px solid #e2e8f0; padding:.5rem .7rem; text-align:left; vertical-align:top; }}
  th {{ background:#f1f5f9; }}
  blockquote {{ border-left:3px solid #cbd5e1; margin:1rem 0; padding:.25rem 1rem; color:#475569; }}
  .back {{ font-size:.85rem; }}
</style></head>
<body><main>
<p class="back"><a href="/settings">← Back to dashboard</a></p>
{body}
</main></body></html>"""


@router.get("/docs", response_class=HTMLResponse)
async def doc_index() -> HTMLResponse:
    """Index of every shipped doc, grouped by directory.

    Without this, ``/docs`` 404s and the guides are only reachable if you already
    know the exact path — which was the whole discoverability problem.
    """
    if not _DOCS_DIR.is_dir():
        raise HTTPException(status_code=404, detail="docs directory not found")

    groups: dict = {}
    for path in sorted(_DOCS_DIR.rglob("*.md")):
        rel = path.relative_to(_DOCS_DIR).with_suffix("")
        section = str(rel.parent).replace("\\", "/")
        section = "General" if section == "." else section
        if section not in _INDEX_SECTIONS:
            continue
        title = rel.name.replace("-", " ").replace("_", " ").title()
        groups.setdefault(section, []).append((title, str(rel).replace("\\", "/")))

    parts = ["<h1>Documentation</h1>",
             '<p>Shipped with this build. The API explorer lives at '
             '<a href="/swagger">/swagger</a>.</p>']
    for section in sorted(groups):
        parts.append(f"<h2>{_html.escape(section)}</h2><ul>")
        for title, href in groups[section]:
            parts.append(f'<li><a href="/docs/{_html.escape(href)}">{_html.escape(title)}</a></li>')
        parts.append("</ul>")
    return HTMLResponse(_SHELL.format(title="Documentation", body="".join(parts)))


@router.get("/docs/{page:path}", response_class=HTMLResponse)
async def doc_page(page: str) -> HTMLResponse:
    """Render ``docs/<page>.md``. 404 if it's missing or escapes the docs dir."""
    rel = page.strip("/")
    if not rel:
        raise HTTPException(status_code=404, detail="doc not found")
    candidate = (_DOCS_DIR / f"{rel}.md").resolve()
    # Path-traversal guard: the resolved file must live under docs/.
    try:
        candidate.relative_to(_DOCS_DIR)
    except ValueError:
        raise HTTPException(status_code=404, detail="doc not found")
    if not candidate.is_file():
        raise HTTPException(status_code=404, detail="doc not found")

    html = _render_markdown(candidate.read_text(encoding="utf-8"))
    # Escape the page-derived title before reflecting it into the HTML shell —
    # it originates from the request path, so render it as text, not markup
    # (prevents reflected XSS; CodeQL py/reflective-xss).
    title = _html.escape(rel.rsplit("/", 1)[-1].replace("-", " ").replace("_", " ").title())
    return HTMLResponse(_SHELL.format(title=title, body=html))
