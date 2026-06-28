"""Cross-cloud cost — month-to-date (MTD) spend.

Two views:
- **Account/subscription total** (``get_cost_summary``): the whole account's MTD
  spend per cloud.
- **Dashboard-managed breakdown** (``get_cost_breakdown``): MTD spend for
  resources tagged ``managed-by=vm-dashboard`` (the canonical tag from #196),
  grouped per cloud → per service.

AWS uses Cost Explorer; Azure uses the Cost Management REST query (reusing the
existing ``httpx`` + Azure credential — no extra SDK). GCP has no simple cost API
(it needs a BigQuery billing export), so it always reports ``unavailable``.

Per-cloud failures are caught and reported as ``status="unavailable"`` so one
misconfigured cloud never sinks the result — same resilience contract as the
containers page.
"""
import asyncio
import logging
from datetime import date, timedelta

import httpx

from . import aws_service, azure_service

logger = logging.getLogger(__name__)

_AZURE_MGMT = "https://management.azure.com"

# Canonical dashboard resource tag (#194/#196) — scopes the breakdown to
# dashboard-managed resources.
_MANAGED_TAG_KEY = "managed-by"
_MANAGED_TAG_VALUE = "vm-dashboard"


def _month_range() -> tuple:
    """(first-of-month, tomorrow) as YYYY-MM-DD. AWS CE's End is exclusive, so
    tomorrow captures today's partial spend."""
    today = date.today()
    return today.replace(day=1).isoformat(), (today + timedelta(days=1)).isoformat()


async def get_aws_mtd_cost() -> tuple:
    """AWS account month-to-date UnblendedCost via Cost Explorer. Returns
    (amount, currency). Reuses ``aws_service._aws_kwargs`` for credential/region
    resolution; raises ``aws_service.AWSError`` on failure (incl. a missing
    ``ce:GetCostAndUsage`` permission)."""
    aws_service._require_boto3()
    start, end = _month_range()

    def _query() -> tuple:
        import boto3
        from botocore.exceptions import BotoCoreError, ClientError
        ce = boto3.client("ce", **aws_service._aws_kwargs(""))
        try:
            resp = ce.get_cost_and_usage(
                TimePeriod={"Start": start, "End": end},
                Granularity="MONTHLY",
                Metrics=["UnblendedCost"],
            )
        except (BotoCoreError, ClientError) as e:
            raise aws_service.AWSError(f"AWS Cost Explorer query failed: {e}") from e
        amount, currency = 0.0, "USD"
        for period in resp.get("ResultsByTime", []):
            blob = period.get("Total", {}).get("UnblendedCost", {})
            amount += float(blob.get("Amount") or 0)
            currency = blob.get("Unit") or currency
        return amount, currency

    return await asyncio.to_thread(_query)


async def get_azure_mtd_cost() -> tuple:
    """Azure subscription month-to-date ActualCost via the Cost Management REST
    query. Returns (amount, currency). Reuses ``azure_service._ensure_creds`` for
    the credential + subscription; raises ``azure_service.AzureError``."""
    cred, sub_id = await azure_service._ensure_creds()
    token = (await asyncio.to_thread(cred.get_token, f"{_AZURE_MGMT}/.default")).token
    url = (f"{_AZURE_MGMT}/subscriptions/{sub_id}/providers/"
           "Microsoft.CostManagement/query?api-version=2023-03-01")
    body = {
        "type": "ActualCost",
        "timeframe": "MonthToDate",
        "dataset": {
            "granularity": "None",
            "aggregation": {"totalCost": {"name": "Cost", "function": "Sum"}},
        },
    }
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                url, json=body, headers={"Authorization": f"Bearer {token}"})
        resp.raise_for_status()
    except httpx.HTTPError as e:
        raise azure_service.AzureError(f"Azure Cost Management query failed: {e}") from e

    props = (resp.json() or {}).get("properties", {})
    cols = [c.get("name") for c in props.get("columns", [])]
    cost_idx = cols.index("Cost") if "Cost" in cols else 0
    cur_idx = cols.index("Currency") if "Currency" in cols else None
    amount, currency = 0.0, "USD"
    for row in props.get("rows", []):
        amount += float(row[cost_idx] or 0)
        if cur_idx is not None and row[cur_idx]:
            currency = row[cur_idx]
    return amount, currency


