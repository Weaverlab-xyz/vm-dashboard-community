"""Certificate Lab: the dashboard mints the functional account from the CA build.

The Certificate plugin needs one functional account carrying TWO credentials — the
certificate authority's, and the BeyondInsight identity that writes the bundle to Secrets
Safe — and there are two shapes of the second. The plugin reads both and **prefers OAuth**:

    oauth   name and password are the CA credential WHOLE, and the BeyondInsight OAuth
            client id and secret ride the account's own API key and secret fields
    apikey  name ``<ca-account>:<bi-run-as-user>``, password ``<ca-secret>:<bi-api-key>``,
            each split on the LAST colon

That account used to be an operator's manual step, which was the wrong shape for a reason
that is easy to lose:

- **the CA half exists for one moment only.** It comes back in the apply's outputs — a
  GCP service account key, an AWS secret access key — both of which their APIs return
  exactly once. By the time somebody opens BeyondInsight the only copies left are the
  remote terraform state and the dict that is about to go out of scope;
- **the manual path hits a wall the automated one does not.** ``ps-cli`` caps a
  functional-account password at 1,000 characters and a GCP ``private_key`` PEM is about
  1,700. The REST API this uses accepts 3,216;
- **the most common mistake is pasting the whole JSON key file** instead of the
  ``private_key`` field out of it.

So these pin: the composition per cloud on both shapes, which of the two the resolver
picks (and that an install already working on the packed one is never moved off it by an
upgrade), that the PEM is the field and survives at full length, that a referenced account
is never recorded as owned (teardown deletes only what it minted), and that neither
composed credential ever reaches a log line.

Runs under pytest, or standalone:  python tests/test_cert_lab_functional_account.py
"""
import asyncio
import io
import logging
import os
import re
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

CONF = {}
CALLS = []

# A real-shape PKCS#8 RSA-2048 PEM: what matters is that it is over the 1,000-character
# ps-cli cap, so a test that quietly used a short key would prove nothing.
_PEM = ("-----BEGIN PRIVATE KEY-----\n"
        + "\n".join(["MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7VJTUt9Us8cKj"] * 26)
        + "\n-----END PRIVATE KEY-----\n")
_KEY_JSON = ('{"type": "service_account", "project_id": "bt-se-lab", '
             '"private_key_id": "abc123", "private_key": '
             + '"' + _PEM.replace("\n", "\\n") + '", '
             '"client_email": "certauth-1a2b3c4d@bt-se-lab.iam.gserviceaccount.com"}')

_GCP_OUTPUTS = {
    "pool_id": "demo-pipeline-pool-47455d",
    "location": "us-central1",
    "ca_chain_pem": "-----BEGIN CERTIFICATE-----\nchain\n-----END CERTIFICATE-----",
    "service_account_email": "certauth-1a2b3c4d@bt-se-lab.iam.gserviceaccount.com",
    "service_account_key_json": _KEY_JSON,
}
_AWS_OUTPUTS = {
    "ca_arn": "arn:aws:acm-pca:us-east-1:111122223333:certificate-authority/abc",
    "region": "us-east-1",
    "ca_chain_pem": "-----BEGIN CERTIFICATE-----\nchain\n-----END CERTIFICATE-----",
    "enroll_access_key_id": "AKIAIOSFODNN7EXAMPLE",
    "enroll_secret_access_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
}


class _Settings:
    def __getattr__(self, _key):
        return ""


class _CertLab:
    def __init__(self, **kw):
        self.id = "1a2b3c4d-5e6f-7890-abcd-ef1234567890"
        self.name = "demo-pipeline"
        self.cloud = "gcp"
        self.error_message = None
        self.ps_functional_account = None
        self.ps_functional_account_id = None
        self.__dict__.update(kw)


async def _fake_create_fa_on_platform(*, platform_id, account_name, display_name,
                                      password, description="", api_key="",
                                      api_secret="", tenant=None):
    CALLS.append(("create", platform_id, account_name, display_name, password,
                  api_key, api_secret))
    return 4242


async def _fake_get_platform_id(name):
    CALLS.append(("get_platform_id", name))
    return 1008


async def _fake_get_functional_account(name, tenant=None):
    CALLS.append(("get", name))
    return {"id": 7, "platform_id": 1008, "platform_name": "Certificate",
            "account_name": name}


async def _fake_delete_functional_account(account_id, tenant=None):
    CALLS.append(("delete", account_id))


