"""Config-Management routes against a real database: CRUD, the table load, and cleanup.

The matching and normalisation rules are tested without a database in
tests/test_config_mgmt_route_match.py — this file covers only what needs storage: the
unique constraint, which rows `load_table` keeps, and the explicit cleanup that stands in
for a foreign key SQLite does not enforce.

Runs under pytest, or standalone:  python tests/test_config_mgmt_routes.py
"""
import json
import os
import sys
import tempfile
from datetime import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.environ.setdefault("DATABASE_URL",
                      "sqlite:///" + os.path.join(tempfile.mkdtemp(), "routes.db").replace("\\", "/"))
os.environ.setdefault("JWT_SECRET_KEY", "x" * 32)

try:
    from web_dashboard.database import (Base, engine, SessionLocal, ConfigMgmtRoute,
                                        HypervisorConnection, HypervisorVMCache, RemoteAgent)
    from web_dashboard.services import agent_service
    from web_dashboard.services import config_mgmt_route_service as cmr
except Exception as exc:  # noqa: BLE001
    print(f"SKIP: {exc}")
    sys.exit(0)

Base.metadata.create_all(bind=engine)


def _fresh():
    """A session with no routes, agents, connections or cached VMs."""
    db = SessionLocal()
    for model in (ConfigMgmtRoute, HypervisorVMCache, HypervisorConnection, RemoteAgent):
        db.query(model).delete()
    db.commit()
    return db


def _agent(db, name, *, active=True, granted=True, version="2.5.0"):
    row = RemoteAgent(
        id=f"agent-{name}", name=name, is_active=active, agent_version=version,
        # An enrolled agent always has one, and status_of reads "enrolling" without it.
        public_key="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI" + name,
        last_seen_at=datetime.utcnow(),
        # None means "every type the dashboard supports", which includes agent_ansible.
        # A narrowed list is how a test takes the grant away.
        allowed_job_types=None if granted else json.dumps(["agent_hypervisor"]),
        created_at=datetime.utcnow())
    db.add(row)
    db.commit()
    return row


def _conn(db, agent_id, name="ws"):
    row = HypervisorConnection(id=f"conn-{name}", kind="workstation", name=name,
                               host="", port=8697, agent_id=agent_id,
                               agent_connection_name=name, is_active=True,
                               created_at=datetime.utcnow())
    db.add(row)
    db.commit()
    return row


def _vm(db, conn_id, vm_id, ip):
    db.add(HypervisorVMCache(connection_id=conn_id, vm_id=vm_id, name=vm_id,
                             power_state="poweredOn",
                             ip_addresses=json.dumps([ip] if ip else []),
                             synced_at=datetime.utcnow()))
    db.commit()


# ── create ───────────────────────────────────────────────────────────────────

def test_a_route_is_stored_normalised_never_as_typed():
    db = _fresh()
    _agent(db, "b")
    out = cmr.create(db, agent_id="agent-b", cidr=" 192.168.235.99 ", label="one host",
                     created_by="admin")
    assert out["cidr"] == "192.168.235.99/32", out
    assert db.query(ConfigMgmtRoute).one().cidr == "192.168.235.99/32"
    db.close()


def test_one_range_names_one_agent():
    """The single ambiguity longest-prefix cannot settle, so it is refused at write time
    with a message naming the row that already holds it — rather than settled later by an
    invisible created_at comparison the operator cannot see."""
    db = _fresh()
    _agent(db, "b")
    _agent(db, "c")
    cmr.create(db, agent_id="agent-b", cidr="192.168.235.0/24", label="vmnet1",
               created_by="admin")
    try:
        cmr.create(db, agent_id="agent-c", cidr="192.168.235.0/24", created_by="admin")
    except cmr.ConfigRouteError as exc:
        assert "b" in str(exc) and "vmnet1" in str(exc), exc
        assert "narrower" in str(exc), "the refusal should point at the way out"
    else:
        raise AssertionError("two agents were allowed to claim one range")
    assert db.query(ConfigMgmtRoute).count() == 1
    db.close()


