"""Workload access tokens from an EXTERNAL IdP, mapped to a service account.

The IdP here is a locally generated RSA key and a stubbed discovery/JWKS fetch -- the
``test_oidc_service`` pattern -- so every claim the real providers put in a
client-credentials token can be varied one at a time. Pinned:

  * a token for a mapped ``(iss, sub)`` is the service account, on REST, WebSocket, /mcp;
  * a token from the SAME client for a DIFFERENT ``sub`` (a person signing in through the
    app) is refused -- the reason the mapping is keyed on ``sub`` and not ``azp``;
  * wrong audience / issuer, expired, unsigned, and HS256 signed with the public key
    (algorithm confusion) are all refused;
  * an unknown ``kid`` refetches the JWKS once (rotation), then stops asking;
  * with the feature unconfigured nothing external is accepted, and nothing else changed;
  * a mapped service account with nothing granted still reaches nothing.

Run: python tests/test_external_workload_tokens.py   (or under pytest)
"""
import base64
import hashlib
import hmac
import json
import os
import sys
import tempfile
import time
import uuid

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="ext-wl-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDB}"
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-external-workload-tests")

# Third-party deps probed by name, first-party imported UNGUARDED
# (tests/test_import_guard_narrowness.py).
try:
    import cryptography  # noqa: F401
    import fastapi  # noqa: F401
    import jose  # noqa: F401
