"""Handing a POV to another SE: ``POST /api/pov/managed/{env_id}/owner``.

``created_by`` was write-once, and it is the column ``pov_own`` reads, so an SE going on
leave could not pass a POV on -- the new SE had to rebuild it. The handoff only rewrites that
column; what matters, and is tested here, is everything it must refuse BEFORE the write:

  * a recipient who would then get a 404 or 403 opening it -- no POV role, a POV access
    picker that hides it, a different workgroup;
  * a recipient who is not a person: an accessor (confined to one POV's /self surface) or a
    service account;
  * a POV the caller cannot see answers 404, not 403, like every other route on the router.

And that a successful handoff is audited and queues the ``pov.owner_changed`` event, which
must be in the shipped default list -- an event left out of it never delivers.

Runs under pytest, or standalone:
    python tests/test_pov_owner_transfer.py
"""
import os
import sys
import tempfile
import uuid

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="pov-owner-test-"), "test.db")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_TMPDB}")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-pov-owner-transfer")

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover
    print("SKIP: fastapi not installed")
    sys.exit(0)

from web_dashboard import database as d  # noqa: E402

d.Base.metadata.create_all(bind=d.engine)

from web_dashboard.api import pov as pov_api  # noqa: E402
from web_dashboard.api import pov_gates  # noqa: E402
from web_dashboard.api.auth import get_current_user  # noqa: E402
from web_dashboard.services import notify_policy  # noqa: E402

_PRESENTER = {"pov_own": ["read", "write", "delete"]}
_SE = {"pov": ["read", "write"]}


class _Operator:
    username = "operator"
    is_admin = True
    is_effective_admin = True
    effective_permissions_dict = {}
    accessor_env_id = None
    pov_env_ids_list = []
    workgroups_list = []


class _Narrowed(_Operator):
    """A full SE whose picker names some other POV, so this one is invisible to them."""
    is_admin = False
    is_effective_admin = False
    effective_permissions_dict = _SE
    pov_env_ids_list = ["some-other-pov"]


_CALLER = {"user": _Operator()}


def _db():
    db = d.SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _client():
    app = FastAPI()
    app.include_router(pov_api.router)
    app.dependency_overrides[get_current_user] = lambda: _CALLER["user"]
    app.dependency_overrides[d.get_db] = _db
    return TestClient(app, raise_server_exceptions=False)


def _env(workgroup="sales", owner="alice", status="active"):
    db = d.SessionLocal()
    try:
        env = d.PovEnvironment(platform="skytap", name="pov-" + uuid.uuid4().hex[:8],
                               workgroup=workgroup, created_by=owner, status=status)
        db.add(env)
        db.commit()
        return env.id
    finally:
        db.close()


def _person(perms=_SE, workgroups=("sales",), env_ids=None, **extra):
    db = d.SessionLocal()
    try:
        extra.setdefault("is_active", True)
        u = d.User(username="u-" + uuid.uuid4().hex[:8], hashed_password="x", **extra)
        u.workgroups_list = list(workgroups)
        u.permissions_dict = perms
        if env_ids is not None:
            u.pov_env_ids_list = env_ids
        db.add(u)
        db.commit()
        return u.username
    finally:
        db.close()


def _owner_of(env_id):
    db = d.SessionLocal()
    try:
        return db.get(d.PovEnvironment, env_id).created_by
    finally:
        db.close()


def _hand(env_id, username):
    return _client().post(f"/api/pov/managed/{env_id}/owner", json={"username": username})


def setup_function(_fn=None):
    _CALLER["user"] = _Operator()


# ── the happy path ───────────────────────────────────────────────────────────

def test_a_handoff_moves_the_owner_and_is_audited():
    setup_function()
    env_id = _env()
    bob = _person()
    res = _hand(env_id, bob)
    assert res.status_code == 200, res.text
    assert res.json()["environment"]["created_by"] == bob
    assert _owner_of(env_id) == bob
    db = d.SessionLocal()
    try:
        row = (db.query(d.AuditLog).filter(d.AuditLog.action == "pov_owner_changed")
               .order_by(d.AuditLog.id.desc()).first())
        assert row is not None, "the handoff left no audit entry"
        assert env_id in (row.details or "") and bob in (row.details or "")
    finally:
        db.close()


def test_an_own_only_presenter_can_receive_one():
    """Their scope is read off created_by, so an empty picker is no obstacle."""
    setup_function()
    env_id = _env()
    pres = _person(perms=_PRESENTER)
    assert _hand(env_id, pres).status_code == 200
    assert _owner_of(env_id) == pres


# ── refusals, all before the write ───────────────────────────────────────────

def _refused(env_id, username, needle):
    before = _owner_of(env_id)
    res = _hand(env_id, username)
    assert res.status_code == 400, f"{res.status_code}: {res.text}"
    assert needle in res.json()["detail"], res.json()["detail"]
    assert _owner_of(env_id) == before, "a refused handoff still changed the owner"


def test_an_unknown_or_inactive_user_is_refused():
    setup_function()
    env_id = _env()
    _refused(env_id, "nobody-by-this-name", "no active user")
    _refused(env_id, _person(is_active=False), "no active user")


def test_someone_without_a_pov_role_is_refused():
    setup_function()
    _refused(_env(), _person(perms={"pov": ["read"]}), "cannot run POVs")


def test_someone_in_another_workgroup_is_refused():
    setup_function()
    _refused(_env(workgroup="sales"), _person(workgroups=("finance",)), "workgroup")


def test_a_picker_that_hides_the_pov_is_refused():
    setup_function()
    _refused(_env(), _person(env_ids=["a-different-pov"]), "narrowed")


def test_an_accessor_or_a_service_account_is_refused():
    setup_function()
    env_id = _env()
    _refused(env_id, _person(accessor_env_id=env_id), "accessor")
    _refused(env_id, _person(is_service_account=True), "service account")


def test_handing_it_to_the_current_owner_is_refused():
    setup_function()
    bob = _person()
    _refused(_env(owner=bob), bob, "already owns")


def test_a_destroyed_pov_cannot_be_handed_off():
    setup_function()
    res = _hand(_env(status="destroyed"), _person())
    assert res.status_code == 409


def test_a_pov_the_caller_cannot_see_is_a_404():
    """404, not 403: a 403 would confirm the id exists."""
    setup_function()
    env_id = _env()
    _CALLER["user"] = _Narrowed()
    try:
        res = _hand(env_id, _person())
        assert res.status_code == 404, res.status_code
        assert _owner_of(env_id) == "alice"
    finally:
        setup_function()


# ── wiring ───────────────────────────────────────────────────────────────────

def test_the_route_carries_the_per_pov_write_gate():
    route = next(r for r in pov_api.router.routes
                 if getattr(r, "path", "") == "/api/pov/managed/{env_id}/owner")
    deps = [getattr(dep, "call", None) or getattr(dep, "dependency", None)
            for dep in route.dependant.dependencies]
    assert pov_gates.require_write_on_env in deps


def test_the_event_ships_on_and_above_the_default_floor():
    events = notify_policy.parse_event_types(notify_policy.DEFAULT_EVENT_TYPES)
    assert "pov.owner_changed" in events
    assert notify_policy.EVENT_SEVERITY["pov.owner_changed"] in ("warning", "critical")


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
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
