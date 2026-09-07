"""The suspend-schedule sweep: business-hours power windows for cloud VMs.

``suspend_schedule`` answers *when* (pure policy, no clock of its own);
``vm_suspend_policy`` answers *whether a VM may carry one at all*; this walks the
inventory, asks both, and enqueues the power job. It never powers anything itself — for a
VM it enqueues the identical ``*_power`` row the Start/Suspend button creates, which is
what keeps this small and means the audit row, the ``/jobs`` entry and the ownership
record all happen exactly as they do when a human presses it.

**Its own sweep, not folded into the auto-delete pass.** They look alike and the temptation
is real, but ``expiry_sweep`` is gated on ``resource_expiry_enabled`` — and a suspend
schedule must work for an operator who has never turned the destructive timer on. Sharing
the pass would have made the reversible feature depend on the irreversible one's flag.

**Two gates, not the timer's four.** ``pov_spend`` already made this argument and it holds
here: the action is reversible, so it earns a lighter brake than something that destroys.
What it does NOT skip is the NULL latch — a schedule that has never been evaluated acts on
nothing, so enabling this on an existing fleet cannot suspend a backlog of boundaries that
were crossed while nobody was watching. That rule lives in ``suspend_schedule.due_action``.

How a pass reaches the worker mirrors the auto-delete sweep exactly:

    main._suspend_sweeper_loop  ──►  enqueue_sweep_if_due()   [app, gunicorn -w 2]
                                        └─ creates ONE `suspend_sweep` job
    jobs_worker._claim_one      ──►  run()                    [worker, replicas: 3]

The loop only enqueues. ``_claim_one``'s ``UPDATE ... WHERE status='pending'`` rowcount is
what makes the pass single-flight across both app workers and every worker replica,
portably across SQLite and PostgreSQL.
"""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..database import Job
from . import job_service, suspend_schedule, vm_suspend_policy

logger = logging.getLogger(__name__)

SWEEP_JOB_TYPE = "suspend_sweep"
FLAG = "vm_suspend_schedule_enabled"

_ENQUEUE_LOCK_ID = 20260401
_LAST_SWEEP_KEY = "vm_suspend_last_sweep"

# The power job each cloud's deploy job maps to.
_POWER_JOB = {"aws": "ec2_power", "gcp": "gce_power"}


def _utcnow() -> datetime:
    return datetime.utcnow()


def enabled() -> bool:
    from . import config_service
    return config_service.get_bool(FLAG, False)


def interval_seconds() -> int:
    """How often to evaluate. Ten minutes by default: a boundary is crossed at most twice
    a day, and the cost of being late is a VM running a few minutes longer than intended.

    Floored at 60s — a schedule evaluated more often than that is all overhead, and the
    latch means a faster sweep finds nothing to do anyway.
    """
    from . import config_service
    try:
        minutes = int(config_service.get("vm_suspend_sweep_interval_minutes", "") or 10)
    except (TypeError, ValueError):
        minutes = 10
    return max(60, minutes * 60)


def enqueue_sweep_if_due(db: Session) -> "str | None":
    """Create one sweep job if none is in flight and the interval has elapsed."""
    if not enabled():
        return None
    try:
        from ..database import _is_sqlite
        if not _is_sqlite:
            db.execute(text("SELECT pg_advisory_xact_lock(:i)"), {"i": _ENQUEUE_LOCK_ID})
        existing = (db.query(Job.id)
                    .filter(Job.job_type == SWEEP_JOB_TYPE,
                            Job.status.in_(job_service.ACTIVE_STATUSES))
                    .first())
        if existing:
            return None
        floor = _utcnow() - timedelta(seconds=interval_seconds())
        recent = (db.query(Job.id)
                  .filter(Job.job_type == SWEEP_JOB_TYPE, Job.created_at >= floor)
                  .first())
        if recent:
            return None
        job = job_service.create_job(db, job_type=SWEEP_JOB_TYPE, created_by="system")
        return job.id
    except Exception:
        logger.warning("could not enqueue a suspend sweep", exc_info=True)
        db.rollback()
        return None


def scheduled_vms(db: Session) -> list:
    """Deploy job rows that carry a schedule and are still live."""
    return (db.query(Job)
            .filter(Job.job_type.in_(("ec2_deploy", "gce_deploy")),
                    Job.status == "completed",
                    Job.suspend_at_local.isnot(None))
            .all())


async def run(db: Session, *, job_id: str, meta: dict) -> None:
    """One pass. Reports what it would do whether or not it may act, so an operator can
    see the schedule working before trusting it with anything."""
    job_service.set_running(db, job_id)
    acted, skipped, considered = [], [], 0
    now = datetime.now(timezone.utc)

    try:
        for row in scheduled_vms(db):
            m = row.metadata_dict
            if m.get("destroyed"):
                continue
            considered += 1
            ok, reason = vm_suspend_policy.schedulable(row.job_type, m)
            if not ok:
                skipped.append({"job_id": row.id, "reason": reason})
                continue

            action = suspend_schedule.due_action(row, now)
            # Stamp the latch whichever way it went. A pass that looked and found no
            # boundary has still covered that window; not recording it would make the
            # next pass re-examine a period this one already answered for — and, on the
            # very first pass, would leave the NULL latch NULL forever.
            row.schedule_last_checked_at = now.replace(tzinfo=None)
            if not action:
                continue

            verb = "start" if action == suspend_schedule.RESUME else "stop"
            cloud = vm_suspend_policy.cloud_of(row.job_type)
            child = job_service.create_job(
                db,
                job_type=_POWER_JOB[cloud],
                created_by="system",
                workgroup=row.workgroup,
                metadata=_power_meta(cloud, row, m, verb),
            )
            acted.append({"job_id": row.id, "action": verb, "power_job_id": child.id})
            job_service.append_job_log(
                db, job_id, f"{verb} {m.get('instance_name') or m.get('instance_id')} "
                            f"(job {child.id})")

        db.commit()
        job_service.set_completed(db, job_id, {
            "considered": considered, "acted": acted, "skipped": skipped,
        })
    except Exception as exc:
        logger.error("suspend sweep failed: %s", exc)
        db.rollback()
        job_service.set_failed(db, job_id, str(exc))


def _power_meta(cloud: str, row, meta: dict, verb: str) -> dict:
    """The metadata the power runner expects — the same keys the endpoint persists, so a
    scheduled action and a button press produce identical job rows."""
    if cloud == "aws":
        return {"action": verb, "instance_id": meta.get("instance_id"),
                "region": meta.get("region"), "deploy_job_id": row.id}
    return {"action": verb, "instance_name": meta.get("instance_name"),
            "zone": meta.get("zone"), "project_id": meta.get("project_id"),
            "deploy_job_id": row.id}
