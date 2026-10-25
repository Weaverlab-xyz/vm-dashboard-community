"""Config-Management routes narrowed to one network (migration 0004).

A cloud route may name the exact AWS VPC, Azure VNet or GCP network it governs — the answer
region and workstream only approximate. What has to hold:

  * **One parser.** Each cloud's network is read and normalised in one place
    (``network_of``), from what the launchers record and the backfill fills in.
  * **Exact matching.** Two VPCs in one region sharing a range each get their own agent,
    and a VM with no recorded network never matches a network-narrowed route.
  * **Recording.** New deploys record their network from the SDK's own response.
  * **The backfill** fills only what is missing, skips a VM the cloud no longer has, and is
    queued once when a network-narrowed route is saved.
  * **Deployed agents** narrow their own routes to the network their host runs in.
  * **The upgrade** reaches the same schema from every state a database can be in.

Runs under pytest, or standalone:  python tests/test_config_mgmt_route_networks.py
"""
import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timezone

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.environ.setdefault("DATABASE_URL",
                      "sqlite:///" + os.path.join(tempfile.mkdtemp(), "nets.db").replace("\\", "/"))
os.environ.setdefault("JWT_SECRET_KEY", "x" * 32)

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
except ModuleNotFoundError as exc:
    print(f"SKIP: {exc}")
    sys.exit(0)

from web_dashboard.database import (AgentHost, Base, ConfigMgmtRoute, Job,  # noqa: E402
                                    RemoteAgent, SessionLocal, engine, get_db)
from web_dashboard.services import config_mgmt_route_service as cmr  # noqa: E402
from web_dashboard.services import route_network_backfill as rnb  # noqa: E402

Base.metadata.create_all(bind=engine)

VPC_A = "vpc-0aaaaaaaaaaaaaaa1"
VPC_B = "vpc-0bbbbbbbbbbbbbbb2"
SUB = "00000000-0000-0000-0000-000000000001"
VNET = (f"/subscriptions/{SUB}/resourcegroups/rg-net/providers/microsoft.network/"
        f"virtualnetworks/vnet-a")
GCP_NET = "projects/my-project-1/global/networks/vpc-a"


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _fresh():
    db = SessionLocal()
    for model in (AgentHost, ConfigMgmtRoute, Job, RemoteAgent):
        db.query(model).delete()
    db.commit()
    return db


def _agent(db, name):
    db.add(RemoteAgent(id=f"agent-{name}", name=name, is_active=True, agent_version="2.9.0",
                       public_key="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI" + name,
                       last_seen_at=_utcnow(), allowed_job_types=None, created_at=_utcnow()))
    db.commit()


def _deploy(db, job_id, job_type="ec2_deploy", **meta):
    meta.setdefault("private_ip", "10.0.1.5")
    meta.setdefault("region", "us-east-2")
    db.add(Job(id=job_id, job_type=job_type, status="completed", workgroup="team-a",
               extra_data=json.dumps(meta), created_at=_utcnow()))
    db.commit()


# ── the parser ────────────────────────────────────────────────────────────────

def test_each_clouds_network_is_read_and_normalised_in_one_place():
    assert cmr.network_of("aws", {"vpc_id": VPC_A.upper().replace("VPC", "vpc")}) == VPC_A
    # Azure: the recorded VNet, else cut down from the deploy's subnet id; any case.
    subnet = (f"/subscriptions/{SUB}/resourceGroups/RG-Net/providers/Microsoft.Network/"
              f"virtualNetworks/VNet-A/subnets/default")
    assert cmr.network_of("azure", {"subnet_id": subnet}) == VNET
    assert cmr.network_of("azure", {"vnet_id": VNET.upper()}) == VNET
    # GCP: a full self-link is cut to projects/…/global/networks/….
    link = f"https://www.googleapis.com/compute/v1/{GCP_NET}"
    assert cmr.network_of("gcp", {"network": link}) == GCP_NET
    assert cmr.network_of("aws", {}) == "" and cmr.network_of("onprem", {"vpc_id": VPC_A}) == ""


