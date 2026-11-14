"""Marker-guarded RBAC data migrations that ``init_db`` runs after the schema is ready.

Each one is idempotent through a ``schema_markers`` row, so a database that has applied it
never does the work again. The ``_BACKFILL_V1_*`` tables are frozen on purpose: a migration
has to describe what it did when it ran, not what the catalog says today.
"""
import json

from . import (  # models live in the package until they are split out
    AccessRole, OAuthGroupMapping, SchemaMarker, User,
)
from ..config import settings


# What to grant so that newly-gating a route revokes nothing.
#
# THE RULE, and it is narrower than "every level the scope offers": grant exactly the
# levels a NON-ADMIN WITH AN EXPLICIT PERMISSION MAP could already reach. Those users are
# the only ones a new gate can affect — admins pass everything, and a NULL map still means
# unrestricted.
#
# So a router that was `get_current_user`-only contributes its full level set (anyone
# logged in could do all of it), and a router that was `require_admin` contributes
# NOTHING and is absent below. Granting on an ex-admin route would not be preserving
# access, it would be handing out access nobody outside the admin flag ever had —
# the mirror image of the bug this backfill exists to prevent.
#
# The same logic excludes the routes that were gated on the phantom `require_permission
# ("admin", …)` scope: `"admin"` was never in the catalog, so for a user with an explicit
# map `has_permission` returned False every time. Unreachable then, not granted now.
#
# FROZEN IN TIME ON PURPOSE. This is a migration and must describe what v1 did forever.
# Importing the live catalog from api/auth.py would mean a scope added next year gets
# retroactively granted by a migration that claims to have run long ago. A future
# catalog addition needs its own marker and its own list.
_BACKFILL_V1_SCOPES = {
    # Wholly ungated before: every route was `get_current_user` only.
    "pov": ["read", "write", "delete", "use"],
    "proxmox": ["read", "write", "delete"],
    "vsphere": ["read", "write"],
    "hyperv": ["read", "write"],
    "nutanix": ["read", "write", "delete"],
    "xcpng": ["read", "write"],
    "inventory": ["read"],
    "epml": ["read", "write"],
    # Including delete: `DELETE /api/ot/tunnels/{slug}` was `get_current_user` only, so
    # tearing a protocol tunnel down was already reachable. (The cell-BUILD routes are
    # unaffected either way -- they keep their per-cloud write dependency.)
    "ot": ["read", "write", "delete"],
    # Mixed, so only the previously-reachable half. Storage's data plane (/backends,
    # /list, /list-all, /fetch, /upload*) was ungated; its configuration and its deletes
    # were not, and `storage:delete` is therefore a new capability that starts off.
    "storage": ["read", "write"],
    # Only `GET /api/gateways` was reachable; create and delete were phantom-admin.
    "gateways": ["read"],
    # Read only. The template/blueprint/cloud-template LIST routes were authenticated
    # (`GET /builds`, `/verify`, `/runner-script`, `/blueprints`, `/cloud-templates`), so
    # withholding read would revoke pages people can open today. Every WRITE there was
    # require_admin, so write and delete are deliberately absent.
    "pov_templates": ["read"],
    # ── already in the catalog, newly ENFORCED ────────────────────────────────
    # These two are not new scopes, but nothing has ever checked them, so a map that
    # happens to omit them would lose access the moment the gate goes in. Same rule
    # applied: config-management was entirely ungated, and of the image routes only the
    # three reads were.
    "config_mgmt": ["read", "write"],
    "images": ["read"],
}

