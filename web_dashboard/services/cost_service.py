"""Cross-cloud cost — month-to-date (MTD) spend.

Two views:
- **Account/subscription total** (``get_cost_summary``): the whole account's MTD
  spend per cloud.
- **Dashboard-managed breakdown** (``get_cost_breakdown``): MTD spend for
  resources tagged ``managed-by=vm-dashboard`` (the canonical tag from #196),
  grouped per cloud → per service.

AWS uses Cost Explorer; Azure uses the Cost Management REST query (reusing the
existing ``httpx`` + Azure credential — no extra SDK). GCP has no simple cost API,
so it queries the **BigQuery billing export** (when an export table is configured;
otherwise it reports ``unavailable`` with a configure hint).

Per-cloud failures are caught and reported as ``status="unavailable"`` so one
misconfigured cloud never sinks the result — same resilience contract as the
containers page.
"""
import asyncio
import calendar
import logging
from datetime import date, timedelta

import httpx

from . import aws_service, azure_service, config_service, gcp_service, oci_service

logger = logging.getLogger(__name__)

_AZURE_MGMT = "https://management.azure.com"

# Canonical dashboard resource tag (#194/#196) and the sandbox bootstrapper's tag
# (scripts/sandbox/Linux/lib/common.sh:11-13). SAME KEY, different value — which is
# what lets one query per cloud return both scopes. AWS/Azure activate cost-allocation
# tags by KEY, so covering the second value needs no extra tag activation.
_MANAGED_TAG_KEY = "managed-by"
_MANAGED_TAG_VALUE = "vm-dashboard"
_SANDBOX_TAG_VALUE = "dashboard-sandbox"

SCOPE_DASHBOARD = "dashboard"
SCOPE_SANDBOX = "sandbox"
SCOPE_UNATTRIBUTED = "unattributed"
_SCOPES = (SCOPE_DASHBOARD, SCOPE_SANDBOX, SCOPE_UNATTRIBUTED)

# Tag-filter values for AWS CE / Azure Cost Management. Dashboard first (the order
# shows up in the request).
_SCOPE_TAG_VALUES = [_MANAGED_TAG_VALUE, _SANDBOX_TAG_VALUE]

# GCP label variants. setup-gcp.sh used to rewrite BOTH key and value to underscores
# while the dashboard's own labels stayed hyphenated (gcp_service.py:745). The script
# now emits hyphens, but these variants are PERMANENT, not a migration shim: BigQuery
# billing rows are immutable history, sandboxes stay underscored until someone re-runs
# setup, and the month the fix landed legitimately contains both forms.
_GCP_LABEL_KEYS = (_MANAGED_TAG_KEY, _MANAGED_TAG_KEY.replace("-", "_"))
_SCOPE_BY_TAG_VALUE = {
    _MANAGED_TAG_VALUE: SCOPE_DASHBOARD,
    _MANAGED_TAG_VALUE.replace("-", "_"): SCOPE_DASHBOARD,
    _SANDBOX_TAG_VALUE: SCOPE_SANDBOX,
    _SANDBOX_TAG_VALUE.replace("-", "_"): SCOPE_SANDBOX,
}

# Cache key for the breakdown payload. BUMP THIS whenever the shape changes — the 6h
# TTL would otherwise serve an old shape to a template expecting the new one. Both
# api/costs.py and main.py's warmer read it from here so the two can't drift.
CACHE_KEY_BREAKDOWN = "cost_breakdown_v2"


def _scope_of(tag_value) -> str:
    """Map a ``managed-by`` tag/label value to a scope. Anything unrecognised (or
    absent) is ``unattributed`` — never silently folded into a real scope."""
    return _SCOPE_BY_TAG_VALUE.get((tag_value or "").strip().lower(), SCOPE_UNATTRIBUTED)


