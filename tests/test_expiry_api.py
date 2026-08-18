"""Unit tests for the auto-delete timer's API surface — api/expiry.py's selection guard
and expiry_reaper.set_expiry's per-item behaviour.

Three properties worth pinning:

  * an id the caller's RBAC filter hid comes back as **unknown, not forbidden**. A 403
    would confirm the resource exists, which is the leak plan_bulk_run's docstring calls
    out; this endpoint takes ids from the same namespace and must behave the same way;
  * the selection is capped and de-duped, so a mis-clicked "select all" can't rewrite an
    unbounded number of rows in one audited act;
  * one ineligible row is REPORTED, not raised — a mixed selection must still apply to
    the rows that are valid, or a single stale id discards the operator's whole action.

Plus a source-level check that the endpoint invalidates the inventory cache. That one
can't be tested behaviourally without a running app, and without it an Extend shows the
stale value for 60s and the operator clicks again.

sqlalchemy / the DB models / config_service are stubbed. Skips if fastapi isn't
installed. Runs under pytest, or standalone:
    python tests/test_expiry_api.py
"""
import ast
import os
import re
import sys
import types
from datetime import datetime, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

CONF = {"resource_expiry_enabled": "1", "resource_expiry_max_total_hours": "720"}


def _install_stubs():
    # Marked as a package: expiry_reaper pulls in job_service, which imports
    # sqlalchemy.exc — a plain module stub makes that a confusing "not a package" error.
    sa = types.ModuleType("sqlalchemy")
    sa.__path__ = []
    sa.text = lambda s: s
    # job_service.list_jobs builds `~and_(...)`; only construction + inversion matter here.
    sa.and_ = lambda *a, **k: type("_Expr", (), {"__invert__": lambda s: s})()
    sa_orm = types.ModuleType("sqlalchemy.orm")
    sa_orm.Session = type("Session", (), {})
    sa.orm = sa_orm
    sa_exc = types.ModuleType("sqlalchemy.exc")
    sa_exc.IntegrityError = type("IntegrityError", (Exception,), {})
    sa.exc = sa_exc
    sys.modules.setdefault("sqlalchemy", sa)
    sys.modules.setdefault("sqlalchemy.orm", sa_orm)
    sys.modules.setdefault("sqlalchemy.exc", sa_exc)

    db = types.ModuleType("web_dashboard.database")
    for name in ("CloudDatabase", "Job", "K8sCluster", "VirtualDesktop", "User",
                 "AuditLog", "JobLog", "HypervisorConnection", "HypervisorVMCache"):
        setattr(db, name, type(name, (), {}))
    db.get_db = lambda: None
    db._is_sqlite = True
    sys.modules.setdefault("web_dashboard.database", db)

    cfg = types.ModuleType("web_dashboard.services.config_service")
    cfg.get = lambda key, default="", workgroup=None: CONF.get(key, default)
    cfg.get_bool = lambda key, default=False: (
        str(CONF.get(key, default)).strip().lower() in ("1", "true", "yes", "on")
    )
    cfg.set = lambda key, value: CONF.__setitem__(key, value)
    sys.modules.setdefault("web_dashboard.services.config_service", cfg)

    conf_mod = types.ModuleType("web_dashboard.config")
    conf_mod.settings = types.SimpleNamespace()
    sys.modules.setdefault("web_dashboard.config", conf_mod)


_install_stubs()
try:
    from web_dashboard.services import expiry_reaper, inventory_service
