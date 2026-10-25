"""The dashboard half of remote-agent lifecycle: releases, gates, pause, rollouts.

  * Upgrade and restart are refused, each with the reason the operator acts on, unless
    the agent is SPIRE-attested, at least 2.10, its policy.yaml opts in, an updater is
    beside it, it is not paused, and no other control job is in flight.
  * The lease hands control jobs only to an attested agent, with exactly the payload the
    agent checks against its policy; a paused agent leases nothing but stays online.
  * A "completed" upgrade on any version but the target is recorded as failed.
  * A rollout upgrades the canary ring first, the default ring only after every canary
    completed, and stops on the first failure. Ineligible agents are listed, with why.
  * Docker Hub is read for the digest and the agent's version label.

Uses a temporary SQLite file and the real routes. Runs under pytest, or standalone:
    python tests/test_agent_lifecycle_api.py
"""
import json
import os
import sys
import tempfile
import uuid

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="agent-lifecycle-test-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDB}"
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-agent-lifecycle-tests")

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
except ModuleNotFoundError as exc:
    print(f"SKIP: {exc}")
    sys.exit(0)

from web_dashboard.database import (AgentRelease, AgentRollout, Base, Job,  # noqa: E402
                                    RemoteAgent, SessionLocal, engine, get_db)
from web_dashboard.api import agent as agent_api  # noqa: E402
from web_dashboard.services import (agent_lifecycle_service, agent_release_service,  # noqa: E402
                                    agent_signing, config_service, schedule_sweeper)

Base.metadata.create_all(bind=engine)
AUDIENCE = "https://agents.example.com"
DIGEST = "sha256:" + "b" * 64


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


def _signed(private, agent_id, path, payload=None):
    body = agent_signing.serialize(payload or {})
    headers = agent_signing.sign_request(private, agent_id=agent_id, audience=AUDIENCE,
                                        method="POST", path=path, body=body)
    headers["Content-Type"] = "application/json"
    return CLIENT.post(path, content=body, headers=headers)


def _edit(agent_id, **fields):
    db = SessionLocal()
    try:
        row = db.query(RemoteAgent).filter(RemoteAgent.id == agent_id).first()
        for k, v in fields.items():
            setattr(row, k, v)
        db.commit()
    finally:
        db.close()


def _agent(*, spiffe=True, version="2.10.0", upgrades=True, updater=True, ring=None):
    """An enrolled agent, then shaped as if it had attested and self-reported."""
    resp = CLIENT.post("/api/agents", json={"name": f"agent-{uuid.uuid4().hex[:8]}"})
    assert resp.status_code == 201, resp.text
    private, public = agent_signing.generate_keypair()
    resp = CLIENT.post("/api/agent/enroll", json={
        "enrollment_code": resp.json()["enrollment_code"], "public_key": public})
    assert resp.status_code == 200, resp.text
    agent_id = resp.json()["agent_id"]
    _edit(agent_id, auth_mode="spiffe" if spiffe else None, agent_version=version,
          control_upgrades=upgrades, control_updater=updater, upgrade_ring=ring)
    return agent_id, private


def _release(version="2.10.1", digest=DIGEST):
    resp = CLIENT.post("/api/agents/releases", json={"version": version, "digest": digest})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _job(job_id):
    db = SessionLocal()
    try:
        return db.query(Job).filter(Job.id == job_id).first()
    finally:
        db.close()


def _clear_control_jobs():
    db = SessionLocal()
    try:
        db.query(Job).filter(Job.job_type.in_(agent_lifecycle_service.CONTROL_JOB_TYPES)
                             ).update({Job.status: "completed"}, synchronize_session=False)
        db.query(AgentRollout).update({AgentRollout.status: "done"},
                                      synchronize_session=False)
        db.commit()
    finally:
        db.close()


RELEASE = None


def _rel():
    global RELEASE
    if RELEASE is None:
        RELEASE = _release()
    return RELEASE


# ── gates ─────────────────────────────────────────────────────────────────────

