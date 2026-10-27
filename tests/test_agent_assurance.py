"""Ongoing checks on a remote agent after it attests (agent_assurance_service, agent 2.11).

  * clone: a request signed by the key the last attestation replaced is refused and raises
    "two copies are running", once; outside the window it is an ordinary refusal.
  * policy_changed: from an attestation or a 2.11 lease self-report.
  * unsigned_version: a 2.10+ version that is not a recorded release, only when releases
    are recorded.
  * stale_key: an online attested 2.11 agent that has not re-attested in twice its interval.
  * Each is audited, notified as agent.attention, shown on the row, cleared by acknowledging.
  * The agent re-attests between jobs only, keeps its key when that fails, and reports
    its interval and policy hash on the lease.

Run: python tests/test_agent_assurance.py   (or under pytest)
"""
import importlib.util
import json
import os
import sys
import tempfile
import time
import uuid
from datetime import timedelta
from types import SimpleNamespace
from urllib.parse import urlparse

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="agent-assurance-test-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDB}"
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-agent-assurance-tests")
os.environ["AGENT_STATE_DIR"] = tempfile.mkdtemp(prefix="agent-assurance-state-")

try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from jose import jwk, jwt
except ModuleNotFoundError as exc:  # pragma: no cover
    print(f"SKIP: {exc}")
    sys.exit(0)

from web_dashboard.database import (AgentRelease, Base, RemoteAgent,  # noqa: E402
                                    SessionLocal, SpiffeTrustDomain, engine, get_db)
from web_dashboard.api import agent as agent_api  # noqa: E402
from web_dashboard.services import (agent_assurance_service, agent_service,  # noqa: E402
                                    agent_signing, config_service, notification_service)
from web_dashboard.utils.clock import utcnow  # noqa: E402

Base.metadata.create_all(bind=engine)

AUDIENCE = "https://agents.test"
ATTEST_AUD = AUDIENCE + "/api/agent/attest"
TD = "dash.test"

_spec = importlib.util.spec_from_file_location(
    "agent_runner_assurance", os.path.join(_ROOT, "runners", "agent", "agent.py"))
agent_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(agent_mod)
agent_mod.POLICY = SimpleNamespace(digest="b" * 64, job_types=set(), ansible_max_concurrent=1)

NOTIFIED = []
_real_emit = notification_service.emit_safe
notification_service.emit_safe = lambda db, event: NOTIFIED.append(event) or 1


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

_KEY = ec.generate_private_key(ec.SECP256R1())
_PRIV = _KEY.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                           serialization.NoEncryption()).decode()
_PUB = _KEY.public_key().public_bytes(serialization.Encoding.PEM,
                                      serialization.PublicFormat.SubjectPublicKeyInfo).decode()
_JWK = jwk.construct(_PUB, "ES256").to_dict()
_JWK.update(kid="k1", use="jwt-svid")
_JWK.pop("alg", None)
with SessionLocal() as _db:
    if not _db.query(SpiffeTrustDomain).filter(SpiffeTrustDomain.trust_domain == TD).first():
        _db.add(SpiffeTrustDomain(trust_domain=TD, bundle_json=json.dumps({"keys": [_JWK]})))
        _db.commit()


def _svid(sub: str) -> str:
    now = int(time.time())
    return jwt.encode({"sub": sub, "aud": ATTEST_AUD, "iat": now, "exp": now + 300,
                       "jti": uuid.uuid4().hex}, _PRIV, algorithm="ES256",
                      headers={"kid": "k1"})


def _bound_agent() -> tuple:
    resp = CLIENT.post("/api/agents", json={"name": f"a-{uuid.uuid4().hex[:8]}"})
    assert resp.status_code == 201, resp.text
    agent_id = resp.json()["id"]
    sid = f"spiffe://{TD}/agent/{agent_id}"
    assert CLIENT.post(f"/api/agents/{agent_id}/spiffe-id",
                       json={"spiffe_id": sid}).status_code == 200
    return agent_id, sid


class _Session:
    def __init__(self):
        self.trust_env = True
        self.verify = True

    def request(self, method, url, data=None, headers=None, timeout=None, allow_redirects=False):
        return CLIENT.request(method, urlparse(url).path, content=data, headers=headers)


def _attested(sid: str):
    """A real agent Dashboard client, attested with its own fresh in-memory key."""
    d = agent_mod.Dashboard(AUDIENCE)
    d.session = _Session()
    tokfile = os.path.join(tempfile.mkdtemp(), "attest.jwt")
    with open(tokfile, "w", encoding="ascii") as fh:
        fh.write(_svid(sid))
    d.identity = d.attest(tokfile)
    return d


