"""Unit tests for services/workload_credential_lease.py.

The module loads by file path: its only top-level imports are stdlib plus
``sqlalchemy.text``, and every relative import sits inside a function. So the decision
logic — usability windows, backoff, the memo, config resolution — is testable with no
package chain, no database and no network.

Config access goes through ``_cfg`` / ``_cfg_bool``, which are stubbed per test. Note the
un-stubbed behaviour is itself an invariant worth asserting: with no config reachable at
all, the module must conclude "not on the dynamic tier" rather than erroring, because that
is the state of every deployment that has never heard of this feature.

Runs under pytest, or standalone:
    python tests/test_workload_credential_lease.py
"""
import importlib.util
import os
import sys
from datetime import datetime, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PATH = os.path.join(_ROOT, "web_dashboard", "services",
                     "workload_credential_lease.py")

_spec = importlib.util.spec_from_file_location("workload_credential_lease", _PATH)
lease = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lease)

NOW = datetime(2026, 8, 20, 12, 0, 0)


def _read(*parts):
    with open(os.path.join(_ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


def _stub_cfg(values=None, flags=None):
    """Point the module's config accessors at plain dicts."""
    values = values or {}
    flags = flags or {}
    lease._cfg = lambda key, default="": values.get(key, default)
    lease._cfg_bool = lambda key: bool(flags.get(key, False))


def _reset():
    lease.invalidate()


def _snap(**over):
    base = {"cloud": "aws", "purpose": "provision", "payload": "enc",
            "lease_id": "L1", "issued_at": NOW - timedelta(hours=1),
            "expires_at": NOW + timedelta(hours=1), "last_error": None,
            "last_attempt_at": None, "consecutive_failures": 0,
            "cooldown_until": None, "claim_until": None, "claim_owner": None}
    base.update(over)
    return base


# ── Usability window ─────────────────────────────────────────────────────────

def test_a_valid_lease_is_usable():
    assert lease._usable(_snap(), NOW) is True


def test_a_row_with_no_payload_is_not_usable():
    assert lease._usable(_snap(payload=None), NOW) is False


def test_a_row_with_no_expiry_is_not_usable():
    # An unknown expiry is treated as unusable rather than assumed valid: handing out a
    # dead credential fails somewhere else, looking like a permissions problem.
    assert lease._usable(_snap(expires_at=None), NOW) is False


def test_an_expired_lease_is_not_usable():
    assert lease._usable(_snap(expires_at=NOW - timedelta(minutes=1)), NOW) is False


def test_a_lease_inside_the_safety_window_is_not_usable():
    # A call that starts 30s before expiry can still be in flight when it dies.
    near = NOW + timedelta(seconds=lease._EXPIRY_SAFETY_SECONDS - 30)
    assert lease._usable(_snap(expires_at=near), NOW) is False


def test_a_lease_just_outside_the_safety_window_is_usable():
    ok = NOW + timedelta(seconds=lease._EXPIRY_SAFETY_SECONDS + 30)
    assert lease._usable(_snap(expires_at=ok), NOW) is True


def test_a_missing_row_is_not_usable():
    assert lease._usable(None, NOW) is False


def test_snap_of_nothing_is_none():
    assert lease._snap(None) is None


# ── Backoff ──────────────────────────────────────────────────────────────────

def test_cooldown_grows_with_consecutive_failures():
    first = lease._cooldown_seconds(1)
    second = lease._cooldown_seconds(2)
    assert second > first


def test_cooldown_is_capped():
    assert lease._cooldown_seconds(50) == lease._COOLDOWN_MAX_SECONDS


def test_cooldown_of_a_first_failure_is_the_base():
    assert lease._cooldown_seconds(1) == lease._FAIL_BASE_SECONDS


# ── Memo ─────────────────────────────────────────────────────────────────────

def test_a_memoised_credential_is_returned():
    _reset()
    lease._memo_put("aws", "provision", {"access_key_id": "A"},
                    NOW + timedelta(hours=1))
    assert lease._memo_get("aws", "provision", NOW) == {"access_key_id": "A"}


def test_the_memo_respects_the_credentials_own_expiry():
    # The memo TTL is about staleness across processes; it must never outlive the
    # credential it is caching.
    _reset()
    lease._memo_put("aws", "provision", {"access_key_id": "A"},
                    NOW + timedelta(seconds=5))
    assert lease._memo_get("aws", "provision", NOW) is None


def test_an_absent_memo_entry_is_none():
    _reset()
    assert lease._memo_get("aws", "provision", NOW) is None


def test_invalidate_can_target_one_cloud():
    _reset()
    exp = NOW + timedelta(hours=1)
    lease._memo_put("aws", "provision", {"k": "aws"}, exp)
    lease._memo_put("azure", "provision", {"k": "azure"}, exp)
    lease.invalidate("aws")
    assert lease._memo_get("aws", "provision", NOW) is None
    assert lease._memo_get("azure", "provision", NOW) == {"k": "azure"}


def test_invalidate_with_no_argument_clears_everything():
    _reset()
    exp = NOW + timedelta(hours=1)
    lease._memo_put("aws", "provision", {"k": "aws"}, exp)
    lease._memo_put("azure", "readonly", {"k": "azure"}, exp)
    lease.invalidate()
    assert lease._memo_get("aws", "provision", NOW) is None
    assert lease._memo_get("azure", "readonly", NOW) is None


# ── Config resolution ────────────────────────────────────────────────────────

def test_both_gates_are_required_to_enable_a_cloud():
    # Enabling the feature to browse secrets must not reroute a cloud's credentials as a
    # side effect, so the master flag alone is not enough.
    _stub_cfg(flags={"workload_credentials_enabled": True})
    assert lease.dynamic_enabled("aws") is False
    _stub_cfg(flags={"wlc_aws_enabled": True})
    assert lease.dynamic_enabled("aws") is False
    _stub_cfg(flags={"workload_credentials_enabled": True, "wlc_aws_enabled": True})
    assert lease.dynamic_enabled("aws") is True


def test_gcp_can_never_be_on_the_dynamic_tier():
    # Workload Credentials does not mint GCP credentials. This is a design boundary, so
    # even a stray wlc_gcp_enabled row must not turn it on.
    _stub_cfg(flags={"workload_credentials_enabled": True, "wlc_gcp_enabled": True})
    assert lease.dynamic_enabled("gcp") is False


def test_an_unknown_cloud_is_never_enabled():
    _stub_cfg(flags={"workload_credentials_enabled": True, "wlc_oci_enabled": True})
    assert lease.dynamic_enabled("oci") is False


def test_the_readonly_secret_falls_back_to_the_provisioning_secret():
    # The read-only split is an optional refinement; an operator who has not created the
    # second dynamic secret should still get a working dashboard.
    _stub_cfg(values={"wlc_aws_secret_name": "dashboard-provision"})
    assert lease.secret_name_for("aws", lease.PURPOSE_READONLY) == "dashboard-provision"


def test_the_readonly_secret_is_used_when_it_is_set():
    _stub_cfg(values={"wlc_aws_secret_name": "dashboard-provision",
                      "wlc_aws_readonly_secret_name": "dashboard-readonly"})
    assert lease.secret_name_for("aws", lease.PURPOSE_READONLY) == "dashboard-readonly"


def test_the_provisioning_secret_never_falls_back_to_readonly():
    # Silently provisioning with a read-only session would fail deep inside a job.
    _stub_cfg(values={"wlc_aws_readonly_secret_name": "dashboard-readonly"})
    assert lease.secret_name_for("aws", lease.PURPOSE_PROVISION) == ""


def test_source_reports_dynamic_when_enabled():
    _stub_cfg(flags={"workload_credentials_enabled": True, "wlc_aws_enabled": True})
    assert lease.source_for("aws") == "dynamic"


def test_source_reports_static_when_keys_are_present():
    _stub_cfg(values={"aws_access_key_id": "AKIA", "aws_secret_access_key": "s"})
    assert lease.source_for("aws") == "static"


def test_source_reports_unconfigured_when_there_is_nothing():
    _stub_cfg()
    assert lease.source_for("aws") == "unconfigured"


def test_azure_source_reads_its_own_keys():
    _stub_cfg(values={"azure_client_id": "cid", "azure_client_secret": "sec"})
    assert lease.source_for("azure") == "static"


# ── Subprocess env ───────────────────────────────────────────────────────────

def test_subprocess_env_carries_the_session_token():
    # The field all four call sites were missing. Without it terraform and Packer fail
    # InvalidClientTokenId while boto3 calls succeed.
    original = lease.credentials
    lease.credentials = lambda cloud, purpose=None: {
        "access_key_id": "ASIA", "secret_access_key": "s", "session_token": "tok"}
    try:
        env = lease.aws_subprocess_env()
    finally:
        lease.credentials = original
    assert env == {"AWS_ACCESS_KEY_ID": "ASIA",
                   "AWS_SECRET_ACCESS_KEY": "s",
                   "AWS_SESSION_TOKEN": "tok"}


def test_subprocess_env_is_none_when_not_on_the_dynamic_tier():
    original = lease.credentials
    lease.credentials = lambda cloud, purpose=None: None
    try:
        assert lease.aws_subprocess_env() is None
    finally:
        lease.credentials = original


# ── Fail-safe defaults ───────────────────────────────────────────────────────

def test_with_no_config_reachable_nothing_is_on_the_dynamic_tier():
    """The state of every deployment that has never heard of this feature.

    ``_cfg`` swallows a failed config import and returns the default, so a module loaded
    with no package chain must conclude "static tier" rather than raising. If this ever
    inverts, every existing install breaks on upgrade.
    """
    _spec2 = importlib.util.spec_from_file_location("wcl_fresh", _PATH)
    fresh = importlib.util.module_from_spec(_spec2)
    _spec2.loader.exec_module(fresh)
    assert fresh.dynamic_enabled("aws") is False
    assert fresh.credentials("aws") is None
    assert fresh.aws_subprocess_env() is None
    assert fresh.refresh("aws") is False


# ── Invariants that must hold in the source ──────────────────────────────────

def test_a_failure_never_writes_the_payload():
    """The whole reason the table exists.

    A failed generate must leave the last working credential in place. If _record_failure
    ever touches payload or expires_at, a transient error blanks the credential — which
    reads as "this cloud was never on the dynamic tier", the most confusing state
    available.
    """
    src = _read("web_dashboard", "services", "workload_credential_lease.py")
    body = src.split("def _record_failure", 1)[1].split("\ndef ", 1)[0]
    assert "row.payload" not in body
    assert "row.expires_at" not in body
    assert "row.lease_id" not in body


def test_the_payload_is_encrypted_at_rest():
    # It holds a live cloud credential, so it gets the same treatment as app_config.
    src = _read("web_dashboard", "services", "workload_credential_lease.py")
    success = src.split("def _record_success", 1)[1].split("\ndef ", 1)[0]
    assert "encrypt_value" in success
    assert "json.dumps" in success


def test_the_advisory_lock_is_transaction_scoped():
    # A session-scoped lock leaks through the connection pool and has already deadlocked
    # startup in this repo. It must also never be held across the provider call.
    src = _read("web_dashboard", "services", "workload_credential_lease.py")
    assert "pg_try_advisory_xact_lock" in src
    assert "pg_advisory_lock" not in src


def test_all_four_aws_credential_sites_handle_the_dynamic_tier():
    """The drift guard.

    Four independent places build AWS credentials, and a fifth added later that forgets
    the lease would silently keep using a static key the operator may have retired.
    """
    checks = [
        (("web_dashboard", "services", "aws_service.py"), "workload_credential_lease"),
        (("web_dashboard", "services", "terraform_provider_env.py"), "aws_subprocess_env"),
        (("web_dashboard", "services", "terraform.py"), "aws_subprocess_env"),
        (("web_dashboard", "services", "packer_build_service.py"), "aws_subprocess_env"),
    ]
    for parts, needle in checks:
        assert needle in _read(*parts), f"{parts[-1]} does not consult the lease store"


def test_the_boto_kwargs_path_sets_a_session_token():
    src = _read("web_dashboard", "services", "aws_service.py")
    body = src.split("def _aws_kwargs(", 1)[1].split("\ndef ", 1)[0]
    assert "aws_session_token" in body


def test_the_boto_kwargs_path_fails_closed():
    # It must not swallow LeaseUnavailable and fall through to the static branch.
    src = _read("web_dashboard", "services", "aws_service.py")
    body = src.split("def _aws_kwargs(", 1)[1].split("\ndef ", 1)[0]
    assert "raise AWSError" in body


def test_the_model_exists_with_the_documented_key():
    src = _read("web_dashboard", "database.py")
    assert "class WorkloadCredentialLease" in src
    assert 'workload_credential_lease' in src
    # claim_* rather than lease_*: "lease" already means the provider-issued credential.
    assert "claim_until" in src and "claim_owner" in src


# ── Purpose collapsing (the duplicate-issuance bug) ──────────────────────────
#
# Iterating a static PURPOSES list minted a second lease from the SAME dynamic secret
# under a different label. Two rows, both refreshing forever, nothing reading the copy,
# and the only symptom is the invoice. These tests exist so that cannot come back.

def test_no_purposes_when_the_cloud_is_not_on_the_dynamic_tier():
    _stub_cfg(values={"wlc_aws_secret_name": "dashboard-provision"})
    assert lease.purposes_for("aws") == ()


def test_no_purposes_when_no_dynamic_secret_is_named():
    _stub_cfg(flags={"workload_credentials_enabled": True, "wlc_aws_enabled": True})
    assert lease.purposes_for("aws") == ()


def test_only_provision_when_there_is_no_distinct_readonly_secret():
    # The bug: this used to return both, and the readonly one resolved to the SAME
    # dynamic secret name — a second billable issuance for one credential.
    _stub_cfg(values={"wlc_aws_secret_name": "dashboard-provision"},
              flags={"workload_credentials_enabled": True, "wlc_aws_enabled": True})
    assert lease.purposes_for("aws") == (lease.PURPOSE_PROVISION,)


def test_both_purposes_once_a_readonly_secret_is_configured():
    _stub_cfg(values={"wlc_aws_secret_name": "dashboard-provision",
                      "wlc_aws_readonly_secret_name": "dashboard-readonly"},
              flags={"workload_credentials_enabled": True, "wlc_aws_enabled": True})
    assert lease.purposes_for("aws") == (lease.PURPOSE_PROVISION, lease.PURPOSE_READONLY)


def test_readonly_collapses_onto_provision_without_its_own_secret():
    # So both labels share ONE row rather than duplicating it.
    _stub_cfg(values={"wlc_aws_secret_name": "dashboard-provision"})
    assert lease._effective_purpose("aws", lease.PURPOSE_READONLY) == lease.PURPOSE_PROVISION


def test_readonly_stays_itself_once_it_has_its_own_secret():
    _stub_cfg(values={"wlc_aws_secret_name": "dashboard-provision",
                      "wlc_aws_readonly_secret_name": "dashboard-readonly"})
    assert lease._effective_purpose("aws", lease.PURPOSE_READONLY) == lease.PURPOSE_READONLY


def test_provision_never_collapses():
    _stub_cfg(values={"wlc_aws_secret_name": "dashboard-provision"})
    assert lease._effective_purpose("aws", lease.PURPOSE_PROVISION) == lease.PURPOSE_PROVISION


def test_the_refresh_loops_iterate_configured_purposes_not_the_constant():
    """The guard on the actual bug.

    Both the startup warmer and the worker pass looped over the PURPOSES constant, which
    is what produced the duplicate lease. They must ask which purposes are configured.
    """
    for parts in (("web_dashboard", "main.py"),
                  ("web_dashboard", "jobs_worker.py")):
        src = _read(*parts)
        assert "purposes_for(cloud)" in src, f"{parts[-1]} does not use purposes_for"
        assert "in leases.PURPOSES" not in src, (
            f"{parts[-1]} still iterates the PURPOSES constant, which mints a duplicate "
            f"lease when no distinct readonly secret is configured")


# ── The job boundary (Phase 2b) ──────────────────────────────────────────────
#
# `_aws_kwargs` receives only a region, so the purpose cannot be a parameter. A context
# variable entered at the job boundary is the seam; these tests pin the semantics that
# make it safe, above all that nothing changes until a second dynamic secret exists.

def test_without_a_second_secret_everything_resolves_to_provision():
    """The no-op guarantee.

    An operator opts into the split by creating a second dynamic secret, never by
    upgrading. Until then there is one lease and it serves everything.
    """
    _stub_cfg(values={"wlc_aws_secret_name": "dashboard-provision"},
              flags={"workload_credentials_enabled": True, "wlc_aws_enabled": True})
    assert lease.default_purpose("aws") == lease.PURPOSE_PROVISION
    with lease.provisioning():
        assert lease.default_purpose("aws") == lease.PURPOSE_PROVISION


def test_with_a_second_secret_the_request_path_gets_the_everyday_lease():
    _stub_cfg(values={"wlc_aws_secret_name": "dashboard-provision",
                      "wlc_aws_readonly_secret_name": "dashboard-everyday"},
              flags={"workload_credentials_enabled": True, "wlc_aws_enabled": True})
    assert lease.default_purpose("aws") == lease.PURPOSE_READONLY


def test_inside_a_job_the_purpose_is_provision():
    _stub_cfg(values={"wlc_aws_secret_name": "dashboard-provision",
                      "wlc_aws_readonly_secret_name": "dashboard-everyday"},
              flags={"workload_credentials_enabled": True, "wlc_aws_enabled": True})
    with lease.provisioning():
        assert lease.default_purpose("aws") == lease.PURPOSE_PROVISION


def test_the_provisioning_marker_is_unset_on_the_way_out():
    # A leaked token would give every later request write privilege — the exact opposite
    # of the feature.
    _stub_cfg(values={"wlc_aws_secret_name": "p", "wlc_aws_readonly_secret_name": "e"},
              flags={"workload_credentials_enabled": True, "wlc_aws_enabled": True})
    with lease.provisioning():
        pass
    assert lease.default_purpose("aws") == lease.PURPOSE_READONLY


def test_the_marker_is_unset_even_when_the_job_raises():
    _stub_cfg(values={"wlc_aws_secret_name": "p", "wlc_aws_readonly_secret_name": "e"},
              flags={"workload_credentials_enabled": True, "wlc_aws_enabled": True})
    try:
        with lease.provisioning():
            raise RuntimeError("job blew up")
    except RuntimeError:
        pass
    assert lease.default_purpose("aws") == lease.PURPOSE_READONLY


def test_nesting_restores_the_outer_value():
    _stub_cfg(values={"wlc_aws_secret_name": "p", "wlc_aws_readonly_secret_name": "e"},
              flags={"workload_credentials_enabled": True, "wlc_aws_enabled": True})
    with lease.provisioning():
        with lease.provisioning():
            assert lease.default_purpose("aws") == lease.PURPOSE_PROVISION
        assert lease.default_purpose("aws") == lease.PURPOSE_PROVISION
    assert lease.default_purpose("aws") == lease.PURPOSE_READONLY


# ── What the warmer may pre-mint ─────────────────────────────────────────────

def test_the_warmer_never_pre_mints_provision_once_the_split_is_on():
    """The whole security payoff.

    Pre-minting `provision` would leave a credential carrying iam:PassRole and
    iam:CreateRole sitting in the row at all times — defeating the split while looking
    like a harmless optimisation.
    """
    _stub_cfg(values={"wlc_aws_secret_name": "dashboard-provision",
                      "wlc_aws_readonly_secret_name": "dashboard-everyday"},
              flags={"workload_credentials_enabled": True, "wlc_aws_enabled": True})
    assert lease.warm_purposes_for("aws") == (lease.PURPOSE_READONLY,)


def test_the_warmer_still_warms_the_single_lease_when_there_is_no_split():
    _stub_cfg(values={"wlc_aws_secret_name": "dashboard-provision"},
              flags={"workload_credentials_enabled": True, "wlc_aws_enabled": True})
    assert lease.warm_purposes_for("aws") == (lease.PURPOSE_PROVISION,)


def test_nothing_is_warmed_for_a_cloud_not_on_the_dynamic_tier():
    _stub_cfg(values={"wlc_aws_secret_name": "dashboard-provision"})
    assert lease.warm_purposes_for("aws") == ()


def test_the_warmer_and_the_worker_use_warm_purposes_not_all_purposes():
    """The guard on the payoff.

    Swapping this back to purposes_for would silently restore a standing
    write-privileged credential, with no error and no visible symptom.
    """
    for parts in (("web_dashboard", "main.py"), ("web_dashboard", "jobs_worker.py")):
        src = _read(*parts)
        assert "warm_purposes_for(cloud)" in src, f"{parts[-1]} pre-mints too much"
        assert "leases.purposes_for(cloud)" not in src


def test_the_job_runner_enters_the_provisioning_scope():
    # And inside _run_job, not the supervisor: contextvars snapshot per Task, so entering
    # it in the loop body would tag whichever job happened to be starting.
    src = _read("web_dashboard", "jobs_worker.py")
    body = src.split("async def _run_job", 1)[1].split("\nasync def ", 1)[0]
    assert "with _wlc_provisioning():" in body
    # Scoped to the dispatch, not the whole task: the heartbeat is created before it and
    # must not inherit write privilege. Also keeps `with correlation(job_id):` a single
    # statement, which test_worker_tiers pins literally.
    assert "with correlation(job_id):" in body
    assert "with correlation(job_id), _wlc_provisioning():" not in body


# ── Azure (Phase 3) ──────────────────────────────────────────────────────────
#
# The asymmetry that makes Azure different from AWS: Workload Credentials mints a
# password onto an app registration and has no idea which SUBSCRIPTION the dashboard
# targets, so the lease is incomplete on its own. Miss that and you build a client that
# authenticates fine and then acts on nothing.

def _stub_azure_lease(values, subscription=""):
    """Point `credentials` at a fixed Azure lease and config at a subscription."""
    original = lease.credentials
    lease.credentials = lambda cloud, purpose="": values if cloud == "azure" else None
    lease._cfg = lambda key, default="": (subscription if key == "azure_subscription_id"
                                          else default)
    return original


AZURE_LEASE = {"client_id": "cid", "client_secret": "csec", "tenant_id": "tid",
               "key_id": "kid"}


def test_the_azure_quad_takes_its_subscription_from_config():
    original = _stub_azure_lease(AZURE_LEASE, subscription="sub-123")
    try:
        quad = lease.azure_credentials()
    finally:
        lease.credentials = original
    assert quad == {"client_id": "cid", "client_secret": "csec",
                    "tenant_id": "tid", "subscription_id": "sub-123"}


def test_a_missing_subscription_raises_rather_than_returning_three_quarters():
    """A credential with no subscription authenticates and then acts on nothing.

    Returning the triple and letting the SDK fail later would surface as a confusing
    permissions or not-found error a long way from the cause.
    """
    original = _stub_azure_lease(AZURE_LEASE, subscription="")
    try:
        lease.azure_credentials()
    except lease.LeaseUnavailable as exc:
        assert "azure_subscription_id" in str(exc)
    else:
        raise AssertionError("expected LeaseUnavailable")
    finally:
        lease.credentials = original


def test_the_azure_quad_is_none_when_azure_is_not_on_the_dynamic_tier():
    original = lease.credentials
    lease.credentials = lambda cloud, purpose="": None
    try:
        assert lease.azure_credentials() is None
        assert lease.azure_subprocess_env() is None
    finally:
        lease.credentials = original


def test_the_azure_subprocess_env_carries_all_four_arm_variables():
    # Three copies of this mapping existed before it was shared; each was a chance to
    # omit one, and an absent ARM_SUBSCRIPTION_ID fails inside terraform, not here.
    original = _stub_azure_lease(AZURE_LEASE, subscription="sub-123")
    try:
        env = lease.azure_subprocess_env()
    finally:
        lease.credentials = original
    assert env == {"ARM_CLIENT_ID": "cid", "ARM_CLIENT_SECRET": "csec",
                   "ARM_TENANT_ID": "tid", "ARM_SUBSCRIPTION_ID": "sub-123"}


# ── The Azure credential sites ───────────────────────────────────────────────

def test_all_four_azure_credential_sites_consult_the_lease():
    """The drift guard, matching the AWS one.

    `aks_get_token` is the one that bites: it deliberately bypasses `_ensure_creds`
    because the sync k8s-runner path cannot await, so wiring only `_ensure_creds` leaves
    the runner silently using a static credential the operator may have retired.
    """
    checks = [
        (("web_dashboard", "services", "azure_service.py"), "workload_credential_lease"),
        (("web_dashboard", "services", "terraform_provider_env.py"), "azure_subprocess_env"),
        (("web_dashboard", "services", "terraform.py"), "azure_subprocess_env"),
        (("web_dashboard", "services", "packer_build_service.py"), "azure_credentials"),
    ]
    for parts, needle in checks:
        assert needle in _read(*parts), f"{parts[-1]} does not consult the lease for Azure"


def test_aks_get_token_resolves_the_dynamic_tier_itself():
    src = _read("web_dashboard", "services", "azure_service.py")
    body = src.split("def aks_get_token", 1)[1].split("\ndef ", 1)[0]
    assert "_resolve_azure_credentials_sync()" in body


def test_the_azure_credential_cache_is_keyed_on_the_material():
    """It used to rebuild only on explicit invalidation, with no expiry at all.

    Survivable while every source was long-lived; wrong for a credential that expires in
    1-24 hours, since the process would pin a dead secret until restart — and
    invalidate_credentials is process-local, so a sibling worker would keep its own dead
    copy regardless.
    """
    src = _read("web_dashboard", "services", "azure_service.py")
    assert "_cred_key" in src
    body = src.split("async def _ensure_creds", 1)[1].split("\ndef ", 1)[0]
    assert "_cred_key != key" in body
    # And it must not go back to the populated-only check.
    assert "if _cred_cache is None:" not in body


def test_the_azure_resolver_runs_on_azures_own_pool():
    """Off the event loop, but NOT on the shared default executor.

    The dynamic rung is a memo hit in the common case and mints over HTTP on a cold
    start, so it cannot run inline. It also cannot run on `asyncio.to_thread`: that pool
    is eight slots for the whole application, and saturating it is the 30-minute outage
    the per-cloud pools were introduced to prevent. test_cloud_executor pins this too.
    """
    src = _read("web_dashboard", "services", "azure_service.py")
    body = src.split("async def _ensure_creds", 1)[1].split("\ndef ", 1)[0]
    assert "await _to_thread(" in body
    assert "asyncio.to_thread" not in body


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in fns:
        try:
            _reset()
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
