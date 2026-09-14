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
import logging
import os
import re
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


def _ok(addr, package=ps.CERT_PACKAGE_LEAF):
    ps._validate_certificate_dns_name(addr, package)
    ps._check_address_length(addr, "certificate")


def _bad(addr, *expect, package=ps.CERT_PACKAGE_LEAF):
    try:
        ps._validate_certificate_dns_name(addr, package)
        ps._check_address_length(addr, "certificate")
    except ps.PSResourceError as exc:
        msg = str(exc).lower()
        for token in expect:
            assert token.lower() in msg, f"{token!r} not named in: {exc}"
        return str(exc)
    raise AssertionError(f"expected {addr!r} to be refused on {package}")


def _sub_ok(addr):
    return _ok(addr, ps.CERT_PACKAGE_SUBCA)


def _sub_bad(addr, *expect):
    return _bad(addr, *expect, package=ps.CERT_PACKAGE_SUBCA)


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


# ── the two packages ───────────────────────────────────────────────────────────
# The plugin ships as TWO .psplugin packages over a shared core, appearing in
# BeyondInsight as separate PLATFORMS with separate access control: "Certificate" issues
# an end-entity certificate, "Subordinate CA" issues an issuing authority. The package is
# therefore not a flag on a profile — it decides which backends are legal and what isca=
# and bundle= default to, neither of which the address says. See
# docs/design/pra-session-ca.md.


def test_the_package_is_normalised_and_falls_through_to_the_leaf_one():
    # A typo must never silently switch an address onto the platform that issues
    # certificate AUTHORITIES, so the safe value is the one that falls through.
    for name in ("subca", "subordinateca", "Subordinate CA", "SUBORDINATE", "subordinate-ca"):
        assert ps.cert_normalise_package(name) == ps.CERT_PACKAGE_SUBCA, name
    for name in ("certificate", "", None, "leaf", "sub", "subordinatte"):
        assert ps.cert_normalise_package(name) == ps.CERT_PACKAGE_LEAF, name


def test_the_subordinate_package_offers_only_the_four_that_can_sign_one():
    # Not a subset chosen here: it mirrors the plugin's own CaCapabilities flag. Most
    # certificate authorities cannot sign a subordinate at all, so offering all nine and
    # failing later at the CA is the thing this replaces.
    assert set(ps._cert_backends_for(ps.CERT_PACKAGE_SUBCA)) == {
        "awspca", "gcpcas", "ejbca", "vaultpki"}
    leaves = set(ps._cert_backends_for(ps.CERT_PACKAGE_LEAF))
    assert leaves == {"adcs", "est", "ejbca", "vaultpki", "stepca", "digicert",
                      "sectigo", "awspca", "gcpcas", "selfsigned"}
    # The harness CA is in neither list: it writes its own key unencrypted to disk.
    assert "selfsignedtest" not in leaves


def test_the_capability_gate_fails_closed_and_names_the_backend():
    # Fails CLOSED on purpose. An earlier version of the plugin switched on the backend
    # name and allowed anything it did not recognise, so every backend added afterwards
    # was permitted to attempt CA issuance until somebody wrote a refusal for it.
    for backend in sorted(set(ps._CERT_BACKENDS.values())):
        if backend in ps._CERT_SUBCA_CAPABLE or backend == "selfsignedtest":
            continue
        assert backend in ps._CERT_SUBCA_REFUSED, (
            f"{backend!r} cannot sign a subordinate but carries no reason — the gate "
            f"refuses it, and the operator is told nothing about why")
    msg = _sub_bad(f"est?url=https://ca.example.com&{_STORE}", "est", "does not support")
    assert "rfc 7030" in msg.lower()
    msg = _sub_bad(rf"adcs?ca=X\Y&template=T&{_STORE}", "adcs", "does not support")
    assert "approval" in msg.lower() and "cr_disp_under_submission" in msg.lower()
    msg = _sub_bad(f"selfsigned?{_STORE}", "selfsigned", "does not support")
    assert "trust root" in msg.lower()
    for backend in ("stepca?url=https://ca.example.com&fingerprint=ab",
                    "digicert?url=https://one.digicert.com&profile=p",
                    "sectigo?url=https://cm.sectigo.com&profile=p"):
        _sub_bad(f"{backend}&{_STORE}", "does not support")


