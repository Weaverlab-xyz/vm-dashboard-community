"""Per-region config resolution (multi-region support).

The dashboard historically pinned ONE region per cloud plus a single set of
per-feature resource ids tied to that region (Azure: ``azure_desktops_subnet_id``,
``azure_db_subnet_id``, …; AWS: ``aws_default_subnet_id``, ``ec2_ssh_key_secret``,
…; GCP: ``gcp_subnetwork``, ``gcp_db_network``, …). Operators who run several
regions in one account kept hitting cross-region mismatches (a subnet in westus2
while the deploy defaulted to centralus, …).

This module adds **per-region config sets**: a JSON map stored under a single
``<cloud>_region_configs`` config_service key, e.g. ``azure_region_configs``::

    {
      "westus2":   {resource_group, desktops_subnet_id, db_subnet_id, …},
      "centralus": {...}
    }

``resolve_region(cloud, region)`` returns that region's config with **per-field
fallback to the existing flat keys**. The flat keys remain the source of truth for
the configured default region and for any field a region entry leaves blank — so a
single-region install (no ``<cloud>_region_configs`` at all) resolves to EXACTLY the
flat-key values it does today. That backward-compat guarantee is the #1 requirement.

The sandbox auto-populates region entries (``<cloud>_region.<region>.<field>`` keys
merged by ``/api/setup/import``), so running it in two regions seeds both sets
without the operator hand-editing JSON.

Originally Azure-only (``resolve_azure_region``, still exported as a thin wrapper);
generalised to AWS/GCP. OCI has no per-region resource sets and is not represented
here.
"""
import json
import logging
from dataclasses import dataclass, field
from typing import Optional

from . import region_catalog

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Spec:
    # config_service key holding the JSON region map for this cloud.
    configs_key: str
    # config key whose value is the configured default region (never shadowed).
    default_region_key: str
    # region-config field -> the flat config key it falls back to when the region
    # entry leaves the field blank (or when resolving the default region).
    field_fallbacks: dict[str, str]
    # optional secondary flat-key fallback per field, tried when the primary flat
    # key is also blank (preserves historical quirks, e.g. Azure's vnet RG → RG).
    secondary_fallbacks: dict[str, str] = field(default_factory=dict)


