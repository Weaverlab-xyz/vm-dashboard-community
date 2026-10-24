"""When a POV runs: its schedule, idle suspend, a one-off wake-up and its spend cap.

Registered on ``api/pov.py``'s router, not a router of its own, so the router-level gates
(the ``pov:read`` floor and the per-POV instance gate) cover every route here exactly as they
did when this lived in that file. ``api/pov.py`` imports this module last, after everything
imported from it below is defined.
"""

from fastapi import Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import User, get_db
from ..services import (lab_platforms, pov_cloud_cost, pov_env_service, pov_reconcile,
                        pov_wake, spend_policy, suspend_schedule)
from .auth import get_current_user
from .pov import _POV_WRITE_OWN, _env_or_404, logger, router


class ScheduleRequest(BaseModel):
    """A suspend schedule. Every field blank clears it."""
    suspend_at_local: str = ""
    resume_at_local: str = ""
    schedule_timezone: str = ""
    schedule_days: str = ""


@router.post("/managed/{env_id}/schedule", dependencies=_POV_WRITE_OWN)
def set_schedule(env_id: str, payload: ScheduleRequest,
                       db: Session = Depends(get_db),
                       current_user: User = Depends(get_current_user)):
    """Set or clear this POV's suspend schedule.

    Refused on a platform that has its own idle timer. Skytap's `suspend_on_idle` and this
    answer the same question, and a POV carrying both would be one where neither is
    clearly in charge — the platform would suspend on idle, the dashboard would resume on
    a boundary, and the pair would fight quietly for the length of the evaluation.

    Setting a schedule CLEARS the evaluation latch, so the first pass after it records the
    time and acts on nothing. Without that, changing 19:00 to 22:00 at 20:00 would look
    back over a window in which the old boundary had already passed.
    """
    env = pov_env_service.get(db, env_id)
    if env is None:
        raise HTTPException(status_code=404, detail="No such POV environment")
    if lab_platforms.supports(env.platform, "idle_suspend"):
        label = lab_platforms.capabilities(env.platform)["label"]
        raise HTTPException(
            status_code=409,
            detail=(f"{label} suspends an environment on its own idle timer, so a "
                    f"dashboard schedule would fight it. Set the idle timeout instead."))
    if not lab_platforms.supports(env.platform, "scheduled_suspend"):
        raise HTTPException(
            status_code=409,
            detail=f"{env.platform} has no scheduled suspend.")

    try:
        fields = suspend_schedule.validate(
            suspend_at=payload.suspend_at_local, resume_at=payload.resume_at_local,
            tz_name=payload.schedule_timezone, days=payload.schedule_days)
    except suspend_schedule.ScheduleError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None

    for field, value in fields.items():
        setattr(env, field, value)
    env.schedule_last_checked_at = None
    db.commit()
    logger.info("POV %s: schedule set by %s", env.name,
                getattr(current_user, "username", "?"))
    return {"schedule": suspend_schedule.describe(env)}


class IdleSuspendRequest(BaseModel):
    """The idle window in seconds. 0 turns idle suspend off."""
    seconds: int = 0


@router.post("/managed/{env_id}/idle-suspend", dependencies=_POV_WRITE_OWN)
def set_idle_suspend(env_id: str, payload: IdleSuspendRequest,
                     db: Session = Depends(get_db),
                     current_user: User = Depends(get_current_user)):
    """Set or clear the dashboard's idle timer on a cloud POV.

    Only where the DASHBOARD is the timer (``dashboard_idle_suspend``). Skytap holds its
    own ``suspend_on_idle`` and this row is a copy the reconcile pass refreshes from it,
    so a write here would be silently put back ten minutes later.
    """
    from ..services import pov_cloud_env
    env = _env_or_404(db, env_id)
    if not lab_platforms.supports(env.platform, "dashboard_idle_suspend"):
        label = lab_platforms.capabilities(env.platform)["label"]
        raise HTTPException(
            status_code=409,
            detail=f"{label} POVs have no dashboard idle timer to set.")
    seconds = int(payload.seconds or 0)
    if seconds and not (pov_cloud_env.IDLE_MIN_WINDOW_S <= seconds
                        <= pov_cloud_env.IDLE_MAX_WINDOW_S):
        raise HTTPException(
            status_code=400,
            detail=(f"the idle window must be between "
                    f"{pov_cloud_env.IDLE_MIN_WINDOW_S // 60} minutes and "
                    f"{pov_cloud_env.IDLE_MAX_WINDOW_S // 3600} hours, or 0 to turn it "
                    f"off — the cloud's metrics arrive in five-minute steps and up to "
                    f"fifteen minutes late, so a shorter one could not tell a pause from "
                    f"an idle lab"))
    env.suspend_on_idle_seconds = seconds or None
    db.commit()
    logger.info("POV %s: idle suspend set to %ss by %s", env.name, seconds,
                getattr(current_user, "username", "?"))
    return {"suspend_on_idle_seconds": seconds}