def test_a_subordinate_profile_on_a_capable_backend_is_steered_to_the_other_package():
    # The old model made this a flag on one platform. It is now a different PACKAGE, and
    # a subordinate managed system on the leaf platform would put an issuer under the
    # access control meant for a leaf — so on a backend that CAN sign one, this steers.
    base = f"gcpcas?project=p&location=l&pool=q&{_STORE}"
    for opt in ("isca=true", "permitdns=db.example.com", "pathlen=0",
                "permitip=10.0.0.0/8", "permitemail=corp.example.com",
                "excludedns=lab.example.com"):
        msg = _bad(f"{base}&{opt}", opt.split("=")[0], "subordinate ca")
        assert "separate" in msg.lower() and "platform" in msg.lower()
    # pathlen=0 is a MEANINGFUL value, so presence is the test and not truthiness.
    _bad(f"{base}&pathlen=0", "pathlen", "subordinate ca")


def test_the_two_refusals_are_different_and_the_policy_one_lives_on_the_leaf_package():
    """Which refusal you get depends on which platform you are standing on, and that is
    the point.

    The **Subordinate CA** package declines a backend by NAME, before any policy is
    consulted, and lists the four it does offer — it names what to use instead.

    The **policy reasoning** lives on the Certificate package, because that is the only
    platform those backends are offered on at all: "why not ADCS?" is answered there or
    nowhere. A blanket "use the other package" here would lose the answer a customer
    asking that question actually wants.
    """
    adcs = rf"adcs?ca=DC01\Corp Issuing CA&template=T&{_STORE}"
    est = f"est?url=https://ca.corp.example.com&{_STORE}"
    selfsigned = f"selfsigned?{_STORE}"

    # Leaf package: the backend's own reason, named.
    msg = _bad(f"{adcs}&isca=true", "isca", "adcs")
    assert "approval" in msg.lower() and "cr_disp_under_submission" in msg.lower()
    assert "subordinate ca' platform" not in msg.lower(), \
        "the steer would replace the approval-flag answer a customer asked for"
    msg = _bad(f"{selfsigned}&isca=true", "isca", "selfsigned")
    assert "trust root" in msg.lower()
    msg = _bad(f"{est}&isca=true", "isca", "est")
    assert "rfc 7030" in msg.lower()
    # A constraint option alone is enough to trip it — isca= need not be present.
    msg = _bad(f"{adcs}&permitdns=x.example.com", "permitdns", "adcs")
    assert "approval" in msg.lower()

    # Subordinate package: declined by name, with the four that are available.
    for addr in (adcs, est, selfsigned):
        msg = _sub_bad(addr, "does not support")
        for offered in ("awspca", "ejbca", "gcpcas", "vaultpki"):
            assert offered in msg, f"{offered} not listed as available: {msg}"
        assert "certificate platform" in msg.lower(), \
            "declining without naming where the backend IS offered sends nobody anywhere"


# ── subordinate-CA issuance, on the package that does it ──────────────────────
# The managed credential here is the ISSUER: some other system holds it and mints its own
# certificates beneath it. That inverts what a mistake costs, so this block is the
# strictest in the file.
#
# Note the absent isca=. The package defaults it to true, so a real subordinate address
# says nothing at all here — which is also 10 characters of the 255-character budget back,
# on the one topology where the budget and the safety control pull against each other.
_SUBCA = f"gcpcas?project=p&location=l&pool=q&{_STORE}"


def test_a_silent_address_on_the_subordinate_package_still_issues_an_authority():
    # The whole reason `package` is threaded through the validator rather than inferred
    # from the address: nothing in this string says "certificate authority".
    _sub_ok(_SUBCA)
    _sub_ok(f"{_SUBCA}&pathlen=0&permitdns=db.corp.example.com")
    _sub_ok(f"{_SUBCA}&permitemail=corp.example.com&excludedns=lab.example.com")
    # And the checks that only apply to an issuer fire on that silent address — proving
    # the default reached them rather than merely being accepted.
    _sub_bad(f"{_SUBCA}&eku=ClientAuth", "eku=", "subordinate")
    _sub_bad(f"{_SUBCA}&publisher=entraapp&tenant=t&appid=a", "publisher=", "thumbprint")


