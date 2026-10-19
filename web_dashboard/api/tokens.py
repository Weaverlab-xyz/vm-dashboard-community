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
from .auth import get_current_user, require_person
from ..utils.clock import utcnow

router = APIRouter(prefix="/api/tokens", tags=["tokens"])

PAT_PREFIX = "vmcli_"
# What an omitted ``expires_days`` means. It used to mean "never".
DEFAULT_PAT_DAYS = 90


def _generate_raw() -> str:
    return PAT_PREFIX + secrets.token_hex(32)


def hash_pat(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def pat_max_days() -> int:
    """The cap on a new PAT's lifetime in days, 0 for none (``pat_max_days``)."""
    from ..config import settings
    from ..services import config_service
    raw = config_service.get("pat_max_days", "")
    try:
        return max(0, int(raw if str(raw).strip() != "" else settings.pat_max_days))
    except (TypeError, ValueError):
        return max(0, int(settings.pat_max_days))


def pat_expiry(expires_days: Optional[int]) -> Optional[datetime]:
    """When a PAT minted now should expire, or ``None`` for never; 422 past the cap.

    ``None`` means the default (``DEFAULT_PAT_DAYS``, or the cap if that is shorter);
    ``0`` means never, which only an install with no cap allows. One rule for the
    self-service route here and the admin route in ``api/users``.
    """
    cap = pat_max_days()
    days = min(DEFAULT_PAT_DAYS, cap or DEFAULT_PAT_DAYS) if expires_days is None else expires_days
    if days < 0:
        raise HTTPException(status_code=422, detail="expires_days cannot be negative.")
    if cap and (days == 0 or days > cap):
        raise HTTPException(
            status_code=422,
            detail=f"A Personal Access Token can live at most {cap} days on this install "
                   f"(pat_max_days). Pass expires_days between 1 and {cap}.")
    return None if days == 0 else utcnow() + timedelta(days=days)


# ── Pydantic models ────────────────────────────────────────────────────────────

class CreateTokenRequest(BaseModel):
    name: str
    # None = DEFAULT_PAT_DAYS; 0 = never, only where pat_max_days is 0. See pat_expiry.
    expires_days: Optional[int] = None


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
def create_token(
    body: CreateTokenRequest,
    current_user: User = Depends(require_person),
    db: Session = Depends(get_db),
):
    """
    Create a new PAT for the authenticated user.
    The raw token is returned **once** — it cannot be retrieved again.
    """
    expires_at = pat_expiry(body.expires_days)
    raw = _generate_raw()
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
def list_tokens(
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
def revoke_token(
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
