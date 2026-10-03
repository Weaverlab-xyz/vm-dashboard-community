"""Workload Lab demos on the dashboard's OWN trust domain (L1 and L2).

docs/design/dashboard-workload-identity.md. The agent demo cell and service-account SVID
clients used to need a Workload Lab SPIRE server; now the dashboard's own server
(services/dashboard_spire) can attest them. No SPIRE image is available to the test run,
so ``docker exec … spire-server`` is a stateful fake answering in SPIRE 1.15's
``-output json`` shapes, including ``entry delete -entryID`` and ``agent evict
-spiffeID`` (cmd/spire-server/cli/{entry/delete.go,agent/evict.go}). Pinned:

  * a dashboard-mode cell needs no lab: a node-scoped join token, an entry under that node
    selecting the worker's uid, a per-cell ID under /demo/agent-cell/, an SVID-bound OAuth
    client — and the join token in the response only, never on the row;
  * nothing is created on SPIRE when a guard refuses the request;
  * the options list the dashboard's server, and say why when it does not answer;
  * revoke keeps the identity (the demo is watching authorization end while identity
    stays); a separate, revoked-only action removes it, and never fails on SPIRE being down;
  * the service-account route registers only SVID clients under /workload/ in the
    dashboard's trust domain, embeds the uid in the command, keeps the token out of the
    audit row, is admin-only, and revoking the client removes the entry;
  * a client cannot be bound to a remote agent's or the dashboard's own ID in that domain,
    though the same paths in a lab's domain are fine;
  * the trust bundle stays current once registered, whichever switches are on.

Run: python tests/test_spire_workloads_on_dashboard_td.py   (or under pytest)
"""
import json
import os
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="spire-workloads-test-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDB}"
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-spire-workloads-tests")

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

# First-party imports UNGUARDED: a broken module must fail this file, not skip it.
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from jose import jwk  # noqa: E402
from web_dashboard.database import (AgentCell, AuditLog, Base, OAuthClient,  # noqa: E402
                                    SessionLocal, SpiffeTrustDomain, engine)
from web_dashboard.api import agentcell as agentcell_api  # noqa: E402
from web_dashboard.api import users as users_api  # noqa: E402
from web_dashboard.services import (agent_service, config_service,  # noqa: E402
                                    dashboard_spire, feature_flags, service_accounts,
                                    spire_lab_service)

Base.metadata.create_all(bind=engine)

TD = "dash.example"
AUDIENCE = "https://agents.example.com"
SECRET_TOKEN = "join-" + uuid.uuid4().hex

