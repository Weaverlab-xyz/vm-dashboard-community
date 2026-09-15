"""Which agent may execute a Config-Management run — the first tests `_resolve_agent_target`
has ever had.

That function is the only gate between a request and an agent-executed playbook, and until
Config-Management routes existed nothing exercised it: `inventory_service._hv_item` set a
target's `agent_id` to its connection's, so `payload.agent_id == conn.agent_id` always and
the check never fired. Which means the security property it exists for — an agent that
neither brokers the connection nor is routed the address cannot run against it — was
untested, and so was the address pin beside it.

The sharpest test here is
:func:`test_a_route_cannot_be_steered_by_the_address_in_the_body`. Everything else can be
re-derived by reading the code; that one cannot.

Runs under pytest, or standalone:  python tests/test_config_mgmt_agent_resolution.py
"""
import json
import os
import sys
import tempfile
from datetime import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.environ.setdefault("DATABASE_URL",
                      "sqlite:///" + os.path.join(tempfile.mkdtemp(), "resolve.db").replace("\\", "/"))
os.environ.setdefault("JWT_SECRET_KEY", "x" * 32)

try:
    from fastapi import HTTPException

    from web_dashboard.database import (Base, engine, SessionLocal, ConfigMgmtRoute,
                                        HypervisorConnection, HypervisorVMCache, RemoteAgent)
    from web_dashboard.api.config_mgmt import _resolve_agent_target
    from web_dashboard.services import config_mgmt_route_service as cmr
except Exception as exc:  # noqa: BLE001
    print(f"SKIP: {exc}")
    sys.exit(0)

Base.metadata.create_all(bind=engine)


class _Payload:
    """The handful of RunRequest fields the gate reads. A stand-in rather than the real
    model so this file does not depend on pydantic validation rules that have nothing to
    do with agent resolution."""

    def __init__(self, **kw):
        self.agent_id = kw.get("agent_id", "")
        self.target_kind = kw.get("target_kind", "vm")
        self.target_id = kw.get("target_id", "vm-1")
        self.connection_id = kw.get("connection_id", "conn-ws")
        self.target = kw.get("target", "")
        self.transport = kw.get("transport", "")
        self.port = kw.get("port", 0)


def _fresh():
    db = SessionLocal()
    for model in (ConfigMgmtRoute, HypervisorVMCache, HypervisorConnection, RemoteAgent):
        db.query(model).delete()
    db.commit()
    return db


def _agent(db, name, *, active=True, granted=True, version="2.5.0", online=True):
    row = RemoteAgent(
        id=f"agent-{name}", name=name, is_active=active, agent_version=version,
        # public_key is load-bearing in the fixture, not decoration: status_of returns
        # "enrolling" without one, and the gate refuses anything that is not "online" —
        # so an agent missing it fails every test here with the offline message.
        public_key="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI" + name,
        last_seen_at=datetime.utcnow() if online else datetime(2020, 1, 1),
        allowed_job_types=None if granted else json.dumps(["agent_hypervisor"]),
        created_at=datetime.utcnow())
    db.add(row)
    db.commit()
    return row


def _scene(db, *, vm_ips=("10.0.0.5",)):
    """A workstation connection brokered by agent-host, holding one synced VM."""
    _agent(db, "host")
    db.add(HypervisorConnection(id="conn-ws", kind="workstation", name="ws", host="",
                                port=8697, agent_id="agent-host",
                                agent_connection_name="ws", is_active=True,
                                created_at=datetime.utcnow()))
    db.add(HypervisorVMCache(connection_id="conn-ws", vm_id="vm-1", name="oracle9",
                             power_state="poweredOn", guest_os="oracleLinux9_64",
                             ip_addresses=json.dumps(list(vm_ips)),
                             synced_at=datetime.utcnow()))
    db.commit()


def _refusal(payload, db):
    try:
        _resolve_agent_target(payload, db)
    except HTTPException as exc:
        return exc
    raise AssertionError("the gate accepted this run")


# ── the default, unchanged ───────────────────────────────────────────────────

