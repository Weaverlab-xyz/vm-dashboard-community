"""The operator's uploaded logo: bytes in, validated; bytes out, cached.

The third module in the branding split. ``services/ui_theme`` owns what the chrome looks
like and is a pure function of its arguments; ``services/branding`` owns where the
operator's choices are stored and is the only half that reads ``app_config``. This owns the
one branding value that is not a short string -- an image -- because bytes, magic-number
sniffing and a blob table have no business in either of the other two.

Storage is the ``brand_asset`` table, not ``app_config``; see that model's docstring for
why. Four short pointer rows in ``app_config`` (``brand_logo_etag``/``_mime``/``_w``/``_h``)
let the render path decide whether there is a logo at all without touching the blob, and
those are read by ``services/branding`` rather than here.

SECURITY, the whole of it in one place.

SVG is accepted, because a vector logo is what a brand team actually hands over and there
is no image library in requirements.txt to convert one for the operator. SVG is also
executable markup, so the exposure is managed by *how it is served*, never by trusting the
file:

  1. It is NEVER inlined into a document. ``_brand_mark.html`` renders an ``img`` element
     whose src points at the route below. In image context a browser runs no script, fires
     no event handler and fetches no external subresource. Inlining it, or piping it
     through Jinja's ``safe``, converts this feature into stored XSS -- which is why
     ``tests/test_brand_logo`` asserts on the partial's source text.
  2. The route sets its own hardening headers. There is no CSP middleware anywhere in this
     app, so those headers are the entire control and not decoration.
  3. :func:`validate_svg` REJECTS rather than strips. A regex sanitiser for SVG is the
     classic losing game; a 400 that names the problem cannot be half-right.
  4. Well-formedness is checked with the stdlib XML parser, which does not expand external
     entities, and the root element must actually be an ``svg``.
  5. The stored and served content type comes from :func:`sniff`, never from the client.
     A renamed file is rejected on its bytes.

GIF is deliberately absent: animated chrome, no upside. Pinned in the tests so the
omission reads as a decision rather than an oversight.
"""
import hashlib
import re
import xml.etree.ElementTree as ET
from datetime import datetime

# 512 KB. A logo is a logo; this is generous for a PNG wordmark at 2x and still small
# enough that the whole row fits comfortably in a single Postgres TOAST read.
_MAX_LOGO_BYTES = 512 * 1024

_SVG_MIME = "image/svg+xml"
_ALLOWED = ("image/png", "image/jpeg", "image/webp", _SVG_MIME)

# The one slot this module manages today. The table's primary key, so "one global logo" is
# a schema property rather than a convention.
_SLOT = "logo"

# Serving is a cache MISS by definition (the hash is in the URL and the response is
# immutable), but a cold fleet can still arrive together on the first page load after an
# upload. One process-local entry keyed on the digest is enough, and it can be held
# forever: a different upload has a different key.
_memo: tuple[str, bytes, str] | None = None


