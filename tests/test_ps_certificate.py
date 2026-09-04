"""Tests for the ``certificate`` registration method in ps_resource_service — onboarding
an x.509 certificate identity as a Password Safe managed account on the "Certificate"
custom plugin.

Most of this file is the address grammar, for the same reason as its k8ssa sibling and
one more. The plugin ships as a checksum-sealed .psplugin whose packaged appsettings.json
cannot be edited after import, and on **Password Safe Cloud** cannot be reached at all —
so the managed system's Network Address is the ONLY configuration surface there is. Unlike
the k8s plugin, this one *warns* about an unrecognised option and carries on with a
default, which is worse: a mistyped ``lifetim=30d`` issues a real certificate against a
validity nobody chose, on a schedule, hours later. So the validator rejects what the plugin
would merely mention, and these tests are the oracle for that transcription.

The address is also genuinely tight: the ADCS profile printed in the plugin's own test-case
document is 269 characters against Password Safe's 255-character column, which is pinned
below so the budget cannot quietly regress.

Imports ps_resource_service with a stubbed web_dashboard.config (no app deps).
Runs under pytest or standalone:  python tests/test_ps_certificate.py
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

# A minimal valid profile per backend, with the store options every Cloud address needs.
_STORE = "biurl=https://acme-ps.ps.beyondtrustcloud.com&folder=PKI/Pipelines&owner=1"
_GCPCAS = f"gcpcas?project=bt-se-lab&location=us-central1&pool=demo-pool&{_STORE}"
_ADCS = rf"adcs?ca=DC01.corp.example.com\Corp Issuing CA&template=CorpPipelineClientAuth&{_STORE}"
_AWSPCA = (f"awspca?arn=arn:aws:acm-pca:us-east-1:111122223333:certificate-authority/1a2b3c"
           f"&{_STORE}")
_ENTRA = (f"selfsigned?publisher=entraapp&tenant=contoso.onmicrosoft.com"
          f"&appid=graph-reader:8f1c9d2e-1111&{_STORE}")

# The certificate shape: the profile in dns_name, a placeholder ip, port 0 (the platform
# does not use one), and the account password-managed — the "password" IS the PKCS#12
# passphrase, which is never seeded because the first Change Password generates it.
_CERT = dict(name="pki-pipelines", host_name="pki-pipelines", ip_address="127.0.0.1",
             port=0, functional_account_id=88, platform_id=2001, entity_type_id=1,
             workgroup_id="55", managed_account_name="svc-deploy-pipeline",
             ssh_key_enforcement_mode=2, method="certificate")


def _ok(addr):
    ps._validate_certificate_dns_name(addr)
    ps._check_address_length(addr, "certificate")


def _bad(addr, *expect):
    try:
        ps._validate_certificate_dns_name(addr)
        ps._check_address_length(addr, "certificate")
    except ps.PSResourceError as exc:
        msg = str(exc).lower()
        for token in expect:
            assert token.lower() in msg, f"{token!r} not named in: {exc}"
        return str(exc)
    raise AssertionError(f"expected {addr!r} to be refused")


# ── the method's place in the two method sets ──────────────────────────────────

def test_certificate_is_a_plugin_method_and_password_managed():
    # Plugin: the address rides dns_name and no SSH reachability is required.
    # Password-managed: the credential is the PKCS#12 passphrase, not an SSH key.
    assert "certificate" in ps._PLUGIN_METHODS
    assert "certificate" in ps._PASSWORD_MANAGED_METHODS


# ── the four topologies from the plugin's own documentation ────────────────────

def test_each_documented_backend_parses():
    for addr in (_GCPCAS, _ADCS, _AWSPCA, _ENTRA):
        _ok(addr)


def test_backend_names_are_normalised_for_case_and_punctuation():
    # The plugin normalises both, so "AWS-PCA" and "awspca" are one thing.
    for name in ("awspca", "AWSPCA", "AWS-PCA", "aws", "acm-pca", "aws_private_ca"):
        _ok(f"{name}?arn=arn:aws:acm-pca:us-east-1:1:certificate-authority/x&{_STORE}")
    for name in ("gcpcas", "GCP-CAS", "gcp", "cas", "google-cas"):
        _ok(f"{name}?project=p&location=l&pool=q&{_STORE}")


def test_semicolon_separates_options_for_consoles_that_dislike_ampersand():
    _ok("gcpcas?project=p;location=us-central1;pool=q;biurl=https://b;owner=1")


def test_a_bare_option_with_no_equals_reads_as_true():
    parsed = ps.parse_certificate_address("adcs?ca=X&template=T&impersonate")
    assert parsed["options"]["impersonate"] == "true"


def test_option_aliases_resolve_to_their_canonical_key():
    parsed = ps.parse_certificate_address(
        "gcpcas?project=p&location=l&pool=q&ttl=24h&keyalg=rsa2048&subjectdn=CN=x"
        "&ekus=ClientAuth&warnpercent=25&secretname=cert/x&ownergroup=7&tenantid=t")
    for canonical in ("lifetime", "key", "subject", "eku", "warn", "secret", "owner",
                      "tenant"):
        assert canonical in parsed["options"], canonical


def test_values_are_never_percent_decoded_so_a_ca_name_survives_verbatim():
    # An ADCS CA configuration string carries a backslash and spaces; an ARN carries
    # colons and slashes. Both are typed literally, which is the whole reason the plugin
    # does not decode.
    ca = r"DC01.corp.example.com\Corp Issuing CA"
    parsed = ps.parse_certificate_address(f"adcs?ca={ca}&template=T")
    assert parsed["options"]["ca"] == ca
    arn = "arn:aws:acm-pca:us-east-1:111122223333:certificate-authority/1a2b3c"
    assert ps.parse_certificate_address(f"awspca?arn={arn}")["options"]["arn"] == arn


# ── what must be refused ───────────────────────────────────────────────────────

def test_an_empty_address_names_the_shipped_no_default_behaviour():
    # The package deliberately ships Backend empty, because silently falling back to the
    # self-signed TEST CA in production would be worse than a clear error.
    _bad("", "no certificate authority backend is configured")


def test_an_unknown_backend_lists_the_valid_ones():
    _bad("mystery?x=1", "not recognised", "adcs", "awspca", "gcpcas", "selfsigned")


def test_the_selfsigned_test_ca_is_refused_outright():
    # It generates and persists its own CA private key UNENCRYPTED beside the plugin and
    # logs a warning on every use. "Never select this in production" is not advice a
    # registration path should leave to the operator.
    for name in ("test", "selfsignedtest"):
        _bad(name, "harness use only", "selfsigned")


def test_each_backends_required_options_are_named_when_missing():
    _bad(f"adcs?ca=X&{_STORE}", "template=")
    _bad(f"adcs?template=T&{_STORE}", "ca=")
    _bad(f"awspca?{_STORE}", "arn=")
    _bad(f"gcpcas?project=p&location=l&{_STORE}", "pool=")


def test_an_unrecognised_option_is_refused_rather_than_left_to_default():
    # The plugin only WARNS here, which is exactly why this must not.
    msg = _bad(f"adcs?ca=X&template=T&lifetim=30d&{_STORE}", "lifetim", "default")
    assert "alias" in msg.lower()


def test_a_backend_scoped_option_on_the_wrong_backend_is_refused():
    _bad(f"gcpcas?project=p&location=l&pool=q&region=us-east-1&{_STORE}",
         "region", "awspca", "ignore")
    _bad(f"awspca?arn=a:b&issuer=x&{_STORE}", "issuer", "gcpcas")
    _bad(f"gcpcas?project=p&location=l&pool=q&template=T&{_STORE}", "template", "adcs")


def test_profile_values_are_range_checked():
    base = f"gcpcas?project=p&location=l&pool=q&{_STORE}"
    _bad(f"{base}&lifetime=30x", "lifetime", "90m")
    _bad(f"{base}&key=rsa1024", "key", "ecdsa-p256")
    _bad(f"{base}&hash=md5", "hash", "sha256")
    _bad(f"{base}&pbe=rc4", "pbe", "aes256")
    _bad(f"{base}&bundle=jks", "bundle", "pkcs12")
    _bad(f"{base}&store=vault", "store", "secretssafe")
    _bad(f"{base}&warndays=soon", "warndays", "whole number")


def test_every_documented_lifetime_unit_is_accepted_and_a_bare_number_means_days():
    base = f"gcpcas?project=p&location=l&pool=q&{_STORE}"
    for value in ("90m", "12h", "30d", "2w", "1y", "7"):
        _ok(f"{base}&lifetime={value}")


def test_warn_is_a_percentage_capped_at_ninety():
    # A single absolute threshold cannot serve both lifetime regimes — 21 days would fire
    # permanently on a 24-hour certificate — so the primary threshold is a share of the
    # certificate's OWN lifetime, and the plugin caps it at 90.
    base = f"gcpcas?project=p&location=l&pool=q&{_STORE}"
    _ok(f"{base}&warn=90")
    msg = _bad(f"{base}&warn=91", "warn", "percentage")
    assert "warndays" in msg


# ── the Entra publisher's own preconditions ────────────────────────────────────

def test_a_publisher_needs_a_tenant_and_its_own_target_id():
    _bad(f"selfsigned?publisher=entraapp&appid=x&{_STORE}", "tenant=")
    _bad(f"selfsigned?publisher=entrasp&spid=x&{_STORE}", "tenant=")
    _bad(f"selfsigned?publisher=entraapp&tenant=t&{_STORE}", "appid=", "object id")
    _bad(f"selfsigned?publisher=entrasp&tenant=t&{_STORE}", "spid=")


def test_the_service_principal_target_names_the_confusion_it_is_easy_to_make():
    # A service principal's object id is NOT the object id of its associated application,
    # and the portal shows both.
    msg = _bad(f"selfsigned?publisher=entrasp&tenant=t&{_STORE}", "spid=")
    assert "not" in msg.lower() and "application" in msg.lower()


def test_publisher_options_without_a_publisher_are_refused():
    # Without publisher= these do nothing at all and the certificate is never registered
    # with the relying party — which for Entra means an application left holding a key the
    # directory does not recognise.
    _bad(f"selfsigned?tenant=contoso.onmicrosoft.com&{_STORE}", "publisher=")
    _bad(f"selfsigned?retain=2&{_STORE}", "publisher=")


def test_both_publisher_aliases_are_accepted():
    for alias, target in (("entraapp", "appid"), ("app", "appid"),
                          ("entrasp", "spid"), ("sp", "spid")):
        _ok(f"selfsigned?publisher={alias}&tenant=t&{target}=abc&{_STORE}")


# ── the address budget ─────────────────────────────────────────────────────────

def test_every_address_in_the_test_case_document_fits_and_parses():
    # Verbatim from Certificate-Test-Case.md §3.3, §4.2, §4.3 and §5.3. These are the
    # profiles an operator copies, so a grammar that rejects one of them is wrong about
    # the plugin rather than strict. Lengths are asserted too: the whole design is a
    # length budget, and a silent 20-character growth here is what eventually overruns it.
    documented = {
        231: (r"adcs?ca=DC01.corp.example.com\Corp Issuing CA"
              "&template=CorpPipelineClientAuth"
              "&subject=CN={AccountName},OU=Service Accounts,DC=corp,DC=example"
              "&eku=ClientAuth&warn=30&biurl=https://bi01.corp.example.com"
              "&folder=Certs/Pipelines&owner=1"),
        214: ("gcpcas?project=<project>&location=us-central1&pool=demo-pipeline-pool"
              "&lifetime=24h&key=ecdsa-p256&subject=CN={AccountName},O=Example"
              "&eku=ClientAuth&biurl=https://bi01.corp.example.com"
              "&folder=Certs/Pipelines&owner=1"),
        180: ("awspca?arn=arn:aws:acm-pca:us-east-1:<acct>:certificate-authority/<id>"
              "&lifetime=24h&key=ecdsa-p256&eku=ClientAuth"
              "&biurl=https://bi01.corp.example.com&folder=Certs/Pipelines&owner=1"),
        202: ("selfsigned?publisher=entraapp&tenant=contoso.onmicrosoft.com"
              "&appid=graph-reader:<target app registration OBJECT id>"
              "&key=rsa2048&warn=30&biurl=https://bi01.corp.example.com"
              "&folder=Certs/EntraApps&owner=1"),
    }
    for expected_len, addr in documented.items():
        assert len(addr) == expected_len, f"{addr[:20]}… is {len(addr)}, expected {expected_len}"
        _ok(addr)


def test_an_over_long_profile_is_refused_with_the_overage_and_what_to_drop():
    # This is §3.3's own earlier draft, which the plugin's documentation now cites as the
    # worked example of overrunning the column: a CA, a template, a subject DN, a
    # BeyondInsight URL and a Secrets Safe folder reach 269 characters without looking
    # excessive. It fits today only because `secret=` (the default title written out
    # longhand) was dropped and `folder=` shortened — together 38 characters.
    #
    # The failure it guards against is silent: an address trimmed to fit loses whatever
    # sat at its end, and a truncated `&owner=1` reads as an ABSENT owner rather than as
    # damage. So the message has to name the overage, not merely refuse.
    overlong = (
        r"adcs?ca=DC01.corp.example.com\Corp Issuing CA"
        "&template=CorpPipelineClientAuth"
        "&subject=CN={AccountName},OU=Service Accounts,DC=corp,DC=example"
        "&eku=ClientAuth&warn=30&biurl=https://bi01.corp.example.com"
        "&folder=Certificates/Pipelines&secret=cert/{system}/{account}&owner=1")
    assert len(overlong) == 269
    msg = _bad(overlong, "269", "14", "255")
    # And the advice has to be about a certificate profile, not about the DB plugins'
    # Resource Broker cert path.
    assert "default" in msg.lower() and "managed account name" in msg.lower()


def test_an_address_at_exactly_the_limit_is_accepted():
    pad = "x" * (255 - len(f"gcpcas?project=&location=l&pool=q&{_STORE}"))
    addr = f"gcpcas?project={pad}&location=l&pool=q&{_STORE}"
    assert len(addr) == 255
    _ok(addr)


# ── the HCL the provider actually applies ──────────────────────────────────────

def _hcl(**over):
    args = dict(_CERT, dns_name=_GCPCAS, emit_private_key=False,
                dss_auto_management=False, timeout_value=60)
    args.pop("method", None)
    args.update(over)
    return ps._generate_managed_system_hcl(method="certificate", **args)


def test_the_system_block_carries_the_profile_in_dns_name_with_a_placeholder_ip():
    hcl = _hcl()
    assert f'dns_name                 = "{_GCPCAS}"' in hcl
    assert 'ip_address               = "127.0.0.1"' in hcl


def test_the_port_is_zero_not_the_packagers_postgres_default():
    # The CLI packager defaults the platform port to 5432, inherited from the PostgreSQL
    # plugin it was written for. This platform does not use a port at all.
    assert "port                     = 0" in _hcl()
    assert "5432" not in _hcl()


def test_the_system_block_omits_the_ssh_only_fields():
    hcl = _hcl()
    assert "remote_client_type" not in hcl
    assert "ssh_key_enforcement_mode" not in hcl


def test_the_account_is_api_enabled_and_not_dss_managed():
    hcl = _hcl()
    # Non-human consumers matter here more than anywhere: GET /ManagedAccounts returns
    # only accounts with ApiEnabled, and a CI pipeline is the whole use case.
    assert "api_enabled              = true" in hcl
    assert "dss_auto_management_flag = false" in hcl
    assert "private_key" not in hcl


def test_the_timeout_rides_the_managed_system_because_the_plugin_reads_it():
    assert "timeout                  = 60" in _hcl()


def test_own_credentials_is_never_emitted():
    # "Change Password Using Own Credentials" makes Password Safe call the action the
    # plugin declares NotSupported: a certificate identity holds no CA credential and
    # cannot enroll for itself.
    assert "use_own_credentials" not in _hcl()


# ── ordering: nothing may reach Terraform before the address is checked ────────

def test_validation_happens_before_terraform_runs():
    import asyncio
    called = {"n": 0}

    def _boom(*a, **k):
        called["n"] += 1
        raise AssertionError("terraform must not run for an invalid address")

    original = ps._apply_hcl_sync
    ps._apply_hcl_sync = _boom
    try:
        for addr in ("", "mystery?x=1", f"adcs?ca=X&template=T&lifetim=1d&{_STORE}"):
            try:
                asyncio.run(ps.register_managed_system(
                    functional_account_id=1, platform_id=2, workgroup_id="3",
                    name="c", host_name="c", method="certificate", dns_name=addr))
            except ps.PSResourceError:
                pass
    finally:
        ps._apply_hcl_sync = original
    assert called["n"] == 0, "validation must happen before terraform runs"


def test_own_credentials_is_refused_at_the_registration_boundary():
    import asyncio
    try:
        asyncio.run(ps.register_managed_system(
            functional_account_id=1, platform_id=2, workgroup_id="3",
            name="c", host_name="c", method="certificate", dns_name=_GCPCAS,
            use_own_credentials=True))
    except ps.PSResourceError as exc:
        assert "notsupported" in str(exc).lower().replace(" ", "")
        return
    raise AssertionError("use_own_credentials must be refused for a certificate identity")


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
