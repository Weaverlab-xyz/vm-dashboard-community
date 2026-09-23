"""A change window that closes must stop the job, not delay it.

The claim predicate (tests/test_job_scheduling_claim.py) makes a job wait for its window.
This file covers the other half, which is the half that makes a window a *window* rather
than a delay timer: what happens when the window closes and the job still has not run.

The rule is **skip and mark missed**. A change approved for 02:00–06:00 on Saturday must
never run at 09:00 on Monday because a worker happened to free up then — that is the exact
outcome change control exists to prevent, and a scheduler without this guard is more
dangerous than no scheduler, because the operator believes the window was honoured.

Two boundaries are load-bearing here and each has a test below:

  * a job that has STARTED is never touched — the window governs when work may BEGIN, and
    killing a terraform apply or a half-applied playbook at the boundary orphans cloud
    resources;
  * the reap waits ``MISSED_GRACE`` past the window end, so it cannot race the claim query
    it is backstopping and cancel a row a worker picked up a fraction of a second earlier.

Run: python tests/test_schedule_sweeper.py   (or under pytest)
"""
import asyncio
import os
import sys
from datetime import datetime, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

os.environ.setdefault("JWT_SECRET_KEY", "test-secret-schedule-sweeper")
os.environ["DATABASE_URL"] = "sqlite://"

# Probe the optional THIRD-PARTY dependency by name, then import first-party unguarded.
# A wider guard here would let a broken `schedule_sweeper` import turn this file into a
# silent no-op — and the missed-window reap is the property that stops a change running
# outside its approved window. See tests/test_import_guard_narrowness.py.
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
from web_dashboard.services import job_service, schedule_sweeper  # noqa: E402


def _session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _add(db, **kw):
    kw.setdefault("job_type", "ansible_local")
    kw.setdefault("status", "pending")
    kw.setdefault("created_at", datetime.utcnow())
    kw.setdefault("created_by", "tester")
    job = Job(**kw)
    db.add(job)
    db.commit()
    return job


def _closed(minutes_ago=30):
    """A window whose end is comfortably past MISSED_GRACE."""
    return datetime.utcnow() - timedelta(minutes=minutes_ago)


# ── The core rule ─────────────────────────────────────────────────────────────

def test_a_job_whose_window_closed_is_marked_missed():
    db = _session()
    job = _add(db, scheduled_for=_closed(90), window_ends_at=_closed(30))

    job_service.set_missed_window(db, job.id, "window closed")
    db.refresh(job)

    assert job.status == "cancelled", f"expected cancelled, got {job.status}"
    assert job.missed_window_at is not None
    assert job.completed_at is not None


def test_a_missed_job_is_not_marked_failed():
    """`failed` would put a change that never ran into the failed-jobs panel AND into the
    dead-letter tail (`status='failed' AND attempts > 0`), which means "used every retry".
    Neither is true: nothing went wrong, the job simply did not happen."""
    db = _session()
    job = _add(db, window_ends_at=_closed())
    job_service.set_missed_window(db, job.id, "window closed")
    db.refresh(job)
    assert job.status != "failed"
    assert (job.attempts or 0) == 0


def test_a_missed_job_is_no_longer_claimable():
    """The point of the whole exercise: after the reap it can never run."""
    db = _session()
    job = _add(db, scheduled_for=_closed(90), window_ends_at=_closed(30))
    job_service.set_missed_window(db, job.id, "window closed")

    claimable = db.query(Job).filter(Job.status == "pending",
                                     *job_service.claimable_now()).all()
    assert claimable == [], "a job whose window closed was still claimable"


def test_the_sweep_reaps_it_end_to_end():
    """Through `run()` itself, including its job bookkeeping."""
    db = _session()
    sweep = _add(db, job_type="schedule_sweep", created_by="system")
    doomed = _add(db, scheduled_for=_closed(90), window_ends_at=_closed(30))

    asyncio.run(schedule_sweeper.run(db, job_id=sweep.id, meta={}))

    db.refresh(doomed)
    db.refresh(sweep)
    assert doomed.status == "cancelled", f"not reaped: {doomed.status}"
    assert sweep.status == "completed", f"sweep did not finish: {sweep.status}"
    assert sweep.metadata_dict.get("missed_count") == 1


# ── The two boundaries ────────────────────────────────────────────────────────

def test_a_running_job_is_never_reaped():
    """The window governs when work may BEGIN. A job already applying infrastructure is
    left to finish — interrupting it is how you get orphaned cloud resources and a host
    in a state nobody can describe."""
    db = _session()
    job = _add(db, status="running", started_at=_closed(60),
               window_ends_at=_closed(30))

    due = schedule_sweeper.due_missed(db, datetime.utcnow())
    assert job.id not in [j.id for j in due], "the sweep selected a RUNNING job"

    job_service.set_missed_window(db, job.id, "window closed")
    db.refresh(job)
    assert job.status == "running", "a running job was cancelled at the window boundary"


