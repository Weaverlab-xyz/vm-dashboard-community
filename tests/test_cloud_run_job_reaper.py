"""Reaping the GCP Cloud Run runner jobs whose self-delete never landed.

Each runner deletes its own job in a `finally`; a worker restart between the
execution ending and that delete landing strands the job. The live project was
holding k8s-runner jobs from four separate days that way. `_cloud_run_job_row`
hides them from the panel — this reaper is what actually reclaims them.

Deleting cloud resources on a timer is the kind of thing that is only safe if the
guards are exactly right, so those are what this file pins down:

  * ONLY ours — `labels.managed-by=vm-dashboard`, never a job someone else created;
  * ONLY finished — a PENDING or RUNNING runner is live work. This one fails safe by
    construction: `_cloud_run_job_status` falls back to "RUNNING" when it can't read
    the fields, so field-shape drift makes the reaper do nothing, never the reverse;
  * ONLY cold — the runner polls the execution and fetches logs BEFORE its own delete,
    so reaping inside that window would pull the job out from under a live runner and
    fail a run whose work had succeeded. Hence the age floor;
  * ALWAYS non-fatal — a delete that 403s must not blank the containers panel.

Run: python tests/test_cloud_run_job_reaper.py   (or under pytest)
"""
import ast
import inspect
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from web_dashboard.services import gcp_service
    from web_dashboard.services.gcp_service import (
        _CLOUD_RUN_REAP_AGE_MIN_FLOOR,
        _cloud_run_job_finished_age_s,
        _cloud_run_reap_min_age_s,
        _cloud_run_reap_target,
        _reap_cloud_run_jobs,
    )
