"""
Workgroup CRUD API.

Workgroups scope RBAC and cloud-resource visibility (via AWS `Workgroup` tag,
Azure/GCP `workgroup` tag/label). Names are canonical lowercase; the
``display_name`` field preserves the original casing for UI rendering.
"""
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..database import User, get_db
from ..services import workgroup_service
from ..services.workgroup_service import WorkgroupError
from .auth import get_current_user, require_admin, require_permission

router = APIRouter(prefix="/api/workgroups", tags=["workgroups"])


# ── Schemas ───────────────────────────────────────────────────────────────────

class WorkgroupCreate(BaseModel):
    name: str = Field(..., min_length=2, max_length=64)
    display_name: str = Field(..., min_length=1, max_length=200)
    description: Optional[str] = None
    local_vm_path: Optional[str] = None
    is_default: bool = False


class WorkgroupUpdate(BaseModel):
    display_name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    description: Optional[str] = None
    local_vm_path: Optional[str] = None


class WorkgroupResponse(BaseModel):
    id: str
    name: str
    display_name: str
    description: Optional[str] = None
    local_vm_path: Optional[str] = None
    is_default: bool
    member_count: int

    class Config:
        from_attributes = True


class WorkgroupDetailResponse(WorkgroupResponse):
    members: List[str]  # usernames


class MemberAssignRequest(BaseModel):
    username: str


# ── Helpers ───────────────────────────────────────────────────────────────────

def _to_response(db: Session, wg) -> WorkgroupResponse:
    return WorkgroupResponse(
        id=wg.id,
        name=wg.name,
        display_name=wg.display_name,
        description=wg.description,
        local_vm_path=wg.local_vm_path,
        is_default=bool(wg.is_default),
        member_count=len(workgroup_service.members(db, wg.name)),
    )


def _raise_from(err: WorkgroupError, default_code: int = 400) -> None:
    msg = str(err)
    code = default_code
    if "not found" in msg.lower():
        code = 404
    elif "already exists" in msg.lower() or "cannot be deleted" in msg.lower() or "still assigned" in msg.lower():
        code = 409
    raise HTTPException(status_code=code, detail=msg)


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("", response_model=List[WorkgroupResponse])
def list_workgroups(
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("workgroups", "read")),
):
    return [_to_response(db, w) for w in workgroup_service.list_all(db)]


@router.get("/{name}", response_model=WorkgroupDetailResponse)
def get_workgroup(
    name: str,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("workgroups", "read")),
):
    wg = workgroup_service.get(db, name)
    if not wg:
        raise HTTPException(status_code=404, detail=f"Workgroup '{name}' not found")
    members = [u.username for u in workgroup_service.members(db, wg.name)]
    base = _to_response(db, wg)
    return WorkgroupDetailResponse(**base.model_dump(), members=members)


@router.post("", response_model=WorkgroupResponse, status_code=status.HTTP_201_CREATED)
def create_workgroup(
    payload: WorkgroupCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("workgroups", "write")),
):
    try:
        wg = workgroup_service.create(
            db,
            name=payload.name,
            display_name=payload.display_name,
            description=payload.description,
            local_vm_path=payload.local_vm_path,
            is_default=payload.is_default,
            created_by_user_id=current_user.id,
        )
    except WorkgroupError as exc:
        _raise_from(exc)
    return _to_response(db, wg)


@router.patch("/{name}", response_model=WorkgroupResponse)
def update_workgroup(
    name: str,
    payload: WorkgroupUpdate,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("workgroups", "write")),
):
    try:
        wg = workgroup_service.update(
            db,
            name,
            display_name=payload.display_name,
            description=payload.description,
            local_vm_path=payload.local_vm_path,
        )
    except WorkgroupError as exc:
        _raise_from(exc)
    return _to_response(db, wg)


@router.delete("/{name}", status_code=status.HTTP_204_NO_CONTENT)
def delete_workgroup(
    name: str,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    try:
        workgroup_service.delete(db, name)
    except WorkgroupError as exc:
        _raise_from(exc)


@router.get("/{name}/members", response_model=List[str])
def list_members(
    name: str,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("workgroups", "read")),
):
    if not workgroup_service.exists(db, name):
        raise HTTPException(status_code=404, detail=f"Workgroup '{name}' not found")
    return [u.username for u in workgroup_service.members(db, name)]


@router.post("/{name}/members/{username}", status_code=status.HTTP_204_NO_CONTENT)
def assign_member(
    name: str,
    username: str,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("workgroups", "write")),
):
    user = db.query(User).filter(User.username == username).first()
    if not user:
        raise HTTPException(status_code=404, detail=f"User '{username}' not found")
    try:
        workgroup_service.assign_user(db, name, user)
    except WorkgroupError as exc:
        _raise_from(exc)


@router.delete("/{name}/members/{username}", status_code=status.HTTP_204_NO_CONTENT)
def unassign_member(
    name: str,
    username: str,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("workgroups", "write")),
):
    user = db.query(User).filter(User.username == username).first()
    if not user:
        raise HTTPException(status_code=404, detail=f"User '{username}' not found")
    workgroup_service.unassign_user(db, name, user)
