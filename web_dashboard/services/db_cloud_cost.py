"""List-price lookups for managed databases: RDS, Cloud SQL, Azure flexible servers.

The VM lookups in ``pov_cloud_cost`` answer what a cell's VMs cost; this answers the same
for its databases, from **the same price lists** with the same clients, cache and
"None, never 0" rule:

* **AWS:** the Pricing API (``AmazonRDS``). The instance class per hour plus General
  Purpose storage per GB-month, Single-AZ, for the engine. Needs ``pricing:GetProducts``,
  the permission VM pricing already needs.
* **GCP:** the Cloud Billing Catalog for Cloud SQL. A shared-core tier (``db-f1-micro``,
  ``db-g1-small``) is one SKU; any other tier is vCPUs and memory, read from its name
  (``db-custom-2-7680``, ``db-n1-standard-2``).
* **Azure:** the public Retail Prices API. A Burstable flexible server is priced by its
  size; General Purpose and Memory Optimized are per vCore of their series.

**Priced engines:** PostgreSQL and MySQL. SQL Server and Oracle carry licence editions
that change the price several-fold, and Atlas and OCI Autonomous are billed by another
party: those return None, and the hub names them as not priced.

**It is a list price, not a bill**, like everything the hub shows: no reservations,
credits, backups beyond the free allowance, I/O, or data transfer. A stopped database
still bills its storage, which is why storage is returned separately.

Never raises. Every miss is None, cached for as long as a hit would be not at all: a
transient failure must not pin a missing price for six hours.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Optional

from . import pov_cloud_cost as pcc

logger = logging.getLogger(__name__)

#: The engines a price is looked up for.
PRICED_ENGINES = ("postgres", "mysql")

HOURS_PER_MONTH = pcc.HOURS_PER_MONTH


def engine_reason(engine: str) -> str:
    return (f"{engine or 'this engine'} is not priced: only PostgreSQL and MySQL have a "
            f"price lookup")


def rates(cloud: str, region: str, spec: dict) -> Optional[tuple]:
    """``(compute_usd_per_hour, storage_usd_per_hour)`` for one database, or None.

    ``spec`` is the provisioning request's shape: ``engine`` plus ``instance_class`` /
    ``allocated_storage`` (AWS), ``tier`` / ``disk_size`` (GCP), ``sku_name`` /
    ``storage_mb`` (Azure). None when the engine, the cloud or the size has no price.
    """
    engine = (spec.get("engine") or "").lower()
    if engine not in PRICED_ENGINES or not region:
        return None
    try:
        if cloud == "aws":
            return _aws(region, engine, spec)
        if cloud == "gcp":
            return _gcp(region, engine, spec)
        if cloud == "azure":
            return _azure(region, engine, spec)
    except Exception:  # noqa: BLE001 -- one unreadable price never breaks a page
        logger.info("database price: %s %s %s failed", cloud, region, engine, exc_info=True)
    return None


def _gb(value, default: float) -> float:
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return default


# ── AWS: RDS ─────────────────────────────────────────────────────────────────

_RDS_ENGINE = {"postgres": "PostgreSQL", "mysql": "MySQL"}


def _aws(region: str, engine: str, spec: dict) -> Optional[tuple]:
    location = pcc._location_for(region)
    klass = spec.get("instance_class") or "db.t3.micro"
    if not location:
        return None
    compute = _rds_price(f"rds:{region}:{engine}:{klass}", [
        pcc._f("instanceType", klass),
        pcc._f("location", location),
        pcc._f("databaseEngine", _RDS_ENGINE[engine]),
        pcc._f("deploymentOption", "Single-AZ"),
    ])
    per_gb = _rds_price(f"rds-storage:{region}:{engine}", [
        pcc._f("location", location),
        pcc._f("productFamily", "Database Storage"),
        pcc._f("volumeType", "General Purpose"),
        pcc._f("databaseEngine", _RDS_ENGINE[engine]),
        pcc._f("deploymentOption", "Single-AZ"),
    ])
    if compute is None or per_gb is None:
        return None
    storage = per_gb * _gb(spec.get("allocated_storage"), 20) / HOURS_PER_MONTH
    return compute, storage


def _rds_price(key: str, filters: list) -> Optional[float]:
    cached = pcc._cached(key)
    if cached is not None:
        return cached
    try:
        resp = pcc._client().get_products(ServiceCode="AmazonRDS", MaxResults=10,
                                          Filters=filters)
    except Exception:  # noqa: BLE001
        logger.info("database price: RDS lookup %s failed", key, exc_info=True)
        return None
    price = pcc._first_price(resp)
    if price is not None:
        pcc._prices[key] = (time.time(), price)
    return price


# ── GCP: Cloud SQL ───────────────────────────────────────────────────────────

_CLOUDSQL_SERVICE = "9662-B51E-5089"
_CLOUDSQL_ENGINE = {"postgres": "postgresql", "mysql": "mysql"}
#: Memory per vCPU for the predefined tiers, GB.
_N1_RAM = {"standard": 3.75, "highmem": 6.5}


def cloudsql_shape(tier: str) -> Optional[tuple]:
    """``("shared", "micro"|"small")`` or ``("dedicated", vcpus, ram_gb)``, or None."""
    t = (tier or "").lower()
    if t == "db-f1-micro":
        return ("shared", "micro")
    if t == "db-g1-small":
        return ("shared", "small")
    m = re.fullmatch(r"db-custom-(\d+)-(\d+)", t)
    if m:
        return ("dedicated", int(m.group(1)), int(m.group(2)) / 1024.0)
    m = re.fullmatch(r"db-n1-(standard|highmem)-(\d+)", t)
    if m:
        cpus = int(m.group(2))
        return ("dedicated", cpus, cpus * _N1_RAM[m.group(1)])
    return None


def cloudsql_index(skus, region: str) -> dict:
    """``{engine: {"vcpu", "ram", "storage", "micro", "small"}}`` from catalogue SKUs.

    Zonal (single-zone) on-demand only: a demo cell's database is not highly available,
    and committed-use prices are cheaper than what it pays.
    """
    index: dict = {}
    for sku in skus:
        category = sku.get("category") or {}
        if category.get("usageType") != "OnDemand":
            continue
        if region not in (sku.get("serviceRegions") or []):
            continue
        d = (sku.get("description") or "").lower()
        engine = next((e for e, word in _CLOUDSQL_ENGINE.items()
                       if f"cloud sql for {word}" in d), None)
        if engine is None or "zonal" not in d or "regional" in d:
            continue
        price = pcc._gcp_sku_price(sku)
        if price is None:
            continue
        slot = index.setdefault(engine, {})
        for part, needle in (("vcpu", "vcpu"), ("ram", "ram"),
                             ("storage", "standard storage"),
                             ("micro", "micro instance"), ("small", "small instance")):
            if needle in d:
                slot.setdefault(part, price)
                break
    return index


def _cloudsql_catalog(region: str) -> dict:
    key = f"cloudsql:catalog:{region}"
    cached = pcc._cached(key)
    if cached is not None:
        return cached
    try:
        from . import gcp_service
        session = gcp_service._authed_session()
    except Exception:  # noqa: BLE001
        logger.info("database price: no GCP credentials for the billing catalogue",
                    exc_info=True)
        return {}
    skus, params = [], {"currencyCode": "USD", "pageSize": pcc._GCP_PAGE_SIZE}
    url = pcc._GCP_CATALOG.format(svc=_CLOUDSQL_SERVICE)
    try:
        for _page in range(pcc._GCP_MAX_PAGES):
            resp = session.get(url, params=params, timeout=pcc._GCP_TIMEOUT_S)
            resp.raise_for_status()
            body = resp.json()
            skus.extend(body.get("skus") or [])
            token = body.get("nextPageToken")
            if not token:
                break
            params = dict(params, pageToken=token)
    except Exception:  # noqa: BLE001
        logger.info("database price: Cloud SQL catalogue read failed", exc_info=True)
    index = cloudsql_index(skus, region)
    if index:
        pcc._prices[key] = (time.time(), index)
    return index


def _gcp(region: str, engine: str, spec: dict) -> Optional[tuple]:
    shape = cloudsql_shape(spec.get("tier") or "db-f1-micro")
    prices = _cloudsql_catalog(region).get(engine) or {}
    if shape is None or "storage" not in prices:
        return None
    if shape[0] == "shared":
        compute = prices.get(shape[1])
    elif "vcpu" in prices and "ram" in prices:
        compute = shape[1] * prices["vcpu"] + shape[2] * prices["ram"]
    else:
        compute = None
    if compute is None:
        return None
    storage = prices["storage"] * _gb(spec.get("disk_size"), 20) / HOURS_PER_MONTH
    return compute, storage


# ── Azure: flexible server ───────────────────────────────────────────────────

_AZURE_SERVICE = {"postgres": "Azure Database for PostgreSQL",
                  "mysql": "Azure Database for MySQL"}


def azure_size(sku_name: str) -> Optional[tuple]:
    """``("burstable", "b1ms")`` or ``("vcore", series, vcores)`` from a flexible-server
    SKU name such as ``B_Standard_B1ms`` or ``GP_Standard_D2ds_v4``, or None."""
    parts = (sku_name or "").split("_", 2)
    if len(parts) < 3:
        return None
    tier, size = parts[0].upper(), parts[2]
    if tier == "B":
        return ("burstable", size.lower())
    m = re.fullmatch(r"([A-Za-z])(\d+)([A-Za-z]*)(?:_v(\d+))?", size)
    if not m:
        return None
    letter, cores, suffix, version = m.groups()
    series = f"{letter.upper()}{suffix.lower()}" + (f"v{version}" if version else "")
    return ("vcore", series.lower(), int(cores))


def azure_pick(items, engine: str, sku_name: str) -> tuple:
    """``(compute_per_hour, storage_per_gb_month)`` out of Retail Prices items; either
    may be None."""
    size = azure_size(sku_name)
    compute, per_gb = None, None
    for item in items:
        product = (item.get("productName") or "").lower()
        meter = (item.get("meterName") or "").lower()
        sku = (item.get("skuName") or "").lower()
        unit = (item.get("unitOfMeasure") or "").lower()
        price = float(item.get("retailPrice") or 0.0)
        if "flexible server" not in product or price <= 0:
            continue
        if "storage" in product and "data stored" in meter and "gb" in unit:
            per_gb = per_gb if per_gb is not None else price
            continue
        if size is None or "hour" not in unit or "compute" not in product:
            continue
        if size[0] == "burstable":
            if sku == size[1] or meter == size[1]:
                compute = price
        elif f" {size[1]} series" in product and "vcore" in (sku + meter):
            compute = price * size[2]
    return compute, per_gb


def _azure(region: str, engine: str, spec: dict) -> Optional[tuple]:
    sku_name = spec.get("sku_name") or "B_Standard_B1ms"
    key = f"azure:flex:{region}:{engine}:{sku_name}"
    cached = pcc._cached(key)
    if cached is None:
        items = pcc._azure_query(
            f"serviceName eq '{_AZURE_SERVICE[engine]}' and armRegionName eq '{region}' "
            f"and priceType eq 'Consumption'")
        compute, per_gb = azure_pick(items, engine, sku_name)
        if compute is None or per_gb is None:
            return None
        cached = (compute, per_gb)
        pcc._prices[key] = (time.time(), cached)
    compute, per_gb = cached
    gb = _gb(spec.get("storage_mb"), 32768) / 1024.0
    return compute, per_gb * gb / HOURS_PER_MONTH
