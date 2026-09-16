"""
Access-role CRUD, built-in seeds, and the one writer of a principal's role columns.

An access role is a named, reusable permission map. A user or an OIDC group mapping is
*assigned* one, and the per-principal permission grid stays available on top of it as an
additive override -- so a role is the normal path and the grid is the escape hatch, rather
than a replacement for it.

Three things in here are load-bearing rather than tidy, and all three are about the same
hazard: ``api/auth.has_permission`` reads an EMPTY permission map as UNRESTRICTED.

1. **A role's own empty map means the opposite of a user's.** On ``users.permissions``, NULL
   is "unrestricted". On ``access_roles.permissions`` it is "grants nothing" -- a role is a
   positive statement about access. ``validate_role_permissions`` therefore refuses a
   missing map outright instead of defaulting one, because a defaulted role would silently
   grant everything to everyone assigned it.

2. **``apply_role_to_user`` is the ONLY writer of ``users.role_id`` /
   ``users.role_permissions``, and it always writes both.** The second column is a
   materialised copy of the role's map, which exists because ``effective_permissions_dict``
   is read on a DETACHED User by ``api/mcp_server`` -- a relationship read there raises, and
   the natural ``except: return {}`` around it is a total authorization bypass. A
   half-written pair (an id with no copy) is caught by ``database._ROLE_UNRESOLVED`` and
   denies, but it should never happen.

3. **``is_admin`` is expressible on exactly one role, the frozen ``administrator``
   built-in.** ``api/auth.validate_permissions_payload`` accepts the key -- it has to,
   because ``oauth_group_mappings`` uses it for the documented Entitle admin group -- so the
   refusal lives here instead. Without it, "can edit a role" becomes "can mint an
   administrator" one hop removed, since editing a role nobody audits is quieter than
   ticking the Admin box on a user.

Deliberately NOT built on ``services/personas``. A persona is curation and may never gate,
precisely because it can arrive from an editable cookie; a role does nothing but gate. The
two must not learn about each other -- ``tests/test_rbac_roles.py`` asserts they never
import one another.
"""
import re
import uuid
from datetime import datetime
from typing import List, Optional

from sqlalchemy.orm import Session

from ..database import AccessRole, OAuthGroupMapping, User

# Same shape as workgroup_service.NAME_RE: a slug is a URL-safe handle, and it is what the
# built-in seeds are keyed by, so it must be stable and lowercase.
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}[a-z0-9]$")

# The one role allowed to carry `is_admin`. Compared by SLUG rather than by name, so
# renaming the display label cannot move the privilege.
ADMIN_ROLE_SLUG = "administrator"


class RoleError(ValueError):
    """Service-layer validation failure (404/409/400/422 at the API edge)."""


# ── The built-ins ─────────────────────────────────────────────────────────────
#
# FROZEN LITERALS, and not derived from the live catalog. The temptation is to write
# "read-only = every scope's read level" and have it keep up on its own; that is wrong here
# for a reason specific to seeding rather than for the usual migration reason.
#
# `seed_builtins` INSERTS ONCE and never updates. So a derived definition is frozen anyway
# -- at whatever the catalog happened to hold on the day each install first booted. Two
# installs on the same version would then hold two different `read-only` roles, with nothing
# in the tree to point at and no way to tell which is which. A literal is at least
# identically wrong everywhere, and it is inspectable as data.
#
# What that costs, stated plainly: when the catalog gains a 32nd scope, every user holding a
# built-in silently loses it -- the same silent revocation `api/auth.has_permission`
# documents for explicit user maps. That scope's backfill must widen THREE tables now, not
# two (users.permissions, oauth_group_mappings.default_permissions, and the built-in rows
# here), then call `reconcile`. There is a note beside `_BACKFILL_V1_SCOPES` saying so.
#
# Every (scope, level) pair below is checked against PERMISSION_SCOPE_LEVELS by
# `tests/test_rbac_roles.py`, so a level a scope does not offer fails the suite rather than
# seeding a grant nothing can enforce.
_ALL = ["read", "write", "delete", "use"]
_RWD = ["read", "write", "delete"]
_RW = ["read", "write"]
_R = ["read"]
_RWU = ["read", "write", "use"]
_RU = ["read", "use"]

