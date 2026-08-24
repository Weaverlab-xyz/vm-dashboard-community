"""Unit tests for cloud_database_service._build_tf_variables.

This is the pure -var builder behind cloud-DB provisioning: it maps an
(engine, cloud) pair + caller opts to the Terraform variable dict for that
engine module. It carries the cross-cloud special-casing that's easy to break
when a new engine/cloud lands (RDS SQL Server omits db_name and rejects micro
classes; Cloud SQL SQL Server forces the `sqlserver` login + a db-custom tier;
GCP uses tier/disk_size/labels while Azure uses sku_name/storage_mb/tags), so
it's worth pinning down without standing up Terraform or a cloud account.

The heavy app deps (web_dashboard.database → bcrypt, config_service, the
terraform helpers) are stubbed in sys.modules so the test needs only
SQLAlchemy. config lookups are routed through a controllable dict (`CONF`).
Runs under pytest, or standalone:  python tests/test_cloud_db_tf_vars.py
"""
import os
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# config_service.get() reads from this so tests can drive the _cfg fallbacks
# (e.g. gcp_project, the *-parameter-group keys) deterministically.
CONF = {}


class _Settings:
    """Stand-in for the pydantic Settings: any unknown key resolves to ""."""
    def __getattr__(self, _key):
        return ""


def _install_stubs():
    # Avoid importing the real config (pydantic) — the builder only reads config
    # through config_service / settings fallbacks, both stubbed here.
    confmod = types.ModuleType("web_dashboard.config")
    confmod.settings = _Settings()
    sys.modules["web_dashboard.config"] = confmod

    dbmod = types.ModuleType("web_dashboard.database")
    dbmod.CloudDatabase = type("CloudDatabase", (), {})
    dbmod.Job = type("Job", (), {})
    sys.modules["web_dashboard.database"] = dbmod

    cfg = types.ModuleType("web_dashboard.services.config_service")
    cfg.get = lambda key: CONF.get(key, "")
    cfg.set = lambda key, val: CONF.__setitem__(key, val)
    # The GCP branches read this too, to decide whether a new Cloud SQL instance comes
    # up with cloudsql.iam_authentication on (_gcp_iam_auth_wanted).
    cfg.get_bool = lambda key, default=False: bool(CONF.get(key, default))
    sys.modules["web_dashboard.services.config_service"] = cfg

    # cloud_database_service imports these at module load but the builder never
    # touches them — empty stand-ins keep the import light.
    for name in ("job_service", "terraform", "terraform_provider_env"):
        sys.modules[f"web_dashboard.services.{name}"] = types.ModuleType(
            f"web_dashboard.services.{name}")


_install_stubs()
try:
    from web_dashboard.services import cloud_database_service as svc
