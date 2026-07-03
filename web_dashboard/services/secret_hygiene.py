"""Secret hygiene — age / staleness signal for stored secrets (community Phase 1).

Read-only, no rotation. Flags secrets that haven't changed in a while. The
"last-changed" clock is chosen per secret by the caller and passed in as
``changed_at``:

- **External-vault references** (``aws_sm://`` / ``azure_kv://`` / ``gcp_sm://`` /
  ``bt_safe://``): the vault's own last-changed / last-rotated timestamp, so a
  secret you rotate in the backend reads as fresh (not falsely stale).
- **DB-stored secrets** (or when the vault can't report a date): the dashboard's
  ``AppConfig.updated_at`` — stamped by ``config_service.set()`` on every write.

This module is pure (no DB / no vault I/O) so it unit-tests trivially; the caller
(``api/secrets.py``) does the resolution and hands ``summarize`` a list of items.
"""
from datetime import datetime
from typing import Iterable, Optional


def score(changed_at: Optional[datetime], max_age_days: int,
          now: Optional[datetime] = None) -> dict:
    """Age of a secret and whether it's stale.

    ``max_age_days <= 0`` disables the staleness check (never stale). A secret
    with no ``changed_at`` has ``age_days = None`` and is not stale.
    """
    now = now or datetime.utcnow()
    if changed_at is None:
        return {"age_days": None, "stale": False}
    age_days = (now - changed_at).days
    stale = bool(max_age_days and max_age_days > 0 and age_days >= max_age_days)
    return {"age_days": age_days, "stale": stale}


def summarize(items: Iterable[dict], max_age_days: int,
              now: Optional[datetime] = None) -> dict:
    """Roll up per-secret staleness.

    ``items`` is an iterable of ``{key, source, changed_at}`` where ``source`` is
    ``"database"`` or a backend id (``"aws_sm"`` …) and ``changed_at`` is a
    ``datetime`` (or ``None`` if unknown). Returns ``{enabled, max_age_days,
    items, stale_count, stale_keys}``; each returned item adds ``age_days`` and
    ``stale``. Items are sorted oldest-first so the UI leads with the worst.
    """
    now = now or datetime.utcnow()
    out = []
    for it in items:
        s = score(it.get("changed_at"), max_age_days, now=now)
        ca = it.get("changed_at")
        out.append({
            "key": it["key"],
            "source": it.get("source", "database"),
            "changed_at": ca.isoformat() if isinstance(ca, datetime) else None,
            "age_days": s["age_days"],
            "stale": s["stale"],
        })

    out.sort(key=lambda i: (i["age_days"] is None, -(i["age_days"] or 0)))
    stale = [i for i in out if i["stale"]]
    return {
        "enabled": bool(max_age_days and max_age_days > 0),
        "max_age_days": max_age_days,
        "items": out,
        "stale_count": len(stale),
        "stale_keys": [i["key"] for i in stale],
    }
