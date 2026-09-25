"""
Job management API endpoints.
"""
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..config import settings
from ..database import User, get_db
from ..models.job import JobResponse, JobListResponse
from ..services import agent_job_meta, change_window_service, job_service, terraform
from .auth import get_current_user, can_audit_jobs, require_admin

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


def _job_to_response(job, *, window_names: Optional[dict] = None,
                     db: Session = None) -> JobResponse:
    """Project one Job row for the API.

    ``window_names`` is a ``{change_window_id: name}`` map so the LIST endpoint can
    resolve every window in one query instead of one per row. The single-job endpoints
    pass ``db`` instead and take the one-row lookup — without it the job detail page
    showed a booked change with no window name, which reads as "booked into nothing".
    """
    if window_names is None and db is not None and job.change_window_id:
        window_names = change_window_service.names_for(db, [job])
    return JobResponse(
        id=job.id,
        job_type=job.job_type,
        workgroup=job.workgroup,
        vm_path=job.vm_path,
        description=job.metadata_dict.get("description"),
        batch_id=job.batch_id,
        status=job.status,
        progress_pct=job.progress_pct,
        progress_message=job.progress_message,
        created_at=job.created_at,
        started_at=job.started_at,
        completed_at=job.completed_at,
        created_by=job.created_by,
        error_message=job.error_message,
        duration_seconds=job.duration_seconds,
        schedule_state=job_service.schedule_state(job),
        scheduled_for=job.scheduled_for,
        window_ends_at=job.window_ends_at,
        change_window_name=(window_names or {}).get(job.change_window_id),
        approval_required=bool(job.approval_required),
        approved_at=job.approved_at,
        approved_by=job.approved_by,
    )


