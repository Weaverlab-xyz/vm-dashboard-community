"""A workstream's post-deploy runs: assets queued on every VM deployed into it.

What these pin:

- only an administrator sets the list (each entry is code run on every new machine); a
  member reads it; the stored shape is checked on write and filtered on read;
- a completed deploy queues one ordinary `ansible_local` job per asset, in order, aimed at
  the VM the way a bulk run aims `job:<id>`, scheduled minutes out, owned by the deployer;
- it queues once, honours `skip_post_deploy`, refuses `local`-storage assets for a cloud
  VM, records why when it cannot run, and never raises into the deploy;
- every runner calls it right after marking the deploy complete, and the opt-out survives
  every fan-out.

Uses a real temp SQLite database. Runs standalone:
    python tests/test_post_deploy_runs.py
"""
import os
import re
import sys
import tempfile
import uuid

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

_TMPDIR = tempfile.mkdtemp(prefix="post-deploy-test-")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{os.path.join(_TMPDIR, 'test.db')}")
os.environ.setdefault("JWT_SECRET_KEY", "post-deploy-test-secret")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from web_dashboard.api import auth  # noqa: E402
from web_dashboard.api import workgroups as api  # noqa: E402
from web_dashboard.api.auth import full_access_map  # noqa: E402
from web_dashboard.database import (AuditLog, Base, Job, SessionLocal, User,  # noqa: E402
                                    Workgroup, engine)
from web_dashboard.services import job_service, storage_service  # noqa: E402
from web_dashboard.services import post_deploy_service as pds  # noqa: E402
from web_dashboard.services import workgroup_service as ws  # noqa: E402
from web_dashboard.services.workgroup_service import WorkgroupError  # noqa: E402
from web_dashboard.utils.clock import utcnow  # noqa: E402

Base.metadata.create_all(bind=engine)
storage_service.active_backend = lambda: "s3"


def _person(name, workgroups=(), admin=False):
    db = SessionLocal()
    try:
        u = User(id=str(uuid.uuid4()), username=name, hashed_password="x", is_admin=admin)
        u.workgroups_list = list(workgroups)
        u.permissions_dict = full_access_map()
        db.add(u)
        db.commit()
        db.refresh(u)
        db.expunge(u)
        return u
    finally:
        db.close()


ALICE = _person("alice", ["team-a"])
BOB = _person("bob", ["team-b"])
ROOT = _person("root", admin=True)
_db = SessionLocal()
for _n in ("team-a", "team-b", "team-c"):
    _db.add(Workgroup(name=_n, display_name=_n))
_db.commit()
_db.close()


def _client(user):
    app = FastAPI()
    app.include_router(api.router)

    def _me():
        db = SessionLocal()
        try:
            return db.query(User).filter(User.id == user.id).first()
        finally:
            db.close()
    app.dependency_overrides[auth.get_current_user] = _me
    return TestClient(app)


def _set(name, runs):
    db = SessionLocal()
    try:
        return ws.set_post_deploy_runs(db, name, runs)
    finally:
        db.close()


def _deploy(workgroup="team-a", status="completed", **meta):
    db = SessionLocal()
    try:
        base = {"instance_id": "i-1", "instance_name": "web-1", "region": "us-east-1",
                "public_ip": "203.0.113.9", "private_ip": "10.0.0.9"}
        base.update(meta)
        job = job_service.create_job(db, job_type="ec2_deploy", created_by="alice",
                                     workgroup=workgroup, metadata=base, batch_id="b-1")
        if status == "completed":
            job_service.set_completed(db, job.id)
        return job.id
    finally:
        db.close()


def _queue(deploy_id):
    db = SessionLocal()
    try:
        return pds.queue_runs(db, deploy_id)
    finally:
        db.close()


def _job(job_id):
    db = SessionLocal()
    try:
        j = db.query(Job).filter(Job.id == job_id).first()
        db.expunge(j)
        return j
    finally:
        db.close()


# ── The list ──────────────────────────────────────────────────────────────────

def test_the_list_is_checked_and_empty_is_null():
    for bad in ("not-a-list", [{"asset": ""}], [{"asset": "../etc/passwd"}],
                [{"asset": "/abs.yml"}], [{"asset": "a.yml", "asset_backend": 3}],
                [{"asset": f"a{i}.yml"} for i in range(11)], ["a.yml"]):
        try:
            _set("team-c", bad)
            raise AssertionError(f"{bad!r} should be refused")
        except WorkgroupError:
            pass
    out = _set("team-c", [{"asset": " base.yml ", "asset_backend": "s3"}, {"asset": "agent.sh"}])
    assert out == [{"asset": "base.yml", "asset_backend": "s3"},
                   {"asset": "agent.sh", "asset_backend": ""}]
    _set("team-c", [])
    db = SessionLocal()
    try:
        assert ws.get(db, "team-c").post_deploy_runs is None
        ws.get(db, "team-c").post_deploy_runs = '[{"asset": "ok.yml"}, 5, {"x": 1}, "junk"]'
        db.commit()
        assert ws.post_deploy_runs("team-c", db) == [{"asset": "ok.yml", "asset_backend": ""}]
        ws.get(db, "team-c").post_deploy_runs = None
        db.commit()
    finally:
        db.close()


