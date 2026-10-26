"""Cloud databases through Config-Management routes (phase 2 of
docs/design/remote-worker-and-agent.md).

  * **Matched by subnets, not an address.** A cloud database's endpoint is a DNS name on
    AWS and Azure, so a route governs it when its range contains EVERY recorded subnet
    (clouddb_network). None recorded matches nothing: it stays on the remote worker.
  * **The gate reads the row, not the request.** The executing agent is the one the route
    covering the database's subnets names; another workstream's database is not found; a
    cloud='local' database keeps its registered agent.
  * **One answer everywhere:** the inventory row, the pickers and the routes page agree.
  * **The bundle carries the worker's own credential** (ansible_connection_vars), scrubbed.
  * **Phase 1's worker fallback applies** to a routed database too.
  * **Where a database lives is read from the cloud**, best-effort at provisioning and by
    the network backfill, and never fails a provision.

Runs under pytest, or standalone:  python tests/test_config_mgmt_cloud_db_routes.py
"""
import asyncio
import json
import os
import sys
import tempfile
import types
from datetime import datetime, timedelta, timezone

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("DATABASE_URL", "sqlite:///" + os.path.join(
    tempfile.mkdtemp(prefix="cloud-db-routes-"), "t.db").replace("\\", "/"))
os.environ.setdefault("JWT_SECRET_KEY", "x" * 32)

try:
    from fastapi import HTTPException
except ModuleNotFoundError as exc:  # pragma: no cover — no app deps installed
    print(f"SKIP: {exc}")
    sys.exit(0)

from web_dashboard.api import config_mgmt as cm  # noqa: E402
from web_dashboard.api import connections as conn_api  # noqa: E402
from web_dashboard.database import (Base, CloudDatabase, ConfigMgmtRoute, Job,  # noqa: E402
                                    JobLog, RemoteAgent, SessionLocal, Workgroup, engine)
from web_dashboard.services import (agent_ansible_bundle, agent_ansible_meta,  # noqa: E402
                                    agent_host_service, aws_service, azure_service,
                                    clouddb_network, config_service, inventory_service,
                                    job_service, route_network_backfill, storage_service)
from web_dashboard.services import config_mgmt_route_service as cmr  # noqa: E402

Base.metadata.create_all(bind=engine)

HOST = "db1.abc123.us-east-2.rds.amazonaws.com"
SUBNETS = ["10.40.1.0/24", "10.40.2.0/24"]
VPC = "vpc-0abc12345def67890"


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _admin():
    return types.SimpleNamespace(username="tester", is_effective_admin=True,
                                 workgroups_list=[], effective_permissions_dict={})


def _member(*workgroups, name="member"):
    return types.SimpleNamespace(username=name, is_effective_admin=False,
                                 workgroups_list=list(workgroups),
                                 effective_permissions_dict={})


def _fresh():
    db = SessionLocal()
    for model in (ConfigMgmtRoute, Job, JobLog, CloudDatabase, RemoteAgent):
        db.query(model).delete()
    db.commit()
    return db


def _agent(db, name="vpc", *, online=True):
    db.add(RemoteAgent(
        id=f"agent-{name}", name=name, is_active=True, agent_version="2.11.0",
        public_key="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI" + name,
        last_seen_at=_utcnow() if online else _utcnow() - timedelta(hours=2),
        allowed_job_types=None, created_at=_utcnow()))
    db.commit()


def _database(db, db_id="db-1", *, cloud="aws", cidrs=SUBNETS, network=VPC,
              workgroup="team-a", deploy_workgroup="team-a", agent_id=None,
              private_host=HOST, region="us-east-2", source="provisioned"):
    db.add(CloudDatabase(
        id=db_id, engine="postgres", provider="rds", cloud=cloud, region=region,
        source=source, instance_id="db-ABCDEF", private_host=private_host, port=5432,
        db_name="app", status="available", workgroup=workgroup, created_by="tester",
        deploy_workgroup=deploy_workgroup, agent_id=agent_id,
        route_cidrs=json.dumps(cidrs) if cidrs is not None else None,
        route_network=network, created_at=_utcnow()))
    db.add(Job(id=f"prov-{db_id}", job_type="clouddb_provision", status="completed",
               workgroup=workgroup, created_at=_utcnow(),
               extra_data=json.dumps({"db_id": db_id, "tf_variables": {
                   "identifier": "db1", "region": region, "master_username": "dbadmin"}})))
    db.commit()
    config_service.set(f"clouddb/{db_id}/admin", "s3cret-pw")


