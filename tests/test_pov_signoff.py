"""The close-out report and sign-off: ``services/pov_signoff``, its routes, ``pov/report.html``.

What is pinned, because each would fail quietly:

  * **No customer field, no free text.** ``PovEnvironment`` says this database is not
    approved to hold a company name; a sign-off with a comment box is where one gets typed.
    So the table's columns are checked, and so is the report: it has no input of any kind,
    and the customer and opportunity are literal placeholders.
  * **No email reaches the report.** An accessor's email domain is the company name. The
    summary the report renders is checked for the address of an accessor that has one.
  * **A sign-off is a snapshot.** Ticking more afterwards marks it ``current: False``
    rather than stretching it over work it never saw.
  * **It is never deleted.** Withdrawing keeps the row, marked.
  * **A double click is one sign-off.**
  * **It works on a destroyed POV**, which is when a close-out is most often asked for.

Runs under pytest, or standalone:
    python tests/test_pov_signoff.py
"""
import json
import os
import re
import sys
import tempfile
import uuid

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="pov-signoff-test-"), "test.db")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_TMPDB}")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-pov-signoff")

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover
    print("SKIP: fastapi not installed")
    sys.exit(0)

from web_dashboard import database as d  # noqa: E402

d.Base.metadata.create_all(bind=d.engine)

from web_dashboard.api import pov as pov_api  # noqa: E402
from web_dashboard.api import pov_accessor as accessor_api  # noqa: E402
from web_dashboard.api.auth import get_current_user  # noqa: E402
from web_dashboard.services import pov_signoff, pov_summary, pov_use_cases  # noqa: E402

_REPORT = os.path.join(_ROOT, "web_dashboard", "templates", "pov", "report.html")
_MAIN = os.path.join(_ROOT, "web_dashboard", "main.py")
_CARD_A = "pov-devops-no-inbound"
_CARD_B = "pov-hypervisor-rotate-root"


def _env(status="active"):
    db = d.SessionLocal()
    try:
        env = d.PovEnvironment(platform="skytap", name="pov-" + uuid.uuid4().hex[:8],
                               status=status, created_by="se1",
                               pra_tenant_id="t-pra", ps_tenant_id="t-ps")
        db.add(env)
        db.commit()
        return env.id
    finally:
        db.close()


def _tick(env_id, card, by="se1", kind=pov_use_cases.KIND_SE):
    db = d.SessionLocal()
    try:
        env = db.get(d.PovEnvironment, env_id)
        pov_use_cases.set_state(db, env, card, state="done", by=by, by_kind=kind)
    finally:
        db.close()


def _record(env_id, by="se1", kind=pov_use_cases.KIND_SE):
    db = d.SessionLocal()
    try:
        return pov_signoff.record(db, db.get(d.PovEnvironment, env_id), by=by, by_kind=kind)
    finally:
        db.close()


def _summary(env_id):
    db = d.SessionLocal()
    try:
        return pov_summary.build(db, db.get(d.PovEnvironment, env_id))
    finally:
        db.close()


# ── the data rules ───────────────────────────────────────────────────────────

def test_the_table_has_no_customer_or_free_text_column():
    cols = {c.name for c in d.PovSignoff.__table__.columns}
    banned = re.compile(r"customer|company|opp|account|comment|note|email|reason")
    assert not [c for c in cols if banned.search(c)], cols


def test_nothing_covered_is_refused():
    env_id = _env()
    try:
        _record(env_id)
    except pov_signoff.SignoffError as exc:
        assert "nothing" in str(exc)
    else:
        raise AssertionError("signed off an empty checklist")


def test_a_double_click_is_one_signoff():
    env_id = _env()
    _tick(env_id, _CARD_A)
    first, second = _record(env_id), _record(env_id)
    assert first["id"] == second["id"]
    assert len(_summary(env_id)["signoffs"]) == 1


def test_a_signoff_is_a_snapshot_of_what_was_covered():
    env_id = _env()
    _tick(env_id, _CARD_A)
    first = _record(env_id)
    assert first["current"] is True and first["coverage"]["done"] == 1
    _tick(env_id, _CARD_B)
    rows = _summary(env_id)["signoffs"]
    assert rows[0]["current"] is False, "an old sign-off stretched over new coverage"
    assert rows[0]["coverage"]["done"] == 1
    second = _record(env_id)
    assert second["id"] != first["id"] and second["current"] is True


def test_withdrawing_keeps_the_row():
    env_id = _env()
    _tick(env_id, _CARD_A)
    signed = _record(env_id)
    db = d.SessionLocal()
    try:
        out = pov_signoff.revoke(db, db.get(d.PovEnvironment, env_id), signed["id"], by="se2")
    finally:
        db.close()
    assert out["revoked"] and out["revoked_by"] == "se2"
    rows = _summary(env_id)["signoffs"]
    assert len(rows) == 1 and rows[0]["revoked"], "a withdrawn sign-off disappeared"