def evaluate_budget(total_mtd, currency, limit, today=None) -> dict:
    """Compare month-to-date spend against a monthly budget.

    Returns ``None`` when there's no budget (``limit`` <= 0) or no spend figure.
    Otherwise ``{limit, currency, mtd, projected, pct_of_budget, status}`` where
    ``projected`` is the month-end estimate from the current pace
    (MTD / day-of-month * days-in-month) and ``status`` is:
      - ``"over"``        — MTD already at/over budget,
      - ``"approaching"`` — on pace to exceed by month-end (projected >= limit),
      - ``"ok"``          — otherwise.
    ``today`` is injectable for deterministic tests."""
    if not limit or limit <= 0 or total_mtd is None:
        return None
    today = today or date.today()
    days_in_month = calendar.monthrange(today.year, today.month)[1]
    day = today.day or 1
    projected = round(total_mtd / day * days_in_month, 2)
    pct = round(total_mtd / limit * 100, 1)
    if total_mtd >= limit:
        status = "over"
    elif projected >= limit:
        status = "approaching"
    else:
        status = "ok"
    return {
        "limit": round(float(limit), 2),
        "currency": currency or "USD",
        "mtd": round(total_mtd, 2),
        "projected": projected,
        "pct_of_budget": pct,
        "status": status,
    }


_BUDGET_KEYS = {"aws": "cost_budget_aws", "azure": "cost_budget_azure",
                "gcp": "cost_budget_gcp", "oci": "cost_budget_oci"}


