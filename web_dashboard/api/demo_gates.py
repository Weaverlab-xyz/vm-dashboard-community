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
* **The agent cell's free choice of token user** (api/agentcell). Minting issues a
  credential that acts as a user, and every MCP tool applies that user's permissions. A
  `config_mgmt:write` holder picks any non-admin user; a caller who gets in through
  `demo` (``via_demo``) picks nobody -- the cell creates its own service account, holding
  the built-in Demo Agent role, in the cell's workstream, and only if the caller already
  holds every grant that role carries. So a presenter's agent never reaches further than
  the presenter. Linking an agent to a Workload Lab credential and opening a Kubernetes
  access episode stay on `config_mgmt:write` alone.
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


def via_demo(user: User, scope: str, level: str, demo_level: str = "") -> bool:
    """Does this caller get past a cell gate ONLY through `demo`?

    The switch a route reads to take its presenter path -- narrower choices, row checks --
    while a caller holding `<scope>:<level>` keeps the behaviour that scope always had.
    """
    return (not has_permission(user, scope, level)
            and demo_allows(user, demo_level or level))


def require_cell(scope: str, level: str, *, demo_level: str = ""):
    """Dependency: `<scope>:<level>` as before, or `demo:<demo_level or level>`.

    ``demo_level`` exists for a route whose own scope has no level for what it does to a
    cell: revoking an agent cell is `config_mgmt:write` (that scope offers no delete) but
    is a delete for `demo`.

    Tagged with the ORIGINAL scope, because that is still what the route answers to for
    everyone who is not a demo presenter -- a test reading the tag should see the gate it
    always saw.
    """
    alt = demo_level or level

    async def _check(current_user: User = Depends(get_current_user)) -> User:
        if has_permission(current_user, scope, level) or demo_allows(current_user, alt):
            return current_user
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Requires '{scope}:{level}' or 'demo:{alt}' permission.")
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


# ── Custom cell blueprints (api/custom_cell) ──────────────────────────────────
#
# `demo_blueprints` is the AUTHOR's scope -- the recipes -- and stands alone: there is no
# older scope to OR it with, because blueprints did not exist. Launching a cell from one
# is `demo:write` (``require_demo``), so a Demo Presenter launches what a Cell Builder
# wrote and cannot rewrite it. One literal call per level, as with `_DEMO` above.
_BLUEPRINTS = {
    "read": lambda u: has_explicit_permission(u, "demo_blueprints", "read"),
    "write": lambda u: has_explicit_permission(u, "demo_blueprints", "write"),
    "delete": lambda u: has_explicit_permission(u, "demo_blueprints", "delete"),
}


def blueprints_allow(user: User, level: str) -> bool:
    check = _BLUEPRINTS.get(level)
    return bool(check and check(user))


def require_blueprints(level: str):
    """Dependency: `demo_blueprints:<level>`.

    ``read`` also passes on `demo:write`: a presenter has to see the blueprint list to
    launch from it, and seeing a recipe grants nothing a launch would not.
    """
    async def _check(current_user: User = Depends(get_current_user)) -> User:
        if blueprints_allow(current_user, level):
            return current_user
        if level == "read" and demo_allows(current_user, "write"):
            return current_user
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Requires 'demo_blueprints:{level}' permission.")
    return _tag(_check, "demo_blueprints", level, explicit=True)


def require_demo(level: str):
    """Dependency: `demo:<level>` alone -- the custom cell routes are new, so there is no
    prior scope to accept beside it."""
    async def _check(current_user: User = Depends(get_current_user)) -> User:
        if demo_allows(current_user, level):
            return current_user
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail=f"Requires 'demo:{level}' permission.")
    return _tag(_check, "demo", level, explicit=True)
