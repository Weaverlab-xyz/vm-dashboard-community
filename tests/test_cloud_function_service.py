"""Cloud Functions: the pure half of cloud_function_service.

Covers the things that are cheap to get wrong and expensive to discover in the
cloud: the per-cloud ``-var`` set, the guarantee that no secret is persisted to job
metadata, and the credential wiring — which cloud mechanism each reference turns
into, and the refusal to accept a credential VALUE anywhere. All pure functions, so
they are tested with the heavy imports (config, database, SQLAlchemy) stubbed out —
the repo's sys.modules idiom.
"""
import asyncio
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
    # Region-keyed, falling back to the cloud-wide entry so the pre-existing
    # __region__<cloud> users keep working. Mirrors the real semantics that matter
    # here: a field the region does not set resolves to its FLAT key, which is why
    # the default region and an unconfigured region must behave identically.
    _flat = {"functions_subnet_ids": "aws_functions_subnet_ids",
             "functions_security_group_ids": "aws_functions_security_group_ids",
             "functions_subnet_id": "azure_functions_subnet_id",
             "functions_network": "gcp_functions_network",
             "functions_subnetwork": "gcp_functions_subnetwork"}

    def _resolve(cloud, region):
        entry = dict(CONF.get(f"__region__{cloud}:{region}",
                              CONF.get(f"__region__{cloud}", {})))
        for fld, flat in _flat.items():
            if not entry.get(fld):
                entry[fld] = CONF.get(flat, "")
        return entry

    _mod("web_dashboard.services.region_config", resolve_region=_resolve)


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


def test_memory_is_only_sent_to_the_clouds_that_have_a_knob_for_it():
    """On Azure the memory an app gets is a property of the App Service plan SKU, so
    the module has no memory_mb variable — and terraform fails the WHOLE apply on a
    -var it does not declare, five seconds in, before it creates anything."""
    _reset()
    assert _vars("aws")["memory_mb"] == 256
    assert _vars("gcp")["memory_mb"] == 256
    assert "memory_mb" not in _vars("azure")
    assert _vars("azure")["timeout_seconds"] == 60


def _declared_variables(cloud):
    """The variable blocks of a cloud's module, as ``{name: has_default}``."""
    import re
    body = open(os.path.join(svc.template_dir(cloud), "main.tf"),
                encoding="utf-8").read()
    blocks = re.findall(r'^variable\s+"([^"]+)"\s*\{(.*?)^\}', body,
                        re.S | re.M)
    return {name: bool(re.search(r'^\s*default\s*=', block, re.M))
            for name, block in blocks}


def test_every_var_the_service_sends_is_declared_by_the_module():
    """The drift guard. `terraform apply -var x=…` on a variable the root module does
    not declare is a hard error, not a warning, so this mismatch cannot be caught by
    anything short of a real deploy — which is how azure shipped with memory_mb and
    timeout_seconds nobody declared."""
    _reset()
    for cloud in ("aws", "azure", "gcp"):
        declared = _declared_variables(cloud)
        assert declared, cloud                      # the parse itself works
        undeclared = sorted(set(_vars(cloud)) - set(declared))
        assert not undeclared, (cloud, undeclared)


def test_every_required_module_variable_is_actually_sent():
    """The other direction: a variable with no default that nobody passes stops the
    apply the same way."""
    _reset()
    for cloud in ("aws", "azure", "gcp"):
        required = {n for n, has_default in _declared_variables(cloud).items()
                    if not has_default}
        missing = sorted(required - set(_vars(cloud)))
        assert not missing, (cloud, missing)


def test_a_replayed_var_the_module_no_longer_takes_is_dropped():
    """Destroy and update replay the var set persisted at DEPLOY time, so they carry
    whatever that older build sent — including a variable the module has since
    stopped declaring. Terraform refuses the whole run over one of those, which on
    destroy means a function that can never be torn down."""
    _reset()
    kept = svc._drop_undeclared("azure", {"name": "fn-demo", "memory_mb": 256,
                                          "location": "eastus"})
    assert kept == {"name": "fn-demo", "location": "eastus"}
    # aws does declare it — the filter must not be a blanket strip.
    assert svc._drop_undeclared("aws", {"memory_mb": 256}) == {"memory_mb": 256}


def test_an_unreadable_module_disables_the_filter_rather_than_emptying_it():
    """Fail OPEN here: an empty var set fails every apply, while an unfiltered one
    only fails the (rare) drifted case this guard exists for."""
    _reset()
    svc._MODULE_VARIABLE_CACHE.clear()
    original = svc._TEMPLATE_DIRS["aws"]
    svc._TEMPLATE_DIRS["aws"] = os.path.join(original, "does-not-exist")
    try:
        assert svc._drop_undeclared("aws", {"anything": 1}) == {"anything": 1}
    finally:
        svc._TEMPLATE_DIRS["aws"] = original
        svc._MODULE_VARIABLE_CACHE.clear()


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
                          ("gcp", "gcp_functions_network")):
        try:
            svc._resolved_network(cloud, "r", network_mode="vpc", subnet_ids=None,
                                  subnet_id="", vpc_connector="",
                                  security_group_ids=None)
        except svc.CloudFunctionError as exc:
            assert needle in str(exc), (cloud, str(exc))
        else:
            raise AssertionError(f"expected an error for {cloud}")


