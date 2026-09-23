"""The change-window sweep: what happens to a scheduled job when time passes.

Scheduling itself needs no sweeper. A job booked for Saturday 02:00 is an ordinary
``pending`` row with a future ``scheduled_for``, and ``job_service.claimable_now`` simply
does not select it until then — so the *happy path* costs one clause in a query that was
already running every two seconds, and works for every job type at once.

What needs a sweeper is the UNHAPPY path, and it is the whole reason a change window is
more than a delay timer:

  * **A window that closed while the job was still waiting.** The worker was saturated,
    the app was down, an earlier change overran. Nothing is wrong with the job — it simply
    never got its turn, and the one thing it must not now do is run at 09:00 on Monday.
    ``_reap_missed`` marks it ``cancelled`` with ``missed_window_at``, and a human decides
    whether to rebook it. This is the sweeper's core job and the reason it must run even
    on an install with no recurring schedules at all.

  * **A recurring schedule whose next occurrence has come round.** ``_materialise``
    creates the real job row, which then queues exactly like a hand-booked one.

**A job already RUNNING when its window closes is left alone.** The window governs when
work may BEGIN. Killing a terraform apply or a half-applied playbook at 06:00 because the
clock says so is how you get orphaned cloud resources and a host in a state nobody can
describe -- this tree has scars from precisely that, which is why the job runner survives
gunicorn recycling in the first place. The overrun is recorded and surfaced
(``job_service.schedule_state`` returns ``"overran"``); it is not acted on.

How a pass reaches the worker mirrors the auto-delete and suspend sweeps exactly:

    main._schedule_sweeper_loop ──►  enqueue_sweep_if_due()   [app, gunicorn -w 2]
                                        └─ creates ONE `schedule_sweep` job
    jobs_worker._claim_one      ──►  run()                    [worker, replicas: 3]

The loop only enqueues. ``_claim_one``'s ``UPDATE ... WHERE status='pending'`` rowcount is
what makes the pass single-flight across both app workers and every worker replica,
portably across SQLite and PostgreSQL.

**No feature flag.** Unlike the suspend and spend sweeps, this one has no master switch,
and that is deliberate: the guard it provides is what makes the scheduler safe to use at
all. A deployment where an operator booked a change into a window, the flag was off, and
the job ran twelve hours late would be worse than one with no scheduler. The pass costs
two indexed queries when nothing is scheduled, and ``enqueue_sweep_if_due`` returns early
when there is nothing to look at.
"""
import logging
from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..database import Job
from . import job_service

logger = logging.getLogger(__name__)

SWEEP_JOB_TYPE = "schedule_sweep"

# A new id in the 2026xxxx series. Taken elsewhere: 20260101 (init_db DDL), 20260102
# (audit-chain append), 20260103 (expiry sweep enqueue), 20260401 (suspend), 20260402
# (spend). Sharing one would make this sweep contend with schema init or audit appends,
# which is the failure mode the comment on expiry_reaper's id warns about.
_ENQUEUE_LOCK_ID = 20260501

# How often to evaluate, in seconds. Five minutes by default.
#
# This is the granularity of the whole feature and the number to understand before
# changing it: it bounds how long a job can sit past its window before being marked
# missed, but it does NOT bound how late a job starts -- the claim query picks a job up
# within ``jobs_worker.POLL_INTERVAL`` (2s) of its ``scheduled_for``, with no sweep
# involved. So a slower sweep costs accuracy on the *reap*, not punctuality on the *run*.
_DEFAULT_INTERVAL_SECONDS = 300

# The floor exists for the same reason the suspend sweep's does: below this the pass is
# all overhead. Unlike that one it is also a correctness floor -- see MISSED_GRACE.
_MIN_INTERVAL_SECONDS = 60