def test_each_unmet_gate_is_refused_with_what_to_do_next():
    rel = _rel()
    cases = [
        ({"spiffe": False}, "Migrate to SPIRE"),
        ({"version": "2.9.3"}, "by hand once"),
        ({"upgrades": False}, "policy.yaml"),
        ({"updater": None}, "updater container"),
    ]
    for kwargs, expect in cases:
        agent_id, _ = _agent(**kwargs)
        resp = CLIENT.post(f"/api/agents/{agent_id}/upgrade", json={"release_id": rel})
        assert resp.status_code == 409, (kwargs, resp.text)
        assert expect in resp.json()["detail"], (kwargs, resp.json())
        row = CLIENT.get(f"/api/agents/{agent_id}").json()
        assert expect in row["lifecycle_blocked"], row
        assert CLIENT.post(f"/api/agents/{agent_id}/restart").status_code == 409


def test_one_control_job_at_a_time_and_no_silent_downgrade():
    agent_id, _ = _agent()
    resp = CLIENT.post(f"/api/agents/{agent_id}/upgrade", json={"release_id": _rel()})
    assert resp.status_code == 202, resp.text
    again = CLIENT.post(f"/api/agents/{agent_id}/restart")
    assert again.status_code == 409 and "already queued" in again.json()["detail"]

    newer_id, _ = _agent(version="2.11.0")
    resp = CLIENT.post(f"/api/agents/{newer_id}/upgrade", json={"release_id": _rel()})
    assert resp.status_code == 409 and "downgrade needs force" in resp.json()["detail"]
    resp = CLIENT.post(f"/api/agents/{newer_id}/upgrade",
                       json={"release_id": _rel(), "force": True})
    assert resp.status_code == 202, resp.text


def test_a_scheduled_upgrade_waits_for_its_time():
    agent_id, private = _agent()
    from datetime import timedelta
    from web_dashboard.utils.clock import utcnow
    when = (utcnow() + timedelta(days=30)).strftime("%Y-%m-%dT02:00")
    resp = CLIENT.post(f"/api/agents/{agent_id}/upgrade",
                       json={"release_id": _rel(), "run_at": when, "run_timezone": "UTC"})
    assert resp.status_code == 202, resp.text
    assert resp.json()["scheduled_for"].startswith(when[:10])
    assert _signed(private, agent_id, "/api/agent/lease").json()["job"] is None


# ── the lease ─────────────────────────────────────────────────────────────────

def test_the_lease_carries_exactly_what_the_agent_checks():
    agent_id, private = _agent()
    job_id = CLIENT.post(f"/api/agents/{agent_id}/upgrade",
                         json={"release_id": _rel()}).json()["job_id"]
    out = _signed(private, agent_id, "/api/agent/lease").json()
    assert out["job"]["job_id"] == job_id and out["job"]["job_type"] == "agent_upgrade"
    assert out["job"]["payload"] == {"repository": "chrweav/dashboard-agent",
                                     "digest": DIGEST, "version": "2.10.1"}


def test_an_unattested_agent_is_never_handed_a_control_job():
    agent_id, private = _agent(spiffe=False)
    db = SessionLocal()
    try:
        db.add(Job(id=str(uuid.uuid4()), job_type="agent_restart", status="queued",
                   agent_id=agent_id, extra_data=json.dumps({})))
        db.commit()
    finally:
        db.close()
    assert _signed(private, agent_id, "/api/agent/lease").json()["job"] is None


def test_a_concurrent_agent_gets_a_control_job_only_when_it_asks_for_one():
    agent_id, private = _agent()
    CLIENT.post(f"/api/agents/{agent_id}/restart")
    busy = _signed(private, agent_id, "/api/agent/lease",
                   {"want_job_types": ["agent_ansible"]}).json()
    assert busy["job"] is None
    free = _signed(private, agent_id, "/api/agent/lease",
                   {"want_job_types": ["agent_ansible", "agent_restart"]}).json()
    assert free["job"]["job_type"] == "agent_restart"


def test_the_self_report_records_the_hosts_opt_in():
    agent_id, private = _agent(upgrades=None, updater=None)
    _signed(private, agent_id, "/api/agent/lease",
            {"agent_version": "2.10.0",
             "control": {"upgrades_enabled": True, "updater_present": False}})
    row = CLIENT.get(f"/api/agents/{agent_id}").json()
    assert row["control_upgrades"] is True and row["control_updater"] is False
    assert "updater" in row["lifecycle_blocked"]
    # An older agent sends no `control`: left as it was.
    _signed(private, agent_id, "/api/agent/lease", {"agent_version": "2.10.0"})
    assert CLIENT.get(f"/api/agents/{agent_id}").json()["control_upgrades"] is True


