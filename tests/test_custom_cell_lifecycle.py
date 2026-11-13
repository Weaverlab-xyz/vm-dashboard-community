"""Custom demo cells: launch, booked start, roll-up, end time and teardown.

No cloud is touched. ``_create_member`` -- the seam that calls each cloud's own create
handler -- is replaced by a fake that writes the row a real handler would (a deploy Job,
a K8sCluster, a CloudDatabase), and one test pins that the real seam calls those
handlers with the LAUNCHER as the actor. Teardown is the real code against those rows,
with the cluster/database ``start_decommission`` calls recorded.

Runs standalone:
    python tests/test_custom_cell_lifecycle.py
"""
import asyncio
import os
import sys
import tempfile
import uuid
from datetime import timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

_TMPDIR = tempfile.mkdtemp(prefix="custom-cell-life-")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{os.path.join(_TMPDIR, 'test.db')}")
os.environ.setdefault("JWT_SECRET_KEY", "custom-cell-life-test")

from fastapi import HTTPException  # noqa: E402

from web_dashboard.api import custom_cell as api  # noqa: E402
from web_dashboard.api.demo_gates import require_demo  # noqa: E402
from web_dashboard.database import (Base, CloudDatabase, CustomCell,  # noqa: E402
                                    CustomCellMember, Job, K8sCluster, SessionLocal, User,
                                    Workgroup, engine)
from web_dashboard.services import custom_cell_service as svc  # noqa: E402
from web_dashboard.services import role_service  # noqa: E402
from web_dashboard.utils.clock import utcnow  # noqa: E402

Base.metadata.create_all(bind=engine)

GKE = {"kind": "k8s", "cloud": "gcp", "region": "us-central1", "label": "gke",
       "spec": {"node_count": 2}}
MYSQL = {"kind": "database", "cloud": "gcp", "region": "us-central1", "label": "db",
         "spec": {"engine": "mysql"}}
UBUNTU = {"kind": "vm", "cloud": "gcp", "label": "web", "count": 2,
          "spec": {"image_self_link": "projects/x/global/images/ubuntu", "zone": "us-central1-a"}}

CALLS = {"created": [], "decommissioned": []}


async def _fake_create(db, user, cell, item, rname):
    """What each real handler leaves behind, minus the cloud."""
    if rname.endswith("-boom"):
        raise HTTPException(status_code=403, detail="admission policy refused it")
    CALLS["created"].append((item["kind"], rname, user.username))
    if item["kind"] == "vm":
        job = Job(id=str(uuid.uuid4()), job_type="gce_deploy", status="pending",
                  created_by=user.username, workgroup=cell.workgroup,
                  expires_at=utcnow() + timedelta(hours=1))
        job.metadata_dict = {"instance_name": rname, "zone": "us-central1-a",
                             "project_id": "p"}
        db.add(job)
        db.commit()
        return job.id, job.id
    if item["kind"] == "k8s":
        row = K8sCluster(id=str(uuid.uuid4()), cloud="gcp", name=rname,
                         status="provisioning", source="provisioned",
                         workgroup=cell.workgroup)
    else:
        row = CloudDatabase(id=str(uuid.uuid4()), engine="mysql", cloud="gcp", instance_id=rname,
                            status="provisioning", source="provisioned",
                            workgroup=cell.workgroup)
    db.add(row)
    db.commit()
    return row.id, "job-" + row.id[:8]


def _fake_decommission(kind):
    def _f(db, rid, created_by=None):
        CALLS["decommissioned"].append((kind, rid, created_by))
        model = K8sCluster if kind == "k8s" else CloudDatabase
        row = db.query(model).filter(model.id == rid).first()
        row.status = "decommissioning"
        db.commit()
        return {"job_id": "x"}
    return _f


