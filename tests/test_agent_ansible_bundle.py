"""The run-bundle route, driven against a real SQLite database.

The sibling of ``test_agent_secret_api.py``, and the more dangerous of the two routes: that
one hands out a hypervisor password, this one hands out a **playbook, an SSH private key and
a become password**. So it carries every gate that one does, and the assertions that matter
most are the same shape:

  * **Nothing in the request selects anything.** The body is one ephemeral key. The run comes
    wholly from the job row, so a stolen agent identity cannot ask for another job's
    playbook — and the refusal for "not yours" is identical to "does not exist", so the API
    is not an oracle either.
  * **The AAD binds the endpoint.** A bundle released for one host must not open as the
    bundle for a host the attacker controls. That is what turns credential *confusion* into
    credential *exfiltration*, and it is the one property a bare purpose string would lose.
  * A *cancelled* job may still log and complete but must not be handed fresh credentials;
    the response carries ``Cache-Control: no-store``; and no plaintext appears in the body.

Plus the two rules unique to this route: an ``ansible_*`` extra var is refused rather than
forwarded, and the bundle is size-capped so an oversized playbook produces a message naming
the limit instead of the agent's opaque response-ceiling error.

Uses a temporary SQLite file and the real ORM. Runs under pytest, or standalone:
    python tests/test_agent_ansible_bundle.py
"""
import json
import os
import sys
import tempfile
import uuid

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="agent-bundle-test-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDB}"
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-agent-bundle-tests")

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from web_dashboard.database import Base, Job, SessionLocal, engine, get_db
    from web_dashboard.api import agent as agent_api
    from web_dashboard.services import (agent_ansible_bundle, agent_sealing, agent_signing,
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
AGENT_VERSION = "2.3.0"
HOST, PORT = "10.20.10.5", 22


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


def _ready(name: str = "") -> tuple:
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


def _job(agent_id: str, *, status: str = "running", job_type: str = "agent_ansible",
         **meta_over) -> str:
    meta = {"run_kind": "vm", "transport": "ssh", "target_host": HOST,
            "target_port": PORT, "connection_id": "c-1", "target_id": "vm-1",
            "asset": "p.yml", "asset_backend": "", "extra_vars": {}}
    meta.update(meta_over)
    db = SessionLocal()
    try:
        job = job_service.create_job(db, job_type=job_type, created_by="tester",
                                     agent_id=agent_id, metadata=meta)
        row = db.query(Job).filter(Job.id == job.id).first()
        row.status = status
        db.commit()
        return job.id
    finally:
        db.close()


def _fetch(private: str, agent_id: str, job_id: str, *, reply_key=None):
    """Sign and issue a bundle fetch. Returns ``(response, reply_private_key)``.

    The body is sent as **exactly the bytes that were signed** — ``canonical_request``
    covers ``sha256(body)``, so re-serializing it with different separators produces a
    signature that can never verify, and the 401 that follows looks like a revoked agent.
    """
    reply_private = ""
    if reply_key is None:
        reply_private, reply_key = agent_sealing.generate_reply_keypair()
    path = f"/api/agent/jobs/{job_id}/ansible-bundle"
    body = agent_signing.serialize({"reply_key": reply_key})
    headers = agent_signing.sign_request(
        private, agent_id=agent_id, audience=AUDIENCE, method="POST",
        path=path, body=body)
    headers["Content-Type"] = "application/json"
    return CLIENT.request("POST", path, content=body, headers=headers), reply_private


# ── the request selects nothing ───────────────────────────────────────────────

def test_the_body_carries_only_an_ephemeral_key():
    """A field that could name a job, an asset or a connection would be a field a stolen
    identity could use to enumerate what else the dashboard holds."""
    assert set(agent_api.BundleRequest.model_fields) == {"reply_key"}


def test_another_agents_job_is_refused_identically_to_a_missing_one():
    """IDOR, and the refusal must not be an oracle: "not yours" and "does not exist" have to
    read the same."""
    _a_id, a_priv = _ready()
    b_id, _b_priv = _ready()
    b_job = _job(b_id)
    mine, _ = _fetch(a_priv, _a_id, b_job)
    absent, _ = _fetch(a_priv, _a_id, str(uuid.uuid4()))
    assert mine.status_code == absent.status_code == 409
    assert mine.json()["detail"] == absent.json()["detail"], (
        f"{mine.json()['detail']!r} vs {absent.json()['detail']!r}")


def test_the_wrong_job_type_is_refused():
    agent_id, priv = _ready()
    job = _job(agent_id, job_type="agent_discover")
    resp, _ = _fetch(priv, agent_id, job)
    assert resp.status_code == 409
    assert "does not use a Config-Management run bundle" in resp.json()["detail"]


def test_a_cancelled_job_gets_no_fresh_credentials():
    """It may still log and complete — cooperative cancel stays reachable — but it has no
    business being handed a playbook and a key."""
    agent_id, priv = _ready()
    for status in ("cancelled", "pending", "queued", "completed", "failed"):
        job = _job(agent_id, status=status)
        resp, _ = _fetch(priv, agent_id, job)
        assert resp.status_code == 409, f"{status} was served a bundle"


def test_an_unsigned_request_is_refused():
    agent_id, _priv = _ready()
    job = _job(agent_id)
    resp = CLIENT.post(f"/api/agent/jobs/{job}/ansible-bundle", json={"reply_key": "x"})
    assert resp.status_code == 401


def test_a_bad_reply_key_is_refused_before_anything_is_resolved():
    """Checked first on purpose: assembling a bundle can open a real Password Safe request,
    so answering a call that could never be sealed would let a caller burn checkouts."""
    agent_id, priv = _ready()
    job = _job(agent_id)
    for bad in ("", "not-base64!!", "AAAA"):
        resp, _ = _fetch(priv, agent_id, job, reply_key=bad)
        assert resp.status_code == 400, bad
        assert "reply key" in resp.json()["detail"].lower()


# ── what the route refuses on content ─────────────────────────────────────────

def test_an_ansible_extra_var_is_refused_rather_than_forwarded():
    """The dashboard applies the same filter the agent does, so an operator who typed one
    gets a clean refusal now instead of a puzzling one in Live Output later."""
    agent_id, priv = _ready()
    job = _job(agent_id, extra_vars={"ansible_connection": "local", "role": "web"})
    resp, _ = _fetch(priv, agent_id, job)
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert "ansible_connection" in detail and "role" not in detail


def test_a_malformed_run_is_refused_with_the_reason():
    agent_id, priv = _ready()
    job = _job(agent_id, target_host="")
    resp, _ = _fetch(priv, agent_id, job)
    assert resp.status_code == 409
    assert "no address" in resp.json()["detail"]


# ── the pure rules, without the route ─────────────────────────────────────────

def test_reserved_vars_names_every_offender_and_ignores_the_rest():
    assert agent_ansible_bundle.reserved_vars(
        {"ansible_user": 1, "ANSIBLE_CONNECTION": 2, "role": 3, "db_login_host": 4}
    ) == ["ANSIBLE_CONNECTION", "ansible_user"]
    assert agent_ansible_bundle.reserved_vars({}) == []
    assert agent_ansible_bundle.reserved_vars(None) == []


def test_the_bundle_is_size_capped():
    """The agent's own response ceiling is 1 MB and trips as an opaque error, so the useful
    limit is the one stated here."""
    assert 0 < agent_ansible_bundle.MAX_BUNDLE_BYTES <= 512 * 1024


def test_the_job_dir_is_not_under_tmp():
    """The runner mounts a tmpfs over /tmp at start, which would shadow anything the archive
    PUT placed there before start and delete it with no diagnostic."""
    assert not agent_ansible_bundle.JOB_DIR.startswith("/tmp")


def test_a_winrm_target_gets_its_scheme_from_the_port():
    assert agent_ansible_bundle._winrm_options("winrm", 5986)["scheme"] == "https"
    assert agent_ansible_bundle._winrm_options("winrm", 5985)["scheme"] == "http"
    assert agent_ansible_bundle._winrm_options("ssh", 22) == {}


# ── a bundle that is actually served ──────────────────────────────────────────

PLAYBOOK = b"- hosts: all\n  tasks: []\n"


def _served(agent_id, priv, job_id):
    """Fetch a bundle with the asset store stubbed, and open it. Returns the bundle dict.

    Only ``fetch_asset_in`` is stubbed — standing up a storage backend would test the
    storage layer, not this route. Everything else is the real path.
    """
    from web_dashboard.services import storage_service

    async def _fake(backend, name):
        return PLAYBOOK

    original = storage_service.fetch_asset_in
    storage_service.fetch_asset_in = _fake
    try:
        resp, reply_priv = _fetch(priv, agent_id, job_id)
    finally:
        storage_service.fetch_asset_in = original
    assert resp.status_code == 200, resp.text
    ref = agent_sealing.bundle_ref(run_kind="vm", transport="ssh", host=HOST, port=PORT)
    plain = agent_sealing.open_sealed(
        reply_priv, resp.json()["sealed"], agent_id=agent_id, audience=AUDIENCE,
        job_id=job_id, ref=ref)
    return json.loads(plain), resp


def test_a_served_bundle_opens_and_carries_the_playbook():
    agent_id, priv = _ready()
    job = _job(agent_id, asset_backend="s3", extra_vars={"role": "web"})
    payload, _resp = _served(agent_id, priv, job)
    assert payload["bundle"]["playbook"] == PLAYBOOK.decode()
    assert payload["bundle"]["run_kind"] == "vm"
    assert payload["bundle"]["extra_vars"] == {"role": "web"}


def test_a_served_bundle_carries_no_inventory():
    """The agent renders that itself. An inventory here is a place to write
    `ansible_connection: local`, which would run the play inside the runner container."""
    agent_id, priv = _ready()
    job = _job(agent_id, asset_backend="s3")
    payload, _resp = _served(agent_id, priv, job)
    assert "inventory" not in payload["bundle"]
    assert not [k for k in payload["bundle"] if str(k).startswith("ansible_")]


def test_the_response_body_is_ciphertext_and_uncacheable():
    """A TLS-inspecting proxy is the deployment this whole feature exists for, and it reads
    response bodies."""
    agent_id, priv = _ready()
    job = _job(agent_id, asset_backend="s3")
    _payload, resp = _served(agent_id, priv, job)
    assert resp.headers.get("Cache-Control") == "no-store"
    assert PLAYBOOK.decode().split("\n")[0] not in resp.text, "plaintext in the response"
    assert set(resp.json()) == {"sealed"}


def test_the_seal_does_not_open_against_a_different_endpoint():
    """The binding, end to end rather than on the ref string alone."""
    agent_id, priv = _ready()
    job = _job(agent_id, asset_backend="s3")
    from web_dashboard.services import storage_service

    async def _fake(backend, name):
        return PLAYBOOK

    original = storage_service.fetch_asset_in
    storage_service.fetch_asset_in = _fake
    try:
        resp, reply_priv = _fetch(priv, agent_id, job)
    finally:
        storage_service.fetch_asset_in = original
    wrong = agent_sealing.bundle_ref(run_kind="vm", transport="ssh",
                                     host="10.20.10.99", port=PORT)
    try:
        agent_sealing.open_sealed(reply_priv, resp.json()["sealed"], agent_id=agent_id,
                                  audience=AUDIENCE, job_id=job, ref=wrong)
        raise AssertionError("a bundle opened against the wrong endpoint")
    except agent_sealing.SealError:
        pass


def test_the_applied_playbook_is_fingerprinted_for_drift():
    """Stamped when the bundle is built, because that is the only moment the dashboard holds
    the bytes. Hashing at completion instead would hash whatever the asset says *then*, which
    is a different playbook if somebody re-uploaded in between."""
    from web_dashboard.services import config_drift

    agent_id, priv = _ready()
    job = _job(agent_id, asset_backend="s3")
    _served(agent_id, priv, job)
    db = SessionLocal()
    try:
        row = db.query(Job).filter(Job.id == job).first()
        stamped = (row.metadata_dict or {}).get("content_hash")
    finally:
        db.close()
    assert stamped == config_drift.content_hash(PLAYBOOK), stamped


def test_the_fingerprint_does_not_reach_the_agent():
    """It is a job-row field, not a wire field — the envelope projection is an allowlist."""
    from web_dashboard.services import agent_ansible_meta
    assert "content_hash" not in agent_ansible_meta.ENVELOPE_KEYS
    assert "content_hash" not in agent_ansible_meta.envelope_payload(
        {"run_kind": "vm", "transport": "ssh", "target_host": HOST,
         "target_port": PORT, "content_hash": "deadbeef"})


def test_a_completed_run_records_drift_and_a_failed_one_does_not():
    """Only successes record an apply, which is what makes hooking `complete_job` enough:
    `reconcile_stale_jobs` writes `failed` inline without passing through it."""
    from web_dashboard.database import ConfigApplyState, RemoteAgent
    from web_dashboard.services import agent_service

    def _complete(job_id, **kw):
        db = SessionLocal()
        try:
            row = db.query(Job).filter(Job.id == job_id).first()
            ag = db.query(RemoteAgent).filter(RemoteAgent.id == agent_id).first()
            agent_service.complete_job(db, ag, row, **kw)
        finally:
            db.close()

    def _applies(target):
        db = SessionLocal()
        try:
            return db.query(ConfigApplyState).filter(
                ConfigApplyState.target == target).count()
        finally:
            db.close()

    agent_id, priv = _ready()
    host = f"10.20.10.{uuid.uuid4().int % 200 + 20}"
    before = _applies(host)

    _complete(_job(agent_id, target_host=host, content_hash="abc123"),
              status="failed", error="boom")
    assert _applies(host) == before, "a failed run recorded a drift apply"

    _complete(_job(agent_id, target_host=host, content_hash="abc123"),
              status="completed", result={"exit_code": 0})
    assert _applies(host) == before + 1, "a completed run recorded no drift apply"


# ── the AAD binding ───────────────────────────────────────────────────────────

def test_the_bundle_ref_binds_the_endpoint():
    """Without the endpoint in the AAD, a bundle released for one host could be relabelled
    as the bundle for a host the attacker controls."""
    base = dict(run_kind="vm", transport="ssh", host=HOST, port=PORT)
    ref = agent_sealing.bundle_ref(**base)
    for changed in (dict(base, host="10.20.10.6"), dict(base, port=5985),
                    dict(base, run_kind="database"), dict(base, transport="winrm")):
        assert agent_sealing.bundle_ref(**changed) != ref, changed


def test_the_bundle_ref_is_domain_separated_from_a_connection_secret():
    """`/secret`'s ref is a connection name; this one must not be able to collide with it."""
    assert agent_sealing.bundle_ref(run_kind="vm", transport="ssh", host="h",
                                    port=22).startswith("ansible:")


def test_a_normalised_host_cannot_split_the_binding():
    a = agent_sealing.bundle_ref(run_kind="vm", transport="ssh", host="HOST.Lab.", port="22")
    b = agent_sealing.bundle_ref(run_kind="vm", transport="ssh", host="host.lab", port=22)
    assert a == b


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
