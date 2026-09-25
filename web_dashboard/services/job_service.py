"""
Job management service.
Creates, updates, and queries background job records.
"""
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional, List
from sqlalchemy import and_, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from ..database import Job, AuditLog
# Stdlib-only and pure by design (see its module docstring), so this is safe at import
# time where most sibling services are not — CHECKIN_VERBS below reads its verb lists.
from . import agent_hypervisor_meta, audit_chain, retry_policy

logger = logging.getLogger(__name__)

# Postgres transaction-advisory-lock id that serializes audit-chain appends +
# backfill across Gunicorn/jobs-worker processes (distinct from init_db's
# 20260101). No-op on SQLite, whose whole-DB write lock already serializes.
_AUDIT_LOCK_ID = 20260102

# A job is "active" while its work is still ahead of it. ``queued`` is a child row
# a parent job will drive (see create_job); ``pending`` is waiting for the runner to
# claim it. Neither has started, but neither is finished either.
ACTIVE_STATUSES = ("queued", "pending", "running")

# Recurring internal maintenance passes. What sets these apart from every other job type
# is that they are driven by a timer rather than by a person, they reference no resource,
# and they write a row whether or not there was anything to do — the auto-delete sweep
# alone is 48 rows/day at its 30-minute default, which is enough to push a real deploy off
# the first page of /jobs within hours.
#
# :func:`list_jobs` hides these by default, and ONLY where ``status == "completed"``: a
# failed or cancelled pass is the case an operator needs to see, and it stays visible in
# the dashboard's failed-jobs panel and here alike. ``expiry_reaper.prune_sweep_history``
# deletes exactly the set that is hidden, so "hidden" and "eventually discarded" never
# disagree.
#
# Display and retention only — nothing authorizes off this tuple.
ROUTINE_JOB_TYPES = ("expiry_sweep", "schedule_sweep")

# The same noise, one level down. A job type here is unattended for SOME of its rows and
# operator work for the rest, told apart by the verb in its metadata — so it cannot join
# ROUTINE_JOB_TYPES above, which hides a whole type.
#
# ``agent_hypervisor`` is the case: an ``inventory_sync`` is a timer polling a hypervisor
# every 30 minutes per connection, paged, so ONE 3000-VM vCenter writes several rows per
# pass and a busy estate buries the first page of /jobs faster than the sweeps do. A
# ``power_on`` is the same job type, because it is the same agent handler under the same
# grant — and it is an operator's record of having stopped a production VM. Hiding by
# type would hide both, which is why :data:`~web_dashboard.database.Job.is_checkin` is a
# column and this table is keyed on the verb.
#
# Read at CREATE time only (:func:`create_job` stamps the column). Changing it does not
# reclassify rows already written, which is the honest behaviour: the column records what
# the job was understood to be when it was enqueued.
#
# Display only. Unlike ROUTINE_JOB_TYPES, nothing prunes off this — a check-in row is
# hidden, never deleted, so the history stays complete for anyone who ticks the box.
CHECKIN_VERBS = {
    "agent_hypervisor": agent_hypervisor_meta.READ_VERBS,
}


def is_checkin(job_type: str, metadata: Optional[dict]) -> bool:
    """Is this an unattended check-in, per :data:`CHECKIN_VERBS`?

    Pure, and deliberately conservative: an absent or unrecognised verb is NOT a
    check-in. ``agent_hypervisor_meta.normalize`` falls an unknown verb back to
    ``inventory_sync``, so the opposite default would quietly classify a malformed
    power op as noise and hide it.
    """
    verbs = CHECKIN_VERBS.get(job_type or "")
    if not verbs:
        return False
    return str((metadata or {}).get("verb") or "") in verbs


def claimable_now(now: Optional[datetime] = None) -> list:
    """The time-and-approval clauses BOTH claim queries must apply, in one place.

    There are two claim queries in this tree and they are near-copies of each other:
    ``jobs_worker._claim_one`` (``status='pending'``, the local runner) and
    ``agent_service.lease_one`` (``status='queued' AND agent_id=:id``, a remote agent).
    A config-management run against an on-prem target goes through the SECOND one, so a
    scheduling rule applied to only the first would be silently unenforced for exactly
    the targets a change window matters most for.

    The copies have already drifted once, which is why this returns a list instead of
    being written out twice: ``retry_after`` is honoured by ``_claim_one`` and was never
    added to ``lease_one``, so an agent-bound job that failed transiently is re-leased
    immediately instead of waiting out its backoff. Routing both through here fixes that
    in passing and makes the next clause impossible to add to only one of them.

    Returns SQLAlchemy clauses to splat into a ``.filter(...)``. Each is written so that
    NULL — what every row that predates the scheduler carries — PASSES:

    * ``retry_after``   NULL = never failed, claimable now
    * ``scheduled_for`` NULL = run as soon as there is capacity
    * ``approval_required`` NULL/False = no gate. Spelled ``isnot(True)`` rather than
      ``== False`` because the column was added as a bare BOOLEAN with no DEFAULT (see
      database.py), so existing rows are NULL and ``== False`` would exclude all of them
      — which would wedge the entire queue on the first deploy.
    """
    now = now or datetime.utcnow()
    return [
        (Job.retry_after.is_(None)) | (Job.retry_after <= now),
        (Job.scheduled_for.is_(None)) | (Job.scheduled_for <= now),
        (Job.approval_required.isnot(True)) | (Job.approved_at.isnot(None)),
    ]