# Scopes added in v1 that are deliberately NOT backfilled, recorded so the next reader
# does not "fix" the omission: every route behind them required the admin flag, so no
# explicitly-permissioned user is losing anything. They are grantable from the Users page
# now, which is the point — they start empty and an administrator turns them on.
_BACKFILL_V1_DELIBERATELY_EMPTY = (
    "connections", "costs", "agents", "audit", "notifications",
    # `change_windows` is empty for a THIRD reason, distinct from the two the comment
    # above gives, and worth stating because it is the one a future scope is most
    # likely to share: every route it gates is BRAND NEW. There was no prior access
    # to preserve — no user, explicit map or not, could define a change window or
    # approve a change yesterday — so a backfill would not be preserving access, it
    # would be granting a brand-new authority to everyone who happens to have an
    # explicit permission map. Paired with `require_explicit_permission` on every one
    # of its routes, which is what test_the_form_of_every_gate_agrees_with_the_backfill
    # checks this decision against.
    "change_windows",
    # Brand-new for the same reason as change_windows: managed Active Directory is a new
    # page over new routes, every one behind require_explicit_permission. Nobody had
    # access yesterday, so a backfill would grant, not preserve.
    "directories",
    # Same reason: a brand-new authority (create, and run/destroy your own POVs), so no
    # prior access to preserve. The one place it IS added on upgrade is the built-in POV
    # Presenter role, which is the point of it -- `_grant_presenter_own_pov`.
    "pov_own",
    # Empty for a FOURTH reason: it narrows nothing, so there is nothing to preserve. Every
    # route `demo` reaches still accepts the scope it took before (api/demo_gates.py ORs
    # them), and its only route of its own -- the /demo hub -- is new. A backfill would
    # hand every explicitly-permissioned user the right to build demo cells on every cloud,
    # which no one has today without that cloud's own write. Explicit form on every site.
    "demo",
    # Brand-new authority over brand-new routes (api/custom_cell's blueprint CRUD): nobody
    # authored a cell blueprint yesterday, so a backfill would grant, not preserve. The
    # Read-Only and Cell Builder built-ins carry it from their literals.
    "demo_blueprints",
)

# The third category, and the only one that needs both halves spelled out: routes that
# were gated on the PHANTOM ``require_permission("admin", ...)`` scope.
#
# ``"admin"`` was never in the catalog, so that decorator answered two opposite things:
# False for every user with an explicit map (the scope is absent, so it is a deny) and
# True for every legacy NULL-map user (an empty map reads as unrestricted). Repointing
# them therefore needs BOTH:
#
#   * the PERMISSIVE form, so the legacy user keeps what they had, and
#   * NO backfill, because the explicitly-permissioned user never had it.
#
# Which is why these pairs look like a backfill omission and are not one.
_BACKFILL_V1_PHANTOM_ADMIN = (
    ("gateways", "write"), ("gateways", "delete"),
    ("storage", "delete"),
    # `images` predates this change, so its pairs are exempt for a second reason too.
    ("images", "write"), ("images", "delete"),
)

# A NOTE FOR WHOEVER WRITES THE NEXT ONE OF THESE. There are now THREE tables holding
# permission maps, not two, and a scope added later has to widen all of them or the users
# who lose it are the ones who look best configured:
#
#   users.permissions                       an admin's explicit per-user map
#   oauth_group_mappings.default_permissions  what a matched group confers at login
#   access_roles.permissions                  the BUILT-IN rows -- custom ones are the
#                                             operator's own and must not be touched
#
# The third is the one that will be missed, and it is the widest blast radius of the three:
# a built-in role is held by many people at once, and `services/role_service._BUILTIN_ROLES`
# is a frozen literal that does NOT track the catalog (see the comment there for why
# deriving it live is worse, not better). After widening those rows, call
# `role_service.reconcile()` so every assignee's materialised copy picks the change up --
# widening the role alone changes nothing for anyone already holding it.
def _commit_marked(db, key: str) -> bool:
    """Commit a marker-guarded data migration. False when another process got there first.

    These run outside init_db's advisory lock, so two processes booting together (the
    image's two gunicorn workers, or app and worker) can both find the marker absent and
    both do the work. The marker's primary key lets one commit; the other's whole
    transaction rolls back, which is the right outcome -- but it used to surface as a
    logged failure, and for the empty-permission migration as an ERROR saying users
    were denied, when the work was done.
    """
    import logging as _logging
    from sqlalchemy.exc import IntegrityError
    try:
        db.commit()
        return True
    except IntegrityError:
        db.rollback()
        if db.query(SchemaMarker).filter(SchemaMarker.key == key).first() is None:
            raise
        _logging.getLogger(__name__).info("%s: another process applied it first", key)
        return False


