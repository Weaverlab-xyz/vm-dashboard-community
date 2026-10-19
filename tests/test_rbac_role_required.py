"""Every group mapping and every person's account holds a role.

The permission grid is an addition ON TOP of a role, not a substitute for one: grid-only
access was a second, unnamed permission set no role edit could reach. So:

  * a group mapping cannot be created, or saved again, without a role;
  * a person's account cannot be created without one, and any edit to a row that has none
    must choose one -- rows saved before the rule keep working until they are edited;
  * exempt: the Admin flag (itself the whole grant), service accounts (workloads),
    accounts single sign-on provisioned (their mapping's role rebuilds their access at
    every sign-in), your own row (you cannot change your own role), and edits that only
    operate on an account (activate, password, the SSO switch).

Runs standalone:
    python tests/test_rbac_role_required.py
"""
import os
import sys
import tempfile
import uuid

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

_TMPDIR = tempfile.mkdtemp(prefix="rbac-role-required-")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{os.path.join(_TMPDIR, 'test.db')}")
os.environ.setdefault("JWT_SECRET_KEY", "rbac-role-required-test")

from fastapi import HTTPException  # noqa: E402

from web_dashboard.api import groups as groups_api  # noqa: E402
from web_dashboard.api import users as users_api  # noqa: E402
from web_dashboard.database import (Base, OAuthGroupMapping, SessionLocal,  # noqa: E402
                                    User, Workgroup, engine)
from web_dashboard.services import role_service  # noqa: E402

Base.metadata.create_all(bind=engine)


def _db():
    db = SessionLocal()
    role_service.seed_builtins(db)
    if not db.query(Workgroup).filter(Workgroup.name == "default").first():
        db.add(Workgroup(id=str(uuid.uuid4()), name="default", display_name="Default"))
        db.commit()
    return db


def _ro(db):
    return role_service.get_by_slug(db, "read-only")


def _user(db, **cols):
    u = User(id=str(uuid.uuid4()), username=f"u-{uuid.uuid4().hex[:8]}",
             hashed_password="x", is_active=True, **cols)
    db.add(u)
    db.commit()
    return u


def _root(db):
    return _user(db, is_admin=True)


def _refused(fn):
    try:
        fn()
    except HTTPException as exc:
        assert exc.status_code == 422, exc.status_code
        assert "role" in exc.detail.lower(), exc.detail
        return True
    return False


def _patch(db, target, admin, **fields):
    return users_api.update_user(target.id, users_api.UserUpdateRequest(**fields),
                                 admin=admin, db=db)


# ── group mappings ────────────────────────────────────────────────────────────

def test_a_mapping_cannot_be_created_without_a_role():
    db = _db()
    for role_id in (None, "", "  "):
        body = groups_api.GroupMappingCreate(entra_group_id=f"g-{uuid.uuid4().hex[:6]}",
                                             display_name="x", workgroup="default",
                                             role_id=role_id)
        assert _refused(lambda: groups_api.create_group_mapping(body, db=db)), role_id
    ok = groups_api.create_group_mapping(groups_api.GroupMappingCreate(
        entra_group_id=f"g-{uuid.uuid4().hex[:6]}", display_name="ok", workgroup="default",
        role_id=_ro(db).id), db=db)
    assert ok.role_id == _ro(db).id


def test_a_mappings_role_cannot_be_cleared():
    db = _db()
    m = groups_api.create_group_mapping(groups_api.GroupMappingCreate(
        entra_group_id=f"g-{uuid.uuid4().hex[:6]}", display_name="m", workgroup="default",
        role_id=_ro(db).id), db=db)
    assert _refused(lambda: groups_api.update_group_mapping(
        m.id, groups_api.GroupMappingUpdate(role_id=""), db=db))
    db.expire_all()
    assert db.get(OAuthGroupMapping, m.id).role_id == _ro(db).id


def test_a_legacy_mapping_keeps_working_but_any_edit_needs_a_role():
    db = _db()
    legacy = OAuthGroupMapping(id=str(uuid.uuid4()), entra_group_id=f"g-{uuid.uuid4().hex[:6]}",
                               display_name="old", workgroup="default")
    db.add(legacy)
    db.commit()
    assert _refused(lambda: groups_api.update_group_mapping(
        legacy.id, groups_api.GroupMappingUpdate(display_name="renamed"), db=db))
    db.expire_all()
    assert db.get(OAuthGroupMapping, legacy.id).display_name == "old", (
        "the refused edit was still written")
    out = groups_api.update_group_mapping(
        legacy.id, groups_api.GroupMappingUpdate(display_name="renamed", role_id=_ro(db).id),
        db=db)
    assert out.display_name == "renamed" and out.role_id == _ro(db).id


# ── creating users ────────────────────────────────────────────────────────────

def _create(db, root, **fields):
    body = users_api.UserCreateRequest(username=f"n-{uuid.uuid4().hex[:8]}",
                                       password="Sup3r-secret-pw!", **fields)
    return users_api.create_user(body, _admin=root, db=db)


