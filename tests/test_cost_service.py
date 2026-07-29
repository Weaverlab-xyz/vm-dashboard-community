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
# Grouped fixtures for the scoped breakdown. AWS renders a TAG group key as
# "<key>$<value>", and the second GroupBy entry is the managed-by tag.
_AWS_CE_GROUPED = {"ResultsByTime": [{"Groups": [
    {"Keys": ["Amazon EC2", "managed-by$vm-dashboard"],
     "Metrics": {"UnblendedCost": {"Amount": "8.00", "Unit": "USD"}}},
    {"Keys": ["Amazon RDS", "managed-by$vm-dashboard"],
     "Metrics": {"UnblendedCost": {"Amount": "2.50", "Unit": "USD"}}},
    {"Keys": ["Amazon Virtual Private Cloud", "managed-by$dashboard-sandbox"],
     "Metrics": {"UnblendedCost": {"Amount": "7.20", "Unit": "USD"}}},
    {"Keys": ["AWS Secrets Manager", "managed-by$dashboard-sandbox"],
     "Metrics": {"UnblendedCost": {"Amount": "0.40", "Unit": "USD"}}},
]}]}
_AZURE_GROUPED_RESULT = {"properties": {
    "columns": [{"name": "Cost"}, {"name": "ServiceName"},
                {"name": "managed-by"}, {"name": "Currency"}],
    "rows": [[6.0, "Virtual Machines", "vm-dashboard", "USD"],
             [5.0, "Container Registry", "dashboard-sandbox", "USD"],
             [1.5, "Storage", "dashboard-sandbox", "USD"]],
}}
# GCP BigQuery billing-export fake rows (a Row supports r["col"]; dicts suffice).
# The sandbox row is deliberately UNDERSCORED — that's what setup-gcp.sh used to
# emit, and immutable billing history means those rows never go away.
_BQ_SUMMARY = [{"net": 42.5, "currency": "USD"}]
_BQ_GROUPED = [
    {"managed_by": "vm-dashboard", "service": "Compute Engine", "amount": 30.0, "currency": "USD"},
    {"managed_by": "dashboard_sandbox", "service": "Cloud Storage", "amount": 0.5, "currency": "USD"},
    {"managed_by": None, "service": "Networking", "amount": 1.5, "currency": "USD"},  # Cloud NAT
]
# OCI Usage API fake items (compartment-scoped; the API ignores freeform tags).
_OCI_SANDBOX_COMP = "ocid1.compartment.oc1..sandbox"
_OCI_ITEMS = [
    ("Database", _OCI_SANDBOX_COMP, 3.0),
    ("Compute", _OCI_SANDBOX_COMP, 1.5),
    ("Object Storage", "ocid1.compartment.oc1..other", 9.0),
]
# Every stubbed cloud call appends (label, kwargs) so request-shape can be asserted.
CALLS = []
# The sibling-service stubs, kept so they can be re-bound onto cost_service — see
# _bind_stub_siblings for why sys.modules alone isn't enough.
STUBS = {}
# Backs the stubbed config_service so tests can set/clear the export table.
CONF = {}


class _AWSError(Exception):
    pass


class _AzureError(Exception):
    pass


