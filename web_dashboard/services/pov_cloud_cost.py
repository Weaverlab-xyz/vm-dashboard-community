"""What a cloud POV is costing, and the part of that the dashboard can honestly claim.

Skytap bills a lab flat, and suspending an environment stops nearly all of it. A cloud
does not: stopping an instance halts the compute charge and **nothing else**. The disks,
the public addresses and the network keep billing for the whole multi-week evaluation, on
a community user's own account. A POV instance that says nothing about this is the one
that produces the surprise invoice this module exists to prevent.

Two answers, kept apart on purpose.

**The footprint is always available and always true.** Instances, vCPU, gigabytes of EBS,
how long each environment has been up — all of it read from the same describe the VM list
comes from, so the page cannot show a footprint that disagrees with the VMs beside it. No
credentials beyond the ones the POV already uses, and no estimate.

**The money is optional, and clearly an estimate.** On-demand list price from the AWS
Pricing API, which needs a permission (`pricing:GetProducts`) that a POV's EC2-scoped key
will usually not have. When it is absent the page says so and shows the footprint alone —
which is the right failure, because the alternative is a hardcoded price table that goes
stale silently and reports a number an operator might plan around.

**It is a LIST price, not a bill.** No Savings Plans, no Reserved Instances, no free tier,
no credits, no data transfer, no snapshots. `docs/pov-instance.md` says so where an
operator reads it, because a credit cliff reading as a cost rise is a mistake this
codebase has made on a real account before.
"""
from __future__ import annotations

import json
import logging
import time

logger = logging.getLogger(__name__)

HOURS_PER_MONTH = 730

# Price lookups are memoised per process. Deliberately process-local and deliberately
# unbothered by that: under `gunicorn -w 2` each worker warms its own copy at the cost of
# one API call, and this is read-only reference data where staleness is measured in
# quarters. A shared cache table would be machinery for nothing.
_TTL_S = 6 * 3600
_prices: dict = {}

# AWS bills EBS by the GB-month, and gp3 is what `pov_cloud_aws` creates. Queried like the
# instance price rather than assumed, for the same reason.
_GP3 = "gp3"

# The Pricing API is only served from these two regions, whatever region you are asking
# about. Getting this wrong yields an endpoint error that reads like a credential problem.
_PRICING_REGION = "us-east-1"

# `location` in the Pricing API is a human region NAME, not a region id. There is an
# endpoint that maps them, and it needs its own permission — so the common ones are here
# and anything else degrades to "no price", which is a smaller lie than guessing.
_LOCATIONS = {
    "us-east-1": "US East (N. Virginia)",
    "us-east-2": "US East (Ohio)",
    "us-west-1": "US West (N. California)",
    "us-west-2": "US West (Oregon)",
    "eu-west-1": "EU (Ireland)",
    "eu-west-2": "EU (London)",
    "eu-central-1": "EU (Frankfurt)",
    "ap-southeast-1": "Asia Pacific (Singapore)",
    "ap-southeast-2": "Asia Pacific (Sydney)",
    "ap-northeast-1": "Asia Pacific (Tokyo)",
    "ap-south-1": "Asia Pacific (Mumbai)",
    "ca-central-1": "Canada (Central)",
    "sa-east-1": "South America (Sao Paulo)",
}


# The clouds a price can be looked up for. **Built, not planned** — the same discipline
# `lab_platforms.CLOUD_PLATFORMS` follows, and for the same reason: a cloud listed here
# without a working lookup would offer a spend cap that silently never accrues, which is
# worse than no cap at all.
#
# Each cloud needs its own price client and they are not alike: AWS's Pricing API needs an
# IAM permission, Azure's Retail Prices API is public but bills managed disks by SIZE TIER
# rather than per GB, GCP's Cloud Billing Catalog needs its own API enabled, and OCI
# publishes a price list with no auth at all. One at a time.
PRICED_CLOUDS = ("aws", "azure", "gcp")


def priced(cloud: str) -> bool:
    """Whether a spend estimate — and therefore a spend cap — is possible on ``cloud``."""
    return (cloud or "").strip().lower() in PRICED_CLOUDS