def test_the_subordinate_options_are_accepted_on_all_four_capable_backends():
    # The reason these cannot live in _CERT_BACKEND_KEYS: that table maps a key to
    # exactly ONE owning backend, and these are legitimate on four.
    for base in (f"gcpcas?project=p&location=l&pool=q&{_STORE}",
                 f"awspca?arn=arn:aws:acm-pca:us-east-1:1:certificate-authority/x&{_STORE}",
                 f"ejbca?url=https://ca.example.com&ca=RootCA&subcaprofile=SUBCA&{_STORE}",
                 f"vault?url=https://vault.example.com&mount=pki&{_STORE}"):
        _sub_ok(base)
        _sub_ok(f"{base}&pathlen=0&permitdns=db.corp.example.com")


def test_isca_false_on_the_subordinate_package_is_legal_and_said_out_loud():
    # Legal — it makes that platform behave as the Certificate platform does — but it
    # points at using the other package, so it is not accepted in silence.
    msg = _warnings_from(f"{_SUBCA}&isca=false", package=ps.CERT_PACKAGE_SUBCA)
    assert "isca=false" in msg and "certificate platform" in msg
    # With isca=false the subordinate-only options become silent no-ops again.
    _sub_bad(f"{_SUBCA}&isca=false&permitip=10.0.0.0/8", "permitip=", "isca=false")


def test_a_bare_isca_reads_as_true_like_every_other_flag():
    _sub_ok(f"gcpcas?project=p&location=l&pool=q&isca&permitdns=db.example.com&{_STORE}")


def test_isca_must_be_a_boolean_the_plugin_can_actually_read():
    # The sharpest edge in the grammar: a value that does not parse as a boolean issues
    # an END-ENTITY certificate where an authority was asked for, and that surfaces at
    # the relying party as an untrusted issuer rather than anywhere near the cause.
    msg = _sub_bad(f"gcpcas?project=p&location=l&pool=q&isca=yes&{_STORE}",
                   "isca", "true")
    assert "end-entity" in msg.lower()


def test_the_verbatim_subordinate_test_case_address_fits_and_parses():
    # Verbatim from the Subordinate CA test case §5 — the profile an operator copies for
    # the PRA session-CA topology. Three options an earlier draft carried are GONE
    # because the package already defaults them: isca=true, bundle=PemBundle and
    # pathlen=0. That is 37 characters of budget back with their separators, on the one
    # topology where the budget and the security control compete.
    addr = ("gcpcas?project=<project>&location=us-central1&pool=demo-subca-pool"
            "&lifetime=8d&permitdns=db.corp.example.com"
            "&biurl=https://bi01.corp.example.com&folder=Certs/SubCA&owner=1")
    assert len(addr) == 171, len(addr)
    _sub_ok(addr)
    # Spelling the three defaults out is harmless and reads as documentation — it just
    # costs the budget. Pinned so the saving cannot quietly stop being real.
    spelled = addr.replace("&lifetime=8d",
                           "&isca=true&pathlen=0&bundle=PemBundle&lifetime=8d")
    assert len(spelled) - len(addr) == 37, len(spelled) - len(addr)
    _sub_ok(spelled)


def test_pathlen_is_a_number_and_aws_stops_at_its_last_template():
    _sub_bad(f"{_SUBCA}&pathlen=deep", "pathlen", "whole number")
    _sub_ok(f"{_SUBCA}&pathlen=9")  # gcpcas honours the CSR, subject to the pool's policy
    aws = f"awspca?arn=arn:aws:acm-pca:us-east-1:1:certificate-authority/x&{_STORE}"
    _sub_ok(f"{aws}&pathlen=3")
    msg = _sub_bad(f"{aws}&pathlen=4", "pathlen", "template")
    assert "subordinatecacertificate_pathlen3" in msg.lower().replace(" ", "")


def test_a_permitted_ip_subtree_must_be_a_network_not_an_address_inside_one():
    # The plugin says so explicitly: 10.0.0.0/8, not 10.1.2.3/8. Getting it wrong
    # constrains the subordinate to something other than what was meant, and nothing
    # downstream can detect that.
    _sub_ok(f"{_SUBCA}&permitip=10.0.0.0/8,192.168.0.0/16")
    _sub_ok(f"{_SUBCA}&permitip=2001:db8::/32")
    msg = _sub_bad(f"{_SUBCA}&permitip=10.1.2.3/8", "permitip", "host bits")
    assert "10.0.0.0/8" in msg          # names the network they probably meant
    _sub_bad(f"{_SUBCA}&permitip=not-a-cidr", "permitip", "cidr")
    _sub_bad(f"{_SUBCA}&permitip=10.0.0.0/33", "permitip", "cidr")