_BACKFILL_V1_MARKER = "rbac_scope_backfill_v1"


def _backfill_new_permission_scopes(db) -> int:
    """Grant the v1 new scopes to everyone who already has an explicit permission map.

    Why this is mandatory rather than tidy: ``api/auth.has_permission`` treated an empty map
    as UNRESTRICTED (until ``_materialise_empty_permission_maps``) but a non-empty map as a strict per-scope allowlist. So the instant a
    route is gated on a new scope, every user with an explicit map loses that route —
    silently, with no error and no log line. These features were previously ungated, which
    means all four levels were reachable; granting each new scope its full level set
    reproduces exactly what those users could already do. Admins tighten afterwards, on
    purpose, seeing the rows.

    Users with a NULL map are left alone: NULL still means unrestricted, so writing a map
    for them would NARROW them to whatever we wrote.

    ``oauth_group_mappings.default_permissions`` gets the same treatment, because
    ``_complete_oauth_login`` overwrites ``session_permissions`` wholesale from those
    mappings on every login — widening only the user rows would be undone the next time an
    OIDC user signed in.

    ``jit_permissions`` is never touched: Entitle owns that column, and a grant it did not
    make is a grant it cannot revoke.

    Returns the number of rows changed. Idempotent via ``schema_markers``.
    """
    if db.query(SchemaMarker).filter(SchemaMarker.key == _BACKFILL_V1_MARKER).first():
        return 0

    changed = 0

    def _widen(current: dict) -> dict:
        out = dict(current)
        for scope, levels in _BACKFILL_V1_SCOPES.items():
            # Union per LEVEL, not "skip if the scope is present". `config_mgmt` and
            # `images` are already in the catalog, so a map can hold a subset of them
            # already — {"images": ["write"]} still needs `read` added, because the three
            # image reads were ungated and are about to stop being.
            have = out.get(scope)
            if not isinstance(have, list):
                # Absent, or a malformed non-list value that `has_permission` would turn
                # into a substring test. Replace it with the honest list either way.
                out[scope] = list(levels)
                continue
            merged = list(have) + [lv for lv in levels if lv not in have]
            if merged != have:
                out[scope] = merged
        return out

    for user in db.query(User).filter(User.permissions.isnot(None)).all():
        # An accessor is confined by a path allowlist, not by permissions. Widening its
        # map cannot reach anything, but it makes a confined login look permissive to the
        # next person reading the row, so skip it.
        if user.accessor_env_id:
            continue
        existing = user.permissions_dict
        if not existing:
            continue  # "{}" at rest is unrestricted; do not turn it into a restriction
        widened = _widen(existing)
        if widened != existing:
            user.permissions_dict = widened
            changed += 1

    for mapping in db.query(OAuthGroupMapping).filter(
            OAuthGroupMapping.default_permissions.isnot(None)).all():
        try:
            existing = json.loads(mapping.default_permissions) or {}
        except Exception:
            continue  # malformed JSON is skipped at login too, not repaired here
        if not isinstance(existing, dict) or not existing:
            continue
        widened = _widen(existing)
        if widened != existing:
            mapping.default_permissions = json.dumps(widened)
            changed += 1

    db.add(SchemaMarker(
        key=_BACKFILL_V1_MARKER,
        detail=f"widened {changed} row(s) with {len(_BACKFILL_V1_SCOPES)} new scopes",
    ))
    return changed if _commit_marked(db, _BACKFILL_V1_MARKER) else 0


_EMPTY_MEANS_NOTHING_MARKER = "rbac_empty_means_nothing_v1"


