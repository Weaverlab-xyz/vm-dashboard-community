"""
OAuth Group Mapping API — admin only.
Manages the Entra ID group → dashboard workgroup mappings stored in the DB.
"""
from typing import List, Optional
from pydantic import BaseModel
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..config import settings
from ..database import OAuthGroupMapping, get_db
from .auth import get_current_user, require_admin

router = APIRouter(prefix="/api/groups", tags=["groups"])


# ── Pydantic schemas ──────────────────────────────────────────────────────────

class GroupMappingCreate(BaseModel):
    entra_group_id: str
    display_name: str
    workgroup: str
    default_permissions: Optional[dict] = None  # None = all access for auto-provisioned users


class GroupMappingResponse(BaseModel):
    id: str
    entra_group_id: str
    display_name: str
    workgroup: str
    default_permissions: Optional[dict] = None

    class Config:
        from_attributes = True


# ── Endpoints ─────────────────────────────────────────────────────────────────

import json as _json


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
    )


@router.get("", response_model=List[GroupMappingResponse], dependencies=[Depends(require_admin)])
def list_group_mappings(db: Session = Depends(get_db)):
    """Return all configured Entra group → workgroup mappings."""
    return [_mapping_to_response(m) for m in db.query(OAuthGroupMapping).order_by(OAuthGroupMapping.created_at).all()]


@router.post("", response_model=GroupMappingResponse, dependencies=[Depends(require_admin)])
def create_group_mapping(payload: GroupMappingCreate, db: Session = Depends(get_db)):
    """Add a new Entra group → workgroup mapping."""
    valid_workgroups = list(settings.workgroups.keys())
    if payload.workgroup not in valid_workgroups:
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
def list_available_workgroups():
    """Return the workgroup names configured on this server."""
    return list(settings.workgroups.keys())