def _budget_limit(key: str) -> float:
    """A configured budget value as a float (0 when unset/blank/invalid)."""
    from ..config import settings
    if not key:
        return 0.0
    try:
        return float(config_service.get(key) or getattr(settings, key, 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def apply_budget_alerts(summary: dict) -> dict:
    """Return a copy of a cost summary with budget evaluations attached: a
    top-level ``budget`` (total vs ``cost_monthly_budget``) and a per-cloud
    ``budget`` on each ``clouds[]`` entry (its amount vs ``cost_budget_<cloud>``).
    Date/config-dependent — call per request, not inside the cached summary."""
    clouds = [
        {**c, "budget": evaluate_budget(
            c.get("amount"), c.get("currency"),
            _budget_limit(_BUDGET_KEYS.get(c.get("cloud"), "")))}
        for c in summary.get("clouds", [])
    ]
    overall = evaluate_budget(summary.get("total_mtd"), summary.get("currency"),
                              _budget_limit("cost_monthly_budget"))
    return {**summary, "clouds": clouds, "budget": overall}


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


def _retry_after_seconds(header_val, *, default: int, cap: int = 30) -> int:
    """Parse a Cost Management ``Retry-After`` header (seconds; HTTP-date form is
    not used by the API). Falls back to ``default``, capped so a retry never
    stretches a request past a reasonable ceiling."""
    if not header_val:
        return default
    try:
        return max(1, min(int(header_val), cap))
    except (TypeError, ValueError):
        return default


async def _azure_cost_query(sub_id: str, token: str, body: dict, *, label: str) -> dict:
    """POST an Azure Cost Management query and return the parsed JSON body.

    Cost Management is aggressively rate-limited per subscription (429 with a
    ``Retry-After`` header) — retry once honoring the header so a single throttled
    response doesn't poison the tile for the whole 6 h cache TTL. On a repeat
    429 (or any other HTTP error) surface an ``AzureError`` with a message that
    starts with ``label`` so the caller's user-facing string is preserved."""
    url = (f"{_AZURE_MGMT}/subscriptions/{sub_id}/providers/"
           "Microsoft.CostManagement/query?api-version=2023-03-01")
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            for attempt in (0, 1):
                resp = await client.post(url, json=body, headers=headers)
                if getattr(resp, "status_code", None) == 429 and attempt == 0:
                    wait = _retry_after_seconds(
                        resp.headers.get("Retry-After") if hasattr(resp, "headers") else None,
                        default=10)
                    logger.warning(
                        "azure cost 429 for %s; retrying in %ss (Retry-After honored)",
                        label, wait)
                    await asyncio.sleep(wait)
                    continue
                resp.raise_for_status()
                return resp.json() or {}
    except httpx.HTTPError as e:
        raise azure_service.AzureError(f"{label}: {e}") from e
    raise azure_service.AzureError(f"{label}: rate limited after retry")


async def get_azure_mtd_cost() -> tuple:
    """Azure subscription month-to-date ActualCost via the Cost Management REST
    query. Returns (amount, currency). Reuses ``azure_service._ensure_creds`` for
    the credential + subscription; raises ``azure_service.AzureError``."""
    cred, sub_id = await azure_service._ensure_creds()
    token = (await asyncio.to_thread(cred.get_token, f"{_AZURE_MGMT}/.default")).token
    body = {
        "type": "ActualCost",
        "timeframe": "MonthToDate",
        "dataset": {
            "granularity": "None",
            "aggregation": {"totalCost": {"name": "Cost", "function": "Sum"}},
        },
    }
    data = await _azure_cost_query(
        sub_id, token, body, label="Azure Cost Management query failed")
    props = data.get("properties", {})
    cols = [c.get("name") for c in props.get("columns", [])]
    cost_idx = cols.index("Cost") if "Cost" in cols else 0
    cur_idx = cols.index("Currency") if "Currency" in cols else None
    amount, currency = 0.0, "USD"
    for row in props.get("rows", []):
        amount += float(row[cost_idx] or 0)
        if cur_idx is not None and row[cur_idx]:
            currency = row[cur_idx]
    return amount, currency


def _empty_scopes() -> dict:
    return {s: {"total": None, "services": []} for s in _SCOPES}


def _scoped_breakdown_result(amounts: dict, currency: str, *,
                             measured=(SCOPE_DASHBOARD, SCOPE_SANDBOX),
                             basis=None, notes=None) -> dict:
    """Shape a ``{(scope, service): amount}`` map into the breakdown payload.

    ``services`` (the legacy key) still holds amount-desc service rows summing to
    ``total``, but now carries dashboard **and** sandbox rows, each tagged with its
    ``scope``. The unattributed remainder lives only under ``scopes.unattributed``:
    folding it into ``total`` would double-count against the account-total card.

    ``measured`` names the scopes this cloud's query genuinely observes. A scope
    outside it with no rows reports ``total: None`` (**unknown** — e.g. OCI can't see
    dashboard spend at all) rather than ``0.0`` (**measured, and zero**). That
    distinction is the whole point of the page, so don't collapse it."""
    per_scope: dict = {s: [] for s in _SCOPES}
    for (scope, service), amount in amounts.items():
        per_scope.setdefault(scope, []).append(
            {"scope": scope, "service": service, "amount": round(amount, 2)})

    scopes: dict = {}
    for s in _SCOPES:
        rows = sorted(per_scope.get(s, []), key=lambda r: r["amount"], reverse=True)
        total = round(sum(r["amount"] for r in rows), 2) if (s in measured or rows) else None
        scopes[s] = {"total": total, "services": rows}

    flat = sorted(scopes[SCOPE_DASHBOARD]["services"] + scopes[SCOPE_SANDBOX]["services"],
                  key=lambda r: r["amount"], reverse=True)
    attributed = [t for t in (scopes[SCOPE_DASHBOARD]["total"],
                              scopes[SCOPE_SANDBOX]["total"]) if t is not None]
    return {
        "total": round(sum(attributed), 2) if attributed else None,
        "dashboard_total": scopes[SCOPE_DASHBOARD]["total"],
        "sandbox_total": scopes[SCOPE_SANDBOX]["total"],
        "unattributed_total": scopes[SCOPE_UNATTRIBUTED]["total"],
        "currency": currency,
        "services": flat,
        "scopes": scopes,
        "scope_basis": dict(basis or {}),
        "notes": list(notes or []),
    }


def _breakdown_result(services: dict, currency: str) -> dict:
    """Back-compat shim: a flat ``{service: amount}`` map is all dashboard scope."""
    return _scoped_breakdown_result(
        {(SCOPE_DASHBOARD, k): v for k, v in services.items()}, currency,
        measured=(SCOPE_DASHBOARD,))


async def get_aws_managed_breakdown() -> dict:
    """AWS MTD spend tagged ``managed-by``, split into dashboard vs sandbox scope and
    grouped by service. Raises ``aws_service.AWSError`` (incl. when the ``managed-by``
    cost-allocation tag isn't activated in Billing yet).

    ``unattributed_total`` is ``None`` for AWS: the tag filter structurally excludes
    untagged spend, so we can't measure the remainder — the account-total card already
    shows the whole number."""
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
                # One request, both scopes: same tag KEY, two values (Values is an OR
                # list). Cost Explorer bills ~$0.01/request — don't split this in two.
                Filter={"Tags": {"Key": _MANAGED_TAG_KEY,
                                 "Values": _SCOPE_TAG_VALUES,
                                 "MatchOptions": ["EQUALS"]}},
                # CE allows at most 2 GroupBy entries, and service + the scope tag is
                # exactly 2 — there is no room for a third dimension here.
                GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"},
                         {"Type": "TAG", "Key": _MANAGED_TAG_KEY}],
            )
        except (BotoCoreError, ClientError) as e:
            raise aws_service.AWSError(
                f"AWS Cost Explorer breakdown failed: {e}. If this is a tag error, "
                f"activate the '{_MANAGED_TAG_KEY}' cost-allocation tag in the AWS "
                "Billing console (forward-only; ~24h to populate)."
            ) from e
        amounts, currency = {}, "USD"
        for period in resp.get("ResultsByTime", []):
            for grp in period.get("Groups", []):
                keys = grp.get("Keys") or []
                service = (keys[0] if keys else "") or "(unknown)"
                # CE renders a TAG group key as "<key>$<value>" ("<key>$" when absent).
                raw = keys[1] if len(keys) > 1 else ""
                scope = _scope_of(raw.split("$", 1)[1] if "$" in raw else raw)
                blob = grp.get("Metrics", {}).get("UnblendedCost", {})
                k = (scope, service)
                amounts[k] = amounts.get(k, 0.0) + float(blob.get("Amount") or 0)
                currency = blob.get("Unit") or currency
        return _scoped_breakdown_result(amounts, currency, basis={
            SCOPE_DASHBOARD: f"tag:{_MANAGED_TAG_KEY}={_MANAGED_TAG_VALUE}",
            SCOPE_SANDBOX: f"tag:{_MANAGED_TAG_KEY}={_SANDBOX_TAG_VALUE}",
        }, notes=[
            "Untaggable line items (inter-AZ data transfer, some VPC charges) can't be "
            "attributed to either scope; they show only in the account total.",
        ])

    return await asyncio.to_thread(_query)


