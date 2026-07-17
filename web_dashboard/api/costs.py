"""Cross-cloud cost API — account/subscription month-to-date spend.

Gated on ``cost_explorer_enabled``. Read-only and **admin-only** (it surfaces
billing data). Served through the shared cache with a long TTL — cost data
changes slowly and AWS Cost Explorer bills per request, so we cache hard and a
background warmer keeps the tile populated.

``?refresh=true`` busts the cache before fetching so an admin can force fresh
figures right after fixing a permission / cost-allocation tag / config —
otherwise the page's Refresh button just re-reads the up-to-6h cache. It
re-runs the billable Cost Explorer / rate-limited Cost Management queries, so
it's opt-in per request: the dashboard tile and the initial page load stay on
the cache; only an explicit Refresh click forces the requery.
"""
import logging

from fastapi import APIRouter, Depends, Query

from ..database import User
from ..services import cache_service, cost_service
from .auth import require_admin

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/costs", tags=["costs"])


@router.get("/summary")
async def cost_summary(
    refresh: bool = Query(False, description="Bust the cache and re-query the clouds"),
    current_user: User = Depends(require_admin),
) -> dict:
    """Per-cloud account/subscription MTD spend + total (cached).

    Always 200 with per-cloud ``status`` ("ok"/"unavailable") so the tile can
    render partial data — a cloud without creds/permission degrades to
    "unavailable" rather than failing the whole request. ``refresh=true``
    invalidates the cache first so the next fetch is fresh."""
    key = cache_service.key_global("cost_summary")
    if refresh:
        await cache_service.invalidate(key)
    data, cached_at = await cache_service.get_or_refresh(
        key,
        cache_service.TTL["cost_summary"],
        cost_service.get_cost_summary,
    )
    # Budgets (overall + per-cloud) are date- and config-dependent, so evaluate
    # them per request from the cached totals rather than caching them.
    data = cost_service.apply_budget_alerts(data)
    return {**data, "cached_at": cached_at}


@router.get("/breakdown")
async def cost_breakdown(
    refresh: bool = Query(False, description="Bust the cache and re-query the clouds"),
    current_user: User = Depends(require_admin),
) -> dict:
    """Per-cloud, per-service MTD spend for dashboard-managed resources
    (``managed-by=vm-dashboard``), cached. Same resilience contract as
    /summary — clouds without the tag/creds/permission report "unavailable"
    (with a hint) rather than failing the request. ``refresh=true`` invalidates
    the cache first so the next fetch is fresh."""
    key = cache_service.key_global("cost_breakdown")
    if refresh:
        await cache_service.invalidate(key)
    data, cached_at = await cache_service.get_or_refresh(
        key,
        cache_service.TTL["cost_breakdown"],
        cost_service.get_cost_breakdown,
    )
    return {**data, "cached_at": cached_at}
