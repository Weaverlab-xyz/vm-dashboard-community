"""The drawer's links sit in groups, and a group never hides a page or shows an empty heading.

The drawer was one flat list of ~32 links. It is now a handful of collapsible groups named
after the tabs of Settings, Integrations -- Hypervisors, Clouds, Cloud services, Preview,
Operations, Admin -- with Dashboard and the POV pages left at the top. What this file holds
in place:
  * every link is either top-level or in exactly one group, and the groups are the ones
    named here;
  * Cloud services is exactly the pages whose Settings toggles sit on the `cloud` tab, and
    Preview exactly the pages whose flags are preview flags;
  * a group is guarded by the OR of its links' flags, so it never renders as a heading
    over nothing;
  * a tap on a heading does not close the drawer, a tap on a link does;
  * responsiveNav opens the lit link's group, and pinning hides an emptied group rather
    than removing it.

Runs under pytest, or standalone:
    python tests/test_nav_groups.py
"""
import os
import re
import sys
from _settings_src import settings_source

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-nav-groups")

_TPL = os.path.join(_ROOT, "web_dashboard", "templates")
_NAV = os.path.join(_TPL, "_nav_links.html")
_BASE = os.path.join(_TPL, "base.html")
_APP_JS = os.path.join(_ROOT, "web_dashboard", "static", "js", "app.js")

_TOP_LEVEL = {"dashboard", "pov", "pov_templates"}
_GROUPS = ("hypervisors", "clouds", "cloud_services", "preview", "operations", "admin")


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _groups():
    """``{group_id: (opening_tag, body)}`` for every details group in the link list."""
    src = _read(_NAV)
    out = {}
    for m in re.finditer(r'(<details class="nav-group" data-nav-group="([a-z_]+)"[^>]*>)(.*?)</details>',
                         src, re.S):
        assert m.group(2) not in out, f"two groups share the id {m.group(2)!r}"
        out[m.group(2)] = (m.group(1), m.group(3))
    return out


def _ids(text):
    return set(re.findall(r'<a\b[^>]*?data-nav="([a-z0-9_]+)"', text, re.S))


def _guard_before(group_id):
    """The Jinja if-test directly above a group's details tag, or None."""
    src = _read(_NAV)
    i = src.index(f'data-nav-group="{group_id}"')
    before = src[:src.rindex("<details", 0, i)].rstrip()
    m = re.search(r"{%\s*if\s+([^%]+?)\s*%}$", before)
    return m.group(1) if m else None


def test_the_groups_are_the_ones_named():
    assert tuple(_groups()) == _GROUPS, f"groups are {tuple(_groups())}, expected {_GROUPS}"


def test_every_link_is_top_level_or_in_exactly_one_group():
    all_ids = _ids(_read(_NAV))
    assert len(all_ids) >= 30, "the nav link list shrank unexpectedly"
    seen = {}
    for gid, (_tag, body) in _groups().items():
        for nav in _ids(body):
            assert nav not in seen, f"{nav!r} is in both {seen[nav]!r} and {gid!r}"
            seen[nav] = gid
    loose = all_ids - set(seen)
    assert loose == _TOP_LEVEL, (
        f"links outside any group are {sorted(loose)}, expected {sorted(_TOP_LEVEL)}")


def test_the_hypervisors_group_holds_the_on_prem_consoles():
    body = _groups()["hypervisors"][1]
    assert {"vms", "proxmox", "vsphere", "hyperv", "nutanix", "xcpng", "connections"} == _ids(body)


def test_the_clouds_group_holds_the_four_consoles():
    assert _ids(_groups()["clouds"][1]) == {"aws", "azure", "gcp", "oci"}


def test_cloud_services_is_the_settings_cloud_tab():
    """The Settings integrations on the `cloud` tab that own a page. Discovery and
    multi-region are settings of the cloud consoles, not pages, so they have no link."""
    settings = settings_source()
    cloud_keys = set(re.findall(r"\{ key: '([a-z0-9_]+)',[^}]*?group: 'cloud'", settings))
    page_of = {"portainer": "containers", "cloud_database": "databases",
               "k8s_management": "k8s", "cloud_functions": "functions"}
    assert set(page_of) <= cloud_keys, (
        f"Settings' cloud tab no longer holds {sorted(set(page_of) - cloud_keys)}")
    assert _ids(_groups()["cloud_services"][1]) == set(page_of.values())