def test_an_eku_on_a_subordinate_ca_is_refused_because_the_plugin_drops_it():
    # The plugin asserts NO extended key usage on a CA, deliberately — an EKU there
    # constrains the whole subtree beneath it and implementations disagree on how. So
    # eku= would be silently dropped, which is precisely what this validator exists for.
    msg = _sub_bad(f"{_SUBCA}&eku=ClientAuth", "eku=", "subordinate")
    assert "beneath" in msg.lower()
    # ...and it stays valid on a leaf, which is the common case.
    _ok(f"gcpcas?project=p&location=l&pool=q&eku=ClientAuth&{_STORE}")


def test_the_leaf_only_options_are_refused_on_an_issuer():
    # retain=, publisher= and the SAN options are in the SHARED grammar because the core
    # is shared, and they do nothing on an issuer: a publisher exists for a relying party
    # that pins a certificate by thumbprint, and an issuer has none.
    msg = _sub_bad(f"{_SUBCA}&publisher=entraapp&tenant=t&appid=8f1c",
                   "publisher=", "tenant=", "appid=")
    assert "thumbprint" in msg.lower()
    _sub_bad(f"{_SUBCA}&retain=2", "retain=", "subordinate ca")


# The origin a `url=` refusal tells the operator to retype, which both messages carry in
# repr quotes. Pulled out and compared by EQUALITY, deliberately: a substring containment
# check against a URL literal is the shape CodeQL flags as incomplete URL sanitization,
# and it is right to — `"https://host" in value` is a classic auth bypass in production
# code, so a test written that way teaches the wrong pattern. Equality is also the
# stronger assertion here, because it proves the remedy is EXACTLY the origin and carries
# no path rather than merely containing one somewhere.
_SUGGESTED_ORIGIN = re.compile(r"'(https?://[^']+)'")


def _suggested_origin(msg):
    """The last quoted origin in a refusal — the remedy, not the offending value.

    Both messages quote the offending url= first, so the LAST match is the suggestion.
    On the no-scheme message the offending value has no scheme at all and cannot match,
    which is why taking the last works for both rather than needing two readers."""
    found = _SUGGESTED_ORIGIN.findall(msg)
    assert found, f"no origin suggested in: {msg}"
    return found[-1]


def test_the_service_url_is_an_origin_over_https():
    # Three separate mistakes, each with its own message, because they are made for
    # different reasons and the remedies differ.
    host = "ca.corp.example.com"
    _ok(f"est?url=https://{host}&{_STORE}")
    _ok(f"est?url=https://{host}:8443&{_STORE}")
    # A path: the plugin appends its OWN API path, so this yields a doubled one and a 404
    # at the first credential change rather than at registration. Same trap as biurl=.
    msg = _bad(f"est?url=https://{host}/.well-known/est&{_STORE}",
               "url", "path", "origin")
    assert _suggested_origin(msg) == f"https://{host}", \
        "the remedy must be the origin with the path DROPPED, not merely contain it"
    # The port is part of the origin and has to survive into the suggestion.
    msg = _bad(f"est?url=https://{host}:8443/est&{_STORE}", "url", "path")
    assert _suggested_origin(msg) == f"https://{host}:8443"
    # Plain http: the request carries the enrollment credential in a header.
    _bad(f"est?url=http://{host}&{_STORE}", "url", "not https")
    # ...except on Vault, where a development server may legitimately be plain.
    _ok(f"vault?url=http://127.0.0.1:8200&role=pipelines&{_STORE}")
    # A bare host is the commonest of the three, and it leaves the scheme EMPTY — so a
    # check that only compared against "https" would pass it.
    msg = _bad(f"est?url={host}&{_STORE}", "url", "no scheme")
    assert _suggested_origin(msg) == f"https://{host}", \
        "the remedy must add the scheme to the host they typed"
    _bad(f"est?url=https://&{_STORE}", "url", "no host")
    # And url= is required on est at all — the label alone is not a profile.
    _bad(f"est?label=pipelines&{_STORE}", "url=")


