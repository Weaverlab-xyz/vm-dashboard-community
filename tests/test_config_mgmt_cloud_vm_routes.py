"""Cloud VMs routed to a remote agent: a Config-Management route with ``include_cloud_vms``.

A large cloud estate can run its Config Management on persistent agents inside its VPCs
or VNets instead of the remote worker. Everything here is about keeping that opt-in and
keeping the anti-substitution rule intact:

  * **Opt-in.** A route without the flag, which is every route that predates it, sends no
    cloud VM anywhere: the remote worker still runs it.
  * **The address is the deploy job's.** The enqueue gate reads the private address off the
    deploy job and resolves the executor from that. The request may propose an address and
    an agent; neither decides.
  * **One answer everywhere.** The inventory row, the target picker, ``/cloud-targets`` and
    the gate agree about who runs a VM.
  * **The bundle carries the VM's own key**, through the same function the remote worker
    uses, and a Windows server is flagged so the agent picks the PowerShell shell.

Runs under pytest, or standalone:  python tests/test_config_mgmt_cloud_vm_routes.py
"""
import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timezone


def _utcnow():
    """Naive UTC now: what the deprecated datetime.utcnow() returned."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.environ.setdefault("DATABASE_URL",
                      "sqlite:///" + os.path.join(tempfile.mkdtemp(), "cloudvm.db").replace("\\", "/"))
os.environ.setdefault("JWT_SECRET_KEY", "x" * 32)

try:
    from fastapi import HTTPException
except ModuleNotFoundError as exc:
    print(f"SKIP: {exc}")
    sys.exit(0)

from web_dashboard.database import (Base, engine, SessionLocal, ConfigMgmtRoute, Job,  # noqa: E402
                                    RemoteAgent)
from web_dashboard.api import config_mgmt  # noqa: E402
from web_dashboard.api.config_mgmt import _resolve_agent_target  # noqa: E402
from web_dashboard.services import (agent_ansible_bundle, agent_ansible_meta,  # noqa: E402
                                    ansible_local_run_service, inventory_service,
                                    storage_service)
from web_dashboard.services import config_mgmt_route_service as cmr  # noqa: E402

Base.metadata.create_all(bind=engine)

PRIVATE = "10.40.1.15"
PUBLIC = "54.1.2.3"


class _User:
    """What the gate reads off a user: admin-ness and workstreams."""

    def __init__(self, *, admin=True, workgroups=()):
        self.is_effective_admin = admin
        self.workgroups_list = list(workgroups)
        self.username = "tester"


class _Payload:
    def __init__(self, **kw):
        self.agent_id = kw.get("agent_id", "agent-vpc")
        self.target_kind = "vm"
        self.target_id = kw.get("target_id", "deploy-1")
        self.connection_id = kw.get("connection_id", "")
        self.cloud = kw.get("cloud", "aws")
        self.target = kw.get("target", PRIVATE)
        self.transport = kw.get("transport", "")
        self.port = kw.get("port", 0)
        self.ansible_user = kw.get("ansible_user", "")


def _fresh():
    db = SessionLocal()
    for model in (ConfigMgmtRoute, Job, RemoteAgent):
        db.query(model).delete()
    db.commit()
    return db


def _agent(db, name, *, version="2.9.0"):
    db.add(RemoteAgent(
        id=f"agent-{name}", name=name, is_active=True, agent_version=version,
        public_key="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI" + name,
        last_seen_at=_utcnow(), allowed_job_types=None, created_at=_utcnow()))
    db.commit()


def _deploy(db, job_id="deploy-1", *, job_type="ec2_deploy", workgroup="team-a",
            private_ip=PRIVATE, windows=False, destroyed=False):
    meta = {"instance_name": "web-1", "instance_id": "i-123", "private_ip": private_ip,
            "public_ip": PUBLIC}
    if windows:
        meta.update({"os_type": "windows", "admin_username": "Administrator"})
    if destroyed:
        meta["destroyed"] = True
    db.add(Job(id=job_id, job_type=job_type, status="completed", workgroup=workgroup,
               extra_data=json.dumps(meta), created_at=_utcnow()))
    db.commit()


def _route(db, cidr="10.40.0.0/16", agent="agent-vpc", *, cloud=True):
    return cmr.create(db, agent_id=agent, cidr=cidr, include_cloud_vms=cloud)


def _scene(db, **deploy_kw):
    _agent(db, "vpc")
    _deploy(db, **deploy_kw)


def _refusal(payload, db, user=None):
    try:
        _resolve_agent_target(payload, db, user or _User())
    except HTTPException as exc:
        return exc
    raise AssertionError("the gate accepted this run")


# ── opt-in ────────────────────────────────────────────────────────────────────

def test_a_route_without_the_flag_routes_no_cloud_vm():
    db = _fresh()
    _scene(db)
    _route(db, cloud=False)
    table = cmr.load_table(db)
    assert table.match_for(PRIVATE) is not None, "the route still governs hypervisor guests"
    assert table.match_for(PRIVATE, cloud_vm=True) is None
    exc = _refusal(_Payload(), db)
    assert exc.status_code == 400 and "remote worker" in exc.detail


def test_a_route_from_before_the_migration_routes_no_cloud_vm():
    """schema_migrations 0001 adds the column with no DEFAULT, so every existing row reads
    NULL. NULL must mean off, or an upgrade would move cloud VMs off the remote worker."""
    db = _fresh()
    _scene(db)
    db.add(ConfigMgmtRoute(id="legacy", agent_id="agent-vpc", cidr="10.40.0.0/16",
                           is_active=True, include_cloud_vms=None, created_at=_utcnow()))
    db.commit()
    assert cmr.load_table(db).match_for(PRIVATE, cloud_vm=True) is None
    assert cmr.list_routes(db)[0]["include_cloud_vms"] is False
    from web_dashboard import schema_migrations
    assert any(m.name == "0001_config_route_cloud_vms" for m in schema_migrations.MIGRATIONS)


def test_a_new_route_defaults_to_off():
    db = _fresh()
    _agent(db, "vpc")
    out = cmr.create(db, agent_id="agent-vpc", cidr="10.40.0.0/16")
    assert out["include_cloud_vms"] is False


def test_a_flagged_route_inside_an_unflagged_one_governs_its_cloud_vms():
    db = _fresh()
    _agent(db, "vpc")
    _agent(db, "lab")
    _route(db, "10.40.0.0/16", "agent-lab", cloud=False)
    _route(db, "10.40.1.0/24", "agent-vpc", cloud=True)
    table = cmr.load_table(db)
    assert table.executor_for(PRIVATE, cloud_vm=True) == "agent-vpc"
    assert table.executor_for("10.40.9.9", cloud_vm=True) == ""
    assert table.executor_for("10.40.9.9") == "agent-lab"


# ── the gate ──────────────────────────────────────────────────────────────────

def test_the_routed_agent_runs_against_the_deploys_private_address():
    db = _fresh()
    _scene(db)
    _route(db)
    out = _resolve_agent_target(_Payload(), db, _User())
    assert out["target_host"] == PRIVATE and out["run_kind"] == "vm"
    assert out["cloud_deploy_job_id"] == "deploy-1" and out["cloud"] == "aws"
    assert out["connection_id"] == "" and out["windows_ssh"] is False
    assert out["executor_name"] == "vpc"
    assert out["login_user"] == "ec2-user"


def test_the_address_in_the_body_is_ignored():
    """The sharpest one: a caller who names another host still gets the recorded one."""
    db = _fresh()
    _scene(db)
    _route(db)
    out = _resolve_agent_target(_Payload(target="10.40.200.200"), db, _User())
    assert out["target_host"] == PRIVATE


def test_another_agent_is_refused():
    db = _fresh()
    _scene(db)
    _agent(db, "other")
    _route(db)
    exc = _refusal(_Payload(agent_id="agent-other"), db)
    assert exc.status_code == 400 and "routed to agent 'vpc'" in exc.detail


def test_a_destroyed_vm_or_another_workstreams_vm_is_not_found():
    db = _fresh()
    _scene(db, destroyed=True)
    _route(db)
    assert _refusal(_Payload(), db).status_code == 404

    db = _fresh()
    _scene(db, workgroup="team-b")
    _route(db)
    exc = _refusal(_Payload(), db, _User(admin=False, workgroups=["team-a"]))
    assert exc.status_code == 404
    out = _resolve_agent_target(_Payload(), db, _User(admin=False, workgroups=["team-b"]))
    assert out["target_host"] == PRIVATE


def test_no_user_means_no_cloud_vm():
    """Fail closed: a caller that forgot to pass the user sees nothing, never everything."""
    db = _fresh()
    _scene(db)
    _route(db)
    try:
        _resolve_agent_target(_Payload(), db)
    except HTTPException as exc:
        assert exc.status_code == 404
    else:
        raise AssertionError("resolved a cloud VM with no user to check")


def test_a_deploy_of_another_cloud_is_not_found():
    db = _fresh()
    _scene(db, job_type="azure_deploy")
    _route(db)
    assert _refusal(_Payload(cloud="aws"), db).status_code == 404


def test_a_windows_server_needs_an_agent_that_knows_the_shell():
    db = _fresh()
    _agent(db, "vpc", version="2.8.0")
    _deploy(db, windows=True)
    _route(db)
    exc = _refusal(_Payload(), db)
    assert exc.status_code == 400 and "2.9" in exc.detail

    db = _fresh()
    _scene(db, windows=True)
    _route(db)
    out = _resolve_agent_target(_Payload(), db, _User())
    assert out["windows_ssh"] is True and out["login_user"] == "Administrator"


def test_the_meta_check_accepts_a_cloud_vm_and_nothing_looser():
    base = {"run_kind": "vm", "transport": "ssh", "target_host": PRIVATE, "target_port": 22}
    assert agent_ansible_meta.check(dict(base, cloud_deploy_job_id="d", cloud="aws")) == ""
    assert agent_ansible_meta.check(dict(base, cloud_deploy_job_id="d", cloud="oci"))
    assert agent_ansible_meta.check(dict(base, cloud="aws"))
    assert agent_ansible_meta.check(dict(base, cloud_deploy_job_id="d", cloud="aws",
                                         transport="winrm", windows_ssh=True))
    assert "windows_ssh" not in agent_ansible_meta.ENVELOPE_KEYS
    assert agent_ansible_meta.normalize({"windows_ssh": "true"})["windows_ssh"] is False


# ── one answer everywhere ─────────────────────────────────────────────────────

def test_the_inventory_row_and_its_run_spec_name_the_agent():
    db = _fresh()
    _scene(db)
    _route(db)
    item = next(i for i in inventory_service.collect(db) if i["id"] == "job:deploy-1")
    assert item["agent_id"] == "agent-vpc" and item["private_ip"] == PRIVATE
    spec = inventory_service._target_spec(item)
    assert spec == {"agent_id": "agent-vpc", "connection_id": "", "target_id": "deploy-1",
                    "target": PRIVATE, "transport": "ssh", "cloud": "aws"}
    # And the gate accepts exactly what the spec proposes.
    out = _resolve_agent_target(_Payload(**{k: v for k, v in spec.items()
                                            if k != "transport"}), db, _User())
    assert out["executor_name"] == "vpc"


def test_an_unrouted_vm_keeps_the_remote_worker_spec():
    db = _fresh()
    _scene(db)
    item = next(i for i in inventory_service.collect(db) if i["id"] == "job:deploy-1")
    assert item["agent_id"] is None
    assert inventory_service._target_spec(item) == {"target": PUBLIC, "cloud": "aws"}


def test_a_routed_vm_leaves_cloud_targets_for_agent_targets():
    db = _fresh()
    _scene(db)
    assert [t["ip"] for t in config_mgmt.get_cloud_targets(_User(), db)["aws"]] == [PUBLIC]
    _route(db)
    assert config_mgmt.get_cloud_targets(_User(), db)["aws"] == []
    rows = config_mgmt.get_agent_targets(_User(), db)["cloud_vms"]
    assert [(r["target_id"], r["ip"], r["agent_id"]) for r in rows] == \
        [("deploy-1", PRIVATE, "agent-vpc")]
    assert "vpc" in rows[0]["executor_note"] and rows[0]["reason"] == ""


def test_the_routes_page_counts_cloud_vms():
    db = _fresh()
    _scene(db)
    out = _route(db)
    counts = cmr.cloud_match_counts(db, inventory_service.cloud_vm_private_ips(db))
    assert counts == {out["id"]: 1}


# ── the bundle ────────────────────────────────────────────────────────────────

def _bundle_job(db, **meta_kw):
    meta = agent_ansible_meta.normalize(dict(
        run_kind="vm", transport="ssh", target_host=PRIVATE, target_port=22,
        target_id="deploy-1", target_label="aws/web-1", asset="site.yml",
        asset_backend="s3", login_user="ec2-user", cloud_deploy_job_id="deploy-1",
        cloud="aws", **meta_kw))
    job = Job(id="run-1", job_type="agent_ansible", status="running",
              agent_id="agent-vpc", extra_data=json.dumps(meta), created_at=_utcnow())
    db.add(job)
    db.commit()
    return job


def _build(db, job, *, pem="-----BEGIN KEY-----\nabc\n-----END KEY-----\n"):
    seen = {}

    async def _asset(backend, name):
        return b"- hosts: all\n  tasks: []\n"

    async def _key(cloud, meta):
        seen["cloud"], seen["meta"] = cloud, meta
        return pem

    originals = (storage_service.fetch_asset_in, ansible_local_run_service.cloud_ssh_key_for_deploy)
    storage_service.fetch_asset_in = _asset
    ansible_local_run_service.cloud_ssh_key_for_deploy = _key
    try:
        agent = db.query(RemoteAgent).filter(RemoteAgent.id == "agent-vpc").first()
        bundle, scrub = asyncio.run(agent_ansible_bundle.build(db, job=job, agent=agent))
    finally:
        storage_service.fetch_asset_in, ansible_local_run_service.cloud_ssh_key_for_deploy = originals
    return bundle, scrub, seen


def test_the_bundle_carries_the_vms_own_key_and_scrubs_it():
    db = _fresh()
    _scene(db)
    job = _bundle_job(db)
    bundle, scrub, seen = _build(db, job)
    assert bundle["ssh_private_key"].startswith("-----BEGIN KEY-----")
    assert bundle["ssh_private_key"] in scrub
    assert seen["cloud"] == "aws" and seen["meta"]["instance_id"] == "i-123"
    assert bundle["windows_ssh"] is False


def test_the_bundle_flags_a_windows_server():
    db = _fresh()
    _scene(db, windows=True)
    bundle, _scrub, _seen = _build(db, _bundle_job(db, windows_ssh=True))
    assert bundle["windows_ssh"] is True


def test_no_key_anywhere_is_an_operator_message():
    db = _fresh()
    _scene(db)
    try:
        _build(db, _bundle_job(db), pem=None)
    except agent_ansible_bundle.BundleError as exc:
        assert "No SSH key" in str(exc)
    else:
        raise AssertionError("built a bundle with no key")


def test_a_vm_destroyed_after_enqueue_gets_no_key():
    db = _fresh()
    _scene(db)
    job = _bundle_job(db)
    deploy = db.query(Job).filter(Job.id == "deploy-1").first()
    deploy.metadata_dict = {**deploy.metadata_dict, "destroyed": True}
    db.commit()
    try:
        _build(db, job)
    except agent_ansible_bundle.BundleError as exc:
        assert "no longer deployed" in str(exc)
    else:
        raise AssertionError("released a key for a destroyed VM")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
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
