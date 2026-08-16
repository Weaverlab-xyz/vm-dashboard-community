"""Region catalog endpoint.

Serves the shared ``services/region_catalog`` to the UI so deploy/provision forms
and the per-region config editors share one source of region ids + display labels
(and the configured default region), instead of each template hardcoding its own
list. The catalog is a convenience list, not an allow-list — operators may run
regions we don't enumerate, so custom entries are still accepted by the validators.

Two lists, for two different jobs:

  * ``regions`` — the catalog. What an operator may *configure* (the per-region
    config editors in Settings → Multi-region), where any real region is fair game.
  * ``configured`` — what the deploy forms may *offer*: the regions that actually
    have a config set, from ``region_config.deployable_regions``. A region without
    one has no subnet of its own, so deploying there lands the resource on the
    default region's network.
"""
from fastapi import APIRouter, Depends, HTTPException, Query

from ..database import User
from ..services import region_catalog, region_config
from .auth import get_current_user

router = APIRouter(prefix="/api/regions", tags=["regions"])


@router.get("")
async def list_regions(
    cloud: str = Query(..., description="aws | gcp | azure | oci"),
    current_user: User = Depends(get_current_user),
):
    """Selectable regions (``id`` + display ``label``) for ``cloud``, the configured
    default region, and the subset that carries a per-region config set.

    For GCP the payload also carries ``zones``: the zone each configured region
    places zonal resources in, so a form that needs a zone can follow the region
    picker instead of asking the operator to keep the two in step by hand.
    """
    c = (cloud or "").strip().lower()
    if c not in region_catalog.CLOUDS:
        raise HTTPException(status_code=400, detail=f"unknown cloud {cloud!r}")
    configured = region_config.deployable_regions(c)
    payload = {
        "cloud": c,
        "regions": region_catalog.regions(c),
        "default": region_catalog.default_region(c),
        "configured": configured,
    }
    if c == "gcp":
        payload["zones"] = {r: region_config.resolve_zone_for_region(r) for r in configured}
    return payload