def test_gcp_vpc_mode_prefers_direct_egress():
    """Direct VPC egress: no connector, so nothing is billed while idle."""
    _reset()
    CONF["gcp_functions_network"] = "sandbox-vpc"
    CONF["gcp_functions_subnetwork"] = "sandbox-vm-subnet"
    CONF["gcp_functions_vpc_connector"] = "projects/p/locations/r/connectors/c"
    got = svc._resolved_network("gcp", "us-east1", network_mode="vpc", subnet_ids=None,
                               subnet_id="", vpc_connector="", security_group_ids=None)
    assert got == {"vpc_network": "sandbox-vpc", "vpc_subnetwork": "sandbox-vm-subnet"}, got


def test_gcp_connector_is_the_fallback_not_the_default():
    """A pre-existing connector still works — it is the only way to reach another
    region from a region-pinned function."""
    _reset()
    CONF["gcp_functions_vpc_connector"] = "projects/p/locations/r/connectors/c"
    got = svc._resolved_network("gcp", "us-east1", network_mode="vpc", subnet_ids=None,
                               subnet_id="", vpc_connector="", security_group_ids=None)
    assert got == {"vpc_connector": "projects/p/locations/r/connectors/c"}, got


def test_aws_vpc_fallback_never_uses_the_db_subnet_group_name():
    """db_subnet_group_name is an RDS subnet-GROUP NAME. Using it as a subnet id
    passes validation here and dies at apply on InvalidSubnetID.NotFound."""
    _reset()
    CONF["__region__aws"] = {"db_subnet_group_name": "dashboard-sandbox-db",
                             "db_security_group_id": "sg-db",
                             "default_subnet_id": "subnet-private"}
    got = svc._resolved_network("aws", "us-east-1", network_mode="vpc", subnet_ids=None,
                               subnet_id="", vpc_connector="", security_group_ids=None)
    assert got["subnet_ids"] == ["subnet-private"], got
    assert "dashboard-sandbox-db" not in got["subnet_ids"]


# ── Region correctness: the function must land on ITS OWN region's network ────
# A flat *_functions_* key is the single-region install's answer. Left outranking a
# per-region one it pins every function to the DEFAULT region's network while the row,
# the job and the operator all say otherwise — AWS then dies at apply on
# InvalidSubnetID.NotFound, and Azure on a cross-region VNet integration. This matters
# most for the clouddb adapter, whose region is the database's and not a picked value.

def test_aws_per_region_functions_subnets_beat_the_flat_key():
    _reset()
    CONF["aws_functions_subnet_ids"] = "subnet-default-region"
    CONF["aws_functions_security_group_ids"] = "sg-default-region"
    CONF["__region__aws:us-west-2"] = {"functions_subnet_ids": "subnet-usw2",
                                       "functions_security_group_ids": "sg-usw2"}
    got = svc._resolved_network("aws", "us-west-2", network_mode="vpc", subnet_ids=None,
                                subnet_id="", vpc_connector="", security_group_ids=None)
    assert got == {"subnet_ids": ["subnet-usw2"],
                   "security_group_ids": ["sg-usw2"]}, got


def test_gcp_per_region_functions_subnetwork_beats_the_flat_key():
    _reset()
    CONF["gcp_functions_network"] = "default-region-vpc"
    CONF["gcp_functions_subnetwork"] = "default-region-subnet"
    CONF["__region__gcp:us-east1"] = {"functions_network": "sandbox-vpc",
                                      "functions_subnetwork": "sandbox-use1-subnet"}
    got = svc._resolved_network("gcp", "us-east1", network_mode="vpc", subnet_ids=None,
                                subnet_id="", vpc_connector="", security_group_ids=None)
    assert got == {"vpc_network": "sandbox-vpc",
                   "vpc_subnetwork": "sandbox-use1-subnet"}, got


def test_azure_per_region_functions_subnet_beats_the_flat_key():
    """Azure had no per-region functions subnet at all, so a second region silently
    got the default region's - and VNet integration requires a same-region subnet."""
    _reset()
    CONF["azure_functions_subnet_id"] = "/subs/x/eastus/fn"
    CONF["__region__azure:westus2"] = {"functions_subnet_id": "/subs/x/westus2/fn"}
    got = svc._resolved_network("azure", "westus2", network_mode="vpc", subnet_ids=None,
                                subnet_id="", vpc_connector="", security_group_ids=None)
    assert got == {"subnet_id": "/subs/x/westus2/fn"}, got


