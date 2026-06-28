"""Cross-provider deployment inventory API — a read-only aggregation of every
resource the dashboard has deployed, built from its own DB records (no live cloud
calls). Cached (a handful of indexed queries) and filtered to the caller's
workgroups; admins see everything.
"""
import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, Query

from ..database import User
from ..services import cache_service, inventory_service
from .auth import get_current_user

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/inventory", tags=["inventory"])


def _accessible_workgroups(user: User) -> Optional[List[str]]:
    """Canonical workgroup names the user can see, or None for admins (mirrors
    the per-provider list endpoints, e.g. api/aws.py)."""
    if user.is_effective_admin:
        return None
    return [w.lower() for w in user.workgroups_list]


@router.get("")
async def list_inventory(
    provider: Optional[str] = Query(None, description="Filter by cloud/provider (aws, azure, gcp, proxmox, nutanix)"),
    kind: Optional[str] = Query(None, description="Filter by kind (vm, database, k8s, desktop)"),
    current_user: User = Depends(get_current_user),
) -> dict:
    """All dashboard-deployed resources visible to the caller. Cached; RBAC +
    optional provider/kind filters applied per request."""
    cache_key = cache_service.key_global("deployment_inventory")
    ttl = cache_service.TTL["deployment_inventory"]

    async def _fetch():
        # Fresh session so a stale-while-revalidate background refresh isn't tied
        # to a request-scoped session that may already be closed.
        from ..database import SessionLocal
        s = SessionLocal()
        try:
            return inventory_service.collect(s)
        finally:
            s.close()

    raw, cached_at = await cache_service.get_or_refresh(cache_key, ttl, _fetch)

    accessible = _accessible_workgroups(current_user)
    items = [i for i in raw
             if inventory_service.visible_to(i, accessible, current_user.username)]
    if provider:
        items = [i for i in items if i["cloud"] == provider.lower()]
    if kind:
        items = [i for i in items if i["kind"] == kind.lower()]
    return {"items": items, "count": len(items), "cached_at": cached_at}