def create_job(
    db: Session,
    job_type: str,
    created_by: str,
    vm_path: Optional[str] = None,
    workgroup: Optional[str] = None,
    metadata: Optional[dict] = None,
    batch_id: Optional[str] = None,
    status: str = "pending",
    expires_at: Optional[datetime] = None,
    agent_id: Optional[str] = None,
    scheduled_for: Optional[datetime] = None,
    window_ends_at: Optional[datetime] = None,
    change_window_id: Optional[str] = None,
    job_schedule_id: Optional[str] = None,
    approval_required: bool = False,
) -> Job:
    """Create a new job record, ``pending`` by default.

    ``batch_id`` groups the jobs of one bulk Config-Management run; it is a plain
    label (nothing authorizes off it) that makes the batch filterable on /jobs.

    ``status`` exists for one case: a job row that a *parent* job drives rather than
    the runner. The runner claims on ``status='pending' AND job_type IN
    HANDLED_TYPES`` (jobs_worker._claim_one), so a child created ``pending`` under a
    handled type would be claimed and run a second time, concurrently with its
    parent. Creating such children ``queued`` keeps them out of the claim query while
    still showing on /jobs. Only pass it when something else owns the execution.

    ``agent_id`` assigns the row to an enrolled remote agent and **forces**
    ``status='queued'`` — it is not a request, and a caller cannot opt out. A remote
    agent leases on ``status='queued' AND agent_id=:id``; if such a row were left
    ``pending`` under a type the local runner also handles, both would claim it and it
    would execute twice, once in a network the other cannot even reach. Forcing it in
    this one funnel — the single function every job row passes through — is what makes
    that unforgettable, rather than a rule each new call site has to remember.

    ``expires_at`` is the auto-delete timer. Pass it to override; leave it None and a
    cloud VM deploy is stamped from the global default. This is the ONE funnel every
    VM deploy row passes through — single deploys, count fan-outs and bulk children
    alike — so stamping here means no provider can be forgotten and a fifth cloud is
    covered the day it's added. See expiry_policy.default_expiry_for, which returns
    None on a set-membership test for every job type that isn't a VM deploy.

    ``scheduled_for`` holds the row out of BOTH claim queries until that instant (naive
    UTC), and ``window_ends_at`` is the far edge of its change window: if the job has not
    been claimed by then, ``schedule_sweeper`` marks it missed rather than letting it run
    late. Leave both None — as ~175 call sites do — and the job runs as soon as there is
    capacity, exactly as before. Scheduling is therefore available to EVERY job type the
    moment it passes through here, which is the point of putting it in this funnel rather
    than in one router.

    ``approval_required`` holds the row out of the claim queries a second way, until
    someone with ``change_windows:use`` sets ``approved_at``. Stored on the row rather
    than re-read from policy, so flipping the setting can neither freeze jobs already
    queued nor release changes nobody approved."""
    if agent_id:
        status = "queued"
    job = Job(
        id=str(uuid.uuid4()),
        job_type=job_type,
        vm_path=vm_path,
        workgroup=workgroup,
        status=status,
        progress_pct=0,
        created_at=datetime.utcnow(),
        created_by=created_by,
        batch_id=batch_id or None,
        agent_id=agent_id or None,
        scheduled_for=scheduled_for,
        window_ends_at=window_ends_at,
        change_window_id=change_window_id or None,
        job_schedule_id=job_schedule_id or None,
        approval_required=bool(approval_required) or None,
        # Derived here, never passed in, for the reason agent_id is forced here: this
        # is the one function every job row passes through, so a new caller that
        # enqueues an inventory_sync cannot forget to classify it and quietly start
        # spamming /jobs again.
        #
        # Three-valued on purpose, and the third value is what makes the backfill
        # terminate: True/False mean "classified", NULL means "a type that is never a
        # check-in, so nobody looked". If a power op were written NULL too,
        # :func:`backfill_job_checkins` could not tell it from a row that predates the
        # column and would re-scan every one of them on every boot, forever.
        is_checkin=(is_checkin(job_type, metadata)
                    if (job_type or "") in CHECKIN_VERBS else None),
    )
    if metadata:
        job.metadata_dict = metadata
    if expires_at is not None:
        job.expires_at = expires_at
    else:
        # Imported here, not at module top: expiry_policy reads config_service, and a
        # top-level import would make job_service depend on it at import time.
        from . import expiry_policy
        try:
            # The auto-delete clock starts when the VM will EXIST, not when the row was
            # written. For an immediate job those are the same instant and this passes
            # None, which is what default_expiry_for already means by "now". For a
            # SCHEDULED deploy they are not: a VM booked for next Saturday under a 24h
            # default would otherwise be stamped to expire on Sunday of THIS week — born
            # already expired, and reaped by the first sweep after it finally deployed.
            job.expires_at = expiry_policy.default_expiry_for(
                job_type, workgroup=workgroup, now=scheduled_for)
        except Exception:  # noqa: BLE001 — a timer must never block a deploy
            job.expires_at = None
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def set_cloud_resource_id(db: Session, job_id: str, resource_id: str) -> Optional[Job]:
    """Record the cloud SDK resource id (EC2 instance id, Azure VM name, GCP
    instance id) on a Job so the reassign endpoints can find this Job when an
    admin rewrites the resource's Workgroup tag/label."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if job:
        job.cloud_resource_id = resource_id
        db.commit()
    return job


def set_running(db: Session, job_id: str) -> Optional[Job]:
    """Mark a job as running.

    Deliberately does NOT ``db.refresh(job)`` after the commit, unlike the terminal
    setters below. This one is called at the START of a long job, on the session the job
    then holds for its whole duration — and a refresh is a SELECT, which opens a
    transaction that nothing closes until that session's next commit. For a provision
    that is after the entire terraform apply, so the row read here pinned a PostgreSQL
    backend ``idle in transaction`` for 30+ minutes: it blocks autovacuum on `jobs` and
    `job_logs`, the two highest-churn tables, and if the server ever sets
    ``idle_in_transaction_session_timeout`` the session is killed and the post-apply
    commit fails *after a successful apply*.

    Nothing reads the return value (all 56 call sites discard it), and with
    ``expire_on_commit`` at its default the refresh could not have kept the instance
    usable anyway — the next attribute access would re-SELECT regardless. The terminal
    setters keep their refresh because ``set_failed`` genuinely uses the refreshed row
    (``notify_job_failed``) and because by then the session is about to be closed.
    """
    job = db.query(Job).filter(Job.id == job_id).first()
    if job:
        job.status = "running"
        job.started_at = datetime.utcnow()
        job.updated_at = datetime.utcnow()
        db.commit()
    return job


def update_progress(db: Session, job_id: str, pct: int, message: str) -> Optional[Job]:
    """Update progress percentage and message for a running job."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if job:
        job.progress_pct = pct
        job.progress_message = message
        job.updated_at = datetime.utcnow()
        db.commit()
    return job