# Column names Cost Management returns that are NOT the managed-by tag value.
_AZURE_KNOWN_COLS = {"Cost", "CostUSD", "PreTaxCost", "Currency", "ServiceName",
                     "UsageDate", "ResourceGroup", "ResourceGroupName"}


def _azure_tag_col(cols: list):
    """Index of the ``managed-by`` tag VALUE column, or None.

    The name is inconsistent across API versions/tenants — it has been observed named
    after the tag key itself and, elsewhere, ``TagValue``. Try value-shaped names first
    (so a TagKey/TagValue *pair* resolves to the value, not the key), then fall back to
    "the one column we don't recognise". Guessing wrong degrades every row to
    ``unattributed``, which is visible on the page rather than silently wrong."""
    for name in (_MANAGED_TAG_KEY, "TagValue", "Tag", "TagKey"):
        if name in cols:
            return cols.index(name)
    extras = [i for i, c in enumerate(cols) if c not in _AZURE_KNOWN_COLS]
    return extras[0] if len(extras) == 1 else None


async def get_azure_managed_breakdown() -> dict:
    """Azure MTD spend tagged ``managed-by``, split into dashboard vs sandbox scope and
    grouped by service. Raises ``azure_service.AzureError``.

    ``unattributed_total`` is ``None``: Azure tags do NOT inherit from a resource group
    to its children, and Azure creates children (disks, NICs, public IPs, ACI) untagged,
    so a tag-filtered query can't see them. Enabling *Cost Management → Manage tag
    inheritance* at subscription scope fixes this with no code change — the sandbox
    already tags its RG (setup-azure.sh:59) and inherited tags lose to resource tags, so
    it yields exactly the right semantics. Note RG-dimension scoping is NOT an option:
    the dashboard deploys into the same ``dashboard-sandbox-rg``, so the dimension can't
    separate the two scopes."""
    cred, sub_id = await azure_service._ensure_creds()
    token = (await asyncio.to_thread(cred.get_token, f"{_AZURE_MGMT}/.default")).token
    body = {
        "type": "ActualCost",
        "timeframe": "MonthToDate",
        "dataset": {
            "granularity": "None",
            "aggregation": {"totalCost": {"name": "Cost", "function": "Sum"}},
            # Cost Management caps `grouping` at 2 entries, so ServiceName + the scope
            # tag uses the whole budget — a third (ResourceGroupName) isn't possible.
            "grouping": [
                {"type": "Dimension", "name": "ServiceName"},
                {"type": "TagKey", "name": _MANAGED_TAG_KEY},
            ],
            "filter": {"tags": {"name": _MANAGED_TAG_KEY, "operator": "In",
                                "values": _SCOPE_TAG_VALUES}},
        },
    }
    data = await _azure_cost_query(
        sub_id, token, body, label="Azure Cost Management breakdown failed")
    props = data.get("properties", {})
    cols = [c.get("name") for c in props.get("columns", [])]
    cost_idx = cols.index("Cost") if "Cost" in cols else 0
    svc_idx = cols.index("ServiceName") if "ServiceName" in cols else None
    cur_idx = cols.index("Currency") if "Currency" in cols else None
    tag_idx = _azure_tag_col(cols)
    amounts, currency = {}, "USD"
    for row in props.get("rows", []):
        name = row[svc_idx] if svc_idx is not None else "(unknown)"
        scope = _scope_of(row[tag_idx]) if tag_idx is not None else SCOPE_UNATTRIBUTED
        k = (scope, name)
        amounts[k] = amounts.get(k, 0.0) + float(row[cost_idx] or 0)
        if cur_idx is not None and row[cur_idx]:
            currency = row[cur_idx]
    return _scoped_breakdown_result(amounts, currency, basis={
        SCOPE_DASHBOARD: f"tag:{_MANAGED_TAG_KEY}={_MANAGED_TAG_VALUE}",
        SCOPE_SANDBOX: f"tag:{_MANAGED_TAG_KEY}={_SANDBOX_TAG_VALUE}",
    }, notes=[
        "Azure tags don't inherit to child resources (disks, NICs, public IPs, ACI), so "
        "their cost is missing here. Enable Cost Management → Manage tag inheritance at "
        "subscription scope to include it.",
    ])


