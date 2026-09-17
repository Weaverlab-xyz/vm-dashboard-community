"""An operator-uploaded logo: accepted on its bytes, served as an inert image.

This is the first thing in the app that takes an image from a person and hands it back to
a browser, and three of its failure modes are the kind that ship quietly.

  * **It is stored XSS if it is ever inlined.** SVG is accepted, because a vector logo is
    what a brand team hands over and there is no image library in requirements.txt to
    convert one. That is only safe because the bytes are reached exclusively through an
    `img` element and served with hardening headers -- there is no CSP middleware anywhere
    in this app, so those headers are the whole control. Both halves are asserted here,
    including on the partial's source text, because "render it inline instead" is a
    plausible-looking refactor that would silently remove the guard.
  * **The serving route must NOT be admin-gated.** The sign-in page and the public docs
    shell both render the mark, and this app has no auth cookie -- only a bearer header,
    which a browser cannot attach to an `img` request. Gating it turns every logo into a
    broken image with a 401 nobody sees.
  * **The pointer rows must stay out of `_BRANDING_KEYS`.** `patch_branding` blanks every
    key in that tuple on every save so that clearing a field erases its row. A logo pointer
    listed there would be wiped whenever somebody changed a colour, orphaning the blob and
    reverting the nav -- with a success toast.

The pure half needs no database. The round-trip at the end builds a one-router app over a
file-backed SQLite database, in the style of tests/test_agent_api, and skips if the app's
dependencies are unavailable.

Runs under pytest, or standalone:
    python tests/test_brand_logo.py
"""
import base64
import hashlib
import os
import re
import struct
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-brand-logo")
# Set before web_dashboard.database is imported, so the engine binds to it. A file rather
# than :memory: because the app opens more than one connection.
_TMPDB = os.path.join(tempfile.mkdtemp(prefix="brand-logo-test-"), "test.db")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_TMPDB}")

from web_dashboard.services import brand_logo, branding, config_service, ui_theme  # noqa: E402

_TPL = os.path.join(_ROOT, "web_dashboard", "templates")
_MARK = os.path.join(_TPL, "_brand_mark.html")
_SETUP_API = os.path.join(_ROOT, "web_dashboard", "api", "setup.py")
_DOCS = os.path.join(_ROOT, "web_dashboard", "api", "docs_pages.py")

_DIGEST = "a" * 64
_SVG_HEAD = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 240 60">'


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


# ── Fixtures, built by hand so the bytes are exactly what the sniffer reads ──

def _png(width=1200, height=300):
    import zlib
    ihdr = b"IHDR" + struct.pack(">II", width, height) + bytes([8, 6, 0, 0, 0])
    return (b"\x89PNG\r\n\x1a\x0a" + struct.pack(">I", 13) + ihdr
            + struct.pack(">I", zlib.crc32(ihdr)))


def _jpeg(width=640, height=240):
    # A correctly formed APP0 (length 16 == 2 bytes of length + 14 of payload), then SOF0.
    app0 = (b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x01\x01" + b"\x00"
            + struct.pack(">HH", 1, 1) + b"\x00\x00")
    sof = (b"\xff\xc0" + struct.pack(">H", 17) + b"\x08"
           + struct.pack(">HH", height, width) + b"\x03" + b"\x00" * 9)
    return b"\xff\xd8" + app0 + sof


def _webp(width=100, height=50):
    return (b"RIFF" + b"\x00" * 4 + b"WEBP" + b"VP8X" + b"\x00" * 8
            + (width - 1).to_bytes(3, "little") + (height - 1).to_bytes(3, "little"))


def _svg(body=b'<path d="M1 1L9 9" stroke="currentColor"/>'):
    return _SVG_HEAD + body + b"</svg>"


class _Stub:
    def __init__(self, rows):
        self.rows = rows

    def __call__(self, key, default="", workgroup=None):
        return self.rows.get(key, default)


