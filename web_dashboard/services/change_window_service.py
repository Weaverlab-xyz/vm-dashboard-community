"""Change windows, where they meet the database.

``change_window`` is the pure policy — no clock, no session, no app imports, so every DST
and day-mask case is testable on its own. This module is the thin layer that reads and
writes rows and turns an operator's choice on a form into the two instants stamped onto a
job. Split for the same reason ``expiry_policy`` and ``expiry_reaper`` are, and pinned the
same way: the policy module must stay importable with nothing installed.

The one function worth reading before using any of this is :func:`resolve`. Everything a
run form needs — "now", "at this time", "in the Saturday window" — comes back from it as
the same ``(scheduled_for, window_ends_at, change_window_id)`` triple, so a caller adding
scheduling to a new page has one thing to call and one shape to pass to ``create_job``.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from sqlalchemy.orm import Session

from ..database import ChangeWindow
from . import change_window
from .suspend_schedule import ScheduleError

logger = logging.getLogger(__name__)

# How far ahead a hand-typed time may be booked. Two years is not a limit anybody will
# meet by accident; it exists so a fat-fingered year ("2206") is refused at the form
# instead of creating a job row that sits pending forever and quietly holds a worker
# slot's worth of queue depth in every listing.
MAX_LEAD_DAYS = 730

# How long a bare "run at this time" booking stays eligible before it counts as missed.
#
# A hand-typed time has no window of its own, but it still needs an end: without one,
# "skip and mark missed" has nothing to compare against and a job delayed by a worker
# outage would run whenever capacity appeared, which is the behaviour change windows
# exist to prevent. An hour is long enough to absorb a queue backed up behind a long
# apply, and short enough that a change which slipped an hour is worth a human's
# attention rather than silent execution.
DEFAULT_GRACE_MINUTES = 60


def _utcnow() -> datetime:
    return datetime.utcnow()


def grace_minutes() -> int:
    """The implicit window length for an ad-hoc "run at" booking. Read live."""
    try:
        from . import config_service
        value = int(config_service.get("change_window_grace_minutes", "")
                    or DEFAULT_GRACE_MINUTES)
    except Exception:  # noqa: BLE001 — a config blip must not break a booking
        value = DEFAULT_GRACE_MINUTES
    return max(1, value)


def approval_required_default() -> bool:
    """Whether newly scheduled changes need a second person by default."""
    try:
        from . import config_service
        return config_service.get_bool("change_approval_required", False)
    except Exception:  # noqa: BLE001
        return False


def allow_self_approval() -> bool:
    """Whether the person who booked a change may approve it.

    Default False, and that default is the entire value of the feature: an approval the
    requester can grant themselves is not an approval. Exists as a setting only because a
    single-operator install would otherwise be unable to use windows at all.
    """
    try:
        from . import config_service
        return config_service.get_bool("change_approval_allow_self", False)
    except Exception:  # noqa: BLE001
        return False


# ── Reads ─────────────────────────────────────────────────────────────────────

def get(db: Session, window_id: str) -> Optional[ChangeWindow]:
    if not window_id:
        return None
    return db.query(ChangeWindow).filter(ChangeWindow.id == window_id).first()


def list_windows(db: Session, *, enabled_only: bool = False) -> list:
    rows = db.query(ChangeWindow).order_by(ChangeWindow.name.asc()).all()
    if enabled_only:
        rows = [r for r in rows if r.enabled is not False]
    return rows


def names_for(db: Session, jobs: Iterable) -> dict:
    """``{change_window_id: name}`` for a page of jobs, in ONE query.

    Resolved by id rather than joined, because ``Job.change_window_id`` is deliberately
    not a ForeignKey — a window may have been deleted since a job was booked into it, and
    that job must still render. A missing id simply has no entry here, and the job shows
    its concrete times without a name.
    """
    ids = {getattr(j, "change_window_id", None) for j in jobs}
    ids.discard(None)
    if not ids:
        return {}
    try:
        rows = (db.query(ChangeWindow.id, ChangeWindow.name)
                .filter(ChangeWindow.id.in_(ids)).all())
        return {r[0]: r[1] for r in rows}
    except Exception:  # noqa: BLE001 — a label must never break the jobs list
        logger.warning("could not resolve change window names", exc_info=True)
        return {}


def options(db: Session) -> list:
    """Enabled windows, described, with their next occurrence — what a run form renders.

    A window whose recurrence cannot be resolved is returned WITHOUT a next occurrence
    rather than dropped. Silently omitting it would leave an operator staring at a form
    missing the window they were told to use, with nothing to explain why.
    """
    out = []
    for row in list_windows(db, enabled_only=True):
        entry = {"id": row.id}
        try:
            entry.update(change_window.describe(row))
        except ScheduleError as exc:
            entry.update({"name": row.name, "summary": f"misconfigured: {exc}"})
            out.append(entry)
            continue
        try:
            start, end = change_window.next_occurrence(row, _utcnow())
            entry["next_start"] = start.isoformat()
            entry["next_end"] = end.isoformat()
        except ScheduleError as exc:
            entry["next_error"] = str(exc)
        out.append(entry)
    return out


# ── The one function a run form calls ─────────────────────────────────────────

def resolve(db: Session, *, run_at: str = "", run_timezone: str = "",
            change_window_id: str = "", now: Optional[datetime] = None) -> tuple:
    """Turn a form's scheduling choice into ``(scheduled_for, window_ends_at, window_id)``.

    All three are None/empty for "run now", which is what every caller gets when it
    passes nothing — so adding this to an endpoint cannot change the behaviour of an
    unscheduled run.

    Three modes, in the order they are checked:

    * **a named window** — its next occurrence, both instants copied onto the job;
    * **a hand-typed time** — that instant, plus an implicit grace window, because
      "skip and mark missed" needs an end to compare against (see DEFAULT_GRACE_MINUTES);
    * **neither** — run now.

    Naming both a window and a time is not an error: the time is used, and checked
    against the window, so "the 03:00 slot of the Saturday window" works and "03:00 on a
    Tuesday, in the Saturday window" is refused. Silently preferring one over the other
    would let an operator book a change into a window it does not fall inside.

    Raises :class:`ScheduleError` with an operator-facing message. Callers turn it into a
    400 — a refusal on the form now, rather than a change that quietly never runs.
    """
    now = now or _utcnow()
    window = get(db, change_window_id) if change_window_id else None
    if change_window_id and window is None:
        raise ScheduleError("that change window no longer exists; pick another")
    if window is not None and window.enabled is False:
        raise ScheduleError(
            f"the change window {window.name!r} is disabled and cannot take new changes")

    at = _parse_run_at(run_at, run_timezone) if (run_at or "").strip() else None

    if at is None and window is not None:
        start, end = change_window.next_occurrence(window, now)
        return start, end, window.id

    if at is None:
        return None, None, None

    _check_lead(at, now)

    if window is not None:
        if not change_window.occurrence_covering(window, at):
            raise ScheduleError(
                f"{_label(at)} UTC does not fall inside the {window.name!r} window "
                f"({change_window.describe(window)['summary']}) — pick a time inside it, "
                f"or clear the window to schedule a one-off change")
        _start, end = change_window.next_occurrence(window, at - timedelta(seconds=1))
        return at, end, window.id

    return at, at + timedelta(minutes=grace_minutes()), None


def _parse_run_at(run_at: str, tz_name: str) -> datetime:
    """A browser-local ``datetime-local`` value plus an IANA zone, as naive UTC.

    The browser sends wall-clock text with no offset ("2026-10-04T02:00"), so the zone
    has to come alongside it. Interpreting it as UTC instead would silently shift every
    booking by the operator's offset — a change booked for 02:00 running at 21:00 the
    previous evening, with nothing in the UI to show why.
    """
    raw = (run_at or "").strip().replace(" ", "T")
    # Tolerate seconds and a trailing Z, both of which some browsers include.
    if raw.endswith("Z"):
        raw = raw[:-1]
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        raise ScheduleError(
            f"{run_at!r} is not a date and time; expected YYYY-MM-DDTHH:MM") from None
    if parsed.tzinfo is not None:
        return parsed.astimezone(timezone.utc).replace(tzinfo=None)
    tz = change_window.resolve_timezone(tz_name)
    return parsed.replace(tzinfo=tz).astimezone(timezone.utc).replace(tzinfo=None)


def schedule_kwargs(db: Session, *, run_at: str = "", run_timezone: str = "",
                    change_window_id: str = "") -> dict:
    """The scheduling arguments for ``create_job``, or ``{}`` for an immediate run.

    The ONE thing a run form calls. Every page that creates a job takes the same three
    fields off its request model, passes them here, and splats the result into
    ``create_job``:

        job_service.create_job(db, job_type=..., ...,
                               **change_window_service.schedule_kwargs(db, **sched))

    Returning an EMPTY DICT rather than a dict of Nones is the load-bearing detail.
    Splatted into ``create_job`` it is then byte-for-byte the call that was there
    before, so adding scheduling to a page cannot change what an unscheduled run does
    — which is the property that makes rolling this out across a dozen forms safe.

    Raises ``HTTPException(400)`` rather than ``ScheduleError`` because every caller is
    an HTTP route and each would otherwise write the same three-line translation. The
    messages are already operator-facing ("02:00 does not fall inside the Prod Weekend
    window…"), so a refusal lands on the form at submit time rather than becoming a
    change that quietly never runs.

    **Approval applies to SCHEDULED changes only.** An immediate run is an operator
    doing something now, and is unchanged. Requiring approval for those would gate
    every button in the application — a different and much larger feature. The gate
    here is "a change booked for later needs a second person before it fires".
    """
    try:
        scheduled_for, window_ends_at, window_id = resolve(
            db, run_at=run_at, run_timezone=run_timezone,
            change_window_id=change_window_id)
    except ScheduleError as exc:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail=str(exc))
    if scheduled_for is None:
        return {}
    return {
        "scheduled_for": scheduled_for,
        "window_ends_at": window_ends_at,
        "change_window_id": window_id,
        "approval_required": approval_required_default(),
    }


def _check_lead(at: datetime, now: datetime) -> None:
    if at <= now:
        raise ScheduleError(
            f"{_label(at)} UTC is in the past — to run this change immediately, "
            f"choose Run now")
    if at > now + timedelta(days=MAX_LEAD_DAYS):
        raise ScheduleError(
            f"{_label(at)} UTC is more than {MAX_LEAD_DAYS // 365} years away; "
            f"check the year")


def _label(value: datetime) -> str:
    return value.isoformat(sep=" ", timespec="minutes")
