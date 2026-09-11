"""Two halves of the same problem: what the server accepts, and what it must not revoke.

**Validation.** ``PATCH /api/users/{id}`` stored whatever dict it was handed for the whole
life of the feature — no key check, no level check, no type check — while
``api/entitle_rest.py`` validated its own input from day one. The asymmetry is how a typo
became permanent and invisible: the grid renders only keys it knows but round-trips the
whole object on every save, so an unknown key survives forever and still grants if any
route is ever gated on that string. The same handler validates ``persona`` against its
registry eight lines earlier, which is the pattern this follows.

The non-list check is not pedantry. ``has_permission`` does ``level in perms.get(scope)``,
so ``{"secrets": "use"}`` turns an exact match into a **substring** test.

**Backfill.** ``has_permission`` treats ``{}`` as unrestricted but a non-empty map as a
strict per-scope allowlist, so a scope absent from a map is a deny. Adding a scope and
gating a route on it silently revokes that route from every explicitly-permissioned user —
no error, no log line, and the admin who set those permissions never saw the row.
``api/expiry.py`` declined to invent a scope for exactly this reason.

The mirror-image mistake is just as bad and easier to make by accident: backfilling a
scope whose routes used to require the ADMIN FLAG hands out access nobody outside that flag
ever had. So the rule is "grant exactly what a non-admin with an explicit map could already
reach", and that is what these tests pin.

Runs under pytest, or standalone:
    python tests/test_permission_validation_backfill.py
"""
import json
import os
import sys
import tempfile
import uuid

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

_TMPDIR = tempfile.mkdtemp(prefix="perm-backfill-test-")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{os.path.join(_TMPDIR, 'test.db')}")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-perm-backfill")

from fastapi import HTTPException  # noqa: E402

from web_dashboard.api.auth import (PERMISSION_SCOPE_LEVELS,  # noqa: E402
                                    has_explicit_permission, has_permission,
                                    validate_permissions_payload)
from web_dashboard import database as db_mod  # noqa: E402
from web_dashboard.database import (Base, OAuthGroupMapping, SchemaMarker,  # noqa: E402
                                    User, _BACKFILL_V1_DELIBERATELY_EMPTY,
                                    _BACKFILL_V1_SCOPES,
                                    _backfill_new_permission_scopes)


def _refused(payload):
    try:
        validate_permissions_payload(payload)
    except HTTPException as exc:
        assert exc.status_code == 422, f"expected 422, got {exc.status_code}"
        return str(exc.detail)
    return None


# ── validation ───────────────────────────────────────────────────────────────

def test_a_valid_payload_passes_unchanged():
    payload = {"pov": ["read", "use"], "vms": ["read"]}
    assert validate_permissions_payload(payload) is payload
    assert validate_permissions_payload(None) is None
    assert validate_permissions_payload({}) == {}


def test_an_unknown_scope_is_refused_and_the_message_names_the_valid_ones():
    detail = _refused({"nope": ["read"]})
    assert detail, "an unknown scope was accepted — it would be stored forever, invisibly"
    assert "nope" in detail and "Valid scopes" in detail


def test_an_unknown_level_is_refused():
    assert _refused({"vms": ["reed"]}), "a misspelled level was accepted"


def test_a_level_the_scope_does_not_offer_is_refused_with_a_useful_message():
    detail = _refused({"inventory": ["delete"]})
    assert detail, "inventory:delete was accepted but nothing can ever enforce it"
    assert "does not offer" in detail and "read" in detail


def test_a_non_list_value_is_refused_because_it_becomes_a_substring_test():
    detail = _refused({"secrets": "use"})
    assert detail, (
        'permissions {"secrets": "use"} was accepted — `level in perms.get(scope)` is then '
        "a substring test, so `use` matches and so would any substring of it")
    assert "must be a list" in detail


def test_a_non_dict_payload_is_refused():
    assert _refused(["vms"])
    assert _refused("vms:read")


def test_is_admin_is_allowed_as_a_bool_and_refused_otherwise():
    """It is a real key in the session/jit columns (see effective_permissions_dict), so it
    must survive validation — but only as the type the union actually ORs."""
    assert validate_permissions_payload({"is_admin": True}) == {"is_admin": True}
    assert _refused({"is_admin": ["read"]})


