"""A one-off "have it running before my demo": the warm-up booking on a POV.

The recurring suspend schedule answers "when does this go to sleep every night?". Before
this, "start it at 13:40 on Thursday for a 14:00 call" could only be said by moving that
schedule's resume time and remembering to move it back -- and on Skytap, which refuses a
dashboard schedule because it has its own idle timer, it could not be said at all. An SE
woke the lab by hand and hoped it had finished resuming when the customer joined.

**It only ever starts a lab.** That is why it may run where the recurring schedule may not:
the reason Skytap refuses a schedule is that the dashboard and the idle timer would fight
over suspending, and a single start fights nothing. It never suspends anything afterwards;
the platform's idle timer, the recurring schedule or a person does that, exactly as they
would after somebody pressed Start.

**It fires at most once.** The sweep clears ``wake_at`` the first time it evaluates the row
at or after that moment, whether it had to act or not -- already running, a power job in
flight, or a pass so late the demo is over. A booking that could fire twice is one that
starts a lab an SE has since suspended on purpose.

**Precision is the reconcile interval.** The decision rides ``pov_reconcile``'s pass, which
runs every ten minutes by default, so a 20-minute lead is really 10 to 20. :func:`warnings`
says so rather than letting an SE find out on the call.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from . import suspend_schedule

DEFAULT_LEAD_MINUTES = 20
MAX_LEAD_MINUTES = 240
MAX_AHEAD = timedelta(days=60)
# After the demo started plus this, a booking the sweep never reached is dropped rather
# than acted on: a lab started for a call that already ended is only cost.
STALE_AFTER = timedelta(hours=1)
# How long a demo is assumed to run, for the "will something suspend it mid-call?" check.
DEMO_LENGTH = timedelta(hours=2)

META_KEY = "wake"
WAKE_ACTOR = "pov-warmup"

START, MISSED = "start", "missed"


class WakeError(ValueError):
    """A booking that cannot mean anything. The message is shown to the SE."""


def _naive_utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def plan(demo_at_local: str, tz_name: str, lead_minutes: int | None, *,
         now: datetime | None = None) -> tuple[datetime, datetime, int]:
    """``(wake_at, demo_at, lead_minutes)`` in naive UTC from what the form sent.

    ``demo_at_local`` is the browser's ``datetime-local`` value, ``YYYY-MM-DDTHH:MM``, in
    ``tz_name`` (IANA; blank is UTC). Raises :class:`WakeError`.
    """
    tz = suspend_schedule.resolve_timezone(tz_name)
    try:
        local = datetime.fromisoformat((demo_at_local or "").strip())
    except ValueError:
        raise WakeError("the demo time must look like 2026-10-22T14:00") from None
    if local.tzinfo is None:
        local = local.replace(tzinfo=tz)
    lead = DEFAULT_LEAD_MINUTES if lead_minutes is None else int(lead_minutes)
    if not 0 <= lead <= MAX_LEAD_MINUTES:
        raise WakeError(f"the lead must be between 0 and {MAX_LEAD_MINUTES} minutes")
    now_utc = now or datetime.now(timezone.utc).replace(tzinfo=None)
    demo_at = _naive_utc(local)
    wake_at = demo_at - timedelta(minutes=lead)
    if demo_at <= now_utc:
        raise WakeError("that demo time has already passed")
    if demo_at - now_utc > MAX_AHEAD:
        raise WakeError(f"book a warm-up at most {MAX_AHEAD.days} days ahead")
    # A lead that reaches back past now is fine: the next pass starts it.
    return max(wake_at, now_utc), demo_at, lead


def warnings(env, wake_at: datetime, demo_at: datetime, *, interval_s: int,
             idle_suspend: bool) -> list[str]:
    """Why this booking may not deliver a running lab for the whole call."""
    out = []
    lead_s = (demo_at - wake_at).total_seconds()
    if lead_s < interval_s:
        out.append(f"The dashboard checks bookings every {interval_s // 60} min, so the "
                   f"lab may start up to that late. A lead of at least "
                   f"{max(1, interval_s // 60) * 2} min leaves it time to resume.")
    idle = int(getattr(env, "suspend_on_idle_seconds", 0) or 0)
    if idle_suspend and idle and lead_s + interval_s > idle:
        out.append(f"The platform suspends this lab after {idle // 60} idle min, which is "
                   f"shorter than the lead; it may go back to sleep before the call. "
                   f"Shorten the lead, or connect to it once it is up.")
    if suspend_schedule.has_schedule(env):
        try:
            tz = suspend_schedule.resolve_timezone(getattr(env, "schedule_timezone", ""))
            hhmm = suspend_schedule.parse_hhmm(env.suspend_at_local)
            days = suspend_schedule.normalize_days(getattr(env, "schedule_days", "") or "")
            start = wake_at.replace(tzinfo=timezone.utc)
            hits = suspend_schedule._boundaries(hhmm, tz, days, start,
                                                (demo_at + DEMO_LENGTH).replace(
                                                    tzinfo=timezone.utc)) if hhmm else []
        except suspend_schedule.ScheduleError:
            hits = []
        if hits:
            out.append(f"This POV's schedule suspends it at {env.suspend_at_local} "
                       f"({getattr(env, 'schedule_timezone', '') or 'UTC'}), during or "
                       f"just after the call. Move the schedule if the demo may run long.")
    return out


def due(env, now: datetime) -> str:
    """``START``, ``MISSED`` or ``""`` for this row at ``now`` (naive UTC)."""
    wake_at = getattr(env, "wake_at", None)
    if wake_at is None or now < wake_at:
        return ""
    demo_at = _demo_at(env) or wake_at
    if now > demo_at + STALE_AFTER:
        return MISSED
    return START


def _demo_at(env):
    raw = ((getattr(env, "metadata_dict", None) or {}).get(META_KEY) or {}).get("demo_at")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", ""))
    except ValueError:
        return None


def book(env, wake_at: datetime, demo_at: datetime, lead: int, *, by: str,
         tz_name: str) -> None:
    env.wake_at = wake_at
    meta = dict(env.metadata_dict or {})
    meta[META_KEY] = {"demo_at": demo_at.isoformat(), "lead_minutes": lead, "by": by,
                      "timezone": (tz_name or "").strip() or "UTC"}
    env.metadata_dict = meta


def clear(env) -> None:
    env.wake_at = None
    meta = dict(env.metadata_dict or {})
    if META_KEY in meta:
        meta.pop(META_KEY)
        env.metadata_dict = meta


def describe(env) -> dict:
    """The booking as the POV page renders it. No calls."""
    wake_at = getattr(env, "wake_at", None)
    if wake_at is None:
        return {"booked": False, "wake_at": "", "demo_at": "", "lead_minutes": 0,
                "timezone": "", "by": ""}
    info = (getattr(env, "metadata_dict", None) or {}).get(META_KEY) or {}
    demo_at = _demo_at(env)
    return {
        "booked": True,
        "wake_at": wake_at.isoformat() + "Z",
        "demo_at": demo_at.isoformat() + "Z" if demo_at else "",
        "lead_minutes": int(info.get("lead_minutes") or 0),
        "timezone": info.get("timezone") or "UTC",
        "by": info.get("by") or "",
    }
