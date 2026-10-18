"""An empty permission map grants nothing -- and nobody lost access when that became true.

It used to mean UNRESTRICTED (every non-admin scope) in ``api/auth.has_permission``, which
is how an SSO group mapping saved with no permissions handed every member the storage,
inventory and credential routes. The rule flipped, and one migration
(``database._materialise_empty_permission_maps``) wrote an explicit ``full_access_map()``
onto every user and group mapping that relied on the old one. These tests pin:

  * the rule, in ``has_permission`` and the routes that restated it by hand;
  * that "Full access" leaves out exactly the pairs an empty map never reached;
  * the migration: who it converts, who it leaves alone, and that it runs once;
  * the login merge: a converted mapping still grants only when nothing else matched does;
  * the Users and Groups APIs: {} is "Full access", written out; None grants nothing.

Runs standalone:
    python tests/test_empty_permissions_deny.py
"""
import json
import os
import re
import sys
import tempfile
import uuid

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

_TMPDIR = tempfile.mkdtemp(prefix="empty-perms-test-")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{os.path.join(_TMPDIR, 'test.db')}")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-empty-perms")

from web_dashboard import database  # noqa: E402
from web_dashboard.api import auth  # noqa: E402
from web_dashboard.api.auth import (EXPLICIT_ONLY_LEVELS,  # noqa: E402
                                    LEGACY_FULL_ACCESS_KEY, PERMISSION_SCOPE_LEVELS,
                                    full_access_map, has_explicit_permission,
                                    has_permission)
from web_dashboard.config import settings  # noqa: E402
from web_dashboard.database import (Base, OAuthGroupMapping, SchemaMarker,  # noqa: E402
                                    SessionLocal, User, engine)

Base.metadata.create_all(bind=engine)

_APP = os.path.join(_ROOT, "web_dashboard")


def _session():
    db = SessionLocal()
    db.query(User).delete()
    db.query(OAuthGroupMapping).delete()
    db.query(SchemaMarker).filter(
        SchemaMarker.key == database._EMPTY_MEANS_NOTHING_MARKER).delete()
    db.commit()
    return db


def _sorted(perms):
    """The login merge sorts each level list; compare maps independent of order."""
    return {k: sorted(v) if isinstance(v, list) else v for k, v in perms.items()}


def _user(db, name, **cols):
    u = User(id=str(uuid.uuid4()), username=name, hashed_password="x", **cols)
    db.add(u)
    db.commit()
    return u


def _mapping(db, gid, default_permissions=None, role_id=None):
    m = OAuthGroupMapping(entra_group_id=gid, display_name=gid, workgroup="Default",
                          default_permissions=default_permissions, role_id=role_id)
    db.add(m)
    db.commit()
    return m


# ── the rule ──────────────────────────────────────────────────────────────────

def test_an_empty_map_grants_nothing():
    u = User(username="nobody", hashed_password="x")
    assert u.effective_permissions_dict == {}
    for scope, levels in PERMISSION_SCOPE_LEVELS.items():
        for level in levels:
            assert not has_permission(u, scope, level), f"{{}} still grants {scope}:{level}"


def test_no_route_restates_the_old_rule():
    """Four routes copied "empty means allowed" by hand (`if not perms: return True`,
    `if perms and ...`). The flip in has_permission does not reach a copy."""
    pattern = re.compile(r"if not perms\s*:\s*\n\s*return True"
                         r"|if perms and .*not in perms\.get\(|not perms or .*in perms\.get\(")
    hits = []
    for base, _dirs, files in os.walk(_APP):
        for name in files:
            if name.endswith(".py"):
                path = os.path.join(base, name)
                with open(path, encoding="utf-8") as fh:
                    if pattern.search(fh.read()):
                        hits.append(os.path.relpath(path, _ROOT))
    assert not hits, f"empty-means-allowed restated in: {hits}"


def test_secrets_use_follows_the_rule():
    from web_dashboard.api.config_mgmt import _can_use_secrets

    class _U:
        is_effective_admin = False

    empty, full = _U(), _U()
    empty.effective_permissions_dict = {}
    full.effective_permissions_dict = full_access_map()
    assert not _can_use_secrets(empty)
    assert _can_use_secrets(full), "an empty map used secrets:use, so Full access must too"


# ── what "Full access" is ─────────────────────────────────────────────────────