_BUILTIN_ROLES = (
    {
        "slug": ADMIN_ROLE_SLUG,
        "name": "Administrator",
        "description": "Full access, including the admin-only pages. The permission grid is "
                       "not consulted for an administrator.",
        # The ONLY built-in whose map is not a scope list, and the only one immune to the
        # silent-revocation note above: a 32nd scope cannot narrow the admin flag.
        "permissions": {"is_admin": True},
    },
    {
        "slug": "operator",
        "name": "Operator",
        "description": "Day-to-day operation: deploy, run and use everything, but delete "
                       "nothing.",
        "permissions": {
            "vms": _RWU, "jobs": _RW, "workgroups": _R, "inventory": _R,
            "aws": _RWU, "azure": _RWU, "gcp": _RWU, "oci": _RWU, "costs": _R,
            "proxmox": _RW, "vsphere": _RW, "hyperv": _RW, "nutanix": _RW, "xcpng": _RW,
            "connections": _RW,
            "images": _RWU, "containers": _RWU, "k8s": _RWU,
            "cloud_function": _RWU, "cloud_database": _RWU,
            "storage": _RW, "secrets": _RU, "config_mgmt": _RWU,
            "pov": _RWU, "pov_templates": _R,
            "gateways": _RW, "agents": _RW, "notifications": _RW, "epml": _RW, "ot": _RW,
        },
    },
    {
        "slug": "read-only",
        "name": "Read-Only",
        "description": "See everything, change nothing. Every section at its read level.",
        # All 31 scopes offer `read`, so this one is exhaustive by construction.
        "permissions": {
            "vms": _R, "jobs": _R, "workgroups": _R, "inventory": _R, "audit": _R,
            "aws": _R, "azure": _R, "gcp": _R, "oci": _R, "costs": _R,
            "proxmox": _R, "vsphere": _R, "hyperv": _R, "nutanix": _R, "xcpng": _R,
            "connections": _R,
            "images": _R, "containers": _R, "k8s": _R, "cloud_function": _R,
            "cloud_database": _R, "storage": _R, "secrets": _R, "config_mgmt": _R,
            "pov": _R, "pov_templates": _R,
            "gateways": _R, "agents": _R, "notifications": _R, "epml": _R, "ot": _R,
        },
    },
    {
        "slug": "pov-presenter",
        "name": "POV Presenter",
        "description": "Run a proof of value: tick use cases, wake environments, and read "
                       "the estate behind them. Narrow it further with the POV access "
                       "picker on the user.",
        # `pov:use` is the level that carries use-case ticking and wake -- see
        # docs/permissions.md. Create/destroy/share stay on write/delete, which this omits.
        "permissions": {
            "pov": _RU, "pov_templates": _R,
            "vms": _R, "jobs": _R, "inventory": _R, "connections": _R,
        },
    },
    {
        "slug": "auditor",
        "name": "Auditor",
        "description": "Oversight without change: the audit trail, the job history, and the "
                       "inventory those jobs produced.",
        "permissions": {
            "audit": _R, "jobs": _R, "inventory": _R, "vms": _R,
            "agents": _R, "workgroups": _R, "notifications": _R, "costs": _R,
        },
    },
    {
        "slug": "cloud-admin",
        "name": "Cloud Admin",
        "description": "Full control of the cloud accounts and what runs in them, without "
                       "the admin-only pages.",
        "permissions": {
            "aws": _ALL, "azure": _ALL, "gcp": _ALL, "oci": _ALL, "costs": _RW,
            "vms": _RWD, "images": _RWD, "jobs": _RW, "inventory": _R, "workgroups": _R,
        },
    },
    {
        "slug": "dba",
        "name": "DBA",
        "description": "Cloud databases end to end, plus the secrets a database run needs.",
        "permissions": {
            "cloud_database": _ALL,
            "secrets": _RU, "jobs": _RW, "connections": _R, "inventory": _R,
        },
    },
    {
        "slug": "platform-k8s",
        "name": "Platform / K8s",
        "description": "Clusters, containers and functions, plus the images and "
                       "configuration they are built from.",
        "permissions": {
            "k8s": _ALL, "containers": _ALL, "cloud_function": _ALL,
            "images": _RWD, "storage": _RWD, "config_mgmt": _RWD,
            "jobs": _RW, "inventory": _R,
        },
    },
)

