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
    _stub_cfg(values={"wlc_subprocess_credential_process": "false"})
    original = lease.credentials
    lease.credentials = lambda cloud, purpose=None, **kw: {
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
    lease.credentials = lambda cloud, purpose=None, **kw: None
    try:
        assert lease.aws_subprocess_env() is None
    finally:
        lease.credentials = original



# ── Subprocess credentials need most of their life ahead of them ────────────
# Terraform and Packer read the credential once, as environment variables, so a lease
# served near its end dies mid-apply. This is what lets the provisioning lease be short.

def test_a_fresh_lease_is_fresh_enough():
    snap = _snap(issued_at=NOW - timedelta(minutes=5), expires_at=NOW + timedelta(minutes=25))
    assert lease._fresh_enough(snap, NOW, 80) is True


def test_a_lease_past_its_first_fifth_is_not_fresh_enough_at_80():
    snap = _snap(issued_at=NOW - timedelta(minutes=10), expires_at=NOW + timedelta(minutes=20))
    assert lease._fresh_enough(snap, NOW, 80) is False


def test_an_unusable_or_undated_lease_is_never_fresh_enough():
    assert lease._fresh_enough(None, NOW, 1) is False
    assert lease._fresh_enough(_snap(issued_at=None), NOW, 1) is False
    assert lease._fresh_enough(_snap(expires_at=NOW + timedelta(seconds=30)), NOW, 1) is False


def test_the_subprocess_floor_defaults_to_80_and_is_clamped():
    _stub_cfg()
    assert lease.subprocess_min_remaining_pct() == 80
    _stub_cfg(values={"wlc_subprocess_min_remaining_pct": "100"})
    assert lease.subprocess_min_remaining_pct() == 95
    _stub_cfg(values={"wlc_subprocess_min_remaining_pct": "0"})
    assert lease.subprocess_min_remaining_pct() == 1
    _stub_cfg(values={"wlc_subprocess_min_remaining_pct": "-5"})
    assert lease.subprocess_min_remaining_pct() == 1
    _stub_cfg(values={"wlc_subprocess_min_remaining_pct": "lots"})
    assert lease.subprocess_min_remaining_pct() == 80


def test_both_subprocess_helpers_ask_for_a_fresh_lease():
    _stub_cfg(values={"azure_subscription_id": "sub"})
    seen = []
    original = lease.credentials

    def fake(cloud, purpose="", **kw):
        seen.append((cloud, kw.get("min_remaining_pct")))
        return {"access_key_id": "A", "secret_access_key": "s", "session_token": "t",
                "client_id": "c", "client_secret": "x", "tenant_id": "t"}
    lease.credentials = fake
    try:
        lease.aws_subprocess_env()
        lease.azure_subprocess_env()
    finally:
        lease.credentials = original
    assert seen == [("aws", 80), ("azure", 80)], seen


def _serve_fresh_with(snap):
    """Run _serve_fresh against a stored snapshot; report whether it minted."""
    calls = []
    real = (lease._stored_snap, lease._serve_or_mint, lease._values_of, lease._utcnow)
    lease._stored_snap = lambda cloud, purpose: snap
    lease._values_of = lambda s: {"access_key_id": "stored"}
    lease._utcnow = lambda: NOW

    def mint(cloud, purpose, name, *, force):
        calls.append(force)
        return {"access_key_id": "minted"}
    lease._serve_or_mint = mint
    try:
        return lease._serve_fresh("aws", "provision", "dashboard-provision", 80), calls
    finally:
        lease._stored_snap, lease._serve_or_mint, lease._values_of, lease._utcnow = real


def test_a_fresh_stored_lease_is_served_without_a_mint():
    _reset()
    values, calls = _serve_fresh_with(_snap(issued_at=NOW - timedelta(minutes=2),
                                            expires_at=NOW + timedelta(minutes=28)))
    assert values == {"access_key_id": "stored"} and calls == []


def test_a_stale_stored_lease_is_replaced_by_a_forced_mint():
    _reset()
    values, calls = _serve_fresh_with(_snap(issued_at=NOW - timedelta(minutes=20),
                                            expires_at=NOW + timedelta(minutes=10)))
    assert values == {"access_key_id": "minted"} and calls == [True]



# ── credential_process: runs that renew their own credential ─────────────────
# Verified against a real terraform binary: the S3 backend reports
# credentials_source=ProcessProvider, re-runs the command as each Expiration nears, and
# an empty AWS_ACCESS_KEY_ID is what stops a static key in the container env winning.

def _creds(**kw):
    return {"access_key_id": "ASIA", "secret_access_key": "s", "session_token": "tok"}


def test_credential_process_is_on_unless_turned_off():
    _stub_cfg()
    assert lease.credential_process_enabled() is True
    for off in ("false", "0", "off", "no", "False"):
        _stub_cfg(values={"wlc_subprocess_credential_process": off})
        assert lease.credential_process_enabled() is False, off


def test_the_subprocess_gets_a_profile_not_keys():
    _stub_cfg(values={"wlc_aws_secret_name": "dashboard-provision"})
    original = lease.credentials
    lease.credentials = lambda cloud, purpose="", **kw: _creds()
    try:
        env = lease.aws_subprocess_env()
    finally:
        lease.credentials = original
    assert env["AWS_PROFILE"] == "vmdash-wlc-provision"
    assert env["AWS_SDK_LOAD_CONFIG"] == "1"
    # Blank, not absent: env credentials beat a profile in every AWS SDK.
    for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        assert env[key] == "", key
    text = _read_file(env["AWS_CONFIG_FILE"])
    assert text.startswith("[profile vmdash-wlc-provision]")
    assert "credential_process = " in text and "--purpose provision" in text
    assert "ASIA" not in text and "tok" not in text, "a credential was written to disk"


def _read_file(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def test_a_subprocess_still_fails_fast_when_no_lease_can_be_had():
    """The lease is fetched up front even in profile mode, so a broken configuration
    fails the job with the lease's own message, not as an opaque SDK error later."""
    _stub_cfg()
    original = lease.credentials

    def boom(cloud, purpose="", **kw):
        raise lease.LeaseUnavailable("no usable aws credential for provision")
    lease.credentials = boom
    try:
        try:
            lease.aws_subprocess_env()
            raise AssertionError("expected LeaseUnavailable")
        except lease.LeaseUnavailable:
            pass
    finally:
        lease.credentials = original


def test_the_purpose_is_resolved_in_the_job_and_written_into_the_command():
    # The callback is a fresh process and cannot see the provisioning marker.
    _stub_cfg(values={"wlc_aws_secret_name": "p", "wlc_aws_readonly_secret_name": "e"},
              flags={"workload_credentials_enabled": True, "wlc_aws_enabled": True})
    with lease.provisioning():
        inside = lease.aws_process_env()
    outside = lease.aws_process_env()
    assert inside["AWS_PROFILE"] == "vmdash-wlc-provision"
    assert outside["AWS_PROFILE"] == "vmdash-wlc-readonly"
    assert "--purpose provision" in _read_file(inside["AWS_CONFIG_FILE"])


def test_the_command_quotes_its_paths():
    cfg = lease._process_config("provision")
    line = [ln for ln in cfg.splitlines() if ln.startswith("credential_process")][0]
    import shlex
    argv = shlex.split(line.split("=", 1)[1])
    assert argv[0] == sys.executable and argv[1].endswith("wlc_credential_process.py")
    assert argv[2:] == ["--cloud", "aws", "--purpose", "provision"]
    assert os.path.isfile(argv[1])


def test_the_output_reports_an_early_expiration():
    _reset()
    _stub_cfg()
    real = (lease.credentials, lease._utcnow)
    expires = NOW + timedelta(minutes=30)

    def fake(cloud, purpose="", **kw):
        assert kw.get("min_remaining_pct") == lease.PROCESS_MIN_REMAINING_PCT
        lease._memo_put("aws", "provision", _creds(), expires)
        return _creds()
    lease.credentials, lease._utcnow = fake, (lambda: NOW)
    try:
        out = lease.credential_process_output("aws", "provision")
    finally:
        lease.credentials, lease._utcnow = real
    assert out["Version"] == 1 and out["AccessKeyId"] == "ASIA"
    assert out["SessionToken"] == "tok"
    assert out["Expiration"] == "2026-08-20T12:25:00Z", out   # five minutes early


def test_a_nearly_spent_lease_is_reported_proportionally_early():
    # Five minutes early on a lease with four left would be a time in the past, and the
    # SDK would call again in a loop.
    _reset()
    _stub_cfg()
    real = (lease.credentials, lease._utcnow)

    def fake(cloud, purpose="", **kw):
        lease._memo_put("aws", "provision", _creds(), NOW + timedelta(minutes=4))
        return _creds()
    lease.credentials, lease._utcnow = fake, (lambda: NOW)
    try:
        out = lease.credential_process_output("aws", "provision")
    finally:
        lease.credentials, lease._utcnow = real
    assert out["Expiration"] == "2026-08-20T12:03:12Z", out


def test_credential_process_is_aws_only():
    try:
        lease.credential_process_output("azure", "provision")
        raise AssertionError("expected LeaseUnavailable")
    except lease.LeaseUnavailable:
        pass


def test_the_script_keeps_stdout_for_the_credential():
    src = _read("web_dashboard", "wlc_credential_process.py")
    assert 'os.write(1, json.dumps(out).encode("utf-8"))' in src
    assert "sys.stdout" not in src.split("def main", 1)[1]
    # Every other print goes to stderr.
    for line in src.splitlines():
        if "print(" in line:
            assert "file=sys.stderr" in line or "file=sys.stderr" in src[src.index(line):][:300]


def test_the_packer_archive_carries_the_session_token():
    src = _read("web_dashboard", "services", "packer_service.py")
    assert 'aws_session_token=credentials.get("aws_session_token")' in src
    build = _read("web_dashboard", "services", "packer_build_service.py")
    assert 'env["AWS_ACCESS_KEY_ID"]' not in build.split("def _archive_credentials")[0].split(
        "archive_to_s3(")[-1], "the archive reads keys a profile-mode env does not carry"



# ── Refreshable credentials: in-process clients renew their own ──────────────

def _with_fresh(fn):
    """Run ``fn`` with _fresh_lease returning a fixed lease; return its result and calls."""
    calls = []
    real = lease._fresh_lease

    def fresh(cloud, purpose):
        calls.append((cloud, purpose))
        return _creds(), NOW + timedelta(minutes=30)
    lease._fresh_lease = fresh
    try:
        return fn(), calls
    finally:
        lease._fresh_lease = real


def test_refresh_metadata_carries_the_real_expiry():
    meta, _ = _with_fresh(lambda: lease._refresh_metadata("aws", "provision"))
    assert meta == {"access_key": "ASIA", "secret_key": "s", "token": "tok",
                    "expiry_time": "2026-08-20T12:30:00Z"}


def test_one_refreshable_object_per_purpose_bound_at_creation():
    _reset()
    _stub_cfg(values={"wlc_aws_secret_name": "p", "wlc_aws_readonly_secret_name": "e"},
              flags={"workload_credentials_enabled": True, "wlc_aws_enabled": True})

    def build():
        with lease.provisioning():
            job = lease.aws_refreshable_credentials()
        request = lease.aws_refreshable_credentials()
        return job, request, lease.aws_refreshable_credentials()
    (job, request, again), calls = _with_fresh(build)
    assert job is not request and request is again
    assert calls == [("aws", "provision"), ("aws", "readonly")], calls
    # The refresh callback keeps the purpose it was built with, whatever context it
    # later runs in: a deploy must not drift onto the everyday lease.
    _, later = _with_fresh(lambda: job._refresh_using())
    assert later == [("aws", "provision")], later
    _reset()


def test_the_refresh_windows_fit_a_short_lease():
    """botocore's 15/10-minute defaults would keep a 15-minute lease permanently inside
    the window and re-read it on every call. The attributes are botocore internals,
    pinned with boto3 in requirements.txt; an upgrade that renames them fails here."""
    _reset()
    _stub_cfg()
    creds, _ = _with_fresh(lambda: lease.aws_refreshable_credentials("provision"))
    assert creds._advisory_refresh_timeout == lease._ADVISORY_REFRESH_SECONDS == 300
    assert creds._mandatory_refresh_timeout == lease._MANDATORY_REFRESH_SECONDS == 120
    from botocore.credentials import RefreshableCredentials
    assert hasattr(RefreshableCredentials, "_advisory_refresh_timeout")
    assert hasattr(RefreshableCredentials, "_mandatory_refresh_timeout")
    _reset()


def test_invalidate_drops_the_refreshable_object():
    _reset()
    _stub_cfg()
    first, _ = _with_fresh(lambda: lease.aws_refreshable_credentials("provision"))
    lease.invalidate("aws")
    second, _ = _with_fresh(lambda: lease.aws_refreshable_credentials("provision"))
    assert first is not second
    _reset()


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


def test_the_pool_the_sdk_calls_run_on_carries_the_provisioning_marker():
    """The guard on the seam this whole section relies on.

    _aws_kwargs resolves the lease INSIDE a cloud_executor pool thread (every service's
    _to_thread routes there — test_cloud_executor pins that). A bare pool.submit starts
    the thread on a fresh context, so the provisioning() marker entered in _run_job
    never reaches the credential lookup and every job is silently served the everyday
    lease. Live failure 2026-08-25: ec2_deploy ran as bt-dashboard-everyday-…, and
    iam:PassRole was denied by that lease's session policy. copy_context in the
    executor is what closes it; test_cloud_executor proves the behaviour."""
    src = _read("web_dashboard", "services", "cloud_executor.py")
    assert "copy_context" in src, \
        "cloud_executor stopped copying the caller's context into its pool threads"


def test_the_passrole_hint_never_offers_iam_surgery_for_a_session_policy_deny():
    """An explicit deny in a session policy beats any identity policy, so the static-tier
    remedy ("attach iam:PassRole to the principal") cannot work on the dynamic tier and
    sends the operator to widen IAM for nothing. The launch hint must branch on it."""
    src = _read("web_dashboard", "services", "aws_service.py")
    assert '"sessionpolicy" in low' in src, \
        "a session-policy PassRole deny gets the static-key remedy, which cannot work"


# ── Azure (Phase 3) ──────────────────────────────────────────────────────────
#
# The asymmetry that makes Azure different from AWS: Workload Credentials mints a
# password onto an app registration and has no idea which SUBSCRIPTION the dashboard
# targets, so the lease is incomplete on its own. Miss that and you build a client that
# authenticates fine and then acts on nothing.

def _stub_azure_lease(values, subscription=""):
    """Point `credentials` at a fixed Azure lease and config at a subscription."""
    original = lease.credentials
    lease.credentials = lambda cloud, purpose="", **kw: values if cloud == "azure" else None
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
    lease.credentials = lambda cloud, purpose="", **kw: None
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


def test_the_azure_resolver_fails_closed_like_the_aws_one():
    """LeaseUnavailable must become an AzureError.

    Unwrapped it is not an AzureError, so it escapes every route's `except AzureError` as
    a bare 500 — which means no 503, and the Azure page raises its credential banner only
    on a 503. The one sentence explaining the failure ended up in the container log and
    nowhere else. aws_service has wrapped this since the dynamic tier landed.
    """
    src = _read("web_dashboard", "services", "azure_service.py")
    body = src.split("def _resolve_azure_credentials_sync", 1)[1].split("\ndef ", 1)[0]
    assert "LeaseUnavailable" in body
    assert "raise AzureError" in body


# ── Backoff is not overridable (the runaway-retry bug) ───────────────────────
#
# `force` meant "mint even though the lease still works". It also skipped the cooldown,
# and `refresh` had no cooldown check of its own, so a broken lease was re-attempted on
# every sweep forever. An auth failure is not billed, so the visible symptom was only log
# noise — but the failure this backoff was written for, a generate that succeeds at the
# provider and then cannot be parsed here, is charged for on every attempt.

def test_a_row_in_cooldown_is_in_backoff():
    assert lease._in_backoff(_snap(cooldown_until=NOW + timedelta(minutes=5)), NOW) is True


def test_a_row_whose_cooldown_has_passed_is_not_in_backoff():
    assert lease._in_backoff(_snap(cooldown_until=NOW - timedelta(seconds=1)), NOW) is False


def test_a_row_that_never_failed_is_not_in_backoff():
    assert lease._in_backoff(_snap(), NOW) is False


def test_a_missing_row_is_not_in_backoff():
    # No row means nothing has failed yet, so there is nothing to wait for.
    assert lease._in_backoff(None, NOW) is False


def test_force_does_not_override_the_backoff():
    """The claim path must consult the backoff unconditionally.

    `force` exists to get past the "still usable" short-circuit for a renewal. Letting it
    also mean "ignore the backoff" is what took the periodic refresher outside the
    backoff, since that is the one caller that passes it.
    """
    src = _read("web_dashboard", "services", "workload_credential_lease.py")
    body = src.split("def _claim(", 1)[1].split("\ndef ", 1)[0]
    assert "_in_backoff(snap, now)" in body
    assert "cooldown_until" not in body, (
        "_claim compares cooldown_until inline again — use _in_backoff, so the check "
        "cannot pick up a `not force` clause that refresh() does not have")


def test_the_refresher_checks_the_backoff_before_minting():
    # Reaching _claim would refuse the mint anyway, but only by raising LeaseUnavailable,
    # which the caller logs at WARNING — once per sweep, indefinitely.
    src = _read("web_dashboard", "services", "workload_credential_lease.py")
    body = src.split("def refresh(", 1)[1].split("\ndef ", 1)[0]
    assert "_in_backoff(snap, now)" in body


def test_a_config_change_clears_the_backoff():
    """Otherwise fixing the cause is invisible for up to _COOLDOWN_MAX_SECONDS.

    Nothing overrides the cooldown any more, so the panel save has to lift it explicitly —
    and the PAT saved on that panel is the single likeliest thing to have been failing.
    """
    assert hasattr(lease, "clear_backoff")
    src = _read("web_dashboard", "services", "workload_credential_lease.py")
    body = src.split("def clear_backoff(", 1)[1].split("\ndef ", 1)[0]
    assert "cooldown_until = None" in body
    # Same asymmetry as _record_failure, from the other side: clearing failure state is
    # not a reason to discard a credential that works.
    assert "row.payload" not in body
    setup = _read("web_dashboard", "api", "setup.py")
    assert "clear_backoff()" in setup, (
        "the workload_credentials panel save does not lift the retry backoff")


# ── The health surface reports every lease (the invisible-failure bug) ───────

def test_credential_sources_reports_every_configured_purpose():
    """It read only `provision`, which is not the lease serving traffic.

    Once a readonly secret exists, `provision` is minted per job and left to expire, so a
    missing one is healthy — and the everyday lease, the one failing every sweep with a
    401, was not reported at all. The panel that exists to answer "is this working?" was
    the last place able to answer it.
    """
    src = _read("web_dashboard", "api", "secrets.py")
    body = src.split("async def get_credential_sources", 1)[1].split("\n@router", 1)[0]
    assert "purposes_for(cloud)" in body
    assert "PURPOSE_PROVISION" not in body, (
        "credential-sources is back to reporting one hard-coded purpose")
    # A purpose nothing pre-mints has no lease between jobs; without this the second row
    # becomes a permanent false alarm, which is the same bug wearing the other hat.
    assert "warm_purposes_for(cloud)" in body


def test_the_credential_panel_renders_each_lease():
    src = _read("web_dashboard", "templates", "settings.html")
    assert "c.leases" in src, "the panel does not iterate the per-purpose collection"
    assert "c.lease.last_error" not in src, "the panel still reads the single-lease shape"


def test_the_cloud_pages_do_not_offer_a_static_remedy_on_the_dynamic_tier():
    """`aws configure` / a .env key cannot fix a cloud whose credentials are minted.

    Both banners gave exactly one remedy regardless of tier. The Azure one also dropped
    the server's message on the floor, so the reason was not shown at all.
    """
    for parts in (("web_dashboard", "templates", "aws", "index.html"),
                  ("web_dashboard", "templates", "azure", "index.html")):
        src = _read(*parts)
        assert "includes('Workload Credentials')" in src, (
            f"{parts[-2]}/{parts[-1]} still shows one remedy for every credential failure")
    azure = _read("web_dashboard", "templates", "azure", "index.html")
    assert 'x-text="credError"' in azure, "the Azure banner discards the server's reason"


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