# Per-cloud specs. The field -> flat-key maps are NOT derivable by string transform
# (Azure's gallery_name → azure_shared_image_gallery, GCP's ssh_key_secret →
# gcp_ssh_key_secret_name, …), so each is explicit.
_SPECS: dict[str, _Spec] = {
    "azure": _Spec(
        configs_key="azure_region_configs",
        default_region_key="azure_location",
        field_fallbacks={
            "resource_group":         "azure_resource_group",
            "vnet_resource_group":    "azure_vnet_resource_group",
            "default_subnet_id":      "azure_default_subnet_id",
            "desktops_subnet_id":     "azure_desktops_subnet_id",
            "db_subnet_id":           "azure_db_subnet_id",
            "db_mysql_subnet_id":     "azure_db_mysql_subnet_id",
            "db_private_dns_zone_id": "azure_db_private_dns_zone_id",
            "gallery_name":           "azure_shared_image_gallery",
            "gallery_resource_group": "azure_gallery_resource_group",
            "default_vm_size":        "azure_desktops_vm_size",
        },
        # Historical: a blank vnet RG inherits the resource group.
        secondary_fallbacks={"vnet_resource_group": "azure_resource_group"},
    ),
    "aws": _Spec(
        configs_key="aws_region_configs",
        default_region_key="aws_region",
        field_fallbacks={
            "default_subnet_id":          "aws_default_subnet_id",
            "default_security_group_id":  "aws_default_security_group_id",
            "ssh_key_secret":             "ec2_ssh_key_secret",
            "ssm_instance_profile":       "ec2_ssm_instance_profile",
            "db_subnet_group_name":       "aws_db_subnet_group_name",
            # VPC-scoped ids. A VPC lives in exactly one region, so everything
            # derived from it (route table, DB/NAT security groups, the DB
            # parameter groups, the Jumpoint/ECS runner subnets) is per-region
            # too — the sandbox emits all of these.
            "vpc_id":                     "aws_vpc_id",
            "vpc_cidr":                   "aws_vpc_cidr",
            "private_route_table_id":     "aws_private_route_table_id",
            "db_security_group_id":       "aws_db_security_group_id",
            "db_parameter_group_name":    "aws_db_parameter_group_name",
            "db_mysql_parameter_group_name": "aws_db_mysql_parameter_group_name",
            "nat_security_group_id":      "aws_nat_security_group_id",
            "ecs_subnet_id":              "ansible_ecs_subnet_id",
            "ecs_security_group_ids":     "ansible_ecs_security_group_ids",
            "ecs_cluster":                "bt_ecs_cluster",
            "jumpoint_subnet_id":         "bt_ecs_jumpoint_subnet_id",
            "jumpoint_security_group_id": "bt_ecs_jumpoint_security_group_id",
        },
        # The Jumpoint host and the ECS runners share the sandbox's public subnet
        # unless split explicitly.
        secondary_fallbacks={
            "jumpoint_subnet_id":         "ansible_ecs_subnet_id",
            "jumpoint_security_group_id": "ansible_ecs_security_group_ids",
        },
    ),
    "gcp": _Spec(
        configs_key="gcp_region_configs",
        default_region_key="gcp_region",
        field_fallbacks={
            "zone":                 "gcp_zone",
            "network":              "gcp_network",
            "subnetwork":           "gcp_subnetwork",
            "jumpoint_subnetwork":  "gcp_jumpoint_subnetwork",
            "db_network":           "gcp_db_network",
            "ssh_key_secret":       "gcp_ssh_key_secret_name",
            "default_network_tag":  "gcp_default_network_tag",
            # Subnetworks are regional in GCP, so the runner/Jumpoint subnets and
            # the Cloud Router/NAT the sandbox creates are per-region too.
            "ecs_subnetwork":       "gcp_runner_subnetwork",
            "router_name":          "gcp_router_name",
            "nat_name":             "gcp_nat_name",
        },
        # Historical: jumpoint subnet inherits the VM subnet; DB network the network.
        secondary_fallbacks={
            "jumpoint_subnetwork": "gcp_subnetwork",
            "db_network":          "gcp_network",
            "ecs_subnetwork":      "gcp_subnetwork",
        },
    ),
}

# Clouds that support per-region config sets.
REGION_CONFIG_CLOUDS = tuple(_SPECS.keys())


def _spec(cloud: str) -> _Spec:
    c = (cloud or "").strip().lower()
    if c not in _SPECS:
        raise ValueError(f"no per-region config for cloud {cloud!r} "
                         f"(expected one of {', '.join(REGION_CONFIG_CLOUDS)})")
    return _SPECS[c]


def _norm(cloud: str, region: Optional[str]) -> str:
    """Normalise a region to its canonical lookup key (delegated to region_catalog:
    lower/trim, and for Azure strip spaces so 'West US 2' → 'westus2')."""
    return region_catalog.normalize(cloud, region)


def region_fields(cloud: str) -> tuple[str, ...]:
    """Canonical region-config field order for ``cloud``."""
    return tuple(_spec(cloud).field_fallbacks.keys())


def field_fallbacks(cloud: str) -> dict[str, str]:
    """The region-field → flat-config-key map for ``cloud`` (copy)."""
    return dict(_spec(cloud).field_fallbacks)


def configs_key(cloud: str) -> str:
    """The config_service key holding ``cloud``'s JSON region map."""
    return _spec(cloud).configs_key


def load_region_configs(cloud: str) -> dict[str, dict]:
    """Return the parsed ``<cloud>_region_configs`` map ({} when unset/invalid).

    Keys are normalised regions; values are partial field dicts. Never raises — a
    malformed value logs and yields {} so resolution falls back to flat keys.
    """
    from . import config_service
    spec = _spec(cloud)
    raw = config_service.get(spec.configs_key)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        logger.warning("%s is not valid JSON — ignoring (using flat keys).", spec.configs_key)
        return {}
    if not isinstance(data, dict):
        logger.warning("%s is not a JSON object — ignoring (using flat keys).", spec.configs_key)
        return {}
    out: dict[str, dict] = {}
    for loc, fields in data.items():
        if isinstance(fields, dict):
            out[_norm(cloud, loc)] = fields
    return out