class RejectedLogo(Exception):
    """Upload refused. ``status`` is the HTTP code the route should return."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


# -- Sniffing ------------------------------------------------------------------------

def _png_size(data: bytes) -> tuple[int | None, int | None]:
    """Width and height out of the IHDR chunk, which is always first."""
    if len(data) < 24 or data[12:16] != b"IHDR":
        return (None, None)
    return (
        int.from_bytes(data[16:20], "big") or None,
        int.from_bytes(data[20:24], "big") or None,
    )


def _jpeg_size(data: bytes) -> tuple[int | None, int | None]:
    """Walk the marker segments to the first Start-Of-Frame.

    Returns ``(None, None)`` rather than raising on anything unexpected: the dimensions are
    a nicety (they reserve the right aspect ratio in the nav) and the columns are nullable
    precisely so that a file we cannot measure is still a file we can serve.
    """
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            return (None, None)
        marker = data[i + 1]
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            return (
                int.from_bytes(data[i + 7:i + 9], "big") or None,
                int.from_bytes(data[i + 5:i + 7], "big") or None,
            )
        i += 2 + int.from_bytes(data[i + 2:i + 4], "big")
    return (None, None)


def _webp_size(data: bytes) -> tuple[int | None, int | None]:
    """VP8X carries the canvas size; the other WebP flavours are not worth parsing."""
    if len(data) >= 30 and data[12:16] == b"VP8X":
        return (
            int.from_bytes(data[24:27], "little") + 1,
            int.from_bytes(data[27:30], "little") + 1,
        )
    return (None, None)


# A viewBox is the most reliable size an SVG carries -- width/height are often percentages
# or carry units, and neither is required to be present at all.
_VIEWBOX_RE = re.compile(
    rb"viewBox\s*=\s*[\"']\s*[-\d.]+[,\s]+[-\d.]+[,\s]+([\d.]+)[,\s]+([\d.]+)", re.I)
# Everything a browser skips before the root element: a BOM, whitespace, the XML
# declaration, a doctype, comments and processing instructions.
_SVG_PREAMBLE_RE = re.compile(rb"^(?:\xef\xbb\xbf|\s|<\?[^>]*\?>|<!--.*?-->|<!DOCTYPE[^>]*>)*",
                              re.I | re.S)


def _svg_size(data: bytes) -> tuple[int | None, int | None]:
    m = _VIEWBOX_RE.search(data[:4096])
    if not m:
        return (None, None)
    try:
        w, h = float(m.group(1)), float(m.group(2))
    except ValueError:
        return (None, None)
    return (int(w) or None, int(h) or None)


def _looks_like_svg(data: bytes) -> bool:
    stripped = _SVG_PREAMBLE_RE.sub(b"", data[:4096], count=1)
    return stripped[:4].lower().startswith(b"<svg")


def sniff(data: bytes) -> tuple[str, int | None, int | None] | None:
    """``(mime, width, height)`` from the bytes alone, or ``None`` if unrecognised.

    Magic numbers, not the filename and not the client's declared type. A ``.png`` that is
    really an HTML document has to fail here, because this value is what the route later
    sends back as ``Content-Type``.
    """
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return (("image/png",) + _png_size(data))
    if data.startswith(b"\xff\xd8\xff"):
        return (("image/jpeg",) + _jpeg_size(data))
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return (("image/webp",) + _webp_size(data))
    if _looks_like_svg(data):
        return ((_SVG_MIME,) + _svg_size(data))
    return None


# -- SVG validation ------------------------------------------------------------------

# Each pattern is a thing an SVG can do that an image must not. Named rather than combined
# so the rejection can say which one tripped -- an operator who gets "contains scripting"
# and no detail will just try again with the same file.
_SVG_BANNED = (
    (re.compile(rb"<\s*script", re.I), "a script element"),
    (re.compile(rb"<\s*iframe", re.I), "an iframe element"),
    (re.compile(rb"<\s*foreignObject", re.I), "a foreignObject element"),
    (re.compile(rb"<\s*!ENTITY", re.I), "an entity declaration"),
    (re.compile(rb"\son[a-z]+\s*=", re.I), "an inline event handler"),
    (re.compile(rb"javascript\s*:", re.I), "a javascript: URL"),
    (re.compile(rb"(?:xlink:)?href\s*=\s*[\"']\s*(?:https?:)?//", re.I),
     "a reference to an external URL"),
)


def validate_svg(data: bytes) -> None:
    """Raise :class:`RejectedLogo` unless this is an inert, well-formed SVG.

    Reject, never strip. Rewriting hostile markup into safe markup is a problem nobody
    solves with a regex, and a half-sanitised file that renders is worse than a clear
    refusal: the operator believes the logo is fine.
    """
    for pattern, what in _SVG_BANNED:
        if pattern.search(data):
            raise RejectedLogo(
                f"This SVG contains {what}, which cannot be served safely as an image. "
                "Export a flattened SVG with no scripts or external references, or upload "
                "a PNG instead."
            )
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise RejectedLogo(f"This file is not well-formed XML ({exc}).") from exc
    # Namespaced in any real SVG; the bare tag is tolerated because a hand-written file
    # that omits xmlns still renders in every browser.
    if root.tag not in ("{http://www.w3.org/2000/svg}svg", "svg"):
        raise RejectedLogo("The root element of this file is not an svg element.")


def sniff_and_validate(data: bytes, declared: str = "") -> tuple[str, int | None, int | None]:
    """The whole accept/reject decision. Returns the sniffed ``(mime, w, h)``.

    ``declared`` is the browser's ``Content-Type`` and is advisory: it is compared only so
    the error can say what the file actually is, which is the difference between an
    operator fixing a rename and an operator retrying the same upload.
    """
    if not data:
        raise RejectedLogo("The uploaded file is empty.")
    if len(data) > _MAX_LOGO_BYTES:
        raise RejectedLogo(
            f"The logo is {len(data) // 1024} KB. The limit is "
            f"{_MAX_LOGO_BYTES // 1024} KB.",
            status=413,
        )

    found = sniff(data)
    if found is None:
        raise RejectedLogo(
            "This file is not a PNG, JPEG, WebP or SVG image. (Animated GIFs are not "
            "accepted.)"
        )
    mime, width, height = found
    if mime not in _ALLOWED:  # pragma: no cover - sniff cannot return anything else today
        raise RejectedLogo(f"{mime} images are not accepted.")

    declared = (declared or "").split(";")[0].strip().lower()
    if declared and declared in _ALLOWED and declared != mime:
        raise RejectedLogo(
            f"This file was sent as {declared} but its contents are {mime}. "
            "Upload it with its real extension."
        )

    if mime == _SVG_MIME:
        if not svg_allowed():
            raise RejectedLogo("SVG uploads are disabled on this instance.", status=415)
        validate_svg(data)
    return (mime, width, height)


def svg_allowed() -> bool:
    """Whether this instance accepts SVG at all. On by default.

    An escape hatch for an operator whose threat model says no executable markup in the
    database, without forcing that policy on everyone who just wants their logo to look
    right. Read through ``config_service`` so it is settable with the same tooling as every
    other flag.
    """
    from . import config_service
    try:
        return config_service.get_bool("brand_logo_allow_svg", True)
    except Exception:
        return True


# -- Storage -------------------------------------------------------------------------

def store(data: bytes, mime: str, *, filename: str = "", width: int | None = None,
          height: int | None = None, username: str = "") -> str:
    """Upsert the logo row. Returns the sha256 hex digest, which becomes the URL segment."""
    # SQLite accepts a str into a BLOB column and Postgres does not, so this would be a
    # bug that passes every local test and fails only on a real deployment.
    assert isinstance(data, bytes), "logo payload must be bytes"

    from ..database import SessionLocal, BrandAsset
    digest = hashlib.sha256(data).hexdigest()
    db = SessionLocal()
    try:
        row = db.query(BrandAsset).filter(BrandAsset.slot == _SLOT).first()
        if row is None:
            row = BrandAsset(slot=_SLOT)
            db.add(row)
        row.content_type = mime
        row.data = data
        row.sha256 = digest
        row.byte_size = len(data)
        row.width = width
        row.height = height
        row.filename = (filename or "")[:255] or None
        row.uploaded_by = (username or "")[:128] or None
        row.updated_at = datetime.utcnow()
        db.commit()
    finally:
        db.close()
    _forget()
    return digest


def remove() -> None:
    """Drop the logo row. No-op when there isn't one."""
    from ..database import SessionLocal, BrandAsset
    db = SessionLocal()
    try:
        db.query(BrandAsset).filter(BrandAsset.slot == _SLOT).delete()
        db.commit()
    finally:
        db.close()
    _forget()