def test_only_an_admin_sets_it_a_member_reads_it_and_it_is_audited():
    body = {"runs": [{"asset": "base.yml"}]}
    assert _client(ALICE).put("/api/workgroups/team-a/post-deploy-runs", json=body).status_code == 403
    db = SessionLocal()
    try:
        before = db.query(AuditLog).filter(
            AuditLog.action == "workstream_post_deploy_runs_update").count()
    finally:
        db.close()
    assert _client(ROOT).put("/api/workgroups/team-a/post-deploy-runs", json=body).status_code == 200
    r = _client(ALICE).get("/api/workgroups/team-a/post-deploy-runs")
    assert r.status_code == 200 and r.json()["runs"][0]["asset"] == "base.yml"
    assert _client(BOB).get("/api/workgroups/team-a/post-deploy-runs").status_code == 403
    db = SessionLocal()
    try:
        assert db.query(AuditLog).filter(
            AuditLog.action == "workstream_post_deploy_runs_update").count() == before + 1
    finally:
        db.close()
    _set("team-a", [])


# ── Queueing ──────────────────────────────────────────────────────────────────

def test_a_deploy_queues_one_aimed_delayed_job_per_asset_in_order():
    _set("team-a", [{"asset": "base.yml"}, {"asset": "agent.sh", "asset_backend": "gcs"}])
    try:
        dep = _deploy()
        ids = _queue(dep)
        assert len(ids) == 2
        first, second = _job(ids[0]), _job(ids[1])
        m1, m2 = first.metadata_dict, second.metadata_dict
        assert first.job_type == "ansible_local" and first.status == "pending"
        assert (m1["asset"], m1["asset_backend"]) == ("base.yml", "s3")
        assert (m2["asset"], m2["asset_backend"]) == ("agent.sh", "gcs")
        assert m1["target"] == "203.0.113.9" and m1["cloud"] == "aws"
        assert m1["post_deploy_of"] == dep and first.created_by == "alice"
        assert first.batch_id == "b-1"
        assert first.scheduled_for and first.scheduled_for > utcnow(), (
            "a run queued for now would hit a VM whose sshd is not up yet")
        assert not m1.get("secret_vars") and not m1.get("managed_account")
        assert _job(dep).metadata_dict["post_deploy_jobs"] == ids
    finally:
        _set("team-a", [])


def test_it_queues_once():
    _set("team-a", [{"asset": "base.yml"}])
    try:
        dep = _deploy()
        assert len(_queue(dep)) == 1
        assert _queue(dep) == [], "a second call must not queue the runs again"
    finally:
        _set("team-a", [])


def test_opt_out_nothing_set_and_unfinished_deploys_queue_nothing():
    _set("team-a", [{"asset": "base.yml"}])
    try:
        assert _queue(_deploy(skip_post_deploy=True)) == []
        assert _queue(_deploy(req={"skip_post_deploy": True})) == []
        assert _queue(_deploy(status="pending")) == []
        assert _queue(_deploy(workgroup="team-b")) == [], "team-b has no runs"
    finally:
        _set("team-a", [])


def test_local_assets_and_missing_addresses_are_recorded_not_run():
    _set("team-a", [{"asset": "base.yml", "asset_backend": "local"}, {"asset": "ok.yml"}])
    try:
        dep = _deploy()
        ids = _queue(dep)
        assert len(ids) == 1 and _job(ids[0]).metadata_dict["asset"] == "ok.yml"
        assert "local storage" in _job(dep).metadata_dict["post_deploy_error"]
        dep = _deploy(public_ip="", private_ip="")
        assert _queue(dep) == []
        assert "no recorded IP" in _job(dep).metadata_dict["post_deploy_error"]
    finally:
        _set("team-a", [])


def test_it_never_raises_into_the_deploy():
    _set("team-a", [{"asset": "base.yml"}])
    dep = _deploy()
    saved = job_service.create_job

    def boom(*a, **k):
        raise RuntimeError("db gone")
    job_service.create_job = boom
    try:
        assert _queue(dep) == []
    finally:
        job_service.create_job = saved
        _set("team-a", [])
    assert "db gone" in _job(dep).metadata_dict["post_deploy_error"]


# ── Wiring ────────────────────────────────────────────────────────────────────

def _src(*parts):
    with open(os.path.join(_ROOT, "web_dashboard", *parts), encoding="utf-8") as fh:
        return fh.read()


def test_every_runner_queues_right_after_the_deploy_completes():
    for cloud in ("aws", "azure", "gcp", "oci"):
        src = _src("services", f"{cloud}_vm_service.py")
        calls = [m.start() for m in re.finditer(r"post_deploy_service\.queue_runs\(db, job_id\)", src)]
        assert len(calls) == 1, f"{cloud}: expected one post-deploy hook"
        before = src[max(0, calls[0] - 400):calls[0]]
        assert re.search(r"job_service\.set_completed\(db, job_id, \w+\)", before), (
            f"{cloud}: the hook must follow the deploy's set_completed — it only queues for "
            "a completed deploy")


def test_the_opt_out_survives_every_fan_out():
    from web_dashboard.models import aws, azure, gcp, oci
    for model in (aws.DeployRequest, aws.BulkDeployRequest, azure.AzureDeployRequest,
                  azure.AzureBulkDeployRequest, gcp.GCPDeployRequest,
                  gcp.GCPBulkDeployRequest, oci.OCIDeployRequest, oci.OCIBulkDeployRequest):
        f = model.model_fields.get("skip_post_deploy")
        assert f is not None and f.default is False, model.__name__
    assert _src("api", "aws.py").count('"skip_post_deploy": req.skip_post_deploy') == 3
    for cloud in ("gcp", "oci"):
        assert "skip_post_deploy=req.skip_post_deploy" in _src("api", f"{cloud}.py"), cloud


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
            traceback.print_exc()
    sys.exit(1 if failures else 0)