def test_a_paused_agent_heartbeats_but_leases_nothing():
    agent_id, private = _agent(spiffe=False, version="2.4.0", upgrades=None, updater=None)
    db = SessionLocal()
    try:
        db.add(Job(id=str(uuid.uuid4()), job_type="agent_discover", status="queued",
                   agent_id=agent_id, extra_data=json.dumps({})))
        db.commit()
    finally:
        db.close()
    assert CLIENT.post(f"/api/agents/{agent_id}/pause").json()["paused"] is True
    resp = _signed(private, agent_id, "/api/agent/lease")
    assert resp.status_code == 200 and resp.json()["job"] is None
    assert CLIENT.get(f"/api/agents/{agent_id}").json()["status"] == "online"
    assert CLIENT.post(f"/api/agents/{agent_id}/resume").json()["paused"] is False
    assert _signed(private, agent_id, "/api/agent/lease").json()["job"] is not None


# ── completion ────────────────────────────────────────────────────────────────

def _lease_upgrade():
    agent_id, private = _agent()
    job_id = CLIENT.post(f"/api/agents/{agent_id}/upgrade",
                         json={"release_id": _rel()}).json()["job_id"]
    assert _signed(private, agent_id, "/api/agent/lease").json()["job"]["job_id"] == job_id
    return agent_id, private, job_id


def test_the_successor_completes_on_the_target_version():
    agent_id, private, job_id = _lease_upgrade()
    resp = _signed(private, agent_id, f"/api/agent/jobs/{job_id}/complete",
                   {"status": "completed",
                    "result": {"version": "2.10.1", "outcome": "upgraded"}})
    assert resp.status_code == 200, resp.text
    assert _job(job_id).status == "completed"


def test_completed_on_the_wrong_version_is_a_failure():
    agent_id, private, job_id = _lease_upgrade()
    _signed(private, agent_id, f"/api/agent/jobs/{job_id}/complete",
            {"status": "completed", "result": {"version": "2.10.0"}})
    job = _job(job_id)
    assert job.status == "failed" and "not 2.10.1" in job.error_message


# ── releases ──────────────────────────────────────────────────────────────────

def test_a_release_needs_a_version_and_a_digest_not_a_tag():
    for body in ({"version": "latest", "digest": DIGEST},
                 {"version": "2.10.9", "digest": "latest"}):
        assert CLIENT.post("/api/agents/releases", json=body).status_code == 400
    _release("2.10.7", "sha256:" + "c" * 64)
    clash = CLIENT.post("/api/agents/releases",
                        json={"version": "2.10.7", "digest": "sha256:" + "d" * 64})
    assert clash.status_code == 400 and "already recorded" in clash.json()["detail"]
    versions = [r["version"] for r in CLIENT.get("/api/agents/releases").json()["releases"]]
    assert versions == sorted(versions, key=agent_lifecycle_service.version_tuple,
                              reverse=True)


class _Resp:
    def __init__(self, doc, headers=None):
        self._doc, self.headers = doc, headers or {}

    def json(self):
        return self._doc

    def raise_for_status(self):
        return None


class _Hub:
    """Docker Hub's token, index, platform manifest and config blob."""
    def __init__(self):
        self.urls = []

    def get(self, url, **kw):
        self.urls.append(url)
        return _Resp({"token": "t"})

    def request(self, method, url, **kw):
        self.urls.append(url)
        if url.endswith("/manifests/latest"):
            return _Resp({"manifests": [
                {"digest": "sha256:att", "platform": {"os": "unknown"}},
                {"digest": "sha256:amd", "platform": {"os": "linux"}}]},
                {"Docker-Content-Digest": "sha256:" + "e" * 64})
        if url.endswith("/manifests/sha256:amd"):
            return _Resp({"config": {"digest": "sha256:cfg"}})
        if url.endswith("/blobs/sha256:cfg"):
            return _Resp({"config": {"Labels": {"dashboard-agent.version": "2.12.0"}}})
        raise AssertionError(url)