def _install_lazy_stubs():
    """Stub the SDKs cost_service imports LAZILY, inside functions (boto3,
    botocore.exceptions, google.cloud.bigquery, oci).

    These must be re-installable, not install-once: a lazy import re-reads sys.modules on
    every call, so any sibling test module that stubs boto3/bigquery/oci differently
    silently hijacks our queries when the whole suite runs in one process. `_restore()`
    calls this before each parsing test so the file behaves the same run alone or run as
    part of `pytest tests/`. (Module-scope imports — httpx, and the web_dashboard.services
    siblings — are bound into cost_service at import and can't be hijacked this way.)"""
    boto3 = types.ModuleType("boto3")

    class _CE:
        def get_cost_and_usage(self, **kw):
            # GroupBy present → the breakdown query; else the summary query.
            CALLS.append(("aws_breakdown" if "GroupBy" in kw else "aws_summary", kw))
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

    # google.cloud.bigquery — fake Client returning grouped/summary rows by SQL.
    class _QueryJob:
        def __init__(self, rows): self._rows = rows
        def result(self): return iter(self._rows)

    class _BQClient:
        def __init__(self, *a, **k): pass
        def query(self, sql, job_config=None):
            # The scoped breakdown groups by managed_by; the summary doesn't.
            grouped = "managed_by" in sql
            CALLS.append(("gcp_breakdown" if grouped else "gcp_summary", {"sql": sql}))
            return _QueryJob(_BQ_GROUPED if grouped else _BQ_SUMMARY)

    google = types.ModuleType("google")
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

    # oci SDK. cost_service also imports oci_service at module scope (stubbed in
    # _install_stubs) — without both, the OCI paths degrade in every test and any bug
    # in them stays invisible.
    oci = types.ModuleType("oci")
    usage_api = types.ModuleType("oci.usage_api")
    models = types.ModuleType("oci.usage_api.models")

    class _Details:
        def __init__(self, **kw):
            self.__dict__.update(kw)
            # Mirror the SDK: absent kwargs read as None, not AttributeError.
            for opt in ("filter", "group_by"):
                self.__dict__.setdefault(opt, None)

    class _Kw:
        def __init__(self, **kw): self.__dict__.update(kw)

    models.RequestSummarizedUsagesDetails = _Details
    models.Filter = _Kw
    models.Tag = _Kw
    models.Dimension = _Kw

    class _UsageClient:
        def __init__(self, *a, **k): pass

        def request_summarized_usages(self, details):
            CALLS.append(("oci_breakdown" if details.group_by else "oci_summary", details))
            if not details.group_by:  # summary: one aggregate bucket
                return types.SimpleNamespace(data=types.SimpleNamespace(items=[
                    types.SimpleNamespace(
                        service=None, compartment_id=None, currency="USD",
                        computed_amount=sum(a for _, _, a in _OCI_ITEMS))]))
            return types.SimpleNamespace(data=types.SimpleNamespace(items=[
                types.SimpleNamespace(service=s, compartment_id=c,
                                      computed_amount=amt, currency="USD")
                for s, c, amt in _OCI_ITEMS]))

    usage_api.UsageapiClient = _UsageClient
    usage_api.models = models
    oci.usage_api = usage_api
    sys.modules["oci"] = oci
    sys.modules["oci.usage_api"] = usage_api
    sys.modules["oci.usage_api.models"] = models


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
            CALLS.append(("azure_breakdown" if grouped else "azure_summary", k))
            return _Resp(_AZURE_GROUPED_RESULT if grouped else _AZURE_QUERY_RESULT)
    httpx.AsyncClient = _AsyncClient
    sys.modules["httpx"] = httpx

    _install_lazy_stubs()

    # gcp_service (creds + GCPError) + config_service (export-table key) + a light
    # web_dashboard.config so cost_service's lazy `from ..config import settings`
    # doesn't pull pydantic.
    gcp = types.ModuleType("web_dashboard.services.gcp_service")
    gcp.GCPError = type("GCPError", (Exception,), {})
    gcp._gcp_project = lambda: "proj"
    gcp._gcp_creds = lambda: None
    sys.modules["web_dashboard.services.gcp_service"] = gcp

    # oci_service + the `oci` SDK. cost_service imports oci_service at module scope, so
    # without these the OCI paths degrade in every test and their bugs stay invisible.
    ocis = types.ModuleType("web_dashboard.services.oci_service")
    ocis.OCIError = type("OCIError", (Exception,), {})
    ocis._oci_config = lambda: {}
    ocis._compartment = lambda: CONF.get("oci_compartment_ocid") or CONF.get("oci_tenancy_ocid", "")
    sys.modules["web_dashboard.services.oci_service"] = ocis

    cfg = types.ModuleType("web_dashboard.services.config_service")
    cfg.get = lambda key, default="": CONF.get(key, default)
    sys.modules["web_dashboard.services.config_service"] = cfg

    confmod = types.ModuleType("web_dashboard.config")
    confmod.settings = type("_S", (), {"__getattr__": lambda self, k: ""})()
    sys.modules["web_dashboard.config"] = confmod

    STUBS.update({"aws_service": aws, "azure_service": az, "gcp_service": gcp,
                  "oci_service": ocis, "config_service": cfg})