def test_a_region_that_sets_no_functions_field_matches_the_default_region():
    """The regression the first attempt at this shipped, and the reason the per-region
    field is purpose-specific rather than generic.

    Beating the flat key with a per-region GENERIC subnet fixes the region by
    discarding the PURPOSE, and only for non-default regions - a region entry is empty
    for the default one. The result was a feature that picked a different-purpose
    subnet depending on whether the region happened to be the default. Every tier has
    to be symmetric.
    """
    _reset()
    CONF["gcp_run_network"] = "runner-vpc"
    CONF["gcp_run_subnetwork"] = "runner-subnet"
    # us-east1 configures its generic subnet but NOT its functions one.
    CONF["__region__gcp:us-east1"] = {"network": "generic-vpc",
                                      "subnetwork": "generic-subnet"}
    default = svc._resolved_network("gcp", "us-central1", network_mode="vpc",
                                    subnet_ids=None, subnet_id="", vpc_connector="",
                                    security_group_ids=None)
    other = svc._resolved_network("gcp", "us-east1", network_mode="vpc",
                                  subnet_ids=None, subnet_id="", vpc_connector="",
                                  security_group_ids=None)
    assert default == other, (default, other)
    assert other["vpc_subnetwork"] == "runner-subnet", other


def test_the_default_region_still_resolves_to_the_flat_keys():
    """A field the region does not set resolves to its flat key, so a single-region
    install sees byte-identical behaviour."""
    _reset()
    CONF["aws_functions_subnet_ids"] = "subnet-a"
    CONF["aws_functions_security_group_ids"] = "sg-1"
    CONF["gcp_functions_network"] = "vpc"
    CONF["gcp_functions_subnetwork"] = "subnet"
    assert svc._resolved_network("aws", "us-east-1", network_mode="vpc", subnet_ids=None,
                                 subnet_id="", vpc_connector="",
                                 security_group_ids=None) == {
        "subnet_ids": ["subnet-a"], "security_group_ids": ["sg-1"]}
    assert svc._resolved_network("gcp", "us-central1", network_mode="vpc",
                                 subnet_ids=None, subnet_id="", vpc_connector="",
                                 security_group_ids=None) == {
        "vpc_network": "vpc", "vpc_subnetwork": "subnet"}


def test_explicit_arguments_still_beat_a_per_region_entry():
    """A caller naming ids means them — the adapter path names none, so it gets the
    region's."""
    _reset()
    CONF["__region__gcp:us-east1"] = {"functions_network": "regional-vpc",
                                      "functions_subnetwork": "regional-subnet"}
    got = svc._resolved_network("gcp", "us-east1", network_mode="vpc", subnet_ids=None,
                                subnet_id="", vpc_connector="", security_group_ids=None,
                                vpc_network="explicit-vpc",
                                vpc_subnetwork="explicit-subnet")
    assert got == {"vpc_network": "explicit-vpc",
                   "vpc_subnetwork": "explicit-subnet"}, got


def test_a_cloud_with_no_per_region_dimension_still_resolves():
    """region_config raises ValueError for oci/local. Swallowing only THAT is the
    point: anything else is a bug and must not become a wrong-region deploy."""
    _reset()

    def _boom(cloud, region):
        raise ValueError("no per-region config for cloud 'oci'")

    import web_dashboard.services.cloud_function_service as mod
    saved = mod.resolve_region
    mod.resolve_region = _boom
    try:
        CONF["gcp_functions_network"] = "vpc"
        CONF["gcp_functions_subnetwork"] = "subnet"
        got = svc._resolved_network("gcp", "us-east1", network_mode="vpc",
                                    subnet_ids=None, subnet_id="", vpc_connector="",
                                    security_group_ids=None)
        assert got == {"vpc_network": "vpc", "vpc_subnetwork": "subnet"}, got
    finally:
        mod.resolve_region = saved


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


# ── Required settings: what stops the deploy form building an inert function ──
#
# The three adapters read their target out of the environment. Deployed without it
# they are not degraded, they are inert — every request fails, for the life of the
# function — and finding out costs a real cloud build. The picker reads the same
# declaration this validates against, so the warning and the refusal cannot disagree.

def test_the_catalog_carries_what_each_workload_requires():
    catalog = {entry["name"]: entry for entry in svc.workload_catalog()}
    for name, entry in catalog.items():
        assert isinstance(entry["required_env"], list), (name, entry)
        assert entry["required_env"] == list(svc.required_env(name)), name
    # The adapters need configuration; the diagnostic workloads deliberately do not.
    for name in ("db_grant", "portainer_access", "azure_role_grant"):
        assert catalog[name]["required_env"], name
    for name in ("echo_diag", "entitle_webhook_echo"):
        assert catalog[name]["required_env"] == [], name


def test_required_env_survives_a_workload_that_does_not_import():
    """The catalog must never be the thing that breaks the page — same rule as the
    description lookup, which already tolerates an unimportable module."""
    assert svc.required_env("no_such_workload_here") == ()


def test_a_missing_required_setting_is_refused_and_names_itself():
    for missing, supplied in (
        ("FN_DB_ENGINE", {"FN_DB_HOST": "db.internal", "FN_DB_NAME": "appdb"}),
        ("FN_DB_HOST", {"FN_DB_ENGINE": "mysql", "FN_DB_NAME": "appdb"}),
        ("FN_DB_NAME or FN_DB_NAMES",
         {"FN_DB_ENGINE": "mysql", "FN_DB_HOST": "db.internal"}),
    ):
        try:
            svc._check_required_env("db_grant", supplied)
        except svc.CloudFunctionError as exc:
            assert missing in str(exc), (missing, str(exc))
        else:
            raise AssertionError(f"accepted a deploy missing {missing}")


