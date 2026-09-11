"""Granting read on ONE POV: the object dimension the permission model did not have.

Every scope in ``PERMISSION_SCOPES`` is a feature area, so "this user may read POV *X*"
was not expressible. The only way to confine somebody to one POV was to mint an accessor —
a login bound by ``User.accessor_env_id`` to a five-route ``/self`` surface, which is right
for an anonymous prospect and wrong for a named stakeholder who should also be a real user.

Two axes now, and the separation is the design:

  * ``pov`` scope — what you may DO. ``use`` is the level that ticks a use case and wakes a
    suspended environment, deliberately excluding create, destroy, share and accessor
    minting. That is what makes "read their POV and check off use cases" a grant rather
    than a compromise.
  * ``User.pov_env_ids`` — WHICH POVs. Empty means every POV the scope allows, so every
    pre-existing user is unaffected.

The things that are silent when they break, and therefore tested here:

  * **The instance gate is router-level.** ``api/pov.py`` has ~40 routes and most name an
    ``{env_id}``; a per-handler check is something each new route must remember, and the
    one that forgets leaks somebody else's POV with no error. So the guard reads
    ``request.path_params`` once, for the whole router.
  * **404, not 403.** A 403 on a POV you were not granted confirms it exists.
  * **Lists filter separately.** A list has no ``{env_id}`` to inspect, so
    ``GET /managed`` and the archive filter in the query. Without that a narrowed user saw
    every POV on the page and found the limit only by clicking one.
  * **An accessor is not narrowed, it is confined.** Setting ``pov_env_ids`` on one would
    put two contradictory bindings on the same row.

Runs under pytest, or standalone:
    python tests/test_pov_instance_grants.py
"""
import os
import sys
import tempfile
import uuid

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="pov-grants-test-"), "test.db")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_TMPDB}")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-pov-instance-grants")

from fastapi import HTTPException  # noqa: E402

from web_dashboard.api import auth as auth_mod  # noqa: E402
from web_dashboard.database import User  # noqa: E402


class _FakeRequest:
    """Just the one attribute require_pov_env_access reads."""

    def __init__(self, **path_params):
        self.path_params = path_params


def _user(*, admin=False, env_ids=None, perms=None, accessor=None):
    u = User(username="u-" + uuid.uuid4().hex[:8], hashed_password="x")
    u.is_admin = admin
    u.accessor_env_id = accessor
    if env_ids is not None:
        u.pov_env_ids_list = env_ids
    if perms is not None:
        u.permissions_dict = perms
    return u


def _reach(user, env_id):
    """True if this user may reach that env, per the router-level dependency."""
    try:
        auth_mod.require_pov_env_access(_FakeRequest(env_id=env_id), user)
        return True
    except HTTPException as exc:
        assert exc.status_code == 404, (
            f"expected 404 so the id is not probeable, got {exc.status_code}")
        return False


# ── the column ───────────────────────────────────────────────────────────────

def test_empty_means_every_pov():
    """The default for every existing row, and what makes the scope backfill honest."""
    u = _user(perms={"pov": ["read"]})
    assert u.pov_env_ids_list == []
    assert auth_mod.pov_env_scope(u) is None, "empty list must mean unrestricted"
    assert _reach(u, "any-env-at-all")


def test_setting_and_clearing_round_trips():
    u = _user(perms={"pov": ["read"]})
    u.pov_env_ids_list = ["env-a", "env-b"]
    assert u.pov_env_ids_list == ["env-a", "env-b"]
    assert u.pov_env_ids is not None
    u.pov_env_ids_list = []
    assert u.pov_env_ids is None, "clearing must store NULL, i.e. 'every POV'"
    assert u.pov_env_ids_list == []


def test_blank_and_non_string_entries_are_dropped():
    """A blank id would narrow the user to an environment that cannot exist, which reads
    as "can see nothing" -- and withholding the scope is how you express that."""
    u = _user()
    u.pov_env_ids_list = ["env-a", "  ", "", "  env-b  "]
    assert u.pov_env_ids_list == ["env-a", "env-b"]


def test_a_malformed_column_reads_as_unrestricted_not_as_a_substring_test():
    """A bare string would make ``env_id in scope`` a SUBSTRING test -- the same trap a
    non-list permission value has in has_permission."""
    u = _user()
    u.pov_env_ids = '"env-a"'          # a JSON string, not a list
    assert u.pov_env_ids_list == []
    u.pov_env_ids = "not json at all"
    assert u.pov_env_ids_list == []


# ── the gate ─────────────────────────────────────────────────────────────────

def test_a_narrowed_user_reaches_only_their_pov():
    u = _user(perms={"pov": ["read", "use"]}, env_ids=["env-mine"])
    assert _reach(u, "env-mine")
    assert not _reach(u, "env-theirs")


