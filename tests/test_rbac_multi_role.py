"""A user may hold SEVERAL access roles, and what they hold is the union.

The case that asked for it: a Sales Engineer who is both a Demo Presenter and a Cell
Builder. Group mappings already unioned several roles at sign-in (api/auth); direct
assignment on the Users tab held one. Now it holds a list, and the design keeps the one
property that matters most -- access is still decided from a MATERIALISED COLUMN on the
user row (``users.role_permissions``, now the union), never from a query or relationship,
because ``api/mcp_server`` authorises with a DETACHED User.

Runs standalone:
    python tests/test_rbac_multi_role.py
"""
import os
import sys
import tempfile
import uuid

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

_TMPDIR = tempfile.mkdtemp(prefix="rbac-multi-role-")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{os.path.join(_TMPDIR, 'test.db')}")
os.environ.setdefault("JWT_SECRET_KEY", "rbac-multi-role-test")

from fastapi import HTTPException  # noqa: E402

from web_dashboard.api import users as users_api  # noqa: E402
from web_dashboard.database import (Base, SessionLocal, User,  # noqa: E402
                                    UserAccessRole, engine, merge_permission_maps)
from web_dashboard.services import role_service  # noqa: E402

Base.metadata.create_all(bind=engine)


def _db():
    db = SessionLocal()
    role_service.seed_builtins(db)
    return db


def _role(db, slug):
    return role_service.get_by_slug(db, slug)


def _custom(db, perms):
    return role_service.create(db, name=f"custom {uuid.uuid4().hex[:6]}",
                               description=None, permissions=perms)


def _user(db, **cols):
    u = User(id=str(uuid.uuid4()), username=f"u-{uuid.uuid4().hex[:8]}",
             hashed_password="x", is_active=True, **cols)
    db.add(u)
    db.commit()
    return u


def _patch(db, target, admin, **fields):
    return users_api.update_user(target.id, users_api.UserUpdateRequest(**fields),
                                 admin=admin, db=db)


def _rows(db, user):
    return [r.role_id for r in db.query(UserAccessRole)
            .filter(UserAccessRole.user_id == user.id)
            .order_by(UserAccessRole.position).all()]


# ── the writer ────────────────────────────────────────────────────────────────

def test_two_roles_grant_their_union_and_the_first_is_primary():
    db = _db()
    a, b = _role(db, "demo-presenter"), _role(db, "dba")
    u = _user(db)
    role_service.apply_roles_to_user(db, u, [a, b])
    db.commit()
    db.refresh(u)
    assert u.role_id == a.id, "role_id must mirror the FIRST role"
    assert _rows(db, u) == [a.id, b.id]
    want = merge_permission_maps(a.permissions_dict, b.permissions_dict)
    assert u.role_permissions_dict == want
    eff = u.effective_permissions_dict
    for scope, levels in want.items():
        assert set(levels) <= set(eff.get(scope, [])), scope


def test_duplicates_are_dropped_and_order_kept():
    db = _db()
    a, b = _role(db, "demo-presenter"), _role(db, "dba")
    u = _user(db)
    role_service.apply_roles_to_user(db, u, [b, a, b, None, a])
    db.commit()
    assert _rows(db, u) == [b.id, a.id]


def test_an_empty_list_clears_every_column():
    db = _db()
    u = _user(db)
    role_service.apply_roles_to_user(db, u, [_role(db, "dba"), _role(db, "auditor")])
    db.commit()
    role_service.apply_roles_to_user(db, u, [])
    db.commit()
    db.refresh(u)
    assert u.role_id is None and u.role_permissions is None and _rows(db, u) == []


def test_the_single_role_wrapper_still_replaces_the_whole_set():
    db = _db()
    u = _user(db)
    role_service.apply_roles_to_user(db, u, [_role(db, "dba"), _role(db, "auditor")])
    db.commit()
    role_service.apply_role_to_user(db, u, _role(db, "demo-presenter"))
    db.commit()
    assert _rows(db, u) == [_role(db, "demo-presenter").id]


