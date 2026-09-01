"""When a POV should be asleep: the suspend schedule, as pure policy.

Skytap suspends an environment on its own idle timer, and its docs call that the single
biggest lever on lab spend. No public cloud has one. So for a platform whose
``idle_suspend`` capability is False, the dashboard has to *be* the timer — and this
module is the part of that with no I/O in it, so the decision can be tested without a
clock, a database or a cloud.

**A schedule, not an inactivity timer.** "Idle" on a cloud has no honest definition from
outside the guest. A PRA session says nothing about a customer clicking around a console;
an agent heartbeat never stops; a page view is the SE, not the customer. Every candidate
signal has a blind spot that either leaves a POV running all month or suspends one
mid-demo. Business hours are a thing an operator can state, predict and explain on an
invoice.

**The rule is BOUNDARY CROSSED, not "should it be asleep right now?"** Those differ in
exactly the case that matters. An SE starts a POV by hand at 20:00 for a call with a
customer in another timezone; a state check would suspend it again on the next sweep, four
minutes later, forever. A boundary check leaves it alone until tomorrow's suspend time —
the operator's action wins until the schedule next has something new to say.

It also gets an outage right. If the dashboard was down from 18:00 to 22:00 and both a
suspend (19:00) and a resume (21:00) were missed, the later crossing wins and the POV ends
up where the schedule says it should be — rather than replaying both, or neither.

Pure: stdlib only, no app imports. ``pov_reconcile`` supplies the times and enqueues the
job; nothing here touches a platform.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# "HH:MM", 24-hour. A single format rather than a parser that accepts several: the value
# is rendered back into the same box it was typed in, and a field that accepts "7pm" and
# redisplays "19:00" reads as the dashboard having changed it.
_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

# Monday first, matching `datetime.weekday()`. Seven characters so the stored value is
# readable in a database client without a lookup table.
DAYS_ALL = "1111111"
DAYS_WEEKDAYS = "1111100"
DAY_LABELS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

# What a crossing asks for. These are the two runstates a cloud environment has, and the
# strings `pov_env_power` already takes.
SUSPEND = "stopped"
RESUME = "running"

# How far back a single evaluation will look. A row whose latch is older than this — the
# dashboard was off for a week, or the column was just backfilled — is not replayed; it is
# re-latched to now and left alone. Replaying a week-old boundary would power an
# environment on or off because of something that "happened" while nobody was watching,
# and the operator has no way to see why.
MAX_CATCHUP = timedelta(hours=25)


class ScheduleError(ValueError):
    """A schedule could not be understood. The message names the field."""


def parse_hhmm(value: str) -> tuple | None:
    """``(hour, minute)``, or None for a blank. Anything else is refused."""
    raw = (value or "").strip()
    if not raw:
        return None
    m = _HHMM_RE.match(raw)
    if not m:
        raise ScheduleError(f"{value!r} is not a time; use 24-hour HH:MM, e.g. 19:00")
    return int(m.group(1)), int(m.group(2))


def normalize_days(value: str) -> str:
    """Seven '0'/'1' characters, Monday first. Blank means every day."""
    raw = (value or "").strip()
    if not raw:
        return DAYS_ALL
    if len(raw) != 7 or set(raw) - {"0", "1"}:
        raise ScheduleError(
            "days must be seven characters of 0 or 1, Monday first — "
            f"{DAYS_WEEKDAYS!r} is Monday to Friday")
    if "1" not in raw:
        raise ScheduleError(
            "a schedule with no days selected would never do anything; clear the times "
            "instead to turn it off")
    return raw


def resolve_timezone(name: str):
    """The schedule's timezone, defaulting to UTC.

    UTC rather than the server's local zone: a container's local zone is an accident of
    its base image, and a POV suspending an hour early after a rebuild is the kind of bug
    nobody attributes to the right cause.
    """
    raw = (name or "").strip()
    if not raw:
        return timezone.utc
    try:
        return ZoneInfo(raw)
    except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
        raise ScheduleError(
            f"{raw!r} is not a timezone this system knows; use an IANA name such as "
            f"Europe/London or America/New_York") from exc


def validate(*, suspend_at: str, resume_at: str, tz_name: str, days: str) -> dict:
    """Refuse a schedule that cannot mean anything, and return the stored form.

    Called from the API before a write, so every refusal is a 4xx on the form rather than
    a surprise at 19:00 three weeks later.
    """
    suspend = parse_hhmm(suspend_at)
    resume = parse_hhmm(resume_at)
    if resume is not None and suspend is None:
        raise ScheduleError(
            "a resume time with no suspend time would wake a POV that nothing ever puts "
            "to sleep; set a suspend time too, or clear both")
    if suspend is not None and suspend == resume:
        raise ScheduleError(
            "the suspend and resume times are the same, so the schedule would both stop "
            "and start the POV at that moment")
    resolve_timezone(tz_name)
    normalized_days = normalize_days(days) if suspend is not None else ""
    return {
        "suspend_at_local": suspend_at.strip() if suspend is not None else None,
        "resume_at_local": resume_at.strip() if resume is not None else None,
        "schedule_timezone": (tz_name or "").strip() or None,
        "schedule_days": normalized_days or None,
    }


def has_schedule(row) -> bool:
    """A schedule exists only if it has a suspend time. A resume alone is not one."""
    return bool((getattr(row, "suspend_at_local", "") or "").strip())


def _boundaries(hhmm: tuple, tz, days: str, start_utc: datetime, end_utc: datetime):
    """Every occurrence of one daily time strictly after ``start_utc``, up to ``end_utc``.

    Computed by walking LOCAL days and converting each to UTC, never by adding 24 hours to
    a UTC instant: across a DST change those differ by an hour, and the schedule an
    operator typed is the local one.
    """
    hour, minute = hhmm
    out = []
    # One day either side, so a local midnight-adjacent boundary near the window edge is
    # not missed by the UTC-to-local date conversion.
    day = (start_utc.astimezone(tz).date() - timedelta(days=1))
    last = (end_utc.astimezone(tz).date() + timedelta(days=1))
    while day <= last:
        if days[day.weekday()] == "1":
            local = datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)
            at = local.astimezone(timezone.utc)
            if start_utc < at <= end_utc:
                out.append(at)
        day += timedelta(days=1)
    return out


def due_action(row, now_utc: datetime) -> str:
    """What the schedule wants done to ``row`` right now: ``""``, ``SUSPEND`` or ``RESUME``.

    ``""`` is by far the common answer — a boundary is crossed twice a day at most, and
    the sweep runs every ten minutes.

    Never acts on the first evaluation. A NULL latch means "never checked", and an
    unbounded window has crossed every boundary there has ever been; the caller records
    the time and moves on. Same shape as the auto-delete timer's arming rule, and for the
    same reason: enabling a thing must not make a backlog eligible at once.
    """
    if not has_schedule(row):
        return ""
    last = getattr(row, "schedule_last_checked_at", None)
    if last is None:
        return ""
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    if last >= now_utc:
        return ""
    if now_utc - last > MAX_CATCHUP:
        return ""

    tz = resolve_timezone(getattr(row, "schedule_timezone", "") or "")
    days = normalize_days(getattr(row, "schedule_days", "") or "")

    crossings = []
    suspend = parse_hhmm(getattr(row, "suspend_at_local", "") or "")
    if suspend is not None:
        crossings += [(at, SUSPEND) for at in _boundaries(suspend, tz, days, last, now_utc)]
    resume = parse_hhmm(getattr(row, "resume_at_local", "") or "")
    if resume is not None:
        crossings += [(at, RESUME) for at in _boundaries(resume, tz, days, last, now_utc)]

    if not crossings:
        return ""
    # The LAST crossing wins. After an outage that swallowed both, the environment should
    # end where the schedule says it should be now — not replay each in turn.
    crossings.sort()
    return crossings[-1][1]


def describe(row) -> dict:
    """The schedule as the POV page renders it.

    ``summary`` is a sentence rather than four fields, because the question an operator is
    actually asking the row is "when does this go to sleep?".
    """
    if not has_schedule(row):
        return {"scheduled": False, "summary": "", "suspend_at_local": "",
                "resume_at_local": "", "schedule_timezone": "", "schedule_days": ""}
    days = normalize_days(getattr(row, "schedule_days", "") or "")
    tz_name = (getattr(row, "schedule_timezone", "") or "").strip() or "UTC"
    suspend = (getattr(row, "suspend_at_local", "") or "").strip()
    resume = (getattr(row, "resume_at_local", "") or "").strip()
    if days == DAYS_ALL:
        when = "every day"
    elif days == DAYS_WEEKDAYS:
        when = "Mon–Fri"
    else:
        when = ", ".join(label for label, on in zip(DAY_LABELS, days) if on == "1")
    summary = f"Suspends {suspend} {when} ({tz_name})"
    summary += f", resumes {resume}" if resume else ", resume by hand"
    return {
        "scheduled": True,
        "summary": summary,
        "suspend_at_local": suspend,
        "resume_at_local": resume,
        "schedule_timezone": tz_name,
        "schedule_days": days,
    }
