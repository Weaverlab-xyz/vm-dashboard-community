"""The permission catalog has to agree with what the routes actually enforce.

For the whole life of this feature it did not, in both directions at once, and neither
disagreement produced an error:

  * ``require_permission("admin", …)`` had 15 call sites across ``api/gateways.py``,
    ``api/images.py`` and ``api/storage.py``, and ``"admin"`` was never in
    ``PERMISSION_SCOPES``. No grid row, Entitle asset or bootstrap group could grant it, so
    a user with an explicit permission map always got 403 while a legacy NULL-map user was
    unrestricted — two opposite answers from one decorator.
  * ``images``, ``config_mgmt`` and ``jobs`` sat *in* the catalog with no
    ``require_permission`` site at all. The Images and Configuration rows in the grid were
    decorative: ticking them changed nothing, and all 12 config-management routes —
    ``POST /run`` included — were reachable by anyone logged in.

Nothing detected either, because both are spelled correctly and import cleanly. Hence a
source sweep: ``test_every_enforced_scope_is_in_the_catalog`` is the test that would have
caught the first, and ``test_every_shipped_nav_section_has_a_scope`` the second.

The nav test reads ``api/setup._PREVIEW_FLAGS`` rather than a hard-coded exclusion list, so
graduating a preview feature fails here instead of shipping it ungated.

Run: python tests/test_permission_catalog.py   (or under pytest)
"""
import ast
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from web_dashboard.api.auth import (  # noqa: E402
    PERMISSION_LEVELS, PERMISSION_SCOPE_LEVELS, PERMISSION_SCOPES, levels_for_scope)


