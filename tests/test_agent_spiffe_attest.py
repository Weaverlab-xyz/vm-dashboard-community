"""SPIRE attestation for remote agents: POST /api/agent/attest, end to end.

docs/design/agent-and-human-identity.md, "Agent → dashboard". An agent bound to a SPIFFE
ID presents a JWT-SVID (audience = <pinned audience>/api/agent/attest) and a key it just
made in memory, signed over the SVID. Pinned here, against a real SQLite database and
the real agent code (runners/agent/agent.py) talking to the real routes:

  * the agent's own attest() binds its in-memory key, and that key then signs an
    ordinary lease — the rest of the protocol is untouched;
  * the SVID is single-use, audience-bound, and must be for a BOUND, ACTIVE agent;
  * the proof must be by the key being bound;
  * the route does not exist unless spire_attest_enabled is on, and never derives the
    audience from the request;
  * attesting replaces an Ed25519 key; re-issuing an enrolment code is the rollback;
  * unbinding an attested agent cuts its key off.

Run: python tests/test_agent_spiffe_attest.py   (or under pytest)
"""
import base64
import importlib.util
import json
import os
import sys
import tempfile
import time
import uuid
from types import SimpleNamespace
from urllib.parse import urlparse

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="agent-attest-test-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDB}"
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-agent-attest-tests")
os.environ["AGENT_STATE_DIR"] = tempfile.mkdtemp(prefix="agent-attest-state-")

try:
    import cryptography  # noqa: F401
    import fastapi  # noqa: F401
    import jose  # noqa: F401
    import sqlalchemy  # noqa: F401
except ModuleNotFoundError as exc:  # pragma: no cover — app deps missing
    try:
        import pytest
        pytest.skip(f"app dependencies unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)

# First-party imports UNGUARDED: a broken module must fail this file, not skip it.
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from jose import jwk, jwt  # noqa: E402
from web_dashboard.database import (Base, RemoteAgent, SessionLocal,  # noqa: E402
                                    SpiffeTrustDomain, engine, get_db)
from web_dashboard.api import agent as agent_api  # noqa: E402
from web_dashboard.services import agent_service, agent_signing, config_service  # noqa: E402

Base.metadata.create_all(bind=engine)

AUDIENCE = "https://agents.test"
ATTEST_AUD = AUDIENCE + "/api/agent/attest"
TD = "dash.test"

# The real agent, loaded the way the other agent tests load it.
_spec = importlib.util.spec_from_file_location(
    "agent_runner_attest", os.path.join(_ROOT, "runners", "agent", "agent.py"))
agent_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(agent_mod)
agent_mod.POLICY = SimpleNamespace(digest="b" * 64, job_types=set())


class _Admin:
    username = "tester"
    is_admin = True
    is_effective_admin = True


def _app() -> TestClient:
    app = FastAPI()
    app.include_router(agent_api.router)
    app.include_router(agent_api.admin_router)

    def _db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _db
    from web_dashboard.api.auth import get_current_user
    app.dependency_overrides[get_current_user] = lambda: _Admin()
    config_service.set(agent_api._AUDIENCE_CONFIG, AUDIENCE)
    config_service.set("spire_attest_enabled", "1")
    return TestClient(app)


CLIENT = _app()

# ── the trust domain: an EC key published as a stored SPIFFE bundle ─────────

_KEY = ec.generate_private_key(ec.SECP256R1())
_PRIV = _KEY.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                           serialization.NoEncryption()).decode()
_PUB = _KEY.public_key().public_bytes(serialization.Encoding.PEM,
                                      serialization.PublicFormat.SubjectPublicKeyInfo).decode()
_JWK = jwk.construct(_PUB, "ES256").to_dict()
_JWK.update(kid="k1", use="jwt-svid")
_JWK.pop("alg", None)
_ROGUE = ec.generate_private_key(ec.SECP256R1()).private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption()).decode()

_db = SessionLocal()
if not _db.query(SpiffeTrustDomain).filter(SpiffeTrustDomain.trust_domain == TD).first():
    _db.add(SpiffeTrustDomain(trust_domain=TD, bundle_json=json.dumps({"keys": [_JWK]})))
    _db.commit()
_db.close()


