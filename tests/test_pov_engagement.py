"""Customer engagement on a POV: ``services/pov_engagement`` and the login stamp behind it.

What is pinned, because each would fail quietly:

  * **A login used is not a login issued.** The summary already refused to claim the
    customer "took part" off a login existing; the stamp is what makes "they signed in"
    a fact, and it is only ever written at sign-in, never per request.
  * **It survives revocation.** Revoking an accessor deletes its user row, so the stamp
    is ALSO kept on the accessor row -- the record a finished POV still has.
  * **A stamp that fails never fails the login.**
  * **The list stays three queries** whatever its length: the POV list is the endpoint
    every SE loads, and a per-row query there scales with the inventory.

Runs under pytest, or standalone:
    python tests/test_pov_engagement.py
"""
import os
import sys
import tempfile
import uuid
from datetime import datetime, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="pov-engagement-test-"), "test.db")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_TMPDB}")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-pov-engagement")

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy import event
except ImportError:  # pragma: no cover
    print("SKIP: fastapi not installed")
    sys.exit(0)

from web_dashboard import database as d  # noqa: E402

d.Base.metadata.create_all(bind=d.engine)

from web_dashboard.api import auth as auth_api  # noqa: E402
from web_dashboard.api import pov as pov_api  # noqa: E402
from web_dashboard.services import pov_engagement as eng  # noqa: E402
from web_dashboard.services import pov_use_cases  # noqa: E402
from _pov_api_src import pov_api_source

NOW = datetime(2026, 10, 20, 12, 0, 0)


# ── the projection ───────────────────────────────────────────────────────────

def _p(**kw):
    base = dict(issued=0, live=0, signed_in=0, last_login=None, customer_ticks=0,
                last_tick=None, now=NOW)
    base.update(kw)
    return eng.project(**base)


def test_the_states_rank_by_what_they_prove():
    assert _p()["state"] == eng.NO_LOGIN
    assert _p(issued=2, live=2)["state"] == eng.NOT_SIGNED_IN
    assert _p(issued=2, signed_in=1, last_login=NOW)["state"] == eng.SIGNED_IN
    assert _p(issued=1, signed_in=1, last_login=NOW, customer_ticks=3,
              last_tick=NOW)["state"] == eng.ACTIVE


def test_idle_days_counts_from_the_latest_customer_action():
    out = _p(issued=1, signed_in=1, last_login=NOW - timedelta(days=9),
             customer_ticks=1, last_tick=NOW - timedelta(days=4))
    assert out["idle_days"] == 4
    assert _p(issued=1)["idle_days"] is None, "never seen is not zero days idle"


# ── the stamp ────────────────────────────────────────────────────────────────

def _env():
    db = d.SessionLocal()
    try:
        env = d.PovEnvironment(platform="skytap", name="pov-" + uuid.uuid4().hex[:8],
                               status="active", created_by="se1")
        db.add(env)
        db.commit()
        return env.id
    finally:
        db.close()


def _accessor(env_id, password="correct-horse"):
    username = "pov_t_" + uuid.uuid4().hex[:6]
    db = d.SessionLocal()
    try:
        db.add(d.User(username=username, hashed_password=d.get_password_hash(password),
                      is_active=True, auth_provider="local", accessor_env_id=env_id))
        db.add(d.PovAccessor(environment_id=env_id, username=username, source="manual"))
        db.commit()
    finally:
        db.close()
    return username


def _login_client():
    app = FastAPI()
    app.include_router(auth_api.router)

    def _get():
        db = d.SessionLocal()
        try:
            yield db
        finally:
            db.close()
    app.dependency_overrides[d.get_db] = _get
    return TestClient(app)


def test_a_password_login_stamps_the_user_and_the_accessor_row():
    env_id = _env()
    username = _accessor(env_id)
    res = _login_client().post("/api/auth/login",
                               data={"username": username, "password": "correct-horse"})
    assert res.status_code == 200, res.text
    db = d.SessionLocal()
    try:
        user = db.query(d.User).filter(d.User.username == username).one()
        row = db.query(d.PovAccessor).filter(d.PovAccessor.username == username).one()
        assert user.last_login_at is not None
        assert row.last_login_at is not None, "the accessor row was not stamped"
    finally:
        db.close()


