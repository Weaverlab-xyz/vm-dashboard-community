"""Granting read on ONE POV: the object dimension the permission model did not have.

Every scope in ``PERMISSION_SCOPES`` is a feature area, so "this user may read POV *X*"
was not expressible. The only way to confine somebody to one POV was to mint an accessor —
a login bound by ``User.accessor_env_id`` to a five-route ``/self`` surface, which is right
for an anonymous prospect and wrong for a named stakeholder who should also be a real user.

Two axes now, and the separation is the design:

  * ``pov`` scope — what you may DO. ``use`` is the level that ticks a use case,
    deliberately excluding create, destroy, share, power and accessor minting. That is what
    makes "read their POV and check off use cases" a grant rather than a compromise.
    (Powering stays on ``write``: the route carries a runstate, so it suspends and stops as
    readily as it starts.)
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


# ── the page the stakeholder actually opens ──────────────────────────────────
#
# Everything above tests a dependency in isolation, which is how the first cut of this
# feature shipped a grant that was correct at every route and still produced a blank page.
# `templates/pov/index.html` calls GET /api/pov/platforms FIRST and `init()` returns early
# on any non-ok answer, so ONE 403 there costs the whole page: no POV list, no use cases,
# nothing the `pov:use` level was added to allow. These drive the real router.

def _pov_client():
    """The real api/pov.py router, with only the principal faked."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from web_dashboard.database import Base, SessionLocal, engine, get_db
    from web_dashboard.api import pov as pov_api

    Base.metadata.create_all(bind=engine)
    app = FastAPI()
    app.include_router(pov_api.router)

    def _db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    principal = {}
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[auth_mod.get_current_user] = lambda: principal["user"]
    return TestClient(app, raise_server_exceptions=False), principal


# What index.html fetches before it renders anything at all.
_PAGE_BOOTSTRAP = ("/api/pov/platforms", "/api/pov/managed", "/api/pov/managed/archive")


def test_the_stakeholder_can_paint_the_pov_page():
    """A POC stakeholder holding exactly {"pov": ["read","use"]} must get through every
    call the POV list page makes before it renders. `/platforms` is the registry of lab
    platforms this INSTANCE may use -- it names no environment, template or customer -- so
    read is the right level; gating it on write made the page die whole."""
    client, principal = _pov_client()
    principal["user"] = _user(perms={"pov": ["read", "use"]}, env_ids=["env-theirs"])
    for path in _PAGE_BOOTSTRAP:
        res = client.get(path)
        assert res.status_code == 200, (
            f"{path} answered {res.status_code} to a read+use stakeholder: index.html's "
            f"init() returns early on this and never lists a single POV")


def test_the_stakeholder_still_cannot_provision_or_list_the_platform():
    """The other half of the same rule: read+use opens the page and nothing more. The
    pickers stay on write because /environments lists environments this dashboard did not
    create -- other customers' POVs on a shared lab account."""
    client, principal = _pov_client()
    principal["user"] = _user(perms={"pov": ["read", "use"]}, env_ids=["env-theirs"])
    for meth, path in (("GET", "/api/pov/environments"),
                       ("GET", "/api/pov/templates"),
                       ("POST", "/api/pov/managed"),
                       ("POST", "/api/pov/managed/reconcile"),
                       ("POST", "/api/pov/managed/env-theirs/share"),
                       ("DELETE", "/api/pov/managed/env-theirs")):
        res = client.request(meth, path)
        assert res.status_code == 403, f"{meth} {path} answered {res.status_code}, want 403"


def test_a_pov_they_were_not_granted_is_still_404_through_the_real_router():
    """The instance gate, end to end rather than against a fake request. 404 so the id is
    not probeable."""
    client, principal = _pov_client()
    principal["user"] = _user(perms={"pov": ["read", "use"]}, env_ids=["env-theirs"])
    for path in ("/api/pov/managed/env-someone-else",
                 "/api/pov/managed/env-someone-else/summary",
                 "/api/pov/managed/env-someone-else/use-cases"):
        assert client.get(path).status_code == 404, f"{path} leaked a POV or its existence"


