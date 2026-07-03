"""Config-drift visibility for the Ansible stream (community backlog #5).

Records a per-target fingerprint of each successful apply so the dashboard can
answer "when did I last apply to host X?" and "is X running an older version of
this playbook than what's in storage?". Passive and read-only — no target-side
action (the active ``--check`` reconciler is deliberately out of scope).

``content_hash`` / ``inputs_hash`` / ``evaluate`` are pure (unit-tested without a
DB); ``record_apply`` does the upsert.
"""
import hashlib
import json
from datetime import datetime
from typing import Iterable, Optional


def content_hash(asset_bytes: bytes) -> str:
    """Stable fingerprint of the applied asset's bytes. Re-applying the same
    playbook yields the same hash; an author edit changes it."""
    return hashlib.sha256(asset_bytes or b"").hexdigest()


def inputs_hash(extra_vars: Optional[dict]) -> str:
    """One-way fingerprint of the run inputs (never stores the values). Empty for
    no inputs; canonical JSON so key order doesn't matter."""
    if not extra_vars:
        return ""
    canonical = json.dumps(extra_vars, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def evaluate(rows: Iterable[dict], current_hashes: dict, stale_days: int,
             now: Optional[datetime] = None) -> dict:
    """Per-target drift signals.

    ``rows`` — ``{target, playbook_ref, content_hash, applied_at, job_id}`` per
    tracked (target, playbook). ``current_hashes`` — ``{playbook_ref: current
    content_hash}`` from storage, for change detection. A row is **unverified**
    when its last apply is ``stale_days`` or older, and **changed** when the
    stored playbook's current hash differs from what was applied. Returns
    ``{stale_days, items, unverified_count, changed_count}``; drift rows sort
    first, oldest-applied first.
    """
    now = now or datetime.utcnow()
    items = []
    for r in rows:
        applied_at = r.get("applied_at")
        age_days = (now - applied_at).days if applied_at else None
        unverified = bool(stale_days and stale_days > 0
                          and age_days is not None and age_days >= stale_days)
        cur = current_hashes.get(r.get("playbook_ref"))
        changed = bool(cur and cur != r.get("content_hash"))
        items.append({
            "target": r.get("target"),
            "playbook_ref": r.get("playbook_ref"),
            "applied_at": applied_at.isoformat() if isinstance(applied_at, datetime) else None,
            "age_days": age_days,
            "unverified": unverified,
            "changed": changed,
            "job_id": r.get("job_id"),
        })

    items.sort(key=lambda i: (not (i["unverified"] or i["changed"]), -(i["age_days"] or 0)))
    return {
        "stale_days": stale_days,
        "items": items,
        "unverified_count": sum(1 for i in items if i["unverified"]),
        "changed_count": sum(1 for i in items if i["changed"]),
    }


def record_apply(db, target: str, playbook_ref: str, content_hash: str,
                 inputs_hash: str, job_id: str) -> None:
    """Upsert the apply-state row for ``(target, playbook_ref)`` on a successful
    apply. Best-effort — the caller guards it so a tracking hiccup never fails
    the job."""
    from ..database import ConfigApplyState

    row = (db.query(ConfigApplyState)
           .filter(ConfigApplyState.target == target,
                   ConfigApplyState.playbook_ref == playbook_ref)
           .first())
    now = datetime.utcnow()
    if row:
        row.content_hash = content_hash
        row.inputs_hash = inputs_hash
        row.applied_at = now
        row.job_id = job_id
    else:
        db.add(ConfigApplyState(
            target=target, playbook_ref=playbook_ref, content_hash=content_hash,
            inputs_hash=inputs_hash, applied_at=now, job_id=job_id))
    db.commit()
