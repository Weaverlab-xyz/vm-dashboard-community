"""Cloud Functions: the pure half of cloud_function_service.

Covers the two things that are cheap to get wrong and expensive to discover in the
cloud: the per-cloud ``-var`` set, and the guarantee that no secret is persisted to
job metadata. Both are pure functions, so they are tested with the heavy imports
(config, database, SQLAlchemy) stubbed out — the repo's sys.modules idiom.
"""
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

# ── Stub everything cloud_function_service imports but does not need here ─────

CONF = {}


def _install_stubs():
    def _mod(name, **attrs):
        module = types.ModuleType(name)
        for key, val in attrs.items():
            setattr(module, key, val)
        sys.modules[name] = module
        return module

    settings = types.SimpleNamespace(terraform_executable="terraform")
    _mod("web_dashboard.config", settings=settings, Settings=object)

    class _Col:
        def __init__(self, *a, **k):
            pass

    _mod("web_dashboard.database", CloudFunction=object, Job=object)
    _mod("web_dashboard.services.config_service",
         get=lambda key, default="": CONF.get(key, default),
         get_bool=lambda key, default=False: bool(CONF.get(key, default)),
         set=lambda key, value: CONF.__setitem__(key, value),
         delete=lambda key: CONF.pop(key, None))
    _mod("web_dashboard.services.job_service", create_job=None, set_running=None,
         set_completed=None, set_failed=None, cancel_check=None)
    _mod("web_dashboard.services.terraform", apply=None, destroy=None)
    _mod("web_dashboard.services.terraform_provider_env", provider_env=lambda c: None)
    _mod("web_dashboard.services.region_config",
         resolve_region=lambda cloud, region: CONF.get(f"__region__{cloud}", {}))


try:
    import sqlalchemy  # noqa: F401
except ImportError:
    print("SKIP: sqlalchemy not installed")
    sys.exit(0)

_install_stubs()

from web_dashboard.services import cloud_function_service as svc  # noqa: E402


def _reset():
    CONF.clear()
    CONF.update({
        "function_package_s3_bucket": "fn-pkgs",
        "function_package_gcs_bucket": "fn-pkgs-gcp",
        "storage_azure_account": "dashstorage",
        "azure_resource_group": "rg-dash",
        "gcp_project": "proj-1",
    })


_PACKAGE = {"bucket": "fn-pkgs", "key": "function-packages/f1/abc.zip",
            "sha256_b64": "YWJj", "sas_url": "https://x/pkg.zip?sig=SECRET",
            "storage_account": "dashstorage", "storage_key": "STORAGEKEY",
            "uri": "s3://fn-pkgs/function-packages/f1/abc.zip"}

_OPTS = {"shared_secret": "TOPSECRET", "auth_mode": "", "timeout_seconds": None,
         "memory_mb": None, "environment": {"FOO": "bar"}, "project": "proj-1",
         "resource_group_name": "rg-dash", "sku_name": "B1",
         "service_account_email": "fn@proj-1.iam.gserviceaccount.com"}


def _vars(cloud, network=None, opts=None):
    return svc._build_tf_variables(
        cloud=cloud, region="us-east-1", name="fn-demo", workload="echo_diag",
        package=_PACKAGE, network=network or {}, opts={**_OPTS, **(opts or {})})


# ── Terraform variables ───────────────────────────────────────────────────────

def test_aws_vars_match_the_module_inputs():
    _reset()
    got = _vars("aws")
    assert got["package_bucket"] == "fn-pkgs"
    assert got["package_key"] == "function-packages/f1/abc.zip"
    assert got["package_sha256_b64"] == "YWJj"
    assert got["runtime"] == "python3.12"
    assert got["subnet_ids"] == [] and got["security_group_ids"] == []
    assert got["region"] == "us-east-1"


def test_gcp_vars_use_package_object_not_package_key():
    """cloudfunctions2's storage_source calls it `object`; a mismatch here is a
    'no value for required variable' failure minutes into a deploy."""
    _reset()
    got = _vars("gcp")
    assert got["package_object"] == "function-packages/f1/abc.zip"
    assert "package_key" not in got
    assert got["runtime"] == "python312"
    assert got["project"] == "proj-1"


def test_azure_vars_use_location_and_sas_url():
    _reset()
    got = _vars("azure")
    assert got["location"] == "us-east-1" and "region" not in got
    assert got["package_sas_url"] == "https://x/pkg.zip?sig=SECRET"
    assert got["python_version"] == "3.12"
    assert got["resource_group_name"] == "rg-dash"
    assert got["sku_name"] == "B1"


