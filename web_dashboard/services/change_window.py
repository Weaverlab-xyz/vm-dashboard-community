"""When a change is allowed to start: the change window, as pure policy.

``suspend_schedule`` answers "should this VM be asleep?"; this answers "when does the
next approved window open, and when does it close?". They are different questions with
the same arithmetic underneath, so this module BORROWS that one's primitives rather than
restating them — ``parse_hhmm``, ``normalize_days``, ``resolve_timezone`` and the local-
day walk are imported, not copied.

That reuse is the whole point of the module existing separately. The hard part of any
local-time schedule is DST, and ``suspend_schedule._boundaries`` already gets it right by
walking LOCAL days and converting each to UTC — never by adding 24 hours to a UTC instant,
because across a DST change those differ by an hour and the schedule an operator typed is
the local one. A second implementation would get that wrong eventually, and the symptom
would be a change running an hour outside its approved window twice a year.

**A window is a RECURRENCE resolved to an INSTANT at booking time, and then forgotten.**
``next_occurrence`` turns "every Saturday 02:00–06:00 New York" into one concrete pair of
naive UTC datetimes, which the caller copies onto the job row. Nothing re-reads the window
afterwards. So:

  * editing a window does not move changes already booked into it;
  * deleting one does not strand them;
  * a job that crosses a DST boundary between booking and running still runs at the wall-
    clock time the operator saw, because the conversion happened once, against the rules
    in force for that date.

Pure: stdlib plus ``suspend_schedule`` (itself stdlib-only). No database, no clock of its
own, no app imports — so every case below is testable without either.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .suspend_schedule import (  # noqa: F401 — DAY_LABELS/DAYS_* re-exported for the UI
    DAY_LABELS,
    DAYS_ALL,
    DAYS_WEEKDAYS,
    ScheduleError,
    normalize_days,
    parse_hhmm,
    resolve_timezone,
)

# A window must be long enough to be worth booking into and short enough to still be a
# window. One minute is the floor because a zero-length window can never be entered --
# the job would be missed the instant it was booked. A week is the ceiling because
# anything longer is not a change window, it is "whenever", and an operator who wants
# that should not be scheduling at all.
MIN_DURATION_MINUTES = 1
MAX_DURATION_MINUTES = 7 * 24 * 60

# How far ahead next_occurrence will look before giving up. A window with days selected
# recurs at least weekly, so anything beyond eight days means the day mask and the search
# disagree -- a bug, not a long wait. Bounded so a malformed row cannot spin forever.
_SEARCH_DAYS = 8


def validate(*, name: str, start_at_local: str, duration_minutes, tz_name: str,
             days: str) -> dict:
    """Refuse a window that cannot mean anything, and return the stored form.

    Called from the API before a write, so every refusal is a 4xx on the form rather than
    a change that silently never runs three weeks later. Same contract as
    ``suspend_schedule.validate``.
    """
    clean_name = (name or "").strip()
    if not clean_name:
        raise ScheduleError("a change window needs a name — it is how operators pick it")

    if parse_hhmm(start_at_local) is None:
        raise ScheduleError("a change window needs a start time; use 24-hour HH:MM, "
                            "e.g. 02:00")
    try:
        minutes = int(duration_minutes)
    except (TypeError, ValueError):
        raise ScheduleError(
            f"{duration_minutes!r} is not a number of minutes") from None
    if minutes < MIN_DURATION_MINUTES:
        raise ScheduleError(
            "a window of zero length could never be entered — every change booked into "
            "it would be missed the moment it was booked")
    if minutes > MAX_DURATION_MINUTES:
        raise ScheduleError(
            f"a window longer than a week is not a change window; "
            f"the maximum is {MAX_DURATION_MINUTES} minutes (7 days)")

    resolve_timezone(tz_name)              # raises with a helpful message if unknown
    return {
        "name": clean_name,
        "start_at_local": start_at_local.strip(),
        "duration_minutes": minutes,
        "timezone": (tz_name or "").strip() or None,
        "schedule_days": normalize_days(days),
    }


def next_occurrence(window, after_utc: datetime) -> tuple:
    """``(start_utc, end_utc)`` for the first occurrence that a job could still use.

    Both are NAIVE UTC, matching every datetime column in this application — the database
    stores ``datetime.utcnow()`` throughout, and handing back an aware value would raise
    on the first comparison against one of them.

    "Could still use" means the window has not already CLOSED, not that it has not yet
    opened. Booking into a window that is open right now is the common case for "run this
    in the current maintenance period", and refusing it would send the change a week
    away for no reason. A window that closed an hour ago is skipped.

    Computed by walking LOCAL days through ``suspend_schedule``'s primitives, so a window
    on the night the clocks change lands at the wall-clock time the operator typed.
    """
    tz = resolve_timezone(getattr(window, "timezone", "") or "")
    days = normalize_days(getattr(window, "schedule_days", "") or "")
    hhmm = parse_hhmm(getattr(window, "start_at_local", "") or "")
    if hhmm is None:
        raise ScheduleError("this change window has no start time")
    duration = timedelta(minutes=int(getattr(window, "duration_minutes", 0) or 0))
    if duration <= timedelta(0):
        raise ScheduleError("this change window has no duration")

    hour, minute = hhmm
    after = _as_aware(after_utc)

    # Start a day early: a window that began yesterday LOCAL time can still be open now
    # (a 6-hour window from 22:00 runs past local midnight), and that occurrence is the
    # one an operator means by "the current window".
    day = after.astimezone(tz).date() - timedelta(days=1)
    for _ in range(_SEARCH_DAYS + 1):
        if days[day.weekday()] == "1":
            local = datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)
            start = local.astimezone(timezone.utc)
            end = start + duration
            if end > after:
                return _naive(start), _naive(end)
        day += timedelta(days=1)
    raise ScheduleError(
        "this change window has no occurrence in the next week — check its selected days")


def occurrence_covering(window, at_utc: datetime) -> bool:
    """Is ``at_utc`` inside an occurrence of this window?

    Used to police a hand-typed time against a named window, so "run at 03:00 in the
    Saturday window" cannot be accepted when 03:00 falls on a Tuesday.
    """
    try:
        start, end = next_occurrence(window, at_utc - timedelta(seconds=1))
    except ScheduleError:
        return False
    at = _strip(at_utc)
    return start <= at < end


def describe(window) -> dict:
    """The window as a form and a job page render it.

    ``summary`` is a sentence rather than four fields, because the question being asked
    is "when can I change things?" and the answer is a sentence.
    """
    days = normalize_days(getattr(window, "schedule_days", "") or "")
    tz_name = (getattr(window, "timezone", "") or "").strip() or "UTC"
    start = (getattr(window, "start_at_local", "") or "").strip()
    minutes = int(getattr(window, "duration_minutes", 0) or 0)
    if days == DAYS_ALL:
        when = "every day"
    elif days == DAYS_WEEKDAYS:
        when = "Mon–Fri"
    else:
        when = ", ".join(label for label, on in zip(DAY_LABELS, days) if on == "1")
    return {
        "name": getattr(window, "name", ""),
        "summary": f"{when} {start}–{_end_label(start, minutes)} ({tz_name}), "
                   f"{format_duration(minutes)}",
        "start_at_local": start,
        "duration_minutes": minutes,
        "timezone": tz_name,
        "schedule_days": days,
        "enabled": getattr(window, "enabled", True) is not False,
    }


def format_duration(minutes: int) -> str:
    """"4h", "90m", "4h 30m" — for a label, not for parsing back."""
    minutes = int(minutes or 0)
    hours, mins = divmod(minutes, 60)
    if hours and mins:
        return f"{hours}h {mins}m"
    if hours:
        return f"{hours}h"
    return f"{mins}m"


def _end_label(start_at_local: str, minutes: int) -> str:
    """The window's local end time, for display only.

    Wall-clock arithmetic on purpose: this is a label under the start time an operator
    typed, not an instant. A window crossing local midnight is marked so the label cannot
    be misread as ending earlier the same morning.
    """
    hhmm = parse_hhmm(start_at_local)
    if hhmm is None:
        return "?"
    total = hhmm[0] * 60 + hhmm[1] + int(minutes or 0)
    day_offset, into_day = divmod(total, 24 * 60)
    hour, minute = divmod(into_day, 60)
    label = f"{hour:02d}:{minute:02d}"
    return f"{label} (+{day_offset}d)" if day_offset else label


def _as_aware(value: datetime) -> datetime:
    """Treat a naive datetime as UTC, which is what every column in this app stores."""
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _naive(value: datetime) -> datetime:
    """Back to the naive UTC the database speaks."""
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _strip(value: datetime) -> datetime:
    return _naive(value) if value.tzinfo is not None else value
