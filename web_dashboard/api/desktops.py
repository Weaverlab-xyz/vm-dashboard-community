"""Virtual-desktop management API — Phase 0 scaffold.

Gated on ``vdesktops_enabled``. Phase 0 endpoints CRUD the ``virtual_desktops``
table via ``vdesktop_service`` — no cloud calls, no PRA.

  GET    /api/desktops/__phase0__       — health check (router-mounted probe)
  GET    /api/desktops                  — list seats
  GET    /api/desktops/pools            — list pool summaries
  POST   /api/desktops/pools            — create a pool (Phase 0: seat rows only)
  POST   /api/desktops/pools/{name}/scale
  DELETE /api/desktops/pools/{name}
"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import User, get_db
from ..models.vdesktop import PoolCreateRequest, PoolScaleRequest
from ..services import vdesktop_service
from ..services.vdesktop_service import VDesktopError
from .auth import require_admin

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/desktops", tags=["desktops"])


@router.get("/__phase0__")
def phase0_status() -> dict:
    """Health check — confirms the router is mounted/reachable."""
    return {
        "phase": 0,
        "ok": True,
        "note": (
            "Virtual-desktop Phase 0 scaffold — virtual_desktops CRUD only; "
            "Phase 1 wires real VM provisioning, Phase 2 adds PRA brokering."
        ),
    }


@router.get("")
async def list_desktops(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Every desktop seat across all pools."""
    return {"desktops": vdesktop_service.list_desktops(db)}


@router.get("/pools")
async def list_pools(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Pool summaries (name, cloud, kind, seat count, status breakdown)."""
    return {"pools": vdesktop_service.list_pools(db)}


@router.post("/pools", status_code=201)
async def create_pool(
    payload: PoolCreateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Create a desktop pool. Phase 0 inserts pending seat rows; Phase 1
    provisions the backing VMs."""
    try:
        return vdesktop_service.create_pool(
            db, cloud=payload.cloud, name=payload.name, image=payload.image,
            size=payload.size, count=payload.count, created_by=current_user.username,
        )
    except VDesktopError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/pools/{name}/scale")
async def scale_pool(
    name: str,
    payload: PoolScaleRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Grow/shrink a pool to ``count`` seats."""
    try:
        return vdesktop_service.scale_pool(db, name, payload.count)
    except VDesktopError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/pools/{name}")
async def delete_pool(
    name: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Delete a pool (Phase 1 deprovisions the backing VMs first)."""
    n = vdesktop_service.delete_pool(db, name)
    if n == 0:
        raise HTTPException(status_code=404, detail=f"Pool '{name}' not found.")
    return {"ok": True, "deleted_seats": n}
