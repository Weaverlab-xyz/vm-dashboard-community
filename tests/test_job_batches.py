"""Unit tests: job_service._summarize_statuses — the rollup behind a bulk-run batch.

A bulk Config-Management run fans one asset out to N targets, one job each, tagged
with a shared `batch_id`. The jobs page rolls those up so the question an operator
actually has after a fleet change — *did any of them fail?* — is answerable without
reading N rows.

The SQL around it (a `group_by(Job.status)` count, scoped to the caller's own jobs
when they lack `jobs:read`) needs a database. The arithmetic doesn't, and the
arithmetic is where the mistakes are:

  * a status with no rows must report an explicit **0**, not be absent — "0 failed"
    is the reassuring answer, and a missing key renders as nothing at all;
  * the total must come from the counted rows, so it can never disagree with the
    per-status numbers shown beside it;
  * a status outside the known set must be carried through rather than dropped,
    or an unexpected value would silently vanish from the total.

Loaded by file path with the sqlalchemy imports stubbed, so it runs with stdlib only.
Runs under pytest, or standalone:  python tests/test_job_batches.py
"""
import importlib.util
import os
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PATH = os.path.join(_ROOT, "web_dashboard", "services", "job_service.py")


def _install_stubs():
    sa = types.ModuleType("sqlalchemy")
    sa.text = lambda s: s
    sa.func = types.SimpleNamespace(count=lambda *a, **k: None)
    sa_exc = types.ModuleType("sqlalchemy.exc")
    sa_exc.IntegrityError = type("IntegrityError", (Exception,), {})
    sa_orm = types.ModuleType("sqlalchemy.orm")
    sa_orm.Session = type("Session", (), {})
    sa.exc, sa.orm = sa_exc, sa_orm
    sys.modules.setdefault("sqlalchemy", sa)
    sys.modules.setdefault("sqlalchemy.exc", sa_exc)
    sys.modules.setdefault("sqlalchemy.orm", sa_orm)

    db = types.ModuleType("web_dashboard.database")
    # `id` needs an `in_` for finish_batch_parent's filter expression to evaluate; the
    # fake query below ignores the result.
    db.Job = type("Job", (), {"id": types.SimpleNamespace(in_=lambda *a, **k: None)})
    db.AuditLog = type("AuditLog", (), {})
    sys.modules.setdefault("web_dashboard.database", db)

    chain = types.ModuleType("web_dashboard.services.audit_chain")
    sys.modules.setdefault("web_dashboard.services.audit_chain", chain)


_install_stubs()
try:
    if _ROOT not in sys.path:
        sys.path.insert(0, _ROOT)
    _spec = importlib.util.spec_from_file_location("job_service", _PATH)
    js = importlib.util.module_from_spec(_spec)
    # The module does `from ..database import ...` / `from . import audit_chain`;
    # give it a package so the relative imports resolve to the stubs above.
    js.__package__ = "web_dashboard.services"
    _spec.loader.exec_module(js)