def test_the_discovering_agent_still_executes_when_nothing_is_routed():
    """THE BACKWARDS-COMPATIBILITY CONTRACT, and the single most important assertion in
    this file: with no routes, the gate behaves exactly as it did before routes existed."""
    db = _fresh()
    _scene(db)
    out = _resolve_agent_target(_Payload(agent_id="agent-host"), db)
    assert out["run_kind"] == "vm"
    assert out["target_host"] == "10.0.0.5"
    assert out["executor_name"] == "host"
    db.close()


def test_an_agent_that_neither_brokers_nor_is_routed_is_refused():
    """The security property the old `conn.agent_id != agent.id` check existed for, which
    nothing asserted until now."""
    db = _fresh()
    _scene(db)
    _agent(db, "stranger")
    exc = _refusal(_Payload(agent_id="agent-stranger"), db)
    assert exc.status_code == 400
    assert "stranger" in exc.detail and "host" in exc.detail, exc.detail
    assert "no Config-Management route covers" in exc.detail, exc.detail
    db.close()


def test_the_refusal_names_the_agent_that_would_run_it():
    """The old message named only the agent that could not run — the half that does not
    help. An operator needs the name of the one that would."""
    db = _fresh()
    _scene(db)
    _agent(db, "stranger")
    exc = _refusal(_Payload(agent_id="agent-stranger"), db)
    assert "brokered by agent 'host'" in exc.detail, exc.detail
    db.close()


# ── the new allowance ────────────────────────────────────────────────────────

def test_a_routed_agent_may_run_against_a_connection_it_does_not_broker():
    """The whole feature. agent-lab brokers nothing, and runs against the VM's address
    because a route says its segment is reachable from there."""
    db = _fresh()
    _scene(db, vm_ips=("192.168.235.99",))
    _agent(db, "lab")
    cmr.create(db, agent_id="agent-lab", cidr="192.168.235.0/24", created_by="admin")
    out = _resolve_agent_target(_Payload(agent_id="agent-lab"), db)
    assert out["target_host"] == "192.168.235.99"
    assert out["executor_name"] == "lab"
    db.close()


def test_naming_the_brokering_agent_for_a_routed_address_is_refused():
    """One correct answer per (connection, VM, address). Under a "broker OR routed agent"
    rule this would be accepted, lease, run, and time out — which is the bug the feature
    exists to remove, surviving as an accepted input."""
    db = _fresh()
    _scene(db, vm_ips=("192.168.235.99",))
    _agent(db, "lab")
    cmr.create(db, agent_id="agent-lab", cidr="192.168.235.0/24", label="vmnet1",
               created_by="admin")
    exc = _refusal(_Payload(agent_id="agent-host"), db)
    assert exc.status_code == 400
    assert "lab" in exc.detail, exc.detail
    assert "192.168.235.0/24" in exc.detail, exc.detail
    db.close()


def test_an_inactive_route_sends_the_run_back_to_the_discovering_agent():
    db = _fresh()
    _scene(db, vm_ips=("192.168.235.99",))
    _agent(db, "lab")
    out = cmr.create(db, agent_id="agent-lab", cidr="192.168.235.0/24", created_by="admin")
    cmr.update(db, out["id"], is_active=False)
    assert _resolve_agent_target(_Payload(agent_id="agent-host"), db)["executor_name"] == "host"
    _refusal(_Payload(agent_id="agent-lab"), db)
    db.close()


# ── the address pin, which a route must never move ──────────────────────────

def test_a_route_cannot_be_steered_by_the_address_in_the_body():
    """THE ONE TO KEEP. A route may change who executes, never what is targeted.

    The caller proposes an address that a route DOES cover but the agent never reported.
    Two things must hold: the address is pinned back to one the agent reported, and the
    executor is resolved from THAT address — so the proposed address cannot select the
    agent either. If the resolution were hoisted above the pin, this run would be accepted
    and aimed at 10.0.0.5 by an agent chosen via 192.168.99.50.
    """
    db = _fresh()
    _scene(db, vm_ips=("10.0.0.5",))
    _agent(db, "lab")
    cmr.create(db, agent_id="agent-lab", cidr="192.168.99.0/24", created_by="admin")

    exc = _refusal(_Payload(agent_id="agent-lab", target="192.168.99.50"), db)
    assert exc.status_code == 400
    assert "10.0.0.5" in exc.detail, \
        f"the refusal should be about the address the agent reported: {exc.detail}"

    # And the broker still runs it, against the reported address, body notwithstanding.
    out = _resolve_agent_target(_Payload(agent_id="agent-host", target="192.168.99.50"), db)
    assert out["target_host"] == "10.0.0.5", \
        "a body-supplied address survived the pin"
    db.close()


