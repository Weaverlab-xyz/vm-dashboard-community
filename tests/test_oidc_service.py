"""Unit tests for the generic OIDC login helper.

Covers the parts that don't need a live IdP: configuration gating, discovery
caching and error messages, scope handling, and the claim-shape differences
between providers (which is where a generic implementation actually earns its
keep — Keycloak, Okta and Entra all disagree about where the email and groups
live).

Runs under pytest, or standalone:  python tests/test_oidc_service.py
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

try:
    from web_dashboard.services import oidc_service as oidc
except Exception as exc:  # httpx / jose absent
    oidc = None
    _IMPORT_ERR = exc

_CONF = {}


def _install_fake_config():
    """Point the module's config lookup at an in-memory dict."""
    import types
    fake = types.SimpleNamespace(get=lambda key: _CONF.get(key, ""))
    sys.modules["web_dashboard.services"].config_service = fake  # type: ignore[attr-defined]
    return fake


def _reset(**cfg):
    _CONF.clear()
    _CONF.update(cfg)
    oidc.clear_cache()


def test_is_configured_requires_issuer_and_client_id():
    _reset()
    assert oidc.is_configured() is False
    _reset(oidc_issuer="https://idp.example.com")
    assert oidc.is_configured() is False, "issuer alone must not count as configured"
    _reset(oidc_issuer="https://idp.example.com", oidc_client_id="abc")
    assert oidc.is_configured() is True


def test_provider_label_falls_back_to_issuer_host():
    _reset(oidc_issuer="https://keycloak.example.com/realms/main", oidc_client_id="x")
    assert oidc.provider_label() == "keycloak.example.com"
    _reset(oidc_issuer="https://keycloak.example.com/realms/main", oidc_client_id="x",
           oidc_provider_name="Corp SSO")
    assert oidc.provider_label() == "Corp SSO"


def test_scopes_always_include_openid():
    # Without openid the provider returns no id_token and the flow silently
    # degrades to plain OAuth2 — force it in regardless of what's configured.
    _reset(oidc_scopes="profile email")
    assert oidc.scopes().split()[0] == "openid"
    _reset(oidc_scopes="openid email")
    assert oidc.scopes() == "openid email"
    _reset()
    assert "openid" in oidc.scopes()


def test_discovery_without_issuer_raises():
    _reset()
    try:
        oidc.discovery()
    except oidc.OIDCError as e:
        assert "issuer" in str(e).lower()
    else:
        raise AssertionError("expected OIDCError when no issuer is configured")


def test_discovery_error_names_the_url():
    """A typo'd issuer is the most common misconfiguration; the error has to say
    which URL failed rather than surfacing a bare connection error."""
    _reset(oidc_issuer="https://nonexistent.invalid", oidc_client_id="x")
    oidc.httpx = _Boom()  # type: ignore[attr-defined]
    try:
        oidc.discovery()
    except oidc.OIDCError as e:
        assert ".well-known/openid-configuration" in str(e)
    else:
        raise AssertionError("expected OIDCError")
    finally:
        import httpx as _real
        oidc.httpx = _real  # type: ignore[attr-defined]


class _Boom:
    @staticmethod
    def get(*a, **k):
        raise RuntimeError("no route to host")


def test_discovery_is_cached():
    _reset(oidc_issuer="https://idp.example.com", oidc_client_id="x")
    calls = {"n": 0}
    doc = {"authorization_endpoint": "https://idp.example.com/auth",
           "token_endpoint": "https://idp.example.com/token",
           "jwks_uri": "https://idp.example.com/jwks",
           "issuer": "https://idp.example.com"}

    def fake_fetch(url):
        calls["n"] += 1
        return doc

    orig, oidc._fetch = oidc._fetch, fake_fetch
    try:
        assert oidc.discovery()["token_endpoint"].endswith("/token")
        oidc.discovery()
        assert calls["n"] == 1, "discovery should be cached, not re-fetched per login"
    finally:
        oidc._fetch = orig


def test_discovery_rejects_incomplete_document():
    _reset(oidc_issuer="https://idp.example.com", oidc_client_id="x")
    orig, oidc._fetch = oidc._fetch, lambda url: {"authorization_endpoint": "a"}
    try:
        oidc.discovery()
    except oidc.OIDCError as e:
        assert "token_endpoint" in str(e)
    else:
        raise AssertionError("expected OIDCError for a discovery doc missing endpoints")
    finally:
        oidc._fetch = orig


def test_authorization_url_carries_pkce_and_state():
    _reset(oidc_issuer="https://idp.example.com", oidc_client_id="client-123")
    orig = oidc._fetch
    oidc._fetch = lambda url: {"authorization_endpoint": "https://idp.example.com/auth",
                               "token_endpoint": "t", "jwks_uri": "j",
                               "issuer": "https://idp.example.com"}
    try:
        url = oidc.authorization_url("https://dash.example.com/cb", "st8", "chal")
    finally:
        oidc._fetch = orig
    for expected in ("client_id=client-123", "state=st8", "code_challenge=chal",
                     "code_challenge_method=S256", "response_type=code"):
        assert expected in url, f"missing {expected} in {url}"


def test_claim_identity_handles_provider_differences():
    # Keycloak / Authentik: email + groups
    _reset()
    sub, email, name, groups = oidc.claim_identity(
        {"sub": "u1", "email": "a@x.io", "name": "A", "groups": ["admins", "devs"]})
    assert (sub, email, name, groups) == ("u1", "a@x.io", "A", ["admins", "devs"])

    # Entra: no email claim, upn/preferred_username instead
    _, email, _, _ = oidc.claim_identity({"sub": "u2", "preferred_username": "b@x.io"})
    assert email == "b@x.io"
    _, email, _, _ = oidc.claim_identity({"sub": "u3", "upn": "c@x.io"})
    assert email == "c@x.io"

    # Some IdPs emit a comma-separated string rather than a list
    _reset(oidc_groups_claim="roles")
    _, _, _, groups = oidc.claim_identity({"sub": "u4", "roles": "a, b ,c"})
    assert groups == ["a", "b", "c"]

    # Missing everything must not raise — the caller redirects on a blank email
    sub, email, name, groups = oidc.claim_identity({})
    assert (sub, email, name, groups) == ("", "", "", [])


def test_group_ids_are_stringified():
    """Entra emits group OIDs as strings, but some IdPs use numeric ids; the
    workgroup map is keyed by string, so normalise."""
    _reset()
    _, _, _, groups = oidc.claim_identity({"sub": "u", "groups": [1, 2]})
    assert groups == ["1", "2"]


if __name__ == "__main__":
    if oidc is None:
        print(f"SKIP all: oidc_service unavailable ({_IMPORT_ERR})")
        sys.exit(0)
    _install_fake_config()
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
