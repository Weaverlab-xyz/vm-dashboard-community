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
import inspect
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
        self.env_ref = kw.get("env_ref", "{}")


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


# ── Re-applying the invoker bindings ──────────────────────────────────────────
#
# The bindings were written ONCE, at deploy, from whatever the config key held then.
# An in-place update inherits the deploy's variables and a second deploy is refused
# while a service exists, so filling the key in afterwards — the normal order, since
# the brokers are often not known yet — reached nothing. Live on 2026-09-14: the
# service deployed with an empty key, its IAM policy named nobody, and the first
# Verify Functional Account was refused with 403 and an empty body.

def _with_attr(module, name, value):
    original = getattr(module, name)
    setattr(module, name, value)
    return lambda: setattr(module, name, original)


def _sync_harness(*, applied, configured, env_ref):
    """(restores, calls) for a service holding ``applied`` with ``configured`` set."""
    calls = []
    restores = [
        _with_service(_Row(status="available", env_ref=env_ref)),
        _with_config({"clouddb_ps_gcp_dbops_invokers": configured}),
        _with_attr(cloud_function_service, "deployed_tf_variables",
                   lambda db, fn_id: {"invoker_members": list(applied)}),
        _with_attr(cloud_function_service, "update_environment",
                   lambda db, **kw: calls.append(kw) or {"job_id": "job-9",
                                                        "tf_variables": {}}),
    ]
    return (lambda: [r() for r in reversed(restores)]), calls


def test_sync_invokers_is_a_noop_when_the_service_already_holds_them():
    """No job at all, rather than a job that applies nothing: an operator watching for
    drift must be able to trust that a button that did something says so."""
    restore, calls = _sync_harness(
        applied=["serviceAccount:a@p.iam.gserviceaccount.com"],
        configured="a@p.iam.gserviceaccount.com",
        env_ref='{"FN_DBOPS_ALLOWED_INVOKERS": "a@p.iam.gserviceaccount.com"}')
    try:
        result = clouddb_dbops_service.sync_invokers(None, region="us-east1")
        assert result["ok"] and result["changed"] is False, result
        assert calls == [], calls
    finally:
        restore()


def test_sync_invokers_applies_config_in_place_and_moves_both_gates_together():
    """IAM and FN_DBOPS_ALLOWED_INVOKERS are two gates in two trust domains ON PURPOSE,
    but a service holding one list in its policy and another in its environment is not
    a boundary — it is a 403 whose cause depends on which gate you happen to fail."""
    restore, calls = _sync_harness(
        applied=[], configured="a@p.iam.gserviceaccount.com",
        env_ref="{}")
    try:
        result = clouddb_dbops_service.sync_invokers(None, region="us-east1")
        assert result["ok"] and result["changed"] is True, result
        assert len(calls) == 1, calls
        assert calls[0]["invoker_members"] == [
            "serviceAccount:a@p.iam.gserviceaccount.com"], calls
        assert calls[0]["environment"] == {
            "FN_DBOPS_ALLOWED_INVOKERS": "a@p.iam.gserviceaccount.com"}, calls
        # The SAME function row — the whole point is that the URL, and so every
        # managed-system address already registered against it, survives.
        assert calls[0]["fn_id"] == "fn-1", calls
    finally:
        restore()


def test_sync_invokers_refuses_when_the_region_has_nothing_deployed():
    restore = _with_service(None)
    try:
        result = clouddb_dbops_service.sync_invokers(None, region="us-east1")
        assert result["ok"] is False and "us-east1" in result["reason"], result
    finally:
        restore()


def test_drift_is_a_set_comparison_not_a_list_one():
    """The module wraps the list in toset(), so order is not a difference — and an IAM
    member is not case-sensitive. Reporting either as drift would offer a button that
    applies nothing, forever."""
    restore, _calls = _sync_harness(
        applied=["serviceAccount:B@p.iam.gserviceaccount.com",
                 "serviceAccount:a@p.iam.gserviceaccount.com"],
        configured="a@p.iam.gserviceaccount.com, b@p.iam.gserviceaccount.com",
        env_ref="{}")
    try:
        assert not clouddb_dbops_service.invokers_drifted(None, "us-east1")
    finally:
        restore()


def test_an_unavailable_service_never_reports_drift():
    """It has a job running against it; "differs from config" is not actionable while
    terraform is mid-apply, and the button would race it."""
    for status in ("deploying", "failed"):
        restore = _with_service(_Row(status=status))
        try:
            assert not clouddb_dbops_service.invokers_drifted(None, "us-east1")
        finally:
            restore()


def test_bindings_are_the_ONE_non_environment_setting_an_update_may_change():
    """Everything else is inherited from the deploy job's variables, so a setting the
    update does not name cannot drift from what was applied. None means "leave them";
    an empty list is a real request to revoke, and must not be confused with it."""
    signature = inspect.signature(cloud_function_service.update_environment)
    assert signature.parameters["invoker_members"].default is None, signature
    source = inspect.getsource(cloud_function_service.update_environment)
    assert "if invoker_members is not None:" in source, source
    # GCP alone declares the variable; handing an unknown one to the AWS or Azure
    # module is an apply error three minutes in, not a no-op.
    assert 'row.cloud != "gcp"' in source, source


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


def test_concurrency_pins_a_whole_vcpu():
    """The live failure this pins: a gen2 function derives CPU from memory (256M ->
    ~0.17 vCPU) and Cloud Run then refuses the service with "Total cpu < 1 is not
    supported with concurrency > 1" — a 400 at APPLY, after the shared secret and
    its accessor binding already exist, so the retry needs a destroy first."""
    source = _tf_source()
    assert 'available_cpu = var.concurrency > 1 ? "1" : null' in source, source
    # Wired, not merely computed — the ingress_settings lesson.
    assert "available_cpu         = local.available_cpu" in source, source


def test_the_dbops_deploy_asks_for_more_than_the_platform_floor():
    """512M, not the 256M module default: the workload imports cryptography, pytds
    and pymysql at cold start and holds a TLS session per concurrent request. An OOM
    kill lands in the same mid-rotation window min_instances exists to close."""
    assert clouddb_dbops_service._DEFAULT_MEMORY_MB >= 512, (
        clouddb_dbops_service._DEFAULT_MEMORY_MB)
    source = inspect.getsource(clouddb_dbops_service.run_deploy)
    assert "memory_mb=_DEFAULT_MEMORY_MB" in source, source


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