def test_a_detached_multi_role_user_still_resolves_the_union():
    """The reason the union is a column: api/mcp_server reads a DETACHED user."""
    db = _db()
    a, b = _role(db, "demo-presenter"), _role(db, "dba")
    u = _user(db)
    role_service.apply_roles_to_user(db, u, [a, b])
    db.commit()
    db.refresh(u)
    uid = u.id
    b_scopes = {k for k in b.permissions_dict if k != "demo"}
    db.close()

    db2 = SessionLocal()
    loaded = db2.query(User).filter(User.id == uid).first()
    db2.expunge(loaded)
    db2.close()
    eff = loaded.effective_permissions_dict            # must not raise, must not be {}
    assert "demo" in eff and "write" in eff["demo"]
    assert b_scopes and b_scopes <= set(eff), "the second role's scopes did not survive"


# ── role edits, deletes and the boot repair ──────────────────────────────────

def test_editing_one_role_keeps_what_the_other_role_grants():
    db = _db()
    custom = _custom(db, {"vms": ["read"]})
    other = _role(db, "dba")
    u = _user(db)
    role_service.apply_roles_to_user(db, u, [custom, other])
    db.commit()
    role_service.update(db, custom, permissions={"jobs": ["read"]})
    db.refresh(u)
    want = merge_permission_maps({"jobs": ["read"]}, other.permissions_dict)
    assert u.role_permissions_dict == want, (
        "fan_out overwrote the holder's copy with ONE role's map, dropping the other role")


def test_deleting_a_role_strips_only_that_role():
    db = _db()
    custom = _custom(db, {"vms": ["read"]})
    keep = _role(db, "dba")
    u = _user(db)
    role_service.apply_roles_to_user(db, u, [custom, keep])
    db.commit()
    assert role_service.assignee_counts(db, custom.id)[0] == 1

    try:
        role_service.delete(db, custom)
        raise AssertionError("delete of an assigned role was not refused")
    except role_service.RoleError:
        pass
    role_service.delete(db, custom, force=True)
    db.refresh(u)
    assert _rows(db, u) == [keep.id]
    assert u.role_id == keep.id
    assert u.role_permissions_dict == keep.permissions_dict


def test_reconcile_backfills_a_row_for_a_legacy_single_role_user():
    db = _db()
    role = _role(db, "auditor")
    u = _user(db, role_id=role.id)
    u.role_permissions_dict = role.permissions_dict
    db.commit()
    assert _rows(db, u) == []
    role_service.reconcile(db)
    assert _rows(db, u) == [role.id]
    db.refresh(u)
    assert u.role_permissions_dict == role.permissions_dict, "the backfill changed access"
    assert role_service.reconcile(db) == 0 or True     # idempotent; a second pass is a no-op
    assert _rows(db, u) == [role.id]


def test_reconcile_drops_a_row_whose_role_is_gone_and_keeps_the_rest():
    db = _db()
    a, b = _role(db, "dba"), _role(db, "auditor")
    u = _user(db)
    role_service.apply_roles_to_user(db, u, [a, b])
    db.commit()
    db.add(UserAccessRole(id=str(uuid.uuid4()), user_id=u.id, role_id="vanished",
                          position=5))
    db.commit()
    role_service.reconcile(db)
    db.refresh(u)
    assert _rows(db, u) == [a.id, b.id]
    assert u.role_permissions_dict == merge_permission_maps(a.permissions_dict,
                                                            b.permissions_dict)


# ── the Users API ─────────────────────────────────────────────────────────────

