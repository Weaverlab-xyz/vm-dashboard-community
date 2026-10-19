"""A workstream's deploy defaults: what its cloud VMs get unless the deploy form says otherwise.

What these pin:

- the profile is an ALLOWLIST: any member may edit it, so a free-form key would let them
  override bt_client_secret (or any global) for their team; unknown keys are refused;
- an unset key is absent, never False; one workstream's defaults never show up in
  another's; null clears a key, and an empty profile is stored as NULL;
- the PRA jump target resolves explicit → workstream → per-cloud key → global, and the
  opt-in resolves explicit → workstream → on;
- the API lets a member, the creator or an admin read and write it, refuses anyone else,
  and audits every write; deleting a workstream drops its defaults;
- a hand-edited row cannot carry an unknown key or a mistyped value past a read.

Uses a real temp SQLite database. Runs standalone:
    python tests/test_workstream_profile.py
"""
import os
import sys
import tempfile
import uuid

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

_TMPDIR = tempfile.mkdtemp(prefix="workstream-profile-test-")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{os.path.join(_TMPDIR, 'test.db')}")
os.environ.setdefault("JWT_SECRET_KEY", "workstream-profile-test-secret")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from web_dashboard.api import auth  # noqa: E402
from web_dashboard.api import workgroups as api  # noqa: E402
from web_dashboard.api.auth import full_access_map  # noqa: E402
from web_dashboard.database import (AuditLog, Base, SessionLocal, User,  # noqa: E402
                                    Workgroup, engine)
from web_dashboard.services import config_service  # noqa: E402
from web_dashboard.services import vm_pra_service as vps  # noqa: E402
from web_dashboard.services import workgroup_service as ws  # noqa: E402
from web_dashboard.services.workgroup_service import WorkgroupError  # noqa: E402

Base.metadata.create_all(bind=engine)


def _person(name, workgroups=(), admin=False):
    db = SessionLocal()
    try:
        u = User(id=str(uuid.uuid4()), username=name, hashed_password="x", is_admin=admin)
        u.workgroups_list = list(workgroups)
        u.permissions_dict = full_access_map()
        db.add(u)
        db.commit()
        db.refresh(u)
        db.expunge(u)
        return u
    finally:
        db.close()


def _workstream(name):
    db = SessionLocal()
    try:
        db.add(Workgroup(name=name, display_name=name))
        db.commit()
    finally:
        db.close()


ALICE = _person("alice", ["team-a"])
BOB = _person("bob", ["team-b"])
ROOT = _person("root", admin=True)
for _n in ("team-a", "team-b", "team-c", "team-gone"):
    _workstream(_n)


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


# ── The service ───────────────────────────────────────────────────────────────

def _set(name, values):
    db = SessionLocal()
    try:
        return ws.set_profile(db, name, values)
    finally:
        db.close()


def test_unknown_keys_are_refused():
    for bad in ({"bt_client_secret": "x"}, {"pra_enabled": True, "ec2_ssh_key_secret": "s"}):
        try:
            _set("team-c", bad)
            raise AssertionError(f"{bad} should be refused")
        except WorkgroupError as e:
            assert "Not a workstream default" in str(e)
    assert ws.profile("team-c") == {}, "a refused write must write nothing"


def test_values_are_typed_and_checked():
    for bad in ({"pra_enabled": "yes"}, {"entra_join_mode": "aad"}, {"jump_group": "x" * 300}):
        try:
            _set("team-c", bad)
            raise AssertionError(f"{bad} should be refused")
        except WorkgroupError:
            pass
    out = _set("team-c", {"pra_enabled": False, "jump_group": " ops ", "entra_join_mode": ""})
    assert out == {"pra_enabled": False, "jump_group": "ops", "entra_join_mode": ""}
    assert ws.profile("team-c") == out
    _set("team-c", {"pra_enabled": None, "jump_group": None, "entra_join_mode": None})


def test_unset_is_absent_and_null_clears():
    _set("team-c", {"register_in_entitle": True})
    assert ws.profile("team-c")["register_in_entitle"] is True
    _set("team-c", {"register_in_entitle": None, "jump_group": ""})
    assert ws.profile("team-c") == {}
    db = SessionLocal()
    try:
        assert ws.get(db, "team-c").deploy_defaults is None, "an empty profile is stored as NULL"
    finally:
        db.close()


def test_one_workstream_never_sees_anothers():
    _set("team-a", {"enable_rdp": False})
    try:
        assert ws.profile("team-a")["enable_rdp"] is False
        assert ws.profile("team-b") == {}
        assert ws.profile("no-such-team") == {} and ws.profile(None) == {}
    finally:
        _set("team-a", {"enable_rdp": None})