# How far past ``window_ends_at`` a job must be before it is reaped as missed.
#
# Without a grace period this sweep races the claim query it is supposed to be a backstop
# for: a job whose window ends at 06:00:00 could be claimed by a worker at 05:59:59.8 and
# marked missed by a sweep at 06:00:00.1 -- leaving a row that is `cancelled` in the
# database while a terraform apply it describes is genuinely running. ``set_missed_window``
# refuses to touch a row that has left ``pending``, so the database stays consistent, but
# the grace is what stops the two from meeting at all.
#
# Comfortably longer than one poll interval, and small enough that an operator watching a
# window close sees the outcome while they are still watching.
MISSED_GRACE = timedelta(seconds=90)

# Rows reaped per pass. A bound rather than an unbounded UPDATE for the same reason
# expiry_reaper caps its deletions: the first pass after a long outage could face every
# job booked during it, and that backlog must not hold the worker slot open.
_REAP_BATCH = 200

# How far back to look for finished occurrences when folding results into their
# schedules. Comfortably more than one sweep interval so nothing is missed when a pass is
# late, and bounded so this stays a small indexed query rather than a scan of every job
# a schedule has ever produced.
_FINISHED_LOOKBACK = 3 * 60 * 60


def _utcnow() -> datetime:
    return datetime.utcnow()


def interval_seconds() -> int:
    """How often to sweep. Read live on every pass, so a Settings change lands on the
    next tick without an app restart.

    Catches Exception, not just the parse errors. Reading a setting is a DATABASE call,
    so a brief blip raises ``OperationalError`` — nothing to do with a malformed value —
    and this sweep is the one with no feature flag protecting it. ``_sweeper_loop``
    would catch the escape and fall back, but ``enqueue_sweep_if_due`` also calls this
    and would then skip a pass for a reason that has nothing to do with scheduling.
    Degrading to the default is strictly better than not sweeping.
    """
    try:
        from . import config_service
        seconds = int(config_service.get("schedule_sweep_interval_seconds", "")
                      or _DEFAULT_INTERVAL_SECONDS)
    except Exception:  # noqa: BLE001 — see above
        seconds = _DEFAULT_INTERVAL_SECONDS
    return max(_MIN_INTERVAL_SECONDS, seconds)