def no_price_reason(cloud: str) -> str:
    """Why there is no estimate, in words an operator can act on."""
    label = (cloud or "this platform").strip() or "this platform"
    if label == "skytap":
        return ("Skytap bills the lab, not the VM, so the dashboard has no per-POV figure "
                "to accrue. Its own idle timer is the lever there.")
    return (f"No price source is wired up for {label} yet, so a spend estimate would be "
            f"zero and a cap would never act. AWS is supported today.")


def hourly_for_vm(cloud: str, region: str, vm: dict) -> float:
    """What one VM costs per hour: its compute if running, plus its disk always.

    Missing prices contribute nothing rather than raising. The caller decides whether the
    whole environment is unpriceable; one shape the catalogue has never heard of should
    not blank the other four VMs.
    """
    hourly = 0.0
    if (vm.get("runstate") or "") == "running":
        hourly += _compute_hourly(cloud, region, vm) or 0.0
    hourly += _storage_hourly(cloud, region, vm) or 0.0
    return hourly


def _priceable(cloud: str, region: str) -> bool:
    """Whether a price lookup can even be attempted for this cloud and region.

    Separate from ``priced``: that answers "is there a client at all", this answers "will
    it have anything to say about here". AWS's Pricing API needs a region NAME this module
    holds a map of; Azure's takes the region id directly, so every region is in scope.
    """
    if not priced(cloud):
        return False
    if cloud == "aws":
        return region in _LOCATIONS
    # Azure and GCP both key on the region id itself, so every region is in scope and a
    # cap works the day a new one opens.
    return bool(region)


def _rate_sync(environment: dict, region: str, cloud: str):
    hourly = 0.0
    priced_any = False
    for vm in environment.get("vms") or []:
        value = hourly_for_vm(cloud, region, vm)
        if value:
            priced_any = True
        hourly += value
    # An environment with VMs but no price for any of them is UNKNOWN, not free. Reporting
    # 0.0 there would let a cap sit at zero for the length of an evaluation and never act,
    # which is the failure `rate_usd_per_hour` returns None to avoid.
    if (environment.get("vms") or []) and not priced_any:
        return None
    return round(hourly, 6)


async def rate_usd_per_hour(environment: dict, region: str, cloud: str = "aws"):
    """What this environment costs per hour, right now, at list price. None if unknown.

    The accrual rate behind the spend cap — see ``pov_spend``. Compute counts only for VMs
    that are RUNNING; storage counts for all of them, because a disk bills whether its
    instance is up or not. That second half is the one that matters: without it a POV left
    suspended for a month would accrue nothing and its cap would never trip, which would
    contradict everything else this feature says about suspend not stopping the bill.

    **Async, and the lookups run on their cloud's own executor.** They are memoised for six
    hours, so this is a dict read on all but the first pass after a restart — but that
    first pass makes a real HTTP call per distinct shape, and doing it inline would block
    the worker's event loop and every other job queued behind it.
    """
    if not _priceable(cloud, region):
        return None
    from . import cloud_executor
    try:
        return await cloud_executor.run(cloud, _rate_sync, environment, region, cloud)
    except Exception:  # noqa: BLE001
        logger.info("POV cost: no rate for %s in %s", cloud, region, exc_info=True)
        return None


def footprint(environments: list) -> dict:
    """What is running, in units nobody has to trust an estimate for.

    ``environments`` are the adapter's environment dicts. Counts every VM, running or
    not, and separates the two — because "3 of 5 running" is the number that explains why
    a suspended POV still costs something.
    """
    vms = [vm for e in environments for vm in (e.get("vms") or [])]
    running = [v for v in vms if (v.get("runstate") or "") == "running"]
    return {
        "environments": len(environments),
        "vms": len(vms),
        "vms_running": len(running),
        "disk_gb": sum(int(v.get("disk_gb") or 0) for v in vms),
        # Instance shapes, so an operator can see at a glance that somebody's template
        # names an m5.4xlarge.
        "instance_types": sorted({(v.get("instance_type") or "") for v in vms} - {""}),
    }


def _client():
    from . import aws_service
    aws_service._require_boto3()
    import boto3
    return boto3.client("pricing", **aws_service._aws_kwargs(_PRICING_REGION))


def _cached(key: str):
    hit = _prices.get(key)
    if hit and (time.time() - hit[0]) < _TTL_S:
        return hit[1]
    return None