def test_profile_and_template_are_one_option_and_may_not_disagree():
    # The plugin aliases template= to profile= on the non-ADCS family, so setting both to
    # different values is not a preference to resolve — one of the two is silently
    # ignored, and which one is an implementation detail nobody should have to know.
    ejbca = f"ejbca?url=https://ca.example.com&ca=RootCA&{_STORE}"
    _ok(f"{ejbca}&profile=ENDUSER")
    _ok(f"{ejbca}&template=ENDUSER")              # the alias spelling, same meaning
    _ok(f"{ejbca}&profile=ENDUSER&template=ENDUSER")   # agreeing is harmless
    _bad(f"{ejbca}&profile=ENDUSER&template=SERVER", "profile=", "template=", "disagree")
    # On ADCS template= is its own thing and profile= is not an option at all.
    _bad(rf"adcs?ca=DC01\CA&template=T&profile=X&{_STORE}", "profile", "adcs")


def test_vaults_auth_method_is_one_of_its_two():
    vault = f"vault?url=https://vault.example.com&role=pipelines&{_STORE}"
    _ok(f"{vault}&auth=token")
    # AppRole is the cleanest credential fit in the family: role_id as the account name
    # and secret_id as the password, needing no encoding.
    _ok(f"{vault}&auth=approle&approlemount=approle")
    _bad(f"{vault}&auth=userpass", "auth", "token", "approle")


def test_every_boolean_option_is_refused_unless_it_reads_as_one():
    # The plugin falls back to its DEFAULT on a value it cannot read as a boolean, which
    # is a silent change rather than an error — and for revokeondisable that silently
    # decides whether a disabled identity's certificate is withdrawn.
    base = f"gcpcas?project=p&location=l&pool=q&{_STORE}"
    for name in ("revokeondisable", "revokeonrenewal"):
        _ok(f"{base}&{name}=true")
        _ok(f"{base}&{name}=false")
        _ok(f"{base}&{name}")                     # a bare flag reads as true
        _bad(f"{base}&{name}=yes", name, "true or false")
    est = f"est?url=https://ca.example.com&{_STORE}"
    _bad(f"{est}&insecure=1", "insecure", "true or false")
    _bad(rf"adcs?ca=DC01\CA&template=T&impersonate=no&{_STORE}",
         "impersonate", "true or false")


def test_insecure_is_allowed_but_called_what_it_is():
    # It exists for one real moment — a private CA's own endpoint presented on a
    # certificate chaining to that very CA, which the broker does not trust until the
    # chain is installed. Installing the chain is the actual fix.
    msg = _warnings_from(f"est?url=https://ca.example.com&insecure=true&{_STORE}")
    assert "not validated" in msg and "trust store" in msg
    assert "not validated" not in _warnings_from(
        f"est?url=https://ca.example.com&insecure=false&{_STORE}")


def test_the_two_backend_specific_subordinate_prerequisites_are_named():
    # Each is something the CA would otherwise refuse further along, with an error that
    # names a policy rather than the missing option.
    ejbca = f"ejbca?url=https://ca.example.com&ca=RootCA&{_STORE}"
    # An END-ENTITY profile cannot produce basicConstraints CA:TRUE, which is why
    # subcaprofile= is its own option rather than profile= wearing a second hat.
    msg = _sub_bad(f"{ejbca}&profile=ENDUSER", "subcaprofile=", "ca:true")
    assert "end-entity profile" in msg.lower()
    _sub_ok(f"{ejbca}&subcaprofile=SUBCA")
    # Vault signs a subordinate through pki/root/sign-intermediate, which is not
    # role-scoped at all — so role= here is read by nothing.
    vault = f"vault?url=https://vault.example.com&{_STORE}"
    msg = _sub_bad(f"{vault}&role=pipelines", "role=", "sign-intermediate")
    assert "not" in msg.lower() and "role-scoped" in msg.lower()
    _sub_ok(vault)
    # And the other direction: a LEAF from Vault has no issuance policy without one.
    msg = _bad(vault, "role=", "issuance policy")
    _ok(f"{vault}&role=pipelines")


class _LogCapture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


def _warnings_from(addr, package=ps.CERT_PACKAGE_LEAF):
    """Validate ``addr`` while capturing ps_resource_service's warnings+."""
    handler = _LogCapture()
    level = ps.logger.level
    ps.logger.addHandler(handler)
    ps.logger.setLevel(logging.DEBUG)
    try:
        _ok(addr, package)
    finally:
        ps.logger.setLevel(level)
        ps.logger.removeHandler(handler)
    return " ".join(r.getMessage() for r in handler.records
                    if r.levelno >= logging.WARNING).lower()


