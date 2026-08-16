"""Proxy-header trust, and the OAuth redirect URI that used to depend on it.

`trusted_proxy_hosts` now defaults to loopback instead of `"*"`, because a wildcard
lets any client that can reach the socket declare its own source address — and
`get_remote_address`, which the login throttle's per-address cap keys off, believes it.

That change is not free, and this file is the proof of exactly what it costs and what
pays for it. `request.url.scheme` is only `https` behind a proxy because
`ProxyHeadersMiddleware` rewrote it from `X-Forwarded-Proto`, and only for trusted
peers. So on a proxied deployment that has not listed its proxy, every OAuth redirect
URI silently becomes `http://` — which the identity provider rejects for a
redirect-URI mismatch, with nothing in the logs connecting the two.

The matrix below is asserted end-to-end through the real middleware stack:

    public_base_url unset,  no proxy                -> http origin   (correct)
    public_base_url unset,  proxy NOT trusted       -> http origin   (THE REGRESSION)
    public_base_url unset,  proxy trusted           -> https origin  (correct)
    public_base_url set,    any of the above        -> https origin  (decoupled)

Skips if app deps are absent. Runs under pytest, or standalone:
    python tests/test_proxy_trust.py
"""
import os
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="proxy-trust-test-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDB}"
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-proxy-trust-tests")

try:
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
    from web_dashboard.database import Base, engine
    Base.metadata.create_all(bind=engine)
    from web_dashboard.api.auth import _build_redirect_uri, _oidc_redirect_uri
    from web_dashboard.services import config_service
except Exception as exc:  # pragma: no cover — app deps missing
    try:
        import pytest
        pytest.skip(f"app dependencies unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)


PROXY_IP = "172.29.7.2"          # stands in for the Caddy container
PROXY_HEADERS = {"X-Forwarded-Proto": "https",
                 "X-Forwarded-For": "203.0.113.7",
                 "Host": "dash.example.com"}


class _SetPeer:
    """ASGI shim that supplies a transport peer address.

    Starlette's TestClient leaves ``scope["client"]`` as None, and
    ProxyHeadersMiddleware compares that against its trusted set — so without this,
    NOTHING is ever trusted and every "trusted proxy" case silently tests the untrusted
    path instead. Worth knowing: a test written without it passes for the wrong reason.
    """

    def __init__(self, app, peer):
        self.app, self.peer = app, peer

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            scope = dict(scope)
            scope["client"] = self.peer
        await self.app(scope, receive, send)


def _client(trusted_hosts: str, peer: str = PROXY_IP) -> TestClient:
    app = FastAPI()

    @app.get("/probe")
    def probe(request: Request):          # noqa: ANN202
        return {"entra": _build_redirect_uri(request),
                "oidc": _oidc_redirect_uri(request)}

    app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=trusted_hosts)
    return TestClient(_SetPeer(app, (peer, 5555)))


def _set_public_base(value: str) -> None:
    config_service.set("public_base_url", value)
    config_service.invalidate()


def _entra(trusted: str, headers=None) -> str:
    return _client(trusted).get("/probe", headers=headers or {}).json()["entra"]


def _oidc(trusted: str, headers=None) -> str:
    return _client(trusted).get("/probe", headers=headers or {}).json()["oidc"]


# ── the harness itself ────────────────────────────────────────────────────────

def test_the_peer_shim_actually_makes_trust_work():
    """Guard the guard. Without _SetPeer the client address is None, nothing matches
    the trusted set, and every assertion below would pass while testing the wrong
    branch."""
    _set_public_base("")
    try:
        untrusted_shimmed = _entra("127.0.0.1", PROXY_HEADERS)
        trusted_shimmed = _entra(PROXY_IP, PROXY_HEADERS)
        assert untrusted_shimmed.startswith("http://")
        assert trusted_shimmed.startswith("https://"), (
            "the shim is not supplying a peer address; the 'trusted' cases below would "
            "be testing the untrusted path")
    finally:
        _set_public_base("")


# ── public_base_url unset: derivation, and its dependence on proxy trust ──────

def test_direct_access_derives_the_request_origin():
    _set_public_base("")
    assert _entra("127.0.0.1") == "http://testserver/api/auth/oauth/azure/callback"


def test_an_untrusted_proxy_yields_an_http_callback():
    """THE REGRESSION that pinning trusted_proxy_hosts introduces. Documented as an
    assertion rather than left to be discovered by a user whose OAuth stopped working:
    the proxy's headers are ignored, so the dashboard believes it is plain http."""
    _set_public_base("")
    assert _entra("127.0.0.1", PROXY_HEADERS).startswith("http://"), (
        "if this ever passes as https, the trust model changed and the warning "
        "middleware's rationale needs revisiting")


def test_a_trusted_proxy_yields_an_https_callback():
    """And the mechanism does work once the proxy is named — which is what the warning
    tells the operator to do."""
    _set_public_base("")
    assert _entra(PROXY_IP, PROXY_HEADERS) == \
        "https://dash.example.com/api/auth/oauth/azure/callback"


# ── public_base_url set: the coupling is gone ────────────────────────────────

def test_the_configured_origin_holds_in_every_proxy_configuration():
    """The payoff. Whatever the proxy situation, the callback is the one registered
    with the identity provider."""
    _set_public_base("https://dash.example.com")
    try:
        expected = "https://dash.example.com/api/auth/oauth/azure/callback"
        assert _entra("127.0.0.1") == expected                    # no proxy at all
        assert _entra("127.0.0.1", PROXY_HEADERS) == expected     # proxy not trusted
        assert _entra(PROXY_IP, PROXY_HEADERS) == expected        # proxy trusted
    finally:
        _set_public_base("")


