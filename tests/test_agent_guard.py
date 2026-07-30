"""Behaviour of the remote-agent throttle, against a real SQLite database.

``/api/agent/*`` is the only router this dashboard publishes to a network it does not
own, and before ``agent_guard`` it had no rate limit of any kind — the SlowAPI limiter in
``main.py`` is inert by design. These tests pin the properties that make the guard worth
having, each of which is a bug that reads as working software if it regresses:

  * **A 429 is not a 401.** The runner treats 401 as revocation and exits for good, so
    conflating the two would turn a load spike into a permanently dead fleet.
  * **The window actually reopens.** If a throttled request counted against its own cap,
    the window would slide forward on every retry and the block would never lift — a
    lockout dressed up as a sliding window.
  * **One agent's flood is one agent's problem.** The cap is keyed on the authenticated
    agent id, so a noisy site must not throttle a quiet one.
  * **Signature first, throttle second.** An unauthenticated caller who guesses an agent
    id must not be able to spend that agent's budget.

Uses a temporary SQLite file and the real ORM. Skips if app dependencies are absent.
Runs under pytest, or standalone:
    python tests/test_agent_guard.py
"""
import os
import sys
import tempfile
import uuid
from datetime import datetime, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="agent-guard-test-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDB}"
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-agent-guard-tests")

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from web_dashboard.database import (AgentEnrollAttempt, AgentNonce, Base,
                                        SessionLocal, engine, get_db)
    from web_dashboard.api import agent as agent_api
    from web_dashboard.services import (agent_guard, agent_service, agent_signing,
                                        config_service)
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
    username = "tester"
    is_admin = True
    is_effective_admin = True


def _app() -> TestClient:
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
    config_service.set(agent_api._AUDIENCE_CONFIG, AUDIENCE)
    return TestClient(app)


CLIENT = _app()


# ── helpers ───────────────────────────────────────────────────────────────────

def _ready() -> tuple[str, str]:
    """A registered, enrolled agent. Returns (agent_id, private key)."""
    resp = CLIENT.post("/api/agent",
                       json={"name": f"agent-{uuid.uuid4().hex[:8]}", "site": "dc1"})
    assert resp.status_code == 201, resp.text
    code = resp.json()["enrollment_code"]
    private, public = agent_signing.generate_keypair()
    resp = CLIENT.post("/api/agent/enroll",
                       json={"enrollment_code": code, "public_key": public})
    assert resp.status_code == 200, resp.text
    return resp.json()["agent_id"], private


def _lease(private: str, agent_id: str):
    body = agent_signing.serialize({})
    headers = agent_signing.sign_request(private, agent_id=agent_id, audience=AUDIENCE,
                                        method="POST", path="/api/agent/lease", body=body)
    headers["Content-Type"] = "application/json"
    return CLIENT.post("/api/agent/lease", content=body, headers=headers)


def _set_cap(value) -> None:
    config_service.set("agent_max_requests_per_minute", str(value))


def _nonce_count(agent_id: str) -> int:
    db = SessionLocal()
    try:
        return db.query(AgentNonce).filter(AgentNonce.agent_id == agent_id).count()
    finally:
        db.close()


def _clear_enroll_attempts() -> None:
    db = SessionLocal()
    try:
        db.query(AgentEnrollAttempt).delete()
        db.commit()
    finally:
        db.close()


# ── the per-agent request cap ─────────────────────────────────────────────────

def test_an_agent_over_its_cap_gets_429_with_retry_after():
    agent_id, private = _ready()
    _set_cap(3)
    try:
        # Enrolment does not sign, so the budget starts empty: three succeed.
        for i in range(3):
            assert _lease(private, agent_id).status_code == 200, f"request {i}"
        resp = _lease(private, agent_id)
        assert resp.status_code == 429, resp.text
        # Without this header a backing-off agent has to guess, and a fleet throttled
        # together would all guess the same interval and return in lockstep.
        assert int(resp.headers["Retry-After"]) >= 1
    finally:
        _set_cap("")