def _sub_warnings_from(addr):
    return _warnings_from(addr, ps.CERT_PACKAGE_SUBCA)


def test_an_unconstrained_subordinate_is_allowed_but_cautioned():
    # Allowed because the constraints belong on the PARENT pool's issuance policy where
    # they are inherited and cost nothing against the address — so their absence here is
    # not necessarily a mistake. Cautioned because it might be.
    msg = _sub_warnings_from(_SUBCA)
    assert "name constraints" in msg and "issuance policy" in msg
    # And silence once any one of them is set.
    msg = _sub_warnings_from(f"{_SUBCA}&permitdns=db.example.com")
    assert "name constraints" not in msg


def test_a_long_lived_subordinate_is_cautioned_because_rotation_does_not_revoke():
    # The bound on a leaked signing key is the subordinate's own validity, NOT the
    # rotation interval — a year-long subordinate rotated weekly just accumulates ~52
    # concurrently valid authorities. Warned rather than refused for the plugin's own
    # reason: the rotation interval lives in an account policy this cannot see.
    msg = _sub_warnings_from(f"{_SUBCA}&permitdns=db.example.com&lifetime=1y")
    assert "rotation does not revoke" in msg and "365" in msg
    # 8d against a 7-day policy is the documented pairing, and must stay quiet.
    assert "rotation does not revoke" not in _sub_warnings_from(
        f"{_SUBCA}&permitdns=db.example.com&lifetime=8d")
    # The caution is about the SUBORDINATE's validity, so a long-lived LEAF is silent —
    # same address, other package.
    assert "rotation does not revoke" not in _warnings_from(
        f"gcpcas?project=p&location=l&pool=q&lifetime=1y&{_STORE}")


def test_the_bundle_caution_follows_the_packages_own_default():
    # PRA Vault's X.509 Parent Certificate Authority account takes a PEM key, a
    # passphrase and a PEM certificate as three separate fields — a PKCS#12 has to be
    # unpacked with openssl before any of it can be pasted in. Warned rather than
    # refused: a subordinate could be destined for something else, and this cannot know.
    #
    # The EFFECTIVE value is what the caution keys off, and it differs per package. On
    # the Subordinate CA package the default is already PemBundle, so a silent address is
    # RIGHT and must be silent — that is the change the plugin split brought.
    assert "pembundle" not in _sub_warnings_from(f"{_SUBCA}&permitdns=db.example.com")
    # Typed on purpose, the caution fires and names what was actually set.
    msg = _sub_warnings_from(f"{_SUBCA}&permitdns=db.example.com&bundle=Pkcs12")
    assert "pembundle" in msg and "default" not in msg
    assert "pembundle" not in _sub_warnings_from(
        f"{_SUBCA}&permitdns=db.example.com&bundle=PemBundle")
    # And a LEAF is never cautioned about this — PKCS#12 is the right default there.
    assert "pembundle" not in _warnings_from(
        f"gcpcas?project=p&location=l&pool=q&{_STORE}")


def test_the_aws_template_and_the_package_may_not_contradict_each_other():
    # ACM PCA ignores the CSR's basic constraints and builds from a template, so these
    # two naming different things is not a preference to resolve — one of them is a lie,
    # and the request SUCCEEDS while returning the wrong kind of certificate.
    aws = f"awspca?arn=arn:aws:acm-pca:us-east-1:1:certificate-authority/x&{_STORE}"
    sub = "arn:aws:acm-pca:::template/SubordinateCACertificate_PathLen0/V1"
    leaf = "arn:aws:acm-pca:::template/EndEntityCertificate/V1"
    _sub_ok(f"{aws}&templatearn={sub}")
    _ok(f"{aws}&templatearn={leaf}")
    msg = _sub_bad(f"{aws}&templatearn={leaf}", "templatearn", "contradicts")
    assert "end-entity" in msg.lower()
    # The more insidious direction: a subordinate template on the LEAF package produces
    # a real CA certificate that the rest of the profile was never written for.
    msg = _bad(f"{aws}&templatearn={sub}", "templatearn", "isca")
    assert "authority" in msg.lower()


# ── the composer's side of the sub-CA grammar ──────────────────────────────────
# cert_ps_service builds addresses from config defaults + form values and validates the
# result, so a refusal added above can be triggered by a value the operator never chose
# for that profile. These two pin the seam.

from web_dashboard.services import cert_ps_service as cps  # noqa: E402


