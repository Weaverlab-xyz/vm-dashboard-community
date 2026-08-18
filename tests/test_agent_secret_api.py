"""The just-in-time credential route, driven against a real SQLite database.

This is the only agent route that hands a credential *out*, so it is the one where a
missing clause is worth the most to an attacker. Two of the assertions here matter more
than the rest:

  * **The connection is derived from the job, never chosen by the request.** Without that,
    a stolen agent identity enumerates every credential the dashboard holds by asking for
    arbitrary refs against a job it legitimately owns. It is the most important property in
    the whole feature and it is not a cryptographic one.
  * **IDOR.** Agent A must not obtain a credential for agent B's job, and the refusal must
    be shape-identical to a job that does not exist — otherwise the API is an oracle for
    which jobs and connections exist.

Plus the ones that are easy to lose in a refactor: a *cancelled* job is still winding down
and may log and complete, but must not be handed a fresh credential; the response must
carry `Cache-Control: no-store`; and the plaintext must appear nowhere in the response body.

Uses a temporary SQLite file and the real ORM. Runs under pytest, or standalone:
    python tests/test_agent_secret_api.py
"""
import base64
import os
import sys
import tempfile
import uuid

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="agent-secret-test-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDB}"
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-agent-secret-tests")

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from web_dashboard.database import (Base, HypervisorConnection, Job, SessionLocal,
                                       engine, get_db)
    from web_dashboard.api import agent as agent_api
    from web_dashboard.services import (agent_sealing, agent_signing,
                                        hypervisor_connection_service as hcs,
                                        job_service)