svc._create_member = _fake_create
from web_dashboard.services import cloud_database_service, k8s_service  # noqa: E402
k8s_service.start_decommission = _fake_decommission("k8s")
cloud_database_service.start_decommission = _fake_decommission("database")
from web_dashboard.services import feature_flags  # noqa: E402
_real_enabled = feature_flags.enabled
feature_flags.enabled = lambda key, *a, **k: True if key == "k8s_management_enabled" \
    else _real_enabled(key, *a, **k)


def _db():
    db = SessionLocal()
    role_service.seed_builtins(db)
    if not db.query(Workgroup).filter(Workgroup.name == "demo").first():
        db.add(Workgroup(id=str(uuid.uuid4()), name="demo", display_name="demo"))
        db.commit()
    CALLS["created"].clear()
    CALLS["decommissioned"].clear()
    return db


def _user(db, *roles):
    u = User(id=str(uuid.uuid4()), username=f"u-{uuid.uuid4().hex[:8]}",
             hashed_password="x", is_active=True)
    u.workgroups_list = ["demo"]
    db.add(u)
    role_service.apply_roles_to_user(db, u, [role_service.get_by_slug(db, r) for r in roles])
    db.commit()
    return u


def _bp(db, items=(GKE, MYSQL, UBUNTU), **kw):
    return svc.create_blueprint(db, {"name": f"bp-{uuid.uuid4().hex[:6]}",
                                     "items": list(items), **kw},
                                created_by="builder", workgroup="demo")


def _launch(db, user, bp, run_at="", ttl=None, name=None, **kw):
    return asyncio.run(svc.launch(
        db, bp, user=user, name=name or f"c{uuid.uuid4().hex[:6]}", workgroup="demo",
        schedule_fields={"run_at": run_at, "run_timezone": "UTC", "change_window_id": ""},
        ttl_hours=ttl, **kw))


def _members(db, cell):
    return svc.members_of(db, cell.id)


def _finish(db, cell, vm="completed", other="ready"):
    """Pretend every member's own runner finished."""
    for m in _members(db, cell):
        if m.kind == "vm":
            j = db.query(Job).filter(Job.id == m.ref_id).first()
            j.status = vm
        else:
            model = K8sCluster if m.kind == "k8s" else CloudDatabase
            row = db.query(model).filter(model.id == m.ref_id).first()
            row.status = {"ready": "registered" if m.kind == "k8s" else "available",
                          "failed": "failed"}[other]
    db.commit()


# ── launch ───────────────────────────────────────────────────────────────────

def test_a_cell_launched_now_builds_every_member_as_the_launcher():
    db = _db()
    me = _user(db, "demo-presenter")
    cell = _launch(db, me, _bp(db), ttl=6)
    names = sorted(n for _, n, _ in CALLS["created"])
    assert names == sorted([f"{cell.name}-gke", f"{cell.name}-db",
                            f"{cell.name}-web-1", f"{cell.name}-web-2"]), names
    assert {u for _, _, u in CALLS["created"]} == {me.username}
    assert cell.status == "provisioning"
    assert len(_members(db, cell)) == 4


def test_a_booked_cell_builds_nothing_until_its_start():
    db = _db()
    me = _user(db, "demo-presenter")
    start = (utcnow() + timedelta(days=2)).replace(second=0, microsecond=0)
    cell = _launch(db, me, _bp(db), run_at=start.strftime("%Y-%m-%dT%H:%M"), ttl=4)
    assert cell.status == "scheduled" and CALLS["created"] == []
    assert cell.scheduled_for == start
    assert cell.expires_at == start + timedelta(hours=4), "the TTL must count from the START"

    asyncio.run(svc.sweep(db, now=start - timedelta(minutes=5)))
    assert CALLS["created"] == [], "built early"
    asyncio.run(svc.sweep(db, now=start + timedelta(minutes=1)))
    db.refresh(cell)
    assert len(CALLS["created"]) == 4 and cell.status == "provisioning"