def _with_rows(rows):
    original = config_service.get_raw
    config_service.get_raw = _Stub(rows)
    try:
        return branding.overrides()
    finally:
        config_service.get_raw = original


# ── Sniffing ─────────────────────────────────────────────────────────────────


def test_the_sniffer_identifies_each_accepted_format():
    assert brand_logo.sniff(_png(1200, 300)) == ("image/png", 1200, 300)
    assert brand_logo.sniff(_jpeg(640, 240)) == ("image/jpeg", 640, 240)
    assert brand_logo.sniff(_webp(100, 50)) == ("image/webp", 100, 50)
    assert brand_logo.sniff(_svg()) == ("image/svg+xml", 240, 60)


def test_an_svg_is_recognised_behind_a_bom_and_a_declaration():
    """Real exports lead with a BOM, an XML declaration, a doctype and a comment."""
    prefixed = (b"\xef\xbb\xbf" + b'<?xml version="1.0" encoding="UTF-8"?>\n'
                + b"<!-- Generator: Illustrator -->\n" + _svg())
    assert brand_logo.sniff(prefixed)[0] == "image/svg+xml"


def test_a_non_image_is_rejected():
    for data, label in ((b"%PDF-1.4\n", "pdf"), (b"PK\x03\x04", "zip"),
                        (b"hello world", "text"), (b"", "empty"),
                        (b"<html><body>hi</body></html>", "html")):
        assert brand_logo.sniff(data) is None, label


def test_gif_is_not_accepted():
    """A deliberate omission -- animated chrome, no upside -- not an oversight."""
    assert brand_logo.sniff(b"GIF89a" + b"\x00" * 16) is None
    assert brand_logo.sniff(b"GIF87a" + b"\x00" * 16) is None
    assert "image/gif" not in brand_logo._ALLOWED


def test_a_renamed_file_is_rejected_on_its_bytes():
    """The declared type is advisory; the sniffer is the control. Both directions, because
    an operator who renamed a file needs to be told which way round it is."""
    for data, declared in ((_png(), "image/svg+xml"), (_svg(), "image/png"),
                           (_jpeg(), "image/webp")):
        try:
            brand_logo.sniff_and_validate(data, declared)
            assert False, f"{declared} on other bytes was accepted"
        except brand_logo.RejectedLogo as exc:
            assert exc.status == 400
            assert "real extension" in str(exc)


def test_a_matching_declared_type_passes():
    assert brand_logo.sniff_and_validate(_png(), "image/png")[0] == "image/png"
    # A charset parameter must not defeat the comparison.
    assert brand_logo.sniff_and_validate(_svg(), "image/svg+xml; charset=utf-8")[0] \
        == "image/svg+xml"
    # An absent or unknown declaration is simply ignored.
    assert brand_logo.sniff_and_validate(_png(), "")[0] == "image/png"
    assert brand_logo.sniff_and_validate(_png(), "application/octet-stream")[0] == "image/png"


def test_an_oversized_image_is_rejected_with_413():
    data = _png() + b"\x00" * brand_logo._MAX_LOGO_BYTES
    try:
        brand_logo.sniff_and_validate(data)
        assert False, "an oversized logo was accepted"
    except brand_logo.RejectedLogo as exc:
        assert exc.status == 413


# ── SVG ──────────────────────────────────────────────────────────────────────


def test_svg_with_script_or_external_references_is_rejected():
    """The security test. Rejected, not stripped: a regex sanitiser for SVG is the classic
    losing game, and a half-sanitised file that renders is worse than a clear refusal."""
    hostile = {
        "script": _svg(b"<script>alert(1)</script>"),
        "uppercase script": _svg(b"<SCRIPT>alert(1)</SCRIPT>"),
        "script split over a newline": _svg(b"<\n  script>alert(1)</script>"),
        "script with a space": _svg(b"< script>alert(1)</script>"),
        "inline handler": _svg(b'<rect onload="alert(1)"/>'),
        "mixed-case handler": _svg(b'<rect onLoad ="alert(1)"/>'),
        "foreignObject": _svg(b"<foreignObject><body/></foreignObject>"),
        "iframe": _svg(b'<iframe src="//x"/>'),
        "javascript: url": _svg(b'<a href="javascript:alert(1)"><rect/></a>'),
        "external xlink": _svg(b'<image xlink:href="http://evil/x.png"/>'),
        "protocol-relative href": _svg(b'<image href="//evil/x.png"/>'),
        "entity declaration": (b'<!DOCTYPE s [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
                               + _svg()),
    }
    for label, data in hostile.items():
        try:
            brand_logo.validate_svg(data)
            assert False, f"{label} was accepted"
        except brand_logo.RejectedLogo:
            pass