# ── GCP (BigQuery billing export) ────────────────────────────────────────────

_GCP_TABLE_KEY = "gcp_billing_export_table"
# Net cost = usage cost + credits (credits are negative) — matches the GCP Billing
# console's "cost".
_GCP_NET = "SUM(cost) + SUM(IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) c), 0))"


def _gcp_billing_table() -> str:
    from ..config import settings
    return (config_service.get(_GCP_TABLE_KEY) or getattr(settings, _GCP_TABLE_KEY, "") or "").strip()


def _gcp_bq_client(table: str):
    """A BigQuery client from the dashboard's GCP creds. Raises GCPError when the
    export table isn't configured or the lib isn't installed."""
    if not table:
        raise gcp_service.GCPError(
            "GCP cost is unavailable — set the BigQuery billing-export table on the "
            "Cloud Costs settings (project.dataset.gcp_billing_export_v1_XXXX) and grant the "
            "dashboard service account BigQuery Data Viewer + Job User on that dataset."
        )
    try:
        from google.cloud import bigquery
    except ImportError as e:
        raise gcp_service.GCPError("google-cloud-bigquery is not installed.") from e
    client = bigquery.Client(
        project=gcp_service._gcp_project() or None, credentials=gcp_service._gcp_creds())
    return bigquery, client


def _gcp_month_param(bigquery):
    return bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter("month_start", "STRING", _month_range()[0]),
    ])


async def get_gcp_mtd_cost() -> tuple:
    """GCP account month-to-date **net** cost from the BigQuery billing export.
    Returns (amount, currency). Raises ``gcp_service.GCPError`` (incl. when the
    export table isn't configured)."""
    table = _gcp_billing_table()

    def _query() -> tuple:
        bigquery, client = _gcp_bq_client(table)
        sql = (f"SELECT {_GCP_NET} AS net, ANY_VALUE(currency) AS currency "
               f"FROM `{table}` WHERE usage_start_time >= TIMESTAMP(@month_start)")
        rows = list(client.query(sql, job_config=_gcp_month_param(bigquery)).result())
        if not rows:
            return 0.0, "USD"
        return float(rows[0]["net"] or 0), (rows[0]["currency"] or "USD")

    try:
        return await asyncio.to_thread(_query)
    except gcp_service.GCPError:
        raise
    except Exception as e:  # noqa: BLE001
        raise gcp_service.GCPError(f"GCP BigQuery cost query failed: {e}") from e


def _gcp_key_list() -> str:
    """The accepted ``managed-by`` label keys as a SQL IN-list literal. Module
    constants, not user input."""
    return "(" + ", ".join(f"'{k}'" for k in _GCP_LABEL_KEYS) + ")"


