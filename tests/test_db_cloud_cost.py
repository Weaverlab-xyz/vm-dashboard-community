"""Managed-database list prices (services/db_cloud_cost).

Each cloud's price list is read through a parser held in place here against a response
shaped like the real one, since a test cannot call the live API:

  * **RDS:** the instance class and General Purpose storage, Single-AZ, per engine.
  * **Cloud SQL:** a shared-core tier is one SKU; any other is vCPUs + memory read from
    its name; zonal on-demand only, in the database's region.
  * **Azure flexible server:** Burstable by size, General Purpose by vCore of its series,
    storage per GB-month.
  * **Not priced:** SQL Server, Oracle and the rest, an unknown tier, a missing price.

Runs under pytest, or standalone:
    python tests/test_db_cloud_cost.py
"""
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from web_dashboard.services import db_cloud_cost as dbc  # noqa: E402
from web_dashboard.services import pov_cloud_cost as pcc  # noqa: E402

H = dbc.HOURS_PER_MONTH


def _close(a, b):
    return a is not None and abs(a - b) < 1e-6


# ── AWS ──────────────────────────────────────────────────────────────────────

def _price_list(usd):
    return {"PriceList": [json.dumps({"terms": {"OnDemand": {"x": {"priceDimensions": {
        "y": {"pricePerUnit": {"USD": str(usd)}}}}}}})]}


class _RDS:
    def __init__(self):
        self.calls = []

    def get_products(self, **kw):
        self.calls.append(kw)
        families = {f["Field"]: f["Value"] for f in kw["Filters"]}
        if families.get("productFamily") == "Database Storage":
            return _price_list(0.115)
        return _price_list(0.017 if families.get("instanceType") == "db.t3.micro" else 0)


def test_rds_is_instance_plus_storage_for_the_engine():
    saved = pcc._client
    fake = _RDS()
    pcc._client = lambda: fake
    pcc._prices.clear()
    try:
        r = dbc.rates("aws", "us-east-1", {"engine": "postgres",
                                           "instance_class": "db.t3.micro",
                                           "allocated_storage": 20})
    finally:
        pcc._client = saved
    assert r and _close(r[0], 0.017) and _close(r[1], 0.115 * 20 / H), r
    filters = [{f["Field"]: f["Value"] for f in c["Filters"]} for c in fake.calls]
    assert all(f["databaseEngine"] == "PostgreSQL" and f["deploymentOption"] == "Single-AZ"
               for f in filters), filters


def test_an_unknown_rds_class_is_unpriced():
    saved = pcc._client
    pcc._client = lambda: _RDS()
    pcc._prices.clear()
    try:
        assert dbc.rates("aws", "us-east-1", {"engine": "mysql",
                                              "instance_class": "db.x99.huge"}) is None
    finally:
        pcc._client = saved


# ── GCP ──────────────────────────────────────────────────────────────────────

def _sku(desc, usd, region="us-central1", usage="OnDemand"):
    units, nanos = int(usd), int(round((usd - int(usd)) * 1e9))
    return {"description": desc, "category": {"usageType": usage},
            "serviceRegions": [region],
            "pricingInfo": [{"pricingExpression": {"tieredRates": [
                {"unitPrice": {"units": str(units), "nanos": nanos}}]}}]}


SKUS = [
    _sku("Cloud SQL for PostgreSQL: Zonal - vCPU in Americas", 0.0413),
    _sku("Cloud SQL for PostgreSQL: Zonal - RAM in Americas", 0.007),
    _sku("Cloud SQL for PostgreSQL: Zonal - Standard storage in Americas", 0.17),
    _sku("Cloud SQL for PostgreSQL: Zonal - Micro instance in Americas", 0.0105),
    _sku("Cloud SQL for PostgreSQL: Regional - vCPU in Americas", 0.0826),
    _sku("Cloud SQL for PostgreSQL: Zonal - vCPU in Americas", 0.02, usage="Commit1Yr"),
    _sku("Cloud SQL for MySQL: Zonal - vCPU in Americas", 0.0413, region="europe-west1"),
]


def test_cloudsql_tiers_are_read_from_their_names():
    assert dbc.cloudsql_shape("db-f1-micro") == ("shared", "micro")
    assert dbc.cloudsql_shape("db-g1-small") == ("shared", "small")
    assert dbc.cloudsql_shape("db-custom-2-7680") == ("dedicated", 2, 7.5)
    assert dbc.cloudsql_shape("db-n1-standard-4") == ("dedicated", 4, 15.0)
    assert dbc.cloudsql_shape("db-n1-highmem-2") == ("dedicated", 2, 13.0)
    assert dbc.cloudsql_shape("db-perf-optimized-N-8") is None


