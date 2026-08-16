"""Auto-delete timer API — set, extend or clear a resource's expiry, and read the
sweeper's state.

Kept out of ``api/inventory.py`` on purpose: that module's docstring commits to being a
read-only aggregation, and these are mutations.

One endpoint serves both the per-row button and a multi-row selection
(``inventory_ids`` of length 1 *is* the button), so the two can never drift apart.
Resources are named by INVENTORY id — ``job:…`` / ``clouddb:…`` / ``k8s:…`` — rather than
by cloud id, matching ``BulkRunRequest``; the server resolves each from its own records.
Ids travel in the body rather than a path segment so a ``:`` never has to survive URL
encoding through a proxy.
"""
import logging
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..database import User, get_db
from ..services import cache_service, expiry_reaper, inventory_service
from .auth import get_current_user, require_admin

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/expiry", tags=["expiry"])

# Ceiling on one request, matching the bulk Config-Management run. A mis-clicked
# "select all" should not rewrite an unbounded number of rows in one audited act.
MAX_TARGETS = inventory_service.MAX_BULK_TARGETS


class ExpirySetRequest(BaseModel):
    """One expiry change applied to one or more resources.

    Exactly one of ``extend_hours`` / ``expires_at`` / ``never`` is meaningful.
    ``extend_hours`` is relative to the resource's CURRENT expiry (or now, if it has
    none), so extending something with 12h left by 24h leaves 36h.
    """
    inventory_ids: List[str] = Field(default_factory=list)
    extend_hours: Optional[int] = None
    expires_at: Optional[datetime] = None
    never: bool = False


def _load_visible(db: Session, user: User, ids: List[str]) -> List[dict]:
    """The caller's inventory rows for ``ids``, or raise.

    RBAC is ``inventory_service.visible_to`` — the same predicate the listing and the
    bulk-run endpoint use, so an id the caller cannot see comes back as *unknown* rather
    than *forbidden*. That non-leak property is deliberate (see ``plan_bulk_run``): a
    403 would confirm the resource exists.
    """
    ids = list(dict.fromkeys(ids or []))               # de-dupe, keep order
    if not ids:
        raise HTTPException(status_code=400, detail="No resources selected.")
    if len(ids) > MAX_TARGETS:
        raise HTTPException(
            status_code=400,
            detail=f"{len(ids)} resources selected; the limit for one request is "
                   f"{MAX_TARGETS}. Narrow the selection and try again.")

    accessible = inventory_service.accessible_workgroups(user)
    visible = {
        i["id"]: i for i in inventory_service.collect(db)
        if inventory_service.visible_to(i, accessible, user.username)
    }
    unknown = [i for i in ids if i not in visible]
    if unknown:
        raise HTTPException(
            status_code=404,
            detail=f"{len(unknown)} selected resource(s) are not in your inventory: "
                   f"{', '.join(sorted(unknown)[:5])}. Refresh and try again.")
    return [visible[i] for i in ids]


@router.get("/status")
async def get_status(current_user: User = Depends(get_current_user)) -> dict:
    """Feature state, thresholds and the un-lowerable floors — everything the pages need
    to render a timer and explain a clamp."""
    return expiry_reaper.status()


@router.post("/set")
async def set_expiry(
    payload: ExpirySetRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    """Set, extend or clear the auto-delete timer on the named resources.

    Authorization is visibility: anyone who can see a resource may extend it, because
    extending only ever DELAYS a deletion. That matches how ``api/jobs.py`` and
    ``api/k8s.py`` already treat ownerless rows, and avoids inventing a permission scope
    — a new entry in ``PERMISSION_SCOPES`` would silently strip this ability from every
    user who has explicit (non-NULL) permissions.

    Clearing a timer outright (``never``) is the exception: it defeats the feature, so it
    needs both an administrator and ``resource_expiry_allow_never``. That check lives in
    ``expiry_policy.resolve_expiry`` so the API and any future caller enforce one rule.
    """
    if not (payload.extend_hours or payload.expires_at or payload.never):
        raise HTTPException(
            status_code=400,
            detail="Specify one of extend_hours, expires_at or never.")

    items = _load_visible(db, current_user, payload.inventory_ids)
    result = expiry_reaper.set_expiry(
        db, items,
        extend_hours=payload.extend_hours,
        absolute=payload.expires_at,
        never=payload.never,
        is_admin=current_user.is_effective_admin,
        actor=current_user.username,
    )

    # The inventory listing is cached for 60s and nothing else invalidates it, so without
    # this an Extend shows the stale value and the operator clicks again.
    await cache_service.invalidate(cache_service.key_global("deployment_inventory"))

    if result["updated"] and not result["failed"]:
        result["ok"] = True
    else:
        result["ok"] = bool(result["updated"])
    return result


@router.get("/last-sweep")
async def last_sweep(current_user: User = Depends(require_admin)) -> dict:
    """The most recent sweep summary. Cached — does not trigger a pass. Returns
    ``{"never_run": true}`` before the background loop has completed one."""
    return expiry_reaper.get_last_sweep_result()


@router.post("/sweep")
async def force_sweep(
    force: bool = Query(False, description="Enqueue even if a pass is already active"),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
) -> dict:
    """Enqueue a sweep now instead of waiting for the next scheduled tick.

    Enqueues rather than running inline — unlike the cloud-identity force-sweep — because
    a sweep is the destructive path and everything destructive in this codebase runs in
    the worker, through the queue, with a job row behind it.

    ``force`` bypasses ONLY the "a pass is already queued or running" check. It does not
    bypass report-only mode, the arming delay, or the per-pass cap; those are what make
    the feature safe and no request may waive them.

    ``min_gap_seconds=0`` opts out of the enqueue's recency window. That window exists to
    collapse the two app workers' simultaneous *timer* ticks into one row; applying it here
    would refuse an operator's button for up to half an interval and report it as "already
    queued or running", which would be a plain lie. A human pressing Run Sweep means now.
    """
    from ..services import expiry_policy
    if not expiry_policy.enabled():
        raise HTTPException(
            status_code=400,
            detail="The auto-delete timer is disabled. Enable it in Settings first.")

    job_id = expiry_reaper.enqueue_sweep_if_due(db, min_gap_seconds=0)
    if job_id is None and force:
        from ..services import job_service
        job_id = job_service.create_job(
            db, job_type=expiry_reaper.SWEEP_JOB_TYPE,
            created_by=current_user.username).id
    if job_id is None:
        return {"ok": False, "job_id": None,
                "message": "A sweep is already queued or running."}
    logger.info("auto-delete sweep enqueued by %s (job %s)", current_user.username, job_id)
    return {"ok": True, "job_id": job_id, "message": "Sweep queued."}
