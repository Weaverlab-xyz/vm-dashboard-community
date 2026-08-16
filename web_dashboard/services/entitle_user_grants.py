"""Permission grant/revoke rules for the dashboard's own Entitle adapter.

Pure: every function takes an already-loaded user object and mutates only its
``jit_permissions_dict``. No FastAPI, no database, no HTTP — so the logic that
decides who gets administrator is provable with nothing installed, rather than only
in CI behind a web framework.

The one rule that matters most: **Entitle may only ever touch what Entitle granted.**
A dashboard user's permissions come from three independent sources —

    permissions          the admin-set baseline
    session_permissions  derived from OIDC/Entra groups, overwritten on every login
    jit_permissions      granted by Entitle through the REST adapter

— and this module writes only the third. An operator's own grant must survive an
Entitle revoke, and a group-derived permission is the group's to remove.
"""

# Not a scope/level pair: the is_admin flag. Named separately so it can never fall
# out of a generic loop over scopes, and so granting it is always an explicit act.
ADMIN_ASSET = "dashboard:admin"
ADMIN_ROLE = "admin"
ASSET_PREFIX = "dashboard:scope:"


def asset_for(scope: str, levels) -> dict:
    """A permission scope, as an Entitle asset whose roles are its levels."""
    return {
        "identifier": f"{ASSET_PREFIX}{scope}",
        "name": f"Dashboard: {scope}",
        "type": "dashboard_scope",
        "role_options": [
            {"code": level, "display_name": level.capitalize(), "available": True,
             "permissions": [f"{scope}:{level}"]}
            for level in levels
        ],
    }


def admin_asset() -> dict:
    return {
        "identifier": ADMIN_ASSET,
        "name": "Dashboard: administrator",
        "type": "dashboard_admin",
        "role_options": [{
            "code": ADMIN_ROLE, "display_name": "Administrator",
            "available": True, "permissions": ["*"],
        }],
    }


def scope_from_asset(payload: dict) -> str:
    """The scope an Entitle request names, or ``""`` if it names nothing we serve."""
    identifier = str((payload.get("asset") or {}).get("identifier") or "").strip()
    if identifier == ADMIN_ASSET:
        return ADMIN_ASSET
    if identifier.startswith(ASSET_PREFIX):
        return identifier[len(ASSET_PREFIX):]
    return ""


def matches_actor(user, identifier: str) -> bool:
    """Whether ``user`` is the actor named.

    Username OR email, case-insensitively: Entitle's actor identifier is whatever
    the operator mapped, and an exact-match-only lookup would silently grant
    nothing while reporting success.
    """
    target = (identifier or "").strip().lower()
    if not target:
        return False
    return target in (
        str(getattr(user, "username", "") or "").strip().lower(),
        str(getattr(user, "email", "") or "").strip().lower(),
    )


def grant(user, scope: str, level: str) -> bool:
    """Add one permission to the Entitle-granted set. ``True`` if it changed."""
    current = dict(user.jit_permissions_dict)
    if scope == ADMIN_ASSET:
        if current.get("is_admin"):
            return False
        current["is_admin"] = True
    else:
        levels = list(current.get(scope) or [])
        if level in levels:
            return False
        current[scope] = sorted(set(levels) | {level})
    user.jit_permissions_dict = current
    return True


def revoke(user, scope: str, level: str) -> bool:
    """Remove one permission. ``True`` if it changed.

    Touches only ``jit_permissions``. Entitle must not be able to revoke something
    it did not grant — an operator's admin flag and a group-derived permission both
    survive this untouched.
    """
    current = dict(user.jit_permissions_dict)
    if scope == ADMIN_ASSET:
        if not current.get("is_admin"):
            return False
        current.pop("is_admin", None)
    else:
        before = list(current.get(scope) or [])
        levels = [lvl for lvl in before if lvl != level]
        if levels == before:
            return False
        if levels:
            current[scope] = levels
        else:
            current.pop(scope, None)
    user.jit_permissions_dict = current
    return True


def held_by(user) -> list:
    """``[(role_code, ...)]`` Entitle currently holds for this user — ITS OWN grants
    only. Reporting the baseline or group-derived sets would invite Entitle to
    reconcile away access it never granted."""
    out = []
    for scope, value in user.jit_permissions_dict.items():
        if scope == "is_admin":
            if value:
                out.append(ADMIN_ROLE)
            continue
        out.extend(value or [])
    return out
