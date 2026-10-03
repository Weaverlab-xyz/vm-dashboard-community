"""One-click "Migrate to SPIRE" and the dashboard's own trust domain (services/dashboard_spire).

The dashboard drives its own SPIRE server with ``docker exec <container> spire-server …
-output json``. No SPIRE image is available to the test run, so the subprocess is
replaced by a small fake SPIRE that answers in SPIRE 1.15's ``-output json`` shapes
(protojson, proto field names) and keeps state. Pinned:

  * migrating mints a NODE-scoped join token, creates the workload entry under that node
    selecting the agent's uid, registers the trust domain and binds the agent;
  * a second migration reuses the entry and only mints a new token;
  * the synced bundle really verifies an SVID end to end at /api/agent/attest;
  * a trust domain registered by a SPIRE lab, or by hand, is never overwritten;
  * failures are a 502 with a cause and no token; off is a 409; revoked is a 409;
  * the daily re-sync respects its cadence and its switch, and never raises.

Run: python tests/test_dashboard_spire_migrate.py   (or under pytest)
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="dash-spire-test-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDB}"
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-dashboard-spire-tests")

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

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from jose import jwk, jwt  # noqa: E402
from web_dashboard.database import (Base, RemoteAgent, SessionLocal,  # noqa: E402
                                    SpiffeTrustDomain, engine, get_db)
from web_dashboard.api import agent as agent_api  # noqa: E402
from web_dashboard.services import (agent_service, agent_signing, config_service,  # noqa: E402
                                    dashboard_spire)

Base.metadata.create_all(bind=engine)

AUDIENCE = "https://agents.test"
TD = "dash.example"
SECRET_TOKEN = "join-token-" + uuid.uuid4().hex

_KEY = ec.generate_private_key(ec.SECP256R1())
_PRIV = _KEY.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                           serialization.NoEncryption()).decode()
_JWK = jwk.construct(_KEY.public_key().public_bytes(
    serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode(),
    "ES256").to_dict()
_JWK.update(kid="k1", use="jwt-svid")
_JWK.pop("alg", None)


class FakeSpire:
    """docker exec <container> /opt/spire/bin/spire-server …, as SPIRE 1.15 answers it."""

    def __init__(self):
        self.calls = []
        self.entries = []
        self.tokens = 0
        self.down = False
        self.refuse_entry = False

    def __call__(self, argv, timeout):
        self.calls.append(argv)
        assert argv[:2] == ["docker", "exec"], argv
        assert argv[3] == "/opt/spire/bin/spire-server", argv
        if self.down:
            return subprocess.CompletedProcess(argv, 1, "", "Error response from daemon: "
                                               "No such container: vmdash-spire-server\n")
        a = argv[4:]
        out = lambda d: subprocess.CompletedProcess(argv, 0, json.dumps(d) + "\n", "")  # noqa: E731
        if a[:2] == ["bundle", "show"]:
            if "-format" in a:
                return subprocess.CompletedProcess(argv, 0, json.dumps(
                    {"keys": [_JWK], "spiffe_sequence": 3}, indent=2), "")
            if "-output" in a:
                return out({"trust_domain": TD, "x509_authorities": [], "jwt_authorities": [],
                            "refresh_hint": "0", "sequence_number": "3"})
            return subprocess.CompletedProcess(
                argv, 0, "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n", "")
        if a[:2] == ["token", "generate"]:
            self.tokens += 1
            return out({"value": f"{SECRET_TOKEN}-{self.tokens}", "expires_at": "0"})
        if a[:2] == ["entry", "show"]:
            sid = a[a.index("-spiffeID") + 1]
            return out({"entries": [e for e in self.entries if e["spiffe_id"] == sid],
                        "next_page_token": ""})
        if a[:2] == ["entry", "create"]:
            if self.refuse_entry:
                return out({"results": [{"status": {"code": 6, "message": "similar entry "
                                                                         "already exists"}}]})
            entry = {"id": uuid.uuid4().hex, "spiffe_id": a[a.index("-spiffeID") + 1],
                     "parent_id": a[a.index("-parentID") + 1],
                     "selectors": [a[a.index("-selector") + 1]],
                     "jwt_svid_ttl": int(a[a.index("-jwtSVIDTTL") + 1])}
            self.entries.append(entry)
            return out({"results": [{"status": {"code": 0, "message": "OK"}, "entry": entry}]})
        raise AssertionError(f"unexpected spire-server call {a}")


SPIRE = FakeSpire()
dashboard_spire._run = SPIRE


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


def _register() -> str:
    resp = CLIENT.post("/api/agents", json={"name": f"a-{uuid.uuid4().hex[:8]}", "site": "dc1"})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _migrate(agent_id: str):
    return CLIENT.post(f"/api/agents/{agent_id}/migrate-spire")


def _row(agent_id):
    db = SessionLocal()
    try:
        return db.query(RemoteAgent).filter(RemoteAgent.id == agent_id).first()
    finally:
        db.close()


def _td_row():
    db = SessionLocal()
    try:
        return db.query(SpiffeTrustDomain).filter(SpiffeTrustDomain.trust_domain == TD).first()
    finally:
        db.close()


# ── the happy path ───────────────────────────────────────────────────────────

def test_migrate_creates_the_entries_registers_the_domain_and_binds():
    agent_id = _register()
    resp = _migrate(agent_id)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    sid, node = f"spiffe://{TD}/agent/{agent_id}", f"spiffe://{TD}/node/{agent_id}"
    assert body["spiffe_id"] == sid and body["trust_domain"] == TD
    assert body["join_token"].startswith(SECRET_TOKEN)
    assert "SPIRE_JOIN_TOKEN=" + body["join_token"] in body["env"]
    assert "SPIRE_SERVER_ADDRESS=agents.test" in body["env"]
    assert f"SPIRE_TRUST_DOMAIN={TD}" in body["env"]
    assert "BEGIN CERTIFICATE" in body["bootstrap_pem"]
    gen = next(c for c in SPIRE.calls if c[4:6] == ["token", "generate"])
    assert gen[gen.index("-spiffeID") + 1] == node, "the join token is not scoped to the node"
    entry = next(e for e in SPIRE.entries if e["spiffe_id"] == sid)
    assert entry["parent_id"] == node
    assert entry["selectors"] == ["unix:uid:10001"], "the entry must select the agent's uid"
    row = _row(agent_id)
    assert row.spiffe_id == sid and row.auth_mode is None, (
        "binding must not switch the mode — that happens at the first attest")
    assert _td_row() is not None and _td_row().created_by == dashboard_spire.OWNER


def test_a_second_migration_reuses_the_entry_and_mints_a_new_token():
    agent_id = _register()
    first = _migrate(agent_id).json()["join_token"]
    before = len([e for e in SPIRE.entries if e["spiffe_id"].endswith(agent_id)])
    second = _migrate(agent_id).json()["join_token"]
    after = len([e for e in SPIRE.entries if e["spiffe_id"].endswith(agent_id)])
    assert before == after == 1, "a second migration duplicated the workload entry"
    assert first != second


def test_the_synced_bundle_verifies_an_svid_at_the_attest_route():
    agent_id = _register()
    sid = _migrate(agent_id).json()["spiffe_id"]
    now = int(time.time())
    svid = jwt.encode({"sub": sid, "aud": AUDIENCE + "/api/agent/attest", "iat": now,
                       "exp": now + 300}, _PRIV, algorithm="ES256", headers={"kid": "k1"})
    private, public = agent_signing.generate_keypair()
    resp = CLIENT.post("/api/agent/attest", json={
        "svid": svid, "public_key": public,
        "proof": agent_signing.sign_bytes(private, agent_service.attest_proof_message(svid))})
    assert resp.status_code == 200, resp.text
    assert _row(agent_id).auth_mode == "spiffe"


def test_the_list_reports_the_banner_inputs():
    body = CLIENT.get("/api/agents").json()
    assert body["spire_attest_enabled"] is True
    assert body["spire_min_agent_version"] == dashboard_spire.MIN_AGENT_VERSION == "2.6.0"


# ── refusals and failures ────────────────────────────────────────────────────

def test_a_trust_domain_someone_else_registered_is_never_overwritten():
    db = SessionLocal()
    rec = db.query(SpiffeTrustDomain).filter(SpiffeTrustDomain.trust_domain == TD).first()
    keep = (rec.created_by, rec.bundle_json, rec.spire_lab_id)
    rec.created_by, rec.bundle_json = "alice", '{"keys": []}'
    db.commit()
    db.close()
    try:
        resp = _migrate(_register())
        assert resp.status_code == 502 and "already registered" in resp.json()["detail"], resp.text
        assert _td_row().bundle_json == '{"keys": []}', "another owner's keys were overwritten"
        db = SessionLocal()
        rec = db.query(SpiffeTrustDomain).filter(SpiffeTrustDomain.trust_domain == TD).first()
        rec.created_by, rec.spire_lab_id = dashboard_spire.OWNER, "lab-1"
        db.commit()
        db.close()
        assert _migrate(_register()).status_code == 502, "a lab's trust domain was overwritten"
    finally:
        db = SessionLocal()
        rec = db.query(SpiffeTrustDomain).filter(SpiffeTrustDomain.trust_domain == TD).first()
        rec.created_by, rec.bundle_json, rec.spire_lab_id = keep
        db.commit()
        db.close()


def test_a_server_that_is_down_is_a_502_with_the_cause():
    SPIRE.down = True
    try:
        resp = _migrate(_register())
        assert resp.status_code == 502
        assert "not running" in resp.json()["detail"]
    finally:
        SPIRE.down = False


def test_a_refused_entry_is_a_502_and_never_leaks_the_token():
    SPIRE.refuse_entry = True
    try:
        resp = _migrate(_register())
        assert resp.status_code == 502 and "refused the workload entry" in resp.json()["detail"]
        assert SECRET_TOKEN not in resp.text, "the join token reached an error response"
    finally:
        SPIRE.refuse_entry = False


def test_off_means_409_and_revoked_means_409():
    agent_id = _register()
    config_service.set("spire_attest_enabled", "0")
    try:
        assert _migrate(agent_id).status_code == 409
    finally:
        config_service.set("spire_attest_enabled", "1")
    assert CLIENT.delete(f"/api/agents/{agent_id}").status_code in (200, 204)
    assert _migrate(agent_id).status_code == 409


def test_the_route_needs_agents_write():
    src = open(os.path.join(_ROOT, "web_dashboard", "api", "agent.py"), encoding="utf-8").read()
    body = src.split("def migrate_to_spire(", 1)[1].split("\n@", 1)[0]
    assert 'require_explicit_permission("agents", "write")' in body
    assert '"join_token"' not in body.split("log_audit(", 1)[1].split(")", 1)[0], (
        "the audit record carries the join token")


# ── the daily re-sync ────────────────────────────────────────────────────────

def test_sync_if_due_respects_its_cadence_and_switch_and_never_raises():
    db = SessionLocal()
    try:
        rec = db.query(SpiffeTrustDomain).filter(SpiffeTrustDomain.trust_domain == TD).first()
        rec.bundle_captured_at = datetime.utcnow()
        db.commit()
        assert dashboard_spire.sync_if_due(db) is False, "re-synced a fresh bundle"
        later = datetime.utcnow() + timedelta(hours=25)
        assert dashboard_spire.sync_if_due(db, now=later) is True
        config_service.set("spire_attest_enabled", "0")
        assert dashboard_spire.sync_if_due(db, now=later + timedelta(days=2)) is False
        config_service.set("spire_attest_enabled", "1")
        SPIRE.down = True
        assert dashboard_spire.sync_if_due(db, now=later + timedelta(days=2)) is False
    finally:
        SPIRE.down = False
        config_service.set("spire_attest_enabled", "1")
        db.close()


def test_the_refresh_loop_calls_the_resync():
    main = open(os.path.join(_ROOT, "web_dashboard", "main.py"), encoding="utf-8").read()
    loop = main.split("async def _spire_refresh_loop", 1)[1].split("\nasync def ", 1)[0]
    assert "dashboard_spire.sync_if_due(db)" in loop


def test_the_default_container_is_the_overlays():
    import yaml
    overlay = yaml.safe_load(open(os.path.join(_ROOT, "docker-compose.spire.yml"),
                                  encoding="utf-8"))
    assert overlay["services"]["spire-server"]["container_name"] == dashboard_spire.DEFAULT_CONTAINER


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    # Order matters for the shared fake; run the registration test first, as pytest does
    # by file order.
    fns.sort(key=lambda f: f.__code__.co_firstlineno)
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
