"""Unit tests for the SQL Server offering discriminator in cloud_database_service
(PR: scaffold Entitle-compatible SQL Server — AWS RDS Custom / Azure SQL MI).

Covers the routing that opts["sqlserver_tier"] drives:
- _resolve_provider: the recorded provider (default flavors unchanged; the two
  Entitle-compatible tiers map to distinct providers; a bad (cloud, tier) raises);
- _module_dir: provider -> terraform module dir (defaults via the (engine, cloud) map,
  the new offerings via their own dirs);
- _tier_for_provider: provider -> tier round-trip used by the decommission rebuild;
- _build_tf_variables: the new tiers emit the module's variable set, and the default
  SQL Server path is untouched.

Run where the app deps exist (inside the container):
    docker compose run --rm worker python tests/test_sqlserver_offerings.py
Also runs under pytest.
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from web_dashboard.services import cloud_database_service as svc  # noqa: E402


def test_resolve_provider_defaults_unchanged():
    # Every implemented (engine, cloud) keeps its historical provider; an explicit
    # "standard" tier is a no-op.
    for (engine, cloud), provider in svc._PROVIDER.items():
        assert svc._resolve_provider(engine, cloud, {}) == provider
        assert svc._resolve_provider(engine, cloud, {"sqlserver_tier": "standard"}) == provider


def test_resolve_provider_entitle_tiers():
    assert svc._resolve_provider("sqlserver", "aws", {"sqlserver_tier": "rds_custom"}) == "rds_custom"
    assert svc._resolve_provider("sqlserver", "azure", {"sqlserver_tier": "managed_instance"}) == "sql_managed_instance"


def test_resolve_provider_bad_tier_raises():
    # Wrong cloud for the tier, and a cloud (gcp) with no Entitle-compatible offering.
    for cloud, tier in (("aws", "managed_instance"), ("azure", "rds_custom"), ("gcp", "managed_instance")):
        try:
            svc._resolve_provider("sqlserver", cloud, {"sqlserver_tier": tier})
            raise AssertionError(f"expected CloudDatabaseError for {cloud}/{tier}")
        except svc.CloudDatabaseError:
            pass
    # A non-SQL-Server engine ignores the tier entirely.
    assert svc._resolve_provider("postgres", "aws", {"sqlserver_tier": "rds_custom"}) == "rds"


def test_module_dir_routing():
    # Defaults resolve through the (engine, cloud) map.
    assert svc._module_dir("sqlserver", "aws", "rds") == svc._TEMPLATE_DIRS[("sqlserver", "aws")]
    assert svc._module_dir("sqlserver", "gcp", "cloudsql") == svc._TEMPLATE_DIRS[("sqlserver", "gcp")]
    # The Entitle-compatible offerings resolve to their own modules by provider.
    assert svc._module_dir("sqlserver", "aws", "rds_custom").endswith("db_aws_sqlserver_custom")
    assert svc._module_dir("sqlserver", "azure", "sql_managed_instance").endswith("db_azure_sqlserver_mi")


def test_tier_for_provider_roundtrip():
    assert svc._tier_for_provider("rds_custom") == "rds_custom"
    assert svc._tier_for_provider("sql_managed_instance") == "managed_instance"
    for p in ("rds", "cloudsql", "sql_database", "flexibleserver", None, ""):
        assert svc._tier_for_provider(p) == "standard"


def _patch_region(monkey_dict):
    """Replace svc.resolve_region with a stub so _build_tf_variables doesn't depend on
    live region config. Returns the original for restore."""
    original = svc.resolve_region
    svc.resolve_region = lambda cloud, region: monkey_dict
    return original


def test_build_tf_variables_rds_custom():
    original = _patch_region({"db_subnet_group_name": "sg-x"})
    try:
        v = svc._build_tf_variables(
            engine="sqlserver", cloud="aws", region="us-east-1", db_id="abcd1234",
            db_name="appdb", master_username="dbadmin", master_password="pw",
            opts={"sqlserver_tier": "rds_custom", "engine_version": "15.00.cev-1",
                  "custom_iam_instance_profile": "rds-custom-profile",
                  "kms_key_id": "arn:aws:kms:...:key/abc", "db_subnet_group_name": "sg-x"},
        )
    finally:
        svc.resolve_region = original
    assert v["engine_version"] == "15.00.cev-1"
    assert v["custom_iam_instance_profile"] == "rds-custom-profile"
    assert v["kms_key_id"].startswith("arn:aws:kms:")
    assert v["master_username"] == "dbadmin"
    # RDS Custom, like standard RDS SQL Server, has NO db_name (connect to master).
    assert "db_name" not in v


def test_build_tf_variables_managed_instance():
    original = _patch_region({"resource_group": "rg-x"})
    try:
        v = svc._build_tf_variables(
            engine="sqlserver", cloud="azure", region="eastus", db_id="abcd1234",
            db_name="appdb", master_username="dbadmin", master_password="pw",
            opts={"sqlserver_tier": "managed_instance", "vcores": 8,
                  "storage_size_in_gb": 64, "subnet_id": "/subscriptions/.../subnets/mi"},
        )
    finally:
        svc.resolve_region = original
    assert v["administrator_login"] == "dbadmin"
    assert v["sku_name"] == "GP_Gen5"
    assert v["vcores"] == 8 and v["storage_size_in_gb"] == 64
    assert v["subnet_id"].endswith("/subnets/mi")


def test_build_tf_variables_default_sqlserver_unchanged():
    # Absent tier => the historical RDS-standard SQL Server var set (no Custom keys).
    original = _patch_region({"db_subnet_group_name": "sg-x"})
    try:
        v = svc._build_tf_variables(
            engine="sqlserver", cloud="aws", region="us-east-1", db_id="abcd1234",
            db_name="appdb", master_username="dbadmin", master_password="pw", opts={},
        )
    finally:
        svc.resolve_region = original
    assert "custom_iam_instance_profile" not in v and "kms_key_id" not in v
    assert v["instance_class"] == "db.t3.small"   # the RDS-standard SQL Server default


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