def test_docker_hub_gives_the_index_digest_and_the_version_label():
    db = SessionLocal()
    try:
        hub = _Hub()
        row = agent_release_service.check_registry(db, actor="t", session=hub)
        assert (row.version, row.digest, row.source) == ("2.12.0", "sha256:" + "e" * 64,
                                                        "registry")
        assert not any("sha256:att" in u for u in hub.urls)
        db.delete(row)
        db.commit()
    finally:
        db.close()


# ── rollouts ──────────────────────────────────────────────────────────────────

def _finish(job_id, status):
    db = SessionLocal()
    try:
        db.query(Job).filter(Job.id == job_id).update({Job.status: status},
                                                       synchronize_session=False)
        db.commit()
    finally:
        db.close()


def _advance():
    db = SessionLocal()
    try:
        return agent_lifecycle_service.advance_rollouts(db)
    finally:
        db.close()


def _fresh_fleet():
    """Retire every earlier test's agent, so a rollout sees only this fleet."""
    _clear_control_jobs()
    db = SessionLocal()
    try:
        db.query(RemoteAgent).update({RemoteAgent.is_active: False},
                                     synchronize_session=False)
        db.commit()
    finally:
        db.close()


def test_a_rollout_holds_the_default_ring_until_the_canary_is_up():
    _fresh_fleet()
    rel = _release("2.10.2", "sha256:" + "f" * 64)
    canary, _ = _agent(ring="canary")
    rest, _ = _agent()
    stale, _ = _agent(spiffe=False)
    out = CLIENT.post("/api/agents/rollouts", json={"release_id": rel}).json()
    assert out["status"] == "canary", out
    assert [(j["ring"], j["status"]) for j in out["jobs"]] == [("canary", "queued")], out
    assert any("Migrate to SPIRE" in why for why in out["skipped"].values()), out
    with SessionLocal() as db:
        assert schedule_sweeper.has_work(db)

    _advance()
    assert CLIENT.get(f"/api/agents/rollouts/{out['id']}").json()["status"] == "canary"

    _finish(out["jobs"][0]["job_id"], "completed")
    _advance()
    now = CLIENT.get(f"/api/agents/rollouts/{out['id']}").json()
    assert now["status"] == "default"
    assert [j["ring"] for j in now["jobs"]] == ["canary", "default"]
    _finish(now["jobs"][1]["job_id"], "completed")
    _advance()
    assert CLIENT.get(f"/api/agents/rollouts/{out['id']}").json()["status"] == "done"


def test_a_failed_canary_stops_the_rollout():
    _fresh_fleet()
    rel = _release("2.10.3", "sha256:" + "1" * 64)
    _agent(ring="canary")
    _agent()
    out = CLIENT.post("/api/agents/rollouts", json={"release_id": rel}).json()
    _finish(out["jobs"][0]["job_id"], "failed")
    _advance()
    now = CLIENT.get(f"/api/agents/rollouts/{out['id']}").json()
    assert now["status"] == "stopped" and "failed" in now["detail"]
    assert [j["ring"] for j in now["jobs"]] == ["canary"]


def test_the_ring_is_set_through_patch():
    agent_id, _ = _agent()
    assert CLIENT.patch(f"/api/agents/{agent_id}",
                        json={"upgrade_ring": "canary"}).json()["upgrade_ring"] == "canary"
    assert CLIENT.patch(f"/api/agents/{agent_id}",
                        json={"upgrade_ring": "first"}).status_code == 400


def test_no_canary_means_the_default_ring_straight_away():
    _fresh_fleet()
    rel = _release("2.10.4", "sha256:" + "2" * 64)
    _agent()
    out = CLIENT.post("/api/agents/rollouts", json={"release_id": rel}).json()
    assert out["status"] == "default" and [j["ring"] for j in out["jobs"]] == ["default"]


def test_the_release_table_is_its_own_and_the_migration_is_recorded():
    from web_dashboard import schema_migrations as sm
    assert AgentRelease.__tablename__ == "agent_releases"
    mig = [m for m in sm.MIGRATIONS if m.name == "0005_agent_lifecycle"]
    assert len(mig) == 1 and len(mig[0].steps) == 4


if __name__ == "__main__":
    fns = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
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
