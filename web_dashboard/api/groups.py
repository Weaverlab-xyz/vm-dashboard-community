"""
OAuth Group Mapping API — admin only.
Manages the Entra ID group → dashboard workgroup mappings stored in the DB.
"""
from typing import List, Optional
from pydantic import BaseModel
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import OAuthGroupMapping, get_db
from ..services import workgroup_service
from .auth import get_current_user, require_admin

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


class GroupMappingResponse(BaseModel):
    id: str
    entra_group_id: str
    display_name: str
    workgroup: str
    default_permissions: Optional[dict] = None
    persona: str = ""
    persona_priority: Optional[int] = None

    class Config:
        from_attributes = True


# ── Endpoints ─────────────────────────────────────────────────────────────────

import json as _json


def _valid_persona(raw) -> Optional[str]:
    """A registry key, or None. Never the raw string.

    An unvalidated column would store a focus that resolves to nothing and then reads as
    "unset" with no way to tell it apart from a group that never had one -- and this value
    is chosen from a dropdown, so anything else arriving here is a client bug or a script.
    """
    from ..services import personas
    want = (raw or "").strip().lower()
    if not want:
        return None
    if want not in personas.VALID_PERSONAS:
        raise HTTPException(status_code=422, detail=f"Unknown persona '{want}'")
    return want


def _mapping_to_response(m: OAuthGroupMapping) -> GroupMappingResponse:
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
    )


@router.get("", response_model=List[GroupMappingResponse], dependencies=[Depends(require_admin)])
def list_group_mappings(db: Session = Depends(get_db)):
    """Return all configured Entra group → workgroup mappings."""
    return [_mapping_to_response(m) for m in db.query(OAuthGroupMapping).order_by(OAuthGroupMapping.created_at).all()]


@router.post("", response_model=GroupMappingResponse, dependencies=[Depends(require_admin)])
def create_group_mapping(payload: GroupMappingCreate, db: Session = Depends(get_db)):
    """Add a new Entra group → workgroup mapping."""
    if not workgroup_service.exists(db, payload.workgroup):
        valid_workgroups = workgroup_service.list_names(db)
        raise HTTPException(
            status_code=400,
            detail=f"Unknown workgroup '{payload.workgroup}'. Valid values: {valid_workgroups}",
        )
    if db.query(OAuthGroupMapping).filter(OAuthGroupMapping.entra_group_id == payload.entra_group_id).first():
        raise HTTPException(status_code=409, detail="A mapping for this Entra group ID already exists.")

    mapping = OAuthGroupMapping(
        entra_group_id=payload.entra_group_id.strip(),
        display_name=payload.display_name.strip(),
        workgroup=payload.workgroup,
        default_permissions=_json.dumps(payload.default_permissions) if payload.default_permissions else None,
        persona=_valid_persona(payload.persona),
        persona_priority=payload.persona_priority,
    )
    db.add(mapping)
    db.commit()
    db.refresh(mapping)
    return _mapping_to_response(mapping)


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
