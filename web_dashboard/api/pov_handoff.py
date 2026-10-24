"""Handing a POV over: share links, a new owner, and its expiry.

Registered on ``api/pov.py``'s router, not a router of its own, so the router-level gates
(the ``pov:read`` floor and the per-POV instance gate) cover every route here exactly as they
did when this lived in that file. ``api/pov.py`` imports this module last, after everything
imported from it below is defined.
"""

from fastapi import Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import User, get_db
from ..services import (expiry_policy, expiry_reaper, job_service, pov_broker,
                        pov_env_service, pov_share)
from . import pov_gates
from .auth import get_current_user
from .pov import _POV_WRITE_OWN, _env_or_404, _platform_error, _serialize, logger, router


class ShareRequest(BaseModel):
    """How long the customer-facing link should last.

    ``days`` omitted means "decide for me" — the POV's own auto-delete date if it has one,
    otherwise ``pov_share.DEFAULT_DAYS``. There is deliberately no "never" and no password
    field: see ``services/pov_share`` for why both would be footguns rather than options.
    """
    days: int | None = None


def _share_or_400(exc: pov_share.ShareError) -> HTTPException:
    """A share refusal is about the REQUEST, so 400 rather than 502.

    Kept apart from ``_platform_error`` on purpose: "this platform has no share links" and
    "Skytap returned 500" are different problems with different fixes, and one status code
    for both is how an SE spends an afternoon on the wrong one.
    """
    return HTTPException(status_code=400, detail=str(exc))


@router.post("/managed/{env_id}/share", dependencies=_POV_WRITE_OWN)
async def share(env_id: str, payload: ShareRequest,
                db: Session = Depends(get_db),
                current_user: User = Depends(get_current_user)):
    """Publish this POV at one customer-facing URL, password-protected and time-limited.

    Synchronous rather than a job: it is a single platform call, the SE is looking at the
    result, and the generated password is returned exactly once in this response. A job
    would have to persist that password somewhere a job row could show it, which is the
    opposite of what this slice is for.
    """
    env = pov_env_service.get(db, env_id)
    if env is None:
        raise HTTPException(status_code=404, detail="No such POV environment")
    ok, why = pov_env_service.may_act_on(env)
    if not ok:
        raise HTTPException(status_code=409, detail=why)
    try:
        result = await pov_share.create(db, env, days=payload.days)
    except pov_share.ShareError as exc:
        raise _share_or_400(exc) from exc
    except Exception as exc:  # noqa: BLE001
        raise _platform_error(exc, "publishing the share link") from exc

    job_service.log_audit(
        db, getattr(current_user, "username", "") or "", "pov_share_created",
        target_vm=env.name,
        details={"environment_id": env.id, "share_id": result.get("share_id", ""),
                 "expires_at": result.get("share_expires_at", "")})
    return {"share": result,
            "environment": _serialize(env, broker=pov_broker.describe(db, env))}


@router.delete("/managed/{env_id}/share", dependencies=_POV_WRITE_OWN)
async def unshare(env_id: str, db: Session = Depends(get_db),
                  current_user: User = Depends(get_current_user)):
    """Revoke the link without touching the environment."""
    env = pov_env_service.get(db, env_id)
    if env is None:
        raise HTTPException(status_code=404, detail="No such POV environment")
    share_id = env.share_id or ""
    try:
        await pov_share.revoke(db, env)
    except pov_share.ShareError as exc:
        raise _share_or_400(exc) from exc
    except Exception as exc:  # noqa: BLE001
        raise _platform_error(exc, "revoking the share link") from exc

    if share_id:
        job_service.log_audit(
            db, getattr(current_user, "username", "") or "", "pov_share_revoked",
            target_vm=env.name,
            details={"environment_id": env.id, "share_id": share_id})
    return {"environment": _serialize(env, broker=pov_broker.describe(db, env))}


@router.post("/managed/{env_id}/share/reveal", dependencies=_POV_WRITE_OWN)
def reveal_share_password(env_id: str, db: Session = Depends(get_db),
                                current_user: User = Depends(get_current_user)):
    """Show the share link's password.

    POST rather than GET, and audited, because this is the one endpoint in the router that
    hands back a live credential. A GET would be logged in referrers and browser history
    and prefetchable; an unaudited one would mean nobody could answer "who has this link's
    password" after the fact, which is the first question asked when a POV URL turns up
    somewhere it should not have.
    """
    env = pov_env_service.get(db, env_id)
    if env is None:
        raise HTTPException(status_code=404, detail="No such POV environment")
    password = pov_share.reveal_password(env)
    if not password:
        raise HTTPException(
            status_code=404,
            detail="this POV has no stored share password — re-share it to get a new "
                   "link and password")
    job_service.log_audit(
        db, getattr(current_user, "username", "") or "", "pov_share_password_revealed",
        target_vm=env.name,
        details={"environment_id": env.id, "share_id": env.share_id or ""})
    return {"password": password}


