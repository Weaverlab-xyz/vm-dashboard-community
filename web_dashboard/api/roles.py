"""
Access Role API — admin only.

Manages the named, reusable permission maps a user or an identity-provider group mapping is
assigned. See ``services/role_service`` for the rules; this module is the HTTP edge.

**Every route is ``require_admin``, and that is a decision rather than a leftover.** The
obvious alternative -- a ``roles`` permission scope, so role administration could itself be
delegated -- is wrong here for three reasons, in order of weight:

1. Adding a scope to ``PERMISSION_SCOPE_LEVELS`` is a **silent revocation** for every user
   holding an explicit map (see ``api/auth.has_permission``), and by this repo's own rules it
   would need its own frozen backfill list and its own ``schema_markers`` entry. That
   backfill would grant *nothing*, because these routes are new and no existing user is
   losing access to them. Full cost, zero preserved access.
2. ``roles:write`` is "edit the role N people already hold", which is one hop from
   "assign Administrator". A grantable scope there is an escalation primitive.
3. ``/api/users`` and ``/api/groups`` are ``require_admin`` for exactly this reason, and
   ``tests/test_permission_catalog._NAV_EXEMPT`` already records identity administration as
   the category that must stay on the admin flag. A third pattern here would contradict it.

If a non-admin surface ever needs role *names* for display, add one narrow projection route
returning slug + name. Do not invent the scope.
"""
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import User, get_db
from ..services import role_service
from ..services.role_service import RoleError
from .auth import require_admin

router = APIRouter(prefix="/api/roles", tags=["roles"])


# ── Pydantic schemas ──────────────────────────────────────────────────────────

class RoleCreate(BaseModel):
    name: str
    description: Optional[str] = None
    # REQUIRED, and deliberately not defaulted. On a role an empty map grants nothing, so a
    # caller who omitted the field did not mean that -- and a future reader who "fixes"
    # empty to mean unrestricted would hand every assignee every scope. `role_service`
    # refuses it; the type here just stops the 422 coming from the wrong layer.
    permissions: dict


class RoleUpdate(BaseModel):
    """Every field three-state: absent/None = no change, a value = set it."""
    name: Optional[str] = None
    description: Optional[str] = None
    permissions: Optional[dict] = None


class RoleClone(BaseModel):
    name: str
    description: Optional[str] = None


class RoleResponse(BaseModel):
    id: str
    slug: str
    name: str
    description: Optional[str] = None
    permissions: Optional[dict] = None
    is_builtin: bool = False
    # Split, not summed. A user is reassigned here and now; a group mapping's members only
    # pick a change up at their next sign-in, so an admin reading a blast radius needs to
    # know which kind they are looking at.
    user_count: int = 0
    group_mapping_count: int = 0


class RoleAssigneeUser(BaseModel):
    id: str
    username: str
    full_name: Optional[str] = None
    is_active: bool = True


class RoleAssigneeMapping(BaseModel):
    id: str
    display_name: str
    # The IdP's group object id. Named for Entra because that is the stored column and the
    # API contract; the UI labels it neutrally, since any OIDC provider's groups claim feeds
    # it. See docs/integrations/oidc.md.
    entra_group_id: str


class RoleAssigneesResponse(BaseModel):
    users: List[RoleAssigneeUser] = []
    group_mappings: List[RoleAssigneeMapping] = []


class RoleWriteResponse(RoleResponse):
    # How many users' materialised copies this edit rewrote. Returned because the admin is
    # changing other people's access, possibly a lot of it, and the count is the only
    # feedback that says so.
    fanned_out: int = 0


# ── Helpers ───────────────────────────────────────────────────────────────────

def _raise_from(err: RoleError) -> None:
    """Map a service error onto a status code, matching ``api/workgroups._raise_from``."""
    msg = str(err)
    low = msg.lower()
    if "not found" in low:
        code = 404
    elif ("already exists" in low or "cannot be edited" in low
            or "cannot be deleted" in low or "cannot be cloned" in low
            or "still assigned" in low):
        code = 409
    elif "invalid role name" in low:
        code = 400
    else:
        # A rejected permission map. 422 matches what `validate_permissions_payload` raises
        # for the same class of problem on the user and group paths.
        code = 422
    raise HTTPException(status_code=code, detail=msg)


