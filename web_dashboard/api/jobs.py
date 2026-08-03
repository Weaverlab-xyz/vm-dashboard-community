"""
Job management API endpoints.
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..config import settings
from ..database import User, get_db
from ..models.job import JobResponse, JobListResponse
from ..services import agent_job_meta, agent_service, job_service
from .auth import get_current_user, can_audit_jobs

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


def _job_to_response(job) -> JobResponse:
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
    )


@router.get("", response_model=JobListResponse)
async def list_jobs(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status: Optional[str] = Query(None),
    workgroup: Optional[str] = Query(None),
    batch_id: Optional[str] = Query(None, description="Only jobs from one bulk run"),
    include_routine: bool = Query(
        False, description="Include completed timer-driven maintenance passes "
                           "(job_service.ROUTINE_JOB_TYPES). Excluded by default."),
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
    )
    return JobListResponse(
        jobs=[_job_to_response(j) for j in jobs],
        total=total,
        page=page,
        page_size=page_size,
    )


# Declared BEFORE /{job_id}: FastAPI matches in declaration order, so the catch-all
# path param would otherwise swallow /batches/... and 404 on a missing "job".
@router.get("/batches/{batch_id}")
async def get_batch_summary(
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
async def get_job(
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
    return _job_to_response(job)


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
async def get_job_findings(
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
    if job.job_type not in agent_service.AGENT_JOB_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Job type '{job.job_type}' does not produce discovery findings.")

    return {"job_id": job.id, "status": job.status,
            **agent_job_meta.discover_findings(job.metadata_dict)}


@router.delete("/{job_id}")
async def cancel_job(
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
