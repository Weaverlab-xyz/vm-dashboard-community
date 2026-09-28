"""Wiring tests for the SPIRE Lab Settings panel.

The panel is driven by the generic feature-config machinery in api/setup.py, so what is
worth pinning is the wiring rather than the markup:

  * its fields are exactly the keys ``spire_lab_service`` reads -- a drift here is a panel
    that saves values nothing consumes, or a setting only reachable as raw config (which
    is how the Helm pins, the k3s version and the unpinned-download opt-out started out);
  * every field is in the template and has a default in ``config.settings``;
  * the unpinned-download opt-out round-trips as a real boolean and defaults OFF.

Skips cleanly without fastapi. Standalone:  python tests/test_spire_settings_panel.py
"""
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

os.environ.setdefault("JWT_SECRET_KEY", "test-secret-spire-panel")

try:
    from web_dashboard.api import setup as setup_api
except Exception as exc:  # fastapi absent outside CI
    setup_api = None
    _IMPORT_ERR = exc


def _service_keys():
    with open(os.path.join(_ROOT, "web_dashboard", "services", "spire_lab_service.py"),
              encoding="utf-8") as fh:
        return set(re.findall(r'_cfg\("(spire_lab_[a-z0-9_]+)"', fh.read()))


def _panel_fields():
    return set(setup_api.SpireLabFeatureConfig.model_fields) - {"enabled"}


def test_the_panel_is_registered_and_config_only():
    assert setup_api._FEATURE_MODELS.get("spire_lab") is setup_api.SpireLabFeatureConfig
    assert "spire_lab" in setup_api._CONFIG_ONLY_FEATURES, (
        "the Preview toggle owns spire_lab_enabled; a panel `enabled` would be a second one")


def test_panel_fields_match_what_the_service_reads():
    """Read from the service's ``_cfg("...")`` calls rather than listed here, so a key the
    service starts reading without a panel field (or the reverse) fails this test."""
    expected = _service_keys()
    assert "spire_lab_version" in expected and "spire_lab_allow_unpinned" in expected, (
        "the _cfg scan found nothing -- the service no longer reads config that way")
    fields = _panel_fields()
    assert fields == expected, (
        f"panel fields drifted from the service's config keys: "
        f"only in panel={fields - expected}, missing={expected - fields}")


def test_every_field_is_on_the_page_and_has_a_default():
    from web_dashboard.config import settings
    with open(os.path.join(_ROOT, "web_dashboard", "templates", "settings.html"),
              encoding="utf-8") as fh:
        page = fh.read()
    for field in _panel_fields():
        assert f"panelCfg.{field}" in page, f"{field} has no input on the Settings page"
        assert hasattr(settings, field), f"{field} has no default in config.settings"


def test_the_unpinned_opt_out_is_a_boolean_that_defaults_off():
    from web_dashboard.config import settings
    info = setup_api.SpireLabFeatureConfig.model_fields["spire_lab_allow_unpinned"]
    assert info.annotation is bool, (
        "_read_feature round-trips bools only when annotated bool; a str would render "
        "the checkbox wrong")
    assert info.default is False and settings.spire_lab_allow_unpinned is False


def test_the_service_reads_the_saved_opt_out_as_true():
    """config_service may hand back a real bool or its string form; both must enable it,
    and every falsy form must not."""
    from web_dashboard.services import spire_lab_service as svc
    orig = svc._cfg
    try:
        for stored, want in ((True, True), ("True", True), ("true", True), ("1", True),
                             (False, False), ("False", False), ("", False)):
            svc._cfg = (lambda v: lambda key, default="": (
                str(v) if key == "spire_lab_allow_unpinned" and v != "" else default))(stored)
            assert svc._pin_vars()["spire_allow_unpinned"] is want, stored
    finally:
        svc._cfg = orig


def test_blank_chart_versions_fall_back_to_the_pins_not_latest():
    from web_dashboard.services import spire_lab_service as svc
    orig = svc._cfg
    try:
        svc._cfg = lambda key, default="": default
        row = svc.SpireLab(trust_domain="panel.test", name="lab", deployment_mode="k8s")
        v = svc._helm_vars(row)
        assert v["spire_chart_version"] == svc.SPIRE_CHART_VERSION
        assert v["spire_crds_chart_version"] == svc.SPIRE_CRDS_CHART_VERSION
    finally:
        svc._cfg = orig


if __name__ == "__main__":
    if setup_api is None:
        print(f"SKIP: {_IMPORT_ERR}")
        sys.exit(0)
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {type(e).__name__}: {e}")
    sys.exit(1 if failures else 0)