async def get_gcp_managed_breakdown() -> dict:
    """GCP MTD net cost labelled ``managed-by``, split into dashboard vs sandbox scope
    and grouped by service. Raises GCPError.

    Unlike AWS/Azure the query is UNFILTERED, so ``unattributed_total`` here is a real
    measured number rather than ``None``. That matters: Cloud Router and Cloud NAT — the
    entire GCP idle cost — accept no labels at all, so they can only ever land in the
    unattributed bucket. The ``project.labels`` arm of the COALESCE is the escape hatch:
    ``gcloud projects update <p> --update-labels managed-by=dashboard-sandbox`` puts them
    in the sandbox scope (forward-only; billing-export rows are immutable), while a
    resource label still wins for dashboard-provisioned VMs."""
    table = _gcp_billing_table()

    def _query() -> dict:
        bigquery, client = _gcp_bq_client(table)
        # The label keys are fixed constants (not user input); the table is an
        # admin-configured identifier (BigQuery can't parameterize table names).
        keys = _gcp_key_list()
        sql = (
            "WITH line_items AS ("
            "  SELECT service.description AS service, cost, credits, currency, "
            "    COALESCE("
            f"      (SELECT l.value FROM UNNEST(labels) l WHERE l.key IN {keys} LIMIT 1), "
            f"      (SELECT p.value FROM UNNEST(project.labels) p WHERE p.key IN {keys} LIMIT 1)"
            "    ) AS managed_by"
            f"  FROM `{table}`"
            "  WHERE usage_start_time >= TIMESTAMP(@month_start)"
            ") "
            f"SELECT managed_by, service, {_GCP_NET} AS amount, "
            "ANY_VALUE(currency) AS currency FROM line_items "
            "GROUP BY managed_by, service"
        )
        amounts, currency = {}, "USD"
        for r in client.query(sql, job_config=_gcp_month_param(bigquery)).result():
            name = r["service"] or "(unknown)"
            k = (_scope_of(r["managed_by"]), name)
            amounts[k] = amounts.get(k, 0.0) + float(r["amount"] or 0)
            if r["currency"]:
                currency = r["currency"]
        return _scoped_breakdown_result(amounts, currency, measured=_SCOPES, basis={
            SCOPE_DASHBOARD: f"label:{'|'.join(_GCP_LABEL_KEYS)}={_MANAGED_TAG_VALUE}",
            SCOPE_SANDBOX: f"label:{'|'.join(_GCP_LABEL_KEYS)}={_SANDBOX_TAG_VALUE} "
                           "(resource or project label)",
        }, notes=[
            "Cloud Router and Cloud NAT accept no labels, so the sandbox's standing GCP "
            "cost lands in unattributed. Label the project "
            "(managed-by=dashboard-sandbox) to attribute it going forward.",
        ])

    try:
        return await asyncio.to_thread(_query)
    except gcp_service.GCPError:
        raise
    except Exception as e:  # noqa: BLE001
        raise gcp_service.GCPError(f"GCP BigQuery breakdown query failed: {e}") from e


# ── OCI (Usage API) ──────────────────────────────────────────────────────────
# OCI has no Cost Explorer equivalent; the Usage API (request_summarized_usages)
# returns cost. MONTHLY granularity requires the time range aligned to month
# boundaries, so we query [first-of-this-month, first-of-next-month) — future
# usage is $0, so that MONTHLY bucket IS the month-to-date cost.

def _oci_month_bounds():
    """(month_start_dt, next_month_start_dt) as UTC-midnight datetimes for the
    Usage API's month-aligned MONTHLY query."""
    from datetime import datetime, timezone
    today = date.today()
    start = today.replace(day=1)
    nxt = (date(start.year + 1, 1, 1) if start.month == 12
           else date(start.year, start.month + 1, 1))
    to_dt = lambda d: datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
    return to_dt(start), to_dt(nxt)


def _oci_tenancy() -> str:
    return (config_service.get("oci_tenancy_ocid") or "").strip()


async def get_oci_mtd_cost() -> tuple:
    """OCI tenancy month-to-date cost via the Usage API. Returns (amount, currency).
    Raises ``oci_service.OCIError`` (incl. when OCI isn't configured)."""
    tenancy = _oci_tenancy()
    if not tenancy:
        raise oci_service.OCIError("OCI cost unavailable — oci_tenancy_ocid not configured.")

    def _query() -> tuple:
        import oci
        client = oci.usage_api.UsageapiClient(oci_service._oci_config())
        start, end = _oci_month_bounds()
        details = oci.usage_api.models.RequestSummarizedUsagesDetails(
            tenant_id=tenancy, time_usage_started=start, time_usage_ended=end,
            granularity="MONTHLY", query_type="COST",
        )
        resp = client.request_summarized_usages(details)
        amount, currency = 0.0, "USD"
        for item in (resp.data.items or []):
            amount += float(getattr(item, "computed_amount", 0) or 0)
            currency = getattr(item, "currency", None) or currency
        return amount, currency

    try:
        return await asyncio.to_thread(_query)
    except oci_service.OCIError:
        raise
    except Exception as e:  # noqa: BLE001
        raise oci_service.OCIError(f"OCI Usage API cost query failed: {e}") from e


