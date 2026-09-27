"""Retiring the permission levels nothing enforced: the stored maps, the validator, and
the one invariant that makes it safe.

The original fourteen scopes offered all four levels whether or not anything checked
them. `vms:delete`, `jobs:write`, `secrets:read` and thirty-odd others were checkboxes
that saved and granted nothing, and Entitle published them as requestable roles. They
are now out of the catalog (``api/auth.RETIRED_LEVELS``). Maps stored before that still
name them, so ``database._retire_unenforced_levels`` strips them once, at boot.

The invariant everything here rests on: **an empty permission map means unrestricted.**
Stripping ``{"secrets": ["read"]}`` must leave ``{"secrets": []}``, a strict allowlist
granting nothing (exactly what it granted before), and never ``{}``, which grants every
scope in the dashboard. So the strongest test below is not "the pairs are gone" but
"nobody's effective access changed", checked against every pair the catalog still
offers.

Runs on a throwaway SQLite file. tests/test_postgres_retired_levels.py imports the
scenarios below and runs them against PostgreSQL, because a JSON-in-Text rewrite is
where the two dialects part.

Run: python tests/test_permission_levels_retired.py   (or under pytest)
"""
import os
import sys
import tempfile
import uuid

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
_TMPDIR = tempfile.mkdtemp(prefix="retired-levels-test-")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{os.path.join(_TMPDIR, 'test.db')}")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-retired-levels")

from fastapi import HTTPException  # noqa: E402

from web_dashboard.api.auth import (PERMISSION_SCOPE_LEVELS,  # noqa: E402
                                    RETIRED_LEVELS, has_permission, is_retired,
                                    validate_permissions_payload)
from web_dashboard.database import (AccessRole, Base, OAuthGroupMapping,  # noqa: E402
                                    SchemaMarker, SessionLocal, User,
                                    _RETIRE_LEVELS_MARKER, _retire_unenforced_levels,
                                    _strip_retired, engine)
from web_dashboard.services import role_service  # noqa: E402

Base.metadata.create_all(bind=engine)

_PREFIX = "retire-test-"


def _clean(db):
    """Remove only what these tests create, so a shared PostgreSQL database is left as
    found, and reset the marker so the migration runs again."""
    db.query(User).filter(User.username.like(f"{_PREFIX}%")).delete(synchronize_session=False)
    db.query(OAuthGroupMapping).filter(
        OAuthGroupMapping.entra_group_id.like(f"{_PREFIX}%")).delete(synchronize_session=False)
    db.query(AccessRole).filter(AccessRole.slug.like(f"{_PREFIX}%")).delete(
        synchronize_session=False)
    db.query(SchemaMarker).filter(SchemaMarker.key == _RETIRE_LEVELS_MARKER).delete()
    db.commit()


def _user(db, **cols):
    u = User(id=str(uuid.uuid4()), username=f"{_PREFIX}{uuid.uuid4().hex[:8]}",
             is_active=True, is_admin=False)
    for k, v in cols.items():
        setattr(u, k, v)
    db.add(u)
    return u


def _pre_retirement_map():
    """A map holding every level the fourteen used to offer -- i.e. every retired pair
    alongside every live one -- plus a scope that loses ALL its levels (`secrets` minus
    `use`)."""
    m = {s: list(PERMISSION_SCOPE_LEVELS[s]) + list(RETIRED_LEVELS.get(s, ()))
         for s in PERMISSION_SCOPE_LEVELS}
    m["secrets"] = ["read", "write", "delete"]          # every one of them retired
    return m


def _access(user):
    return {(s, lv) for s, levels in PERMISSION_SCOPE_LEVELS.items() for lv in levels
            if has_permission(user, s, lv)}


# ── the pure helper ───────────────────────────────────────────────────────────

def test_stripping_never_removes_a_key():
    out = _strip_retired({"secrets": ["read"], "vms": ["read", "delete"], "is_admin": True},
                         RETIRED_LEVELS)
    assert out == {"secrets": [], "vms": ["read"], "is_admin": True}, out
    assert _strip_retired({"secrets": ["read"]}, RETIRED_LEVELS) == {"secrets": []}


def test_the_retired_pairs_are_really_out_of_the_catalog():
    for scope, levels in RETIRED_LEVELS.items():
        for lv in levels:
            assert lv not in PERMISSION_SCOPE_LEVELS[scope], f"{scope}:{lv} is still offered"
            assert is_retired(scope, lv)
    assert not is_retired("secrets", "use") and not is_retired("pov", "use")


# ── the migration ─────────────────────────────────────────────────────────────

