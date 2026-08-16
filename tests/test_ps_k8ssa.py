"""Tests for the ``k8ssa`` registration method in ps_resource_service — onboarding a
Kubernetes ServiceAccount token as a Password Safe managed account on the "Kubernetes
Service Account Token" custom plugin.

Most of this file is the address grammar, and that is deliberate. The plugin ships as a
checksum-sealed .psplugin, so its packaged appsettings.json cannot be edited after
import: the managed system address is the ONLY per-cluster configuration surface, and
the plugin therefore rejects an unrecognised option rather than ignoring it (a silently
dropped option in a sealed package is neither diagnosable nor fixable). That makes a
malformed address a first-rotation failure at 3am rather than a registration error — so
_validate_k8ssa_dns_name is transcribed from the plugin's Factories/ParameterFactory.cs
and these tests are the oracle for that transcription.

Imports ps_resource_service with a stubbed web_dashboard.config (no app deps).
Runs under pytest or standalone:  python tests/test_ps_k8ssa.py
"""
import os
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_cfg_stub = types.ModuleType("web_dashboard.config")
_cfg_stub.settings = object()
sys.modules.setdefault("web_dashboard.config", _cfg_stub)

from web_dashboard.services import ps_resource_service as ps  # noqa: E402

_SUB = "11111111-2222-3333-4444-555555555555"

# The k8ssa shape: dns_name = the cluster address, placeholder ip, port 443 (unused —
# the API server port belongs to the endpoint URL), account name "<ns>/<sa>", and the
# account password-managed (the credential IS the bearer token — see the seeding tests
# for why that token cannot be supplied at create time).
_K8SSA = dict(name="k8s-gke", host_name="k8s-gke", ip_address="127.0.0.1", port=443,
              functional_account_id=88, platform_id=1008, entity_type_id=1,
              workgroup_id="55", managed_account_name="pra-access/pra-access",
              ssh_key_enforcement_mode=2, method="k8ssa",
              dns_name="gke;my-project-123456;us-central1;prod-cluster",
              emit_private_key=False, dss_auto_management=False)


def _ok(addr):
    ps._validate_k8ssa_dns_name(addr)


def _bad(addr, *expect):
    try:
        ps._validate_k8ssa_dns_name(addr)
    except ps.PSResourceError as exc:
        for token in expect:
            assert token in str(exc), f"{token!r} missing from: {exc}"
        return str(exc)
    raise AssertionError(f"{addr!r} should have been rejected")


# ── method registration ─────────────────────────────────────────────────────────

def test_k8ssa_is_a_plugin_method_and_password_managed():
    # Plugin method → the address goes in dns_name and the SSH-only fields are omitted.
    assert "k8ssa" in ps._PLUGIN_METHODS
    # Password-managed → dss_auto_management_flag = false, because the credential IS the
    # bearer token, not an SSH key Password Safe would manage separately.
    assert "k8ssa" in ps._PASSWORD_MANAGED_METHODS


# ── the four valid address shapes ───────────────────────────────────────────────

def test_the_four_prefixes_parse_at_their_minimum_field_count():
    _ok("eks;us-east-1;prod-cluster-01")
    _ok(f"aks;{_SUB};rg-aks-prod;aks-prod-01")
    _ok("gke;my-project-123456;us-central1;prod-cluster")
    _ok("k8s;https://api.k8s.internal:6443")


def test_the_prefix_is_case_insensitive():
    # ParseAddress lowercases fields[0] before the lookup.
    _ok("EKS;us-east-1;prod")
    _ok("Gke;proj;us-central1;c1")


def test_the_bare_mode_shorthands_and_options_are_accepted():
    _ok("gke;my-project;us-central1;autopilot-01;bound;ttl=43200")
    _ok("gke;my-project;us-central1;prod;dnsEndpoint=true")
    _ok("eks;us-east-1;prod;roleArn=arn:aws:iam::123456789012:role/PasswordSafeRotator")
    _ok("k8s;https://api.k8s.internal:6443;ns=beyondtrust;bound")
    _ok("aks;%s;rg;c1;aadAppId=6dae42f8-4368-4678-94ff-3960e28e3630" % _SUB)
    _ok("eks;us-east-1;prod;mode=longlived;serverName=api.example.com")
    _ok("eks;us-east-1;prod;allowHostnameMismatch=true")


def test_option_keys_are_case_insensitive():
    # ApplyOption switches on key.ToLowerInvariant().
    _ok("gke;p;us-central1;c;DNSENDPOINT=true")
    _ok("gke;p;us-central1;c;TTL=600")


def test_a_trailing_semicolon_is_tolerated():
    # ParseOptions skips whitespace-only fields, so an empty trailing field is fine —
    # worth pinning because the address is assembled by string concatenation.
    _ok("eks;us-east-1;prod;")
    _ok("eks;us-east-1;prod;;bound")


# ── the length cap ──────────────────────────────────────────────────────────────