@router.get("", response_model=JobListResponse)
def list_jobs(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status: Optional[str] = Query(None),
    workgroup: Optional[str] = Query(None),
    batch_id: Optional[str] = Query(None, description="Only jobs from one bulk run"),
    include_routine: bool = Query(
        False, description="Include completed timer-driven maintenance passes "
                           "(job_service.ROUTINE_JOB_TYPES). Excluded by default."),
    include_checkins: bool = Query(
        False, description="Include completed unattended check-ins — the hypervisor "
                           "inventory syncs a cadence enqueues (job_service."
                           "CHECKIN_VERBS). Excluded by default. Operator-initiated "
                           "work of the same job type, e.g. a power op, is never "
                           "affected by this."),
    dead_lettered: bool = Query(
        False, description="Only jobs that used every retry and failed anyway."),
    scheduled: bool = Query(
        False, description="Only jobs waiting for a change window or an approval."),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    List jobs with optional filters.
    Non-admin users only see their own jobs.

    Completed rows of ``job_service.ROUTINE_JOB_TYPES`` are hidden unless
    ``include_routine`` is set. The default is what makes both this page and the
    dashboard's recent-activity widget readable: the auto-delete sweep writes 48 rows a
    day whether or not it found anything, so a real deploy drops off the first page within
    hours. A *failed* routine pass is never hidden, so the dashboard's failed-jobs panel
    keeps working — see the constant for why that split matters.

    ``include_checkins`` is the same default for the same reason, applied to the
    hypervisor inventory syncs a cadence enqueues: 30 minutes per connection, several
    paged rows apiece, all completing green with nothing for anyone to do. It keys on
    the ``is_checkin`` column, not the job type — an operator's power op is an
    ``agent_hypervisor`` row too and is never hidden — and again only ``completed``
    rows, so a sync that failed still reaches the failed-jobs panel.

    ``dead_lettered=true`` is the retry tail: jobs that used every attempt and failed
    anyway. Scoped by the same owner filter as everything else here.
    """
    owner_filter = None if can_audit_jobs(current_user) else current_user.username
    jobs, total = job_service.list_jobs(
        db,
        page=page,
        page_size=page_size,
        status=status,
        created_by=owner_filter,
        workgroup=workgroup,
        batch_id=batch_id,
        include_routine=include_routine,
        include_checkins=include_checkins,
        dead_lettered=dead_lettered,
        scheduled=scheduled,
    )
    # Resolved ONCE for the page, not per row — otherwise a 100-job page is 100 queries.
    window_names = change_window_service.names_for(db, jobs)
    return JobListResponse(
        jobs=[_job_to_response(j, window_names=window_names) for j in jobs],
        total=total,
        page=page,
        page_size=page_size,
    )


# Declared BEFORE /{job_id}: FastAPI matches in declaration order, so the catch-all
# path param would otherwise swallow /batches/... and 404 on a missing "job".
@router.get("/batches/{batch_id}")
def get_batch_summary(
    batch_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Status rollup for one bulk-run batch — `{batch_id, total, by_status}`.

    Scoped exactly like the list endpoint: a user without `jobs:read` sees counts of
    their own jobs only, so this can't reveal that someone else's jobs exist. An
    unknown batch is not a 404 — it reports zeros, which is what a batch whose jobs
    the caller cannot see looks like anyway, and avoids turning the endpoint into an
    existence oracle."""
    owner_filter = None if can_audit_jobs(current_user) else current_user.username
    return job_service.batch_summary(db, batch_id, created_by=owner_filter)


@router.get("/{job_id}", response_model=JobResponse)
def get_job(
    job_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get details for a specific job."""
    job = job_service.get_job(db, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.created_by != current_user.username and not can_audit_jobs(current_user):
        raise HTTPException(status_code=403, detail="Access denied")
    return _job_to_response(job, db=db)


@router.get("/{job_id}/logs")
async def get_job_logs(
    job_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Fetch CloudWatch logs for an ansible_run job. Ansible-feature only."""
    if not settings.ansible_enabled:
        raise HTTPException(status_code=404, detail="Ansible feature not enabled")

    job = job_service.get_job(db, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.created_by != current_user.username and not can_audit_jobs(current_user):
        raise HTTPException(status_code=403, detail="Access denied")

    meta = job.metadata_dict
    log_group = meta.get("ecs_log_group")
    log_stream = meta.get("ecs_log_stream")

    if not log_group or not log_stream:
        return {"lines": [], "message": "No log stream recorded for this job (run a new job to enable log capture)."}

    from ..services import ansible_service
    from ..services.ansible_service import AnsibleError

    region = settings.storage_s3_region or settings.aws_region
    try:
        lines = await ansible_service.fetch_cloudwatch_logs(region, log_group, log_stream)
        return {"lines": lines, "log_group": log_group, "log_stream": log_stream}
    except AnsibleError as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.get("/{job_id}/findings")
def get_job_findings(
    job_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """What a discovery scan found — the result half of an ``agent_discover`` job.

    A remote agent reports its findings by completing the job, and ``set_completed``
    merges them into the job's metadata. Nothing else reads that metadata back out:
    ``_job_to_response`` deliberately projects a fixed set of scalars, so without this
    endpoint a scan's entire result is written to the database and never shown to anyone,
    and a run that found a cluster is indistinguishable from one that found nothing.

    Returns the allowlisted projection from ``agent_job_meta.discover_findings``, never
    the metadata dict — job metadata carries Terraform variables and resolved config for
    other job types, and an endpoint keyed only on a job id must not become the way to
    read them.

    The counters come back for any status, including ``failed``: "scanned 0 of 384
    because policy refused them" is the answer to the question a fruitless scan raises,
    and a job that failed part-way still has one.
    """
    job = job_service.get_job(db, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    # Same ownership rule as GET /api/jobs/{id} — findings name hosts on a private
    # network, so they are at least as sensitive as the job row itself.
    if job.created_by != current_user.username and not can_audit_jobs(current_user):
        raise HTTPException(status_code=403, detail="Access denied")
    # Named explicitly rather than "any agent job type". Discovery is the only type
    # whose result IS a findings list; the moment AGENT_JOB_TYPES grew a second member
    # a membership test started serving discover_findings() for a job with a different
    # result shape, which renders as "this scan found nothing" for a job that never
    # scanned. A new type wanting a panel gets its own projection and its own endpoint.
    if job.job_type != "agent_discover":
        raise HTTPException(
            status_code=400,
            detail=f"Job type '{job.job_type}' does not produce discovery findings.")

    return {"job_id": job.id, "status": job.status,
            **agent_job_meta.discover_findings(job.metadata_dict)}


# -- Terraform state lock ------------------------------------------------------
# A killed or lost terraform leaves its state lock behind, and that lock then wedges
# every later run against that state -- including the destroy that would clean the
# deployment up. services/terraform.py releases the locks a CANCEL orphans, because
# there the worker killed the holder itself and knows it is dead. Everything else
# (worker OOM-killed, replica rolled mid-apply) is an operator's call, taken against
# the lock's own Who/Created. These two endpoints are that call, in-app, instead of
# "go delete the .tflock object out of the bucket".
#
# The state key is never taken from the caller. It is read out of the failed job's own
# error text, so an admin can break a lock a job actually complained about and nothing
# else -- and it is read from the lock's PATH rather than assumed to be the job's own
# id, because a decommission destroys in the provision job's deploy dir and those two
# ids differ exactly when this feature is needed.


def _lock_view(job) -> dict:
    """The lock ``job`` reported, or ``{}``. Reads error_message because that is the
    projection the operator is actually looking at on the job page."""
    return terraform.reported_lock(job.error_message or "")


def _blocker_view(db, created) -> list:
    return [{"job_id": j.id, "job_type": j.job_type,
             "started_at": j.started_at.isoformat() if j.started_at else None}
            for j in job_service.terraform_lock_blockers(db, created)]


def _blocker_reason(blockers: list) -> str:
    """Why the unlock is refused, in terms the operator can act on.

    Deliberately conservative: EVERY running job is considered, not an allowlist of
    the ones known to run terraform. An allowlist would go stale the first time a new
    job type started using terraform, and it would go stale in the unsafe direction --
    silently failing to name a real holder. In practice this rarely fires, because a
    blocker has to have been running since before the lock was taken, and a stale lock
    is usually hours old. When it does fire on a wedged job, cancelling that job is the
    way through, so say so."""
    names = ", ".join(f"{b['job_type']} {b['job_id'][:8]}" for b in blockers)
    return ("A job that was already running when this lock was taken could still be "
            f"holding it: {names}. Wait for it to finish, or cancel it if it is stuck.")


@router.get("/{job_id}/state-lock")
async def get_job_state_lock(
    job_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Report the Terraform state lock this job failed on, and whether it is safe to
    break. Admin-only, and a plain 200 with ``reported: null`` when the job named no
    lock -- the panel that calls this is speculative, and a 404 for the overwhelmingly
    common "this job is not about a lock" case would be noise, not information."""
    job = job_service.get_job(db, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    reported = _lock_view(job)
    if not reported:
        return {"job_id": job_id, "reported": None, "can_unlock": False,
                "reason": "This job did not report a Terraform state lock."}

    state_job_id = reported["state_job_id"]
    out = {"job_id": job_id, "reported": reported, "state_job_id": state_job_id}
    try:
        live = await terraform.inspect_state_lock(state_job_id)
    except Exception as exc:
        logger.warning("state-lock inspect for job %s (state %s) failed: %s",
                       job_id, state_job_id, exc)
        return {**out, "can_unlock": False,
                "reason": f"Could not read the lock from the state backend: {exc}"}

    out.update(live)
    if not live.get("supported"):
        return {**out, "can_unlock": False, "reason": live.get("detail", "")}
    if not live.get("locked"):
        return {**out, "can_unlock": False,
                "reason": "The lock is already gone - whatever held it released it. "
                          "Retry the operation that failed."}

    created = terraform._parse_lock_time(live["info"].get("Created", ""))
    out["age_seconds"] = (
        int((datetime.now(timezone.utc) - created).total_seconds()) if created else None)

    blockers = _blocker_view(db, created)
    out["blockers"] = blockers
    if blockers:
        return {**out, "can_unlock": False, "reason": _blocker_reason(blockers)}
    return {**out, "can_unlock": True, "reason": ""}


@router.delete("/{job_id}/state-lock")
async def force_unlock_job_state(
    job_id: str,
    request: Request,
    lock_id: str = Query(..., description="The lock id the operator was shown. The "
                                          "unlock is refused if it no longer matches."),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Break the Terraform state lock this job failed on. Admin-only.

    Every guard from the GET is re-evaluated here rather than trusted from the page:
    the state key is re-read from the job's error, the blocker check is re-run against
    the LIVE lock's stamp, and terraform.force_unlock_state re-reads the lock and
    refuses unless ``lock_id`` is still the one held. A page left open for an hour
    therefore cannot break a lock some run took five minutes ago."""
    job = job_service.get_job(db, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    reported = _lock_view(job)
    if not reported:
        raise HTTPException(status_code=409,
                            detail="This job did not report a Terraform state lock.")
    state_job_id = reported["state_job_id"]

    try:
        live = await terraform.inspect_state_lock(state_job_id)
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Could not read the lock from the state backend: {exc}")
    if not live.get("locked"):
        raise HTTPException(
            status_code=409,
            detail="No lock is held on this state any more - nothing to break.")

    created = terraform._parse_lock_time(live["info"].get("Created", ""))
    blockers = _blocker_view(db, created)
    if blockers:
        raise HTTPException(status_code=409, detail=_blocker_reason(blockers))

    try:
        result = await terraform.force_unlock_state(state_job_id, lock_id)
    except terraform.TerraformError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:
        logger.warning("force-unlock for job %s (state %s) failed: %s",
                       job_id, state_job_id, exc)
        raise HTTPException(status_code=502, detail=f"force-unlock failed: {exc}")

    broken = result.get("info", {})
    job_service.log_audit(
        db, current_user.username, "terraform.force_unlock",
        ip_address=(request.client.host if request.client else ""),
        details={"job_id": job_id, "state_job_id": state_job_id,
                 "backend": result.get("backend", ""),
                 "lock_id": result.get("lock_id", ""),
                 "who": broken.get("Who", ""), "created": broken.get("Created", ""),
                 "operation": broken.get("Operation", "")})
    logger.warning("terraform state lock %s on %s force-unlocked by %s (held by %s "
                   "since %s)", result.get("lock_id", ""), state_job_id,
                   current_user.username, broken.get("Who", ""),
                   broken.get("Created", ""))
    return {"message": "State lock released", "state_job_id": state_job_id, **result}


class RescheduleRequest(BaseModel):
    """Move a booked change. Blank everything = run it as soon as there is capacity."""
    run_at: str = ""
    run_timezone: str = ""
    change_window_id: str = ""


@router.post("/{job_id}/reschedule")
def reschedule_job(
    job_id: str,
    payload: RescheduleRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Move a scheduled change to a different time or window — or release it to run now.

    Gated on OWNERSHIP, not on ``change_windows``, and the distinction matters: moving
    your own change is the same authority as raising it in the first place, whereas
    approving one is deliberately somebody else's. An admin can move anyone's.

    **Re-approval is required after a move.** A change approved for 02:00 Saturday is not
    thereby approved for 14:00 Tuesday — the approver signed off on a specific time in a
    specific window. Clearing the approval here is what stops a reschedule from being a
    way around the gate, and it is the one line in this endpoint worth not deleting.

    A missed change is rescheduled by the same call, which is the intended recovery
    path: ``cancelled`` with ``missed_window_at`` set is terminal for the RUN, not for
    the intent, so the row is revived rather than making the operator rebuild the job
    from scratch.
    """
    job = job_service.get_job(db, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.created_by != current_user.username and not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Access denied")
    if job.status == "running":
        raise HTTPException(status_code=409,
                            detail="this change has already started")
    if job.status in ("completed", "failed"):
        raise HTTPException(status_code=409,
                            detail=f"this change is {job.status} and cannot be moved")

    from ..services.suspend_schedule import ScheduleError
    try:
        scheduled_for, window_ends_at, window_id = change_window_service.resolve(
            db, run_at=payload.run_at, run_timezone=payload.run_timezone,
            change_window_id=payload.change_window_id)
    except ScheduleError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    job.scheduled_for = scheduled_for
    job.window_ends_at = window_ends_at
    job.change_window_id = window_id
    # Reviving a missed change: clear the terminal marks so the claim query can see it
    # again. Left set, the row would stay `cancelled` and the reschedule would appear to
    # work while nothing ever ran.
    job.missed_window_at = None
    job.completed_at = None
    job.error_message = None
    # Status is PRESERVED for a job that is still active, never recomputed. `queued`
    # does not only mean "an agent owns this" — it is also how a child row a PARENT job
    # drives is kept out of the runner's claim query (see job_service.create_job).
    # Rewriting such a row to `pending` because it has no agent_id would hand it to the
    # worker while its parent is still driving it, and it would execute twice.
    #
    # Only a revived missed change needs a status chosen, and that row was `cancelled`.
    # A parent-driven child cannot reach that state: the sweeper only reaps rows with a
    # `window_ends_at`, and a parent sets none.
    if job.status not in ("queued", "pending"):
        job.status = "queued" if job.agent_id else "pending"
    # See the docstring: the approver signed off on a time, not on the job.
    if job.approval_required:
        job.approved_at = None
        job.approved_by = None
    db.commit()
    job_service.log_audit(
        db, current_user.username, "change_rescheduled",
        details={"job_id": job.id, "job_type": job.job_type,
                 "scheduled_for": scheduled_for.isoformat() if scheduled_for else "now",
                 "change_window_id": window_id or ""})
    return {"job_id": job.id, "status": job.status,
            "scheduled_for": job.scheduled_for,
            "window_ends_at": job.window_ends_at,
            "schedule_state": job_service.schedule_state(job)}


@router.delete("/{job_id}")
def cancel_job(
    job_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Cancel a queued, pending or running job."""
    job = job_service.get_job(db, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.created_by != current_user.username and not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Access denied")
    # `queued` included: a job assigned to a remote agent that never came back would
    # otherwise be uncancellable, and it is the one an operator most wants to clear.
    if job.status not in ("queued", "pending", "running"):
        raise HTTPException(status_code=409, detail=f"Cannot cancel a job with status '{job.status}'")

    updated = job_service.set_cancelled(db, job_id)
    return {"message": "Job cancelled", "job_id": job_id, "status": updated.status}
