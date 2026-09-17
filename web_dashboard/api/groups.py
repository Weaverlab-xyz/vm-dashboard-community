"""
OAuth Group Mapping API — admin only.

Maps a group from the identity provider onto a dashboard workgroup, a default permission
set, an optional access role and an optional persona.

**Not Entra-only.** `entra_group_id` is the stored column name and part of this API's
contract, so it stays -- but the app has a fully generic OIDC login path beside the Entra
one (`api/auth.oauth_oidc_callback`), and the value is simply whatever group identifier the
provider's groups claim emits: an Entra group Object ID, an Okta group id, a Keycloak group
name. User-facing prose says "identity provider", matching docs/integrations/oidc.md; only
identifiers carry the older name. Same split as Gateway/Jumpoint -- see
tests/test_gateway_terminology.py.
"""
from typing import List, Optional
from pydantic import BaseModel
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import OAuthGroupMapping, get_db
from ..services import role_service, workgroup_service
from .auth import get_current_user, require_admin, validate_permissions_payload

router = APIRouter(prefix="/api/groups", tags=["groups"])


# ── Pydantic schemas ──────────────────────────────────────────────────────────

class GroupMappingCreate(BaseModel):
    entra_group_id: str
    display_name: str
    workgroup: str
    default_permissions: Optional[dict] = None  # None = all access for auto-provisioned users
    # The focus this group confers, and the tie-break when a user matches several
    # mappings. Both optional: a broad catch-all group can grant a workgroup without
    # dictating a focus, and that is the common case. NOT a permission -- see
    # services/personas on why a persona can only ever reorder.
    persona: Optional[str] = None
    persona_priority: Optional[int] = None
    # The access role every member of this group is granted. Unioned into their
    # session_permissions at login, alongside default_permissions above.
    role_id: Optional[str] = None


class GroupMappingResponse(BaseModel):
    id: str
    entra_group_id: str
    display_name: str
    workgroup: str
    default_permissions: Optional[dict] = None
    persona: str = ""
    persona_priority: Optional[int] = None
    role_id: str = ""
    role_name: str = ""

    class Config:
        from_attributes = True


class GroupMappingUpdate(BaseModel):
    """Every field three-state: None = no change, a value = set it.

    `entra_group_id` is deliberately absent. It is the mapping's IDENTITY -- the group object
    id the IdP's claim is matched against -- so changing it does not correct this mapping, it
    makes a different one. Delete and re-add for that.
    """
    display_name: Optional[str] = None
    workgroup: Optional[str] = None
    default_permissions: Optional[dict] = None
    persona: Optional[str] = None
    persona_priority: Optional[int] = None
    role_id: Optional[str] = None


# ── Endpoints ─────────────────────────────────────────────────────────────────

import json as _json


def _valid_persona(raw) -> Optional[str]:
    """A registry key, or None. Never the raw string.

    An unvalidated column would store a focus that resolves to nothing and then reads as
    "unset" with no way to tell it apart from a group that never had one -- and this value
    is chosen from a dropdown, so anything else arriving here is a client bug or a script.

    Refused outright on an instance with no focus axis (``personas.applies``), because the
    alternative is worse than a 409: the row would store a focus that nothing resolves, and
    an instance later reconfigured to an estate one would start honouring group assignments
    nobody made. CLEARING stays allowed on both profiles -- the same asymmetry a masked
    feature flag has, where turning it off is always permitted.
    """
    from ..services import feature_flags, personas
    want = (raw or "").strip().lower()
    if not want:
        return None
    if not personas.applies():
        raise HTTPException(
            status_code=409,
            detail=f"A focus cannot be assigned on {feature_flags.profile_noun()}.")
    if want not in personas.VALID_PERSONAS:
        raise HTTPException(status_code=422, detail=f"Unknown persona '{want}'")
    return want


def _resolve_role(db: Session, raw):
    """A role id to an `AccessRole`, or None to clear. 422 if unknown. Mirrors
    `api/users._resolve_role` so the two assignment paths cannot disagree."""
    if raw is None or not str(raw).strip():
        return None
    role = role_service.get(db, str(raw).strip())
    if role is None:
        raise HTTPException(status_code=422, detail=f"Unknown role '{raw}'")
    return role


def _mapping_to_response(m: OAuthGroupMapping, db: Session = None) -> GroupMappingResponse:
    perms = None
    if m.default_permissions:
        try:
            perms = _json.loads(m.default_permissions)
        except Exception:
            pass
    return GroupMappingResponse(
        id=m.id,
        entra_group_id=m.entra_group_id,
        display_name=m.display_name,
        workgroup=m.workgroup,
        default_permissions=perms,
        persona=m.persona or "",
        persona_priority=m.persona_priority,
        role_id=m.role_id or "",
        role_name=(_role_name(db, m.role_id) if db is not None else ""),
    )


def _role_name(db: Session, role_id) -> str:
    if not role_id:
        return ""
    role = role_service.get(db, role_id)
    return role.name if role else ""


