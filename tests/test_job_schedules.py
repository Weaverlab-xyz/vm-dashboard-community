"""A recurring schedule must produce exactly one job per window occurrence.

Three properties, in descending order of how badly they fail:

  * **Idempotence.** Two sweeps inside one window — which happens routinely, since the
    sweep runs every five minutes and a window is hours long — must produce ONE job.
    Getting this wrong means a patch playbook running twelve times on a Saturday night.
    It is enforced by `last_materialised_for` holding the window's START instant, not by
    a lock or a tick count, so it also survives restarts and overlapping passes.
  * **Arming.** Creating a schedule while its window is already open must not
    immediately fire an occurrence — the operator just ran the job, that is where the
    schedule came from. Same rule the suspend schedule and the auto-delete timer follow.
  * **The allowlist.** A payload is copied verbatim and replayed weeks later, so it is
    only safe for job types whose metadata holds references rather than values.

Run: python tests/test_job_schedules.py   (or under pytest)
"""
import os
import sys
from datetime import datetime, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

os.environ.setdefault("JWT_SECRET_KEY", "test-secret-job-schedules")
os.environ["DATABASE_URL"] = "sqlite://"

# Probe the third-party dep by name, then import first-party unguarded — a wider guard
# would let a broken import turn this file into a silent no-op.
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

from web_dashboard.database import Base, ChangeWindow, Job, JobSchedule  # noqa: E402
from web_dashboard.services import job_service, schedule_service  # noqa: E402
from web_dashboard.services.suspend_schedule import ScheduleError  # noqa: E402


def _session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _window(db, start="00:00", minutes=24 * 60 - 1, days="1111111", enabled=True):
    """An always-open window by default, so a test can control time with `now` alone."""
    w = ChangeWindow(name=f"W{id(db)}-{start}", start_at_local=start,
                     duration_minutes=minutes, timezone="UTC", schedule_days=days,
                     enabled=enabled, created_by="admin")
    db.add(w)
    db.commit()
    return w


def _job(db, job_type="ansible_local", meta=None, created_by="alice"):
    j = Job(job_type=job_type, status="completed", created_by=created_by,
            workgroup="ansible", created_at=datetime.utcnow())
    j.metadata_dict = meta or {"asset": "patch.yml", "target": "10.0.0.5",
                               "description": "Ansible (playbook): patch.yml"}
    db.add(j)
    db.commit()
    return j


def _user(db, username="alice", active=True):
    from web_dashboard.database import User
    u = User(username=username, email=f"{username}@example.com",
             hashed_password="x", is_active=active)
    db.add(u)
    db.commit()
    return u


# ── The allowlist ─────────────────────────────────────────────────────────────

def test_a_config_management_job_may_be_repeated():
    db = _session()
    assert schedule_service.schedulable_reason(_job(db)) == ""


def test_a_job_type_outside_the_allowlist_is_refused():
    """Default-deny. A new job type must not become schedulable — and possibly unsafe —
    the day it is added, merely by nobody having thought about it."""
    db = _session()
    reason = schedule_service.schedulable_reason(_job(db, job_type="packer_aws_build"))
    assert reason, "a packer build was accepted as repeatable"
    assert "packer_aws_build" in reason


def test_the_allowlist_is_per_key_not_per_type():
    """The mechanism itself, because the per-type version of it was unsafe.

    A type-only allowlist said "this job type may repeat" and then copied the metadata
    verbatim — but `job_service.set_completed` MERGES each runner's result into that
    metadata, so the dict being copied is the post-run one. Naming the keys is what
    stops a runner's output reaching `job_schedules.payload`.
    """
    keys = schedule_service.SCHEDULABLE_PAYLOAD_KEYS
    assert isinstance(keys, dict) and keys, "the allowlist must be a per-type key map"
    for job_type, allowed in keys.items():
        assert isinstance(allowed, frozenset) and allowed, (
            f"{job_type} names no keys, so it would store an empty payload")
    assert schedule_service.SCHEDULABLE_JOB_TYPES == frozenset(keys), (
        "the type set and the key map have drifted — one is derived from the other "
        "precisely so they cannot")


def test_a_runner_result_never_reaches_the_stored_payload():
    """The property the filter exists for, exercised on a real post-run job.

    `epml_sync` is the sharp case: its create-time metadata is two harmless strings,
    and its COMPLETED metadata carries BeyondTrust pre-signed download URLs that
    expire in ~30 minutes. Copied verbatim they would sit in the schedules table
    indefinitely.
    """
    db = _session()
    job = _job(db, job_type="epml_sync", meta={"description": "EPM-L sync",
                                               "backend": "s3"})
    # What the runner writes back on completion.
    job_service.set_completed(db, job.id, {
        "packages": [{"name": "epm", "link": "https://bt.example/pkg?sig=SECRET"}],
        "rpm_uploaded": 3, "summary": "ok",
    })
    db.refresh(job)
    assert "packages" in job.metadata_dict, "precondition: the runner result merged"

    payload = schedule_service.payload_for(job)
    assert payload == {"description": "EPM-L sync", "backend": "s3"}, payload
    blob = repr(payload)
    assert "SECRET" not in blob and "packages" not in blob, (
        "a runner result reached the stored payload: " + blob)