def _materialise_empty_permission_maps(db) -> int:
    """Write an explicit full-access map wherever an empty one was granting it.

    An empty permission map used to mean UNRESTRICTED in ``api/auth.has_permission``; it now
    grants nothing. This runs once, before anyone is asked the new question, so nobody's
    access changes on upgrade -- an administrator tightens afterwards, seeing the rows.

    Users: a person with no baseline, no group-derived and no role map, who is not an
    administrator, a POV accessor or a service account (the last two never read the empty
    map as unrestricted: see ``effective_permissions_dict``). An SSO user is left alone when
    group mappings decide their access, because the next login rebuilds
    ``session_permissions`` from the mappings below -- a baseline written here would pin
    their access past the day they leave the group. With no mappings at all (DB or
    ``.env``) nothing rebuilds it, so they are treated like a local user.

    Group mappings with neither ``default_permissions`` nor a role get the full map, tagged
    ``LEGACY_FULL_ACCESS_KEY`` so the login merge still applies it only when no other
    matched mapping grants anything -- the old rule, exactly. Malformed JSON is left as it
    is: login skips it, as it always has.

    ``jit_permissions`` is not consulted: Entitle owns it, and when a grant ends the user
    falls back to what this writes. Returns the number of rows changed. Idempotent via
    ``schema_markers``.
    """
    if db.query(SchemaMarker).filter(SchemaMarker.key == _EMPTY_MEANS_NOTHING_MARKER).first():
        return 0
    from ..api.auth import LEGACY_FULL_ACCESS_KEY, full_access_map

    mappings = db.query(OAuthGroupMapping).all()
    groups_decide = bool(mappings) or bool(getattr(settings, "azure_oauth_group_map", None))
    users = mappings_changed = 0

    for user in db.query(User).all():
        if user.is_admin or user.accessor_env_id or user.is_service_account or user.role_id:
            continue
        if user.permissions_dict or user.session_permissions_dict or user.role_permissions_dict:
            continue
        if groups_decide and (user.auth_provider or "local") != "local":
            continue
        user.permissions_dict = full_access_map()
        users += 1

    for mapping in mappings:
        if mapping.role_id:
            continue
        raw = mapping.default_permissions
        if raw:
            try:
                parsed = json.loads(raw)
            except Exception:
                continue
            if not isinstance(parsed, dict) or parsed:
                continue
        mapping.default_permissions = json.dumps({LEGACY_FULL_ACCESS_KEY: True,
                                                  **full_access_map()})
        mappings_changed += 1

    db.add(SchemaMarker(
        key=_EMPTY_MEANS_NOTHING_MARKER,
        detail=(f"wrote the full-access map onto {users} user(s) and "
                f"{mappings_changed} group mapping(s) that held an empty one"),
    ))
    return users + mappings_changed if _commit_marked(db, _EMPTY_MEANS_NOTHING_MARKER) else 0


_RETIRE_LEVELS_MARKER = "rbac_retire_unenforced_levels_v1"


def _strip_retired(perms: dict, retired: dict) -> dict:
    """``perms`` without any retired (scope, level) pair. Never removes a KEY.

    That is the invariant the whole migration rests on. An empty map means UNRESTRICTED
    (``api/auth.has_permission``), so `{"secrets": ["read"]}` must become
    `{"secrets": []}` -- a strict allowlist that grants nothing, exactly what it granted
    before -- and never `{}`, which would grant everything. ``is_admin`` and any non-list
    value pass through untouched.
    """
    out = dict(perms)
    for scope, levels in perms.items():
        gone = retired.get(scope)
        if gone and isinstance(levels, list):
            kept = [lv for lv in levels if lv not in gone]
            if kept != levels:
                out[scope] = kept
    return out


