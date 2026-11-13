"""Who may see and run a demo cell: the scope the route always took, or `demo`.

The sibling of api/pov_gates.py, and the same shape for the same reason. The cell routes
were gated on the scope of whatever they touch -- `gcp:write` to build a network cell,
`aws:write` / `azure:write` / `gcp:write` to build an OT cell, `ot:read` for the OT router
-- which meant the only way to let somebody run a demo was to let them deploy anything
they liked into that cloud. `demo` is the narrower grant: these routes, and nothing else.

**Always an alternative, never a replacement.** Every gate here passes on the scope the
route took before, exactly as before, and additionally on `demo:<level>`. So adding it
narrowed nothing and needed no backfill (database/rbac_backfills, "deliberately empty").
Swapping a route to ``require_cell`` is safe for every existing user by construction; the
thing to review on such a swap is only whether that route belongs to a cell at all.

`demo` is checked in the EXPLICIT form, the way `pov_own` is in pov_gates: it is a new
authority nobody held before, so it must only ever be what an administrator named.

What `demo` deliberately does NOT reach:

* **The cloud's own routes.** ``POST /api/gcp/deploy``, the cloud's instance Destroy and
  every other general cloud route keep their gate. A cell is destroyed through
  ``DELETE /api/demo/cells/<kind>/<id>`` instead, which checks the row IS a live cell and
  then runs the cloud's destroy handler on that one VM; and the cell forms read their
  pickers through ``/api/demo/cells/<cloud>/...`` (``require_cell_options`` below).
* **The agent cell** (api/agentcell). Minting one issues an API token that acts as any
  non-admin user the caller picks, and every MCP tool applies that user's permissions.
  With `demo:write` on it, a presenter could mint a token as a Cloud Admin user and hold
  that user's reach. It stays on `config_mgmt:write` until the token user is bounded by
  the caller's own permissions.
* **Standalone OT protocol tunnels** (``/api/ot/tunnels``). They wire real plant gear,
  not a demo cell, so they keep `ot:*` plus the cloud level.
"""
from fastapi import Depends, HTTPException, status

from ..database import User
from .auth import _tag, get_current_user, has_explicit_permission, has_permission

# One literal call per level, so tests/test_permission_catalog's AST sweep sees each
# `demo:<level>` enforced. A single `has_explicit_permission(user, "demo", level)` would
# enforce exactly the same thing and be invisible to it.
_DEMO = {
    "read": lambda u: has_explicit_permission(u, "demo", "read"),
    "write": lambda u: has_explicit_permission(u, "demo", "write"),
    "delete": lambda u: has_explicit_permission(u, "demo", "delete"),
}


def demo_allows(user: User, level: str) -> bool:
    """Does this user hold `demo:<level>`? False for a level `demo` does not offer."""
    check = _DEMO.get(level)
    return bool(check and check(user))


def may(user: User, scope: str, level: str) -> bool:
    """`<scope>:<level>`, or `demo:<level>`. The predicate form of :func:`require_cell`."""
    return has_permission(user, scope, level) or demo_allows(user, level)


def require_cell(scope: str, level: str):
    """Dependency: `<scope>:<level>` as before, or `demo:<level>`.

    Tagged with the ORIGINAL scope, because that is still what the route answers to for
    everyone who is not a demo presenter -- a test reading the tag should see the gate it
    always saw.
    """
    async def _check(current_user: User = Depends(get_current_user)) -> User:
        if may(current_user, scope, level):
            return current_user
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Requires '{scope}:{level}' or 'demo:{level}' permission.")
    return _tag(_check, scope, level, explicit=False)


def require_cell_options(cloud: str):
    """Dependency for a cell form's pickers: `<cloud>:read`, or `demo:write`.

    `demo:write` and not `demo:read`, because the only reason to list a cloud's images,
    subnets and security groups here is to BUILD a cell -- a hub reader who cannot build
    one has no use for an inventory of the account it would land in. The cloud-scope half
    is the scope the cloud's own picker route already takes, so nothing narrows.
    """
    async def _check(current_user: User = Depends(get_current_user)) -> User:
        if has_permission(current_user, cloud, "read") or demo_allows(current_user, "write"):
            return current_user
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Requires '{cloud}:read' or 'demo:write' permission.")
    return _tag(_check, cloud, "read", explicit=False)


async def _require_demo_read(current_user: User = Depends(get_current_user)) -> User:
    if demo_allows(current_user, "read"):
        return current_user
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                        detail="Requires 'demo:read' permission or administrator.")


# The hub's own gate: `demo:read` alone. The hub is new, so there is no prior scope to OR.
require_demo_read = _tag(_require_demo_read, "demo", "read", explicit=True)