def _explicit_pairs_in_code():
    """Every (scope, level) checked with the explicit form, plus `can_audit_jobs`."""
    pattern = re.compile(r"\b(?:require|has)_explicit_permission\(\s*(?:[a-z_]+,\s*)?"
                         r"[\"']([a-z_]+)[\"'],\s*[\"']([a-z]+)[\"']")
    found = {("jobs", "read")}  # can_audit_jobs reads the map directly
    for base, _dirs, files in os.walk(_APP):
        for name in files:
            if name.endswith(".py"):
                with open(os.path.join(base, name), encoding="utf-8") as fh:
                    found.update(pattern.findall(fh.read()))
    return found


def test_every_explicit_gate_is_left_out_of_full_access():
    """An empty map never passed these, so "Full access" must not grant them: a new explicit
    gate that is not listed in EXPLICIT_ONLY_LEVELS would be handed to every converted user
    and every "Full access" tick."""
    listed = {(s, lv) for s, levels in EXPLICIT_ONLY_LEVELS.items() for lv in levels}
    missing = sorted(_explicit_pairs_in_code() - listed)
    assert not missing, f"add these to api/auth.EXPLICIT_ONLY_LEVELS: {missing}"


def test_full_access_is_the_catalog_minus_the_explicit_pairs():
    full = full_access_map()
    assert set(full) == set(PERMISSION_SCOPE_LEVELS)
    for scope, levels in PERMISSION_SCOPE_LEVELS.items():
        skip = EXPLICIT_ONLY_LEVELS.get(scope, ())
        assert full[scope] == [lv for lv in levels if lv not in skip], scope
    u = User(username="full", hashed_password="x")
    u.permissions_dict = full
    assert has_permission(u, "storage", "read") and has_permission(u, "aws", "delete")
    assert not has_explicit_permission(u, "audit", "read")
    assert not has_permission(u, "jobs", "read"), "Full access must not see everyone's jobs"
    assert not u.is_effective_admin


# ── the migration ─────────────────────────────────────────────────────────────

def test_the_migration_converts_exactly_the_users_who_relied_on_an_empty_map():
    db = _session()
    _mapping(db, "g-restricted", json.dumps({"aws": ["read"]}))
    local = _user(db, "local")
    restricted = _user(db, "restricted")
    restricted.permissions_dict = {"aws": ["read"]}
    admin = _user(db, "admin", is_admin=True)
    sa = _user(db, "robot", is_service_account=True)
    roled = _user(db, "roled", role_id="some-role")
    accessor = _user(db, "accessor", accessor_env_id="env-1")
    sso = _user(db, "sso", auth_provider="oidc")
    db.commit()

    database._materialise_empty_permission_maps(db)
    for u in (local, restricted, admin, sa, roled, accessor, sso):
        db.refresh(u)

    assert local.permissions_dict == full_access_map()
    assert restricted.permissions_dict == {"aws": ["read"]}
    assert admin.permissions_dict == {}, "an admin bypasses the map; do not write one"
    assert sa.permissions_dict == {}, "a service account never read {} as unrestricted"
    assert roled.permissions_dict == {}, "a role holder was already on a strict allowlist"
    assert accessor.permissions_dict == {}, "an accessor is confined by its allowlist"
    assert sso.permissions_dict == {}, (
        "an SSO user's access is rebuilt from the mappings at each login; a baseline "
        "written here would outlive their group membership")


def test_with_no_mappings_an_sso_user_is_converted_like_a_local_one():
    """Nothing rebuilds their session permissions, so nobody else keeps their access."""
    db = _session()
    saved = settings.azure_oauth_group_map
    settings.azure_oauth_group_map = {}
    try:
        sso = _user(db, "sso-only", auth_provider="oidc")
        database._materialise_empty_permission_maps(db)
        db.refresh(sso)
        assert sso.permissions_dict == full_access_map()
    finally:
        settings.azure_oauth_group_map = saved


def test_the_migration_tags_mappings_that_granted_everything():
    db = _session()
    null = _mapping(db, "g-null")
    empty = _mapping(db, "g-empty", "{}")
    restricted = _mapping(db, "g-restricted", json.dumps({"aws": ["read"]}))
    roled = _mapping(db, "g-roled", role_id="some-role")
    broken = _mapping(db, "g-broken", "{not json")

    database._materialise_empty_permission_maps(db)
    for m in (null, empty, restricted, roled, broken):
        db.refresh(m)

    want = {LEGACY_FULL_ACCESS_KEY: True, **full_access_map()}
    assert json.loads(null.default_permissions) == want
    assert json.loads(empty.default_permissions) == want
    assert json.loads(restricted.default_permissions) == {"aws": ["read"]}
    assert roled.default_permissions is None, "its role already granted a strict map"
    assert broken.default_permissions == "{not json", "login skips it; so does this"