def _bind_stub_siblings(mod):
    """Point cost_service's sibling-module attributes at our stubs.

    cost_service does `from . import aws_service, ...` at module scope, which BINDS those
    module objects at import time. Stuffing sys.modules is therefore not enough: when the
    whole suite runs in one process, a sibling test module can import the real
    web_dashboard.services.cost_service before we install anything, and
    `from web_dashboard.services import cost_service` then hands us that already-imported
    module with the REAL aws_service attached (which raises "boto3 is not installed").
    Rebinding the attributes directly is import-order independent."""
    for name, stub in STUBS.items():
        if getattr(mod, name, None) is not stub:
            setattr(mod, name, stub)


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

_bind_stub_siblings(svc)


# Capture the real fetchers so the parsing tests are immune to the summary
# tests reassigning the module globals (pytest runs in definition order).
_ORIG_AWS = svc.get_aws_mtd_cost
_ORIG_AZURE = svc.get_azure_mtd_cost
_ORIG_AWS_BD = svc.get_aws_managed_breakdown
_ORIG_AZURE_BD = svc.get_azure_managed_breakdown
_ORIG_GCP = svc.get_gcp_mtd_cost
_ORIG_GCP_BD = svc.get_gcp_managed_breakdown
_ORIG_OCI = svc.get_oci_mtd_cost
_ORIG_OCI_BD = svc.get_oci_managed_breakdown

S_D, S_S, S_U = svc.SCOPE_DASHBOARD, svc.SCOPE_SANDBOX, svc.SCOPE_UNATTRIBUTED


def _run(coro):
    return asyncio.run(coro)


def _restore():
    # Re-assert the stubs: in a whole-suite run a sibling test module may have replaced
    # boto3/bigquery/oci in sys.modules, or rebound cost_service's sibling attributes,
    # since this file was imported. Both make the parsing tests query the real SDKs.
    _install_lazy_stubs()
    _bind_stub_siblings(svc)
    svc.get_aws_mtd_cost = _ORIG_AWS
    svc.get_azure_mtd_cost = _ORIG_AZURE
    svc.get_aws_managed_breakdown = _ORIG_AWS_BD
    svc.get_azure_managed_breakdown = _ORIG_AZURE_BD
    svc.get_gcp_mtd_cost = _ORIG_GCP
    svc.get_gcp_managed_breakdown = _ORIG_GCP_BD
    svc.get_oci_mtd_cost = _ORIG_OCI
    svc.get_oci_managed_breakdown = _ORIG_OCI_BD
    CALLS.clear()


def _last_call(label):
    """The most recent recorded call with this label (fails loudly if absent)."""
    for name, payload in reversed(CALLS):
        if name == label:
            return payload
    raise AssertionError(f"no {label} call recorded; got {[n for n, _ in CALLS]}")


def _svc_amounts(res, scope):
    return {s["service"]: s["amount"] for s in res["scopes"][scope]["services"]}


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


def test_azure_429_retries_once_and_recovers(monkeypatch=None):
    """Cost Management throttles at 429 with a Retry-After header. One 429 must not
    poison the tile — the helper must retry once (honoring Retry-After) and return
    the second response."""
    _restore()
    import httpx as _httpx

    class _Resp429:
        status_code = 429
        headers = {"Retry-After": "1"}
        def raise_for_status(self): pass
        def json(self): return {}

    class _Resp200:
        status_code = 200
        headers = {}
        def raise_for_status(self): pass
        def json(self): return _AZURE_QUERY_RESULT

    calls = {"n": 0}

    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **k):
            calls["n"] += 1
            return _Resp429() if calls["n"] == 1 else _Resp200()

    orig_client, orig_sleep = _httpx.AsyncClient, asyncio.sleep
    slept = []
    async def _fast_sleep(s): slept.append(s)
    _httpx.AsyncClient = _Client
    asyncio.sleep = _fast_sleep
    try:
        amount, currency = _run(svc.get_azure_mtd_cost())
    finally:
        _httpx.AsyncClient = orig_client
        asyncio.sleep = orig_sleep
    assert calls["n"] == 2                # first was 429, second succeeded
    assert slept == [1]                   # honored Retry-After: 1s
    assert round(amount, 3) == 123.456    # parsed the retry response


