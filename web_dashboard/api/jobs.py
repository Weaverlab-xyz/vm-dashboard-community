"""
Job management API endpoints.
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..config import settings
from ..database import User, get_db
from ..models.job import JobResponse, JobListResponse
from ..services import job_service
from .auth import get_current_user, can_audit_jobs

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


def _job_to_response(job) -> JobResponse:
    return JobResponse(
        id=job.id,
        job_type=job.job_type,
        workgroup=job.workgroup,
        vm_path=job.vm_path,
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
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    List jobs with optional filters.
    Non-admin users only see their own jobs.
    """
    owner_filter = None if can_audit_jobs(current_user) else current_user.username
    jobs, total = job_service.list_jobs(
        db,
        page=page,
        page_size=page_size,
        status=status,
        created_by=owner_filter,
        workgroup=workgroup,
    )
    return JobListResponse(
        jobs=[_job_to_response(j) for j in jobs],
        total=total,
        page=page,
        page_size=page_size,
    )


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


@router.delete("/{job_id}")
async def cancel_job(
    job_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Cancel a pending or running job."""
    job = job_service.get_job(db, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.created_by != current_user.username and not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Access denied")
    if job.status not in ("pending", "running"):
        raise HTTPException(status_code=409, detail=f"Cannot cancel a job with status '{job.status}'")

    updated = job_service.set_cancelled(db, job_id)
    return {"message": "Job cancelled", "job_id": job_id, "status": updated.status}