def scenario_every_stored_map_is_cleaned():
    db = SessionLocal()
    try:
        _clean(db)
        u = _user(db)
        u.permissions_dict = _pre_retirement_map()
        u.session_permissions_dict = {"jobs": ["read", "write"]}
        u.jit_permissions_dict = {"aws": ["read", "use"]}
        role = AccessRole(id=str(uuid.uuid4()), slug=f"{_PREFIX}role", name=f"{_PREFIX}role",
                          is_builtin=False)
        role.permissions_dict = {"vms": ["read", "delete"], "k8s": ["use"]}
        db.add(role)
        mapping = OAuthGroupMapping(entra_group_id=f"{_PREFIX}group", display_name="g",
                                    workgroup="hydra")
        import json
        mapping.default_permissions = json.dumps({"config_mgmt": ["read", "delete", "use"]})
        db.add(mapping)
        db.commit()

        assert _retire_unenforced_levels(db) >= 5
        db.expire_all()
        u = db.query(User).filter(User.id == u.id).one()
        for scope, levels in u.permissions_dict.items():
            assert not any(is_retired(scope, lv) for lv in levels), (scope, levels)
        assert u.permissions_dict["secrets"] == [], "secrets lost its key -- map could empty"
        assert u.session_permissions_dict == {"jobs": ["read"]}
        assert u.jit_permissions_dict == {"aws": ["read"]}
        role = db.query(AccessRole).filter(AccessRole.slug == f"{_PREFIX}role").one()
        assert role.permissions_dict == {"vms": ["read"], "k8s": []}
        mapping = db.query(OAuthGroupMapping).filter(
            OAuthGroupMapping.entra_group_id == f"{_PREFIX}group").one()
        assert json.loads(mapping.default_permissions) == {"config_mgmt": ["read"]}
    finally:
        _clean(db)
        db.close()


def scenario_nobodys_effective_access_changes():
    """The retired pairs granted nothing, so removing them must change nothing. Checked
    for every live pair, on a user whose ONLY map loses a whole scope's levels -- the case
    that would flip to unrestricted if a key were dropped."""
    db = SessionLocal()
    try:
        _clean(db)
        full = _user(db)
        full.permissions_dict = _pre_retirement_map()
        narrow = _user(db)
        narrow.permissions_dict = {"secrets": ["read", "write"]}   # nothing live at all
        db.commit()
        before = {u.id: _access(u) for u in (full, narrow)}
        assert before[narrow.id] == set(), "the fixture already granted something"

        _retire_unenforced_levels(db)
        db.expire_all()
        for uid, was in before.items():
            u = db.query(User).filter(User.id == uid).one()
            assert _access(u) == was, f"access changed for {u.username}"
            assert u.effective_permissions_dict, "a map was emptied -- now unrestricted"
    finally:
        _clean(db)
        db.close()


def scenario_it_runs_once_and_leaves_empty_and_null_maps_alone():
    db = SessionLocal()
    try:
        _clean(db)
        unrestricted = _user(db)                     # NULL: unrestricted, must stay so
        braces = _user(db, permissions="{}")         # {} at rest: also unrestricted
        db.commit()
        _retire_unenforced_levels(db)
        db.expire_all()
        assert db.query(User).filter(User.id == unrestricted.id).one().permissions is None
        assert db.query(User).filter(User.id == braces.id).one().permissions == "{}"

        later = _user(db)
        later.permissions_dict = {"vms": ["delete"]}
        db.commit()
        assert _retire_unenforced_levels(db) == 0, "the marker did not stop a second run"
    finally:
        _clean(db)
        db.close()


def scenario_reconcile_rebuilds_role_copies_from_the_cleaned_roles():
    """init_db runs the migration BEFORE role_service.reconcile, because a user's copy of
    their role is rebuilt from the role row."""
    db = SessionLocal()
    try:
        _clean(db)
        role = AccessRole(id=str(uuid.uuid4()), slug=f"{_PREFIX}ops", name=f"{_PREFIX}ops",
                          is_builtin=False)
        role.permissions_dict = {"vms": ["read", "use"]}
        db.add(role)
        u = _user(db, role_id=role.id)
        u.role_permissions_dict = {"vms": ["read", "use"]}
        db.commit()
        _retire_unenforced_levels(db)
        role_service.reconcile(db)
        db.expire_all()
        assert db.query(User).filter(User.id == u.id).one().role_permissions_dict == {
            "vms": ["read"]}
    finally:
        _clean(db)
        db.close()


def test_every_stored_map_is_cleaned():
    scenario_every_stored_map_is_cleaned()


def test_nobodys_effective_access_changes():
    scenario_nobodys_effective_access_changes()


def test_it_runs_once_and_leaves_empty_and_null_maps_alone():
    scenario_it_runs_once_and_leaves_empty_and_null_maps_alone()


def test_reconcile_rebuilds_role_copies_from_the_cleaned_roles():
    scenario_reconcile_rebuilds_role_copies_from_the_cleaned_roles()


# ── the validator ─────────────────────────────────────────────────────────────

def test_the_validator_drops_a_retired_level_in_place():
    """A grid loaded before the upgrade still sends `vms:delete`. It granted nothing, so
    the save must succeed -- and callers keep using the object they passed, so the strip
    has to happen in place."""
    payload = {"vms": ["read", "delete"], "secrets": ["read"]}
    validate_permissions_payload(payload)
    assert payload == {"vms": ["read"], "secrets": []}, payload


def test_the_validator_still_refuses_what_was_never_valid():
    for bad in ({"vms": ["frobnicate"]}, {"inventory": ["delete"]}, {"nope": ["read"]}):
        try:
            validate_permissions_payload(bad)
        except HTTPException as exc:
            assert exc.status_code == 422
        else:
            raise AssertionError(f"accepted {bad}")


def test_built_in_roles_hold_no_retired_pair():
    for spec in role_service._BUILTIN_ROLES:
        for scope, levels in spec["permissions"].items():
            if isinstance(levels, list):
                assert not any(is_retired(scope, lv) for lv in levels), (spec["slug"], scope)


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
