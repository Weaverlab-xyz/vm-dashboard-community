"""
User management API endpoints (admin only).
All routes require the authenticated user to have is_admin=True.
"""
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import User, Fido2Credential, PersonalAccessToken, get_db, get_password_hash
from ..models.user import UserResponse
from .auth import get_current_user, require_admin
from .tokens import _generate_raw, hash_pat, TokenCreateResponse

router = APIRouter(prefix="/api/users", tags=["users"])


# ── Pydantic models ────────────────────────────────────────────────────────────

class UserCreateRequest(BaseModel):
    username: str
    password: str
    full_name: Optional[str] = None
    email: Optional[str] = None
    workgroups: List[str] = []
    is_admin: bool = False


class UserUpdateRequest(BaseModel):
    full_name: Optional[str] = None
    email: Optional[str] = None
    workgroups: Optional[List[str]] = None
    is_active: Optional[bool] = None
    is_admin: Optional[bool] = None
    password: Optional[str] = None   # supply to reset password
    permissions: Optional[dict] = None  # None = no change; {} = clear (full access); dict = set specific perms


class UserTokenItem(BaseModel):
    id: str
    name: str
    created_at: datetime
    expires_at: Optional[datetime] = None
    last_used_at: Optional[datetime] = None
    is_active: bool


# ── List users ─────────────────────────────────────────────────────────────────