def test_a_second_reported_address_is_still_accepted():
    """The pin is "one the agent reported", not "the first one". A dual-homed guest is a
    legitimate way out of an unroutable segment, so naming its other address must work —
    and the executor is then resolved from the address actually chosen."""
    db = _fresh()
    _scene(db, vm_ips=("192.168.235.99", "10.0.0.73"))
    _agent(db, "lab")
    cmr.create(db, agent_id="agent-lab", cidr="192.168.235.0/24", created_by="admin")
    # The routed address resolves to the routed agent…
    out = _resolve_agent_target(_Payload(agent_id="agent-lab", target="192.168.235.99"), db)
    assert out["target_host"] == "192.168.235.99"
    # …while the other reported address is outside the route, so the broker runs that one.
    out = _resolve_agent_target(_Payload(agent_id="agent-host", target="10.0.0.73"), db)
    assert out["target_host"] == "10.0.0.73"
    assert out["executor_name"] == "host"
    db.close()


# ── the routed agent gets its own preflight ─────────────────────────────────

def test_the_routed_agent_is_the_one_the_grant_checks_run_against():
    """`allowed_job_types`, `supports_ansible` and `status_of` all run against
    payload.agent_id, so a routed executor is gated on its own capabilities with no extra
    code. The message must name IT, or the operator grants the wrong agent."""
    db = _fresh()
    _scene(db, vm_ips=("192.168.235.99",))
    agent = _agent(db, "lab")
    cmr.create(db, agent_id="agent-lab", cidr="192.168.235.0/24", created_by="admin")
    # The grant is removed AFTER the route exists, because `create` refuses an ungranted
    # agent outright. This is the state an operator reaches by editing permissions later,
    # and it is the only way to reach the run-time branch.
    agent.allowed_job_types = json.dumps(["agent_hypervisor"])
    db.commit()
    exc = _refusal(_Payload(agent_id="agent-lab"), db)
    assert "lab" in exc.detail, exc.detail
    assert "Config-Management job type" in exc.detail, exc.detail
    db.close()


def test_an_offline_routed_agent_is_refused_before_anything_is_queued():
    db = _fresh()
    _scene(db, vm_ips=("192.168.235.99",))
    _agent(db, "lab")
    cmr.create(db, agent_id="agent-lab", cidr="192.168.235.0/24", created_by="admin")
    agent = db.query(RemoteAgent).filter(RemoteAgent.id == "agent-lab").first()
    agent.last_seen_at = datetime(2020, 1, 1)
    db.commit()
    exc = _refusal(_Payload(agent_id="agent-lab"), db)
    assert "not online" in exc.detail, exc.detail
    db.close()


# ── unchanged neighbours ────────────────────────────────────────────────────

def test_a_vm_with_no_address_is_still_refused_with_the_sync_remedy():
    """The remedy names `sync_guest_details` on the BROKER's connections.yaml, which is
    why `_hv_item` keeps broker_agent_id separately from the executor."""
    db = _fresh()
    _scene(db, vm_ips=())
    exc = _refusal(_Payload(agent_id="agent-host"), db)
    assert "sync_guest_details" in exc.detail, exc.detail
    db.close()


def test_a_vm_outside_the_connections_inventory_is_still_refused():
    db = _fresh()
    _scene(db)
    exc = _refusal(_Payload(agent_id="agent-host", target_id="vm-does-not-exist"), db)
    assert exc.status_code == 404
    assert "synced inventory" in exc.detail, exc.detail
    db.close()


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
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