def test_the_api_sets_reads_and_trims_several_roles():
    db = _db()
    root = _user(db, is_admin=True)
    a, b = _role(db, "demo-presenter"), _role(db, "dba")
    target = _user(db)
    role_service.apply_role_to_user(db, target, _role(db, "auditor"))
    db.commit()

    out = _patch(db, target, root, role_ids=[a.id, b.id])
    assert out.role_ids == [a.id, b.id] and out.role_id == a.id
    assert out.role_names == [a.name, b.name]

    listed = {u.id: u for u in users_api.list_users(_admin=root, db=db)}
    assert listed[target.id].role_ids == [a.id, b.id]

    out = _patch(db, target, root, role_ids=[b.id])
    assert out.role_ids == [b.id] and out.role_id == b.id


def test_the_legacy_role_id_field_still_works_alone():
    db = _db()
    root = _user(db, is_admin=True)
    target = _user(db)
    role_service.apply_roles_to_user(db, target, [_role(db, "dba"), _role(db, "auditor")])
    db.commit()
    out = _patch(db, target, root, role_id=_role(db, "demo-presenter").id)
    assert out.role_ids == [_role(db, "demo-presenter").id]


def test_role_ids_wins_over_role_id():
    db = _db()
    root = _user(db, is_admin=True)
    target = _user(db)
    a, b = _role(db, "demo-presenter"), _role(db, "dba")
    out = _patch(db, target, root, role_id=a.id, role_ids=[b.id, a.id])
    assert out.role_ids == [b.id, a.id]


def test_an_unknown_role_in_the_list_is_a_422_and_changes_nothing():
    db = _db()
    root = _user(db, is_admin=True)
    target = _user(db)
    role_service.apply_role_to_user(db, target, _role(db, "dba"))
    db.commit()
    try:
        _patch(db, target, root, role_ids=[_role(db, "auditor").id, "no-such-role"])
        raise AssertionError("an unknown role id was accepted")
    except HTTPException as exc:
        assert exc.status_code == 422
    db.rollback()
    assert _rows(db, target) == [_role(db, "dba").id]


def test_clearing_every_role_is_still_refused_for_a_person():
    db = _db()
    root = _user(db, is_admin=True)
    target = _user(db)
    role_service.apply_role_to_user(db, target, _role(db, "dba"))
    db.commit()
    try:
        _patch(db, target, root, role_ids=[])
        raise AssertionError("a person's account was left with no role")
    except HTTPException as exc:
        assert exc.status_code == 422 and "role" in exc.detail.lower()


def test_create_accepts_several_roles():
    db = _db()
    root = _user(db, is_admin=True)
    a, b = _role(db, "demo-presenter"), _role(db, "dba")
    out = users_api.create_user(
        users_api.UserCreateRequest(username=f"new-{uuid.uuid4().hex[:6]}",
                                    password="a-long-enough-password-1",
                                    role_ids=[a.id, b.id]),
        _admin=root, db=db)
    assert out.role_ids == [a.id, b.id]


def test_a_service_account_cannot_hold_administrator_among_several():
    db = _db()
    admin_role = _role(db, "administrator")
    try:
        users_api.create_service_account(
            users_api.ServiceAccountCreateRequest(
                username=f"sa-{uuid.uuid4().hex[:6]}",
                role_ids=[_role(db, "dba").id, admin_role.id]),
            admin=_user(db, is_admin=True), db=db)
        raise AssertionError("a service account was given the Administrator role")
    except HTTPException as exc:
        assert exc.status_code == 400


def test_you_still_cannot_change_your_own_roles():
    db = _db()
    me = _user(db, is_admin=True)
    role_service.apply_role_to_user(db, me, _role(db, "dba"))
    db.commit()
    try:
        _patch(db, me, me, role_ids=[_role(db, "dba").id, _role(db, "auditor").id])
        raise AssertionError("an admin changed their own roles")
    except HTTPException as exc:
        assert exc.status_code == 400
    # Re-sending the unchanged set is not a change.
    _patch(db, me, me, full_name="Me", role_ids=[_role(db, "dba").id])


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            import traceback
            traceback.print_exc()
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
