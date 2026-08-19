"""The dashboard's Gateways tile, and the hash it hands to the Containers page.

The tile itself is one line of tile config. What is not visible in that line is that
its href is a *contract with another template*: `/containers#gateways` only works
because `containers/index.html` accepts `gateways` in its hash allowlist. Miss that
and the tile still renders, still links, and silently lands on the Cloud tab — a bug
no one reads out of the diff, because both halves look right alone.

Three things are pinned here:

  * **every #hash a dashboard tile points at /containers with is a tab that page
    honours.** Stated generally, not just for gateways, so the next tile added this
    way fails here rather than in someone's browser.
  * **the gateways hash is Jinja-gated on `pra_enabled`.** Unlike the Portainer
    panel — which renders either way, so `#portainer` is always safe — the gateways
    panel is wrapped in `{% if pra_enabled %}` in its entirety. Honouring the
    hash with the feature off would select a tab that isn't in the DOM: a blank page.
  * **landing via the hash loads the list.** The tab *button* calls `loadGateways()` on
    click; arriving from the dashboard skips that click, so init has to do it or the
    table renders empty on a page that has gateways.

Templates are read as text — Jinja and Alpine expressions are the subject here, and
the assertions are about what the markup says, so no jinja2 or DOM is needed.

Run: python tests/test_gateway_dashboard_tile.py   (or under pytest)
"""
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TEMPLATES = os.path.join(_ROOT, "web_dashboard", "templates")


def _read(*parts):
    with open(os.path.join(_TEMPLATES, *parts), encoding="utf-8") as f:
        return f.read()


DASHBOARD = _read("dashboard.html")
CONTAINERS = _read("containers", "index.html")

# One tile literal: { key: '...', title: '...', href: '...', flag: '...', ... }
TILE = re.compile(r"\{\s*key:\s*'(?P<key>[a-z_]+)'[^}]*\}")


def _tiles():
    """Every tile literal in dashboard.html, as {key: raw source}."""
    return {m.group("key"): m.group(0) for m in TILE.finditer(DASHBOARD)}


def _field(tile_src, name):
    m = re.search(rf"{name}:\s*'([^']*)'", tile_src)
    return m.group(1) if m else None


def _containers_tab_allowlist():
    """The tab ids `containers/index.html` honours in `window.location.hash`."""
    m = re.search(r"const tabs = \[(?P<body>[^\]]*)\]", CONTAINERS)
    assert m, "the hash allowlist in containers/index.html init() moved or changed shape"
    return m.group("body")


def test_the_gateways_tile_exists_in_the_containers_section():
    """It belongs to the Containers band — gateways are container workloads, and the
    page they link to is the Containers page."""
    tiles = _tiles()
    assert "gateways" in tiles, "the dashboard lost its Gateways tile"

    # The section literal runs from its id to the next section's, so a tile matched
    # inside this slice is a tile in this section.
    section = DASHBOARD[DASHBOARD.index("id: 'containers'"):]
    section = section[:section.index("],")]
    assert "key: 'gateways'" in section, (
        "the Gateways tile is no longer under the Containers section")

    tile = tiles["gateways"]
    assert _field(tile, "href") == "/containers#gateways", (
        "the tile must deep-link to the Gateways tab, not the Containers page default")
    # Same flag as the tab and the API routes, so the tile can't offer a surface the
    # rest of the app has switched off.
    assert _field(tile, "flag") == "pra", (
        "the tile must gate on the same flag as the tab and /api/gateways")


def test_the_tile_counts_managed_and_requested_together():
    """The point of the tile is one number for both kinds of gateway. `/api/gateways`
    is the only endpoint that returns both, and it already omits deleted rows.

    The tile moved server-side — the page reads every tile it can from
    GET /api/dashboard/stats — so the count is built in api/dashboard.py rather than fetched
    from `/api/gateways?reconcile=false`. What must not change is that it counts the WHOLE
    registry and does not reconcile: reconciling dials every cloud, and that endpoint makes
    no cloud calls.
    """
    with open(os.path.join(_ROOT, "web_dashboard", "api", "dashboard.py"),
              encoding="utf-8") as fh:
        api = fh.read()

    assert '_safe("gateways"' in api, (
        "gateways is not registered with _safe() in api/dashboard.py, so it never reaches "
        "the response and the tile reads 'unavailable'")

    body = api[api.index("    def _gateways():"):api.index('    _safe("gateways"')]
    assert "gateway_service.list_gateways(db)" in body, (
        "the tile must count the whole registry through the same service the Gateways tab "
        "lists from; a per-cloud call would make it a partial total")

    # Comments stripped before the absence check: the code's own note explaining why it
    # does NOT reconcile contains the word, so matching raw source fails the fix.
    code = "\n".join(ln.split("#", 1)[0] for ln in body.splitlines())
    assert "cloud=" not in code, "a cloud filter would make the tile a partial total"
    assert "reconcile" not in code, (
        "the aggregate must not reconcile — that dials every cloud, and this endpoint's "
        "whole contract is that it makes no cloud call. The Gateways tab reconciles on open")


def test_every_containers_hash_a_tile_links_to_is_a_tab_that_page_honours():
    """The general rule behind the gateways case: a tile href is only as good as the
    target page's hash handling. `onprem` is the Portainer tab's former id, kept as an
    alias, so it counts as honoured too."""
    allowlist = _containers_tab_allowlist() + "'onprem'"
    for key, tile in _tiles().items():
        href = _field(tile, "href") or ""
        if not href.startswith("/containers#"):
            continue
        tab = href.split("#", 1)[1]
        assert f"'{tab}'" in allowlist, (
            f"the {key} tile links to /containers#{tab}, but the Containers page "
            f"ignores that hash and would open its default tab instead")


def test_the_gateways_hash_is_gated_on_the_feature_flag():
    """The gateways panel is Jinja-gated in full, so accepting the hash unconditionally
    would select a tab that isn't rendered."""
    allowlist = _containers_tab_allowlist()
    assert "'gateways'" in allowlist, (
        "the Containers page ignores #gateways, so the dashboard tile lands on Cloud")
    assert "{% if pra_enabled %}" in allowlist, (
        "#gateways must be accepted only when the panel is rendered, or the tab "
        "selects nothing and the page comes up blank")

    # The premise of that gate: the panel really is Jinja-gated, unlike Portainer's.
    panel = CONTAINERS.index("activeTab === 'gateways'\" x-cloak")
    assert "{% if pra_enabled %}" in CONTAINERS[:panel][-400:], (
        "the gateways panel is no longer Jinja-gated — if it now renders "
        "unconditionally, the gate on the hash is no longer needed")


def test_arriving_by_hash_loads_the_gateway_list():
    """`loadGateways()` hangs off the tab button's click handler; the dashboard tile
    doesn't click it."""
    init = CONTAINERS[CONTAINERS.index("async init()"):]
    init = init[:init.index("async refresh()")]
    assert "loadGateways()" in init, (
        "init() never loads gateways, so a visit to /containers#gateways shows an "
        "empty table until the operator hits Refresh")
    m = re.search(r"if \(this\.activeTab === 'gateways'\)[^\n]*loadGateways\(\)", init)
    assert m, ("the init load must be conditional on landing on the tab — loading it "
               "for every Containers visit adds a query no one asked for")


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