def test_the_filter_keeps_what_the_runner_actually_reads():
    """The other half — filtering must not drop a key the run path needs, or every
    occurrence would run with different parameters from the job it was copied from."""
    db = _session()
    meta = {"asset": "patch.yml", "target": "10.0.0.5", "extra_vars": {"a": 1},
            "secret_vars": {"PW": "config://x"}, "description": "Ansible: patch.yml"}
    job = _job(db, meta=dict(meta))
    job_service.set_completed(db, job.id, {"output": "PLAY [all] ...",
                                           "returncode": 0})
    db.refresh(job)
    payload = schedule_service.payload_for(job)
    for key, value in meta.items():
        assert payload.get(key) == value, f"{key} was dropped from the payload"
    assert "output" not in payload and "returncode" not in payload, payload


# ── Creation ──────────────────────────────────────────────────────────────────

def test_creating_a_schedule_copies_the_jobs_payload():
    db = _session()
    _user(db)
    job = _job(db, meta={"asset": "patch.yml", "target": "10.0.0.5"})
    row = schedule_service.create_from_job(
        db, job=job, name="Weekly patch", change_window_id=_window(db).id,
        created_by="alice")
    assert row.job_type == "ansible_local"
    assert row.payload_dict == {"asset": "patch.yml", "target": "10.0.0.5"}
    assert row.workgroup == "ansible"


def test_a_schedule_is_armed_not_fired():
    """Created inside an open window, it must NOT produce a job for that occurrence —
    the operator just ran it, which is where the schedule came from."""
    db = _session()
    _user(db)
    window = _window(db)
    row = schedule_service.create_from_job(
        db, job=_job(db), name="Weekly patch", change_window_id=window.id,
        created_by="alice")
    assert row.last_materialised_for is not None, "the new schedule was not armed"
    assert schedule_service.due(db, datetime.utcnow()) == [], (
        "a freshly created schedule fired immediately for the window it was created in")


def test_a_missing_window_is_refused_at_creation():
    db = _session()
    _user(db)
    _refuses(db, change_window_id="does-not-exist")


def test_a_disabled_window_is_refused_at_creation():
    db = _session()
    _user(db)
    _refuses(db, change_window_id=_window(db, enabled=False).id)


def test_a_nameless_schedule_is_refused():
    db = _session()
    _user(db)
    _refuses(db, change_window_id=_window(db).id, name="  ")


def _refuses(db, *, change_window_id, name="Weekly patch"):
    try:
        schedule_service.create_from_job(db, job=_job(db), name=name,
                                         change_window_id=change_window_id,
                                         created_by="alice")
    except ScheduleError:
        return
    raise AssertionError("accepted an invalid schedule")


# ── Idempotence: the property that matters most ───────────────────────────────

def test_the_next_occurrence_is_due():
    db = _session()
    _user(db)
    row = schedule_service.create_from_job(
        db, job=_job(db), name="Weekly patch", change_window_id=_window(db).id,
        created_by="alice")
    # Step past the armed occurrence into the next day's.
    later = datetime.utcnow() + timedelta(days=1, hours=1)
    assert [r[0].id for r in schedule_service.due(db, later)] == [row.id]


def test_materialising_twice_in_one_window_creates_one_job():
    """The sweep runs every five minutes; a window is hours long. Without this the
    schedule fires on every pass for the whole window."""
    db = _session()
    _user(db)
    schedule_service.create_from_job(
        db, job=_job(db), name="Weekly patch", change_window_id=_window(db).id,
        created_by="alice")
    later = datetime.utcnow() + timedelta(days=1, hours=1)

    for row, _w, start, end in schedule_service.due(db, later):
        schedule_service.materialise(db, row, start=start, end=end)
    db.commit()

    # A second pass a minute later, inside the same occurrence.
    again = later + timedelta(minutes=1)
    assert schedule_service.due(db, again) == [], (
        "the same occurrence came up due twice — last_materialised_for did not hold")

    made = db.query(Job).filter(Job.job_schedule_id.isnot(None)).count()
    assert made == 1, f"one occurrence produced {made} jobs"


def test_the_occurrence_job_is_booked_into_the_window():
    """It must queue behind the same gate a hand-booked change does — including being
    marked missed if the window closes first."""
    db = _session()
    _user(db)
    schedule_service.create_from_job(
        db, job=_job(db), name="Weekly patch", change_window_id=_window(db).id,
        created_by="alice")
    later = datetime.utcnow() + timedelta(days=1, hours=1)
    row, _w, start, end = schedule_service.due(db, later)[0]
    job = schedule_service.materialise(db, row, start=start, end=end)
    db.commit()

    assert job.scheduled_for == start
    assert job.window_ends_at == end
    assert job.job_schedule_id == row.id
    assert job.change_window_id == row.change_window_id
    assert job.created_by == "alice", "the occurrence lost its owning identity"


