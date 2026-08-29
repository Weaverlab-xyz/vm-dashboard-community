"""The POV reconcile sweep: keeping the dashboard's view of a lab platform true.

`PovEnvironment.runstate` used to be written only at provision and at an explicit power
action. So `suspend_on_idle` — the single biggest lever on lab-platform spend — would
suspend an environment and the row would go on claiming `running` forever, while the POV
page hid the Start button on the strength of it. What is pinned here is the sweep that ends
that, and the two ways it could do more harm than good:

  * **Absence from the listing is not deletion.** The listing is project-scoped, so an
    environment outside the configured project is invisible and perfectly alive. Only a
    404 from a DIRECT read may set `platform_missing` — a rate limit, a timeout or a 500
    must not.
  * **Nothing is ever destroyed.** A missing environment is flagged for a human. The row
    holds the only record of which tenants that POV was wired into.
  * **A row a job owns is not touched.** A provision or destroy writes `runstate` itself,
    and a listing read taken before its last transition would overwrite it.
  * **The flag un-latches.** An environment that becomes visible again — the usual cause
    is a changed Project ID — clears, because a flag that only ever latches on is one
    nobody trusts.

Runs against SQLite in a temp file with a stubbed adapter. No network, no app.

Runs under pytest, or standalone:
    python tests/test_pov_reconcile.py
"""
import asyncio
import os
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-pov-reconcile")

# A temp database, set BEFORE web_dashboard.database is imported — the suite shares a real
# vm_cli.db and a test that writes rows into it mutates the dev install.
_DB_FD, _DB_PATH = tempfile.mkstemp(suffix=".db")
os.close(_DB_FD)
os.environ["DATABASE_URL"] = f"sqlite:///{_DB_PATH}"

from web_dashboard import database as db_mod  # noqa: E402
from web_dashboard.database import Base, PovEnvironment  # noqa: E402
from web_dashboard.services import pov_reconcile as pr  # noqa: E402

Base.metadata.create_all(bind=db_mod.engine)


def _session():
    return db_mod.SessionLocal()


def _clean(db):
    db.query(PovEnvironment).delete()
    db.commit()


def _env(db, **kw):
    kw.setdefault("platform", "skytap")
    kw.setdefault("name", "acme")
    kw.setdefault("status", "active")
    kw.setdefault("platform_environment_id", "env-1")
    row = PovEnvironment(**kw)
    db.add(row)
    db.commit()
    return row


class _Stub:
    """The adapter surface reconcile() uses."""

    def __init__(self, listing=None, *, configured=True, get_raises=None):
        self.listing = listing if listing is not None else []
        self._configured = configured
        self._get_raises = get_raises
        self.gets = []

    def configured(self):
        return self._configured

    async def list_environments(self):
        return list(self.listing)

    async def get_environment(self, env_id):
        self.gets.append(env_id)
        if self._get_raises is not None:
            raise self._get_raises
        return {"id": env_id}


def _reconcile(db, stub):
    saved = pr.lab_platforms.adapter
    pr.lab_platforms.adapter = lambda _p: stub
    try:
        return asyncio.run(pr.reconcile(db, "skytap"))
    finally:
        pr.lab_platforms.adapter = saved


# ── the bug this exists for ──────────────────────────────────────────────────

def test_a_platform_suspend_reaches_the_row():
    """The whole point. Nothing else in this codebase notices `suspend_on_idle` firing."""
    db = _session()
    try:
        _clean(db)
        row = _env(db, runstate="running")
        out = _reconcile(db, _Stub([{"id": "env-1", "runstate": "suspended"}]))
        db.refresh(row)
        assert row.runstate == "suspended", row.runstate
        assert out["updated"] == 1, out
        assert row.platform_seen_at is not None, "the row does not say when it was confirmed"
    finally:
        _clean(db)
        db.close()