def test_a_malformed_or_non_svg_file_is_rejected():
    for data, label in ((b"<svg><rect></svg>", "mismatched tags"),
                        (b'<html xmlns="http://www.w3.org/2000/svg"><svg/></html>',
                         "not an svg root")):
        try:
            brand_logo.validate_svg(data)
            assert False, f"{label} was accepted"
        except brand_logo.RejectedLogo:
            pass


def test_a_clean_svg_is_accepted():
    brand_logo.validate_svg(_svg())
    brand_logo.validate_svg(_svg(b'<rect fill="#ff5400" width="10" height="10"/>'))
    # A local fragment reference is not an external one.
    brand_logo.validate_svg(
        _svg(b'<defs><linearGradient id="g"/></defs><rect fill="url(#g)"/>'))


def test_the_rejection_names_what_it_found():
    """An operator who gets 'contains scripting' and no detail retries the same file."""
    try:
        brand_logo.validate_svg(_svg(b"<script>alert(1)</script>"))
    except brand_logo.RejectedLogo as exc:
        assert "script element" in str(exc)
        assert "PNG" in str(exc), "the message does not suggest a way forward"


# ── The read path ────────────────────────────────────────────────────────────


def test_no_logo_means_the_theme_still_has_none():
    for profile, env in (("demo", "production"), ("pov", "production")):
        assert ui_theme.theme_for(profile, env)["logo"] is None


def test_a_valid_pointer_becomes_a_url_and_dimensions():
    out = _with_rows({"brand_logo_etag": _DIGEST, "brand_logo_mime": "image/png",
                      "brand_logo_w": "1200", "brand_logo_h": "300"})
    assert out["logo"] == {
        "url": f"/api/setup/branding/logo/{_DIGEST}",
        "mime": "image/png", "width": 1200, "height": 300,
    }


def test_a_hand_edited_pointer_never_reaches_an_src():
    """The etag lands in a URL and then in an attribute -- including on the public docs
    shell, which does not autoescape. Same argument as the colours: app_config is reachable
    from config_migrate and from psql."""
    for bad in ("../../etc/passwd", "a" * 63, "a" * 65, "", "   ",
                "z" * 64, "a" * 64 + " x", '"><img src=x onerror=alert(1)>',
                "/etc/passwd", "http://evil/x.png"):
        out = _with_rows({"brand_logo_etag": bad, "brand_logo_mime": "image/png"})
        assert "logo" not in out, f"{bad!r} was accepted"


def test_an_uppercase_digest_is_normalised_rather_than_dropped():
    """hashlib emits lowercase, but a dump or a hand-edit can carry upper -- and it is
    still a well-formed digest, so normalising beats falling back to the built-in mark."""
    out = _with_rows({"brand_logo_etag": _DIGEST.upper(), "brand_logo_mime": "image/png"})
    assert out["logo"]["url"].endswith(_DIGEST)


def test_an_off_allowlist_mime_is_dropped():
    for bad in ("text/html", "image/gif", "application/xml", "", "image/png; x=1"):
        out = _with_rows({"brand_logo_etag": _DIGEST, "brand_logo_mime": bad})
        assert "logo" not in out, f"{bad!r} was accepted"


