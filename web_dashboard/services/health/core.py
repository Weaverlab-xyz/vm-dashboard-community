"""The shapes every health collector shares.

A collector answers one question for one **area** of the estate -- "are the BeyondTrust
tenants answering?", "are the remote agents online?" -- with one :class:`Item` per object
and a few checks on each. The runner stores what it returns; the Health page only ever
reads what was stored. Same status vocabulary and same "worst check wins" rule as the
Directory health page, whose helpers these are.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional
from ...utils.clock import utcnow

OK, WARN, FAIL, UNKNOWN = "ok", "warn", "fail", "unknown"
STATUSES = (OK, WARN, FAIL, UNKNOWN)


def check(key: str, label: str, status: str, detail: str = "") -> dict:
    return {"key": key, "label": label, "status": status, "detail": text(detail, 600)}


def overall(checks: list) -> str:
    """The worst status among ``checks``. Unknown never outranks a real answer."""
    seen = {c.get("status") for c in checks}
    for status in (FAIL, WARN, OK):
        if status in seen:
            return status
    return UNKNOWN


def text(value, limit: int = 200) -> str:
    """A display string from anything a provider sent: control characters out, capped."""
    return re.sub(r"[\x00-\x1f\x7f]", " ", str(value if value is not None else ""))[:limit]


def parse_when(value) -> Optional[datetime]:
    """A naive-UTC datetime from the date strings providers return, or None.

    Tolerates a trailing ``Z``, an offset, and more than six fractional digits (Password
    Safe sends seven). Anything else is None rather than an exception: a date we cannot
    read is a detail to show, not a reason to fail a whole collector.
    """
    if not value:
        return None
    raw = str(value).strip().replace("Z", "+00:00")
    raw = re.sub(r"(\.\d{6})\d+", r"\1", raw)
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def ago(when: Optional[datetime], now: Optional[datetime] = None) -> str:
    if not when:
        return "never"
    secs = int(((now or utcnow()) - when).total_seconds())
    if secs < 0:
        return "just now"
    if secs < 5400:
        return f"{max(1, secs // 60)} min ago"
    if secs < 172800:
        return f"{secs // 3600} h ago"
    return f"{secs // 86400} days ago"


@dataclass
class Item:
    """One object's health, as a collector reports it."""
    object_id: str
    name: str
    checks: list
    kind: str = ""
    href: str = ""

    @property
    def status(self) -> str:
        return overall(self.checks)


@dataclass(frozen=True)
class Collector:
    """One health area.

    ``flags`` is ANY-of: the area is collected (and shown) when at least one of these
    feature flags is on, and an empty tuple means always. ``personas`` only orders the
    page -- a persona never hides an area, the rule ``services/personas`` is built on.
    """
    area: str
    label: str
    collect: Callable[..., Awaitable[list]]
    interval_minutes: int
    flags: tuple = ()
    personas: tuple = ()
    description: str = ""
    extra: dict = field(default_factory=dict)
