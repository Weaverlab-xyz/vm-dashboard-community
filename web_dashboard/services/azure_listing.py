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


def resource_group_from_vm_id(vm_id) -> str:
    """The resource group named in a VM's ARM id, or "" if it names none.

    ARM ids are the only authoritative answer to "which group is this VM in":
    ``/subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.Compute/…``.
    Matching is case-insensitive on the segment name because ARM is inconsistent
    about ``resourceGroups`` vs ``resourcegroups`` depending on which API wrote
    the id.
    """
    if not vm_id:
        return ""
    parts = str(vm_id).split("/")
    for i, part in enumerate(parts[:-1]):
        if part.lower() == "resourcegroups":
            return parts[i + 1]
    return ""


def destroy_probe_order(groups, preferred=None):
    """Resource groups to probe, in order, when locating a VM that has no deploy
    job recording its group.

    ``preferred`` — a group derived from the resource itself, e.g. parsed out of a
    desktop seat's ARM ``vm_resource_id`` — goes first. The rest follow in the same
    sorted order ``listing_resource_groups`` is walked in, so the VM found here is
    the one the VM list showed: two regions can each hold a VM of the same name
    (``clouddb-jumpoint`` does), and the listing de-duplicates by name, keeping the
    first group it sees. Blanks and duplicates are dropped.
    """
    order = []
    for rg in [preferred] + sorted(groups or ()):
        if rg and rg not in order:
            order.append(rg)
    return order
