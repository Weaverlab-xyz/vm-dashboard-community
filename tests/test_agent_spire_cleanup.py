"""Revoking a remote agent also removes its SPIRE registration; deployed hosts keep their
node key across a restart.

  * Revoke, unbind and re-enrol delete the workload entry the dashboard created and evict
    its node, so SPIRE stops issuing SVIDs for an identity the dashboard no longer trusts.
  * A SPIFFE ID an operator bound by hand (any other shape) is never touched: it lives in
    a SPIRE server the dashboard does not manage.
  * A SPIRE server that is down cannot fail the revoke; what was left is reported.
  * A deployed SPIRE host keeps its node key on disk: its join token is single-use and
    deleted, so a memory key could never attest again after a reboot.

Runs under pytest, or standalone:  python tests/test_agent_spire_cleanup.py
"""
import base64
import os
import re
import sys
import tempfile
import uuid

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="agent-spire-cleanup-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDB}"
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-agent-spire-cleanup")

from web_dashboard.database import Base, RemoteAgent, SessionLocal, engine  # noqa: E402
from web_dashboard.services import agent_host_service as ahs  # noqa: E402
from web_dashboard.services import agent_service, dashboard_spire  # noqa: E402

Base.metadata.create_all(bind=engine)
TD = "dash.example.com"


class _Spire:
    """Stands in for the spire-server CLI: records every call, holds one entry."""
    def __init__(self, down=False):
        self.calls = []
        self.down = down

    def json(self, *args, **kw):
        self.calls.append(args)
        if self.down:
            raise dashboard_spire.DashboardSpireError("the SPIRE server container is not running")
        if args[:2] == ("entry", "show"):
            return {"entries": [{"id": "entry-1"}]}
        return {"results": [{"status": {"code": 0}}]}

    def cli(self, *args, **kw):
        self.calls.append(args)
        if self.down:
            raise dashboard_spire.DashboardSpireError("the SPIRE server container is not running")
        return ""


def _with_spire(fake, fn):
    saved = dashboard_spire._json, dashboard_spire._cli
    dashboard_spire._json, dashboard_spire._cli = fake.json, fake.cli
    try:
        return fn()
    finally:
        dashboard_spire._json, dashboard_spire._cli = saved


def _agent(spiffe_id=None):
    db = SessionLocal()
    row = RemoteAgent(name=f"a-{uuid.uuid4().hex[:8]}", is_active=True, public_key="k",
                      auth_mode="spiffe")
    db.add(row)
    db.commit()
    row.spiffe_id = spiffe_id if spiffe_id is not None else f"spiffe://{TD}/agent/{row.id}"
    db.commit()
    return db, row


def test_revoking_removes_the_entry_and_evicts_the_node():
    fake = _Spire()
    db, agent = _agent()
    try:
        _with_spire(fake, lambda: agent_service.revoke_agent(db, agent))
        assert ("entry", "delete", "-entryID", "entry-1") in fake.calls
        assert ("agent", "evict", "-spiffeID", f"spiffe://{TD}/node/{agent.id}") in fake.calls
        assert agent.spire_left_behind == []
        assert not agent.is_active and agent.spiffe_id, "the binding stays as the record"
    finally:
        db.close()


def test_unbinding_and_re_enrolling_remove_it_too():
    for action in (lambda db, a: agent_service.bind_spiffe_id(db, a, ""),
                   lambda db, a: agent_service.reissue_enroll_code(db, a)):
        fake = _Spire()
        db, agent = _agent()
        try:
            _with_spire(fake, lambda: action(db, agent))
            assert any(c[:2] == ("agent", "evict") for c in fake.calls)
            assert agent.spiffe_id is None
        finally:
            db.close()


def test_a_hand_bound_identity_is_left_to_its_owner():
    fake = _Spire()
    db, agent = _agent("spiffe://corp.example/workloads/dashboard-agent")
    try:
        _with_spire(fake, lambda: agent_service.revoke_agent(db, agent))
        assert fake.calls == [], fake.calls
    finally:
        db.close()


def test_another_agents_id_is_not_this_agents_registration():
    fake = _Spire()
    db, agent = _agent(f"spiffe://{TD}/agent/{uuid.uuid4()}")
    try:
        _with_spire(fake, lambda: agent_service.revoke_agent(db, agent))
        assert fake.calls == []
    finally:
        db.close()


def test_a_down_server_cannot_fail_the_revoke():
    fake = _Spire(down=True)
    db, agent = _agent()
    try:
        _with_spire(fake, lambda: agent_service.revoke_agent(db, agent))
        assert not agent.is_active and agent.public_key is None
        assert agent.spire_left_behind and "not running" in agent.spire_left_behind[0]
    finally:
        db.close()


def test_a_deployed_spire_host_keeps_its_node_key_on_disk():
    script = ahs.bootstrap_script(
        url="https://dash.example.com", policy=ahs.policy_yaml(["10.40.0.0/16"], 2),
        material={"mode": "spire", "join_token": "t", "trust_domain": TD,
                  "bootstrap_pem": "-----BEGIN CERTIFICATE-----\n",
                  "server_address": "spire.example.com", "server_port": 8081})
    files = {m.group(2): base64.b64decode(m.group(1)).decode()
             for m in re.finditer(r"echo (\S+) \| base64 -d > \"\$D/([^\"]+)\"", script)}
    conf = files["spire/conf/agent.conf"]
    assert 'KeyManager "disk"' in conf and 'KeyManager "memory"' not in conf
    assert 'directory = "/opt/spire/data"' in conf
    assert '-v "$D/spire/data:/opt/spire/data"' in script, "and the directory is a host mount"


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
