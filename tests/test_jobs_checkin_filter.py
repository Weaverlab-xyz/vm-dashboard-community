"""Hiding the unattended hypervisor check-ins on /jobs, without hiding the power ops.

An agent-bound hypervisor connection is polled on a cadence — 30 minutes by default,
per connection, and a large vCenter is several paged job rows per pass. They all
complete green with nothing for anyone to do, and they push real work off the first
page of /jobs within the hour. This is the same problem `ROUTINE_JOB_TYPES` already
solved for the expiry and schedule sweeps, with one difference that is the whole
reason this file exists:

**`agent_hypervisor` cannot be hidden by job type.** An `inventory_sync` and a
`power_on` are the same type deliberately — one agent handler, one `allowed_job_types`
grant — so hiding the type would also hide an operator's record of having stopped a
production VM. The verb tells them apart, the verb lives in `extra_data` JSON, and no
operator filters that portably across SQLite and PostgreSQL. Hence `Job.is_checkin`: a
real indexed column, stamped from `job_service.CHECKIN_VERBS` in the one funnel every
job row passes through.

What is pinned here, in the order a mistake would reach an operator:

  * the classification itself, including the two conservative defaults — an absent
    verb and an unrecognised one are NOT check-ins. `agent_hypervisor_meta.normalize`
    falls an unknown verb back to `inventory_sync`, so the opposite default would
    quietly hide a malformed power op;
  * `create_job` stamping it, three-valued, and the third value mattering: a power op
    is `False`, not NULL, or the backfill below never terminates;
  * the filter hiding only *completed* check-ins, and only check-ins — a FAILED sync
    is the connection an operator needs to look at, and a power op is never touched;
  * the two hide flags being INDEPENDENT, so someone diagnosing a stale inventory can
    have the syncs without 48 rows/day of expiry sweeps on top;
  * the backfill, because without it the feature does nothing on an existing install:
    every row already on /jobs reads NULL and NULL means "show it";
  * `agent_hypervisor` staying OUT of `ROUTINE_JOB_TYPES`, which is not a style rule —
    `expiry_reaper.prune_sweep_history` DELETES completed rows of the types listed
    there, so the "obvious simplification" of moving it across would permanently
    destroy power-op history.

Runs against a real throwaway SQLite database for the storage half, and reads the
sources for the wiring half. Under pytest, or standalone:

    python tests/test_jobs_checkin_filter.py
"""
import os
import re
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.environ.setdefault(
    "DATABASE_URL",
    "sqlite:///" + os.path.join(tempfile.mkdtemp(), "checkin.db").replace("\\", "/"))
os.environ.setdefault("JWT_SECRET_KEY", "x" * 32)

_API = os.path.join(_ROOT, "web_dashboard", "api", "jobs.py")
_JOBSVC = os.path.join(_ROOT, "web_dashboard", "services", "job_service.py")
_DB = os.path.join(_ROOT, "web_dashboard", "database.py")
_PAGE = os.path.join(_ROOT, "web_dashboard", "templates", "jobs", "list.html")

# The only legitimate reason to skip is a bare interpreter with no app deps, so probe
# for those BY NAME and let every other ImportError propagate as a failure. A blanket
# `except Exception: skip` around the first-party imports below would turn "job_service
# no longer imports" — the exact regression half this file exists to catch — into a
# SKIP that exits 0 and reads as a pass. See tests/test_import_guard_narrowness.py.
try:
    import pydantic          # noqa: F401
    import pydantic_settings  # noqa: F401
    import sqlalchemy        # noqa: F401
