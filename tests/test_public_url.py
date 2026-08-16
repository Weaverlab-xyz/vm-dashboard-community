"""The dashboard's own public origin, and the proxy-trust coupling it breaks.

`trusted_proxy_hosts` used to default to `"*"`, which let any client that could reach
the socket declare its own source address — and `get_remote_address`, which the login
throttle's per-address cap keys off, believed it. Pinning it to loopback is the fix.

The reason that fix was not free is the subject of this file. Three call sites derived
their absolute URL from `request.url.scheme`, which is only `https` behind a proxy
because `ProxyHeadersMiddleware` rewrote it from `X-Forwarded-Proto` — and only for
trusted peers. So tightening proxy trust would silently turn every OAuth redirect URI
into `http://`, which the identity provider rejects for a redirect-URI mismatch, with
nothing in the logs connecting cause to effect.

`public_base_url` decouples the two. These assertions pin that decoupling, the URL
normalisation (operators paste what is in the address bar), and the warning that makes
a mis-pinned proxy diagnosable instead of mysterious.

Runs under pytest, or standalone:
    python tests/test_public_url.py
"""
import importlib.util
import os
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PATH = os.path.join(_ROOT, "web_dashboard", "services", "public_url.py")

# Stub the two things public_url imports lazily, so the module is testable with no
# database and no app config — mirrors test_setup_feature_roundtrip's approach.
CONF = {}


def _install_stubs():
    pkg = types.ModuleType("web_dashboard")
    pkg.__path__ = []
    sys.modules.setdefault("web_dashboard", pkg)

    services = types.ModuleType("web_dashboard.services")
    services.__path__ = []
    sys.modules["web_dashboard.services"] = services

    cfg = types.ModuleType("web_dashboard.services.config_service")
    cfg.get = lambda key, default="", workgroup=None: CONF.get(key, default)
    sys.modules["web_dashboard.services.config_service"] = cfg
    services.config_service = cfg

    conf_mod = types.ModuleType("web_dashboard.config")
    conf_mod.settings = types.SimpleNamespace(public_base_url="")
    sys.modules["web_dashboard.config"] = conf_mod
    pkg.config = conf_mod


_install_stubs()
_spec = importlib.util.spec_from_file_location(
    "web_dashboard.services.public_url", _PATH)
pu = importlib.util.module_from_spec(_spec)
sys.modules["web_dashboard.services.public_url"] = pu
_spec.loader.exec_module(pu)


class _Req:
    """Just enough of a Starlette request for these helpers."""

    def __init__(self, scheme="http", netloc="localhost:8001"):
        self.url = types.SimpleNamespace(scheme=scheme, netloc=netloc)


# ── normalisation ─────────────────────────────────────────────────────────────

def test_a_trailing_slash_is_dropped():
    """Otherwise the callback becomes `https://host//api/...`, which does not match the
    URI registered with the identity provider."""
    assert pu.normalize("https://d.example.com/") == "https://d.example.com"


def test_a_pasted_path_is_stripped_back_to_the_origin():
    """Operators paste what is in the address bar. Concatenating a callback path onto
    `https://host/login` produces a redirect URI that fails to match, with no clue why."""
    for value in ("https://d.example.com/login",
                  "https://d.example.com/login#token=abc",
                  "https://d.example.com/setup?step=2"):
        assert pu.normalize(value) == "https://d.example.com", value


def test_a_port_survives():
    assert pu.normalize("https://d.example.com:8443/") == "https://d.example.com:8443"


def test_a_bare_hostname_is_assumed_https():
    """Guessing http:// for a value the operator meant to be public would be the wrong
    guess, and would produce a callback the IdP rejects."""
    assert pu.normalize("d.example.com") == "https://d.example.com"


def test_the_scheme_is_lowercased():
    assert pu.normalize("HTTPS://D.example.com") == "https://D.example.com"


def test_empty_stays_empty():
    for value in ("", "   ", None):
        assert pu.normalize(value) == ""


# ── resolution order ──────────────────────────────────────────────────────────

def test_the_configured_origin_wins_over_the_request():
    """THE assertion. Behind a proxy the request-derived scheme is only as trustworthy
    as trusted_proxy_hosts; the configured value is the operator's statement of fact."""
    CONF.clear()
    CONF["public_base_url"] = "https://dash.example.com"
    try:
        assert pu.resolve(_Req("http", "10.0.0.5:8000")) == "https://dash.example.com"
    finally:
        CONF.clear()


def test_the_request_is_used_when_nothing_is_configured():
    """A laptop install reached over localhost, an IP and a hostname on different days
    should keep working with no configuration at all."""
    CONF.clear()
    assert pu.resolve(_Req("http", "localhost:8001")) == "http://localhost:8001"


def test_a_configured_origin_works_with_no_request_at_all():
    """Background code — a notification deep link, a worker — has no request to derive
    from."""
    CONF.clear()
    CONF["public_base_url"] = "https://dash.example.com"
    try:
        assert pu.resolve() == "https://dash.example.com"
    finally:
        CONF.clear()