def test_a_disabled_schedule_is_never_due():
    db = _session()
    _user(db)
    row = schedule_service.create_from_job(
        db, job=_job(db), name="Weekly patch", change_window_id=_window(db).id,
        created_by="alice")
    row.enabled = False
    db.commit()
    later = datetime.utcnow() + timedelta(days=1, hours=1)
    assert schedule_service.due(db, later) == []


def test_a_schedule_whose_window_was_deleted_is_skipped_not_raised():
    """One broken row must not stop every other change in the estate from being booked."""
    db = _session()
    _user(db)
    row = schedule_service.create_from_job(
        db, job=_job(db), name="Weekly patch", change_window_id=_window(db).id,
        created_by="alice")
    db.query(ChangeWindow).filter(ChangeWindow.id == row.change_window_id).delete()
    db.commit()
    assert schedule_service.due(db, datetime.utcnow() + timedelta(days=2)) == []


# ── The owner outliving their access ──────────────────────────────────────────

def test_a_schedule_whose_owner_is_gone_disables_itself():
    """A recurring change must not outlive the authority that created it — the
    occurrences run as that identity and resolve that person's secret references."""
    db = _session()
    _user(db, "alice")
    schedule_service.create_from_job(
        db, job=_job(db), name="Weekly patch", change_window_id=_window(db).id,
        created_by="alice")
    from web_dashboard.database import User
    db.query(User).filter(User.username == "alice").delete()
    db.commit()

    later = datetime.utcnow() + timedelta(days=1, hours=1)
    row, _w, start, end = schedule_service.due(db, later)[0]
    job = schedule_service.materialise(db, row, start=start, end=end)
    db.commit()

    assert job is None, "an occurrence ran for a user who no longer exists"
    db.refresh(row)
    assert row.enabled is False
    assert "no longer exists" in (row.disabled_reason or "")


def test_a_deactivated_owner_also_disables_it():
    db = _session()
    _user(db, "alice", active=False)
    schedule_service.create_from_job(
        db, job=_job(db), name="Weekly patch", change_window_id=_window(db).id,
        created_by="alice")
    later = datetime.utcnow() + timedelta(days=1, hours=1)
    row, _w, start, end = schedule_service.due(db, later)[0]
    assert schedule_service.materialise(db, row, start=start, end=end) is None
    # Commit before refreshing: `materialise` leaves the disable pending in the session
    # (the sweep owns the transaction), so a bare refresh would re-read the old row and
    # this assertion would test the database round-trip rather than the behaviour.
    db.commit()
    db.refresh(row)
    assert row.enabled is False


# ── Repeated failure ──────────────────────────────────────────────────────────

def test_repeated_failures_disable_the_schedule():
    """A recurring change that fails every week forever trains people to ignore the
    notification, and by the time it matters nobody is reading it."""
    db = _session()
    _user(db)
    row = schedule_service.create_from_job(
        db, job=_job(db), name="Weekly patch", change_window_id=_window(db).id,
        created_by="alice")

    for _ in range(schedule_service.MAX_CONSECUTIVE_FAILURES):
        failed = _job(db)
        failed.status = "failed"
        failed.job_schedule_id = row.id
        db.commit()
        schedule_service.note_result(db, failed)
        db.commit()

    db.refresh(row)
    assert row.enabled is False, "the schedule kept firing after repeated failures"
    assert "consecutive" in (row.disabled_reason or "")


def test_a_success_resets_the_failure_count():
    db = _session()
    _user(db)
    row = schedule_service.create_from_job(
        db, job=_job(db), name="Weekly patch", change_window_id=_window(db).id,
        created_by="alice")

    failed = _job(db)
    failed.status = "failed"
    failed.job_schedule_id = row.id
    db.commit()
    schedule_service.note_result(db, failed)

    ok = _job(db)
    ok.status = "completed"
    ok.job_schedule_id = row.id
    db.commit()
    schedule_service.note_result(db, ok)
    db.commit()

    db.refresh(row)
    assert row.consecutive_failures == 0
    assert row.enabled is not False


def test_a_still_running_occurrence_does_not_count_as_a_failure():
    db = _session()
    _user(db)
    row = schedule_service.create_from_job(
        db, job=_job(db), name="Weekly patch", change_window_id=_window(db).id,
        created_by="alice")
    running = _job(db)
    running.status = "running"
    running.job_schedule_id = row.id
    db.commit()
    schedule_service.note_result(db, running)
    db.commit()
    db.refresh(row)
    assert row.consecutive_failures == 0


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
