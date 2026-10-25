"""The dashboard half of concurrent agent runs: the lease filter, the slot report, the cap.

  * ``want_job_types`` narrows a lease to what the agent has a free slot for, and can only
    narrow it: a type the agent was never granted stays out whatever it asks for. Absent —
    every agent before 2.9 — the lease is what it always was.
  * ``ansible_slots`` is stored as reported, clamped to the agent's own ceiling, and an
    older agent that sends none leaves it alone.
  * The per-agent request cap scales with the reported slots, because N runs each sending
    Live Output and a heartbeat are N times the traffic, not a flood.

Uses a temporary SQLite file and the real routes. Runs under pytest, or standalone:
    python tests/test_agent_lease_slots.py
"""
import json
import os
import sys
import tempfile
import uuid

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="agent-slots-test-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDB}"
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-agent-slots-tests")

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
except ModuleNotFoundError as exc:
    print(f"SKIP: {exc}")
    sys.exit(0)

from web_dashboard.database import Base, Job, RemoteAgent, SessionLocal, engine, get_db  # noqa: E402
from web_dashboard.api import agent as agent_api  # noqa: E402
from web_dashboard.services import agent_service, agent_signing, config_service  # noqa: E402

Base.metadata.create_all(bind=engine)
AUDIENCE = "https://agents.example.com"


class _Admin:
    username = "tester"
    is_admin = True
    is_effective_admin = True
    accessor_env_id = None


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


def _ready():
    resp = CLIENT.post("/api/agents",
                       json={"name": f"agent-{uuid.uuid4().hex[:8]}", "site": "dc1"})
    assert resp.status_code == 201, resp.text
    code = resp.json()["enrollment_code"]
    private, public = agent_signing.generate_keypair()
    resp = CLIENT.post("/api/agent/enroll",
                       json={"enrollment_code": code, "public_key": public})
    assert resp.status_code == 200, resp.text
    return resp.json()["agent_id"], private


def _lease(private, agent_id, payload=None):
    body = agent_signing.serialize(payload or {})
    headers = agent_signing.sign_request(private, agent_id=agent_id, audience=AUDIENCE,
                                        method="POST", path="/api/agent/lease", body=body)
    headers["Content-Type"] = "application/json"
    return CLIENT.post("/api/agent/lease", content=body, headers=headers)


def _queue(agent_id, job_type):
    db = SessionLocal()
    try:
        job = Job(id=str(uuid.uuid4()), job_type=job_type, status="queued",
                  agent_id=agent_id, extra_data=json.dumps({}))
        db.add(job)
        db.commit()
        return job.id
    finally:
        db.close()


def _agent(agent_id):
    db = SessionLocal()
    try:
        return db.query(RemoteAgent).filter(RemoteAgent.id == agent_id).first()
    finally:
        db.close()


def _claim(agent_id, want):
    db = SessionLocal()
    try:
        row = db.query(RemoteAgent).filter(RemoteAgent.id == agent_id).first()
        return agent_service.lease_one(db, row, want_job_types=want)
    finally:
        db.close()


# ── the lease filter ──────────────────────────────────────────────────────────

def test_want_job_types_narrows_the_claim():
    agent_id, _priv = _ready()
    discover = _queue(agent_id, "agent_discover")
    ansible = _queue(agent_id, "agent_ansible")
    assert _claim(agent_id, []) is None, "an empty want claimed something"
    got = _claim(agent_id, ["agent_ansible"])
    assert got and got["id"] == ansible
    got = _claim(agent_id, None)
    assert got and got["id"] == discover


def test_want_job_types_cannot_widen_the_grant():
    agent_id, _priv = _ready()
    db = SessionLocal()
    try:
        row = db.query(RemoteAgent).filter(RemoteAgent.id == agent_id).first()
        row.allowed_job_types = json.dumps(["agent_discover"])
        db.commit()
    finally:
        db.close()
    _queue(agent_id, "agent_ansible")
    assert _claim(agent_id, ["agent_ansible"]) is None


def test_an_old_agents_lease_is_unchanged():
    agent_id, priv = _ready()
    job = _queue(agent_id, "agent_discover")
    resp = _lease(priv, agent_id, {})
    assert resp.status_code == 200, resp.text
    assert resp.json()["job"]["job_id"] == job


# ── the slot report ───────────────────────────────────────────────────────────

def test_the_slot_report_is_stored_clamped_and_kept():
    agent_id, priv = _ready()
    assert agent_service.ansible_slots(_agent(agent_id)) == 1
    assert _lease(priv, agent_id, {"ansible_slots": 4}).status_code == 200
    assert _agent(agent_id).reported_ansible_slots == 4
    assert _lease(priv, agent_id, {"ansible_slots": 500}).status_code == 200
    assert _agent(agent_id).reported_ansible_slots == agent_service.MAX_REPORTED_ANSIBLE_SLOTS
    # An agent that sends nothing (an older build) leaves the stored value alone.
    assert _lease(priv, agent_id, {}).status_code == 200
    assert _agent(agent_id).reported_ansible_slots == agent_service.MAX_REPORTED_ANSIBLE_SLOTS
    rows = CLIENT.get("/api/agents").json()
    rows = rows.get("agents", rows) if isinstance(rows, dict) else rows
    mine = next(r for r in rows if r["id"] == agent_id)
    assert mine["ansible_slots"] == agent_service.MAX_REPORTED_ANSIBLE_SLOTS


# ── the cap scales with the slots ─────────────────────────────────────────────

def test_the_request_cap_scales_with_reported_slots():
    agent_id, priv = _ready()
    config_service.set("agent_max_requests_per_minute", "3")
    try:
        # The first lease reports two slots; the cap it is checked against is still 3,
        # because the report lands after the check. From then on the cap is 6.
        for i in range(6):
            resp = _lease(priv, agent_id, {"ansible_slots": 2})
            assert resp.status_code == 200, f"request {i}: {resp.text}"
        assert _lease(priv, agent_id, {"ansible_slots": 2}).status_code == 429
    finally:
        config_service.set("agent_max_requests_per_minute", "")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {e!r}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