def test_a_bad_dimension_is_dropped_not_rendered():
    """These land in an aspect-ratio style declaration."""
    for bad in ("abc", "0", "-5", "1e9", "999999999", "10; x:y"):
        out = _with_rows({"brand_logo_etag": _DIGEST, "brand_logo_mime": "image/png",
                          "brand_logo_w": bad, "brand_logo_h": bad})
        assert out["logo"]["width"] is None, f"{bad!r} was accepted"
        assert out["logo"]["height"] is None, f"{bad!r} was accepted"


def test_the_read_path_never_touches_the_blob():
    """It runs on every render of every page. A query here would be a per-request cost."""
    src = _read(os.path.join(_ROOT, "web_dashboard", "services", "branding.py"))
    body = src.split("def _logo_override")[1].split("\ndef ")[0]
    for forbidden in ("SessionLocal", "BrandAsset", "brand_logo."):
        assert forbidden not in body, f"_logo_override reaches for {forbidden}"


# ── Rendering ────────────────────────────────────────────────────────────────


def test_the_partial_never_inlines_the_logo():
    """The one refactor that would turn this feature into stored XSS."""
    src = _read(_MARK)
    assert "{% if theme.logo %}" in src, "the partial has no logo branch"
    assert "<img" in src, "the logo is not rendered as an image element"
    assert "|safe" not in src and "| safe" not in src, \
        "the partial pipes something through safe, which is how an SVG becomes script"
    # The built-in mark must survive as the fallback.
    assert "theme.mark_warp_path" in src and "theme.mark_weft_path" in src


def test_the_logo_is_hidden_from_assistive_technology():
    """Both call sites render the brand or product name as adjacent text, so a describing
    alt would make a screen reader announce it twice."""
    src = _read(_MARK)
    img = re.search(r"<img[^>]*>", src, re.S).group(0)
    assert 'alt=""' in img
    assert 'aria-hidden="true"' in img
    assert "loading=" not in img, "the mark is above the fold on every page"


def test_every_call_site_bounds_the_logo_width():
    """A flex item's min-width is its content, so an unbounded image pushes the cog and the
    hamburger off the right edge and makes the whole document scroll sideways on a phone."""
    for name in ("base.html", "login.html"):
        markup = _read(os.path.join(_TPL, name))
        block = markup.split("_brand_mark.html")[0]
        withs = re.findall(r"\{%\s*with\b.*?%\}", block, re.S)
        assert withs, f"{name} does not pass anything to the partial"
        last = withs[-1]
        assert "mark_h" in last, f"{name} does not pass a height class"
        assert "mark_max" in last, f"{name} does not bound the logo width"


def test_the_partial_defaults_are_bounded_too():
    """A future third call site that forgets the parameters must still be safe."""
    src = _read(_MARK)
    img = re.search(r"<img[^>]*>", src, re.S).group(0)
    assert "mark_max|default(" in img
    assert "mark_h|default(" in img
    assert "w-auto" in img, "a wide wordmark needs width:auto, not a square box"


def test_the_docs_shell_renders_the_logo_to_an_anonymous_visitor():
    from web_dashboard.api import docs_pages
    original = config_service.get_raw
    config_service.get_raw = _Stub({"brand_logo_etag": _DIGEST,
                                    "brand_logo_mime": "image/png"})
    try:
        html = docs_pages._shell("Title", "<p>body</p>")
    finally:
        config_service.get_raw = original
    assert f'src="/api/setup/branding/logo/{_DIGEST}"' in html
    assert "<script" not in html
    # The built-in mark must be gone, not merely hidden behind it.
    assert "<svg" not in html.split("</header>")[0]


def test_the_docs_shell_falls_back_to_the_built_in_mark():
    from web_dashboard.api import docs_pages
    original = config_service.get_raw
    config_service.get_raw = _Stub({})
    try:
        html = docs_pages._shell("Title", "<p>body</p>")
    finally:
        config_service.get_raw = original
    assert "<svg" in html
    assert "/api/setup/branding/logo/" not in html


