"""Recurring job schedules — the /schedules page's API.

A schedule is created FROM A JOB (``POST /from-job/{job_id}``), never from a form of its
own. See ``services/schedule_service`` for why: the payload is the job's already-validated
metadata, so there is no second code path to drift from the first, and every schedulable
job type is covered without per-form wiring.

**Ownership, not the change_windows scope, governs these routes.** Repeating your own job
is the same authority as running it — you could press the button again yourself. What
``change_windows`` gates is defining the CALENDAR (`write`) and APPROVING a change
somebody else booked (`use`); neither is what "run my playbook again next Saturday" is.
Admins see and manage everyone's, matching how ``api/jobs`` scopes the job list.
"""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import JobSchedule, User, get_db
from ..services import job_service, schedule_service
from ..services.suspend_schedule import ScheduleError
from .auth import can_audit_jobs, get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/schedules", tags=["schedules"])


class CreateFromJobRequest(BaseModel):
    name: str = ""
    change_window_id: str
    approval_required: Optional[bool] = None


class UpdateScheduleRequest(BaseModel):
    """Only the fields that are safe to change in place.

    The PAYLOAD is deliberately not among them. Editing what a recurring change actually
    does, without re-running it once, is how a schedule quietly starts doing something
    nobody has tested — so changing the work means repeating a new job instead.
    """
    name: Optional[str] = None
    enabled: Optional[bool] = None
    change_window_id: Optional[str] = None
    approval_required: Optional[bool] = None


def _owned(db: Session, schedule_id: str, user: User) -> JobSchedule:
    """Fetch a schedule the caller may act on, or 404.

    404 rather than 403 for someone else's, following this codebase's ownership-guard
    convention: a 403 confirms the row exists, which turns the endpoint into an
    existence oracle over other people's work.
    """
    row = db.query(JobSchedule).filter(JobSchedule.id == schedule_id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Schedule not found")
    if row.created_by != user.username and not can_audit_jobs(user):
        raise HTTPException(status_code=404, detail="Schedule not found")
    return row


@router.get("")
def list_schedules(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Every schedule the caller owns, or all of them for an auditor/admin."""
    query = db.query(JobSchedule)
    if not can_audit_jobs(current_user):
        query = query.filter(JobSchedule.created_by == current_user.username)
    rows = query.order_by(JobSchedule.name.asc()).all()
    return {"schedules": [schedule_service.describe(db, r) for r in rows]}


@router.get("/eligibility/{job_id}")
def check_eligibility(
    job_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Can this job be repeated, and if not, why not?

    Exists so the job detail page can hide the Repeat control with a reason instead of
    offering a button that 400s. The reason is the useful part: "packer builds cannot be
    repeated because their parameters are values, not references" teaches the rule.
    """
    job = job_service.get_job(db, job_id)
    if job is None or (job.created_by != current_user.username
                       and not can_audit_jobs(current_user)):
        raise HTTPException(status_code=404, detail="Job not found")
    reason = schedule_service.schedulable_reason(job)
    return {"job_id": job_id, "schedulable": not reason, "reason": reason}


@router.post("/from-job/{job_id}", status_code=201)
def create_from_job(
    job_id: str,
    payload: CreateFromJobRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Repeat this job on every occurrence of a change window."""
    job = job_service.get_job(db, job_id)
    if job is None or (job.created_by != current_user.username
                       and not can_audit_jobs(current_user)):
        raise HTTPException(status_code=404, detail="Job not found")
    try:
        row = schedule_service.create_from_job(
            db, job=job,
            name=payload.name or (job.metadata_dict.get("description")
                                  or job.job_type),
            change_window_id=payload.change_window_id,
            # The occurrences run as the person who set the schedule up, not as whoever
            # originally ran the job. They are the one choosing to make it recur.
            created_by=current_user.username,
            approval_required=payload.approval_required,
        )
    except ScheduleError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    job_service.log_audit(db, current_user.username, "job_schedule_create",
                          details={"schedule_id": row.id, "name": row.name,
                                   "job_type": row.job_type, "from_job": job_id,
                                   "change_window_id": row.change_window_id})
    return schedule_service.describe(db, row)


@router.put("/{schedule_id}")
def update_schedule(
    schedule_id: str,
    payload: UpdateScheduleRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    row = _owned(db, schedule_id, current_user)

    if payload.name is not None:
        clean = payload.name.strip()
        if not clean:
            raise HTTPException(status_code=400, detail="a schedule needs a name")
        row.name = clean

    if payload.change_window_id is not None:
        from ..services import change_window, change_window_service
        window = change_window_service.get(db, payload.change_window_id)
        if window is None:
            raise HTTPException(status_code=400, detail="that change window is gone")
        try:
            # Refuse a window that cannot recur, here rather than at fire time.
            start, _end = change_window.next_occurrence(window, schedule_service._utcnow())
        except ScheduleError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        row.change_window_id = window.id
        # Re-arm against the new window. Without this, a schedule moved from a window
        # whose occurrence has passed into one that is open RIGHT NOW fires immediately
        # — an occurrence the operator did not ask for, at the moment they were editing.
        row.last_materialised_for = start

    if payload.approval_required is not None:
        row.approval_required = bool(payload.approval_required) or None

    if payload.enabled is not None:
        row.enabled = bool(payload.enabled)
        if payload.enabled:
            # Re-enabling clears the self-disable bookkeeping, or the schedule would
            # switch itself off again after a single further failure.
            row.disabled_reason = None
            row.consecutive_failures = 0

    db.commit()
    db.refresh(row)
    job_service.log_audit(db, current_user.username, "job_schedule_update",
                          details={"schedule_id": row.id, "name": row.name,
                                   "enabled": row.enabled is not False})
    return schedule_service.describe(db, row)


@router.delete("/{schedule_id}")
def delete_schedule(
    schedule_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Delete a schedule.

    Occurrences it already created are left alone — they are ordinary jobs with their own
    history, and some may still be booked into a window. Deleting the schedule stops
    future ones only. To stop a booked occurrence as well, cancel that job.
    """
    row = _owned(db, schedule_id, current_user)
    name = row.name
    db.delete(row)
    db.commit()
    job_service.log_audit(db, current_user.username, "job_schedule_delete",
                          details={"schedule_id": schedule_id, "name": name})
    return {"deleted": schedule_id}