class WakeRequest(BaseModel):
    """A one-off warm-up before a demo. ``demo_at_local`` blank cancels the booking.

    ``demo_at_local`` is the demo's start, ``YYYY-MM-DDTHH:MM`` in ``timezone`` (IANA,
    the browser's own zone by default); ``lead_minutes`` is how long before it to start.
    """
    demo_at_local: str = ""
    timezone: str = ""
    lead_minutes: int | None = None


@router.post("/managed/{env_id}/wake-at", dependencies=_POV_WRITE_OWN)
def set_wake_at(env_id: str, payload: WakeRequest,
                db: Session = Depends(get_db),
                current_user: User = Depends(get_current_user)):
    """Book, or cancel, a one-off start before a demo.

    Allowed on every platform, including one with its own idle timer that refuses the
    recurring schedule above: this only ever STARTS a lab, so it has nothing to fight
    over. The booking is acted on by the reconcile pass (``pov_reconcile._sweep_wake``),
    which queues the same ``pov_env_power`` job the Start button does.

    Returns ``warnings``: reasons the lab may not be running for the whole call -- a lead
    shorter than the reconcile interval, an idle timer shorter than the lead, a recurring
    suspend that lands mid-call. Warnings, not refusals: each has a legitimate reading.
    """
    env = _env_or_404(db, env_id)
    actor = getattr(current_user, "username", "") or ""
    if not (payload.demo_at_local or "").strip():
        pov_wake.clear(env)
        db.commit()
        logger.info("POV %s: warm-up cancelled by %s", env.name, actor)
        return {"wake": pov_wake.describe(env), "warnings": []}

    ok, why = pov_env_service.may_act_on(env)
    if not ok:
        raise HTTPException(status_code=409, detail=why)
    if not env.platform_environment_id:
        raise HTTPException(status_code=409,
                            detail="this environment was never created on the platform")
    try:
        wake_at, demo_at, lead = pov_wake.plan(payload.demo_at_local, payload.timezone,
                                               payload.lead_minutes)
    except (pov_wake.WakeError, suspend_schedule.ScheduleError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None

    pov_wake.book(env, wake_at, demo_at, lead, by=actor, tz_name=payload.timezone)
    db.commit()
    logger.info("POV %s: warm-up booked by %s for %s (start %s)", env.name, actor,
                demo_at.isoformat(), wake_at.isoformat())
    return {"wake": pov_wake.describe(env),
            "warnings": pov_wake.warnings(
                env, wake_at, demo_at, interval_s=pov_reconcile.interval_seconds(),
                idle_suspend=lab_platforms.supports(env.platform, "idle_suspend"))}


class SpendCapRequest(BaseModel):
    """A spend cap in US dollars. Blank or zero clears it."""
    cap_usd: float | None = None


@router.post("/managed/{env_id}/spend-cap", dependencies=_POV_WRITE_OWN)
def set_spend_cap(env_id: str, payload: SpendCapRequest,
                        db: Session = Depends(get_db),
                        current_user: User = Depends(get_current_user)):
    """Set or clear this POV's estimated-spend cap.

    **Raising the cap clears both latches**, so a POV that was warned — or suspended — at
    the old figure gets a fresh warning and a fresh action at the new one. Without that,
    "give it another fifty dollars" would leave the row permanently flagged and the cap
    unable to fire again. The same rule the auto-delete timer follows when an expiry is
    extended.

    The accrued total is deliberately NOT reset. It is what this POV has cost so far, and
    zeroing it on every edit would make the cap mean "another $X from now", which is a
    different and much weaker promise than the one the page makes.
    """
    env = pov_env_service.get(db, env_id)
    if env is None:
        raise HTTPException(status_code=404, detail="No such POV environment")
    if not pov_cloud_cost.priced(env.platform):
        raise HTTPException(
            status_code=409, detail=pov_cloud_cost.no_price_reason(env.platform))

    try:
        cap = spend_policy.validate_cap(payload.cap_usd)
    except spend_policy.SpendError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None

    previous = float(env.spend_cap_usd or 0.0)
    env.spend_cap_usd = cap
    if cap is None or cap > previous:
        env.spend_warned_at = None
        env.spend_capped_at = None
    db.commit()
    logger.info("POV %s: spend cap set to %s by %s", env.name, cap,
                getattr(current_user, "username", "?"))
    return {"spend": pov_reconcile.describe(env).get("spend", {})}
