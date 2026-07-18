"""Cross-cloud region catalog, validators, and default-region resolution.

Single source of truth for:
  * the set of selectable regions per cloud, with human-readable **labels**;
  * the region-string **validators** (one authoritative regex per cloud, plus a
    GCP zone validator); and
  * resolution of the configured **default region** (config key + hardcoded
    fallback).

This consolidates lists/regexes that were previously duplicated across
``api/aws.py``, ``api/gcp.py``, ``api/azure.py``, ``api/cloud_databases.py``,
``services/k8s_service.py``, ``services/azure_service.py``, and
``templates/setup.html``.

Backward-compat: each validator is a superset of the per-file regexes it replaces
(GovCloud + multi-digit partitions for AWS, multi-digit for GCP/OCI), and default
resolution reads the same config keys with the same hardcoded fallbacks — so
existing single-region behaviour is unchanged. Catalog lists are the union of every
region id the app previously offered anywhere, so no picker loses an option.

This module imports nothing from the app except ``config_service`` / ``settings``
(lazily, inside functions), so services and API routers can import it freely.
"""
import re
from typing import Optional

# Clouds that carry a region dimension in this dashboard.
CLOUDS = ("aws", "gcp", "azure", "oci")


# ── Catalog (id + display label) ──────────────────────────────────────────────
# Union of every region id the app offered before (setup.html labelled arrays,
# k8s_service.K8S_REGIONS, azure_service.get_network_options locations). Labels are
# the standard provider display names.
_CATALOG: dict[str, list[tuple[str, str]]] = {
    "aws": [
        ("us-east-1", "US East (N. Virginia)"),
        ("us-east-2", "US East (Ohio)"),
        ("us-west-1", "US West (N. California)"),
        ("us-west-2", "US West (Oregon)"),
        ("ca-central-1", "Canada (Central)"),
        ("eu-west-1", "Europe (Ireland)"),
        ("eu-west-2", "Europe (London)"),
        ("eu-central-1", "Europe (Frankfurt)"),
        ("ap-southeast-1", "Asia Pacific (Singapore)"),
        ("ap-southeast-2", "Asia Pacific (Sydney)"),
        ("ap-northeast-1", "Asia Pacific (Tokyo)"),
    ],
    "azure": [
        ("eastus", "East US"),
        ("eastus2", "East US 2"),
        ("westus", "West US"),
        ("westus2", "West US 2"),
        ("westus3", "West US 3"),
        ("centralus", "Central US"),
        ("northcentralus", "North Central US"),
        ("southcentralus", "South Central US"),
        ("canadacentral", "Canada Central"),
        ("canadaeast", "Canada East"),
        ("northeurope", "North Europe"),
        ("westeurope", "West Europe"),
        ("uksouth", "UK South"),
        ("ukwest", "UK West"),
        ("germanywestcentral", "Germany West Central"),
        ("francecentral", "France Central"),
        ("australiaeast", "Australia East"),
        ("australiasoutheast", "Australia Southeast"),
        ("southeastasia", "Southeast Asia"),
        ("eastasia", "East Asia"),
        ("japaneast", "Japan East"),
        ("japanwest", "Japan West"),
    ],
    "gcp": [
        ("us-central1", "US Central (Iowa)"),
        ("us-east1", "US East (South Carolina)"),
        ("us-east4", "US East (N. Virginia)"),
        ("us-west1", "US West (Oregon)"),
        ("us-west2", "US West (Los Angeles)"),
        ("us-west3", "US West (Salt Lake City)"),
        ("northamerica-northeast1", "Canada (Montréal)"),
        ("northamerica-northeast2", "Canada (Toronto)"),
        ("southamerica-east1", "South America (São Paulo)"),
        ("europe-west1", "Europe (Belgium)"),
        ("europe-west2", "Europe (London)"),
        ("europe-west3", "Europe (Frankfurt)"),
        ("europe-west4", "Europe (Netherlands)"),
        ("europe-west6", "Europe (Zürich)"),
        ("europe-north1", "Europe (Finland)"),
        ("europe-central2", "Europe (Warsaw)"),
        ("asia-east1", "Asia (Taiwan)"),
        ("asia-east2", "Asia (Hong Kong)"),
        ("asia-northeast1", "Asia (Tokyo)"),
        ("asia-northeast3", "Asia (Seoul)"),
        ("asia-south1", "Asia (Mumbai)"),
        ("asia-southeast1", "Asia (Singapore)"),
        ("australia-southeast1", "Australia (Sydney)"),
    ],
    "oci": [
        ("us-ashburn-1", "US East (Ashburn)"),
        ("us-phoenix-1", "US West (Phoenix)"),
        ("us-sanjose-1", "US West (San Jose)"),
        ("ca-toronto-1", "Canada Southeast (Toronto)"),
        ("ca-montreal-1", "Canada Southeast (Montreal)"),
        ("sa-saopaulo-1", "Brazil East (São Paulo)"),
        ("uk-london-1", "UK South (London)"),
        ("eu-frankfurt-1", "Germany Central (Frankfurt)"),
        ("eu-amsterdam-1", "Netherlands Northwest (Amsterdam)"),
        ("ap-mumbai-1", "India West (Mumbai)"),
        ("ap-tokyo-1", "Japan East (Tokyo)"),
        ("ap-seoul-1", "South Korea Central (Seoul)"),
        ("ap-singapore-1", "Singapore (Singapore)"),
        ("ap-sydney-1", "Australia East (Sydney)"),
    ],
}