def test_azure_429_twice_raises_azure_error():
    """Two consecutive 429s exhaust the single retry — surface AzureError so the
    caller can degrade the tile, but only after a real second attempt was made."""
    _restore()
    import httpx as _httpx

    class _Resp429:
        status_code = 429
        headers = {}
        def raise_for_status(self):
            raise _httpx.HTTPError("429 Too Many Requests")
        def json(self): return {}

    calls = {"n": 0}

    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **k):
            calls["n"] += 1
            return _Resp429()

    orig_client, orig_sleep = _httpx.AsyncClient, asyncio.sleep
    async def _fast_sleep(_): pass
    _httpx.AsyncClient = _Client
    asyncio.sleep = _fast_sleep
    try:
        raised = False
        try:
            _run(svc.get_azure_mtd_cost())
        except svc.azure_service.AzureError as e:
            raised = True
            assert "Azure Cost Management query failed" in str(e)
    finally:
        _httpx.AsyncClient = orig_client
        asyncio.sleep = orig_sleep
    assert raised
    assert calls["n"] == 2  # exactly one retry, not an infinite loop


# ── scope plumbing (pure) ────────────────────────────────────────────────────

def test_scope_of_maps_hyphen_and_underscore_variants():
    """setup-gcp.sh used to underscore both key and value, and those rows live in
    immutable billing history — so both spellings must resolve forever."""
    assert svc._scope_of("vm-dashboard") == S_D
    assert svc._scope_of("vm_dashboard") == S_D
    assert svc._scope_of("dashboard-sandbox") == S_S
    assert svc._scope_of("dashboard_sandbox") == S_S
    assert svc._scope_of("  VM-Dashboard  ") == S_D  # trimmed + lowercased
    for junk in ("", None, "garbage", "   "):
        assert svc._scope_of(junk) == S_U, junk


def test_legacy_breakdown_result_defaults_to_dashboard_scope():
    """The flat-map shim keeps older callers working: everything is dashboard scope,
    and sandbox reports None (unknown) rather than 0.0 (measured zero)."""
    res = svc._breakdown_result({"Amazon EC2": 8.0, "Amazon RDS": 2.5}, "USD")
    assert res["dashboard_total"] == 10.5
    assert res["sandbox_total"] is None
    assert all(s["scope"] == S_D for s in res["services"])


def test_scoped_result_excludes_unattributed_from_total():
    """The double-count guard: unattributed spend is reported separately, never folded
    into `total` — otherwise it would be counted twice against the account-total card."""
    res = svc._scoped_breakdown_result(
        {(S_D, "EC2"): 10.5, (S_S, "Secrets Manager"): 0.4, (S_U, "Data Transfer"): 3.0},
        "USD", measured=svc._SCOPES)
    assert res["dashboard_total"] == 10.5 and res["sandbox_total"] == 0.4
    assert res["unattributed_total"] == 3.0
    assert res["total"] == 10.9
    assert [s["service"] for s in res["services"]] == ["EC2", "Secrets Manager"]


def test_unmeasured_scope_is_none_not_zero():
    """None ("can't see it") and 0.0 ("looked, found nothing") must stay distinct."""
    res = svc._scoped_breakdown_result({}, "USD", measured=(S_S,))
    assert res["sandbox_total"] == 0.0
    assert res["dashboard_total"] is None


def test_breakdown_cache_key_is_versioned_and_has_a_ttl():
    """The payload shape changed, so the key must not collide with pre-upgrade entries —
    and it needs a TTL entry, or get_or_refresh KeyErrors on every request.

    Read cache_service as TEXT rather than importing it: sibling test modules stub
    web_dashboard.services.cache_service in sys.modules, which would make an import here
    assert against a fake TTL dict."""
    assert svc.CACHE_KEY_BREAKDOWN != "cost_breakdown"
    path = os.path.join(_ROOT, "web_dashboard", "services", "cache_service.py")
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    assert f'"{svc.CACHE_KEY_BREAKDOWN}"' in src, (
        f"{svc.CACHE_KEY_BREAKDOWN} has no TTL entry in cache_service.TTL")


# ── managed breakdown: parsing ───────────────────────────────────────────────

