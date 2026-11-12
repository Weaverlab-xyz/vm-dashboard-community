"""Rate limiting for in-product feedback submissions.

Why this exists
---------------
``POST /api/feedback/submit`` is reachable by every authenticated person, and the POV
accessor form behind it is reachable by a *prospect* -- somebody the operator has no
relationship with beyond one evaluation. In DIRECT mode each accepted submission makes
an outbound call that creates a public-or-private GitHub issue, so an unbounded
submit route is a way to spam a tracker, burn an API budget, and grow a table, using
nothing but a legitimate login.

Why not ``@limiter.limit``
--------------------------
The same reasons ``login_guard`` and ``agent_guard`` give, and they apply unchanged:

* **SlowAPI's ``Limiter`` is inert here.** ``main.py`` builds one but installs no
  ``SlowAPIMiddleware`` and decorates nothing, deliberately -- a blanket per-address
  cap breaks the UI.
* **Its storage is in-process, and gunicorn runs two workers.** A counter in memory
  gives each worker a private allowance, so a cap of 5 is really a cap of 10 and
  nothing says so.
* **A per-address key is attacker-controlled.** ``get_remote_address`` reads
  ``request.client.host``, which ``ProxyHeadersMiddleware`` rewrites from
  ``X-Forwarded-For``, and the shipped ``trusted_proxy_hosts`` default trusts that
  header from any peer.

So the cap is keyed on the **principal** -- the username, proven by the bearer token --
which the submitter cannot rotate.

The table IS the counter
------------------------
``agent_guard``'s central trick, and it transfers exactly. Every submission already
inserts one ``feedback_submissions`` row *before* the GitHub call, so counting rows in
the window needs no second table and no extra write. Writing first is also what keeps
the count honest: a submission whose file fails still consumed its slot, so a bad token
cannot be retried without limit.

Unlike ``login_guard`` there is no ``clear()`` on success. A *successful* submission is
exactly the thing being limited.

**Sliding window, not a lockout**, for the reason ``login_guard`` gives: a hard lockout
hands anyone who can reach the endpoint a denial-of-service against a real user. The
block lifts on its own and ``Retry-After`` says exactly when.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from ..database import FeedbackSubmission
from ..utils.clock import utcnow

logger = logging.getLogger(__name__)

#: Hard ceiling on rows one check may scan, whatever the configured cap says. The
#: query is ordered and limited, so a misconfigured cap of 100000 cannot turn a
#: throttle check into a table scan on every submit.
_MAX_ROWS_SCANNED = 500

DEFAULT_MAX_PER_USER = 5
DEFAULT_MAX_GLOBAL = 50
DEFAULT_WINDOW_HOURS = 24


class FeedbackThrottled(Exception):
    """Raised when a submitter, or the instance, is over its cap.

    Carries ``retry_after`` because a throttle that reaches the client as anything but
    a 429 with ``Retry-After`` is indistinguishable from a bug -- the caller has no way
    to tell "slow down" from "broken", and a form that says neither just looks dead.
    """

    def __init__(self, retry_after: int, scope: str):
        self.retry_after = max(1, int(retry_after))
        self.scope = scope           # "user" | "global" -- for logs and the message
        super().__init__(f"feedback submissions throttled ({scope})")


def _cfg_int(key: str, default: int) -> int:
    from . import config_service
    try:
        raw = config_service.get(key)
        return int(raw) if str(raw).strip() else default
    except Exception:  # noqa: BLE001 — a bad config value must not disable the guard
        return default


def _window() -> timedelta:
    hours = _cfg_int("feedback_window_hours", DEFAULT_WINDOW_HOURS)
    return timedelta(hours=max(1, hours))


def _retry_after(oldest: datetime, window: timedelta, now: datetime) -> int:
    """Seconds until the caller drops back under the cap.

    ``oldest`` is the earliest submission still inside the window; once it ages out the
    caller is one under again. Returning the real number matters -- a client honouring
    Retry-After should not have to poll to find out it was let back in.
    """
    return max(1, int((oldest + window - now).total_seconds()) + 1)


def _stamps(db: Session, window_start: datetime, cap: int,
            username: Optional[str]) -> list:
    q = db.query(FeedbackSubmission.created_at).filter(
        FeedbackSubmission.created_at >= window_start)
    if username is not None:
        q = q.filter(FeedbackSubmission.submitted_by == username)
    return [r.created_at for r in
            q.order_by(FeedbackSubmission.created_at.asc()).limit(cap).all()]


def check(db: Session, *, username: str, now: Optional[datetime] = None) -> None:
    """Raise :class:`FeedbackThrottled` if this submitter, or the instance, is over cap.

    Called **before** anything is written and before any outbound call, so a throttled
    flood neither grows the table nor touches GitHub.

    A cap of ``0`` disables that cap, matching ``login_guard`` and ``agent_guard``.
    """
    now = now or utcnow()
    window = _window()
    start = now - window

    per_user = _cfg_int("feedback_max_per_user", DEFAULT_MAX_PER_USER)
    if per_user > 0 and username:
        cap = min(per_user, _MAX_ROWS_SCANNED)
        stamps = _stamps(db, start, cap, username)
        if len(stamps) >= cap:
            logger.warning("feedback throttled for %s: %d in %dh (cap %d)",
                           username, len(stamps), int(window.total_seconds() // 3600), cap)
            raise FeedbackThrottled(_retry_after(stamps[0], window, now), "user")

    # The instance-wide backstop. Deliberately generous: its job is to bound a
    # distributed flood across many logins, not to be the limit anybody meets.
    overall = _cfg_int("feedback_max_global", DEFAULT_MAX_GLOBAL)
    if overall > 0:
        cap = min(overall, _MAX_ROWS_SCANNED)
        stamps = _stamps(db, start, cap, None)
        if len(stamps) >= cap:
            logger.warning("feedback throttled instance-wide: %d in %dh (cap %d)",
                           len(stamps), int(window.total_seconds() // 3600), cap)
            raise FeedbackThrottled(_retry_after(stamps[0], window, now), "global")


def remaining(db: Session, *, username: str, now: Optional[datetime] = None) -> int:
    """How many submissions this user has left in the window, for the form to show.

    Advisory only -- :func:`check` is what enforces. ``-1`` means "no per-user cap".
    """
    per_user = _cfg_int("feedback_max_per_user", DEFAULT_MAX_PER_USER)
    if per_user <= 0:
        return -1
    now = now or utcnow()
    used = len(_stamps(db, now - _window(), min(per_user, _MAX_ROWS_SCANNED), username))
    return max(0, per_user - used)