def test_members_carry_the_backstop_timer_and_the_cell_owns_the_schedule():
    db = _db()
    me = _user(db, "demo-presenter")
    bp = _bp(db, suspend_at_local="19:00", resume_at_local="07:00",
             schedule_timezone="America/Chicago")
    cell = _launch(db, me, bp, ttl=72)
    want = cell.expires_at + svc.BACKSTOP
    for m in _members(db, cell):
        if m.kind == "vm":
            row = db.query(Job).filter(Job.id == m.ref_id).first()
            # The cell's sweep powers its VMs; a copy here would be a second owner.
            assert not row.suspend_at_local and not row.resume_at_local
            assert row.metadata_dict["custom_cell_id"] == cell.id
        else:
            model = K8sCluster if m.kind == "k8s" else CloudDatabase
            row = db.query(model).filter(model.id == m.ref_id).first()
        assert row.expires_at == want, (m.kind, row.expires_at, want)


def test_bad_launches_are_refused_before_anything_is_built():
    db = _db()
    me = _user(db, "demo-presenter")
    bp = _bp(db)
    for bad in ("Has Caps", "x" * 30, "9starts-with-digit"):
        try:
            _launch(db, me, bp, name=bad)
            raise AssertionError(f"accepted name {bad!r}")
        except svc.CellError:
            pass
    _launch(db, me, bp, name="dup")
    try:
        _launch(db, me, bp, name="dup")
        raise AssertionError("two live cells with one name")
    except svc.CellError:
        pass
    assert len(CALLS["created"]) == 4, "a refused launch built something"


def test_one_member_failing_keeps_the_rest_and_fails_the_cell():
    db = _db()
    me = _user(db, "demo-presenter")
    boom = dict(GKE, label="boom")
    cell = _launch(db, me, _bp(db, items=(MYSQL, boom)))
    by_label = {m.label.split("-")[-1]: m for m in _members(db, cell)}
    assert by_label["boom"].status == "failed"
    assert "admission policy refused" in by_label["boom"].last_error
    assert by_label["db"].ref_id
    assert cell.status == "failed" and "admission policy" in cell.status_message


def test_a_launcher_who_can_no_longer_sign_in_builds_nothing():
    db = _db()
    me = _user(db, "demo-presenter")
    start = utcnow() + timedelta(hours=1)
    cell = _launch(db, me, _bp(db), run_at=start.strftime("%Y-%m-%dT%H:%M"))
    me.is_active = False
    db.commit()
    asyncio.run(svc.sweep(db, now=start + timedelta(minutes=5)))
    db.refresh(cell)
    assert cell.status == "failed" and CALLS["created"] == []


# ── roll-up ──────────────────────────────────────────────────────────────────

def test_the_cell_goes_active_once_every_member_is_ready():
    db = _db()
    me = _user(db, "demo-presenter")
    cell = _launch(db, me, _bp(db))
    assert svc.refresh(db, cell).status == "provisioning"
    _finish(db, cell)
    assert svc.refresh(db, cell).status == "active"


def test_a_member_failing_later_fails_the_cell():
    db = _db()
    me = _user(db, "demo-presenter")
    cell = _launch(db, me, _bp(db))
    _finish(db, cell, other="failed")
    assert svc.refresh(db, cell).status == "failed"


# ── teardown ─────────────────────────────────────────────────────────────────

def test_destroy_tears_down_every_member_through_its_own_path():
    db = _db()
    me = _user(db, "demo-presenter")
    cell = _launch(db, me, _bp(db))
    _finish(db, cell)
    svc.refresh(db, cell)
    svc.start_destroy(db, cell, actor=me.username)
    assert cell.status == "destroying" and cell.expires_at is None
    kinds = sorted(k for k, _, _ in CALLS["decommissioned"])
    assert kinds == ["database", "k8s"]
    destroys = db.query(Job).filter(Job.job_type == "gce_destroy",
                                    Job.created_by == me.username).all()
    names = {d.metadata_dict.get("instance_name") for d in destroys}
    assert {f"{cell.name}-web-1", f"{cell.name}-web-2"} <= names