def _first_price(resp) -> float | None:
    """The on-demand USD/unit out of a Pricing API response.

    The payload is JSON-inside-JSON with two layers of opaque keys, so this walks rather
    than indexes. Returns None rather than raising on any shape it does not recognise: an
    unreadable price and an absent one lead to the same honest page.
    """
    for raw in resp.get("PriceList") or []:
        try:
            doc = json.loads(raw) if isinstance(raw, str) else raw
            for term in (doc.get("terms", {}).get("OnDemand") or {}).values():
                for dim in (term.get("priceDimensions") or {}).values():
                    usd = (dim.get("pricePerUnit") or {}).get("USD")
                    if usd is not None and float(usd) > 0:
                        return float(usd)
        except Exception:  # noqa: BLE001
            continue
    return None


def _f(field: str, value: str) -> dict:
    return {"Type": "TERM_MATCH", "Field": field, "Value": value}


def instance_hourly(region: str, instance_type: str, os_family: str = "linux"):
    """On-demand USD/hour for one instance shape, or None."""
    location = _LOCATIONS.get(region)
    if not location or not instance_type:
        return None
    key = f"ec2:{region}:{instance_type}:{os_family}"
    cached = _cached(key)
    if cached is not None:
        return cached
    try:
        resp = _client().get_products(ServiceCode="AmazonEC2", MaxResults=10, Filters=[
            _f("instanceType", instance_type),
            _f("location", location),
            _f("operatingSystem", "Windows" if os_family == "windows" else "Linux"),
            _f("tenancy", "Shared"),
            _f("capacitystatus", "Used"),
            _f("preInstalledSw", "NA"),
            # Without this a Windows lookup matches the BYOL SKU too, and the cheaper one
            # wins by accident.
            _f("licenseModel", "Bring your own license" if os_family == "windows"
               else "No License required"),
        ])
    except Exception:  # noqa: BLE001
        logger.info("POV cost: no EC2 price for %s in %s", instance_type, region,
                    exc_info=True)
        return None
    price = _first_price(resp)
    if price is not None:
        _prices[key] = (time.time(), price)
    return price


def storage_gb_month(region: str):
    """On-demand USD per GB-month of gp3, or None."""
    location = _LOCATIONS.get(region)
    if not location:
        return None
    key = f"ebs:{region}"
    cached = _cached(key)
    if cached is not None:
        return cached
    try:
        resp = _client().get_products(ServiceCode="AmazonEC2", MaxResults=10, Filters=[
            _f("location", location),
            _f("productFamily", "Storage"),
            _f("volumeApiName", _GP3),
        ])
    except Exception:  # noqa: BLE001
        logger.info("POV cost: no EBS price in %s", region, exc_info=True)
        return None
    price = _first_price(resp)
    if price is not None:
        _prices[key] = (time.time(), price)
    return price


# ── Azure: the Retail Prices API ─────────────────────────────────────────────
#
# **Public and unauthenticated**, which is the whole reason Azure is the second cloud to
# get a price source rather than the last. AWS's Pricing API needs an IAM permission an
# EC2-scoped key does not have; this one needs nothing but egress, so a spend cap works on
# Azure the moment the provider is selected.
#
# It has one wrinkle AWS does not, and it is the reason storage is modelled per DISK here
# rather than per GB: **Azure bills a managed disk by SIZE TIER.** A 30 GB and a 32 GB
# Standard SSD are both an E4 and cost exactly the same; a 33 GB one is an E6 and costs
# roughly double. Multiplying a per-GB figure would be wrong in both directions.

_AZURE_PRICES_URL = "https://prices.azure.com/api/retail/prices"
_AZURE_TIMEOUT_S = 15.0

# Standard SSD (E-series) tiers, as (largest GiB the tier holds, sku name). The BOUNDARIES
# are product structure and change about never; the PRICES still come from the API. A disk
# larger than the last tier has no answer rather than a guessed one.
#
# `pov_cloud_azure` creates StandardSSD_LRS, so this is the only family that needs mapping.
_AZURE_SSD_TIERS = (
    (4, "E1 LRS"), (8, "E2 LRS"), (16, "E3 LRS"), (32, "E4 LRS"), (64, "E6 LRS"),
    (128, "E10 LRS"), (256, "E15 LRS"), (512, "E20 LRS"), (1024, "E30 LRS"),
    (2048, "E40 LRS"), (4096, "E50 LRS"), (8192, "E60 LRS"), (16384, "E70 LRS"),
    (32767, "E80 LRS"),
)


