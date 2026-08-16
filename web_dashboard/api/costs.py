"""Cross-cloud cost API — account/subscription month-to-date spend.

Gated on ``cost_explorer_enabled``. Read-only and **admin-only** (it surfaces billing
data). Served out of ``services/cost_cache``, a durable per-cloud store shared by both
gunicorn workers and the jobs worker; a background warmer keeps it populated.

``?refresh=true`` forces a live requery. It deliberately does **not** invalidate first:
deleting the cached value before finding out whether there is anything to replace it with
is how a single Azure 429 used to wipe a working figure and install a six-hour error
string. A forced refresh is also floored at ``cost_refresh_min_interval_seconds`` per
(cloud, view), and a cloud in throttle cooldown is skipped entirely — mashing Refresh on a
rate-limited page can no longer compound the throttle that caused it.

The dashboard tile and the initial page load never force anything; they read whatever the
cache holds.
"""
import logging

from fastapi import APIRouter, Depends, Query

from ..database import User
from ..services import cost_cache, cost_service
from .auth import require_admin

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/costs", tags=["costs"])


@router.get("/summary")
async def cost_summary(
    refresh: bool = Query(False, description="Force a live requery (cooldown still applies)"),
    current_user: User = Depends(require_admin),
) -> dict:
    """Per-cloud account/subscription MTD spend + total.

    Always 200 with per-cloud ``status`` so the tile can render partial data. ``status`` is
    ``"ok"`` whenever a figure exists — **including a stale one**, in which case ``stale``
    is true and ``as_of``/``note`` say how old it is and why. ``"unavailable"`` now means
    the narrow thing it says: this cloud has never returned a number."""
    data = await cost_cache.get_summary(refresh=refresh)
    # Budgets (overall + per-cloud) are date- and config-dependent, so evaluate them per
    # request from the cached totals rather than caching them.
    return cost_service.apply_budget_alerts(data)


@router.get("/breakdown")
async def cost_breakdown(
    refresh: bool = Query(False, description="Force a live requery (cooldown still applies)"),
    current_user: User = Depends(require_admin),
) -> dict:
    """Per-cloud, per-service MTD spend split into **dashboard**
    (``managed-by=vm-dashboard``) and **sandbox** (``managed-by=dashboard-sandbox``) scope.

    Same resilience contract as /summary — a cloud that fails keeps serving its last known
    figures rather than blanking, and the payload shape is versioned by
    ``cost_cache.PAYLOAD_VERSION`` so a shape change is a miss, not a stale serve."""
    return await cost_cache.get_breakdown(refresh=refresh)