def _scene(*, route_cidr="10.40.0.0/16", agent="vpc", online=True, fallback=False, **db_kw):
    db = _fresh()
    _agent(db, "vpc", online=online)
    if agent != "vpc":
        _agent(db, agent, online=online)
    _database(db, **db_kw)
    if route_cidr:
        cmr.create(db, agent_id=f"agent-{agent}", cidr=route_cidr, scope="aws",
                   fallback_to_worker=fallback)
    return db


def _row(db, db_id="db-1"):
    return db.query(CloudDatabase).filter(CloudDatabase.id == db_id).first()


def _request(**over):
    base = dict(asset="site.yml", asset_backend="s3", agent_id="agent-vpc",
                target_kind="database", target_id="db-1")
    base.update(over)
    return cm.RunRequest(**base)


def _gate(db, payload, user=None):
    return cm._resolve_agent_target(payload, db, user or _admin())


def _refusal(db, payload, user=None):
    try:
        _gate(db, payload, user)
    except HTTPException as exc:
        return exc
    raise AssertionError("the gate accepted this run")


# ── matching ──────────────────────────────────────────────────────────────────

def test_a_route_covers_a_database_only_when_every_subnet_is_inside_it():
    db = _scene(route_cidr="10.40.1.0/24")
    assert cmr.match_for_database(db, _row(db)) is None, "one subnet was outside the route"
    cmr.create(db, agent_id="agent-vpc", cidr="10.40.0.0/20", scope="aws")
    assert cmr.match_for_database(db, _row(db)).cidr == "10.40.0.0/20"


def test_a_database_with_no_recorded_subnets_matches_nothing():
    db = _scene(cidrs=None)
    assert cmr.scope_of_database(_row(db))[4] == []
    assert cmr.match_for_database(db, _row(db)) is None


def test_the_narrowers_apply():
    db = _fresh()
    for name in ("team-a", "team-b"):
        if db.query(Workgroup).filter(Workgroup.name == name).first() is None:
            db.add(Workgroup(name=name, display_name=name))
    db.commit()
    _agent(db)
    _database(db, deploy_workgroup="team-a", workgroup="team-b")
    row = _row(db)
    for kw in ({"region": "eu-west-1"}, {"workstream": "team-b"},
               {"network": "vpc-0fff12345def67890"}):
        r = cmr.create(db, agent_id="agent-vpc", cidr="10.40.0.0/16", scope="aws", **kw)
        assert cmr.match_for_database(db, row) is None, kw
        cmr.delete(db, r["id"])
    cmr.create(db, agent_id="agent-vpc", cidr="10.40.0.0/16", scope="azure")
    assert cmr.match_for_database(db, row) is None, "another cloud's route matched"
    cmr.create(db, agent_id="agent-vpc", cidr="10.40.0.0/16", scope="aws",
               region="us-east-2", workstream="team-a", network=VPC)
    assert cmr.match_for_database(db, row) is not None, "the deploy workstream decides"


def test_the_table_and_the_one_shot_agree():
    db = _scene()
    row = _row(db)
    assert cmr.load_table(db).match_for_database(row).id == cmr.match_for_database(db, row).id


# ── the gate ──────────────────────────────────────────────────────────────────

def test_the_routed_agent_runs_against_the_databases_endpoint():
    db = _scene()
    out = _gate(db, _request(target="10.9.9.9", port=1))
    assert out["run_kind"] == "database" and out["transport"] == "local"
    assert out["target_host"] == HOST and out["target_port"] == 5432
    assert out["target_id"] == "db-1"


def test_another_agent_is_refused_naming_the_routed_one():
    db = _scene(agent="other")
    exc = _refusal(db, _request())
    assert exc.status_code == 400 and "'other'" in exc.detail and "10.40.0.0/16" in exc.detail


def test_an_unrouted_database_runs_on_the_remote_worker():
    db = _scene(route_cidr="")
    exc = _refusal(db, _request())
    assert exc.status_code == 400 and "remote worker" in exc.detail
    db = _scene(cidrs=None)
    exc = _refusal(db, _request())
    assert "Look up networks" in exc.detail, exc.detail


def test_another_workstreams_database_is_not_found():
    db = _scene()
    assert _refusal(db, _request(), _member("team-b")).status_code == 404
    assert _gate(db, _request(), _member("team-a"))["target_host"] == HOST


def test_a_local_database_keeps_its_registered_agent():
    db = _scene(cloud="local", cidrs=None, network=None, agent_id="agent-vpc",
                source="registered", route_cidr="")
    assert _gate(db, _request())["target_host"] == HOST
    _agent(db, "other")
    assert "not reachable" in _refusal(db, _request(agent_id="agent-other")).detail