def azure_disk_sku(disk_gb: int) -> str:
    """The Standard SSD tier a disk of this size lands in, or "".

    Rounded UP, because that is how Azure charges: a 33 GB disk is billed as a 64 GiB E6.
    Zero and negative sizes have no tier, and neither does anything past the largest.
    """
    size = int(disk_gb or 0)
    if size <= 0:
        return ""
    for ceiling, sku in _AZURE_SSD_TIERS:
        if size <= ceiling:
            return sku
    return ""


def _azure_query(filter_expr: str) -> list:
    """One page of the Retail Prices API, or []. Never raises.

    One page is enough by construction: every filter here names a single SKU in a single
    region. Paginating would only ever fetch reservation terms this does not read.
    """
    try:
        import httpx
    except ImportError:  # pragma: no cover - httpx is a hard dependency elsewhere
        return []
    try:
        resp = httpx.get(_AZURE_PRICES_URL, timeout=_AZURE_TIMEOUT_S,
                         params={"currencyCode": "USD", "$filter": filter_expr})
        resp.raise_for_status()
        return resp.json().get("Items") or []
    except Exception:  # noqa: BLE001
        logger.info("POV cost: Azure retail price query failed", exc_info=True)
        return []


def _azure_instance_hourly(region: str, instance_type: str, os_family: str):
    """On-demand USD/hour for one Azure VM size, or None.

    Three things are filtered out in Python rather than in the OData query, because the
    API has no field for any of them:

      * **Spot and Low Priority**, which are cheaper and would understate a cap;
      * the **wrong OS** — a Windows SKU carries the licence and costs more, and the two
        differ only by a substring of `productName`;
      * anything not billed by the hour.
    """
    if not instance_type:
        return None
    key = f"azure:vm:{region}:{instance_type}:{os_family}"
    cached = _cached(key)
    if cached is not None:
        return cached
    items = _azure_query(
        f"serviceName eq 'Virtual Machines' and armRegionName eq '{region}' "
        f"and armSkuName eq '{instance_type}' and priceType eq 'Consumption'")
    want_windows = os_family == "windows"
    best = None
    for item in items:
        meter = (item.get("meterName") or "").lower()
        product = (item.get("productName") or "").lower()
        if "spot" in meter or "low priority" in meter:
            continue
        if ("windows" in product) != want_windows:
            continue
        if "hour" not in (item.get("unitOfMeasure") or "").lower():
            continue
        price = float(item.get("retailPrice") or 0.0)
        if price > 0 and (best is None or price < best):
            best = price
    if best is not None:
        _prices[key] = (time.time(), best)
    return best


def _azure_disk_monthly(region: str, disk_gb: int):
    """USD/month for one Standard SSD managed disk of this size, or None."""
    sku = azure_disk_sku(disk_gb)
    if not sku:
        return None
    key = f"azure:disk:{region}:{sku}"
    cached = _cached(key)
    if cached is not None:
        return cached
    items = _azure_query(
        f"serviceName eq 'Storage' and armRegionName eq '{region}' "
        f"and skuName eq '{sku}' and priceType eq 'Consumption'")
    for item in items:
        product = (item.get("productName") or "").lower()
        if "standard ssd" not in product or "managed disk" not in product:
            continue
        price = float(item.get("retailPrice") or 0.0)
        if price > 0:
            _prices[key] = (time.time(), price)
            return price
    return None

# ── GCP: the Cloud Billing Catalog ───────────────────────────────────────────
#
# The most involved of the four, because **GCE does not price a machine type.** It prices
# vCPU and memory separately, so `n2-standard-4` is four units of "N2 Instance Core" plus
# sixteen of "N2 Instance Ram". Two lookups are needed to answer one question:
#
#   1. the shape's vCPU and memory, from the Compute API — the catalogue does not know
#      what `n2-standard-4` contains;
#   2. the per-core and per-GB rates for that family and region, from the catalogue.
#
# **The catalogue cannot be filtered server-side.** There is no query for "N2 cores in
# us-central1" — the only call is "list every Compute Engine SKU", a few thousand of them
# over two or three pages. So it is fetched once, reduced immediately to a small index of
# the entries this needs, and memoised for six hours like every other price here. The full
# response is never kept.
#
# It needs `cloudbilling.googleapis.com` enabled on the project. Unlike AWS's Pricing API
# it needs no special IAM role — the catalogue is a public price list — but an unenabled
# API answers 403, so the reason says which one to turn on.