def save_region_configs(cloud: str, configs: dict[str, dict]) -> None:
    """Persist ``cloud``'s region map as one JSON string.

    Each value is filtered to the known fields (blank fields dropped) and regions
    are normalised; an entry with no non-blank field is omitted. Replace-on-save.
    """
    from . import config_service
    spec = _spec(cloud)
    fields = spec.field_fallbacks
    cleaned: dict[str, dict] = {}
    for loc, vals in (configs or {}).items():
        nloc = _norm(cloud, loc)
        if not nloc or not isinstance(vals, dict):
            continue
        entry = {
            f: str(vals[f]).strip()
            for f in fields
            if vals.get(f) is not None and str(vals.get(f)).strip()
        }
        if entry:
            cleaned[nloc] = entry
    config_service.set(spec.configs_key, json.dumps(cleaned, sort_keys=True))


def merge_region_fields(cloud: str, updates: dict[str, dict]) -> dict[str, dict]:
    """Merge ``{region: {field: value}}`` updates into ``cloud``'s stored map and
    persist. Existing entries are updated field-by-field (never clobbered), so
    running the sandbox in two regions populates BOTH. Returns the merged map.
    """
    spec = _spec(cloud)
    current = load_region_configs(cloud)
    for loc, fields in (updates or {}).items():
        nloc = _norm(cloud, loc)
        if not nloc or not isinstance(fields, dict):
            continue
        entry = dict(current.get(nloc, {}))
        for f, value in fields.items():
            if f not in spec.field_fallbacks or value is None:
                continue
            sval = str(value).strip()
            if sval:
                entry[f] = sval
        if entry:
            current[nloc] = entry
    save_region_configs(cloud, current)
    return current


def _flat(cloud: str, fld: str) -> str:
    """Resolve a region field straight from its flat config key(s): the primary
    flat key, then any secondary fallback (e.g. Azure vnet RG → RG)."""
    from . import config_service
    from ..config import settings
    spec = _spec(cloud)
    key = spec.field_fallbacks[fld]
    val = config_service.get(key) or getattr(settings, key, "")
    if not val and fld in spec.secondary_fallbacks:
        sk = spec.secondary_fallbacks[fld]
        val = config_service.get(sk) or getattr(settings, sk, "")
    return val


def resolve_region(cloud: str, region: Optional[str]) -> dict:
    """Return the effective config set for ``region`` on ``cloud``.

    For every field: use the region entry's value when set, else fall back to the
    flat config key. When ``region`` is the configured default region or has no
    region entry, every field resolves to the flat keys — i.e. identical to the
    pre-multi-region behaviour. Single-region installs (no ``<cloud>_region_configs``)
    therefore see no change.

    The returned dict always carries every field key (blank string when nothing
    resolves), so callers can index ``resolve_region(cloud, r)[field]`` directly.
    """
    from . import config_service
    from ..config import settings
    spec = _spec(cloud)

    nloc = _norm(cloud, region)
    default_loc = _norm(cloud, config_service.get(spec.default_region_key)
                        or getattr(settings, spec.default_region_key, ""))

    # The default region (or an unconfigured/blank lookup) maps straight to the flat
    # keys — no region entry can shadow the historical defaults.
    entry: dict = {}
    if nloc and nloc != default_loc:
        entry = load_region_configs(cloud).get(nloc, {})

    resolved: dict[str, str] = {}
    for fld in spec.field_fallbacks:
        regional = entry.get(fld)
        if regional is not None and str(regional).strip():
            resolved[fld] = str(regional).strip()
        else:
            resolved[fld] = _flat(cloud, fld)
    return resolved


# ── Azure back-compat shims (unchanged public surface) ────────────────────────
# The module was Azure-only; these keep existing imports/callers working verbatim.

REGION_CONFIGS_KEY = _SPECS["azure"].configs_key
_FIELD_FALLBACK_KEYS = _SPECS["azure"].field_fallbacks
REGION_FIELDS = tuple(_SPECS["azure"].field_fallbacks.keys())


def resolve_azure_region(location: Optional[str]) -> dict:
    """Azure per-region resolution — thin wrapper over ``resolve_region('azure', …)``."""
    return resolve_region("azure", location)