def test_a_person_cannot_be_created_without_a_role():
    db = _db()
    root = _root(db)
    for role_id in (None, ""):
        assert _refused(lambda: _create(db, root, role_id=role_id)), role_id
    assert _create(db, root, role_id=_ro(db).id).role_id == _ro(db).id


def test_the_admin_flag_needs_no_role():
    db = _db()
    out = _create(db, _root(db), is_admin=True)
    assert out.is_admin and not out.role_id


# ── editing users ─────────────────────────────────────────────────────────────

def test_a_role_cannot_be_cleared_from_a_person():
    db = _db()
    root = _root(db)
    u = _user(db)
    role_service.apply_role_to_user(db, u, _ro(db))
    db.commit()
    assert _refused(lambda: _patch(db, u, root, role_id=""))


def test_unticking_admin_must_land_on_a_role():
    db = _db()
    root, u = _root(db), _user(db, is_admin=True)
    assert _refused(lambda: _patch(db, u, root, is_admin=False))
    db.expire_all()
    assert db.get(User, u.id).is_admin, "the refused demotion was still written"
    out = _patch(db, u, root, is_admin=False, role_id=_ro(db).id)
    assert not out.is_admin and out.role_id == _ro(db).id


def test_a_legacy_person_keeps_working_but_an_edit_needs_a_role():
    db = _db()
    root, u = _root(db), _user(db, full_name="Old")
    assert _refused(lambda: _patch(db, u, root, full_name="New"))
    db.expire_all()
    assert db.get(User, u.id).full_name == "Old"
    assert _patch(db, u, root, full_name="New", role_id=_ro(db).id).full_name == "New"


def test_operating_on_an_account_is_not_an_edit():
    """Deactivating, resetting a password or flipping the SSO switch on a row saved before
    the rule must still work: none of them says anything about the account's access."""
    db = _db()
    root = _root(db)
    u = _user(db)
    _patch(db, u, root, is_active=False)
    _patch(db, u, root, password="An0ther-long-password!")
    _patch(db, u, root, sso_sign_in=True)
    db.expire_all()
    got = db.get(User, u.id)
    assert not got.is_active and got.auth_provider == "sso_pending" and not got.role_id


def test_a_refused_edit_writes_nothing_even_with_the_sso_switch():
    """The SSO switch audits, and log_audit COMMITS. The role check therefore runs before
    anything is written, or this request would half-apply."""
    db = _db()
    root, u = _root(db), _user(db, full_name="Before")
    assert _refused(lambda: _patch(db, u, root, full_name="After", sso_sign_in=True))
    db.expire_all()
    got = db.get(User, u.id)
    assert got.full_name == "Before" and (got.auth_provider or "local") == "local"


def test_exempt_accounts_save_without_a_role():
    db = _db()
    root = _root(db)
    for cols in ({"auth_provider": "oidc", "oauth_subject": f"s-{uuid.uuid4().hex}"},
                 {"auth_provider": "azure_ad", "oauth_subject": f"s-{uuid.uuid4().hex}"},
                 {"is_service_account": True}):
        u = _user(db, **cols)
        assert _patch(db, u, root, full_name="Edited").full_name == "Edited", cols


def test_your_own_row_saves_with_its_unchanged_role():
    """The form sends role_id on every save. Before this, the self-change refusal fired on
    that unchanged value, so an admin could not edit their own name at all."""
    db = _db()
    me = _user(db, is_admin=True)
    role_service.apply_role_to_user(db, me, role_service.get_by_slug(db, "administrator"))
    db.commit()
    out = _patch(db, me, me, full_name="Me", role_id=me.role_id)
    assert out.full_name == "Me"
    try:
        _patch(db, me, me, role_id=_ro(db).id)
    except HTTPException as exc:
        assert exc.status_code == 400
    else:
        raise AssertionError("an admin changed their own role")
    # And an admin with no role can still edit themselves: the rule cannot be met there.
    bare = _user(db, is_admin=True)
    assert _patch(db, bare, bare, full_name="Bare", role_id="").full_name == "Bare"


# ── the forms ─────────────────────────────────────────────────────────────────

def _src(rel):
    return open(os.path.join(_ROOT, *rel.split("/")), encoding="utf-8").read()


def test_the_forms_require_the_role():
    groups = _src("web_dashboard/templates/rbac/_groups.html")
    assert 'x-model="form.role_id" required' in groups
    assert "No role &mdash; use the permissions below only" not in groups, (
        "the Groups form still offers a mapping with no role")
    users = _src("web_dashboard/templates/rbac/_users.html")
    assert ':required="roleRequired()"' in users
    body = users[users.index("roleRequired() {"):][:700]
    for exempt in ("form.is_admin", "form.is_service_account", "currentUserId",
                   "'azure_ad', 'oidc'"):
        assert exempt in body, f"roleRequired() lost the {exempt} exemption the API has"


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