def _svid(sub: str, *, aud=ATTEST_AUD, ttl=300, priv=None) -> str:
    now = int(time.time())
    return jwt.encode({"sub": sub, "aud": aud, "iat": now, "exp": now + ttl},
                      priv or _PRIV, algorithm="ES256", headers={"kid": "k1"})


# ── helpers ─────────────────────────────────────────────────────────────────

def _register() -> tuple:
    resp = CLIENT.post("/api/agents", json={"name": f"a-{uuid.uuid4().hex[:8]}", "site": "dc1"})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"], resp.json()["enrollment_code"]


def _bind(agent_id: str, spiffe_id: str):
    return CLIENT.post(f"/api/agents/{agent_id}/spiffe-id", json={"spiffe_id": spiffe_id})


def _bound_agent() -> tuple:
    agent_id, _code = _register()
    sid = f"spiffe://{TD}/agent/{agent_id}"
    assert _bind(agent_id, sid).status_code == 200
    return agent_id, sid


def _attest_raw(svid: str, *, private=None, public=None, proof=None):
    if private is None:
        private, public = agent_signing.generate_keypair()
    proof = proof if proof is not None else agent_signing.sign_bytes(
        private, agent_service.attest_proof_message(svid))
    return CLIENT.post("/api/agent/attest", json={
        "svid": svid, "public_key": public, "proof": proof,
        "agent_version": "2.6.0", "policy_hash": "c" * 64}), private


def _row(agent_id: str) -> RemoteAgent:
    db = SessionLocal()
    try:
        return db.query(RemoteAgent).filter(RemoteAgent.id == agent_id).first()
    finally:
        db.close()


class _Session:
    """requests.Session as the agent uses it, routed into the TestClient."""
    def __init__(self):
        self.trust_env = True
        self.verify = True

    def request(self, method, url, data=None, headers=None, timeout=None, allow_redirects=False):
        return CLIENT.request(method, urlparse(url).path, content=data, headers=headers)


def _agent_dashboard():
    d = agent_mod.Dashboard(AUDIENCE)
    d.session = _Session()
    return d


# ── the real agent, end to end ──────────────────────────────────────────────

def test_the_agent_attests_with_an_in_memory_key_and_then_leases_normally():
    agent_id, sid = _bound_agent()
    tokfile = os.path.join(tempfile.mkdtemp(), "attest.jwt")
    with open(tokfile, "w", encoding="ascii") as fh:
        fh.write(_svid(sid))
    d = _agent_dashboard()
    ident = d.attest(tokfile)
    assert ident.agent_id == agent_id
    assert ident.audience == AUDIENCE
    assert not os.path.exists(agent_mod._IDENTITY_FILE), (
        "attest wrote identity.json — the key must live only in memory")
    row = _row(agent_id)
    assert row.auth_mode == "spiffe" and row.public_key
    d.identity = ident
    assert d.lease() is None, "the attested key could not sign an ordinary lease"


def test_the_agent_reads_a_base64_token_file_too():
    tok = _svid(f"spiffe://{TD}/agent/x")
    assert agent_mod._parse_jwt_file(tok + "\n") == tok
    assert agent_mod._parse_jwt_file(base64.b64encode(tok.encode()).decode()) == tok
    assert agent_mod._parse_jwt_file("not a token") == ""


def test_the_proof_context_matches_on_both_sides():
    assert agent_mod.ATTEST_PROOF_CONTEXT == agent_service.ATTEST_PROOF_CONTEXT


# ── refusals ────────────────────────────────────────────────────────────────

def test_an_svid_is_single_use():
    _agent_id, sid = _bound_agent()
    svid = _svid(sid)
    first, _ = _attest_raw(svid)
    assert first.status_code == 200, first.text
    again, _ = _attest_raw(svid)
    assert again.status_code == 401, "a replayed SVID bound a second key"


def test_the_audience_must_be_this_dashboards_attest_route():
    _agent_id, sid = _bound_agent()
    for aud in (AUDIENCE + "/api/oauth/token", "https://evil.test/api/agent/attest", AUDIENCE):
        resp, _ = _attest_raw(_svid(sid, aud=aud))
        assert resp.status_code == 401, f"an SVID for {aud!r} was accepted"


def test_an_unbound_identity_attests_nothing():
    resp, _ = _attest_raw(_svid(f"spiffe://{TD}/agent/{uuid.uuid4()}"))
    assert resp.status_code == 401