def _row(agent_id: str) -> RemoteAgent:
    with SessionLocal() as db:
        return db.query(RemoteAgent).filter(RemoteAgent.id == agent_id).first()


def _kinds(agent_id: str) -> list:
    return [i["kind"] for i in CLIENT.get(f"/api/agents/{agent_id}").json()["attention"]]


def _edit(agent_id, **fields):
    with SessionLocal() as db:
        row = db.query(RemoteAgent).filter(RemoteAgent.id == agent_id).first()
        for k, v in fields.items():
            setattr(row, k, v)
        db.commit()


# ── clone ─────────────────────────────────────────────────────────────────────

def test_the_older_copy_signing_with_the_replaced_key_is_caught_once():
    agent_id, sid = _bound_agent()
    first = _attested(sid)
    assert first.lease() is None
    second = _attested(sid)                  # another copy attests as the same agent
    assert second.lease() is None, "the newer copy works"
    for _ in range(2):
        try:
            first.lease()
        except agent_mod.AgentFatal:
            pass
        else:
            raise AssertionError("the replaced key was accepted")
    assert _kinds(agent_id) == ["clone"], "flagged once, not once per request"
    assert any(e.event_type == "agent.attention" and "Two copies" in e.title for e in NOTIFIED)


def test_an_old_key_outside_the_window_is_an_ordinary_refusal():
    agent_id, sid = _bound_agent()
    first = _attested(sid)
    _attested(sid)
    _edit(agent_id, previous_key_until=utcnow() - timedelta(seconds=1))
    try:
        first.lease()
    except agent_mod.AgentFatal:
        pass
    assert _kinds(agent_id) == []


def test_a_restart_with_nothing_left_running_is_not_a_clone():
    agent_id, sid = _bound_agent()
    _attested(sid).lease()
    _attested(sid).lease()                   # the restarted process; the old one is gone
    assert _kinds(agent_id) == []


# ── policy, version ───────────────────────────────────────────────────────────

def test_a_changed_policy_is_flagged_from_attest_and_from_the_lease():
    agent_id, sid = _bound_agent()
    _attested(sid)
    agent_mod.POLICY.digest = "d" * 64
    try:
        d = _attested(sid)
        assert _kinds(agent_id) == ["policy_changed"]
        CLIENT.post(f"/api/agents/{agent_id}/acknowledge")
        agent_mod.POLICY.digest = "e" * 64
        d.lease()                            # 2.11 reports the hash on every lease
        assert _kinds(agent_id) == ["policy_changed"]
        assert _row(agent_id).policy_hash == "e" * 64
    finally:
        agent_mod.POLICY.digest = "b" * 64


def test_an_unrecorded_signed_era_version_is_flagged_only_when_releases_exist():
    agent_id, sid = _bound_agent()
    d = _attested(sid)
    assert _kinds(agent_id) == [], "no releases recorded: nothing to compare with"
    with SessionLocal() as db:
        db.add(AgentRelease(version=agent_mod.AGENT_VERSION, digest="sha256:" + "1" * 64))
        db.commit()
    try:
        d.lease()
        assert _kinds(agent_id) == [], "a recorded release is fine"
        with SessionLocal() as db:
            row = db.query(RemoteAgent).filter(RemoteAgent.id == agent_id).first()
            agent_service.record_self_report(db, row, agent_version="2.9.4")
            assert agent_assurance_service.items(row) == [], "pre-signing builds are not flagged"
            agent_service.record_self_report(db, row, agent_version="2.15.0")
            assert [i["kind"] for i in agent_assurance_service.items(row)] == ["unsigned_version"]
    finally:
        with SessionLocal() as db:
            db.query(AgentRelease).delete()
            db.commit()


# ── stale key ─────────────────────────────────────────────────────────────────

def test_an_online_agent_that_stopped_rotating_is_flagged():
    cases = {"stale": ("2.11.0", 3, True), "old_build": ("2.10.0", 3, False),
             "offline": ("2.11.0", 3, False), "fresh": ("2.11.0", 0.5, False)}
    ids = {}
    for name, (version, days, _) in cases.items():
        agent_id, _sid = _bound_agent()
        _edit(agent_id, auth_mode="spiffe", agent_version=version, reported_reattest_hours=24,
              last_attest_at=utcnow() - timedelta(days=days),
              last_seen_at=utcnow() - timedelta(hours=2 if name == "offline" else 0))
        ids[name] = agent_id
    with SessionLocal() as db:
        assert agent_assurance_service.has_work(db)
        agent_assurance_service.sweep(db)
        agent_assurance_service.sweep(db)
    for name, (_, _, expect) in cases.items():
        assert (_kinds(ids[name]) == ["stale_key"]) is expect, (name, _kinds(ids[name]))
    for agent_id in ids.values():
        _edit(agent_id, is_active=False)