def test_a_429_is_never_a_401():
    """The runner exits for good on 401 and backs off on 429. Conflating them would take
    a whole fleet down permanently the first time it got busy."""
    agent_id, private = _ready()
    _set_cap(1)
    try:
        assert _lease(private, agent_id).status_code == 200
        assert _lease(private, agent_id).status_code == 429
    finally:
        _set_cap("")

    # And the genuinely unauthenticated case still answers 401, not 429.
    other_private, _ = agent_signing.generate_keypair()
    assert _lease(other_private, agent_id).status_code == 401


def test_a_throttled_request_does_not_spend_the_budget_it_was_refused_by():
    """The check sits before the nonce insert, so a throttled flood stops growing
    `agent_nonces` at the cap. If it did not, every retry would push the window forward
    and the block would never lift."""
    agent_id, private = _ready()
    _set_cap(2)
    try:
        for _ in range(2):
            assert _lease(private, agent_id).status_code == 200
        for _ in range(5):
            assert _lease(private, agent_id).status_code == 429
        assert _nonce_count(agent_id) == 2
    finally:
        _set_cap("")


def test_one_agents_flood_does_not_throttle_another():
    noisy_id, noisy_key = _ready()
    quiet_id, quiet_key = _ready()
    _set_cap(2)
    try:
        for _ in range(2):
            assert _lease(noisy_key, noisy_id).status_code == 200
        assert _lease(noisy_key, noisy_id).status_code == 429
        # The cap is keyed on the authenticated agent id, not the address — and every
        # request in this test arrives from the same test client address.
        assert _lease(quiet_key, quiet_id).status_code == 200
    finally:
        _set_cap("")


def test_a_bad_signature_cannot_spend_an_agents_budget():
    """Throttle after the signature, exactly like the replay check: otherwise anyone who
    can guess an agent id locks it out of its own queue without authenticating."""
    agent_id, private = _ready()
    wrong_key, _ = agent_signing.generate_keypair()
    _set_cap(2)
    try:
        for _ in range(6):
            assert _lease(wrong_key, agent_id).status_code == 401
        # Budget untouched by the six refusals.
        for _ in range(2):
            assert _lease(private, agent_id).status_code == 200
    finally:
        _set_cap("")


def test_a_zero_cap_disables_the_check():
    agent_id, private = _ready()
    _set_cap(0)
    try:
        for _ in range(8):
            assert _lease(private, agent_id).status_code == 200
    finally:
        _set_cap("")


def test_the_window_is_clamped_to_how_long_nonces_survive():
    """The counter reads `agent_nonces`, which `sweep_nonces` prunes at
    SIGNATURE_WINDOW_SECONDS * 4. Counting over a longer window would silently
    under-report, leaving a throttle that looks configured and does nothing."""
    assert agent_guard._request_window().total_seconds() <= \
        agent_signing.SIGNATURE_WINDOW_SECONDS * 4


# ── the enrolment cap ─────────────────────────────────────────────────────────

def test_enrolment_failures_are_throttled_and_successes_are_not():
    _clear_enroll_attempts()
    db = SessionLocal()
    try:
        cap = agent_guard.DEFAULT_ENROLL_MAX_PER_IP
        for _ in range(cap):
            agent_guard.record_enroll_failure(db, ip="10.0.0.9")
        try:
            agent_guard.check_enroll(db, ip="10.0.0.9")
            assert False, "expected the cap to engage"
        except agent_guard.AgentThrottled as exc:
            assert exc.scope == "ip"
            assert exc.retry_after >= 1
        # A different address is unaffected by this one's failures.
        agent_guard.check_enroll(db, ip="10.0.0.10")
    finally:
        db.close()
        _clear_enroll_attempts()