def test_a_destroyed_pov_can_still_be_signed_off():
    env_id = _env()
    _tick(env_id, _CARD_A)
    db = d.SessionLocal()
    try:
        db.get(d.PovEnvironment, env_id).status = "destroyed"
        db.commit()
    finally:
        db.close()
    assert _record(env_id)["current"] is True


def test_no_accessor_email_reaches_the_summary():
    env_id = _env()
    db = d.SessionLocal()
    try:
        db.add(d.PovAccessor(environment_id=env_id, username="pov_x_abc123",
                             email="jane@prospect-corp.example", source="manual"))
        db.commit()
    finally:
        db.close()
    _tick(env_id, _CARD_A, by="pov_x_abc123", kind=pov_use_cases.KIND_ACCESSOR)
    _record(env_id, by="pov_x_abc123", kind=pov_use_cases.KIND_ACCESSOR)
    blob = json.dumps(_summary(env_id))
    assert "prospect-corp" not in blob, "an accessor's email domain reached the report"
    assert "@" not in blob, "an email address reached the report"


# ── the routes ───────────────────────────────────────────────────────────────

class _Operator:
    username = "se1"
    is_admin = True
    is_effective_admin = True
    effective_permissions_dict = {}
    accessor_env_id = None
    pov_env_ids_list = []
    workgroups_list = []


_CALLER = {"user": _Operator()}


def _db():
    db = d.SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _client():
    app = FastAPI()
    app.include_router(pov_api.router)
    app.include_router(accessor_api.self_router)
    app.dependency_overrides[get_current_user] = lambda: _CALLER["user"]
    app.dependency_overrides[d.get_db] = _db
    return TestClient(app, raise_server_exceptions=False)


def _audits(action):
    db = d.SessionLocal()
    try:
        return db.query(d.AuditLog).filter(d.AuditLog.action == action).count()
    finally:
        db.close()


def test_the_se_routes_sign_and_withdraw_with_an_audit_trail():
    env_id = _env()
    c = _client()
    assert c.post(f"/api/pov/managed/{env_id}/signoff").status_code == 400, \
        "an empty checklist was signed"
    _tick(env_id, _CARD_A)
    before = _audits("pov_signoff")
    res = c.post(f"/api/pov/managed/{env_id}/signoff")
    assert res.status_code == 200, res.text
    assert _audits("pov_signoff") == before + 1
    signoff_id = res.json()["signoff"]["id"]
    assert c.delete(f"/api/pov/managed/{env_id}/signoff/nope").status_code == 404
    res = c.delete(f"/api/pov/managed/{env_id}/signoff/{signoff_id}")
    assert res.status_code == 200 and res.json()["signoff"]["revoked"]
    assert _audits("pov_signoff_withdrawn") >= 1


def test_the_customer_signs_from_their_own_page():
    env_id = _env()
    db = d.SessionLocal()
    try:
        user = d.User(username="pov_y_" + uuid.uuid4().hex[:6], hashed_password="x",
                      is_active=True, accessor_env_id=env_id)
        db.add(user)
        db.add(d.PovAccessor(environment_id=env_id, username=user.username,
                             source="manual"))
        db.commit()
        db.refresh(user)
        db.expunge(user)
    finally:
        db.close()
    _tick(env_id, _CARD_A, by=user.username, kind=pov_use_cases.KIND_ACCESSOR)
    _CALLER["user"] = user
    try:
        c = _client()
        res = c.post("/api/pov/accessor/self/signoff")
        assert res.status_code == 200, res.text
        assert res.json()["signoff"]["signed_by_kind"] == pov_use_cases.KIND_ACCESSOR
        view = c.get("/api/pov/accessor/self")
        assert view.status_code == 200, view.text
        assert view.json()["signoff"]["id"] == res.json()["signoff"]["id"]
    finally:
        _CALLER["user"] = _Operator()


def test_the_customer_route_needs_no_allowlist_change():
    """Under the accessor's one allowed prefix, so nothing in auth.py had to widen."""
    from web_dashboard.api import auth
    assert auth._accessor_may_reach("/api/pov/accessor/self/signoff")


# ── the report page ──────────────────────────────────────────────────────────

def _markup():
    src = open(_REPORT, encoding="utf-8").read()
    src = re.sub(r"\{#.*?#\}", "", src, flags=re.S)
    return re.sub(r"<!--.*?-->", "", src, flags=re.S)


def test_the_report_has_no_input_of_any_kind():
    src = _markup()
    for tag in ("<input", "<textarea", "<select", "contenteditable", "<form"):
        assert tag not in src.lower(), f"the report carries {tag}"


def test_the_report_leaves_the_customer_and_opportunity_as_placeholders():
    src = _markup()
    assert "«Customer name»" in src and "«Opportunity»" in src
    assert "email" not in src.lower()


def test_the_report_renders_people_text_with_x_text_only():
    assert "x-html" not in _markup()


def test_the_report_page_is_routed_behind_the_pov_gate():
    src = open(_MAIN, encoding="utf-8").read()
    i = src.index('@app.get("/pov/{env_id}/report"')
    assert '_feature_gate("pov_environments_enabled")' in src[i:i + 300]


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
