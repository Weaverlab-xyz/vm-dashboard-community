"""A scoped feature whose only screen was Settings → Integrations must have a home a
non-admin holding the scope can reach.

Settings → Integrations is admin-only, correctly: its drawers are instance configuration,
and the drawer core (/api/setup/feature/{key}, /api/setup/*/test) is require_admin. But
six features had their ONLY UI inside those drawers while their routes took a grantable
scope, so a role granting the scope granted an API and no way to use it:

  * notification endpoints   notifications:read / write      -> /notifications (new page)
  * change windows           change_windows:write            -> the Schedules page
  * agent signing audience   agents:read / write             -> /agents, Identity tab
  * SPIFFE identity + check  agents:read / write             -> /agents, Identity tab
  * DB-Ops services          cloud_database (+cloud_function) -> /databases
  * Rancher/Portainer firewall readouts   containers:read    -> /containers

What this file holds in place:
  * each home exists, is reachable (route, nav link or tab), and calls the routes it
    claims to;
  * every write control in it shows on $store.auth.can(<that route's scope>, 'write'),
    and the routes really do take those scopes (so the UI gate and the control cannot
    drift apart silently);
  * the two editors shared with Settings are ONE copy (a partial plus an app.js
    component), not a fork, and the require_admin delivery log stays admin-only;
  * the components behave, run under node when it is installed (as on CI).

Runs standalone:
    python tests/test_scoped_integration_homes.py
"""
import json
import os
import re
import shutil
import subprocess
import sys
from _settings_src import settings_source

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)