def test_an_admin_is_never_narrowed():
    u = _user(admin=True, env_ids=["env-mine"])
    assert auth_mod.pov_env_scope(u) is None
    assert _reach(u, "env-theirs")


def test_a_route_with_no_env_id_is_not_refused():
    """``/managed``, ``/platforms`` and the archive take no id; the gate must pass them
    through and let the list filter do the narrowing."""
    u = _user(perms={"pov": ["read"]}, env_ids=["env-mine"])
    auth_mod.require_pov_env_access(_FakeRequest(), u)


def test_the_gate_is_attached_to_the_router_not_to_handlers():
    """~40 routes, most naming an {env_id}. A per-handler check is a thing every future
    route has to remember; this asserts it stays a router-level dependency."""
    with open(os.path.join(_ROOT, "web_dashboard", "api", "pov.py"), encoding="utf-8") as fh:
        src = fh.read()
    # Split on a line-initial ")" -- the nested require_permission(...) calls mean the
    # first plain ")" is not the end of the declaration.
    router_decl = src.split("router = APIRouter(")[1].split("\n)")[0]
    assert "require_pov_env_access" in router_decl, (
        "the instance gate left the router declaration in api/pov.py")
    assert 'require_permission("pov", "read")' in router_decl, (
        "the pov:read floor left the router declaration in api/pov.py")


def test_the_accessor_router_carries_the_gate_and_the_self_router_does_not():
    with open(os.path.join(_ROOT, "web_dashboard", "api", "pov_accessor.py"),
              encoding="utf-8") as fh:
        src = fh.read()
    se_half = src.split('router = APIRouter(')[1].split("self_router")[0]
    assert "require_pov_env_access" in se_half, (
        "the SE half of pov_accessor.py names {env_id} on every route and lost its gate")
    self_half = src.split("self_router = APIRouter(")[1].split("\n\n")[0]
    assert "require_permission" not in self_half, (
        "the /self router must NOT carry a permission dependency: an accessor's map is "
        "empty, which has_permission reads as unrestricted, so the gate would be a no-op "
        "that breaks the day an accessor is given an explicit map")


# ── the levels ───────────────────────────────────────────────────────────────

def test_use_is_the_stakeholder_level_and_does_not_imply_write():
    """The whole point: read their POV, tick their use cases, nothing else."""
    u = _user(perms={"pov": ["read", "use"]}, env_ids=["env-mine"])
    assert auth_mod.has_permission(u, "pov", "read")
    assert auth_mod.has_permission(u, "pov", "use")
    assert not auth_mod.has_permission(u, "pov", "write"), (
        "use must not imply write, or the stakeholder can provision and share")
    assert not auth_mod.has_permission(u, "pov", "delete")


def test_the_use_case_routes_are_gated_on_use_and_destroy_on_delete():
    with open(os.path.join(_ROOT, "web_dashboard", "api", "pov.py"), encoding="utf-8") as fh:
        src = fh.read()
    for line in src.split("\n"):
        if "/use-cases/{card_id}" in line and line.strip().startswith("@router."):
            assert "_POV_USE" in line, f"use-case route not on the use level: {line.strip()}"
    destroy = [l for l in src.split("\n")
               if l.strip().startswith('@router.delete("/managed/{env_id}"')]
    assert destroy and "_POV_DELETE" in destroy[0], (
        f"destroy is not on the delete level: {destroy}")


def test_pov_offers_use_in_the_catalog():
    assert "use" in auth_mod.levels_for_scope("pov"), (
        "the pov scope lost its `use` level, so the stakeholder grant is inexpressible")


# ── the accessor boundary ────────────────────────────────────────────────────

def test_an_accessor_is_confined_by_the_path_allowlist_not_by_pov_grants():
    """Granting `pov` to an accessor must widen nothing: get_current_user's allowlist runs
    first and it can only reach /self and /api/auth/me."""
    assert auth_mod._accessor_may_reach("/api/pov/accessor/self")
    assert auth_mod._accessor_may_reach("/api/auth/me")
    assert not auth_mod._accessor_may_reach("/api/pov/managed")
    assert not auth_mod._accessor_may_reach("/api/pov/managed/env-theirs/use-cases")


def test_the_users_api_refuses_pov_grants_on_an_accessor():
    """Two contradictory bindings on one row: accessor_env_id says "exactly this one and
    nothing else", pov_env_ids says "these, among the POV pages you can browse"."""
    with open(os.path.join(_ROOT, "web_dashboard", "api", "users.py"), encoding="utf-8") as fh:
        src = fh.read()
    block = src.split("if body.pov_env_ids is not None:")[1].split("db.commit")[0]
    assert "_refuse_accessor(user)" in block, (
        "PATCH /api/users lets an admin set pov_env_ids on a POV accessor")


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