def test_overlapping_ranges_of_different_sizes_are_allowed():
    """The whole point of longest-prefix: a broad range with a narrower exception inside
    it has to be expressible, or a lab with one odd segment cannot be described."""
    db = _fresh()
    _agent(db, "b")
    _agent(db, "c")
    cmr.create(db, agent_id="agent-b", cidr="192.168.0.0/16", created_by="admin")
    cmr.create(db, agent_id="agent-c", cidr="192.168.235.0/24", created_by="admin")
    assert cmr.executor_for(db, "192.168.235.5") == "agent-c"
    assert cmr.executor_for(db, "192.168.9.5") == "agent-b"
    db.close()


def test_a_route_naming_an_unusable_agent_is_refused_at_create():
    """Checked in the form rather than left to the run: a route naming an agent that
    cannot take the job type resolves EVERY run in its range to a refusal, and the
    operator would otherwise meet that as a failed job."""
    db = _fresh()
    _agent(db, "revoked", active=False)
    _agent(db, "ungranted", granted=False)
    for agent_id, needle in (("agent-missing", "not registered"),
                             ("agent-revoked", "revoked"),
                             ("agent-ungranted", "agent_ansible")):
        try:
            cmr.create(db, agent_id=agent_id, cidr="10.0.0.0/24", created_by="admin")
        except cmr.ConfigRouteError as exc:
            assert needle in str(exc), f"{agent_id}: {exc}"
        else:
            raise AssertionError(f"{agent_id} was accepted as a route target")
    assert db.query(ConfigMgmtRoute).count() == 0
    db.close()


# ── the table load ───────────────────────────────────────────────────────────

def test_an_inactive_route_is_not_loaded_and_falls_back():
    """Deactivating reverts to the discovering agent without losing the record of the
    decision — which is why `is_active` exists instead of making delete the only way."""
    db = _fresh()
    _agent(db, "b")
    out = cmr.create(db, agent_id="agent-b", cidr="192.168.235.0/24", created_by="admin")
    assert cmr.executor_for(db, "192.168.235.5", fallback="broker") == "agent-b"
    cmr.update(db, out["id"], is_active=False)
    assert cmr.executor_for(db, "192.168.235.5", fallback="broker") == "broker"
    cmr.update(db, out["id"], is_active=True)
    assert cmr.executor_for(db, "192.168.235.5", fallback="broker") == "agent-b"
    db.close()


def test_a_revoked_agents_route_is_still_loaded():
    """Deliberately NOT filtered out. Dropping it here would silently route runs back to
    the agent that discovered the VM — an agent with no path to it — which is the original
    bug wearing a different hat. It stays visible so the operator can be told.
    """
    db = _fresh()
    agent = _agent(db, "b")
    cmr.create(db, agent_id="agent-b", cidr="192.168.235.0/24", created_by="admin")
    agent.is_active = False
    db.commit()
    assert cmr.executor_for(db, "192.168.235.5", fallback="broker") == "agent-b", \
        "a revoked delegate silently fell back instead of being surfaced"
    db.close()


def test_the_bulk_table_and_the_single_lookup_agree():
    """Two entry points into one rule. The picker and the inventory use `load_table` once
    for the whole page; the enqueue gate uses `executor_for` for one address. They must
    never answer differently for the same address."""
    db = _fresh()
    _agent(db, "b")
    _agent(db, "c")
    cmr.create(db, agent_id="agent-b", cidr="10.0.0.0/8", created_by="admin")
    cmr.create(db, agent_id="agent-c", cidr="10.1.2.0/24", created_by="admin")
    table = cmr.load_table(db)
    for address in ("10.1.2.3", "10.9.9.9", "192.168.1.1", "", "not-an-ip"):
        assert table.executor_for(address, fallback="fb") == \
            cmr.executor_for(db, address, fallback="fb"), address
    db.close()