def test_a_network_in_another_clouds_shape_is_refused():
    db = _fresh()
    _agent(db, "a")
    for kw in ({"scope": "aws", "network": GCP_NET}, {"scope": "gcp", "network": VPC_A},
               {"scope": "azure", "network": "vnet-a"}, {"scope": "aws", "network": "vpc-xyz"},
               {"scope": "onprem", "network": VPC_A}):
        try:
            cmr.create(db, agent_id="agent-a", cidr="10.0.0.0/16", **kw)
        except cmr.ConfigRouteError:
            continue
        raise AssertionError(f"{kw} was accepted")
    out = cmr.create(db, agent_id="agent-a", cidr="10.0.0.0/16", scope="gcp",
                     network=f"https://www.googleapis.com/compute/v1/{GCP_NET}")
    assert out["network"] == GCP_NET


# ── matching ──────────────────────────────────────────────────────────────────

def test_two_vpcs_in_one_region_sharing_a_range_each_get_their_own_agent():
    db = _fresh()
    _agent(db, "a")
    _agent(db, "b")
    cmr.create(db, agent_id="agent-a", cidr="10.0.0.0/16", scope="aws", network=VPC_A)
    cmr.create(db, agent_id="agent-b", cidr="10.0.0.0/16", scope="aws", network=VPC_B)
    table = cmr.load_table(db)
    at = dict(scope="aws", region="us-east-2", workstream="team-a")
    assert table.executor_for("10.0.1.5", network=VPC_A, **at) == "agent-a"
    assert table.executor_for("10.0.1.5", network=VPC_B, **at) == "agent-b"


def test_a_vm_with_no_recorded_network_never_matches_a_network_narrowed_route():
    db = _fresh()
    _agent(db, "narrow")
    _agent(db, "broad")
    cmr.create(db, agent_id="agent-narrow", cidr="10.0.1.0/24", scope="aws", network=VPC_A)
    table = cmr.load_table(db)
    assert table.executor_for("10.0.1.5", scope="aws", network="") == "", \
        "a VM with no network was handed to a network-narrowed route"
    cmr.create(db, agent_id="agent-broad", cidr="10.0.0.0/16", scope="aws")
    table = cmr.load_table(db)
    assert table.executor_for("10.0.1.5", scope="aws", network="") == "agent-broad"
    assert table.executor_for("10.0.1.5", scope="aws", network=VPC_A) == "agent-narrow"


def test_network_counts_as_a_narrower_at_an_equal_prefix():
    db = _fresh()
    for n in ("region", "net"):
        _agent(db, n)
    cmr.create(db, agent_id="agent-region", cidr="10.0.0.0/16", scope="aws", region="us-east-2")
    cmr.create(db, agent_id="agent-net", cidr="10.0.0.0/16", scope="aws", region="us-east-2",
               network=VPC_A)
    table = cmr.load_table(db)
    assert table.executor_for("10.0.1.5", scope="aws", region="us-east-2",
                              network=VPC_A) == "agent-net"
    assert table.executor_for("10.0.1.5", scope="aws", region="us-east-2",
                              network=VPC_B) == "agent-region"


def test_the_deploy_job_decides_the_vms_network():
    db = _fresh()
    _deploy(db, "ec2-a", vpc_id=VPC_A)
    job = db.query(Job).filter(Job.id == "ec2-a").first()
    assert cmr.scope_of_job(job) == ("aws", "us-east-2", "team-a", VPC_A)


# ── recording at launch ───────────────────────────────────────────────────────