def _oci_sandbox_compartment() -> str:
    """The sandbox compartment OCID, or "" when the config points at the tenancy root.

    ``oci_service._compartment()`` falls back to the tenancy when
    ``oci_compartment_ocid`` is blank — taking that fallback here would make "sandbox"
    mean "the entire tenancy", so guard it explicitly."""
    tenancy = _oci_tenancy()
    comp = (config_service.get("oci_compartment_ocid") or "").strip()
    return "" if (not comp or comp == tenancy) else comp


async def get_oci_managed_breakdown() -> dict:
    """OCI MTD cost grouped by compartment + service, scoped by COMPARTMENT rather than
    by tag. Raises OCIError.

    The Usage API only reports tags configured as **cost-tracking (defined) tags**, and
    the sandbox writes a *freeform* tag (setup-oci.sh:51) — so the tag filter this
    replaced could never match anything, and OCI's breakdown was always empty. Compartment
    is the only scope OCI billing can express.

    Two honest consequences: ``dashboard_total`` is ``None`` (unknowable — OCI can't tell
    dashboard-provisioned resources from the sandbox baseline, since both live in the same
    compartment), and the sandbox figure therefore *includes* dashboard deploys. Promoting
    ``managed-by`` to a cost-tracking tag namespace and re-tagging would let OCI split like
    AWS/Azure."""
    tenancy = _oci_tenancy()
    if not tenancy:
        raise oci_service.OCIError("OCI cost unavailable — oci_tenancy_ocid not configured.")
    sandbox_comp = _oci_sandbox_compartment()

    def _query() -> dict:
        import oci
        client = oci.usage_api.UsageapiClient(oci_service._oci_config())
        start, end = _oci_month_bounds()
        details = oci.usage_api.models.RequestSummarizedUsagesDetails(
            tenant_id=tenancy, time_usage_started=start, time_usage_ended=end,
            granularity="MONTHLY", query_type="COST",
            # No tag filter — see the docstring. compartmentId is returned per item, so
            # group by it (not compartmentName) to compare against the configured OCID.
            group_by=["compartmentId", "service"],
        )
        resp = client.request_summarized_usages(details)
        amounts, currency = {}, "USD"
        for item in (resp.data.items or []):
            name = getattr(item, "service", None) or "(unknown)"
            comp = (getattr(item, "compartment_id", None) or "").strip()
            scope = (SCOPE_SANDBOX if sandbox_comp and comp == sandbox_comp
                     else SCOPE_UNATTRIBUTED)
            k = (scope, name)
            amounts[k] = amounts.get(k, 0.0) + float(getattr(item, "computed_amount", 0) or 0)
            currency = getattr(item, "currency", None) or currency
        notes = ["OCI can't separate dashboard-provisioned resources from the sandbox "
                 "baseline: the Usage API ignores freeform tags, so this is scoped by "
                 "compartment and the sandbox figure includes dashboard deploys."]
        if not sandbox_comp:
            notes.append("oci_compartment_ocid is unset (or equals the tenancy root), so "
                         "nothing can be attributed — set it to the sandbox compartment.")
        # Without a configured compartment the sandbox scope isn't measurable at all, so
        # it must report None ("can't see it") rather than 0.0 ("looked, found nothing").
        measured = (SCOPE_SANDBOX, SCOPE_UNATTRIBUTED) if sandbox_comp else (SCOPE_UNATTRIBUTED,)
        return _scoped_breakdown_result(
            amounts, currency, measured=measured,
            basis={SCOPE_SANDBOX: f"compartment:{sandbox_comp}"} if sandbox_comp else {},
            notes=notes)

    try:
        return await asyncio.to_thread(_query)
    except oci_service.OCIError:
        raise
    except Exception as e:  # noqa: BLE001
        raise oci_service.OCIError(f"OCI Usage API breakdown query failed: {e}") from e


def _unavailable_breakdown(cloud: str, detail: str) -> dict:
    """A degraded entry that still exposes every key the template reads, so an
    unavailable cloud renders "unavailable" instead of `undefined`."""
    return {"cloud": cloud, "status": "unavailable", "detail": detail,
            "total": None, "dashboard_total": None, "sandbox_total": None,
            "unattributed_total": None, "currency": None, "services": [],
            "scopes": _empty_scopes(), "scope_basis": {}, "notes": []}


