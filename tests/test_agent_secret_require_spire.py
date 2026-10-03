"""Release dashboard-held credentials only to SPIRE-attested agents, driven against a real
SQLite database and the real routes.

docs/remote-agents/credentials.md, "Central storage with SPIRE". With
``dashboard_secrets_require_spire`` on, the three routes that hand out a credential the
dashboard holds — /jobs/{id}/secret, /gateway-key and /ansible-bundle — answer only an
agent whose key came from /api/agent/attest (``auth_mode == "spiffe"``). Pinned here:

  * off (the default), an Ed25519 agent is served exactly as before;
  * on, an Ed25519 agent is refused with 403 and a remedy, and the credential appears
    nowhere in the refusal;
  * on, an attested agent is still served, and the release is still audited;
  * the check runs AFTER job ownership, so another agent's job still answers exactly as a
    missing one does — the policy adds no oracle;
  * all three release routes carry it, before their own type checks.

The attestation itself is pinned in test_agent_spiffe_attest.py; here an enrolled agent
is marked attested in the database, because the signing that follows is identical.

Run: python tests/test_agent_secret_require_spire.py   (or under pytest)
"""
import os
import sys
import tempfile
import uuid

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="agent-secret-spire-test-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDB}"
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-agent-secret-spire-tests")

try:
    import cryptography  # noqa: F401
    import fastapi  # noqa: F401
    import sqlalchemy  # noqa: F401
