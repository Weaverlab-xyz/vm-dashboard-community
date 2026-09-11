"""The audit log is hash-chained, and now also readable, exportable and self-checking.

Before this, 75 `log_audit` call sites wrote to a table whose entire read surface was
`GET /api/audit/verify` returning `{ok, count, first_broken_seq}` — no list, no filter,
no export, and nothing calling verify on a schedule. A tamper-evident log nobody reads
and nothing checks has the *property* without the *practice*.

Two things here are load-bearing beyond "the endpoints work":

  * **`ip_address` is inside the hash now (chain V2).** The column existed and no caller
    populated it, so nothing was unprotected in practice — but the moment an address was
    recorded it would have been alterable without breaking verification, which is the
    worst kind of integrity guarantee: one that reads as covering a field it does not.
  * **The migration verifies before it rewrites.** Re-hashing every row is exactly what
    someone who had edited one would want; the new chain would be internally consistent
    with the altered content and the tampering would vanish. So a table that does not
    verify under V1 is left alone and reported. That refusal is the single most important
    assertion in this file.

Run: python tests/test_audit_readable.py   (or under pytest)
"""
import ast
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="audit-readable-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDB}"
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-audit-readable-tests")

try:
    from fastapi import HTTPException
    from web_dashboard.database import AuditLog, Base, SessionLocal, engine
    from web_dashboard.services import audit_chain, config_service, job_service
    from web_dashboard.api import audit as api_audit
    from web_dashboard import logging_context as lc
