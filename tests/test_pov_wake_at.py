"""The one-off warm-up before a demo: ``services/pov_wake`` and its sweep.

What is pinned, because each fails quietly:

  * **It fires at most once.** The booking is cleared the first time the sweep evaluates
    it as due -- having started the lab, finding it already running, finding a power job
    in flight, or finding the demo long over. A booking that could fire twice would wake a
    lab an SE had since suspended on purpose.
  * **It runs on a platform with its own idle timer.** Skytap refuses the recurring
    schedule; a warm-up only starts a lab, so it must not inherit that skip -- which it
    would by sitting a few lines lower in the same loop.
  * **The time means the SE's wall clock**: 14:00 Europe/London in October is 13:00 UTC.
  * **It says when it cannot deliver** a running lab for the whole call.

Runs under pytest, or standalone:
    python tests/test_pov_wake_at.py
"""
import os
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="pov-wake-test-"), "test.db")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_TMPDB}")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-pov-wake")

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover
    print("SKIP: fastapi not installed")
    sys.exit(0)

from web_dashboard import database as d  # noqa: E402

d.Base.metadata.create_all(bind=d.engine)

from web_dashboard.api import pov as pov_api  # noqa: E402
from web_dashboard.api.auth import get_current_user  # noqa: E402
from web_dashboard.services import lab_platforms, pov_reconcile, pov_wake  # noqa: E402

NOW = datetime(2026, 10, 20, 9, 0, 0)


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ── plan: what the form sent ─────────────────────────────────────────────────

def test_the_time_is_the_ses_wall_clock():
    wake, demo, lead = pov_wake.plan("2026-10-22T14:00", "Europe/London", 20, now=NOW)
    assert demo == datetime(2026, 10, 22, 13, 0), demo   # BST is UTC+1 in October
    assert wake == datetime(2026, 10, 22, 12, 40)
    assert lead == 20


def test_blank_timezone_is_utc_and_lead_defaults():
    wake, demo, lead = pov_wake.plan("2026-10-22T14:00", "", None, now=NOW)
    assert demo == datetime(2026, 10, 22, 14, 0)
    assert lead == pov_wake.DEFAULT_LEAD_MINUTES


def test_nonsense_is_refused_in_words():
    for args in (("tomorrow", "UTC", 20), ("2026-10-19T14:00", "UTC", 20),
                 ("2027-06-01T14:00", "UTC", 20), ("2026-10-22T14:00", "UTC", -5),
                 ("2026-10-22T14:00", "UTC", 999)):
        try:
            pov_wake.plan(*args, now=NOW)
        except pov_wake.WakeError as exc:
            assert str(exc)
        else:
            raise AssertionError(f"{args} was accepted")


def test_a_lead_reaching_into_the_past_starts_on_the_next_pass():
    wake, _demo, _lead = pov_wake.plan("2026-10-20T09:10", "UTC", 60, now=NOW)
    assert wake == NOW


# ── warnings ─────────────────────────────────────────────────────────────────

class _Row:
    suspend_on_idle_seconds = 0
    suspend_at_local = ""
    schedule_timezone = ""
    schedule_days = ""


def test_a_lead_shorter_than_the_pass_is_warned():
    w = pov_wake.warnings(_Row(), NOW, NOW + timedelta(minutes=5), interval_s=600,
                          idle_suspend=False)
    assert any("every 10 min" in x for x in w), w


def test_an_idle_timer_shorter_than_the_lead_is_warned():
    row = _Row()
    row.suspend_on_idle_seconds = 15 * 60
    w = pov_wake.warnings(row, NOW, NOW + timedelta(minutes=30), interval_s=600,
                          idle_suspend=True)
    assert any("idle" in x for x in w), w


def test_a_recurring_suspend_during_the_call_is_warned():
    row = _Row()
    row.suspend_at_local, row.schedule_timezone, row.schedule_days = "10:00", "UTC", "1111111"
    w = pov_wake.warnings(row, NOW, NOW + timedelta(minutes=30), interval_s=600,
                          idle_suspend=False)
    assert any("suspends it at 10:00" in x for x in w), w
    quiet = pov_wake.warnings(_Row(), NOW, NOW + timedelta(minutes=30), interval_s=600,
                              idle_suspend=False)
    assert quiet == []


# ── the sweep ────────────────────────────────────────────────────────────────

def _env(*, runstate="suspended", platform="skytap", wake_in=timedelta(minutes=-1),
         demo_in=timedelta(minutes=19)):
    db = d.SessionLocal()
    try:
        env = d.PovEnvironment(platform=platform, name="pov-" + uuid.uuid4().hex[:8],
                               status="active", runstate=runstate,
                               platform_environment_id="123")
        now = _utcnow()
        pov_wake.book(env, now + wake_in, now + demo_in, 20, by="se", tz_name="UTC")
        db.add(env)
        db.commit()
        return env.id
    finally:
        db.close()


def _power_jobs(env_id):
    db = d.SessionLocal()
    try:
        rows = db.query(d.Job).filter(d.Job.job_type == "pov_env_power").all()
        return [r for r in rows if r.metadata_dict.get("environment_id") == env_id]
    finally:
        db.close()