def _to_response(db: Session, role, *, fanned_out: Optional[int] = None):
    users, mappings = role_service.assignee_counts(db, role.id)
    fields = dict(
        id=role.id,
        slug=role.slug,
        name=role.name,
        description=role.description,
        permissions=role.permissions_dict or None,
        is_builtin=bool(role.is_builtin),
        user_count=users,
        group_mapping_count=mappings,
    )
    if fanned_out is None:
        return RoleResponse(**fields)
    return RoleWriteResponse(**fields, fanned_out=fanned_out)


def _get_or_404(db: Session, role_id: str):
    role = role_service.get(db, role_id)
    if role is None:
        raise HTTPException(status_code=404, detail="Role not found")
    return role


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("", response_model=List[RoleResponse])
def list_roles(
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    return [_to_response(db, r) for r in role_service.list_all(db)]


@router.get("/{role_id}", response_model=RoleResponse)
def get_role(
    role_id: str,
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    return _to_response(db, _get_or_404(db, role_id))


@router.get("/{role_id}/assignees", response_model=RoleAssigneesResponse)
def get_role_assignees(
    role_id: str,
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Who holds this role. The question the permission grid could never answer.

    Before roles, "who can delete a cloud database?" meant opening every user in turn. This
    is the whole reason the abstraction is worth having, so it is a first-class route rather
    than something the page assembles by filtering the user list client-side -- a non-admin
    list would be filtered server-side and the counts would silently disagree.
    """
    _get_or_404(db, role_id)
    rows = role_service.assignees(db, role_id)
    return RoleAssigneesResponse(
        users=[RoleAssigneeUser(id=u.id, username=u.username, full_name=u.full_name,
                                is_active=bool(u.is_active)) for u in rows["users"]],
        group_mappings=[RoleAssigneeMapping(id=m.id, display_name=m.display_name,
                                            entra_group_id=m.entra_group_id)
                        for m in rows["group_mappings"]],
    )


@router.post("", response_model=RoleResponse, status_code=201)
def create_role(
    body: RoleCreate,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    try:
        role = role_service.create(
            db, name=body.name, description=body.description,
            permissions=body.permissions, created_by_user_id=admin.id)
    except RoleError as exc:
        _raise_from(exc)
    return _to_response(db, role)


@router.patch("/{role_id}", response_model=RoleWriteResponse)
def update_role(
    role_id: str,
    body: RoleUpdate,
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Edit a custom role, fanning the new map out to its assignees in the same transaction.

    Built-ins are refused (409) -- clone and edit the copy. Group-mapping assignees are NOT
    fanned out to and do not need to be: their permissions are rebuilt from the mapping's
    role on every login, so they pick this up at next sign-in, exactly as a
    ``default_permissions`` edit always has.
    """
    role = _get_or_404(db, role_id)
    try:
        fanned = role_service.update(
            db, role, name=body.name, description=body.description,
            permissions=body.permissions)
    except RoleError as exc:
        _raise_from(exc)
    return _to_response(db, role, fanned_out=fanned)


@router.post("/{role_id}/clone", response_model=RoleResponse, status_code=201)
def clone_role(
    role_id: str,
    body: RoleClone,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Copy a role's grants into a new custom role — the supported way to adapt a built-in."""
    role = _get_or_404(db, role_id)
    try:
        new = role_service.clone(db, role, name=body.name, description=body.description,
                                 created_by_user_id=admin.id)
    except RoleError as exc:
        _raise_from(exc)
    return _to_response(db, new)


@router.delete("/{role_id}")
def delete_role(
    role_id: str,
    force: bool = False,
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Delete a custom role. 409 while assigned unless ``?force=true``.

    ``force`` clears the assignment from every user and group mapping in the same
    transaction rather than relying on the foreign key -- the retrofit ``ALTER TABLE``
    carries no ``REFERENCES`` clause, so on an upgraded install there is no constraint to
    rely on. See ``role_service.delete``.
    """
    role = _get_or_404(db, role_id)
    try:
        cleared = role_service.delete(db, role, force=force)
    except RoleError as exc:
        _raise_from(exc)
    return {"status": "deleted", **cleared}