def test_an_address_over_249_characters_is_refused_with_the_count():
    # Password Safe truncates the field at 255; the plugin refuses at 249 so a silently
    # truncated address never reaches the cluster lookup. The count is in the message
    # because "shorten it" without a number is not actionable.
    addr = "k8s;https://api.example.com:6443;serverName=" + ("a" * 250)
    msg = _bad(addr, "249", "character")
    assert str(len(addr)) in msg


def test_an_address_at_exactly_249_characters_is_accepted():
    pad = "a" * (249 - len("k8s;https://x:6443;serverName="))
    addr = "k8s;https://x:6443;serverName=" + pad
    assert len(addr) == 249
    _ok(addr)


# ── malformed addresses ─────────────────────────────────────────────────────────

def test_an_empty_address_names_all_four_forms():
    _bad("", "eks;")
    _bad("   ", "gke;")


def test_an_unknown_prefix_is_refused():
    _bad("oke;us-ashburn-1;prod", "not recognised")
    # OKE clusters go down the generic k8s; path — the plugin has no OCI provider.
    _ok("k8s;https://oke.example.com:6443")


def test_too_few_positional_fields_is_refused_per_prefix():
    _bad("eks;us-east-1", "at least 3")
    _bad(f"aks;{_SUB};rg", "at least 4")
    _bad("gke;my-project;us-central1", "at least 4")
    _bad("k8s", "at least 2")


def test_an_empty_positional_field_is_refused():
    # "gke;;us-central1;prod" has four fields and would pass a bare count check, then
    # fail inside the plugin against an empty project id.
    _bad("gke;;us-central1;prod", "empty positional")
    _bad("eks;;prod", "empty positional")


def test_an_unrecognised_option_is_refused_not_ignored():
    _bad("eks;us-east-1;prod;garbage", "not a recognised option")
    _bad("eks;us-east-1;prod;wibble=1", "not a recognised option key")


def test_a_provider_scoped_option_on_the_wrong_prefix_is_refused():
    _bad("gke;p;us-central1;c;roleArn=arn:aws:iam::1:role/R", "applies only to 'eks'")
    _bad("eks;us-east-1;prod;aadAppId=6dae42f8-4368-4678-94ff-3960e28e3630",
         "applies only to 'aks'")
    _bad("gke;p;us-central1;c;ca=/etc/ca.pem", "applies only to 'k8s'")


def test_a_bad_mode_or_ttl_or_namespace_is_refused():
    _bad("eks;us-east-1;prod;mode=forever", "token mode")
    _bad("eks;us-east-1;prod;ttl=0", "positive whole number")
    _bad("eks;us-east-1;prod;ttl=-5", "positive whole number")
    _bad("eks;us-east-1;prod;ttl=abc", "positive whole number")
    _bad("eks;us-east-1;prod;ns=Bad_Namespace", "not a valid Kubernetes name")
    _bad("eks;us-east-1;prod;ns=-leading", "not a valid Kubernetes name")


def test_a_low_ttl_is_accepted_here_because_the_plugin_only_requires_positive():
    # The API server's 600s floor is applied by whoever BUILDS the address, not by the
    # parser — matching ParameterFactory, which checks only ttl > 0. Validating 600 here
    # would reject an address the plugin itself accepts.
    _ok("eks;us-east-1;prod;ttl=1")


# ── the generated HCL ───────────────────────────────────────────────────────────

def test_the_system_block_carries_the_address_in_dns_name_with_a_placeholder_ip():
    hcl = ps._generate_managed_system_hcl(**_K8SSA)
    assert 'dns_name                 = "gke;my-project-123456;us-central1;prod-cluster"' in hcl
    assert 'ip_address               = "127.0.0.1"' in hcl
    assert "port                     = 443" in hcl


def test_the_system_block_omits_the_ssh_only_fields():
    hcl = ps._generate_managed_system_hcl(**_K8SSA)
    assert "remote_client_type" not in hcl
    assert "ssh_key_enforcement_mode" not in hcl


def test_the_account_is_password_managed_and_pushes_no_key():
    hcl = ps._generate_managed_system_hcl(**_K8SSA)
    assert "dss_auto_management_flag = false" in hcl
    assert "auto_management_flag     = true" in hcl
    assert "private_key" not in hcl, "no SSH key exists for a bearer-token account"
    # A declared-but-unset required variable fails apply under TF_INPUT=0, so the
    # private-key variable must be omitted entirely, not just left unused.
    assert "ps_account_private_key" not in hcl


def test_the_account_name_is_namespace_slash_serviceaccount():
    hcl = ps._generate_managed_system_hcl(**_K8SSA)
    assert 'account_name             = "pra-access/pra-access"' in hcl


def test_the_token_rides_a_tf_var_and_never_the_hcl():
    hcl = ps._generate_managed_system_hcl(**_K8SSA)
    assert "password                 = var.ps_account_password" in hcl
    assert 'variable "ps_account_password"' in hcl