def test_a_failed_login_stamps_nothing():
    env_id = _env()
    username = _accessor(env_id)
    res = _login_client().post("/api/auth/login",
                               data={"username": username, "password": "wrong"})
    assert res.status_code == 401
    db = d.SessionLocal()
    try:
        assert db.query(d.User).filter(d.User.username == username).one().last_login_at is None
    finally:
        db.close()


def test_a_stamp_that_fails_never_fails_the_login():
    class _Broken:
        def commit(self):
            raise RuntimeError("database went away")

        def rollback(self):
            pass

        def query(self, *_a):
            raise RuntimeError("database went away")

    user = d.User(username="x", hashed_password="x")
    auth_api._stamp_login(_Broken(), user)   # must not raise


def test_every_session_issuing_path_stamps():
    src = open(auth_api.__file__, encoding="utf-8").read()
    issued = src.count('create_access_token({"sub": user.username})')
    stamped = src.count("_stamp_login(db, user)\n    token = create_access_token")
    assert issued == stamped == 3, (issued, stamped)


# ── describe_many ────────────────────────────────────────────────────────────

def test_engagement_survives_the_accessor_being_revoked():
    env_id = _env()
    username = _accessor(env_id)
    db = d.SessionLocal()
    try:
        row = db.query(d.PovAccessor).filter(d.PovAccessor.username == username).one()
        row.last_login_at = NOW
        row.revoked_at = NOW + timedelta(hours=1)
        db.query(d.User).filter(d.User.username == username).delete()
        db.commit()
        out = eng.describe(db, db.get(d.PovEnvironment, env_id))
    finally:
        db.close()
    assert out["logins_used"] == 1 and out["logins_live"] == 0
    assert out["state"] == eng.SIGNED_IN


def test_customer_ticks_make_it_active_and_se_ticks_do_not():
    env_id = _env()
    db = d.SessionLocal()
    try:
        env = db.get(d.PovEnvironment, env_id)
        env.pra_tenant_id, env.ps_tenant_id = "t-pra", "t-ps"
        db.commit()
        pov_use_cases.set_state(db, env, "pov-devops-no-inbound", state="done", by="se1")
        assert eng.describe(db, env)["customer_ticks"] == 0
        pov_use_cases.set_state(db, env, "pov-hypervisor-rotate-root", state="done",
                                by="pov_t_x", by_kind=pov_use_cases.KIND_ACCESSOR)
        out = eng.describe(db, env)
    finally:
        db.close()
    assert out["customer_ticks"] == 1 and out["state"] == eng.ACTIVE


def test_a_page_of_povs_costs_a_fixed_number_of_queries():
    ids = [_env() for _ in range(6)]
    for env_id in ids:
        _accessor(env_id)
    db = d.SessionLocal()
    try:
        envs = db.query(d.PovEnvironment).filter(d.PovEnvironment.id.in_(ids)).all()
        seen = []

        def count(*_a, **_k):
            seen.append(1)
        event.listen(d.engine, "before_cursor_execute", count)
        try:
            out = eng.describe_many(db, envs)
        finally:
            event.remove(d.engine, "before_cursor_execute", count)
    finally:
        db.close()
    assert len(out) == 6
    assert len(seen) <= 3, f"{len(seen)} queries for six POVs"


def test_the_list_and_detail_endpoints_carry_it():
    src = pov_api_source()
    body = src.split("def list_managed(", 1)[1].split("\n@router", 1)[0]
    assert "pov_engagement.describe_many(db, rows)" in body
    detail = src.split("def get_managed(", 1)[1].split("\n@router", 1)[0]
    assert "pov_engagement.describe(db, env)" in detail


def test_both_columns_have_their_retrofit():
    from web_dashboard.schema_legacy import STATEMENTS
    assert "ALTER TABLE users ADD COLUMN last_login_at TIMESTAMP" in STATEMENTS
    assert "ALTER TABLE pov_accessors ADD COLUMN last_login_at TIMESTAMP" in STATEMENTS


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