def set_completed(db: Session, job_id: str, result: Optional[dict] = None) -> Optional[Job]:
    """Mark a job as completed with optional result metadata."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if job:
        job.status = "completed"
        job.progress_pct = 100
        job.completed_at = datetime.utcnow()
        job.updated_at = datetime.utcnow()
        if result:
            existing = job.metadata_dict
            existing.update(result)
            job.metadata_dict = existing
        db.commit()
        db.refresh(job)
    return job


def _retry_enabled() -> bool:
    """Whether transient failures are requeued. **Never raises.**

    This is read from inside ``set_failed``, which is the path a job takes when something
    has already gone wrong — and the config store is backed by the same database that may
    well be the thing going wrong. A config read that throws here would throw out of
    ``set_failed`` itself and leave the row stuck in ``running`` forever, which is
    strictly worse than the failure it was trying to record.

    So an unreadable flag means "off", which degrades to exactly the behaviour this
    codebase had before retry existed.
    """
    try:
        from . import config_service
        from ..config import settings
        return config_service.get_bool("job_retry_enabled",
                                       getattr(settings, "job_retry_enabled", False))
    except Exception:  # noqa: BLE001 — see the docstring; off is the safe answer
        return False


def _retry_limit():
    """The configured attempt ceiling, or None for the default. Never raises, for the
    same reason as :func:`_retry_enabled` — ``retry_policy.max_attempts`` treats None as
    "use the default"."""
    try:
        from . import config_service
        from ..config import settings
        return (config_service.get("job_retry_max_attempts")
                or getattr(settings, "job_retry_max_attempts", None))
    except Exception:  # noqa: BLE001
        return None


def _raise_dead_letter(db: Session, job) -> None:
    """Announce a job that has used every attempt.

    **The error text is deliberately not in here.** ``emit_safe`` queues an outbox row the
    worker drains to a webhook — Slack, Teams, or an arbitrary HTTP endpoint — and job
    errors in this codebase routinely echo request parameters from runners that handle SSH
    keys, deploy keys, PRA client secrets and generated Azure admin passwords. Pushing that
    to a third party buys nothing: the error is already on the row, already rendered on the
    job page, and that page is one click away through the URL below.

    (The neighbouring ``notify_job_failed`` does still send ``error_message[:1000]``. That
    predates this and operators may triage from it, so changing it is its own decision
    rather than something to smuggle in here.)

    **Called AFTER the caller has committed.** ``emit_safe`` rolls the session back when a
    notification fails — see its docstring, which states that every emit site sits after
    the commit of the thing it reports. Called before, a broken webhook would roll back the
    very writes marking this job failed and leave it ``running`` forever.
    """
    try:
        from . import notification_service, notify_policy
        label = job.vm_path or job.cloud_resource_id or job.job_type
        notification_service.emit_safe(db, notify_policy.NotificationEvent(
            event_type="job.dead_lettered",
            title=f"{job.job_type} failed after {job.attempts + 1} attempts — {label}",
            body=("Every retry for this job has been used and the failure persists. "
                  "Open the job to see the error."),
            # The routing fields notify_job_failed sets, so a dead letter reaches the same
            # subscribers a first failure does rather than only the unscoped ones.
            resource_id=f"job:{job.id}",
            resource_kind="job",
            resource_name=label or job.job_type,
            workgroup=job.workgroup or "",
            url=f"/jobs/{job.id}",
            dedupe_bucket=f"job.dead_lettered:{job.id}",
            fields={"Job type": job.job_type, "Attempts": job.attempts + 1,
                    "Started by": job.created_by},
        ))
    except Exception:  # noqa: BLE001
        logger.info("could not raise job.dead_lettered for %s", job.id, exc_info=True)


def set_failed(db: Session, job_id: str, error: str,
               result: Optional[dict] = None) -> Optional[Job]:
    """Mark a job as failed with an error message and optional result metadata.

    ``result`` merges into the row's metadata exactly as ``set_completed``'s does. A run
    that got partway can have collected detail worth keeping — see
    ``ps_k8s_token_service.run``, where the non-fatal steps' warnings carry the remedy —
    and without this the only durable home for any of it was ``error``. Both still
    matter: ``error_message`` is the one field the job page renders, this is where the
    same thing survives as structure rather than prose.

    **A caller reaching here from `except` may hand us a POISONED session.** The thing
    that failed the job is quite often the thing that aborted its transaction — a
    deadlock, a lost connection, a constraint violation mid-flush — and on PostgreSQL
    every later statement on that session then raises ``InFailedSqlTransaction``
    ("current transaction is aborted, commands ignored until end of transaction block").
    Which means the one function whose entire job is to record why a run died is the one
    that cannot run when the death was a database error, and the real cause survives only
    in the application log.
    """
    # Recover such a session rather than dying on it — see the docstring. Nothing is lost
    # by the rollback: PostgreSQL has ALREADY discarded every write in an aborted
    # transaction, so the only choice left is whether this function gets to add one. On a
    # healthy session the first read succeeds and the except is never entered.
    #
    # Seen live 2026-09-25 on the 26.10.20 upgrade: `init_db`'s ALTER TABLE on `jobs`
    # deadlocked against a mid-flight `expiry_sweep`, PostgreSQL chose the sweep as the
    # victim, and `expiry_reaper.run` caught that and called straight in here on the same
    # session. The InFailedSqlTransaction escaped a service whose docstring says it never
    # raises, and the job was left to `jobs_worker._fail_backstop`, whose fresh session
    # did mark it failed — but with "current transaction is aborted" as the error message
    # instead of "deadlock detected", pointing at nothing.
    try:
        job = db.query(Job).filter(Job.id == job_id).first()
    except SQLAlchemyError:
        db.rollback()
        job = db.query(Job).filter(Job.id == job_id).first()
    if job:
        now = datetime.utcnow()
        job.error_message = error
        job.updated_at = now
        if result:
            existing = job.metadata_dict
            existing.update(result)
            job.metadata_dict = existing

        # The one hook. Every one of the 80-odd runners funnels through here, so retry
        # needs no change in any of them — the same way `log_audit` grew an ip_address
        # default without its 75 call sites knowing. `retry_policy` decides; see there
        # for why a deploy is never on the retryable list.
        retrying = _retry_enabled()
        dead_lettered = False
        if retrying and retry_policy.should_retry(
                job.job_type, error, job.attempts, limit=_retry_limit()):
            job.attempts = (job.attempts or 0) + 1
            job.retry_after = now + timedelta(
                seconds=retry_policy.backoff_seconds(job.attempts - 1))
            # Back to `pending`, NOT to a new status — `_claim_one` will pick it up once
            # `retry_after` passes. `completed_at` is deliberately left alone: the run has
            # not completed, and stamping it would make a requeued job read as finished to
            # every duration and staleness check in the tree.
            job.status = "pending"
            job.started_at = None
            # The error text is deliberately NOT in this line, for the same reason
            # `_raise_dead_letter` leaves it out of the notification: it is the one field
            # that carries whatever a runner echoed back, and application logs are shipped
            # to aggregators whose readers are a different set of people from those with
            # access to this database. Before this, nothing in this module or in
            # `jobs_worker` had ever logged it. What is here is the decision — which job,
            # which type, which attempt, how long until the next one — and `error_message`
            # is on the row, rendered on /jobs/{id}, for the rest.
            logger.info("job %s (%s) failed transiently, attempt %d — requeued in %ds",
                        job_id, job.job_type, job.attempts,
                        retry_policy.backoff_seconds(job.attempts - 1))
        else:
            job.status = "failed"
            job.completed_at = now
            # `retrying` gates the read as well as the write. With the flag off this
            # function does not touch `attempts` at all, so its behaviour is byte-identical
            # to what it was before retry existed — which is what "off by default" has to
            # mean on a path every one of the 80-odd runners funnels through. It is also
            # true on its own terms: with no retries, there is no such thing as an
            # exhausted job to dead-letter.
            # Exhausted rather than failed first time: the dead-letter tail is exactly this
            # shape — `failed` with attempts spent — so nothing new has to be taught to any
            # page. The ANNOUNCEMENT happens after the commit below, not here.
            dead_lettered = retrying and (job.attempts or 0) > 0
        db.commit()
        db.refresh(job)
        # After the commit, and that ordering is load-bearing rather than tidy: emit_safe
        # rolls the session back when a notification fails, so announcing before the commit
        # would let a broken webhook discard the status/error/completed_at writes above and
        # leave this job `running` forever. emit_safe's own docstring states the rule.
        if dead_lettered:
            _raise_dead_letter(db, job)
        # After the commit, always: the notification is a report on a transition that
        # has already happened, so nothing it does can undo one. Hooked here rather than
        # at the ~221 call sites that reach this function.
        notify_job_failed(db, job)
    return job


def notify_job_failed(db: Session, job) -> None:
    """Queue a ``job.failed`` notification. INSERT-only, and cannot raise.

    Deliberately does no network I/O — it writes an outbox row and returns, and the
    worker's drain loop does the sending. That is the strongest available form of
    "a notification can never fail a job", and ``tests/test_notify_wiring.py`` asserts
    it statically rather than trusting the convention to hold.
    """
    try:
        if job is None:
            return
        from . import notification_service, notify_policy
        label = job.vm_path or job.cloud_resource_id or job.job_type
        event = notify_policy.NotificationEvent(
            event_type="job.failed",
            title=f"{job.job_type} failed — {label}",
            body=(job.error_message or "No error message was recorded.")[:1000],
            resource_id=f"job:{job.id}",
            resource_kind="job",
            resource_name=label or job.job_type,
            workgroup=job.workgroup or "",
            url=f"/jobs/{job.id}",
            fields={"Job type": job.job_type, "Started by": job.created_by},
        )
        notification_service.emit_safe(db, event)
    except Exception:                                  # noqa: BLE001
        logger.warning("could not queue a job.failed notification for %s",
                       getattr(job, "id", "?"), exc_info=True)
        try:
            db.rollback()
        except Exception:                              # pragma: no cover - defensive
            pass


def finish_batch_parent(db: Session, parent_job_id: str, child_job_ids: list) -> Optional[Job]:
    """Close out a ``*_bulk_deploy`` parent once its children have all been driven.

    A parent exists only to drive its children, so nothing ever gave it a terminal
    status. The worker sets a job ``running`` when it claims it and only marks it
    failed if dispatch *raises* — a batch that finishes normally therefore left its
    parent stuck at ``running`` 0% until ``reconcile_stale_jobs`` eventually failed a
    batch that had actually succeeded.

    The parent's outcome is the children's: completed unless every child failed, since
    a parent that ran to the end did its job even when an individual VM did not. The
    per-child counts land on the parent's metadata so the row is worth reading rather
    than just being unstuck.
    """
    children = db.query(Job).filter(Job.id.in_(child_job_ids)).all() if child_job_ids else []
    counts = {}
    for child in children:
        counts[child.status] = counts.get(child.status, 0) + 1
    total = len(children)
    failed = counts.get("failed", 0)
    summary = {"batch_total": total, "batch_failed": failed,
               "batch_completed": counts.get("completed", 0)}

    if total and failed == total:
        job = db.query(Job).filter(Job.id == parent_job_id).first()
        if job:
            meta = job.metadata_dict
            meta.update(summary)
            job.metadata_dict = meta
            db.commit()
        return set_failed(db, parent_job_id,
                          f"All {total} instance(s) in the batch failed.")
    return set_completed(db, parent_job_id, summary)


def update_metadata(db: Session, job_id: str, data: dict) -> Optional[Job]:
    """Merge `data` into the job's existing metadata without changing status."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if job:
        existing = job.metadata_dict
        existing.update(data)
        job.metadata_dict = existing
        db.commit()
        db.refresh(job)
    return job