except Exception as exc:  # pragma: no cover — app deps missing
    try:
        import pytest
        pytest.skip(f"app dependencies unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)

Base.metadata.create_all(bind=engine)


class _Admin:
    username = "root"
    is_admin = True
    is_effective_admin = True
    workgroups_list: list = []


def _reset():
    db = SessionLocal()
    try:
        db.query(AuditLog).delete()
        db.commit()
    finally:
        db.close()
    try:
        config_service.set(job_service._CHAIN_VERSION_KEY, "")
    except Exception:
        pass


def _write(n=3, ip=None):
    db = SessionLocal()
    try:
        for i in range(n):
            tok = lc.set_client_ip(ip or "")
            try:
                job_service.log_audit(db, f"user{i}", f"thing.action{i}",
                                      target_vm=f"target-{i}", details={"i": i})
            finally:
                lc.reset_client_ip(tok)
    finally:
        db.close()


def _drain(response) -> str:
    """StreamingResponse.body_iterator is an ASYNC iterator; drain it to a string."""
    import asyncio

    async def _go():
        out = []
        async for chunk in response.body_iterator:
            out.append(chunk if isinstance(chunk, (bytes, bytearray))
                       else str(chunk).encode())
        return b"".join(out).decode()

    return asyncio.run(_go())


def _rows():
    db = SessionLocal()
    try:
        return db.query(AuditLog).order_by(AuditLog.seq.asc()).all()
    finally:
        db.close()


# ── The chain now covers ip_address ───────────────────────────────────────────

def test_ip_address_is_inside_the_hash():
    _reset()
    _write(1, ip="203.0.113.7")
    db = SessionLocal()
    try:
        assert job_service.verify_audit_chain(db)["ok"]
        row = db.query(AuditLog).first()
        assert row.ip_address == "203.0.113.7", "the address should have been recorded"
        row.ip_address = "198.51.100.9"
        db.commit()
        out = job_service.verify_audit_chain(db)
        assert not out["ok"], "editing ip_address must break the chain — it did not under V1"
        assert out["first_broken_seq"] == 1
    finally:
        db.close()


def test_the_address_comes_from_the_request_context_and_is_blank_without_one():
    _reset()
    tok = lc.set_client_ip("203.0.113.7")
    db = SessionLocal()
    try:
        job_service.log_audit(db, "alice", "in.request")
    finally:
        lc.reset_client_ip(tok)
        db.close()
    # No request in scope — the job worker audits too, and it has no client.
    db = SessionLocal()
    try:
        job_service.log_audit(db, "worker", "in.worker")
    finally:
        db.close()
    by_action = {r.action: r.ip_address for r in _rows()}
    assert by_action["in.request"] == "203.0.113.7"
    assert not by_action["in.worker"], "a worker-written row must not inherit an address"


def test_an_explicit_address_still_wins():
    """The parameter predates the contextvar and callers that pass one must keep working."""
    _reset()
    tok = lc.set_client_ip("203.0.113.7")
    db = SessionLocal()
    try:
        job_service.log_audit(db, "alice", "explicit", ip_address="192.0.2.1")
    finally:
        lc.reset_client_ip(tok)
        db.close()
    assert _rows()[0].ip_address == "192.0.2.1"


# ── The migration ─────────────────────────────────────────────────────────────

def _write_v1_chain(n=3):
    """Rows hashed the old way, as an un-migrated database would hold them."""
    _reset()
    db = SessionLocal()
    try:
        prev = audit_chain.GENESIS_PREV
        for i in range(1, n + 1):
            e = AuditLog(id=f"id-{i}", timestamp=datetime(2026, 1, 1) + timedelta(minutes=i),
                         username=f"u{i}", action=f"a{i}", target_vm=f"t{i}",
                         details=json.dumps({"i": i}), ip_address=None,
                         seq=i, prev_hash=prev)
            e.entry_hash = audit_chain.compute_entry_hash_v1(
                e.seq, e.timestamp, e.username, e.action, e.target_vm, e.details, prev)
            prev = e.entry_hash
            db.add(e)
        db.commit()
    finally:
        db.close()


def test_migration_rechains_a_healthy_v1_table():
    _write_v1_chain(3)
    db = SessionLocal()
    try:
        assert audit_chain.verify_chain_v1(_rows()) == (True, None)
        assert not job_service.verify_audit_chain(db)["ok"], "V1 rows must not verify as V2"
        out = job_service.rechain_audit_log(db)
        assert out["status"] == "rechained" and out["count"] == 3, out
        assert job_service.verify_audit_chain(db)["ok"], "post-migration chain must verify"
    finally:
        db.close()


def test_migration_REFUSES_to_rechain_a_broken_table():
    """The assertion this file exists for. Rewriting the hashes of a tampered table
    would make the tampering undetectable — a migration must never launder evidence."""
    _write_v1_chain(4)
    db = SessionLocal()
    try:
        victim = db.query(AuditLog).filter(AuditLog.seq == 2).first()
        victim.username = "attacker"
        db.commit()
        before = [(r.seq, r.entry_hash) for r in _rows()]

        out = job_service.rechain_audit_log(db)
        assert out["status"] == "refused", out
        assert out["first_broken_seq"] == 2, out
        assert [(r.seq, r.entry_hash) for r in _rows()] == before, \
            "a refused migration must leave every hash exactly as it found it"
        # And it must not mark the database as migrated, or the next boot skips the check.
        assert str(config_service.get(job_service._CHAIN_VERSION_KEY, "") or "") != "2"
    finally:
        db.close()


def test_migration_is_idempotent_and_survives_a_lost_marker():
    _write_v1_chain(2)
    db = SessionLocal()
    try:
        assert job_service.rechain_audit_log(db)["status"] == "rechained"
        assert job_service.rechain_audit_log(db)["status"] == "current"
        # A restored database / rolled-back image can lose the marker; the rows are
        # already V2, so this is not a migration and must not rewrite anything.
        config_service.set(job_service._CHAIN_VERSION_KEY, "")
        before = [(r.seq, r.entry_hash) for r in _rows()]
        assert job_service.rechain_audit_log(db)["status"] == "current"
        assert [(r.seq, r.entry_hash) for r in _rows()] == before
    finally:
        db.close()


# ── Verification does not materialize the table ───────────────────────────────

def test_verify_streams_rather_than_loading_everything():
    """`audit_log` has no retention and cannot have one — pruning any row breaks the
    chain by construction — so the check that proves it intact must not need it all in
    memory. Asserted structurally because the failure mode is invisible until the table
    is large enough to hurt."""
    src = ast.dump(ast.parse(
        open(os.path.join(_ROOT, "web_dashboard/services/job_service.py"),
             encoding="utf-8").read()))
    fn = [n for n in ast.walk(ast.parse(open(os.path.join(
        _ROOT, "web_dashboard/services/job_service.py"), encoding="utf-8").read()))
        if isinstance(n, ast.FunctionDef) and n.name == "verify_audit_chain"][0]
    body = ast.dump(fn)
    assert "yield_per" in body, "verify_audit_chain should stream"
    assert "'all'" not in body, "verify_audit_chain should not call .all()"
    assert src  # keep the parse meaningful


# ── The scheduled check ───────────────────────────────────────────────────────

def test_the_scanner_is_quiet_on_an_intact_chain_and_loud_on_a_broken_one():
    """A new condition that silently returns 0 forever is a dud feature, so assert it
    both ways. The bucket carries the offending seq as well as the day: a standing break
    should repeat daily, but a NEW break must not be swallowed by the old one's message."""
    import asyncio

    from web_dashboard.services import notify_scanner

    _reset()
    _write(3, ip="203.0.113.7")

    emitted = []
    orig = notify_scanner._emit
    notify_scanner._emit = lambda db, event, **kw: (emitted.append((event, kw)) or 1)
    db = SessionLocal()
    try:
        assert asyncio.run(notify_scanner._scan_audit(db, "2026-09-07")) == 0
        assert not emitted, "an intact chain must not notify"

        row = db.query(AuditLog).filter(AuditLog.seq == 2).first()
        row.username = "attacker"
        db.commit()

        assert asyncio.run(notify_scanner._scan_audit(db, "2026-09-07")) == 1
        assert len(emitted) == 1, emitted
        event, kw = emitted[0]
        assert event == "audit.chain_broken", event
        assert kw["bucket"] == "2026-09-07:2", kw["bucket"]
        assert "2" in str(kw["fields"]["First broken seq"])
    finally:
        notify_scanner._emit = orig
        db.close()


def test_the_new_event_is_registered_and_on_by_default():
    """An event missing from the catalog is delivered at a default severity and cannot
    be filtered; one missing from the default set ships silently off."""
    from web_dashboard.services import notify_policy

    assert notify_policy.EVENT_SEVERITY.get("audit.chain_broken") == "critical"
    assert "audit.chain_broken" in notify_policy.DEFAULT_EVENT_TYPES


# ── The read API ──────────────────────────────────────────────────────────────

def test_list_filters_and_paginates():
    _reset()
    _write(5, ip="203.0.113.7")
    db = SessionLocal()
    try:
        page1 = api_audit.list_audit(page=1, page_size=2, username=None, action=None,
                                     target=None, since=None, until=None, q=None,
                                     current_user=_Admin(), db=db)
        assert page1["total"] == 5 and len(page1["entries"]) == 2
        assert page1["entries"][0]["seq"] > page1["entries"][1]["seq"], "newest first"

        one = api_audit.list_audit(page=1, page_size=50, username="user3", action=None,
                                   target=None, since=None, until=None, q=None,
                                   current_user=_Admin(), db=db)
        assert one["total"] == 1 and one["entries"][0]["username"] == "user3"

        # Action filter is a PREFIX — actions are namespaced (`agent.create`).
        pref = api_audit.list_audit(page=1, page_size=50, username=None, action="thing.",
                                    target=None, since=None, until=None, q=None,
                                    current_user=_Admin(), db=db)
        assert pref["total"] == 5, pref["total"]

        none = api_audit.list_audit(page=1, page_size=50, username=None, action="nope.",
                                    target=None, since=None, until=None, q=None,
                                    current_user=_Admin(), db=db)
        assert none["total"] == 0 and none["entries"] == []
    finally:
        db.close()


def test_a_bad_date_is_a_400_not_a_500():
    _reset()
    db = SessionLocal()
    try:
        try:
            api_audit.list_audit(page=1, page_size=10, username=None, action=None,
                                 target=None, since="not-a-date", until=None, q=None,
                                 current_user=_Admin(), db=db)
            raise AssertionError("a malformed date should be refused")
        except HTTPException as exc:
            assert exc.status_code == 400, exc.status_code
    finally:
        db.close()


def test_export_carries_the_hashes_and_reads_oldest_first():
    """An export is for handing to something else. Without the hashes the receiver has a
    table it must take on trust; with them it can recompute the chain itself."""
    _reset()
    _write(3, ip="203.0.113.7")
    db = SessionLocal()
    try:
        body = _drain(api_audit.export_audit(
            fmt="csv", username=None, action=None, target=None, since=None, until=None,
            q=None, current_user=_Admin(), db=db))
    finally:
        db.close()
    lines = [l for l in body.splitlines() if l.strip()]
    assert lines[0].split(",")[:3] == ["seq", "timestamp", "username"]
    assert "entry_hash" in lines[0] and "prev_hash" in lines[0]
    assert lines[1].startswith("1,"), "oldest first, so the chain reads forwards"
    assert len(lines) == 4, lines


def test_an_empty_export_is_still_a_valid_csv():
    """An empty file reads like a failure; a header with no rows reads like an answer."""
    _reset()
    db = SessionLocal()
    try:
        body = _drain(api_audit.export_audit(
            fmt="csv", username=None, action=None, target=None, since=None, until=None,
            q=None, current_user=_Admin(), db=db))
    finally:
        db.close()
    assert body.strip().startswith("seq,timestamp,username"), body[:80]


def test_json_export_is_parseable():
    _reset()
    _write(2)
    db = SessionLocal()
    try:
        body = _drain(api_audit.export_audit(
            fmt="json", username=None, action=None, target=None, since=None, until=None,
            q=None, current_user=_Admin(), db=db))
    finally:
        db.close()
    parsed = json.loads(body)
    assert len(parsed) == 2 and parsed[0]["seq"] == 1
    assert "entry_hash" in parsed[0]


def test_every_read_route_needs_an_administrator_or_an_explicit_grant():
    """Stated rather than assumed — see the module docstring in api/audit.py.

    These routes are gated on ``audit:read`` rather than the admin flag, so the log can be
    handed to somebody whose job is reading it without making them an administrator. The
    property that must not slip is which FORM of the check is used:
    ``require_explicit_permission`` refuses a user whose permission map is empty, whereas
    the plain ``require_permission`` treats an empty map as UNRESTRICTED — which would
    open the audit log to every account that predates the permission columns.
    """
    import inspect
    for name in ("verify_audit_log", "list_audit", "list_actions", "export_audit"):
        sig = inspect.signature(getattr(api_audit, name))
        dep = sig.parameters["current_user"].default
        assert getattr(dep, "dependency", None) is not None, name
        fn = dep.dependency
        assert getattr(fn, "permission_scope", None) == "audit", (
            f"{name} is not gated on the audit scope (got {fn.__name__})")
        assert getattr(fn, "permission_level", None) == "read", name
        assert getattr(fn, "permission_explicit", False) is True, (
            f"{name} uses the permissive form, so an empty permission map would read as "
            "unrestricted and the audit log would be world-readable to legacy accounts")


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