def test_either_spelling_satisfies_an_alternation():
    for name_key in ("FN_DB_NAME", "FN_DB_NAMES"):
        svc._check_required_env("db_grant", {
            "FN_DB_ENGINE": "mysql", "FN_DB_HOST": "db.internal", name_key: "appdb"})


def test_a_blank_value_does_not_count_as_supplied():
    """`FN_DB_HOST=""` reaches the function as an empty string and fails there
    exactly as an absent one does, so it must fail here too."""
    try:
        svc._check_required_env("db_grant", {
            "FN_DB_ENGINE": "mysql", "FN_DB_HOST": "  ", "FN_DB_NAME": "appdb"})
    except svc.CloudFunctionError as exc:
        assert "FN_DB_HOST" in str(exc), str(exc)
    else:
        raise AssertionError("accepted a blank required setting")


def test_a_workload_with_no_requirements_is_never_refused():
    svc._check_required_env("echo_diag", None)
    svc._check_required_env("echo_diag", {})


def _run(coro):
    """Drive one coroutine to completion. The register preflight is async because the
    thing it asks is an HTTP call."""
    return asyncio.run(coro)


# ── Registering in Entitle: don't publish an adapter that resolves nothing ────
#
# A db_grant function with no FN_DB_* registered CLEANLY and produced a live REST
# integration in the tenant with no assets behind it. Nothing in the dashboard looked
# wrong — the failure was only visible in Entitle, which is the worst place for it.

def test_only_a_contract_serving_workload_is_an_adapter():
    for name in ("db_grant", "portainer_access", "azure_role_grant",
                 "entitle_webhook_echo"):
        assert svc.is_entitle_adapter(name), name
    # echo_diag serves / and a probe payload, not the eight contract routes.
    assert not svc.is_entitle_adapter("echo_diag")
    assert not svc.is_entitle_adapter("no_such_workload_here")


def test_the_catalog_says_which_workloads_can_be_registered():
    """The page reads this instead of keeping its own list, which is what it used to
    do — so the flag and the button cannot disagree."""
    for entry in svc.workload_catalog():
        assert entry["entitle_adapter"] is svc.is_entitle_adapter(entry["name"]), entry


def test_an_unconfigured_adapter_is_refused_before_it_reaches_entitle():
    row = types.SimpleNamespace(id="fn1", name="my-grant-broker", workload="db_grant")
    calls = []

    async def _fake_invoke(db, *, fn_id, method="POST", path="/", payload=None):
        calls.append((method, path))
        return {"status": 200, "body": {"data": {
            "valid": False, "problems": ["FN_DB_ENGINE must be mysql or sqlserver (got '')"]}}}

    real, svc.invoke = svc.invoke, _fake_invoke
    try:
        _run(svc._refuse_unconfigured_adapter(None, row))
    except svc.CloudFunctionError as exc:
        assert "not configured" in str(exc), str(exc)
        assert "FN_DB_ENGINE" in str(exc), str(exc)
    else:
        raise AssertionError("registered an adapter that reports itself unconfigured")
    finally:
        svc.invoke = real
    # It asked the adapter rather than re-deriving the rules.
    assert calls == [("POST", "/check_config")], calls


def test_a_configured_adapter_is_allowed_through():
    row = types.SimpleNamespace(id="fn1", name="my-grant-broker", workload="db_grant")

    async def _fake_invoke(db, *, fn_id, method="POST", path="/", payload=None):
        return {"status": 200, "body": {"data": {"valid": True, "problems": []}}}

    real, svc.invoke = svc.invoke, _fake_invoke
    try:
        _run(svc._refuse_unconfigured_adapter(None, row))     # must not raise
    finally:
        svc.invoke = real


def test_an_unrecognisable_check_config_body_does_not_block():
    """Only an explicit refusal blocks. This must never become the reason a working
    pairing job stops working."""
    row = types.SimpleNamespace(id="fn1", name="fn", workload="entitle_webhook_echo")
    for body in ({"data": {"valid": True}}, {"data": {}}, {}, "not json at all", None):
        async def _fake_invoke(db, *, fn_id, method="POST", path="/", payload=None,
                               _b=body):
            return {"status": 200, "body": _b}

        real, svc.invoke = svc.invoke, _fake_invoke
        try:
            _run(svc._refuse_unconfigured_adapter(None, row))
        finally:
            svc.invoke = real


def test_an_unreachable_adapter_is_refused_too():
    row = types.SimpleNamespace(id="fn1", name="fn", workload="db_grant")

    async def _fake_invoke(db, *, fn_id, method="POST", path="/", payload=None):
        raise OSError("connection refused")

    real, svc.invoke = svc.invoke, _fake_invoke
    try:
        _run(svc._refuse_unconfigured_adapter(None, row))
    except svc.CloudFunctionError as exc:
        assert "dead endpoint" in str(exc), str(exc)
    else:
        raise AssertionError("registered an adapter that cannot be reached")
    finally:
        svc.invoke = real