def test_the_enrolment_window_reopens_on_its_own():
    """A sliding window, not a lockout: anyone able to reach the endpoint could otherwise
    keep a real agent from ever enrolling."""
    _clear_enroll_attempts()
    db = SessionLocal()
    try:
        stale = datetime.utcnow() - timedelta(
            minutes=agent_guard.DEFAULT_ENROLL_WINDOW_MINUTES + 1)
        for _ in range(agent_guard.DEFAULT_ENROLL_MAX_PER_IP + 5):
            db.add(AgentEnrollAttempt(ip="10.0.0.11", attempted_at=stale))
        db.commit()
        agent_guard.check_enroll(db, ip="10.0.0.11")   # aged out; no refusal
    finally:
        db.close()
        _clear_enroll_attempts()


def test_a_bad_enrolment_code_is_recorded_and_a_good_one_is_not():
    _clear_enroll_attempts()
    private, public = agent_signing.generate_keypair()
    resp = CLIENT.post("/api/agent/enroll",
                       json={"enrollment_code": "agte_" + "0" * 64,
                             "public_key": public})
    assert resp.status_code == 400, resp.text

    db = SessionLocal()
    try:
        assert db.query(AgentEnrollAttempt).count() == 1
    finally:
        db.close()

    _ready()   # a successful enrolment
    db = SessionLocal()
    try:
        assert db.query(AgentEnrollAttempt).count() == 1, \
            "a successful enrolment must not consume the failure budget"
    finally:
        db.close()
        _clear_enroll_attempts()


def test_throttled_is_not_an_agent_error():
    """AgentError means 401 at the endpoint. If AgentThrottled inherited from it, every
    throttle would reach the agent as a revocation."""
    assert not issubclass(agent_guard.AgentThrottled, agent_service.AgentError)


# ── the audience may only be pinned by a trusted caller ───────────────────────

def test_an_unauthenticated_request_cannot_pin_the_signing_audience():
    """Pinning writes the value every future signature is checked against, permanently.
    If an unauthenticated poll could do it, the first stranger to reach /lease with a
    forged Host would 401 every real agent until an operator cleared the config by hand.
    """
    agent_id, private = _ready()
    config_service.set(agent_api._AUDIENCE_CONFIG, "")
    try:
        _lease(private, agent_id)      # unauthenticated as far as pinning is concerned
        assert not (config_service.get(agent_api._AUDIENCE_CONFIG) or ""), \
            "a signature-path request pinned the audience"
    finally:
        config_service.set(agent_api._AUDIENCE_CONFIG, AUDIENCE)


def test_an_operator_minting_a_code_does_pin_it():
    config_service.set(agent_api._AUDIENCE_CONFIG, "")
    try:
        resp = CLIENT.post("/api/agent",
                           json={"name": f"agent-{uuid.uuid4().hex[:8]}"})
        assert resp.status_code == 201, resp.text
        assert config_service.get(agent_api._AUDIENCE_CONFIG)
    finally:
        config_service.set(agent_api._AUDIENCE_CONFIG, AUDIENCE)


def test_the_install_hint_offers_a_code_file_form_and_absolute_bind_paths():
    resp = CLIENT.post("/api/agent", json={"name": f"agent-{uuid.uuid4().hex[:8]}"})
    assert resp.status_code == 201, resp.text
    install = resp.json()["install"]
    code = resp.json()["enrollment_code"]

    # `docker run -v` rejects a relative source path outright, so the command as emitted
    # has to use an absolute one or it cannot run at all.
    assert "-v ./policy.yaml" not in install["docker_run"]
    assert "$PWD/policy.yaml" in install["docker_run"]

    alt = install["docker_run_code_file"]
    assert "AGENT_ENROLLMENT_CODE_FILE=" in alt
    assert "AGENT_ENROLLMENT_CODE=" not in alt, \
        "the file form must not also pass the code as an environment variable"
    assert code in alt and "rm ./agent-enroll-code" in alt


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}: {exc}")
    print(f"\n{failures} failure(s)")
    sys.exit(1 if failures else 0)