_GCP_CATALOG = "https://cloudbilling.googleapis.com/v1/services/{svc}/skus"
# Compute Engine's service id. A constant of the API, not of this project.
_GCP_COMPUTE_SERVICE = "6F81-5844-456A"
_GCP_TIMEOUT_S = 30.0
_GCP_PAGE_SIZE = 5000
# A guard, not a tuning knob: the catalogue is ~2-3 pages at that size, and a paging bug
# on either side must not turn one price lookup into an unbounded download.
_GCP_MAX_PAGES = 8

# `pov_cloud_gcp` creates pd-balanced disks. The catalogue calls that "Balanced PD
# Capacity"; matching on the description is the only way, since the resource group is the
# generic "SSD".
_GCP_DISK_DESCRIPTION = "balanced pd capacity"


def _gcp_sku_price(sku: dict):
    """The on-demand unit price out of one catalogue SKU, or None.

    A price is `units` + `nanos`, both optional, and a promotional tier can be zero. The
    first tier with a non-zero price is the on-demand rate.
    """
    for info in sku.get("pricingInfo") or []:
        for tier in ((info.get("pricingExpression") or {})
                     .get("tieredRates") or []):
            price = tier.get("unitPrice") or {}
            value = float(price.get("units") or 0) + float(price.get("nanos") or 0) / 1e9
            if value > 0:
                return value
    return None


def _gcp_catalog(region: str) -> dict:
    """A small index of the Compute Engine SKUs this module needs, for one region.

    ``{"cpu": {"N2": usd_per_core_hour, ...}, "ram": {...}, "disk": usd_per_gb_month}``

    Reduced as it is read rather than after: the full catalogue is megabytes of JSON and
    all but a few dozen entries are irrelevant here.
    """
    key = f"gcp:catalog:{region}"
    cached = _cached(key)
    if cached is not None:
        return cached

    index = {"cpu": {}, "ram": {}, "disk": None}
    try:
        from . import gcp_service
        session = gcp_service._authed_session()
    except Exception:  # noqa: BLE001
        logger.info("POV cost: no GCP credentials for the billing catalogue",
                    exc_info=True)
        return index

    url = _GCP_CATALOG.format(svc=_GCP_COMPUTE_SERVICE)
    params = {"currencyCode": "USD", "pageSize": _GCP_PAGE_SIZE}
    try:
        for _page in range(_GCP_MAX_PAGES):
            resp = session.get(url, params=params, timeout=_GCP_TIMEOUT_S)
            resp.raise_for_status()
            body = resp.json()
            for sku in body.get("skus") or []:
                _gcp_index_sku(index, sku, region)
            token = body.get("nextPageToken")
            if not token:
                break
            params = dict(params, pageToken=token)
    except Exception:  # noqa: BLE001
        logger.info("POV cost: GCP billing catalogue read failed", exc_info=True)
        # A partial index is still worth caching only if it found something; an empty one
        # would pin the failure for six hours.
        if not index["cpu"] and index["disk"] is None:
            return index

    _prices[key] = (time.time(), index)
    return index


def _gcp_index_sku(index: dict, sku: dict, region: str) -> None:
    """Fold one catalogue SKU into the index, if it is one this module can use."""
    category = sku.get("category") or {}
    if category.get("usageType") != "OnDemand":
        # Preemptible and Spot are cheaper and would understate a cap — the one direction
        # an estimate behind a cap must not err. Commit1Yr/3Yr likewise are not what an
        # un-discounted POV pays.
        return
    if region not in (sku.get("serviceRegions") or []):
        return
    price = _gcp_sku_price(sku)
    if price is None:
        return

    description = (sku.get("description") or "")
    lowered = description.lower()
    group = category.get("resourceGroup") or ""

    if group == "CPU" and " instance core running" in lowered:
        # The family lives ONLY in the description — "N2 Instance Core running in
        # Americas". Split on the space so "N2D" can never be read as "N2".
        family = description.split(" ", 1)[0].upper()
        index["cpu"].setdefault(family, price)
    elif group == "RAM" and " instance ram running" in lowered:
        family = description.split(" ", 1)[0].upper()
        index["ram"].setdefault(family, price)
    elif index["disk"] is None and _GCP_DISK_DESCRIPTION in lowered:
        index["disk"] = price