except ModuleNotFoundError as exc:  # pragma: no cover — bare interpreter
    try:
        import pytest
        pytest.skip(f"app deps unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)

from web_dashboard.database import Base, Job, SessionLocal, engine  # noqa: E402
from web_dashboard.services import agent_hypervisor_meta as ahm     # noqa: E402
from web_dashboard.services import job_service                      # noqa: E402

Base.metadata.create_all(bind=engine)


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _fresh():
    """A session over an empty jobs table, so each case counts only its own rows."""
    db = SessionLocal()
    db.query(Job).delete()
    db.commit()
    return db


def _sync(db, status="completed", verb="inventory_sync"):
    job = job_service.create_job(db, job_type="agent_hypervisor", created_by="system:sync",
                                 metadata={"verb": verb, "connection_id": "c1"})
    job.status = status
    db.commit()
    return job


def _power(db, status="completed", verb="power_on"):
    job = job_service.create_job(db, job_type="agent_hypervisor", created_by="alice",
                                 metadata={"verb": verb, "target_id": "101"})
    job.status = status
    db.commit()
    return job


def _verbs(db, **kwargs):
    """The verbs (or job types) the filter let through, sorted."""
    jobs, total = job_service.list_jobs(db, **kwargs)
    got = sorted((j.metadata_dict or {}).get("verb") or j.job_type for j in jobs)
    assert total == len(got), f"total {total} disagrees with the {len(got)} rows returned"
    return got


# ── the classification ────────────────────────────────────────────────────────

def test_an_inventory_sync_is_a_checkin_and_a_power_op_is_not():
    assert job_service.is_checkin("agent_hypervisor", {"verb": "inventory_sync"})
    for verb in ahm.WRITE_VERBS:
        assert not job_service.is_checkin("agent_hypervisor", {"verb": verb}), (
            f"'{verb}' is an operator action and would be hidden from /jobs")


def test_every_read_verb_is_a_checkin_so_a_new_one_is_covered_by_default():
    for verb in ahm.READ_VERBS:
        assert job_service.is_checkin("agent_hypervisor", {"verb": verb}), (
            f"READ_VERBS gained '{verb}' but CHECKIN_VERBS does not cover it")


def test_a_missing_or_unknown_verb_is_not_a_checkin():
    """The conservative direction, and it is not arbitrary.

    `normalize` turns an unrecognised verb into `inventory_sync`, so a row that reached
    the database malformed may well BE a power op. Defaulting the other way would hide
    it, and a hidden power op is one an operator cannot prove they did or did not run.
    """
    assert not job_service.is_checkin("agent_hypervisor", {})
    assert not job_service.is_checkin("agent_hypervisor", None)
    assert not job_service.is_checkin("agent_hypervisor", {"verb": ""})
    assert not job_service.is_checkin("agent_hypervisor", {"verb": "not_a_verb"})


def test_an_unrelated_job_type_is_never_a_checkin():
    for job_type in ("ec2_deploy", "expiry_sweep", "agent_discover", ""):
        assert not job_service.is_checkin(job_type, {"verb": "inventory_sync"}), (
            f"'{job_type}' is not in CHECKIN_VERBS and must not be classified by verb")


# ── the stamp ─────────────────────────────────────────────────────────────────

def test_create_job_stamps_the_column_three_valued():
    """True, False and NULL each mean something different, and NULL is load-bearing.

    A power op must be an explicit False rather than NULL: the backfill's candidate
    query is `job_type IN CHECKIN_VERBS AND is_checkin IS NULL`, so a NULL power op
    would be re-read on every boot for the life of the install and never converge.
    """
    db = _fresh()
    try:
        assert _sync(db).is_checkin is True
        assert _power(db).is_checkin is False, (
            "a power op must be classified False, not left NULL — see the backfill")
        assert job_service.create_job(
            db, job_type="ec2_deploy", created_by="a").is_checkin is None, (
            "a type outside CHECKIN_VERBS was never looked at and must stay NULL")
    finally:
        db.close()


def test_the_stamp_happens_in_the_funnel_not_at_the_call_sites():
    """`create_job` derives it, so a new caller cannot forget — the same argument the
    docstring already makes for forcing `status='queued'` on an agent-bound row."""
    src = _read(_JOBSVC)
    body = src[src.index("def create_job("):src.index("def set_cloud_resource_id(")]
    assert "is_checkin=" in body, "create_job no longer stamps is_checkin"
    assert "is_checkin: " not in src[src.index("def create_job("):
                                     src.index(") -> Job:")], (
        "is_checkin became a create_job PARAMETER — then a call site can get it wrong, "
        "and the point of deriving it in the funnel is that none of them can")


# ── the filter ────────────────────────────────────────────────────────────────

def test_a_completed_checkin_is_hidden_and_a_power_op_is_not():
    db = _fresh()
    try:
        _sync(db)
        _power(db)
        assert _verbs(db) == ["inventory_sync", "power_on"], "the default still shows both"
        assert _verbs(db, include_checkins=False) == ["power_on"], (
            "hiding check-ins took the operator's power op with it")
    finally:
        db.close()


def test_a_failed_or_running_checkin_stays_visible():
    """The same split routine sweeps make, for the same reason: a sync that failed is a
    connection whose inventory is now stale, and it has to reach the failed-jobs panel.
    A running one is the sync an operator just pressed the button for."""
    db = _fresh()
    try:
        _sync(db, status="failed")
        _sync(db, status="running")
        _sync(db, status="cancelled")
        assert len(_verbs(db, include_checkins=False)) == 3, (
            "only COMPLETED check-ins may be hidden")
    finally:
        db.close()


def test_a_row_that_predates_the_column_is_shown_not_hidden():
    """NULL means "never classified", and the filter has to read that as visible.

    Spelled `is_(True)` rather than `== True` for exactly this: under SQL three-valued
    logic `NULL = TRUE` is NULL, so the negated filter would drop every pre-upgrade row
    from /jobs — thousands of them, silently, on the first boot after the upgrade.
    """
    db = _fresh()
    try:
        job = _sync(db)
        job.is_checkin = None          # what an un-backfilled upgrade leaves behind
        db.commit()
        assert _verbs(db, include_checkins=False) == ["inventory_sync"]
    finally:
        db.close()


def test_the_two_hide_flags_are_independent():
    db = _fresh()
    try:
        _sync(db)
        _power(db)
        sweep = job_service.create_job(db, job_type="expiry_sweep", created_by="system")
        sweep.status = "completed"
        db.commit()

        assert _verbs(db, include_routine=False) == ["inventory_sync", "power_on"], (
            "the routine flag reached the check-ins")
        assert _verbs(db, include_checkins=False) == ["expiry_sweep", "power_on"], (
            "the check-in flag reached the sweeps")
        assert _verbs(db, include_routine=False, include_checkins=False) == ["power_on"]
    finally:
        db.close()


# ── the backfill ──────────────────────────────────────────────────────────────

def test_the_backfill_classifies_the_rows_that_are_already_there():
    """Without this the filter is inert for months on an existing install — the rows it
    exists to hide are precisely the ones written before the column did."""
    db = _fresh()
    try:
        sync, power = _sync(db), _power(db)
        job_service.create_job(db, job_type="ec2_deploy", created_by="a")
        db.query(Job).update({Job.is_checkin: None})   # as the ALTER TABLE leaves them
        db.commit()

        assert job_service.backfill_job_checkins(db, batch=1) == 2, (
            "the backfill must classify both hypervisor rows, and only those two")
        db.refresh(sync)
        db.refresh(power)
        assert sync.is_checkin is True and power.is_checkin is False
        assert _verbs(db, include_checkins=False) == ["ec2_deploy", "power_on"]
    finally:
        db.close()


def test_the_backfill_converges_so_it_is_not_paid_for_on_every_boot():
    db = _fresh()
    try:
        _sync(db)
        _power(db)
        db.query(Job).update({Job.is_checkin: None})
        db.commit()
        assert job_service.backfill_job_checkins(db) == 2
        assert job_service.backfill_job_checkins(db) == 0, (
            "the second pass rewrote rows — it does not converge, and every boot will "
            "re-scan every hypervisor job this install has ever written")
    finally:
        db.close()


def test_the_backfill_does_not_load_whole_job_rows():
    """A sync row is not a small row. `complete_job` merges the agent's inventory page
    into the SAME `extra_data` this has to read the verb out of, and that page is capped
    at 256 KB — so a batch of 1000 ORM rows is a quarter of a gigabyte of JSON at
    startup, on a container sized for none of it.
    """
    src = _read(_JOBSVC)
    body = src[src.index("def backfill_job_checkins("):src.index("def get_job(")]
    assert "db.query(Job.id, Job.job_type, Job.extra_data)" in body, (
        "the backfill loads whole ORM Job rows again — each one can carry 256 KB of "
        "inventory page, and the identity map holds them all until the session closes")
    m = re.search(r"def backfill_job_checkins\(db: Session, batch: int = (\d+)\)", src)
    assert m and int(m.group(1)) <= 500, (
        "the backfill's default batch grew; peak memory is batch x 256 KB")


def test_the_backfill_runs_outside_the_advisory_locked_transaction():
    """It is data, not DDL. Run inside init_db's advisory-locked transaction it would
    hold that lock across a batched write over every hypervisor job in the table — the
    shape of the startup deadlock this tree has already had once."""
    src = _read(_DB)
    call = src.index("backfill_job_checkins")
    lock_block = src.index("Base.metadata.create_all(bind=conn)")
    assert call > lock_block, "the check-in backfill moved inside the locked block"
    # Same structural marker the other two backfills carry: its own short session.
    assert "with SessionLocal() as _checkin_db:" in src, (
        "the backfill no longer opens its own session outside the lock")


# ── the wiring ────────────────────────────────────────────────────────────────

def test_the_api_hides_checkins_by_default():
    """The default is what makes /jobs AND the dashboard's recent-activity widget
    readable; both call this endpoint without asking for check-ins."""
    src = _read(_API)
    m = re.search(r"include_checkins:\s*bool\s*=\s*Query\(\s*(\w+)", src)
    assert m, "the /api/jobs endpoint has no include_checkins parameter"
    assert m.group(1) == "False", (
        "include_checkins defaults to True on the API, so nothing is hidden by default")
    assert "include_checkins=include_checkins" in src, (
        "the endpoint accepts the flag and never passes it to job_service.list_jobs")


def test_the_service_default_leaves_every_other_caller_alone():
    """True in the service, False at the API. Hiding is a choice made by the caller
    rendering a list, never a filter silently applied to every count in the app —
    the same split `include_routine` already has."""
    src = _read(_JOBSVC)
    body = src[src.index("def list_jobs("):src.index(") -> tuple[List[Job], int]:")]
    assert "include_checkins: bool = True" in body, (
        "list_jobs defaults to hiding check-ins, which changes counts nobody asked "
        "it to change")


def test_the_jobs_page_offers_the_checkbox():
    page = _read(_PAGE)
    assert "includeCheckins" in page, "the jobs page has no check-in toggle"
    assert 'x-model="includeCheckins"' in page, (
        "the checkbox is not bound — bound-but-undeclared and declared-but-unbound both "
        "render a control that does nothing")
    assert "includeCheckins: false" in page, (
        "the toggle is not declared in x-data, or does not default to hiding — an Alpine "
        "x-model against an undeclared property is silently discarded")
    assert "include_checkins=true" in page, (
        "ticking the box never reaches the API")
    assert "page=1; loadJobs()" in page.split("includeCheckins")[1][:200], (
        "toggling the filter does not reset to page 1, so it can land on an empty page")


def test_the_hypervisor_type_is_not_a_routine_type():
    """Not a style rule. `expiry_reaper.prune_sweep_history` DELETES completed rows of
    every type in ROUTINE_JOB_TYPES past the retention window, and `agent_hypervisor`
    carries operator power ops. Folding the two mechanisms together — which looks like
    a tidy-up from the outside — would permanently destroy that history."""
    assert "agent_hypervisor" not in job_service.ROUTINE_JOB_TYPES, (
        "agent_hypervisor is in ROUTINE_JOB_TYPES, so the sweep-history prune can now "
        "delete completed power ops; hide it with is_checkin instead")
    assert set(job_service.CHECKIN_VERBS).isdisjoint(job_service.ROUTINE_JOB_TYPES), (
        "a job type is in BOTH hide mechanisms — one of the two boxes on /jobs is now "
        "a no-op for it, and the prune reaches rows the verb said were operator work")


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
    print(f"{chr(10)}{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
