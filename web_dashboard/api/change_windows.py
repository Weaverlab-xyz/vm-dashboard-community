"""Change windows: the maintenance calendar, and the approval gate on it.

Two surfaces here, deliberately on one router because they share one authority:

* **the windows themselves** (``change_windows:write``) — named recurring periods an
  administrator defines once and every run form then offers by name;
* **approving a change booked into one** (``change_windows:use``) — a second person
  signing off before the job becomes eligible to run.

They are separate LEVELS rather than separate scopes because they are the same calendar
seen from two sides, but they are not the same right: maintaining the maintenance
calendar and signing off a production change are different jobs in most organisations,
and collapsing them would make the gate meaningless.

**Every route here uses ``require_explicit_permission``, not ``require_permission``.**
That is the correct form for an authority being delegated out of admin: the permissive
variant treats an empty permission map as unrestricted, which would silently hand change
approval to every legacy pre-OIDC user on the install. See ``api/auth.has_permission``
and ``docs/permissions.md``. No backfill exists for this scope for the same reason —
every route below is new, so there is no prior access to preserve.

Reading the window LIST is not gated on this scope. The run forms need it to render the
"Next window" picker, and gating it would mean Config Management suddenly required a
second permission — the silent-revocation trap the permissions doc warns about. Knowing
that a maintenance window exists is not sensitive; changing one is.
"""
import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import ChangeWindow, User, get_db
from ..services import change_window, change_window_service, job_service
from ..services.suspend_schedule import ScheduleError
from .auth import get_current_user, require_explicit_permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/change-windows", tags=["change-windows"])

_WRITE = Depends(require_explicit_permission("change_windows", "write"))
_APPROVE = Depends(require_explicit_permission("change_windows", "use"))


class ChangeWindowRequest(BaseModel):
    name: str
    start_at_local: str            # "HH:MM", local to `timezone`
    duration_minutes: int
    timezone: str = ""             # IANA; blank = UTC
    schedule_days: str = ""        # 7 chars, Monday first; blank = every day
    description: str = ""
    enabled: bool = True


def _out(row: ChangeWindow) -> dict:
    """One window, described. Never raises on a misconfigured row — a window that
    cannot be resolved must still be listable, or an administrator has no way to find
    and fix the one that is broken."""
    body = {"id": row.id, "description": row.description or "",
            "created_by": row.created_by or ""}
    try:
        body.update(change_window.describe(row))
    except ScheduleError as exc:
        body.update({"name": row.name, "summary": f"misconfigured: {exc}",
                     "enabled": row.enabled is not False})
    return body