def test_aws_breakdown_splits_dashboard_and_sandbox():
    _restore()
    res = _run(svc.get_aws_managed_breakdown())  # stubbed boto3 → _AWS_CE_GROUPED
    assert res["dashboard_total"] == 10.5   # EC2 8.00 + RDS 2.50
    assert res["sandbox_total"] == 7.6      # VPC 7.20 + Secrets Manager 0.40
    assert res["total"] == 18.1
    assert res["currency"] == "USD"
    assert _svc_amounts(res, S_S) == {"Amazon Virtual Private Cloud": 7.2,
                                      "AWS Secrets Manager": 0.4}
    assert all(s.get("scope") for s in res["services"])
    # amount-desc across both scopes
    assert [s["amount"] for s in res["services"]] == [8.0, 7.2, 2.5, 0.4]


def test_aws_breakdown_request_filters_both_values_with_two_groupbys():
    _restore()
    _run(svc.get_aws_managed_breakdown())
    kw = _last_call("aws_breakdown")
    assert kw["Filter"]["Tags"]["Values"] == ["vm-dashboard", "dashboard-sandbox"]
    assert kw["Filter"]["Tags"]["MatchOptions"] == ["EQUALS"]
    # Cost Explorer caps GroupBy at 2 — pin it so nobody adds a third and gets a 400.
    assert len(kw["GroupBy"]) == 2
    assert kw["GroupBy"][1] == {"Type": "TAG", "Key": "managed-by"}
    # Cost Explorer bills ~$0.01/request: both scopes must come from ONE call.
    assert len([n for n, _ in CALLS if n == "aws_breakdown"]) == 1


def test_aws_breakdown_unattributed_is_none():
    """A tag-filtered query structurally cannot see untagged spend, so the remainder is
    unknown — not zero. The template derives it from the account total instead."""
    _restore()
    assert _run(svc.get_aws_managed_breakdown())["unattributed_total"] is None


def test_aws_breakdown_malformed_tag_key_is_unattributed():
    _restore()
    import boto3 as _boto3
    orig = _boto3.client

    class _CE1:
        def get_cost_and_usage(self, **kw):
            return {"ResultsByTime": [{"Groups": [
                {"Keys": ["Amazon EC2"],  # no tag group key at all
                 "Metrics": {"UnblendedCost": {"Amount": "5.00", "Unit": "USD"}}},
                {"Keys": ["Amazon S3", "managed-by$"],  # tag present but empty
                 "Metrics": {"UnblendedCost": {"Amount": "1.00", "Unit": "USD"}}},
            ]}]}
    _boto3.client = lambda name, **kw: _CE1()
    try:
        res = _run(svc.get_aws_managed_breakdown())
    finally:
        _boto3.client = orig
    assert res["dashboard_total"] == 0.0 and res["sandbox_total"] == 0.0
    assert res["services"] == []


def test_azure_breakdown_splits_scopes_from_tag_column():
    _restore()
    res = _run(svc.get_azure_managed_breakdown())  # stubbed httpx → _AZURE_GROUPED_RESULT
    assert res["dashboard_total"] == 6.0
    assert res["sandbox_total"] == 6.5   # ACR 5.0 + Storage 1.5
    assert _svc_amounts(res, S_D) == {"Virtual Machines": 6.0}


def test_azure_breakdown_request_has_two_groupings_and_both_values():
    _restore()
    _run(svc.get_azure_managed_breakdown())
    ds = _last_call("azure_breakdown")["json"]["dataset"]
    assert ds["filter"]["tags"]["values"] == ["vm-dashboard", "dashboard-sandbox"]
    assert ds["filter"]["tags"]["operator"] == "In"
    # Cost Management also caps grouping at 2 — ServiceName + the scope tag.
    assert len(ds["grouping"]) == 2
    assert ds["grouping"][1] == {"type": "TagKey", "name": "managed-by"}


