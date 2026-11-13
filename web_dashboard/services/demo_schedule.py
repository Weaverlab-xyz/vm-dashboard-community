"""Book a demo cell for later, and give it an end time.

A Demo Presenter builds a cell for a meeting. They want it READY when the meeting starts
and GONE when it ends, without remembering to do either. Both halves already exist as
generic Job machinery; this module only decides what to hand them:

* **Build later** is ``Job.scheduled_for`` on the ``ot_cell_deploy`` PARENT (the one row the
  worker claims). The queued children are never claimable, so only the parent carries the
  booking. ``change_window_service.schedule_kwargs`` is the one place that parses it.
* **Destroy at the end** is ``demo_end_at`` on the cell's deploy row plus an ``expires_at``
  stamped to the same instant, and ``sweep`` below, which the change-window sweep runs
  every few minutes. It is NOT a destroy job booked at build time, because what a destroy
  needs (an EC2 instance id) does not exist until the build has finished.

``sweep`` rather than the auto-delete reaper, for three reasons: the reaper is a global
switch an operator may have off, it has a 60-minute floor and a grace period that suit
forgotten VMs and not a meeting that ends at 15:00, and it books its destroy as
``expiry-reaper`` where a presenter's cell should keep the presenter's name. The two
share ``expires_at`` and the same at-most-once breadcrumbs, so whichever gets there first
leaves nothing for the other.
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from ..database import Job
from . import change_window_service, expiry_policy, job_service
from ..utils.clock import utcnow

logger = logging.getLogger(__name__)

#: Metadata key on a cell's deploy row: the UTC instant it should be destroyed.
END_KEY = "demo_end_at"
#: ... and the instant a booked cell is due to start building (absent for "now").
START_KEY = "demo_start_at"

#: A broker is destroyed after its cell, never beside it: the cell's teardown is the one
#: that destroys the plant's Entitle agent token (api/demo.destroy_ot_cell says why).
BROKER_DELAY = timedelta(minutes=2)

_DEPLOY_TYPES = ("gce_deploy", "ec2_deploy", "azure_deploy")
_BATCH = 50


@dataclass
class Plan:
    """What a deploy route splats into its ``create_job`` calls."""
    sched: dict = field(default_factory=dict)     # for the PARENT only
    start: Optional[datetime] = None              # None = now
    end: Optional[datetime] = None                # None = no demo end time
    expires_at: Optional[datetime] = None         # for every VM row; None = policy default
    meta: dict = field(default_factory=dict)      # merged into every VM row's metadata


def plan(db: Session, payload, *, vm_job_type: str, workgroup: str, is_admin: bool) -> Plan:
    """Resolve a request's booking and TTL. Raises ``HTTPException(400)`` on a bad one.

    The end time is measured from when the cell is BOOKED to start, so provisioning time
    counts against it: a presenter who says "8 hours" means it is gone 8 hours after it
    was due, not 8 hours after a slow build finally finished.
    """
    sched = change_window_service.schedule_kwargs(db, **payload.schedule_fields())
    start = sched.get("scheduled_for")
    p = Plan(sched=sched, start=start)
    if start is not None:
        # The OT cell's VM row is not the claimable one, so it cannot say when it will be
        # built; the hub reads the booking off this.
        p.meta[START_KEY] = start.isoformat()
    ttl = getattr(payload, "ttl_hours", None)
    if ttl:
        hours = expiry_policy.clamp_hours(ttl, is_admin=is_admin)
        p.end = expiry_policy.expiry_from_now(hours, now=start)
        p.expires_at = p.end
        p.meta[END_KEY] = p.end.isoformat()
    elif start is not None:
        # No TTL asked for: the policy default, but counted from the booked start so a
        # cell booked for Saturday is not born expired (job_service documents the trap).
        try:
            p.expires_at = expiry_policy.default_expiry_for(
                vm_job_type, workgroup=workgroup, now=start)
        except Exception:  # noqa: BLE001 — a timer must never block a deploy
            p.expires_at = None
    return p


def set_end(db: Session, row: Job, end: Optional[datetime],
            *, fallback_expiry: Optional[datetime] = None) -> None:
    """Set, move or clear the end time on a cell's deploy row and its broker.

    ``expires_at`` is moved with it so the auto-delete reaper and this module can never
    disagree about when the cell dies. Clearing leaves the cell with no timer at all
    (a presenter who wants it to stay), which is what an admin's "never" does elsewhere.
    ``fallback_expiry`` is what a cleared end time falls back to: the policy default.
    """
    rows = [row]
    broker_id = (row.metadata_dict or {}).get("ot_broker_job_id") or ""
    if broker_id:
        broker = db.query(Job).filter(Job.id == broker_id).first()
        if broker is not None:
            rows.append(broker)
    for r in rows:
        meta = r.metadata_dict or {}
        if end is None:
            meta.pop(END_KEY, None)
        else:
            meta[END_KEY] = end.isoformat()
        r.metadata_dict = meta
        r.expires_at = end if end is not None else fallback_expiry
    db.commit()


def _due(db: Session, now: datetime) -> list:
    return (db.query(Job)
            .filter(Job.job_type.in_(_DEPLOY_TYPES),
                    Job.status == "completed",
                    Job.expires_at.isnot(None), Job.expires_at <= now,
                    Job.extra_data.like(f'%"{END_KEY}"%'))
            .order_by(Job.expires_at.asc())
            .limit(_BATCH)
            .all())


def has_work(db: Session) -> bool:
    """Is any built cell with an end time still waiting to be destroyed?"""
    return (db.query(Job.id)
            .filter(Job.job_type.in_(_DEPLOY_TYPES),
                    Job.status == "completed",
                    Job.expires_at.isnot(None),
                    Job.extra_data.like(f'%"{END_KEY}"%'))
            .first()) is not None


def sweep(db: Session, now: Optional[datetime] = None) -> list:
    """Destroy every cell whose end time has passed. Returns ``[(name, destroy_job_id)]``.

    Each destroy is the SAME job row the cloud's Destroy button creates (same builder the
    reaper uses), owned by the cell's creator. One cell failing must not stop the rest.
    """
    now = now or utcnow()
    out = []
    for row in _due(db, now):
        try:
            meta = row.metadata_dict or {}
            if meta.get("destroyed"):
                row.expires_at = None
                db.commit()
                continue
            dtype, payload = expiry_policy.build_destroy_metadata(row.job_type, meta, row.id)
            if dtype is None:
                # Say why on the row and stop retrying every pass; the reaper does the same.
                meta["demo_end_refused"] = str(payload)
                row.metadata_dict = meta
                row.expires_at = None
                db.commit()
                logger.warning("demo end: %s not destroyed: %s", row.id, payload)
                continue
            later = BROKER_DELAY if meta.get("ot_broker") else None
            kwargs = {"scheduled_for": now + later} if later else {}
            destroy = job_service.create_job(
                db, job_type=dtype, created_by=row.created_by or "demo-schedule",
                workgroup=row.workgroup, metadata=payload, **kwargs)
            fresh = db.query(Job).filter(Job.id == row.id).first()
            fresh.expires_at = None                         # at-most-once, as the reaper does
            bc = fresh.metadata_dict or {}
            bc["demo_end_job_id"] = destroy.id
            bc["demo_end_fired_at"] = now.isoformat()
            fresh.metadata_dict = bc
            db.commit()
            out.append((meta.get("instance_name") or meta.get("vm_name") or row.id, destroy.id))
        except Exception:  # noqa: BLE001 — isolate one bad row
            logger.warning("demo end: could not destroy %s", row.id, exc_info=True)
            db.rollback()
    return out