def _gcp_machine_spec(region: str, machine_type: str):
    """``(vcpus, memory_gb)`` for a machine type, or None.

    The catalogue prices cores and memory, not shapes, so this is the half that says how
    many of each. Asked of the Compute API rather than parsed out of the name: `e2-medium`
    has two vCPUs and 4 GB and says neither, and a custom type says both but in a third
    format again.
    """
    key = f"gcp:shape:{region}:{machine_type}"
    cached = _cached(key)
    if cached is not None:
        return cached
    try:
        from google.cloud import compute_v1
        from . import gcp_service, pov_cloud_gcp
        project = pov_cloud_gcp.configured_project_id()
        if not project:
            return None
        zone = pov_cloud_gcp._zone_sync(project, region)
        found = compute_v1.MachineTypesClient(
            credentials=gcp_service._gcp_creds()).get(
                project=project, zone=zone, machine_type=machine_type)
    except Exception:  # noqa: BLE001
        logger.info("POV cost: no GCP machine spec for %s in %s", machine_type, region,
                    exc_info=True)
        return None
    spec = (int(found.guest_cpus or 0), float(found.memory_mb or 0) / 1024.0)
    if spec[0] <= 0:
        return None
    _prices[key] = (time.time(), spec)
    return spec


def _gcp_instance_hourly(region: str, machine_type: str, os_family: str):
    """On-demand USD/hour for one GCE machine type, or None.

    ``os_family`` is accepted and ignored: unlike AWS and Azure, GCE prices the machine
    and licences a premium OS image SEPARATELY, so a Windows VM's licence is a different
    SKU keyed on the image rather than on the shape. Counting it would need the image's
    own SKU; omitting it means a Windows POV is under-estimated by its licence, which is
    stated on the page rather than hidden here.
    """
    if not machine_type:
        return None
    family = machine_type.split("-", 1)[0].upper()
    if not family or family == "CUSTOM":
        return None
    spec = _gcp_machine_spec(region, machine_type)
    if spec is None:
        return None
    catalog = _gcp_catalog(region)
    cpu_rate = catalog["cpu"].get(family)
    ram_rate = catalog["ram"].get(family)
    if cpu_rate is None or ram_rate is None:
        return None
    vcpus, memory_gb = spec
    return vcpus * cpu_rate + memory_gb * ram_rate


def _gcp_disk_gb_month(region: str):
    """USD per GB-month of pd-balanced, or None."""
    return _gcp_catalog(region)["disk"]

def _estimate_sync(environments: list, region: str, cloud: str) -> dict:
    out = {"available": False, "reason": "", "currency": "USD",
           "running_hourly": 0.0, "monthly_if_left": 0.0, "storage_monthly": 0.0,
           "priced_vms": 0, "unpriced_vms": 0}

    compute_hourly, storage_hourly = 0.0, 0.0
    for e in environments:
        for vm in e.get("vms") or []:
            # Split rather than taken from `hourly_for_vm`, because the two answer
            # different questions on the page: "what is it costing right now" excludes the
            # disks of a suspended VM, and "what will a month cost" includes them.
            running = (vm.get("runstate") or "") == "running"
            compute = _compute_hourly(cloud, region, vm) if running else None
            storage = _storage_hourly(cloud, region, vm)
            if compute is None and storage is None:
                out["unpriced_vms"] += 1
                continue
            out["priced_vms"] += 1
            compute_hourly += compute or 0.0
            storage_hourly += storage or 0.0

    if not out["priced_vms"] and (out["unpriced_vms"] or environments):
        out["reason"] = _no_answer_reason(cloud, region)
        return out

    out["available"] = True
    out["running_hourly"] = round(compute_hourly, 4)
    out["storage_monthly"] = round(storage_hourly * HOURS_PER_MONTH, 2)
    out["monthly_if_left"] = round(
        (compute_hourly + storage_hourly) * HOURS_PER_MONTH, 2)
    if out["unpriced_vms"]:
        out["reason"] = (f"{out['unpriced_vms']} VM(s) had no published price and are "
                         f"not counted.")
    return out


