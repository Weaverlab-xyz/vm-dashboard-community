"""Route tests for the /docs vs /swagger split and the authenticated schema.

Pins three things that are easy to regress silently:
  * ``/openapi.json`` requires a token — it enumerates the whole API surface.
  * ``/docs`` is the documentation browser, not Swagger (they used to collide on
    that exact path).
  * The OpenAPI schema advertises **no password grant**, so the API explorer
    can't be used to exercise username/password auth.

Skips cleanly when fastapi isn't installed, like the other API tests here.
Runs under pytest, or standalone:  python tests/test_docs_swagger_routes.py
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

os.environ.setdefault("JWT_SECRET_KEY", "test-secret-docs-routes")

try:
    from fastapi.testclient import TestClient
    from web_dashboard.main import app, _custom_openapi
    from web_dashboard.services import config_service
except Exception as exc:  # fastapi / deps absent outside CI
    TestClient = None
    _IMPORT_ERR = exc


def _client():
    """Client with the lifespan run (creates tables) and the setup guard
    satisfied — otherwise every request 302s to /setup."""
    c = TestClient(app)
    c.__enter__()
    config_service.set("setup_complete", "1")
    config_service._setup_complete = True
    return c


def test_openapi_schema_requires_authentication():
    c = _client()
    assert c.get("/openapi.json").status_code in (401, 403)


def test_swagger_serves_the_explorer():
    c = _client()
    r = c.get("/swagger")
    assert r.status_code == 200 and "swagger-ui" in r.text


def test_docs_is_the_documentation_index_not_swagger():
    c = _client()
    r = c.get("/docs")
    assert r.status_code == 200
    assert "Documentation" in r.text
    assert "swagger-ui" not in r.text, "/docs must not serve the API explorer"


def test_doc_pages_still_render():
    c = _client()
    assert c.get("/docs/integrations/rancher").status_code == 200


def test_oidc_routes_fail_cleanly_when_unconfigured():
    c = _client()
    assert c.get("/api/auth/oauth/oidc/login", follow_redirects=False).status_code == 501
    r = c.get("/api/auth/oauth/oidc/callback?state=bogus&code=x", follow_redirects=False)
    assert r.status_code == 302 and "invalid_state" in r.headers.get("location", "")


def test_openapi_has_no_password_grant():
    schemes = (_custom_openapi().get("components", {}) or {}).get("securitySchemes", {}) or {}
    for defn in schemes.values():
        assert not (defn.get("type") == "oauth2" and "password" in (defn.get("flows") or {})),             "the API explorer must not offer a username/password grant"
    assert any(d.get("scheme") == "bearer" for d in schemes.values()),         "a bearer scheme should be documented so tokens can still be used"


if __name__ == "__main__":
    if TestClient is None:
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