# ── ownership handoff ────────────────────────────────────────────────────────


class OwnerRequest(BaseModel):
    username: str


@router.post("/managed/{env_id}/owner", dependencies=_POV_WRITE_OWN)
def set_owner(env_id: str, payload: OwnerRequest,
              db: Session = Depends(get_db),
              current_user: User = Depends(get_current_user)):
    """Hand this POV to another SE: an SE going on leave, or a deal changing hands.

    ``created_by`` was write-once before this, so the only way to move a POV was to have
    the new SE recreate it. It is the column ``pov_own`` reads (``owned_by``), which is why
    nothing else changes here: the new owner's own-POV access follows from it, and the old
    owner's does too -- they keep reaching it only through ``pov:write`` or their picker.

    Every refusal is checked BEFORE the write (``pov_gates.handoff_refusal``), so a handoff
    can never land a POV on somebody who then gets a 404 opening it.
    """
    env = _env_or_404(db, env_id)
    if env.status == pov_env_service.STATUS_DESTROYED:
        raise HTTPException(status_code=409, detail="this POV has been destroyed")
    username = (payload.username or "").strip()
    target = db.query(User).filter(User.username == username).first() if username else None
    reason = pov_gates.handoff_refusal(db, env, target)
    if reason:
        raise HTTPException(status_code=400, detail=reason)

    previous = env.created_by or ""
    actor = getattr(current_user, "username", "") or ""
    env.created_by = target.username
    db.commit()
    job_service.log_audit(
        db, actor, "pov_owner_changed", target_vm=env.name,
        details={"environment_id": env.id, "from": previous, "to": target.username})
    logger.info("POV %s: owner %s -> %s by %s", env.name, previous or "-",
                target.username, actor)

    from ..services import notification_service, notify_policy
    from ..utils.clock import utcnow
    notification_service.emit_safe(db, notify_policy.NotificationEvent(
        event_type="pov.owner_changed",
        title=f"POV {env.name} handed to {target.username}",
        body=f"{actor or 'someone'} moved {env.name} from {previous or 'nobody'} "
             f"to {target.username}.",
        resource_id=f"pov:{env.id}", resource_kind="pov", resource_name=env.name,
        workgroup=env.workgroup or "", url=f"/pov/{env.id}",
        # A POV can be handed back and forth; each move is its own notification.
        dedupe_bucket=f"{target.username}:{utcnow().isoformat(timespec='seconds')}",
        fields={"from": previous, "to": target.username, "by": actor}))
    db.refresh(env)
    return {"environment": _serialize(env, broker=pov_broker.describe(db, env))}


class ExpiryRequest(BaseModel):
    """Move a POV's auto-delete timer.

    Exactly the three shapes ``/api/inventory``'s Extend control offers, and handled by
    the same service call, so a POV can never end up with looser rules than a cloud VM:
    ``extend_hours`` adds to the current deadline, ``absolute`` sets one, ``never`` clears
    it — and ``never`` is refused unless the operator has ``resource_expiry_allow_never``.
    """
    extend_hours: int | None = None
    absolute: str = ""
    never: bool = False


@router.post("/managed/{env_id}/expiry", dependencies=_POV_WRITE_OWN)
def set_expiry(env_id: str, payload: ExpiryRequest,
                     db: Session = Depends(get_db),
                     current_user: User = Depends(get_current_user)):
    """Extend, set or clear this POV's auto-delete timer.

    Routed through ``expiry_reaper.set_expiry`` rather than writing ``expires_at`` here.
    That function owns the clamping (``max_total_hours``, the one-hour floor), the
    ``never`` permission, the audit entry and — the part that matters most for a POV —
    clearing BOTH warning latches, so the new deadline gets its own full ladder instead of
    being silenced by a rung crossed against the old one.
    """
    env = pov_env_service.get(db, env_id)
    if env is None:
        raise HTTPException(status_code=404, detail="No such POV environment")
    if not expiry_policy.enabled():
        raise HTTPException(
            status_code=409,
            detail="the auto-delete timer is turned off for this instance "
                   "(resource_expiry_enabled).")

    # The same inventory dict the sweep and the /inventory page work from, built by the
    # one function that knows the shape. Constructing a dict by hand here is how a POV
    # would end up eligible under a rule the reaper does not actually apply.
    from ..services import inventory_service
    item = inventory_service._pov_item(env)

    result = expiry_reaper.set_expiry(
        db, [item],
        extend_hours=payload.extend_hours,
        absolute=payload.absolute or None,
        never=payload.never,
        is_admin=bool(getattr(current_user, "is_effective_admin", False)),
        actor=getattr(current_user, "username", "") or "")
    if result["failed"]:
        raise HTTPException(status_code=400, detail=result["failed"][0]["error"])
    db.refresh(env)
    return {"expiry": result["updated"][0] if result["updated"] else None,
            "environment": _serialize(env, broker=pov_broker.describe(db, env))}
