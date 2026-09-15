"""Wiring tests for the Connections page becoming a TAB of Remote Agents.

Hypervisor connections had a page and a nav link of their own. They are now the second tab
of /agents, because a connection is nearly always a connection THROUGH an agent: the
via_agent form holds no host and no username at all, only the name of an entry in that
agent's own connections.yaml, and its Agent dropdown is a list of the rows on the other
tab. Filling one in meant holding two pages open.

Every assertion here pins something that fails LATE and QUIETLY if it is missed: a route
still naming a deleted template (a 500 on a page an SE opens in front of a customer), the
/connections gate disappearing (which is what the persona use cases read to decide whether
to offer a live link), a tab rendering on an instance whose integration is off, or the nav
losing the one route a non-admin has to the connection list.

Text-and-registry checks, no app imports, so it runs on a checkout without the requirements
installed. Runs under pytest or standalone:  python tests/test_connections_tab_wiring.py
"""
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TPL = ("web_dashboard", "templates")
_HYPERVISOR_FLAGS = ("proxmox", "vsphere", "hyperv", "nutanix", "xcpng", "vmware")


def _read(*parts) -> str:
    with open(os.path.join(_ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


def _route(path: str) -> str:
    """The decorator head + body of one `@app.get("<path>")` HTML page route."""
    main = _read("web_dashboard", "main.py")
    for chunk in main.split("@app.get(")[1:]:
        if chunk.split('"')[1] != path:
            continue
        return chunk.split("\n@app.")[0]
    raise AssertionError(f"no @app.get route for {path!r} in main.py")


# ── the old page is gone, and nothing still points at its template ───────────

def test_the_standalone_connections_template_is_deleted():
    assert not os.path.exists(os.path.join(_ROOT, *_TPL, "connections", "index.html")), \
        "connections/index.html is back — the page is a tab of agents/index.html now"


def test_no_route_renders_the_deleted_template():
    """A TemplateResponse naming a template that is not there is a 500, and it only shows
    up when somebody opens the page."""
    main = _read("web_dashboard", "main.py")
    assert "connections/index.html" not in main, \
        "main.py still renders connections/index.html, which no longer exists"


# ── two routes, one template, different gates ────────────────────────────────

def test_both_routes_render_the_hub_on_the_right_tab():
    """/connections is not a redirect, and the tab it opens on comes from the ROUTE.

    A 301 would have dropped the query string, and the discovery hand-off on a job page
    sends ?add=1&kind=…&host=… straight into the add form.
    """
    for path, tab in (("/agents", "agents"), ("/connections", "connections")):
        body = _route(path)
        assert "agents/index.html" in body, f"{path} does not render the hub template"
        assert f'"initial_tab": "{tab}"' in body, \
            f"{path} must open the page on the {tab!r} tab"


def test_connections_keeps_its_own_any_of_six_gate():
    """The gate the persona cards read — see tests/test_personas.py, which parses this
    exact shape out of the route body to decide whether a card may offer a live link.

    It cannot move onto /agents: the two gates differ, and it must stay on an HTML page
    route for that parser to see it at all.
    """
    body = _route("/connections")
    assert "HTMLResponse" in body, \
        "/connections must stay an HTML page route — tests/test_personas.py only parses those"
    assert "status_code=404" in body, "/connections lost its 404 guard"
    m = re.search(r'any\(\s*flags\.get\(f"\{(\w+)\}_enabled"\)\s*for\s+\1\s+in\s*\(([^)]*)\)',
                  body, re.S)
    assert m, "the inline any-of guard changed shape; tests/test_personas.py is now blind"
    assert set(re.findall(r'"(\w+)"', m.group(2))) == set(_HYPERVISOR_FLAGS), \
        "the guard must name every hypervisor kind — the list holds rows for all of them"


def test_agents_stays_ungated_on_purpose():
    """With remote agents off, the Agents panel is the only thing that tells an operator
    where the switch lives. A 404 cannot say that, which is why this route has no gate —
    matching /k8s and the other feature pages."""
    body = _route("/agents")
    assert "_feature_gate" not in body, \
        "/agents grew a gate: the 'not enabled' panel is the page's whole value in that state"
    assert "status_code=404" not in body, "/agents must not 404 when the feature is off"


# ── the hub and its two panels ───────────────────────────────────────────────

def test_the_hub_declares_both_tabs():
    hub = _read(*_TPL, "agents", "index.html")
    tabs = set(re.findall(r"activeTab === '(\w+)'", hub))
    assert {"agents", "connections"} <= tabs, f"hub tabs are {sorted(tabs)}"
    for partial in ("agents/_agents.html", "agents/_connections.html"):
        assert f'{{% include "{partial}" %}}' in hub, f"the hub does not include {partial}"


def test_each_tab_is_gated_on_its_own_integration():
    """A panel rendering against a router that 404s is the nav-link-to-404 bug by another
    door: the Agents tab needs the feature on, and the Connections tab needs a hypervisor.
    """
    hub = _read(*_TPL, "agents", "index.html")
    assert "{% if remote_agents_enabled %}" in hub, "the Agents tab is not flag-gated"
    any_of = " or ".join(f"{k}_enabled" for k in _HYPERVISOR_FLAGS)
    assert any_of in hub, \
        ("the Connections tab must be gated on ALL SIX hypervisor flags, matching the "
         "/connections route: the list holds rows for every kind, so gating it on one "
         f"would hide the others'. Expected: {any_of}")


def test_the_page_renames_itself_when_there_is_no_agents_tab():
    """Remote agents off, a hypervisor on: no Agents tab and no tab bar, so a heading
    saying "Remote Agents" would name a feature this install does not have over the top of
    the only list on the page. It is the Connections page again in that configuration.

    The condition is computed at TEMPLATE TOP LEVEL, not inside `content`, because the
    <title> block has to see it too.
    """
    hub = _read(*_TPL, "agents", "index.html")
    assert "{% set connections_only = tab_slugs == ['connections'] %}" in hub, \
        "the single-tab rename is gone, or no longer derived from the tab list"
    title = re.search(r"{% block title %}(.*?){% endblock %}", hub, re.S)
    assert title and "connections_only" in title.group(1), \
        "the <title> does not follow the rename"
    assert hub.index("{% set connections_only") < hub.index("{% block title %}"), \
        "the condition must be set before the title block that reads it"


def test_the_hub_passes_its_tab_list_through_a_single_quoted_attribute():
    """|tojson does not escape `"`, so in a DOUBLE-quoted attribute the array's first quote
    ends the attribute: Alpine throws on the truncation, the container never initialises,
    x-cloak is never lifted and the page renders its heading and nothing else. This shipped
    once on the Workload Lab container and left all four tabs blank."""
    hub = _read(*_TPL, "agents", "index.html")
    assert re.search(r"x-data='remoteAgentsPage\(.*tojson.*\)'", hub), \
        "the container's x-data must be SINGLE-quoted around the tojson tab list"


def test_each_panel_defines_the_factory_it_names():
    """Self-containment is required rather than tidy: tests/test_template_scripts.py and
    test_templates_parse.py check that any template carrying an x-data defines it. Note a
    script tag inside an `x-if` template is cloned rather than executed, so lazy-mounting a
    panel that way would silently leave a dead x-data."""
    for name, factory in (("_agents.html", "agentsPage"),
                          ("_connections.html", "connectionsPage")):
        src = _read(*_TPL, "agents", name)
        assert f'x-data="{factory}()"' in src, f"{name} does not mount {factory}()"
        assert re.search(r"\nfunction %s\(" % factory, src), \
            f"{name} names {factory}() but does not define it in the same file"


def test_both_panels_explain_a_403_instead_of_flashing_it():
    """Both components mount on page load, so a user holding only one of the two scopes
    (`agents` and `connections` are separate, and both are require_explicit_permission)
    gets a 403 from the other tab. A red toast about a list the reader never asked for
    reads as the whole page being broken."""
    for name in ("_agents.html", "_connections.html"):
        src = _read(*_TPL, "agents", name)
        assert "isDenied(e)" in src, f"{name} does not recognise a permission denial"
        assert "noAccess" in src, f"{name} has no in-place notice for a denial"


# ── the nav ──────────────────────────────────────────────────────────────────

def test_the_nav_offers_one_route_to_the_page_per_viewer():
    """Both links go to the same page now, so the Connections row renders only for the
    viewers the Agents row is not there for: with remote agents off it is absent entirely,
    and when they are on it is admin-only while the connection list is governed by the
    `connections` scope. Deleting the Connections row instead would leave a
    directly-dialled vCenter, or a non-admin with connections permission, with no nav route
    to the page at all."""
    nav = _read(*_TPL, "_nav_links.html")
    conn = re.search(r"<a[^>]*data-nav=\"connections\"[^>]*>", nav, re.S)
    assert conn, "the Connections nav link is gone — a non-admin would have no route to it"
    assert 'x-show="!$store.auth.isAdmin"' in conn.group(0), \
        "an admin would see two nav links to the same page"
    agents = re.search(r"<a[^>]*data-nav=\"agents\"[^>]*>", nav, re.S)
    assert agents, "the Agents nav link is gone"
    assert "/connections" in agents.group(0), \
        "the Agents link must light up on /connections too — it is the same page"


def test_the_discovery_hand_off_still_targets_a_real_route():
    """The reason /connections was kept rather than redirected. A finding on a job page
    hands off with the non-secret fields prefilled, and the form reads them off the query
    string — which a 301 to a fragment would have thrown away."""
    jobs = _read(*_TPL, "jobs", "detail.html")
    assert "'/connections?' + p.toString()" in jobs, \
        "the hand-off URL changed; check it still reaches the Connections tab with its query"
    panel = _read(*_TPL, "agents", "_connections.html")
    assert "window.location.search" in panel, \
        "the Connections panel no longer reads the query string the hand-off sends"


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
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
