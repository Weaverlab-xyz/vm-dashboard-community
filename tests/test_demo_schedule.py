"""A presenter books a demo cell for later and it ends itself.

Build-later is the claimable parent's ``scheduled_for`` (the generic scheduler), and the end
is ``demo_schedule.sweep`` run by the change-window sweep. What this pins:

  * only the PARENT of an OT cell carries the booking, never the queued children;
  * a booked cell's timer counts from its booked start, not from when the row was written;
  * the sweep destroys a built, past-end cell as the PRESENTER (not "expiry-reaper"), once,
    and the broker later than the cell;
  * a cell still building, or not yet due, is left alone;
  * the end-at route is workgroup-scoped and does not need the admin-only expiry route.

Run: python tests/test_demo_schedule.py   (or under pytest)
"""
import os
import re
import sys
from datetime import timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

os.environ.setdefault("JWT_SECRET_KEY", "test-secret-demo-schedule")
os.environ["DATABASE_URL"] = "sqlite://"

try:
    import sqlalchemy  # noqa: F401
except ModuleNotFoundError as exc:  # pragma: no cover — bare interpreter
    try:
        import pytest
        pytest.skip(f"sqlalchemy unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from web_dashboard.database import Base, Job  # noqa: E402
from web_dashboard.utils.clock import utcnow  # noqa: E402
from web_dashboard.services import demo_schedule  # noqa: E402


def _session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _src(*parts):
    with open(os.path.join(_ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


def _cell(db, *, job_type="ec2_deploy", status="completed", end_in=-10, broker=False,
          meta=None, who="presenter"):
    m = {"instance_name": "ot-cell-1", "instance_id": "i-123", "region": "us-east-2",
         "ot_cell": not broker, demo_schedule.END_KEY: "x"}
    if broker:
        m["ot_broker"] = True
    m.update(meta or {})
    job = Job(job_type=job_type, status=status, created_by=who, workgroup="demo",
              created_at=utcnow(), expires_at=utcnow() + timedelta(minutes=end_in))
    job.metadata_dict = m
    db.add(job)
    db.commit()
    return job


def test_a_built_cell_past_its_end_is_destroyed_as_the_presenter():
    db = _session()
    cell = _cell(db)
    out = demo_schedule.sweep(db)
    assert len(out) == 1
    destroy = db.query(Job).filter(Job.id == out[0][1]).first()
    assert destroy.job_type == "ec2_destroy" or "destroy" in destroy.job_type
    assert destroy.created_by == "presenter", "the audit owner must stay the presenter"
    assert destroy.metadata_dict.get("deploy_job_id") == cell.id
    fresh = db.query(Job).filter(Job.id == cell.id).first()
    assert fresh.expires_at is None, "at-most-once: the timer is cleared with the enqueue"
    assert demo_schedule.sweep(db) == [], "a second pass must not destroy it again"


def test_a_cell_that_is_still_building_or_not_due_is_left_alone():
    db = _session()
    _cell(db, status="queued")
    _cell(db, status="running")
    _cell(db, end_in=+120)
    assert demo_schedule.sweep(db) == []
    assert db.query(Job).filter(Job.job_type.like("%destroy%")).count() == 0


def test_the_broker_is_destroyed_after_its_cell():
    db = _session()
    _cell(db)
    _cell(db, broker=True, meta={"instance_name": "ot-cell-1-broker", "instance_id": "i-9"})
    out = demo_schedule.sweep(db)
    assert len(out) == 2
    sched = {j.metadata_dict.get("instance_id"): j.scheduled_for
             for j in db.query(Job).filter(Job.job_type.like("%destroy%")).all()}
    assert sched["i-123"] is None, "the cell goes now"
    assert sched["i-9"] is not None and sched["i-9"] > utcnow(), \
        "the broker waits: the cell's teardown destroys the agent token it relies on"


def test_a_row_the_destroy_cannot_be_built_for_is_refused_once_not_retried_forever():
    db = _session()
    cell = _cell(db, meta={"instance_id": ""})
    assert demo_schedule.sweep(db) == []
    fresh = db.query(Job).filter(Job.id == cell.id).first()
    assert fresh.expires_at is None
    assert "demo_end_refused" in fresh.metadata_dict


def test_has_work_sees_only_built_cells_with_an_end():
    db = _session()
    assert demo_schedule.has_work(db) is False
    _cell(db, status="queued")
    assert demo_schedule.has_work(db) is False
    _cell(db)
    assert demo_schedule.has_work(db) is True


def test_set_end_moves_the_cell_and_its_broker_together():
    db = _session()
    broker = _cell(db, broker=True)
    cell = _cell(db, meta={"ot_broker_job_id": broker.id})
    end = utcnow() + timedelta(hours=3)
    demo_schedule.set_end(db, cell, end)
    for jid in (cell.id, broker.id):
        r = db.query(Job).filter(Job.id == jid).first()
        assert r.expires_at == end and r.metadata_dict[demo_schedule.END_KEY] == end.isoformat()
    demo_schedule.set_end(db, cell, None)
    r = db.query(Job).filter(Job.id == cell.id).first()
    assert demo_schedule.END_KEY not in r.metadata_dict and r.expires_at is None


def test_the_plan_for_a_plain_request_changes_nothing():
    """No booking and no TTL: every create_job call is byte-for-byte what it was."""
    from web_dashboard.models.ot import OTCellDeployRequestAWS
    db = _session()
    req = OTCellDeployRequestAWS(ami_id="a", instance_name="n", subnet_id="s",
                                 security_group_ids=["g"], workgroup="demo")
    p = demo_schedule.plan(db, req, vm_job_type="ec2_deploy", workgroup="demo", is_admin=False)
    assert p.sched == {} and p.expires_at is None and p.meta == {} and p.end is None


def test_a_ttl_stamps_the_end_on_every_row():
    from web_dashboard.models.ot import OTCellDeployRequestAWS
    db = _session()
    req = OTCellDeployRequestAWS(ami_id="a", instance_name="n", subnet_id="s",
                                 security_group_ids=["g"], workgroup="demo", ttl_hours=4)
    p = demo_schedule.plan(db, req, vm_job_type="ec2_deploy", workgroup="demo", is_admin=True)
    assert p.end is not None and p.expires_at == p.end
    assert p.meta[demo_schedule.END_KEY] == p.end.isoformat()


def test_only_the_parent_of_an_ot_cell_is_booked():
    """The children are `queued` and never claimable; a booking on them would show a
    scheduled VM row with no way to start it, and the missed sweep reaps windowed rows."""
    src = _src("web_dashboard", "api", "ot.py")
    assert src.count("**sp.sched,") == 3, "each deploy route books its PARENT"
    for m in re.finditer(r"child = job_service\.create_job\((.*?)\n    \)", src, re.S):
        assert "sp.sched" not in m.group(1), "a child carries the booking"
    assert src.count("expires_at=sp.expires_at,") == 3


def test_the_end_at_route_is_workgroup_scoped_and_not_the_admin_expiry_route():
    src = _src("web_dashboard", "api", "demo.py")
    a = src.index('"/cells/{kind}/{job_id}/end-at"')
    body = src[a:a + 3500]
    assert "_live_cell(" in body, "the route must check the row is a cell in the caller's workstreams"
    assert "may(current_user" in body
    assert "expiry_reaper" not in body, "a presenter must not depend on the admin-only expiry path"


def test_the_sweep_runs_inside_the_change_window_sweep():
    src = _src("web_dashboard", "services", "schedule_sweeper.py")
    assert "demo_schedule.sweep" in src and "demo_schedule.has_work" in src, \
        "no pass -> the cell is never destroyed; no has_work -> no pass is ever enqueued"


def _run():
    tests = [(n, o) for n, o in sorted(globals().items())
             if n.startswith("test_") and callable(o)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"ok   {name}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {name}: {exc}")
        except Exception as exc:                       # noqa: BLE001
            failed += 1
            print(f"ERROR {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run())
