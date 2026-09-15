"""Creating a user from the admin UI must not hand them the whole dashboard.

The create form had no permission grid and sent no ``permissions`` field, so the column
stayed NULL — and ``has_permission`` reads an empty map as UNRESTRICTED, deliberately, for
the pre-OIDC users who predate the column. Every account an admin made therefore held every
non-admin permission until somebody re-opened them in Edit and ticked boxes. Nothing failed,
nothing logged, and the grid the admin never saw was the only place it showed.

The fix is in two halves and both are tested here:

  * ``POST /api/users`` accepts a permission map at all (``test_the_create_route_*``).
  * "Restricted, nothing granted" is *expressible*. It was not: the obvious payload is
    ``{}``, ``{}`` is falsy, and both write paths turn a falsy map into NULL — which is
    the unrestricted state. So asking for nothing got you everything. The UI now sends
    every scope as a key with its granted levels, which is non-empty and therefore a
    strict per-scope allowlist (``test_an_all_empty_map_denies_everything``).

The backward-compatibility cases are the ones to be careful about: an API caller that omits
the field, and the "Full access (unrestricted)" tick, must both still mean NULL.

Run: python tests/test_user_creation_least_privilege.py   (or under pytest)
"""
import os
import sys
import uuid

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from web_dashboard.database import Base, SessionLocal, User, engine, get_db  # noqa: E402
from web_dashboard.api import users as users_api  # noqa: E402
from web_dashboard.api.auth import (  # noqa: E402
    PERMISSION_SCOPES, has_permission, require_admin)


def _client():
    """The real /api/users router with only the admin principal faked."""
    Base.metadata.create_all(bind=engine)
    app = FastAPI()
    app.include_router(users_api.router)

    db = SessionLocal()
    admin = db.query(User).filter(User.username == "_lp_admin").first()
    if not admin:
        admin = User(username="_lp_admin", hashed_password="x", is_admin=True, is_active=True)
        db.add(admin)
        db.commit()
        db.refresh(admin)
    db.close()

    app.dependency_overrides[require_admin] = lambda: admin
    app.dependency_overrides[get_db] = lambda: SessionLocal()
    return TestClient(app)


def _create(**body):
    name = body.pop("username", None) or ("u" + uuid.uuid4().hex[:10])
    payload = {"username": name, "password": "pw"}
    payload.update(body)
    resp = _client().post("/api/users", json=payload)
    db = SessionLocal()
    try:
        return resp, db.query(User).filter(User.username == name).first()
    finally:
        db.close()


def _restricted_map(**grants):
    """What the grid now sends: every scope present, only the named ones granted."""
    out = {scope: [] for scope in PERMISSION_SCOPES}
    out.update(grants)
    return out


# ── the map reaches the column ────────────────────────────────────────────────

def test_the_create_route_stores_the_permission_map():
    resp, user = _create(permissions=_restricted_map(inventory=["read"]))
    assert resp.status_code == 201, resp.text
    assert user.permissions is not None, (
        "POST /api/users dropped the permission map — this is the bug: a NULL column is "
        "unrestricted")
    assert sorted(user.permissions_dict) == sorted(PERMISSION_SCOPES)


def test_a_granted_scope_is_allowed_and_every_other_one_is_not():
    _resp, user = _create(permissions=_restricted_map(inventory=["read"]))
    assert has_permission(user, "inventory", "read")
    assert not has_permission(user, "aws", "read")
    assert not has_permission(user, "pov", "use")
    assert not has_permission(user, "inventory", "write")


def test_an_all_empty_map_denies_everything():
    """The case that used to produce a full-access account.

    Every scope present, nothing granted against any of them. Non-empty, so it is a strict
    allowlist; empty per scope, so the allowlist allows nothing.
    """
    _resp, user = _create(permissions=_restricted_map())
    assert user.permissions is not None, (
        "an all-empty map collapsed to NULL, so 'grant nothing' still means 'grant "
        "everything' — the exact inversion this test exists for")
    still_allowed = [s for s in PERMISSION_SCOPES if has_permission(user, s, "read")]
    assert not still_allowed, f"these scopes are still allowed: {still_allowed}"


def test_the_create_route_accepts_a_pov_narrowing():
    """Reachable from the create panel now that the grid is: ticking a POV level shows
    the picker, and a narrowing chosen there has to survive the POST."""
    _resp, user = _create(permissions=_restricted_map(pov=["read", "use"]),
                          pov_env_ids=["env-a", "env-b"])
    assert user.pov_env_ids_list == ["env-a", "env-b"]


# ── backward compatibility, which is the risky half ───────────────────────────

def test_omitting_permissions_still_means_unrestricted():
    """An API caller written before the field existed must be unaffected. Only the admin
    UI changed its default; the route's default did not."""
    resp, user = _create()
    assert resp.status_code == 201, resp.text
    assert user.permissions is None
    assert has_permission(user, "aws", "delete")


def test_an_empty_map_still_means_unrestricted():
    """What "Full access (unrestricted)" sends. Kept falsy-means-NULL on purpose: every
    legacy user is in this state and changing it would lock them out."""
    _resp, user = _create(permissions={})
    assert user.permissions is None
    assert has_permission(user, "gateways", "delete")


# ── the validator runs on this path too ───────────────────────────────────────

def test_an_unknown_scope_is_refused_on_create():
    """api/users.py's PATCH has validated since the validator existed; the create path did
    not accept permissions at all, so it is new surface and needs the same guard. An
    unvalidated key here is invisible to the grid and permanent."""
    resp, _user = _create(permissions={"not_a_scope": ["read"]})
    assert resp.status_code == 422, resp.text


def test_a_level_the_scope_does_not_offer_is_refused_on_create():
    resp, _user = _create(permissions={"inventory": ["delete"]})
    assert resp.status_code == 422, resp.text


# ── the UI sends what these tests assume ──────────────────────────────────────

def test_the_grid_serialises_every_scope_rather_than_only_the_ticked_ones():
    """Asserted by source: this is a browser-side function, and the assumption every test
    above rests on. Sending only the ticked scopes would make the empty case {}, which is
    where the whole bug came from."""
    with open(os.path.join(_ROOT, "web_dashboard", "static", "js", "app.js"),
              encoding="utf-8") as fh:
        src = fh.read()
    body = src.split("buildPermissionsPayload() {")[1].split("\n        },")[0]
    assert "if (this.form.permissionsUnrestricted) return {};" in body, (
        "the unrestricted escape hatch no longer sends {}")
    assert "for (const scope of (this.permissionScopes || []))" in body, (
        "buildPermissionsPayload iterates something other than the full catalog — if it "
        "only walks the ticked scopes, 'restricted with nothing granted' is {} again, "
        "which stores NULL, which is unrestricted")


def test_the_create_form_defaults_to_restricted_and_shows_the_grid():
    with open(os.path.join(_ROOT, "web_dashboard", "templates", "users", "list.html"),
              encoding="utf-8") as fh:
        src = fh.read()
    block = src.split("openCreate() {")[1].split("openEdit(")[0]
    assert "permissionsUnrestricted: false" in block, (
        "the New User form defaults to unrestricted again")
    assert "is_admin: false" in block, "the New User form defaults to admin"
    assert 'x-show="editingUser && !form.is_admin"' not in src, (
        "the permission grid is hidden on create again, so there is no way to grant "
        "anything before the account exists")
    assert "permissions: this.form.is_admin ? null : this.buildPermissionsPayload()" in src, (
        "the create POST no longer sends a permission map")


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