def _install_stubs():
    confmod = types.ModuleType("web_dashboard.config")
    confmod.settings = _Settings()
    sys.modules["web_dashboard.config"] = confmod

    cfg = types.ModuleType("web_dashboard.services.config_service")
    cfg.get = lambda key: CONF.get(key, "")
    cfg.get_bool = lambda key, default=False: bool(CONF.get(key, default))
    sys.modules["web_dashboard.services.config_service"] = cfg

    ps = types.ModuleType("web_dashboard.services.ps_api_service")
    ps.create_functional_account_on_platform = _fake_create_fa_on_platform
    ps.get_platform_id = _fake_get_platform_id
    ps.get_functional_account = _fake_get_functional_account
    ps.delete_functional_account = _fake_delete_functional_account
    sys.modules["web_dashboard.services.ps_api_service"] = ps

    psr = types.ModuleType("web_dashboard.services.ps_resource_service")
    psr.PSResourceError = type("PSResourceError", (Exception,), {})
    psr._MAX_MANAGED_SYSTEM_ADDRESS = 255
    # The two packages. cert_ps_service imports these by name, so the stub has to carry
    # them — and `cert_normalise_package` is copied rather than faked, because every
    # package-dependent branch under test keys off it and a stub that normalised
    # differently would prove the wrong thing.
    psr.CERT_PACKAGE_LEAF = "certificate"
    psr.CERT_PACKAGE_SUBCA = "subca"
    psr.CERT_PACKAGES = ("certificate", "subca")
    psr.cert_normalise_package = lambda package: (
        "subca" if (package or "").strip().lower() in (
            "subca", "subordinateca", "subordinate ca", "subordinate", "subordinate-ca")
        else "certificate")
    sys.modules["web_dashboard.services.ps_resource_service"] = psr


_install_stubs()
try:
    from web_dashboard.services import cert_ps_service as svc
