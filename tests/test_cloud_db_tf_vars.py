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