except ModuleNotFoundError as exc:  # pragma: no cover — app deps missing
    try:
        import pytest
        pytest.skip(f"app dependencies unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)

# First-party imports UNGUARDED: a broken module must fail this file, not skip it.
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from web_dashboard.database import (AuditLog, Base, Job, RemoteAgent,  # noqa: E402
                                    SessionLocal, engine, get_db)
from web_dashboard.api import agent as agent_api  # noqa: E402
from web_dashboard.services import (agent_sealing, agent_signing,  # noqa: E402
                                    config_service,
                                    hypervisor_connection_service as hcs, job_service)

Base.metadata.create_all(bind=engine)

AUDIENCE = "https://agents.test"
SECRET = "vCenter!Admin#2026"
REF = "dc1-vcenter"


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
    return TestClient(app)


CLIENT = _app()


def _policy(on: bool) -> None:
    config_service.set(agent_api.REQUIRE_SPIRE_FOR_SECRETS, "1" if on else "0")


def _ready(*, attested: bool = False) -> tuple:
    """An enrolled agent: returns (agent_id, private_key)."""
    resp = CLIENT.post("/api/agents",
                       json={"name": f"agent-{uuid.uuid4().hex[:8]}", "site": "dc1"})
    assert resp.status_code == 201, resp.text
    private, public = agent_signing.generate_keypair()
    resp = CLIENT.post("/api/agent/enroll", json={
        "enrollment_code": resp.json()["enrollment_code"], "public_key": public,
        "agent_version": "2.6.0", "policy_hash": "a" * 64})
    assert resp.status_code == 200, resp.text
    agent_id = resp.json()["agent_id"]
    if attested:
        db = SessionLocal()
        try:
            row = db.query(RemoteAgent).filter(RemoteAgent.id == agent_id).first()
            row.auth_mode = "spiffe"
            db.commit()
        finally:
            db.close()
    return agent_id, private


def _hypervisor_job(agent_id: str) -> str:
    db = SessionLocal()
    try:
        conn = hcs.create(db, kind="vsphere", name=f"c-{uuid.uuid4().hex[:8]}",
                          created_by="tester", agent_id=agent_id,
                          agent_connection_name=REF, secret=SECRET)
        job = job_service.create_job(
            db, job_type="agent_hypervisor", created_by="tester", agent_id=agent_id,
            metadata={"verb": "inventory_sync", "connection_ref": REF,
                      "connection_id": conn["id"], "kind": "vsphere"})
        db.query(Job).filter(Job.id == job.id).first().status = "running"
        db.commit()
        return job.id
    finally:
        db.close()


def _post(private: str, agent_id: str, path: str, payload: dict):
    body = agent_signing.serialize(payload)
    headers = agent_signing.sign_request(
        private, agent_id=agent_id, audience=AUDIENCE, method="POST", path=path, body=body)
    headers["Content-Type"] = "application/json"
    return CLIENT.request("POST", path, content=body, headers=headers)


def _fetch(private: str, agent_id: str, job_id: str):
    reply_private, reply_public = agent_sealing.generate_reply_keypair()
    resp = _post(private, agent_id, f"/api/agent/jobs/{job_id}/secret",
                 {"connection_ref": REF, "reply_key": reply_public})
    return resp, reply_private


# ── the policy ────────────────────────────────────────────────────────────────

def test_off_by_default_an_ed25519_agent_is_served():
    assert config_service.get_bool(agent_api.REQUIRE_SPIRE_FOR_SECRETS) is False
    agent_id, private = _ready()
    job_id = _hypervisor_job(agent_id)
    resp, reply_private = _fetch(private, agent_id, job_id)
    assert resp.status_code == 200, resp.text
    assert agent_sealing.open_sealed(
        reply_private, resp.json()["sealed"], agent_id=agent_id, audience=AUDIENCE,
        job_id=job_id, ref=REF) == SECRET


def test_on_an_ed25519_agent_is_refused_with_a_remedy():
    agent_id, private = _ready()
    job_id = _hypervisor_job(agent_id)
    _policy(True)
    try:
        resp, _ = _fetch(private, agent_id, job_id)
    finally:
        _policy(False)
    assert resp.status_code == 403, resp.text
    assert SECRET not in resp.text
    detail = resp.json()["detail"]
    assert "SPIRE" in detail and "Agents page" in detail


def test_on_an_attested_agent_is_served_and_audited():
    agent_id, private = _ready(attested=True)
    job_id = _hypervisor_job(agent_id)
    _policy(True)
    try:
        resp, reply_private = _fetch(private, agent_id, job_id)
    finally:
        _policy(False)
    assert resp.status_code == 200, resp.text
    assert agent_sealing.open_sealed(
        reply_private, resp.json()["sealed"], agent_id=agent_id, audience=AUDIENCE,
        job_id=job_id, ref=REF) == SECRET
    db = SessionLocal()
    try:
        rows = db.query(AuditLog).filter(AuditLog.action == "agent.connection_secret").all()
        assert any(job_id in (r.details or "") for r in rows)
    finally:
        db.close()


def test_the_policy_is_no_oracle_for_other_agents_jobs():
    """Ownership is checked first: an Ed25519 agent asking for ANOTHER agent's job must
    get the same answer as for a job that does not exist, not the policy's 403 — which
    would confirm the job exists."""
    owner_id, _ = _ready(attested=True)
    job_id = _hypervisor_job(owner_id)
    other_id, other_private = _ready()
    _policy(True)
    try:
        foreign, _ = _fetch(other_private, other_id, job_id)
        missing, _ = _fetch(other_private, other_id, str(uuid.uuid4()))
    finally:
        _policy(False)
    assert foreign.status_code == missing.status_code == 409
    assert foreign.json()["detail"] == missing.json()["detail"]


def test_every_release_route_carries_the_policy():
    """The Gateway deploy key and the Config-Management bundle are dashboard-held
    credentials too. The policy runs right after ownership, before each route's own job
    type check — so against a hypervisor job, an Ed25519 agent meets the policy (403) and
    an attested one gets past it to the type refusal (409)."""
    ed_id, ed_private = _ready()
    ed_job = _hypervisor_job(ed_id)
    sp_id, sp_private = _ready(attested=True)
    sp_job = _hypervisor_job(sp_id)
    _, reply_public = agent_sealing.generate_reply_keypair()
    _policy(True)
    try:
        for route in ("gateway-key", "ansible-bundle"):
            ed = _post(ed_private, ed_id, f"/api/agent/jobs/{ed_job}/{route}",
                       {"reply_key": reply_public})
            sp = _post(sp_private, sp_id, f"/api/agent/jobs/{sp_job}/{route}",
                       {"reply_key": reply_public})
            assert ed.status_code == 403, (route, ed.text)
            assert sp.status_code == 409, (route, sp.text)
    finally:
        _policy(False)


def test_the_agents_list_reports_the_policy():
    _policy(True)
    try:
        assert CLIENT.get("/api/agents").json()["dashboard_secrets_require_spire"] is True
    finally:
        _policy(False)
    assert CLIENT.get("/api/agents").json()["dashboard_secrets_require_spire"] is False


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
