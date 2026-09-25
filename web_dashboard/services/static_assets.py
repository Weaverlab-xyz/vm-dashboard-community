"""Cache-busting URLs for the files under ``web_dashboard/static``.

Written after a deploy that could not be un-deployed from the browser's side.

``26.10.20`` shipped a ``static/js/app.js`` with a syntax error. The browser discards a
whole script block on a SyntaxError, so `app.js` defined nothing and every page in the
product rendered its static header and then threw on every Alpine expression -- the
failure class ``tests/test_template_scripts.py`` exists to catch. The fix was deployed
within the hour and the server served the corrected file. **Browsers kept running the
broken one.** The template asked for a bare ``/static/js/app.js`` with no version, and the
response carried no ``Cache-Control`` at all -- only ``etag`` and ``last-modified``. With
no explicit freshness, a browser is free to invent one (RFC 9111 §4.2.2 heuristic
freshness, commonly 10% of the Last-Modified age), and Chrome did: it reused its cached
copy without so much as a conditional request. Verified live on dash.weaverlab.app
2026-09-25 -- the only thing that shifted it was `fetch(url, {cache:'reload'})` from the
console. A returning user's only recourse was a hard reload they had no way to know they
needed.

The fix is to put the file's identity in the URL. ``url("js/app.js")`` renders
``/static/js/app.js?v=<digest>``, where the digest is over the file's BYTES, so:

* a deploy that changes the file changes every page's reference to it, and no browser can
  serve the old bytes for the new URL -- there is no old URL left to serve them for;
* a deploy that does NOT change the file changes nothing, and the cache still hits;
* every replica computes the same value from the same image, so a user bouncing between
  ACA replicas mid-session does not thrash their cache. A build timestamp or a boot-time
  random would fail exactly that case, which is why this is a content hash and not either
  of those.

A digest and not ``DASHBOARD_GIT_SHA`` for a related reason: the git sha is empty in a
source checkout (see :mod:`.build_provenance`), which is precisely where a developer is
editing this file every few minutes and most needs the busting to work.

The other half of the fix is :func:`cache_control` -- the header those URLs are now safe
to carry -- applied by ``main._VersionedStaticFiles``. The guard that keeps a future
template from reintroducing a bare reference is ``tests/test_static_assets.py``.

Stdlib only (the header policy lives here rather than in ``main`` so a test can exercise
it without importing the FastAPI app), and never raises: a missing or unreadable asset
must cost the page its cache busting, not the render.
"""
from __future__ import annotations

import hashlib
import logging
import os
import threading
from urllib.parse import parse_qs

logger = logging.getLogger(__name__)

STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "static")

#: Query parameter carrying the digest. Named in one place because three things have to
#: agree on it: the URL built here, the header logic in ``main``, and the test.
VERSION_PARAM = "v"

#: Hex characters of sha256 kept. 12 is what ``build_provenance.describe`` shows and what
#: git uses for an abbreviated object; collisions are not a security boundary here -- the
#: worst a collision could do is fail to bust a cache, which is where we started.
_DIGEST_LEN = 12

#: What a pinned URL is allowed to promise. Matches the brand-logo route in
#: api/setup.py, which is content-addressed for the same reason.
IMMUTABLE = "public, max-age=31536000, immutable"

#: What an unpinned one is. `no-cache` is not `no-store`: the browser may still keep the
#: bytes and still gets a cheap 304 off the etag. What it may not do is the thing Chrome
#: did during the incident -- reuse them without asking.
REVALIDATE = "no-cache"

# rel_path -> (mtime_ns, size, digest). The stat is the cache KEY, not just a staleness
# hint: `uvicorn --reload` restarts the process on a Python edit but NOT on a JS one, so a
# digest cached for the process lifetime would go stale in the one workflow that edits
# this file most. A stat per render is a few microseconds and nothing else in
# `_profile_context` is cheaper than that.
#
# The corollary: an edit that changes neither mtime nor size reads as no edit. That is a
# `cp -p` or a same-second same-length overwrite, not anything a deploy does -- the image
# build gives every file a fresh mtime.
_cache: dict[str, tuple[int, int, str]] = {}
_lock = threading.Lock()


def _digest(full_path: str) -> str:
    h = hashlib.sha256()
    with open(full_path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()[:_DIGEST_LEN]


def version(rel_path: str) -> str:
    """The digest for a path relative to the static root, or ``""``.

    ``""`` means "could not read it" -- a missing file, a permission error, a directory.
    Callers render an unversioned URL in that case rather than a URL with an empty or
    invented version, because a wrong digest is worse than none: it would pin the browser
    to whatever bytes it first saw at that URL.
    """
    rel = rel_path.strip("/")
    full = os.path.join(STATIC_DIR, *rel.split("/"))
    try:
        st = os.stat(full)
    except OSError as exc:
        logger.debug("static_assets: cannot stat %s: %s", rel, exc)
        return ""
    key = (st.st_mtime_ns, st.st_size)
    cached = _cache.get(rel)
    if cached is not None and cached[:2] == key:
        return cached[2]
    try:
        digest = _digest(full)
    except OSError as exc:
        logger.debug("static_assets: cannot hash %s: %s", rel, exc)
        return ""
    with _lock:
        _cache[rel] = (key[0], key[1], digest)
    return digest


def url(rel_path: str) -> str:
    """``/static/<rel_path>?v=<digest>`` -- what templates call.

    Registered as the Jinja global ``static_url``. Every template reference to a static
    asset goes through this; ``tests/test_static_assets.py`` fails the build if one does
    not.
    """
    rel = rel_path.strip("/")
    digest = version(rel)
    return f"/static/{rel}?{VERSION_PARAM}={digest}" if digest else f"/static/{rel}"


def cache_control(query_string: bytes) -> str:
    """The ``Cache-Control`` for a request to the static mount, from its raw query.

    A URL carrying a non-empty ``v`` is pinned to a set of bytes and can be cached
    forever; anything else must be revalidated. An unpinned request is reached by a
    bookmark, a probe, or a template that skipped :func:`url` -- fail safe there rather
    than let the operator discover it the way we did last time.

    Parsed rather than a substring test, because ``v=`` is a substring of ``rev=`` and
    ``nav=``, and treating one of those as a pin would freeze an unpinned URL in every
    browser for a year.
    """
    query = parse_qs((query_string or b"").decode("latin-1"))
    pinned = any(query.get(VERSION_PARAM) or ())
    return IMMUTABLE if pinned else REVALIDATE