BUILTIN_SLUGS = tuple(r["slug"] for r in _BUILTIN_ROLES)


# ── Naming ────────────────────────────────────────────────────────────────────

def slugify(name: str) -> str:
    """A display name to its canonical handle, or raise.

    Derived rather than asked for, so a duplicate NAME produces a duplicate slug and a 409
    from the unique index -- which is why this model carries no second constraint on `name`.
    """
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    if not SLUG_RE.match(s):
        raise RoleError(
            f"Invalid role name {name!r}. Use 2-64 characters of letters, digits, spaces or "
            "hyphens.")
    return s


# ── Validation ────────────────────────────────────────────────────────────────

def validate_role_permissions(payload, *, slug: Optional[str] = None) -> dict:
    """Return the map, or raise ``RoleError``. The role-shaped wrapper around the user one.

    Two refusals this adds over ``api/auth.validate_permissions_payload``, both of which
    exist because a role is read by every principal assigned it rather than by one person:

    * **A missing or empty map is refused**, not defaulted. On a role, empty means "grants
      nothing", and a caller who omitted the field did not mean that -- while a reader who
      later "fixes" empty to mean "everything" would hand every assignee every scope. Being
      unable to express it at all is the only version of this that stays safe.
    * **``is_admin`` is refused on anything but the frozen ``administrator`` built-in.** The
      admin flag is a total bypass in ``has_permission``, so an editable role carrying it
      makes the role editor an admin-maker.
    """
    from ..api.auth import validate_permissions_payload

    if payload is None or not isinstance(payload, dict) or not payload:
        raise RoleError(
            "A role must say what it grants: send a permissions map. An empty map would "
            "grant nothing, and there is no way to spell 'unrestricted' on a role -- use "
            "the Administrator role, or the user's Admin flag, which is auditable.")

    # Reuse the catalog validator wholesale: unknown scope, unknown level, a level the scope
    # does not offer, and a non-list value (which would turn `level in perms[scope]` into a
    # substring test) all raise there already, with messages naming the catalog.
    validate_permissions_payload(payload)

    if payload.get("is_admin") and slug != ADMIN_ROLE_SLUG:
        raise RoleError(
            "Only the built-in Administrator role may grant the admin flag. Grant the "
            "sections this role needs instead, or set the Admin flag on the user.")
    return payload


# ── Reads ─────────────────────────────────────────────────────────────────────

def get(db: Session, role_id: str) -> Optional[AccessRole]:
    if not role_id:
        return None
    return db.query(AccessRole).filter(AccessRole.id == role_id).first()


def get_by_slug(db: Session, slug: str) -> Optional[AccessRole]:
    return db.query(AccessRole).filter(AccessRole.slug == slug).first()


def list_all(db: Session) -> List[AccessRole]:
    """Built-ins first, then custom roles, each alphabetically.

    Deterministic order matters for a page an admin scans: the eight rows they did not write
    stay together at the top instead of interleaving with their own as those are named.
    """
    rows = db.query(AccessRole).all()
    return sorted(rows, key=lambda r: (not bool(r.is_builtin), (r.name or "").lower()))


def assignees(db: Session, role_id: str) -> dict:
    """Who holds this role -- users and group mappings, listed separately.

    Two lists and not one count, because they answer different questions and have different
    remedies: a user is reassigned here and now, while a group mapping's members only change
    at their next sign-in.
    """
    users = (db.query(User).filter(User.role_id == role_id)
             .order_by(User.username).all())
    mappings = (db.query(OAuthGroupMapping).filter(OAuthGroupMapping.role_id == role_id)
                .order_by(OAuthGroupMapping.display_name).all())
    return {"users": users, "group_mappings": mappings}


def assignee_counts(db: Session, role_id: str) -> tuple:
    """``(user_count, group_mapping_count)`` without loading the rows."""
    return (
        db.query(User).filter(User.role_id == role_id).count(),
        db.query(OAuthGroupMapping).filter(OAuthGroupMapping.role_id == role_id).count(),
    )


