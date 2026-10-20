"""Who may create, change and destroy a POV: the general levels, or `pov_own` on your own.

`pov:write` and `pov:delete` reach every POV the user's POV access picker leaves visible,
and all of them when the picker is empty. `pov_own` reaches only the user's OWN POVs --
created by them, or assigned to them in the picker (`pov_env_service.owned_by`). It is what
the POV Presenter role carries: create POVs, set up and run and destroy your own, and
nothing on anyone else's.

Its own module because three routers need the same rule: api/pov.py, and the accessor and
vendor routers mounted under the same prefix, which cannot import api/pov.py without a
cycle. One copy, so the three cannot drift.

`pov_own` is always checked in the EXPLICIT form. A legacy NULL-permission user reads as
unrestricted, so they already pass the general branch; the explicit form keeps `pov_own`
from ever being the thing that grants them anything.

The router-level `auth.require_pov_env_access` still runs first on every route naming an
env_id, so a POV the user may not SEE is a 404 before any of these is consulted.
"""
from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from ..database import User, get_db
from ..services import pov_env_service
from .auth import _tag, get_current_user, has_explicit_permission, has_permission


def may_create(user: User) -> bool:
    return (has_permission(user, "pov", "write")
            or has_explicit_permission(user, "pov_own", "write"))


def may_on_env(user: User, db: Session, env_id: str, level: str) -> bool:
    """`pov:<level>`, or `pov_own:<level>` on a POV this user owns. `level` is "write" or
    "delete"."""
    if level == "write" and has_permission(user, "pov", "write"):
        return True
    if level == "delete" and has_permission(user, "pov", "delete"):
        return True
    if level == "write" and not has_explicit_permission(user, "pov_own", "write"):
        return False
    if level == "delete" and not has_explicit_permission(user, "pov_own", "delete"):
        return False
    return pov_env_service.owned_by(pov_env_service.get(db, env_id), user)


def handoff_refusal(db: Session, env, target: User | None) -> str:
    """Why ``target`` may NOT be handed this POV, or "" when they may.

    The point is that the new owner can actually run what they were handed. Setting
    ``created_by`` is all a handoff does, so every way that column alone would leave them
    looking at a 404 or a 403 is refused here, before the write, rather than discovered by
    them on the first click:

      * they must be a person who signs in: active, not a service account, not an accessor
        (an accessor is CONFINED to one POV's /self surface; owning one is meaningless);
      * they must be able to create POVs, which is the `pov:write` / `pov_own:write` pair
        :func:`may_create` already defines -- the same bar a presenter cleared to own the
        POVs they made;
      * their POV access picker must not hide it. An own-only user is fine (``created_by``
        is what their scope reads); a narrowed user whose picker omits this POV is not;
      * when the POV carries a workgroup, they must be in it, so job rows stamped with it
        stay inside the team that owns them.
    """
    if target is None or not getattr(target, "is_active", False):
        return "no active user by that name"
    if getattr(target, "is_service_account", False):
        return "a service account cannot own a POV"
    if getattr(target, "accessor_env_id", None):
        return "a POV accessor login cannot own a POV"
    if env.created_by and env.created_by == target.username:
        return "that user already owns this POV"
    if not may_create(target):
        return ("that user cannot run POVs: they need 'pov:write', or 'pov_own:write' "
                "(the POV Presenter role)")
    from .auth import pov_env_scope, pov_owns_only
    if not pov_owns_only(target):
        scope = pov_env_scope(target, db)
        if scope is not None and env.id not in scope:
            return ("that user's POV access is narrowed to other POVs; add this one to "
                    "their picker first")
    wg = (env.workgroup or "").strip().lower()
    if wg and not getattr(target, "is_effective_admin", False):
        if wg not in {w.lower() for w in (target.workgroups_list or [])}:
            return f"that user is not in this POV's workgroup ({env.workgroup})"
    return ""


def _refuse(level: str):
    raise HTTPException(
        status_code=403,
        detail=f"Requires 'pov:{level}' permission, or 'pov_own:{level}' on a POV you own "
               f"(created by you, or assigned to you).")


async def _require_create(current_user: User = Depends(get_current_user)) -> User:
    if may_create(current_user):
        return current_user
    raise HTTPException(
        status_code=403,
        detail="Requires 'pov:write' permission, or 'pov_own:write' to create your own POV.")


async def _require_write_on_path(request: Request,
                                 current_user: User = Depends(get_current_user),
                                 db: Session = Depends(get_db)) -> User:
    """Write on the POV named by the path's `{env_id}`. Reads `request.path_params`, the
    way `require_pov_env_access` does, so it works as a ROUTER-level dependency too -- a
    route added later is covered the day it is written."""
    env_id = (request.path_params or {}).get("env_id")
    if env_id is None:
        # Every route this is attached to names a POV. A route that does not is a wiring
        # mistake; refuse rather than fall back to something broader.
        raise HTTPException(status_code=403, detail="Requires 'pov:write' permission.")
    if may_on_env(current_user, db, env_id, "write"):
        return current_user
    _refuse("write")


async def _require_delete_on_path(request: Request,
                                  current_user: User = Depends(get_current_user),
                                  db: Session = Depends(get_db)) -> User:
    env_id = (request.path_params or {}).get("env_id") or ""
    if may_on_env(current_user, db, env_id, "delete"):
        return current_user
    _refuse("delete")


# Tagged with the GENERAL level they stand in for, so the "every POV route carries a
# permission gate" sweep (tests/test_pov_instance_grants.py) and anything else reading the
# tag see what the route is about.
require_create = _tag(_require_create, "pov", "write", explicit=False)
require_write_on_env = _tag(_require_write_on_path, "pov", "write", explicit=False)
require_delete_on_env = _tag(_require_delete_on_path, "pov", "delete", explicit=False)
