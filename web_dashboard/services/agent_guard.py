"""Request throttling for the remote-agent API.

Why this exists
---------------
``/api/agent/*`` is the only router this dashboard deliberately publishes to a network
it does not own (``docker-compose.agent.yml`` puts it on its own vhost), and until this
module it had **no rate limit of any kind**. The app builds a SlowAPI ``Limiter``, but
``main.py`` explains at length that it is inert — no ``SlowAPIMiddleware``, no
``@limiter.limit`` decorators — because a blanket per-address cap would break the UI.

So the exposure was: unauthenticated ``POST /api/agent/enroll`` doing a database lookup
per call with nothing bounding the call rate, and an authenticated agent (or anyone
holding a copy of an agent's private key) able to poll ``/lease`` as fast as the network
allows, each request inserting an ``agent_nonces`` row while the sweeper only runs
opportunistically on that same path.

Neither is an authentication bypass — enrolment codes are 256 bits and every other route
needs a valid Ed25519 signature. Both are denial-of-service and unbounded table growth.

Why not ``@limiter.limit``
--------------------------
The same two reasons ``login_guard`` gives, and one more:

* **A per-address key is attacker-controlled.** ``get_remote_address`` reads
  ``request.client.host``, which ``ProxyHeadersMiddleware`` rewrites from
  ``X-Forwarded-For``; the shipped ``trusted_proxy_hosts`` default trusts that header
  from any peer. The agent overlay pins it, but nothing guarantees an operator used the
  overlay.
* **SlowAPI's storage is in-process**, and gunicorn runs two workers.
* **There is a better key available here.** Every route except enrolment authenticates a
  *principal* — the agent id, proven by a signature — which the attacker cannot rotate.

So the per-request cap is keyed on the agent id, and the enrolment cap falls back to the
address because enrolment has no principal yet, and is honest about depending on a
pinned proxy.

Two design points worth keeping
-------------------------------
**The nonce table IS the request counter.** Every authenticated agent request already
inserts exactly one ``agent_nonces`` row, so counting rows in the window needs no new
table and no new write. That also means the throttle is checked *after* signature
verification and *before* the nonce insert: after, so an unauthenticated caller who
guesses an agent id cannot consume that agent's budget (the same ordering argument
``agent_service.authenticate`` already makes for the replay check); before, so a
throttled flood stops growing the table the moment it hits the cap.

**Sliding window, not a lockout**, for the reason ``login_guard`` gives: a hard lockout
hands anyone who can reach the endpoint a denial-of-service against a real agent. The
block lifts on its own and ``Retry-After`` says exactly when.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from ..database import AgentEnrollAttempt, AgentNonce
from . import agent_signing

logger = logging.getLogger(__name__)

# ── Per-agent request cap ─────────────────────────────────────────────────────
#
# Sized off what a *busy* agent legitimately does, which is not the poll interval. While
# a scan runs there are no lease polls (execute() blocks the loop), but Reporter flushes
# every 2s and each flush is up to two requests — logs + heartbeat — so ~60/minute, plus
# the completion. Idle polling at the 5s default is ~12/minute. 240 leaves roughly 3x
# headroom over the busiest honest minute while still bounding a runaway by 25x.
DEFAULT_MAX_REQUESTS_PER_WINDOW = 240
REQUEST_WINDOW_SECONDS = 60

# ── Enrolment cap ─────────────────────────────────────────────────────────────
#
# Enrolment is rare, operator-initiated and single-use, so these can be tight. The
# global cap is a backstop against a distributed flood rather than a real access
# control: it can be saturated on purpose to block a legitimate enrolment, which is why
# it is deliberately generous and why the per-address cap is the one doing the work.
DEFAULT_ENROLL_MAX_PER_IP = 10
DEFAULT_ENROLL_MAX_GLOBAL = 200
DEFAULT_ENROLL_WINDOW_MINUTES = 15
DEFAULT_ENROLL_RETENTION_MINUTES = 60

# Bound the work one check can do, however the caps are configured.
_MAX_ROWS_SCANNED = 1000


class AgentThrottled(Exception):
    """Too many recent requests for this agent or address.

    Deliberately **not** an ``AgentError``. That one means "refused, and the answer is
    401" — a throttle has to reach the client as 429 with ``Retry-After``, or the agent
    treats it as revocation and stops instead of backing off.
    """

    def __init__(self, retry_after: int, scope: str):
        self.retry_after = max(1, int(retry_after))
        self.scope = scope           # "agent" | "ip" | "global" — for logs
        super().__init__(f"agent request throttled ({scope})")


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
        return config_service.get_bool("agent_throttle_enabled", True)
    except Exception:  # noqa: BLE001 — fail CLOSED: keep throttling if config is broken
        return True


def _retry_after(oldest: datetime, window: timedelta, now: datetime) -> int:
    """Seconds until the caller drops back under the cap.

    ``oldest`` is the earliest attempt still inside the window; once it ages out the
    caller is one under again. Returning the real time matters — a client honouring
    Retry-After should not have to poll to discover it was let back in.
    """
    return max(1, int((oldest + window - now).total_seconds()) + 1)


# ── Authenticated requests, keyed on the agent ────────────────────────────────

def _request_window() -> timedelta:
    """The counting window, clamped to how long nonces actually survive.

    ``sweep_nonces`` deletes rows older than ``SIGNATURE_WINDOW_SECONDS * 4``. Counting
    over a window longer than that would silently under-report — the rows it wanted to
    count are already gone — so the clamp keeps the two from drifting into a throttle
    that quietly does nothing.
    """
    seconds = min(REQUEST_WINDOW_SECONDS, agent_signing.SIGNATURE_WINDOW_SECONDS * 4)
    return timedelta(seconds=seconds)


def check_request(db: Session, agent_id: str, *, now: Optional[datetime] = None) -> None:
    """Raise :class:`AgentThrottled` if this agent is over its request cap.

    Call **after** the signature verifies and **before** the nonce is recorded; see the
    module docstring for why that order is the whole point.
    """
    if not _enabled() or not agent_id:
        return
    cap = _cfg_int("agent_max_requests_per_minute", DEFAULT_MAX_REQUESTS_PER_WINDOW)
    if cap <= 0:                      # 0 disables the cap, matching login_guard
        return
    cap = min(cap, _MAX_ROWS_SCANNED)

    now = now or datetime.utcnow()
    window = _request_window()
    stamps = [
        row.seen_at for row in
        db.query(AgentNonce.seen_at)
        .filter(AgentNonce.agent_id == agent_id, AgentNonce.seen_at >= now - window)
        .order_by(AgentNonce.seen_at.asc())
        .limit(cap)
        .all()
    ]
    if len(stamps) >= cap:
        logger.warning("agent %s throttled: %d requests in %ds (cap %d)",
                       agent_id, len(stamps), int(window.total_seconds()), cap)
        raise AgentThrottled(_retry_after(stamps[0], window, now), "agent")


# ── Enrolment, keyed on the address ───────────────────────────────────────────

def _enroll_window() -> timedelta:
    return timedelta(minutes=_cfg_int("agent_enroll_window_minutes",
                                      DEFAULT_ENROLL_WINDOW_MINUTES))


def check_enroll(db: Session, *, ip: str = "", now: Optional[datetime] = None) -> None:
    """Raise :class:`AgentThrottled` if enrolment failures are over a cap.

    Only *failures* are counted, so a legitimate operator enrolling a fleet back to back
    never meets this. Called before the code is looked up, so a throttled request never
    reaches the database — which also means the throttle cannot be used to amplify load.
    """
    if not _enabled():
        return
    now = now or datetime.utcnow()
    window = _enroll_window()
    cutoff = now - window

    rows = (
        db.query(AgentEnrollAttempt.ip, AgentEnrollAttempt.attempted_at)
        .filter(AgentEnrollAttempt.attempted_at >= cutoff)
        .order_by(AgentEnrollAttempt.attempted_at.asc())
        .limit(_MAX_ROWS_SCANNED)
        .all()
    )
    if not rows:
        return

    if ip:
        ip_stamps = [r.attempted_at for r in rows if r.ip == ip]
        ip_cap = _cfg_int("agent_enroll_max_per_ip", DEFAULT_ENROLL_MAX_PER_IP)
        if ip_cap > 0 and len(ip_stamps) >= ip_cap:
            logger.warning("agent enrolment throttled for %s: %d recent failures",
                           ip, len(ip_stamps))
            raise AgentThrottled(_retry_after(ip_stamps[0], window, now), "ip")

    global_cap = _cfg_int("agent_enroll_max_global", DEFAULT_ENROLL_MAX_GLOBAL)
    if global_cap > 0 and len(rows) >= global_cap:
        logger.warning("agent enrolment throttled globally: %d recent failures",
                       len(rows))
        raise AgentThrottled(_retry_after(rows[0].attempted_at, window, now), "global")


def record_enroll_failure(db: Session, *, ip: str = "",
                          now: Optional[datetime] = None) -> None:
    """Record one failed enrolment. Never raises — a guard that can 500 the endpoint it
    protects is worse than no guard."""
    if not _enabled():
        return
    try:
        db.add(AgentEnrollAttempt(ip=(ip or "")[:45],
                                  attempted_at=now or datetime.utcnow()))
        db.commit()
        sweep_enroll_attempts(db)
    except Exception:  # noqa: BLE001
        logger.warning("agent guard: could not record an enrolment failure",
                       exc_info=True)
        try:
            db.rollback()
        except Exception:  # pragma: no cover — defensive
            pass


def sweep_enroll_attempts(db: Session, now: Optional[datetime] = None) -> int:
    """Drop rows past the retention horizon. Opportunistic, on the failure path — the
    table only grows when enrolment is failing, which is exactly when it needs tidying,
    and it needs no timer process."""
    try:
        cutoff = (now or datetime.utcnow()) - timedelta(
            minutes=_cfg_int("agent_enroll_retention_minutes",
                             DEFAULT_ENROLL_RETENTION_MINUTES))
        deleted = db.query(AgentEnrollAttempt).filter(
            AgentEnrollAttempt.attempted_at < cutoff).delete(synchronize_session=False)
        db.commit()
        return deleted
    except Exception:  # noqa: BLE001
        try:
            db.rollback()
        except Exception:  # pragma: no cover — defensive
            pass
        return 0