except Exception as exc:  # pragma: no cover — deps absent outside CI
    try:
        import pytest
        pytest.skip(f"gcp_service import unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)

PROJECT = "project-4e93c8e3-4e96-4bc0-9d1"
REGION = "us-central1"
HOUR = 3600.0
OLD = 6 * HOUR          # comfortably past any age guard
FRESH = 60.0            # inside the runner's own cleanup window


def _ago(seconds: float) -> datetime:
    return datetime.now(timezone.utc) - timedelta(seconds=seconds)


# ── fakes shaped like the run_v2.Job proto wrapper the reaper walks ───────────

class _FakeExecutionRef:
    """job._pb.latest_created_execution — an unset completion_time must read back
    as HasField() False, NOT as the epoch-0 Timestamp the proto would hand over."""

    def __init__(self, completion):
        self.completion_time = completion if completion is not None else _FakeTimestamp(0)
        self._finished = completion is not None

    def HasField(self, name):
        return name == "completion_time" and self._finished


class _FakeTimestamp:
    """protobuf Timestamp: exposes ToDatetime(), returns NAIVE UTC (older protobuf)."""

    def __init__(self, epoch_seconds: float):
        self._epoch = epoch_seconds

    def ToDatetime(self):
        return datetime.fromtimestamp(self._epoch, tz=timezone.utc).replace(tzinfo=None)


class _FakePB:
    def __init__(self, execution):
        self.latest_created_execution = execution

    def HasField(self, name):
        return name == "latest_created_execution" and self.latest_created_execution is not None


class _FakeJob:
    def __init__(self, name, *, state, labels=None, finished_ago=OLD, created_ago=None,
                 completion=..., pb=True):
        self.name = f"projects/{PROJECT}/locations/{REGION}/jobs/{name}"
        self.labels = {"managed-by": "vm-dashboard", "purpose": "k8s-runner"} \
            if labels is None else labels
        # Jobs are created before they finish, so create_time is at least as old.
        self.create_time = _ago(created_ago if created_ago is not None
                                else (finished_ago or 0) + 120)
        if pb:
            if state == "PENDING":
                execution = None
            elif completion is not ...:      # caller pinning the completion field
                execution = _FakeExecutionRef(completion)
            else:
                execution = _FakeExecutionRef(
                    _ago(finished_ago) if state == "COMPLETED" else None)
            self._pb = _FakePB(execution)
        # pb=False → no _pb attribute at all: the unreadable-state case.


class _FakeJobsClient:
    """Records delete_job calls; `fails` names the jobs whose delete raises."""

    def __init__(self, fails=()):
        self.deleted = []
        self.fails = set(fails)

    def delete_job(self, name):
        short = name.split("/")[-1]
        if short in self.fails:
            raise PermissionDenied(f"403 caller lacks run.jobs.delete on {short}")
        self.deleted.append(name)
        return object()  # an LRO the reaper deliberately does not wait on


class PermissionDenied(Exception):
    pass


# ── the guards: what may be reaped ───────────────────────────────────────────

def test_a_stranded_finished_runner_is_reaped():
    """The reported bug: k8s-runner jobs sitting COMPLETED for days."""
    job = _FakeJob("k8s-runner-cc4f7612", state="COMPLETED", finished_ago=11 * 24 * HOUR)
    target = _cloud_run_reap_target(job, REGION, min_age_s=HOUR)
    assert target is not None
    assert target["name"] == "k8s-runner-cc4f7612"
    assert target["region"] == REGION
    assert target["purpose"] == "k8s-runner"
    assert target["full_name"] == job.name, "the delete needs the full resource name"


def test_a_running_runner_is_never_reaped():
    assert _cloud_run_reap_target(
        _FakeJob("k8s-runner-live", state="RUNNING"), REGION, min_age_s=0) is None


def test_a_pending_runner_is_never_reaped():
    """Created but not yet executing — no execution to have finished."""
    assert _cloud_run_reap_target(
        _FakeJob("ansible-runner-1", state="PENDING"), REGION, min_age_s=0) is None


def test_a_job_that_just_finished_is_left_for_its_own_runner():
    """The runner polls the execution and fetches logs AFTER completion_time and only
    then deletes. Reaping in that window 404s the runner's own get_execution and fails
    a run whose work succeeded — so a fresh COMPLETED job is not ours to take."""
    job = _FakeJob("k8s-runner-justdone", state="COMPLETED", finished_ago=FRESH)
    assert _cloud_run_reap_target(job, REGION, min_age_s=HOUR) is None


def test_a_foreign_job_is_never_reaped_however_old():
    """The project holds Cloud Run Jobs the dashboard didn't create. Age is no excuse."""
    ancient = 400 * 24 * HOUR
    for labels in ({}, {"managed-by": "terraform"}, {"purpose": "k8s-runner"}):
        job = _FakeJob("someone-elses-job", state="COMPLETED",
                       labels=labels, finished_ago=ancient)
        assert _cloud_run_reap_target(job, REGION, min_age_s=HOUR) is None, labels


def test_an_unreadable_job_is_never_reaped():
    """Fail safe: `_cloud_run_job_status` falls back to "RUNNING" across google-cloud-run
    field-shape changes, so drift can only ever stop the reaper — never let it delete a
    job that is still running."""
    job = _FakeJob("promote-runner-1", state="COMPLETED", pb=False)
    assert _cloud_run_reap_target(job, REGION, min_age_s=0) is None


# ── dating a job ─────────────────────────────────────────────────────────────

def test_age_is_measured_from_the_execution_completion():
    job = _FakeJob("k8s-runner-x", state="COMPLETED", finished_ago=3 * HOUR)
    age = _cloud_run_job_finished_age_s(job)
    assert age is not None and abs(age - 3 * HOUR) < 30, age


def test_age_reads_a_protobuf_timestamp():
    """Older protobuf hands back a NAIVE datetime from ToDatetime(); treating it as
    local time would shift the age by the UTC offset and could reap a fresh job."""
    ts = _FakeTimestamp(_ago(3 * HOUR).timestamp())
    job = _FakeJob("k8s-runner-pb", state="COMPLETED", completion=ts)
    age = _cloud_run_job_finished_age_s(job)
    assert age is not None and abs(age - 3 * HOUR) < 30, age


def test_age_falls_back_to_create_time_when_completion_is_unreadable():
    """Keeps the reaper working through field-shape drift instead of silently leaking
    again. create_time only ever OVERSTATES the age (by the run's duration, capped at
    the runners' 1200s execution timeout), which the age floor absorbs."""
    job = _FakeJob("k8s-runner-y", state="COMPLETED", created_ago=5 * HOUR)
    del job._pb
    age = _cloud_run_job_finished_age_s(job)
    assert age is not None and abs(age - 5 * HOUR) < 30, age


def test_an_undatable_job_is_never_reaped():
    job = _FakeJob("k8s-runner-z", state="COMPLETED")
    del job._pb
    job.create_time = None
    assert _cloud_run_job_finished_age_s(job) is None


def test_an_unset_completion_time_does_not_date_the_job_to_1970():
    """An unset protobuf Timestamp reads back as epoch 0. Taken at face value that
    makes every job 50 years old and the age guard meaningless — HasField guards it,
    and the create_time fallback dates the job honestly."""
    job = _FakeJob("k8s-runner-unset", state="COMPLETED",
                   completion=None, created_ago=2 * HOUR)
    age = _cloud_run_job_finished_age_s(job)
    assert age is not None and abs(age - 2 * HOUR) < 30, age


# ── the age guard ────────────────────────────────────────────────────────────

def test_the_age_guard_is_floored_below_a_runners_own_lifetime():
    """A runner can legitimately hold a job for ~21 min (1200s execution timeout plus
    the poll loop and the log fetch). A misconfigured guard must not reach into that."""
    original = gcp_service._cfg
    gcp_service._cfg = lambda key: "1" if "reap_age" in key else original(key)
    try:
        assert _cloud_run_reap_min_age_s() == _CLOUD_RUN_REAP_AGE_MIN_FLOOR * 60
        assert _CLOUD_RUN_REAP_AGE_MIN_FLOOR * 60 > 21 * 60, (
            "the floor no longer clears a runner's whole lifetime")
    finally:
        gcp_service._cfg = original


def test_a_configured_age_is_honoured_and_garbage_falls_back():
    original = gcp_service._cfg
    try:
        gcp_service._cfg = lambda key: "240" if "reap_age" in key else original(key)
        assert _cloud_run_reap_min_age_s() == 240 * 60
        gcp_service._cfg = lambda key: "soon" if "reap_age" in key else original(key)
        assert _cloud_run_reap_min_age_s() == gcp_service._CLOUD_RUN_REAP_AGE_MIN_DEFAULT * 60
    finally:
        gcp_service._cfg = original


# ── the delete itself ────────────────────────────────────────────────────────

def test_reaping_deletes_every_target_by_full_resource_name():
    targets = [_cloud_run_reap_target(_FakeJob(n, state="COMPLETED"), REGION, min_age_s=HOUR)
               for n in ("k8s-runner-a", "ansible-runner-b", "promote-runner-c")]
    client = _FakeJobsClient()
    outcome = _reap_cloud_run_jobs(client, targets)
    assert outcome["failed"] == 0
    assert len(outcome["reaped"]) == 3
    assert client.deleted == [t["full_name"] for t in targets]


def test_a_delete_that_403s_is_counted_not_raised():
    """A 403 on one delete must not propagate — the caller is rendering a panel."""
    targets = [_cloud_run_reap_target(_FakeJob(n, state="COMPLETED"), REGION, min_age_s=HOUR)
               for n in ("k8s-runner-a", "k8s-runner-denied", "k8s-runner-c")]
    client = _FakeJobsClient(fails={"k8s-runner-denied"})
    outcome = _reap_cloud_run_jobs(client, targets)
    assert outcome["failed"] == 1
    assert [t["name"] for t in outcome["reaped"]] == ["k8s-runner-a", "k8s-runner-c"], (
        "one failed delete must not abandon the rest of the sweep")


# ── the wiring ───────────────────────────────────────────────────────────────
# The two callers each open a live JobsClient, so their wiring is asserted the same
# way the listing test asserts its filter: over the source.

def _calls(fn):
    tree = ast.parse(inspect.getsource(fn).strip())
    return {n.func.id for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}


def test_the_listing_reaps_what_it_hides():
    """The listing already walks every region and reads every job's state, so the
    opportunistic sweep rides along for free. If this wiring is dropped, stranded jobs
    go back to being hidden but never reclaimed — the exact bug, just invisible."""
    called = _calls(gcp_service._list_cloud_run_jobs_sync)
    assert {"_cloud_run_reap_target", "_reap_cloud_run_jobs"} <= called, sorted(called)


def test_the_listing_reaps_after_the_walk_and_behind_a_guard():
    """Two structural requirements a name check can't see: the delete must not run
    while paging the list, and it must be wrapped so a 403 can't blank the panel."""
    tree = ast.parse(inspect.getsource(gcp_service._list_cloud_run_jobs_sync).strip())

    def _reap_calls(node):
        return [n for n in ast.walk(node) if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Name) and n.func.id == "_reap_cloud_run_jobs"]

    for loop in [n for n in ast.walk(tree) if isinstance(n, (ast.For, ast.AsyncFor))]:
        assert not _reap_calls(loop), "reap runs inside a loop — never delete while paging"

    guarded = [c for t in ast.walk(tree) if isinstance(t, ast.Try)
               for c in _reap_calls(t) if t.handlers]
    assert guarded, "the reap call is not inside a try/except — a 403 would blank the panel"


def test_the_explicit_sweep_covers_every_runner_region():
    """Runner jobs are regional and the three runners can target different regions —
    a sweep that only visited the default region would leave the others leaking."""
    called = _calls(gcp_service._reap_stranded_cloud_run_jobs_sync)
    assert "_cloud_run_runner_regions" in called, sorted(called)
    assert {"_cloud_run_reap_target", "_reap_cloud_run_jobs"} <= called, sorted(called)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"ok   {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {t.__name__}: {e}")
    sys.exit(1 if failures else 0)
