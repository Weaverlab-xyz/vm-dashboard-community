"""Losing administrator leaves a user with nothing until something grants access.

``has_permission`` reads a user whose permissions are all empty as UNRESTRICTED -- every
section, though never the admin-only pages -- for backward compatibility with accounts
that predate the permission columns. The Admin flag bypasses the per-user map, so the
form saved none for an administrator, which stored NULL. Unticking Admin then opened the
grid with "Full access (unrestricted)" pre-ticked, and saving left the ex-administrator
with every section in the dashboard.

The rule now, on the server as well as in the form:

  * Losing administrator (the flag cleared, or the Administrator role removed) resets
    the user's own map to NOTHING GRANTED -- every scope, no levels -- unless the same
    request sends permissions. A role assigned in the same request still grants.
  * An administrator is created with nothing granted underneath, so there is no NULL
    waiting to become "unrestricted".
  * The form resets the grid to nothing ticked, with "Full access" unticked.

Runs under pytest, or standalone:  python tests/test_admin_demotion.py
"""
import os
import re
import sys
import tempfile
import uuid

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
_TMPDIR = tempfile.mkdtemp(prefix="admin-demotion-test-")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{os.path.join(_TMPDIR, 'test.db')}")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-admin-demotion")

from web_dashboard.api import users as users_api  # noqa: E402
from web_dashboard.api.auth import PERMISSION_SCOPES, has_permission  # noqa: E402
from web_dashboard.database import Base, SessionLocal, User, engine  # noqa: E402
from web_dashboard.services import role_service  # noqa: E402

Base.metadata.create_all(bind=engine)


def _db():
    db = SessionLocal()
    role_service.seed_builtins(db)
    return db


def _root(db):
    u = User(id=str(uuid.uuid4()), username=f"root-{uuid.uuid4().hex[:6]}",
             hashed_password="x", is_active=True, is_admin=True)
    db.add(u)
    db.commit()
    return u


def _legacy_admin(db, perms=None):
    """An administrator as the old form stored one: flag set, map NULL (or stale)."""
    u = User(id=str(uuid.uuid4()), username=f"adm-{uuid.uuid4().hex[:6]}",
             hashed_password="x", is_active=True, is_admin=True)
    if perms is not None:
        u.permissions_dict = perms
    db.add(u)
    db.commit()
    return u


def _patch(db, target, root, **fields):
    body = users_api.UserUpdateRequest(**fields)
    users_api.update_user(target.id, body, admin=root, db=db)
    db.expire_all()
    return db.query(User).filter(User.id == target.id).one()


def _grants_nothing(u):
    return not any(has_permission(u, s, lv) for s in PERMISSION_SCOPES
                   for lv in ("read", "write", "delete", "use"))


def test_unticking_admin_leaves_nothing_granted():
    db = _db()
    root, u = _root(db), _legacy_admin(db)
    u = _patch(db, u, root, is_admin=False)
    assert not u.is_effective_admin
    assert u.permissions_dict == {s: [] for s in PERMISSION_SCOPES}, u.permissions_dict
    assert _grants_nothing(u), "a demoted admin can still reach a section"


def test_a_stale_map_under_the_admin_does_not_come_back():
    """Grants nobody has looked at since the flag started bypassing them."""
    db = _db()
    root, u = _root(db), _legacy_admin(db, perms={"aws": ["read", "write", "delete"]})
    u = _patch(db, u, root, is_admin=False)
    assert not has_permission(u, "aws", "delete")


def test_permissions_sent_with_the_demotion_are_honoured():
    db = _db()
    root, u = _root(db), _legacy_admin(db)
    u = _patch(db, u, root, is_admin=False, permissions={"vms": ["read"]})
    assert has_permission(u, "vms", "read") and not has_permission(u, "aws", "read")


def test_a_role_assigned_with_the_demotion_still_grants():
    db = _db()
    root, u = _root(db), _legacy_admin(db)
    ro = role_service.get_by_slug(db, "read-only")
    u = _patch(db, u, root, is_admin=False, role_id=ro.id)
    assert has_permission(u, "aws", "read"), "the role's grants were lost"
    assert not has_permission(u, "aws", "write")


def test_removing_the_administrator_role_is_a_demotion_too():
    db = _db()
    root = _root(db)
    u = User(id=str(uuid.uuid4()), username=f"roleadm-{uuid.uuid4().hex[:6]}",
             hashed_password="x", is_active=True, is_admin=False)
    db.add(u)
    role_service.apply_role_to_user(db, u, role_service.get_by_slug(db, "administrator"))
    db.commit()
    assert u.is_effective_admin
    u = _patch(db, u, root, role_id="")
    assert not u.is_effective_admin and _grants_nothing(u)


def test_an_unrelated_edit_does_not_touch_a_non_admins_map():
    db = _db()
    root = _root(db)
    u = User(id=str(uuid.uuid4()), username=f"op-{uuid.uuid4().hex[:6]}",
             hashed_password="x", is_active=True, is_admin=False)
    u.permissions_dict = {"vms": ["read"]}
    db.add(u)
    db.commit()
    u = _patch(db, u, root, full_name="Renamed")
    assert u.permissions_dict == {"vms": ["read"]}


def test_an_admin_is_created_with_nothing_granted_underneath():
    db = _db()
    root = _root(db)
    body = users_api.UserCreateRequest(username=f"new-{uuid.uuid4().hex[:6]}",
                                       password="Sup3r-secret-pw!", is_admin=True)
    out = users_api.create_user(body, _admin=root, db=db)
    u = db.query(User).filter(User.id == out.id).one()
    assert u.is_admin and u.permissions_dict == {s: [] for s in PERMISSION_SCOPES}
    u.is_admin = False                      # even a demotion that bypassed the API
    assert _grants_nothing(u)


def test_the_form_resets_the_grid_on_demotion():
    """The server rule applies only when a request sends no permissions, and the form
    always sends them -- so the form has to send NOTHING GRANTED, not a pre-ticked
    "Full access" left over from an admin who had no stored map."""
    src = open(os.path.join(_ROOT, "web_dashboard", "templates", "rbac", "_users.html"),
               encoding="utf-8").read()
    assert re.search(r'x-model="form\.is_admin"\s+@change="onAdminToggled\(\)"', src)
    assert re.search(r'x-model="form\.role_id"\s+@change="onRoleChanged\(\)"', src)
    reset = src[src.index("resetToNothingGranted() {"):][:200]
    assert "permissionsUnrestricted = false" in reset and "permissions = {}" in reset


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {e!r}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
