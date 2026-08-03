"""_claim_one against a real database (SQLite), exercising the actual SQL.

tests/test_worker_supervisor.py fakes the claim to test the scheduling around it. This
file does the opposite: real tables, real rows, real queries — because the two properties
the whole design rests on are properties of that SQL, not of the loop.

The headline tests:

  * :func:`test_two_claimants_racing_one_row_produce_exactly_one_winner` — the
    ``UPDATE ... WHERE status='pending'`` rowcount IS the lock. It is what lets several
    tasks in one process, and several worker replicas, share a queue without a job ever
    running twice. It is deliberately not ``SKIP LOCKED``, so it has to be proven on
    SQLite as well as PostgreSQL — that portability is the entire reason for the design.
  * :func:`test_the_allowlist_narrows_the_query_not_the_result` — the tier allowlist must
    reach the database. Fetching the oldest pending row of any type and rejecting it in
    Python would re-SELECT that same unclaimable row on every attempt, forever.
  * :func:`test_queued_rows_are_never_claimed` — the behavioural half of the static check
    in test_worker_dispatch: bulk children and agent-owned jobs are ``queued``, and if the
    claim ever picked them up a bulk deploy would double-execute.

Run: python tests/test_worker_claim.py   (or under pytest)
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta

# Must be set BEFORE web_dashboard.config is imported — it is read at import time to
# build the engine.
_DB_DIR = tempfile.mkdtemp(prefix="worker_claim_")
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(_DB_DIR, 'claim.db')}".replace("\\", "/")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from web_dashboard import jobs_worker as jw                                  # noqa: E402
from web_dashboard.database import Base, Job, SessionLocal, engine           # noqa: E402

Base.metadata.create_all(bind=engine)

_T0 = datetime(2026, 1, 1, 12, 0, 0)


def _seed(*rows):
    """rows: (job_id, job_type, status, minutes_old). Oldest first is the caller's job —
    created_at is what the claim orders by."""
    db = SessionLocal()
    try:
        db.query(Job).delete()
        for job_id, job_type, status, age_min in rows:
            db.add(Job(id=job_id, job_type=job_type, status=status,
                       created_at=_T0 - timedelta(minutes=age_min)))
        db.commit()
    finally:
        db.close()


def _status(job_id):
    db = SessionLocal()
    try:
        row = db.query(Job).filter(Job.id == job_id).first()
        return row.status if row else None
    finally:
        db.close()


def _claim(allowed=jw.HANDLED_TYPES):
    db = SessionLocal()
    try:
        return jw._claim_one(db, allowed)
    finally:
        db.close()


# ── the lock ─────────────────────────────────────────────────────────────────

def test_two_claimants_racing_one_row_produce_exactly_one_winner():
    """One pending row, two sessions. The rowcount lock means the loser sees `claimed == 0`
    and moves on — no job ever runs twice, which is what makes both in-process concurrency
    and replica scale-out safe. Proven here on SQLite because there is no SKIP LOCKED."""
    _seed(("only", "expiry_sweep", "pending", 5))
    a, b = SessionLocal(), SessionLocal()
    try:
        first = jw._claim_one(a, jw.HANDLED_TYPES)
        second = jw._claim_one(b, jw.HANDLED_TYPES)
    finally:
        a.close()
        b.close()
    winners = [c for c in (first, second) if c is not None]
    assert len(winners) == 1, (
        f"both claimants won the same row — the job would run twice: {first}, {second}")
    assert winners[0][0] == "only"
    assert _status("only") == "running"


def test_a_claimed_row_is_marked_running_with_timestamps():
    """reconcile_stale_jobs keys off updated_at, so a claim that left it NULL would make
    the job look stale the moment a sibling worker started up."""
    _seed(("j", "expiry_sweep", "pending", 1))
    assert _claim() is not None
    db = SessionLocal()
    try:
        row = db.query(Job).filter(Job.id == "j").first()
        assert row.status == "running"
        assert row.started_at is not None, "started_at was not set on claim"
        assert row.updated_at is not None, (
            "updated_at was not set on claim — the row has no heartbeat and the next "
            "worker's startup reconcile would fail it")
    finally:
        db.close()


# ── the allowlist ────────────────────────────────────────────────────────────

def test_the_allowlist_narrows_the_query_not_the_result():
    """An OLD heavy job and a NEW light one. With only the light tier allowed, the claim
    must return the light job — not None, and not the heavy one. If `allowed` were applied
    after the SELECT this returns None forever and the light job never runs."""
    _seed(("old_heavy", "k8s_provision", "pending", 60),
          ("new_light", "aws_export_image", "pending", 1))
    claim = _claim(jw.LIGHT_TYPES)
    assert claim is not None, (
        "nothing was claimed although a light job was queued — the allowlist is being "
        "applied to the result instead of to the query")
    assert claim[0] == "new_light"
    assert _status("old_heavy") == "pending", "the heavy job was claimed out of its tier"


def test_an_empty_allowlist_claims_nothing():
    """Every tier full: the supervisor skips the round trip entirely."""
    _seed(("j", "expiry_sweep", "pending", 1))
    assert jw._claim_one(SessionLocal(), ()) is None
    assert _status("j") == "pending"


def test_oldest_first_within_the_allowlist():
    """FIFO still holds inside a tier — the overtaking is strictly across tiers."""
    _seed(("newer", "ami_copy", "pending", 1),
          ("older", "aws_export_image", "pending", 30),
          ("oldest", "gce_capture_image", "pending", 90))
    assert _claim(jw.LIGHT_TYPES)[0] == "oldest"
    assert _claim(jw.LIGHT_TYPES)[0] == "older"
    assert _claim(jw.LIGHT_TYPES)[0] == "newer"


def test_a_type_outside_handled_types_is_never_claimed():
    _seed(("foreign", "agent_discover", "pending", 1))
    assert _claim() is None
    assert _status("foreign") == "pending"


# ── statuses ─────────────────────────────────────────────────────────────────

def test_queued_rows_are_never_claimed():
    """`queued` is how a bulk parent's children and an agent's jobs are parked. The
    behavioural half of test_worker_dispatch's static check: if the claim widened to
    include it, every bulk deploy would run its children twice — once via the parent, once
    via the loop — and reconcile_stale_jobs skips `pending`, so nothing would notice."""
    _seed(("child", "ec2_deploy", "queued", 5))
    assert _claim() is None
    assert _status("child") == "queued"


def test_terminal_rows_are_never_reclaimed():
    _seed(("done", "expiry_sweep", "completed", 5),
          ("bad", "expiry_sweep", "failed", 4),
          ("gone", "expiry_sweep", "cancelled", 3),
          ("live", "expiry_sweep", "running", 2))
    assert _claim() is None, "a terminal or already-running row was claimed"


def test_an_empty_queue_returns_none():
    _seed()
    assert _claim() is None


# ── the retry bound ──────────────────────────────────────────────────────────

def test_the_retry_gives_up_rather_than_spinning():
    """If a row stayed selectable but unclaimable, the old `while True` would spin the
    supervisor at full tilt. The bound turns that into a logged giveup the next poll tick
    retries. Simulated by making the UPDATE never match."""
    _seed(("j", "expiry_sweep", "pending", 1))
    db = SessionLocal()
    real_query = db.query
    calls = {"n": 0}

    class _NoMatchUpdate:
        def __init__(self, q):
            self._q = q

        def filter(self, *a, **k):
            return _NoMatchUpdate(self._q.filter(*a, **k))

        def order_by(self, *a, **k):
            return _NoMatchUpdate(self._q.order_by(*a, **k))

        def first(self):
            return self._q.first()

        def update(self, *a, **k):
            calls["n"] += 1
            return 0                      # always "lost the race"

    try:
        db.query = lambda *a, **k: _NoMatchUpdate(real_query(*a, **k))
        assert jw._claim_one(db, jw.HANDLED_TYPES, max_attempts=4) is None
    finally:
        db.query = real_query
        db.close()
    assert calls["n"] == 4, (
        f"expected exactly max_attempts=4 UPDATE attempts, got {calls['n']} — the retry "
        "is either unbounded or not retrying at all")


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
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
