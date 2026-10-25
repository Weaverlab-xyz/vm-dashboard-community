"""Scoped Config-Management routes: on-premises guests, or one cloud's VMs (migration 0003).

An address alone does not name a machine — every AWS region's default VPC is 172.31.0.0/16,
and many sites use 192.168.1.0/24 — so a route says what it applies to, and a lookup only
ever considers routes in its target's own scope. What has to hold:

  * **Overlapping ranges coexist.** On-premises and AWS, two regions, two connections: one
    route each, and each target resolves to its own.
  * **No cross-capture.** A cloud route never governs an on-premises guest at the same
    address, which used to send the guest's run (and credentials) to an agent that
    reached a different machine; and one cloud's route never governs another's VMs.
  * **Delegated execution is unchanged.** The route's agent EXECUTES, independent of the
    agent brokering the connection — the vmrest / Hyper-V shape routes exist for — with
    or without a connection narrower.
  * **The upgrade keeps every answer.** A flagged route becomes four, an unflagged one
    on-premises, on an old-schema database, idempotently.

Runs under pytest, or standalone:  python tests/test_config_mgmt_route_scopes.py
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timezone

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.environ.setdefault("DATABASE_URL",
                      "sqlite:///" + os.path.join(tempfile.mkdtemp(), "scopes.db").replace("\\", "/"))
os.environ.setdefault("JWT_SECRET_KEY", "x" * 32)

try:
    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient
except ModuleNotFoundError as exc:
    print(f"SKIP: {exc}")
    sys.exit(0)

from web_dashboard.database import (Base, ConfigMgmtRoute, HypervisorConnection,  # noqa: E402
                                    HypervisorVMCache, Job, RemoteAgent, SessionLocal,
                                    engine, get_db)
from web_dashboard.api.config_mgmt import _resolve_agent_target  # noqa: E402
from web_dashboard.services import config_mgmt_route_service as cmr  # noqa: E402

Base.metadata.create_all(bind=engine)

GUEST = "10.40.1.15"


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _fresh():
    db = SessionLocal()
    for model in (ConfigMgmtRoute, HypervisorVMCache, HypervisorConnection, Job, RemoteAgent):
        db.query(model).delete()
    db.commit()
    return db


def _agent(db, name):
    db.add(RemoteAgent(id=f"agent-{name}", name=name, is_active=True, agent_version="2.9.0",
                       public_key="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI" + name,
                       last_seen_at=_utcnow(), allowed_job_types=None,
                       created_at=_utcnow()))
    db.commit()


def _connection(db, conn_id, *, kind="workstation", broker="agent-host", ip=GUEST,
                vm_id="vm-1"):
    """A hypervisor connection brokered by ``broker``, holding one synced guest."""
    db.add(HypervisorConnection(id=conn_id, kind=kind, name=conn_id, host="", port=8697,
                                agent_id=broker, agent_connection_name=conn_id,
                                is_active=True, created_at=_utcnow()))
    db.add(HypervisorVMCache(connection_id=conn_id, vm_id=vm_id, name=vm_id,
                             power_state="poweredOn", guest_os="oracleLinux9_64",
                             ip_addresses=json.dumps([ip]), synced_at=_utcnow()))
    db.commit()


class _GuestRun:
    def __init__(self, agent_id, connection_id="conn-ws", target_id="vm-1"):
        self.agent_id = agent_id
        self.target_kind = "vm"
        self.target_id = target_id
        self.connection_id = connection_id
        self.target = ""
        self.transport = ""
        self.port = 0


class _CloudRun:
    def __init__(self, agent_id, cloud, target_id):
        self.agent_id = agent_id
        self.target_kind = "vm"
        self.target_id = target_id
        self.connection_id = ""
        self.cloud = cloud
        self.target = GUEST
        self.transport = ""
        self.port = 0
        self.ansible_user = ""


class _Admin:
    is_effective_admin = True
    workgroups_list = []
    username = "tester"


def _gate(db, payload):
    """``(executor name, "")`` when accepted, ``("", detail)`` when refused."""
    try:
        out = _resolve_agent_target(payload, db, _Admin())
    except HTTPException as exc:
        return "", exc.detail
    return out["executor_name"], ""


def _deploy(db, job_id, job_type, **meta):
    meta.setdefault("private_ip", GUEST)
    meta.setdefault("instance_name", job_id)
    db.add(Job(id=job_id, job_type=job_type, status="completed", workgroup="team-a",
               extra_data=json.dumps(meta), created_at=_utcnow()))
    db.commit()


# ── the matcher ───────────────────────────────────────────────────────────────

def test_on_premises_and_a_cloud_can_route_the_same_range():
    db = _fresh()
    _agent(db, "lab")
    _agent(db, "vpc")
    cmr.create(db, agent_id="agent-lab", cidr="10.40.0.0/16")
    cmr.create(db, agent_id="agent-vpc", cidr="10.40.0.0/16", scope="aws")
    table = cmr.load_table(db)
    assert table.executor_for(GUEST, scope="onprem") == "agent-lab"
    assert table.executor_for(GUEST, scope="aws", region="us-east-2") == "agent-vpc"
    assert table.executor_for(GUEST, scope="azure") == "", "another cloud was captured"


def test_a_narrow_cloud_route_no_longer_captures_on_premises_guests():
    """The misdirection: before scopes, a /24 flagged for cloud VMs beat the on-premises
    /16 for the guests inside it, so their runs went to an agent in the VPC."""
    db = _fresh()
    _agent(db, "lab")
    _agent(db, "vpc")
    cmr.create(db, agent_id="agent-lab", cidr="10.40.0.0/16")
    cmr.create(db, agent_id="agent-vpc", cidr="10.40.1.0/24", scope="aws")
    table = cmr.load_table(db)
    assert table.executor_for(GUEST, fallback="agent-host") == "agent-lab"
    assert table.executor_for(GUEST, scope="aws") == "agent-vpc"


def test_two_regions_with_the_default_vpc_each_get_their_own_agent():
    db = _fresh()
    _agent(db, "east1")
    _agent(db, "east2")
    cmr.create(db, agent_id="agent-east1", cidr="172.31.0.0/16", scope="aws",
               region="us-east-1")
    cmr.create(db, agent_id="agent-east2", cidr="172.31.0.0/16", scope="aws",
               region="us-east-2")
    table = cmr.load_table(db)
    assert table.executor_for("172.31.5.5", scope="aws", region="us-east-1") == "agent-east1"
    assert table.executor_for("172.31.5.5", scope="aws", region="us-east-2") == "agent-east2"
    assert table.executor_for("172.31.5.5", scope="aws", region="eu-west-1") == ""


def test_a_narrowed_route_wins_at_an_equal_prefix_and_a_longer_prefix_still_wins():
    db = _fresh()
    _agent(db, "any")
    _agent(db, "site")
    _agent(db, "narrow")
    _connection(db, "conn-a", broker="agent-any")
    cmr.create(db, agent_id="agent-any", cidr="192.168.1.0/24")
    cmr.create(db, agent_id="agent-site", cidr="192.168.1.0/24", connection_id="conn-a")
    cmr.create(db, agent_id="agent-narrow", cidr="192.168.1.128/25")
    table = cmr.load_table(db)
    assert table.executor_for("192.168.1.5", connection_id="conn-a") == "agent-site"
    assert table.executor_for("192.168.1.5", connection_id="conn-b") == "agent-any"
    assert table.executor_for("192.168.1.200", connection_id="conn-a") == "agent-narrow"


def test_a_duplicate_in_one_scope_is_refused_naming_the_scope():
    db = _fresh()
    _agent(db, "vpc")
    cmr.create(db, agent_id="agent-vpc", cidr="10.40.0.0/16", scope="aws", region="us-east-2")
    try:
        cmr.create(db, agent_id="agent-vpc", cidr="10.40.0.0/16", scope="aws",
                   region="us-east-2")
    except cmr.ConfigRouteError as exc:
        assert "AWS · us-east-2" in str(exc) and "'vpc'" in str(exc)
    else:
        raise AssertionError("a duplicate in one scope was accepted")


def test_a_narrower_must_fit_its_scope():
    db = _fresh()
    _agent(db, "vpc")
    _connection(db, "conn-a", broker="agent-vpc")
    for kw in ({"scope": "onprem", "region": "us-east-2"},
               {"scope": "aws", "connection_id": "conn-a"},
               {"scope": "aws", "region": "mars-north-1"},
               {"scope": "onprem", "connection_id": "conn-missing"},
               {"scope": "oci"}):
        try:
            cmr.create(db, agent_id="agent-vpc", cidr="10.40.0.0/16", **kw)
        except cmr.ConfigRouteError:
            continue
        raise AssertionError(f"{kw} was accepted")


def test_moving_a_route_to_another_scope_drops_its_old_narrower():
    db = _fresh()
    _agent(db, "vpc")
    out = cmr.create(db, agent_id="agent-vpc", cidr="10.40.0.0/16", scope="aws",
                     region="us-east-2")
    moved = cmr.update(db, out["id"], scope="onprem")
    assert (moved["scope"], moved["region"], moved["connection_id"]) == ("onprem", "", "")


def test_scope_of_deploy_reads_each_clouds_region_key():
    assert cmr.scope_of_deploy("ec2_deploy", {"region": "us-east-2"}) == ("aws", "us-east-2")
    assert cmr.scope_of_deploy("azure_deploy", {"location": "EastUS"}) == ("azure", "eastus")
    assert cmr.scope_of_deploy("gce_deploy", {"zone": "us-central1-a"}) == ("gcp", "us-central1")
    assert cmr.scope_of_deploy("proxmox_deploy", {}) == ("", "")


# ── delegated execution: the broker discovers, the routed agent executes ─────

def test_the_routed_agent_executes_a_workstation_guest_and_the_broker_is_refused():
    for kind in ("workstation", "hyperv"):
        db = _fresh()
        _agent(db, "host")       # brokers the connection: vmrest / Hyper-V on Windows
        _agent(db, "runner")     # executes: on the guests' network
        _connection(db, "conn-ws", kind=kind)
        cmr.create(db, agent_id="agent-runner", cidr="10.40.1.0/24")
        assert _gate(db, _GuestRun("agent-runner")) == ("runner", ""), kind
        name, detail = _gate(db, _GuestRun("agent-host"))
        assert not name and "routed to agent 'runner'" in detail, (kind, detail)
        db.close()


def test_a_connection_narrower_keeps_the_routed_agent_executing():
    db = _fresh()
    _agent(db, "host")
    _agent(db, "runner")
    _connection(db, "conn-ws")
    cmr.create(db, agent_id="agent-runner", cidr="10.40.1.0/24", connection_id="conn-ws")
    assert _gate(db, _GuestRun("agent-runner")) == ("runner", "")


def test_a_route_narrowed_to_another_connection_falls_back_to_the_broker():
    db = _fresh()
    _agent(db, "host")
    _agent(db, "runner")
    _connection(db, "conn-ws")
    _connection(db, "conn-other", broker="agent-host", vm_id="vm-2", ip="10.99.0.5")
    cmr.create(db, agent_id="agent-runner", cidr="10.40.1.0/24", connection_id="conn-other")
    assert _gate(db, _GuestRun("agent-host")) == ("host", ""), "the broker lost its guest"
    name, _detail = _gate(db, _GuestRun("agent-runner"))
    assert not name, "a route for another connection captured this guest"


def test_a_cloud_route_does_not_capture_a_guest_at_the_same_address():
    db = _fresh()
    _agent(db, "host")
    _agent(db, "vpc")
    _connection(db, "conn-ws")
    cmr.create(db, agent_id="agent-vpc", cidr="10.40.1.0/24", scope="aws")
    assert _gate(db, _GuestRun("agent-host")) == ("host", "")
    name, _detail = _gate(db, _GuestRun("agent-vpc"))
    assert not name, "a cloud route captured an on-premises guest"


# ── cloud VMs at the same address in two clouds ───────────────────────────────

def test_an_azure_vm_in_an_aws_only_range_stays_on_the_remote_worker():
    db = _fresh()
    _agent(db, "vpc")
    _deploy(db, "ec2-1", "ec2_deploy", region="us-east-2")
    _deploy(db, "az-1", "azure_deploy", location="eastus")
    cmr.create(db, agent_id="agent-vpc", cidr="10.40.0.0/16", scope="aws")
    assert _gate(db, _CloudRun("agent-vpc", "aws", "ec2-1")) == ("vpc", "")
    name, detail = _gate(db, _CloudRun("agent-vpc", "azure", "az-1"))
    assert not name and "remote worker" in detail and "Azure" in detail, detail


# ── the API ───────────────────────────────────────────────────────────────────

def test_the_api_refuses_the_superseded_flag_and_takes_a_scope():
    from web_dashboard.api import connections
    from web_dashboard.api.auth import get_current_user
    db = _fresh()
    _agent(db, "vpc")
    app = FastAPI()
    app.include_router(connections.router)

    def _db():
        s = SessionLocal()
        try:
            yield s
        finally:
            s.close()
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_user] = lambda: _Admin()
    c = TestClient(app)
    r = c.post("/api/connections/config-mgmt-routes",
               json={"agent_id": "agent-vpc", "cidr": "10.40.0.0/16", "include_cloud_vms": True})
    assert r.status_code == 400 and "scope" in r.json()["detail"]
    r = c.post("/api/connections/config-mgmt-routes",
               json={"agent_id": "agent-vpc", "cidr": "10.40.0.0/16", "scope": "aws",
                     "region": "us-east-2"})
    assert r.status_code == 201, r.text
    listed = c.get("/api/connections/config-mgmt-routes").json()
    assert listed["routes"][0]["scope_text"] == "AWS · us-east-2"
    assert [s["id"] for s in listed["scopes"]] == list(cmr.SCOPES)
    res = c.get("/api/connections/config-mgmt-routes/resolve",
                params={"address": GUEST, "scope": "aws", "region": "us-east-2"}).json()
    assert res["agent_id"] == "agent-vpc"
    res = c.get("/api/connections/config-mgmt-routes/resolve",
                params={"address": GUEST}).json()
    assert res["source"] == "none", "an on-premises lookup matched a cloud route"


# ── the upgrade ───────────────────────────────────────────────────────────────

_OLD_SCHEMA = """CREATE TABLE config_mgmt_routes (
    id VARCHAR(36) PRIMARY KEY, agent_id VARCHAR(36) NOT NULL, cidr VARCHAR(64) NOT NULL,
    label VARCHAR(64), is_active BOOLEAN NOT NULL, include_cloud_vms BOOLEAN,
    created_at DATETIME NOT NULL, created_by VARCHAR(100), updated_at DATETIME,
    CONSTRAINT uq_config_mgmt_route_cidr UNIQUE (cidr))"""


def _old_database():
    from sqlalchemy import create_engine, text
    path = os.path.join(tempfile.mkdtemp(), "old.db")
    eng = create_engine(f"sqlite:///{path}")
    with eng.begin() as c:
        c.execute(text(_OLD_SCHEMA))
        c.execute(text("CREATE INDEX ix_config_mgmt_routes_agent_id "
                       "ON config_mgmt_routes (agent_id)"))
        c.execute(text("CREATE TABLE schema_markers (key VARCHAR PRIMARY KEY, "
                       "applied_at DATETIME, detail TEXT)"))
        c.execute(text(
            "INSERT INTO config_mgmt_routes VALUES "
            "('flagged','agent-vpc','10.40.0.0/16','vpc',1,1,'2026-01-01','t',NULL),"
            "('plain','agent-lab','192.168.1.0/24',NULL,1,NULL,'2026-01-01','t',NULL)"))
    return eng


def _migrate(eng):
    from web_dashboard import schema_migrations as sm
    with eng.begin() as c:
        return sm.apply_numbered(c, [m for m in sm.MIGRATIONS
                                     if m.name == "0003_config_route_scopes"])


def test_the_upgrade_keeps_every_answer():
    from sqlalchemy import inspect
    from sqlalchemy.orm import sessionmaker
    eng = _old_database()
    assert _migrate(eng) == ["0003_config_route_scopes"]
    s = sessionmaker(bind=eng)()
    rows = s.query(ConfigMgmtRoute).all()
    assert sorted((r.id if r.id in ("flagged", "plain") else "new", r.scope) for r in rows) == [
        ("flagged", "onprem"), ("new", "aws"), ("new", "azure"), ("new", "gcp"),
        ("plain", "onprem")]
    table = cmr.load_table(s)
    # What each lookup answered before the upgrade, it answers after.
    assert table.executor_for("10.40.1.15") == "agent-vpc"
    for cloud in ("aws", "azure", "gcp"):
        assert table.executor_for("10.40.1.15", scope=cloud, region="r") == "agent-vpc"
    assert table.executor_for("192.168.1.5") == "agent-lab"
    assert table.executor_for("192.168.1.5", scope="aws") == ""
    names = {u["name"] for u in inspect(eng).get_unique_constraints("config_mgmt_routes")}
    assert names == {"uq_config_mgmt_route_scope"}
    s.close()


def test_the_upgrade_runs_once_and_is_a_no_op_on_a_fresh_table():
    eng = _old_database()
    _migrate(eng)
    assert _migrate(eng) == []
    from sqlalchemy import create_engine
    fresh = create_engine("sqlite:///" + os.path.join(tempfile.mkdtemp(), "fresh.db"))
    Base.metadata.create_all(bind=fresh)
    assert _migrate(fresh) == ["0003_config_route_scopes"]
    from sqlalchemy.orm import sessionmaker
    assert sessionmaker(bind=fresh)().query(ConfigMgmtRoute).count() == 0


def test_postgresql_drops_the_old_constraint_in_place():
    """No PostgreSQL here, so the branch is pinned by source, as the other migration
    tests pin dialect-specific DDL."""
    import inspect as pyinspect

    from web_dashboard import schema_migrations as sm
    src = pyinspect.getsource(sm._drop_cidr_only_unique)
    assert "DROP CONSTRAINT IF EXISTS uq_config_mgmt_route_cidr" in src
    assert 'conn.dialect.name == "postgresql"' in src


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            import traceback
            traceback.print_exc()
            print(f"FAIL {fn.__name__}: {e!r}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
