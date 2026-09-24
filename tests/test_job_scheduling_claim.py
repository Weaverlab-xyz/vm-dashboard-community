"""A scheduled job must not be claimable before its window opens.

This is the whole engine. Scheduling in this application is not a queue, a cron table or a
separate runner — it is two columns on ``jobs`` plus one predicate
(``job_service.claimable_now``) shared by the two queries that hand work out:

    jobs_worker._claim_one    status='pending'                  → the local runner
    agent_service.lease_one   status='queued' AND agent_id=:id  → a remote agent

So the property worth testing is not "does the column exist" but "does a row with a future
``scheduled_for`` come back from either of them". Both, because a config-management run
against an on-premises target is agent-executed: a change window enforced on only the first
query would be silently unenforced for exactly the hosts most likely to have one.

The parity here is also retrospective. ``lease_one`` was written as a near-copy of
``_claim_one`` and never picked up ``retry_after``, so an agent-bound job that failed
transiently was re-leased immediately instead of waiting out its backoff — a real drift
that existed before this feature and that routing both through one predicate fixes. The
last test pins that the two cannot diverge again.

Run: python tests/test_job_scheduling_claim.py   (or under pytest)
"""
import os
import sys
from datetime import datetime, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

os.environ.setdefault("JWT_SECRET_KEY", "test-secret-scheduling-claim")
# An in-memory database per run: these tests write job rows and claim them, and must not
# touch the developer's vm_cli.db sitting in the repo root.
os.environ["DATABASE_URL"] = "sqlite://"

# Probe the optional THIRD-PARTY dependency by name, then import the first-party
# modules unguarded. Catching anything wider around the `web_dashboard` imports would
# swallow a plain ImportError — "cannot import name claimable_now" — and this file would
# print SKIP and exit 0 forever while the claim gate it exists to protect regressed.
# See tests/test_import_guard_narrowness.py.
try:
    import sqlalchemy  # noqa: F401
