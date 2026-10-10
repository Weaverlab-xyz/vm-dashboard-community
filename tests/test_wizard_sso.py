"""The setup wizard offers generic OIDC single sign-on, the default SSO path.

Before this, the wizard's only sign-in option was the legacy Sign in with Microsoft panel on
the Azure step, so an operator who wanted Okta, Keycloak or Entra-through-OIDC had to finish
setup and then find the Settings panel. Four properties, each of which fails quietly:

  * **It writes the keys SSO actually reads**, the ones ``oidc_service.is_configured()``
    checks, and on either install profile: signing in is not a demo-only concern.
  * **Absent means leave it alone.** ``sso: SsoSetup | None = None``, as for ``profile`` and
    ``persona``. A reconfigure that omits the block must never switch off a working login.
  * **A blank secret keeps the stored one**, as for every other wizard secret, and the page
    never reads the secret back into the form.
  * **The page sends the block only while its panel is open**, so a reconfigure whose
    config load failed cannot post an empty issuer.

Runs under pytest, or standalone:
    python tests/test_wizard_sso.py
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-wizard-sso")

_SETUP_HTML = os.path.join(_ROOT, "web_dashboard", "templates", "setup.html")
_KEYS = ("oidc_issuer", "oidc_client_id", "oidc_client_secret", "oidc_provider_name")


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _ensure_schema():
    from web_dashboard import database as d
    d.Base.metadata.create_all(bind=d.engine)


def _payload(sso=None, profile="pov"):
    """A minimal wizard body. POV by default, which also proves SSO is written on a
    profile that skips every cloud write."""
    from web_dashboard.api import setup as s
    return s.SetupPayload(
        admin=s.AdminSetup(username="admin", password=""),
        profile=s.ProfileSetup(install_profile=profile),
        aws=s.AWSSetup(), azure=s.AzureSetup(), features=s.FeaturesSetup(),
        sso=None if sso is None else s.SsoSetup(**sso))


def _clear():
    from web_dashboard.services import config_service
    for k in _KEYS + ("install_profile",):
        config_service.delete(k)
    config_service.invalidate()


def _get(key):
    from web_dashboard.services import config_service
    config_service.invalidate()
    return config_service.get(key) or ""


# ── what the API writes ──────────────────────────────────────────────────────

def test_the_wizard_writes_the_keys_oidc_reads_on_a_pov_instance_too():
    _ensure_schema()
    from web_dashboard.api.setup import _apply_config
    from web_dashboard.services import oidc_service
    _clear()
    try:
        _apply_config(_payload({"oidc_issuer": " https://example.okta.com ",
                                "oidc_client_id": "abc", "oidc_client_secret": "s3cret",
                                "oidc_provider_name": "Okta"}))
        assert _get("oidc_issuer") == "https://example.okta.com", "issuer not stored, or not trimmed"
        assert _get("oidc_client_id") == "abc"
        assert _get("oidc_client_secret") == "s3cret"
        assert _get("oidc_provider_name") == "Okta"
        assert oidc_service.is_configured(), "the login page would still show no SSO button"
    finally:
        _clear()


def test_a_reconfigure_that_omits_the_block_leaves_sso_alone():
    _ensure_schema()
    from web_dashboard.api.setup import _apply_config
    _clear()
    try:
        _apply_config(_payload({"oidc_issuer": "https://idp", "oidc_client_id": "abc",
                                "oidc_client_secret": "s3cret"}))
        _apply_config(_payload(None))
        assert _get("oidc_issuer") == "https://idp", "omitting the block cleared the issuer"
        assert _get("oidc_client_secret") == "s3cret"
    finally:
        _clear()


def test_a_blank_secret_keeps_the_stored_one_and_other_fields_still_change():
    _ensure_schema()
    from web_dashboard.api.setup import _apply_config
    _clear()
    try:
        _apply_config(_payload({"oidc_issuer": "https://idp", "oidc_client_id": "abc",
                                "oidc_client_secret": "s3cret"}))
        _apply_config(_payload({"oidc_issuer": "https://idp2", "oidc_client_id": "abc",
                                "oidc_client_secret": ""}))
        assert _get("oidc_client_secret") == "s3cret", "a blank secret erased the stored one"
        assert _get("oidc_issuer") == "https://idp2"
    finally:
        _clear()


def test_clearing_the_issuer_in_an_open_panel_turns_sso_off():
    """Sending the block with an empty issuer is a deliberate act, and must work."""
    _ensure_schema()
    from web_dashboard.api.setup import _apply_config
    from web_dashboard.services import oidc_service
    _clear()
    try:
        _apply_config(_payload({"oidc_issuer": "https://idp", "oidc_client_id": "abc"}))
        _apply_config(_payload({"oidc_issuer": "", "oidc_client_id": "abc"}))
        assert not oidc_service.is_configured()
    finally:
        _clear()


# ── what the page does ───────────────────────────────────────────────────────

def test_the_panel_is_on_the_admin_step_which_both_profiles_see():
    src = _read(_SETUP_HTML)
    admin = src.split("stepKey === 'admin'", 1)[1].split("stepKey === 'profile'", 1)[0]
    for key in _KEYS:
        assert f"form.sso.{key}" in admin, f"the Admin step has no {key} field"
    steps = src.split("allSteps: [", 1)[1].split("\n    ],", 1)[0]
    line = [ln for ln in steps.split("\n") if "'admin'" in ln][0]
    assert "profiles:" not in line, "the Admin step is no longer shown on both profiles"


def test_the_page_sends_the_block_only_while_the_panel_is_open():
    src = _read(_SETUP_HTML)
    body = src.split("const body = {", 1)[1].split("\n        };", 1)[0]
    assert "this.showSso ? { sso: this.form.sso }" in body, (
        "submit() sends the SSO block unconditionally; a reconfigure whose config load "
        "failed would post an empty issuer and switch SSO off")


def test_the_secret_is_never_read_back_into_the_form():
    src = _read(_SETUP_HTML)
    init = src.split("async init()", 1)[1].split("\n    },", 1)[0]
    assert "form.sso.oidc_issuer" in init, "a reconfigure does not load the stored issuer"
    assert "form.sso.oidc_client_secret" not in init, \
        "the stored client secret is copied into the wizard form"


def test_the_microsoft_panel_is_marked_legacy_and_points_at_oidc():
    src = _read(_SETUP_HTML)
    assert "Sign in with Microsoft (legacy)" in src
    azure = src.split("stepKey === 'azure'", 1)[1].split("stepKey === 'gcp'", 1)[0]
    assert "Single sign-on (OIDC)" in azure, \
        "the legacy panel does not send people to the OIDC panel"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
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
