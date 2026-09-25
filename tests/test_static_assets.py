"""No template may reference a static asset by a bare, unversioned path.

`26.10.20` shipped a `static/js/app.js` with a syntax error, which took every page in the
product down (the failure class `test_template_scripts.py` guards). The fix was deployed
and the server served the corrected file. Browsers did not pick it up: the template asked
for the file by a bare path with no version parameter, and the response carried no
`Cache-Control` -- only `etag` and `last-modified` -- so Chrome applied heuristic
freshness and reused its broken copy without revalidating. Verified live on
dash.weaverlab.app on 2026-09-25; only `fetch(url, {cache:'reload'})` shifted it. A
returning user's only recourse was a hard reload they had no reason to know about.

So the deploy is only half a fix, and the half that is missing is not in any code path a
test normally walks. Three things have to stay true, and each is checked below:

1. every template asset reference goes through `static_url()`, which appends a hash of
   the file's bytes -- the scan is over ALL templates, since the two that had the bare
   reference were `base.html` and `login.html`, and `login.html` owns its own head and
   would have been the one silently left behind;
2. `static_url` is registered as a Jinja global, or every one of those references
   renders empty and the product ships with no JavaScript at all;
3. the mount states its caching: a year for a pinned URL, revalidate for an unpinned one.

The scan covers comments as well as attributes, deliberately. A banned literal quoted in
a comment is indistinguishable from a real one to the next person's grep, and the comment
in `base.html` is written to describe the path rather than spell it out for exactly that
reason.

Runs under pytest, or standalone:  python tests/test_static_assets.py
"""
import os
import pathlib
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

TEMPLATES = pathlib.Path(_ROOT) / "web_dashboard" / "templates"
MAIN_PY = pathlib.Path(_ROOT) / "web_dashboard" / "main.py"

from web_dashboard.services import static_assets  # noqa: E402  (after sys.path)

# The mount prefix, assembled rather than written, so this file does not trip its own
# scan if somebody ever points the scan at tests/ too.
_MOUNT = "/" + "static" + "/"


def _templates():
    for path in sorted(TEMPLATES.rglob("*.html")):
        yield path.relative_to(TEMPLATES).as_posix(), path.read_text(encoding="utf-8")


def test_no_template_writes_a_static_path_by_hand():
    """The whole point. A hand-written path is an unversioned one, and an unversioned
    one is a file a browser may keep serving after it has been fixed."""
    offenders = []
    for rel, text in _templates():
        for n, line in enumerate(text.splitlines(), 1):
            if _MOUNT in line:
                offenders.append(f"{rel}:{n}: {line.strip()}")
    assert not offenders, (
        "template(s) reference a static asset by a literal path instead of\n"
        "  {{ static_url('js/app.js') }}\n"
        "An unversioned URL is what let a fixed app.js stay broken in Chrome for hours\n"
        "after the deploy (see web_dashboard/services/static_assets.py). If the literal\n"
        "below is inside a COMMENT, describe the path instead of quoting it -- a comment\n"
        "is how the next real one gets pasted in.\n  " + "\n  ".join(offenders))


def test_every_script_and_link_in_a_template_is_versioned_or_remote():
    """The complement of the scan above, stated positively: look at what each `src` and
    `href` actually resolves to, so a future asset served from some other local prefix
    is caught as well as one under the mount.

    Remote URLs are exempt -- Tailwind and Alpine come off a CDN, and their caching is
    not ours to set."""
    attr = re.compile(r'\b(?:src|href)\s*=\s*"([^"]*)"')
    offenders = []
    for rel, text in _templates():
        for n, line in enumerate(text.splitlines(), 1):
            for value in attr.findall(line):
                v = value.strip()
                if not v or v.startswith(("http://", "https://", "//", "#", "data:",
                                          "mailto:")):
                    continue
                if "static" not in v.lower():
                    continue          # an ordinary in-app link, not an asset
                if "static_url(" in v:
                    continue          # the sanctioned form
                offenders.append(f"{rel}:{n}: {v}")
    assert not offenders, (
        "static asset reference(s) not built by static_url():\n  " + "\n  ".join(offenders))