def test_the_ec2_launcher_records_the_vpc():
    from web_dashboard.services import aws_service

    class _EC2:
        def run_instances(self, **kw):
            return {"Instances": [{"InstanceId": "i-1", "State": {"Name": "pending"},
                                   "PrivateIpAddress": "10.0.1.5", "VpcId": VPC_A,
                                   "SubnetId": "subnet-1"}]}
    saved = (aws_service._get_ec2, aws_service._root_bdm_sync)
    try:
        aws_service._get_ec2 = lambda region: _EC2()
        aws_service._root_bdm_sync = lambda *a, **k: []
        out = aws_service._launch_instance_sync(
            "us-east-2", "ami-1", "web-1", "t3.micro", "ssh-ed25519 AAAA", "subnet-1", ["sg-1"])
    finally:
        aws_service._get_ec2, aws_service._root_bdm_sync = saved
    assert out["vpc_id"] == VPC_A and out["subnet_id"] == "subnet-1"


def test_an_azure_subnet_id_names_its_vnet():
    from web_dashboard.services.azure_service import vnet_of_subnet
    subnet = (f"/subscriptions/{SUB}/resourceGroups/RG-Net/providers/Microsoft.Network/"
              f"virtualNetworks/VNet-A/subnets/default")
    assert vnet_of_subnet(subnet) == VNET
    assert vnet_of_subnet("") == "" and vnet_of_subnet("/not/an/arm/id") == ""


def test_the_gce_deploy_keeps_the_network_in_its_metadata():
    src = open(os.path.join(_ROOT, "web_dashboard", "services", "gcp_vm_service.py"),
               encoding="utf-8").read()
    assert '"network":       result.get("network", "")' in src, \
        "gcp_vm_service's metadata whitelist drops the network the launcher reports"


# ── the backfill ──────────────────────────────────────────────────────────────

def _with_clouds(aws=None, azure=None, gcp=None):
    from web_dashboard.services import aws_service, azure_service, config_service, gcp_service
    saved = (aws_service.instance_network, azure_service.nic_vnet, gcp_service.instance_network,
             config_service.get)
    calls = []

    async def _aws(region, iid):
        calls.append(("aws", iid))
        return (aws or {}).get(iid)

    async def _az(rg, nic):
        calls.append(("azure", nic))
        return (azure or {}).get(nic)

    async def _gcp(project, zone, name):
        calls.append(("gcp", name))
        return (gcp or {}).get(name)
    aws_service.instance_network = _aws
    azure_service.nic_vnet = _az
    gcp_service.instance_network = _gcp
    real_get = config_service.get
    config_service.get = lambda key, default="", workgroup=None: (
        "my-project-1" if key == "gcp_project_id" else real_get(key, default, workgroup))

    def restore():
        (aws_service.instance_network, azure_service.nic_vnet, gcp_service.instance_network,
         config_service.get) = saved
    return calls, restore


def _run_backfill(db):
    job_id = rnb.enqueue(db, created_by="t")
    asyncio.run(rnb.run(db, job_id=job_id, meta={}))
    db.expire_all()
    return json.loads(db.query(Job).filter(Job.id == job_id).first().extra_data)


def test_the_backfill_fills_only_what_is_missing_and_skips_vms_that_are_gone():
    db = _fresh()
    _deploy(db, "ec2-old", instance_id="i-old")
    _deploy(db, "ec2-new", instance_id="i-new", vpc_id=VPC_B)
    _deploy(db, "ec2-gone", instance_id="i-gone")
    _deploy(db, "az-old", "azure_deploy", resource_group="rg", nic_name="web-nic",
            location="eastus")
    _deploy(db, "gce-old", "gce_deploy", zone="us-central1-a", instance_name="web-g")
    calls, restore = _with_clouds(aws={"i-old": {"vpc_id": VPC_A, "subnet_id": "subnet-1"}},
                                  azure={"web-nic": VNET},
                                  gcp={"web-g": f"https://x/compute/v1/{GCP_NET}"})
    try:
        assert rnb.missing_count(db) == 4
        result = _run_backfill(db)
        assert ("aws", "i-new") not in calls, "a VM with a recorded network was looked up"
        assert result["filled"] == 3
        assert [s["job_id"] for s in result["skipped"]] == ["ec2-gone"]
        meta = {j.id: j.metadata_dict for j in db.query(Job).all()}
        assert meta["ec2-old"]["vpc_id"] == VPC_A
        assert meta["az-old"]["vnet_id"] == VNET
        assert cmr.network_of("gcp", meta["gce-old"]) == GCP_NET
        assert not meta["ec2-gone"].get("destroyed"), "a gone VM was marked destroyed"
        calls.clear()
        _run_backfill(db)
        assert calls == [("aws", "i-gone")], "a re-run looked up VMs it had already filled"
    finally:
        restore()


