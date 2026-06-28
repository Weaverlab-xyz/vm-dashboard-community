"""Cross-cloud cost API — account/subscription month-to-date spend.

Gated on ``cost_explorer_enabled``. Read-only and **admin-only** (it surfaces
billing data). Served through the shared cache with a long TTL — cost data
changes slowly and AWS Cost Explorer bills per request, so we cache hard and a
background warmer keeps the tile populated.
"""
import logging

from fastapi import APIRouter, Depends

from ..database import User
from ..services import cache_service, cost_service
from .auth import require_admin

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/costs", tags=["costs"])


@router.get("/summary")
async def cost_summary(current_user: User = Depends(require_admin)) -> dict:
    """Per-cloud account/subscription MTD spend + total (cached).

    Always 200 with per-cloud ``status`` ("ok"/"unavailable") so the tile can
    render partial data — a cloud without creds/permission degrades to
    "unavailable" rather than failing the whole request."""
    data, cached_at = await cache_service.get_or_refresh(
        cache_service.key_global("cost_summary"),
        cache_service.TTL["cost_summary"],
        cost_service.get_cost_summary,
    )
    return {**data, "cached_at": cached_at}


@router.get("/breakdown")
async def cost_breakdown(current_user: User = Depends(require_admin)) -> dict:
    """Per-cloud, per-service MTD spend for dashboard-managed resources
    (``managed-by=vm-dashboard``), cached. Same resilience contract as
    /summary — clouds without the tag/creds/permission report "unavailable"
    (with a hint) rather than failing the request."""
    data, cached_at = await cache_service.get_or_refresh(
        cache_service.key_global("cost_breakdown"),
        cache_service.TTL["cost_breakdown"],
        cost_service.get_cost_breakdown,
    )
    return {**data, "cached_at": cached_at}
