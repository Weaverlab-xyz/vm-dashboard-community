"""Suspend schedules for cloud VMs — set, read, clear.

The schedule lives on the VM's deploy job row, because a cloud VM has no inventory table
of its own: its deploy job IS its record of existence, which is the same reason
``expires_at`` lives there. So these endpoints are keyed on a deploy job id.

**Ownership is delegated, not re-derived.** A schedule powers a VM, so it must require
exactly what pressing Suspend requires — and the cloud consoles key that on ``is_admin``
plus workgroups, NOT the ``is_effective_admin`` rule inventory and databases use. Rather
than restate either, this imports the twin module's own ``_assert_can_act``: the same
function the power endpoint calls, so a schedule can never be set on something its owner
could not power by hand. Restating a rule is how two copies of it drift.
"""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import Job, User, get_db
from ..services import (job_service, suspend_schedule, suspend_sweeper,
                        vm_suspend_policy)
from .auth import get_current_user, has_permission

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/suspend", tags=["suspend"])


class ScheduleRequest(BaseModel):
    suspend_at: str                      # "HH:MM", local to `timezone`
    resume_at: str = ""                  # "" = never wake it automatically
    timezone: str = "UTC"                # IANA name
    days: str = suspend_schedule.DAYS_ALL  # 7 chars, Monday first


def _guard(cloud: str):
    """The twin module's ownership check for this cloud — imported, never re-derived."""
    if cloud == "aws":
        from .aws import _assert_can_act
    elif cloud == "gcp":
        from .gcp import _assert_can_act
    else:                                  # pragma: no cover — schedulable() refuses first
        raise HTTPException(status_code=400, detail=f"{cloud} VMs cannot be scheduled.")
    return _assert_can_act


_PERM_SCOPE = {"aws": "aws", "gcp": "gcp"}


def _load(db: Session, job_id: str, user: User) -> Job:
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="No such deployment.")
    cloud = vm_suspend_policy.cloud_of(job.job_type)
    if not cloud:
        raise HTTPException(status_code=400,
                            detail="That job is not a cloud VM deployment.")
    # The permission scope follows the VM's cloud. Hardcoding one would let `aws:write`
    # schedule a GCE instance, which is not what "you may power what you can see" means.
    if not has_permission(user, _PERM_SCOPE[cloud], "write"):
        raise HTTPException(status_code=403,
                            detail=f"Requires '{_PERM_SCOPE[cloud]}:write' permission.")
    _guard(cloud)(user, job.workgroup, "This VM")
    return job


@router.get("/{job_id}")
async def read_schedule(
    job_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    """The VM's schedule, and whether it may have one at all."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="No such deployment.")
    out = vm_suspend_policy.describe(job.job_type, job.metadata_dict)
    out["enabled"] = suspend_sweeper.enabled()
    out["schedule"] = suspend_schedule.describe(job) if suspend_schedule.has_schedule(job) else None
    return out


@router.put("/{job_id}")
async def set_schedule(
    job_id: str,
    payload: ScheduleRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    """Give a VM a business-hours power window.

    Refused with a reason when the VM cannot safely carry one — see
    ``services/vm_suspend_policy``. The reason is returned rather than a bare 403 because
    a control that refuses without saying why is a bug report waiting to happen.
    """
    job = _load(db, job_id, current_user)
    ok, reason = vm_suspend_policy.schedulable(job.job_type, job.metadata_dict)
    if not ok:
        raise HTTPException(status_code=409, detail=reason)

    # validate() RAISES rather than returning a verdict, and hands back the stored form —
    # so the columns are written from its output, not from the payload. Two places
    # normalising the same input is how they come to disagree.
    try:
        stored = suspend_schedule.validate(
            suspend_at=payload.suspend_at, resume_at=payload.resume_at,
            tz_name=payload.timezone, days=payload.days)
    except suspend_schedule.ScheduleError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    job.suspend_at_local = stored["suspend_at_local"]
    job.resume_at_local = stored["resume_at_local"]
    job.schedule_timezone = stored["schedule_timezone"]
    job.schedule_days = stored["schedule_days"]
    # Left NULL on purpose. due_action refuses to act on a never-evaluated schedule, so
    # setting one cannot suspend a VM for boundaries crossed before it existed — the same
    # arming rule the auto-delete timer uses, and the reason enabling this on an existing
    # fleet does nothing until a boundary is crossed with the sweep watching.
    job.schedule_last_checked_at = None
    db.commit()

    job_service.log_audit(db, current_user.username, "vm_schedule_set",
                          target_vm=job_id,
                          details={"suspend_at": job.suspend_at_local,
                                   "resume_at": job.resume_at_local,
                                   "timezone": job.schedule_timezone,
                                   "days": job.schedule_days})
    return {"ok": True, "schedule": suspend_schedule.describe(job),
            "enabled": suspend_sweeper.enabled()}


@router.delete("/{job_id}")
async def clear_schedule(
    job_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    """Remove the schedule. Does not change the VM's current power state — clearing a
    schedule should not start or stop anything, only stop it happening again."""
    job = _load(db, job_id, current_user)
    job.suspend_at_local = None
    job.resume_at_local = None
    job.schedule_timezone = None
    job.schedule_days = None
    job.schedule_last_checked_at = None
    db.commit()
    job_service.log_audit(db, current_user.username, "vm_schedule_cleared",
                          target_vm=job_id)
    return {"ok": True, "schedule": None}