def _forget() -> None:
    global _memo
    _memo = None


def load(etag: str) -> tuple[bytes, str] | None:
    """``(bytes, mime)`` for this exact digest, or ``None``.

    Matched against the stored ``sha256`` rather than against the cached ``brand_logo_etag``
    pointer on purpose. The pointer lives in ``config_service``'s 5-second cache, and under
    more than one worker a sibling serves the previous value for up to that long -- so
    comparing against it would make the *new* hash 404 right after an upload, which is the
    request that matters. Comparing against the row makes a *stale* hash 404 instead, which
    is a briefly missing image on a page that is already out of date.
    """
    global _memo
    if not re.fullmatch(r"[0-9a-f]{64}", etag or ""):
        return None
    memo = _memo
    if memo and memo[0] == etag:
        return (memo[1], memo[2])

    from ..database import SessionLocal, BrandAsset
    db = SessionLocal()
    try:
        row = db.query(BrandAsset).filter(BrandAsset.slot == _SLOT).first()
        if row is None or row.sha256 != etag:
            return None
        payload = (bytes(row.data), row.content_type)
    finally:
        db.close()
    _memo = (etag, payload[0], payload[1])
    return payload


def metadata() -> dict:
    """Everything the settings card shows about the current logo, or ``{}``.

    Admin-only caller (``GET /api/setup/branding``), so a database read is fine here. The
    RENDER path must never call this -- it reads the ``app_config`` pointer rows through
    ``branding._logo_override`` instead, which costs a dict lookup in an already-warm cache
    and never touches the blob.

    Never raises, for the same reason ``branding.overrides`` does not: an unmigrated
    database must cost the operator this panel's preview, not the settings page.
    """
    from ..database import SessionLocal, BrandAsset
    try:
        db = SessionLocal()
        try:
            row = db.query(BrandAsset).filter(BrandAsset.slot == _SLOT).first()
            if row is None:
                return {}
            return {
                "url": url_for(row.sha256),
                "mime": row.content_type,
                "bytes": row.byte_size,
                "width": row.width,
                "height": row.height,
                "filename": row.filename,
                "uploaded_by": row.uploaded_by,
            }
        finally:
            db.close()
    except Exception:
        return {}


def url_for(etag: str) -> str:
    """The public URL for a digest.

    Under ``/api/setup`` because that prefix is already in ``main._SETUP_BYPASS_PREFIXES``,
    so the logo loads on the sign-in page and during the setup wizard. A tidier ``/brand/``
    path would be redirected to ``/setup`` on a fresh instance, and nobody types this URL.
    """
    return f"/api/setup/branding/logo/{etag}"