def test_both_write_paths_call_the_validator():
    """A validator nothing calls is the state this feature was already in."""
    for rel, marker in ((("api", "users.py"), "if body.permissions is not None:"),
                        (("api", "groups.py"), "default_permissions")):
        with open(os.path.join(_ROOT, "web_dashboard", *rel), encoding="utf-8") as fh:
            src = fh.read()
        assert "validate_permissions_payload" in src, (
            f"web_dashboard/{'/'.join(rel)} stores permissions without validating them")
        assert marker in src


# ── the two predicates ───────────────────────────────────────────────────────

def test_an_empty_map_is_unrestricted_for_the_permissive_form():
    """Load-bearing backward compatibility for pre-OIDC users. Changing it locks them
    out of things they can already do."""
    u = User(username="legacy", hashed_password="x")
    assert u.effective_permissions_dict == {}
    assert has_permission(u, "pov", "write")


def test_an_empty_map_is_NOT_unrestricted_for_the_explicit_form():
    """The whole reason require_explicit_permission exists. Converting an ex-admin route
    with the permissive form would hand it to every legacy NULL-permission user."""
    u = User(username="legacy", hashed_password="x")
    assert not has_explicit_permission(u, "audit", "read")
    assert not has_explicit_permission(u, "connections", "write")


def test_both_forms_still_pass_an_admin():
    u = User(username="boss", hashed_password="x")
    u.is_admin = True
    assert has_permission(u, "audit", "read")
    assert has_explicit_permission(u, "audit", "read")


def test_the_explicit_form_honours_a_real_grant():
    u = User(username="named", hashed_password="x")
    u.permissions_dict = {"audit": ["read"]}
    assert has_explicit_permission(u, "audit", "read")
    assert not has_explicit_permission(u, "audit", "write")


# ── the backfill ─────────────────────────────────────────────────────────────