def _retire_unenforced_levels(db) -> int:
    """Strip the levels in ``api/auth.RETIRED_LEVELS`` from every stored permission map.

    Those levels were offered by the original fourteen scopes and enforced by nothing, so
    removing them changes no one's access. That is the point, and the reason this is safe
    to run unattended. What it fixes is the stored copies, because a map holding a pair the
    catalog no longer offers:
      * shows as a grant the grid cannot display;
      * would 422 on its next save, were the validator not tolerant of exactly these
        pairs.

    Five places hold maps, and all five are cleaned:
      * users.permissions, session_permissions and jit_permissions;
      * access_roles.permissions, built-in and custom alike;
      * oauth_group_mappings.default_permissions.

    ``users.role_permissions`` is left to ``role_service.reconcile``, which rewrites it
    from the cleaned roles, which is why this runs BEFORE that call in ``init_db``.
    ``jit_permissions`` is the one column this file otherwise refuses to touch, because
    Entitle owns it. Stripping is different from granting: it removes only grants that did
    nothing, and ``api/entitle_rest`` answers Entitle's later revoke of one with success.

    Marker-guarded, so it runs once. A re-run would be harmless anyway, because it only
    ever removes pairs nothing can grant any more.
    """
    if db.query(SchemaMarker).filter(SchemaMarker.key == _RETIRE_LEVELS_MARKER).first():
        return 0
    from ..api.auth import RETIRED_LEVELS

    changed = 0

    def _clean_json(raw):
        """(new_raw, changed?) for one JSON-in-Text column value."""
        if not raw:
            return raw, False
        try:
            perms = json.loads(raw)
        except Exception:
            return raw, False  # malformed is skipped wherever it is read, not repaired here
        if not isinstance(perms, dict) or not perms:
            return raw, False  # {} at rest is unrestricted; nothing to narrow
        cleaned = _strip_retired(perms, RETIRED_LEVELS)
        if cleaned == perms:
            return raw, False
        return json.dumps(cleaned), True

    for user in db.query(User).all():
        for col in ("permissions", "session_permissions", "jit_permissions"):
            new, did = _clean_json(getattr(user, col))
            if did:
                setattr(user, col, new)
                changed += 1
    for role in db.query(AccessRole).all():
        new, did = _clean_json(role.permissions)
        if did:
            role.permissions = new
            changed += 1
    for mapping in db.query(OAuthGroupMapping).all():
        new, did = _clean_json(mapping.default_permissions)
        if did:
            mapping.default_permissions = new
            changed += 1

    db.add(SchemaMarker(
        key=_RETIRE_LEVELS_MARKER,
        detail=f"stripped retired levels from {changed} stored map(s)",
    ))
    return changed if _commit_marked(db, _RETIRE_LEVELS_MARKER) else 0


_PRESENTER_OWN_POV_MARKER = "rbac_presenter_own_pov_v1"


def _grant_presenter_own_pov(db) -> int:
    """Give the built-in POV Presenter role ``pov_own`` write and delete on an existing
    install.

    A presenter may create POVs, and set up, run and destroy their own -- created by them
    or assigned to them -- and no others (api/pov_gates.py). New installs get it from the
    role literal in
    ``services/role_service``, but ``seed_builtins`` never updates an existing row's
    permissions, so without this the grant would reach new installs only.

    Only the BUILT-IN row, found by slug and is_builtin. A custom role an operator
    cloned from it is theirs to decide. Runs before ``role_service.reconcile`` in
    init_db, which copies the change to every user holding the role; group members get it
    at their next sign-in, when their permissions are rebuilt from the role. Marker-guarded
    so it runs once; built-ins cannot be edited, so there is no later removal for a re-run
    to undo.
    """
    if db.query(SchemaMarker).filter(SchemaMarker.key == _PRESENTER_OWN_POV_MARKER).first():
        return 0
    changed = 0
    role = db.query(AccessRole).filter(AccessRole.slug == "pov-presenter",
                                       AccessRole.is_builtin.is_(True)).first()
    if role is not None:
        perms = role.permissions_dict
        levels = perms.get("pov_own")
        want = {"write", "delete"}
        if not isinstance(levels, list) or not want <= set(levels):
            perms["pov_own"] = sorted(set(levels if isinstance(levels, list) else []) | want)
            role.permissions_dict = perms
            changed = 1
    db.add(SchemaMarker(key=_PRESENTER_OWN_POV_MARKER,
                        detail=f"granted pov_own write+delete to {changed} built-in role(s)"))
    return changed if _commit_marked(db, _PRESENTER_OWN_POV_MARKER) else 0


