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
    db.Job = type("Job", (), {})
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
