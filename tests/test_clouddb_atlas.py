"""MongoDB Atlas provisioning — the parts that can be proven without Atlas.

An Atlas row keeps ``cloud`` = the backing provider (the PRA gateway's cloud) and is
marked by ``provider="atlas"``. What matters here:

* the -var set matches the module (terraform refuses an undeclared -var at apply time,
  inside a background job);
* the cloud region becomes Atlas's own region name, and an unknown one is refused up
  front rather than failing minutes into an apply;
* the Atlas module runs with the service account's environment, never the cloud's;
* the access list is the gateway's /32 plus configured extras;
* the Entitle gate admits an Atlas cluster and still refuses self-hosted MongoDB.

Stubs mirror test_cloud_db_tf_vars.py. Runs under pytest or standalone:
    python tests/test_clouddb_atlas.py
"""
import os
import re
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

CONF = {}


class _Settings:
    def __getattr__(self, _key):
        return ""


def _install_stubs():
    confmod = types.ModuleType("web_dashboard.config")
    confmod.settings = _Settings()
    sys.modules["web_dashboard.config"] = confmod

    dbmod = types.ModuleType("web_dashboard.database")
    dbmod.CloudDatabase = type("CloudDatabase", (), {})
    dbmod.Job = type("Job", (), {})
    sys.modules["web_dashboard.database"] = dbmod

    cfg = types.ModuleType("web_dashboard.services.config_service")
    cfg.get = lambda key, default="": CONF.get(key, default)
    cfg.get_fresh = lambda key, default="": CONF.get(key, default)
    cfg.set = lambda key, val: CONF.__setitem__(key, val)
    cfg.get_bool = lambda key, default=False: bool(CONF.get(key, default))
    sys.modules["web_dashboard.services.config_service"] = cfg

    tpe = types.ModuleType("web_dashboard.services.terraform_provider_env")
    tpe.provider_env = lambda cloud: {"CLOUD_ENV_FOR": cloud}
    sys.modules["web_dashboard.services.terraform_provider_env"] = tpe

    for name in ("job_service", "terraform"):
        sys.modules[f"web_dashboard.services.{name}"] = types.ModuleType(
            f"web_dashboard.services.{name}")


_install_stubs()
try:
    from web_dashboard.services import cloud_database_service as svc
