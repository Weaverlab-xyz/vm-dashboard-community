"""Brute-force throttling for the password login endpoint.

Why this is not a ``@limiter.limit`` decorator
----------------------------------------------
The app builds a SlowAPI ``Limiter`` keyed on ``get_remote_address``, and reaching for
it here would be the three-line fix. It would also not work, for two reasons that both
matter:

* **The key is attacker-controlled.** ``get_remote_address`` reads
  ``request.client.host``, which ``ProxyHeadersMiddleware`` has already rewritten from
  ``X-Forwarded-For``. The shipped default trusts that header from *any* peer, so an
  attacker rotates one header value per request and the limit evaporates. A per-IP cap
  is only as strong as ``TRUSTED_PROXY_HOSTS``, which most deployments will not have
  pinned.
* **SlowAPI's default storage is in-process.** Gunicorn runs two workers, so a "5 per
  minute" cap is really ten, and it resets on every redeploy.

So the primary key here is the **username**, which the attacker cannot rotate — it is
the thing they are trying to break into. A per-IP cap rides along as a second key to
catch spraying across many accounts, and is honest about depending on a pinned proxy.
State lives in the database because that is what makes it hold across both gunicorn
workers and survive a restart, the same reason the job claim and the notification
outbox live there.

Deliberately a **sliding window, not a lockout.** A hard lockout hands anyone who knows
a username a denial-of-service against that account. Here the block lifts on its own as
old failures age out, and ``Retry-After`` says exactly when — enough to make guessing
uneconomic without giving an attacker a way to keep a real user out.

A side effect worth having: failed logins were not recorded anywhere before this, so a
brute-force attempt against this dashboard left no trace at all. Now it leaves rows.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from ..database import LoginAttempt

logger = logging.getLogger(__name__)

# Defaults chosen so a human who genuinely forgot their password never meets them:
# ten tries in fifteen minutes is far past "let me try my other password" and far below
# anything useful for guessing.
DEFAULT_MAX_PER_USER = 10
DEFAULT_MAX_PER_IP = 50          # generous: a whole office can share one NAT address
DEFAULT_WINDOW_MINUTES = 15
DEFAULT_RETENTION_MINUTES = 60   # keep a little history past the window, for forensics

# Bound the work a single check can do if a sweep has not run for a while.
_MAX_ROWS_SCANNED = 500


class LoginThrottled(Exception):
    """Too many recent failures for this username or address."""

    def __init__(self, retry_after: int, scope: str):
        self.retry_after = max(1, int(retry_after))
        self.scope = scope           # "user" | "ip" — for logging, never for the client
        super().__init__(f"too many failed login attempts ({scope})")


def _cfg_int(key: str, default: int) -> int:
    from . import config_service
    try:
        raw = config_service.get(key)
        return int(raw) if str(raw).strip() else default
    except Exception:  # noqa: BLE001 — a bad config value must not disable the guard
        return default


def _enabled() -> bool:
    from . import config_service
    try:
        return config_service.get_bool("login_throttle_enabled", True)
    except Exception:  # noqa: BLE001 — fail CLOSED: keep throttling if config is broken
        return True


def normalize_username(username: str) -> str:
    """Case-folded and trimmed, so ``Admin`` and ``admin`` share one budget rather than
    handing an attacker a fresh allowance per capitalisation."""
    return (username or "").strip().lower()[:150]


def _window() -> timedelta:
    return timedelta(minutes=_cfg_int("login_window_minutes", DEFAULT_WINDOW_MINUTES))


def _retry_after(stamps: list, cap: int, window: timedelta, now: datetime) -> int:
    """Seconds until the caller drops back below ``cap``.

    ``stamps`` is ascending. To fall to ``cap - 1`` the attempt at index
    ``len - cap`` has to age out, so its timestamp plus the window is the moment the
    block lifts. Returning the *correct* time matters: a client that honours
    Retry-After should not have to poll to discover it was let back in.
    """
    pivot = stamps[len(stamps) - cap]
    return int((pivot + window - now).total_seconds()) + 1


def check(db: Session, *, username: str, ip: str = "", now: Optional[datetime] = None) -> None:
    """Raise :class:`LoginThrottled` if this username or address is over its cap.

    Called **before** the password is verified, so a throttled request never reaches
    bcrypt — which also means the throttle cannot be used as a CPU amplifier.

    Behaves identically whether or not the username exists. That is deliberate: a
    throttle that only engaged for real accounts would answer "does this user exist?"
    for anyone willing to send eleven requests.
    """
    if not _enabled():
        return
    now = now or datetime.utcnow()
    window = _window()
    cutoff = now - window
    key = normalize_username(username)

    rows = (
        db.query(LoginAttempt.username, LoginAttempt.ip, LoginAttempt.attempted_at)
        .filter(LoginAttempt.attempted_at >= cutoff)
        .filter((LoginAttempt.username == key) | (LoginAttempt.ip == (ip or "")))
        .order_by(LoginAttempt.attempted_at.asc())
        .limit(_MAX_ROWS_SCANNED)
        .all()
    )

    user_stamps = [r.attempted_at for r in rows if r.username == key]
    user_cap = _cfg_int("login_max_attempts", DEFAULT_MAX_PER_USER)
    if user_cap > 0 and len(user_stamps) >= user_cap:
        raise LoginThrottled(_retry_after(user_stamps, user_cap, window, now), "user")

    if ip:
        ip_stamps = [r.attempted_at for r in rows if r.ip == ip]
        ip_cap = _cfg_int("login_max_attempts_per_ip", DEFAULT_MAX_PER_IP)
        if ip_cap > 0 and len(ip_stamps) >= ip_cap:
            raise LoginThrottled(_retry_after(ip_stamps, ip_cap, window, now), "ip")


def record_failure(db: Session, *, username: str, ip: str = "",
                   now: Optional[datetime] = None) -> None:
    """Record one failed attempt. Never raises — a throttle that can 500 the login
    endpoint is worse than no throttle."""
    if not _enabled():
        return
    try:
        db.add(LoginAttempt(username=normalize_username(username), ip=(ip or "")[:45],
                            attempted_at=now or datetime.utcnow()))
        db.commit()
        _sweep(db)
    except Exception:  # noqa: BLE001
        logger.warning("login guard: could not record a failed attempt", exc_info=True)
        try:
            db.rollback()
        except Exception:  # pragma: no cover — defensive
            pass


def clear(db: Session, *, username: str) -> None:
    """Drop a username's failures after a successful login.

    Only the username's rows, never the address's: on a shared NAT one person
    remembering their password would otherwise reset the budget for everyone behind it,
    including whoever is spraying.
    """
    try:
        db.query(LoginAttempt).filter(
            LoginAttempt.username == normalize_username(username)
        ).delete(synchronize_session=False)
        db.commit()
    except Exception:  # noqa: BLE001
        logger.warning("login guard: could not clear attempts", exc_info=True)
        try:
            db.rollback()
        except Exception:  # pragma: no cover — defensive
            pass


def _sweep(db: Session, now: Optional[datetime] = None) -> int:
    """Delete rows past the retention horizon. Opportunistic, on the failure path —
    the table only grows when logins are failing, so that is exactly when it needs
    tidying, and it needs no timer process."""
    try:
        cutoff = (now or datetime.utcnow()) - timedelta(
            minutes=_cfg_int("login_retention_minutes", DEFAULT_RETENTION_MINUTES))
        deleted = db.query(LoginAttempt).filter(
            LoginAttempt.attempted_at < cutoff).delete(synchronize_session=False)
        db.commit()
        return deleted
    except Exception:  # noqa: BLE001
        try:
            db.rollback()
        except Exception:  # pragma: no cover — defensive
            pass
        return 0


def recent_failures(db: Session, *, minutes: int = 60, limit: int = 100) -> list:
    """Recent failed attempts, newest first — the visibility that did not exist before.
    Consumed by the admin surface; returns plain tuples, never ORM rows."""
    cutoff = datetime.utcnow() - timedelta(minutes=minutes)
    rows = (
        db.query(LoginAttempt.username, LoginAttempt.ip, LoginAttempt.attempted_at)
        .filter(LoginAttempt.attempted_at >= cutoff)
        .order_by(LoginAttempt.attempted_at.desc())
        .limit(limit)
        .all()
    )
    return [{"username": r.username, "ip": r.ip, "attempted_at": r.attempted_at}
            for r in rows]
