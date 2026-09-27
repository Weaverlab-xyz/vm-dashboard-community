"""SPIFFE JWT-SVIDs as OAuth client assertions (services/spiffe_assertion).

The trust domain here is a locally generated EC P-256 key -- SPIRE's default JWT key type
-- published two ways: as a stored SPIFFE bundle (``use: jwt-svid``, beside an X.509 root
that must be ignored) and as a live JWKS URL with a stubbed fetch. Pinned:

  * a JWT-SVID for a bound SPIFFE ID gets a dashboard token, with no secret anywhere;
  * the audience must be THIS dashboard's token endpoint (or issuer) -- an SVID minted for
    another relying party is refused;
  * each assertion is single-use; long-lived and expired ones are refused;
  * unregistered trust domain, unbound SPIFFE ID, unknown key, HS256 forgery, client_id
    mismatch, revoked client, disabled account: all one ``invalid_client``;
  * an SVID client has no usable secret, cannot be rotated, and one SPIFFE ID binds one
    active client;
  * the URL source refetches once on an unknown kid (rotation) and no more.

Run: python tests/test_spiffe_client_assertion.py   (or under pytest)
"""
import json
import os
import sys
import tempfile
import time
import uuid

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="svid-assert-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDB}"
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-spiffe-assertion-tests")

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
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from fastapi import Depends, FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from jose import jwk, jwt  # noqa: E402

from web_dashboard.database import Base, OAuthClient, SessionLocal, User, engine  # noqa: E402
from web_dashboard.api import oauth as oauth_api, users as users_api  # noqa: E402
from web_dashboard.api.auth import get_current_user, require_admin  # noqa: E402
from web_dashboard.services import spiffe_assertion  # noqa: E402

Base.metadata.create_all(bind=engine)

TD = "lab.example"
URL_TD = "prod.example"
AUD = "http://testserver/api/oauth/token"
SPIFFE_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-spiffe"
BEARER_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"