def test_the_migration_runs_once():
    db = _session()
    _user(db, "first")
    assert database._materialise_empty_permission_maps(db) == 1
    later = _user(db, "created-after")
    assert database._materialise_empty_permission_maps(db) == 0
    db.refresh(later)
    assert later.permissions_dict == {}, "a user created after the flip gets nothing by default"


# ── the login merge ───────────────────────────────────────────────────────────

def _login(db, groups, email):
    resp = auth._complete_oauth_login(db, subject=f"sub-{email}", email=email,
                                      display_name=email, groups=groups, provider="oidc")
    assert resp.status_code == 302, resp.status_code
    assert "error=" not in resp.headers.get("location", ""), resp.headers.get("location")
    return db.query(User).filter(User.email == email).first()


def test_a_converted_mapping_alone_still_grants_full_access():
    db = _session()
    _mapping(db, "g-legacy")
    database._materialise_empty_permission_maps(db)
    u = _login(db, ["g-legacy"], "legacy@example.com")
    assert u.session_permissions_dict == _sorted(full_access_map())
    assert LEGACY_FULL_ACCESS_KEY not in u.effective_permissions_dict


def test_a_converted_mapping_does_not_widen_a_restricted_one():
    """Before the flip, a user in a NULL mapping AND a restricted one got the restricted
    one's map -- the NULL one contributed nothing. The converted mapping must keep that."""
    db = _session()
    _mapping(db, "g-legacy")
    _mapping(db, "g-restricted", json.dumps({"aws": ["read"]}))
    database._materialise_empty_permission_maps(db)
    u = _login(db, ["g-legacy", "g-restricted"], "both@example.com")
    assert u.session_permissions_dict == {"aws": ["read"]}


def test_a_converted_mapping_does_not_widen_a_users_own_grid():
    """Before the flip, an SSO user with their own restricted map and only a NULL mapping
    held their own map: the empty session added nothing. It still must."""
    db = _session()
    _mapping(db, "g-legacy")
    database._materialise_empty_permission_maps(db)
    u = _user(db, "own-grid", email="own@example.com", auth_provider="oidc",
              oauth_subject="sub-own@example.com")
    u.permissions_dict = {"aws": ["read"]}
    db.commit()
    u = _login(db, ["g-legacy"], "own@example.com")
    assert u.session_permissions_dict == {}
    assert u.effective_permissions_dict == {"aws": ["read"]}


def test_a_new_mapping_with_no_permissions_grants_nothing():
    db = _session()
    database._materialise_empty_permission_maps(db)  # the upgrade already happened
    _mapping(db, "g-new")
    u = _login(db, ["g-new"], "new@example.com")
    assert u.effective_permissions_dict == {}
    assert not has_permission(u, "storage", "read")


def test_the_env_fallback_still_means_full_access():
    """The .env group map cannot express permissions and has no row to migrate."""
    db = _session()
    saved = settings.azure_oauth_group_map
    settings.azure_oauth_group_map = {"g-env": "Default"}
    try:
        u = _login(db, ["g-env"], "env@example.com")
        assert u.session_permissions_dict == _sorted(full_access_map())
    finally:
        settings.azure_oauth_group_map = saved


# ── the APIs: {} is "Full access", written out ────────────────────────────────

def test_the_groups_api_writes_full_access_out_and_null_grants_nothing():
    from web_dashboard.api import groups
    assert groups._stored_permissions(None) is None
    assert json.loads(groups._stored_permissions({})) == full_access_map()
    assert json.loads(groups._stored_permissions({"aws": ["read"]})) == {"aws": ["read"]}


def test_the_groups_api_never_shows_the_legacy_key():
    from web_dashboard.api import groups
    db = _session()
    m = _mapping(db, "g-null")
    database._materialise_empty_permission_maps(db)
    db.refresh(m)
    shown = groups._mapping_to_response(m, db).default_permissions
    assert LEGACY_FULL_ACCESS_KEY not in shown
    assert shown == full_access_map()


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