# ── the bundle ────────────────────────────────────────────────────────────────

def test_the_bundle_carries_the_workers_own_credential_scrubbed():
    db = _scene()
    meta = agent_ansible_meta.normalize(dict(
        run_kind="database", transport="local", target_host=HOST, target_port=5432,
        target_id="db-1", target_label="postgres/db-1", asset="site.yml",
        asset_backend="s3"))
    job = Job(id="run-1", job_type="agent_ansible", status="running", agent_id="agent-vpc",
              extra_data=json.dumps(meta), created_at=_utcnow())
    db.add(job)
    db.commit()

    async def _asset(backend, name):
        return b"- hosts: localhost\n  tasks: []\n"
    saved = storage_service.fetch_asset_in
    storage_service.fetch_asset_in = _asset
    try:
        agent = db.query(RemoteAgent).filter(RemoteAgent.id == "agent-vpc").first()
        bundle, scrub = asyncio.run(agent_ansible_bundle.build(db, job=job, agent=agent))
    finally:
        storage_service.fetch_asset_in = saved
    assert bundle["db"]["db_login_host"] == HOST
    assert bundle["db"]["db_login_user"] == "dbadmin"
    assert bundle["db"]["db_login_password"] == "s3cret-pw" and "s3cret-pw" in scrub


# ── inventory, pickers, routes page ───────────────────────────────────────────

def _item(db):
    return next(i for i in inventory_service.collect(db) if i["id"] == "clouddb:db-1")


def test_the_inventory_row_and_its_bulk_spec_name_the_agent():
    db = _scene()
    item = _item(db)
    assert item["agent_id"] == "agent-vpc" and item["route_cidrs"] == SUBNETS
    spec = inventory_service._target_spec(item)
    assert spec["agent_id"] == "agent-vpc" and spec["target_kind"] == "database"
    db = _scene(route_cidr="")
    assert inventory_service._target_spec(_item(db)) == {"target_kind": "database",
                                                         "target_id": "db-1"}


def test_a_routed_database_moves_from_the_localhost_picker_to_the_agent_picker():
    db = _scene()
    assert cm.get_localhost_targets(_admin(), db)["databases"] == []
    rows = cm.get_agent_targets(_admin(), db)["databases"]
    assert [r["target_id"] for r in rows] == ["db-1"]
    assert "covers its subnets" in rows[0]["executor_note"] and rows[0]["reason"] == ""
    db = _scene(route_cidr="")
    assert [d["id"] for d in cm.get_localhost_targets(_admin(), db)["databases"]] == ["db-1"]


def test_the_localhost_picker_lists_only_the_callers_databases():
    db = _scene(route_cidr="")
    assert cm.get_localhost_targets(_member("team-b"), db)["databases"] == []
    assert len(cm.get_localhost_targets(_member("team-a"), db)["databases"]) == 1


def test_the_routes_page_counts_databases_and_unrecorded_ones():
    db = _scene()
    _database(db, "db-2", cidrs=None, network=None)
    out = conn_api.list_config_routes(db, _admin())
    assert out["routes"][0]["database_matches"] == 1
    assert out["cloud_dbs_without_ranges"] == 1
    assert out["networks"]["aws"][0]["id"] == VPC


# ── the worker fallback ───────────────────────────────────────────────────────

class _RunPath:
    def __enter__(self):
        self.saved = cm._scope_check
        cm._scope_check = lambda *a, **k: None
        return self

    def __exit__(self, *exc):
        cm._scope_check = self.saved


def test_an_offline_agent_with_fallback_sends_the_database_run_to_the_worker():
    db = _scene(online=False, fallback=True)
    with _RunPath():
        out = asyncio.run(cm.run_playbook(_request(), db, _admin()))
    job = db.query(Job).filter(Job.id == out["job_id"]).first()
    assert job.job_type == "ansible_cloud_run", job.job_type
    assert job.metadata_dict["fallback_from_agent"] == "vpc"
    assert job.metadata_dict["target_id"] == "db-1"
    lines = [line for _, line in job_service.get_job_logs(db, job.id)]
    assert any("remote worker" in line for line in lines), lines


def test_without_fallback_an_offline_agent_is_refused():
    db = _scene(online=False)
    with _RunPath():
        try:
            asyncio.run(cm.run_playbook(_request(), db, _admin()))
        except HTTPException as exc:
            assert "not online" in exc.detail
        else:
            raise AssertionError("queued a run for an offline agent")


# ── where a database lives ────────────────────────────────────────────────────