def _breakdown_result(services: dict, currency: str) -> dict:
    """Shape a ``{service: amount}`` map into a sorted services list + total."""
    items = sorted(
        ({"service": k, "amount": round(v, 2)} for k, v in services.items()),
        key=lambda s: s["amount"], reverse=True,
    )
    return {"total": round(sum(s["amount"] for s in items), 2),
            "currency": currency, "services": items}


async def get_aws_managed_breakdown() -> dict:
    """AWS MTD spend for resources tagged ``managed-by=vm-dashboard``, grouped by
    service. Returns ``{total, currency, services:[{service, amount}]}``. Raises
    ``aws_service.AWSError`` (incl. when the ``managed-by`` cost-allocation tag
    isn't activated in Billing yet)."""
    aws_service._require_boto3()
    start, end = _month_range()

    def _query() -> dict:
        import boto3
        from botocore.exceptions import BotoCoreError, ClientError
        ce = boto3.client("ce", **aws_service._aws_kwargs(""))
        try:
            resp = ce.get_cost_and_usage(
                TimePeriod={"Start": start, "End": end},
                Granularity="MONTHLY",
                Metrics=["UnblendedCost"],
                Filter={"Tags": {"Key": _MANAGED_TAG_KEY,
                                 "Values": [_MANAGED_TAG_VALUE],
                                 "MatchOptions": ["EQUALS"]}},
                GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
            )
        except (BotoCoreError, ClientError) as e:
            raise aws_service.AWSError(
                f"AWS Cost Explorer breakdown failed: {e}. If this is a tag error, "
                f"activate the '{_MANAGED_TAG_KEY}' cost-allocation tag in the AWS "
                "Billing console (forward-only; ~24h to populate)."
            ) from e
        services, currency = {}, "USD"
        for period in resp.get("ResultsByTime", []):
            for grp in period.get("Groups", []):
                name = (grp.get("Keys") or ["(unknown)"])[0]
                blob = grp.get("Metrics", {}).get("UnblendedCost", {})
                services[name] = services.get(name, 0.0) + float(blob.get("Amount") or 0)
                currency = blob.get("Unit") or currency
        return _breakdown_result(services, currency)

    return await asyncio.to_thread(_query)


async def get_azure_managed_breakdown() -> dict:
    """Azure MTD spend for resources tagged ``managed-by=vm-dashboard``, grouped
    by service. Returns ``{total, currency, services:[...]}``. Raises
    ``azure_service.AzureError``."""
    cred, sub_id = await azure_service._ensure_creds()
    token = (await asyncio.to_thread(cred.get_token, f"{_AZURE_MGMT}/.default")).token
    url = (f"{_AZURE_MGMT}/subscriptions/{sub_id}/providers/"
           "Microsoft.CostManagement/query?api-version=2023-03-01")
    body = {
        "type": "ActualCost",
        "timeframe": "MonthToDate",
        "dataset": {
            "granularity": "None",
            "aggregation": {"totalCost": {"name": "Cost", "function": "Sum"}},
            "grouping": [{"type": "Dimension", "name": "ServiceName"}],
            "filter": {"tags": {"name": _MANAGED_TAG_KEY, "operator": "In",
                                "values": [_MANAGED_TAG_VALUE]}},
        },
    }
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                url, json=body, headers={"Authorization": f"Bearer {token}"})
        resp.raise_for_status()
    except httpx.HTTPError as e:
        raise azure_service.AzureError(f"Azure Cost Management breakdown failed: {e}") from e

    props = (resp.json() or {}).get("properties", {})
    cols = [c.get("name") for c in props.get("columns", [])]
    cost_idx = cols.index("Cost") if "Cost" in cols else 0
    svc_idx = cols.index("ServiceName") if "ServiceName" in cols else None
    cur_idx = cols.index("Currency") if "Currency" in cols else None
    services, currency = {}, "USD"
    for row in props.get("rows", []):
        name = row[svc_idx] if svc_idx is not None else "(unknown)"
        services[name] = services.get(name, 0.0) + float(row[cost_idx] or 0)
        if cur_idx is not None and row[cur_idx]:
            currency = row[cur_idx]
    return _breakdown_result(services, currency)


