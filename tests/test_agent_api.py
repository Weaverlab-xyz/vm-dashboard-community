"""End-to-end behaviour of the remote-agent API, against a real SQLite database.

The static tests elsewhere pin the shape of this code; this one drives it. What it is
really here to catch is the class of bug that reads as working software:

  * **IDOR.** Agent A must never see, lease, log to, or complete agent B's job. This is
    the highest-severity bug this API could have, and nothing about it is visible from
    a single-agent test.
  * **A PAT must not be an agent, and an agent must not be a user.** The two identity
    systems share a transport (``Authorization``-less signed requests vs bearer tokens)
    and must not share authority.
  * **Replay.** A captured request must fail the second time.
  * **The lease/heartbeat/complete lifecycle**, including that a revoked agent stops
    working immediately rather than at the next reconcile.

Uses a temporary SQLite file and the real ORM — no mocks of the thing under test.
Skips if the app dependencies are absent. Runs under pytest, or standalone:
    python tests/test_agent_api.py
"""
import os
import sys
import tempfile
import time
import uuid

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# A file-backed SQLite DB, set before web_dashboard.database is imported so the engine
# binds to it. Deliberately not :memory: — the app opens more than one connection.
_TMPDB = os.path.join(tempfile.mkdtemp(prefix="agent-api-test-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDB}"
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-agent-api-tests")

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from web_dashboard.database import Base, Job, SessionLocal, engine, get_db
    from web_dashboard.api import agent as agent_api
    from web_dashboard.services import agent_service, agent_signing, job_service
except Exception as exc:  # pragma: no cover — app deps missing
    try:
        import pytest
        pytest.skip(f"app dependencies unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)

Base.metadata.create_all(bind=engine)

AUDIENCE = "https://agents.test"


class _Admin:
    """Stands in for an authenticated admin. The operator half is covered by
    require_admin, which is asserted statically in test_agent_lease_invariants; here we
    just need a principal so the handlers can record who acted."""
    username = "tester"
    is_admin = True
    is_effective_admin = True


def _app() -> TestClient:
    """A minimal app carrying only the agent router, so these tests do not depend on
    the whole application's startup (config_service, warmers, the setup guard)."""
    app = FastAPI()
    app.include_router(agent_api.router)

    def _db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _db
    from web_dashboard.api.auth import require_admin
    app.dependency_overrides[require_admin] = lambda: _Admin()

    # Pin the audience rather than letting it derive from the test client's host, so
    # the value the agent signs is the value the server checks.
    from web_dashboard.services import config_service
    config_service.set(agent_api._AUDIENCE_CONFIG, AUDIENCE)
    return TestClient(app)


CLIENT = _app()


# ── helpers ───────────────────────────────────────────────────────────────────

def _register(name: str = "") -> tuple[str, str]:
    """Create an agent through the operator API; return (agent_id, enrolment code)."""
    name = name or f"agent-{uuid.uuid4().hex[:8]}"
    resp = CLIENT.post("/api/agent", json={"name": name, "site": "dc1"})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    return body["id"], body["enrollment_code"]


def _enroll(code: str) -> tuple[str, str]:
    """Enrol with a fresh keypair; return (agent_id, private key)."""
    private, public = agent_signing.generate_keypair()
    resp = CLIENT.post("/api/agent/enroll", json={
        "enrollment_code": code, "public_key": public,
        "agent_version": "1.0.0", "policy_hash": "a" * 64})
    assert resp.status_code == 200, resp.text
    return resp.json()["agent_id"], private


def _signed(private: str, agent_id: str, method: str, path: str, payload: dict,
            **overrides):
    """Issue a correctly signed request. Overrides let a test corrupt exactly one
    thing at a time."""
    body = agent_signing.serialize(payload)
    headers = agent_signing.sign_request(
        private, agent_id=agent_id, audience=overrides.get("audience", AUDIENCE),
        method=method, path=path, body=body,
        timestamp=overrides.get("timestamp"), nonce=overrides.get("nonce"))
    headers["Content-Type"] = "application/json"
    return CLIENT.request(method, path, content=body, headers=headers)


def _queue_job(agent_id: str, job_type: str = "agent_discover") -> str:
    db = SessionLocal()
    try:
        job = job_service.create_job(db, job_type=job_type, created_by="tester",
                                     metadata={"scan_kind": "k8s"}, agent_id=agent_id)
        return job.id
    finally:
        db.close()


def _job(job_id: str) -> Job:
    db = SessionLocal()
    try:
        return db.query(Job).filter(Job.id == job_id).first()
    finally:
        db.close()


def _ready() -> tuple[str, str]:
    _id, code = _register()
    return _enroll(code)


# ── enrolment ─────────────────────────────────────────────────────────────────

def test_enrolment_binds_the_public_key_and_returns_the_dashboard_key():
    _id, code = _register()
    private, public = agent_signing.generate_keypair()
    resp = CLIENT.post("/api/agent/enroll", json={
        "enrollment_code": code, "public_key": public, "agent_version": "1.0.0"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["agent_id"] == _id
    assert body["audience"] == AUDIENCE
    # The agent pins this to verify job envelopes; without it there is no provenance.
    assert agent_signing.load_public_key(body["dashboard_public_key"])


def test_an_enrolment_code_is_single_use():
    """A captured code must not enrol a second, attacker-controlled container against
    the same agent row."""
    _id, code = _register()
    _enroll(code)
    _private, public = agent_signing.generate_keypair()
    resp = CLIENT.post("/api/agent/enroll",
                       json={"enrollment_code": code, "public_key": public})
    assert resp.status_code == 400


def test_a_garbage_enrolment_code_is_refused():
    _private, public = agent_signing.generate_keypair()
    for code in ("", "nope", "vmcli_" + "0" * 64, "agte_" + "0" * 64):
        resp = CLIENT.post("/api/agent/enroll",
                           json={"enrollment_code": code, "public_key": public})
        assert resp.status_code == 400, code


def test_a_malformed_public_key_is_refused_at_enrolment():
    """Better here than as an unexplained 401 on every later poll."""
    _id, code = _register()
    resp = CLIENT.post("/api/agent/enroll",
                       json={"enrollment_code": code, "public_key": "not-a-key"})
    assert resp.status_code == 400


def test_duplicate_agent_names_are_refused():
    name = f"dupe-{uuid.uuid4().hex[:8]}"
    assert CLIENT.post("/api/agent", json={"name": name}).status_code == 201
    assert CLIENT.post("/api/agent", json={"name": name}).status_code == 400


# ── signing ───────────────────────────────────────────────────────────────────

def test_an_unsigned_request_is_rejected():
    agent_id, _private = _ready()
    assert CLIENT.post("/api/agent/lease", json={}).status_code == 401


def test_a_correctly_signed_lease_succeeds():
    agent_id, private = _ready()
    resp = _signed(private, agent_id, "POST", "/api/agent/lease", {})
    assert resp.status_code == 200
    assert resp.json()["job"] is None      # empty queue, not an error


def test_a_signature_from_another_key_is_rejected():
    agent_id, _private = _ready()
    other, _pub = agent_signing.generate_keypair()
    assert _signed(other, agent_id, "POST", "/api/agent/lease", {}).status_code == 401


def test_a_replayed_request_is_rejected():
    """The nonce store is what turns the ±60s window from 'narrow' into 'once'."""
    agent_id, private = _ready()
    body = agent_signing.serialize({})
    headers = agent_signing.sign_request(
        private, agent_id=agent_id, audience=AUDIENCE, method="POST",
        path="/api/agent/lease", body=body)
    headers["Content-Type"] = "application/json"

    first = CLIENT.post("/api/agent/lease", content=body, headers=headers)
    replay = CLIENT.post("/api/agent/lease", content=body, headers=headers)
    assert first.status_code == 200
    assert replay.status_code == 401, "the identical request must not work twice"


def test_a_stale_timestamp_is_rejected():
    agent_id, private = _ready()
    old = str(int(time.time()) - agent_signing.SIGNATURE_WINDOW_SECONDS - 30)
    resp = _signed(private, agent_id, "POST", "/api/agent/lease", {}, timestamp=old)
    assert resp.status_code == 401


def test_a_signature_for_another_audience_is_rejected():
    agent_id, private = _ready()
    resp = _signed(private, agent_id, "POST", "/api/agent/lease", {},
                   audience="https://evil.test")
    assert resp.status_code == 401


def test_a_signature_bound_to_a_different_path_is_rejected():
    """A lease signature replayed against /complete is the concrete attack."""
    agent_id, private = _ready()
    job_id = _queue_job(agent_id)
    body = agent_signing.serialize({})
    headers = agent_signing.sign_request(
        private, agent_id=agent_id, audience=AUDIENCE, method="POST",
        path="/api/agent/lease", body=body)
    headers["Content-Type"] = "application/json"
    resp = CLIENT.post(f"/api/agent/jobs/{job_id}/complete", content=body, headers=headers)
    assert resp.status_code == 401


def test_a_pat_shaped_token_gets_no_agent_access():
    """The two identity systems must not share authority. A PAT is full user
    impersonation; it must buy exactly nothing here."""
    resp = CLIENT.post("/api/agent/lease", json={},
                       headers={"Authorization": "Bearer vmcli_" + "0" * 64})
    assert resp.status_code == 401


# ── the lease ─────────────────────────────────────────────────────────────────

def test_a_queued_job_is_leased_and_marked_running():
    agent_id, private = _ready()
    job_id = _queue_job(agent_id)
    assert _job(job_id).status == "queued", "create_job(agent_id=…) must force 'queued'"

    resp = _signed(private, agent_id, "POST", "/api/agent/lease", {})
    assert resp.status_code == 200
    job = resp.json()["job"]
    assert job["job_id"] == job_id
    assert job["job_type"] == "agent_discover"
    assert _job(job_id).status == "running"


def test_the_lease_envelope_is_signed_by_the_dashboard():
    """Provenance: this signature is what an attacker with database write access
    cannot forge."""
    agent_id, private = _ready()
    _queue_job(agent_id)
    body = _signed(private, agent_id, "POST", "/api/agent/lease", {}).json()
    assert agent_signing.verify_envelope(
        agent_service.envelope_public_key(), body["job"], body["signature"])


def test_a_job_is_leased_exactly_once():
    agent_id, private = _ready()
    _queue_job(agent_id)
    first = _signed(private, agent_id, "POST", "/api/agent/lease", {}).json()
    second = _signed(private, agent_id, "POST", "/api/agent/lease", {}).json()
    assert first["job"] is not None
    assert second["job"] is None, "a running job must not be leased again"


def test_agent_b_cannot_lease_agent_as_job():
    """The IDOR case, and the reason the lease filters on agent_id in both the select
    and the claiming update."""
    agent_a, _pa = _ready()
    agent_b, pb = _ready()
    job_id = _queue_job(agent_a)

    resp = _signed(pb, agent_b, "POST", "/api/agent/lease", {})
    assert resp.status_code == 200
    assert resp.json()["job"] is None, "agent B leased agent A's job"
    assert _job(job_id).status == "queued", "agent A's job must be untouched"


def test_the_local_worker_cannot_claim_an_agent_job():
    """The other half of the race: jobs_worker claims `pending` and only its own
    HANDLED_TYPES. An agent job is neither."""
    from web_dashboard import jobs_worker
    agent_id, _private = _ready()
    _queue_job(agent_id)
    db = SessionLocal()
    try:
        assert "agent_discover" not in jobs_worker.HANDLED_TYPES
        assert jobs_worker._claim_one(db) is None
    finally:
        db.close()


# ── reporting back ────────────────────────────────────────────────────────────

def test_logs_land_in_job_logs_where_the_websocket_reads_them():
    """This is what makes an agent's output appear in the existing Live Output pane
    with no frontend change."""
    agent_id, private = _ready()
    job_id = _queue_job(agent_id)
    _signed(private, agent_id, "POST", "/api/agent/lease", {})

    resp = _signed(private, agent_id, "POST", f"/api/agent/jobs/{job_id}/logs",
                   {"lines": ["FOUND k8s at 10.20.0.11:6443", "…64/128 probed"]})
    assert resp.status_code == 200 and resp.json()["written"] == 2
    db = SessionLocal()
    try:
        lines = [line for _seq, line in job_service.get_job_logs(db, job_id)]
    finally:
        db.close()
    assert "FOUND k8s at 10.20.0.11:6443" in lines


def test_control_characters_are_stripped_from_agent_output():
    """Output originates from an untrusted host on the far side of the agent and is
    rendered in an operator's browser and terminal."""
    agent_id, private = _ready()
    job_id = _queue_job(agent_id)
    _signed(private, agent_id, "POST", "/api/agent/lease", {})
    _signed(private, agent_id, "POST", f"/api/agent/jobs/{job_id}/logs",
            {"lines": ["\x1b[31mred\x1b[0m\x07 and \x00 nul"]})
    db = SessionLocal()
    try:
        line = job_service.get_job_logs(db, job_id)[0][1]
    finally:
        db.close()
    assert "\x1b" not in line and "\x07" not in line and "\x00" not in line
    assert "red" in line


def test_the_heartbeat_reports_progress_and_the_cancel_signal():
    """The cancel half is what makes the existing Cancel button work end to end."""
    agent_id, private = _ready()
    job_id = _queue_job(agent_id)
    _signed(private, agent_id, "POST", "/api/agent/lease", {})

    resp = _signed(private, agent_id, "POST", f"/api/agent/jobs/{job_id}/heartbeat",
                   {"progress_pct": 40, "message": "probing"})
    assert resp.status_code == 200 and resp.json()["cancel_requested"] is False
    assert _job(job_id).progress_pct == 40

    db = SessionLocal()
    try:
        job_service.set_cancelled(db, job_id)
    finally:
        db.close()
    resp = _signed(private, agent_id, "POST", f"/api/agent/jobs/{job_id}/heartbeat",
                   {"progress_pct": 50, "message": "probing"})
    assert resp.json()["cancel_requested"] is True


def test_a_cancelled_job_can_still_be_logged_to_and_wound_down():
    """The full cooperative-cancel path. The agent only learns about a cancel from the
    heartbeat, so every endpoint has to keep working while it winds down — refusing
    them the instant the status flips would make cancel unreachable in practice."""
    agent_id, private = _ready()
    job_id = _queue_job(agent_id)
    _signed(private, agent_id, "POST", "/api/agent/lease", {})

    db = SessionLocal()
    try:
        job_service.set_cancelled(db, job_id)
    finally:
        db.close()

    assert _signed(private, agent_id, "POST", f"/api/agent/jobs/{job_id}/heartbeat",
                   {"progress_pct": 60}).json()["cancel_requested"] is True
    assert _signed(private, agent_id, "POST", f"/api/agent/jobs/{job_id}/logs",
                   {"lines": ["stopping on request"]}).status_code == 200

    # The agent reports `failed` when it stops, but the operator's cancel must win:
    # showing a red failure for something that did exactly what was asked is wrong.
    resp = _signed(private, agent_id, "POST", f"/api/agent/jobs/{job_id}/complete",
                   {"status": "failed", "error": "Cancelled by the operator."})
    assert resp.status_code == 200 and resp.json()["status"] == "cancelled"
    assert _job(job_id).status == "cancelled"


def test_completing_a_job_stores_the_findings():
    agent_id, private = _ready()
    job_id = _queue_job(agent_id)
    _signed(private, agent_id, "POST", "/api/agent/lease", {})

    findings = [{"kind": "k8s", "host": "10.20.0.11", "port": 6443,
                 "api_server": "https://10.20.0.11:6443", "server_version": "v1.31.3+k3s1"}]
    resp = _signed(private, agent_id, "POST", f"/api/agent/jobs/{job_id}/complete",
                   {"status": "completed", "result": {"findings": findings, "scanned": 12}})
    assert resp.status_code == 200 and resp.json()["status"] == "completed"

    job = _job(job_id)
    assert job.status == "completed"
    stored = job.metadata_dict["findings"][0]
    assert stored["api_server"] == "https://10.20.0.11:6443"
    # Annotated server-side; the agent is never handed the dashboard's inventory.
    assert stored["already_registered"] is False


def test_agent_b_cannot_complete_agent_as_job():
    agent_a, pa = _ready()
    agent_b, pb = _ready()
    job_id = _queue_job(agent_a)
    _signed(pa, agent_a, "POST", "/api/agent/lease", {})

    resp = _signed(pb, agent_b, "POST", f"/api/agent/jobs/{job_id}/complete",
                   {"status": "completed", "result": {}})
    assert resp.status_code == 409
    assert _job(job_id).status == "running", "agent B completed agent A's job"


def test_agent_b_cannot_write_logs_to_agent_as_job():
    agent_a, pa = _ready()
    agent_b, pb = _ready()
    job_id = _queue_job(agent_a)
    _signed(pa, agent_a, "POST", "/api/agent/lease", {})

    resp = _signed(pb, agent_b, "POST", f"/api/agent/jobs/{job_id}/logs",
                   {"lines": ["injected"]})
    assert resp.status_code == 409


def test_an_unleased_job_cannot_be_completed():
    agent_id, private = _ready()
    job_id = _queue_job(agent_id)          # queued, never leased
    resp = _signed(private, agent_id, "POST", f"/api/agent/jobs/{job_id}/complete",
                   {"status": "completed", "result": {}})
    assert resp.status_code == 409


def test_an_invalid_terminal_status_is_refused():
    agent_id, private = _ready()
    job_id = _queue_job(agent_id)
    _signed(private, agent_id, "POST", "/api/agent/lease", {})
    resp = _signed(private, agent_id, "POST", f"/api/agent/jobs/{job_id}/complete",
                   {"status": "cancelled", "result": {}})
    assert resp.status_code == 400


def test_an_oversized_result_fails_the_job_rather_than_the_request():
    """A cap that 500s would leave the job running forever; failing it is the honest
    outcome and the operator can see why."""
    agent_id, private = _ready()
    job_id = _queue_job(agent_id)
    _signed(private, agent_id, "POST", "/api/agent/lease", {})
    huge = {"findings": [{"blob": "x" * 1000} for _ in range(400)]}
    resp = _signed(private, agent_id, "POST", f"/api/agent/jobs/{job_id}/complete",
                   {"status": "completed", "result": huge})
    assert resp.status_code == 200 and resp.json()["status"] == "failed"
    assert "exceeded" in (_job(job_id).error_message or "")


# ── revocation ────────────────────────────────────────────────────────────────

def test_revoking_an_agent_stops_it_immediately_and_fails_its_job():
    """Not at the next 10-minute reconcile. An operator who hits Revoke should not have
    to wonder whether something is still running out there."""
    agent_id, private = _ready()
    job_id = _queue_job(agent_id)
    _signed(private, agent_id, "POST", "/api/agent/lease", {})
    assert _job(job_id).status == "running"

    assert CLIENT.delete(f"/api/agent/{agent_id}").status_code == 200
    assert _job(job_id).status == "failed"
    assert _signed(private, agent_id, "POST", "/api/agent/lease", {}).status_code == 401


def test_reissuing_a_code_locks_out_the_running_container():
    """The container being replaced must stop being able to lease the moment the new
    code is issued, not whenever someone remembers to stop it."""
    agent_id, code = _register()
    _agent_id, private = _enroll(code)
    assert _signed(private, agent_id, "POST", "/api/agent/lease", {}).status_code == 200

    resp = CLIENT.post(f"/api/agent/{agent_id}/enrollment-code")
    assert resp.status_code == 200 and resp.json()["enrollment_code"].startswith("agte_")
    assert _signed(private, agent_id, "POST", "/api/agent/lease", {}).status_code == 401


# ── operator half ─────────────────────────────────────────────────────────────

def test_the_listing_derives_status_and_never_leaks_key_material():
    agent_id, private = _ready()
    _signed(private, agent_id, "POST", "/api/agent/lease", {})
    rows = CLIENT.get("/api/agent").json()["agents"]
    row = next(r for r in rows if r["id"] == agent_id)
    assert row["status"] == "online"
    for leaky in ("public_key", "enroll_code_hash", "enrollment_code", "private_key"):
        assert leaky not in row, f"the listing exposes {leaky}"


def test_the_listing_carries_the_signals_needed_to_judge_an_agent():
    """The console renders these to answer 'did I register this, and where is it?'.
    They were all returned but unrendered; a field dropped here goes silently blank in
    the UI rather than erroring."""
    agent_id, private = _ready()
    _signed(private, agent_id, "POST", "/api/agent/lease", {})
    row = next(r for r in CLIENT.get("/api/agent").json()["agents"] if r["id"] == agent_id)
    for field in ("created_by", "created_at", "enrolled_at", "last_seen_ip",
                  "policy_hash", "id"):
        assert field in row, f"the listing no longer returns {field}"
    assert row["created_by"] == "tester"
    assert row["policy_hash"] == "a" * 64      # what _enroll reports


# ── removing a revoked record ─────────────────────────────────────────────────

def test_a_live_agent_record_cannot_be_removed():
    """Removal must not be the first action available on a working agent — revoking is
    what settles its in-flight jobs, and deleting the row first would leave one running
    with nothing to reconcile it."""
    agent_id, _private = _ready()
    resp = CLIENT.delete(f"/api/agent/{agent_id}/record")
    assert resp.status_code == 409
    assert "revoke" in resp.json()["detail"].lower()


def test_removing_a_revoked_record_frees_the_name():
    """The reason this endpoint exists. create_agent enforces name uniqueness across
    ALL rows, so before this a revoked agent squatted its name permanently."""
    name = f"squatter-{uuid.uuid4().hex[:8]}"
    agent_id, code = _register(name)
    _enroll(code)

    # Same name is refused while the row exists, revoked or not.
    assert CLIENT.delete(f"/api/agent/{agent_id}").status_code == 200
    assert CLIENT.post("/api/agent", json={"name": name}).status_code == 400

    assert CLIENT.delete(f"/api/agent/{agent_id}/record").status_code == 200
    assert CLIENT.post("/api/agent", json={"name": name}).status_code == 201, \
        "the name should be reusable once the record is gone"


def test_removing_a_record_keeps_the_job_history():
    """Deleting the agent must not take its jobs with it — the logs and results are the
    record of what it did, and that is exactly what you want after removing one you
    were suspicious of."""
    agent_id, private = _ready()
    job_id = _queue_job(agent_id)
    _signed(private, agent_id, "POST", "/api/agent/lease", {})
    _signed(private, agent_id, "POST", f"/api/agent/jobs/{job_id}/logs",
            {"lines": ["something it did"]})

    CLIENT.delete(f"/api/agent/{agent_id}")
    assert CLIENT.delete(f"/api/agent/{agent_id}/record").status_code == 200

    job = _job(job_id)
    assert job is not None, "the job row must survive the agent's deletion"
    assert job.agent_id is None, "and must no longer point at a row that is gone"
    db = SessionLocal()
    try:
        lines = [line for _seq, line in job_service.get_job_logs(db, job_id)]
    finally:
        db.close()
    assert "something it did" in lines


def test_the_record_route_is_not_swallowed_by_the_agent_id_route():
    """`DELETE /{agent_id}` is declared first. A path param matches one segment, so
    /{id}/record must still reach its own handler — a 404 here would mean the route
    ordering silently broke."""
    agent_id, _private = _ready()
    assert CLIENT.delete(f"/api/agent/{agent_id}/record").status_code == 409  # not 404


def test_removing_an_unknown_record_is_a_404():
    assert CLIENT.delete(f"/api/agent/{uuid.uuid4()}/record").status_code == 404


def test_discovery_is_refused_for_an_offline_agent():
    """Queuing work for a dead agent would silently sit there; saying so is kinder."""
    agent_id, _code = _register()
    resp = CLIENT.post(f"/api/agent/{agent_id}/discover", json={"scan_kind": "k8s"})
    assert resp.status_code == 409


def test_discovery_queues_a_job_bound_to_the_agent():
    agent_id, private = _ready()
    resp = CLIENT.post(f"/api/agent/{agent_id}/discover",
                       json={"scan_kind": "both", "cidrs": ["10.20.0.0/24"],
                             "max_hosts": 10 ** 7})
    assert resp.status_code == 202
    job = _job(resp.json()["job_id"])
    assert job.status == "queued" and job.agent_id == agent_id
    # Clamped at enqueue, so the stored row is already sane.
    from web_dashboard.services import agent_job_meta
    assert job.metadata_dict["max_hosts"] == agent_job_meta.MAX_HOSTS_CEILING


def test_a_queued_agent_job_is_cancellable():
    """It used to 409 — latent while the only queued rows were bulk children."""
    agent_id, _private = _ready()
    job_id = _queue_job(agent_id)
    db = SessionLocal()
    try:
        assert job_service.set_cancelled(db, job_id).status == "cancelled"
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
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