def test_azure_tag_column_naming_variants():
    """Cost Management names the tag-VALUE column inconsistently across API versions.
    A TagKey/TagValue pair must resolve to the VALUE, not the key."""
    assert svc._azure_tag_col(["Cost", "ServiceName", "managed-by", "Currency"]) == 2
    assert svc._azure_tag_col(["Cost", "ServiceName", "TagValue", "Currency"]) == 2
    assert svc._azure_tag_col(["Cost", "ServiceName", "TagKey", "TagValue", "Currency"]) == 3
    assert svc._azure_tag_col(["Cost", "ServiceName", "Odd", "Currency"]) == 2  # lone unknown
    assert svc._azure_tag_col(["Cost", "ServiceName", "Currency"]) is None


def test_azure_breakdown_unresolvable_tag_column_degrades_to_unattributed():
    """Guessing wrong must be VISIBLE on the page, not silently wrong arithmetic."""
    _restore()
    import httpx as _httpx
    orig = _httpx.AsyncClient

    class _Resp:
        def raise_for_status(self): pass
        def json(self):
            return {"properties": {
                "columns": [{"name": "Cost"}, {"name": "ServiceName"}, {"name": "Currency"}],
                "rows": [[4.0, "Virtual Machines", "USD"]]}}

    class _C:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **k): return _Resp()
    _httpx.AsyncClient = _C
    try:
        res = _run(svc.get_azure_managed_breakdown())
    finally:
        _httpx.AsyncClient = orig
    assert res["dashboard_total"] == 0.0 and res["sandbox_total"] == 0.0
    assert res["scopes"][S_U]["services"][0]["amount"] == 4.0


def test_azure_breakdown_notes_mention_tag_inheritance():
    """The non-inheritance caveat must reach the UI — it's the documented fix."""
    _restore()
    notes = " ".join(_run(svc.get_azure_managed_breakdown())["notes"]).lower()
    assert "inherit" in notes


def test_gcp_breakdown_splits_scopes_and_accepts_underscore_labels():
    _restore()
    CONF["gcp_billing_export_table"] = "proj.ds.gcp_billing_export_v1_X"
    try:
        res = _run(svc.get_gcp_managed_breakdown())
    finally:
        CONF.pop("gcp_billing_export_table", None)
    assert res["dashboard_total"] == 30.0
    assert res["sandbox_total"] == 0.5          # from the UNDERSCORED label
    # The GCP query is unfiltered, so the remainder is genuinely measured — this is
    # where unlabellable Cloud Router/NAT spend surfaces.
    assert res["unattributed_total"] == 1.5


def test_gcp_sql_accepts_both_label_keys_and_project_labels():
    """Regression guard for a future 'cleanup' that drops the underscore variants or
    the project-label fallback — both are load-bearing, not migration cruft."""
    _restore()
    CONF["gcp_billing_export_table"] = "proj.ds.gcp_billing_export_v1_X"
    try:
        _run(svc.get_gcp_managed_breakdown())
    finally:
        CONF.pop("gcp_billing_export_table", None)
    sql = _last_call("gcp_breakdown")["sql"]
    assert "'managed-by'" in sql and "'managed_by'" in sql
    assert "UNNEST(labels)" in sql and "UNNEST(project.labels)" in sql


def test_oci_breakdown_scopes_sandbox_compartment():
    _restore()
    CONF.update({"oci_tenancy_ocid": "ocid1.tenancy.oc1..t",
                 "oci_compartment_ocid": _OCI_SANDBOX_COMP})
    try:
        res = _run(svc.get_oci_managed_breakdown())
    finally:
        CONF.pop("oci_tenancy_ocid", None)
        CONF.pop("oci_compartment_ocid", None)
    assert res["sandbox_total"] == 4.5          # Database 3.0 + Compute 1.5
    assert res["unattributed_total"] == 9.0     # the other compartment
    # OCI cannot distinguish dashboard-provisioned resources from the baseline.
    assert res["dashboard_total"] is None


def test_oci_breakdown_sends_no_tag_filter():
    """The load-bearing guard: the Usage API only reports cost-tracking (defined) tags
    and the sandbox writes a FREEFORM tag, so the old tags= filter could never match
    anything. Compartment grouping is the only scope OCI billing can express."""
    _restore()
    CONF.update({"oci_tenancy_ocid": "ocid1.tenancy.oc1..t",
                 "oci_compartment_ocid": _OCI_SANDBOX_COMP})
    try:
        _run(svc.get_oci_managed_breakdown())
    finally:
        CONF.pop("oci_tenancy_ocid", None)
        CONF.pop("oci_compartment_ocid", None)
    details = _last_call("oci_breakdown")
    assert details.filter is None
    assert details.group_by == ["compartmentId", "service"]


