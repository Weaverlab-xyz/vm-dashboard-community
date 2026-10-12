"""
Health checks across the estate (``services/health``).
  GET  /api/estate-health              — every enabled area's stored results
  POST /api/estate-health/{area}/run   — run one area's checks now

Not ``/api/health``: that is the liveness probe, public and answering in milliseconds.

Administrator-only, like the Audit page. One page here spans BeyondTrust tenants, every
Password Safe account the dashboard onboarded and every agent, across all workgroups; a
grantable scope over that would be a view of everyone's resources by another name.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from ..database import User, get_db
from .auth import require_admin

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/estate-health", tags=["health"])


@router.get("")
def health_checks(request: Request, summary: bool = False, db: Session = Depends(get_db),
                  user: User = Depends(require_admin)):
    """Reads stored results only: no provider is called here. ``summary`` drops the
    per-object rows, for the home page's band."""
    from ..services import personas
    from ..services.health import runner
    persona, _source = personas.resolve(request)
    snap = runner.snapshot(db, persona=persona)
    if summary:
        snap["areas"] = [{"area": a["area"], "label": a["label"], "counts": a["counts"],
                          "last_error": a["last_error"]} for a in snap["areas"]]
    return snap


@router.post("/{area}/run")
async def run_area(area: str, db: Session = Depends(get_db),
                   user: User = Depends(require_admin)):
    from ..services.health import runner
    from ..services.health.registry import get
    collector = get(area)
    if collector is None:
        raise HTTPException(status_code=404, detail="no such health area")
    if not runner.enabled(collector):
        raise HTTPException(status_code=409,
                            detail=f"{collector.label} is not enabled on this instance.")
    return await runner.run_now(db, area)