# ── Wiring ───────────────────────────────────────────────────────────────────


def test_the_logo_keys_are_not_in_the_branding_patch_set():
    """patch_branding writes every key in _BRANDING_KEYS as "" on every save, so that
    clearing a field erases its row. A logo pointer in that tuple would therefore be wiped
    whenever an operator changed a colour -- orphaning the blob row and reverting the nav to
    the built-in mark, with a success toast and nothing in any log."""
    from web_dashboard.api import setup as setup_api
    offenders = [k for k in setup_api._BRANDING_KEYS if k.startswith("brand_logo")]
    assert not offenders, f"{offenders} would be erased on every colour save"
    # And the pointers the logo endpoints do own.
    assert set(setup_api._LOGO_KEYS) == {
        "brand_logo_etag", "brand_logo_mime", "brand_logo_w", "brand_logo_h"}
    assert not set(setup_api._LOGO_KEYS) & set(setup_api._BRANDING_KEYS)


def test_every_pointer_key_is_read_by_the_reader():
    """A key written by the API and read by nobody is a setting that does nothing."""
    from web_dashboard.api import setup as setup_api
    reader = _read(os.path.join(_ROOT, "web_dashboard", "services", "branding.py"))
    for key in setup_api._LOGO_KEYS:
        assert f'"{key}"' in reader, f"{key} is written by the API and read by nobody"


def test_the_logo_pointers_do_not_migrate_between_instances():
    """config_migrate's scope is app_config, and the bytes live in `brand_asset`. So a
    carried pointer would arrive as an orphan: the target renders an img at a digest it has
    never stored, the public route answers 404, and every page -- including the sign-in
    screen -- shows a broken image. Dropped instead, so it falls back to the mark.

    The colours have no such dependency and must keep migrating.
    """
    from web_dashboard.scripts.config_migrate import classify
    from web_dashboard.api import setup as setup_api
    for key in setup_api._LOGO_KEYS:
        assert classify.exclusion_reason(key), f"{key} would migrate and dangle"
    for key in ("brand_primary", "brand_secondary", "brand_accent_hex",
                "brand_name", "brand_accent", "brand_env_color"):
        assert not classify.exclusion_reason(key), f"{key} stopped migrating"


def test_the_public_logo_route_is_not_admin_gated():
    """There is no auth cookie in this app -- only a bearer header, which a browser cannot
    attach to an img element's request. An admin gate here makes every logo a broken image
    on the sign-in page, with a 401 nobody ever sees."""
    src = _read(_SETUP_API)
    handler = src.split("def get_branding_logo(")[1].split("\n@router")[0]
    # The CALL, not the name: the handler's docstring explains at length why the gate is
    # absent, so matching the bare identifier would match the explanation.
    assert "_require_admin(request)" not in handler, \
        "the public logo route is admin-gated, so it can never render pre-auth"
    # The other two must be gated.
    for name in ("upload_branding_logo", "delete_branding_logo"):
        body = src.split(f"def {name}(")[1].split("\n@router")[0]
        assert "_require_admin(request)" in body, f"{name} is not admin-gated"


def test_the_public_logo_route_survives_the_setup_guard():
    """setup_guard 302s any non-bypassed path to /setup until setup completes, which would
    make the logo a redirect on the sign-in page of a fresh instance."""
    from web_dashboard import main
    url = brand_logo.url_for(_DIGEST)
    assert any(url.startswith(prefix) for prefix in main._SETUP_BYPASS_PREFIXES), \
        f"{url} is not under a bypassed prefix"


def test_the_serving_route_is_synchronous():
    """It does a blocking SQLAlchemy query on a cache miss. As `async def` that would block
    the event loop, and this endpoint is hit by every cold browser."""
    src = _read(_SETUP_API)
    assert "\ndef get_branding_logo(" in src, \
        "the logo route is async, so its DB read blocks the event loop"