except Exception as exc:  # pragma: no cover — skip if other app deps are missing
    try:
        import pytest
        pytest.skip(f"cloud_database_service import unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)


def _build(engine, cloud, **over):
    args = dict(
        engine=engine, cloud=cloud, region="r1",
        db_id="abcdef0123456789", db_name="appdb",
        master_username="dbadmin", master_password="s3cr3t", opts={},
    )
    args.update(over)
    return svc._build_tf_variables(**args)


# ── identifier + common shape ────────────────────────────────────────────────

def test_identifier_is_clouddb_prefixed_first8_of_db_id():
    tf = _build("postgres", "aws")
    assert tf["identifier"] == "clouddb-abcdef01"  # first 8 chars only


def test_credentials_and_tags_passthrough():
    tf = _build("postgres", "aws")
    assert tf["master_username"] == "dbadmin"
    assert tf["master_password"] == "s3cr3t"
    assert tf["tags"] == {"managed-by": "vm-dashboard", "clouddb-id": "abcdef0123456789"}


# ── AWS branches ─────────────────────────────────────────────────────────────

def test_postgres_aws_defaults():
    tf = _build("postgres", "aws")
    assert tf["db_name"] == "appdb"
    assert tf["instance_class"] == "db.t3.micro"
    assert tf["allocated_storage"] == 20
    assert "tier" not in tf  # tier is a GCP var, not RDS


def test_postgres_aws_opts_override():
    tf = _build("postgres", "aws", opts={
        "instance_class": "db.m5.large", "allocated_storage": 100,
        "vpc_security_group_ids": ["sg-1", "sg-2"],
    })
    assert tf["instance_class"] == "db.m5.large"
    assert tf["allocated_storage"] == 100
    assert tf["vpc_security_group_ids"] == ["sg-1", "sg-2"]


def test_mysql_aws_parameter_group_from_config():
    CONF.clear()
    CONF["aws_db_mysql_parameter_group_name"] = "mysql80-nossl"
    try:
        tf = _build("mysql", "aws")
        assert tf["parameter_group_name"] == "mysql80-nossl"
        assert tf["db_name"] == "appdb"
    finally:
        CONF.clear()


def test_sqlserver_aws_omits_db_name():
    # RDS for SQL Server rejects db_name at creation — you connect to `master`.
    tf = _build("sqlserver", "aws")
    assert "db_name" not in tf


def test_sqlserver_aws_bumps_micro_class():
    # micro is too small for sqlserver-ex (needs >= 2 GiB) → coerced to small.
    assert _build("sqlserver", "aws", opts={"instance_class": "db.t3.micro"})["instance_class"] == "db.t3.small"
    # default (no class supplied) is already small
    assert _build("sqlserver", "aws")["instance_class"] == "db.t3.small"
    # a non-micro class is respected as-is
    assert _build("sqlserver", "aws", opts={"instance_class": "db.m5.large"})["instance_class"] == "db.m5.large"


# ── GCP branches ─────────────────────────────────────────────────────────────

def test_postgres_gcp_uses_tier_and_project():
    CONF.clear()
    CONF.update({"gcp_project": "proj-x", "gcp_network": "net-x"})
    try:
        tf = _build("postgres", "gcp")
        assert tf["project"] == "proj-x"
        assert tf["tier"] == "db-f1-micro"
        assert tf["disk_size"] == 20
        assert tf["private_network"] == "net-x"
        assert tf["labels"] == {"managed-by": "vm-dashboard", "clouddb-id": "abcdef0123456789"}
        assert "instance_class" not in tf  # instance_class is an RDS var
    finally:
        CONF.clear()


def test_gcp_iam_authentication_is_off_until_ps_onboarding_is_configured():
    # The flag only ever PERMITS IAM database auth, but it is still a change to the
    # instance, so a deployment that never onboards to Password Safe should not get it.
    CONF.clear()
    CONF["gcp_project"] = "proj-x"
    try:
        for engine in ("postgres", "mysql"):
            assert _build(engine, "gcp")["iam_authentication"] is False, engine
    finally:
        CONF.clear()


def test_gcp_iam_authentication_follows_both_switches():
    # Both are required: the master toggle AND the per-cloud method, matching
    # _ps_db_onboarding_enabled. The master toggle alone is shared with AWS/Azure.
    CONF.clear()
    CONF.update({"gcp_project": "proj-x", "clouddb_ps_onboarding_enabled": True})
    try:
        assert _build("postgres", "gcp")["iam_authentication"] is False, "method still off"
        CONF["passwordsafe_gcp_db_registration_method"] = "dataapi"
        for engine in ("postgres", "mysql"):
            assert _build(engine, "gcp")["iam_authentication"] is True, engine
    finally:
        CONF.clear()


def test_gcp_sqlserver_never_gets_the_iam_authentication_var():
    # Cloud SQL for SQL Server has no IAM database auth at all, and its module does not
    # declare the variable — passing it would fail the apply outright.
    CONF.clear()
    CONF.update({"gcp_project": "proj-x", "clouddb_ps_onboarding_enabled": True,
                 "passwordsafe_gcp_db_registration_method": "dataapi"})
    try:
        assert "iam_authentication" not in _build("sqlserver", "gcp")
    finally:
        CONF.clear()


def test_sqlserver_gcp_forces_login_and_custom_tier():
    CONF.clear()
    CONF["gcp_project"] = "proj-x"
    try:
        # Cloud SQL ignores any login but the built-in `sqlserver` account.
        tf = _build("sqlserver", "gcp", master_username="dbadmin")
        assert tf["master_username"] == "sqlserver"
        # A shared-core tier is rejected for SQL Server → coerced to db-custom.
        assert tf["tier"] == "db-custom-2-7680"
        # An explicit db-custom tier is respected.
        tf2 = _build("sqlserver", "gcp", opts={"tier": "db-custom-4-15360"})
        assert tf2["tier"] == "db-custom-4-15360"
    finally:
        CONF.clear()


# ── Azure branches ───────────────────────────────────────────────────────────

def test_postgres_azure_uses_sku_and_storage():
    tf = _build("postgres", "azure", opts={"resource_group_name": "rg-1"})
    assert tf["resource_group_name"] == "rg-1"
    assert tf["location"] == "r1"
    assert tf["administrator_login"] == "dbadmin"
    assert tf["sku_name"] == "B_Standard_B1ms"
    assert tf["storage_mb"] == 32768
    assert "instance_class" not in tf and "tier" not in tf


def test_azure_branches_read_engine_specific_subnet_keys():
    # Each Azure engine reads its own delegated-subnet key (a delegated subnet
    # hosts one flexible-server type), so a misrouted key is a real bug.
    CONF.clear()
    CONF.update({
        "azure_db_subnet_id": "subnet-pg",
        "azure_db_mysql_subnet_id": "subnet-mysql",
    })
    try:
        assert _build("postgres", "azure")["delegated_subnet_id"] == "subnet-pg"
        assert _build("mysql", "azure")["delegated_subnet_id"] == "subnet-mysql"
    finally:
        CONF.clear()


# ── multi-region: per-region subnet/network resolution (Phase 2b) ─────────────

def test_azure_db_uses_non_default_region_subnet_and_rg():
    import json
    CONF.clear()
    CONF.update({
        "azure_location": "centralus",                 # configured default region
        "azure_resource_group": "rg-default",
        "azure_db_subnet_id": "subnet-default-pg",     # flat = default region
        "azure_db_mysql_subnet_id": "subnet-default-my",
        "azure_region_configs": json.dumps({
            "westus2": {"db_subnet_id": "subnet-west-pg",
                        "db_mysql_subnet_id": "subnet-west-my",
                        "resource_group": "rg-west"},
        }),
    })
    try:
        pg = _build("postgres", "azure", region="westus2")
        assert pg["delegated_subnet_id"] == "subnet-west-pg"
        assert pg["resource_group_name"] == "rg-west"
        my = _build("mysql", "azure", region="westus2")
        assert my["delegated_subnet_id"] == "subnet-west-my"
        # The default region still resolves to the flat keys.
        assert _build("postgres", "azure", region="centralus")["delegated_subnet_id"] == "subnet-default-pg"
    finally:
        CONF.clear()


def test_azure_engine_specific_zones_and_pe_subnet_are_region_resolved():
    """MySQL's DNS zone and SQL Server's PE subnet + zone read the region set.

    All three used to be read flat, so a westus2 MySQL server got the centralus zone
    and a westus2 SQL database a private endpoint in a subnet of a VNet it is not in.
    Postgres was already region-resolved, which is what made the gap easy to miss.
    """
    import json
    CONF.clear()
    CONF.update({
        "azure_location": "centralus",
        "azure_db_mysql_private_dns_zone_id": "zone-default-my",
        "azure_db_sqlserver_subnet_id": "subnet-default-sql",
        "azure_db_sqlserver_private_dns_zone_id": "zone-default-sql",
        "azure_region_configs": json.dumps({
            "westus2": {
                "db_mysql_private_dns_zone_id": "zone-west-my",
                "db_sqlserver_subnet_id": "subnet-west-sql",
                "db_sqlserver_private_dns_zone_id": "zone-west-sql",
            },
        }),
    })
    try:
        assert _build("mysql", "azure", region="westus2")["private_dns_zone_id"] == "zone-west-my"
        sql = _build("sqlserver", "azure", region="westus2")
        assert sql["subnet_id"] == "subnet-west-sql"
        assert sql["private_dns_zone_id"] == "zone-west-sql"
        # The default region still resolves to the flat keys.
        assert _build("mysql", "azure", region="centralus")["private_dns_zone_id"] == "zone-default-my"
        default_sql = _build("sqlserver", "azure", region="centralus")
        assert default_sql["subnet_id"] == "subnet-default-sql"
        assert default_sql["private_dns_zone_id"] == "zone-default-sql"
    finally:
        CONF.clear()


def test_aws_db_uses_non_default_region_parameter_groups():
    """The cleartext parameter groups are per-region resources.

    The sandbox creates clouddb-nossl-pg16 / clouddb-nossl-mysql84 in every region it
    runs in and emits both per-region, but the builder read them flat — so a second
    region's RDS instance was handed the first region's group name. RDS rejects a
    parameter group from another region, and without one it falls back to the default
    group, which forces SSL and breaks the PRA protocol tunnel.
    """
    import json
    CONF.clear()
    CONF.update({
        "aws_region": "us-east-1",
        "aws_db_subnet_group_name": "grp-default",
        "aws_db_parameter_group_name": "pg-default",
        "aws_db_mysql_parameter_group_name": "my-default",
        "aws_region_configs": json.dumps({
            "us-west-2": {"db_subnet_group_name": "grp-west",
                          "db_parameter_group_name": "pg-west",
                          "db_mysql_parameter_group_name": "my-west"},
        }),
    })
    try:
        pg = _build("postgres", "aws", region="us-west-2")
        assert pg["parameter_group_name"] == "pg-west"
        assert pg["db_subnet_group_name"] == "grp-west"
        assert _build("mysql", "aws", region="us-west-2")["parameter_group_name"] == "my-west"
        # The default region still resolves to the flat keys.
        assert _build("postgres", "aws", region="us-east-1")["parameter_group_name"] == "pg-default"
        assert _build("mysql", "aws", region="us-east-1")["parameter_group_name"] == "my-default"
    finally:
        CONF.clear()


def test_aws_db_falls_back_to_configured_security_group():
    """No SG selected in the provision form → attach the sandbox's DB-tier group.

    The regression this pins: every AWS branch used to pass opts' list straight
    through, so an unattended provision got the VPC DEFAULT security group, which
    has no ingress on 5432/3306/1433 from the Gateway SG — the tunnel to the new
    database then times out. All three AWS engines share the fallback.
    """
    CONF.clear()
    CONF["aws_db_security_group_id"] = "sg-db"
    try:
        for engine in ("postgres", "mysql", "sqlserver"):
            assert _build(engine, "aws")["vpc_security_group_ids"] == ["sg-db"], engine
    finally:
        CONF.clear()


def test_aws_db_explicit_security_groups_win_over_config():
    CONF.clear()
    CONF["aws_db_security_group_id"] = "sg-db"
    try:
        for engine in ("postgres", "mysql", "sqlserver"):
            tf = _build(engine, "aws", opts={"vpc_security_group_ids": ["sg-1", "sg-2"]})
            assert tf["vpc_security_group_ids"] == ["sg-1", "sg-2"], engine
    finally:
        CONF.clear()


def test_aws_db_security_groups_empty_when_unconfigured():
    """No sandbox run → no configured SG → [] → the VPC default, as before. The
    fallback must not invent an empty-string SG id, which fails at apply."""
    CONF.clear()
    for engine in ("postgres", "mysql", "sqlserver"):
        assert _build(engine, "aws")["vpc_security_group_ids"] == [], engine
    # A blank selection is not a selection either — an empty-string SG id would
    # reach Terraform and fail at apply.
    assert _build("postgres", "aws", opts={"vpc_security_group_ids": [""]})[
        "vpc_security_group_ids"] == []
    # Defensive: the provision route strips None from opts (`if v is not None`), so
    # the key is absent in practice — but the builder must not trip if that changes.
    assert _build("postgres", "aws", opts={"vpc_security_group_ids": None})[
        "vpc_security_group_ids"] == []


def test_aws_db_uses_non_default_region_security_group():
    """The SG is VPC-scoped, so a non-default region must get ITS group, never the
    default region's (which does not exist in that VPC)."""
    import json
    CONF.clear()
    CONF.update({
        "aws_region": "us-east-1",                     # configured default region
        "aws_db_security_group_id": "sg-default",      # flat = default region
        "aws_region_configs": json.dumps({"us-west-2": {"db_security_group_id": "sg-west"}}),
    })
    try:
        assert _build("postgres", "aws", region="us-west-2")["vpc_security_group_ids"] == ["sg-west"]
        assert _build("mysql", "aws", region="us-west-2")["vpc_security_group_ids"] == ["sg-west"]
        # The default region still resolves to the flat key.
        assert _build("postgres", "aws", region="us-east-1")["vpc_security_group_ids"] == ["sg-default"]
    finally:
        CONF.clear()


def test_gcp_db_uses_non_default_region_network():
    import json
    CONF.clear()
    CONF.update({
        "gcp_project": "proj-x",
        "gcp_region": "us-central1",                   # configured default region
        "gcp_network": "net-default",                  # flat (secondary fallback)
        "gcp_region_configs": json.dumps({"europe-west1": {"db_network": "net-eu"}}),
    })
    try:
        assert _build("postgres", "gcp", region="europe-west1")["private_network"] == "net-eu"
        # Default region: db_network unset → secondary fallback to gcp_network.
        assert _build("postgres", "gcp", region="us-central1")["private_network"] == "net-default"
    finally:
        CONF.clear()


# ── guard ────────────────────────────────────────────────────────────────────

def test_unsupported_combo_raises_not_implemented():
    try:
        _build("mongodb", "aws")
    except NotImplementedError:
        return
    raise AssertionError("expected NotImplementedError for an unimplemented combo")


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