def test_the_lookup_reads_each_clouds_subnets():
    db = _fresh()
    _database(db, "aws-1")
    _database(db, "az-1", cloud="azure", cidrs=None, network=None, region="eastus")
    _database(db, "gcp-1", cloud="gcp", cidrs=None, network=None, private_host="10.90.0.5",
              region="us-central1")
    prov = db.query(Job).filter(Job.id == "prov-az-1").first()
    sub = ("/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg/"
           "providers/Microsoft.Network/"
           "virtualNetworks/vnet-a/subnets/db")
    prov.metadata_dict = {"db_id": "az-1", "tf_variables": {"delegated_subnet_id": sub}}
    gprov = db.query(Job).filter(Job.id == "prov-gcp-1").first()
    gprov.metadata_dict = {"db_id": "gcp-1", "tf_variables": {"private_network": "default",
                                                               "project": "proj-1"}}
    db.commit()
    seen = {}

    async def _rds(region, identifier, resource_id=""):
        seen["aws"] = (region, identifier)
        return {"vpc_id": VPC, "cidrs": ["10.40.2.0/24", "10.40.1.0/24"]}

    async def _subnet(subnet_id):
        seen["azure"] = subnet_id
        return ["10.50.3.0/24"]
    saved = aws_service.rds_subnet_ranges, azure_service.subnet_prefixes
    aws_service.rds_subnet_ranges, azure_service.subnet_prefixes = _rds, _subnet
    try:
        assert asyncio.run(clouddb_network.lookup(db, _row(db, "aws-1"))) == (SUBNETS, VPC)
        cidrs, network = asyncio.run(clouddb_network.lookup(db, _row(db, "az-1")))
        assert cidrs == ["10.50.3.0/24"] and network.endswith("/virtualnetworks/vnet-a")
        assert asyncio.run(clouddb_network.lookup(db, _row(db, "gcp-1"))) == (
            ["10.90.0.5/32"], "projects/proj-1/global/networks/default")
    finally:
        aws_service.rds_subnet_ranges, azure_service.subnet_prefixes = saved
    assert seen["aws"] == ("us-east-2", "db1") and seen["azure"] == sub


def test_a_failed_lookup_leaves_the_database_on_the_worker_and_the_provision_alone():
    db = _fresh()
    _database(db, cidrs=None, network=None, deploy_workgroup=None)
    row = _row(db)

    async def _boom(*a, **k):
        raise aws_service.AWSError("AccessDenied")
    saved = aws_service.rds_subnet_ranges
    aws_service.rds_subnet_ranges = _boom
    logged = []
    try:
        asyncio.run(clouddb_network.record_on_provision(db, row, log=logged.append))
    finally:
        aws_service.rds_subnet_ranges = saved
    assert row.route_cidrs is None and row.deploy_workgroup == "team-a"
    assert logged and "Look up networks" in logged[0]


def test_the_backfill_fills_only_databases_without_subnets():
    db = _fresh()
    _database(db, "known")
    _database(db, "unknown", cidrs=None, network=None, deploy_workgroup=None)
    calls = []

    async def _lookup(db_, row):
        calls.append(row.id)
        return ["10.40.7.0/24"], VPC
    saved = clouddb_network.lookup
    clouddb_network.lookup = _lookup
    try:
        job = job_service.create_job(db, job_type=route_network_backfill.JOB_TYPE,
                                     created_by="tester", metadata={})
        asyncio.run(route_network_backfill.run(db, job_id=job.id, meta={}))
    finally:
        clouddb_network.lookup = saved
    assert calls == ["unknown"]
    db.expire_all()
    assert json.loads(_row(db, "unknown").route_cidrs) == ["10.40.7.0/24"]
    assert _row(db, "unknown").deploy_workgroup == "team-a"
    assert json.loads(_row(db, "known").route_cidrs) == SUBNETS


# ── migration and the deployed agent's policy ─────────────────────────────────

def test_the_migration_is_recorded():
    from web_dashboard import schema_migrations as sm
    mig = [m for m in sm.MIGRATIONS if m.name == "0008_clouddb_route_ranges"]
    assert len(mig) == 1 and len(mig[0].steps) == 3


def test_a_deployed_agent_can_run_database_plays():
    import yaml
    doc = yaml.safe_load(agent_host_service.policy_yaml(["10.40.0.0/16"], 2))
    assert doc["ansible"]["db_image"] == agent_host_service.DB_IMAGE
    for target in (doc["targets"][0], doc["ansible"]["targets"][0]):
        assert {22, 5432, 3306, 1433, 1521, 27017} <= set(target["ports"])


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
