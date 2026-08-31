"""The DB-Ops service deploy path — audience resolution, granularity, deploy flags.

The properties worth pinning here are the ones whose failure is a working-looking
misconfiguration rather than an error:

  * a DEPLOYED per-region service beats the flat config key, not the other way round
  * the audience is a bare origin (the plugin rejects a path, query or fragment)
  * only an AVAILABLE service contributes an audience
  * the front door and the placement cannot be weakened for this workload
  * the module's new knobs are actually reachable from the service

No database and no cloud: the row lookups are stubbed, and the terraform assertions
read the .tf file. Stdlib only.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from web_dashboard.services import (clouddb_dbops_service, cloud_function_package,
                                    cloud_function_service)

_TF = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "terraform",
                   "cloud_function", "gcp_cloudrun", "main.tf")


class _Row:
    def __init__(self, **kw):
        self.id = kw.get("id", "fn-1")
        self.name = kw.get("name", clouddb_dbops_service.SERVICE_NAME)
        self.status = kw.get("status", "available")
        self.invoke_url = kw.get("invoke_url", "")
        self.region = kw.get("region", "us-east1")


def _with_service(row):
    """Swap find_for_region for a stub, returning a restore callable."""
    original = clouddb_dbops_service.find_for_region
    clouddb_dbops_service.find_for_region = lambda db, region: row
    return lambda: setattr(clouddb_dbops_service, "find_for_region", original)


def _with_config(mapping):
    from web_dashboard.services import config_service
    original = config_service.get
    config_service.get = lambda key, *a, **kw: mapping.get(key, "")
    return lambda: setattr(config_service, "get", original)


# ── The audience ──────────────────────────────────────────────────────────────

def test_origin_strips_path_query_and_fragment():
    """Field 4 is used verbatim as both the request target and the token audience,
    and ps_resource_service refuses anything but a bare origin — so a trailing slash
    out of a terraform output is an address Password Safe will reject."""
    cases = {
        "https://bt-dbops-123.us-east1.run.app": "https://bt-dbops-123.us-east1.run.app",
        "https://bt-dbops-123.us-east1.run.app/": "https://bt-dbops-123.us-east1.run.app",
        "https://x.run.app/v1/credential-op?a=1#f": "https://x.run.app",
        "": "",
        "not-a-url": "",
    }
    for raw, want in cases.items():
        assert clouddb_dbops_service.origin(raw) == want, (raw, want)


def test_only_an_available_service_contributes_an_audience():
    """A half-deployed service has a URL that may still change; stamping it into a
    managed-system address would leave Password Safe holding an address for a service
    that never finished."""
    for status in ("deploying", "failed", "deleted"):
        restore = _with_service(_Row(status=status, invoke_url="https://x.run.app"))
        try:
            assert clouddb_dbops_service.audience_for_region(None, "us-east1") == ""
        finally:
            restore()
    restore = _with_service(_Row(status="available", invoke_url="https://x.run.app"))
    try:
        assert clouddb_dbops_service.audience_for_region(None, "us-east1") == "https://x.run.app"
    finally:
        restore()


def test_a_deployed_service_beats_the_flat_config_key():
    """THE ordering decision. The instinct is that explicit config wins; a Cloud Run
    service on Direct VPC egress is region-locked, so a global key would address a
    rotation for a europe-west1 database at a us-east1 service that cannot reach the
    instance — and a rotation that times out may already have applied."""
    from web_dashboard.services import cloud_database_service

    row = type("R", (), {"id": "db-1", "region": "europe-west1"})()
    restore_cfg = _with_config({"clouddb_ps_gcp_dbops_audience": "https://operator.example"})
    restore_svc = _with_service(_Row(status="available",
                                     invoke_url="https://deployed.run.app"))
    try:
        assert cloud_database_service._dbops_audience(row, object()) == \
            "https://deployed.run.app"
    finally:
        restore_svc()
        restore_cfg()


def test_the_flat_key_still_serves_a_region_with_no_deployed_service():
    """Existing installs must not change: a BYO service keeps working, and so does a
    region the dashboard has not deployed into."""
    from web_dashboard.services import cloud_database_service

    row = type("R", (), {"id": "db-1", "region": "europe-west1"})()
    restore_cfg = _with_config({"clouddb_ps_gcp_dbops_audience": "https://operator.example"})
    restore_svc = _with_service(None)
    try:
        assert cloud_database_service._dbops_audience(row, object()) == \
            "https://operator.example"
        # …and with no session at all (a caller that has none) it is the only source.
        assert cloud_database_service._dbops_audience(row, None) == \
            "https://operator.example"
    finally:
        restore_svc()
        restore_cfg()


def test_an_audience_lookup_failure_falls_back_rather_than_breaking_onboarding():
    from web_dashboard.services import cloud_database_service

    def _boom(db, region):
        raise RuntimeError("table missing")

    row = type("R", (), {"id": "db-1", "region": "us-east1"})()
    original = clouddb_dbops_service.audience_for_region
    clouddb_dbops_service.audience_for_region = _boom
    restore_cfg = _with_config({"clouddb_ps_gcp_dbops_audience": "https://fallback.example"})
    try:
        assert cloud_database_service._dbops_audience(row, object()) == \
            "https://fallback.example"
    finally:
        clouddb_dbops_service.audience_for_region = original
        restore_cfg()


# ── Invokers ──────────────────────────────────────────────────────────────────

def test_a_bare_email_is_prefixed_and_blanks_are_dropped():
    """An operator copying an email out of the console has no reason to know
    Terraform wants serviceAccount:, and a missing prefix is an apply error 90
    seconds in rather than a validation error at the click."""
    restore = _with_config({"clouddb_ps_gcp_dbops_invokers":
                            " a@p.iam.gserviceaccount.com , ,serviceAccount:b@p.iam.gserviceaccount.com "})
    try:
        assert clouddb_dbops_service.invoker_members() == [
            "serviceAccount:a@p.iam.gserviceaccount.com",
            "serviceAccount:b@p.iam.gserviceaccount.com"], \
            clouddb_dbops_service.invoker_members()
    finally:
        restore()


def test_ingress_defaults_to_public_because_on_prem_brokers_exist():
    restore = _with_config({})
    try:
        assert clouddb_dbops_service.ingress_setting() == "ALLOW_ALL"
    finally:
        restore()
    restore = _with_config({"clouddb_ps_gcp_dbops_ingress": "internal"})
    try:
        assert clouddb_dbops_service.ingress_setting() == "ALLOW_INTERNAL_AND_GCLB"
    finally:
        restore()


# ── The workload's deploy constraints ─────────────────────────────────────────

def test_ps_dbops_is_gcp_only():
    assert cloud_function_service.clouds_for("ps_dbops") == ("gcp",), \
        cloud_function_service.clouds_for("ps_dbops")


def test_an_open_front_door_or_a_public_deploy_is_refused():
    """Both would deploy cleanly. The first turns a credential-changing endpoint into
    an open door; the second produces a service that fails every request."""
    for kwargs, expect in (
        ({"auth_mode": "none", "network_mode": "vpc"}, "auth_mode"),
        ({"auth_mode": "run_invoker", "network_mode": "public"}, "network_mode"),
    ):
        try:
            cloud_function_service._check_front_door("ps_dbops", **kwargs)
        except cloud_function_service.CloudFunctionError as exc:
            assert expect in str(exc), (kwargs, exc)
        else:
            raise AssertionError(f"_check_front_door allowed {kwargs}")
    # And the combination the deploy path actually uses is fine.
    cloud_function_service._check_front_door(
        "ps_dbops", auth_mode="run_invoker", network_mode="vpc")
    # Every other workload is untouched — this must not become a global tightening.
    cloud_function_service._check_front_door(
        "db_grant", auth_mode="none", network_mode="public")


def test_the_drivers_are_vendored_for_ps_dbops():
    """Without this the package builds and the function 500s on its first connect,
    at cold start, in a private subnet."""
    vendored = cloud_function_package._WORKLOAD_VENDOR.get("ps_dbops", ())
    for dist in ("pymysql", "pytds", "OpenSSL", "cryptography"):
        assert dist in vendored, (dist, vendored)


# ── The Terraform variables ───────────────────────────────────────────────────

def _tf_source() -> str:
    with open(_TF, encoding="utf-8") as fh:
        return fh.read()


def test_the_new_module_variables_are_wired_not_just_declared():
    """ingress_settings was the cautionary tale: declared, documented, and never
    passed by _build_tf_variables, so every GCP function was ALLOW_ALL whatever the
    module said."""
    source = _tf_source()
    for fragment in ("min_instance_count    = var.min_instances",
                     "max_instance_request_concurrency",
                     "FN_AUTH_MODE_FRONT_DOOR = var.auth_mode"):
        assert fragment in source, fragment

    variables = cloud_function_service._build_tf_variables(
        cloud="gcp", region="us-east1", name="bt-dbops", workload="ps_dbops",
        package={"bucket": "b", "key": "k", "sha256_b64": "x"}, network={},
        opts={"shared_secret": "s", "project": "p", "ingress_settings": "ALLOW_ALL",
              "min_instances": 1, "concurrency": 8, "max_instances": 5,
              "invoker_members": ["serviceAccount:a@p.iam.gserviceaccount.com"]})
    assert variables["min_instances"] == 1, variables
    assert variables["concurrency"] == 8, variables
    assert variables["max_instances"] == 5, variables
    assert variables["ingress_settings"] == "ALLOW_ALL", variables
    assert variables["invoker_members"] == ["serviceAccount:a@p.iam.gserviceaccount.com"]


def test_existing_functions_get_the_old_behaviour_when_nothing_is_passed():
    """db_grant and every hand-deployed function must plan byte-identically."""
    variables = cloud_function_service._build_tf_variables(
        cloud="gcp", region="us-east1", name="jit-mysql-abc", workload="db_grant",
        package={"bucket": "b", "key": "k", "sha256_b64": "x"}, network={},
        opts={"shared_secret": "s", "project": "p"})
    assert variables["min_instances"] == 0, variables
    assert variables["concurrency"] == 0, variables
    assert variables["max_instances"] == cloud_function_service._DEFAULT_MAX_INSTANCES
    assert variables["ingress_settings"] == "ALLOW_ALL", variables
    assert variables["invoker_members"] == [], variables


def test_the_module_refuses_the_wildcard_principals():
    """Not a comment, a Terraform validation — allUsers on a credential-changing
    service is the mistake this whole front door exists to prevent."""
    source = _tf_source()
    assert 'variable "invoker_members"' in source
    block = source.split('variable "invoker_members"', 1)[1].split("\nvariable ", 1)[0]
    assert "validation" in block and "allusers" in block.lower(), block
    assert "allauthenticatedusers" in block.lower(), block


def test_the_deploy_defaults_match_what_the_plugin_article_calls_load_bearing():
    assert clouddb_dbops_service._DEFAULT_MIN_INSTANCES == 1
    assert clouddb_dbops_service._DEFAULT_CONCURRENCY == 8
    assert clouddb_dbops_service._DEFAULT_TIMEOUT_SECONDS == 120


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
