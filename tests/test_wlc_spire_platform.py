"""Workload Credentials authenticating with the dashboard's own SPIFFE identity.

What these pin:

- ``spire`` is a workload-identity platform whose token is a file: the ``wlc.jwt``
  that dashboard_identity keeps for the Workload Credentials audience, in the same
  token directory, with no override from a leftover ``wlc_identity_token_file``;
- the constants copied into the stdlib-only service match dashboard_identity's own;
- what can be missing is named: the audience, the identity, its WLC audience;
- a missing file says how to make the dashboard write it;
- the panel offers the platform and shows the audience field for it.

Run: python tests/test_wlc_spire_platform.py   (or under pytest)
"""
import contextlib
import os
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="wlc-spire-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDB}"
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-wlc-spire")

from web_dashboard.services import dashboard_identity  # noqa: E402
from web_dashboard.services import workload_credentials_service as wlc  # noqa: E402

ON = {"wlc_auth_mode": "workload", "wlc_site_id": "SITE", "wlc_service_name": "svc",
      "wlc_identity_platform": "spire", "wlc_identity_audience": "beyondtrust-wlc",
      "dashboard_spiffe_identity_enabled": "1", "dashboard_spiffe_aud_wlc": "1"}


@contextlib.contextmanager
def _config(**values):
    original = wlc._cfg
    wlc._cfg = lambda key, fallback="": values.get(key, fallback)
    try:
        yield
    finally:
        wlc._cfg = original


def test_spire_is_a_file_platform():
    assert wlc.PLATFORM_SPIRE in wlc.VALID_IDENTITY_PLATFORMS
    assert wlc.PLATFORM_SPIRE in wlc.FILE_PLATFORMS
    with _config(wlc_identity_platform="spire"):
        assert wlc.identity_platform() == "spire"


def test_the_copied_constants_are_dashboard_identitys_own():
    assert wlc.SPIRE_TOKEN_DIR == dashboard_identity.DEFAULT_TOKEN_DIR
    assert wlc.SPIRE_TOKEN_NAME == "wlc.jwt" and "wlc" in dashboard_identity.TOKEN_NAMES
    assert wlc.SPIRE_SETTINGS == (dashboard_identity.ENABLED, dashboard_identity.AUD_WLC)


def test_the_token_is_the_dashboards_wlc_file_and_cannot_be_redirected():
    with _config(wlc_identity_token_file="/var/run/secrets/kubernetes.io/serviceaccount/token"):
        assert wlc.identity_token_path("spire", env={}) == "/run/spiffe-tokens/wlc.jwt"
        assert wlc.identity_token_path("spire", env={"SPIFFE_TOKEN_DIR": "/t"}) == "/t/wlc.jwt"


def test_the_token_file_is_read_fresh_every_call():
    d = tempfile.mkdtemp(prefix="wlc-spire-tok-")
    path = os.path.join(d, "wlc.jwt")
    os.environ["SPIFFE_TOKEN_DIR"] = d
    try:
        with _config(**ON):
            open(path, "w", encoding="utf-8").write("a.b.c\n")
            assert wlc._workload_token() == "a.b.c"
            open(path, "w", encoding="utf-8").write("d.e.f")
            assert wlc._workload_token() == "d.e.f"
            assert wlc._auth_headers() == {"Authorization": "Bearer d.e.f",
                                           "X-BT-Service-Name": "svc"}
    finally:
        del os.environ["SPIFFE_TOKEN_DIR"]


def test_a_complete_configuration_is_missing_nothing():
    with _config(**ON):
        assert wlc.missing_settings() == []


def test_what_is_missing_is_named():
    with _config(**dict(ON, wlc_identity_audience="", dashboard_spiffe_aud_wlc="0",
                        dashboard_spiffe_identity_enabled="")):
        assert wlc.missing_settings() == ["wlc_identity_audience",
                                          "dashboard_spiffe_identity_enabled",
                                          "dashboard_spiffe_aud_wlc"]


def test_a_missing_file_says_how_to_get_it_written():
    os.environ["SPIFFE_TOKEN_DIR"] = tempfile.mkdtemp(prefix="wlc-spire-none-")
    try:
        with _config(**ON):
            try:
                wlc._workload_token()
                raise AssertionError("expected WorkloadCredentialsError")
            except wlc.WorkloadCredentialsError as exc:
                assert "Give the dashboard its own SPIFFE identity" in str(exc)
                assert "wlc.jwt" in str(exc)
    finally:
        del os.environ["SPIFFE_TOKEN_DIR"]


def test_the_panel_offers_it_and_asks_for_its_audience():
    html = open(os.path.join(_ROOT, "web_dashboard", "templates", "settings.html"),
                encoding="utf-8").read()
    assert '<option value="spire">' in html
    # The audience field is shown on every platform but the projected-file ones.
    assert "x-show=\"!['aws', 'file'].includes(panelCfg.wlc_identity_platform)\"" in html
    assert "panelCfg.wlc_identity_platform === 'spire'" in html
    assert "/docs/oidc/dashboard-identity#beyondtrust-workload-credentials" in html


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
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