def test_the_subordinate_options_compose_before_the_certificate_shape():
    # Not cosmetic: isca= changes the meaning of every option after it, and an unknown
    # key would otherwise be appended at the tail, after owner=.
    addr = cps.compose_address("gcpcas", {"owner": "1", "lifetime": "8d",
                                          "isca": "true", "pathlen": "0",
                                          "permitdns": "db.example.com"})
    assert addr.index("isca=") < addr.index("lifetime=") < addr.index("owner=")
    assert addr.index("isca=") < addr.index("permitdns=") < addr.index("lifetime=")


def test_a_config_default_eku_does_not_block_a_subordinate_ca():
    # cert_default_eku applies to EVERY profile, so if it survived into a sub-CA address
    # the validator's eku= refusal would block the path with an option nobody chose for
    # it. Dropped from the DEFAULTS layer only.
    base = {"project": "p", "location": "l", "pool": "q"}
    store = {"biurl": "https://b", "owner": "1"}
    real_defaults = cps.profile_defaults
    cps.profile_defaults = lambda package=cps.CERT_PACKAGE_LEAF: {
        "eku": "ClientAuth", "key": "rsa3072"}
    try:
        addr = cps.build_address("gcpcas", {**base, "permitdns": "db.example.com"},
                                 store, cps.CERT_PACKAGE_SUBCA)
        assert "eku=" not in addr and "key=rsa3072" in addr
        # A leaf still gets it — this must not have disabled the default outright.
        assert "eku=ClientAuth" in cps.build_address("gcpcas", base, store)
        # And an EXPLICIT eku= on the subordinate package is still a contradiction,
        # because there it is the operator's own value rather than a global default.
        try:
            cps.build_address("gcpcas", {**base, "eku": "ClientAuth"}, store,
                              cps.CERT_PACKAGE_SUBCA)
        except ps.PSResourceError as exc:
            assert "eku=" in str(exc)
        else:
            raise AssertionError(
                "an explicit eku= on the subordinate package must still be refused")
    finally:
        cps.profile_defaults = real_defaults


def test_the_package_default_isca_is_never_spelled_out_onto_the_address():
    # The package IS the default, so emitting isca=true again spends 10 characters to
    # restate it — on the one topology where the budget and the safety control (a
    # permitted-DNS list, which can exceed 100 characters alone) pull against each other.
    base = {"project": "p", "location": "l", "pool": "q"}
    store = {"biurl": "https://b", "owner": "1"}
    addr = cps.build_address("gcpcas", {**base, "permitdns": "db.example.com"},
                             store, cps.CERT_PACKAGE_SUBCA)
    assert "isca=" not in addr, addr
    # Even asked for explicitly, when it AGREES with the package.
    addr = cps.build_address("gcpcas", {**base, "isca": "true"}, store,
                             cps.CERT_PACKAGE_SUBCA)
    assert "isca=" not in addr, addr
    # But an explicit isca=false is a real override of the package and must survive —
    # dropping it would silently issue an authority where a leaf was asked for.
    addr = cps.build_address("gcpcas", {**base, "isca": "false"}, store,
                             cps.CERT_PACKAGE_SUBCA)
    assert "isca=false" in addr, addr


def test_the_preview_names_the_platform_the_address_is_destined_for():
    # The page shows this beside the address. Two platforms means "255 characters" is no
    # longer the only thing an operator needs to see about where a profile is going.
    base = {"project": "p", "location": "l", "pool": "q"}
    leaf = cps.address_preview("gcpcas", base, None, cps.CERT_PACKAGE_LEAF)
    sub = cps.address_preview("gcpcas", base, {"permitdns": "db.example.com"},
                              cps.CERT_PACKAGE_SUBCA)
    assert leaf["package"] == cps.CERT_PACKAGE_LEAF
    assert sub["package"] == cps.CERT_PACKAGE_SUBCA
    assert leaf["platform"] != sub["platform"]
    assert leaf["error"] is None and sub["error"] is None
    # A refused profile still returns the address it WOULD have built, plus the reason —
    # the field has to show both while it is still being edited.
    bad = cps.address_preview("gcpcas", base, {"permitdns": "db.example.com"},
                              cps.CERT_PACKAGE_LEAF)
    assert bad["error"] and "subordinate ca" in bad["error"].lower()
    assert bad["address"] and bad["length"] == len(bad["address"])


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