def _min_gap_seconds() -> int:
    """Half the interval, matching ``expiry_policy.sweep_min_gap_seconds``. Not
    configurable: it is a dedupe term, not a tuning knob."""
    return max(1, interval_seconds() // 2)


def has_work(db: Session) -> bool:
    """Is there anything for a pass to do?

    An install that has never used the scheduler should pay nothing for the feature
    existing, and this sweep runs unconditionally (no master flag), so the cheap check
    lives here instead. Two indexed existence queries, and both are empty on a fresh
    install -- so the common answer is False and no job row is written at all.
    """
    scheduled = (db.query(Job.id)
                 .filter(Job.status.in_(job_service.ACTIVE_STATUSES),
                         Job.scheduled_for.isnot(None))
                 .first())
    if scheduled:
        return True
    windowed = (db.query(Job.id)
                .filter(Job.status.in_(job_service.ACTIVE_STATUSES),
                        Job.window_ends_at.isnot(None))
                .first())
    if windowed:
        return True
    # Recurring schedules, and this clause is not optional. Without it an install whose
    # ONLY use of the scheduler is a weekly playbook has nothing pending between
    # occurrences, so no sweep is ever enqueued, so the occurrence is never created —
    # the feature would appear to work right up until it silently never fired.
    from ..database import JobSchedule
    recurring = (db.query(JobSchedule.id)
                 .filter(JobSchedule.enabled.isnot(False))
                 .first())
    return bool(recurring)


def enqueue_sweep_if_due(db: Session, *, min_gap_seconds: "int | None" = None) -> "str | None":
    """Create one sweep job unless a pass is already active or just ran.

    The three guards are the ones ``expiry_reaper.enqueue_sweep_if_due`` documents, and
    the third is not redundant: a liveness check alone provably fails when the work is
    instantaneous, because with two app workers ticking ~0.4s apart a sub-second pass is
    already ``completed`` before the second worker looks. That was measured live -- 5 of
    55 rows were duplicate pairs -- so the recency term is load-bearing, not defensive.
    """
    try:
        if min_gap_seconds is None:
            min_gap_seconds = _min_gap_seconds()
        from ..database import _is_sqlite
        if not _is_sqlite:
            db.execute(text("SELECT pg_advisory_xact_lock(:i)"), {"i": _ENQUEUE_LOCK_ID})
        if not has_work(db):
            return None
        existing = (db.query(Job.id)
                    .filter(Job.job_type == SWEEP_JOB_TYPE,
                            Job.status.in_(job_service.ACTIVE_STATUSES))
                    .first())
        if existing:
            return None
        if min_gap_seconds > 0:
            floor = _utcnow() - timedelta(seconds=min_gap_seconds)
            recent = (db.query(Job.id)
                      .filter(Job.job_type == SWEEP_JOB_TYPE, Job.created_at >= floor)
                      .first())
            if recent:
                return None
        # create_job commits, which ends the transaction and releases the lock.
        job = job_service.create_job(db, job_type=SWEEP_JOB_TYPE, created_by="system")
        return job.id
    except Exception:
        logger.warning("could not enqueue a schedule sweep", exc_info=True)
        db.rollback()
        return None


def due_missed(db: Session, now: datetime) -> list:
    """Jobs whose change window closed before they were ever claimed.

    ``ACTIVE_STATUSES`` minus ``running`` — i.e. ``queued`` and ``pending`` — because a
    job that has STARTED is past the point this rule governs. That filter is the entire
    difference between "the window controls when work begins" and "the window kills work
    in flight", so it is spelled out rather than inherited from a constant that might
    later gain a member.
    """
    return (db.query(Job)
            .filter(Job.status.in_(("queued", "pending")),
                    Job.window_ends_at.isnot(None),
                    Job.window_ends_at < now - MISSED_GRACE)
            .order_by(Job.window_ends_at.asc())
            .limit(_REAP_BATCH)
            .all())


def _reason(job: Job, now: datetime) -> str:
    """Why this job did not run, in the words an operator needs to decide what to do.

    Names the two cases apart, because they call for different actions: an unapproved
    change needs a person, a starved one needs either a longer window or more worker
    capacity. Both are far more useful than "window closed".
    """
    ends = job.window_ends_at.isoformat(sep=" ", timespec="minutes")
    if job.approval_required and not job.approved_at:
        return (f"Change window closed at {ends} UTC without approval — the job was "
                f"never eligible to run. Approve before the window closes, or reschedule.")
    late = int((now - job.window_ends_at).total_seconds() // 60)
    return (f"Change window closed at {ends} UTC before this job could start "
            f"({late} min ago). It was NOT run outside its window; reschedule it.")


def _reap_missed(db: Session, sweep_job_id: str, now: datetime) -> list:
    """Mark every job whose window closed while it waited. Returns what was reaped."""
    reaped = []
    for job in due_missed(db, now):
        reason = _reason(job, now)
        if job_service.set_missed_window(db, job.id, reason) is None:
            continue
        reaped.append({"job_id": job.id, "job_type": job.job_type,
                       "created_by": job.created_by})
        job_service.append_job_log(
            db, sweep_job_id, f"missed window: {job.job_type} {job.id} — {reason}")
        _notify_missed(db, job, reason)
    return reaped


def _notify_missed(db: Session, job: Job, reason: str) -> None:
    """Emit ``job.window_missed``, never raising.

    A missed change is exactly the event an operator wants pushed rather than discovered:
    nothing failed, so no failure alert fires, and the job quietly did not happen. Guarded
    the way ``expiry_reaper._notify`` is — a notification must never break a sweep.
    """
    try:
        from . import notification_service, notify_policy
        notification_service.emit_safe(db, notify_policy.NotificationEvent(
            event_type="job.window_missed",
            title=f"Change window missed: {job.job_type}",
            body=reason,
            resource_id=f"job:{job.id}",
            resource_name=job.job_type,
            workgroup=job.workgroup or "",
            fields={"job_id": job.id, "job_type": job.job_type,
                    "created_by": job.created_by or "",
                    "window_ends_at": job.window_ends_at.isoformat()
                    if job.window_ends_at else ""},
            url=f"/jobs/{job.id}",
        ))
    except Exception:  # noqa: BLE001 — a notification must never break the sweep
        logger.warning("could not emit job.window_missed for %s", job.id, exc_info=True)


def _prune_history(db: Session) -> int:
    """Drop this sweep's own completed rows past the shared retention window.

    Imported inside the function, not at module top: ``expiry_reaper`` pulls in
    ``inventory_service`` and the cloud adapters behind it, and a top-level import would
    make the change-window sweep — which must work on an install with no clouds
    configured at all — depend on all of that at import time.
    """
    try:
        from . import expiry_reaper
        return expiry_reaper.prune_sweep_history(db, job_type=SWEEP_JOB_TYPE)
    except Exception:  # noqa: BLE001 — losing a prune must not fail the pass
        logger.warning("could not prune schedule sweep history", exc_info=True)
        return 0


def _materialise_due(db: Session, sweep_job_id: str, now: datetime) -> list:
    """Create this occurrence's job for every recurring schedule that is due.

    Each schedule is isolated: one that raises is disabled with the reason and the pass
    continues. A single broken schedule must not stop every other change in the estate
    from being booked — and it must not fail the sweep either, because the sweep is also
    what marks missed windows.
    """
    from . import schedule_service
    made = []
    for row, _window, start, end in schedule_service.due(db, now):
        try:
            job = schedule_service.materialise(db, row, start=start, end=end)
        except Exception as exc:  # noqa: BLE001 — see docstring
            logger.warning("schedule %s could not be materialised: %s", row.id, exc)
            db.rollback()
            try:
                schedule_service.disable(
                    db, row, f"could not create this occurrence's job: {exc}")
                db.commit()
            except Exception:  # noqa: BLE001
                db.rollback()
            continue
        if job is None:
            continue
        made.append({"schedule_id": row.id, "name": row.name, "job_id": job.id})
        job_service.append_job_log(
            db, sweep_job_id,
            f"scheduled run: {row.name} → {row.job_type} {job.id} "
            f"(window {start:%Y-%m-%d %H:%M} UTC)")
    return made


def _note_finished(db: Session, now: datetime) -> None:
    """Fold recently-finished occurrences back into their schedules.

    Done here rather than on the job's own completion path so a recurring schedule cannot
    add latency — or a new failure mode — to the job runner itself. The self-disable it
    drives is not time-critical, so trailing reality by one sweep is fine.
    """
    from . import schedule_service
    rows = (db.query(Job)
            .filter(Job.job_schedule_id.isnot(None),
                    Job.status.in_(("completed", "failed", "cancelled")),
                    Job.completed_at.isnot(None),
                    Job.completed_at >= now - timedelta(seconds=_FINISHED_LOOKBACK))
            .all())
    for job in rows:
        try:
            schedule_service.note_result(db, job)
        except Exception:  # noqa: BLE001 — bookkeeping must not fail the pass
            logger.warning("could not record result of %s", job.id, exc_info=True)


async def run(db: Session, *, job_id: str, meta: dict) -> None:
    """One pass: reap missed windows, materialise due schedules, record outcomes."""
    job_service.set_running(db, job_id)
    now = _utcnow()
    try:
        reaped = _reap_missed(db, job_id, now)
        created = _materialise_due(db, job_id, now)
        _note_finished(db, now)
        db.commit()
        # This sweep writes a row per pass whether or not it found anything, so it prunes
        # its own history the way the auto-delete sweep does — otherwise it would push
        # real deploys off the first page of /jobs within days. Shares that feature's
        # retention setting (a plain config read, not gated on the auto-delete flag) so
        # there is ONE answer to "how long is a routine sweep row kept".
        pruned = _prune_history(db)
        job_service.set_completed(db, job_id, {
            "missed": reaped,
            "missed_count": len(reaped),
            "scheduled": created,
            "scheduled_count": len(created),
            "pruned": pruned,
        })
    except Exception as exc:
        logger.error("schedule sweep failed: %s", exc)
        db.rollback()
        job_service.set_failed(db, job_id, str(exc))
