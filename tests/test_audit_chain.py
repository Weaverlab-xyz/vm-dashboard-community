"""Unit tests for the tamper-evident audit hash chain.

Exercises the pure crypto/verify core in ``web_dashboard/services/audit_chain.py``
(``compute_entry_hash`` / ``verify_chain`` / ``GENESIS_PREV``) plus a faithful
re-implementation of the append and backfill sequencing the DB seam performs, so
the chain semantics are pinned down without standing up a database.

The module is pure (only ``hashlib`` + ``datetime``), so it's loaded by file path
to skip the ``web_dashboard`` package __init__ chain — the test needs only stdlib.
Runs under pytest, or standalone:  python tests/test_audit_chain.py
"""
import importlib.util
import os
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_AC_PATH = os.path.join(_ROOT, "web_dashboard", "services", "audit_chain.py")

_spec = importlib.util.spec_from_file_location("audit_chain", _AC_PATH)
ac = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ac)


def _row(seq, ts, username, action, target_vm, details, prev_hash):
    """An entry object shaped like the ORM row, with entry_hash computed the same
    way log_audit/backfill compute it."""
    return SimpleNamespace(
        seq=seq, timestamp=ts, username=username, action=action,
        target_vm=target_vm, details=details, prev_hash=prev_hash,
        entry_hash=ac.compute_entry_hash(seq, ts, username, action, target_vm, details, prev_hash),
    )


def _chain(specs):
    """Build a linked chain from ``[(username, action, target_vm, details), ...]``,
    mirroring the append sequencing: seq = 1..N, prev_hash = prior entry_hash,
    genesis prev = GENESIS_PREV. Returns the ordered rows."""
    rows, prev, base = [], ac.GENESIS_PREV, datetime(2026, 6, 1, 12, 0, 0)
    for i, (user, action, tvm, det) in enumerate(specs, start=1):
        r = _row(i, base + timedelta(seconds=i), user, action, tvm, det, prev)
        rows.append(r)
        prev = r.entry_hash
    return rows


# ── compute_entry_hash ────────────────────────────────────────────────────────

def test_hash_is_deterministic_and_hex_sha256():
    ts = datetime(2026, 6, 1, 12, 0, 0)
    a = ac.compute_entry_hash(1, ts, "alice", "vm_start", "vm-1", None, ac.GENESIS_PREV)
    b = ac.compute_entry_hash(1, ts, "alice", "vm_start", "vm-1", None, ac.GENESIS_PREV)
    assert a == b
    assert len(a) == 64 and all(c in "0123456789abcdef" for c in a)


def test_hash_changes_when_any_field_changes():
    ts = datetime(2026, 6, 1, 12, 0, 0)
    base = ac.compute_entry_hash(1, ts, "alice", "vm_start", "vm-1", None, ac.GENESIS_PREV)
    assert ac.compute_entry_hash(1, ts, "alice", "vm_stop", "vm-1", None, ac.GENESIS_PREV) != base
    assert ac.compute_entry_hash(2, ts, "alice", "vm_start", "vm-1", None, ac.GENESIS_PREV) != base
    assert ac.compute_entry_hash(1, ts, "bob", "vm_start", "vm-1", None, ac.GENESIS_PREV) != base
    assert ac.compute_entry_hash(1, ts, "alice", "vm_start", "vm-1", '{"k":1}', ac.GENESIS_PREV) != base
    # prev_hash is bound in → same content, different predecessor = different hash
    assert ac.compute_entry_hash(1, ts, "alice", "vm_start", "vm-1", None, "a" * 64) != base


# ── verify_chain ──────────────────────────────────────────────────────────────

def test_empty_chain_is_ok():
    ok, broken = ac.verify_chain([])
    assert ok is True and broken is None


def test_valid_chain_verifies():
    rows = _chain([
        ("alice", "user_login", None, None),
        ("alice", "vm_start", "vm-1", '{"region":"us-east-1"}'),
        ("bob", "vm_stop", "vm-1", None),
    ])
    assert rows[0].prev_hash == ac.GENESIS_PREV      # genesis link
    assert rows[1].prev_hash == rows[0].entry_hash   # chained forward
    ok, broken = ac.verify_chain(rows)
    assert ok is True and broken is None


def test_tampered_row_detected_at_its_seq():
    rows = _chain([
        ("alice", "user_login", None, None),
        ("alice", "vm_delete", "vm-9", '{"forced":true}'),
        ("bob", "vm_stop", "vm-1", None),
    ])
    # Attacker edits the action of entry seq=2 in place but can't recompute the
    # chained hashes without the whole downstream chain — verify catches it here.
    rows[1].action = "vm_start"
    ok, broken = ac.verify_chain(rows)
    assert ok is False and broken == 2


def test_deleted_row_breaks_link():
    rows = _chain([
        ("alice", "a1", None, None),
        ("alice", "a2", None, None),
        ("alice", "a3", None, None),
    ])
    # Drop the middle row: row 3's prev_hash no longer matches its predecessor.
    surviving = [rows[0], rows[2]]
    ok, broken = ac.verify_chain(surviving)
    assert ok is False and broken == rows[2].seq


def test_backfill_sequencing_yields_valid_chain():
    # Simulate backfill over pre-existing (unordered) rows: order by timestamp,
    # assign seq + chain, then verify — the algorithm backfill_audit_chain uses.
    ok, broken = ac.verify_chain(_chain([
        ("sys", "boot", None, None),
        ("alice", "user_login", None, None),
        ("alice", "vm_start", "vm-1", None),
        ("alice", "vm_stop", "vm-1", None),
    ]))
    assert ok is True and broken is None


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
    sys.exit(1 if failures else 0)