def _read(*parts):
    with open(os.path.join(_ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


def _tpl(*parts):
    return _read("web_dashboard", "templates", *parts)


def _app_js():
    return _read("web_dashboard", "static", "js", "app.js")


def _braced(text, start):
    """text[start:] up to and including the brace that closes the first `{` after start."""
    depth = 0
    for i in range(text.index("{", start), len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    raise AssertionError("unbalanced braces from offset %d" % start)


def _function(text, name):
    m = re.search(r"\nfunction %s\s*\(" % name, text)
    assert m, f"function {name}() is not defined"
    return _braced(text, m.start() + 1)


def _method(text, name):
    m = re.search(r"\n\s*(?:async\s+)?%s\s*\([^)]*\)\s*\{" % name, text)
    assert m, f"method {name}() is not defined"
    return _braced(text, m.start() + len(m.group(0)) - len(m.group(0).lstrip()))


# ── the routes take the scopes the UI gates on ───────────────────────────────

def _gate(api_file, method, path):
    """The Depends(...) permission expression on one route: in its signature, or in the
    decorator's dependencies=[...] through a module-level alias (`_WRITE = Depends(...)`)."""
    src = _read("web_dashboard", "api", api_file)
    m = re.search(r"@\w*router\.%s\(\"%s\"([^\n]*)\)\s*\n(?:async\s+)?def\s+\w+\((.*?)\):"
                  % (method, re.escape(path)), src, re.S)
    assert m, f"{method.upper()} {path} not found in api/{api_file}"
    where = m.group(1) + m.group(2)
    for alias in re.findall(r"dependencies=\[(_[A-Z_]+)\]", m.group(1)):
        a = re.search(r"^%s = (Depends\(.*\))$" % alias, src, re.M)
        assert a, f"{alias} is not defined at module level in api/{api_file}"
        where += a.group(1)
    g = re.search(r"Depends\((require_\w+(?:\([^)]*\))?)\)", where)
    assert g, f"{method.upper()} {path} has no require_* dependency"
    return g.group(1).replace(" ", "")


def test_the_routes_still_take_the_scopes_the_homes_gate_on():
    want = {
        ("notifications.py", "get", "/endpoints"): 'require_explicit_permission("notifications","read")',
        ("notifications.py", "get", "/summary"): 'require_explicit_permission("notifications","read")',
        ("notifications.py", "post", "/endpoints"): 'require_explicit_permission("notifications","write")',
        ("notifications.py", "patch", "/endpoints/{endpoint_id}"): 'require_explicit_permission("notifications","write")',
        ("notifications.py", "delete", "/endpoints/{endpoint_id}"): 'require_explicit_permission("notifications","write")',
        ("notifications.py", "post", "/endpoints/{endpoint_id}/test"): 'require_explicit_permission("notifications","write")',
        ("notifications.py", "get", "/deliveries"): "require_admin",
        ("change_windows.py", "post", ""): 'require_explicit_permission("change_windows","write")',
        ("change_windows.py", "put", "/{window_id}"): 'require_explicit_permission("change_windows","write")',
        ("change_windows.py", "delete", "/{window_id}"): 'require_explicit_permission("change_windows","write")',
        ("agent.py", "get", "/audience"): 'require_explicit_permission("agents","read")',
        ("agent.py", "delete", "/audience"): 'require_explicit_permission("agents","write")',
        ("spiffe_oidc.py", "get", ""): 'require_explicit_permission("agents","read")',
        ("spiffe_oidc.py", "post", "/check-issuer"): 'require_explicit_permission("agents","write")',
        ("cloud_databases.py", "get", "/dbops/status"): 'require_permission("cloud_database","read")',
        ("cloud_databases.py", "post", "/dbops/deploy"): 'require_permission("cloud_database","write")',
        ("cloud_databases.py", "post", "/dbops/invokers"): 'require_permission("cloud_database","write")',
        ("containers.py", "get", "/rancher/firewall"): 'require_permission("containers","read")',
        ("containers.py", "get", "/portainer/node/firewall"): 'require_permission("containers","read")',
    }
    for (f, method, path), gate in want.items():
        assert _gate(f, method, path) == gate, (
            f"{method.upper()} {path} in api/{f} is {_gate(f, method, path)}, not {gate} -- "
            "the page's can() gate for it must change with it")


# ── notifications ────────────────────────────────────────────────────────────

def test_notifications_has_a_page_a_route_and_a_scoped_nav_link():
    main = _read("web_dashboard", "main.py")
    assert re.search(r'@app\.get\("/notifications"[^)]*\)\s*\nasync def \w+\(request: Request\):'
                     r'.*?"notifications/index\.html"', main, re.S), "no /notifications page route"
    page = _tpl("notifications", "index.html")
    assert 'x-data="notificationEndpoints()"' in page and "loadNotifications()" in page
    assert '{% include "partials/notification_endpoints.html" %}' in page
    nav = _tpl("_nav_links.html")
    link = re.search(r'{%\s*if notifications_enabled\s*%}\s*<a\b[^>]*data-nav="notifications"[^>]*>',
                     nav, re.S)
    assert link, "the Notifications nav link is missing or not behind notifications_enabled"
    assert "x-show=\"$store.auth.can('notifications', 'read')\"" in link.group(0)


def test_the_endpoint_editor_is_one_copy_shared_with_settings():
    part = _tpl("partials", "notification_endpoints.html")
    write = "$store.auth.can('notifications', 'write')"
    assert f'x-show="{write}" @click="newEndpoint()"' in part, "Add endpoint is not write-gated"
    row = re.search(r'<div x-show="([^"]+)" class="flex items-center gap-1 shrink-0">', part)
    assert row and row.group(1) == write, "Test/Edit/Delete are not write-gated"
    for call in ("testEndpoint(ep)", "editEndpoint(ep)", "deleteEndpoint(ep)"):
        assert part.index(call) > row.start(), f"{call} sits outside the write-gated row"
    assert re.search(r'<div x-show="\$store\.auth\.isAdmin"[^>]*>\s*<div class="flex items-center '
                     r'justify-between mb-2">\s*<span[^>]*>Recent deliveries', part), (
        "the delivery log is require_admin and must stay admin-only")

    settings = settings_source()
    drawer = settings[settings.index("panel?.key === 'notifications' && !panelLoading"):]
    drawer = drawer[:drawer.index("</template>\n\n      <template x-if=")]
    assert 'x-data="notificationEndpoints()" x-init="loadNotifications()"' in drawer
    assert '{% include "partials/notification_endpoints.html" %}' in drawer
    # The fork this replaced: a second copy of the state and methods on the root component.
    for gone in ("    notif: {", "async loadNotifications()", "async saveEndpoint()",
                 "notifSummaryLine() {", "await this.loadNotifications();"):
        assert gone not in settings, f"settings.html still defines {gone.strip()!r} itself"
    assert "function notificationEndpoints()" in _app_js()


# ── change windows ───────────────────────────────────────────────────────────

def test_change_windows_render_on_schedules_and_settings_from_one_copy():
    part = _tpl("partials", "change_windows_editor.html")
    write = "$store.auth.can('change_windows', 'write')"
    for call in ("cw.startNew()", "cw.edit(w)", "cw.remove(w)"):
        assert f'x-show="{write}" @click="{call}"' in part, f"{call} is not write-gated"

    sched = _tpl("schedules", "index.html")
    assert re.search(r'x-data="changeWindowsEditor\(\)" x-init="cw\.load\(\)">.*?'
                     r'{% include "partials/change_windows_editor\.html" %}', sched, re.S), (
        "the Schedules page does not render the change-window editor")

    settings = settings_source()
    assert 'x-data="changeWindowsEditor()" x-init="cw.load()"' in settings
    assert '{% include "partials/change_windows_editor.html" %}' in settings
    assert "    cw: {" not in settings, "settings.html still carries its own copy of `cw`"
    # The approval settings are instance configuration and stay in the drawer.
    assert 'x-model="panelCfg.change_approval_required"' in settings
    assert "panelCfg" not in part, "the shared editor must not bind instance settings"
    assert "function changeWindowsEditor()" in _app_js()


# ── agents: signing audience + SPIFFE identity ───────────────────────────────

def test_agents_page_has_a_scoped_identity_tab():
    hub = _tpl("agents", "index.html")
    tab = re.search(r"{%\s*if remote_agents_enabled\s*%}\s*{%\s*set _ = tabs\.append\("
                    r"\{'slug': 'identity'[^}]*\}\)\s*%}", hub)
    assert tab, "the Identity tab is missing or not behind remote_agents_enabled"
    assert "'show': \"$store.auth.can('agents', 'read')\"" in tab.group(0)
    assert re.search(r"x-show=\"activeTab === 'identity'\".*?{% include \"agents/_identity\.html\" %}",
                     hub, re.S)

    panel = _tpl("agents", "_identity.html")
    assert 'x-data="agentIdentityPanel()"' in panel and "function agentIdentityPanel()" in panel
    for route in ("API.get('/api/agents/audience')", "API.get('/api/spiffe-identity')",
                  "API.delete('/api/agents/audience')",
                  "API.post('/api/spiffe-identity/check-issuer'"):
        assert route in panel, f"the Identity tab no longer calls {route}"
    write = "$store.auth.can('agents', 'write')"
    reset = panel[:panel.index('@click="resetAudience()"')]
    assert reset.rfind(write) > reset.rfind("<div"), "Reset is not write-gated"
    check = panel[:panel.index('@click="checkIssuer()"')]
    assert check.rfind(write) > check.rfind("<div"), "the issuer check is not write-gated"

    agents = _tpl("agents", "_agents.html")
    assert "select('identity')" in agents, (
        "the blocked-enrolment banner should send a non-admin to the Identity tab, not to "
        "an admin-only Settings drawer")


# ── databases: DB-Ops ────────────────────────────────────────────────────────

def test_databases_page_offers_the_dbops_services():
    page = _tpl("databases", "index.html")
    sect = re.search(r"{%\s*if password_safe_enabled and cloud_functions_enabled\s*%}\s*"
                     r"<details[^>]*x-data=\"dbopsServices\(\)\"", page)
    assert sect, "the DB-Ops section is missing, or not behind both of its feature flags"
    fn = _function(page, "dbopsServices")
    for route in ("/api/databases/dbops/status", "/api/databases/dbops/deploy",
                  "/api/databases/dbops/invokers"):
        assert route in fn
    assert "auth.can('cloud_database', 'write') && auth.can('cloud_function', 'write')" in fn, (
        "deploy and sync check cloud_function:write inside the route too")
    assert '<div x-show="canWrite()" class="shrink-0 flex items-center gap-2">' in page


# ── containers: the firewall readouts ────────────────────────────────────────

def test_containers_page_reads_both_node_firewalls():
    page = _tpl("containers", "index.html")
    toggle = _method(page, "toggleFirewallView")
    assert "'/api/containers/rancher/firewall'" in toggle
    assert "'/api/containers/portainer/node/firewall'" in toggle
    assert "API.get(url)" in toggle, "the readout must be the read-only GET"
    for kind in ("rancher", "portainer"):
        assert f"toggleFirewallView('{kind}')" in page
        assert f'x-for="row in firewallRows({kind}FirewallView)"' in page


# ── the permissions doc ──────────────────────────────────────────────────────

def test_the_doc_no_longer_calls_them_admin_only():
    doc = _read("docs", "admin", "permissions.md")
    menu = doc[doc.index("## What the menu shows"):]
    menu = menu[:menu.index("\n## ", 5)]
    assert "can still only be reached by administrators" not in menu
    for home in ("/notifications", "Schedules", "Identity", "Databases", "Containers"):
        assert home in menu, f"'What the menu shows' does not say where {home} is"
    assert "| **Notifications** | `notifications` read" in menu


# ── behaviour, under node ────────────────────────────────────────────────────

def _node():
    return shutil.which("node") or os.environ.get("NODE")


def _run(js):
    proc = subprocess.run([_node(), "-e", js], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_components_behave():
    if not _node():
        print("   (skipped: node not installed)")
        return
    app = _app_js()
    identity = _tpl("agents", "_identity.html")
    js = f"""
global.toast = () => {{}};
global.confirm = () => true;
let calls = [];
const stubApi = (fail) => ({{
  get: async (u) => {{
    calls.push('GET ' + u);
    if (fail[u]) throw new Error(fail[u]);
    if (u === '/api/notifications/endpoints') return {{endpoints: [{{id: 'e1'}}], formats: ['slack'], known_events: ['x']}};
    if (u.startsWith('/api/notifications/deliveries')) return {{deliveries: [{{id: 'd1'}}]}};
    return {{ok: true}};
  }},
  post: async (u) => {{ calls.push('POST ' + u); return {{}}; }},
  put: async (u) => {{ calls.push('PUT ' + u); return {{}}; }},
  del: async (u) => {{ calls.push('DELETE ' + u); return {{}}; }},
  delete: async (u) => {{ calls.push('DELETE ' + u); return {{}}; }},
}});
{_function(app, 'notificationEndpoints')}
{_function(app, 'changeWindowsEditor')}
{_function(identity, 'agentIdentityPanel')}
(async () => {{
  const out = {{}};
  global.API = stubApi({{}});

  // A non-admin holding notifications:read never asks for the require_admin log.
  calls = [];
  let n = Object.assign(notificationEndpoints(), {{$store: {{auth: {{isAdmin: false}}}}}});
  await n.loadNotifications();
  out.nonadmin_calls = calls.slice().sort();
  out.nonadmin_endpoints = n.notif.endpoints.length;
  out.nonadmin_deliveries = n.notif.deliveries.length;
  calls = [];
  n = Object.assign(notificationEndpoints(), {{$store: {{auth: {{isAdmin: true}}}}}});
  await n.loadNotifications();
  out.admin_asked_deliveries = calls.some(c => c.startsWith('GET /api/notifications/deliveries'));
  out.admin_deliveries = n.notif.deliveries.length;

  // The change-window editor: a round trip through edit() keeps an unset zone unset,
  // and the day mask toggles.
  const c = changeWindowsEditor().cw;
  c.edit({{id: 'w1', name: 'Prod', timezone: 'UTC', schedule_days: '1000000'}});
  out.tz_roundtrip = c.draft.timezone;
  c.toggleDay(6);
  out.days = c.draft.schedule_days;
  out.day_off = c.dayOn(1);
  calls = [];
  await c.save();
  out.saved = calls;
  out.closed_after_save = c.draft === null;

  // The Identity tab: one 403 is the whole answer; the feature-off 404 is not an error.
  global.API = stubApi({{'/api/agents/audience': "Requires 'agents:read' permission.",
                        '/api/spiffe-identity': "Requires 'agents:read' permission."}});
  let p = agentIdentityPanel();
  await p.load();
  out.denied = p.noAccess;
  global.API = stubApi({{'/api/agents/audience': 'This integration is not enabled. Turn it on in Settings.'}});
  p = agentIdentityPanel();
  await p.load();
  out.off_denied = p.noAccess;
  out.off_message = p.audienceUnavailable;
  out.off_spiffe_loaded = !!p.spiffe;
  console.log(JSON.stringify(out));
}})().catch(e => {{ console.error(e); process.exit(1); }});
"""
    got = _run(js)
    assert got["nonadmin_calls"] == ["GET /api/notifications/endpoints",
                                     "GET /api/notifications/summary"], got
    assert got["nonadmin_endpoints"] == 1 and got["nonadmin_deliveries"] == 0, got
    assert got["admin_asked_deliveries"] is True and got["admin_deliveries"] == 1, got
    assert got["tz_roundtrip"] == "" and got["days"] == "1000001" and got["day_off"] is False, got
    assert got["saved"][0] == "PUT /api/change-windows/w1" and got["closed_after_save"], got
    assert got["denied"] is True, got
    assert got["off_denied"] is False and "Remote agents are off" in got["off_message"], got
    assert got["off_spiffe_loaded"] is True, got


def test_page_gates_behave():
    if not _node():
        print("   (skipped: node not installed)")
        return
    dbops = _function(_tpl("databases", "index.html"), "dbopsServices")
    rows = _method(_tpl("containers", "index.html"), "firewallRows")
    js = f"""
global.summarizeCidrs = (l) => (l || []).join(', ');
{dbops}
const store = (isAdmin, permissions) => ({{auth: {{isAdmin, permissions,
  can(s, l) {{ if (this.isAdmin) return true; const v = (this.permissions || {{}})[s];
               return Array.isArray(v) && v.includes(l); }}}}}});
const d = (st) => Object.assign(dbopsServices(), {{$store: st}});
const fw = {{ {rows} }};
const r = fw.firewallRows({{opened: true, dashboard_egress_ip: '203.0.113.9/32',
  merged: ['203.0.113.9/32', '198.51.100.0/24', '192.0.2.1/32'],
  dashboard_egress_pool: {{count: 1, cidrs: ['192.0.2.1/32']}},
  gateway_cidrs: ['198.51.100.0/24']}});
console.log(JSON.stringify({{
  admin: d(store(true, {{}})).canWrite(),
  both: d(store(false, {{cloud_database: ['read', 'write'], cloud_function: ['write']}})).canWrite(),
  db_only: d(store(false, {{cloud_database: ['read', 'write']}})).canWrite(),
  read_only: d(store(false, {{cloud_database: ['read'], cloud_function: ['write']}})).canWrite(),
  rows: Object.fromEntries(r),
  closed: fw.firewallRows({{opened: false, merged: []}})[0][1],
  none: fw.firewallRows(null).length,
}}));
"""
    got = _run(js)
    assert got["admin"] is True and got["both"] is True, got
    assert got["db_only"] is False and got["read_only"] is False, got
    # The outbound pool is counted, never listed, and left out of the merged list.
    assert got["rows"]["Merged allow-list"] == "203.0.113.9/32, 198.51.100.0/24", got
    assert got["rows"]["Hosting platform outbound pool"] == "1 addresses", got
    assert got["closed"].startswith("CLOSED"), got
    assert got["none"] == 0, got


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            import traceback
            traceback.print_exc()
            print(f"FAIL {fn.__name__}: {exc}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