def test_a_cell_is_destroyed_once_every_member_is_gone():
    db = _db()
    me = _user(db, "demo-presenter")
    cell = _launch(db, me, _bp(db))
    _finish(db, cell)
    svc.start_destroy(db, cell, actor=me.username)
    for m in _members(db, cell):
        if m.kind == "vm":
            j = db.query(Job).filter(Job.id == m.ref_id).first()
            meta = j.metadata_dict
            meta["destroyed"] = True
            j.metadata_dict = meta
        else:
            model = K8sCluster if m.kind == "k8s" else CloudDatabase
            row = db.query(model).filter(model.id == m.ref_id).first()
            db.delete(row)
    db.commit()
    asyncio.run(svc.sweep(db))
    db.refresh(cell)
    assert cell.status == "destroyed"


def test_a_member_still_building_is_torn_down_on_a_later_pass_not_mid_apply():
    db = _db()
    me = _user(db, "demo-presenter")
    cell = _launch(db, me, _bp(db, items=(GKE,)))
    svc.start_destroy(db, cell, actor=me.username)
    assert CALLS["decommissioned"] == [], "decommissioned a cluster mid-provision"
    _finish(db, cell)
    asyncio.run(svc.sweep(db))
    assert [k for k, _, _ in CALLS["decommissioned"]] == ["k8s"]


def test_a_vm_whose_deploy_has_not_started_is_cancelled_not_destroyed():
    db = _db()
    me = _user(db, "demo-presenter")
    cell = _launch(db, me, _bp(db, items=(dict(UBUNTU, count=1),)))
    svc.start_destroy(db, cell, actor=me.username)
    m = _members(db, cell)[0]
    assert db.query(Job).filter(Job.id == m.ref_id).first().status == "cancelled"
    assert cell.status == "destroyed"


def test_destroying_a_booked_cell_cancels_it_and_builds_nothing():
    db = _db()
    me = _user(db, "demo-presenter")
    start = utcnow() + timedelta(days=1)
    cell = _launch(db, me, _bp(db), run_at=start.strftime("%Y-%m-%dT%H:%M"))
    svc.start_destroy(db, cell, actor=me.username)
    asyncio.run(svc.sweep(db, now=start + timedelta(hours=1)))
    db.refresh(cell)
    assert cell.status == "destroyed" and CALLS["created"] == []


def test_the_sweep_ends_a_cell_at_its_end_time_as_its_presenter():
    db = _db()
    me = _user(db, "demo-presenter")
    cell = _launch(db, me, _bp(db), ttl=2)
    _finish(db, cell)
    asyncio.run(svc.sweep(db, now=cell.expires_at - timedelta(minutes=1)))
    db.refresh(cell)
    assert cell.status == "active"
    end = cell.expires_at
    asyncio.run(svc.sweep(db, now=end + timedelta(minutes=1)))
    db.refresh(cell)
    assert cell.status == "destroying"
    mine = {m.ref_id for m in _members(db, cell)}
    actors = {a for _, rid, a in CALLS["decommissioned"] if rid in mine}
    assert actors == {me.username}, actors


def test_moving_the_end_moves_every_members_backstop():
    db = _db()
    me = _user(db, "demo-presenter")
    cell = _launch(db, me, _bp(db), ttl=2)
    new_end = cell.expires_at + timedelta(hours=10)
    svc.set_end(db, cell, new_end)
    for m in _members(db, cell):
        model = {"vm": Job, "k8s": K8sCluster}.get(m.kind, CloudDatabase)
        assert db.query(model).filter(model.id == m.ref_id).first().expires_at == \
            new_end + svc.BACKSTOP


# ── whole-cell power ─────────────────────────────────────────────────────────

POWERED = []


def _fake_power(kind):
    def _f(db, rid, action, actor="", **kw):
        POWERED.append((kind, rid, action, actor, kw.get("restop", False)))
        return {"job_id": "p"}
    return _f


k8s_service.start_power = _fake_power("k8s")
cloud_database_service.start_power = _fake_power("database")