def _read(*parts):
    with open(os.path.join(_ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


def _py_files():
    for dirpath, dirnames, filenames in os.walk(os.path.join(_ROOT, "web_dashboard")):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for name in filenames:
            if name.endswith(".py"):
                yield os.path.join(dirpath, name)


def _enforced_pairs():
    """Every (scope, level) literal passed to require_permission / the explicit variant.

    Walks the AST rather than grepping: a decorator argument can be split across lines,
    and ``require_permission(\n  "storage",\n  "read")`` is exactly the shape a formatter
    produces.
    """
    wanted = {"require_permission", "require_explicit_permission",
              "has_permission", "has_explicit_permission"}
    out = []
    for path in _py_files():
        try:
            tree = ast.parse(_read(path))
        except SyntaxError as exc:  # pragma: no cover - the syntax suite covers this
            raise AssertionError(f"{path} does not parse: {exc}") from exc
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
            if name not in wanted:
                continue
            args = [a for a in node.args
                    if isinstance(a, ast.Constant) and isinstance(a.value, str)]
            # has_permission(user, scope, level) has a leading non-constant arg; the
            # dependency factories take (scope, level). Either way the trailing two string
            # constants are the pair.
            if len(args) < 2:
                continue
            rel = os.path.relpath(path, _ROOT).replace("\\", "/")
            out.append((args[-2].value, args[-1].value, rel, node.lineno))
    return out


def test_the_sweep_finds_something():
    """A guard on the guard: if the AST walk stops matching, every assertion below
    passes vacuously and the drift it exists to catch comes straight back."""
    pairs = _enforced_pairs()
    assert len(pairs) > 100, f"only found {len(pairs)} enforcement sites — sweep is broken"


def test_every_enforced_scope_is_in_the_catalog():
    """The phantom-``admin`` test. A scope nothing can grant is not a permission."""
    unknown = sorted({
        f"{scope}:{level} at {rel}:{line}"
        for scope, level, rel, line in _enforced_pairs()
        if scope not in PERMISSION_SCOPE_LEVELS
    })
    assert not unknown, (
        "these routes enforce a scope that is not in PERMISSION_SCOPES, so no grid row, "
        "Entitle asset or bootstrap group can ever grant them:\n  " + "\n  ".join(unknown))


def test_every_enforced_level_is_offered_by_its_scope():
    """``inventory:delete`` would be unreachable forever: the grid renders no checkbox for
    it, and validate_permissions_payload 422s anyone who sends it by hand."""
    bad = sorted({
        f"{scope}:{level} at {rel}:{line} (scope offers {', '.join(levels_for_scope(scope))})"
        for scope, level, rel, line in _enforced_pairs()
        if scope in PERMISSION_SCOPE_LEVELS and level not in PERMISSION_SCOPE_LEVELS[scope]
    })
    assert not bad, "enforced level that its scope does not offer:\n  " + "\n  ".join(bad)


def test_the_catalog_and_its_list_form_agree():
    """``PERMISSION_SCOPES`` is the list every importer iterates; it must stay derived."""
    assert PERMISSION_SCOPES == list(PERMISSION_SCOPE_LEVELS), (
        "PERMISSION_SCOPES drifted from PERMISSION_SCOPE_LEVELS — derive it, do not "
        "maintain two copies")
    for scope, levels in PERMISSION_SCOPE_LEVELS.items():
        assert levels, f"{scope} offers no levels, so it can never be granted"
        assert list(levels) == sorted(set(levels), key=PERMISSION_LEVELS.index), (
            f"{scope}: levels must be unique and in PERMISSION_LEVELS order, got {levels}")
        for level in levels:
            assert level in PERMISSION_LEVELS, f"{scope}: unknown level {level!r}"


# ── nav coverage ─────────────────────────────────────────────────────────────

# Nav sections that deliberately have no scope, with the reason. Anything NOT here and not
# preview must map to a scope, so adding a page forces a decision rather than defaulting
# to ungated.
_NAV_EXEMPT = {
    # Gating the home page locks every user out of the dashboard.
    "dashboard": "aggregate landing page",
    # A grantable scope on these IS privilege escalation: they administer identity.
    "users": "identity administration (require_admin)",
    "groups": "identity administration (require_admin)",
    "workgroups": "has the `workgroups` scope already",
    # Reads the POV API, so `pov:read` already governs what it can show.
    "use_cases": "renders /api/pov/managed, governed by pov:read",
    # Vault administration stays on the admin flag; `secrets` is the `use` level only.
    "secrets": "vault administration (require_admin)",
    # api/jobs.py filters rows by can_audit_jobs (jobs:read) rather than gating the route.
    "jobs": "row-filtered via can_audit_jobs",
    # Its own nav entry, but it is the POV detail page's sibling under the `pov` scope.
    "pov_templates": "has the `pov_templates` scope",
    # An outbound link to the operator's Entitle portal, not a page on this app. Gating it
    # would hide the one affordance a user without a permission has for asking for it.
    "request_access": "external link to the Entitle request portal",
}

# data-nav value -> the scope that governs it.
_NAV_SCOPE = {
    "pov": "pov", "vms": "vms", "proxmox": "proxmox", "vsphere": "vsphere",
    "hyperv": "hyperv", "nutanix": "nutanix", "xcpng": "xcpng",
    "connections": "connections", "aws": "aws", "azure": "azure", "gcp": "gcp",
    "oci": "oci", "containers": "containers", "images": "images", "storage": "storage",
    "databases": "cloud_database", "functions": "cloud_function", "k8s": "k8s",
    "costs": "costs", "config_mgmt": "config_mgmt", "inventory": "inventory",
    "agents": "agents", "audit": "audit",
}


def _preview_nav_sections():
    """Nav sections whose feature flag is a preview toggle, read from the registry.

    api/setup._PREVIEW_FLAGS is the authoritative marker — the "Preview feature" comment
    in main.py near the cloud-functions router is stale (that flag graduated) and must not
    be trusted.
    """
    from web_dashboard.api.setup import _PREVIEW_FLAGS
    from web_dashboard.services.feature_flags import _DERIVED
    nav = _read("web_dashboard", "templates", "_nav_links.html")
    out = set()
    # Each nav link sits inside `{% if <flag> %}` … `{% endif %}`; pair every data-nav with
    # the nearest preceding flag test.
    flag = None
    for line in nav.split("\n"):
        m = re.search(r"{%\s*if\s+([a-z0-9_]+)\s*%}", line)
        if m:
            flag = m.group(1)
        for nav_m in re.finditer(r'data-nav="([a-z0-9_]+)"', line):
            # A DERIVED flag (feature_flags._DERIVED) has no toggle of its own -- it is the
            # OR of the flags that can reveal the section, which is how one nav link covers
            # the Workload Lab's two labs without adding a third row to Settings. Such a
            # section counts as preview only while EVERY constituent does, so the moment one
            # graduates it drops out of this set and the scope assertions below start
            # demanding a real scope for it. That is the same forcing function a plain
            # single-flag section gets.
            parts = _DERIVED.get(flag, (flag,))
            if all(p in _PREVIEW_FLAGS for p in parts):
                out.add(nav_m.group(1))
    assert out, "no preview nav sections found — the flag/nav pairing broke"
    return out


def test_every_shipped_nav_section_has_a_scope():
    nav = _read("web_dashboard", "templates", "_nav_links.html")
    sections = set(re.findall(r'data-nav="([a-z0-9_]+)"', nav))
    assert len(sections) > 20, f"only found {len(sections)} nav sections — regex broke"

    preview = _preview_nav_sections()
    unaccounted = sorted(
        s for s in sections
        if s not in preview and s not in _NAV_EXEMPT and s not in _NAV_SCOPE)
    assert not unaccounted, (
        "nav sections with no permission scope. Add one to PERMISSION_SCOPE_LEVELS and to "
        "_NAV_SCOPE here, or record why it is exempt in _NAV_EXEMPT:\n  "
        + "\n  ".join(unaccounted))

    for section, scope in sorted(_NAV_SCOPE.items()):
        assert scope in PERMISSION_SCOPE_LEVELS, (
            f"nav section {section!r} maps to {scope!r}, which is not a scope")


def test_preview_sections_are_excluded_rather_than_listed():
    """Graduating a preview feature must FAIL here, not ship ungated.

    The Workload Lab's two tabs -- the Certificate Lab and the SPIRE Lab -- both borrow
    ``cloud_function`` read/write today, so `cloud_function:read` silently grants both. That
    is acceptable only while they are preview; the moment either flag leaves _PREVIEW_FLAGS
    the section stops resolving as preview (see :func:`_preview_nav_sections`) and this test
    demands a real scope.

    ``workload_lab`` is one nav section over both labs on purpose: consolidating them was a
    page-layer change, and Settings still owns exactly one toggle per lab.
    """
    preview = _preview_nav_sections()
    assert {"desktops", "workload_lab"} <= preview, (
        f"expected the preview nav sections, found {sorted(preview)} — if one graduated, "
        "give it its own scope and update _NAV_SCOPE")
    for section in preview:
        assert section not in _NAV_SCOPE, (
            f"{section} is still marked preview but has a scope — remove it from "
            "_PREVIEW_FLAGS or from _NAV_SCOPE, whichever is now wrong")


# ── the backfill ─────────────────────────────────────────────────────────────

def test_the_backfill_covers_every_newly_gated_scope():
    """Every scope this change started enforcing must either be backfilled or be recorded
    as deliberately empty. An omission is a silent revocation for existing users."""
    from web_dashboard.database import (_BACKFILL_V1_DELIBERATELY_EMPTY,
                                        _BACKFILL_V1_SCOPES)
    accounted = set(_BACKFILL_V1_SCOPES) | set(_BACKFILL_V1_DELIBERATELY_EMPTY)
    # The 14 original scopes need no entry except where enforcement is NEW.
    original = {"vms", "aws", "azure", "gcp", "oci", "containers", "jobs", "workgroups",
                "secrets", "cloud_database", "k8s", "cloud_function"}
    missing = sorted(set(PERMISSION_SCOPES) - accounted - original)
    assert not missing, (
        "scopes with no backfill decision — each one silently revokes access from every "
        "user with an explicit permission map:\n  " + "\n  ".join(missing))
    overlap = sorted(set(_BACKFILL_V1_SCOPES) & set(_BACKFILL_V1_DELIBERATELY_EMPTY))
    assert not overlap, f"listed as both backfilled and empty: {overlap}"


def test_the_backfill_grants_only_levels_its_scope_offers():
    from web_dashboard.database import _BACKFILL_V1_SCOPES
    for scope, levels in _BACKFILL_V1_SCOPES.items():
        assert scope in PERMISSION_SCOPE_LEVELS, f"backfill names unknown scope {scope!r}"
        for level in levels:
            assert level in PERMISSION_SCOPE_LEVELS[scope], (
                f"backfill grants {scope}:{level}, which the scope does not offer")


def test_the_backfill_does_not_import_the_live_catalog():
    """It is a migration: it must describe what v1 did forever. Deriving it from
    PERMISSION_SCOPE_LEVELS would retroactively grant a scope added next year."""
    src = _read("web_dashboard", "database.py")
    body = src.split("_BACKFILL_V1_SCOPES")[1].split("def init_db")[0]
    assert "PERMISSION_SCOPE_LEVELS" not in body, (
        "the v1 backfill reads the live catalog — freeze the list instead")


def _pairs_by_form():
    """(scope, level, permissive?) for every enforcement site, by which factory is used."""
    permissive, explicit = set(), set()
    for path in _py_files():
        tree = ast.parse(_read(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
            if name not in {"require_permission", "require_explicit_permission",
                            "has_permission", "has_explicit_permission"}:
                continue
            args = [a for a in node.args
                    if isinstance(a, ast.Constant) and isinstance(a.value, str)]
            if len(args) < 2:
                continue
            pair = (args[-2].value, args[-1].value)
            (explicit if "explicit" in name else permissive).add(pair)
    return permissive, explicit


def test_the_form_of_every_gate_agrees_with_the_backfill():
    """The invariant that ties the two halves of this change together.

    The FORM of a gate encodes what the route used to be:

      * ``require_permission`` (permissive — an empty map passes) is correct only where
        the route was previously UNGATED. Which means the level must also be in the
        backfill, or the change revokes it from every explicitly-permissioned user.
      * ``require_explicit_permission`` is correct where the route required the admin
        flag. Which means the level must NOT be in the backfill, or the change grants
        something no non-admin ever had.

    So the two lists are not independent bookkeeping — each one implies the other, and
    this test is what makes a mismatch fail instead of shipping. It caught two real
    mistakes when it was written: ``ot`` was missing ``delete`` (a previously-ungated
    tunnel teardown) and ``pov_templates`` was missing ``read`` (previously-authenticated
    list routes).

    ``_BACKFILL_V1_PHANTOM_ADMIN`` is the third case, and the reason it needs naming: a
    route on the phantom ``require_permission("admin", ...)`` scope was UNREACHABLE for an
    explicit map and reachable for a legacy one, so it wants the permissive form AND no
    backfill. That combination looks exactly like the omission above and is the opposite
    of one.
    """
    from web_dashboard.database import (_BACKFILL_V1_DELIBERATELY_EMPTY,
                                        _BACKFILL_V1_PHANTOM_ADMIN,
                                        _BACKFILL_V1_SCOPES)
    permissive, explicit = _pairs_by_form()
    # The 14 original scopes predate this and are exempt: their gates were already in
    # place, so the form carries no information about a before-state.
    original = {"vms", "aws", "azure", "gcp", "oci", "containers", "jobs", "workgroups",
                "secrets", "cloud_database", "k8s", "cloud_function", "config_mgmt",
                "images"}

    missing = sorted(
        f"{scope}:{level}"
        for scope, level in permissive
        if scope not in original
        and (scope, level) not in _BACKFILL_V1_PHANTOM_ADMIN
        and level not in _BACKFILL_V1_SCOPES.get(scope, ()))
    assert not missing, (
        "gated with the permissive form (i.e. the route was previously ungated) but not "
        "backfilled — every user with an explicit permission map loses these, silently:\n"
        "  " + "\n  ".join(missing))

    granted = sorted(
        f"{scope}:{level}"
        for scope, level in explicit
        if level in _BACKFILL_V1_SCOPES.get(scope, ()))
    assert not granted, (
        "gated with the explicit form (i.e. the route required the admin flag) but ALSO "
        "backfilled — this hands out access no non-admin ever had:\n  "
        + "\n  ".join(granted))

    # A phantom-admin pair must NOT be backfilled -- that is the whole point of the
    # category. If one appears in both, the exemption is hiding a real grant.
    both = sorted(f"{s}:{l}" for s, l in _BACKFILL_V1_PHANTOM_ADMIN
                  if l in _BACKFILL_V1_SCOPES.get(s, ()))
    assert not both, f"listed as phantom-admin AND backfilled: {both}"

    for scope in _BACKFILL_V1_DELIBERATELY_EMPTY:
        assert scope not in _BACKFILL_V1_SCOPES, (
            f"{scope} is listed as deliberately empty but appears in the backfill")
        assert not any(s == scope for s, _ in permissive), (
            f"{scope} is treated as ex-admin but has a permissive gate somewhere — one of "
            "the two is wrong")


def test_ex_admin_scopes_use_the_explicit_form():
    """A route that required the admin flag must not become reachable by every legacy
    NULL-permission user, for whom {} reads as unrestricted in has_permission."""
    from web_dashboard.database import _BACKFILL_V1_DELIBERATELY_EMPTY
    pairs = _enforced_pairs()
    plain = {
        f"{scope}:{level} at {rel}:{line}"
        for scope, level, rel, line in pairs
        if scope in _BACKFILL_V1_DELIBERATELY_EMPTY
    }
    # Every site for an ex-admin scope must come from the explicit variant. Re-walk to
    # find which function each pair came from.
    offenders = []
    for path in _py_files():
        tree = ast.parse(_read(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
            if name not in {"require_permission", "has_permission"}:
                continue
            args = [a for a in node.args
                    if isinstance(a, ast.Constant) and isinstance(a.value, str)]
            if len(args) < 2:
                continue
            scope, level = args[-2].value, args[-1].value
            if scope in _BACKFILL_V1_DELIBERATELY_EMPTY and level != "read":
                rel = os.path.relpath(path, _ROOT).replace("\\", "/")
                offenders.append(f"{scope}:{level} at {rel}:{node.lineno}")
    assert not offenders, (
        "an ex-admin route is gated with the permissive form, which passes for every "
        "legacy NULL-permission user — use require_explicit_permission:\n  "
        + "\n  ".join(offenders)) or plain is not None


# ── the surfaces that a scope reaches ────────────────────────────────────────

def test_every_scope_has_a_display_label_or_reads_correctly_unmapped():
    """The label map is how a display name changes without renaming a KEY — keys live in
    every user's permissions JSON and in Entra group names, so renaming one un-grants
    everyone who had it and the symptom is a locked-out user, not an error.

    The fallback (underscores to spaces + CSS capitalize) is fine for `storage` and wrong
    for `hyperv`, `xcpng`, `vsphere` and `pov`, which are product names.
    """
    app_js = _read("web_dashboard", "static", "js", "app.js")
    body = app_js.split("function permissionScopeLabel(")[1].split("\n}")[0]
    needs_a_label = []
    for scope in PERMISSION_SCOPES:
        if f"{scope}:" in body:
            continue
        # Unmapped is acceptable only when the fallback already reads as a word.
        if scope.replace("_", " ").istitle() or scope.isalpha() and scope.islower():
            if scope in {"hyperv", "xcpng", "vsphere", "pov", "oci", "epml", "ot", "k8s"}:
                needs_a_label.append(scope)
            continue
        needs_a_label.append(scope)
    assert not needs_a_label, (
        "these scopes render from the raw key and would read wrongly — add an entry to "
        f"permissionScopeLabel: {needs_a_label}")


def test_both_grids_render_only_the_levels_a_scope_offers():
    """A checkbox for a level the scope does not offer saves a payload the server 422s."""
    for rel in (("templates", "users", "list.html"), ("templates", "groups", "index.html")):
        src = _read("web_dashboard", *rel)
        where = "/".join(rel)
        assert "permission_scope_levels | tojson" in src, (
            f"{where} does not receive the per-scope level map from the page context")
        assert "allowsLevel(scope, level)" in src, (
            f"{where} renders a checkbox without asking whether the level is offered")
        assert "permissionScopeAllowsLevel" in src, (
            f"{where} does not delegate to the shared helper in app.js")


def test_the_page_context_ships_the_level_map_to_both_pages():
    src = _read("web_dashboard", "main.py")
    assert src.count('"permission_scope_levels": auth.PERMISSION_SCOPE_LEVELS') == 2, (
        "the /users and /groups routes must both inject the level map, or one grid drifts")


def test_the_entra_bootstrap_iterates_the_per_scope_levels():
    """Every entry becomes a REAL Entra security group via Graph, so the scopes x levels
    cross product would leave permanent groups in a customer's tenant that can never
    grant anything. Asserted by source: the script imports the Azure SDK at module scope,
    so it cannot be imported in this environment.
    """
    src = _read("web_dashboard", "scripts", "bootstrap_entitle_groups.py")
    block = src.split('if "permissions" in scopes:')[1].split('if "workgroups" in scopes:')[0]
    assert "PERMISSION_SCOPE_LEVELS.items()" in block, (
        "the permissions loop is not driven by the per-scope level map")
    assert "for level in PERMISSION_LEVELS" not in block, (
        "the loop still iterates every level for every scope")


def test_the_entitle_asset_catalog_offers_only_real_levels():
    """Entitle publishes these as requestable roles; a role _apply would reject is a
    request that gets approved and then 400s."""
    src = _read("web_dashboard", "api", "entitle_rest.py")
    assert "PERMISSION_SCOPE_LEVELS.items()" in src, (
        "get_assets no longer derives role_options from the per-scope level map")
    assert "does not offer role_code" in src, (
        "_apply does not reject a real level that the named scope does not offer")


def test_the_use_level_has_a_bootstrap_tier():
    """`-use` was missing from every suffix tuple, so `dashboard-secrets-use` fell through
    to the 'tier inference fell through' warning and took the default."""
    src = _read("web_dashboard", "scripts", "bootstrap_entitle_app.py")
    tiers = src.split("_AUTO_APPROVE_SUFFIXES")[1].split("def _tier_for_group")[0]
    assert '"-use"' in tiers, "the -use suffix has no explicit approval tier"


def test_no_scope_offers_a_level_no_route_enforces():
    """The inverse of the enforcement sweep: a level in the catalog that nothing checks is
    a checkbox that grants nothing, which is how the `images` row spent its life.

    The four original scopes keep unenforced levels on purpose (narrowing them would hide
    a checkbox for a grant already stored, and nothing prunes stored keys), so they are
    listed here rather than silently tolerated.
    """
    enforced = {(s, l) for s, l, _, _ in _enforced_pairs()}
    grandfathered = {
        # `secrets` is checked as `secrets:use` in config_mgmt._can_use_secrets, not via
        # require_permission; read/write/delete are documented as unused for it.
        "secrets",
        # `jobs` is a predicate inside auth.can_audit_jobs, not a route gate.
        "jobs",
        # Kept at four levels because grants already exist against them.
        "vms", "aws", "azure", "gcp", "oci", "containers", "workgroups",
        "cloud_database", "k8s", "cloud_function", "config_mgmt", "images",
    }
    dead = sorted(
        f"{scope}:{level}"
        for scope, levels in PERMISSION_SCOPE_LEVELS.items()
        for level in levels
        if scope not in grandfathered and (scope, level) not in enforced)
    assert not dead, (
        "these levels are offered by a scope but enforced nowhere, so ticking them grants "
        f"nothing: {dead}")


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