except Exception as exc:  # pragma: no cover — app deps missing
    try:
        import pytest
        pytest.skip(f"app dependencies unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)

Base.metadata.create_all(bind=engine)

AUDIENCE = "https://agents.test"
SECRET = "vCenter!Admin#2026"
AGENT_VERSION = "2.1.0"


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
    from web_dashboard.services import config_service
    config_service.set(agent_api._AUDIENCE_CONFIG, AUDIENCE)
    return TestClient(app)


CLIENT = _app()


# ── fixtures ──────────────────────────────────────────────────────────────────

def _ready(name: str = "") -> tuple:
    """An enrolled agent: returns (agent_id, private_key)."""
    name = name or f"agent-{uuid.uuid4().hex[:8]}"
    resp = CLIENT.post("/api/agent", json={"name": name, "site": "dc1"})
    assert resp.status_code == 201, resp.text
    code = resp.json()["enrollment_code"]
    private, public = agent_signing.generate_keypair()
    resp = CLIENT.post("/api/agent/enroll", json={
        "enrollment_code": code, "public_key": public,
        "agent_version": AGENT_VERSION, "policy_hash": "a" * 64})
    assert resp.status_code == 200, resp.text
    return resp.json()["agent_id"], private


def _connection(agent_id: str, *, ref: str = "dc1-vcenter", secret: str = SECRET,
                secret_ref: str = "", active: bool = True) -> dict:
    db = SessionLocal()
    try:
        out = hcs.create(db, kind="vsphere", name=f"c-{uuid.uuid4().hex[:8]}",
                         created_by="tester", agent_id=agent_id,
                         agent_connection_name=ref, secret=secret,
                         secret_ref=secret_ref)
        if not active:
            hcs.update(db, out["id"], is_active=False)
        return out
    finally:
        db.close()


def _job(agent_id: str, conn: dict, *, ref: str = "dc1-vcenter",
         job_type: str = "agent_hypervisor", status: str = "running") -> str:
    db = SessionLocal()
    try:
        job = job_service.create_job(
            db, job_type=job_type, created_by="tester", agent_id=agent_id,
            metadata={"verb": "inventory_sync", "connection_ref": ref,
                      "connection_id": conn["id"], "kind": "vsphere"})
        row = db.query(Job).filter(Job.id == job.id).first()
        row.status = status
        db.commit()
        return job.id
    finally:
        db.close()


def _fetch(private: str, agent_id: str, job_id: str, *, ref: str = "dc1-vcenter",
           reply_key: str = None):
    """Sign and issue a credential fetch. Returns (response, reply_private_key).

    ``reply_key=None`` means "generate a good one"; an empty string is passed through, so a
    test can assert on it rather than have it silently replaced.
    """
    reply_private, reply_public = agent_sealing.generate_reply_keypair()
    payload = {"connection_ref": ref,
               "reply_key": reply_public if reply_key is None else reply_key}
    path = f"/api/agent/jobs/{job_id}/secret"
    body = agent_signing.serialize(payload)
    headers = agent_signing.sign_request(
        private, agent_id=agent_id, audience=AUDIENCE, method="POST",
        path=path, body=body)
    headers["Content-Type"] = "application/json"
    return CLIENT.request("POST", path, content=body, headers=headers), reply_private


def _open(resp, reply_private, agent_id, job_id, ref="dc1-vcenter") -> str:
    return agent_sealing.open_sealed(
        reply_private, resp.json()["sealed"], agent_id=agent_id,
        audience=AUDIENCE, job_id=job_id, ref=ref)


# ── the happy path ────────────────────────────────────────────────────────────

def test_an_agent_gets_its_jobs_credential_sealed():
    agent_id, private = _ready()
    conn = _connection(agent_id)
    job_id = _job(agent_id, conn)
    resp, reply_private = _fetch(private, agent_id, job_id)
    assert resp.status_code == 200, resp.text
    assert _open(resp, reply_private, agent_id, job_id) == SECRET


def test_the_plaintext_appears_nowhere_in_the_response():
    """Blunt, and the highest-value assertion per line in this file. Any future refactor
    that adds a convenience field carrying the credential fails here."""
    agent_id, private = _ready()
    conn = _connection(agent_id)
    job_id = _job(agent_id, conn)
    resp, _ = _fetch(private, agent_id, job_id)
    assert SECRET not in resp.text
    assert SECRET not in str(dict(resp.headers))


def test_the_response_is_not_cacheable():
    """Set explicitly, because nothing else in this application sets Cache-Control at all.
    `no-store` and not `no-cache`: the latter lets a proxy keep the body and revalidate."""
    agent_id, private = _ready()
    conn = _connection(agent_id)
    job_id = _job(agent_id, conn)
    resp, _ = _fetch(private, agent_id, job_id)
    assert resp.headers.get("cache-control") == "no-store"


def test_a_repeated_fetch_still_works():
    """A Workstation job reads the credential on every HTTP call, and a transient network
    error has to be retryable, so the server stays permissive. The agent memoises."""
    agent_id, private = _ready()
    conn = _connection(agent_id)
    job_id = _job(agent_id, conn)
    for _ in range(3):
        resp, reply_private = _fetch(private, agent_id, job_id)
        assert resp.status_code == 200
        assert _open(resp, reply_private, agent_id, job_id) == SECRET


# ── authorization ─────────────────────────────────────────────────────────────

def test_agent_b_cannot_fetch_agent_as_job_credential():
    """The highest-severity bug this route could have."""
    a_id, a_private = _ready()
    b_id, b_private = _ready()
    conn = _connection(a_id)
    job_id = _job(a_id, conn)

    resp, _ = _fetch(b_private, b_id, job_id)
    assert resp.status_code == 409
    assert SECRET not in resp.text

    # ...and identically to a job that does not exist, so this is not an oracle.
    missing, _ = _fetch(b_private, b_id, str(uuid.uuid4()))
    assert resp.json()["detail"] == missing.json()["detail"]


def test_a_ref_the_job_did_not_name_is_refused():
    """The body cannot *choose* the connection — it is derived from the job row — so this
    can only mean the agent and the job disagree. Refusing loudly beats handing over a
    credential for a connection the agent did not mean."""
    agent_id, private = _ready()
    conn = _connection(agent_id)
    _connection(agent_id, ref="other-vcenter", secret="other-secret")
    job_id = _job(agent_id, conn)
    resp, _ = _fetch(private, agent_id, job_id, ref="other-vcenter")
    assert resp.status_code == 409
    assert "other-secret" not in resp.text and SECRET not in resp.text


def test_a_connection_bound_to_another_agent_is_refused():
    """Job ownership alone is not the same claim as connection ownership: a job row could
    exist against a connection bound to a different agent, and a stolen key must not turn
    that into a credential read."""
    a_id, a_private = _ready()
    b_id, _b_private = _ready()
    foreign = _connection(b_id)
    job_id = _job(a_id, foreign)          # job is A's, connection is B's
    resp, _ = _fetch(a_private, a_id, job_id)
    assert resp.status_code == 409
    assert SECRET not in resp.text


def test_a_cancelled_or_finished_job_gets_no_credential():
    """`_owned`'s default WINDING_DOWN includes 'cancelled' so cooperative cancel stays
    reachable — correct for logs and complete, wrong for a credential."""
    agent_id, private = _ready()
    conn = _connection(agent_id)
    for status in ("cancelled", "completed", "failed", "queued"):
        job_id = _job(agent_id, conn, status=status)
        resp, _ = _fetch(private, agent_id, job_id)
        assert resp.status_code == 409, f"{status} was handed a credential"
        assert SECRET not in resp.text


def test_a_cancelled_job_can_still_log_even_though_it_cannot_fetch():
    """The other half of the above: narrowing the status tuple must not break cancel."""
    agent_id, private = _ready()
    conn = _connection(agent_id)
    job_id = _job(agent_id, conn, status="cancelled")
    path = f"/api/agent/jobs/{job_id}/logs"
    body = agent_signing.serialize({"lines": ["winding down"]})
    headers = agent_signing.sign_request(
        private, agent_id=agent_id, audience=AUDIENCE, method="POST",
        path=path, body=body)
    headers["Content-Type"] = "application/json"
    assert CLIENT.request("POST", path, content=body, headers=headers).status_code == 200


def test_a_discovery_job_has_no_credential_to_fetch():
    agent_id, private = _ready()
    conn = _connection(agent_id)
    job_id = _job(agent_id, conn, job_type="agent_discover")
    resp, _ = _fetch(private, agent_id, job_id)
    assert resp.status_code == 409


def test_an_unsigned_or_replayed_request_is_refused():
    agent_id, private = _ready()
    conn = _connection(agent_id)
    job_id = _job(agent_id, conn)
    path = f"/api/agent/jobs/{job_id}/secret"

    assert CLIENT.post(path, json={"connection_ref": "dc1-vcenter",
                                   "reply_key": "x"}).status_code == 401

    reply_private, reply_public = agent_sealing.generate_reply_keypair()
    payload = {"connection_ref": "dc1-vcenter", "reply_key": reply_public}
    body = agent_signing.serialize(payload)
    headers = agent_signing.sign_request(
        private, agent_id=agent_id, audience=AUDIENCE, method="POST",
        path=path, body=body)
    headers["Content-Type"] = "application/json"
    assert CLIENT.request("POST", path, content=body,
                          headers=headers).status_code == 200
    # The nonce is single-use, so the identical request must not work twice.
    assert CLIENT.request("POST", path, content=body,
                          headers=headers).status_code == 401


# ── configuration refusals, with the remedy in them ───────────────────────────

def test_a_connection_with_no_credential_says_what_to_do():
    """An agent-bound row holding no secret is the ORIGINAL behaviour, so reaching here
    means the agent's connections.yaml opted in and the dashboard side was never filled
    in. A failed job renders only error_message, so the remedy has to be in this string."""
    agent_id, private = _ready()
    conn = _connection(agent_id, secret="")
    job_id = _job(agent_id, conn)
    resp, _ = _fetch(private, agent_id, job_id)
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert "Connections page" in detail
    assert "dashboard_secret" in detail
    assert "Nothing was attempted" in detail


def test_a_disabled_connection_is_refused():
    agent_id, private = _ready()
    conn = _connection(agent_id, active=False)
    job_id = _job(agent_id, conn)
    resp, _ = _fetch(private, agent_id, job_id)
    assert resp.status_code == 409
    assert SECRET not in resp.text


def test_a_malformed_reply_key_is_a_400_not_a_leak():
    agent_id, private = _ready()
    conn = _connection(agent_id)
    job_id = _job(agent_id, conn)
    for bad in ("", "not base64!!", "c2hvcnQ=", base64.b64encode(bytes(32)).decode()):
        resp, _ = _fetch(private, agent_id, job_id, reply_key=bad)
        assert resp.status_code == 400, bad
        assert SECRET not in resp.text


def test_the_reply_key_is_checked_before_the_credential_is_obtained():
    """Ordering, and it is not cosmetic. Obtaining a Password Safe credential opens a real
    request that is then checked in and rotated on release — so answering a request that
    could never be sealed would let a caller with a stolen agent key burn checkouts and
    rotations on that account at will. `agent_guard` caps the rate; this removes the
    amplification.
    """
    import ast
    src = open(os.path.join(_ROOT, "web_dashboard", "api", "agent.py"),
               encoding="utf-8").read()
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name == "job_secret")

    def _first(attr):
        return min((n.lineno for n in ast.walk(fn) if isinstance(n, ast.Call)
                    and getattr(n.func, "attr", "") == attr), default=None)

    checked = _first("check_reply_key")
    acquired = _first("acquire")
    resolved = _first("resolve_agent_secret")
    assert checked, "job_secret no longer validates the reply key up front"
    assert acquired and resolved, "did the credential lookup move?"
    assert checked < acquired, "the reply key must be checked before a PS checkout"
    assert checked < resolved


# ── audit ─────────────────────────────────────────────────────────────────────

def test_the_release_is_audited_without_the_credential():
    agent_id, private = _ready()
    conn = _connection(agent_id)
    job_id = _job(agent_id, conn)
    resp, _ = _fetch(private, agent_id, job_id)
    assert resp.status_code == 200

    from web_dashboard.database import AuditLog
    db = SessionLocal()
    try:
        rows = db.query(AuditLog).filter(
            AuditLog.action == "agent.connection_secret").all()
        assert rows, "a credential release must leave an audit row"
        row = rows[-1]
        assert row.username.startswith("agent:")
        blob = f"{row.details or ''}{row.target_vm or ''}"
        assert SECRET not in blob, "the audit row must never carry the credential"
        # What it SHOULD carry: enough to answer "which credential, for what".
        assert job_id in blob and "dc1-vcenter" in blob and "secret_enc" in blob
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
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
