"""Region catalog endpoint.

Serves the shared ``services/region_catalog`` to the UI so deploy/provision forms
and the per-region config editors share one source of region ids + display labels
(and the configured default region), instead of each template hardcoding its own
list. The catalog is a convenience list, not an allow-list — operators may run
regions we don't enumerate, so custom entries are still accepted by the validators.
"""
from fastapi import APIRouter, Depends, HTTPException, Query

from ..database import User
from ..services import region_catalog
from .auth import get_current_user

router = APIRouter(prefix="/api/regions", tags=["regions"])


@router.get("")
async def list_regions(
    cloud: str = Query(..., description="aws | gcp | azure | oci"),
    current_user: User = Depends(get_current_user),
):
    """Selectable regions (``id`` + display ``label``) for ``cloud``, plus the
    configured default region."""
    c = (cloud or "").strip().lower()
    if c not in region_catalog.CLOUDS:
        raise HTTPException(status_code=400, detail=f"unknown cloud {cloud!r}")
    return {
        "cloud": c,
        "regions": region_catalog.regions(c),
        "default": region_catalog.default_region(c),
    }