def test_every_cloud_gets_the_shared_secret_and_workload():
    _reset()
    for cloud in ("aws", "azure", "gcp"):
        got = _vars(cloud)
        assert got["shared_secret"] == "TOPSECRET", cloud
        assert got["workload"] == "echo_diag", cloud
        assert got["name"] == "fn-demo", cloud


def test_credential_access_is_per_cloud_and_never_carries_the_value():
    """Three different mechanisms, one rule: the dashboard passes a REFERENCE, never
    the secret itself, so a credential cannot leak through Terraform state."""
    _reset()
    aws = _vars("aws", opts={"readable_secret_arns": "arn:aws:secretsmanager:::a"})
    assert aws["readable_secret_arns"] == ["arn:aws:secretsmanager:::a"]

    gcp = _vars("gcp", opts={"db_admin_secret": "clouddb-admin"})
    assert gcp["db_admin_secret"] == "clouddb-admin"

    # Azure needs no variable at all: a Key Vault reference is just a string in the
    # app settings, resolved by the platform before the worker starts.
    azure = _vars("azure", opts={"environment": {
        "FN_DB_ADMIN_PASSWORD": "@Microsoft.KeyVault(SecretUri=https://v/secrets/db/)"}})
    assert "@Microsoft.KeyVault" in azure["environment"]["FN_DB_ADMIN_PASSWORD"]


def test_credential_access_defaults_to_none():
    """A workload that needs no credential must not be granted one."""
    _reset()
    assert _vars("aws")["readable_secret_arns"] == []
    assert _vars("gcp")["db_admin_secret"] == ""


def test_defaults_are_applied_for_blank_sizing():
    _reset()
    got = _vars("aws")
    assert got["timeout_seconds"] == 60 and got["memory_mb"] == 256


# ── Secret hygiene ────────────────────────────────────────────────────────────

def test_no_secret_survives_into_job_metadata():
    """package_sas_url is the easy one to miss — a credential embedded in a URL.
    Unstripped it lands in jobs.extra_data AND streams into the Live Output."""
    _reset()
    for cloud in ("aws", "azure", "gcp"):
        safe = svc.strip_secrets(_vars(cloud))
        blob = repr(safe)
        assert "TOPSECRET" not in blob, cloud
        assert "STORAGEKEY" not in blob, cloud
        assert "sig=SECRET" not in blob, cloud
        for key in svc._SECRET_TF_KEYS:
            assert key not in safe, (cloud, key)


def test_stripping_keeps_everything_else():
    _reset()
    full = _vars("azure")
    safe = svc.strip_secrets(full)
    assert safe["location"] == full["location"]
    assert safe["workload"] == full["workload"]
    assert len(safe) == len(full) - 3   # the three azure secrets


# ── Networking resolution ─────────────────────────────────────────────────────

def test_public_mode_needs_no_ids():
    _reset()
    assert svc._resolved_network("aws", "us-east-1", network_mode="public",
                                 subnet_ids=None, subnet_id="", vpc_connector="",
                                 security_group_ids=None) == {}


def test_aws_vpc_mode_falls_back_to_config():
    _reset()
    CONF["aws_functions_subnet_ids"] = "subnet-a, subnet-b"
    CONF["aws_functions_security_group_ids"] = "sg-1"
    got = svc._resolved_network("aws", "us-east-1", network_mode="vpc",
                                subnet_ids=None, subnet_id="", vpc_connector="",
                                security_group_ids=None)
    assert got == {"subnet_ids": ["subnet-a", "subnet-b"],
                   "security_group_ids": ["sg-1"]}


def test_aws_vpc_mode_without_a_security_group_is_refused():
    """No SG means the ENIs get the default one and the DB path fails silently —
    better to refuse before the row is written."""
    _reset()
    CONF["aws_functions_subnet_ids"] = "subnet-a"
    try:
        svc._resolved_network("aws", "us-east-1", network_mode="vpc",
                              subnet_ids=None, subnet_id="", vpc_connector="",
                              security_group_ids=None)
    except svc.CloudFunctionError as exc:
        assert "security group" in str(exc)
    else:
        raise AssertionError("expected CloudFunctionError")


def test_vpc_mode_errors_name_the_setting_to_fix():
    _reset()
    for cloud, needle in (("aws", "aws_functions_subnet_ids"),
                          ("azure", "azure_functions_subnet_id"),
                          ("gcp", "gcp_functions_vpc_connector")):
        try:
            svc._resolved_network(cloud, "r", network_mode="vpc", subnet_ids=None,
                                  subnet_id="", vpc_connector="",
                                  security_group_ids=None)
        except svc.CloudFunctionError as exc:
            assert needle in str(exc), (cloud, str(exc))
        else:
            raise AssertionError(f"expected an error for {cloud}")