def test_the_oidc_callback_resolves_the_same_way():
    """Two providers, one resolution path — they were separate functions deriving the
    origin independently, which is how one gets fixed and the other does not."""
    _set_public_base("https://dash.example.com")
    try:
        assert _oidc("127.0.0.1", PROXY_HEADERS) == \
            "https://dash.example.com/api/auth/oauth/oidc/callback"
    finally:
        _set_public_base("")


def test_a_sloppy_configured_value_still_produces_a_clean_callback():
    """Operators paste what is in the address bar. A trailing slash or a leftover path
    would otherwise produce a URI that fails to match the registered one."""
    _set_public_base("https://dash.example.com/login/")
    try:
        assert _entra("127.0.0.1") == \
            "https://dash.example.com/api/auth/oauth/azure/callback"
    finally:
        _set_public_base("")


# ── the diagnostic ────────────────────────────────────────────────────────────

# ── what pinning actually buys ────────────────────────────────────────────────

def test_pinning_is_what_makes_the_per_address_cap_real():
    """The point of the whole change, as a before/after.

    The login throttle has always had a per-address cap. Under `trusted_hosts="*"` it
    was decorative: an attacker spraying one username per request while rotating
    `X-Forwarded-For` looked like a different source every time, so the cap never
    tripped. Pinning the trusted set makes the same spray hit it.

    Asserts BOTH halves. Only checking the new behaviour would pass just as happily if
    the cap tripped for some unrelated reason.
    """
    from fastapi import FastAPI
    from web_dashboard.database import LoginAttempt, SessionLocal, get_db
    from web_dashboard.services import login_guard

    cap = login_guard.DEFAULT_MAX_PER_IP
    attempts = cap + 3

    def _login_app(trusted: str) -> TestClient:
        from web_dashboard.api import auth as auth_api
        app = FastAPI()
        app.include_router(auth_api.router)

        def _get():
            db = SessionLocal()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = _get
        app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=trusted)
        return TestClient(_SetPeer(app, ("203.0.113.50", 5555)))

    def _spray(client) -> list:
        # One request per username, a different forged source address each time.
        return [client.post("/api/auth/login",
                            data={"username": f"sprayed{i}", "password": "x"},
                            headers={"X-Forwarded-For": f"198.51.100.{i % 250}"}).status_code
                for i in range(attempts)]

    def _reset():
        db = SessionLocal()
        try:
            db.query(LoginAttempt).delete()
            db.commit()
        finally:
            db.close()

    _reset()
    wildcard = _spray(_login_app("*"))
    assert 429 not in wildcard, (
        "with trusted_hosts='*' the spray should NOT be caught — if it is, this test "
        "no longer demonstrates anything and the rationale needs rechecking")

    _reset()
    pinned = _spray(_login_app("127.0.0.1"))
    assert 429 in pinned, (
        f"pinned trust must catch a {attempts}-request spray from one host; "
        f"got {pinned.count(401)} x 401 and no 429")
    assert pinned.count(401) == cap, "it should trip exactly at the cap"


def _auditor(trusted="127.0.0.1", **kw):
    from web_dashboard.services.public_url import ForwardedHeaderAuditor
    return ForwardedHeaderAuditor(trusted, **kw)


def test_an_untrusted_forwarded_header_produces_an_actionable_warning():
    """The line that turns 'OAuth mysteriously broke' into a fix. It has to name the
    peer, because that string is literally what the operator pastes into the setting."""
    msg = _auditor().check(PROXY_IP, True)
    assert msg, "an untrusted peer sending X-Forwarded-* must warn"
    assert PROXY_IP in msg, "the warning must name the peer to add"
    assert "TRUSTED_PROXY_HOSTS" in msg
    assert "PUBLIC_BASE_URL" in msg, "and the setting that fixes the OAuth half"


def test_a_peer_is_warned_about_only_once():
    """Otherwise a misconfigured proxy writes a log line per request, which is how a
    useful warning becomes noise everyone filters out."""
    auditor = _auditor()
    assert auditor.check(PROXY_IP, True)
    assert auditor.check(PROXY_IP, True) is None
    assert auditor.check("10.9.9.9", True), "a different peer is still worth warning about"


def test_no_warning_without_forwarded_headers():
    """A direct-access install must not log this on every request."""
    assert _auditor().check(PROXY_IP, False) is None


def test_no_warning_for_a_trusted_peer():
    assert _auditor(trusted=PROXY_IP).check(PROXY_IP, True) is None


def test_no_warning_when_the_wildcard_is_deliberately_configured():
    """An operator who has explicitly chosen "*" has made their decision; nagging them
    on every request would not change it."""
    assert _auditor(trusted="*").check(PROXY_IP, True) is None


def test_the_warned_set_is_bounded():
    """A spray of forged headers from many sources must not grow this without limit.
    Past the cap the diagnostic goes quiet, which is the right way for it to fail."""
    auditor = _auditor(cap=3)
    warned = [auditor.check(f"10.0.0.{i}", True) for i in range(10)]
    assert sum(1 for w in warned if w) == 3
    assert len(auditor.seen) == 3


def test_a_missing_peer_address_does_not_warn():
    """Starlette leaves scope['client'] as None in some harnesses and behind some
    servers; a warning naming "" would be useless and confusing."""
    assert _auditor().check("", True) is None


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
