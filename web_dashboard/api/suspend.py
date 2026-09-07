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

**One side effect, and it is deliberate.** Scheduling an Azure VM deployed before address
pinning shipped writes to its NIC first, flipping the private address ARM already gave it
from Dynamic to Static. The address does not change; only ARM's freedom to reclaim it on
deallocate does. It happens because the operator asked for the schedule and this is that
schedule's prerequisite, it is audited as ``azure_address_pinned``, and the response names
the address. If it fails, no schedule is set.
"""
import logging

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
    elif cloud == "azure":
        from .azure import _assert_can_act
    elif cloud == "oci":
        from .oci import _assert_can_act
    else:                                  # pragma: no cover — schedulable() refuses first
        raise HTTPException(status_code=400, detail=f"{cloud} VMs cannot be scheduled.")
    return _assert_can_act


_PERM_SCOPE = {"aws": "aws", "gcp": "gcp", "azure": "azure", "oci": "oci"}


async def _pin_azure_address(db: Session, job: Job, user: User) -> dict:
    """Freeze this Azure VM's private address, then record it. ``{"address", ...}``.

    The address is read **live from the NIC** rather than taken from job metadata: a VM
    that has already been through ``/power/stop`` and ``/power/start`` may have moved, and
    pinning the address we remember could claim one that now belongs to somebody else.
    Writing the live value back repairs stale metadata as a side effect.

    Fails closed. A pin that does not happen means no schedule is set — a VM must never
    carry a schedule its address cannot survive.
    """
    from ..services import azure_service

    meta = job.metadata_dict
    rg = meta.get("resource_group")
    nic_name = meta.get("nic_name")
    if not rg or not nic_name:
        # Deploys predating the `nic_name` result key. Naming the VM is more use than
        # naming the missing key, since the operator's next move is the portal.
        raise HTTPException(
            status_code=409,
            detail=(f"This deployment did not record which NIC belongs to "
                    f"'{meta.get('vm_name') or job.id}', so its address cannot be pinned "
                    f"automatically. Set the NIC's private IP to Static in the Azure "
                    f"portal, then set the schedule."))

    try:
        result = await azure_service.pin_private_address(rg, nic_name)
    except Exception as exc:  # noqa: BLE001 — AzureError and anything the SDK raises
        logger.warning("Pin failed for job %s (%s/%s): %s", job.id, rg, nic_name, exc)
        raise HTTPException(status_code=409, detail=str(exc))

    meta["private_ip"] = result["address"]
    meta["private_ip_static"] = True
    # Written straight to the row rather than through job_service.set_completed, which
    # also stamps completed_at: a deploy job should not claim it has just finished because
    # somebody set a schedule on it months later.
    job.metadata_dict = meta
    db.commit()

    job_service.log_audit(db, user.username, "azure_address_pinned", target_vm=job.id,
                          details={"vm_name": meta.get("vm_name"), "nic_name": nic_name,
                                   "resource_group": rg, "address": result["address"],
                                   "already_static": result["already_static"]})
    return result


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

    # Validated FIRST, before anything reaches a cloud: the pin below writes to a real NIC,
    # and a request that is about to be rejected for a malformed time should not have moved
    # anything. validate() RAISES rather than returning a verdict, and hands back the
    # stored form — so the columns are written from its output, not from the payload. Two
    # places normalising the same input is how they come to disagree.
    try:
        stored = suspend_schedule.validate(
            suspend_at=payload.suspend_at, resume_at=payload.resume_at,
            tz_name=payload.timezone, days=payload.days)
    except suspend_schedule.ScheduleError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # An Azure VM deployed before pinning shipped still has a dynamic address. Pin it now:
    # the operator asked for the schedule and pinning is its prerequisite, the address does
    # not change, and both the audit row and this response name it. `needs_address_pin` is
    # false for a VM that would be refused for any other reason, so nothing writes to the
    # NIC of a VM that is about to be turned away anyway.
    pinned = None
    if vm_suspend_policy.needs_address_pin(job.job_type, job.metadata_dict):
        pinned = await _pin_azure_address(db, job, current_user)

    ok, reason = vm_suspend_policy.schedulable(job.job_type, job.metadata_dict)
    if not ok:
        raise HTTPException(status_code=409, detail=reason)

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
            "enabled": suspend_sweeper.enabled(), "pinned": pinned}


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