# ── the seeded credential ───────────────────────────────────────────────────────
#
# A realistic ServiceAccount bearer token: a JWT of ~900 characters. Fixtures here used to
# be a 43-character stub, which is why nothing caught that the create API refuses anything
# over 128 — no real token is ever that short.
_LONG_TOKEN = ("eyJhbGciOiJSUzI1NiIsImtpZCI6InN2Yy1hY2N0LXNpZ25pbmcta2V5In0."
               + "eyJhdWQiOlsiaHR0cHM6Ly9rdWJlcm5ldGVzLmRlZmF1bHQuc3ZjIl0s" * 12
               + ".c2lnbmF0dXJlLWJ5dGVz" * 8)


def _capture_register(**kw):
    """Run register_managed_system against a faked apply; return (out, captured)."""
    import asyncio
    captured = {}

    def _fake_apply(hcl, tf_vars):
        captured["hcl"] = hcl
        captured["tf_vars"] = tf_vars
        return {"managed_system_id": "1", "managed_account_id": "2", "tf_state_json": "{}"}

    orig = ps._apply_hcl_sync
    ps._apply_hcl_sync = _fake_apply
    try:
        base = dict(name="k8s-gke", host_name="k8s-gke", functional_account_id=88,
                    platform_id=1008, workgroup_id="55",
                    managed_account_name="pra-access/pra-access", method="k8ssa",
                    dns_name="gke;p;us-central1;c1")
        base.update(kw)
        return asyncio.run(ps.register_managed_system(**base)), captured
    finally:
        ps._apply_hcl_sync = orig


def test_a_bearer_token_is_too_long_to_seed_and_is_dropped():
    """The whole reason registration cannot seed this credential. The public REST
    create-managed-account path caps Password at 128 characters and a bearer token is
    800-1,200, so passing it through does not truncate — it fails the apply with
    400 "Password cannot exceed 128 characters." and takes the managed system with it."""
    assert len(_LONG_TOKEN) > ps._MAX_SEED_PASSWORD_LEN
    out, captured = _capture_register(initial_password=_LONG_TOKEN)

    sent = captured["tf_vars"]["ps_account_password"]
    assert sent != _LONG_TOKEN, "an over-long seed must be dropped, not sent"
    assert len(sent) <= ps._MAX_SEED_PASSWORD_LEN
    assert len(sent) >= 16, "the replacement is still a strong placeholder"
    assert _LONG_TOKEN not in captured["hcl"]
    assert out["initial_password_seeded"] is False, (
        "the caller has to be told the vault holds a placeholder — a credential that is "
        "wrong rather than missing is the one this feature cannot detect")


def test_a_seedable_credential_still_rides_through():
    # The parameter is not dead: a short password-managed credential (a DB user, a PRA
    # Vault account) is still seeded, and must still travel by TF_VAR rather than in HCL.
    pw = "S3cret-placeholder-value"
    out, captured = _capture_register(initial_password=pw)
    assert captured["tf_vars"]["ps_account_password"] == pw
    assert pw not in captured["hcl"], "the credential must ride a TF_VAR, never the HCL"
    assert out["initial_password_seeded"] is True


def test_a_seed_of_exactly_the_limit_is_accepted():
    # Boundary: 128 is the documented maximum, not the first rejected length.
    pw = "x" * ps._MAX_SEED_PASSWORD_LEN
    out, captured = _capture_register(initial_password=pw)
    assert captured["tf_vars"]["ps_account_password"] == pw
    assert out["initial_password_seeded"] is True

    out, captured = _capture_register(initial_password="x" * (ps._MAX_SEED_PASSWORD_LEN + 1))
    assert out["initial_password_seeded"] is False


def test_without_initial_password_a_throwaway_placeholder_is_used():
    out, captured = _capture_register()
    assert len(captured["tf_vars"]["ps_account_password"]) >= 16
    assert out["initial_password_seeded"] is False


def test_register_refuses_a_malformed_address_before_touching_terraform():
    import asyncio
    called = {"n": 0}

    def _fake_apply(hcl, tf_vars):
        called["n"] += 1
        return {}

    orig = ps._apply_hcl_sync
    ps._apply_hcl_sync = _fake_apply
    try:
        try:
            asyncio.run(ps.register_managed_system(
                name="k8s-gke", host_name="k8s-gke", functional_account_id=88,
                platform_id=1008, workgroup_id="55",
                managed_account_name="pra-access/pra-access", method="k8ssa",
                dns_name="nope;whatever"))
        except ps.PSResourceError as exc:
            assert "not recognised" in str(exc)
        else:
            raise AssertionError("a bad address must be refused")
    finally:
        ps._apply_hcl_sync = orig
    assert called["n"] == 0, "validation must happen before terraform runs"


# ── the credential must not survive into stashed state ──────────────────────────

def test_scrub_state_redacts_the_account_password():
    # The stashed state drives deregister, so it outlives the apply. Whatever ends up in
    # the password field has to be scrubbed — including the create-time placeholder, which
    # IS the account's live credential in Password Safe until the first rotation.
    secret = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJzYSJ9.c2ln"
    state = ('{"resources":[{"instances":[{"attributes":'
             '{"account_name":"pra-access/pra-access","password":"%s"}}]}]}' % secret)
    out = ps._scrub_state(state)
    assert secret not in out
    assert ps._REDACTED in out


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
