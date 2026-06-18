"""
Job management service.
Creates, updates, and queries background job records.
"""
import json
import uuid
from datetime import datetime, timedelta
from typing import Optional, List
from sqlalchemy.orm import Session

from ..database import Job, AuditLog


def create_job(
    db: Session,
    job_type: str,
    created_by: str,
    vm_path: Optional[str] = None,
    workgroup: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> Job:
    """Create a new job record with status 'pending'."""
    job = Job(
        id=str(uuid.uuid4()),
        job_type=job_type,
        vm_path=vm_path,
        workgroup=workgroup,
        status="pending",
        progress_pct=0,
        created_at=datetime.utcnow(),
        created_by=created_by,
    )
    if metadata:
        job.metadata_dict = metadata
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
    return job


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
    """Mark a job as cancelled."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if job and job.status in ("pending", "running"):
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
) -> tuple[List[Job], int]:
    """
    List jobs with optional filters.
    Returns (jobs, total_count).
    """
    query = db.query(Job)
    if status:
        query = query.filter(Job.status == status)
    if created_by:
        query = query.filter(Job.created_by == created_by)
    if workgroup:
        query = query.filter(Job.workgroup == workgroup)

    total = query.count()
    jobs = (
        query.order_by(Job.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return jobs, total


def has_active_job_for_vm(db: Session, vmx_path: str) -> bool:
    """Return True if a pending/running job already targets this VM."""
    count = (
        db.query(Job)
        .filter(Job.vm_path == vmx_path, Job.status.in_(["pending", "running"]))
        .count()
    )
    return count > 0


def log_audit(
    db: Session,
    username: str,
    action: str,
    ip_address: Optional[str] = None,
    target_vm: Optional[str] = None,
    details: Optional[dict] = None,
):
    """Write an entry to the audit log table."""
    import uuid
    entry = AuditLog(
        id=str(uuid.uuid4()),
        timestamp=datetime.utcnow(),
        username=username,
        action=action,
        target_vm=target_vm,
        ip_address=ip_address,
    )
    if details:
        entry.details_dict = details
    db.add(entry)
    db.commit()


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
    for job in db.query(Job).filter(Job.status == "running").all():
        last = job.updated_at or job.started_at or job.created_at
        if last and last > cutoff:
            continue  # recent heartbeat → still live, leave it
        job.status = "failed"
        job.completed_at = datetime.utcnow()
        job.updated_at = datetime.utcnow()
        job.error_message = "Interrupted by an app restart (no heartbeat) — re-run if needed."
        _flip_resource_row(db, job)
        n += 1
    if n:
        db.commit()
    return n