def _session():
    """A throwaway SQLite database per test, so marker state cannot leak between them."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    path = os.path.join(_TMPDIR, f"bf-{uuid.uuid4().hex}.db")
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _add_user(db, name, perms=None, accessor=None):
    u = User(username=name, hashed_password="x")
    u.accessor_env_id = accessor
    if perms is not None:
        u.permissions_dict = perms
    db.add(u)
    db.commit()
    return u


def test_an_explicit_map_gains_the_new_scopes_and_keeps_what_it_had():
    db = _session()
    u = _add_user(db, "restricted", {"vms": ["read"], "aws": ["read", "write"]})
    changed = _backfill_new_permission_scopes(db)
    assert changed == 1
    got = u.permissions_dict
    assert got["vms"] == ["read"], "an existing grant was altered"
    assert got["aws"] == ["read", "write"]
    assert got["pov"] == ["read", "write", "delete", "use"], (
        "the POV routes were ungated, so all four levels were reachable")
    assert got["proxmox"] == ["read", "write", "delete"]
    assert got["inventory"] == ["read"]


def test_a_null_map_is_left_alone_because_null_already_means_unrestricted():
    """Writing a map for these users would NARROW them to whatever we wrote."""
    db = _session()
    u = _add_user(db, "legacy")
    assert u.permissions is None
    _backfill_new_permission_scopes(db)
    assert u.permissions is None, "the backfill turned an unrestricted user into a limited one"


def test_ex_admin_scopes_are_never_granted():
    """The mirror-image mistake. connections/costs/agents/audit/notifications and the
    pov_templates writes all required the admin flag, so nobody with an explicit map could
    reach them — granting them would be handing out new power, not preserving access."""
    db = _session()
    u = _add_user(db, "restricted", {"vms": ["read"]})
    _backfill_new_permission_scopes(db)
    got = u.permissions_dict
    for scope in _BACKFILL_V1_DELIBERATELY_EMPTY:
        assert scope not in got, (
            f"{scope} was backfilled, but its routes were admin-only — this grants access "
            "that no non-admin ever had")


def test_the_mixed_routers_get_only_their_reachable_half():
    db = _session()
    u = _add_user(db, "restricted", {"vms": ["read"]})
    _backfill_new_permission_scopes(db)
    got = u.permissions_dict
    # Storage's data plane was ungated; its config and deletes were not.
    assert got["storage"] == ["read", "write"], got.get("storage")
    # Only GET /api/gateways was reachable.
    assert got["gateways"] == ["read"], got.get("gateways")
    # Of the image routes, only the three reads were ungated.
    assert got["images"] == ["read"], got.get("images")


def test_an_already_present_scope_is_widened_by_level_not_skipped():
    """`images` and `config_mgmt` were in the catalog already and enforced NOWHERE, so a
    map can hold a subset. Skip-if-present would leave {"images": ["write"]} without the
    `read` its three now-gated routes need."""
    db = _session()
    u = _add_user(db, "partial", {"images": ["write"], "config_mgmt": ["read"]})
    _backfill_new_permission_scopes(db)
    got = u.permissions_dict
    assert set(got["images"]) == {"read", "write"}, got["images"]
    assert set(got["config_mgmt"]) == {"read", "write"}, got["config_mgmt"]


def test_it_is_idempotent_and_a_second_run_cannot_re_grant():
    """The marker is the point: re-running must not restore a scope an administrator
    deliberately removed after the migration."""
    db = _session()
    u = _add_user(db, "restricted", {"vms": ["read"]})
    assert _backfill_new_permission_scopes(db) == 1
    assert db.query(SchemaMarker).count() == 1

    perms = u.permissions_dict
    perms.pop("pov", None)              # the admin decided this user gets no POV access
    u.permissions_dict = perms
    db.commit()

    assert _backfill_new_permission_scopes(db) == 0, "the marker did not stop a second run"
    assert "pov" not in u.permissions_dict, (
        "a second run re-granted a scope an administrator had removed")


def test_an_accessor_is_skipped():
    db = _session()
    u = _add_user(db, "povguest_x", {"vms": ["read"]}, accessor="env-a")
    _backfill_new_permission_scopes(db)
    assert "pov" not in u.permissions_dict, (
        "an accessor's map was widened — it reaches nothing either way, but a confined "
        "login must not read as permissive to the next person looking at the row")


def test_group_mappings_are_widened_too():
    """_complete_oauth_login overwrites session_permissions from these on EVERY login, so
    widening only the user rows would be undone the next time an OIDC user signed in."""
    db = _session()
    m = OAuthGroupMapping(
        entra_group_id="g-1", display_name="Engineers", workgroup="default",
        default_permissions=json.dumps({"vms": ["read"]}))
    db.add(m)
    db.commit()
    _backfill_new_permission_scopes(db)
    got = json.loads(m.default_permissions)
    assert got["vms"] == ["read"]
    assert got["pov"] == ["read", "write", "delete", "use"]


def test_a_mapping_with_null_permissions_is_left_alone():
    """NULL there means "all permissions" (see the column comment), same as on a user."""
    db = _session()
    m = OAuthGroupMapping(entra_group_id="g-2", display_name="All", workgroup="default",
                          default_permissions=None)
    db.add(m)
    db.commit()
    _backfill_new_permission_scopes(db)
    assert m.default_permissions is None


def test_malformed_stored_json_does_not_stop_the_run():
    db = _session()
    m = OAuthGroupMapping(entra_group_id="g-3", display_name="Broken",
                          workgroup="default", default_permissions="{not json")
    db.add(m)
    u = _add_user(db, "restricted", {"vms": ["read"]})
    db.commit()
    _backfill_new_permission_scopes(db)
    assert "pov" in u.permissions_dict, "one malformed mapping aborted the whole backfill"
    assert m.default_permissions == "{not json", "malformed JSON was rewritten"


def test_everything_the_backfill_grants_is_a_real_scope_and_level():
    for scope, levels in _BACKFILL_V1_SCOPES.items():
        assert scope in PERMISSION_SCOPE_LEVELS, f"unknown scope {scope!r}"
        for level in levels:
            assert level in PERMISSION_SCOPE_LEVELS[scope], f"{scope}:{level}"


def test_the_backfill_result_validates():
    """Round-trip: what the migration writes must be something the API would accept."""
    db = _session()
    u = _add_user(db, "restricted", {"vms": ["read"]})
    _backfill_new_permission_scopes(db)
    validate_permissions_payload(u.permissions_dict)


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
