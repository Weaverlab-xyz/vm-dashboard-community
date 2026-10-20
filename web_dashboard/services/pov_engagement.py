"""Is the customer actually using their POV? The engagement signal on each row.

An SE running ten POVs finds out one has gone cold at the next check-in call, which is the
least useful moment to find out. Everything needed to see it sooner was already in this
database except one fact -- whether an accessor login had ever been USED -- and the summary
was careful not to fake it: "a login issued is not evidence" (``pov_summary``).

``PovAccessor.last_login_at`` is that fact now, stamped at sign-in (``api/auth``). This
module turns it, and the customer's own ticks and notes, into one small projection per POV:

  * ``state`` -- the headline, in order of what it proves:
      ``no_login``      no customer login was ever issued
      ``not_signed_in`` logins exist and nobody has used one
      ``signed_in``     somebody signed in, and has ticked nothing yet
      ``active``        the customer ticked or noted something themselves
  * ``last_customer_at`` -- the latest of their sign-ins, ticks and notes
  * ``idle_days`` -- days since then, so a list can sort the cold ones up

Read-only, no network, and :func:`describe_many` is three queries for a whole page however
many POVs it holds -- the list endpoint is the one caller where a per-row query multiplies
by both the inventory and every SE loading it.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from ..database import PovAccessor, PovEnvironment, PovUseCaseProgress
from ..utils.clock import utcnow
from . import pov_use_cases

NO_LOGIN, NOT_SIGNED_IN, SIGNED_IN, ACTIVE = "no_login", "not_signed_in", "signed_in", "active"


def _iso(value) -> str:
    return value.isoformat() + "Z" if value else ""


def _latest(*values):
    present = [v for v in values if v is not None]
    return max(present) if present else None


def project(*, issued: int, live: int, signed_in: int, last_login, customer_ticks: int,
            last_tick, now: datetime | None = None) -> dict:
    """The projection from counts. Pure."""
    now = now or utcnow()
    if customer_ticks:
        state = ACTIVE
    elif signed_in:
        state = SIGNED_IN
    elif issued:
        state = NOT_SIGNED_IN
    else:
        state = NO_LOGIN
    last = _latest(last_login, last_tick)
    return {
        "state": state,
        "logins_issued": issued,
        "logins_live": live,
        "logins_used": signed_in,
        "last_login_at": _iso(last_login),
        "customer_ticks": customer_ticks,
        "last_customer_at": _iso(last),
        "idle_days": (now - last).days if last else None,
    }


def describe_many(db: Session, envs: list[PovEnvironment]) -> dict[str, dict]:
    """``{env.id: projection}`` for a page of POVs, in three queries."""
    if not envs:
        return {}
    ids = [e.id for e in envs]
    now = utcnow()
    acc: dict = {}
    for env_id, revoked_at, expires_at, last_login in (
            db.query(PovAccessor.environment_id, PovAccessor.revoked_at,
                     PovAccessor.expires_at, PovAccessor.last_login_at)
              .filter(PovAccessor.environment_id.in_(ids)).all()):
        slot = acc.setdefault(env_id, {"issued": 0, "live": 0, "signed_in": 0,
                                       "last_login": None})
        slot["issued"] += 1
        if revoked_at is None and (expires_at is None or expires_at > now):
            slot["live"] += 1
        if last_login is not None:
            slot["signed_in"] += 1
            slot["last_login"] = _latest(slot["last_login"], last_login)
    ticks: dict = {}
    for env_id, checked_at in (
            db.query(PovUseCaseProgress.environment_id, PovUseCaseProgress.checked_at)
              .filter(PovUseCaseProgress.environment_id.in_(ids),
                      PovUseCaseProgress.checked_by_kind == pov_use_cases.KIND_ACCESSOR)
              .all()):
        slot = ticks.setdefault(env_id, {"n": 0, "last": None})
        slot["n"] += 1
        slot["last"] = _latest(slot["last"], checked_at)
    out = {}
    for env_id in ids:
        a = acc.get(env_id, {})
        t = ticks.get(env_id, {})
        out[env_id] = project(
            issued=a.get("issued", 0), live=a.get("live", 0),
            signed_in=a.get("signed_in", 0), last_login=a.get("last_login"),
            customer_ticks=t.get("n", 0), last_tick=t.get("last"), now=now)
    return out


def describe(db: Session, env: PovEnvironment) -> dict:
    return describe_many(db, [env]).get(env.id) or project(
        issued=0, live=0, signed_in=0, last_login=None, customer_ticks=0, last_tick=None)
