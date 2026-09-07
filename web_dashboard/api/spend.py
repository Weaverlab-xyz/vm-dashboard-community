"""Spend caps for cloud VMs — set, read, clear.

The cap lives on the VM's deploy job row, because a cloud VM has no inventory table of its
own: its deploy job IS its record of existence, the same reason ``expires_at`` and the five
schedule columns live there. So these endpoints are keyed on a deploy job id.

**Ownership is delegated, not re-derived.** A cap can suspend a VM, so it must require
exactly what pressing Suspend requires — and the cloud consoles key that on ``is_admin``
plus workgroups, NOT the ``is_effective_admin`` rule inventory and databases use. Rather
than restate either, this imports the same ``_assert_can_act`` and ``_PERM_SCOPE`` pair
``api/suspend`` uses, from the cloud module that owns the rule.

**A cap that cannot fire is refused here, not stored and forgotten.** This is the whole
point of the endpoint's ordering. ``spend_policy.accrue`` treats a missing rate as *move the
clock on, bill nothing* — correct for an interval where the price API was briefly
unreachable, and silence for a VM whose region has no price source at all. Stored anyway,
such a cap accrues zero forever: the operator sees ``$0.00 of $500.00``, concludes they are
protected, and is not. So ``spend_policy.cappable`` is asked BEFORE the value is written,
and its refusal names the cloud and the region rather than shrugging.
"""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import Job, User, get_db
from ..services import (job_service, pov_cloud_cost, spend_policy, spend_sweeper,
                        vm_suspend_policy)
from .auth import get_current_user, has_permission
from .suspend import _PERM_SCOPE, _guard

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/spend", tags=["spend"])


class CapRequest(BaseModel):
    cap_usd: float


def _load(db: Session, job_id: str, user: User) -> Job:
    """The deploy row, once this caller has been shown to own it.

    Identical in shape to ``api/suspend._load`` and deliberately so — the two endpoints
    govern the same VMs and must not come to disagree about who may touch them. The
    permission scope follows the VM's own cloud, so ``aws:write`` cannot cap a GCE instance.
    """
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="No such deployment.")
    cloud = vm_suspend_policy.cloud_of(job.job_type)
    if not cloud:
        raise HTTPException(status_code=400,
                            detail="That job is not a cloud VM deployment.")
    if not has_permission(user, _PERM_SCOPE[cloud], "write"):
        raise HTTPException(status_code=403,
                            detail=f"Requires '{_PERM_SCOPE[cloud]}:write' permission.")
    _guard(cloud)(user, job.workgroup, "This VM")
    return job


def _region_of(job: Job, meta: dict) -> str:
    """The region a price is looked up in. One reader, shared with the sweep's ``_rate_for``
    — if the two disagreed, a cap accepted here would find no price there."""
    cloud = vm_suspend_policy.cloud_of(job.job_type)
    region = meta.get("region") or meta.get("location") or meta.get("zone") or ""
    if cloud == "gcp" and region:
        from ..services import region_catalog
        region = region_catalog.region_from_zone(region) or region
    return region


def _cappable(job: Job) -> tuple:
    """``(ok, reason)`` for this VM, with the cloud facts filled in.

    ``suspendable`` is only consulted when the configured action is ``suspend``: under
    ``warn`` a cap on an unsuspendable VM is perfectly meaningful — it still warns — and
    refusing it would be refusing something that works.
    """
    meta = job.metadata_dict
    cloud = vm_suspend_policy.cloud_of(job.job_type)
    suspendable = (True, "")
    if spend_sweeper.action() == spend_policy.ACTION_SUSPEND:
        suspendable = vm_suspend_policy.schedulable(job.job_type, meta)
    return spend_policy.cappable(cloud, _region_of(job, meta),
                                 priceable=pov_cloud_cost.priceable,
                                 suspendable=suspendable)


@router.get("/{job_id}")
async def read_cap(
    job_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    """The VM's cap and what has accrued against it, plus whether it may carry one."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="No such deployment.")
    ok, reason = _cappable(job)
    out = spend_policy.describe(job, warn_at_percent=spend_sweeper.warn_percent(),
                                action=spend_sweeper.action())
    out.update({"cappable": ok, "reason": reason,
                "enabled": spend_sweeper.enabled(),
                "cloud": vm_suspend_policy.cloud_of(job.job_type)})
    return out


@router.put("/{job_id}")
async def set_cap(
    job_id: str,
    payload: CapRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    """Give a VM a spend cap, in US dollars.

    Refused with a reason when the VM cannot carry one — see the module docstring. The
    reason is returned rather than a bare 403 because a control that refuses without saying
    why is a bug report waiting to happen, and because this particular refusal is the
    feature working rather than failing.
    """
    job = _load(db, job_id, current_user)

    # Validated first, so a malformed amount is answered before anything asks a cloud for a
    # price. validate_cap RAISES and returns the stored form, so the column is written from
    # its output rather than from the payload.
    try:
        cap = spend_policy.validate_cap(payload.cap_usd)
    except spend_policy.SpendError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if cap is None:
        raise HTTPException(status_code=400,
                            detail="Give an amount, or DELETE this cap to remove it.")

    ok, reason = _cappable(job)
    if not ok:
        raise HTTPException(status_code=409, detail=reason)

    previous = job.spend_cap_usd
    job.spend_cap_usd = cap
    # Raising the cap clears both latches, which is what makes "give it another fifty
    # dollars" work — a row that stayed capped would never warn or act again.
    if previous is None or cap > float(previous):
        job.spend_warned_at = None
        job.spend_capped_at = None
    # Left NULL on a first cap: accrue() refuses to bill a never-measured row, so setting a
    # cap cannot invent a charge for every hour since the VM was deployed. The same arming
    # rule the suspend schedule's latch and the auto-delete timer both use.
    db.commit()

    job_service.log_audit(db, current_user.username, "vm_spend_cap_set", target_vm=job_id,
                          details={"cap_usd": cap, "previous": previous,
                                   "action": spend_sweeper.action()})
    return {"ok": True, "enabled": spend_sweeper.enabled(),
            **spend_policy.describe(job, warn_at_percent=spend_sweeper.warn_percent(),
                                    action=spend_sweeper.action())}


@router.delete("/{job_id}")
async def clear_cap(
    job_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    """Remove the cap. Does not change the VM's power state — clearing a cap should not
    start anything, only stop it being acted on again.

    The accrued total is KEPT. It is a record of what this VM has cost since it was first
    measured, and it is the same number a re-added cap should continue from; zeroing it
    here would let an operator clear and re-add a cap to reset the meter without meaning to.
    """
    job = _load(db, job_id, current_user)
    job.spend_cap_usd = None
    job.spend_warned_at = None
    job.spend_capped_at = None
    db.commit()
    job_service.log_audit(db, current_user.username, "vm_spend_cap_cleared",
                          target_vm=job_id,
                          details={"spent_usd": round(float(job.spend_estimate_usd or 0), 2)})
    return {"ok": True, "capped": False,
            "spent_usd": round(float(job.spend_estimate_usd or 0.0), 2)}
