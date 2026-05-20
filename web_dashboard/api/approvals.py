"""
Entitle approval workflow — frontend poll endpoint + inbound webhook.

The dashboard never polls Entitle; the webhook is the only channel that
flips `Approval.status` from `pending` to `approved` or `denied`. The
dashboard host therefore needs to be reachable from Entitle's egress —
typically a public ingress with TLS terminating at a reverse proxy.

  GET  /api/approvals/{id}        — frontend poll, returns current status
  POST /api/approvals/webhook     — Entitle calls back with the decision
"""
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import Optional

from ..database import Approval, get_db
from .auth import get_current_user
from ..database import User
from ..services import entitle_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/approvals", tags=["approvals"])


class ApprovalStatus(BaseModel):
    id:         str
    action:     str
    status:     str  # pending | approved | denied | expired | consumed
    expires_at: str
    requested_at: str
    approved_at: str | None = None
    denial_reason: str | None = None


@router.get("/{approval_id}", response_model=ApprovalStatus)
def get_approval(
    approval_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Return the current status of an approval. The frontend polls this
    on a short interval while showing the operator a "waiting" modal."""
    row = db.query(Approval).filter(Approval.id == approval_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Approval not found")
    if row.user_id != current_user.id:
        # Don't leak existence of approvals belonging to other users.
        raise HTTPException(status_code=404, detail="Approval not found")

    # Auto-expire: if the row says pending but the clock is past expires_at,
    # flip to expired so the frontend stops polling. Cheap inline check so
    # we don't need a background sweeper.
    if row.status == "pending":
        now = datetime.now(timezone.utc)
        expires = row.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if now >= expires:
            row.status = "expired"
            db.commit()

    return ApprovalStatus(
        id=row.id,
        action=row.action,
        status=row.status,
        expires_at=row.expires_at.isoformat() if row.expires_at else "",
        requested_at=row.requested_at.isoformat() if row.requested_at else "",
        approved_at=row.approved_at.isoformat() if row.approved_at else None,
        denial_reason=row.denial_reason,
    )


# ── Webhook from Entitle ─────────────────────────────────────────────────────

class WebhookBody(BaseModel):
    request_id:    str
    status:        str  # "approved" | "denied"
    denial_reason: str | None = None


@router.post("/webhook")
async def webhook(
    request: Request,
    x_entitle_signature: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
):
    """Receive an approval decision from Entitle. Verified via HMAC-SHA256
    against `entitle_webhook_secret` — no auth header (Entitle's signature
    IS the auth)."""
    raw = await request.body()
    if not entitle_service.verify_webhook(raw, x_entitle_signature):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid signature")

    try:
        payload = WebhookBody.model_validate_json(raw)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Invalid webhook body: {exc}")

    row = (
        db.query(Approval)
        .filter(Approval.entitle_request_id == payload.request_id)
        .first()
    )
    if not row:
        # Idempotency: a webhook for an unknown request is a no-op. Log so
        # the operator can spot misrouted webhooks.
        logger.warning("Webhook for unknown Entitle request_id=%s", payload.request_id)
        return {"ok": True, "matched": False}

    new_status = payload.status.lower().strip()
    if new_status not in ("approved", "denied"):
        raise HTTPException(status_code=400, detail=f"Unsupported status: {payload.status}")

    # Webhook is the authoritative source; idempotent on the same value.
    row.status = new_status
    if new_status == "approved":
        row.approved_at = datetime.utcnow()
    elif new_status == "denied":
        row.denial_reason = payload.denial_reason or ""
    db.commit()
    logger.info("Entitle webhook: approval %s → %s", row.id, new_status)
    return {"ok": True, "matched": True, "status": new_status}