def test_a_forged_svid_is_refused():
    _agent_id, sid = _bound_agent()
    resp, _ = _attest_raw(_svid(sid, priv=_ROGUE))
    assert resp.status_code == 401


def test_the_proof_must_be_by_the_key_being_bound():
    agent_id, sid = _bound_agent()
    svid = _svid(sid)
    _priv, public = agent_signing.generate_keypair()
    other_priv, _ = agent_signing.generate_keypair()
    bad = agent_signing.sign_bytes(other_priv, agent_service.attest_proof_message(svid))
    resp, _ = _attest_raw(svid, private=other_priv, public=public, proof=bad)
    assert resp.status_code == 401, "a key was bound without proof of possession"
    # And the refused attempt did not burn the SVID: the honest agent still gets in.
    ok, _ = _attest_raw(svid)
    assert ok.status_code == 200, ok.text


def test_a_revoked_agent_cannot_attest():
    agent_id, sid = _bound_agent()
    assert CLIENT.delete(f"/api/agents/{agent_id}").status_code in (200, 204)
    resp, _ = _attest_raw(_svid(sid))
    assert resp.status_code == 401


def test_the_route_is_off_unless_enabled():
    _agent_id, sid = _bound_agent()
    config_service.set("spire_attest_enabled", "0")
    try:
        resp, _ = _attest_raw(_svid(sid))
        assert resp.status_code == 404
    finally:
        config_service.set("spire_attest_enabled", "1")


def test_an_unpinned_dashboard_refuses_rather_than_trusting_the_host_header():
    _agent_id, sid = _bound_agent()
    config_service.set(agent_api._AUDIENCE_CONFIG, "")
    try:
        resp, _ = _attest_raw(_svid(sid, aud="http://testserver/api/agent/attest"))
        assert resp.status_code == 409, "the audience was derived from the request"
    finally:
        config_service.set(agent_api._AUDIENCE_CONFIG, AUDIENCE)


# ── binding, migration and rollback ─────────────────────────────────────────

def test_binding_validates_and_is_one_to_one():
    a, _ = _register()
    b, _ = _register()
    assert _bind(a, "not-a-spiffe-id").status_code == 400
    sid = f"spiffe://{TD}/agent/shared-{uuid.uuid4().hex[:6]}"
    assert _bind(a, sid).status_code == 200
    assert _bind(b, sid).status_code == 400, "one SPIFFE ID was bound to two agents"


def test_attesting_replaces_an_ed25519_key_and_reissue_rolls_back():
    agent_id, code = _register()
    private, public = agent_signing.generate_keypair()
    assert CLIENT.post("/api/agent/enroll", json={
        "enrollment_code": code, "public_key": public}).status_code == 200
    assert _row(agent_id).auth_mode in (None, "ed25519")
    sid = f"spiffe://{TD}/agent/{agent_id}"
    assert _bind(agent_id, sid).status_code == 200
    assert _row(agent_id).public_key == public, "binding alone must not cut the old key off"
    resp, _ = _attest_raw(_svid(sid))
    assert resp.status_code == 200, resp.text
    row = _row(agent_id)
    assert row.auth_mode == "spiffe" and row.public_key != public, (
        "the Ed25519 key survived attestation — the agent would have two identities")
    # Rollback: a fresh enrolment code clears the binding and the attested key.
    assert CLIENT.post(f"/api/agents/{agent_id}/enrollment-code").status_code == 200
    row = _row(agent_id)
    assert row.spiffe_id is None and row.auth_mode is None and row.public_key is None
    again, _ = _attest_raw(_svid(sid))
    assert again.status_code == 401, "an SVID still attested after the rollback"


def test_unbinding_an_attested_agent_cuts_its_key_off():
    agent_id, sid = _bound_agent()
    resp, _ = _attest_raw(_svid(sid))
    assert resp.status_code == 200
    assert _bind(agent_id, "").status_code == 200
    row = _row(agent_id)
    assert row.spiffe_id is None and row.public_key is None


def test_the_agent_row_reports_its_mode():
    agent_id, sid = _bound_agent()
    row = CLIENT.get(f"/api/agents/{agent_id}").json()
    assert row["auth_mode"] == "ed25519" and row["spiffe_id"] == sid
    _attest_raw(_svid(sid))
    assert CLIENT.get(f"/api/agents/{agent_id}").json()["auth_mode"] == "spiffe"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
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