def set_cancelled(db: Session, job_id: str) -> Optional[Job]:
    """Mark a job as cancelled.

    ``queued`` is cancellable for the same reason ``pending`` is: the work has not
    started. It was omitted while the only queued rows were bulk children nobody
    cancels individually, but a job waiting on a remote agent that is offline is
    exactly the row an operator reaches for the Cancel button on.
    """
    job = db.query(Job).filter(Job.id == job_id).first()
    if job and job.status in ("queued", "pending", "running"):
        job.status = "cancelled"
        job.completed_at = datetime.utcnow()
        db.commit()
        db.refresh(job)
    return job


def set_missed_window(db: Session, job_id: str, reason: str) -> Optional[Job]:
    """Terminate a job whose change window closed before it was ever claimed.

    ``cancelled``, not ``failed``, and the distinction is not cosmetic. Nothing went
    wrong — the job never ran — so ``failed`` would put a perfectly healthy change into
    the failed-jobs panel on the dashboard AND into the dead-letter tail, which is the
    query ``status='failed' AND attempts > 0`` and means "used every retry and failed
    anyway". Neither is true here. ``cancelled`` already reads as "did not happen, on
    purpose", is already terminal, and is already rendered on every page.

    ``missed_window_at`` is what separates this from a human pressing Cancel; the reason
    goes in ``error_message`` because that is the one field the job detail page already
    surfaces for a non-successful job.

    Only ever applied to a row that has NOT started. The window governs when work may
    BEGIN; a job already running when its window closes is left alone to finish, because
    interrupting a terraform apply or a half-applied playbook is how you get orphaned
    cloud resources and a host in an unknown state. That overrun is recorded and shown,
    not acted on -- see ``schedule_state``.
    """
    job = db.query(Job).filter(Job.id == job_id).first()
    if job and job.status in ("queued", "pending"):
        now = datetime.utcnow()
        job.status = "cancelled"
        job.missed_window_at = now
        job.completed_at = now
        job.error_message = reason
        db.commit()
        db.refresh(job)
    return job