except Exception as exc:  # pragma: no cover — skip if other app deps are missing
    try:
        import pytest
        pytest.skip(f"cert_ps_service import unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)


def _reset(**conf):
    CONF.clear()
    CONF.update({"cert_ps_bi_api_key": "9f2c1b7e4a",
                 "pscli_api_account_name": "certauth-svc"})
    CONF.update(conf)
    CALLS[:] = []


def _run(coro):
    return asyncio.run(coro)


def _last_create():
    for call in reversed(CALLS):
        if call[0] == "create":
            return call
    raise AssertionError(f"no functional account was created; calls were {CALLS}")


# ── composing the two credentials ─────────────────────────────────────────────

def test_gcp_composes_both_halves_onto_one_account():
    _reset()
    out = _run(svc.ensure_functional_account(_CertLab(), _GCP_OUTPUTS))
    _, platform_id, account_name, display_name, password, _, _ = _last_create()
    assert platform_id == 1008
    assert account_name == ("certauth-1a2b3c4d@bt-se-lab.iam.gserviceaccount.com"
                            ":certauth-svc")
    assert password.endswith(":9f2c1b7e4a")
    assert out["account_name"] == account_name
    assert out["id"] == "4242"
    assert out["mode"] == "create"


def test_aws_composes_from_its_own_differently_named_outputs():
    _reset()
    _run(svc.ensure_functional_account(_CertLab(cloud="aws"), _AWS_OUTPUTS))
    _, _, account_name, _, password, _, _ = _last_create()
    assert account_name == "AKIAIOSFODNN7EXAMPLE:certauth-svc"
    assert password == "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY:9f2c1b7e4a"


def test_the_password_is_the_private_key_field_not_the_whole_json_file():
    """The single most common setup mistake, and it is silent — the plugin takes the
    value it is given and fails later, at a rotation."""
    _reset()
    _run(svc.ensure_functional_account(_CertLab(), _GCP_OUTPUTS))
    password = _last_create()[4]
    assert password.startswith("-----BEGIN PRIVATE KEY-----")
    assert not password.lstrip().startswith("{")
    assert "service_account" not in password, "the JSON envelope leaked into the password"
    assert "private_key_id" not in password


def test_a_full_size_pem_survives_at_full_length():
    """ps-cli caps a functional-account password at 1,000 characters; the REST API this
    goes through accepts 3,216. Nothing here may quietly truncate to the smaller one."""
    _reset()
    _run(svc.ensure_functional_account(_CertLab(), _GCP_OUTPUTS))
    password = _last_create()[4]
    assert len(_PEM) > 1000, "the fixture key is too short to prove anything"
    assert password == f"{_PEM}:9f2c1b7e4a"


def test_the_escaped_newlines_in_the_key_file_come_back_as_real_ones():
    _reset()
    _run(svc.ensure_functional_account(_CertLab(), _GCP_OUTPUTS))
    password = _last_create()[4]
    assert "\\n" not in password, "the PEM is still JSON-escaped"
    assert password.count("\n") > 20


def test_the_display_name_carries_the_row_so_a_retry_resolves_back():
    _reset()
    _run(svc.ensure_functional_account(_CertLab(), _GCP_OUTPUTS))
    display_name = _last_create()[3]
    assert display_name == "demo-pipeline-certauth-1a2b3c4d"


# ── the BeyondInsight half ────────────────────────────────────────────────────

def test_the_run_as_user_falls_back_to_the_configured_password_safe_one():
    _reset()
    _run(svc.ensure_functional_account(_CertLab(), _GCP_OUTPUTS))
    assert _last_create()[2].endswith(":certauth-svc")

    _reset(cert_ps_bi_run_as_user="cert-plugin-svc")
    _run(svc.ensure_functional_account(_CertLab(), _GCP_OUTPUTS))
    assert _last_create()[2].endswith(":cert-plugin-svc")


def test_a_missing_api_key_names_the_config_key_and_creates_nothing():
    _reset(cert_ps_bi_api_key="")
    try:
        _run(svc.ensure_functional_account(_CertLab(), _GCP_OUTPUTS))
    except svc.CertPSError as exc:
        assert "cert_ps_bi_api_key" in str(exc)
    else:
        raise AssertionError("a missing API key must raise")
    assert not [c for c in CALLS if c[0] == "create"]


def test_a_missing_run_as_user_names_both_keys_it_could_come_from():
    _reset(pscli_api_account_name="")
    try:
        _run(svc.ensure_functional_account(_CertLab(), _GCP_OUTPUTS))
    except svc.CertPSError as exc:
        assert "cert_ps_bi_run_as_user" in str(exc)
        assert "pscli_api_account_name" in str(exc)
    else:
        raise AssertionError("a missing run-as user must raise")


def test_a_colon_in_a_beyondinsight_half_is_refused():
    """Splitting on the LAST colon is what lets the CA credential contain one. A colon
    in either BeyondInsight half moves the split point and mis-parses BOTH fields."""
    for key, value in (("cert_ps_bi_run_as_user", "corp:svc"),
                       ("cert_ps_bi_api_key", "9f2c:1b7e")):
        _reset(**{key: value})
        try:
            _run(svc.ensure_functional_account(_CertLab(), _GCP_OUTPUTS))
        except svc.CertPSError as exc:
            assert "':'" in str(exc) or "colon" in str(exc).lower()
        else:
            raise AssertionError(f"a colon in {key} must be refused")
        assert not [c for c in CALLS if c[0] == "create"]


# ── bad or absent CA material ─────────────────────────────────────────────────

def test_a_key_file_without_a_private_key_field_is_refused():
    _reset()
    outputs = dict(_GCP_OUTPUTS, service_account_key_json='{"type": "service_account"}')
    try:
        _run(svc.ensure_functional_account(_CertLab(), outputs))
    except svc.CertPSError as exc:
        assert "private_key" in str(exc)
    else:
        raise AssertionError("a key file with no private_key must be refused")


def test_an_apply_that_returned_no_credential_is_refused():
    _reset()
    outputs = dict(_GCP_OUTPUTS, service_account_key_json="")
    try:
        _run(svc.ensure_functional_account(_CertLab(), outputs))
    except svc.CertPSError as exc:
        assert "enrollment credential" in str(exc)
    else:
        raise AssertionError("no credential must be refused")


# ── modes ─────────────────────────────────────────────────────────────────────

def test_reference_mode_creates_nothing_and_uses_the_named_account():
    _reset(cert_ps_functional_account_mode="reference",
           cert_ps_functional_account="svc-adcs:certauth-svc")
    out = _run(svc.ensure_functional_account(_CertLab(), _GCP_OUTPUTS))
    assert out == {"mode": "reference", "package": "certificate",
                   "account_name": "svc-adcs:certauth-svc", "id": None}
    assert CALLS == [], "reference mode must not touch Password Safe here"


def test_reference_mode_falls_back_to_the_leaf_account_for_the_subordinate_package():
    """Right for an operator running only one of the two platforms, and wrong for one
    running both — so the dedicated key wins where it is set."""
    _reset(cert_ps_functional_account_mode="reference",
           cert_ps_functional_account="svc-adcs:certauth-svc")
    out = _run(svc.ensure_functional_account(_CertLab(), _GCP_OUTPUTS, "subca"))
    assert out["account_name"] == "svc-adcs:certauth-svc"
    assert out["package"] == "subca"
    _reset(cert_ps_functional_account_mode="reference",
           cert_ps_functional_account="svc-adcs:certauth-svc",
           cert_ps_subca_functional_account="svc-subca:certauth-svc")
    out = _run(svc.ensure_functional_account(_CertLab(), _GCP_OUTPUTS, "subca"))
    assert out["account_name"] == "svc-subca:certauth-svc"
    # ...and the leaf one is unaffected by the presence of the second key.
    out = _run(svc.ensure_functional_account(_CertLab(), _GCP_OUTPUTS))
    assert out["account_name"] == "svc-adcs:certauth-svc"


def test_reference_mode_reports_no_id_so_teardown_cannot_delete_it():
    """The id is the ownership flag. Returning one for an operator's own account would
    make teardown delete something every other CA may point at."""
    _reset(cert_ps_functional_account_mode="reference",
           cert_ps_functional_account="svc-adcs:certauth-svc")
    assert _run(svc.ensure_functional_account(_CertLab(), _GCP_OUTPUTS))["id"] is None


def test_the_mode_is_case_and_whitespace_tolerant():
    _reset(cert_ps_functional_account_mode="  Reference  ")
    assert svc.functional_account_mode() == "reference"


def test_an_unset_or_mistyped_mode_creates():
    """Anything that is not `reference` is `create`: a typo must not silently switch off
    the thing that makes the feature work."""
    _reset(cert_ps_functional_account_mode="")
    assert svc.functional_account_mode() == "create"
    _reset(cert_ps_functional_account_mode="refrence")
    assert svc.functional_account_mode() == "create"


def test_the_empty_account_error_points_at_the_right_remedy_per_mode():
    _reset()
    try:
        _run(svc.resolve_functional_account(""))
    except svc.CertPSError as exc:
        assert "Wire up Password Safe" in str(exc)
    else:
        raise AssertionError("create mode with no account must raise")

    _reset(cert_ps_functional_account_mode="reference")
    try:
        _run(svc.resolve_functional_account(""))
    except svc.CertPSError as exc:
        assert "cert_ps_functional_account" in str(exc)
    else:
        raise AssertionError("reference mode with no account must raise")


# ── the credential must not leak ──────────────────────────────────────────────

def test_the_composed_password_never_reaches_a_log_line():
    """The progress path persists every line verbatim into JobLog with no redaction, so
    a logger call carrying the password would put a private key in the database."""
    _reset()
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    root = logging.getLogger()
    root.addHandler(handler)
    old_level = root.level
    root.setLevel(logging.DEBUG)
    try:
        _run(svc.ensure_functional_account(_CertLab(), _GCP_OUTPUTS))
    finally:
        root.removeHandler(handler)
        root.setLevel(old_level)
    logged = buf.getvalue()
    assert "BEGIN PRIVATE KEY" not in logged
    assert "9f2c1b7e4a" not in logged, "the API key was logged"


def test_the_returned_dict_carries_the_name_but_never_the_credential():
    _reset()
    out = _run(svc.ensure_functional_account(_CertLab(), _GCP_OUTPUTS))
    assert "BEGIN PRIVATE KEY" not in repr(out)
    assert "9f2c1b7e4a" not in repr(out)


# ── the OAuth path, and which of the two the resolver picks ───────────────────
#
# The plugin reads BOTH and prefers OAuth: `ECredentialType` is a flags enum and
# `CredentialParameter` carries ApiKey/ApiSecret as fields of their own, so one account
# can be `Password, ApiKey` at once. What the dashboard cannot see from here is whether a
# given Password Safe console offers an API key credential type on a PLUGIN-supplied
# platform, so the packed form stays and `cert_ps_bi_auth` pins either.

_OAUTH = {"cert_ps_bi_client_id": "8f14e45f-ea", "cert_ps_bi_client_secret": "s3cr3t-cs"}


def test_oauth_puts_four_values_in_four_fields_and_packs_nothing():
    _reset(cert_ps_bi_api_key="", **_OAUTH)
    out = _run(svc.ensure_functional_account(_CertLab(), _GCP_OUTPUTS))
    _, _, account_name, _, password, api_key, api_secret = _last_create()
    assert account_name == "certauth-1a2b3c4d@bt-se-lab.iam.gserviceaccount.com", \
        "the OAuth path appends no run-as user — the name is the CA account, whole"
    assert password == _PEM, "the password is the CA secret, whole and unsuffixed"
    assert (api_key, api_secret) == ("8f14e45f-ea", "s3cr3t-cs")
    assert out["auth"] == "oauth"


def test_the_oauth_path_lets_a_ca_secret_contain_a_colon():
    """Not expressible at all on the packed path, where the LAST colon is the delimiter:
    a CA secret ending in something colon-shaped would be silently truncated and the tail
    read as an API key."""
    _reset(cert_ps_bi_api_key="", **_OAUTH)
    outputs = dict(_AWS_OUTPUTS, enroll_secret_access_key="S0me:P@ssword")
    _run(svc.ensure_functional_account(_CertLab(cloud="aws"), outputs))
    assert _last_create()[4] == "S0me:P@ssword"


def test_the_packed_path_sends_neither_api_field():
    """Half a pair is not a pair — the plugin falls back to the packed format on one, so
    an ApiKey with no secret would produce an account that authenticates as nothing."""
    _reset()
    _run(svc.ensure_functional_account(_CertLab(), _GCP_OUTPUTS))
    assert _last_create()[5:] == ("", "")


def test_auto_keeps_an_install_that_already_has_an_api_key_on_the_packed_path():
    """The regression that must not happen on an upgrade. A functional account that
    authenticates as nothing onboards GREEN and fails hours later at a rotation, so an
    install working on the packed path stays there until somebody says otherwise."""
    _reset(pscli_client_id="dash-client", pscli_client_secret="dash-secret")
    assert svc.resolve_bi_credential()["auth"] == "apikey"
    _run(svc.ensure_functional_account(_CertLab(), _GCP_OUTPUTS))
    assert _last_create()[2].endswith(":certauth-svc")
    assert _last_create()[5] == ""


def test_auto_reaches_oauth_from_the_dashboards_own_registration():
    """Same tenant by construction, so an install that has configured Password Safe at
    all needs nothing new to reach the preferred path."""
    _reset(cert_ps_bi_api_key="", pscli_client_id="dash-client",
           pscli_client_secret="dash-secret")
    cred = svc.resolve_bi_credential()
    assert cred["auth"] == "oauth"
    assert cred["source"] == "pscli_client_id"
    assert (cred["client_id"], cred["client_secret"]) == ("dash-client", "dash-secret")


def test_a_dedicated_registration_outranks_a_configured_api_key():
    """Nobody sets cert_ps_bi_client_id by accident, and the dedicated one is the point:
    the dashboard's own client administers the whole tenant, where this is handed to a
    plugin running on a Resource Broker."""
    _reset(pscli_client_id="dash-client", pscli_client_secret="dash-secret", **_OAUTH)
    cred = svc.resolve_bi_credential()
    assert cred["auth"] == "oauth"
    assert cred["source"] == "cert_ps_bi_client_id"
    assert cred["client_id"] == "8f14e45f-ea"


def test_half_a_dedicated_pair_is_refused_rather_than_silently_substituted():
    """Falling through to pscli_* would authenticate as a DIFFERENT registration than the
    one named in config, and the only symptom would be a permission error on a folder the
    operator believes they granted."""
    for key in ("cert_ps_bi_client_id", "cert_ps_bi_client_secret"):
        _reset(pscli_client_id="dash-client", pscli_client_secret="dash-secret",
               **{key: _OAUTH[key]})
        try:
            svc.resolve_bi_credential()
        except svc.CertPSError as exc:
            assert "cert_ps_bi_client_id" in str(exc) and "cert_ps_bi_client_secret" in str(exc)
        else:
            raise AssertionError(f"{key} alone must be refused")


def test_pinning_apikey_ignores_an_oauth_registration_entirely():
    """For a console that will not offer an API key credential type on a plugin platform.
    A stray half-set client id must not stand in the way of the path that was pinned."""
    _reset(cert_ps_bi_auth="apikey", cert_ps_bi_client_id="8f14e45f-ea")
    cred = svc.resolve_bi_credential()
    assert cred["auth"] == "apikey"

    _reset(cert_ps_bi_auth="apikey", cert_ps_bi_api_key="", **_OAUTH)
    try:
        svc.resolve_bi_credential()
    except svc.CertPSError as exc:
        assert "cert_ps_bi_api_key" in str(exc)
    else:
        raise AssertionError("apikey pinned with no API key must raise")


def test_pinning_oauth_refuses_when_no_registration_resolves():
    _reset(cert_ps_bi_auth="oauth")
    try:
        svc.resolve_bi_credential()
    except svc.CertPSError as exc:
        assert "cert_ps_bi_client_id" in str(exc)
    else:
        raise AssertionError("oauth pinned with no registration must raise")


def test_an_unrecognised_auth_mode_is_auto_rather_than_a_refusal():
    _reset(cert_ps_bi_auth="  OAuth  ")
    assert svc.bi_auth_mode() == "oauth"
    for val in ("", "oauth2", "apikeys"):
        _reset(cert_ps_bi_auth=val)
        assert svc.bi_auth_mode() == "auto", val


def test_no_beyondinsight_credential_at_all_names_both_ways_to_supply_one():
    _reset(cert_ps_bi_api_key="")
    try:
        _run(svc.ensure_functional_account(_CertLab(), _GCP_OUTPUTS))
    except svc.CertPSError as exc:
        assert "cert_ps_bi_client_id" in str(exc) and "cert_ps_bi_api_key" in str(exc)
    else:
        raise AssertionError("no BeyondInsight credential must raise")
    assert not [c for c in CALLS if c[0] == "create"]


def test_the_oauth_client_secret_never_reaches_a_log_line():
    """The mint logs which path it took — that is the first thing worth knowing when the
    plugin later reports no BeyondInsight credential — but the SOURCE is a config key
    name, never the value."""
    _reset(cert_ps_bi_api_key="", **_OAUTH)
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    root = logging.getLogger()
    root.addHandler(handler)
    old_level = root.level
    root.setLevel(logging.DEBUG)
    try:
        _run(svc.ensure_functional_account(_CertLab(), _GCP_OUTPUTS))
    finally:
        root.removeHandler(handler)
        root.setLevel(old_level)
    logged = buf.getvalue()
    assert "s3cr3t-cs" not in logged, "the OAuth client secret was logged"
    assert "BEGIN PRIVATE KEY" not in logged
    assert "auth=oauth" in logged, "which path an account was minted on has to be visible"


def test_a_colon_free_name_stops_being_warned_about_once_oauth_is_configured():
    """A colon-free name is the NORMAL shape on the OAuth path — the BeyondInsight half
    rides the account's API fields, which GET FunctionalAccounts does not return, so the
    name says nothing about it and the old warning would be false."""
    def _warnings_for(**conf):
        _reset(**conf)
        buf = io.StringIO()
        handler = logging.StreamHandler(buf)
        root = logging.getLogger()
        root.addHandler(handler)
        old_level = root.level
        root.setLevel(logging.DEBUG)
        try:
            _run(svc.resolve_functional_account("svc-adcs-enroll"))
        finally:
            root.removeHandler(handler)
            root.setLevel(old_level)
        return buf.getvalue()

    assert "appsettings.json" in _warnings_for(), \
        "with no OAuth registration a missing second half is still a real problem"
    assert "appsettings.json" not in _warnings_for(cert_ps_bi_api_key="", **_OAUTH)


# ── the wiring, read off the source ───────────────────────────────────────────
#
# Text assertions rather than execution, matching tests/test_cert_lab_wiring.py: these
# are cross-file couplings where the failure is silent, and the whole cert_lab_service
# import pulls in sqlalchemy and the job worker for no extra proof.

def _src(rel):
    with open(os.path.join(_ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def test_the_build_wires_up_the_account_and_never_fails_on_it():
    src = _src("web_dashboard/services/cert_lab_service.py")
    block = src.split("async def run_provision_apply(")[1].split("\nasync def ")[0]
    assert "_wire_up_functional_account(row, outputs)" in block, \
        "the build must mint the account while the apply's outputs are still in scope"
    assert block.index("_read_outputs(row, outputs)") < \
        block.index("_wire_up_functional_account"), "outputs are read first"

    helper = src.split("async def _wire_up_functional_account(")[1].split("\nasync def ")[0]
    assert "except Exception" in helper, "a CA that exists must not be rolled back"
    assert "row.error_message = str(exc)" in helper, \
        "only the message may be stored — never anything out of `outputs`"
    assert "row.status" not in helper, "the CA stays available when the account fails"


def test_add_identity_uses_this_cas_own_account_for_this_package():
    src = _src("web_dashboard/services/cert_lab_service.py")
    block = src.split("async def run_ps_register(")[1].split("\nasync def ")[0]
    assert "functional_account=functional_account_for(row, package)" in block, \
        ("onboarding against another CA's identity fails every credential action — and "
         "against the other PACKAGE's identity fails them too, since a managed system "
         "inherits its functional account's platform")


def test_add_identity_is_refused_at_the_click_when_the_leaf_account_is_missing():
    """And it consults BOTH sources: a CA built before this has a NULL column and an
    operator-configured account that works, which must not start being refused.

    Only the LEAF package is refused for a missing account. The subordinate one is minted
    lazily on first use, so its absence is the ordinary state of a CA that has never
    issued an authority rather than a fault."""
    src = _src("web_dashboard/services/cert_lab_service.py")
    block = src.split("def start_ps_register(")[1].split("\nasync def ")[0]
    assert "functional_account_for(row, package)" in block
    assert 'config_service.get("cert_ps_functional_account")' in block
    assert "Wire up Password Safe" in block
    assert "CERT_PACKAGE_LEAF and not (" in block, \
        "the subordinate account is minted lazily; refusing its absence blocks the path"


def test_the_subordinate_account_is_minted_lazily_from_the_state():
    """A CA that never issues an authority should carry no functional account on the
    platform that would — and the enrollment credential is still in its terraform state,
    so this needs no rebuild. That matters here more than usual: CAS never hands a
    deleted pool id back, so a rebuild is a one-way door."""
    src = _src("web_dashboard/services/cert_lab_service.py")
    block = src.split("async def run_ps_register(")[1].split("\nasync def ")[0]
    assert "if not functional_account_for(row, package):" in block
    assert "_read_ca_outputs(row)" in block
    assert "_wire_up_functional_account(row, outputs, package)" in block
    # And it is fatal rather than reported: a managed system cannot exist on a platform
    # with no functional account, so there is nothing to carry on to.
    assert "raise CertLabError(" in block.split("_wire_up_functional_account(row, outputs, package)")[1]


def test_the_page_shows_both_accounts_and_offers_the_retry_per_package():
    api = _src("web_dashboard/api/cert_lab.py")
    # The Certificate tab of the Workload Lab, markup and Alpine factory in the one file.
    page = _src("web_dashboard/templates/workload_lab/_certificates.html")
    assert '"functional_account": row.ps_functional_account' in api
    assert '"subca_functional_account": row.ps_subca_functional_account' in api
    assert "/functional-account" in api, "the retry route is missing"
    assert "wireUp(ca, 'certificate')" in page and "async wireUp(ca, pkg)" in page
    assert "ca.functional_account" in page, \
        "a missing account has to be visible before Add identity is clicked"


def test_teardown_deletes_both_accounts_and_only_ones_the_dashboard_minted():
    """Both, because an account is platform-bound: a CA that served the Certificate AND
    the Subordinate CA platform has one on each, and forgetting the second leaves an
    orphan holding a live enrollment credential after the CA it belonged to is gone."""
    src = _src("web_dashboard/services/cert_lab_service.py")
    block = src.split("async def run_decommission(")[1].split("\nasync def ")[0]
    assert "for package, (name_col, id_col) in _FA_COLUMNS.items():" in block, \
        "one account is deleted and the other orphaned"
    assert "if not fa_id:\n                continue" in block, \
        "a NULL id is an operator-owned account and must survive"
    assert "delete_functional_account" in block
    assert "if not deregistered:" in block, \
        "a still-registered managed system blocks the delete; say so rather than 400"
    assert block.index("deregister(row.ps_tf_state)") < \
        block.index("delete_functional_account"), "the managed system goes first"


def test_the_id_is_cleared_only_after_a_successful_delete():
    src = _src("web_dashboard/services/cert_lab_service.py")
    block = src.split("async def run_decommission(")[1].split("\nasync def ")[0]
    delete_at = block.index("delete_functional_account")
    clear_at = block.index("setattr(row, id_col, None)", delete_at)
    except_at = block.index("except Exception as exc:", delete_at)
    assert clear_at < except_at, \
        "clearing before the delete succeeds makes a retried teardown skip it"


def test_the_retry_reads_the_credential_back_out_of_the_state():
    src = _src("web_dashboard/services/cert_lab_service.py")
    reader = src.split("async def _read_ca_outputs(")[1].split("\nasync def ")[0]
    assert "terraform.read_state_outputs(row.deploy_job_id)" in reader, \
        "a rebuild is not a retry: CAS never hands a deleted pool id back"
    block = src.split("async def rewire_functional_account(")[1].split("\nasync def ")[0]
    assert "_read_ca_outputs(row)" in block
    assert "read_state_outputs" in _src("web_dashboard/services/terraform.py")


def test_every_account_column_has_a_migration():
    src = _src("web_dashboard/database.py")
    for col in ("ps_functional_account", "ps_functional_account_id",
                "ps_subca_functional_account", "ps_subca_functional_account_id"):
        assert f"ALTER TABLE cert_labs ADD COLUMN {col} " in src, \
            f"{col} has no migration, so it is missing on every existing install"


def test_every_new_config_key_is_declared_bound_and_classified():
    conf = _src("web_dashboard/config.py")
    setup = _src("web_dashboard/api/setup.py")
    panel = _src("web_dashboard/templates/settings.html")
    model = setup.split("class CertLabFeatureConfig(")[1].split("\nclass ")[0]
    for key in ("cert_ps_bi_api_key", "cert_ps_bi_run_as_user",
                "cert_ps_functional_account_mode", "cert_ps_bi_auth",
                "cert_ps_bi_client_id", "cert_ps_bi_client_secret"):
        assert re.search(rf"^    {key}: ", conf, re.M), f"{key} missing from Settings"
        assert f"{key}: " in model, f"{key} missing from CertLabFeatureConfig"
        assert f"panelCfg.{key}" in panel, f"{key} is unbound, so a save discards it"
    secrets = setup.split("_SECRET_FEATURE_KEYS = frozenset({")[1].split("})")[0]
    for key in ("cert_ps_bi_api_key", "cert_ps_bi_client_secret"):
        assert f'"{key}"' in secrets, \
            "an unclassified secret round-trips in clear through the feature API"
    assert '("cert_ps_bi_client_secret",' in _src("web_dashboard/services/secret_hygiene.py"), \
        "a secret missing from SECRET_REGISTRY is invisible to the hygiene scanner"


def test_the_auth_select_keeps_its_x_init_default():
    """Same trap as the mode select: an unset string key reads back "" from
    /api/setup/feature, not the model default, so a select without x-init renders blank
    and saves "" over it — which here would read as `auto` anyway, but only by accident."""
    panel = _src("web_dashboard/templates/settings.html")
    at = panel.index('x-model="panelCfg.cert_ps_bi_auth"')
    assert "x-init" in panel[at:at + 300]


def test_nothing_read_out_of_the_credential_dict_reaches_the_minting_log_line():
    """CodeQL taints per-DICT, not per-key — and it is right to: the two halves of this
    credential differ only by which key they are under. So the account name takes its
    run-as user from config rather than off `cred`, and the auth is a branch literal."""
    src = _src("web_dashboard/services/cert_ps_service.py")
    block = src.split("async def ensure_functional_account(")[1].split("\nasync def ")[0]
    call = block.split("logger.info(")[1].split("\n    return")[0]
    assert "cred[" not in call, "a read of the credential dict reaches a log line"
    assert "auth_label" in call
    name = [ln for ln in block.splitlines() if ln.strip().startswith("account_name = ")]
    assert name and "cred[" not in name[0], \
        "the account name is logged, so it must not be built out of the credential dict"


def test_the_rest_call_sends_both_api_fields_or_neither():
    """A consumer treats half the pair as absent and falls back, so one alone produces an
    account that looks configured and authenticates as nothing."""
    src = _src("web_dashboard/services/ps_api_service.py")
    block = src.split("async def create_functional_account_on_platform(")[1] \
               .split("\nasync def ")[0]
    assert "if api_key and api_secret:" in block
    assert '"ApiKey"' in block and '"ApiSecret"' in block


def test_a_rewire_that_switches_path_reports_the_account_it_left_behind():
    """The two shapes differ in the account NAME, so Password Safe sees a new object
    rather than a duplicate and the id teardown deletes by moves to it. Unsaid, the first
    account stays behind holding a live enrollment credential."""
    src = _src("web_dashboard/services/cert_lab_service.py")
    block = src.split("async def rewire_functional_account(")[1].split("\ndef ")[0]
    assert "had_id" in block and 'str(fa.get("id")' in block
    assert "delete_functional_account" not in block, \
        "a managed system may still reference it; deleting it under one breaks it"
    assert "r.replaced" in _src(
        "web_dashboard/templates/workload_lab/_certificates.html"), \
        "an orphaned account the operator is never told about is the whole failure mode"


def test_create_is_the_default_in_both_places_that_declare_it():
    conf = _src("web_dashboard/config.py")
    setup = _src("web_dashboard/api/setup.py")
    assert 'cert_ps_functional_account_mode: str = "create"' in conf
    assert 'cert_ps_functional_account_mode: str = "create"' in setup


def test_the_mode_select_keeps_its_x_init_default():
    """An unset string key reads back "" from /api/setup/feature, not the model default,
    so a select without x-init renders blank and saves "" over the mode."""
    panel = _src("web_dashboard/templates/settings.html")
    at = panel.index('x-model="panelCfg.cert_ps_functional_account_mode"')
    assert "x-init" in panel[at:at + 300]


if __name__ == "__main__":
    fns = {k: v for k, v in sorted(globals().items()) if k.startswith("test_")}
    failures = 0
    for name, fn in fns.items():
        try:
            fn()
            print(f"ok   {name}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL {name}: {exc}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