except Exception as exc:  # pragma: no cover
    try:
        import pytest
        pytest.skip(f"expiry_reaper import unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)


_API = os.path.join(_ROOT, "web_dashboard", "api", "expiry.py")


# ── fakes ────────────────────────────────────────────────────────────────────

class _FakeRow:
    """Stands in for a Job / CloudDatabase / K8sCluster row."""
    def __init__(self, created_at=None, expires_at=None):
        self.created_at = created_at or (datetime.utcnow() - timedelta(hours=5))
        self.expires_at = expires_at
        self.expiry_warned_at = datetime.utcnow()


class _FakeSession:
    """Enough Session to satisfy expiry_reaper._resolve_row and the commit path."""
    def __init__(self, rows=None, fail_on_commit=False):
        self.rows = rows or {}
        self.commits = 0
        self.rollbacks = 0
        self.fail_on_commit = fail_on_commit

    def query(self, model):
        return self

    def filter(self, *a, **k):
        return self

    def first(self):
        return self._next

    def commit(self):
        if self.fail_on_commit:
            raise RuntimeError("db exploded")
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def _patch_resolve(rows):
    """Replace _resolve_row so the tests don't need a real query builder. rows maps an
    inventory id to (row, created_at, current_expiry) or None for 'gone'."""
    original = expiry_reaper._resolve_row
    expiry_reaper._resolve_row = lambda db, inv_id: rows.get(inv_id)
    return original


def _item(**over):
    base = {"id": "job:a", "kind": "vm", "cloud": "aws", "name": "vm-1",
            "source": "provisioned", "state": "active", "workgroup": None}
    base.update(over)
    return base


def _silence_audit():
    """log_audit needs a real session + the audit chain; the tests assert on the returned
    result, not on the audit row (test_audit_chain covers the chain itself)."""
    from web_dashboard.services import job_service
    original = job_service.log_audit
    job_service.log_audit = lambda *a, **k: None
    return original


# ── set_expiry: per-item behaviour ───────────────────────────────────────────

def test_a_valid_extend_is_applied_and_reported():
    _silence_audit()
    row = _FakeRow()
    _patch_resolve({"job:a": (row, row.created_at, None)})
    db = _FakeSession()
    out = expiry_reaper.set_expiry(db, [_item()], extend_hours=24, actor="alice")
    assert out["failed"] == []
    assert len(out["updated"]) == 1
    assert out["updated"][0]["expires_at"] is not None
    assert row.expires_at is not None and db.commits == 1


def test_an_ineligible_row_is_reported_not_raised():
    """A registered database in the selection must not discard the whole action — the
    operator gets a per-row reason and every valid row still lands."""
    _silence_audit()
    good = _FakeRow()
    _patch_resolve({"job:a": (good, good.created_at, None),
                    "clouddb:b": (_FakeRow(), datetime.utcnow(), None)})
    db = _FakeSession()
    items = [_item(), _item(id="clouddb:b", kind="database", state="available",
                            source="registered", name="db-1")]
    out = expiry_reaper.set_expiry(db, items, extend_hours=24, actor="alice")
    assert len(out["updated"]) == 1 and out["updated"][0]["id"] == "job:a"
    assert len(out["failed"]) == 1 and out["failed"][0]["id"] == "clouddb:b"
    assert "registered" in out["failed"][0]["error"]


def test_a_row_that_vanished_is_reported():
    _silence_audit()
    _patch_resolve({})                                  # nothing resolves
    out = expiry_reaper.set_expiry(_FakeSession(), [_item()], extend_hours=24, actor="a")
    assert out["updated"] == [] and len(out["failed"]) == 1
    assert "inventory" in out["failed"][0]["error"]


def test_a_db_failure_rolls_back_and_is_reported():
    _silence_audit()
    row = _FakeRow()
    _patch_resolve({"job:a": (row, row.created_at, None)})
    db = _FakeSession(fail_on_commit=True)
    out = expiry_reaper.set_expiry(db, [_item()], extend_hours=24, actor="alice")
    assert out["updated"] == [] and len(out["failed"]) == 1
    assert db.rollbacks == 1


def test_a_clamped_extend_is_flagged():
    """The operator asked for more than policy allows. Clamp and say so — a 400 would
    discard an intent ("keep this longer") that policy can partly honour."""
    _silence_audit()
    CONF["resource_expiry_max_total_hours"] = "100"
    try:
        row = _FakeRow(created_at=datetime.utcnow() - timedelta(hours=90))
        _patch_resolve({"job:a": (row, row.created_at, None)})
        out = expiry_reaper.set_expiry(_FakeSession(), [_item()],
                                       extend_hours=500, actor="alice")
        assert out["updated"][0]["clamped"] is True
    finally:
        CONF["resource_expiry_max_total_hours"] = "720"


def test_never_is_refused_for_a_non_admin():
    _silence_audit()
    CONF["resource_expiry_allow_never"] = "1"
    try:
        row = _FakeRow(expires_at=datetime.utcnow() + timedelta(hours=5))
        _patch_resolve({"job:a": (row, row.created_at, row.expires_at)})
        out = expiry_reaper.set_expiry(_FakeSession(), [_item()], never=True,
                                       is_admin=False, actor="alice")
        assert out["updated"] == [] and len(out["failed"]) == 1
        # And the row is untouched — a refusal must not half-apply.
        assert row.expires_at is not None
    finally:
        CONF.pop("resource_expiry_allow_never", None)


def test_never_clears_the_timer_for_an_allowed_admin():
    _silence_audit()
    CONF["resource_expiry_allow_never"] = "1"
    try:
        row = _FakeRow(expires_at=datetime.utcnow() + timedelta(hours=5))
        _patch_resolve({"job:a": (row, row.created_at, row.expires_at)})
        out = expiry_reaper.set_expiry(_FakeSession(), [_item()], never=True,
                                       is_admin=True, actor="admin")
        assert len(out["updated"]) == 1
        assert out["updated"][0]["expires_at"] is None
        assert row.expires_at is None
    finally:
        CONF.pop("resource_expiry_allow_never", None)


def test_moving_an_expiry_resets_the_warned_flag():
    """The old warning described a deadline that no longer exists, so a moved timer
    earns a fresh one."""
    _silence_audit()
    row = _FakeRow()
    _patch_resolve({"job:a": (row, row.created_at, None)})
    expiry_reaper.set_expiry(_FakeSession(), [_item()], extend_hours=24, actor="alice")
    assert row.expiry_warned_at is None


# ── the sweep's last-result plumbing ─────────────────────────────────────────

def test_a_corrupt_stored_report_does_not_raise():
    """An operator reading "corrupted" learns more than one staring at an empty panel."""
    CONF["resource_expiry_last_sweep"] = "{not json"
    try:
        out = expiry_reaper.get_last_sweep_result()
        assert out.get("corrupted") is True and "raw_preview" in out
    finally:
        CONF.pop("resource_expiry_last_sweep", None)


def test_an_absent_report_reads_as_never_run():
    CONF.pop("resource_expiry_last_sweep", None)
    assert expiry_reaper.get_last_sweep_result() == {"never_run": True}


def test_the_enqueue_is_a_no_op_while_the_feature_is_off():
    CONF["resource_expiry_enabled"] = "0"
    try:
        assert expiry_reaper.enqueue_sweep_if_due(_FakeSession()) is None
    finally:
        CONF["resource_expiry_enabled"] = "1"


# ── api/expiry.py source-level guarantees ────────────────────────────────────

def _api_src():
    return open(_API, encoding="utf-8").read()


def _strip_docstring(fn):
    """The function node without its docstring.

    These checks assert on what the CODE does, and the docstrings here explain the
    reasoning — including naming the status codes that must not be used. Scanning the
    prose alongside the code makes a well-documented function fail its own test.
    """
    body = fn.body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        body = body[1:]
    clone = ast.Module(body=body, type_ignores=[])
    return clone


def test_the_set_endpoint_invalidates_the_inventory_cache():
    """/api/inventory is cached 60s and NOTHING else invalidates that key. Without this
    an Extend appears to do nothing and the operator clicks again."""
    src = _api_src()
    assert "deployment_inventory" in src, (
        "api/expiry does not invalidate the deployment_inventory cache key")
    assert re.search(r"cache_service\.invalidate\(", src), (
        "api/expiry references the cache key but never calls invalidate")


def test_the_selection_is_capped():
    src = _api_src()
    assert "MAX_TARGETS" in src and "MAX_BULK_TARGETS" in src, (
        "api/expiry should reuse inventory_service.MAX_BULK_TARGETS as its cap")
    assert inventory_service.MAX_BULK_TARGETS >= 1


def test_a_hidden_id_is_reported_as_unknown_not_forbidden():
    """The non-leak property. _load_visible must resolve ids against the RBAC-filtered
    set and 404 an id outside it — never 403, which would confirm existence."""
    fn = None
    for node in ast.walk(ast.parse(_api_src())):
        if isinstance(node, ast.FunctionDef) and node.name == "_load_visible":
            fn = node
    assert fn is not None, "_load_visible not found in api/expiry.py"
    src = ast.unparse(_strip_docstring(fn))
    assert "visible_to" in src, "_load_visible does not apply the RBAC predicate"
    assert "404" in src, "_load_visible should 404 an id the caller cannot see"
    assert "403" not in src, (
        "_load_visible raises 403 for a hidden id — that confirms the resource exists")


def test_the_force_sweep_does_not_bypass_the_safety_gates():
    """`force` may only skip the already-active check. Letting it skip dry-run, the arm
    delay or the per-pass cap would make one request able to waive every brake."""
    src = _api_src()
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name == "force_sweep")
    body = ast.unparse(_strip_docstring(fn))
    for waived in ("dry_run", "armed", "max_per_pass", "ARM_DELAY"):
        assert waived not in body, (
            f"force_sweep touches {waived} — force must only bypass the active-pass check")


def test_the_force_sweep_opts_out_of_the_recency_window():
    """The enqueue's recency window exists to collapse the two app workers' simultaneous
    TIMER ticks into one row. Applied to this endpoint it would refuse an operator's button
    for up to half a sweep interval and report it as "already queued or running" — a plain
    lie about why nothing happened. Only the timer path ever fires twice."""
    src = _api_src()
    fn = next(n for n in ast.walk(ast.parse(src))
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name == "force_sweep")
    body = ast.unparse(_strip_docstring(fn))
    assert "min_gap_seconds=0" in body, (
        "force_sweep must pass min_gap_seconds=0 — otherwise Run Sweep silently no-ops "
        "for half an interval after every scheduled pass")


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
    sys.exit(1 if failures else 0)
