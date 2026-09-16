"""The /rbac page: three routes onto one template, and the two failures nothing else sees.

Users and Groups were separate pages. Merging them into tabs of one document introduces two
break modes that the whole existing suite is blind to, and both take the page down silently:

  * **A duplicated module-scope `const`.** Each old page declared the permission catalog at
    script scope, and the Groups one suffixed its copies to avoid a clash that could not
    happen while they were separate documents. In one document, two declarations of one name
    is a SyntaxError that kills BOTH script blocks -- the heading renders and nothing else.
    `test_template_scripts.py` lexes each block independently, so both lex fine.
  * **A missing macro import.** An `{% include %}` does not inherit the includer's
    `{% from %}`, and a missing import fails at RENDER time. `test_templates_parse.py` only
    parses, so it would never see it.

Source assertions only -- no app import, no client -- matching tests/test_connections_tab_wiring.py,
which pins the Agents/Connections consolidation this page copies.

Runs under pytest, or standalone:
    python tests/test_rbac_page_wiring.py
"""
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

_TPL = os.path.join(_ROOT, "web_dashboard", "templates")
_RBAC = os.path.join(_TPL, "rbac")
_PANELS = ("_users.html", "_groups.html", "_roles.html")


def _read(*parts):
    with open(os.path.join(_ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


def _hub():
    return _read("web_dashboard", "templates", "rbac", "index.html")


def _panel(name):
    return _read("web_dashboard", "templates", "rbac", name)


def _main():
    return _read("web_dashboard", "main.py")


def _route_body(path):
    """The source of one HTML page route, from its decorator to the next one."""
    src = _main()
    marker = '@app.get("%s", response_class=HTMLResponse' % path
    assert marker in src, "no HTML route for %s" % path
    return src.split(marker, 1)[1].split("@app.get(", 1)[0]


# ── the old pages are gone ────────────────────────────────────────────────────

def test_the_standalone_users_and_groups_templates_are_deleted():
    """Not kept as thin includes. A one-line `users/list.html` that includes the partial is a
    SECOND page: it would render without the container's x-data, without the tab bar and
    without the shared admin gate. It would also keep every repointed assertion below green
    while checking nothing.
    """
    for rel in (("users", "list.html"), ("groups", "index.html")):
        assert not os.path.exists(os.path.join(_TPL, *rel)), (
            "templates/%s is back — two entry surfaces for one page is what this "
            "consolidation removed" % "/".join(rel))


def test_no_route_renders_a_deleted_template():
    src = _main()
    # Quote-bounded on purpose: a bare substring test for "groups/index.html" also matches
    # "workgroups/index.html", which is a live page, and reports it as a deleted one.
    for gone in ('"users/list.html"', '"groups/index.html"'):
        assert gone not in src, "main.py still renders %s" % gone


# ── three routes, one template, no gate ───────────────────────────────────────

def test_all_three_routes_render_the_page_on_the_right_tab():
    for path, tab in (("/rbac", "users"), ("/users", "users"), ("/groups", "groups")):
        body = _route_body(path)
        assert "rbac/index.html" in body, "%s does not render the RBAC page" % path
        assert '_rbac_context(request, "%s")' % tab in body, (
            "%s does not open on the %s tab" % (path, tab))


def test_every_initial_tab_is_a_tab_the_container_can_render():
    """A route naming a slug the container does not have falls back to the first tab -- so
    the page still works and the route's intent is silently lost."""
    hub = _hub()
    slugs = set(re.findall(r"'slug': '([a-z-]+)'", hub))
    assert slugs, "could not read the container's tab list"
    for want in re.findall(r'_rbac_context\(request, "([a-z-]+)"\)', _main()):
        assert want in slugs, (
            "a route opens on %r, which is not one of the container's tabs: %s"
            % (want, sorted(slugs)))


def test_the_legacy_paths_are_real_routes_and_not_redirects():
    """Runbooks and the single-sign-on settings panel print /users and /groups as things to
    open, and a 301 cannot carry a query string."""
    for path in ("/users", "/groups"):
        body = _route_body(path)
        assert "RedirectResponse" not in body, "%s became a redirect" % path
        assert "status_code=30" not in body, "%s returns a 3xx" % path


def test_the_rbac_routes_are_deliberately_ungated():
    """Identity administration exists on every install, so there is no flag to hide it
    behind -- and no grantable scope either, because anyone who can edit a user or a role
    can make themselves an administrator."""
    for path in ("/rbac", "/users", "/groups"):
        body = _route_body(path)
        assert "_feature_gate" not in body, "%s grew a feature gate" % path
        assert "_profile_page_gate" not in body, "%s grew a profile gate" % path
        assert "status_code=404" not in body, "%s can 404" % path


def test_the_shared_context_holds_no_key_the_processor_would_overwrite():
    """`_profile_context`'s return is applied AFTER a route's own context and overwrites it
    silently, so a clashing key simply does not arrive with nothing to debug. The reason
    the persona picker's key is `persona_options` and not `persona`.

    Needed as its own test because test_install_profile.py's ast sweep looks for dict
    LITERALS passed to TemplateResponse -- a `_rbac_context(...)` call is invisible to it, so
    these three routes would pass that sweep without their keys ever being checked.
    """
    src = _main()
    helper = src.split("def _rbac_context(", 1)[1].split("\n\n\n", 1)[0]
    ours = set(re.findall(r'"([a-z_]+)":', helper))
    assert ours, "could not read the helper's keys"

    proc = src.split("def _profile_context(", 1)[1].split("\ntemplates = ", 1)[0]
    theirs = set(re.findall(r'"([a-z_]+)":', proc))
    clash = (ours & theirs) - {"request"}
    assert not clash, (
        "_rbac_context returns keys the context processor also returns, so the route's own "
        "values are silently discarded: %s" % sorted(clash))


def test_the_role_list_is_not_rendered_into_the_page():
    """Every HTML route here is an unauthenticated shell -- the token lives in localStorage
    and is only sent as a Bearer header on /api/*. So anything rendered into this template is
    readable by an anonymous GET. The permission catalog is a static property of the build and
    safe; the roles an operator has defined are their configuration, and the pickers fetch
    them from /api/roles, which is require_admin.
    """
    src = _main()
    helper = src.split("def _rbac_context(", 1)[1].split("\n\n\n", 1)[0]
    assert '"roles"' not in helper, (
        "the role list is injected into an unauthenticated HTML route, so an anonymous GET "
        "can read every role name — fetch it from /api/roles instead")
    for name in _PANELS:
        assert "{{ roles" not in _panel(name), "%s renders a server-injected role list" % name


# ── the container ─────────────────────────────────────────────────────────────

def test_the_container_declares_all_three_tabs():
    hub = _hub()
    for slug in ("users", "groups", "roles"):
        assert "activeTab === '%s'" % slug in hub, "no panel for the %s tab" % slug
        assert 'rbac/_%s.html' % slug in hub, "the %s panel is not included" % slug


def test_the_container_passes_its_tab_list_through_a_single_quoted_attribute():
    """|tojson does not escape a double quote, so in a DOUBLE-quoted attribute the first one
    ends the attribute: Alpine throws on the truncation, the container never initialises,
    x-cloak is never lifted, and every panel stays hidden while the tab bar renders. The
    page looks built and does nothing."""
    hub = _hub()
    assert re.search(r"x-data='rbacPage\([^']*tojson[^']*\)'", hub), (
        "the container's x-data is not a single-quoted rbacPage(...) call carrying |tojson")


def test_every_panel_toggles_with_x_show_not_x_if():
    """A script tag inside an x-if template is CLONED rather than executed, so lazy-mounting
    a panel that way leaves a dead x-data and no error anywhere."""
    hub = _hub()
    for slug in ("users", "groups", "roles"):
        m = re.search(r"<div ([^>]*activeTab === '%s'[^>]*)>" % slug, hub)
        assert m, "could not find the %s panel wrapper" % slug
        attrs = m.group(1)
        assert "x-show=" in attrs, "the %s panel does not use x-show" % slug
        assert "x-if" not in attrs, "the %s panel uses x-if" % slug


def test_the_container_owns_the_only_admin_gate():
    """All three components mount on one page load. Three separate gates -- which is what
    the two old pages had, by two different mechanisms -- is three redirects to / racing
    each other."""
    sources = [_hub()] + [_panel(n) for n in _PANELS]
    total = sum(s.count("window.location.href = '/'") for s in sources)
    assert total == 1, (
        "expected exactly one redirect-to-root across the RBAC page, found %d — see the "
        "gate in rbacPage()" % total)
    assert "window.location.href = '/'" in _hub(), "the one gate is not in the container"


def test_the_container_owns_the_only_scripts_block():
    """Two sibling includes cannot each own `{% block scripts %}`, and an include cannot
    contribute to a parent's block at all -- so a partial declaring one silently drops its
    whole component."""
    assert "{% block scripts %}" in _hub()
    for name in _PANELS:
        assert "{% block" not in _panel(name), (
            "%s declares a Jinja block; an included partial cannot own one" % name)


# ── the partials ──────────────────────────────────────────────────────────────

def test_each_panel_defines_the_factory_it_names():
    for name, fn in zip(_PANELS, ("usersPage", "groupsPage", "rolesPage")):
        src = _panel(name)
        assert 'x-data="%s()"' % fn in src, "%s does not mount %s" % (name, fn)
        assert "function %s(" % fn in src, (
            "%s names %s but does not define it -- a factory split into a sibling file is "
            "what test_template_scripts.py forbids" % (name, fn))


def test_each_panel_that_renders_the_grid_imports_the_macro_itself():
    """An include does not inherit the container's import, and a missing one fails at RENDER
    time -- test_templates_parse.py only parses, so nothing else would catch it."""
    for name in _PANELS:
        src = _panel(name)
        if "permission_matrix(" not in src:
            continue
        assert 'from "partials/permission_matrix.html" import permission_matrix' in src, (
            "%s calls permission_matrix without importing it" % name)


def test_no_two_panels_declare_the_same_module_global():
    """The SyntaxError this whole file exists for. Two declarations of one name in one
    document's global scope kills both script blocks, and the per-block lexer in
    test_template_scripts.py sees nothing wrong with either."""
    seen = {}
    dupes = []
    for name in ["index.html"] + list(_PANELS):
        src = _panel(name) if name != "index.html" else _hub()
        # Anchored at column ZERO, which is what module scope looks like inside a script
        # body here. An INDENTED `const` is function-local -- `const r = ...` inside a
        # method cannot collide with another file's, and every panel has several.
        for m in re.finditer(r"^(?:const|let|var|function)\s+([A-Za-z_$][\w$]*)",
                             src, re.M):
            ident = m.group(1)
            if ident in seen and seen[ident] != name:
                dupes.append("%s declared in both %s and %s" % (ident, seen[ident], name))
            seen.setdefault(ident, name)
    assert not dupes, "; ".join(dupes)


def test_the_catalog_reaches_each_component_without_a_module_global():
    """Inlined as component properties, which removes the collision class above rather than
    managing it with name suffixes the way the two separate pages did."""
    for name in _PANELS:
        src = _panel(name)
        if "permissionGridState()" not in src and "permission_matrix(" not in src:
            continue
        assert "permission_scope_levels | tojson" in src, (
            "%s does not receive the per-scope level map, so its grid can offer a level "
            "the server will 422" % name)
        assert not re.search(r"^\s*const PERMISSION", src, re.M), (
            "%s declares the catalog at module scope again" % name)


# ── the Roles tab ─────────────────────────────────────────────────────────────

def test_the_roles_grid_does_not_offer_unrestricted():
    """"Unrestricted" is stored as an EMPTY map, and an empty map means unrestricted only on
    a PRINCIPAL. On a role it would hand every assignee every scope, present and future --
    the opposite of what building a restricted role means. The server refuses it too."""
    src = _panel("_roles.html")
    assert "offer_unrestricted=False" in src, (
        "the Roles tab offers the unrestricted escape hatch, which on a role grants "
        "everything to everyone who holds it")
    macro = _read("web_dashboard", "templates", "partials", "permission_matrix.html")
    assert "{% if offer_unrestricted %}" in macro, (
        "the shared grid always renders the unrestricted checkbox, so the Roles tab cannot "
        "suppress it")


def test_a_builtin_role_offers_no_edit_affordance():
    """Enforced by the ABSENCE of a control rather than a disabled grid: 31 rows of disabled
    checkboxes invites "why can't I tick this?", and a rule expressed as a :disabled binding
    can be defeated by a stale expression."""
    src = _panel("_roles.html")
    assert 'x-show="!role.is_builtin" @click="openEdit(' in src, (
        "the Edit control is not withheld from a built-in role")
    assert 'x-show="!role.is_builtin" @click="confirmDelete(' in src, (
        "the Delete control is not withheld from a built-in role")
    assert "openClone(role)" in src, "there is no clone path, which is the supported way "\
                                     "to adapt a built-in"


def test_the_roles_flyout_is_a_slide_over_with_a_pinned_footer():
    """A centred dialog holding this grid put the title off the top of the viewport and Save
    off the bottom with nothing to scroll -- permissions were literally ungrantable. The same
    grid lives here."""
    src = _panel("_roles.html")
    assert "translate-x-full" in src, "the roles panel is not a slide-over"
    assert "flex-1 overflow-y-auto" in src, "the roles flyout body does not scroll"
    assert "flex-shrink-0" in src, "the roles flyout footer is not pinned"


def test_the_roles_panel_explains_a_denial_in_place():
    """All three panels mount at load, so a 403 from this one must not read as the whole
    page being broken for somebody who came to look at users."""
    src = _panel("_roles.html")
    assert "isDenied(" in src and "noAccess" in src


def test_the_clone_deep_copies_the_source_permissions():
    """A shallow copy would let editing the clone mutate the list row it came from, so
    cancelling would still have changed what the table shows."""
    src = _panel("_roles.html")
    assert "JSON.parse(JSON.stringify(" in src


# ── the nav ───────────────────────────────────────────────────────────────────

def test_one_nav_link_lights_on_all_three_paths():
    nav = _read("web_dashboard", "templates", "_nav_links.html")
    assert nav.count('data-nav="rbac"') == 1, "expected exactly one RBAC nav link"
    assert 'href="/rbac"' in nav
    m = re.search(r'data-nav="rbac".*?>RBAC</a>', nav, re.S)
    assert m, "could not read the RBAC nav anchor"
    anchor = m.group(0)
    for path in ("/rbac", "/users", "/groups"):
        assert "'%s'" % path in anchor, (
            "the RBAC link does not light on %s, so an admin who followed a runbook there "
            "has no lit link and reads as 'you are nowhere'" % path)


def test_the_old_nav_links_are_gone():
    """One link, not two. The Agents precedent keeps a second row for non-admins because its
    Connections tab is governed by the `connections` SCOPE; both rows here were admin-only
    with no flag and no scope, so no viewer reaches one tab and not the other."""
    nav = _read("web_dashboard", "templates", "_nav_links.html")
    for gone in ('data-nav="users"', 'data-nav="groups"'):
        assert gone not in nav, "%s is still in the nav" % gone
    assert 'data-nav="workgroups"' in nav, (
        "the Workgroups link disappeared — it is a separate page from RBAC today")


def test_the_security_persona_pins_the_merged_link():
    """`applyPins()` skips a pin naming a link the instance lacks SILENTLY, so a stale pin
    is not a runtime error -- only test_persona_nav.py catches it."""
    src = _read("web_dashboard", "services", "personas.py")
    block = src.split("_SECURITY = Persona(", 1)[1].split("\n)", 1)[0]
    pins = re.search(r"nav_pins=\(([^)]*)\)", block)
    assert pins, "the security persona has no nav_pins"
    names = set(re.findall(r'"([a-z_]+)"', pins.group(1)))
    assert "rbac" in names, "the security persona does not pin the RBAC page"
    assert not (names & {"users", "groups"}), (
        "the security persona still pins a nav id that no longer exists")


def test_the_in_app_link_from_settings_points_at_the_page():
    settings = _read("web_dashboard", "templates", "settings.html")
    assert 'href="/groups"' not in settings, (
        "settings.html still links the old /groups path; it works, but /rbac#groups is the "
        "canonical URL now")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in fns:
        try:
            fn()
            print("ok   %s" % fn.__name__)
        except Exception as e:  # noqa: BLE001
            failures += 1
            print("FAIL %s: %s" % (fn.__name__, e))
    print("\n%d/%d passed" % (len(fns) - failures, len(fns)))
    sys.exit(1 if failures else 0)