def test_the_serving_route_hardens_its_response():
    """There is no CSP middleware in this app, so these headers are the entire control
    that makes accepting SVG safe -- not decoration on top of one."""
    src = _read(_SETUP_API)
    handler = src.split("def get_branding_logo(")[1].split("\n@router")[0]
    for header in ("Content-Security-Policy", "X-Content-Type-Options",
                   "X-Frame-Options", "ETag", "Cache-Control"):
        assert header in handler, f"{header} is missing from the logo response"
    assert "default-src 'none'" in handler
    assert "sandbox" in handler
    assert "immutable" in handler
    assert "Content-Disposition" in handler, \
        "an SVG served without it can be navigated to as a document"


def test_the_upload_endpoint_guards_the_size_before_decoding():
    """Otherwise a hostile multi-megabyte body is expanded into memory first."""
    src = _read(_SETUP_API)
    handler = src.split("def upload_branding_logo(")[1].split("\n@router")[0]
    guard = handler.index("_MAX_LOGO_BYTES")
    decode = handler.index("b64decode")
    assert guard < decode, "the size check runs after the decode"


def test_the_favicon_is_left_alone():
    """No image library here to make a 16px raster, a wordmark is unreadable at that size,
    and the tab icon is the only profile signal visible on an unfocused tab."""
    theme = ui_theme.theme_for("demo", "production",
                               logo={"url": "/x", "mime": "image/png",
                                     "width": None, "height": None})
    assert theme["favicon"].startswith("data:image/svg+xml,")


def test_the_settings_panel_keeps_the_logo_out_of_the_colour_payload():
    """`branding` is PATCHed wholesale on Save. A logo in it would ride every colour save
    as base64."""
    markup = _read(os.path.join(_TPL, "settings.html"))
    declared = re.search(r"branding:\s*\{(.*?)\n    \},", markup, re.S).group(1)
    assert "logo" not in declared, "the logo is a member of the colour PATCH payload"
    assert "brandingLogo" in markup, "the panel has no logo state at all"
    bound = set(re.findall(r'x-model="branding\.(\w+)"', markup))
    assert not any(k.startswith("brand_logo") for k in bound)


def test_store_refuses_a_string_payload():
    """SQLite accepts a str into a BLOB column and Postgres does not, so this would be a
    bug that passes every local test and fails only on a real deployment."""
    try:
        brand_logo.store("not bytes", "image/png")
        assert False, "a str payload was accepted"
    except AssertionError as exc:
        assert "bytes" in str(exc)


def test_load_rejects_a_malformed_etag_without_a_query():
    assert brand_logo.load("") is None
    assert brand_logo.load("../../x") is None
    assert brand_logo.load("a" * 63) is None


# ── Round trip ───────────────────────────────────────────────────────────────


