"""Pure helpers for scoping the Azure VM listing across regions.

Kept free of fastapi and the Azure SDK so the "which resource groups must a
listing cover?" decision is unit-testable on its own — the API layer supplies
the config lookups.
"""
import logging

logger = logging.getLogger(__name__)


def listing_resource_groups(job_meta, default_rg, rg_for, configured_regions):
    """Every resource group a VM listing must cover.

    A VM deployed into a non-default region lives in that region's resource
    group, so listing only the default one hides it. Sources: the configured
    default RG, every RG recorded on a live (non-destroyed) deploy job, and
    every RG configured for a region in ``azure_region_configs``.

    ``default_rg`` / ``configured_regions`` are zero-arg callables and ``rg_for``
    maps a region to its resource group, so this stays independent of how the
    caller reads config.
    """
    groups = set()
    base = default_rg()
    if base:
        groups.add(base)
    for meta in (job_meta or {}).values():
        if not meta.get("destroyed") and meta.get("resource_group"):
            groups.add(meta["resource_group"])
    try:
        for region in configured_regions():
            rg = rg_for(region)
            if rg:
                groups.add(rg)
    except Exception:  # a malformed region map must not break listing
        logger.warning("Azure VM listing: could not enumerate per-region resource groups",
                       exc_info=True)
    return groups