def test_explicit_arguments_beat_config():
    _reset()
    CONF["azure_functions_subnet_id"] = "/from/config"
    got = svc._resolved_network("azure", "eastus", network_mode="vpc",
                                subnet_ids=None, subnet_id="/explicit",
                                vpc_connector="", security_group_ids=None)
    assert got == {"subnet_id": "/explicit"}


def test_unknown_network_mode_is_rejected():
    _reset()
    try:
        svc._resolved_network("aws", "r", network_mode="magic", subnet_ids=None,
                              subnet_id="", vpc_connector="", security_group_ids=None)
    except svc.CloudFunctionError:
        pass
    else:
        raise AssertionError("expected CloudFunctionError")


# ── Names + catalog ───────────────────────────────────────────────────────────

def test_name_normalization_targets_the_strictest_cloud():
    """An Azure Function App name becomes a DNS label, so lowercase alnum + '-'
    only. Normalising to that keeps one name valid on all three."""
    assert svc.normalize_name("My Demo Fn") == "my-demo-fn"
    assert svc.normalize_name("under_scores") == "under-scores"
    assert svc.normalize_name("9lives") == "fn-9lives"
    assert svc.normalize_name("a--b__c") == "a-b-c"
    assert len(svc.normalize_name("x" * 200)) == 60


def test_normalized_names_satisfy_the_validator():
    for raw in ("My Demo Fn", "under_scores", "9lives", "UPPER"):
        assert svc._NAME_RE.match(svc.normalize_name(raw)), raw


def test_workloads_are_universal_unless_restricted():
    """A stdlib-only workload should need no table edit to be deployable — the
    filesystem is the catalog and _CLOUD_RESTRICTED records only exceptions."""
    _reset()
    assert svc.clouds_for("echo_diag") == svc.VALID_CLOUDS
    assert svc.clouds_for("a_workload_added_tomorrow") == svc.VALID_CLOUDS
    assert svc.clouds_for("local_account_broker") == ("aws",)


def test_restricted_workload_is_refused_on_the_wrong_cloud():
    _reset()
    svc._check_target("echo_diag", "azure")          # universal — fine
    # Only reachable once the module exists on disk; assert via clouds_for so the
    # rule is pinned now and the guard below covers it when the file lands.
    assert "gcp" not in svc.clouds_for("local_account_broker")
    if "local_account_broker" in svc.available_workloads():
        try:
            svc._check_target("local_account_broker", "gcp")
        except NotImplementedError as exc:
            assert "cloud SDK" in str(exc)
        else:
            raise AssertionError("expected NotImplementedError")


def test_check_target_rejects_unknown_cloud_and_workload():
    _reset()
    for workload, cloud in (("echo_diag", "digitalocean"), ("no_such", "aws")):
        try:
            svc._check_target(workload, cloud)
        except svc.CloudFunctionError:
            pass
        else:
            raise AssertionError(f"accepted {workload}/{cloud}")


def test_the_catalog_covers_every_workload_on_disk():
    """Every module in fnworkloads/ must be deployable somewhere, and the restricted
    table must not name a cloud that doesn't exist."""
    on_disk = svc.available_workloads()
    assert on_disk, "no workloads found on disk"
    for workload in on_disk:
        clouds = svc.clouds_for(workload)
        assert clouds, workload
        assert set(clouds) <= set(svc.VALID_CLOUDS), (workload, clouds)
    catalog = {entry["name"] for entry in svc.workload_catalog()}
    assert catalog == set(on_disk), catalog ^ set(on_disk)


def test_restricted_table_only_names_real_clouds():
    for workload, clouds in svc._CLOUD_RESTRICTED.items():
        assert set(clouds) <= set(svc.VALID_CLOUDS), (workload, clouds)


def test_package_location_requires_the_bucket_to_be_configured():
    _reset()
    del CONF["function_package_s3_bucket"]
    try:
        svc.package_location("aws", "f1", "abc")
    except svc.CloudFunctionError as exc:
        assert "function_package_s3_bucket" in str(exc)
    else:
        raise AssertionError("expected CloudFunctionError")


def test_package_location_is_content_addressed():
    _reset()
    assert svc.package_location("aws", "f1", "deadbeef")["key"].endswith("deadbeef.zip")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as exc:
            failures += 1
            print(f"FAIL {fn.__name__}: {exc}")
    sys.exit(1 if failures else 0)
