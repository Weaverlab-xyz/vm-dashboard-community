"""Assigning Password Safe attributes: the vocabulary guard, the fan-out, the audit trail.

Writing into a customer's PAM tenant, so the refusals matter more than the successes.
Four properties:

1. **A read-only type is refused before any call.** `Criticality` is `IsReadOnly: true` in
   the tenant this was built against — Password Safe would refuse it anyway, but catching
   it here turns a provider error into a sentence and stops a bulk apply failing
   identically on every target.
2. **An id this tenant does not have is refused.** The vocabulary is read fresh on every
   write, so a stale page cannot assign something that was deleted in the console.
3. **Nothing is typed.** An attribute is picked from a fixed vocabulary by id, which is
   why — unlike the cloud tag editor and the Proxmox tag verb — there is no charset guard
   here. The test that matters is that an id outside the vocabulary cannot get through.
4. **One asset's failure is not the batch's**, and only real changes are audited.

The endpoint shapes were verified against a live tenant on 2026-09-23:
``POST``/``DELETE Assets/{assetID}/Attributes/{attributeID}``, with a ``DELETE`` of an
already-absent attribute answering 404 — treated as success, since the caller asked for it
gone and it is gone.

Runs under pytest, or standalone:  python tests/test_ps_attribute_writes.py
"""
import asyncio
import os
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.environ.setdefault("DATABASE_URL",
                      "sqlite:///" + os.path.join(tempfile.mkdtemp(), "pswrite.db").replace("\\", "/"))
os.environ.setdefault("JWT_SECRET_KEY", "x" * 32)

# Probe the app deps BY NAME; anything else must propagate — a blanket skip would let this
# file exit 0 having tested nothing. See tests/test_import_guard_narrowness.py.
try:
    import fastapi  # noqa: F401
    import pydantic  # noqa: F401
    import sqlalchemy  # noqa: F401
