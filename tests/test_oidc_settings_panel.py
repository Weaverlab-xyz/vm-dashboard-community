"""Wiring tests for the OIDC Settings panel.

The panel is driven by the generic feature-config machinery in api/setup.py, so
what's worth pinning is the wiring rather than the form markup:

  * the feature is registered, so ``/api/setup/feature/oidc`` exists at all;
  * its fields match the keys ``oidc_service`` actually reads — a drift here
    produces a panel that saves values nothing consumes;
  * the client secret is redacted on read and not clobbered on write;
  * it is config-only, so saving can't write a stray ``oidc_enabled`` flag that
    would disagree with ``is_configured()``.

Skips cleanly without fastapi. Standalone:  python tests/test_oidc_settings_panel.py
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

os.environ.setdefault("JWT_SECRET_KEY", "test-secret-oidc-panel")

try:
    from web_dashboard.api import setup as setup_api
except Exception as exc:  # fastapi absent outside CI
    setup_api = None
    _IMPORT_ERR = exc


def test_oidc_feature_is_registered():
    assert "oidc" in setup_api._FEATURE_MODELS, \
        "/api/setup/feature/oidc would 404 without a registry entry"


def test_oidc_is_config_only():
    # SSO is live once an issuer + client id exist (oidc_service.is_configured).
    # An enable flag would be a second source of truth that could disagree.
    assert "oidc" in setup_api._CONFIG_ONLY_FEATURES


def test_panel_fields_match_what_the_service_reads():
    """Guard against drift between the panel and services/oidc_service.py."""
    model_fields = set(setup_api.OidcFeatureConfig.model_fields) - {"enabled"}
    # Every key the service looks up via _cfg(...)
    expected = {
        "oidc_issuer", "oidc_client_id", "oidc_client_secret",
        "oidc_provider_name", "oidc_scopes", "oidc_groups_claim",
    }
    assert model_fields == expected, (
        f"panel fields drifted from the service's config keys: "
        f"only in panel={model_fields - expected}, missing={expected - model_fields}"
    )


def test_client_secret_is_redacted_on_read():
    assert "oidc_client_secret" in setup_api._SECRET_FEATURE_KEYS, \
        "the client secret would be returned in plaintext to the browser"


def test_saving_a_masked_secret_does_not_overwrite_it():
    """The panel round-trips the redaction bullets; writing them back would
    replace a working secret with literal dots."""
    written = {}

    class _FakeConfig:
        @staticmethod
        def set_many(pairs):
            written.update(pairs)

    # _write_feature does `from ..services import config_service`, which resolves
    # the attribute on the package object — so swap it there, not in sys.modules.
    import web_dashboard.services as svc_pkg
    real = getattr(svc_pkg, "config_service", None)
    svc_pkg.config_service = _FakeConfig
    try:
        setup_api._write_feature("oidc", {
            "oidc_issuer": "https://idp.example.com",
            "oidc_client_secret": "••••••••",
        })
    finally:
        if real is not None:
            svc_pkg.config_service = real

    assert written.get("oidc_issuer") == "https://idp.example.com"
    assert "oidc_client_secret" not in written, "masked placeholder must not be persisted"
    assert "oidc_enabled" not in written, "config-only feature must not write an enable flag"


def test_probe_endpoint_is_exposed():
    paths = {r.path for r in setup_api.router.routes}
    assert "/api/setup/oidc/test" in paths


if __name__ == "__main__":
    if setup_api is None:
        print(f"SKIP all: fastapi unavailable ({_IMPORT_ERR})")
        sys.exit(0)
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
    sys.exit(1 if failures else 0)