except Exception as exc:  # pragma: no cover
    try:
        import pytest
        pytest.skip(f"job_service import unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)


def test_absent_statuses_report_explicit_zero():
    """The whole point of the rollup: "0 failed" must be visible, not missing."""
    out = js._summarize_statuses([("completed", 7)])
    assert out["by_status"]["failed"] == 0
    assert out["by_status"]["running"] == 0
    assert set(out["by_status"]) >= set(js.BATCH_STATUSES)


def test_total_is_derived_from_the_counts():
    """A total computed separately could disagree with the numbers beside it."""
    out = js._summarize_statuses([("completed", 7), ("failed", 3), ("running", 2)])
    assert out["total"] == 12
    assert sum(out["by_status"].values()) == out["total"]


def test_empty_batch_is_all_zeros():
    out = js._summarize_statuses([])
    assert out["total"] == 0
    assert all(v == 0 for v in out["by_status"].values())
    assert set(out["by_status"]) == set(js.BATCH_STATUSES)


def test_none_rows_are_tolerated():
    """A count query that returned nothing must not blow up the page."""
    assert js._summarize_statuses(None)["total"] == 0


def test_unknown_status_is_carried_not_dropped():
    """Dropping it would make the total silently disagree with reality."""
    out = js._summarize_statuses([("completed", 1), ("weird", 4)])
    assert out["by_status"]["weird"] == 4
    assert out["total"] == 5


def test_null_status_is_bucketed_rather_than_crashing():
    out = js._summarize_statuses([(None, 2)])
    assert out["by_status"]["unknown"] == 2
    assert out["total"] == 2


def test_repeated_status_rows_accumulate():
    """group_by should collapse these, but summing is the safe reading."""
    out = js._summarize_statuses([("failed", 1), ("failed", 2)])
    assert out["by_status"]["failed"] == 3
    assert out["total"] == 3


def test_counts_are_coerced_to_int():
    """Some drivers hand back Decimal/str for an aggregate; the UI does arithmetic."""
    out = js._summarize_statuses([("completed", "4")])
    assert out["by_status"]["completed"] == 4
    assert out["total"] == 4


# ── finish_batch_parent ──────────────────────────────────────────────────────
#
# A *_bulk_deploy parent exists only to drive its children, so nothing ever gave it a
# terminal status: the worker sets a job `running` when it claims it and only marks it
# failed if dispatch RAISES. A batch that finished normally left its parent stuck at
# `running` 0% until reconcile_stale_jobs eventually failed a batch that had succeeded.


class _FakeJob:
    def __init__(self, job_id, status="completed"):
        self.id, self.status = job_id, status
        self.progress_pct = 0
        self.completed_at = self.updated_at = None
        self.error_message = None
        self.metadata_dict = {}


class _FakeQuery:
    def __init__(self, rows, want_one=None):
        self._rows, self._want_one = rows, want_one

    def filter(self, *a, **k):
        return self

    def all(self):
        return self._rows

    def first(self):
        return self._want_one


class _FakeDB:
    """Returns the child rows for the `.all()` lookup and the parent for `.first()`."""
    def __init__(self, children, parent):
        self._children, self._parent = children, parent
        self.commits = 0

    def query(self, *a, **k):
        return _FakeQuery(self._children, self._parent)

    def commit(self):
        self.commits += 1

    def refresh(self, _obj):
        pass


def _run(children_statuses, child_ids=None):
    children = [_FakeJob(f"c{i}", s) for i, s in enumerate(children_statuses)]
    parent = _FakeJob("parent", "running")
    db = _FakeDB(children, parent)
    ids = child_ids if child_ids is not None else [c.id for c in children]
    js.finish_batch_parent(db, "parent", ids)
    return parent


def test_a_fully_successful_batch_completes_its_parent():
    parent = _run(["completed", "completed", "completed"])
    assert parent.status == "completed"
    assert parent.progress_pct == 100
    assert parent.metadata_dict["batch_total"] == 3
    assert parent.metadata_dict["batch_completed"] == 3
    assert parent.metadata_dict["batch_failed"] == 0


def test_a_partly_failed_batch_still_completes_its_parent():
    """The parent ran to the end; one VM failing is the child's status to carry, not
    the parent's. Marking the parent failed would imply the batch never ran."""
    parent = _run(["completed", "failed", "completed"])
    assert parent.status == "completed"
    assert parent.metadata_dict["batch_failed"] == 1
    assert parent.metadata_dict["batch_completed"] == 2


def test_a_wholly_failed_batch_fails_its_parent():
    parent = _run(["failed", "failed"])
    assert parent.status == "failed"
    assert "2 instance(s)" in (parent.error_message or "")
    assert parent.metadata_dict["batch_failed"] == 2


def test_an_empty_child_list_still_closes_the_parent():
    """The whole point is that the parent never stays `running`. An empty or
    unresolvable child list must not leave it stuck."""
    parent = _run([], child_ids=[])
    assert parent.status == "completed"
    assert parent.metadata_dict["batch_total"] == 0


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