@router.get("", response_model=List[GroupMappingResponse], dependencies=[Depends(require_admin)])
def list_group_mappings(db: Session = Depends(get_db)):
    """Every configured identity-provider group -> workgroup mapping.

    "Entra group" in the column and field names; ANY OIDC provider's groups claim feeds
    them -- see the module docstring.
    """
    rows = db.query(OAuthGroupMapping).order_by(OAuthGroupMapping.created_at).all()
    return [_mapping_to_response(m, db) for m in rows]


@router.post("", response_model=GroupMappingResponse, dependencies=[Depends(require_admin)])
def create_group_mapping(payload: GroupMappingCreate, db: Session = Depends(get_db)):
    """Add a new identity-provider group -> workgroup mapping."""
    if not workgroup_service.exists(db, payload.workgroup):
        valid_workgroups = workgroup_service.list_names(db)
        raise HTTPException(
            status_code=400,
            detail=f"Unknown workgroup '{payload.workgroup}'. Valid values: {valid_workgroups}",
        )
    if db.query(OAuthGroupMapping).filter(OAuthGroupMapping.entra_group_id == payload.entra_group_id).first():
        raise HTTPException(status_code=409, detail="A mapping for this Entra group ID already exists.")
    # Validated like `workgroup` and `persona` already are, rather than json.dumps'd
    # unchecked. A bad scope here is worse than on a user: _complete_oauth_login unions
    # these into session_permissions on EVERY login, so one typo in one mapping keeps
    # rewriting itself into every member's permissions, and there is no PUT on this
    # resource to correct it with -- only delete and recreate.
    validate_permissions_payload(payload.default_permissions)

    mapping = OAuthGroupMapping(
        entra_group_id=payload.entra_group_id.strip(),
        display_name=payload.display_name.strip(),
        workgroup=payload.workgroup,
        default_permissions=_json.dumps(payload.default_permissions) if payload.default_permissions else None,
        persona=_valid_persona(payload.persona),
        persona_priority=payload.persona_priority,
        role_id=(_resolve_role(db, payload.role_id).id if payload.role_id else None),
    )
    db.add(mapping)
    db.commit()
    db.refresh(mapping)
    return _mapping_to_response(mapping, db)


@router.patch("/{mapping_id}", response_model=GroupMappingResponse,
              dependencies=[Depends(require_admin)])
def update_group_mapping(mapping_id: str, payload: GroupMappingUpdate,
                         db: Session = Depends(get_db)):
    """Correct an existing mapping in place.

    **This resource had no edit at all**, and the comment on `create_group_mapping` above
    says why that mattered even then: these permissions are re-applied to every member on
    EVERY login, so a mistake keeps reasserting itself until the mapping is fixed, and the
    only fix was delete-and-recreate.

    Roles are what make the gap untenable rather than merely awkward. Changing a mapping's
    workgroup would otherwise mean deleting the row and retyping the group object id from
    the IdP -- and re-picking the role, where a slip silently changes every member's
    permissions at their next sign-in. `entra_group_id` stays immutable because it is the
    mapping's identity, not a property of it.

    Same validation as create, deliberately: `workgroup` must exist, `persona` must be a
    registry key, `default_permissions` goes through the catalog validator, and an unknown
    `role_id` is a 422 rather than a stored dangling reference.
    """
    mapping = db.query(OAuthGroupMapping).filter(OAuthGroupMapping.id == mapping_id).first()
    if not mapping:
        raise HTTPException(status_code=404, detail="Mapping not found.")

    if payload.workgroup is not None:
        if not workgroup_service.exists(db, payload.workgroup):
            raise HTTPException(
                status_code=400,
                detail=f"Unknown workgroup '{payload.workgroup}'. Valid values: "
                       f"{workgroup_service.list_names(db)}")
        mapping.workgroup = payload.workgroup
    if payload.display_name is not None:
        name = payload.display_name.strip()
        if not name:
            raise HTTPException(status_code=422, detail="display_name cannot be empty.")
        mapping.display_name = name
    if payload.default_permissions is not None:
        validate_permissions_payload(payload.default_permissions)
        mapping.default_permissions = (_json.dumps(payload.default_permissions)
                                       if payload.default_permissions else None)
    if payload.persona is not None:
        mapping.persona = _valid_persona(payload.persona)
    if payload.persona_priority is not None:
        # Only meaningful alongside a persona -- a priority on a mapping that expresses no
        # focus is a value nothing reads. Mirrors what the create form sends.
        mapping.persona_priority = payload.persona_priority if mapping.persona else None
    if payload.role_id is not None:
        role = _resolve_role(db, payload.role_id)
        mapping.role_id = role.id if role else None

    db.commit()
    db.refresh(mapping)
    return _mapping_to_response(mapping, db)


@router.delete("/{mapping_id}", dependencies=[Depends(require_admin)])
def delete_group_mapping(mapping_id: str, db: Session = Depends(get_db)):
    """Remove a group mapping by its ID."""
    mapping = db.query(OAuthGroupMapping).filter(OAuthGroupMapping.id == mapping_id).first()
    if not mapping:
        raise HTTPException(status_code=404, detail="Mapping not found.")
    db.delete(mapping)
    db.commit()
    return {"ok": True}


@router.get("/workgroups", dependencies=[Depends(get_current_user)])
def list_available_workgroups(db: Session = Depends(get_db)):
    """Return the workgroup names configured on this server."""
    return workgroup_service.list_names(db)