def test_the_reap_waits_out_the_grace_period():
    """A job whose window ended one second ago may have been claimed a fraction of a
    second earlier. Without the grace the sweep races the claim query it is backstopping.
    """
    db = _session()
    job = _add(db, window_ends_at=datetime.utcnow() - timedelta(seconds=1))
    due = schedule_sweeper.due_missed(db, datetime.utcnow())
    assert job.id not in [j.id for j in due], (
        "reaped a job the instant its window closed — this races _claim_one")


def test_a_job_with_no_window_is_never_reaped():
    """`window_ends_at IS NULL` is every job that predates this feature and every
    unscheduled one. They must be invisible to this sweep forever."""
    db = _session()
    job = _add(db, created_at=datetime.utcnow() - timedelta(days=400))
    due = schedule_sweeper.due_missed(db, datetime.utcnow())
    assert job.id not in [j.id for j in due], "reaped a job that carries no window"


def test_a_job_still_inside_its_window_is_not_reaped():
    db = _session()
    job = _add(db, scheduled_for=datetime.utcnow() - timedelta(minutes=5),
               window_ends_at=datetime.utcnow() + timedelta(hours=3))
    due = schedule_sweeper.due_missed(db, datetime.utcnow())
    assert job.id not in [j.id for j in due]


# ── Reasons an operator can act on ────────────────────────────────────────────

def test_an_unapproved_job_says_so():
    """The two ways a window is missed need different actions — a person, versus more
    capacity or a longer window — so the message names which happened."""
    db = _session()
    job = _add(db, approval_required=True, window_ends_at=_closed())
    reason = schedule_sweeper._reason(job, datetime.utcnow())
    assert "approval" in reason.lower(), reason


def test_a_starved_job_says_so():
    db = _session()
    job = _add(db, window_ends_at=_closed())
    reason = schedule_sweeper._reason(job, datetime.utcnow())
    assert "approval" not in reason.lower(), reason
    assert "reschedule" in reason.lower(), reason


def test_the_reason_is_stored_on_the_job():
    """error_message is the one field the job detail page already surfaces for a
    non-successful job, so the explanation has to land there."""
    db = _session()
    job = _add(db, window_ends_at=_closed())
    job_service.set_missed_window(db, job.id, "the window closed at 06:00")
    db.refresh(job)
    assert "06:00" in (job.error_message or "")


# ── Enqueue guards ────────────────────────────────────────────────────────────

def test_nothing_is_enqueued_when_nothing_is_scheduled():
    """A deployment that has never used the scheduler must pay nothing for it existing.
    This sweep has no feature flag, so `has_work` is what keeps it free."""
    db = _session()
    _add(db)  # an ordinary unscheduled job
    assert schedule_sweeper.enqueue_sweep_if_due(db) is None
    assert db.query(Job).filter(Job.job_type == "schedule_sweep").count() == 0


def test_a_sweep_is_enqueued_when_something_is_scheduled():
    db = _session()
    _add(db, scheduled_for=datetime.utcnow() + timedelta(hours=4))
    assert schedule_sweeper.enqueue_sweep_if_due(db) is not None


def test_a_second_tick_does_not_enqueue_a_duplicate():
    """Two gunicorn workers reach the same tick ~0.4s apart. The active-job check covers
    this one; the recency check below covers the case it provably cannot."""
    db = _session()
    _add(db, scheduled_for=datetime.utcnow() + timedelta(hours=4))
    first = schedule_sweeper.enqueue_sweep_if_due(db)
    second = schedule_sweeper.enqueue_sweep_if_due(db)
    assert first is not None and second is None
    assert db.query(Job).filter(Job.job_type == "schedule_sweep").count() == 1


def test_the_recency_guard_holds_when_the_pass_already_finished():
    """The failure the active-job check alone provably misses.

    A sweep with nothing to do completes in well under a second, so by the time the
    second app worker looks, the row is already `completed` and the liveness test passes.
    Measured on the live install for the auto-delete sweep: 5 of 55 rows were duplicate
    pairs 0.13-0.4s apart. Only the recency term catches it.
    """
    db = _session()
    _add(db, scheduled_for=datetime.utcnow() + timedelta(hours=4))
    first = schedule_sweeper.enqueue_sweep_if_due(db)
    done = db.query(Job).filter(Job.id == first).first()
    done.status = "completed"          # the pass finished between the two ticks
    done.completed_at = datetime.utcnow()
    db.commit()

    assert schedule_sweeper.enqueue_sweep_if_due(db) is None, (
        "a duplicate sweep was enqueued because the first had already completed — "
        "the liveness check alone cannot dedupe instantaneous work")


def test_the_interval_is_floored():
    """Below a minute the pass is all overhead, and the grace period assumes the sweep
    is not running continuously."""
    assert schedule_sweeper.interval_seconds() >= 60


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
