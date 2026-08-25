"""Unit tests for cloud_database_service._distill_provision_failure.

A create-time capacity stockout (Azure CapacityNotAvailable after ~18 min of
create polling, AWS InsufficientDBInstanceCapacity) raises a TerraformError
whose text is the entire streamed apply — plan, hundreds of "Still creating…"
lines, and the one actionable error at the very bottom. The failed-job detail
shows error_message ONLY, so run_provision_apply must distill that class of
failure into a short cause + remedy (the raw output stays in the job's Live
Output). Anything unrecognized must pass through verbatim.

Same stubbing approach as test_cloud_db_tf_vars.py: heavy app deps are stood in
via sys.modules so the test needs only SQLAlchemy. Runs under pytest, or
standalone:  python tests/test_clouddb_capacity_error.py
"""
import os
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


class _Settings:
    def __getattr__(self, _key):
        return ""


class _TerraformError(Exception):
    """Mirror of terraform.TerraformError, installed on the stub module — the
    distiller's isinstance gate must see the same class it raises with."""


def _install_stubs():
    confmod = types.ModuleType("web_dashboard.config")
    confmod.settings = _Settings()
    sys.modules["web_dashboard.config"] = confmod

    dbmod = types.ModuleType("web_dashboard.database")
    dbmod.CloudDatabase = type("CloudDatabase", (), {})
    dbmod.Job = type("Job", (), {})
    sys.modules["web_dashboard.database"] = dbmod

    cfg = types.ModuleType("web_dashboard.services.config_service")
    cfg.get = lambda key: ""
    cfg.get_bool = lambda key, default=False: default
    sys.modules["web_dashboard.services.config_service"] = cfg

    tf = types.ModuleType("web_dashboard.services.terraform")
    tf.TerraformError = _TerraformError
    sys.modules["web_dashboard.services.terraform"] = tf

    for name in ("job_service", "terraform_provider_env"):
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


def _row(cloud, region):
    return types.SimpleNamespace(cloud=cloud, region=region)


# Trimmed from a real failed clouddb_provision job: the plan + polling wall with
# the capacity error at the bottom, exactly as terraform.apply raises it.
_AZURE_WALL = _TerraformError(
    "terraform apply failed:\n"
    "Terraform will perform the following actions:\n"
    '  # azurerm_postgresql_flexible_server.this will be created\n'
    "azurerm_postgresql_flexible_server.this: Still creating... [10s elapsed]\n"
    "azurerm_postgresql_flexible_server.this: Still creating... [17m0s elapsed]\n"
    'Error: creating Flexible Server (Subscription: "c399a68c")\n'
    'Flexible Server Name: "clouddb-00026884"): polling after Create: polling '
    "failed: the Azure API returned the following error:\n"
    'Status: "CapacityNotAvailable"\n'
    "Message: Capacity is not available in this region/zone. Please retry after some time.\n"
)


def test_azure_capacity_stockout_is_distilled():
    msg = svc._distill_provision_failure(
        _row("azure", "centralus"), {"sku_name": "B_Standard_B1ms"}, _AZURE_WALL)
    assert "Azure has no B_Standard_B1ms capacity in centralus" in msg
    assert "CapacityNotAvailable" in msg
    assert "Delete this database" in msg
    assert "Live Output" in msg
    # The wall itself must be gone — that's the point of the distillation.
    assert "Still creating" not in msg
    assert len(msg) < 700


def test_aws_capacity_stockout_is_distilled():
    exc = _TerraformError(
        "terraform apply failed:\n... Error: creating RDS DB Instance: "
        "InsufficientDBInstanceCapacity: Insufficient DB instance capacity in "
        "the requested Availability Zone. Please try a different one.\n")
    msg = svc._distill_provision_failure(
        _row("aws", "us-east-1"), {"instance_class": "db.t3.micro"}, exc)
    assert "AWS has no db.t3.micro capacity in us-east-1" in msg
    assert "InsufficientDBInstanceCapacity" in msg
    assert "Delete this database" in msg


def test_other_terraform_errors_pass_through_verbatim():
    exc = _TerraformError("terraform apply failed:\nError: subnet not found")
    msg = svc._distill_provision_failure(
        _row("azure", "centralus"), {"sku_name": "B_Standard_B1ms"}, exc)
    assert msg == str(exc)


def test_non_terraform_errors_pass_through_even_with_the_code():
    # The signature is only trusted inside a TerraformError — a stray mention of
    # the code in some other exception must not get rewritten into a stockout.
    exc = RuntimeError("config said CapacityNotAvailable somewhere")
    msg = svc._distill_provision_failure(
        _row("azure", "centralus"), {"sku_name": "B_Standard_B1ms"}, exc)
    assert msg == str(exc)


def test_cloud_without_signature_passes_through():
    msg = svc._distill_provision_failure(
        _row("gcp", "us-east1"), {"tier": "db-f1-micro"}, _AZURE_WALL)
    assert msg == str(_AZURE_WALL)


def test_missing_size_and_region_fall_back_to_placeholders():
    msg = svc._distill_provision_failure(_row("azure", ""), {}, _AZURE_WALL)
    assert "the requested size" in msg
    assert "the requested region" in msg


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