async def _breakdown_entry(cloud: str, fetch) -> dict:
    """One cloud's breakdown, degrading any failure to status=unavailable."""
    try:
        res = await fetch()
        return {"cloud": cloud, "status": "ok", "detail": "", **res}
    except (aws_service.AWSError, azure_service.AzureError, gcp_service.GCPError, oci_service.OCIError) as e:
        return _unavailable_breakdown(cloud, str(e))
    except Exception as e:  # noqa: BLE001 — defensive: unknown errors are still per-cloud
        logger.warning("cost: %s breakdown failed unexpectedly: %s", cloud, e)
        return _unavailable_breakdown(cloud, str(e))


def _sum_scope(entries: list, key: str):
    """Sum one scope across clouds. ``None`` when no cloud could measure it — which is
    different from ``0.0`` (every cloud measured, and it's zero)."""
    vals = [e.get(key) for e in entries if e.get(key) is not None]
    return round(sum(vals), 2) if vals else None


async def get_cost_breakdown() -> dict:
    """Per-cloud, per-service MTD spend split into **dashboard** (``managed-by=
    vm-dashboard``) and **sandbox** (``managed-by=dashboard-sandbox``) scope, plus each
    cloud's unattributed remainder where it can measure one.

    ``grand_total`` covers only clouds that returned ``ok``, and only their attributed
    (dashboard + sandbox) spend — unattributed is reported separately so it never
    double-counts against the account-total card."""
    aws_entry, azure_entry, gcp_entry, oci_entry = await asyncio.gather(
        _breakdown_entry("aws", get_aws_managed_breakdown),
        _breakdown_entry("azure", get_azure_managed_breakdown),
        _breakdown_entry("gcp", get_gcp_managed_breakdown),
        _breakdown_entry("oci", get_oci_managed_breakdown),
    )
    clouds = [aws_entry, azure_entry, gcp_entry, oci_entry]
    oks = [c for c in clouds if c["status"] == "ok"]
    currency = oks[0]["currency"] if oks else "USD"
    return {"clouds": clouds,
            # `total` is None for a cloud that measured nothing, so sum defensively.
            "grand_total": _sum_scope(oks, "total"),
            "dashboard_total": _sum_scope(oks, "dashboard_total"),
            "sandbox_total": _sum_scope(oks, "sandbox_total"),
            "unattributed_total": _sum_scope(oks, "unattributed_total"),
            "currency": currency}


async def _cloud_entry(cloud: str, fetch) -> dict:
    """Run one cloud's MTD query, degrading any failure to status=unavailable so
    a single misconfigured cloud never sinks the whole summary."""
    try:
        amount, currency = await fetch()
        return {"cloud": cloud, "amount": round(amount, 2),
                "currency": currency, "status": "ok", "detail": ""}
    except (aws_service.AWSError, azure_service.AzureError, gcp_service.GCPError, oci_service.OCIError) as e:
        return {"cloud": cloud, "amount": None, "currency": None,
                "status": "unavailable", "detail": str(e)}
    except Exception as e:  # noqa: BLE001 — defensive: unknown errors are still per-cloud
        logger.warning("cost: %s query failed unexpectedly: %s", cloud, e)
        return {"cloud": cloud, "amount": None, "currency": None,
                "status": "unavailable", "detail": str(e)}


async def get_cost_summary() -> dict:
    """Per-cloud account/subscription MTD spend. AWS + Azure are queried live; GCP
    is queried via the BigQuery billing export when configured. ``total_mtd`` sums
    only the clouds that returned ``ok``."""
    aws_entry, azure_entry, gcp_entry, oci_entry = await asyncio.gather(
        _cloud_entry("aws", get_aws_mtd_cost),
        _cloud_entry("azure", get_azure_mtd_cost),
        _cloud_entry("gcp", get_gcp_mtd_cost),
        _cloud_entry("oci", get_oci_mtd_cost),
    )
    clouds = [aws_entry, azure_entry, gcp_entry, oci_entry]
    oks = [c for c in clouds if c["status"] == "ok"]
    total = round(sum(c["amount"] for c in oks), 2) if oks else None
    currency = oks[0]["currency"] if oks else "USD"
    return {"total_mtd": total, "currency": currency, "clouds": clouds}
