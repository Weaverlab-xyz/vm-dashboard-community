"""Unit tests for cost_service (cross-cloud month-to-date spend).

Covers the summary orchestration — per-cloud graceful degradation, total summing
only `ok` clouds, GCP always unavailable — plus the AWS Cost Explorer and Azure
Cost Management response parsing. Heavy deps (aws_service/azure_service, httpx,
boto3, botocore) are stubbed in sys.modules so no cloud SDK or account is needed.
Runs under pytest, or standalone:  python tests/test_cost_service.py
"""
import asyncio
import os
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# Parsing fixtures the stubbed clients return (set once; one scenario each).
_AWS_CE_RESULT = {"ResultsByTime": [
    {"Total": {"UnblendedCost": {"Amount": "10.50", "Unit": "USD"}}},
    {"Total": {"UnblendedCost": {"Amount": "4.25", "Unit": "USD"}}},
]}
_AZURE_QUERY_RESULT = {"properties": {
    "columns": [{"name": "Cost"}, {"name": "Currency"}],
    "rows": [[123.456, "USD"]],
}}
# Grouped (per-service) fixtures for the dashboard-managed breakdown.
_AWS_CE_GROUPED = {"ResultsByTime": [{"Groups": [
    {"Keys": ["Amazon EC2"], "Metrics": {"UnblendedCost": {"Amount": "8.00", "Unit": "USD"}}},
    {"Keys": ["Amazon RDS"], "Metrics": {"UnblendedCost": {"Amount": "2.50", "Unit": "USD"}}},
]}]}
_AZURE_GROUPED_RESULT = {"properties": {
    "columns": [{"name": "Cost"}, {"name": "ServiceName"}, {"name": "Currency"}],
    "rows": [[6.0, "Virtual Machines", "USD"], [1.5, "Storage", "USD"]],
}}
# GCP BigQuery billing-export fake rows (a Row supports r["col"]; dicts suffice).
_BQ_SUMMARY = [{"net": 42.5, "currency": "USD"}]
_BQ_GROUPED = [
    {"service": "Compute Engine", "amount": 30.0, "currency": "USD"},
    {"service": "Cloud Storage", "amount": 12.5, "currency": "USD"},
]
# Backs the stubbed config_service so tests can set/clear the export table.
CONF = {}


class _AWSError(Exception):
    pass


class _AzureError(Exception):
    pass


def _install_stubs():
    aws = types.ModuleType("web_dashboard.services.aws_service")
    aws.AWSError = _AWSError
    aws._require_boto3 = lambda: None
    aws._aws_kwargs = lambda region: {}
    sys.modules["web_dashboard.services.aws_service"] = aws

    az = types.ModuleType("web_dashboard.services.azure_service")
    az.AzureError = _AzureError

    class _Cred:
        def get_token(self, *scopes):
            return types.SimpleNamespace(token="fake-token")

    async def _ensure_creds():
        return _Cred(), "sub-123"
    az._ensure_creds = _ensure_creds
    sys.modules["web_dashboard.services.azure_service"] = az

    # httpx stub (AsyncClient context manager + HTTPError).
    httpx = types.ModuleType("httpx")
    httpx.HTTPError = type("HTTPError", (Exception,), {})

    class _Resp:
        def __init__(self, data): self._data = data
        def raise_for_status(self): pass
        def json(self): return self._data

    class _AsyncClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **k):
            # Grouped query (breakdown) carries dataset.grouping; ungrouped is the summary.
            grouped = bool(((k.get("json") or {}).get("dataset") or {}).get("grouping"))
            return _Resp(_AZURE_GROUPED_RESULT if grouped else _AZURE_QUERY_RESULT)
    httpx.AsyncClient = _AsyncClient
    sys.modules["httpx"] = httpx

    # boto3 / botocore stubs for the AWS Cost Explorer parsing path.
    boto3 = types.ModuleType("boto3")

    class _CE:
        def get_cost_and_usage(self, **kw):
            # GroupBy present → the breakdown query; else the summary query.
            return _AWS_CE_GROUPED if "GroupBy" in kw else _AWS_CE_RESULT
    boto3.client = lambda name, **kw: _CE()
    sys.modules["boto3"] = boto3

    botocore = types.ModuleType("botocore")
    exc = types.ModuleType("botocore.exceptions")
    exc.BotoCoreError = type("BotoCoreError", (Exception,), {})
    exc.ClientError = type("ClientError", (Exception,), {})
    botocore.exceptions = exc
    sys.modules["botocore"] = botocore
    sys.modules["botocore.exceptions"] = exc

    # gcp_service (creds + GCPError) + config_service (export-table key) + a light
    # web_dashboard.config so cost_service's lazy `from ..config import settings`
    # doesn't pull pydantic.
    gcp = types.ModuleType("web_dashboard.services.gcp_service")
    gcp.GCPError = type("GCPError", (Exception,), {})
    gcp._gcp_project = lambda: "proj"
    gcp._gcp_creds = lambda: None
    sys.modules["web_dashboard.services.gcp_service"] = gcp

    cfg = types.ModuleType("web_dashboard.services.config_service")
    cfg.get = lambda key, default="": CONF.get(key, default)
    sys.modules["web_dashboard.services.config_service"] = cfg

    confmod = types.ModuleType("web_dashboard.config")
    confmod.settings = type("_S", (), {"__getattr__": lambda self, k: ""})()
    sys.modules["web_dashboard.config"] = confmod

    # google.cloud.bigquery — fake Client returning grouped/summary rows by SQL.
    class _QueryJob:
        def __init__(self, rows): self._rows = rows
        def result(self): return iter(self._rows)

    class _BQClient:
        def __init__(self, *a, **k): pass
        def query(self, sql, job_config=None):
            return _QueryJob(_BQ_GROUPED if "GROUP BY" in sql else _BQ_SUMMARY)

    google = sys.modules.get("google") or types.ModuleType("google")
    gcloud = types.ModuleType("google.cloud")
    bq = types.ModuleType("google.cloud.bigquery")
    bq.Client = _BQClient
    bq.QueryJobConfig = lambda **k: None
    bq.ScalarQueryParameter = lambda *a, **k: None
    gcloud.bigquery = bq
    google.cloud = gcloud
    sys.modules["google"] = google
    sys.modules["google.cloud"] = gcloud
    sys.modules["google.cloud.bigquery"] = bq