def test_the_asset_that_caused_this_is_versioned():
    """`app.js` specifically, by name, because it is the file the incident was about and
    a scan that passes because it found nothing would be no comfort at all."""
    referencing = [rel for rel, text in _templates() if "static_url(" in text]
    assert referencing, "no template calls static_url() -- did the references move?"
    assert any("app.js" in text for _, text in _templates()
               if "static_url(" in text), "nothing asks static_url for app.js"

    url = static_assets.url("js/app.js")
    assert url.startswith("/static/js/app.js?" + static_assets.VERSION_PARAM + "="), url
    digest = url.split("=", 1)[1]
    assert re.fullmatch(r"[0-9a-f]{12}", digest), f"not a short sha256 digest: {digest!r}"


def test_the_version_follows_the_bytes():
    """Two files differing only in content must get different versions, and the same
    bytes must get the same one -- otherwise a deploy either fails to bust the cache or
    busts it on every release whether or not anything changed."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        original = static_assets.STATIC_DIR
        static_assets.STATIC_DIR = tmp
        static_assets._cache.clear()
        try:
            asset = pathlib.Path(tmp) / "js" / "x.js"
            asset.parent.mkdir(parents=True)

            asset.write_text("var a = 1;\n", encoding="utf-8")
            os.utime(asset, (1_000_000, 1_000_000))
            first = static_assets.version("js/x.js")

            # Same bytes, written again: the digest must not move, or every deploy
            # would invalidate assets it did not touch.
            asset.write_text("var a = 1;\n", encoding="utf-8")
            os.utime(asset, (2_000_000, 2_000_000))
            assert static_assets.version("js/x.js") == first, "digest moved without the bytes"

            # Different bytes, same length: length alone is not the version.
            asset.write_text("var a = 2;\n", encoding="utf-8")
            os.utime(asset, (3_000_000, 3_000_000))
            assert static_assets.version("js/x.js") != first, "digest ignored a content change"
        finally:
            static_assets.STATIC_DIR = original
            static_assets._cache.clear()


def test_a_missing_asset_renders_an_unversioned_url():
    """Never an invented or empty version. `?v=` with nothing after it would pin the
    browser to whatever bytes it saw first at that URL -- worse than no version, because
    it is the same failure with a fresh coat of paint on it.

    The page still renders. Losing cache busting is a regression; a 500 on every page
    because an asset was renamed is an outage."""
    assert static_assets.url("js/no-such-file.js") == "/static/js/no-such-file.js"
    assert static_assets.version("js/no-such-file.js") == ""


def test_the_cache_control_policy():
    """A pinned URL may be cached for a year; an unpinned one must be revalidated."""
    assert static_assets.cache_control(b"v=abc123def456") == static_assets.IMMUTABLE
    assert "immutable" in static_assets.IMMUTABLE
    assert "max-age=31536000" in static_assets.IMMUTABLE

    for unpinned in (b"", b"v=", b"foo=bar", b"rev=abc", b"nav=1"):
        assert static_assets.cache_control(unpinned) == static_assets.REVALIDATE, (
            f"{unpinned!r} is not a pinned URL and must not be cached blind")
    # `no-cache`, not `no-store`: the etag still buys a 304, we only forbid silent reuse.
    assert static_assets.REVALIDATE == "no-cache"


def test_main_wires_the_global_and_the_versioned_mount():
    """Source-parsed rather than imported: `web_dashboard.main` pulls in the whole app,
    the database and every router, which is more than this guard should need to be able
    to run (and more than it can run standalone here).

    Both halves matter and they fail differently. Without the global, `static_url` is
    undefined, Jinja resolves it to nothing, and every page ships with an empty script
    src -- the entire UI dead, the same symptom as the incident. Without the subclass,
    the pages work and the caching quietly reverts to whatever the browser feels like,
    which is how this started."""
    src = MAIN_PY.read_text(encoding="utf-8")
    assert 'templates.env.globals["static_url"] = static_assets.url' in src, (
        "static_url is not registered as a Jinja global in main.py -- every "
        "{{ static_url(...) }} in a template would render as an empty string")
    assert re.search(r'app\.mount\(\s*"/static"\s*,\s*_VersionedStaticFiles\(', src), (
        "the /static mount is not _VersionedStaticFiles -- responses would go back to "
        "carrying no Cache-Control at all")
    assert "static_assets.cache_control(" in src, (
        "_VersionedStaticFiles no longer applies the cache_control policy")


if __name__ == "__main__":
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