# ── acknowledging ─────────────────────────────────────────────────────────────

def test_acknowledging_clears_the_items_and_is_audited():
    from web_dashboard.database import AuditLog
    agent_id, sid = _bound_agent()
    with SessionLocal() as db:
        row = db.query(RemoteAgent).filter(RemoteAgent.id == agent_id).first()
        agent_assurance_service.flag(db, row, "policy_changed", "test")
    row = CLIENT.post(f"/api/agents/{agent_id}/acknowledge").json()
    assert row["attention"] == []
    with SessionLocal() as db:
        actions = [a.action for a in db.query(AuditLog).all()]
    assert "agent.attention" in actions and "agent.acknowledge" in actions


def test_agent_attention_is_a_default_notification_with_its_config_twin():
    from web_dashboard.config import Settings
    from web_dashboard.services import notify_policy
    assert notify_policy.EVENT_SEVERITY["agent.attention"] == "warning"
    assert "agent.attention" in notify_policy.DEFAULT_EVENT_TYPES
    assert Settings.model_fields["notify_event_types"].default == notify_policy.DEFAULT_EVENT_TYPES


# ── the agent: re-attesting while running ─────────────────────────────────────

class _Slots:
    def __init__(self, busy=False):
        self._busy = busy

    def busy(self):
        return self._busy


class _Dash:
    def __init__(self, fail=False):
        self.identity = "old"
        self.calls = 0
        self.fail = fail

    def attest(self, jwt_file):
        self.calls += 1
        if self.fail:
            raise agent_mod.AgentFatal("attestation refused")
        return f"new-{self.calls}"


def test_the_agent_re_attests_between_jobs_and_keeps_its_key_on_failure():
    agent_mod.SPIFFE_JWT_FILE, agent_mod.REATTEST_HOURS = "/run/spiffe/jwt/attest.jwt", 24.0
    try:
        state = {"next": 1000.0, "waiting_since": None}
        d = _Dash()
        assert agent_mod.maybe_reattest(d, _Slots(), state, now=999.0) is False
        assert d.calls == 0, "not due yet"
        assert agent_mod.maybe_reattest(d, _Slots(busy=True), state, now=1000.0) is True
        assert d.calls == 0, "a job is running: wait, leasing nothing new"
        assert agent_mod.maybe_reattest(d, _Slots(), state, now=1010.0) is False
        assert d.identity == "new-1" and state["next"] == 1010.0 + 24 * 3600

        failing = _Dash(fail=True)
        state = {"next": 0.0, "waiting_since": None}
        assert agent_mod.maybe_reattest(failing, _Slots(), state, now=50.0) is False
        assert failing.identity == "old" and state["next"] == 50.0 + 3600

        # A job that will not finish does not hold leases forever.
        state = {"next": 0.0, "waiting_since": None}
        assert agent_mod.maybe_reattest(d, _Slots(busy=True), state, now=10.0) is True
        assert agent_mod.maybe_reattest(
            d, _Slots(busy=True), state, now=10.0 + agent_mod.REATTEST_MAX_WAIT_S) is False
        assert state["next"] == 10.0 + agent_mod.REATTEST_MAX_WAIT_S + 3600
    finally:
        agent_mod.SPIFFE_JWT_FILE, agent_mod.REATTEST_HOURS = "", 24.0


def test_off_means_never_scheduled_and_the_lease_reports_the_interval():
    assert agent_mod.maybe_reattest(_Dash(), _Slots(), {"next": None}, now=1e12) is False
    sent = []
    d = agent_mod.Dashboard(AUDIENCE)
    d.identity = SimpleNamespace(agent_id="x", audience=AUDIENCE, sign=lambda m: "s")

    class _Resp:
        status_code = 200

        def json(self):
            return {"job": None}

        def raise_for_status(self):
            return None

    d._request = lambda method, path, body=None, **kw: sent.append(body) or _Resp()
    agent_mod.SPIFFE_JWT_FILE = "/run/spiffe/jwt/attest.jwt"
    try:
        d.lease()
    finally:
        agent_mod.SPIFFE_JWT_FILE = ""
    assert sent[0]["reattest_hours"] == 24 and sent[0]["policy_hash"] == "b" * 64
    assert agent_mod._version_tuple(agent_mod.AGENT_VERSION) >= (2, 11, 0)


def test_the_migration_is_recorded():
    from web_dashboard import schema_migrations as sm
    mig = [m for m in sm.MIGRATIONS if m.name == "0006_agent_assurance"]
    assert len(mig) == 1 and len(mig[0].steps) == 6


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
    notification_service.emit_safe = _real_emit
    sys.exit(1 if failures else 0)
