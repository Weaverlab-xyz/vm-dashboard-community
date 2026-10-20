"""A page whose routes take a grantable scope must be reachable by whoever holds the scope.

It was not. Six nav links -- Templates, Images, Storage, Costs, Agents, Audit -- were
``x-show="$store.auth.isAdmin"`` although every route behind them answers to a permission
scope, so a role or group mapping granting ``pov_templates:write`` (found live: a POV
Presenter mapping) granted the API and hid the page. The browser's auth store knew nothing
but ``isAdmin``. It now carries the effective permission map ``/api/auth/me`` reports, and
``$store.auth.can(scope, level)`` answers from it (always true for an administrator).

What this file holds in place:
  * every nav link in ``_NAV_SCOPE`` that is gated at all is gated on ``can()`` with ITS
    OWN scope, never on bare ``isAdmin``;
  * exactly the require_admin pages keep ``isAdmin``: Desktops, Health, Secrets, RBAC;
  * both login paths and the refresh write the map through one writer, and both sign-outs
    clear it;
  * ``can()`` itself behaves (run under node when it is installed, as on CI);
  * the two in-page buttons the audit found: Functions' Entitle registration (scoped) and
    the workstream Delete button (admin-only, matching its route).

Runs standalone:
    python tests/test_nav_scope_gates.py
"""
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)


def _read(*parts):
    with open(os.path.join(_ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


def _catalog():
    """test_permission_catalog's _NAV_SCOPE / _NAV_EXEMPT: one map of nav section -> scope,
    not a second copy that could drift from it."""
    spec = importlib.util.spec_from_file_location(
        "_perm_catalog", os.path.join(_ROOT, "tests", "test_permission_catalog.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod._NAV_SCOPE, mod._NAV_EXEMPT


def _nav_links():
    """data-nav -> the link's opening tag."""
    nav = _read("web_dashboard", "templates", "_nav_links.html")
    return {m.group(1): m.group(0)
            for m in re.finditer(r'<a\b[^>]*?data-nav="([a-z_]+)"[^>]*>', nav, re.S)}


# Sections whose routes are require_admin, so isAdmin is the honest gate.
_ADMIN_ONLY = {"desktops", "health", "secrets", "rbac"}


def test_scoped_links_show_on_their_own_scope():
    scope_of, _ = _catalog()
    links = _nav_links()
    assert len(links) >= 25, f"only {len(links)} nav links parsed -- the regex stopped matching"
    checked = 0
    for nav, tag in sorted(links.items()):
        scope = scope_of.get(nav)
        if not scope:
            continue
        assert "$store.auth.isAdmin\"" not in tag.replace("!$store", "$store"), (
            f"nav link {nav!r} is hidden behind isAdmin, but its routes take the {scope!r} "
            "scope -- use $store.auth.can(scope, level)")
        for used in re.findall(r"\$store\.auth\.can\('([a-z_]+)'", tag):
            # connections is the complement of the Agents link (one route per viewer).
            want = "agents" if nav == "connections" else scope
            assert used == want, f"nav link {nav!r} checks can({used!r}), not its scope {want!r}"
        checked += 1
    assert checked >= 20, f"checked {checked} scoped links; expected the whole catalog"


def test_the_six_links_this_fixed():
    links = _nav_links()
    want = {"pov_templates": "can('pov_templates', 'write')", "images": "can('images', 'read')",
            "storage": "can('storage', 'read')", "costs": "can('costs', 'read')",
            "agents": "can('agents', 'read')", "audit": "can('audit', 'read')"}
    for nav, expr in want.items():
        assert nav in links, f"the {nav} nav link is gone"
        assert expr in links[nav], f"{nav} should show on {expr}: {links[nav][:160]}"


def test_only_require_admin_pages_keep_isadmin():
    gated = {nav for nav, tag in _nav_links().items()
             if 'x-show="$store.auth.isAdmin"' in tag}
    assert gated == _ADMIN_ONLY, (
        f"isAdmin-only nav links are {sorted(gated)}, expected {sorted(_ADMIN_ONLY)}. A new "
        "admin-only link needs routes that are require_admin too; a scoped one uses can()")


def test_the_store_is_fed_by_one_writer_and_cleared_on_sign_out():
    app = _read("web_dashboard", "static", "js", "app.js")
    store = app[:app.index("// ── API helper")]
    assert "can(scope, level) {" in store and "applyProfile(me) {" in store
    assert "localStorage.getItem('vm_cli_permissions')" in store
    logout = store[store.index("logout() {"):][:1200]
    assert "localStorage.removeItem('vm_cli_permissions')" in logout
    assert "refreshProfileIfStale();" in app[app.index("function responsiveNav()"):], (
        "the nav no longer refreshes a missing or stale permission map")
    login = _read("web_dashboard", "templates", "login.html")
    assert login.count("Alpine.store('auth').applyProfile(") == 2, (
        "both login paths (password and the SSO #token callback) must record the profile")
    access = _read("web_dashboard", "templates", "pov", "access.html")
    assert "'vm_cli_permissions'" in access, "an accessor's sign-out leaves a permission map"


def test_the_in_page_buttons_match_their_routes():
    fns = _read("web_dashboard", "templates", "functions", "index.html")
    assert "$store.auth.can('cloud_function', 'write') && isAdapter(fn)" in fns
    assert 'x-show="$store.auth.isAdmin" @click="showCreds(fn)"' in fns, (
        "the Endpoint button hands out credentials (require_admin) and must stay admin-only")
    wgs = _read("web_dashboard", "templates", "rbac", "_workgroups.html")
    assert re.search(r'x-show="isAdmin"\s+@click="deleteWorkgroup\(w\)"', wgs), (
        "the workstream Delete button shows to non-admins, and DELETE is require_admin")


def _node():
    return shutil.which("node") or os.environ.get("NODE")


def test_can_behaves():
    node = _node()
    if not node:
        print("   (skipped: node not installed)")
        return
    app = _read("web_dashboard", "static", "js", "app.js")
    start = app.index("can(scope, level) {")
    body = app[start:app.index("\n        },", start) + len("\n        }")]
    js = f"""
const mk = (isAdmin, permissions) => ({{ isAdmin, permissions, {body} }});
const out = {{
  admin_empty: mk(true, {{}}).can('audit', 'read'),
  granted: mk(false, {{pov_templates: ['read', 'write']}}).can('pov_templates', 'write'),
  other_level: mk(false, {{pov_templates: ['read']}}).can('pov_templates', 'write'),
  other_scope: mk(false, {{pov_templates: ['write']}}).can('audit', 'read'),
  empty: mk(false, {{}}).can('agents', 'read'),
  null_map: mk(false, null).can('agents', 'read'),
  not_a_list: mk(false, {{agents: 'read'}}).can('agents', 'read'),
}};
console.log(JSON.stringify(out));
"""
    proc = subprocess.run([node, "-e", js], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    got = json.loads(proc.stdout.strip().splitlines()[-1])
    assert got == {"admin_empty": True, "granted": True, "other_level": False,
                   "other_scope": False, "empty": False, "null_map": False,
                   "not_a_list": False}, got


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