@router.get("")
def list_change_windows(
    enabled_only: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Every change window, with its next occurrence.

    Authenticated but not scope-gated — see the module docstring. ``enabled_only`` is
    what a run form asks for; the Settings page wants the disabled ones too.
    """
    if enabled_only:
        return {"windows": change_window_service.options(db)}
    return {"windows": [_out(r) for r in change_window_service.list_windows(db)]}


@router.post("", status_code=201, dependencies=[_WRITE])
def create_change_window(
    payload: ChangeWindowRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        fields = change_window.validate(
            name=payload.name, start_at_local=payload.start_at_local,
            duration_minutes=payload.duration_minutes, tz_name=payload.timezone,
            days=payload.schedule_days)
    except ScheduleError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if _by_name(db, fields["name"]) is not None:
        # Named, not silently reused: a window is chosen by name on every run form, and
        # two called "Prod Weekend" would be a coin flip over which one a change went
        # into.
        raise HTTPException(
            status_code=409,
            detail=f"a change window called {fields['name']!r} already exists")

    row = ChangeWindow(**fields, description=payload.description.strip() or None,
                       enabled=bool(payload.enabled),
                       created_by=current_user.username)
    db.add(row)
    db.commit()
    db.refresh(row)
    job_service.log_audit(db, current_user.username, "change_window_create",
                          details={"name": row.name, "id": row.id})
    return _out(row)


@router.put("/{window_id}", dependencies=[_WRITE])
def update_change_window(
    window_id: str,
    payload: ChangeWindowRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Edit a window.

    This does NOT move changes already booked into it. Those rows carry their own
    resolved instants, copied at booking time — see ``database.ChangeWindow``. Editing
    a window that people have already scheduled against therefore affects only future
    bookings, which is the only behaviour that can be explained to an operator.
    """
    row = change_window_service.get(db, window_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Change window not found")
    try:
        fields = change_window.validate(
            name=payload.name, start_at_local=payload.start_at_local,
            duration_minutes=payload.duration_minutes, tz_name=payload.timezone,
            days=payload.schedule_days)
    except ScheduleError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    clash = _by_name(db, fields["name"])
    if clash is not None and clash.id != row.id:
        raise HTTPException(
            status_code=409,
            detail=f"a change window called {fields['name']!r} already exists")

    for key, value in fields.items():
        setattr(row, key, value)
    row.description = payload.description.strip() or None
    row.enabled = bool(payload.enabled)
    db.commit()
    db.refresh(row)
    job_service.log_audit(db, current_user.username, "change_window_update",
                          details={"name": row.name, "id": row.id})
    return _out(row)


@router.delete("/{window_id}", dependencies=[_WRITE])
def delete_change_window(
    window_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Delete a window.

    Jobs booked into it are left alone and keep running to the times stamped on them;
    they simply lose the name in the UI and show their concrete window instead. That is
    why ``Job.change_window_id`` is not a ForeignKey — tidying up the calendar must never
    mutate or cascade into job history.

    A window with changes still pending against it refuses, because deleting it would
    remove the operator's own route to understanding why those jobs are waiting.
    Disabling is the way to retire one.
    """
    row = change_window_service.get(db, window_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Change window not found")

    from ..database import Job
    pending = (db.query(Job.id)
               .filter(Job.change_window_id == window_id,
                       Job.status.in_(job_service.ACTIVE_STATUSES))
               .count())
    if pending:
        raise HTTPException(
            status_code=409,
            detail=(f"{pending} change(s) are still booked into {row.name!r}. Disable "
                    f"the window instead, or wait for them to run or be cancelled."))

    name = row.name
    db.delete(row)
    db.commit()
    job_service.log_audit(db, current_user.username, "change_window_delete",
                          details={"name": name, "id": window_id})
    return {"deleted": window_id}


# ── The approval gate ─────────────────────────────────────────────────────────

class RescheduleRequest(BaseModel):
    run_at: str = ""
    run_timezone: str = ""
    change_window_id: str = ""


@router.post("/approvals/{job_id}", dependencies=[_APPROVE])
def approve_change(
    job_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Approve one scheduled change, making it eligible to run in its window.

    Refuses SELF-approval unless an administrator has explicitly allowed it. A gate the
    requester can clear themselves is not a gate, and the setting exists only so a
    single-operator install can use windows at all.

    Idempotent: approving an already-approved change returns the existing approval
    rather than rewriting who granted it. Two people pressing the button must not make
    the second one the approver of record.
    """
    job = job_service.get_job(db, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if not job.approval_required:
        raise HTTPException(status_code=400,
                            detail="this change does not require approval")
    if job.approved_at:
        return {"job_id": job.id, "approved_at": job.approved_at,
                "approved_by": job.approved_by, "already": True}
    if job.status not in ("queued", "pending"):
        raise HTTPException(
            status_code=409,
            detail=f"this change is {job.status} and can no longer be approved")
    if (job.created_by == current_user.username
            and not change_window_service.allow_self_approval()):
        raise HTTPException(
            status_code=403,
            detail=("you raised this change, so you cannot approve it. Ask someone "
                    "with the change-approval permission to review it."))

    job.approved_at = datetime.utcnow()
    job.approved_by = current_user.username
    db.commit()
    job_service.log_audit(db, current_user.username, "change_approved",
                          details={"job_id": job.id, "job_type": job.job_type,
                                   "requested_by": job.created_by,
                                   "scheduled_for": job.scheduled_for.isoformat()
                                   if job.scheduled_for else ""})
    return {"job_id": job.id, "approved_at": job.approved_at,
            "approved_by": job.approved_by, "already": False}


def _by_name(db: Session, name: str) -> Optional[ChangeWindow]:
    return db.query(ChangeWindow).filter(ChangeWindow.name == name).first()
