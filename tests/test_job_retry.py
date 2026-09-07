"""Retrying transiently-failed jobs, and the far more important question of which ones not to.

Sixty-plus job types and no retry at all, against failures that are transient by nature: an
API throttle, a capacity shortfall, a 5xx, a token that expired mid-run. `retry_policy`
decides, `job_service.set_failed` is the single hook every runner already funnels through,
and `jobs_worker._claim_one` honours the backoff.

**The test this file exists for is `test_a_deploy_is_never_retried`.** `aws_vm_service`
calls `set_failed` and does NOT clean up an instance it may already have launched — Azure's
`_deploy_vm_sync` does via `_best_effort_cleanup`; the AWS path does not. Retrying a failed
`ec2_deploy` would launch a SECOND instance while the first is still running and billing,
and would tell the operator the retry had helped. Every other test here is ordinary
diligence; that one is the reason the type list is an allowlist.

Two more things pinned deliberately:

  * **No new status.** 108 sites in this tree compare against `"failed"` and
    `ACTIVE_STATUSES` is a three-tuple. A fourth terminal state would mean auditing all of
    them, and the first one missed is a job some page silently stops showing. The
    dead-letter tail is `failed AND attempts > 0` — a query.
  * **Flag off is byte-identical to today.** The claim query is polled every two seconds by
    every worker replica, and a failed job becoming non-terminal is a real behaviour change.
    An upgrade must inherit neither.

Run: python tests/test_job_retry.py   (or under pytest)
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="job-retry-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDB}"
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-job-retry-tests")

try:
    from web_dashboard.database import Base, Job, SessionLocal, engine
    from web_dashboard.services import job_service, retry_policy
except Exception as exc:  # pragma: no cover — app deps missing
    try:
        import pytest
        pytest.skip(f"app dependencies unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)

Base.metadata.create_all(bind=engine)

THROTTLE = "ThrottlingException: Rate exceeded"


class _enabled:
    """Turn retry on for one test. The flag reads config_service, so this stubs that."""

    def __init__(self, on=True, limit=None):
        self.on, self.limit = on, limit

    def __enter__(self):
        self.saved = (job_service._retry_enabled, job_service._retry_limit)
        job_service._retry_enabled = lambda: self.on
        job_service._retry_limit = lambda: self.limit
        return self

    def __exit__(self, *exc):
        job_service._retry_enabled, job_service._retry_limit = self.saved
        return False


def _job(job_id, job_type="ec2_power", **cols):
    db = SessionLocal()
    try:
        db.query(Job).filter(Job.id == job_id).delete()
        job = Job(id=job_id, job_type=job_type, status="running", created_by="alice",
                  created_at=datetime.utcnow(), started_at=datetime.utcnow())
        for k, v in cols.items():
            setattr(job, k, v)
        db.add(job)
        db.commit()
    finally:
        db.close()


def _read(job_id):
    db = SessionLocal()
    try:
        return db.query(Job).filter(Job.id == job_id).first()
    finally:
        db.close()


# ── The one that matters ──────────────────────────────────────────────────────

def test_a_deploy_is_never_retried():
    """`aws_vm_service` fails without cleaning up an instance it may already have
    launched. A retry would launch a second one, bill for both, and report success —
    so no deploy is on the allowlist, and this asserts it against the LIVE job-type
    inventory so a type added later cannot quietly opt in."""
    from web_dashboard.jobs_worker import HANDLED_TYPES

    deploys = [t for t in HANDLED_TYPES if "deploy" in t]
    assert len(deploys) >= 10, f"expected the deploy family, found {deploys}"
    for job_type in deploys:
        assert not retry_policy.retryable_type(job_type), job_type
        assert not retry_policy.should_retry(job_type, THROTTLE, 0), job_type

    # And the same for the other non-re-entrant families, named rather than inferred.
    for job_type in HANDLED_TYPES:
        if any(k in job_type for k in ("image", "capture", "export", "copy")):
            assert not retry_policy.retryable_type(job_type), job_type


def test_every_retryable_type_is_a_real_handled_job_type():
    """A typo here is a type that silently never retries — the allowlist would look
    populated and do nothing."""
    from web_dashboard.jobs_worker import HANDLED_TYPES

    unknown = sorted(retry_policy.RETRYABLE_TYPES - set(HANDLED_TYPES))
    assert not unknown, f"listed but not handled by the worker: {unknown}"
    assert retry_policy.RETRYABLE_TYPES, "an empty allowlist disables the feature silently"


# ── Which failures ────────────────────────────────────────────────────────────

def test_only_recognised_transient_errors_retry():
    """An allowlist, not a denylist. A denylist fails open on the next unfamiliar error,
    which is how a permanent misconfiguration gets retried and reaches the operator
    twenty minutes late."""
    for transient in ("ThrottlingException: Rate exceeded",
                      "InsufficientInstanceCapacity",
                      "HTTP 503 Service Unavailable",
                      "The security token included in the request is expired",
                      "Read timed out"):
        assert retry_policy.is_transient(transient), transient

    for permanent in ("InvalidAMIID.NotFound: The image id does not exist",
                      "AccessDenied: not authorized to perform ec2:StopInstances",
                      "ValidationError: instance type is not valid",
                      "Something nobody has ever seen before",
                      ""):
        assert not retry_policy.is_transient(permanent), permanent


def test_a_permanent_error_is_not_rescued_by_a_transient_looking_substring():
    """Request ids and ARNs contain digits. "503" inside a validation error must not
    make it retryable."""
    assert not retry_policy.is_transient(
        "ValidationError: request 503abc-timeout was rejected")
    assert not retry_policy.is_transient("QuotaExceeded: too many requests this month")


# ── Attempts and backoff ──────────────────────────────────────────────────────

def test_attempts_are_bounded_and_the_last_failure_is_terminal():
    assert retry_policy.should_retry("ec2_power", THROTTLE, 0)
    assert retry_policy.should_retry("ec2_power", THROTTLE, 1)
    assert not retry_policy.should_retry("ec2_power", THROTTLE, 2), \
        "3 attempts means run, retry, retry — then stop"
    assert not retry_policy.should_retry("ec2_power", THROTTLE, 99)


def test_backoff_grows_and_never_raises_past_the_schedule():
    steps = [retry_policy.backoff_seconds(i) for i in range(len(retry_policy.BACKOFF_SECONDS))]
    assert steps == list(retry_policy.BACKOFF_SECONDS)
    assert sorted(steps) == steps, "backoff must not go backwards"
    # Past the end it clamps rather than raising — this runs on a failure path, the worst
    # possible place for an IndexError.
    assert retry_policy.backoff_seconds(99) == retry_policy.BACKOFF_SECONDS[-1]
    assert retry_policy.backoff_seconds(-1) == retry_policy.BACKOFF_SECONDS[0]


def test_max_attempts_is_clamped():
    assert retry_policy.max_attempts(0) == retry_policy.MIN_MAX_ATTEMPTS
    assert retry_policy.max_attempts(999) == retry_policy.MAX_MAX_ATTEMPTS
    assert retry_policy.max_attempts("nonsense") == retry_policy.DEFAULT_MAX_ATTEMPTS
    assert retry_policy.max_attempts(None) == retry_policy.DEFAULT_MAX_ATTEMPTS


# ── set_failed: the one hook ──────────────────────────────────────────────────

def test_a_transient_failure_is_requeued_rather_than_failed():
    _job("j1")
    with _enabled():
        job_service.set_failed(SessionLocal(), "j1", THROTTLE)
    row = _read("j1")
    assert row.status == "pending", "a retryable failure goes back on the queue"
    assert row.attempts == 1
    assert row.retry_after is not None and row.retry_after > datetime.utcnow()
    assert row.error_message == THROTTLE, "the reason must survive the requeue"
    # completed_at must stay NULL: the run has not completed, and stamping it would make a
    # requeued job read as finished to every duration and staleness check in the tree.
    assert row.completed_at is None
    assert row.started_at is None, "a requeued job has not started its next attempt"


def test_a_permanent_failure_is_terminal_on_the_first_try():
    _job("j2")
    with _enabled():
        job_service.set_failed(SessionLocal(), "j2", "InvalidAMIID.NotFound")
    row = _read("j2")
    assert row.status == "failed" and row.attempts == 0
    assert row.completed_at is not None


def test_a_deploy_failure_is_terminal_even_with_a_transient_error():
    """The end-to-end version of the test this file exists for."""
    _job("j3", job_type="ec2_deploy")
    with _enabled():
        job_service.set_failed(SessionLocal(), "j3", THROTTLE)
    row = _read("j3")
    assert row.status == "failed", "a deploy must never be requeued"
    assert row.attempts == 0


def test_attempts_exhaust_into_a_dead_letter():
    _job("j4")
    with _enabled():
        for _ in range(5):
            db = SessionLocal()
            try:
                # Put it back to running the way the worker would, so each pass is a real
                # attempt rather than a repeat of the same terminal write.
                row = db.query(Job).filter(Job.id == "j4").first()
                if row.status == "failed":
                    break
                row.status = "running"
                db.commit()
            finally:
                db.close()
            job_service.set_failed(SessionLocal(), "j4", THROTTLE)
    row = _read("j4")
    assert row.status == "failed", "it must stop eventually"
    assert row.attempts == retry_policy.DEFAULT_MAX_ATTEMPTS - 1, row.attempts
    assert row.completed_at is not None


# ── The flag ──────────────────────────────────────────────────────────────────

def test_with_the_flag_off_nothing_changes():
    """The claim query is polled every two seconds by every worker replica and a failed
    job becoming non-terminal is a real behaviour change. An upgrade inherits neither."""
    from web_dashboard.config import settings
    assert settings.job_retry_enabled is False, "must ship off"

    _job("j5")
    with _enabled(on=False):
        job_service.set_failed(SessionLocal(), "j5", THROTTLE)
    row = _read("j5")
    assert row.status == "failed"
    assert row.attempts == 0 and row.retry_after is None


def test_an_unreadable_flag_cannot_break_the_failure_path():
    """`set_failed` is the path a job takes when something has ALREADY gone wrong, and the
    config store is backed by the same database that may be the thing going wrong. A config
    read that threw here would throw out of set_failed itself and leave the row stuck in
    `running` forever — strictly worse than the failure it was recording."""
    saved = job_service._retry_enabled
    import web_dashboard.services.config_service as cfg
    saved_get = cfg.get_bool

    def _boom(*a, **k):
        raise RuntimeError("SENTINEL-config-store-unreachable")

    cfg.get_bool = _boom
    job_service._retry_enabled = saved            # use the real one, with config broken
    try:
        _job("j6")
        job_service.set_failed(SessionLocal(), "j6", THROTTLE)
        row = _read("j6")
        assert row.status == "failed", "the job must still be marked failed"
        assert row.error_message == THROTTLE
    finally:
        cfg.get_bool = saved_get
        job_service._retry_enabled = saved


def test_with_the_flag_off_the_new_columns_are_never_even_read():
    """"Off by default" has to mean untouched on a path all 80-odd runners funnel through.
    A row-like object with no `attempts` attribute must survive set_failed unchanged —
    which is exactly what tests/test_job_batches.py passes it."""
    class _RowWithoutRetryColumns:
        id = "fake"
        job_type = "ec2_power"
        status = "running"
        error_message = None
        completed_at = None
        updated_at = None
        metadata_dict = {}

    row = _RowWithoutRetryColumns()

    class _DB:
        def query(self, *a):
            return self

        def filter(self, *a):
            return self

        def first(self):
            return row

        def commit(self):
            pass

        def refresh(self, *a):
            pass

    with _enabled(on=False):
        job_service.set_failed(_DB(), "fake", THROTTLE)
    assert row.status == "failed"
    assert not hasattr(row, "attempts"), "the flag-off path must not touch attempts"


# ── The claim honours the backoff ─────────────────────────────────────────────

def test_the_claim_skips_a_job_whose_backoff_has_not_passed():
    from web_dashboard import jobs_worker

    db = SessionLocal()
    try:
        db.query(Job).delete()
        db.commit()
    finally:
        db.close()

    _job("waiting", status="pending", attempts=1,
         retry_after=datetime.utcnow() + timedelta(minutes=5))
    db = SessionLocal()
    try:
        assert jobs_worker._claim_one(db, allowed=("ec2_power",)) is None, \
            "a backing-off job must not be claimed"
    finally:
        db.close()

    # Once the backoff has passed it is claimable again.
    _job("ready", status="pending", attempts=1,
         retry_after=datetime.utcnow() - timedelta(seconds=1))
    db = SessionLocal()
    try:
        claimed = jobs_worker._claim_one(db, allowed=("ec2_power",))
        assert claimed is not None and claimed[0] == "ready", claimed
    finally:
        db.close()


def test_a_job_that_never_failed_is_claimable():
    """retry_after is NULL for every row that exists today. The added clause must not
    change what the queue does for them."""
    from web_dashboard import jobs_worker

    db = SessionLocal()
    try:
        db.query(Job).delete()
        db.commit()
    finally:
        db.close()
    _job("fresh", status="pending")
    db = SessionLocal()
    try:
        claimed = jobs_worker._claim_one(db, allowed=("ec2_power",))
        assert claimed is not None and claimed[0] == "fresh"
    finally:
        db.close()


# ── The dead-letter tail is a query, not a status ─────────────────────────────

def test_the_dead_letter_query_finds_exhausted_jobs_and_not_fresh_failures():
    db = SessionLocal()
    try:
        db.query(Job).delete()
        db.commit()
    finally:
        db.close()
    _job("exhausted", status="failed", attempts=2)
    _job("first-failure", status="failed", attempts=0)
    _job("still-running", status="running", attempts=1)

    db = SessionLocal()
    try:
        rows, total = job_service.list_jobs(db, dead_lettered=True)
        assert [r.id for r in rows] == ["exhausted"], [r.id for r in rows]
        assert total == 1
        # And the unfiltered list still shows everything, so the tail is a lens rather
        # than a hiding place.
        _rows, all_total = job_service.list_jobs(db)
        assert all_total == 3
    finally:
        db.close()


def test_no_fourth_status_was_introduced():
    """The design constraint. 108 sites compare against "failed"; a new terminal state
    would mean auditing every one, and the first missed is a job that silently vanishes
    from some page."""
    assert job_service.ACTIVE_STATUSES == ("queued", "pending", "running")
    for module in ("web_dashboard/services/job_service.py",
                   "web_dashboard/services/retry_policy.py",
                   "web_dashboard/jobs_worker.py"):
        src = open(os.path.join(_ROOT, module), encoding="utf-8").read()
        for invented in ('"dead"', "'dead'", '"dead_letter"', '"retrying"', '"retry"'):
            assert f"status = {invented}" not in src and f"status={invented}" not in src, \
                f"{module} introduces a status {invented}"


def test_the_dead_letter_event_is_in_the_default_set():
    from web_dashboard.services import notify_policy
    events = notify_policy.parse_event_types(notify_policy.DEFAULT_EVENT_TYPES)
    assert "job.dead_lettered" in events
    assert notify_policy.EVENT_SEVERITY.get("job.dead_lettered") == "critical"
    # Distinct from job.failed: one fires on the first failure, the other only when every
    # attempt is spent. An operator filtering for one must not be handed the other.
    assert "job.failed" in notify_policy.EVENT_SEVERITY


def test_describe_explains_a_refusal_in_terms_an_operator_can_act_on():
    d = retry_policy.describe("ec2_deploy", THROTTLE, 0)
    assert d["will_retry"] is False and "re-entrant" in d["reason"]
    d = retry_policy.describe("ec2_power", "InvalidAMIID.NotFound", 0)
    assert d["will_retry"] is False and "transient" in d["reason"]
    d = retry_policy.describe("ec2_power", THROTTLE, 99)
    assert d["will_retry"] is False and "no attempts left" in d["reason"]
    d = retry_policy.describe("ec2_power", THROTTLE, 0)
    assert d["will_retry"] is True and d["retry_in_seconds"] == retry_policy.BACKOFF_SECONDS[0]


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
            traceback.print_exc()
    sys.exit(1 if failures else 0)