class _Admin:
    is_effective_admin = True
    username = "tester"


def _client():
    from web_dashboard.api import connections
    from web_dashboard.api.auth import get_current_user
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
    return TestClient(app)


def test_saving_a_network_narrowed_route_queues_the_lookup_once():
    db = _fresh()
    _agent(db, "a")
    _deploy(db, "ec2-old", instance_id="i-old")
    c = _client()
    listed = c.get("/api/connections/config-mgmt-routes").json()
    assert listed["cloud_vms_without_network"] == 1
    r = c.post("/api/connections/config-mgmt-routes",
               json={"agent_id": "agent-a", "cidr": "10.0.0.0/16", "scope": "aws"})
    assert r.status_code == 201
    assert db.query(Job).filter(Job.job_type == rnb.JOB_TYPE).count() == 0, \
        "an unnarrowed route queued a lookup"
    for cidr in ("10.1.0.0/16", "10.2.0.0/16"):
        r = c.post("/api/connections/config-mgmt-routes",
                   json={"agent_id": "agent-a", "cidr": cidr, "scope": "aws", "network": VPC_A})
        assert r.status_code == 201, r.text
    assert db.query(Job).filter(Job.job_type == rnb.JOB_TYPE).count() == 1
    r = c.post("/api/connections/config-mgmt-routes/lookup-networks")
    assert r.status_code == 202 and r.json()["already_running"] is True


def test_the_list_offers_the_networks_deployed_into():
    db = _fresh()
    _deploy(db, "ec2-a1", vpc_id=VPC_A)
    _deploy(db, "ec2-a2", vpc_id=VPC_A, private_ip="10.0.1.6")
    _deploy(db, "gce-1", "gce_deploy", zone="us-central1-a", network=GCP_NET)
    listed = _client().get("/api/connections/config-mgmt-routes").json()
    assert listed["networks"]["aws"] == [{"id": VPC_A, "label": VPC_A, "vms": 2}]
    assert listed["networks"]["gcp"] == [{"id": GCP_NET, "label": "vpc-a", "vms": 1}]
    assert listed["cloud_vms_without_network"] == 0


# ── deployed agents ───────────────────────────────────────────────────────────

def test_a_deployed_agent_narrows_its_own_routes_to_its_network():
    from web_dashboard.services import agent_host_service as ahs
    from web_dashboard.services import agent_service, config_service
    db = _fresh()
    config_service.set(agent_service.AUDIENCE_CONFIG, "https://dash.example.com")
    lifecycle = ahs.validate_lifecycle(db, mode="always")
    host, agent = ahs.create(db, name="vpc-a-agent", cloud="aws", region="us-east-2",
                             cidrs=["10.0.0.0/16", "10.9.0.0/16"], lifecycle=lifecycle)
    cmr.create(db, agent_id=agent.id, cidr="10.0.0.0/16", label="vpc-a-agent (deployed)",
               scope="aws", region="us-east-2")
    cmr.create(db, agent_id=agent.id, cidr="10.9.0.0/16", label="mine", scope="aws",
               region="us-east-2")
    saved = (ahs.launch, ahs._ensure_egress, ahs._ONLINE_POLL_S)

    async def launch(h, script):
        s = SessionLocal()
        a = s.query(RemoteAgent).filter(RemoteAgent.id == h.agent_id).first()
        a.public_key, a.last_seen_at = "ssh-ed25519 AAAAfresh", _utcnow()
        s.commit()
        s.close()
        return {"host_id": "i-agent", "zone": "", "network": VPC_A}

    async def nothing(*a, **k):
        return None
    try:
        ahs.launch, ahs._ensure_egress, ahs._ONLINE_POLL_S = launch, nothing, 0
        job_id = ahs.enqueue(db, host, "start")
        asyncio.run(ahs.run(db, job_id=job_id, meta={"host_id": host.id, "action": "start"}))
    finally:
        ahs.launch, ahs._ensure_egress, ahs._ONLINE_POLL_S = saved
    db.expire_all()
    assert ahs.get(db, host.id).network == VPC_A
    routes = {r["cidr"]: r for r in cmr.list_routes(db)}
    assert routes["10.0.0.0/16"]["network"] == VPC_A
    assert routes["10.9.0.0/16"]["network"] == "", "a route the operator added was narrowed"


