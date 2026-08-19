"""The dashboard's Image Registry tile — the label, the endpoint, and the gate.

The tile linking to `/images` shipped as "OVA Library / local OVA files", counting
`GET /api/images/ovas`. That endpoint does not exist anywhere in the app: the fetcher
404'd, `_fetchListCount` swallowed it into `-1`, and every install rendered a tile that
said "– local OVA files unavailable" forever. Nothing failed loudly, because an
unavailable tile is a *designed* state — it is what a throttled cloud API looks like.

`/images` is the cross-cloud Image Registry: rows in the dashboard's own
`registered_images` table saying which portable images exist and which clouds hold a
promotion of each. Nothing about it is VMware or OVA.

Five things are pinned here:

  * **the tile counts `/api/images`.** The list endpoint behind the page it links to.
    Stated as a rule over every dashboard fetcher — no tile may reference an
    `/api/images/...` sub-path that the images router does not define — so the next
    invented endpoint fails here instead of rendering "unavailable" in production.
  * **its label matches the page.** The `<h1>` on `/images` is "Image Registry"; a
    tile titled for a file format the registry does not model is how the first one
    went unnoticed.
  * **it gates on having a promote target, not on `vmware`.** VALID_CLOUDS is
    (aws, azure, gcp, oci). The old `flag: 'vmware'` was wrong in both directions:
    hidden from every AWS-only operator, and shown to VMware-only ones who cannot
    promote anywhere.
  * **`anyFlag` is honoured by the filter.** A gate the filter ignores is no gate:
    the tile would render on a bare install and suppress the empty state.
  * **"promoted" counts only completed promotions.** A promotion carries a status —
    completed / running / manual / pending / failed. The secondary renders in green,
    so counting a failed promotion would report a landed image that never landed.

Templates and the API router are read as text — Alpine expressions and route decorators
are the subject here — so no DOM and no app import are needed.

Run: python tests/test_dashboard_image_registry_tile.py   (or under pytest)
"""
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts):
    with open(os.path.join(_ROOT, *parts), encoding="utf-8") as f:
        return f.read()


DASHBOARD = _read("web_dashboard", "templates", "dashboard.html")
IMAGES_PAGE = _read("web_dashboard", "templates", "images", "index.html")
IMAGES_API = _read("web_dashboard", "api", "images.py")
REGISTRY_SERVICE = _read("web_dashboard", "services", "image_registry_service.py")

# One tile literal: { key: '...', title: '...', href: '...', flag: '...', ... }
TILE = re.compile(r"\{\s*key:\s*'(?P<key>[a-z_]+)'[^}]*\}")

TILE_KEY = "registered_images"


def _tiles():
    """Every tile literal in dashboard.html, as {key: raw source}."""
    return {m.group("key"): m.group(0) for m in TILE.finditer(DASHBOARD)}


def _field(tile_src, name):
    m = re.search(rf"{name}:\s*'([^']*)'", tile_src)
    return m.group(1) if m else None


DASHBOARD_API = _read("web_dashboard", "api", "dashboard.py")


def _server_tile(name):
    """The body of api/dashboard.py's builder for one DB-backed tile.

    The dashboard reads every tile it can from GET /api/dashboard/stats now, so a tile's
    wiring is a function here rather than a `_fetchTile` branch in the template. The failure
    this guards against is unchanged: a tile with no wiring renders 'unavailable' on every
    install, which is indistinguishable from a throttled cloud API.
    """
    m = re.search(rf"    def {name}\(\):\n(?P<body>(?:        .*\n|\n)+)", DASHBOARD_API)
    assert m, (
        f"api/dashboard.py has no {name}() builder — the tile it serves would render "
        "'unavailable' on every install, which looks exactly like a throttled cloud API")
    return m.group("body")


def _tile_is_registered(key):
    """That `key` actually reaches the response, not just that a builder exists."""
    assert f'_safe("{key}"' in DASHBOARD_API, (
        f"'{key}' is not registered with _safe() in api/dashboard.py, so it never reaches "
        "the response and the tile stays on 'unavailable'")


def _images_router_paths():
    """Every path the /api/images router defines, as full URLs."""
    prefix = re.search(r'APIRouter\(prefix="(?P<p>[^"]+)"', IMAGES_API)
    assert prefix, "the /api/images router prefix moved or changed shape"
    base = prefix.group("p")
    return {base + m.group("sub") for m in
            re.finditer(r'@router\.(?:get|post|delete|put|patch)\("(?P<sub>[^"]*)"', IMAGES_API)}


