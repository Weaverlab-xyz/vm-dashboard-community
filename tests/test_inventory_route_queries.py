"""Resolving the executing agent must not cost a query per VM.

`_target_spec` and `_hv_item` are PURE — no session — and they are called once per row.
So the obvious way to make a target report its executing agent is to look the route up
inside one of them, which is a query per VM on a page that already loads the whole estate.
`_hypervisor_items` loads the table once instead and threads it down, in the same shape as
the per-kind bulk workgroup-override lookup that sits beside it.

This is the sibling of
tests/test_inventory_hypervisor.py::test_the_override_lookup_is_one_query_per_kind_not_per_vm,
and it exists for the same reason: the regression is invisible at test-data scale and
obvious at customer scale.

Runs under pytest, or standalone:  python tests/test_inventory_route_queries.py
"""
import json
import os
import sys
import tempfile
from datetime import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.environ.setdefault("DATABASE_URL",
                      "sqlite:///" + os.path.join(tempfile.mkdtemp(), "invq.db").replace("\\", "/"))
os.environ.setdefault("JWT_SECRET_KEY", "x" * 32)

try:
    from sqlalchemy import event

    from web_dashboard.database import (Base, engine, SessionLocal, ConfigMgmtRoute,
                                        HypervisorConnection, HypervisorVMCache, Job,
                                        RemoteAgent)
    from web_dashboard.services import config_mgmt_route_service as cmr
    from web_dashboard.services import inventory_service
except Exception as exc:  # noqa: BLE001
    print(f"SKIP: {exc}")
    sys.exit(0)

Base.metadata.create_all(bind=engine)


class _Counter:
    """Counts statements the ORM actually sends, by substring."""

    def __init__(self):
        self.statements = []

    def __enter__(self):
        self._listen = lambda conn, cursor, stmt, params, ctx, many: \
            self.statements.append(stmt)
        event.listen(engine, "before_cursor_execute", self._listen)
        return self

    def __exit__(self, *exc):
        event.remove(engine, "before_cursor_execute", self._listen)
        return False

    def matching(self, needle: str) -> int:
        return sum(1 for s in self.statements if needle in s)

    def total(self) -> int:
        return len(self.statements)


def _seed(db, *, vms: int, routes: int = 1):
    for model in (ConfigMgmtRoute, HypervisorVMCache, HypervisorConnection, RemoteAgent, Job):
        db.query(model).delete()
    db.commit()
    for name in ("host", "lab"):
        db.add(RemoteAgent(id=f"agent-{name}", name=name, is_active=True,
                           agent_version="2.5.0",
                           public_key="ssh-ed25519 AAAA" + name,
                           last_seen_at=datetime.utcnow(), created_at=datetime.utcnow()))
    # Two kinds, so the per-kind override lookup runs more than once and the route load
    # is visibly independent of it.
    for kind, cid in (("workstation", "conn-ws"), ("vsphere", "conn-vs")):
        db.add(HypervisorConnection(id=cid, kind=kind, name=cid, host="", port=443,
                                    agent_id="agent-host", agent_connection_name=cid,
                                    is_active=True, created_at=datetime.utcnow()))
    db.commit()
    for i in range(vms):
        cid = "conn-ws" if i % 2 == 0 else "conn-vs"
        db.add(HypervisorVMCache(
            connection_id=cid, vm_id=f"vm-{i}", name=f"vm-{i}", power_state="poweredOn",
            ip_addresses=json.dumps([f"192.168.235.{i % 200 + 1}"]),
            synced_at=datetime.utcnow()))
    db.commit()
    for r in range(routes):
        cmr.create(db, agent_id="agent-lab", cidr=f"10.{r}.0.0/16", created_by="admin")
    cmr.create(db, agent_id="agent-lab", cidr="192.168.235.0/24", created_by="admin")


def test_the_route_table_is_read_once_per_collect_not_per_vm():
    db = SessionLocal()
    _seed(db, vms=40)
    with _Counter() as counter:
        items = inventory_service.collect(db)
    reads = counter.matching("config_mgmt_routes")
    assert reads == 1, \
        (f"the route table was read {reads} times for 40 VMs — it must be loaded once "
         f"per collect() and threaded into the projection")
    assert len(items) == 40, f"the seed did not produce 40 rows: {len(items)}"
    db.close()


def test_the_query_count_does_not_grow_with_the_number_of_vms():
    """Compared rather than absolute: an absolute budget breaks on unrelated query churn,
    while equality between two estate sizes stays true unless something became per-row."""
    counts = {}
    for size in (5, 60):
        db = SessionLocal()
        _seed(db, vms=size)
        with _Counter() as counter:
            inventory_service.collect(db)
        counts[size] = counter.total()
        db.close()
    assert counts[5] == counts[60], \
        (f"collect() issued {counts[5]} statements for 5 VMs and {counts[60]} for 60 — "
         f"something in the projection is querying per row")


def test_the_executor_is_actually_resolved_on_the_collected_rows():
    """Guards against the cheapest way to pass the two tests above: not resolving at all.

    Also pins that `broker_agent_id` stays the DISCOVERING agent — the no-address remedy
    names that agent's connections.yaml, so overwriting it would send an operator to the
    wrong file.
    """
    db = SessionLocal()
    _seed(db, vms=4)
    rows = [i for i in inventory_service.collect(db) if i.get("kind") == "vm"]
    assert rows, "no VM rows collected"
    for row in rows:
        assert row["agent_id"] == "agent-lab", \
            f"{row['ip']} was not routed to the delegated agent: {row['agent_id']}"
        assert row["broker_agent_id"] == "agent-host", \
            "broker_agent_id no longer records the agent that discovered the VM"
    db.close()


def test_with_no_routes_the_executor_is_the_brokering_agent():
    """The backwards-compatibility contract, asserted through the real projection rather
    than the matcher alone."""
    db = SessionLocal()
    _seed(db, vms=4)
    db.query(ConfigMgmtRoute).delete()
    db.commit()
    rows = [i for i in inventory_service.collect(db) if i.get("kind") == "vm"]
    assert rows
    for row in rows:
        assert row["agent_id"] == row["broker_agent_id"] == "agent-host", row
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