def schedule_state(job) -> str:
    """How a job's scheduling stands, as ONE string both job pages render from.

    Derived here rather than in each template because the list and the detail page would
    otherwise each reimplement "is this scheduled or just pending?", and the two would
    disagree the first time one of them was edited. Returns "" for the overwhelming
    majority of rows, which carry no schedule at all.

    Note the deliberate ordering: ``missed`` is checked before ``scheduled``, because a
    missed job still has a ``scheduled_for`` in the past and would otherwise read as
    merely waiting.
    """
    if getattr(job, "missed_window_at", None):
        return "missed"
    if job.status in ("queued", "pending"):
        if getattr(job, "approval_required", None) and not getattr(job, "approved_at", None):
            return "awaiting_approval"
        scheduled_for = getattr(job, "scheduled_for", None)
        if scheduled_for and scheduled_for > datetime.utcnow():
            return "scheduled"
        return ""
    ends = getattr(job, "window_ends_at", None)
    if ends and job.completed_at and job.completed_at > ends:
        return "overran"
    return ""


def backfill_job_checkins(db: Session, batch: int = 200) -> int:
    """One-time: classify pre-existing rows of the :data:`CHECKIN_VERBS` job types.

    Returns the number of rows written. Without this the feature does nothing on an
    existing install for months: :func:`list_jobs` hides on the ``is_checkin`` column,
    every row that predates the column reads NULL, and NULL means "show it" — so the
    thousands of inventory syncs already on /jobs, which are the entire reason the
    filter exists, would stay exactly where they are.

    Convergent and idempotent, deriving that from the data rather than from a marker
    row: it only ever writes rows where the column IS NULL, it writes every row it
    reads (``False`` as readily as ``True``), and the value is a pure function of
    metadata that does not change. Two processes racing compute the same answer, so it
    takes no advisory lock — the one thing that could reintroduce the init_db deadlock.

    Batched, in SMALL batches, because the candidate set is unbounded in a way the other
    backfills in this tree are not — and so is each row. It is every hypervisor job ever
    written (four connections syncing every 30 minutes for a year is five figures), and
    the verb this has to read shares ``extra_data`` with the inventory page the agent
    handed back, which ``agent_service.MAX_RESULT_BYTES`` caps at 256 KB *each*. A
    thousand-row batch is therefore a quarter of a gigabyte of JSON at startup on a
    container sized for none of it. 200 bounds the peak and the rows are freed between
    passes; an interrupted run costs nothing, because the next boot picks up exactly the
    rows it did not reach.

    Reads two columns and writes with a bulk UPDATE rather than loading ORM rows, for
    the same reason: the identity map would hold every page of every sync it touched
    until the session closed.
    """
    types = tuple(CHECKIN_VERBS)
    if not types:
        return 0
    written = 0
    while True:
        rows = (db.query(Job.id, Job.job_type, Job.extra_data)
                  .filter(Job.job_type.in_(types), Job.is_checkin.is_(None))
                  .limit(batch).all())
        if not rows:
            break
        verdicts = {True: [], False: []}
        for job_id, job_type, extra in rows:
            try:
                meta = json.loads(extra) if extra else {}
            except (TypeError, ValueError):
                meta = {}          # unparseable metadata is not a check-in, per is_checkin
            verdicts[is_checkin(job_type, meta if isinstance(meta, dict) else {})
                     ].append(job_id)
        for verdict, ids in verdicts.items():
            if ids:
                (db.query(Job).filter(Job.id.in_(ids))
                   .update({Job.is_checkin: verdict}, synchronize_session=False))
        db.commit()
        written += len(rows)
        # Every row above was written non-NULL, so the next pass cannot return the same
        # ones and this terminates. Belt and braces: a short page is the last page, and
        # bailing on one means a column that silently refused a write can't spin here.
        if len(rows) < batch:
            break
    if written:
        logger.info("job is_checkin backfill: classified %s pre-existing row(s)", written)
    return written


def get_job(db: Session, job_id: str) -> Optional[Job]:
    """Fetch a single job by ID."""
    return db.query(Job).filter(Job.id == job_id).first()


