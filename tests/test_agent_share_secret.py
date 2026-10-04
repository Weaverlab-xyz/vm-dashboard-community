"""The SMB password the dashboard holds for an agent-brokered share, released per job.

docs/design/dashboard-workload-identity.md, Slice 5. A shares.yaml entry with
`dashboard_secret: true` has the agent fetch the share's SMB password through the same
route a hypervisor credential uses, POST /api/agent/jobs/{id}/secret. Pinned here:

  * a running agent_storage job, for THE configured agent and THE configured share, gets
    the password sealed to it — and only that job: another agent, another share, a body ref
    that differs from the job's, or a job no longer running gets nothing;
  * the share is derived from the job row, never chosen by the request;
  * no password held is a 409 naming the remedy, never an empty secret;
  * a bad reply key is refused before the password is read;
  * the SPIRE-only release setting applies here exactly as it does to connections;
  * the release is audited without the password;
  * the storage preflight refuses an agent too old to fetch it, when one is held;
  * /storage never sends the password back, and its mask means "keep".

Runs under pytest, or standalone:
    python tests/test_agent_share_secret.py
"""
import asyncio
import os
import sys
import tempfile
import uuid
from datetime import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="agent-share-secret-test-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDB}"
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-agent-share-secret-tests")

try:
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
from web_dashboard.api import agent as agent_api  # noqa: E402
from web_dashboard.api import storage as storage_api  # noqa: E402
from web_dashboard.database import (AuditLog, Base, Job, RemoteAgent,  # noqa: E402
                                    SessionLocal, engine, get_db)
from web_dashboard.services import (agent_sealing, agent_service,  # noqa: E402
                                    agent_signing, agent_storage_meta,
                                    agent_storage_service, config_service, job_service)

Base.metadata.create_all(bind=engine)

AUDIENCE = "https://agents.test"
PASSWORD = "Smb!Share#2026-pw"
SHARE = "corp-automation"


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


def _ready(version: str = "2.7.0", attested: bool = False) -> tuple:
    resp = CLIENT.post("/api/agents",
                       json={"name": f"agent-{uuid.uuid4().hex[:8]}", "site": "dc1"})
    assert resp.status_code == 201, resp.text
    private, public = agent_signing.generate_keypair()
    resp = CLIENT.post("/api/agent/enroll", json={
        "enrollment_code": resp.json()["enrollment_code"], "public_key": public,
        "agent_version": version, "policy_hash": "a" * 64})
    assert resp.status_code == 200, resp.text
    agent_id = resp.json()["agent_id"]
    if attested:
        db = SessionLocal()
        try:
            db.query(RemoteAgent).filter(RemoteAgent.id == agent_id).first().auth_mode = "spiffe"
            db.commit()
        finally:
            db.close()
    return agent_id, private


def _configure(agent_id: str, *, share: str = SHARE, password: str = PASSWORD,
               require_spire: bool = False) -> None:
    config_service.set("storage_agent_id", agent_id)
    config_service.set("storage_agent_share", share)
    config_service.set(agent_storage_service.SHARE_PASSWORD, password)
    config_service.set(agent_api.REQUIRE_SPIRE_FOR_SECRETS, "1" if require_spire else "0")


def _job(agent_id: str, *, share: str = SHARE, status: str = "running") -> str:
    db = SessionLocal()
    try:
        job = job_service.create_job(
            db, job_type="agent_storage", created_by="tester", agent_id=agent_id,
            metadata=agent_storage_meta.storage_meta("list", share=share))
        db.query(Job).filter(Job.id == job.id).first().status = status
        db.commit()
        return job.id
    finally:
        db.close()


def _fetch(private: str, agent_id: str, job_id: str, *, ref: str = SHARE,
           reply_key: str = None):
    reply_private, reply_public = agent_sealing.generate_reply_keypair()
    body = agent_signing.serialize({"connection_ref": ref,
                                    "reply_key": reply_public if reply_key is None
                                    else reply_key})
    path = f"/api/agent/jobs/{job_id}/secret"
    headers = agent_signing.sign_request(private, agent_id=agent_id, audience=AUDIENCE,
                                         method="POST", path=path, body=body)
    headers["Content-Type"] = "application/json"
    return CLIENT.request("POST", path, content=body, headers=headers), reply_private


# ── the release ───────────────────────────────────────────────────────────────

def test_the_configured_share_gets_its_password_sealed_to_the_job():
    agent_id, private = _ready()
    _configure(agent_id)
    job_id = _job(agent_id)
    resp, reply_private = _fetch(private, agent_id, job_id)
    assert resp.status_code == 200, resp.text
    assert PASSWORD not in resp.text
    assert resp.headers.get("cache-control") == "no-store"
    assert agent_sealing.open_sealed(reply_private, resp.json()["sealed"], agent_id=agent_id,
                                     audience=AUDIENCE, job_id=job_id, ref=SHARE) == PASSWORD


def test_another_agents_storage_job_gets_nothing():
    """The configured agent is one agent. A second agent with its own storage job for the
    same share name owns that job — and still gets no password."""
    configured, _ = _ready()
    other, other_private = _ready()
    _configure(configured)
    resp, _ = _fetch(other_private, other, _job(other))
    assert resp.status_code == 409 and PASSWORD not in resp.text