def test_the_idle_timer_and_rate_limit_land_on_the_row():
    db = _session()
    try:
        _clean(db)
        row = _env(db, runstate="running")
        _reconcile(db, _Stub([{"id": "env-1", "runstate": "running",
                               "rate_limited": True, "suspend_on_idle": 3600}]))
        db.refresh(row)
        assert row.rate_limited is True, row.rate_limited
        assert row.suspend_on_idle_seconds == 3600, row.suspend_on_idle_seconds
    finally:
        _clean(db)
        db.close()


def test_an_unchanged_environment_is_not_counted_as_updated():
    db = _session()
    try:
        _clean(db)
        _env(db, runstate="running", rate_limited=False)
        out = _reconcile(db, _Stub([{"id": "env-1", "runstate": "running"}]))
        assert out["checked"] == 1, out
        assert out["updated"] == 0, out
    finally:
        _clean(db)
        db.close()


# ── drift, and the refusal to guess ──────────────────────────────────────────

def test_absence_from_the_listing_is_confirmed_before_it_is_believed():
    """The listing is project-scoped. An environment outside the project is invisible and
    perfectly alive, so only a 404 from a direct read counts."""
    db = _session()
    try:
        _clean(db)
        row = _env(db)
        stub = _Stub([], get_raises=Exception("Skytap GET /v2/configurations/env-1 "
                                              "failed (404): not found"))
        out = _reconcile(db, stub)
        db.refresh(row)
        assert stub.gets == ["env-1"], "the sweep did not confirm with a direct read"
        assert row.platform_missing is True, row.platform_missing
        assert out["missing"] == 1, out
    finally:
        _clean(db)
        db.close()


def test_a_rate_limit_on_the_confirming_read_is_not_evidence_of_deletion():
    """A bad afternoon on the platform's side must not paint an estate as missing."""
    db = _session()
    try:
        _clean(db)
        row = _env(db)
        stub = _Stub([], get_raises=Exception(
            "Skytap is still busy after 6 retries on GET /v2/configurations/env-1 (423)"))
        out = _reconcile(db, stub)
        db.refresh(row)
        assert row.platform_missing is not True, "a 423 was treated as a deletion"
        assert out["missing"] == 0, out
    finally:
        _clean(db)
        db.close()


def test_an_environment_outside_the_project_that_still_reads_is_not_flagged():
    db = _session()
    try:
        _clean(db)
        row = _env(db)
        # Absent from the scoped listing, but a direct read answers fine.
        out = _reconcile(db, _Stub([]))
        db.refresh(row)
        assert row.platform_missing is not True, row.platform_missing
        assert out["missing"] == 0, out
    finally:
        _clean(db)
        db.close()


def test_the_missing_flag_un_latches_when_it_comes_back():
    """Usual cause is a changed Project ID. A flag that only latches on is one nobody
    trusts."""
    db = _session()
    try:
        _clean(db)
        row = _env(db, platform_missing=True)
        out = _reconcile(db, _Stub([{"id": "env-1", "runstate": "running"}]))
        db.refresh(row)
        assert row.platform_missing is False, row.platform_missing
        assert out["recovered"] == 1, out
    finally:
        _clean(db)
        db.close()


def test_the_sweep_never_deletes_a_row():
    """The row holds the only record of which tenants this POV was wired into."""
    db = _session()
    try:
        _clean(db)
        _env(db)
        _reconcile(db, _Stub([], get_raises=Exception("failed (404): gone")))
        assert db.query(PovEnvironment).count() == 1, \
            "the reconcile deleted a row it should only have flagged"
    finally:
        _clean(db)
        db.close()


# ── what it must not touch ───────────────────────────────────────────────────

def test_a_row_a_job_owns_is_left_alone():
    """A provision writes `runstate` itself; a listing read taken before its last
    transition would overwrite the job's own view."""
    db = _session()
    try:
        _clean(db)
        for status in ("provisioning", "destroying"):
            _clean(db)
            row = _env(db, status=status, runstate="busy")
            out = _reconcile(db, _Stub([{"id": "env-1", "runstate": "running"}]))
            db.refresh(row)
            assert row.runstate == "busy", (status, row.runstate)
            assert out["checked"] == 0, (status, out)
    finally:
        _clean(db)
        db.close()