def test_the_fallback_is_used_when_there_is_neither():
    CONF.clear()
    assert pu.resolve(fallback="http://fallback:1") == "http://fallback:1"
    assert pu.resolve() == ""


def test_a_configured_value_is_normalised_on_the_way_out():
    CONF.clear()
    CONF["public_base_url"] = "  https://dash.example.com/login/  "
    try:
        assert pu.configured() == "https://dash.example.com"
    finally:
        CONF.clear()


# ── joining ───────────────────────────────────────────────────────────────────

def test_join_builds_the_callback_url():
    CONF.clear()
    CONF["public_base_url"] = "https://dash.example.com"
    try:
        assert (pu.join("/api/auth/oauth/azure/callback")
                == "https://dash.example.com/api/auth/oauth/azure/callback")
    finally:
        CONF.clear()


def test_join_does_not_double_the_separator():
    CONF.clear()
    CONF["public_base_url"] = "https://dash.example.com/"
    try:
        assert pu.join("api/auth/x") == "https://dash.example.com/api/auth/x"
        assert pu.join("/api/auth/x") == "https://dash.example.com/api/auth/x"
    finally:
        CONF.clear()


def test_join_returns_empty_when_the_origin_is_unknown():
    """So a caller can fall back rather than emitting a relative string that looks
    absolute."""
    CONF.clear()
    assert pu.join("/api/auth/x") == ""


# ── https detection ───────────────────────────────────────────────────────────

def test_is_https_prefers_the_configured_origin():
    """Behind an untrusted proxy the request says http even when the world sees https;
    the configured value is the one that reflects reality."""
    CONF.clear()
    CONF["public_base_url"] = "https://dash.example.com"
    try:
        assert pu.is_https(_Req("http", "10.0.0.5")) is True
    finally:
        CONF.clear()


def test_is_https_falls_back_to_the_request_and_reports_unknown():
    CONF.clear()
    assert pu.is_https(_Req("https", "d")) is True
    assert pu.is_https(_Req("http", "d")) is False
    assert pu.is_https() is None


# ── the callers ───────────────────────────────────────────────────────────────

def _source(path):
    with open(os.path.join(_ROOT, path), encoding="utf-8") as fh:
        return fh.read()


def test_no_caller_still_derives_its_own_absolute_url():
    """The whole point is that these three stopped hand-rolling
    `f"{request.url.scheme}://{request.url.netloc}"`. A new one would silently
    reintroduce the proxy coupling this module exists to remove."""
    hand_rolled = 'f"{request.url.scheme}://{request.url.netloc}'
    for path in ("web_dashboard/api/agent.py",):
        assert hand_rolled not in _source(path), f"{path} still derives its own origin"

    auth = _source("web_dashboard/api/auth.py")
    # auth.py keeps one derivation as an explicit last-resort fallback inside
    # _oidc_redirect_uri; there must be no more than that.
    assert auth.count(hand_rolled) <= 1, (
        "auth.py should resolve through public_url, keeping at most one inline fallback")
    assert "public_url.join" in auth


def test_the_default_proxy_trust_is_not_a_wildcard():
    """The setting this whole change exists for. A wildcard means any client can
    declare its own source address and walk past the login throttle's per-address cap
    by rotating one header per request."""
    config_src = _source("web_dashboard/config.py")
    import re
    match = re.search(r'trusted_proxy_hosts:\s*str\s*=\s*"([^"]*)"', config_src)
    assert match, "trusted_proxy_hosts setting not found"
    assert match.group(1) != "*", "trusted_proxy_hosts must not default to a wildcard"
    assert match.group(1).strip(), "it must still default to something"


def test_an_untrusted_forwarded_header_is_warned_about():
    """Pinning proxy trust has an invisible failure mode — headers ignored, scheme
    silently http, OAuth breaks with nothing linking cause to effect. The warning is
    what makes it diagnosable, so it must stay wired.

    The wiring lives in main.py; the wording lives in the auditor, which is where it
    can be tested without importing the application.
    """
    main = _source("web_dashboard/main.py")
    assert "warn_untrusted_forwarded_headers" in main, "the middleware must stay wired"
    assert "ForwardedHeaderAuditor" in main, "and must delegate to the auditor"

    auditor_src = _source("web_dashboard/services/public_url.py")
    assert "TRUSTED_PROXY_HOSTS" in auditor_src, "the warning must name the fix"
    assert "PUBLIC_BASE_URL" in auditor_src, "and the setting that fixes the OAuth half"


def test_the_warning_runs_outside_proxy_headers():
    """It reports the real transport peer, which is only visible before
    ProxyHeadersMiddleware rewrites scope['client']. Starlette makes the
    most-recently-added middleware outermost, so the warning must be added AFTER."""
    main = _source("web_dashboard/main.py")
    proxy_at = main.index("app.add_middleware(ProxyHeadersMiddleware")
    warn_at = main.index("async def warn_untrusted_forwarded_headers")
    assert proxy_at < warn_at, (
        "the warning middleware must be registered after ProxyHeadersMiddleware so it "
        "wraps it and still sees the original client address")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