# ── The single writer ─────────────────────────────────────────────────────────

def apply_role_to_user(db: Session, user: User, role: Optional[AccessRole]) -> None:
    """Assign ``role`` to ``user``, or clear the assignment when ``role`` is None.

    THE only place ``users.role_id`` and ``users.role_permissions`` are written, and it
    always writes both. Setting the id without the copy leaves a principal whose role cannot
    be resolved; ``database._ROLE_UNRESOLVED`` makes that deny rather than open, but it is
    still a user who can do nothing and an admin with nothing to look at.

    Does not commit -- the caller owns the transaction, so an assignment and the rest of a
    user update land together.
    """
    if role is None:
        user.role_id = None
        user.role_permissions = None
        return
    user.role_id = role.id
    user.role_permissions_dict = role.permissions_dict


def fan_out(db: Session, role: AccessRole) -> int:
    """Push a role's current map onto every user holding it. Returns the row count.

    Called from the role PATCH in the SAME transaction as the edit, so there is no window on
    PostgreSQL in which half the assignees hold the old map. The blast radius is returned so
    the API can tell the admin how many people they just changed.
    """
    rows = db.query(User).filter(User.role_id == role.id).all()
    for u in rows:
        u.role_permissions_dict = role.permissions_dict
    return len(rows)


def reconcile(db: Session) -> int:
    """Rewrite every role-bearing user's copy from its role. Returns rows changed.

    The repair half of the materialised copy: a hand-edited database, a restore, or an
    interrupted deploy can leave a copy that no longer matches its role, and nothing else
    would ever notice. Runs once per boot.

    Not marker-gated, unlike ``_backfill_new_permission_scopes``. A marker exists to stop a
    backfill re-granting something an admin deliberately removed; this writes only what the
    role already says, so running it twice is the same as running it once. A user whose
    ``role_id`` points at a role that no longer exists is CLEARED rather than left dangling
    -- the deny is correct either way, but an admin can act on "no role" and cannot act on
    an id with nothing behind it.
    """
    changed = 0
    for u in db.query(User).filter(User.role_id.isnot(None)).all():
        role = get(db, u.role_id)
        if role is None:
            u.role_id = None
            u.role_permissions = None
            changed += 1
            continue
        want = role.permissions_dict
        if u.role_permissions_dict != want:
            u.role_permissions_dict = want
            changed += 1
    if changed:
        db.commit()
    return changed


# ── Writes ────────────────────────────────────────────────────────────────────

def create(db: Session, *, name: str, description: Optional[str], permissions: dict,
           created_by_user_id: Optional[str] = None) -> AccessRole:
    """Create a custom role. ``is_builtin`` is never settable from outside this module."""
    slug = slugify(name)
    if get_by_slug(db, slug) is not None:
        raise RoleError(f"A role named {name!r} already exists.")
    validate_role_permissions(permissions, slug=slug)
    role = AccessRole(
        id=str(uuid.uuid4()),
        slug=slug,
        name=name.strip(),
        description=(description or "").strip() or None,
        is_builtin=False,
        created_by_user_id=created_by_user_id,
    )
    role.permissions_dict = permissions
    db.add(role)
    db.commit()
    db.refresh(role)
    return role


def update(db: Session, role: AccessRole, *, name=None, description=None,
           permissions=None) -> int:
    """Edit a custom role. Returns how many users were fanned out to.

    Refuses a built-in outright, including its description. A built-in whose definition has
    drifted from ``_BUILTIN_ROLES`` makes the seed's meaning unknowable -- and since the seed
    never updates an existing row, there would be nothing to reconcile it against. Clone it
    and edit the clone; that path exists for exactly this.
    """
    if role.is_builtin:
        raise RoleError(
            f"{role.name!r} is a built-in role and cannot be edited. Clone it and edit the "
            "copy.")
    if name is not None:
        slug = slugify(name)
        clash = get_by_slug(db, slug)
        if clash is not None and clash.id != role.id:
            raise RoleError(f"A role named {name!r} already exists.")
        role.slug = slug
        role.name = name.strip()
    if description is not None:
        role.description = description.strip() or None
    fanned = 0
    if permissions is not None:
        validate_role_permissions(permissions, slug=role.slug)
        role.permissions_dict = permissions
        fanned = fan_out(db, role)
    role.updated_at = datetime.utcnow()
    db.commit()
    return fanned