def test_a_destroyed_row_is_not_checked():
    db = _session()
    try:
        _clean(db)
        _env(db, status="destroyed")
        out = _reconcile(db, _Stub([{"id": "env-1", "runstate": "running"}]))
        assert out["checked"] == 0, out
    finally:
        _clean(db)
        db.close()


def test_a_row_with_no_platform_id_is_not_checked():
    """A failed provision that never got an id has nothing to ask about."""
    db = _session()
    try:
        _clean(db)
        _env(db, status="failed", platform_environment_id=None)
        out = _reconcile(db, _Stub([]))
        assert out["checked"] == 0, out
    finally:
        _clean(db)
        db.close()


def test_a_failed_row_that_kept_its_id_IS_checked():
    """Destroy is allowed from `failed`, so a failed POV's environment is exactly the one
    whose state somebody needs to see."""
    db = _session()
    try:
        _clean(db)
        row = _env(db, status="failed", runstate="")
        _reconcile(db, _Stub([{"id": "env-1", "runstate": "running"}]))
        db.refresh(row)
        assert row.runstate == "running", row.runstate
    finally:
        _clean(db)
        db.close()


def test_an_unconfigured_platform_skips_rather_than_flagging_everything():
    """No credentials means no information — emphatically not "every environment is
    gone"."""
    db = _session()
    try:
        _clean(db)
        row = _env(db, runstate="running")
        out = _reconcile(db, _Stub([], configured=False))
        db.refresh(row)
        assert out["skipped"] == 1, out
        assert row.platform_missing is not True, row.platform_missing
        assert row.runstate == "running", row.runstate
    finally:
        _clean(db)
        db.close()


# ── the enqueue guard ────────────────────────────────────────────────────────

def test_enqueue_is_a_no_op_while_the_feature_is_off():
    """It is launched unconditionally by the app, so "off" has to be the quiet case."""
    saved = pr._feature_on
    pr._feature_on = lambda: False
    db = _session()
    try:
        assert pr.enqueue_if_due(db) is None
    finally:
        pr._feature_on = saved
        db.close()


def test_enqueue_refuses_a_second_pass_within_the_recency_window():
    """Two passes are two sets of reads against an account this integration is careful not
    to rate-limit. A liveness check alone cannot dedupe instantaneous work — the same
    measured reason expiry_reaper carries a recency term."""
    saved = pr._feature_on
    pr._feature_on = lambda: True
    db = _session()
    try:
        from web_dashboard.database import Job
        db.query(Job).filter(Job.job_type == pr.RECONCILE_JOB_TYPE).delete()
        db.commit()
        first = pr.enqueue_if_due(db)
        assert first, "the first enqueue should have created a job"
        # Mark it done so the ACTIVE check cannot be what refuses the second.
        job = db.query(Job).filter(Job.id == first).first()
        job.status = "completed"
        db.commit()
        assert pr.enqueue_if_due(db) is None, "a second pass was enqueued in the window"
    finally:
        pr._feature_on = saved
        from web_dashboard.database import Job
        db.query(Job).filter(Job.job_type == pr.RECONCILE_JOB_TYPE).delete()
        db.commit()
        db.close()


def test_the_interval_has_a_floor():
    """A misconfigured interval must not turn this into a poll against a rate-limited
    account."""
    saved = pr.interval_seconds
    try:
        from web_dashboard.services import config_service
        real_get = config_service.get
        config_service.get = lambda k, *a, **kw: ("1" if k ==
                                                  "pov_reconcile_interval_seconds"
                                                  else real_get(k, *a, **kw))
        try:
            assert pr.interval_seconds() == pr.MIN_INTERVAL_S, pr.interval_seconds()
        finally:
            config_service.get = real_get
    finally:
        pr.interval_seconds = saved


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
    try:
        os.unlink(_DB_PATH)
    except OSError:
        pass
    sys.exit(1 if failures else 0)
