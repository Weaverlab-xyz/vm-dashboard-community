"""Azure per-region config resolution (multi-region support, Follow-on 6 PR3).

The dashboard historically pinned ONE Azure region (``azure_location``) plus a
single set of per-feature resource ids (``azure_desktops_subnet_id``,
``azure_db_subnet_id``, ``azure_shared_image_gallery``, …) tied to that region.
Operators who run ~3 regions in one account kept hitting cross-region mismatches
(a subnet in westus2 while the deploy defaulted to centralus, a gallery image
replicated only to centralus, …).

PR3 adds **per-region config sets**: a JSON map stored under the single
config_service key ``azure_region_configs``::

    {
      "westus2":   {resource_group, vnet_resource_group, desktops_subnet_id,
                    db_subnet_id, db_mysql_subnet_id, db_private_dns_zone_id,
                    gallery_name, gallery_resource_group, default_vm_size},
      "centralus": {...}
    }

``resolve_azure_region(location)`` returns that region's config with **per-field
fallback to the existing flat keys**. The flat keys remain the source of truth
for the configured default region (``azure_location``) and for any field a
region entry leaves blank — so a single-region install (no
``azure_region_configs`` at all) resolves to EXACTLY the flat-key values it does
today. That backward-compat guarantee is the #1 requirement of this change.

The sandbox auto-populates region entries (``azure_region.<loc>.<field>`` keys
merged by ``/api/setup/import``), so running it in two regions seeds both sets
without the operator hand-editing JSON.
"""
import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# The config_service key holding the JSON region map.
REGION_CONFIGS_KEY = "azure_region_configs"

# Region-config field -> the flat config key it falls back to when the region
# entry leaves the field blank (or when resolving the default region). This map
# is the contract that keeps single-region setups behaving exactly as before:
# every region field has a flat-key fallback, and the flat keys are unchanged.
_FIELD_FALLBACK_KEYS: dict[str, str] = {
    "resource_group":          "azure_resource_group",
    "vnet_resource_group":     "azure_vnet_resource_group",
    "desktops_subnet_id":      "azure_desktops_subnet_id",
    "db_subnet_id":            "azure_db_subnet_id",
    "db_mysql_subnet_id":      "azure_db_mysql_subnet_id",
    "db_private_dns_zone_id":  "azure_db_private_dns_zone_id",
    "gallery_name":            "azure_shared_image_gallery",
    "gallery_resource_group":  "azure_gallery_resource_group",
    "default_vm_size":         "azure_desktops_vm_size",
}

# Canonical field order — what the model, resolver, and sandbox emit all agree on.
REGION_FIELDS: tuple[str, ...] = tuple(_FIELD_FALLBACK_KEYS.keys())


def _norm(location: Optional[str]) -> str:
    """Normalise an Azure location to the canonical lookup key (lower, trimmed,
    spaces stripped). Azure regions are case-insensitive and may be written
    'West US 2' or 'westus2'; the sandbox + config store on the compact form."""
    return (location or "").strip().lower().replace(" ", "")


def load_region_configs() -> dict[str, dict]:
    """Return the parsed ``azure_region_configs`` map ({} when unset/invalid).

    Keys are normalised locations; values are partial field dicts. Never raises —
    a malformed value logs and yields {} so resolution falls back to flat keys.
    """
    from . import config_service
    raw = config_service.get(REGION_CONFIGS_KEY)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        logger.warning("azure_region_configs is not valid JSON — ignoring (using flat keys).")
        return {}
    if not isinstance(data, dict):
        logger.warning("azure_region_configs is not a JSON object — ignoring (using flat keys).")
        return {}
    out: dict[str, dict] = {}
    for loc, fields in data.items():
        if isinstance(fields, dict):
            out[_norm(loc)] = fields
    return out


def save_region_configs(configs: dict[str, dict]) -> None:
    """Persist the region map as one JSON string under ``azure_region_configs``.

    Each value is filtered to the known REGION_FIELDS (blank fields dropped) and
    locations are normalised; an entry with no non-blank fields is omitted.
    """
    from . import config_service
    cleaned: dict[str, dict] = {}
    for loc, fields in (configs or {}).items():
        nloc = _norm(loc)
        if not nloc or not isinstance(fields, dict):
            continue
        entry = {
            f: str(fields[f]).strip()
            for f in REGION_FIELDS
            if fields.get(f) is not None and str(fields.get(f)).strip()
        }
        if entry:
            cleaned[nloc] = entry
    config_service.set(REGION_CONFIGS_KEY, json.dumps(cleaned, sort_keys=True))


def merge_region_fields(updates: dict[str, dict]) -> dict[str, dict]:
    """Merge ``{location: {field: value}}`` updates into the stored region map
    and persist. Existing region entries are updated field-by-field (never
    clobbered), so running the sandbox in westus2 then centralus populates BOTH.
    Returns the merged map.
    """
    current = load_region_configs()
    for loc, fields in (updates or {}).items():
        nloc = _norm(loc)
        if not nloc or not isinstance(fields, dict):
            continue
        entry = dict(current.get(nloc, {}))
        for field, value in fields.items():
            if field not in _FIELD_FALLBACK_KEYS:
                continue
            if value is None:
                continue
            sval = str(value).strip()
            if sval:
                entry[field] = sval
        if entry:
            current[nloc] = entry
    save_region_configs(current)
    return current


def _flat(field: str) -> str:
    """Resolve a region field straight from its flat config key (with the
    historical extra fallback that vnet_resource_group inherits the RG)."""
    from . import config_service
    from ..config import settings

    key = _FIELD_FALLBACK_KEYS[field]
    val = config_service.get(key) or getattr(settings, key, "")
    # Preserve the existing behaviour where azure_vnet_resource_group falls back
    # to azure_resource_group when blank (api/azure.py passed _cfg("azure_vnet_
    # resource_group") which the service then OR-ed against the RG; the network
    # path here resolves it up-front so the value is never empty).
    if field == "vnet_resource_group" and not val:
        val = config_service.get("azure_resource_group") or getattr(settings, "azure_resource_group", "")
    return val


def resolve_azure_region(location: Optional[str]) -> dict:
    """Return the effective config set for ``location``.

    For every field: use the region entry's value when set, else fall back to the
    flat config key. When ``location`` is the configured default region
    (``azure_location``) or there's no region entry for it, every field resolves
    to the flat keys — i.e. identical to pre-PR3 behaviour. Single-region installs
    (no ``azure_region_configs``) therefore see no change.

    The returned dict always carries every REGION_FIELDS key (blank string when
    nothing resolves), so callers can read ``resolve_azure_region(loc)[field]``
    unconditionally.
    """
    from . import config_service
    from ..config import settings

    nloc = _norm(location)
    default_loc = _norm(config_service.get("azure_location") or getattr(settings, "azure_location", ""))

    # The default region (or an unconfigured/blank lookup) maps straight to the
    # flat keys — no region entry can shadow the historical defaults.
    entry: dict = {}
    if nloc and nloc != default_loc:
        entry = load_region_configs().get(nloc, {})

    resolved: dict[str, str] = {}
    for field in REGION_FIELDS:
        regional = entry.get(field)
        if regional is not None and str(regional).strip():
            resolved[field] = str(regional).strip()
        else:
            resolved[field] = _flat(field)
    return resolved
