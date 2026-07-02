"""Tamper-evident hash-chaining for the audit log.

Pure functions only — NO database imports — so the crypto/verify logic is unit
testable without a DB session (mirrors the ``test_cost_service`` /
``test_cloud_db_tf_vars`` convention). The DB-bound append/verify/backfill live
in :mod:`web_dashboard.services.job_service`, which calls into here.

Each audit row carries ``seq`` (global monotonic), ``prev_hash`` (the previous
row's ``entry_hash``), and ``entry_hash`` = ``sha256`` over the row's immutable
fields plus ``prev_hash``. Recomputing the chain diverges at the first row whose
content or link was altered, so any edit/delete/reorder is detectable.
"""
from datetime import datetime
import hashlib

# prev_hash of the first (genesis) entry — no predecessor.
GENESIS_PREV = "0" * 64


def _canonical(seq, timestamp, username, action, target_vm, details, prev_hash) -> str:
    """Deterministic string form of the hashed fields.

    ``timestamp`` is normalized to ISO-8601 so the datetime (append/backfill via
    the ORM) and any stringified form hash identically. ``None`` fields collapse
    to ``""``. ``details`` is hashed as its STORED JSON string (never
    re-serialized), so key ordering can't shift the hash. Fields are newline
    joined — the values here (uuids, ISO timestamps, hex digests, identifiers,
    JSON) don't contain bare newlines, so the separator is unambiguous.
    """
    ts = timestamp.isoformat() if isinstance(timestamp, datetime) else str(timestamp or "")
    parts = [
        str(seq),
        ts,
        username or "",
        action or "",
        target_vm or "",
        details or "",
        prev_hash or "",
    ]
    return "\n".join(parts)


def compute_entry_hash(seq, timestamp, username, action, target_vm, details, prev_hash) -> str:
    """SHA-256 hex digest binding this entry's fields to its predecessor."""
    return hashlib.sha256(
        _canonical(seq, timestamp, username, action, target_vm, details, prev_hash).encode("utf-8")
    ).hexdigest()


def verify_chain(entries):
    """Verify an ordered iterable of chained entries.

    Each entry must expose ``seq``, ``timestamp``, ``username``, ``action``,
    ``target_vm``, ``details``, ``prev_hash`` and ``entry_hash`` as attributes
    (ORM rows or any simple object). Returns ``(ok, first_broken_seq)`` —
    ``(True, None)`` when the whole chain recomputes and links cleanly, otherwise
    ``(False, <seq of the first bad entry>)``.
    """
    prev = GENESIS_PREV
    for e in entries:
        expected = compute_entry_hash(
            e.seq, e.timestamp, e.username, e.action, e.target_vm, e.details, prev
        )
        if e.prev_hash != prev or e.entry_hash != expected:
            return (False, e.seq)
        prev = e.entry_hash
    return (True, None)