def list_jobs(
    db: Session,
    page: int = 1,
    page_size: int = 20,
    status: Optional[str] = None,
    created_by: Optional[str] = None,
    workgroup: Optional[str] = None,
    batch_id: Optional[str] = None,
    include_routine: bool = True,
    include_checkins: bool = True,
    dead_lettered: bool = False,
    scheduled: bool = False,
) -> tuple[List[Job], int]:
    """
    List jobs with optional filters.
    Returns (jobs, total_count).

    ``include_routine=False`` drops the completed rows of :data:`ROUTINE_JOB_TYPES` — the
    timer-driven maintenance passes that would otherwise bury every operator-initiated job.
    It excludes only ``completed`` ones, so a failed sweep still surfaces. Defaults to True
    so this stays a display choice made by the caller that renders a list, not a filter
    silently applied to every count in the app.

    ``include_checkins=False`` is the same idea one level down, for the unattended rows
    of a job type whose OTHER rows are operator work — today the hypervisor
    ``inventory_sync`` (see :data:`CHECKIN_VERBS`). It reads the ``is_checkin`` column
    rather than the type, so a ``power_on`` sharing that type is never hidden, and it
    excludes only ``completed`` ones for the same reason routine does: a sync that
    FAILED is a connection an operator needs to look at. Defaults to True, again so the
    hiding is a choice made by the caller rendering a list.

    The two flags are independent on purpose. They are different noise with different
    fixes — sweeps are the dashboard's own housekeeping, check-ins are a cadence an
    operator sets per connection — and someone diagnosing a stale inventory wants the
    syncs without 48 rows/day of expiry sweeps on top.

    ``dead_lettered=True`` narrows to jobs that used every retry and failed anyway. That is
    a QUERY, not a status: ``failed`` with ``attempts > 0``. Adding a fourth status would
    have meant auditing 108 comparisons against ``"failed"`` in this tree, and the first one
    missed is a job some page quietly stops showing.

    ``scheduled=True`` is the change-window queue: jobs that have not started and are
    waiting on a clock or on a person. A QUERY again, for the same reason — a scheduled
    job is an ordinary ``pending`` row, so it must keep appearing under its real status
    everywhere else. Note this deliberately includes a job whose ``scheduled_for`` has
    already passed but which has not been claimed yet: from an operator's point of view
    it is still a booked change, and hiding it the moment its window opened would make
    it vanish for the seconds before a worker picks it up.
    """
    query = db.query(Job)
    if dead_lettered:
        query = query.filter(Job.status == "failed", Job.attempts > 0)
    if scheduled:
        query = query.filter(
            Job.status.in_(("queued", "pending")),
            (Job.scheduled_for.isnot(None)) | (Job.approval_required.is_(True)))
    if not include_routine:
        query = query.filter(~and_(Job.job_type.in_(ROUTINE_JOB_TYPES),
                                   Job.status == "completed"))
    if not include_checkins:
        # `is_(True)` rather than `== True`, so the NULL every row that predates the
        # column carries passes — the same reason claimable_now spells its clauses that
        # way. A NULL here means "was never classified", which must read as "show it".
        query = query.filter(~and_(Job.is_checkin.is_(True),
                                   Job.status == "completed"))
    if status:
        # Accept a comma-separated list (e.g. "pending,running") so a single
        # count call can span multiple statuses; a single value still works.
        query = query.filter(Job.status.in_([s for s in status.split(",") if s]))
    if created_by:
        query = query.filter(Job.created_by == created_by)
    if workgroup:
        query = query.filter(Job.workgroup == workgroup)
    if batch_id:
        query = query.filter(Job.batch_id == batch_id)

    total = query.count()
    jobs = (
        query.order_by(Job.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return jobs, total


# Statuses a batch rollup always reports, so the UI can render a stable set of
# counters instead of columns appearing and disappearing as a batch progresses.
BATCH_STATUSES = ("pending", "running", "completed", "failed", "cancelled")


def _summarize_statuses(rows) -> dict:
    """Shape ``[(status, count), …]`` into ``{"total": n, "by_status": {…}}``.

    Every status in :data:`BATCH_STATUSES` is present with an explicit zero — a
    missing key and a zero mean the same thing to a reader but not to a template,
    and "0 failed" is the answer an operator most wants to see. A status outside the
    set is carried through rather than dropped, so an unexpected value shows up in
    the UI instead of silently vanishing from the total.

    Pure, so the rollup arithmetic is testable without a database.
    """
    by_status = {s: 0 for s in BATCH_STATUSES}
    for status, count in (rows or []):
        by_status[status or "unknown"] = by_status.get(status or "unknown", 0) + int(count)
    return {"total": sum(by_status.values()), "by_status": by_status}


def batch_summary(db: Session, batch_id: str, created_by: Optional[str] = None) -> dict:
    """Status rollup for one bulk-run batch: ``{batch_id, total, by_status}``.

    ``created_by`` scopes the counts exactly as :func:`list_jobs` does — the caller
    passes it for a user who may only see their own jobs, so the rollup can never
    reveal the existence of jobs that user cannot read."""
    from sqlalchemy import func

    query = db.query(Job.status, func.count(Job.id)).filter(Job.batch_id == batch_id)
    if created_by:
        query = query.filter(Job.created_by == created_by)
    summary = _summarize_statuses(query.group_by(Job.status).all())
    return {"batch_id": batch_id, **summary}


def has_active_job_for_vm(db: Session, vmx_path: str) -> bool:
    """Return True if a queued/pending/running job already targets this VM.

    ``queued`` counts as active: the row is waiting on a parent job rather than the
    runner, but the work is still coming."""
    count = (
        db.query(Job)
        .filter(Job.vm_path == vmx_path, Job.status.in_(ACTIVE_STATUSES))
        .count()
    )
    return count > 0


def _audit_lock(db: Session) -> None:
    """Serialize audit-chain appends/backfill across processes. Transaction-scoped
    pg advisory lock on PostgreSQL (released on this txn's commit/rollback); a
    no-op on SQLite, which already serializes writers at the DB level."""
    from ..database import _is_sqlite
    if not _is_sqlite:
        db.execute(text("SELECT pg_advisory_xact_lock(:i)"), {"i": _AUDIT_LOCK_ID})


def log_audit(
    db: Session,
    username: str,
    action: str,
    ip_address: Optional[str] = None,
    target_vm: Optional[str] = None,
    details: Optional[dict] = None,
):
    """Append a hash-chained entry to the audit log.

    Each entry links to its predecessor (``prev_hash``/``entry_hash``) so tampering
    is detectable via :func:`verify_audit_chain` (exposed at ``/api/audit/verify``).
    Appends are serialized with :func:`_audit_lock` so concurrent workers can't fork
    the chain; the unique ``seq`` index is the backstop, and a brief retry absorbs
    the rare SQLite write race. Callers are unchanged from the pre-chain signature.

    ``ip_address`` defaults to the current request's client address rather than being
    threaded through ~75 call sites, most of which are services with no ``Request`` in
    scope. It is empty in the job worker, which has no client — a job's actor is
    recorded as ``created_by`` instead. The address is inside the hash (chain V2), so
    it cannot be altered after the fact without breaking verification.
    """
    if ip_address is None:
        from ..logging_context import get_client_ip
        ip_address = get_client_ip() or None
    for attempt in range(3):
        try:
            _audit_lock(db)
            last = (
                db.query(AuditLog.seq, AuditLog.entry_hash)
                .filter(AuditLog.seq.isnot(None))
                .order_by(AuditLog.seq.desc())
                .first()
            )
            seq = (last[0] + 1) if last else 1
            prev_hash = last[1] if last else audit_chain.GENESIS_PREV
            entry = AuditLog(
                id=str(uuid.uuid4()),
                timestamp=datetime.utcnow(),
                username=username,
                action=action,
                target_vm=target_vm,
                ip_address=ip_address,
                seq=seq,
                prev_hash=prev_hash,
            )
            if details:
                entry.details_dict = details
            # Hash the STORED details string (set above), so verify recomputes it
            # from the same value without re-serializing.
            entry.entry_hash = audit_chain.compute_entry_hash(
                seq, entry.timestamp, username, action, target_vm, entry.details,
                prev_hash, ip_address
            )
            db.add(entry)
            db.commit()
            return
        except IntegrityError:
            # A concurrent writer took this seq first (SQLite race). Roll back and
            # retry against the new tip; on the last attempt, surface the error.
            db.rollback()
            if attempt == 2:
                raise


# -- Terraform state-lock safety ----------------------------------------------
# An operator force-unlock (api/jobs.force_unlock_job_state) must never break a lock
# something is still using. Mapping a state key back to "the job holding it" is not
# generically possible -- a decommission destroys in the PROVISION job's deploy dir, so
# the holder is not the job whose id names the state key, and any guard built on "is
# the owning job terminal?" would happily yank a lock out from under a live destroy.
#
# The lock's own timestamp answers it without that mapping:
#
#     a lock created at T can only be held by something already running at T.
#
# So a job that STARTED AFTER the lock was created provably is not its holder. The
# inference errs only toward naming a blocker that turns out to be innocent (some
# unrelated long-running job), never toward calling a live holder safe -- the
# direction that matters when being wrong corrupts someone's state.
#
# Out of scope by construction: the PRA / Entitle / Password-Safe services shell out
# to their own terraform in a tempdir, so they never touch a `terraform-state/<job_id>`
# key and can hold no lock this reasoning applies to.


def _as_utc(value) -> Optional[datetime]:
    """Job timestamps are naive UTC (``datetime.utcnow``); lock stamps are aware."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def lock_blocking_jobs(jobs, lock_created: datetime) -> list:
    """Of ``jobs``, those that could still be holding a lock created at
    ``lock_created``. Pure, so the reasoning above is testable on its own.

    A running job with no ``started_at`` counts as a blocker: it cannot be ruled out,
    and "cannot be ruled out" must never read as safe here."""
    blockers = []
    for job in jobs:
        # `pending`/`queued` are excluded: they have not run terraform yet, so they
        # cannot hold a lock that already exists. If one starts and locks after we
        # break this one, that is a NEW lock and no business of ours.
        if job.status != "running":
            continue
        # An unreadable lock stamp rules nothing out, so every running job stays a
        # blocker rather than the age check silently passing them all.
        if lock_created is None:
            blockers.append(job)
            continue
        started = _as_utc(job.started_at)
        if started is None or started <= lock_created:
            blockers.append(job)
    return blockers


def terraform_lock_blockers(db: Session, lock_created: datetime) -> list:
    """:func:`lock_blocking_jobs` over every currently-running job."""
    return lock_blocking_jobs(
        db.query(Job).filter(Job.status == "running").all(), lock_created)


def verify_audit_chain(db: Session) -> dict:
    """Recompute the whole audit chain and report integrity.

    Returns ``{"ok": bool, "count": int, "first_broken_seq": int | None}``.
    ``ok`` is False (with the offending ``seq``) if any row was edited, deleted,
    or reordered since it was written."""
    # Streamed, not `.all()`. `audit_log` has no retention and cannot have one —
    # pruning any row breaks the chain by construction — so this table only grows, and
    # the check that proves it is intact must not need it all in memory at once.
    q = (
        db.query(AuditLog)
        .filter(AuditLog.seq.isnot(None))
        .order_by(AuditLog.seq.asc())
        .yield_per(1000)
    )
    count = 0

    def _counted():
        nonlocal count
        for row in q:
            count += 1
            yield row

    ok, broken = audit_chain.verify_chain(_counted())
    return {"ok": ok, "count": count, "first_broken_seq": broken}


def backfill_audit_chain(db: Session) -> int:
    """One-time: assign ``seq`` + chain hashes to pre-existing unchained rows.

    Runs only when no row is chained yet (a fresh upgrade); returns the number of
    rows chained (0 if already done / empty). Advisory-locked so it can't race the
    first appends or a second init_db caller. Orders history by ``(timestamp, id)``."""
    _audit_lock(db)
    if db.query(AuditLog.id).filter(AuditLog.seq.isnot(None)).first():
        db.commit()  # release the advisory lock; nothing to do
        return 0
    rows = (
        db.query(AuditLog)
        .order_by(AuditLog.timestamp.asc(), AuditLog.id.asc())
        .all()
    )
    prev = audit_chain.GENESIS_PREV
    for i, e in enumerate(rows, start=1):
        e.seq = i
        e.prev_hash = prev
        e.entry_hash = audit_chain.compute_entry_hash(
            i, e.timestamp, e.username, e.action, e.target_vm, e.details, prev,
            e.ip_address
        )
        prev = e.entry_hash
    db.commit()
    return len(rows)


_CHAIN_VERSION_KEY = "audit_chain_version"


def rechain_audit_log(db: Session) -> dict:
    """One-time: move an existing chain from the V1 hash form to V2 (which covers
    ``ip_address``). Returns ``{"status": …}``; never raises.

    **Verify before rewriting.** Recomputing every hash is exactly what an attacker who
    had edited a row would want us to do — the new chain would be internally consistent
    with the altered content and the tampering would become undetectable. So the old
    chain is verified under V1 first, and a table that does not verify is left exactly as
    it is, with the break reported. Re-blessing it is the one thing this must not do.

    A chain that fails V1 but passes V2 is a marker that went missing (a restored
    database, a rolled-back image); that is recorded, not rewritten.
    """
    from . import config_service
    _audit_lock(db)
    try:
        try:
            # int, not string: "10" >= "2" is False as strings, and this key is the
            # only thing standing between an upgrade and a needless full re-hash.
            stored = int(config_service.get(_CHAIN_VERSION_KEY, "") or 0)
        except (TypeError, ValueError):
            stored = 0
        if stored >= audit_chain.CHAIN_VERSION:
            db.commit()
            return {"status": "current"}

        rows = (db.query(AuditLog)
                .filter(AuditLog.seq.isnot(None))
                .order_by(AuditLog.seq.asc())
                .all())
        if not rows:
            db.commit()
            config_service.set(_CHAIN_VERSION_KEY, str(audit_chain.CHAIN_VERSION))
            return {"status": "empty"}

        ok, broken = audit_chain.verify_chain_v1(rows)
        if not ok:
            # Already migrated, marker lost? Then there is nothing to do and nothing wrong.
            ok_v2, _ = audit_chain.verify_chain(rows)
            db.commit()
            if ok_v2:
                config_service.set(_CHAIN_VERSION_KEY, str(audit_chain.CHAIN_VERSION))
                return {"status": "current"}
            return {"status": "refused", "first_broken_seq": broken, "count": len(rows)}

        prev = audit_chain.GENESIS_PREV
        for e in rows:
            e.prev_hash = prev
            e.entry_hash = audit_chain.compute_entry_hash(
                e.seq, e.timestamp, e.username, e.action, e.target_vm, e.details, prev,
                e.ip_address)
            prev = e.entry_hash
        db.commit()
        config_service.set(_CHAIN_VERSION_KEY, str(audit_chain.CHAIN_VERSION))
        return {"status": "rechained", "count": len(rows)}
    except Exception:
        db.rollback()
        raise


def append_job_log(db: Session, job_id: str, line: str) -> None:
    """Append one Live Output line for a job with the next per-job seq. Best-effort:
    a logging hiccup must never abort a terraform run (mirrors terraform._stream)."""
    from ..database import JobLog
    try:
        last = (
            db.query(JobLog.seq)
            .filter(JobLog.job_id == job_id)
            .order_by(JobLog.seq.desc())
            .first()
        )
        nxt = (last[0] + 1) if last else 1
        db.add(JobLog(job_id=job_id, seq=nxt, line=line, created_at=datetime.utcnow()))
        db.commit()
    except Exception:
        db.rollback()


def get_job_logs(db: Session, job_id: str, after_seq: int = 0) -> List[tuple]:
    """Return ``[(seq, line), …]`` for a job with ``seq > after_seq``, oldest first —
    the WS endpoint replays these and tails new ones so Live Output survives the
    runner being a separate process (and survives client reconnects)."""
    from ..database import JobLog
    rows = (
        db.query(JobLog.seq, JobLog.line)
        .filter(JobLog.job_id == job_id, JobLog.seq > after_seq)
        .order_by(JobLog.seq.asc())
        .all()
    )
    return [(r[0], r[1]) for r in rows]


def is_cancelled(db: Session, job_id: str) -> bool:
    """True if the job's status was flipped to ``cancelled`` — the cooperative-cancel
    signal an in-flight terraform stream polls for."""
    row = db.query(Job.status).filter(Job.id == job_id).first()
    return bool(row and row[0] == "cancelled")


def cancel_check(job_id: str, state: dict, interval_s: float = 5.0) -> None:
    """Raise ``terraform.JobCancelled`` if the job was cancelled, throttled to at most
    once per ``interval_s`` (a cheap status-only query). Called from the per-line
    ``on_line`` callbacks; ``state`` is the closure's mutable dict (it stores the last
    check time). Lets the operator's Cancel button stop a long apply/destroy within
    ~``interval_s`` without a DB hit on every streamed line."""
    import time
    now = time.monotonic()
    if now - state.get("_cc", 0.0) < interval_s:
        return
    state["_cc"] = now
    from ..database import SessionLocal
    from . import terraform
    s = SessionLocal()
    try:
        if is_cancelled(s, job_id):
            raise terraform.JobCancelled(f"job {job_id} cancelled")
    finally:
        s.close()


def _flip_resource_row(db: Session, job: Job) -> None:
    """Flip the cloud resource a stale provision/decommission job owned to
    ``failed`` so the operator can Delete it to clean up the orphan. Best-effort —
    the job is already marked failed regardless."""
    meta = job.metadata_dict or {}
    try:
        if job.job_type in ("k8s_provision", "k8s_decommission"):
            from ..database import K8sCluster
            cid = meta.get("cluster_id")
            row = db.query(K8sCluster).filter(K8sCluster.id == cid).first() if cid else None
            if row and row.status in ("provisioning", "deploying", "decommissioning"):
                row.status = "failed"
        elif job.job_type in ("clouddb_provision", "clouddb_decommission"):
            from ..database import CloudDatabase
            did = meta.get("db_id")
            row = db.query(CloudDatabase).filter(CloudDatabase.id == did).first() if did else None
            if row and row.status in ("provisioning", "decommissioning"):
                row.status = "failed"
    except Exception:
        pass


def reconcile_stale_jobs(db: Session, stale_after_minutes: int = 10) -> int:
    """Mark ``running`` jobs whose worker died (no heartbeat) as ``failed`` and flip
    their k8s/cloud-DB resource row to ``failed`` so the orphan is visible and
    Delete-able. Run at app + job-runner startup.

    Only ``running`` jobs are reconciled — NOT ``pending``: with the dedicated job
    runner the ``jobs`` table is a queue, so a job legitimately waits ``pending``
    until claimed, and a brief runner outage must not fail queued work.

    "Stale" = no heartbeat (``updated_at``, else ``started_at``/``created_at``) within
    ``stale_after_minutes``. Long-running provisions stream terraform output, which
    heartbeats the row every few seconds, so a *live* job is never falsely failed;
    a job whose worker died stops heartbeating and is reconciled. Idempotent — a
    second caller (gunicorn -w 2 + the runner) finds nothing left to do. Returns
    the count."""
    cutoff = datetime.utcnow() - timedelta(minutes=stale_after_minutes)
    n = 0
    reconciled = []
    for job in db.query(Job).filter(Job.status == "running").all():
        last = job.updated_at or job.started_at or job.created_at
        if last and last > cutoff:
            continue  # recent heartbeat → still live, leave it
        job.status = "failed"
        job.completed_at = datetime.utcnow()
        job.updated_at = datetime.utcnow()
        job.error_message = "Interrupted by an app restart (no heartbeat) — re-run if needed."
        _flip_resource_row(db, job)
        reconciled.append(job)
        n += 1
    if n:
        db.commit()
        # This path writes `failed` inline rather than going through set_failed, so it
        # would otherwise be the one job failure nobody hears about — and "the worker
        # died mid-provision" is exactly what an operator most needs told. Emitted after
        # the commit, or the outbox insert would flush a half-built reconcile. Up to five
        # processes run this at startup; the unique dedupe key absorbs the race.
        for job in reconciled:
            notify_job_failed(db, job)
    return n