def _compute_hourly(cloud: str, region: str, vm: dict):
    itype = vm.get("instance_type") or ""
    os_family = vm.get("os_family") or "linux"
    if cloud == "azure":
        return _azure_instance_hourly(region, itype, os_family)
    if cloud == "gcp":
        return _gcp_instance_hourly(region, itype, os_family)
    return instance_hourly(region, itype, os_family)


def _storage_hourly(cloud: str, region: str, vm: dict):
    """One VM's disk cost per hour, or None. Per-cloud because the MODELS differ.

    AWS bills EBS by the GB-month, so this is a multiplication. Azure bills a managed disk
    by size tier — a 30 GB and a 32 GB Standard SSD cost the same, a 33 GB one costs
    roughly double — so it is a lookup of the tier the disk rounds up into.
    """
    disk_gb = int(vm.get("disk_gb") or 0)
    if disk_gb <= 0:
        return 0.0
    if cloud == "azure":
        # `disk_gb` is the SUM of a VM's disks, and tiering a sum is only exact when there
        # is one — which is what `pov_cloud_azure` creates. A VM given data disks by hand
        # would be tiered as though its total were a single disk, which understates.
        monthly = _azure_disk_monthly(region, disk_gb)
        return None if monthly is None else monthly / HOURS_PER_MONTH
    if cloud == "gcp":
        # Per GB-month, like AWS and unlike Azure — GCE charges pd-balanced by capacity
        # rather than by a size tier.
        gb_month = _gcp_disk_gb_month(region)
        return None if gb_month is None else disk_gb * gb_month / HOURS_PER_MONTH
    gb_month = storage_gb_month(region)
    return None if gb_month is None else disk_gb * gb_month / HOURS_PER_MONTH


def _no_answer_reason(cloud: str, region: str) -> str:
    if cloud == "aws":
        return ("AWS did not answer a price lookup. The estimate needs the "
                "`pricing:GetProducts` permission, which an EC2-scoped key does not have "
                "— add it to see costs here, or read them in the account's own billing "
                "console.")
    if cloud == "azure":
        return (f"Azure published no retail price for these VM sizes in {region}. The "
                f"lookup needs no credentials, so this usually means the size or region "
                f"name is not one the catalogue carries.")
    if cloud == "gcp":
        return ("GCP's billing catalogue returned nothing usable. It needs "
                "`cloudbilling.googleapis.com` enabled on the project — the catalogue "
                "itself is a public price list and needs no extra IAM role.")
    return no_price_reason(cloud)


async def estimate(environments: list, region: str, cloud: str = "aws") -> dict:
    """A list-price estimate for what these environments cost, or a reason there is none.

    Two figures, because they answer different questions:

      ``running_hourly``  - what it costs right now, this hour
      ``monthly_if_left`` - compute at the CURRENT power state for a month, plus storage,
                            which bills whether anything is running or not

    The second is the one worth reading. A POV suspended overnight is not free, and an
    operator asking "can I leave this up for the evaluation?" is asking exactly this.

    Async for the reason ``rate_usd_per_hour`` is: the lookups are memoised, but the first
    call after a restart is real HTTP and belongs on the cloud's own executor rather than
    on the loop serving the page.
    """
    if not priced(cloud):
        return {"available": False, "reason": no_price_reason(cloud), "currency": "USD",
                "running_hourly": 0.0, "monthly_if_left": 0.0, "storage_monthly": 0.0,
                "priced_vms": 0, "unpriced_vms": 0}
    if not _priceable(cloud, region):
        return {"available": False, "currency": "USD",
                "reason": (f"No list prices are published here for {region}, so only the "
                           f"footprint is shown."),
                "running_hourly": 0.0, "monthly_if_left": 0.0, "storage_monthly": 0.0,
                "priced_vms": 0, "unpriced_vms": 0}
    from . import cloud_executor
    return await cloud_executor.run(cloud, _estimate_sync, environments, region, cloud)
