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
        def raise_for_status(self): pass
        def json(self): return _AZURE_QUERY_RESULT

    class _AsyncClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **k): return _Resp()
    httpx.AsyncClient = _AsyncClient
    sys.modules["httpx"] = httpx

    # boto3 / botocore stubs for the AWS Cost Explorer parsing path.
    boto3 = types.ModuleType("boto3")

    class _CE:
        def get_cost_and_usage(self, **kw): return _AWS_CE_RESULT
    boto3.client = lambda name, **kw: _CE()
    sys.modules["boto3"] = boto3

    botocore = types.ModuleType("botocore")
    exc = types.ModuleType("botocore.exceptions")
    exc.BotoCoreError = type("BotoCoreError", (Exception,), {})
    exc.ClientError = type("ClientError", (Exception,), {})
    botocore.exceptions = exc
    sys.modules["botocore"] = botocore
    sys.modules["botocore.exceptions"] = exc


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


def _run(coro):
    return asyncio.run(coro)


def _restore():
    svc.get_aws_mtd_cost = _ORIG_AWS
    svc.get_azure_mtd_cost = _ORIG_AZURE


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


def test_gcp_always_unavailable():
    async def aws(): return (1.0, "USD")
    async def azure(): return (2.0, "USD")
    svc.get_aws_mtd_cost, svc.get_azure_mtd_cost = aws, azure
    gcp = _by_cloud(_run(svc.get_cost_summary()))["gcp"]
    assert gcp["status"] == "unavailable"
    assert "BigQuery" in gcp["detail"]


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