def test_preview_is_the_preview_flagged_pages():
    from web_dashboard.api.setup import _PREVIEW_FLAGS
    from web_dashboard.services.feature_flags import _DERIVED
    body = _groups()["preview"][1]
    for nav in _ids(body):
        # the nearest flag test above the link inside the group
        head = body[:body.index(f'data-nav="{nav}"')]
        flag = re.findall(r"{%\s*if\s+([a-z0-9_]+)\s*%}", head)[-1]
        parts = _DERIVED.get(flag, (flag,))
        assert all(p in _PREVIEW_FLAGS for p in parts), (
            f"{nav!r} is under Preview but its flag {flag!r} is not a preview flag")
    assert _ids(body) == {"desktops", "directories", "workload_lab"}


def test_a_flag_gated_group_is_guarded_by_its_links_flags():
    """A group whose links are all behind flags must be behind their OR, or switching every
    one of them off leaves a heading over nothing."""
    want = {
        "hypervisors": {"vmware_enabled", "proxmox_enabled", "vsphere_enabled",
                        "hyperv_enabled", "nutanix_enabled", "xcpng_enabled"},
        "clouds": {"cloud_pages"},
        "cloud_services": {"portainer_enabled", "cloud_database_enabled",
                           "k8s_management_enabled", "cloud_functions_enabled"},
        "preview": {"vdesktops_enabled", "directories_enabled", "workload_lab_enabled"},
    }
    for gid, flags in want.items():
        guard = _guard_before(gid)
        assert guard, f"the {gid} group has no if-test of its own"
        assert set(re.split(r"\s+or\s+", guard)) == flags, (
            f"the {gid} group is guarded by {guard!r}, expected the OR of {sorted(flags)}")


def test_a_permission_gated_group_carries_its_links_checks():
    """Admin's links are permission-gated, not flag-gated, so for a viewer holding none of
    those scopes the group itself must hide -- unless Request access, which is ungated, is
    in it. Preview is the same when admin-only Desktops is its only page."""
    admin_tag = _groups()["admin"][0]
    for check in ("$store.auth.isAdmin", "can('agents', 'read')", "can('audit', 'read')",
                  "can('notifications', 'read')"):
        assert check in admin_tag, f"the Admin group does not hide on {check}"
    assert "entitle_request_portal_url" in admin_tag
    preview_tag = _groups()["preview"][0]
    assert 'x-show="$store.auth.isAdmin"' in preview_tag


def test_a_heading_tap_does_not_close_the_drawer():
    src = _read(_BASE)
    i = src.index('x-ref="navDrawer"')
    tag = src[src.rindex("<div", 0, i):src.index(">", i) + 1]
    assert "@click=\"if ($event.target.closest('a')) mobileNav = false\"" in tag, (
        f"the drawer closes on any click inside it, a group heading included: {tag}")
    assert "data-nav-active=" in tag, "responsiveNav cannot tell which link is lit"


def test_the_lit_group_is_opened():
    src = _read(_APP_JS)
    assert re.search(r"\$nextTick\(\s*\(\)\s*=>\s*this\.restoreGroups\(\)\s*\)", src), \
        "nothing opens the group holding the current page"
    body = src.split("restoreGroups() {", 1)[1].split("\n        },", 1)[0]
    assert "dataset.navActive" in body and "closest('details.nav-group')" in body
    # Storage is a convenience: every access is guarded.
    for line in body.splitlines():
        if "localStorage" in line:
            assert "try {" in line, f"an unguarded localStorage access: {line.strip()}"


def test_pinning_hides_an_emptied_group_and_never_removes_it():
    src = _read(_APP_JS)
    body = src.split("applyPins() {", 1)[1].split("\n        },", 1)[0]
    assert "group.hidden = true" in body, "a group emptied by pinning stays as a bare heading"
    assert "remove()" not in body and "removeChild" not in body


def test_the_group_chrome_is_styled_in_the_shell():
    src = _read(_BASE)
    for rule in (r"\.nav-group > summary\s*\{[^}]*list-style:\s*none",
                 r"\.nav-group\[open\] \.nav-group-chevron\s*\{[^}]*rotate",
                 r"\.nav-group > a\s*\{[^}]*display:\s*block"):
        assert re.search(rule, src), f"base.html is missing the rule {rule}"


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