def test_list_routes_reads_most_specific_first():
    """The list is the order the matcher applies, so an operator can see which row wins."""
    db = _fresh()
    _agent(db, "b")
    cmr.create(db, agent_id="agent-b", cidr="192.168.0.0/16", created_by="admin")
    cmr.create(db, agent_id="agent-b", cidr="192.168.235.99/32", created_by="admin")
    cmr.create(db, agent_id="agent-b", cidr="192.168.235.0/24", created_by="admin")
    assert [r["cidr"] for r in cmr.list_routes(db)] == [
        "192.168.235.99/32", "192.168.235.0/24", "192.168.0.0/16"]
    db.close()


# ── coverage feedback ────────────────────────────────────────────────────────

def test_match_counts_answers_whether_a_range_bound_to_anything():
    """The "did my CIDR do anything" feedback, and the reason a route needs no Test
    button: there is nothing to dial, so coverage is the only useful signal."""
    db = _fresh()
    _agent(db, "a")
    _agent(db, "b")
    conn = _conn(db, "agent-a")
    _vm(db, conn.id, "vm-1", "192.168.235.10")
    _vm(db, conn.id, "vm-2", "192.168.235.11")
    _vm(db, conn.id, "vm-3", "10.0.0.5")
    _vm(db, conn.id, "vm-4", "")            # no address yet — cannot be covered
    routed = cmr.create(db, agent_id="agent-b", cidr="192.168.235.0/24", created_by="admin")
    empty = cmr.create(db, agent_id="agent-b", cidr="172.16.0.0/24", created_by="admin")
    counts = cmr.match_counts(db)
    assert counts[routed["id"]] == 2, counts
    assert counts[empty["id"]] == 0, counts
    db.close()


def test_a_deactivated_connections_vms_are_not_counted():
    """Matches what the inventory does: a deactivated connection's cache is frozen, not
    current, so counting it would overstate what a route governs."""
    db = _fresh()
    _agent(db, "a")
    _agent(db, "b")
    conn = _conn(db, "agent-a")
    _vm(db, conn.id, "vm-1", "192.168.235.10")
    conn.is_active = False
    db.commit()
    routed = cmr.create(db, agent_id="agent-b", cidr="192.168.235.0/24", created_by="admin")
    assert cmr.match_counts(db)[routed["id"]] == 0
    db.close()


# ── cleanup ──────────────────────────────────────────────────────────────────

def test_deleting_an_agent_deletes_its_routes():
    """The FK's CASCADE is ORM decoration: SQLite does not enforce foreign keys unless
    PRAGMA foreign_keys=ON is set per connection, and nothing sets it. A surviving route
    would keep resolving runs to an agent id that no longer exists — so they would be
    REFUSED rather than falling back to the discovering agent."""
    db = _fresh()
    keep = _agent(db, "keep")
    doomed = _agent(db, "doomed")
    cmr.create(db, agent_id="agent-doomed", cidr="192.168.235.0/24", created_by="admin")
    cmr.create(db, agent_id="agent-keep", cidr="10.0.0.0/24", created_by="admin")
    agent_service.delete_agent(db, doomed)
    remaining = [r.cidr for r in db.query(ConfigMgmtRoute).all()]
    assert remaining == ["10.0.0.0/24"], remaining
    assert cmr.executor_for(db, "192.168.235.5", fallback="broker") == "broker"
    db.close()


def test_deleting_a_route_reverts_and_promotes_nothing():
    """Unlike a default hypervisor connection, an absent route has a correct meaning — the
    discovering agent — so nothing is promoted in its place."""
    db = _fresh()
    _agent(db, "b")
    out = cmr.create(db, agent_id="agent-b", cidr="192.168.235.0/24", created_by="admin")
    cmr.delete(db, out["id"])
    assert db.query(ConfigMgmtRoute).count() == 0
    assert cmr.executor_for(db, "192.168.235.5", fallback="broker") == "broker"
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
