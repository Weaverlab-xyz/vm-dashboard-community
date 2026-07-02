"""Audit-log integrity API.

Read-only and **admin-only**. The audit trail is hash-chained (see
``services/audit_chain.py``); this endpoint recomputes the chain and reports
whether it's intact, so an operator can detect any out-of-band edit or delete of
audit rows.
"""
import logging

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..database import User, get_db
from ..services import job_service
from .auth import require_admin

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/audit", tags=["audit"])


@router.get("/verify")
def verify_audit_log(
    current_user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:
    """Recompute the audit hash chain. Returns ``{ok, count, first_broken_seq}``:
    ``ok=false`` with the offending ``seq`` means a row was altered, removed, or
    reordered since it was written."""
    return job_service.verify_audit_chain(db)
