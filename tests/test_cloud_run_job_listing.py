"""The GCP Cloud Run Jobs panel lists work IN FLIGHT, not job history.

Runner jobs are supposed to delete themselves when their execution ends, but that
delete sits in a `finally` — a worker restart (or a failed delete) between the
execution finishing and the delete landing strands the job in the project. Without a
filter the panel and the dashboard tile fill up with week-old COMPLETED rows and the
tile's "in-flight jobs" count becomes a lie.

So `_cloud_run_job_row` is the discriminator, and both directions matter:

  * a COMPLETED runner must be dropped — that's the reported noise;
  * a runner whose state can't be read must be KEPT, because `_cloud_run_job_status`
    falls back to "RUNNING" across google-cloud-run field-shape changes, and silently
    hiding a live runner is worse than showing one extra row.

Run: python tests/test_cloud_run_job_listing.py   (or under pytest)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from web_dashboard.services.gcp_service import (
        _cloud_run_job_row,
        _cloud_run_job_status,
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


# ── fakes shaped like the run_v2.Job proto wrapper the listing walks ──────────

class _FakeExecutionRef:
    def __init__(self, finished: bool):
        self._finished = finished

    def HasField(self, name):
        return name == "completion_time" and self._finished


class _FakePB:
    def __init__(self, execution):
        self.latest_created_execution = execution

    def HasField(self, name):
        return name == "latest_created_execution" and self.latest_created_execution is not None


class _FakeContainer:
    def __init__(self, image):
        self.image = image


class _FakeTemplate:
    """job.template.template.containers — the doubly-nested run_v2 shape."""
    def __init__(self, image):
        self.template = type("_Inner", (), {"containers": [_FakeContainer(image)]})()


class _FakeTime:
    def __init__(self, iso):
        self._iso = iso

    def isoformat(self):
        return self._iso


class _FakeJob:
    def __init__(self, name, *, state, labels=None, image="dtzar/helm-kubectl:latest",
                 created="2026-07-27T09:21:40+00:00", pb=True):
        self.name = f"projects/{PROJECT}/locations/{REGION}/jobs/{name}"
        self.labels = {"managed-by": "vm-dashboard", "purpose": "k8s-runner"} \
            if labels is None else labels
        self.template = _FakeTemplate(image)
        self.create_time = _FakeTime(created)
        if pb:
            execution = None if state == "PENDING" else _FakeExecutionRef(state == "COMPLETED")
            self._pb = _FakePB(execution)
        # pb=False → no _pb attribute at all: the unreadable-state case.


# ── _cloud_run_job_status ────────────────────────────────────────────────────

def test_status_reads_the_execution_state():
    assert _cloud_run_job_status(_FakeJob("j", state="PENDING")) == "PENDING"
    assert _cloud_run_job_status(_FakeJob("j", state="RUNNING")) == "RUNNING"
    assert _cloud_run_job_status(_FakeJob("j", state="COMPLETED")) == "COMPLETED"


def test_status_falls_back_to_running_when_the_fields_are_unreadable():
    assert _cloud_run_job_status(_FakeJob("j", state="RUNNING", pb=False)) == "RUNNING"


# ── the filter itself ───────────────────────────────────────────────────────

def test_a_finished_runner_is_dropped():
    """The reported bug: k8s-runner jobs from previous weeks sat in the panel as
    COMPLETED because their self-delete never landed."""
    job = _FakeJob("k8s-runner-cc4f7612", state="COMPLETED",
                   created="2026-07-16T11:02:29+00:00")
    assert _cloud_run_job_row(job, REGION) is None


def test_a_running_runner_is_reported_with_its_fields():
    row = _cloud_run_job_row(_FakeJob("k8s-runner-adhoc", state="RUNNING"), REGION)
    assert row == {
        "name": "k8s-runner-adhoc",
        "region": REGION,
        "purpose": "k8s-runner",
        "image": "dtzar/helm-kubectl:latest",
        "status": "RUNNING",
        "created_at": "2026-07-27T09:21:40+00:00",
    }, row


def test_a_pending_runner_is_reported():
    """Created but not yet executing is still work in flight — it must not be
    mistaken for finished."""
    row = _cloud_run_job_row(_FakeJob("ansible-runner-1", state="PENDING"), REGION)
    assert row and row["status"] == "PENDING"


def test_an_unreadable_job_is_kept_not_hidden():
    """Fail open: the status fallback is 'RUNNING', so a field-shape change across
    google-cloud-run versions degrades to an extra row, never to a hidden live run."""
    row = _cloud_run_job_row(_FakeJob("promote-runner-1", state="RUNNING", pb=False), REGION)
    assert row and row["status"] == "RUNNING"


def test_a_foreign_job_is_not_reported():
    """The project can hold Cloud Run Jobs the dashboard didn't create."""
    assert _cloud_run_job_row(
        _FakeJob("someone-elses-job", state="RUNNING", labels={}), REGION) is None
    assert _cloud_run_job_row(
        _FakeJob("someone-elses-job", state="RUNNING", labels={"managed-by": "terraform"}),
        REGION) is None


def test_missing_image_and_create_time_degrade_to_empty_strings():
    """The panel renders '—' for these; a listing must not blow up on a job whose
    template or timestamp doesn't read back."""
    job = _FakeJob("k8s-runner-x", state="RUNNING")
    job.template = None
    job.create_time = None
    row = _cloud_run_job_row(job, REGION)
    assert row and row["image"] == "" and row["created_at"] == ""


def test_finished_jobs_are_filtered_before_the_cap():
    """`_list_cloud_run_jobs_sync` slices to `limit` AFTER shaping. Filtering has to
    happen inside the loop (row is None → never appended), otherwise a backlog of
    stranded COMPLETED jobs — 5 of them were enough in the reported case — pushes the
    genuinely running one off the end of the list."""
    import ast
    import inspect
    from web_dashboard.services import gcp_service

    src = inspect.getsource(gcp_service._list_cloud_run_jobs_sync)
    tree = ast.parse(src.strip())
    called = {n.func.id for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "_cloud_run_job_row" in called, (
        "_list_cloud_run_jobs_sync no longer shapes rows through _cloud_run_job_row — "
        f"finished runners will be listed again. Counted: {sorted(called)}")


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