def _ec():
    key = ec.generate_private_key(ec.SECP256R1())
    priv = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode()
    pub = key.public_key().public_bytes(serialization.Encoding.PEM,
                                        serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    return priv, pub


def _jwk(pub, kid, use="jwt-svid"):
    d = jwk.construct(pub, "ES256").to_dict()
    d.update(kid=kid, use=use)
    d.pop("alg", None)
    return d


_PRIV, _PUB = _ec()
_PRIV2, _PUB2 = _ec()
_ROGUE, _ = _ec()

# A SPIFFE bundle: one JWT key and one X.509 root (which a JWT verifier must ignore).
_BUNDLE = json.dumps({"keys": [_jwk(_PUB, "jwt1"),
                               {"kty": "EC", "use": "x509-svid", "crv": "P-256",
                                "x": "AAAA", "y": "AAAA", "x5c": ["MIIB"]}],
                      "spiffe_sequence": 1})

_URL = {"keys": [_jwk(_PUB, "u1", use="sig")], "fetches": 0}


def _fake_fetch(url, ca_pem="", server_name=""):
    _URL["fetches"] += 1
    return {"keys": list(_URL["keys"])}


spiffe_assertion._fetch_jwks = _fake_fetch


def _svid(sub, *, priv=None, kid="jwt1", aud=AUD, ttl=300, alg="ES256", **extra):
    now = int(time.time())
    body = {"sub": sub, "aud": aud, "iat": now,
            "exp": now + ttl if ttl is not None else None, **extra}
    body = {k: v for k, v in body.items() if v is not None}
    return jwt.encode(body, priv or _PRIV, algorithm=alg, headers={"kid": kid})


# ── App ──────────────────────────────────────────────────────────────────────

def _admin() -> User:
    db = SessionLocal()
    admin = db.query(User).filter(User.username == "_svid_admin").first()
    if not admin:
        admin = User(username="_svid_admin", hashed_password="x", is_admin=True, is_active=True)
        db.add(admin)
        db.commit()
        db.refresh(admin)
    db.expunge(admin)
    db.close()
    return admin


def _app() -> TestClient:
    app = FastAPI()
    app.include_router(oauth_api.router)
    app.include_router(users_api.router)

    @app.get("/probe/me")
    def _me(user: User = Depends(get_current_user)):
        return {"user": user.username}

    admin = _admin()
    app.dependency_overrides[require_admin] = lambda: admin
    return TestClient(app)


# The test client carries no client address, and with none the token endpoint falls back
# to one shared failure budget -- which this file's deliberate refusals would exhaust.
# Throttling is tested in test_service_account_oauth; here each request is its own source.
import itertools  # noqa: E402
_SRC = itertools.count(1)
oauth_api._client_ip = lambda request: "10.77.%d.%d" % divmod(next(_SRC) % 65536, 256)

_C = _app()
_C.put("/api/oauth/spiffe-trust-domains", json={"trust_domain": TD, "bundle_json": _BUNDLE})
_C.put("/api/oauth/spiffe-trust-domains",
       json={"trust_domain": URL_TD, "jwks_url": "https://oidc.prod.example/keys"})


def _svid_client(td=TD) -> dict:
    sa = _C.post("/api/users/service-accounts",
                 json={"username": "svid_" + uuid.uuid4().hex[:8],
                       "permissions": {"jobs": ["read"]}}).json()
    sid = f"spiffe://{td}/agent/{uuid.uuid4().hex[:8]}"
    r = _C.post(f"/api/users/{sa['id']}/oauth-clients", json={"name": "agent", "spiffe_id": sid})
    assert r.status_code == 201, r.text
    return {"sa": sa, "sid": sid, "client": r.json()}


def _exchange(assertion, atype=SPIFFE_TYPE, **extra):
    return _C.post("/api/oauth/token", data={"grant_type": "client_credentials",
                                             "client_assertion_type": atype,
                                             "client_assertion": assertion, **extra})


# ── Tests ────────────────────────────────────────────────────────────────────

def test_a_bound_svid_gets_a_token_with_no_secret_anywhere():
    m = _svid_client()
    assert m["client"]["client_secret"] == ""
    assert m["client"]["auth_method"] == "spiffe_jwt"
    r = _exchange(_svid(m["sid"]))
    assert r.status_code == 200, r.text
    me = _C.get("/probe/me", headers={"Authorization": f"Bearer {r.json()['access_token']}"})
    assert me.json() == {"user": m["sa"]["username"]}


def test_the_rfc7523_jwt_bearer_type_is_accepted_too():
    m = _svid_client()
    assert _exchange(_svid(m["sid"]), atype=BEARER_TYPE).status_code == 200


def test_the_issuer_url_is_an_accepted_audience():
    m = _svid_client()
    assert _exchange(_svid(m["sid"], aud="http://testserver")).status_code == 200


def test_an_svid_for_another_relying_party_is_refused():
    """The k3s API server's SVID must not be replayable at the dashboard."""
    m = _svid_client()
    r = _exchange(_svid(m["sid"], aud="k8s"))
    assert r.status_code == 401 and r.json()["error"] == "invalid_client"


def test_an_assertion_is_single_use():
    m = _svid_client()
    a = _svid(m["sid"])
    assert _exchange(a).status_code == 200
    assert _exchange(a).status_code == 401


def test_long_lived_and_expired_assertions_are_refused():
    m = _svid_client()
    assert _exchange(_svid(m["sid"], ttl=spiffe_assertion.MAX_ASSERTION_LIFETIME + 600)
                     ).status_code == 401
    now = int(time.time())
    assert _exchange(_svid(m["sid"], iat=now - 900, ttl=None, exp=now - 600)).status_code == 401


def test_unregistered_trust_domain_and_unbound_id_are_refused():
    m = _svid_client()
    other = m["sid"].replace(TD, "elsewhere.example")
    assert _exchange(_svid(other)).status_code == 401
    assert _exchange(_svid(f"spiffe://{TD}/nobody/bound")).status_code == 401
    assert _exchange(_svid("not-a-spiffe-id")).status_code == 401


def test_a_key_outside_the_bundle_and_an_x509_root_are_not_trusted():
    m = _svid_client()
    assert _exchange(_svid(m["sid"], priv=_ROGUE)).status_code == 401
    keys = spiffe_assertion.bundle_keys(_BUNDLE)
    assert [k["kid"] for k in keys] == ["jwt1"], "an X.509 root was offered as a JWT key"


def test_hs256_forgery_is_refused():
    m = _svid_client()
    forged = jwt.encode({"sub": m["sid"], "aud": AUD, "exp": int(time.time()) + 300},
                        "anything", algorithm="HS256")
    assert _exchange(forged).status_code == 401


def test_a_secret_beside_an_assertion_is_an_invalid_request():
    m = _svid_client()
    r = _exchange(_svid(m["sid"]), client_secret="x", client_id=m["client"]["client_id"])
    assert r.status_code == 400 and r.json()["error"] == "invalid_request"


def test_client_id_must_match_when_given():
    m = _svid_client()
    assert _exchange(_svid(m["sid"]), client_id="vmsa_someoneelse").status_code == 401
    assert _exchange(_svid(m["sid"]), client_id=m["client"]["client_id"]).status_code == 200


def test_revoked_client_and_disabled_account_are_refused():
    m = _svid_client()
    _C.patch(f"/api/users/{m['sa']['id']}", json={"is_active": False})
    assert _exchange(_svid(m["sid"])).status_code == 401
    _C.patch(f"/api/users/{m['sa']['id']}", json={"is_active": True})
    assert _exchange(_svid(m["sid"])).status_code == 200
    _C.delete(f"/api/users/{m['sa']['id']}/oauth-clients/{m['client']['id']}")
    assert _exchange(_svid(m["sid"])).status_code == 401


def test_an_svid_client_has_no_usable_secret_and_cannot_rotate():
    m = _svid_client()
    db = SessionLocal()
    row = db.query(OAuthClient).filter(OAuthClient.id == m["client"]["id"]).first()
    assert row.secret_hash, "the column should still hold a (discarded) value"
    db.close()
    r = _C.post("/api/oauth/token", data={"grant_type": "client_credentials",
                                          "client_id": m["client"]["client_id"],
                                          "client_secret": "vmss_" + "0" * 64})
    assert r.status_code == 401
    r = _C.post(f"/api/users/{m['sa']['id']}/oauth-clients/{m['client']['id']}/rotate", json={})
    assert r.status_code == 400


def test_one_spiffe_id_binds_one_active_client():
    m = _svid_client()
    r = _C.post(f"/api/users/{m['sa']['id']}/oauth-clients",
                json={"name": "dupe", "spiffe_id": m["sid"]})
    assert r.status_code == 400
    r = _C.post(f"/api/users/{m['sa']['id']}/oauth-clients",
                json={"name": "bad", "spiffe_id": "https://not-spiffe/x"})
    assert r.status_code == 400


def test_the_url_source_refetches_once_on_rotation():
    spiffe_assertion.clear_state()
    _URL["keys"] = [_jwk(_PUB, "u1", use="sig")]
    _URL["fetches"] = 0
    m = _svid_client(td=URL_TD)
    assert _exchange(_svid(m["sid"], kid="u1")).status_code == 200
    assert _URL["fetches"] == 1
    _URL["keys"] = [_jwk(_PUB, "u1", use="sig"), _jwk(_PUB2, "u2", use="sig")]
    assert _exchange(_svid(m["sid"], priv=_PRIV2, kid="u2")).status_code == 200
    assert _URL["fetches"] == 2, "a rotation should cost exactly one refetch"
    for _ in range(3):
        _exchange(_svid(m["sid"], kid="garbage"))
    assert _URL["fetches"] == 2, "an unknown kid must not make every exchange hit the URL"


def test_trust_domain_validation():
    r = _C.put("/api/oauth/spiffe-trust-domains",
               json={"trust_domain": "x.example", "jwks_url": "http://insecure/keys"})
    assert r.status_code == 400
    r = _C.put("/api/oauth/spiffe-trust-domains",
               json={"trust_domain": "x.example",
                     "bundle_json": json.dumps({"keys": [{"kty": "EC", "use": "x509-svid"}]})})
    assert r.status_code == 400
    r = _C.put("/api/oauth/spiffe-trust-domains", json={"trust_domain": "x.example"})
    assert r.status_code == 400
    listed = {t["trust_domain"]: t for t in _C.get("/api/oauth/spiffe-trust-domains").json()}
    assert listed[TD]["source"] == "bundle" and listed[TD]["bundle_jwt_keys"] == 1
    assert listed[URL_TD]["source"] == "url"


def test_a_lab_capture_registers_the_trust_domain_and_its_svids_verify():
    """store_jwt_bundle is the read-back half of the lab's capture job."""
    from types import SimpleNamespace
    from web_dashboard.services import spire_lab_service
    td = "captured.example"
    lab = SimpleNamespace(id=str(uuid.uuid4()), trust_domain=td, created_by="t")
    db = SessionLocal()
    try:
        out = spire_lab_service.store_jwt_bundle(db, lab, _BUNDLE)
    finally:
        db.close()
    assert out["jwt_keys"] == ["jwt1"]
    listed = {t["trust_domain"]: t for t in _C.get("/api/oauth/spiffe-trust-domains").json()}
    assert listed[td]["spire_lab_id"] == lab.id and not listed[td]["stale"]
    m = _svid_client(td=td)
    assert _exchange(_svid(m["sid"])).status_code == 200
    db = SessionLocal()
    try:
        bad = json.dumps({"keys": [{"kty": "EC", "use": "x509-svid"}]})
        try:
            spire_lab_service.store_jwt_bundle(db, lab, bad)
            raise AssertionError("a bundle with no JWT keys was stored")
        except spire_lab_service.SpireLabError:
            pass
    finally:
        db.close()


def test_the_capture_job_is_wired_into_the_worker():
    src = open(os.path.join(_ROOT, "web_dashboard", "jobs_worker.py"), encoding="utf-8").read()
    assert src.count('"spirelab_jwt_bundle"') >= 3, (
        "spirelab_jwt_bundle must be a handled type, in a tier tuple, and dispatched")
    assert "run_jwt_bundle_capture" in src
    assert os.path.exists(os.path.join(_ROOT, "examples", "playbooks", "spire",
                                       "spire-jwt-bundle.yml"))


def test_the_agent_cell_binds_an_svid_client_only_when_it_can():
    from web_dashboard.services import agentcell_service as cell
    db = SessionLocal()
    try:
        assert cell.svid_client_available(db, "unregistered.example") == ""
        sid = cell.svid_client_available(db, "cell.example")
        assert sid == "", "no trust domain registered yet"
        _C.put("/api/oauth/spiffe-trust-domains",
               json={"trust_domain": "cell.example", "bundle_json": _BUNDLE})
        sid = cell.svid_client_available(db, "cell.example")
        assert sid == cell.spiffe_id_for("cell.example")
    finally:
        db.close()
    sa = _C.post("/api/users/service-accounts",
                 json={"username": "cell_" + uuid.uuid4().hex[:8]}).json()
    assert _C.post(f"/api/users/{sa['id']}/oauth-clients",
                   json={"name": "cell", "spiffe_id": sid}).status_code == 201
    db = SessionLocal()
    try:
        assert cell.svid_client_available(db, "cell.example") == "", (
            "a second cell in the same trust domain must fall back to a secret, not fail")
    finally:
        db.close()


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