except Exception as exc:  # pragma: no cover
    try:
        import pytest
        pytest.skip(f"cloud_database_service import unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)


def _build(cloud="aws", region="us-east-1", **opts):
    return svc._build_tf_variables(
        engine="mongodb", cloud=cloud, region=region, db_id="abcdef0123456789",
        db_name="appdb", master_username="dbadmin", master_password="s3cr3t", opts=opts)


def _row(**kw):
    return types.SimpleNamespace(**{"provider": "atlas", "cloud": "aws", **kw})


# ── the matrix ───────────────────────────────────────────────────────────────

def test_mongodb_is_atlas_on_every_backing_cloud():
    for cloud in ("aws", "azure", "gcp"):
        assert ("mongodb", cloud) in svc._IMPLEMENTED, cloud
        assert svc._PROVIDER[("mongodb", cloud)] == "atlas", cloud
        assert svc.template_dir("mongodb", cloud).endswith(
            os.path.join("terraform", "db_atlas_mongodb")), cloud
    assert ("mongodb", "oci") not in svc._IMPLEMENTED


# ── the -var set ─────────────────────────────────────────────────────────────

def test_vars_are_all_declared_by_the_module():
    CONF.clear()
    CONF["atlas_org_id"] = "5f0000000000000000000001"
    with open(os.path.join(_ROOT, "terraform", "db_atlas_mongodb", "main.tf"), encoding="utf-8") as fh:
        declared = set(re.findall(r'^variable "([^"]+)"', fh.read(), re.M))
    for cloud, region in (("aws", "us-east-1"), ("azure", "eastus2"), ("gcp", "us-central1")):
        tf = _build(cloud, region)
        assert set(tf) <= declared, (cloud, set(tf) - declared)
        assert tf["org_id"] == "5f0000000000000000000001"
    CONF.clear()


def test_backing_provider_and_region_name_follow_the_row():
    assert _build("aws", "us-west-2")["backing_provider"] == "AWS"
    assert _build("aws", "us-west-2")["atlas_region"] == "US_WEST_2"
    assert _build("azure", "eastus2")["backing_provider"] == "AZURE"
    assert _build("azure", "eastus2")["atlas_region"] == "US_EAST_2"
    assert _build("gcp", "us-central1")["backing_provider"] == "GCP"
    assert _build("gcp", "us-central1")["atlas_region"] == "CENTRAL_US"


def test_an_explicit_atlas_region_wins_and_an_unknown_one_is_refused():
    assert _build("gcp", "me-central2", atlas_region="ME_CENTRAL_2")["atlas_region"] == "ME_CENTRAL_2"
    try:
        _build("gcp", "me-central2")
    except svc.CloudDatabaseError as exc:
        assert "atlas_region" in str(exc)
    else:
        raise AssertionError("an unmapped region was guessed instead of refused")


def test_tier_defaults_to_flex_then_config_then_form():
    CONF.clear()
    assert _build()["tier"] == "FLEX"
    CONF["atlas_default_tier"] = "m10"
    assert _build()["tier"] == "M10"
    assert _build(atlas_tier="m20")["tier"] == "M20"
    CONF.clear()


def test_access_list_is_the_gateway_slash32_plus_extras():
    CONF.clear()
    assert svc._atlas_access_cidrs("203.0.113.7") == ["203.0.113.7/32"]
    CONF["atlas_extra_access_cidrs"] = "52.45.229.219, 10.0.0.0/8, 203.0.113.7/32"
    assert svc._atlas_access_cidrs("203.0.113.7") == [
        "203.0.113.7/32", "52.45.229.219/32", "10.0.0.0/8"]
    assert svc._atlas_access_cidrs(None) == ["52.45.229.219/32", "10.0.0.0/8", "203.0.113.7/32"]
    CONF.clear()


def test_the_provision_vars_never_carry_the_access_list_secretly_empty():
    # Filled in run_provision_apply from the gateway's egress IP; the builder only
    # passes an explicit list through. The apply path refuses an empty one.
    assert _build()["access_cidrs"] == []
    src = open(os.path.join(_ROOT, "web_dashboard", "services",
                            "cloud_database_service.py"), encoding="utf-8").read()
    assert 'tf_variables["access_cidrs"] = _atlas_access_cidrs(egress_ip)' in src
    assert "reported no egress IP" in src


# ── credentials ──────────────────────────────────────────────────────────────

def test_the_atlas_module_runs_with_the_service_account_not_the_cloud():
    CONF.clear()
    CONF.update(atlas_client_id="mdb_sa_id", atlas_client_secret="mdb_sa_secret")
    assert svc._tf_env(_row()) == {"MONGODB_ATLAS_CLIENT_ID": "mdb_sa_id",
                                   "MONGODB_ATLAS_CLIENT_SECRET": "mdb_sa_secret"}
    assert svc._tf_env(_row(provider="rds")) == {"CLOUD_ENV_FOR": "aws"}
    CONF.clear()


def test_missing_atlas_credentials_fail_loudly():
    CONF.clear()
    try:
        svc._tf_env(_row())
    except svc.CloudDatabaseError as exc:
        assert "service account" in str(exc)
    else:
        raise AssertionError("ran the Atlas module with no credentials")


# ── Entitle ──────────────────────────────────────────────────────────────────

def test_entitle_admits_atlas_and_still_refuses_self_hosted_mongodb():
    assert svc._entitle_ineligible_reason("mongodb", "atlas", source="provisioned") is None
    assert svc._entitle_ineligible_reason("mongodb", "registered", source="registered")
    reason = svc._entitle_ineligible_reason("mongodb", None, source="provisioned")
    assert reason and "self-hosted" in reason


def test_mongodb_session_database_is_the_auth_db():
    row = types.SimpleNamespace(engine="mongodb", source="provisioned", db_name=None, id="x")
    assert svc.connection_db_name(row) == "admin"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as exc:
            failures += 1
            print(f"FAIL {fn.__name__}: {exc!r}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
