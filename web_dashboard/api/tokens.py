"""
Personal Access Token (PAT) API endpoints.

PATs are long-lived Bearer tokens for machine-to-machine access (e.g. GitHub Actions).
Format: vmcli_<64 hex chars>  (prefix lets auth middleware skip JWT decode fast-path)
Stored: SHA-256 hash only — the raw token is shown exactly once at creation.

Usage in GitHub Actions:
  - Store the token as a repository secret (e.g. DASHBOARD_PAT)
  - Pass as: Authorization: Bearer ${{ secrets.DASHBOARD_PAT }}
"""
import hashlib
import secrets
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import PersonalAccessToken, User, get_db
from .auth import get_current_user

router = APIRouter(prefix="/api/tokens", tags=["tokens"])

PAT_PREFIX = "vmcli_"


def _generate_raw() -> str:
    return PAT_PREFIX + secrets.token_hex(32)


def hash_pat(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


# ── Pydantic models ────────────────────────────────────────────────────────────

class CreateTokenRequest(BaseModel):
    name: str
    expires_days: Optional[int] = None  # None = never expires


class TokenCreateResponse(BaseModel):
    id: str
    name: str
    token: str          # shown ONCE — store it now
    created_at: datetime
    expires_at: Optional[datetime] = None


class TokenListItem(BaseModel):
    id: str
    name: str
    created_at: datetime
    expires_at: Optional[datetime] = None
    last_used_at: Optional[datetime] = None
    is_active: bool


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("", response_model=TokenCreateResponse, status_code=201)
async def create_token(
    body: CreateTokenRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Create a new PAT for the authenticated user.
    The raw token is returned **once** — it cannot be retrieved again.
    """
    raw = _generate_raw()
    expires_at = (
        datetime.utcnow() + timedelta(days=body.expires_days)
        if body.expires_days
        else None
    )
    pat = PersonalAccessToken(
        user_id=current_user.id,
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


@router.get("", response_model=list[TokenListItem])
async def list_tokens(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List all PATs belonging to the current user (no raw token values)."""
    pats = (
        db.query(PersonalAccessToken)
        .filter(PersonalAccessToken.user_id == current_user.id)
        .order_by(PersonalAccessToken.created_at.desc())
        .all()
    )
    return [
        TokenListItem(
            id=p.id,
            name=p.name,
            created_at=p.created_at,
            expires_at=p.expires_at,
            last_used_at=p.last_used_at,
            is_active=p.is_active,
        )
        for p in pats
    ]


@router.delete("/{token_id}", status_code=200)
async def revoke_token(
    token_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Revoke (soft-delete) a PAT. Revoked tokens are immediately rejected."""
    pat = (
        db.query(PersonalAccessToken)
        .filter(
            PersonalAccessToken.id == token_id,
            PersonalAccessToken.user_id == current_user.id,
        )
        .first()
    )
    if not pat:
        raise HTTPException(status_code=404, detail="Token not found")
    pat.is_active = False
    db.commit()
    return {"detail": "Token revoked"}