def test_a_hand_edited_row_cannot_smuggle_a_key_or_a_type():
    db = SessionLocal()
    try:
        ws.get(db, "team-c").deploy_defaults = (
            '{"bt_client_secret": "x", "pra_enabled": "no", "jump_group": "g", '
            '"entra_join_mode": "aad"}')
        db.commit()
        assert ws.profile("team-c", db) == {"jump_group": "g"}
        ws.get(db, "team-c").deploy_defaults = "not json"
        db.commit()
        assert ws.profile("team-c", db) == {}
        ws.get(db, "team-c").deploy_defaults = None
        db.commit()
    finally:
        db.close()


# ── Where a VM's jump lands, and whether it gets one ─────────────────────────

def test_jump_target_order_is_explicit_then_workstream_then_cloud_then_global():
    saved = config_service.get
    cfg = {"gcp_bt_jump_group_name": "gcp-group", "bt_jump_group_name": "global-group",
           "bt_jumpoint_name": "global-gw"}
    config_service.get = lambda key, default="", workgroup=None: cfg.get(key, default)
    _set("team-a", {"jump_group": "team-group"})
    try:
        assert vps.resolve_target("gcp", workgroup="team-b") == ("gcp-group", "global-gw")
        assert vps.resolve_target("gcp", workgroup="team-a") == ("team-group", "global-gw")
        assert vps.resolve_target("aws", workgroup="team-a") == ("team-group", "global-gw")
        assert vps.resolve_target("gcp", "mine", workgroup="team-a") == ("mine", "global-gw")
    finally:
        config_service.get = saved
        _set("team-a", {"jump_group": None})


def test_opt_in_is_explicit_then_workstream_then_on():
    _set("team-a", {"pra_enabled": False})
    try:
        assert vps.opted_in(None) is True
        assert vps.opted_in(None, "team-b") is True
        assert vps.opted_in(None, "team-a") is False
        assert vps.opted_in(True, "team-a") is True
        assert vps.opted_in(False, "team-b") is False
    finally:
        _set("team-a", {"pra_enabled": None})


def test_every_runner_hands_the_workstream_to_both_decisions():
    import re
    for cloud in ("aws", "azure", "gcp", "oci"):
        with open(os.path.join(_ROOT, "web_dashboard", "services", f"{cloud}_vm_service.py"),
                  encoding="utf-8") as fh:
            src = fh.read()
        calls = [m.start() for m in re.finditer(r"\b(opted_in|resolve_target)\(", src)]
        assert len(calls) >= 2, cloud
        for i in calls:
            assert "workgroup" in src[i:i + 220], (
                f"{cloud}: {src[i:i + 60]!r} does not pass the workstream")


# ── The API ───────────────────────────────────────────────────────────────────

def _audited(action):
    db = SessionLocal()
    try:
        return db.query(AuditLog).filter(AuditLog.action == action).count()
    finally:
        db.close()


def test_a_member_reads_and_writes_their_own_and_it_is_audited():
    before = _audited("workstream_profile_update")
    c = _client(ALICE)
    r = c.put("/api/workgroups/team-a/profile", json={"pra_enabled": False})
    assert r.status_code == 200, r.text
    assert r.json()["pra_enabled"] is False and r.json()["jump_group"] is None
    assert c.get("/api/workgroups/team-a/profile").json()["pra_enabled"] is False
    # A field left out is unchanged; null clears it.
    c.put("/api/workgroups/team-a/profile", json={"enable_rdp": True})
    assert c.get("/api/workgroups/team-a/profile").json()["pra_enabled"] is False
    c.put("/api/workgroups/team-a/profile", json={"pra_enabled": None, "enable_rdp": None})
    assert c.get("/api/workgroups/team-a/profile").json()["pra_enabled"] is None
    assert _audited("workstream_profile_update") == before + 3


def test_anyone_else_is_refused_and_an_admin_is_not():
    c = _client(BOB)
    assert c.get("/api/workgroups/team-a/profile").status_code == 403
    assert c.put("/api/workgroups/team-a/profile", json={"pra_enabled": True}).status_code == 403
    assert "pra_enabled" not in ws.profile("team-a")
    assert _client(ROOT).put("/api/workgroups/team-a/profile",
                             json={"jump_group": "root-set"}).status_code == 200
    _set("team-a", {"jump_group": None})


def test_a_bad_value_is_a_400_not_a_write():
    r = _client(ALICE).put("/api/workgroups/team-a/profile", json={"entra_join_mode": "aad"})
    assert r.status_code == 400
    assert "entra_join_mode" not in ws.profile("team-a")


def test_deleting_a_workstream_drops_its_defaults():
    _set("team-gone", {"pra_enabled": False, "jump_group": "g"})
    db = SessionLocal()
    try:
        ws.delete(db, "team-gone")
    finally:
        db.close()
    assert ws.profile("team-gone") == {}


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
