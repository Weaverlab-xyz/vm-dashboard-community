"""Cross-provider deployment inventory API — a read-only aggregation of every resource
the dashboard knows about, built from its own DB records (no live cloud calls). That is
mostly what it deployed, plus what it was told about: a registered cloud database, a
registered K8s cluster, and every VM a remote agent has synced from a hypervisor.
Deployed cloud functions are here too — a live function is an HTTPS endpoint into the
network, so it belongs on the one page that answers "what is running".
Cached (a handful of indexed queries) and filtered to the caller's workgroups; admins
see everything.
"""
import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, Query

from ..database import User
from ..services import cache_service, inventory_service
from .auth import get_current_user, require_permission

logger = logging.getLogger(__name__)
# Read-only, and it keeps its existing workgroup row filter
# (inventory_service.accessible_workgroups): the scope says whether you may open the
# page at all, the workgroup filter says which rows are yours. `inventory` is
# read-only by design -- acting on a resource is the owning cloud scope's job.
router = APIRouter(prefix="/api/inventory", tags=["inventory"],
    dependencies=[Depends(require_permission("inventory", "read"))],
)


def _accessible_workgroups(user: User) -> Optional[List[str]]:
    """Canonical workgroup names the user can see, or None for admins (mirrors
    the per-provider list endpoints, e.g. api/aws.py). Delegates to the service so
    this endpoint and the bulk-run endpoint resolve RBAC identically."""
    return inventory_service.accessible_workgroups(user)


async def _attach_cloud_tags(items: list) -> None:
    """Give the cloud VM rows the tags their own console already shows.

    /inventory builds every row from the dashboard's OWN records — a deploy Job for a
    cloud VM — and a Job records what was asked for, never what the provider holds now.
    So a cloud row has no tags of its own, and the Tags column would be empty for the
    majority of an estate. Only the synced hypervisor rows carry them natively
    (inventory_service._hv_item, out of hypervisor_vm_cache).

    This closes that gap **without adding a single cloud call**: each cloud module hands
    back the tags already sitting in the instance cache its own listing fills, and `{}`
    on a miss. Worst case a row shows no tags for a minute, which is what it does today.

    Joined on ``(cloud, name)`` rather than a provider id, because the deploy Job's
    identifier key differs per cloud while the name is the one field every
    ``_vm_item`` shape resolves — and this dashboard already enforces VM-name uniqueness
    per cloud (``inventory_service.live_or_pending_vm_names``). Lowercased on both sides:
    AWS tag values are case-sensitive, Azure resource names are not, and GCE names are
    lowercase by rule.

    Best-effort per cloud: one unconfigured provider must not cost the others their
    tags, so a failure logs and leaves those rows as they were.
    """
    from . import aws, azure, gcp, oci
    for cloud, module in (("aws", aws), ("azure", azure), ("gcp", gcp), ("oci", oci)):
        rows = [i for i in items
                if i.get("cloud") == cloud and i.get("kind") == "vm" and not i.get("tags")]
        if not rows:
            continue
        try:
            by_name = await module.cached_tags_by_name()
        except Exception:  # noqa: BLE001
            logger.warning("inventory: could not read cached %s tags", cloud, exc_info=True)
            continue
        for item in rows:
            chips = by_name.get((item.get("name") or "").lower())
            if chips:
                item["tags"] = chips


@router.get("")
async def list_inventory(
    provider: Optional[str] = Query(None, description=(
        "Filter by cloud/provider (aws, azure, gcp, oci, and the hypervisor kinds "
        "proxmox, nutanix, vsphere, xcpng, hyperv, workstation)")),
    kind: Optional[str] = Query(None, description=(
        "Filter by kind (vm, database, k8s, function, certlab, spirelab, workloadk8s, "
        "workloadcloud, pov, desktop)")),
    current_user: User = Depends(get_current_user),
) -> dict:
    """Every resource visible to the caller. Cached; RBAC + optional provider/kind
    filters applied per request."""
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

    # After the filters, and on copies: `raw` is the SHARED cached list, so writing tags
    # into those dicts would leak one caller's enrichment into every other caller's rows
    # for the rest of the TTL. Copying only what is about to be returned also keeps the
    # work proportional to the page, not to the estate.
    items = [dict(i) for i in items]
    await _attach_cloud_tags(items)

    # Auto-delete state travels with the listing so /inventory's Expires badge and the
    # dashboard's "expiring soon" warning read ONE threshold instead of hardcoding two
    # that could drift. Read per request, not from the cached items, because it is
    # config-derived and the item cache is 60s stale by design.
    from ..services import expiry_policy, expiry_reaper
    expiry = {
        "enabled": expiry_policy.enabled(),
        "enforce": expiry_policy.enforce(),
        "dry_run": expiry_policy.dry_run(),
        "warn_hours": expiry_policy.warn_hours(),
        # The folded "is anything actually being destroyed" answer, so the dashboard's
        # warning wording and /inventory's badge can't disagree about it. Computed
        # server-side because it depends on two arming clocks, not just the flags.
        "deleting": expiry_reaper.status()["deleting"],
    }
    return {"items": items, "count": len(items), "cached_at": cached_at,
            "expiry": expiry}