_READ_ONLY_DEMO_MARKER = "rbac_read_only_demo_v1"

# What the built-in Read-Only role gained after it was first seeded: the Demo hub (#1325)
# and custom-cell blueprints (#1332). FROZEN, like every list in this module: a scope added
# later needs its own marker and its own list, never a widened copy of this one.
_READ_ONLY_DEMO_GRANTS = {"demo": ["read"], "demo_blueprints": ["read"]}


def _grant_read_only_demo_read(db) -> int:
    """Give the built-in Read-Only role read on the demo scopes on an existing install.

    "See everything, change nothing" must cover the Demo hub and blueprints too
    (tests/test_rbac_roles.test_the_read_only_role_covers_the_whole_catalog). New installs
    get both from the role literal in ``services/role_service``, but ``seed_builtins`` never
    updates an existing row, so without this an upgraded install's Read-Only role sees
    neither -- a silent gap held by everyone assigned it.

    Same shape as :func:`_grant_presenter_own_pov`: the BUILT-IN row only, by slug and
    is_builtin (a clone is its owner's to decide); run before ``role_service.reconcile``,
    which copies the change to every holder; marker-guarded so it runs once. Read only --
    no level that changes anything, so nobody gains an action.
    """
    if db.query(SchemaMarker).filter(SchemaMarker.key == _READ_ONLY_DEMO_MARKER).first():
        return 0
    changed = 0
    role = db.query(AccessRole).filter(AccessRole.slug == "read-only",
                                       AccessRole.is_builtin.is_(True)).first()
    if role is not None:
        perms = role.permissions_dict
        for scope, levels in _READ_ONLY_DEMO_GRANTS.items():
            have = perms.get(scope)
            have = have if isinstance(have, list) else []
            if not set(levels) <= set(have):
                perms[scope] = sorted(set(have) | set(levels))
                changed = 1
        if changed:
            role.permissions_dict = perms
    db.add(SchemaMarker(key=_READ_ONLY_DEMO_MARKER,
                        detail=f"granted demo read levels to {changed} built-in role(s)"))
    return changed if _commit_marked(db, _READ_ONLY_DEMO_MARKER) else 0


_PRESENTER_OWN_ONLY_MARKER = "rbac_presenter_own_only_v1"


def _narrow_presenter_to_own_povs(db) -> int:
    """Make the built-in POV Presenter see only its own POVs on an existing install.

    Swaps ``pov:read`` (every POV) for ``pov_own:read`` (your own). ``pov:use`` stays. The
    new levels arrive with the literal in ``services/role_service`` on a fresh install, but
    ``seed_builtins`` never updates an existing row, so without this an upgraded install
    would keep showing presenters every POV.

    Same shape as :func:`_grant_presenter_own_pov`: the BUILT-IN row only, by slug and
    is_builtin -- a role an operator cloned keeps whatever they decided -- before
    ``role_service.reconcile`` copies it to the role's holders, and marker-guarded so it
    runs once. Runs after that function, so ``pov_own`` already holds write and delete.
    """
    if db.query(SchemaMarker).filter(SchemaMarker.key == _PRESENTER_OWN_ONLY_MARKER).first():
        return 0
    changed = 0
    role = db.query(AccessRole).filter(AccessRole.slug == "pov-presenter",
                                       AccessRole.is_builtin.is_(True)).first()
    if role is not None:
        perms = role.permissions_dict
        general = perms.get("pov") if isinstance(perms.get("pov"), list) else []
        own = perms.get("pov_own") if isinstance(perms.get("pov_own"), list) else []
        if "read" in general or "read" not in own:
            perms["pov"] = sorted(set(general) - {"read"})
            perms["pov_own"] = sorted(set(own) | {"read"})
            role.permissions_dict = perms
            changed = 1
    db.add(SchemaMarker(key=_PRESENTER_OWN_ONLY_MARKER,
                        detail=f"narrowed {changed} built-in presenter role(s) to own POVs"))
    return changed if _commit_marked(db, _PRESENTER_OWN_ONLY_MARKER) else 0
