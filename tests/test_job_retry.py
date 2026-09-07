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


# ── The dead-letter notification ──────────────────────────────────────────────

class _capture:
    """Capture the NotificationEvent handed to emit_safe instead of queueing it."""

    def __init__(self, boom=False):
        self.boom, self.events = boom, []

    def __enter__(self):
        import web_dashboard.services.notification_service as ns
        self.ns, self.saved = ns, ns.emit_safe

        def _emit(db, event):
            self.events.append(event)
            if self.boom:
                # What the REAL emit_safe does on failure: it rolls the session back. That
                # is the whole reason the emit has to happen after the commit.
                db.rollback()
                return 0
            return 1

        ns.emit_safe = _emit
        return self

    def __exit__(self, *exc):
        self.ns.emit_safe = self.saved
        return False

    def of_type(self, event_type):
        """The captured events of one type.

        `set_failed` emits TWO things on this path — the dead letter, then the
        pre-existing `job.failed` — so `events[-1]` is the wrong one and answering a
        question about the dead letter with it would pass or fail for the wrong reason.
        """
        return [e for e in self.events if e.event_type == event_type]


def _dead_letter(job_id, capture, error):
    """Fail a job until it dead-letters, with emit_safe captured."""
    with _enabled():
        for _ in range(5):
            db = SessionLocal()
            try:
                row = db.query(Job).filter(Job.id == job_id).first()
                if row.status == "failed":
                    return
                row.status = "running"
                db.commit()
            finally:
                db.close()
            job_service.set_failed(SessionLocal(), job_id, error)


def test_the_error_text_never_reaches_the_notification():
    """emit_safe queues an outbox row the worker drains to a WEBHOOK — Slack, Teams, an
    arbitrary endpoint. Job errors here echo request parameters from runners that handle
    SSH keys, deploy keys, PRA client secrets and generated Azure admin passwords. The
    error is already on the row and rendered on the job page, one click away through the
    URL, so pushing it to a third party buys nothing."""
    secret = "SENTINEL-ssh-private-key-AKIAEXAMPLE-hunter2"
    _job("dl1")
    with _capture() as cap:
        _dead_letter("dl1", cap, f"ThrottlingException: Rate exceeded [{secret}]")

    letters = cap.of_type("job.dead_lettered")
    assert letters, "the dead letter must still be announced"
    # Asserted over the WHOLE serialised event, not named fields: a field added later must
    # not be able to reintroduce this quietly.
    blob = repr(vars(letters[-1]))
    assert secret not in blob, f"the error text reached the notification: {blob[:400]}"
    assert "Rate exceeded" not in blob, blob[:400]

    # The boundary of this change, stated rather than implied. The pre-existing
    # `job.failed` that follows still sends `error_message[:1000]` to the same webhooks —
    # the same class of exposure, in code this change does not touch. Asserting it here
    # keeps the scope honest: if someone later fixes that too, this line is what tells
    # them the omission above was deliberate and not an oversight they are undoing.
    failed = cap.of_type("job.failed")
    assert failed, "the ordinary failure notification must still be sent"
    assert secret in repr(vars(failed[-1])), (
        "notify_job_failed's exposure is pre-existing and out of scope here; if it has "
        "been fixed, delete this assertion rather than reintroducing the error text")


def test_the_dead_letter_still_says_what_an_operator_needs():
    """Dropping the error must not leave a notification nobody can act on."""
    _job("dl2", job_type="ec2_power", created_by="alice")
    with _capture() as cap:
        _dead_letter("dl2", cap, THROTTLE)

    letters = cap.of_type("job.dead_lettered")
    assert letters, "the dead letter must still be announced"
    event = letters[-1]
    assert "ec2_power" in event.title
    assert event.url == "/jobs/dl2", event.url
    assert event.resource_id == "job:dl2", "routing fields make it reach the same subscribers"
    assert event.resource_kind == "job"
    assert event.fields.get("Attempts") == retry_policy.DEFAULT_MAX_ATTEMPTS
    assert event.fields.get("Started by") == "alice"


def test_a_failing_notifier_cannot_lose_the_failure():
    """The bug this ordering exists to prevent. emit_safe ROLLS THE SESSION BACK when a
    notification fails — its own docstring says every emit site sits after the commit of
    the thing it reports. Announced before the commit, a broken webhook would discard the
    status, error and completed_at writes and leave the job `running` forever."""
    _job("dl3")
    with _capture(boom=True) as cap:
        _dead_letter("dl3", cap, THROTTLE)

    assert cap.events, "the emit must have been attempted"
    row = _read("dl3")
    assert row.status == "failed", "a broken notifier lost the failure"
    assert row.error_message == THROTTLE, "…and lost the reason with it"
    assert row.completed_at is not None


def test_the_emit_happens_after_the_commit():
    """Structural, because the runtime test above only catches it if the stub rolls back
    exactly as the real one does — and the next person to touch this will be tempted to
    move the announcement back beside the branch that decides it."""
    import ast
    src = open(os.path.join(_ROOT, "web_dashboard/services/job_service.py"),
               encoding="utf-8").read()
    fn = next(f for f in ast.walk(ast.parse(src)) if isinstance(f, ast.FunctionDef)
              and f.name == "set_failed")
    commits = [n.lineno for n in ast.walk(fn) if isinstance(n, ast.Call)
               and isinstance(n.func, ast.Attribute) and n.func.attr == "commit"]
    emits = [n.lineno for n in ast.walk(fn) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name) and n.func.id == "_raise_dead_letter"]
    assert commits and emits, (commits, emits)
    assert min(commits) < min(emits), \
        "_raise_dead_letter must run AFTER db.commit() — emit_safe rolls back on failure"


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
        # Everything the PRE-EXISTING path reads, present. The point of the stub is that
        # `attempts` and `retry_after` are the ONLY things missing from it — with these
        # absent too, notify_job_failed swallows an AttributeError of its own and the
        # assertion below would pass without the flag-off path ever being the reason.
        vm_path = None
        cloud_resource_id = None
        workgroup = ""
        created_by = "someone"

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

        def add(self, *a):
            pass

        def rollback(self):
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