@router.get("", response_model=List[UserResponse])
async def list_users(
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    users = db.query(User).order_by(User.username).all()
    return [
        UserResponse(
            id=u.id,
            username=u.username,
            full_name=u.full_name,
            email=u.email,
            workgroups=u.workgroups_list,
            is_active=u.is_active,
            is_admin=u.is_admin or False,
            auth_provider=u.auth_provider,
            mfa_required=u.mfa_required,
            permissions=u.permissions_dict or None,
        )
        for u in users
    ]


# ── Create user ────────────────────────────────────────────────────────────────

@router.post("", response_model=UserResponse, status_code=201)
async def create_user(
    body: UserCreateRequest,
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if db.query(User).filter(User.username == body.username).first():
        raise HTTPException(status_code=409, detail="Username already exists")
    user = User(
        username=body.username,
        hashed_password=get_password_hash(body.password),
        full_name=body.full_name,
        email=body.email,
        is_active=True,
        is_admin=body.is_admin,
    )
    user.workgroups_list = body.workgroups
    db.add(user)
    db.commit()
    db.refresh(user)
    return UserResponse(
        id=user.id,
        username=user.username,
        full_name=user.full_name,
        email=user.email,
        workgroups=user.workgroups_list,
        is_active=user.is_active,
        is_admin=user.is_admin or False,
        auth_provider=user.auth_provider,
        mfa_required=user.mfa_required,
        permissions=user.permissions_dict or None,
    )


# ── Update user ────────────────────────────────────────────────────────────────

@router.patch("/{user_id}", response_model=UserResponse)
async def update_user(
    user_id: str,
    body: UserUpdateRequest,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    # Prevent admins from removing their own admin flag
    if user.id == admin.id and body.is_admin is False:
        raise HTTPException(status_code=400, detail="Cannot remove your own admin privilege")

    if body.full_name is not None:
        user.full_name = body.full_name
    if body.email is not None:
        user.email = body.email
    if body.workgroups is not None:
        user.workgroups_list = body.workgroups
    if body.is_active is not None:
        user.is_active = body.is_active
    if body.is_admin is not None:
        user.is_admin = body.is_admin
    if body.password:
        user.hashed_password = get_password_hash(body.password)
    if body.permissions is not None:
        # Empty dict {} clears restrictions (full access); non-empty dict sets specific perms
        user.permissions_dict = body.permissions if body.permissions else None

    db.commit()
    db.refresh(user)
    return UserResponse(
        id=user.id,
        username=user.username,
        full_name=user.full_name,
        email=user.email,
        workgroups=user.workgroups_list,
        is_active=user.is_active,
        is_admin=user.is_admin or False,
        auth_provider=user.auth_provider,
        mfa_required=user.mfa_required,
        permissions=user.permissions_dict or None,
    )


# ── Deactivate user ────────────────────────────────────────────────────────────

@router.delete("/{user_id}", status_code=200)
async def deactivate_user(
    user_id: str,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if user_id == admin.id:
        raise HTTPException(status_code=400, detail="Cannot deactivate your own account")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user.is_active = False
    db.commit()
    return {"detail": "User deactivated"}


# ── Permanently delete user ─────────────────────────────────────────────────────

@router.delete("/{user_id}/permanent", status_code=200)
async def delete_user_permanent(
    user_id: str,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Permanently remove a user and all associated tokens and FIDO2 credentials."""
    if user_id == admin.id:
        raise HTTPException(status_code=400, detail="Cannot delete your own account")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    db.delete(user)
    db.commit()
    return {"detail": "User permanently deleted"}


# ── List a user's PATs (admin view) ────────────────────────────────────────────

@router.get("/{user_id}/tokens", response_model=List[UserTokenItem])
async def list_user_tokens(
    user_id: str,
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if not db.query(User).filter(User.id == user_id).first():
        raise HTTPException(status_code=404, detail="User not found")
    pats = (
        db.query(PersonalAccessToken)
        .filter(PersonalAccessToken.user_id == user_id)
        .order_by(PersonalAccessToken.created_at.desc())
        .all()
    )
    return [
        UserTokenItem(
            id=p.id,
            name=p.name,
            created_at=p.created_at,
            expires_at=p.expires_at,
            last_used_at=p.last_used_at,
            is_active=p.is_active,
        )
        for p in pats
    ]


# ── Create a PAT for any user (admin) ─────────────────────────────────────────

class AdminCreateTokenRequest(BaseModel):
    name: str
    expires_days: Optional[int] = None


@router.post("/{user_id}/tokens", response_model=TokenCreateResponse, status_code=201)
async def create_user_token(
    user_id: str,
    body: AdminCreateTokenRequest,
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Create a PAT on behalf of any user. Raw token shown once — store it immediately."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    from datetime import timedelta
    raw = _generate_raw()
    expires_at = (
        datetime.utcnow() + timedelta(days=body.expires_days)
        if body.expires_days
        else None
    )
    pat = PersonalAccessToken(
        user_id=user_id,
        name=body.name,
        token_hash=hash_pat(raw),
        expires_at=expires_at,
    )
    db.add(pat)
    db.commit()
    db.refresh(pat)
    return TokenCreateResponse(
        id=pat.id,
        name=pat.name,
        token=raw,
        created_at=pat.created_at,
        expires_at=pat.expires_at,
    )


# ── Revoke any user's PAT (admin) ──────────────────────────────────────────────

@router.delete("/{user_id}/tokens/{token_id}", status_code=200)
async def revoke_user_token(
    user_id: str,
    token_id: str,
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    pat = (
        db.query(PersonalAccessToken)
        .filter(
            PersonalAccessToken.id == token_id,
            PersonalAccessToken.user_id == user_id,
        )
        .first()
    )
    if not pat:
        raise HTTPException(status_code=404, detail="Token not found")
    pat.is_active = False
    db.commit()
    return {"detail": "Token revoked"}


# ── FIDO2 summary per user (admin view) ────────────────────────────────────────

@router.get("/{user_id}/fido2", response_model=List[dict])
async def list_user_fido2(
    user_id: str,
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if not db.query(User).filter(User.id == user_id).first():
        raise HTTPException(status_code=404, detail="User not found")
    creds = (
        db.query(Fido2Credential)
        .filter(Fido2Credential.user_id == user_id)
        .all()
    )
    return [
        {
            "id": c.id,
            "device_name": c.device_name,
            "created_at": c.created_at.isoformat(),
            "last_used_at": c.last_used_at.isoformat() if c.last_used_at else None,
            "is_active": c.is_active,
        }
        for c in creds
    ]