def test_platforms_reports_whether_this_caller_may_provision():
    """The page skips the two write-only pickers on this flag instead of firing them and
    rendering the 403 as a page-wide error. It hides UI; every write route still checks."""
    client, principal = _pov_client()
    principal["user"] = _user(perms={"pov": ["read", "use"]}, env_ids=["env-theirs"])
    assert client.get("/api/pov/platforms").json().get("can_provision") is False
    principal["user"] = _user(perms={"pov": ["read", "write", "delete", "use"]})
    assert client.get("/api/pov/platforms").json().get("can_provision") is True
    principal["user"] = _user(admin=True)
    assert client.get("/api/pov/platforms").json().get("can_provision") is True


def test_the_registry_is_read_and_the_pickers_are_write():
    """Pins which of the four platform reads sits at which level, since the difference is
    one `dependencies=` argument and the symptom of getting it wrong is a blank page."""
    with open(os.path.join(_ROOT, "web_dashboard", "api", "pov.py"), encoding="utf-8") as fh:
        lines = fh.read().split("\n")

    def _decorator(path):
        hits = [l for l in lines if l.strip().startswith(f'@router.get("{path}"')]
        assert len(hits) == 1, f"expected exactly one route for {path}, got {hits}"
        return hits[0]

    assert "_POV_WRITE" not in _decorator("/platforms"), (
        "GET /api/pov/platforms is back on pov:write -- it is the first call index.html "
        "makes and init() returns early on a 403, so this blanks the page for every "
        "read-only stakeholder")
    for path in ("/templates", "/environments"):
        assert "_POV_WRITE" in _decorator(path), (
            f"{path} lists platform-side names and must stay on pov:write")


def test_the_platform_environment_id_is_not_called_env_id():
    """`require_pov_env_access` is router-level and keys on path_params["env_id"]. The
    platform's own environment id lives in a different namespace from PovEnvironment
    uuids, so naming it `env_id` made the gate compare the two and 404 every platform
    environment for any narrowed user -- at any level, write included."""
    with open(os.path.join(_ROOT, "web_dashboard", "api", "pov.py"), encoding="utf-8") as fh:
        src = fh.read()
    assert '@router.get("/environments/{platform_env_id}"' in src, (
        "the platform environment route is not on {platform_env_id}")
    assert '@router.get("/environments/{env_id}"' not in src, (
        "the platform environment route took the {env_id} name back, so the instance "
        "gate now compares a lab-platform id against PovEnvironment uuids")


def test_the_page_skips_the_write_only_pickers_without_write():
    """Without this the stakeholder's page loads and then wears 'Could not load
    environments (403)' across the top of it."""
    with open(os.path.join(_ROOT, "web_dashboard", "templates", "pov", "index.html"),
              encoding="utf-8") as fh:
        src = fh.read()
    load_body = src.split("async load() {")[1].split("schedulePoll();")[0]
    assert "canProvision" in load_body, (
        "load() fetches /api/pov/environments and /api/pov/templates unconditionally; "
        "both are pov:write and fetchList renders their 403 as a page error")
    assert "this.canProvision = !!data.can_provision;" in src, (
        "index.html no longer reads can_provision out of /api/pov/platforms")


def test_a_narrowing_outlives_the_permission_map_and_stays_visible():
    """``pov_env_ids`` is its own column and ``pov_env_scope`` reads it without consulting
    the permission map — so a user ticked back to "unrestricted", or stripped of their POV
    row, is STILL confined to the environments named there. The grid must therefore keep
    showing the picker whenever the list is non-empty, or that narrowing is invisible on
    the only page that edits it and cannot be cleared."""
    u = _user(perms={}, env_ids=["env-theirs"])
    assert auth_mod.has_permission(u, "pov", "write"), "empty map should read as unrestricted"
    assert auth_mod.pov_env_scope(u) == {"env-theirs"}, (
        "an unrestricted user with pov_env_ids is still narrowed — that is the point")

    with open(os.path.join(_ROOT, "web_dashboard", "templates", "users", "list.html"),
              encoding="utf-8") as fh:
        src = fh.read()
    assert 'x-show="povNarrowingVisible()"' in src, (
        "the POV picker is gated on something other than povNarrowingVisible()")
    body = src.split("povNarrowingVisible() {")[1].split("},")[0]
    assert "povEnvIds" in body and "length" in body, (
        "povNarrowingVisible() does not show the picker for an already-narrowed user, so "
        "a stale pov_env_ids list is unclearable from the Users page")


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