# ── the upgrade ───────────────────────────────────────────────────────────────

def test_0004_reaches_one_schema_from_a_database_at_0003():
    from sqlalchemy import create_engine, inspect, text

    from web_dashboard import schema_migrations as sm
    eng = create_engine("sqlite:///" + os.path.join(tempfile.mkdtemp(), "at3.db"))
    with eng.begin() as c:
        c.execute(text(
            "CREATE TABLE config_mgmt_routes (id VARCHAR(36) PRIMARY KEY, agent_id VARCHAR(36) "
            "NOT NULL, cidr VARCHAR(64) NOT NULL, label VARCHAR(64), is_active BOOLEAN NOT "
            "NULL, include_cloud_vms BOOLEAN, scope VARCHAR(16) DEFAULT 'onprem' NOT NULL, "
            "scope_region VARCHAR(64) DEFAULT '' NOT NULL, scope_connection_id VARCHAR(36) "
            "DEFAULT '' NOT NULL, scope_workstream VARCHAR(100) DEFAULT '' NOT NULL, "
            "created_at DATETIME NOT NULL, created_by VARCHAR(100), updated_at DATETIME, "
            "CONSTRAINT uq_config_mgmt_route_scope UNIQUE (cidr, scope, scope_region, "
            "scope_connection_id, scope_workstream))"))
        c.execute(text("CREATE TABLE agent_hosts (id VARCHAR(36) PRIMARY KEY)"))
        c.execute(text("CREATE TABLE schema_markers (key VARCHAR PRIMARY KEY, "
                       "applied_at DATETIME, detail TEXT)"))
        c.execute(text("INSERT INTO schema_markers VALUES "
                       "('ddl:0003_config_route_scopes', '2026-01-01', '')"))
        c.execute(text("INSERT INTO config_mgmt_routes VALUES ('r', 'a', '10.0.0.0/16', NULL, 1, "
                       "0, 'aws', 'us-east-2', '', 'team-a', '2026-01-01', 't', NULL)"))
    mig = [m for m in sm.MIGRATIONS if m.name.startswith(("0003", "0004"))]
    for expect in (["0004_config_route_networks"], []):
        with eng.begin() as c:
            assert sm.apply_numbered(c, mig) == expect
    insp = inspect(eng)
    assert {u["name"] for u in insp.get_unique_constraints("config_mgmt_routes")} == \
        {"uq_config_mgmt_route_scope_net"}
    assert "network" in {c["name"] for c in insp.get_columns("agent_hosts")}
    with eng.connect() as c:
        assert c.execute(text("SELECT scope, scope_workstream, scope_network "
                              "FROM config_mgmt_routes")).fetchall() == [("aws", "team-a", "")]


def test_0004_drops_the_old_rule_in_either_form_on_postgresql():
    import inspect as pyinspect

    from web_dashboard import schema_migrations as sm
    src = pyinspect.getsource(sm._drop_route_unique)
    assert 'DROP CONSTRAINT IF EXISTS {name}' in src and 'DROP INDEX IF EXISTS {name}' in src


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
