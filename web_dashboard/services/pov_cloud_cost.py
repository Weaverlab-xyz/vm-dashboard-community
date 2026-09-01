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
PRICED_CLOUDS = ("aws",)


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


def rate_usd_per_hour(environment: dict, region: str, cloud: str = "aws"):
    """What this environment costs per hour, right now, at list price. None if unknown.

    The accrual rate behind the spend cap — see ``pov_spend``. Compute counts only for VMs
    that are RUNNING; storage counts for all of them, because a disk bills whether its
    instance is up or not. That second half is the one that matters: without it a POV left
    suspended for a month would accrue nothing and its cap would never trip, which would
    contradict everything else this feature says about suspend not stopping the bill.

    None rather than 0.0 when there is no price source. Zero is a real answer — an
    environment with nothing in it — and a cap that read "unknown" as "free" would never
    act on the clouds it cannot price.
    """
    if not priced(cloud) or region not in _LOCATIONS:
        return None
    gb_month = storage_gb_month(region)
    if gb_month is None:
        return None

    hourly = 0.0
    for vm in environment.get("vms") or []:
        if (vm.get("runstate") or "") == "running":
            rate = instance_hourly(region, vm.get("instance_type") or "",
                                   vm.get("os_family") or "linux")
            if rate is not None:
                hourly += rate
        hourly += int(vm.get("disk_gb") or 0) * gb_month / HOURS_PER_MONTH
    return round(hourly, 6)


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


def estimate(environments: list, region: str,
             cloud: str = "aws") -> dict:
    """A list-price estimate for what these environments cost, or a reason there is none.

    Two figures, because they answer different questions:

      ``running_hourly``  - what it costs right now, this hour
      ``monthly_if_left`` - compute at the CURRENT power state for a month, plus storage,
                            which bills whether anything is running or not

    The second is the one worth reading. A POV suspended overnight is not free, and an
    operator asking "can I leave this up for the evaluation?" is asking exactly this.
    """
    out = {"available": False, "reason": "", "currency": "USD",
           "running_hourly": 0.0, "monthly_if_left": 0.0, "storage_monthly": 0.0,
           "priced_vms": 0, "unpriced_vms": 0}
    if not priced(cloud):
        out["reason"] = no_price_reason(cloud)
        return out
    if region not in _LOCATIONS:
        out["reason"] = (f"No list prices are published here for {region}, so only the "
                         f"footprint is shown.")
        return out

    gb_month = storage_gb_month(region)
    if gb_month is None:
        out["reason"] = ("AWS did not answer a price lookup. The estimate needs the "
                         "`pricing:GetProducts` permission, which an EC2-scoped key does "
                         "not have — add it to see costs here, or read them in the "
                         "account's own billing console.")
        return out

    hourly = 0.0
    for e in environments:
        for vm in e.get("vms") or []:
            rate = instance_hourly(region, vm.get("instance_type") or "",
                                   vm.get("os_family") or "linux")
            if rate is None:
                out["unpriced_vms"] += 1
                continue
            out["priced_vms"] += 1
            if (vm.get("runstate") or "") == "running":
                hourly += rate

    disk_gb = footprint(environments)["disk_gb"]
    out["available"] = True
    out["running_hourly"] = round(hourly, 4)
    out["storage_monthly"] = round(disk_gb * gb_month, 2)
    out["monthly_if_left"] = round(hourly * HOURS_PER_MONTH + disk_gb * gb_month, 2)
    if out["unpriced_vms"]:
        out["reason"] = (f"{out['unpriced_vms']} VM(s) had no published price and are "
                         f"not counted.")
    return out