async def _breakdown_entry(cloud: str, fetch) -> dict:
    """One cloud's breakdown, degrading any failure to status=unavailable."""
    try:
        res = await fetch()
        return {"cloud": cloud, "status": "ok", "detail": "", **res}
    except (aws_service.AWSError, azure_service.AzureError) as e:
        return {"cloud": cloud, "status": "unavailable", "detail": str(e),
                "total": None, "currency": None, "services": []}
    except Exception as e:  # noqa: BLE001 — defensive: unknown errors are still per-cloud
        logger.warning("cost: %s breakdown failed unexpectedly: %s", cloud, e)
        return {"cloud": cloud, "status": "unavailable", "detail": str(e),
                "total": None, "currency": None, "services": []}


async def get_cost_breakdown() -> dict:
    """Per-cloud, per-service MTD spend for dashboard-managed resources
    (``managed-by=vm-dashboard``). AWS + Azure live; GCP unavailable.
    ``grand_total`` sums only the clouds that returned ``ok``."""
    aws_entry, azure_entry = await asyncio.gather(
        _breakdown_entry("aws", get_aws_managed_breakdown),
        _breakdown_entry("azure", get_azure_managed_breakdown),
    )
    clouds = [aws_entry, azure_entry, {
        "cloud": "gcp", "status": "unavailable", "total": None, "currency": None,
        "services": [],
        "detail": "GCP cost requires a BigQuery billing export (not yet supported).",
    }]
    oks = [c for c in clouds if c["status"] == "ok"]
    grand_total = round(sum(c["total"] for c in oks), 2) if oks else None
    currency = oks[0]["currency"] if oks else "USD"
    return {"clouds": clouds, "grand_total": grand_total, "currency": currency}


async def _cloud_entry(cloud: str, fetch) -> dict:
    """Run one cloud's MTD query, degrading any failure to status=unavailable so
    a single misconfigured cloud never sinks the whole summary."""
    try:
        amount, currency = await fetch()
        return {"cloud": cloud, "amount": round(amount, 2),
                "currency": currency, "status": "ok", "detail": ""}
    except (aws_service.AWSError, azure_service.AzureError) as e:
        return {"cloud": cloud, "amount": None, "currency": None,
                "status": "unavailable", "detail": str(e)}
    except Exception as e:  # noqa: BLE001 — defensive: unknown errors are still per-cloud
        logger.warning("cost: %s query failed unexpectedly: %s", cloud, e)
        return {"cloud": cloud, "amount": None, "currency": None,
                "status": "unavailable", "detail": str(e)}


async def get_cost_summary() -> dict:
    """Per-cloud account/subscription MTD spend. AWS + Azure are queried live; GCP
    is reported unavailable (needs a BigQuery billing export). ``total_mtd`` sums
    only the clouds that returned ``ok``."""
    aws_entry, azure_entry = await asyncio.gather(
        _cloud_entry("aws", get_aws_mtd_cost),
        _cloud_entry("azure", get_azure_mtd_cost),
    )
    clouds = [aws_entry, azure_entry, {
        "cloud": "gcp", "amount": None, "currency": None, "status": "unavailable",
        "detail": "GCP cost requires a BigQuery billing export (not yet supported).",
    }]
    oks = [c for c in clouds if c["status"] == "ok"]
    total = round(sum(c["amount"] for c in oks), 2) if oks else None
    currency = oks[0]["currency"] if oks else "USD"
    return {"total_mtd": total, "currency": currency, "clouds": clouds}