def _wake_of(env_id):
    db = d.SessionLocal()
    try:
        env = db.get(d.PovEnvironment, env_id)
        return env.wake_at, pov_wake.describe(env)
    finally:
        db.close()


def _sweep():
    db = d.SessionLocal()
    try:
        return pov_reconcile.sweep_schedules(db)
    finally:
        db.close()


def test_a_due_booking_starts_the_lab_once_even_on_an_idle_timer_platform():
    assert lab_platforms.supports("skytap", "idle_suspend"), "the premise moved"
    env_id = _env()
    _sweep()
    jobs = _power_jobs(env_id)
    assert len(jobs) == 1, f"expected one start, got {len(jobs)}"
    assert jobs[0].metadata_dict["runstate"] == "running"
    assert jobs[0].created_by == pov_wake.WAKE_ACTOR
    assert _wake_of(env_id)[0] is None, "the booking was not cleared"
    _sweep()
    assert len(_power_jobs(env_id)) == 1, "the booking fired twice"


def test_a_booking_not_yet_due_is_left_alone():
    env_id = _env(wake_in=timedelta(minutes=30), demo_in=timedelta(minutes=50))
    _sweep()
    assert not _power_jobs(env_id)
    assert _wake_of(env_id)[1]["booked"] is True


def test_already_running_clears_without_a_job():
    env_id = _env(runstate="running")
    _sweep()
    assert not _power_jobs(env_id)
    assert _wake_of(env_id)[0] is None


def test_a_power_job_in_flight_clears_without_a_second():
    from web_dashboard.services import job_service
    env_id = _env()
    db = d.SessionLocal()
    try:
        job_service.create_job(db, job_type="pov_env_power", created_by="se",
                               metadata={"environment_id": env_id, "runstate": "running"})
    finally:
        db.close()
    _sweep()
    assert len(_power_jobs(env_id)) == 1
    assert _wake_of(env_id)[0] is None


def test_a_booking_for_a_demo_long_over_is_dropped():
    env_id = _env(wake_in=timedelta(hours=-5), demo_in=timedelta(hours=-4))
    _sweep()
    assert not _power_jobs(env_id), "a lab was started for a call that already ended"
    assert _wake_of(env_id)[0] is None


def test_the_row_carries_the_booking():
    env_id = _env(wake_in=timedelta(minutes=30), demo_in=timedelta(minutes=50))
    db = d.SessionLocal()
    try:
        out = pov_reconcile.describe(db.get(d.PovEnvironment, env_id))
    finally:
        db.close()
    assert out["wake"]["booked"] is True
    assert out["wake"]["wake_at"].endswith("Z") and out["wake"]["demo_at"].endswith("Z")


# ── the route ────────────────────────────────────────────────────────────────

class _Operator:
    username = "operator"
    is_admin = True
    is_effective_admin = True
    effective_permissions_dict = {}
    accessor_env_id = None
    pov_env_ids_list = []
    workgroups_list = []


def _db():
    db = d.SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _client():
    app = FastAPI()
    app.include_router(pov_api.router)
    app.dependency_overrides[get_current_user] = lambda: _Operator()
    app.dependency_overrides[d.get_db] = _db
    return TestClient(app, raise_server_exceptions=False)


def _bare_env(**over):
    db = d.SessionLocal()
    try:
        fields = {"platform": "skytap", "name": "pov-" + uuid.uuid4().hex[:8],
                  "status": "active", "runstate": "suspended",
                  "platform_environment_id": "123"}
        fields.update(over)
        env = d.PovEnvironment(**fields)
        db.add(env)
        db.commit()
        return env.id
    finally:
        db.close()


def test_book_then_cancel_through_the_route():
    env_id = _bare_env()
    when = (_utcnow() + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
    res = _client().post(f"/api/pov/managed/{env_id}/wake-at",
                         json={"demo_at_local": when, "timezone": "UTC", "lead_minutes": 5})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["wake"]["booked"] is True
    assert body["warnings"], "a 5-minute lead under a 10-minute pass was not warned"
    res = _client().post(f"/api/pov/managed/{env_id}/wake-at", json={"demo_at_local": ""})
    assert res.status_code == 200
    assert res.json()["wake"]["booked"] is False
    assert _wake_of(env_id)[0] is None


def test_the_route_refuses_what_it_cannot_do():
    c = _client()
    assert c.post(f"/api/pov/managed/{_bare_env()}/wake-at",
                  json={"demo_at_local": "2020-01-01T10:00"}).status_code == 400
    assert c.post(f"/api/pov/managed/{_bare_env(platform_environment_id=None)}/wake-at",
                  json={"demo_at_local": "2030-01-01T10:00"}).status_code == 409
    assert c.post(f"/api/pov/managed/{_bare_env(status='destroyed')}/wake-at",
                  json={"demo_at_local": "2030-01-01T10:00"}).status_code == 409
    assert c.post("/api/pov/managed/nope/wake-at",
                  json={"demo_at_local": "2030-01-01T10:00"}).status_code == 404


def test_the_column_has_its_retrofit():
    src = open(d.__file__, encoding="utf-8").read()
    assert "ALTER TABLE pov_environments ADD COLUMN wake_at TIMESTAMP" in src


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