def _active_cell(db, me, **bp_kw):
    cell = _launch(db, me, _bp(db, **bp_kw))
    _finish(db, cell)
    svc.refresh(db, cell)
    assert cell.status == "active"
    POWERED.clear()
    return cell


def test_suspend_stops_every_member_through_its_own_power_path():
    db = _db()
    me = _user(db, "demo-presenter")
    cell = _active_cell(db, me)
    svc.power(db, cell, "stop", actor=me.username)
    assert cell.status == "suspended"
    assert sorted(k for k, *_ in POWERED) == ["database", "k8s"]
    assert all(a == "stop" for _, _, a, _, _ in POWERED)
    vm_ids = {m.ref_id for m in _members(db, cell) if m.kind == "vm"}
    power_jobs = [j for j in db.query(Job).filter(Job.job_type == "gce_power").all()
                  if j.metadata_dict.get("deploy_job_id") in vm_ids]
    assert len(power_jobs) == 2 and all(j.metadata_dict["action"] == "stop"
                                        for j in power_jobs)
    assert svc.refresh(db, cell).status == "suspended", "the roll-up woke a suspended cell"

    svc.power(db, cell, "start", actor=me.username)
    assert cell.status == "active"
    assert [a for _, _, a, _, _ in POWERED[-2:]] == ["start", "start"]


def test_suspend_and_resume_refuse_the_wrong_state():
    db = _db()
    me = _user(db, "demo-presenter")
    cell = _active_cell(db, me)
    for action in ("start",):
        try:
            svc.power(db, cell, action, actor=me.username)
            raise AssertionError("resumed an active cell")
        except svc.CellError:
            pass


def test_a_member_whose_power_op_is_missing_says_so_and_the_rest_still_stop():
    db = _db()
    me = _user(db, "demo-presenter")
    cell = _active_cell(db, me)
    saved = k8s_service.start_power
    del k8s_service.start_power
    try:
        svc.power(db, cell, "stop", actor=me.username)
    finally:
        k8s_service.start_power = saved
    k8s = [m for m in _members(db, cell) if m.kind == "k8s"][0]
    assert "not available" in (k8s.last_error or "")
    assert [k for k, *_ in POWERED] == ["database"]


def test_the_schedule_suspends_and_resumes_the_whole_cell():
    db = _db()
    me = _user(db, "demo-presenter")
    cell = _active_cell(db, me, suspend_at_local="19:00", resume_at_local="07:00",
                        schedule_timezone="UTC")
    day = utcnow().replace(hour=18, minute=50, second=0, microsecond=0) - timedelta(days=1)
    cell.expires_at = day + timedelta(days=5)
    cell.schedule_last_checked_at = day
    db.commit()
    asyncio.run(svc.sweep(db, now=day + timedelta(minutes=20)))      # crossed 19:00
    db.refresh(cell)
    assert cell.status == "suspended", cell.status
    asyncio.run(svc.sweep(db, now=day + timedelta(hours=12, minutes=20)))  # crossed 07:00
    db.refresh(cell)
    assert cell.status == "active", cell.status


def test_a_database_its_cloud_restarted_is_stopped_again_while_suspended():
    db = _db()
    me = _user(db, "demo-presenter")
    cell = _active_cell(db, me)
    svc.power(db, cell, "stop", actor=me.username)
    POWERED.clear()
    import types
    fake = types.ModuleType("web_dashboard.services.clouddb_power")
    fake.needs_restop = lambda row, now=None: True
    sys.modules["web_dashboard.services.clouddb_power"] = fake
    try:
        svc._restop_databases(db, cell, me.username)
    finally:
        del sys.modules["web_dashboard.services.clouddb_power"]
    assert [(k, a, r) for k, _, a, _, r in POWERED] == [("database", "stop", True)]


# ── the real seam and the routes ─────────────────────────────────────────────

