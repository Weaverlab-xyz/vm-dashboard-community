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
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse

# Everything GitHub's slugger deletes outright: anything that isn't a word
# character, a literal ASCII hyphen, or a space.
_SLUG_STRIP_RE = re.compile(r"[^\w\- ]", re.UNICODE)


class _GitHubSlugger:
    """Heading → anchor id, the way GitHub does it rather than the way
    python-markdown does it by default.

    The stock ``toc`` slugifier collapses a *run* of separators down to one
    hyphen, so ``## OCI (Autonomous Database) — read the caveats`` becomes
    ``#oci-autonomous-database-read-the-caveats`` here but
    ``#oci-autonomous-database--read-the-caveats`` on GitHub. Every anchor link
    in ``docs/`` is authored and checked against GitHub, so that one-character
    difference silently dumped the reader at the top of the page instead of at
    the section — 30 links' worth.

    GitHub (``github-slugger``) lowercases, *deletes* punctuation, turns spaces
    into hyphens, and de-duplicates with a ``-1``/``-2`` suffix. Deleting rather
    than replacing is the whole trick: `` — `` is space-nothing-space, which
    leaves two spaces and therefore two hyphens.

    Construct one per rendered page — the de-duplication counter is per-document,
    exactly like ``new GithubSlugger()`` is per file.

    Known, deliberate divergence: github-slugger keeps a bare variation selector
    (U+FE0F) when it strips the emoji in front of it, minting an anchor with an
    invisible character in it. We drop it. Headings are checked by
    ``tests/test_docs_anchors.py``, which will fail if a doc ever depends on one.
    """

    def __init__(self) -> None:
        self._seen: dict[str, int] = {}

    def __call__(self, value: str, separator: str) -> str:
        slug = _SLUG_STRIP_RE.sub("", value.lower()).replace(" ", separator)
        original = slug
        while slug in self._seen:
            self._seen[original] += 1
            slug = f"{original}{separator}{self._seen[original]}"
        self._seen[slug] = 0
        return slug


def _render_markdown(text: str) -> str:
    """Render Markdown → HTML. Imported lazily so a missing ``markdown`` lib can't
    crash app startup — the docs renderer is non-essential; it degrades to a
    readable <pre> fallback rather than taking the whole dashboard down."""
    try:
        import markdown as _md
        from markdown.extensions.toc import TocExtension
        return _md.markdown(text, extensions=[
            "fenced_code", "tables", "sane_lists",
            TocExtension(slugify=_GitHubSlugger()),
        ])
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
<title>{title} · {brand} Docs</title>
<link rel="icon" href="{favicon}">
<style>
  :root {{ color-scheme: light dark; }}
  body {{ margin:0; background:{body_bg}; color:#0f172a;
         font:16px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }}
  header {{ background:{nav_bg}; color:#fff; }}
  header .lockup {{ max-width:820px; margin:0 auto; padding:.85rem 1.25rem;
                    display:flex; align-items:center; gap:.6rem; }}
  header .brand {{ font-size:1.05rem; font-weight:700; letter-spacing:-.01em; }}
  header .slash {{ color:{nav_weft}; font-weight:300; }}
  header .product {{ font-size:.85rem; opacity:.9; }}
  .rail {{ height:4px; background:{rail}; }}
  main {{ max-width:820px; margin:0 auto; padding:2.5rem 1.25rem 4rem; }}
  a {{ color:{link}; }}
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
<body>
<header><div class="lockup">
<svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke-width="2.2"
     stroke-linecap="round" aria-hidden="true">
<path d="{mark_warp}" stroke="{nav_warp}"/><path d="{mark_weft}" stroke="{nav_weft}"/></svg>
<span class="brand">{brand}</span><span class="slash">/</span><span class="product">{product}</span>
</div></header>{rail_el}
<main>
<p class="back"><a href="/settings">← Back to dashboard</a></p>
{body}
</main></body></html>"""


def _shell(title: str, body: str) -> str:
    """Wrap rendered Markdown in the branded shell.

    One helper rather than two direct ``.format()`` call sites: the shell now takes ten
    fields instead of two, and threading those through both callers by hand is how one of
    them ends up a version behind.

    The profile lookup is best-effort on purpose. This module's whole premise is that the
    docs renderer is non-essential and degrades to a readable ``<pre>`` fallback rather
    than taking the whole dashboard down -- ``install_profile()`` reads ``config_service``,
    so a database that is down or not yet migrated must yield an unbranded-but-readable
    page, never a 500. The env default is the same fallback ``feature_flags`` itself uses.

    Note ``/docs`` is public and unauthenticated, so this makes the profile name readable
    without signing in. That is deliberate, and already true of ``GET /api/features`` which
    serves ``install_profile`` to anyone -- but it is a decision, not an accident, and it
    would need revisiting if that endpoint were ever locked down.
    """
    from ..config import settings
    from ..services import ui_theme
    try:
        from ..services import feature_flags
        profile = feature_flags.install_profile()
    except Exception:
        profile = settings.install_profile
    theme = ui_theme.theme_for(profile, settings.app_env)
    rail = theme["hex"]["rail"]
    return _SHELL.format(
        title=title,
        body=body,
        brand=theme["brand"],
        product=theme["product"],
        favicon=theme["favicon"],
        mark_warp=theme["mark_warp_path"],
        mark_weft=theme["mark_weft_path"],
        rail=rail or "transparent",
        rail_el='<div class="rail"></div>' if rail else "",
        **{k: theme["hex"][k] for k in ("nav_bg", "nav_warp", "nav_weft", "body_bg", "link")},
    )


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
    return HTMLResponse(_shell("Documentation", "".join(parts)))


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
    return HTMLResponse(_shell(title, html))