def test_a_non_200_from_check_config_is_refused():
    row = types.SimpleNamespace(id="fn1", name="fn", workload="db_grant")

    async def _fake_invoke(db, *, fn_id, method="POST", path="/", payload=None):
        return {"status": 500, "body": {"error": "function not configured",
                                        "problem": "FN_DB_HOST is not set"}}

    real, svc.invoke = svc.invoke, _fake_invoke
    try:
        _run(svc._refuse_unconfigured_adapter(None, row))
    except svc.CloudFunctionError as exc:
        assert "HTTP 500" in str(exc) and "FN_DB_HOST" in str(exc), str(exc)
    else:
        raise AssertionError("registered an adapter whose check_config fails")
    finally:
        svc.invoke = real


# ── The front door vs the workload: two failures wearing one status code ─────
#
# Live, a GCP pairing was refused at +11s, +44s and +51s past the terraform apply
# with Cloud Run's "the access token could not be verified", then served normally at
# +5m: the allUsers → roles/run.invoker binding had not taken effect yet. The job
# failed reading "the adapter is broken" when nothing was.

def test_the_two_401s_are_told_apart_by_the_body_not_the_status():
    """fnruntime.auth always names `error`; no cloud's front door does."""
    # The workload's own denial — the shared secret is wrong, waiting fixes nothing.
    assert not svc._is_front_door_denial(401, {"error": "unauthorized"})
    # Cloud Run: empty body, or Google's text. AWS Function URL under AWS_IAM.
    assert svc._is_front_door_denial(401, "")
    assert svc._is_front_door_denial(401, None)
    assert svc._is_front_door_denial(401, "\n<html>401 Unauthorized</html>")
    assert svc._is_front_door_denial(403, {"Message": "Forbidden"})
    # Nothing else is a front-door denial, whatever the body looks like.
    for status in (200, 404, 500, 502):
        assert not svc._is_front_door_denial(status, ""), status


def test_a_front_door_denial_is_waited_out_not_failed():
    row = types.SimpleNamespace(id="fn1", name="jit-mysql-abc", workload="db_grant")
    bodies = ["", "", {"data": {"valid": True, "problems": []}}]
    calls = []

    async def _fake_invoke(db, *, fn_id, method="POST", path="/", payload=None):
        body = bodies[len(calls)]
        calls.append(path)
        return {"status": 200 if isinstance(body, dict) else 401, "body": body}

    real, svc.invoke = svc.invoke, _fake_invoke
    poll, svc._FRONT_DOOR_POLL_SECONDS = svc._FRONT_DOOR_POLL_SECONDS, 0
    try:
        _run(svc._refuse_unconfigured_adapter(None, row))     # must not raise
    finally:
        svc.invoke, svc._FRONT_DOOR_POLL_SECONDS = real, poll
    assert len(calls) == 3, calls


def test_a_front_door_that_never_opens_names_the_invoke_permission():
    """Waiting costs nothing when the permission is genuinely missing: the same
    refusal, later, pointing at the binding rather than at the adapter."""
    row = types.SimpleNamespace(id="fn1", name="jit-mysql-abc", workload="db_grant")

    async def _fake_invoke(db, *, fn_id, method="POST", path="/", payload=None):
        return {"status": 403, "body": {"Message": "Forbidden"}}

    real, svc.invoke = svc.invoke, _fake_invoke
    try:
        # grace_seconds=0: one call, no wait — the deadline check is the same one.
        _run(svc._refuse_unconfigured_adapter(None, row, grace_seconds=0))
    except svc.CloudFunctionError as exc:
        assert "run.invoker" in str(exc) and "HTTP 403" in str(exc), str(exc)
    else:
        raise AssertionError("registered an adapter its own platform will not invoke")
    finally:
        svc.invoke = real


def test_the_workloads_own_401_fails_at_once_and_names_the_secret():
    """The one 401 that is NOT a race. Retrying it would delay a fixed diagnosis by
    two minutes and then report the wrong cause."""
    row = types.SimpleNamespace(id="fn1", name="jit-mysql-abc", workload="db_grant")
    calls = []

    async def _fake_invoke(db, *, fn_id, method="POST", path="/", payload=None):
        calls.append(path)
        return {"status": 401, "body": {"error": "unauthorized"}}

    real, svc.invoke = svc.invoke, _fake_invoke
    try:
        _run(svc._refuse_unconfigured_adapter(None, row))
    except svc.CloudFunctionError as exc:
        assert "shared secret" in str(exc), str(exc)
        assert "cloudfn/fn1/bearer" in str(exc), str(exc)
    else:
        raise AssertionError("registered an adapter that rejects the dashboard")
    finally:
        svc.invoke = real
    assert calls == ["/check_config"], calls


# ── Test invoke: reaching the route the operator actually meant ───────────────

def test_the_invoke_path_is_appended_not_substituted():
    """Azure's base URL already carries /api/<name>, and the adapters' routes are
    relative to whatever the platform's root is — so this appends."""
    base = "https://fn.example.com/api/my-fn"
    assert svc._invoke_url(base, "/check_config") == f"{base}/check_config"
    assert svc._invoke_url(base, "check_config") == f"{base}/check_config"
    assert svc._invoke_url(base + "/", "/check_config") == f"{base}/check_config"
    # An empty or root path is the old behaviour, byte for byte.
    for path in ("", "/", None):
        assert svc._invoke_url(base, path) == base, path