_KEY = ec.generate_private_key(ec.SECP256R1())
_JWK = jwk.construct(_KEY.public_key().public_bytes(
    serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode(),
    "ES256").to_dict()
_JWK.update(kid="k1", use="jwt-svid")
_JWK.pop("alg", None)


class FakeSpire:
    """docker exec <container> /opt/spire/bin/spire-server …, as SPIRE 1.15 answers it."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.calls = []
        self.entries = []
        self.evicted = []
        self.attested_nodes = set()
        self.tokens = 0
        self.down = False

    def ops(self, verb):
        return [c[4:] for c in self.calls if c[4:4 + len(verb)] == list(verb)]

    def __call__(self, argv, timeout):
        self.calls.append(argv)
        assert argv[:2] == ["docker", "exec"] and argv[3] == "/opt/spire/bin/spire-server"
        if self.down:
            return subprocess.CompletedProcess(argv, 1, "", "Error response from daemon: "
                                               "No such container: vmdash-spire-server\n")
        a = argv[4:]
        out = lambda d: subprocess.CompletedProcess(argv, 0, json.dumps(d) + "\n", "")  # noqa: E731
        if a[:2] == ["bundle", "show"]:
            if "-format" in a:
                return subprocess.CompletedProcess(argv, 0, json.dumps({"keys": [_JWK]}), "")
            if "-output" in a:
                return out({"trust_domain": TD})
            return subprocess.CompletedProcess(
                argv, 0, "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n", "")
        if a[:2] == ["token", "generate"]:
            self.tokens += 1
            return out({"value": f"{SECRET_TOKEN}-{self.tokens}"})
        if a[:2] == ["entry", "show"]:
            sid = a[a.index("-spiffeID") + 1]
            return out({"entries": [e for e in self.entries if e["spiffe_id"] == sid]})
        if a[:2] == ["entry", "create"]:
            entry = {"id": uuid.uuid4().hex, "spiffe_id": a[a.index("-spiffeID") + 1],
                     "parent_id": a[a.index("-parentID") + 1],
                     "selectors": [a[a.index("-selector") + 1]]}
            self.entries.append(entry)
            return out({"results": [{"status": {"code": 0, "message": "OK"}, "entry": entry}]})
        if a[:2] == ["entry", "delete"]:
            eid = a[a.index("-entryID") + 1]
            self.entries = [e for e in self.entries if e["id"] != eid]
            return out({"results": [{"status": {"code": 0, "message": "OK"}, "id": eid}]})
        if a[:2] == ["agent", "evict"]:
            node = a[a.index("-spiffeID") + 1]
            if node not in self.attested_nodes:
                return subprocess.CompletedProcess(argv, 1, "", "Error: rpc error: code = "
                                                   "NotFound desc = agent not found\n")
            self.evicted.append(node)
            return out({})
        raise AssertionError(f"unexpected spire-server call {a}")


SPIRE = FakeSpire()
dashboard_spire._run = SPIRE

# The cell's host is re-derived from deploy rows in production; here every name resolves.
spire_lab_service.resolve_host = lambda db, cloud, ref: {
    "name": ref, "private_ip": "10.0.0.7", "public_ip": "", "deploy_job_id": None}
_flag = feature_flags.enabled
feature_flags.enabled = lambda flag, *a, **k: True if flag == "mcp_server_enabled" else _flag(flag, *a, **k)


class _User:
    def __init__(self, admin=True):
        self.id = str(uuid.uuid4())
        self.username = "tester" if admin else "plain"
        self.is_admin = admin
        self.is_effective_admin = admin
        self.accessor_env_id = None
        self.workgroups_list = []
        self.effective_permissions_dict = {} if admin else {"config_mgmt": "read"}
        self.permissions_dict = self.effective_permissions_dict


ADMIN = _User()
CURRENT = {"user": ADMIN}


def _client() -> TestClient:
    from web_dashboard.database import get_db
    from web_dashboard.api.auth import get_current_user
    app = FastAPI()
    app.include_router(agentcell_api.router)
    app.include_router(users_api.router)

    def _db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_user] = lambda: CURRENT["user"]
    config_service.set(agent_service.AUDIENCE_CONFIG, AUDIENCE)
    return TestClient(app)


CLIENT = _client()


def _service_account(name=None):
    db = SessionLocal()
    try:
        user = service_accounts.create_service_account(
            db, username=name or f"sa-{uuid.uuid4().hex[:6]}")
        db.commit()
        return user.id, user.username
    finally:
        db.close()


def _fresh():
    SPIRE.reset()
    CURRENT["user"] = ADMIN


def _cell(**overrides):
    sa_id, _ = _service_account()
    body = {"name": f"cell-{uuid.uuid4().hex[:6]}", "host_ref": "worker-vm", "cloud": "gcp",
            "trust_source": "dashboard", "pat_user_id": sa_id, "pat_hours": 4}
    body.update(overrides)
    return CLIENT.post("/api/agentcell/agent", json=body)


# ── L1: the agent cell ────────────────────────────────────────────────────────

def test_a_dashboard_cell_needs_no_lab_and_gets_an_entry_and_a_join_token():
    _fresh()
    resp = _cell()
    assert resp.status_code == 200, resp.text
    out = resp.json()
    cell_id = out["id"]
    assert out["spiffe_id"] == f"spiffe://{TD}/demo/agent-cell/{cell_id}"
    node = f"spiffe://{TD}/node/agent-cell-{cell_id}"
    (gen,) = SPIRE.ops(["token", "generate"])
    assert gen[gen.index("-spiffeID") + 1] == node, "the token must be scoped to the node"
    (entry,) = SPIRE.entries
    assert entry["parent_id"] == node and entry["selectors"] == ["unix:uid:0"]
    assert out["join_token"].startswith(SECRET_TOKEN)
    assert "BEGIN CERTIFICATE" in out["bootstrap_pem"]
    assert out["spire_server_address"] == "agents.example.com"
    assert out["spire_server_port"] == 8081
    assert out["token"] == "" and out["client_id"], "a service account gets an SVID client"
    db = SessionLocal()
    try:
        row = db.query(AgentCell).filter(AgentCell.id == cell_id).first()
        assert row.spire_lab_id is None and row.trust_domain == TD
        assert SECRET_TOKEN not in json.dumps({c.name: str(getattr(row, c.name))
                                               for c in row.__table__.columns})
        client = db.query(OAuthClient).filter(OAuthClient.client_id == out["client_id"]).first()
        assert client.spiffe_id == out["spiffe_id"]
        assert db.query(SpiffeTrustDomain).filter(
            SpiffeTrustDomain.trust_domain == TD).first(), "the trust domain is registered"
    finally:
        db.close()


def test_the_worker_uid_is_the_selector():
    _fresh()
    assert _cell(worker_uid=1010).status_code == 200
    assert SPIRE.entries[0]["selectors"] == ["unix:uid:1010"]


def test_nothing_is_created_on_spire_when_a_guard_refuses():
    """The token user is checked after the host and before SPIRE is touched: a refusal
    there must leave no join token minted and no entry behind."""
    _fresh()
    resp = _cell(pat_user_id=str(uuid.uuid4()))
    assert resp.status_code == 404, resp.text
    assert SPIRE.ops(["token", "generate"]) == [] and SPIRE.entries == []


def test_a_dashboard_server_that_is_down_is_a_502_with_the_cause():
    _fresh()
    SPIRE.down = True
    resp = _cell()
    assert resp.status_code == 502 and "not running" in resp.json()["detail"]


def test_the_options_offer_the_dashboard_server_and_say_why_when_they_cannot():
    _fresh()
    opts = CLIENT.get("/api/agentcell/options").json()
    assert opts["dashboard_spire"]["trust_domain"] == TD
    assert opts["dashboard_spire"]["spiffe_id_pattern"].startswith(f"spiffe://{TD}/demo/agent-cell/")
    SPIRE.down = True
    opts = CLIENT.get("/api/agentcell/options").json()
    assert opts["dashboard_spire"] is None
    assert any("did not answer" in m for m in opts["missing"])


def test_the_list_gives_the_install_dialog_the_dashboard_facts():
    _fresh()
    cell_id = _cell().json()["id"]
    row = next(a for a in CLIENT.get("/api/agentcell/agents").json()["agents"]
               if a["id"] == cell_id)
    assert row["trust_source"] == "dashboard" and row["worker_on_node"] is True
    assert row["agent_node_id"] == f"spiffe://{TD}/node/agent-cell-{cell_id}"
    assert row["spire_server_address"] == "agents.example.com"
    assert SECRET_TOKEN not in json.dumps(row)


def test_revoke_keeps_the_identity_and_removal_needs_a_revoked_cell():
    _fresh()
    out = _cell().json()
    cell_id = out["id"]
    node = f"spiffe://{TD}/node/agent-cell-{cell_id}"
    SPIRE.attested_nodes.add(node)
    early = CLIENT.delete(f"/api/agentcell/agent/{cell_id}/spire-identity")
    assert early.status_code == 409, "a live cell's identity must not be removed"
    assert CLIENT.delete(f"/api/agentcell/agent/{cell_id}").status_code == 200
    assert SPIRE.entries, "revoke took the identity with it — the demo is watching it stay"
    resp = CLIENT.delete(f"/api/agentcell/agent/{cell_id}/spire-identity")
    assert resp.status_code == 200 and resp.json()["removed"] is True, resp.text
    assert SPIRE.entries == [] and SPIRE.evicted == [node]


def test_removal_never_fails_on_spire_being_down_and_says_so():
    _fresh()
    cell_id = _cell().json()["id"]
    CLIENT.delete(f"/api/agentcell/agent/{cell_id}")
    SPIRE.down = True
    resp = CLIENT.delete(f"/api/agentcell/agent/{cell_id}/spire-identity")
    assert resp.status_code == 200
    assert resp.json()["removed"] is False and resp.json()["problems"]


def test_a_node_that_never_attested_is_not_a_removal_problem():
    _fresh()
    cell_id = _cell().json()["id"]
    CLIENT.delete(f"/api/agentcell/agent/{cell_id}")
    resp = CLIENT.delete(f"/api/agentcell/agent/{cell_id}/spire-identity").json()
    assert resp["removed"] is True and resp["problems"] == []


# ── L2: service-account clients ───────────────────────────────────────────────

def _sa_client(spiffe_id=None):
    sa_id, username = _service_account()
    body = {"name": "pipeline"}
    if spiffe_id is not None:
        body["spiffe_id"] = spiffe_id.replace("<sa>", username)
    resp = CLIENT.post(f"/api/users/{sa_id}/oauth-clients", json=body)
    assert resp.status_code == 201, resp.text
    return sa_id, resp.json()


def _register_td():
    db = SessionLocal()
    try:
        dashboard_spire.sync_trust_domain(db, TD)
    finally:
        db.close()


def test_a_workload_client_gets_an_entry_and_a_command_with_its_uid():
    _fresh()
    _register_td()
    sa_id, client = _sa_client(f"spiffe://{TD}/workload/<sa>")
    resp = CLIENT.post(f"/api/users/{sa_id}/oauth-clients/{client['id']}/spire-entry",
                       json={"uid": 1010})
    assert resp.status_code == 200, resp.text
    out = resp.json()
    node = f"spiffe://{TD}/node/workload-{client['id']}"
    assert out["node_spiffe_id"] == node and out["join_token"].startswith(SECRET_TOKEN)
    (entry,) = SPIRE.entries
    assert entry["spiffe_id"] == client["spiffe_id"] and entry["parent_id"] == node
    assert entry["selectors"] == ["unix:uid:1010"]
    assert "-e workload_uid=1010" in out["command"]
    assert "spire-agent-install.yml" in out["command"]
    assert "join_token=<the join token above>" in out["command"], "the token stays out of it"
    db = SessionLocal()
    try:
        rows = db.query(AuditLog).filter(AuditLog.action == "service_account.spire_entry").all()
        assert rows and all(SECRET_TOKEN not in (r.details or "") for r in rows)
    finally:
        db.close()


def test_the_entry_route_refuses_what_it_should():
    _fresh()
    _register_td()
    sa_id, secret_client = _sa_client()
    assert CLIENT.post(f"/api/users/{sa_id}/oauth-clients/{secret_client['id']}/spire-entry",
                       json={"uid": 1}).status_code == 409
    sa_id, other = _sa_client("spiffe://lab.example/workload/<sa>")
    resp = CLIENT.post(f"/api/users/{sa_id}/oauth-clients/{other['id']}/spire-entry",
                       json={"uid": 1})
    assert resp.status_code == 409 and "/workload/" in resp.json()["detail"]
    sa_id, outside = _sa_client(f"spiffe://{TD}/pipelines/<sa>")
    assert CLIENT.post(f"/api/users/{sa_id}/oauth-clients/{outside['id']}/spire-entry",
                       json={"uid": 1}).status_code == 409
    sa_id, revoked = _sa_client(f"spiffe://{TD}/workload/<sa>")
    CLIENT.delete(f"/api/users/{sa_id}/oauth-clients/{revoked['id']}")
    assert CLIENT.post(f"/api/users/{sa_id}/oauth-clients/{revoked['id']}/spire-entry",
                       json={"uid": 1}).status_code == 409
    assert SPIRE.entries == [], "a refusal created something on SPIRE"


def test_the_entry_route_is_admin_only():
    _fresh()
    _register_td()
    sa_id, client = _sa_client(f"spiffe://{TD}/workload/<sa>")
    CURRENT["user"] = _User(admin=False)
    try:
        resp = CLIENT.post(f"/api/users/{sa_id}/oauth-clients/{client['id']}/spire-entry",
                           json={"uid": 1})
    finally:
        CURRENT["user"] = ADMIN
    assert resp.status_code == 403


def test_revoking_a_workload_client_removes_its_entry():
    _fresh()
    _register_td()
    sa_id, client = _sa_client(f"spiffe://{TD}/workload/<sa>")
    CLIENT.post(f"/api/users/{sa_id}/oauth-clients/{client['id']}/spire-entry", json={"uid": 7})
    assert SPIRE.entries
    resp = CLIENT.delete(f"/api/users/{sa_id}/oauth-clients/{client['id']}")
    assert resp.status_code == 200 and resp.json()["spire_left_behind"] == []
    assert SPIRE.entries == []


def test_reserved_ids_in_the_dashboards_trust_domain_cannot_bind_a_client():
    _fresh()
    _register_td()
    sa_id, _ = _service_account()
    for sid in (f"spiffe://{TD}/dashboard", f"spiffe://{TD}/agent/x", f"spiffe://{TD}/agent"):
        resp = CLIENT.post(f"/api/users/{sa_id}/oauth-clients",
                           json={"name": "x", "spiffe_id": sid})
        assert resp.status_code == 400 and "reserved" in resp.json()["detail"], (sid, resp.text)
    # A path that merely starts with the same letters is not reserved...
    assert CLIENT.post(f"/api/users/{sa_id}/oauth-clients",
                       json={"name": "y", "spiffe_id": f"spiffe://{TD}/dashboards"}).status_code == 201
    # ...and the same paths in a lab's trust domain are that lab's business.
    sa2, _ = _service_account()
    assert CLIENT.post(f"/api/users/{sa2}/oauth-clients",
                       json={"name": "z", "spiffe_id": "spiffe://lab.example/agent/x"}).status_code == 201


# ── the trust bundle stays current once registered ────────────────────────────

def test_a_registered_trust_domain_stays_current_with_every_switch_off():
    _fresh()
    config_service.set("spire_attest_enabled", "0")
    config_service.set("dashboard_spiffe_identity_enabled", "0")
    db = SessionLocal()
    try:
        db.query(SpiffeTrustDomain).delete()
        db.commit()
        assert dashboard_spire.sync_if_due(db) is False, "nothing registered, nothing on"
        dashboard_spire.sync_trust_domain(db, TD)
        later = datetime.utcnow() + timedelta(days=2)
        assert dashboard_spire.sync_if_due(db, now=later) is True
    finally:
        db.close()


def test_migrate_agent_still_mints_the_same_shape():
    """register_workload was extracted from migrate_agent; the agent's entry is unchanged."""
    _fresh()
    db = SessionLocal()
    try:
        from web_dashboard.database import RemoteAgent
        agent = RemoteAgent(name=f"a-{uuid.uuid4().hex[:6]}", site="dc1")
        db.add(agent)
        db.commit()
        agent_id = agent.id
        out = dashboard_spire.migrate_agent(db, agent)
    finally:
        db.close()
    sid, node = dashboard_spire.ids_for(TD, agent_id)
    assert out["spiffe_id"] == sid and out["node_spiffe_id"] == node
    (entry,) = SPIRE.entries
    assert entry["selectors"] == [f"unix:uid:{dashboard_spire.AGENT_UID}"]


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