except ModuleNotFoundError as exc:  # pragma: no cover -- app deps missing
    try:
        import pytest
        pytest.skip(f"app dependencies unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from fastapi import Depends, FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from jose import jwk, jwt  # noqa: E402

from web_dashboard.database import Base, SessionLocal, User, engine  # noqa: E402
from web_dashboard.api import mcp_server, users as users_api  # noqa: E402
from web_dashboard.api import websocket as ws_api  # noqa: E402
from web_dashboard.api.auth import (get_current_user, require_admin,  # noqa: E402
                                    require_permission)
from web_dashboard.services import external_workload, oidc_service  # noqa: E402

Base.metadata.create_all(bind=engine)

ISS = "https://idp.example.com/tenant"
AUD = "api://vm-dashboard"


# ── The fake IdP ─────────────────────────────────────────────────────────────

def _keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode()
    pub = key.public_key().public_bytes(serialization.Encoding.PEM,
                                        serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    return priv, pub


_PRIV, _PUB = _keypair()
_PRIV2, _PUB2 = _keypair()          # the key the IdP rotates to
_OTHER_PRIV, _ = _keypair()         # a key the IdP never published


def _jwk(pub, kid):
    d = jwk.construct(pub, "RS256").to_dict()
    d.update(kid=kid, use="sig", alg="RS256")
    return d


_IDP = {"keys": [_jwk(_PUB, "k1")], "fetches": {"jwks": 0}}


def _fake_fetch(url):
    if url.endswith("/.well-known/openid-configuration"):
        return {"issuer": ISS, "authorization_endpoint": ISS + "/authorize",
                "token_endpoint": ISS + "/token", "jwks_uri": ISS + "/keys"}
    if url == ISS + "/keys":
        _IDP["fetches"]["jwks"] += 1
        return {"keys": list(_IDP["keys"])}
    raise AssertionError(f"unexpected fetch {url}")


oidc_service._fetch = _fake_fetch

_CONF = {"workload_idp_issuer": ISS, "workload_idp_audience": AUD}
external_workload._cfg = lambda key: _CONF.get(key, "")


def _reset_idp():
    _IDP["keys"] = [_jwk(_PUB, "k1")]
    _IDP["fetches"]["jwks"] = 0
    oidc_service.clear_cache()
    external_workload.clear_state()


def _token(sub, *, priv=None, kid="k1", alg="RS256", **claims):
    now = int(time.time())
    body = {"iss": ISS, "aud": AUD, "sub": sub, "azp": "build-client",
            "iat": now, "nbf": now, "exp": now + 600}
    body.update(claims)
    body = {k: v for k, v in body.items() if v is not None}
    return jwt.encode(body, priv or _PRIV, algorithm=alg, headers={"kid": kid})


# ── The dashboard side ───────────────────────────────────────────────────────

def _admin() -> User:
    db = SessionLocal()
    admin = db.query(User).filter(User.username == "_ext_admin").first()
    if not admin:
        admin = User(username="_ext_admin", hashed_password="x", is_admin=True, is_active=True)
        db.add(admin)
        db.commit()
        db.refresh(admin)
    db.expunge(admin)
    db.close()
    return admin


def _app() -> TestClient:
    app = FastAPI()
    app.include_router(users_api.router)

    @app.get("/probe/vms")
    def _vms(user: User = Depends(require_permission("vms", "read"))):
        return {"user": user.username}

    @app.get("/probe/me")
    def _me(user: User = Depends(get_current_user)):
        return {"user": user.username}

    admin = _admin()
    app.dependency_overrides[require_admin] = lambda: admin
    return TestClient(app)


_C = _app()


def _mapped(permissions=None, **mapping) -> dict:
    """A service account with an external identity mapped to it."""
    sa = _C.post("/api/users/service-accounts",
                 json={"username": "ext_" + uuid.uuid4().hex[:8],
                       "permissions": {"vms": ["read"]} if permissions is None else permissions}
                 ).json()
    sub = mapping.pop("subject", "sp-" + uuid.uuid4().hex[:12])
    r = _C.post(f"/api/users/{sa['id']}/external-identities",
                json={"name": "pipeline", "subject": sub, **mapping})
    assert r.status_code == 201, r.text
    return {"sa": sa, "sub": sub, "mapping": r.json()}


def _get(path, tok):
    return _C.get(path, headers={"Authorization": f"Bearer {tok}"})


# ── Tests ────────────────────────────────────────────────────────────────────

def test_a_mapped_subject_is_its_service_account():
    _reset_idp()
    m = _mapped()
    r = _get("/probe/vms", _token(m["sub"]))
    assert r.status_code == 200, r.text
    assert r.json() == {"user": m["sa"]["username"]}


def test_the_same_client_with_another_subject_is_refused():
    """A person signing in through the workload's app registration carries the same azp
    and their own sub. Mapping on azp would have let them in."""
    _reset_idp()
    m = _mapped()
    assert _get("/probe/me", _token("a-person-" + uuid.uuid4().hex[:6])).status_code == 401
    assert _get("/probe/me", _token(m["sub"])).status_code == 200


def test_expected_client_is_checked_when_set():
    _reset_idp()
    m = _mapped(expected_client="build-client")
    assert _get("/probe/me", _token(m["sub"])).status_code == 200
    assert _get("/probe/me", _token(m["sub"], azp="some-other-app")).status_code == 401


def test_wrong_audience_issuer_and_expiry_are_refused():
    _reset_idp()
    m = _mapped()
    assert _get("/probe/me", _token(m["sub"], aud="api://some-other-api")).status_code == 401
    assert _get("/probe/me", _token(m["sub"], iss="https://evil.example.com")).status_code == 401
    past = int(time.time()) - 3600
    assert _get("/probe/me", _token(m["sub"], iat=past - 600, nbf=past - 600,
                                    exp=past)).status_code == 401
    assert _get("/probe/me", _token(m["sub"], exp=None)).status_code == 401


def test_a_token_signed_by_a_key_the_idp_never_published_is_refused():
    _reset_idp()
    m = _mapped()
    assert _get("/probe/me", _token(m["sub"], priv=_OTHER_PRIV)).status_code == 401


def test_algorithm_confusion_is_refused():
    """HS256 signed with the IdP's PUBLIC key as the HMAC secret -- the classic attack on
    a verifier that lets the token choose its algorithm. It also must not fall through to
    the dashboard's own HS256 path as anything but a failure."""
    _reset_idp()
    m = _mapped()
    # Built by hand: jose refuses to HMAC with a PEM, which is exactly the misuse an
    # attacker does not need a library's permission for.
    def seg(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()
    signing_input = (seg({"alg": "HS256", "typ": "JWT", "kid": "k1"}) + "."
                     + seg({"iss": ISS, "aud": AUD, "sub": m["sub"],
                            "exp": int(time.time()) + 600}))
    sig = hmac.new(_PUB.encode(), signing_input.encode(), hashlib.sha256).digest()
    forged = signing_input + "." + base64.urlsafe_b64encode(sig).rstrip(b"=").decode()
    assert _get("/probe/me", forged).status_code == 401
    # And with the header claiming RS256 over the same HMAC signature.
    lying = (seg({"alg": "RS256", "typ": "JWT", "kid": "k1"}) + "."
             + signing_input.split(".", 1)[1])
    lsig = hmac.new(_PUB.encode(), lying.encode(), hashlib.sha256).digest()
    assert _get("/probe/me", lying + "." + base64.urlsafe_b64encode(lsig).rstrip(b"=").decode()
                ).status_code == 401
    unsigned = forged.rsplit(".", 1)[0] + "."
    assert _get("/probe/me", unsigned).status_code == 401


def test_an_unknown_kid_refetches_once_then_stops_asking():
    _reset_idp()
    m = _mapped()
    assert _get("/probe/me", _token(m["sub"])).status_code == 200
    base = _IDP["fetches"]["jwks"]
    # The IdP rotates: the new key is published and the token names it.
    _IDP["keys"] = [_jwk(_PUB, "k1"), _jwk(_PUB2, "k2")]
    assert _get("/probe/me", _token(m["sub"], priv=_PRIV2, kid="k2")).status_code == 200
    assert _IDP["fetches"]["jwks"] == base + 1, "rotation should cost exactly one refetch"
    # Garbage kids must not turn every request into a round trip to the IdP.
    for _ in range(3):
        assert _get("/probe/me", _token(m["sub"], kid="nope")).status_code in (200, 401)
    assert _IDP["fetches"]["jwks"] == base + 1


def test_removing_the_mapping_or_disabling_the_account_ends_access():
    _reset_idp()
    m = _mapped()
    tok = _token(m["sub"])
    assert _get("/probe/me", tok).status_code == 200
    _C.patch(f"/api/users/{m['sa']['id']}", json={"is_active": False})
    assert _get("/probe/me", tok).status_code == 401
    _C.patch(f"/api/users/{m['sa']['id']}", json={"is_active": True})
    assert _get("/probe/me", tok).status_code == 200
    r = _C.delete(f"/api/users/{m['sa']['id']}/external-identities/{m['mapping']['id']}")
    assert r.status_code == 200
    assert _get("/probe/me", tok).status_code == 401


def test_a_mapped_account_with_nothing_granted_reaches_nothing():
    _reset_idp()
    m = _mapped(permissions={})
    assert _get("/probe/me", _token(m["sub"])).status_code == 200
    assert _get("/probe/vms", _token(m["sub"])).status_code == 403


def test_unconfigured_accepts_nothing_external():
    _reset_idp()
    m = _mapped()
    saved = dict(_CONF)
    try:
        _CONF.pop("workload_idp_audience")
        assert _get("/probe/me", _token(m["sub"])).status_code == 401
    finally:
        _CONF.clear()
        _CONF.update(saved)


def test_extra_issuers_are_accepted_exactly():
    """Entra's v1 issuer ends in a slash; the comparison is exact, not normalised."""
    _reset_idp()
    v1 = "https://sts.windows.net/tenant/"
    saved = dict(_CONF)
    try:
        _CONF["workload_idp_extra_issuers"] = v1
        m = _mapped(issuer=v1)
        assert _get("/probe/me", _token(m["sub"], iss=v1)).status_code == 200
        assert _get("/probe/me", _token(m["sub"], iss=v1.rstrip("/"))).status_code == 401
    finally:
        _CONF.clear()
        _CONF.update(saved)


def test_a_mapping_is_refused_for_an_issuer_not_trusted():
    _reset_idp()
    sa = _C.post("/api/users/service-accounts",
                 json={"username": "ext_" + uuid.uuid4().hex[:8]}).json()
    r = _C.post(f"/api/users/{sa['id']}/external-identities",
                json={"name": "x", "subject": "s", "issuer": "https://elsewhere.example"})
    assert r.status_code == 400, r.text


def test_a_duplicate_mapping_is_refused_and_a_person_cannot_hold_one():
    _reset_idp()
    m = _mapped()
    r = _C.post(f"/api/users/{m['sa']['id']}/external-identities",
                json={"name": "again", "subject": m["sub"]})
    assert r.status_code == 409
    db = SessionLocal()
    person = User(username="person_" + uuid.uuid4().hex[:6], hashed_password="x",
                  is_active=True)
    db.add(person)
    db.commit()
    pid = person.id
    db.close()
    r = _C.post(f"/api/users/{pid}/external-identities", json={"name": "x", "subject": "y"})
    assert r.status_code == 400


class _FakeWS:
    def __init__(self, token):
        self.headers = {"sec-websocket-protocol": f"{ws_api._WS_AUTH_SUBPROTOCOL}, {token}"}


def test_websocket_and_mcp_accept_it_too():
    _reset_idp()
    m = _mapped()
    tok = _token(m["sub"])
    db = SessionLocal()
    user, proto = ws_api._authenticate(_FakeWS(tok), db)
    db.close()
    assert user is not None and user.username == m["sa"]["username"] and proto
    mcp_user = mcp_server._validate_pat(tok)
    assert mcp_user is not None and mcp_user.username == m["sa"]["username"]
    assert mcp_server._validate_pat(_token("unmapped")) is None


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
    sys.exit(1 if failures else 0)
