"""The app's clock: naive UTC, the one representation every stored timestamp uses.

``datetime.utcnow()`` and ``datetime.utcfromtimestamp()`` are deprecated (Python 3.12) and
scheduled for removal. Their replacement, ``datetime.now(timezone.utc)``, returns an AWARE
datetime, and an aware value compared with a naive one raises ``TypeError`` -- every
column in this schema is naive UTC, so swapping the call in place would break the first
comparison against a stored row. These return exactly what the deprecated calls did: the
same instant, naive, in UTC.

A module that tests need to steer keeps its own ``_utcnow()`` seam (``schedule_service``,
``expiry_reaper``, ...); those call through here.
"""
from datetime import datetime, timezone


def utcnow() -> datetime:
    """The current time as naive UTC -- what ``datetime.utcnow()`` returned."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def utcfromtimestamp(ts: float) -> datetime:
    """A POSIX timestamp as naive UTC -- what ``datetime.utcfromtimestamp()`` returned."""
    return datetime.fromtimestamp(ts, timezone.utc).replace(tzinfo=None)