def test_the_real_seam_calls_each_clouds_own_handler_as_the_launcher():
    import importlib
    real = importlib.reload(importlib.import_module(
        "web_dashboard.services.custom_cell_service"))._create_member
    seen = {}

    class _Resp:
        job_id = "job-1"

    async def _gce(payload, current_user, db):
        seen["vm"] = (payload.instance_name, payload.workgroup, current_user.username)
        return _Resp()

    def _k8s(payload, db, current_user):
        seen["k8s"] = (payload.name, payload.cloud, current_user.username)
        return {"cluster_id": "c1", "job_id": "j1"}

    async def _clouddb(payload, db, current_user):
        seen["db"] = (payload.name, payload.engine, current_user.username)
        return {"db_id": "d1", "job_id": "j2"}

    from web_dashboard.api import cloud_databases, gcp, k8s
    saved = (gcp.deploy_instance, k8s.provision_cluster, cloud_databases.provision_database)
    gcp.deploy_instance, k8s.provision_cluster = _gce, _k8s
    cloud_databases.provision_database = _clouddb
    try:
        db = _db()
        me = _user(db, "demo-presenter")
        cell = CustomCell(id="x", name="seam", workgroup="demo", created_by=me.username)
        items = svc.validate_items([GKE, MYSQL, dict(UBUNTU, count=1)])
        out = [asyncio.run(real(db, me, cell, it, f"seam-{it['label']}")) for it in items]
    finally:
        gcp.deploy_instance, k8s.provision_cluster, cloud_databases.provision_database = saved
        svc._create_member = _fake_create
    assert seen["vm"] == ("seam-web", "demo", me.username)
    assert seen["k8s"] == ("seam-gke", "gcp", me.username)
    assert seen["db"] == ("seam-db", "mysql", me.username)
    assert out == [("c1", "j1"), ("d1", "j2"), ("job-1", "job-1")]


def test_who_may_launch_and_destroy():
    db = _db()
    presenter = _user(db, "demo-presenter")
    builder = _user(db, "cell-builder")
    auditor = _user(db, "auditor")

    def _ok(dep, u):
        try:
            asyncio.run(dep(current_user=u))
            return True
        except HTTPException:
            return False
    for lvl in ("read", "write", "delete"):
        assert _ok(require_demo(lvl), presenter) and _ok(require_demo(lvl), builder)
        assert not _ok(require_demo(lvl), auditor), lvl


def test_the_routes_launch_list_and_destroy():
    db = _db()
    me = _user(db, "demo-presenter")
    bp = _bp(db)
    out = asyncio.run(api.launch_cell(api.LaunchRequest(blueprint_id=bp.id, name="routes",
                                                        workgroup="demo", ttl_hours=3),
                                      db=db, current_user=me))
    assert out["status"] == "provisioning" and len(out["members"]) == 4
    listed = api.list_cells(db=db, current_user=me)["cells"]
    assert out["id"] in [c["id"] for c in listed]
    end = api.set_cell_end(out["id"], api.EndAtRequest(ttl_hours=5), db=db, current_user=me)
    assert end["end_at"]
    gone = api.destroy_cell(out["id"], db=db, current_user=me)
    assert gone["status"] in ("destroying", "destroyed")


def test_another_workgroups_cell_is_a_404():
    db = _db()
    me = _user(db, "demo-presenter")
    cell = _launch(db, me, _bp(db))
    stranger = _user(db, "demo-presenter")
    stranger.workgroups_list = ["elsewhere"]
    db.commit()
    for fn in (lambda: api.get_cell(cell.id, db=db, current_user=stranger),
               lambda: api.destroy_cell(cell.id, db=db, current_user=stranger)):
        try:
            fn()
            raise AssertionError("reached another workgroup's cell")
        except HTTPException as exc:
            assert exc.status_code == 404


def test_the_change_window_sweep_runs_the_custom_cell_pass():
    src = open(os.path.join(_ROOT, "web_dashboard", "services", "schedule_sweeper.py"),
               encoding="utf-8").read()
    assert "custom_cell_service.has_work" in src
    assert "await _sweep_custom_cells(db, now)" in src


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            import traceback
            traceback.print_exc()
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
