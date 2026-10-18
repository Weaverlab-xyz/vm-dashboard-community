"""``workgroups:write`` manages your own workstreams, not everyone's.

The scope is grantable (and part of "Full access"), and it let its holder add anybody --
themselves included -- to ANY workstream, then see, power and destroy everything tagged
there. It also let them clear a workstream's required change window, an approval control.
Nothing was audited. Now:

  * editing a workstream or changing who is in it needs an administrator, or a member of
    that workstream, or the person who created it;
  * the change-window fields also need ``change_windows:write``;
  * create, update and every membership change are audited.

Runs standalone:
    python tests/test_workgroups_write_is_scoped.py
"""
import os
import sys
import tempfile
import uuid

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

_TMPDIR = tempfile.mkdtemp(prefix="workgroups-write-test-")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{os.path.join(_TMPDIR, 'test.db')}")
os.environ.setdefault("JWT_SECRET_KEY", "workgroups-write-test-secret")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from web_dashboard.api import auth  # noqa: E402
from web_dashboard.api import workgroups as api  # noqa: E402
from web_dashboard.api.auth import full_access_map  # noqa: E402
from web_dashboard.database import (AuditLog, Base, SessionLocal, User,  # noqa: E402
                                    Workgroup, engine)

Base.metadata.create_all(bind=engine)


def _person(name, workgroups=(), admin=False, extra=None):
    db = SessionLocal()
    try:
        u = User(id=str(uuid.uuid4()), username=name, hashed_password="x", is_admin=admin)
        u.workgroups_list = list(workgroups)
        perms = full_access_map()
        perms.update(extra or {})
        u.permissions_dict = perms
        db.add(u)
        db.commit()
        db.refresh(u)
        db.expunge(u)
        return u
    finally:
        db.close()


def _workstream(name, created_by=None):
    db = SessionLocal()
    try:
        db.add(Workgroup(name=name, display_name=name, created_by_user_id=created_by))
        db.commit()
    finally:
        db.close()


ALICE = _person("alice", ["team-a"])                    # Full access: workgroups:write
BOB = _person("bob", ["team-b"])
CAROL = _person("carol", ["team-a"], extra={"change_windows": ["write"]})
ROOT = _person("root", admin=True)
DAVE = _person("dave")                                  # only ever moved by the admin
_workstream("team-a")
_workstream("team-b")


def _client(user):
    app = FastAPI()
    app.include_router(api.router)

    def _me():
        db = SessionLocal()
        try:
            return db.query(User).filter(User.id == user.id).first()
        finally:
            db.close()
    app.dependency_overrides[auth.get_current_user] = _me
    return TestClient(app)


def _audited(action):
    db = SessionLocal()
    try:
        return db.query(AuditLog).filter(AuditLog.action == action).count()
    finally:
        db.close()


def test_nobody_adds_themselves_to_someone_elses_workstream():
    r = _client(ALICE).post("/api/workgroups/team-b/members/alice")
    assert r.status_code == 403, r.text
    db = SessionLocal()
    try:
        assert "team-b" not in db.query(User).filter(User.username == "alice").one().workgroups_list
    finally:
        db.close()


def test_a_member_manages_their_own_workstream_and_it_is_audited():
    before = _audited("workstream_member_add")
    assert _client(ALICE).post("/api/workgroups/team-a/members/bob").status_code == 204
    assert _audited("workstream_member_add") == before + 1
    assert _client(ALICE).delete("/api/workgroups/team-a/members/bob").status_code == 204
    assert _audited("workstream_member_remove") >= 1


def test_editing_another_workstream_is_refused():
    r = _client(ALICE).patch("/api/workgroups/team-b", json={"description": "mine now"})
    assert r.status_code == 403, r.text


def test_the_creator_may_manage_what_they_created():
    _workstream("alices-new", created_by=ALICE.id)
    assert _client(ALICE).post("/api/workgroups/alices-new/members/bob").status_code == 204


def test_the_change_window_gate_needs_change_windows_write():
    r = _client(ALICE).patch("/api/workgroups/team-a", json={"require_change_window": False})
    assert r.status_code == 403 and "change_windows:write" in r.text, r.text
    ok = _client(CAROL).patch("/api/workgroups/team-a", json={"require_change_window": False})
    assert ok.status_code == 200, ok.text
    assert _audited("workstream_update") >= 1


def test_an_admin_still_manages_any_workstream():
    assert _client(ROOT).post("/api/workgroups/team-b/members/dave").status_code == 204
    assert _client(ROOT).patch("/api/workgroups/team-b",
                               json={"require_change_window": False}).status_code == 200


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            import traceback
            traceback.print_exc()
            print(f"FAIL {fn.__name__}: {exc}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
