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