except ModuleNotFoundError as exc:  # pragma: no cover — bare interpreter
    try:
        import pytest
        pytest.skip(f"sqlalchemy unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from web_dashboard.database import Base, Job  # noqa: E402
from web_dashboard.services import job_service  # noqa: E402


def _session():
    """A fresh in-memory schema per test.

    StaticPool + a shared connection, because the default SQLite pool hands each session a
    NEW in-memory database — the tables would vanish between the insert and the claim.
    """
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _add(db, **kw):
    """Insert a job row directly, bypassing create_job's expiry/notification machinery."""
    kw.setdefault("job_type", "ansible_local")
    kw.setdefault("status", "pending")
    kw.setdefault("created_at", datetime.utcnow())
    kw.setdefault("created_by", "tester")
    job = Job(**kw)
    db.add(job)
    db.commit()
    return job


def _claimable(db, **extra):
    """The rows either claim query would consider, i.e. the shared predicate alone."""
    return (db.query(Job)
            .filter(*job_service.claimable_now(), **extra)
            .all())


# ── The predicate itself ──────────────────────────────────────────────────────

def test_an_unscheduled_job_is_claimable():
    """The regression that matters most: every row that predates this feature carries
    NULL in all three columns, and NULL must pass. A predicate that excluded NULL would
    not fail loudly — it would wedge the entire queue on the first deploy."""
    db = _session()
    job = _add(db)
    assert [j.id for j in _claimable(db)] == [job.id]


def test_a_job_scheduled_in_the_future_is_not_claimable():
    db = _session()
    _add(db, scheduled_for=datetime.utcnow() + timedelta(hours=6))
    assert _claimable(db) == [], "a job booked for later was claimable now"


def test_a_job_whose_window_has_opened_is_claimable():
    db = _session()
    job = _add(db, scheduled_for=datetime.utcnow() - timedelta(seconds=1))
    assert [j.id for j in _claimable(db)] == [job.id]


def test_an_unapproved_job_is_not_claimable():
    db = _session()
    _add(db, approval_required=True)
    assert _claimable(db) == [], "a job awaiting approval was claimable"


def test_an_approved_job_is_claimable():
    db = _session()
    job = _add(db, approval_required=True, approved_at=datetime.utcnow(),
               approved_by="someone-else")
    assert [j.id for j in _claimable(db)] == [job.id]


def test_approval_required_null_passes():
    """Every row written before the column existed reads NULL, not False.

    The predicate therefore says ``IS NOT TRUE`` rather than ``== False``. Under SQL's
    three-valued logic ``NULL = 0`` is NULL, which is not true, so ``== False`` would
    exclude every pre-existing job — the same queue-wedging failure as above, and the
    reason the column is added as a bare BOOLEAN with no DEFAULT.
    """
    db = _session()
    job = _add(db, approval_required=None)
    assert [j.id for j in _claimable(db)] == [job.id]


def test_a_backing_off_retry_is_not_claimable():
    """retry_after predates this work; the shared predicate must not have dropped it."""
    db = _session()
    _add(db, retry_after=datetime.utcnow() + timedelta(minutes=5), attempts=1)
    assert _claimable(db) == []


def test_the_gates_are_independent():
    """A job can be held by more than one clause at once, and satisfying one is not
    enough. Approving a change does NOT make it run before its window."""
    db = _session()
    _add(db, scheduled_for=datetime.utcnow() + timedelta(hours=2),
         approval_required=True, approved_at=datetime.utcnow())
    assert _claimable(db) == [], "approval released a job ahead of its window"


# ── Both claim queries apply it ───────────────────────────────────────────────

def test_the_local_runner_does_not_claim_a_scheduled_job():
    """Through jobs_worker._claim_one itself, not a reconstruction of its filter."""
    from web_dashboard import jobs_worker
    db = _session()
    _add(db, job_type="ansible_local",
         scheduled_for=datetime.utcnow() + timedelta(hours=6))
    assert jobs_worker._claim_one(db, allowed=("ansible_local",)) is None


def test_the_local_runner_claims_it_once_the_window_opens():
    from web_dashboard import jobs_worker
    db = _session()
    job = _add(db, job_type="ansible_local",
               scheduled_for=datetime.utcnow() - timedelta(seconds=1))
    claimed = jobs_worker._claim_one(db, allowed=("ansible_local",))
    assert claimed is not None and claimed[0] == job.id
    db.refresh(job)
    assert job.status == "running"


def test_the_agent_lease_does_not_claim_a_scheduled_job():
    """The second claim query, and the one a config-management run against an on-prem
    target actually goes through."""
    from web_dashboard.services import agent_service

    class _Agent:
        id = "agent-1"

    db = _session()
    _add(db, job_type="agent_ansible", status="queued", agent_id="agent-1",
         scheduled_for=datetime.utcnow() + timedelta(hours=6))

    real = agent_service.allowed_job_types
    agent_service.allowed_job_types = lambda agent: ("agent_ansible",)
    try:
        assert agent_service.lease_one(db, _Agent()) is None
    finally:
        agent_service.allowed_job_types = real


def test_the_agent_lease_claims_it_once_the_window_opens():
    from web_dashboard.services import agent_service

    class _Agent:
        id = "agent-1"

    db = _session()
    job = _add(db, job_type="agent_ansible", status="queued", agent_id="agent-1",
               scheduled_for=datetime.utcnow() - timedelta(seconds=1))

    real = agent_service.allowed_job_types
    agent_service.allowed_job_types = lambda agent: ("agent_ansible",)
    try:
        leased = agent_service.lease_one(db, _Agent())
    finally:
        agent_service.allowed_job_types = real
    assert leased is not None and leased["id"] == job.id


# ── Ordering ──────────────────────────────────────────────────────────────────

def test_the_queue_orders_by_intended_run_time_not_submission_time():
    """Within one window, the order that matters is when each change was BOOKED to run.

    A change submitted three weeks ago for 05:00 must not jump ahead of one submitted
    yesterday for 02:00 — which is what ordering by `created_at` would do, and what
    `COALESCE(scheduled_for, created_at)` fixes.
    """
    from web_dashboard import jobs_worker
    db = _session()
    now = datetime.utcnow()
    late = _add(db, job_type="ansible_local", created_at=now - timedelta(days=21),
                scheduled_for=now - timedelta(minutes=1))
    early = _add(db, job_type="ansible_local", created_at=now - timedelta(days=1),
                 scheduled_for=now - timedelta(minutes=30))
    claimed = jobs_worker._claim_one(db, allowed=("ansible_local",))
    assert claimed[0] == early.id, (
        "claimed the job submitted first rather than the one booked for the earlier "
        f"slot (got {'late' if claimed[0] == late.id else claimed[0]})")


# ── The predicate may not be written out twice ────────────────────────────────

def test_both_claim_queries_go_through_the_shared_predicate():
    """Structural, because the behavioural tests above can only catch a clause that is
    missing TODAY. The two queries are near-copies and have already drifted once —
    ``retry_after`` was honoured by one of them and not the other for as long as both
    existed. Whoever adds the next clause must not have the option of adding it to one.
    """
    import ast

    offenders = []
    for path, fn_name in (("web_dashboard/jobs_worker.py", "_claim_one"),
                          ("web_dashboard/services/agent_service.py", "lease_one")):
        src = open(os.path.join(_ROOT, path), encoding="utf-8").read()
        fn = next((n for n in ast.walk(ast.parse(src))
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and n.name == fn_name), None)
        assert fn is not None, f"{fn_name} not found in {path}"
        calls = [n for n in ast.walk(fn)
                 if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Attribute)
                 and n.func.attr == "claimable_now"]
        if not calls:
            offenders.append(f"{path}:{fn_name} does not call job_service.claimable_now")
    assert not offenders, "; ".join(offenders)


def _run():
    tests = [(n, o) for n, o in sorted(globals().items())
             if n.startswith("test_") and callable(o)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"ok   {name}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {name}: {exc}")
        except Exception as exc:                       # noqa: BLE001
            failed += 1
            print(f"ERROR {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run())