# ── Validators ────────────────────────────────────────────────────────────────
# One authoritative regex per cloud. Supersets of the previous per-file copies:
#   AWS  — GovCloud (us-gov-*) + multi-digit partitions (e.g. us-east-2, ap-southeast-3)
#   GCP  — multi-digit region index (us-central1, europe-west4, australia-southeast1)
#   OCI  — 2-3 letter geo + area + partition digit (us-ashburn-1, eu-frankfurt-1)
#   Azure— canonical compact form: lowercase alphanumeric, no separators (eastus2)
_VALIDATORS: dict[str, re.Pattern] = {
    "aws":   re.compile(r"^[a-z]{2}(-gov)?-[a-z]+-\d+$"),
    "gcp":   re.compile(r"^[a-z]+-[a-z]+\d+$"),
    "azure": re.compile(r"^[a-z0-9]+$"),
    "oci":   re.compile(r"^[a-z]{2,3}-[a-z]+-\d+$"),
}

# GCP zones are <region>-<letter> (us-central1-a, europe-west1-b).
_GCP_ZONE_RE = re.compile(r"^[a-z]+-[a-z]+\d+-[a-z]$")

# Config key that holds the configured default region for each cloud, plus the
# hardcoded fallback used when that key is unset (matches the historical defaults).
_DEFAULT_REGION_KEY: dict[str, str] = {
    "aws": "aws_region", "azure": "azure_location",
    "gcp": "gcp_region", "oci": "oci_region",
}
_DEFAULT_REGION_FALLBACK: dict[str, str] = {
    "aws": "us-east-2", "azure": "centralus",
    "gcp": "us-central1", "oci": "us-ashburn-1",
}


def _check_cloud(cloud: str) -> str:
    c = (cloud or "").strip().lower()
    if c not in CLOUDS:
        raise ValueError(f"unknown cloud {cloud!r} (expected one of {', '.join(CLOUDS)})")
    return c


def normalize(cloud: str, region: Optional[str]) -> str:
    """Canonicalise a region string for the given cloud: lowercase + trim, and for
    Azure also strip spaces (``"West US 2"`` → ``"westus2"``)."""
    c = _check_cloud(cloud)
    r = (region or "").strip().lower()
    if c == "azure":
        r = r.replace(" ", "")
    return r


def validate(cloud: str, region: Optional[str]) -> bool:
    """True if ``region`` is a well-formed region string for ``cloud`` (after
    normalisation). Does not check membership in the catalog — the catalog is a
    convenience list, not an allow-list (operators run regions we don't enumerate)."""
    c = _check_cloud(cloud)
    return bool(_VALIDATORS[c].match(normalize(c, region)))


def validate_zone(zone: Optional[str]) -> bool:
    """True if ``zone`` is a well-formed GCP zone (``<region>-<letter>``)."""
    return bool(_GCP_ZONE_RE.match(normalize("gcp", zone)))


def default_region_key(cloud: str) -> str:
    """The config_service key holding the configured default region for ``cloud``."""
    return _DEFAULT_REGION_KEY[_check_cloud(cloud)]


def default_region(cloud: str) -> str:
    """The configured default region for ``cloud`` (config key → settings env →
    hardcoded fallback). Matches the historical per-file default resolution."""
    c = _check_cloud(cloud)
    from . import config_service
    from ..config import settings
    key = _DEFAULT_REGION_KEY[c]
    return (config_service.get(key) or getattr(settings, key, "")
            or _DEFAULT_REGION_FALLBACK[c])


def default_zone() -> str:
    """The configured default GCP zone (``gcp_zone`` → settings → ``us-central1-a``)."""
    from . import config_service
    from ..config import settings
    return config_service.get("gcp_zone") or getattr(settings, "gcp_zone", "") or "us-central1-a"


def resolve(cloud: str, region: Optional[str]) -> str:
    """Resolve the effective region for a request.

    An explicit, well-formed region wins (normalised); a blank/None region falls
    back to the configured default; a non-blank but malformed region raises
    ``ValueError`` (API callers translate that to HTTP 400). Callers that never pass
    a region are unaffected — they get the configured default, as before.
    """
    c = _check_cloud(cloud)
    if region is None or not str(region).strip():
        return default_region(c)
    r = normalize(c, region)
    if not _VALIDATORS[c].match(r):
        raise ValueError(f"invalid {c} region {region!r}")
    return r


# ── GCP zone helpers (GCP compute is zone-scoped) ─────────────────────────────

def resolve_zone(zone: Optional[str]) -> str:
    """Resolve the effective GCP zone: explicit well-formed zone wins, blank falls
    back to the configured default zone, malformed raises ``ValueError``."""
    if zone is None or not str(zone).strip():
        return default_zone()
    z = normalize("gcp", zone)
    if not _GCP_ZONE_RE.match(z):
        raise ValueError(f"invalid GCP zone {zone!r}")
    return z


def region_from_zone(zone: Optional[str]) -> str:
    """Derive the region a GCP zone belongs to (us-central1-a → us-central1). Falls
    back to the configured default region when the zone doesn't parse."""
    parts = (zone or "").rsplit("-", 1)
    if len(parts) == 2 and _VALIDATORS["gcp"].match(parts[0]):
        return parts[0]
    return default_region("gcp")


# ── Catalog accessors ─────────────────────────────────────────────────────────

def regions(cloud: str) -> list[dict]:
    """Selectable regions for ``cloud`` as ``[{"id","label"}, …]`` (display order)."""
    return [{"id": rid, "label": label} for rid, label in _CATALOG[_check_cloud(cloud)]]


def region_ids(cloud: str) -> list[str]:
    """Just the region ids for ``cloud`` (display order)."""
    return [rid for rid, _ in _CATALOG[_check_cloud(cloud)]]