def test_oci_breakdown_root_compartment_is_all_unattributed():
    """oci_service._compartment() falls back to the tenancy when the compartment is
    unset — taking that fallback here would label the WHOLE TENANCY 'sandbox'."""
    _restore()
    CONF["oci_tenancy_ocid"] = "ocid1.tenancy.oc1..t"
    CONF.pop("oci_compartment_ocid", None)
    try:
        res = _run(svc.get_oci_managed_breakdown())
    finally:
        CONF.pop("oci_tenancy_ocid", None)
    assert res["sandbox_total"] is None
    assert res["unattributed_total"] == 13.5    # everything
    assert any("compartment" in n.lower() for n in res["notes"])


# ── managed breakdown: orchestration ─────────────────────────────────────────

def _bd(total, currency="USD", services=None, **scopes):
    """A fake per-cloud breakdown. Called WITHOUT scope kwargs it returns the OLD
    payload shape on purpose — get_cost_breakdown must tolerate that."""
    async def _f():
        return {"total": total, "currency": currency, "services": services or [], **scopes}
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
        # 30.0 dashboard + 0.5 sandbox; the 1.5 unlabelled row is unattributed and is
        # deliberately NOT in `total` or the flat services list.
        assert res["total"] == 30.5
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


# ── cross-cloud scope totals ─────────────────────────────────────────────────

def test_breakdown_grand_totals_split_by_scope():
    svc.get_aws_managed_breakdown = _bd(
        18.1, dashboard_total=10.5, sandbox_total=7.6, unattributed_total=None)
    svc.get_azure_managed_breakdown = _bd(
        12.5, dashboard_total=6.0, sandbox_total=6.5, unattributed_total=None)
    try:
        out = _run(svc.get_cost_breakdown())
        assert out["grand_total"] == 30.6
        assert out["dashboard_total"] == 16.5
        assert out["sandbox_total"] == 14.1
        # No cloud measured a remainder → None (unknown), not 0.0.
        assert out["unattributed_total"] is None
    finally:
        _restore()


def test_breakdown_tolerates_entries_without_scope_keys():
    """_bd() without scope kwargs returns the pre-change payload shape. The orchestrator
    must not KeyError on it — that's what keeps _sum_scope using .get()."""
    svc.get_aws_managed_breakdown = _bd(10.0)
    svc.get_azure_managed_breakdown = _bd(5.0)
    try:
        out = _run(svc.get_cost_breakdown())
        assert out["grand_total"] == 15.0
        assert out["dashboard_total"] is None and out["sandbox_total"] is None
    finally:
        _restore()


def test_breakdown_grand_total_skips_clouds_that_measured_nothing():
    """`total` is None for a cloud that couldn't measure either scope, so the sum has to
    filter those out rather than crash on None."""
    svc.get_aws_managed_breakdown = _bd(None, dashboard_total=None, sandbox_total=None)
    svc.get_azure_managed_breakdown = _bd(5.0, dashboard_total=5.0, sandbox_total=0.0)
    try:
        out = _run(svc.get_cost_breakdown())
        assert out["grand_total"] == 5.0
        assert out["sandbox_total"] == 0.0   # measured zero survives as 0.0
    finally:
        _restore()


def test_unavailable_entry_exposes_every_scope_key():
    """A degraded cloud must still carry the keys the template reads, or Alpine renders
    `undefined` in the scope subtotals."""
    async def boom(): raise svc.aws_service.AWSError("no creds")
    svc.get_aws_managed_breakdown = boom
    svc.get_azure_managed_breakdown = _bd(1.0)
    try:
        aws = {c["cloud"]: c for c in _run(svc.get_cost_breakdown())["clouds"]}["aws"]
    finally:
        _restore()
    for key in ("total", "dashboard_total", "sandbox_total", "unattributed_total",
                "currency", "services", "scopes", "scope_basis", "notes"):
        assert key in aws, key
    assert set(aws["scopes"]) == set(svc._SCOPES)
    assert aws["notes"] == []


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
