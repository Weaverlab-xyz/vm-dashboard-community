"""
Job management service.
Creates, updates, and queries background job records.
"""
import json
import logging
import uuid
from datetime import datetime, timedelta
from typing import Optional, List
from sqlalchemy import and_, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..database import Job, AuditLog
from . import audit_chain

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
ROUTINE_JOB_TYPES = ("expiry_sweep",)


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
    None on a set-membership test for every job type that isn't a VM deploy."""
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
            job.expires_at = expiry_policy.default_expiry_for(job_type, workgroup=workgroup)
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
    """Mark a job as running."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if job:
        job.status = "running"
        job.started_at = datetime.utcnow()
        job.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(job)
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


def set_failed(db: Session, job_id: str, error: str) -> Optional[Job]:
    """Mark a job as failed with an error message."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if job:
        job.status = "failed"
        job.completed_at = datetime.utcnow()
        job.updated_at = datetime.utcnow()
        job.error_message = error
        db.commit()
        db.refresh(job)
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
) -> tuple[List[Job], int]:
    """
    List jobs with optional filters.
    Returns (jobs, total_count).

    ``include_routine=False`` drops the completed rows of :data:`ROUTINE_JOB_TYPES` — the
    timer-driven maintenance passes that would otherwise bury every operator-initiated job.
    It excludes only ``completed`` ones, so a failed sweep still surfaces. Defaults to True
    so this stays a display choice made by the caller that renders a list, not a filter
    silently applied to every count in the app.
    """
    query = db.query(Job)
    if not include_routine:
        query = query.filter(~and_(Job.job_type.in_(ROUTINE_JOB_TYPES),
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
    """
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
                seq, entry.timestamp, username, action, target_vm, entry.details, prev_hash
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


def verify_audit_chain(db: Session) -> dict:
    """Recompute the whole audit chain and report integrity.

    Returns ``{"ok": bool, "count": int, "first_broken_seq": int | None}``.
    ``ok`` is False (with the offending ``seq``) if any row was edited, deleted,
    or reordered since it was written."""
    rows = (
        db.query(AuditLog)
        .filter(AuditLog.seq.isnot(None))
        .order_by(AuditLog.seq.asc())
        .all()
    )
    ok, broken = audit_chain.verify_chain(rows)
    return {"ok": ok, "count": len(rows), "first_broken_seq": broken}


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
            i, e.timestamp, e.username, e.action, e.target_vm, e.details, prev
        )
        prev = e.entry_hash
    db.commit()
    return len(rows)


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