except ModuleNotFoundError as exc:  # pragma: no cover — bare interpreter
    try:
        import pytest
        pytest.skip(f"app deps unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)

from fastapi import HTTPException

from web_dashboard.api import ps_attributes as api
from web_dashboard.database import AuditLog, Base, SessionLocal, engine
from web_dashboard.services import ps_attribute_catalog as pac

Base.metadata.create_all(bind=engine)

# The real shapes, from the live tenant.
_TYPES = [{"AttributeTypeID": 1, "Name": "Geography", "IsReadOnly": False},
          {"AttributeTypeID": 3, "Name": "Criticality", "IsReadOnly": True}]
_VALUES = {"1": [{"AttributeID": 21, "ShortName": "North America"},
                 {"AttributeID": 72, "ShortName": "Zealandia"}],
           "3": [{"AttributeID": 17, "ShortName": "High"}]}
_VOCAB = pac.build_vocabulary(_TYPES, _VALUES)


class _Writer:
    """Stands in for set_asset_attribute, recording every call."""

    def __init__(self, fail_on=()):
        self.calls, self.fail_on = [], set(fail_on)

    async def __call__(self, asset_id, attribute_id, *, assign, tenant=None):
        self.calls.append((int(asset_id), int(attribute_id), assign))
        if int(asset_id) in self.fail_on:
            raise RuntimeError("Password Safe refused the attribute change (403)")


class _User:
    username = "tester"
    is_admin = True


def _post(asset_ids, attribute_id, *, assign=True, writer=None, db=None,
          vocab_state="ok"):
    writer = writer or _Writer()
    own = db is None
    db = db or SessionLocal()

    async def _vocab(tenant=None):
        return {"state": vocab_state, "types": _TYPES, "values_by_type": _VALUES,
                "detail": "" if vocab_state == "ok" else "no API"}

    real = (api.ps_api_service.read_attribute_vocabulary,
            api.ps_api_service.set_asset_attribute,
            api._enabled, api.ps_api_service.configured)
    api.ps_api_service.read_attribute_vocabulary = _vocab
    api.ps_api_service.set_asset_attribute = writer
    api._enabled = lambda: True
    api.ps_api_service.configured = lambda: True
    try:
        payload = api.AssetAttributeRequest(asset_ids=asset_ids,
                                            attribute_id=attribute_id, assign=assign)
        return asyncio.run(api.set_asset_attributes(payload, db=db,
                                                    current_user=_User())), writer
    finally:
        (api.ps_api_service.read_attribute_vocabulary,
         api.ps_api_service.set_asset_attribute,
         api._enabled, api.ps_api_service.configured) = real
        if own:
            db.close()


def _refused(asset_ids, attribute_id, **kw):
    writer = _Writer()
    try:
        _post(asset_ids, attribute_id, writer=writer, **kw)
    except HTTPException as exc:
        return exc.status_code, str(exc.detail), writer.calls
    return 0, "", writer.calls


# ── 1. the vocabulary guard ──────────────────────────────────────────────────

def test_a_read_only_type_is_refused_before_any_call():
    code, detail, calls = _refused([64], 17)          # Criticality = High
    assert code == 409
    assert "read-only" in detail and "Criticality" in detail
    assert calls == [], "the tenant was written to despite the refusal"


def test_an_attribute_this_tenant_does_not_have_is_refused():
    code, detail, calls = _refused([64], 99999)
    assert code == 409 and calls == []
    assert "vocabulary" in detail


def test_the_refusal_is_409_not_400():
    """The request is well formed and the caller is entitled to make it — this
    particular attribute is not assignable. 400 would read as a typo."""
    assert _refused([64], 17)[0] == 409


def test_the_vocabulary_is_read_fresh_so_a_stale_page_cannot_assign():
    """The picker is loaded once per page; the vocabulary can change in the console
    underneath it. Re-reading on every write is what makes that safe."""
    import inspect
    src = inspect.getsource(api.set_asset_attributes)
    assert "_vocabulary()" in src
    # The QUALIFIED call, not the bare name: the route is itself called
    # `set_asset_attributes`, so an unqualified search matches its own `def` line at
    # index 0 and the ordering assertion below would be meaningless.
    assert src.index("_vocabulary()") < src.index("ps_api_service.set_asset_attribute")


# ── 2. selection refusals ────────────────────────────────────────────────────

def test_no_assets_is_refused():
    code, _d, calls = _refused([], 21)
    assert code == 400 and calls == []


def test_over_the_cap_is_refused_whole():
    code, detail, calls = _refused(list(range(api.MAX_TARGETS + 1)), 21)
    assert code == 400 and str(api.MAX_TARGETS) in detail
    assert calls == [], "a partial batch ran before the cap was checked"


def test_a_repeated_asset_is_written_once():
    _out, writer = _post([64, 64, 65], 21)
    assert [c[0] for c in writer.calls] == [64, 65]


# ── 3. the fan-out ───────────────────────────────────────────────────────────

def test_every_asset_is_written_and_the_result_names_the_attribute():
    out, writer = _post([64, 65], 21)
    assert [c[0] for c in writer.calls] == [64, 65]
    assert all(c[2] is True for c in writer.calls)
    assert out["count"] == 2 and out["assigned"] is True
    assert out["type"] == "Geography" and out["value"] == "North America"


def test_removing_passes_assign_false():
    _out, writer = _post([64], 21, assign=False)
    assert writer.calls == [(64, 21, False)]


def test_one_failure_does_not_end_the_batch_and_is_named():
    out, _w = _post([64, 65, 66], 21, writer=_Writer(fail_on=[65]))
    assert [u["name"] for u in out["updated"]] == ["64", "66"]
    assert [f["name"] for f in out["failed"]] == ["65"]
    assert "403" in out["failed"][0]["error"]


# ── 4. the audit trail ───────────────────────────────────────────────────────

def _rows(db, action):
    return db.query(AuditLog).filter(AuditLog.action == action).all()


def test_an_assignment_writes_one_audit_row_per_asset():
    db = SessionLocal()
    try:
        before = len(_rows(db, api.AUDIT_ASSIGN))
        _post([71, 72], 21, db=db)
        rows = _rows(db, api.AUDIT_ASSIGN)
        assert len(rows) - before == 2
        assert {r.target_vm for r in rows[-2:]} == {"asset:71", "asset:72"}
        assert rows[-1].details_dict["value"] == "North America"
    finally:
        db.close()


def test_a_removal_is_a_different_action_so_audit_can_tell_them_apart():
    db = SessionLocal()
    try:
        _post([73], 21, assign=False, db=db)
        assert [r.target_vm for r in _rows(db, api.AUDIT_REMOVE)] == ["asset:73"]
    finally:
        db.close()


def test_a_failed_asset_writes_no_audit_row():
    db = SessionLocal()
    try:
        before = len(_rows(db, api.AUDIT_ASSIGN))
        _post([81], 21, db=db, writer=_Writer(fail_on=[81]))
        assert len(_rows(db, api.AUDIT_ASSIGN)) == before
    finally:
        db.close()


def test_both_audit_actions_are_dotted_so_the_prefix_filter_groups_them():
    """api/audit.py filters `action` by PREFIX. `attributes.` groups every write into
    Password Safe, and groups them APART from the cloud `tags.` writes — these change a
    customer's PAM tenant, which is a different question."""
    assert api.AUDIT_ASSIGN.startswith("attributes.")
    assert api.AUDIT_REMOVE.startswith("attributes.")
    assert not api.AUDIT_ASSIGN.startswith("tags.")


# ── 5. gating ────────────────────────────────────────────────────────────────

def test_the_write_route_is_admin_only():
    """There is no password_safe permission scope and this does not add one: adding a
    scope silently revokes it for everyone (api/auth.py). Writing into a customer's PAM
    tenant is also not a default-on capability."""
    import inspect
    src = inspect.getsource(api)
    assert "require_admin" in src
    assert "PERMISSION_SCOPES" not in src


def test_no_new_permission_scope_was_invented():
    from web_dashboard.api.auth import PERMISSION_SCOPES
    for invented in ("password_safe", "passwordsafe", "attributes"):
        assert invented not in PERMISSION_SCOPES


def test_an_unreadable_vocabulary_refuses_rather_than_writing_blind():
    code, _d, calls = _refused([64], 21, vocab_state="unavailable")
    assert code == 502 and calls == []


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