def test_cloudsql_index_keeps_zonal_on_demand_in_region_only():
    index = dbc.cloudsql_index(SKUS, "us-central1")
    pg = index["postgres"]
    assert _close(pg["vcpu"], 0.0413), "a regional or committed SKU won"
    assert _close(pg["ram"], 0.007) and _close(pg["storage"], 0.17)
    assert _close(pg["micro"], 0.0105)
    assert "mysql" not in index, "another region's SKU was indexed"


def test_cloudsql_price_is_vcpus_plus_memory_plus_disk():
    saved = dbc._cloudsql_catalog
    dbc._cloudsql_catalog = lambda region: dbc.cloudsql_index(SKUS, region)
    try:
        r = dbc.rates("gcp", "us-central1", {"engine": "postgres",
                                             "tier": "db-custom-2-7680", "disk_size": 20})
        assert r and _close(r[0], 2 * 0.0413 + 7.5 * 0.007), r
        assert _close(r[1], 0.17 * 20 / H)
        micro = dbc.rates("gcp", "us-central1", {"engine": "postgres", "tier": "db-f1-micro"})
        assert micro and _close(micro[0], 0.0105)
        assert dbc.rates("gcp", "us-central1", {"engine": "postgres",
                                                "tier": "db-g1-small"}) is None, (
            "a tier with no SKU in the catalogue read as priced")
    finally:
        dbc._cloudsql_catalog = saved


# ── Azure ────────────────────────────────────────────────────────────────────

ITEMS = [
    {"productName": "Azure Database for PostgreSQL Flexible Server Burstable BS Series Compute",
     "skuName": "B1MS", "meterName": "B1MS", "unitOfMeasure": "1 Hour", "retailPrice": 0.0207},
    {"productName": "Azure Database for PostgreSQL Flexible Server General Purpose Ddsv4 Series Compute",
     "skuName": "vCore", "meterName": "vCore", "unitOfMeasure": "1 Hour", "retailPrice": 0.0885},
    {"productName": "Azure Database for PostgreSQL Flexible Server Storage",
     "skuName": "Storage", "meterName": "Storage Data Stored", "unitOfMeasure": "1 GB/Month",
     "retailPrice": 0.115},
    {"productName": "Azure Database for PostgreSQL Single Server Basic Compute",
     "skuName": "B1MS", "meterName": "B1MS", "unitOfMeasure": "1 Hour", "retailPrice": 9.99},
]


def test_azure_sizes_are_read_from_the_sku_name():
    assert dbc.azure_size("B_Standard_B1ms") == ("burstable", "b1ms")
    assert dbc.azure_size("GP_Standard_D2ds_v4") == ("vcore", "ddsv4", 2)
    assert dbc.azure_size("MO_Standard_E4ds_v5") == ("vcore", "edsv5", 4)
    assert dbc.azure_size("nonsense") is None


def test_azure_burstable_and_vcore_pricing():
    compute, per_gb = dbc.azure_pick(ITEMS, "postgres", "B_Standard_B1ms")
    assert _close(compute, 0.0207) and _close(per_gb, 0.115), "a single-server SKU won"
    compute, _ = dbc.azure_pick(ITEMS, "postgres", "GP_Standard_D2ds_v4")
    assert _close(compute, 2 * 0.0885)
    compute, _ = dbc.azure_pick(ITEMS, "postgres", "GP_Standard_D4s_v3")
    assert compute is None, "a series with no price read as priced"


def test_azure_rates_include_storage_by_size():
    saved = pcc._azure_query
    pcc._azure_query = lambda f: ITEMS
    pcc._prices.clear()
    try:
        r = dbc.rates("azure", "eastus", {"engine": "postgres", "sku_name": "B_Standard_B1ms",
                                          "storage_mb": 65536})
    finally:
        pcc._azure_query = saved
    assert r and _close(r[0], 0.0207) and _close(r[1], 0.115 * 64 / H), r


# ── not priced ───────────────────────────────────────────────────────────────

def test_other_engines_and_clouds_are_not_priced():
    assert dbc.rates("aws", "us-east-1", {"engine": "sqlserver"}) is None
    assert dbc.rates("aws", "us-east-1", {"engine": "oracle"}) is None
    assert dbc.rates("oci", "us-ashburn-1", {"engine": "postgres"}) is None
    assert dbc.rates("aws", "", {"engine": "postgres"}) is None
    assert "PostgreSQL and MySQL" in dbc.engine_reason("sqlserver")


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"ok   {name}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