def test_the_invoke_path_cannot_repoint_the_request():
    """This value reaches an outbound request, so an absolute URL or a climb out of
    the function's own path is refused rather than normalized."""
    base = "https://fn.example.com/api/my-fn"
    for path in ("https://evil.example.com/", "//evil.example.com/",
                 "/../../other-fn/check_config", "/a/../../b"):
        try:
            svc._invoke_url(base, path)
        except svc.CloudFunctionError:
            pass
        else:
            raise AssertionError(f"accepted path {path!r}")


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


# ── Credential references ─────────────────────────────────────────────────────
#
# The rule the whole feature rests on: a credential reaches a function BY REFERENCE
# on every cloud, and a credential VALUE is refused before anything is written.

def test_a_credential_in_the_plaintext_environment_is_refused():
    """`environment` is not in _SECRET_TF_KEYS, so a value here reaches
    jobs.extra_data, the job's Live Output and the function's console page. There is
    no state between accepted and leaked, hence a hard error."""
    _reset()
    for key in ("FN_DB_ADMIN_PASSWORD", "FN_PORTAINER_API_KEY",
                "FN_AZURE_CLIENT_SECRET", "MY_API_TOKEN", "FN_DB_CONNECTION_STRING",
                "FN_SIGNING_PRIVATE_KEY", "FN_UPSTREAM_CREDENTIALS"):
        try:
            svc._reject_plaintext_secrets({key: "hunter2"})
        except svc.CloudFunctionError as exc:
            assert "secret_environment" in str(exc), exc
            assert "hunter2" not in str(exc), "the error quoted the credential"
        else:
            raise AssertionError(f"{key} was accepted as a plaintext setting")


def test_a_reference_in_the_environment_is_allowed():
    """Three things name a credential without carrying one, and all three are how
    the feature is meant to be used."""
    _reset()
    svc._reject_plaintext_secrets({
        "FN_DB_ADMIN_SECRET_ID": "arn:aws:secretsmanager:us-east-1:1:secret:db-Ab12Cd",
        "FN_DB_ADMIN_PASSWORD":
            "@Microsoft.KeyVault(SecretUri=https://v.vault.azure.net/secrets/db/)",
        "FN_PORTAINER_API_KEY": "",
        "FN_DB_HOST": "db.internal",
    })


def test_the_guard_does_not_refuse_ordinary_settings():
    """A guard that fires on FN_AUTH_HEADER — a real, non-secret fnruntime setting —
    teaches people to route around it, which is worse than not having one."""
    _reset()
    svc._reject_plaintext_secrets({
        "FN_AUTH_HEADER": "x-entitle-auth", "FN_AUTH_PREFIX": "",
        "FN_DB_ADMIN_USER": "dbadmin", "FN_PORTAINER_URL": "https://portainer.internal",
        "FN_AZURE_ROLES": "Reader", "FN_EGRESS_PROBE": "example.com:443",
    })


def test_the_guard_matches_what_the_runtime_refuses_to_log():
    """One rule, two enforcement points: what fnruntime.logs will not LOG is what
    the deploy path will not STORE. Drift means a key redacted in the function's
    logs still sitting in cleartext in the dashboard."""
    _reset()
    from web_dashboard import functions  # noqa: F401  (puts fnruntime on sys.path)
    from fnruntime import logs
    missing = set(logs._REDACT_SUBSTRINGS) - set(svc._SECRET_ENV_SUBSTRINGS)
    assert not missing, f"the deploy guard does not cover {sorted(missing)}"


def test_gcp_turns_a_reference_into_a_platform_injected_secret():
    _reset()
    env, tfvars = svc._secret_environment("gcp", {"FN_PORTAINER_API_KEY": "portainer-key"})
    assert env == {}, "nothing should reach the plaintext settings on GCP"
    assert tfvars == {"secret_environment": {"FN_PORTAINER_API_KEY": "portainer-key"}}


def test_aws_turns_a_reference_into_an_id_plus_a_grant():
    """Lambda has no platform-resolved secret, so the id goes in the environment and
    the ARN goes on the role — the function reads it itself at cold start."""
    _reset()
    arn = "arn:aws:secretsmanager:us-east-1:1:secret:portainer-Ab12Cd"
    env, tfvars = svc._secret_environment("aws", {"FN_PORTAINER_API_KEY": arn})
    assert env == {"FN_PORTAINER_API_KEY_SECRET_ID": arn}
    assert tfvars == {"readable_secret_arns": [arn]}


def test_the_aws_id_variable_is_the_one_the_runtime_actually_reads():
    """The two halves are written in different languages in different files; if they
    disagree the function deploys and then cannot find its credential."""
    _reset()
    from web_dashboard import functions  # noqa: F401
    from fnruntime import secretref
    arn = "arn:aws:secretsmanager:us-east-1:1:secret:thing-Ab12Cd"
    env, _ = svc._secret_environment("aws", {"FN_THING": arn})
    assert list(env) == [secretref.id_env_for("FN_THING")]