def clone(db: Session, role: AccessRole, *, name: str, description=None,
          created_by_user_id: Optional[str] = None) -> AccessRole:
    """Copy a role's grants into a new custom role.

    Refuses to clone Administrator, and the refusal is the useful behaviour rather than a
    restriction: its map is ``{"is_admin": true}``, which a clone may not carry, so stripping
    it would produce an empty map -- and an empty role grants nothing. The result would be a
    role called "Administrator copy" that confers no access at all, which is the most
    confusing possible outcome.
    """
    if role.slug == ADMIN_ROLE_SLUG:
        raise RoleError(
            "The Administrator role cannot be cloned -- a copy may not carry the admin "
            "flag, and the copy would grant nothing. Set the Admin flag on the user "
            "instead, or build a role from the sections they need.")
    return create(db, name=name, description=description,
                  permissions=role.permissions_dict,
                  created_by_user_id=created_by_user_id)


def delete(db: Session, role: AccessRole, *, force: bool = False) -> dict:
    """Delete a custom role. Refuses while assigned unless ``force``.

    The guard is here and NOT on the foreign key, which looks like the obvious place and is
    not. The retrofit ``ALTER TABLE`` carries no REFERENCES clause, so an upgraded install
    has no constraint at all -- and the test suite runs on SQLite, which enforces none either
    way. A design leaning on ``ondelete="SET NULL"`` would be untested and, on every install
    that actually has data, absent. Worse, a silent SET NULL clears ``role_id`` and leaves
    ``role_permissions`` populated, which is a principal still holding a role's grants with
    nothing naming the role.
    """
    if role.is_builtin:
        raise RoleError(f"{role.name!r} is a built-in role and cannot be deleted.")
    users, mappings = assignee_counts(db, role.id)
    if (users or mappings) and not force:
        parts = []
        if users:
            parts.append(f"{users} user{'s' if users != 1 else ''}")
        if mappings:
            parts.append(f"{mappings} group mapping{'s' if mappings != 1 else ''}")
        raise RoleError(
            f"{role.name!r} is still assigned to {' and '.join(parts)}. Reassign them, or "
            "delete it with force to clear the assignment.")
    # Clear both columns together, for the reason in the docstring above.
    for u in db.query(User).filter(User.role_id == role.id).all():
        u.role_id = None
        u.role_permissions = None
    for m in db.query(OAuthGroupMapping).filter(OAuthGroupMapping.role_id == role.id).all():
        m.role_id = None
    db.delete(role)
    db.commit()
    return {"cleared_users": users, "cleared_group_mappings": mappings}


# ── Seeding ───────────────────────────────────────────────────────────────────

def seed_builtins(db: Session) -> int:
    """Insert any missing built-in role. Returns how many were created.

    Keyed on SLUG PRESENCE, not on the table being empty. ``workgroup_service.seed_if_empty``
    uses emptiness and it is the wrong key here: a ninth built-in shipped in a later release
    would never appear on an install that already has the first eight.

    **Never updates an existing row**, which is what makes this safe without a
    ``schema_markers`` entry. A marker guards a backfill whose re-run would re-grant
    something an administrator had deliberately removed; an insert-if-absent cannot
    re-grant, because the row it would touch is the row it skips. Combined with built-ins
    being undeletable, there is no state in which this resurrects something a person removed
    on purpose.
    """
    created = 0
    for spec in _BUILTIN_ROLES:
        if get_by_slug(db, spec["slug"]) is not None:
            continue
        role = AccessRole(
            id=str(uuid.uuid4()),
            slug=spec["slug"],
            name=spec["name"],
            description=spec["description"],
            is_builtin=True,
        )
        role.permissions_dict = spec["permissions"]
        db.add(role)
        created += 1
    if created:
        db.commit()
    return created