def test_a_share_other_than_the_configured_one_gets_nothing():
    agent_id, private = _ready()
    _configure(agent_id)
    job_id = _job(agent_id, share="other-share")
    resp, _ = _fetch(private, agent_id, job_id, ref="other-share")
    assert resp.status_code == 409 and PASSWORD not in resp.text


def test_the_share_is_derived_from_the_job_not_chosen_by_the_body():
    agent_id, private = _ready()
    _configure(agent_id)
    job_id = _job(agent_id, share="other-share")
    # The body names the configured share; the job is for another. Refused.
    resp, _ = _fetch(private, agent_id, job_id, ref=SHARE)
    assert resp.status_code == 409 and PASSWORD not in resp.text
    # And the reverse: the job is for the configured share, the body names another.
    resp, _ = _fetch(private, agent_id, _job(agent_id), ref="other-share")
    assert resp.status_code == 409 and "not the one this job was queued for" in resp.text


def test_a_job_that_is_no_longer_running_gets_nothing():
    agent_id, private = _ready()
    _configure(agent_id)
    resp, _ = _fetch(private, agent_id, _job(agent_id, status="cancelled"))
    assert resp.status_code == 409 and PASSWORD not in resp.text


def test_no_password_held_is_a_refusal_that_names_the_remedy():
    agent_id, private = _ready()
    _configure(agent_id, password="")
    resp, _ = _fetch(private, agent_id, _job(agent_id))
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert "/storage" in detail and "dashboard_secret" in detail


def test_a_bad_reply_key_is_refused_before_the_password_is_read():
    agent_id, private = _ready()
    _configure(agent_id)
    reads = []
    real_get = config_service.get

    def _spy(key, *a, **kw):
        reads.append(key)
        return real_get(key, *a, **kw)

    config_service.get = _spy
    try:
        resp, _ = _fetch(private, agent_id, _job(agent_id), reply_key="not-a-key")
    finally:
        config_service.get = real_get
    assert resp.status_code == 400
    assert agent_storage_service.SHARE_PASSWORD not in reads


def test_the_spire_only_setting_applies_to_shares_too():
    agent_id, private = _ready()
    _configure(agent_id, require_spire=True)
    resp, _ = _fetch(private, agent_id, _job(agent_id))
    assert resp.status_code == 403 and PASSWORD not in resp.text

    attested, attested_private = _ready(attested=True)
    _configure(attested, require_spire=True)
    resp, _ = _fetch(attested_private, attested, _job(attested))
    assert resp.status_code == 200, resp.text
    _configure(attested)


def test_the_release_is_audited_without_the_password():
    agent_id, private = _ready()
    _configure(agent_id)
    job_id = _job(agent_id)
    resp, _ = _fetch(private, agent_id, job_id)
    assert resp.status_code == 200
    db = SessionLocal()
    try:
        rows = db.query(AuditLog).filter(AuditLog.action == "agent.share_secret").all()
        blob = " ".join(str(r.details) for r in rows)
    finally:
        db.close()
    assert job_id in blob and SHARE in blob
    assert PASSWORD not in blob


# ── the preflight ─────────────────────────────────────────────────────────────

def _online(agent_id: str) -> None:
    db = SessionLocal()
    try:
        db.query(RemoteAgent).filter(RemoteAgent.id == agent_id).first().last_seen_at = \
            datetime.utcnow()
        db.commit()
    finally:
        db.close()


def _preflight():
    db = SessionLocal()
    try:
        return agent_storage_service._preflight(
            db, agent_storage_meta.storage_meta("list", share=SHARE))
    finally:
        db.close()


def test_an_agent_too_old_to_fetch_the_password_is_refused_before_queueing():
    agent_id, _ = _ready(version="2.6.0")
    _online(agent_id)
    _configure(agent_id)
    try:
        _preflight()
        raise AssertionError("queued for an agent that would send shares.yaml's password")
    except agent_storage_service.AgentStorageError as exc:
        assert "2.7" in str(exc) and "Nothing was queued" in str(exc)


def test_with_no_password_held_an_older_agent_still_queues():
    agent_id, _ = _ready(version="2.6.0")
    _online(agent_id)
    _configure(agent_id, password="")
    assert _preflight().id == agent_id


def test_a_current_agent_queues_with_a_password_held():
    agent_id, _ = _ready()
    _online(agent_id)
    _configure(agent_id)
    assert agent_service.supports_share_secret(_preflight())


# ── /storage never sends it back ──────────────────────────────────────────────

def test_storage_config_masks_the_password_and_the_mask_keeps_it():
    config_service.set(agent_storage_service.SHARE_PASSWORD, PASSWORD)
    out = asyncio.run(storage_api.get_config(current_user=_Admin()))
    assert out["storage_agent_password"] == storage_api._MASK
    assert PASSWORD not in str(out)

    patch = storage_api.StorageConfigPatch(storage_agent_password=storage_api._MASK)
    asyncio.run(storage_api.patch_config(patch, current_user=_Admin()))
    assert config_service.get(agent_storage_service.SHARE_PASSWORD) == PASSWORD

    patch = storage_api.StorageConfigPatch(storage_agent_password="")
    asyncio.run(storage_api.patch_config(patch, current_user=_Admin()))
    assert not config_service.get(agent_storage_service.SHARE_PASSWORD)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
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