def test_aws_refuses_a_bare_secret_name():
    """AWS appends a random suffix to every secret ARN, so a name cannot be turned
    into the ARN the IAM policy needs."""
    _reset()
    try:
        svc._secret_environment("aws", {"FN_PORTAINER_API_KEY": "portainer-key"})
    except svc.CloudFunctionError as exc:
        assert "ARN" in str(exc), exc
    else:
        raise AssertionError("a bare name was accepted on AWS")


def test_azure_turns_a_name_into_a_key_vault_reference():
    _reset()
    env, tfvars = svc._secret_environment(
        "azure", {"FN_PORTAINER_API_KEY": "portainer-key"}, vault="dash-kv")
    assert env == {
        "FN_PORTAINER_API_KEY":
            "@Microsoft.KeyVault(SecretUri="
            "https://dash-kv.vault.azure.net/secrets/portainer-key/)"}
    assert tfvars == {}
    # A caller that already built the reference keeps it verbatim.
    full = "@Microsoft.KeyVault(SecretUri=https://other.vault.azure.net/secrets/k/)"
    env, _ = svc._secret_environment("azure", {"FN_X": full}, vault="dash-kv")
    assert env == {"FN_X": full}


def test_azure_says_which_setting_is_missing():
    """And names a SETTABLE one. The message used to name azure_key_vault_name,
    which is not a config.py field and has no panel — so the one action it asked
    for was impossible to take."""
    _reset()
    try:
        svc._secret_environment("azure", {"FN_PORTAINER_API_KEY": "portainer-key"})
    except svc.CloudFunctionError as exc:
        assert "secrets_azure_kv_url" in str(exc), exc
    else:
        raise AssertionError("an Azure reference resolved with no vault configured")


def test_deploy_resolves_the_vault_the_derived_way():
    """The wiring, not the resolver. _azure_key_vault() has always accepted a URL or
    a name and derived one from the other, but deploy() read the name keys directly —
    so an operator who configured the vault as a URL (the only way the UI offers)
    got "nowhere for FN_DB_ADMIN_PASSWORD to come from" one step AFTER
    _stage_admin_secret had written that very credential into that very vault.
    """
    import inspect
    source = inspect.getsource(svc.deploy)
    assert "_azure_key_vault()" in source, (
        "deploy() must resolve the vault through _azure_key_vault(), which accepts "
        "the URL form the Secrets page actually saves")
    assert 'vault=_cfg("azure_key_vault_name")' not in source, (
        "deploy() is reading the vault name key directly again — it is not a "
        "config.py field, so it is permanently empty")


def test_an_invalid_environment_name_is_refused_by_name():
    """Two of the three clouds reject this at apply time, without saying which key."""
    _reset()
    try:
        svc._secret_environment("gcp", {"not-a-var": "x"})
    except svc.CloudFunctionError as exc:
        assert "not-a-var" in str(exc), exc
    else:
        raise AssertionError("an invalid environment variable name was accepted")


def test_no_references_means_no_variables():
    _reset()
    for cloud in ("aws", "azure", "gcp"):
        assert svc._secret_environment(cloud, None) == ({}, {}), cloud
        assert svc._secret_environment(cloud, {"FN_X": ""}) == ({}, {}), cloud


def test_every_option_the_module_reads_is_one_deploy_actually_passes():
    """The regression this pins cost the feature its credential path on two clouds:
    readable_secret_arns and db_admin_secret were accepted by deploy(), read by
    _build_tf_variables, and dropped in the gap between them — so the AWS role got no
    secretsmanager:GetSecretValue and GCP injected nothing, and a paired db_grant
    adapter deployed cleanly and then failed every grant at cold start."""
    import inspect
    import re as _re
    builder = inspect.getsource(svc._build_tf_variables)
    read = set(_re.findall(r'opts\.get\("([a-z_]+)"', builder))
    read |= set(_re.findall(r'opts\["([a-z_]+)"\]', builder))
    passed = set(_re.findall(r'^\s+"([a-z_]+)":', inspect.getsource(svc.deploy), _re.M))
    missing = read - passed
    assert not missing, (
        f"_build_tf_variables reads {sorted(missing)} from opts, but deploy() never "
        "puts them in the dict it passes")


# ── The bearer secret on Azure ────────────────────────────────────────────────
#
# The one cloud where it reaches neither an app setting NOR Terraform state: the
# dashboard writes it to Key Vault and the module is handed only a reference.

def test_the_azure_vault_is_derivable_from_either_setting():
    """Operators have set one or the other historically, and the name and the URL
    each determine the other."""
    _reset()
    CONF["secrets_azure_kv_url"] = "https://dash-kv.vault.azure.net/"
    name, url, _rg = svc._azure_key_vault()
    assert name == "dash-kv" and url == "https://dash-kv.vault.azure.net"

    _reset()
    CONF["azure_key_vault_name"] = "dash-kv"
    name, url, _rg = svc._azure_key_vault()
    assert name == "dash-kv" and url == "https://dash-kv.vault.azure.net"


