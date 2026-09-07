"""Tamper-evident hash-chaining for the audit log.

Pure functions only — NO database imports — so the crypto/verify logic is unit
testable without a DB session (mirrors the ``test_cost_service`` /
``test_cloud_db_tf_vars`` convention). The DB-bound append/verify/backfill live
in :mod:`web_dashboard.services.job_service`, which calls into here.

Each audit row carries ``seq`` (global monotonic), ``prev_hash`` (the previous
row's ``entry_hash``), and ``entry_hash`` = ``sha256`` over the row's immutable
fields plus ``prev_hash``. Recomputing the chain diverges at the first row whose
content or link was altered, so any edit/delete/reorder is detectable.

WHY THERE ARE TWO CANONICAL FORMS
---------------------------------
V1 hashed seven fields and left ``ip_address`` **outside** the chain. The column
existed and no caller populated it, so nothing was actually unprotected — but the
moment an address was recorded it would have been alterable without breaking
verification, which is the worst kind of integrity guarantee: one that reads as
covering a field it does not.

V2 adds ``ip_address``. That changes every hash, so it cannot be applied to an
existing table by fiat — see :func:`job_service.rechain_audit_log`, which verifies
the old chain under V1 **before** rewriting it under V2 and refuses to re-bless a
table that was already broken. V1 is kept for exactly that check, and for reading a
database that has not been migrated yet. Do not delete it.

Adding a field to :func:`_canonical` in future means the same dance: a new version,
a verify-then-rechain, and the old form kept to verify what came before it.
"""
from datetime import datetime
import hashlib

# prev_hash of the first (genesis) entry — no predecessor.
GENESIS_PREV = "0" * 64

# The current canonical form. Bumping this is a migration, not an edit.
CHAIN_VERSION = 2


def _iso(timestamp) -> str:
    """ISO-8601 form of a timestamp, so the datetime (append/backfill via the ORM)
    and any stringified form hash identically."""
    return timestamp.isoformat() if isinstance(timestamp, datetime) else str(timestamp or "")


def _canonical_v1(seq, timestamp, username, action, target_vm, details, prev_hash) -> str:
    """The pre-``ip_address`` form. Kept to verify rows written before the migration."""
    parts = [
        str(seq), _iso(timestamp), username or "", action or "",
        target_vm or "", details or "", prev_hash or "",
    ]
    return "\n".join(parts)


def _canonical(seq, timestamp, username, action, target_vm, details, prev_hash,
               ip_address=None) -> str:
    """Deterministic string form of the hashed fields (V2).

    ``timestamp`` is normalized to ISO-8601 so the datetime (append/backfill via
    the ORM) and any stringified form hash identically. ``None`` fields collapse
    to ``""``. ``details`` is hashed as its STORED JSON string (never
    re-serialized), so key ordering can't shift the hash. Fields are newline
    joined — the values here (uuids, ISO timestamps, hex digests, identifiers,
    JSON, IP literals) don't contain bare newlines, so the separator is unambiguous.

    ``ip_address`` is last so the V1 prefix is unchanged, which keeps the diff
    between the two forms one appended field rather than a reordering.
    """
    parts = [
        str(seq), _iso(timestamp), username or "", action or "",
        target_vm or "", details or "", prev_hash or "", ip_address or "",
    ]
    return "\n".join(parts)


def compute_entry_hash_v1(seq, timestamp, username, action, target_vm, details,
                          prev_hash) -> str:
    """SHA-256 over the pre-``ip_address`` canonical form."""
    return hashlib.sha256(
        _canonical_v1(seq, timestamp, username, action, target_vm, details,
                      prev_hash).encode("utf-8")
    ).hexdigest()


def compute_entry_hash(seq, timestamp, username, action, target_vm, details, prev_hash,
                       ip_address=None) -> str:
    """SHA-256 hex digest binding this entry's fields to its predecessor."""
    return hashlib.sha256(
        _canonical(seq, timestamp, username, action, target_vm, details, prev_hash,
                   ip_address).encode("utf-8")
    ).hexdigest()


def _walk(entries, prev, hasher, with_ip: bool):
    """Recompute a chain from ``prev``. Returns ``(ok, first_broken_seq, last_hash)``.

    Walks the iterable exactly once, so a query iterator or generator is safe to pass.
    """
    for e in entries:
        if with_ip:
            expected = hasher(e.seq, e.timestamp, e.username, e.action, e.target_vm,
                              e.details, prev, getattr(e, "ip_address", None))
        else:
            expected = hasher(e.seq, e.timestamp, e.username, e.action, e.target_vm,
                              e.details, prev)
        if e.prev_hash != prev or e.entry_hash != expected:
            return (False, e.seq, prev)
        prev = e.entry_hash
    return (True, None, prev)


def verify_chain(entries):
    """Verify an ordered iterable of chained entries under the current form.

    Each entry must expose ``seq``, ``timestamp``, ``username``, ``action``,
    ``target_vm``, ``details``, ``ip_address``, ``prev_hash`` and ``entry_hash`` as
    attributes (ORM rows or any simple object). Returns ``(ok, first_broken_seq)`` —
    ``(True, None)`` when the whole chain recomputes and links cleanly, otherwise
    ``(False, <seq of the first bad entry>)``.

    The walk is single-pass and never indexes the iterable, so the caller can stream a
    query rather than materialize a table that has no retention policy and never will
    (pruning any row breaks the chain by construction).

    It is deliberately a FULL walk rather than resuming from a checkpoint. Altering an
    old row breaks that row's hash but not the links between the rows after it, so a
    check that started partway through would step straight over the tampering it exists
    to find. Streaming keeps that affordable; skipping rows would not keep it correct.
    """
    ok, broken, _ = _walk(entries, GENESIS_PREV, compute_entry_hash, True)
    return (ok, broken)


def verify_chain_v1(entries):
    """Verify under the pre-``ip_address`` form. Only the migration should need this."""
    ok, broken, _ = _walk(entries, GENESIS_PREV, compute_entry_hash_v1, False)
    return (ok, broken)