def test_no_tile_invents_an_images_endpoint():
    """The rule the OVA tile broke. Written over every `/api/images...` literal in the
    dashboard rather than over the one tile, so an invented sub-path can't come back
    under a new key."""
    defined = _images_router_paths()
    for m in re.finditer(r"'(?P<url>/api/images[^']*)'", DASHBOARD):
        url = m.group("url").split("?")[0]
        assert url in defined, (
            f"dashboard.html fetches {url}, which the /api/images router does not "
            f"define. It defines: {sorted(defined)}. A 404 here is invisible — "
            f"_fetchListCount turns it into value -1 and the tile just reads "
            f"'unavailable' forever.")


def test_the_tile_counts_registered_images():
    tiles = _tiles()
    assert TILE_KEY in tiles, (
        "the dashboard lost its Image Registry tile — /images has no count on the home page")

    _tile_is_registered(TILE_KEY)
    body = _server_tile("_registry")
    assert "image_registry_service.list_images" in body, (
        "the tile must count the registry through the same service the /images page "
        "renders from, not a second query")


def test_the_tile_is_labelled_for_the_page_it_opens():
    """The `<h1>` on /images, not a file format the registry does not model."""
    assert "Image Registry</h1>" in IMAGES_PAGE, (
        "the /images page heading changed — this test's premise needs rechecking")

    tile = _tiles()[TILE_KEY]
    assert _field(tile, "title") == "Image Registry", (
        "the tile title must match the heading of the page it links to; 'OVA Library' "
        "against the Image Registry page is what hid a dead endpoint for so long")
    assert _field(tile, "href") == "/images", "the tile must link to the Image Registry page"

    # No OVA vocabulary left on the tile — nothing in the registry is OVA-specific.
    assert "ova" not in tile.lower(), f"the tile still describes itself as OVA: {tile}"


def test_the_tile_gates_on_a_promote_target_not_on_vmware():
    """The registry promotes into VALID_CLOUDS; `vmware` is not one of them."""
    m = re.search(r"VALID_CLOUDS = \((?P<body>[^)]*)\)", REGISTRY_SERVICE)
    assert m, "VALID_CLOUDS moved or changed shape in image_registry_service"
    valid = set(re.findall(r'"([a-z]+)"', m.group("body")))

    tile = _tiles()[TILE_KEY]
    assert _field(tile, "flag") is None, (
        "a single `flag` can only name one integration; the registry serves every cloud")

    any_flag = re.search(r"anyFlag:\s*\[(?P<body>[^\]]*)\]", tile)
    assert any_flag, (
        "the tile has no gate at all — it would render on a bare install with nothing "
        "configured, and the 'No integrations configured yet' empty state would never show")
    gated_on = set(re.findall(r"'([a-z]+)'", any_flag.group("body")))
    assert gated_on == valid, (
        f"the tile gates on {sorted(gated_on)} but the registry promotes to "
        f"{sorted(valid)} — a cloud in one list and not the other either hides the "
        f"registry from an operator who can use it or offers it to one who cannot")


def test_visible_tiles_honours_any_flag():
    """`anyFlag` is only a comment until the filter reads it. A tile carrying a gate
    the filter ignores is always-on — the failure mode is a tile on a bare install."""
    m = re.search(r"visibleTiles\(tiles\) \{(?P<body>.*?)\n      \},", DASHBOARD, re.S)
    assert m, "visibleTiles moved or changed shape"
    body = m.group("body")
    assert "anyFlag" in body, (
        "visibleTiles ignores `anyFlag`, so the Image Registry tile renders even with "
        "no cloud configured")
    assert ".some(" in body, "`anyFlag` passes when ANY of its keys is on, not all of them"
    assert "t.flag" in body, "the plain single-key `flag` gate must still apply"


def test_promoted_counts_only_completed_promotions():
    """The secondary renders green. running/pending/manual/failed are not landed images."""
    body = _server_tile("_registry")
    assert "promotions" in body, (
        "the secondary count must come off each image's promotions map")
    assert '"completed"' in body, (
        "counting every promotion regardless of status reports failed and in-flight "
        "promotions as landed images, in green")

    tile = _tiles()[TILE_KEY]
    assert _field(tile, "secondaryLabel") == "promoted", (
        "the green secondary is labelled 'promoted'; changing the label without "
        "changing the predicate is how it starts lying")

    # The premise: 'completed' is the status the registry writes on a landed promotion,
    # and the page's own badge map is where that vocabulary is defined.
    assert "completed:" in IMAGES_PAGE, (
        "'completed' is no longer a promotion status on the /images page — the tile's "
        "predicate is matching a string nothing writes, so it would count zero forever")


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
    sys.exit(1 if failures else 0)