_install_stubs()
try:
    from web_dashboard.services import cost_service as svc
except Exception as exc:  # pragma: no cover
    try:
        import pytest
        pytest.skip(f"cost_service import unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)


# Capture the real fetchers so the parsing tests are immune to the summary
# tests reassigning the module globals (pytest runs in definition order).
_ORIG_AWS = svc.get_aws_mtd_cost
_ORIG_AZURE = svc.get_azure_mtd_cost
_ORIG_AWS_BD = svc.get_aws_managed_breakdown
_ORIG_AZURE_BD = svc.get_azure_managed_breakdown
_ORIG_GCP = svc.get_gcp_mtd_cost
_ORIG_GCP_BD = svc.get_gcp_managed_breakdown


def _run(coro):
    return asyncio.run(coro)


def _restore():
    svc.get_aws_mtd_cost = _ORIG_AWS
    svc.get_azure_mtd_cost = _ORIG_AZURE
    svc.get_aws_managed_breakdown = _ORIG_AWS_BD
    svc.get_azure_managed_breakdown = _ORIG_AZURE_BD
    svc.get_gcp_mtd_cost = _ORIG_GCP
    svc.get_gcp_managed_breakdown = _ORIG_GCP_BD


def _by_cloud(summary):
    return {c["cloud"]: c for c in summary["clouds"]}


# ── summary orchestration ────────────────────────────────────────────────────

def test_summary_both_ok_sums_total():
    async def aws(): return (100.0, "USD")
    async def azure(): return (50.0, "USD")
    svc.get_aws_mtd_cost, svc.get_azure_mtd_cost = aws, azure
    s = _run(svc.get_cost_summary())
    assert s["total_mtd"] == 150.0
    assert s["currency"] == "USD"
    by = _by_cloud(s)
    assert by["aws"]["status"] == "ok" and by["aws"]["amount"] == 100.0
    assert by["azure"]["status"] == "ok" and by["azure"]["amount"] == 50.0


def test_summary_one_unavailable_excludes_it_from_total():
    async def aws(): raise svc.aws_service.AWSError("no ce:GetCostAndUsage")
    async def azure(): return (50.0, "USD")
    svc.get_aws_mtd_cost, svc.get_azure_mtd_cost = aws, azure
    s = _run(svc.get_cost_summary())
    assert s["total_mtd"] == 50.0  # only the ok cloud
    by = _by_cloud(s)
    assert by["aws"]["status"] == "unavailable" and by["aws"]["amount"] is None
    assert "ce:GetCostAndUsage" in by["aws"]["detail"]
    assert by["azure"]["status"] == "ok"


def test_summary_all_unavailable_total_none():
    async def boom_aws(): raise svc.aws_service.AWSError("x")
    async def boom_azure(): raise svc.azure_service.AzureError("y")
    svc.get_aws_mtd_cost, svc.get_azure_mtd_cost = boom_aws, boom_azure
    s = _run(svc.get_cost_summary())
    assert s["total_mtd"] is None
    assert all(c["status"] == "unavailable" for c in s["clouds"])


def test_gcp_unavailable_without_export_table():
    CONF.pop("gcp_billing_export_table", None)
    async def aws(): return (1.0, "USD")
    async def azure(): return (2.0, "USD")
    svc.get_aws_mtd_cost, svc.get_azure_mtd_cost = aws, azure
    gcp = _by_cloud(_run(svc.get_cost_summary()))["gcp"]
    assert gcp["status"] == "unavailable"
    assert "BigQuery" in gcp["detail"]  # the configure-the-export hint


# ── per-cloud parsing ────────────────────────────────────────────────────────

def test_aws_parsing_sums_results_by_time():
    _restore()  # undo any reassignment from the summary tests
    amount, currency = _run(svc.get_aws_mtd_cost())  # stubbed boto3 → _AWS_CE_RESULT
    assert round(amount, 2) == 14.75  # 10.50 + 4.25
    assert currency == "USD"


def test_azure_parsing_reads_cost_and_currency_columns():
    _restore()
    amount, currency = _run(svc.get_azure_mtd_cost())  # stubbed httpx → _AZURE_QUERY_RESULT
    assert round(amount, 3) == 123.456
    assert currency == "USD"


# ── managed breakdown: parsing ───────────────────────────────────────────────

def test_aws_breakdown_parsing_groups_by_service():
    _restore()
    res = _run(svc.get_aws_managed_breakdown())  # stubbed boto3 → _AWS_CE_GROUPED
    assert res["total"] == 10.5 and res["currency"] == "USD"
    # sorted by amount desc
    assert [s["service"] for s in res["services"]] == ["Amazon EC2", "Amazon RDS"]
    assert res["services"][0]["amount"] == 8.0 and res["services"][1]["amount"] == 2.5


def test_azure_breakdown_parsing_reads_servicename():
    _restore()
    res = _run(svc.get_azure_managed_breakdown())  # stubbed httpx → _AZURE_GROUPED_RESULT
    assert res["total"] == 7.5
    assert [s["service"] for s in res["services"]] == ["Virtual Machines", "Storage"]


# ── managed breakdown: orchestration ─────────────────────────────────────────

def _bd(total, currency="USD", services=None):
    async def _f():
        return {"total": total, "currency": currency, "services": services or []}
    return _f


def test_breakdown_both_ok_grand_total_and_services():
    svc.get_aws_managed_breakdown = _bd(10.0, services=[{"service": "EC2", "amount": 10.0}])
    svc.get_azure_managed_breakdown = _bd(5.0, services=[{"service": "VMs", "amount": 5.0}])
    try:
        out = _run(svc.get_cost_breakdown())
        assert out["grand_total"] == 15.0
        by = {c["cloud"]: c for c in out["clouds"]}
        assert by["aws"]["status"] == "ok" and by["aws"]["total"] == 10.0
        assert by["aws"]["services"][0]["service"] == "EC2"
        assert by["azure"]["status"] == "ok"
    finally:
        _restore()


def test_breakdown_one_unavailable_excluded_from_grand_total():
    async def boom(): raise svc.aws_service.AWSError("activate the managed-by tag")
    svc.get_aws_managed_breakdown = boom
    svc.get_azure_managed_breakdown = _bd(5.0)
    try:
        out = _run(svc.get_cost_breakdown())
        assert out["grand_total"] == 5.0
        by = {c["cloud"]: c for c in out["clouds"]}
        assert by["aws"]["status"] == "unavailable" and by["aws"]["total"] is None
        assert "managed-by" in by["aws"]["detail"]
    finally:
        _restore()


def test_breakdown_gcp_unavailable_without_table():
    CONF.pop("gcp_billing_export_table", None)
    svc.get_aws_managed_breakdown = _bd(1.0)
    svc.get_azure_managed_breakdown = _bd(2.0)
    try:
        gcp = {c["cloud"]: c for c in _run(svc.get_cost_breakdown())["clouds"]}["gcp"]
        assert gcp["status"] == "unavailable" and "BigQuery" in gcp["detail"]
    finally:
        _restore()


# ── GCP (BigQuery billing export) ────────────────────────────────────────────

def test_gcp_mtd_parsing_net_cost():
    _restore()
    CONF["gcp_billing_export_table"] = "proj.ds.gcp_billing_export_v1_ABC"
    try:
        amount, currency = _run(svc.get_gcp_mtd_cost())  # stubbed BQ → _BQ_SUMMARY
        assert amount == 42.5 and currency == "USD"
    finally:
        CONF.clear()


def test_gcp_breakdown_parsing_groups_by_service():
    _restore()
    CONF["gcp_billing_export_table"] = "proj.ds.gcp_billing_export_v1_ABC"
    try:
        res = _run(svc.get_gcp_managed_breakdown())  # stubbed BQ → _BQ_GROUPED
        assert res["total"] == 42.5
        assert [s["service"] for s in res["services"]] == ["Compute Engine", "Cloud Storage"]
    finally:
        CONF.clear()


def test_summary_includes_gcp_when_configured():
    async def aws(): return (10.0, "USD")
    async def azure(): return (5.0, "USD")
    async def gcp(): return (7.0, "USD")
    svc.get_aws_mtd_cost, svc.get_azure_mtd_cost, svc.get_gcp_mtd_cost = aws, azure, gcp
    try:
        s = _run(svc.get_cost_summary())
        assert s["total_mtd"] == 22.0  # all three clouds counted
        assert _by_cloud(s)["gcp"]["status"] == "ok" and _by_cloud(s)["gcp"]["amount"] == 7.0
    finally:
        _restore()


# ── budget evaluation ────────────────────────────────────────────────────────

from datetime import date as _date

# Mid-month "today" so projection = MTD / 15 * 30 = 2x MTD (June has 30 days).
_MID = _date(2026, 6, 15)


def test_budget_disabled_returns_none():
    assert svc.evaluate_budget(100.0, "USD", 0, today=_MID) is None
    assert svc.evaluate_budget(100.0, "USD", None, today=_MID) is None
    assert svc.evaluate_budget(None, "USD", 500, today=_MID) is None  # no spend data


def test_budget_over_when_mtd_exceeds_limit():
    b = svc.evaluate_budget(600.0, "USD", 500, today=_MID)
    assert b["status"] == "over" and b["pct_of_budget"] == 120.0
    assert b["limit"] == 500.0 and b["mtd"] == 600.0


def test_budget_approaching_when_on_pace():
    # MTD 300 on day 15 of a 30-day month → projected 600 ≥ 500 budget, but
    # MTD (300) < 500, so it's "approaching", not "over".
    b = svc.evaluate_budget(300.0, "USD", 500, today=_MID)
    assert b["status"] == "approaching" and b["projected"] == 600.0


def test_budget_ok_when_under_and_not_on_pace():
    # MTD 200 → projected 400 < 500 budget → ok.
    b = svc.evaluate_budget(200.0, "USD", 500, today=_MID)
    assert b["status"] == "ok" and b["projected"] == 400.0


def test_budget_currency_defaults_usd():
    assert svc.evaluate_budget(100.0, None, 500, today=_MID)["currency"] == "USD"


# ── apply_budget_alerts (overall + per-cloud) ────────────────────────────────

def test_apply_budget_alerts_overall_and_per_cloud():
    # Force "over" via MTD >= limit so the result is independent of today's date.
    CONF.clear()
    CONF.update({"cost_monthly_budget": "120", "cost_budget_aws": "50"})  # azure/gcp unset
    try:
        summary = {"total_mtd": 150.0, "currency": "USD", "clouds": [
            {"cloud": "aws", "amount": 100.0, "currency": "USD", "status": "ok"},
            {"cloud": "azure", "amount": 20.0, "currency": "USD", "status": "ok"},
            {"cloud": "gcp", "amount": None, "currency": None, "status": "unavailable"},
        ]}
        out = svc.apply_budget_alerts(summary)
        assert out["budget"]["status"] == "over"            # 150 >= 120
        by = {c["cloud"]: c for c in out["clouds"]}
        assert by["aws"]["budget"]["status"] == "over"      # 100 >= 50
        assert by["azure"]["budget"] is None                # no azure budget set
        assert by["gcp"]["budget"] is None                  # no amount → None
        assert "budget" not in summary["clouds"][0]         # original not mutated
    finally:
        CONF.clear()


def test_apply_budget_alerts_none_without_config():
    CONF.clear()
    summary = {"total_mtd": 10.0, "currency": "USD",
               "clouds": [{"cloud": "aws", "amount": 5.0, "currency": "USD", "status": "ok"}]}
    out = svc.apply_budget_alerts(summary)
    assert out["budget"] is None and out["clouds"][0]["budget"] is None


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
    sys.exit(1 if failures else 0)