def test_the_vault_resource_group_falls_back_to_the_dashboard_one():
    """Only needed for the module's data lookup, which is what lets it detect
    RBAC-vs-access-policy; most vaults live beside everything else."""
    _reset()
    CONF["azure_key_vault_name"] = "dash-kv"
    assert svc._azure_key_vault()[2] == "rg-dash"
    CONF["azure_key_vault_resource_group"] = "rg-security"
    assert svc._azure_key_vault()[2] == "rg-security"


def test_the_azure_reference_names_a_secret_derived_from_the_function_id():
    """Derived, not stored: apply and destroy rebuild it without touching the
    vault again."""
    _reset()
    CONF["azure_key_vault_name"] = "dash-kv"
    refs = svc.azure_bearer_reference("fn-123")
    assert refs["shared_secret_kv_uri"] == (
        "@Microsoft.KeyVault(SecretUri="
        "https://dash-kv.vault.azure.net/secrets/cloudfn-fn-123-bearer/)")
    assert refs["key_vault_name"] == "dash-kv"


def test_azure_vars_carry_the_reference_and_blank_the_literal():
    """The module's precondition wants exactly one of the two, and the literal is
    the one that would end up in state."""
    _reset()
    CONF["azure_key_vault_name"] = "dash-kv"
    got = _vars("azure", opts={"azure_bearer": svc.azure_bearer_reference("fn-1")})
    assert got["shared_secret"] == "", "the literal value still reached terraform"
    assert got["shared_secret_kv_uri"].startswith("@Microsoft.KeyVault(")
    assert got["key_vault_name"] == "dash-kv"
    assert "TOPSECRET" not in repr(got)


def test_azure_without_a_vault_is_refused_not_downgraded():
    """A deploy that quietly falls back to a readable app setting is the failure
    this whole path exists to stop."""
    _reset()
    assert svc.azure_bearer_reference("fn-1") == {}
    try:
        svc._stage_azure_bearer("fn-1", "TOPSECRET")
    except svc.CloudFunctionError as exc:
        assert "Key Vault" in str(exc), exc
        assert "TOPSECRET" not in str(exc)
    else:
        raise AssertionError("an Azure deploy proceeded with no vault configured")


def test_the_azure_reference_survives_stripping_but_the_value_never_appears():
    """The reference is needed at destroy time, and is a name rather than a
    credential — so it is kept, and keeping it leaks nothing."""
    _reset()
    CONF["azure_key_vault_name"] = "dash-kv"
    full = _vars("azure", opts={"azure_bearer": svc.azure_bearer_reference("fn-1")})
    safe = svc.strip_secrets(full)
    assert safe["shared_secret_kv_uri"] == full["shared_secret_kv_uri"]
    assert "TOPSECRET" not in repr(safe)


# ── Updating a deployed function ──────────────────────────────────────────────

def test_settings_merge_and_none_removes():
    """Removal needs a spelling of its own: a merge can only add, so without it
    there is no way to stop serving a database short of destroying the function."""
    _reset()
    current = {"FN_DB_NAMES": "appdb", "FN_DB_DRY_RUN": "1", "FN_DB_HOST": "db.internal"}
    assert svc.merged_environment(current, {"FN_DB_NAMES": "appdb,reporting"}) == {
        "FN_DB_NAMES": "appdb,reporting", "FN_DB_DRY_RUN": "1",
        "FN_DB_HOST": "db.internal"}
    assert svc.merged_environment(current, {"FN_DB_DRY_RUN": None}) == {
        "FN_DB_NAMES": "appdb", "FN_DB_HOST": "db.internal"}
    assert svc.merged_environment(current, {}) == current


def test_an_update_cannot_smuggle_in_a_plaintext_credential():
    """The deploy guard has to apply here too, or `environment` becomes the way
    around it."""
    _reset()
    import inspect
    source = inspect.getsource(svc.update_environment)
    assert "_reject_plaintext_secrets" in source, \
        "update_environment does not run the plaintext-credential guard"


def test_the_package_variables_are_the_ones_each_module_declares():
    """A mismatch is a 'no value for required variable' failure minutes into an
    apply, on a function that was working before the update."""
    _reset()
    aws = svc._package_variables("aws", "f1", "abc", "YWJj")
    assert aws == {"package_key": "function-packages/f1/abc.zip",
                   "package_sha256_b64": "YWJj"}
    assert svc._package_variables("gcp", "f1", "abc", "YWJj") == {
        "package_object": "function-packages/f1/abc.zip"}
    # Azure's is a SAS URL, which is a credential — stripped from persisted vars and
    # rebuilt by _reinject_secrets, so it must NOT be set here.
    assert svc._package_variables("azure", "f1", "abc", "YWJj") == {}


def test_an_update_applies_where_the_function_already_is():
    """The property that makes this an update rather than a second function: the
    terraform state for a function lives under its DEPLOY job's key, so an apply
    keyed on the update job's id would create a parallel one."""
    _reset()
    import inspect
    source = inspect.getsource(svc.run_update_apply)
    assert "_deploy_dir(row.deploy_job_id)" in source, \
        "run_update_apply does not apply in the original deploy directory"


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