def _client():
    """A one-router app over the temp database, in tests/test_agent_api's style.

    `_require_admin` is a plain function rather than a FastAPI dependency, so it is
    monkeypatched directly instead of through dependency_overrides.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from web_dashboard.database import Base, engine
    from web_dashboard.api import setup as setup_api

    Base.metadata.create_all(bind=engine)
    setup_api._require_admin = lambda request: "tester"
    app = FastAPI()
    app.include_router(setup_api.router)
    return TestClient(app)


def _upload(client, data, content_type="", filename="logo.png"):
    return client.post("/api/setup/branding/logo", json={
        "filename": filename,
        "content_type": content_type,
        "content_b64": base64.b64encode(data).decode(),
    })


def test_the_round_trip():
    """Upload, serve, revalidate, replace, delete -- the sequence an operator performs."""
    try:
        client = _client()
    except Exception as exc:  # pragma: no cover - app deps missing
        print(f"   (skipped: {exc})")
        return

    png = _png(1200, 300)
    digest = hashlib.sha256(png).hexdigest()

    r = _upload(client, png, "image/png")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["url"] == f"/api/setup/branding/logo/{digest}"
    assert body["mime"] == "image/png"
    assert (body["width"], body["height"]) == (1200, 300)

    # Served, with the headers that make it safe and cacheable.
    r = client.get(body["url"])
    assert r.status_code == 200, r.text
    assert r.content == png
    assert r.headers["content-type"] == "image/png"
    assert r.headers["etag"] == f'"{digest}"'
    assert "immutable" in r.headers["cache-control"]
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "default-src 'none'" in r.headers["content-security-policy"]
    # Raster is served inline; only SVG gets forced to download.
    assert "content-disposition" not in r.headers

    # A warm browser revalidates and gets nothing back.
    r304 = client.get(body["url"], headers={"If-None-Match": f'"{digest}"'})
    assert r304.status_code == 304
    assert r304.headers["etag"] == f'"{digest}"'

    # An unknown hash is a 404, never a redirect to the current one.
    assert client.get(f"/api/setup/branding/logo/{'b' * 64}").status_code == 404
    assert client.get("/api/setup/branding/logo/nonsense").status_code == 404

    # The pointer rows are what the render path reads.
    assert config_service.get_raw("brand_logo_etag") == digest
    assert config_service.get_raw("brand_logo_mime") == "image/png"
    assert branding.overrides()["logo"]["url"] == body["url"]

    # Replacing it moves the URL and retires the old one.
    svg = _svg()
    svg_digest = hashlib.sha256(svg).hexdigest()
    r = _upload(client, svg, "image/svg+xml", "logo.svg")
    assert r.status_code == 201, r.text
    assert client.get(f"/api/setup/branding/logo/{digest}").status_code == 404
    r = client.get(f"/api/setup/branding/logo/{svg_digest}")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/svg+xml")
    assert r.headers["content-disposition"].startswith("attachment")

    # A hostile SVG is refused with a message worth reading.
    r = _upload(client, _svg(b"<script>alert(1)</script>"), "image/svg+xml", "x.svg")
    assert r.status_code == 400, r.text
    assert "script element" in r.json()["detail"]
    # ...and the good logo is still there.
    assert client.get(f"/api/setup/branding/logo/{svg_digest}").status_code == 200

    # Non-images, renames and bad base64.
    assert _upload(client, b"%PDF-1.4\n", "image/png").status_code == 400
    assert _upload(client, _png(), "image/svg+xml").status_code == 400
    r = client.post("/api/setup/branding/logo", json={"content_b64": "not base64!!"})
    assert r.status_code == 400

    # Oversized, refused before the decode.
    r = _upload(client, _png() + b"\x00" * brand_logo._MAX_LOGO_BYTES, "image/png")
    assert r.status_code == 413, r.text

    # The admin panel sees it.
    meta = client.get("/api/setup/branding").json()
    assert meta["logo"]["mime"] == "image/svg+xml"
    assert meta["logo"]["filename"] == "logo.svg"
    assert meta["logo_svg_allowed"] is True

    # A colour save must not disturb the logo -- the _BRANDING_KEYS trap, end to end.
    r = client.patch("/api/setup/branding", json={
        "brand_primary": "#1903A6", "brand_secondary": "#030973",
        "brand_accent_hex": "#FF5400",
    })
    assert r.status_code == 200, r.text
    assert config_service.get_raw("brand_logo_etag") == svg_digest, \
        "changing a colour erased the logo pointer"
    assert client.get(f"/api/setup/branding/logo/{svg_digest}").status_code == 200

    # A partial trio is refused.
    assert client.patch("/api/setup/branding",
                        json={"brand_primary": "#1903A6"}).status_code == 422

    # Removal falls back to the built-in mark.
    assert client.delete("/api/setup/branding/logo").status_code == 200
    assert client.get(f"/api/setup/branding/logo/{svg_digest}").status_code == 404
    assert config_service.get_raw("brand_logo_etag") == ""
    assert "logo" not in branding.overrides()
    assert client.get("/api/setup/branding").json()["logo"] == {}
    # Deleting again is a no-op, not a 500.
    assert client.delete("/api/setup/branding/logo").status_code == 200


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
