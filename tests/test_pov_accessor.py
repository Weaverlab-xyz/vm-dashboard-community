"""POV accessors: a login for somebody outside the account, confined and reaped.

Every other principal in this codebase belongs to the operator. An accessor is a
prospect's ephemeral login, so the assertions here are about **what it cannot do**, and
almost all of them are silent when they break: nothing errors, nothing looks wrong, and a
person outside the account quietly has more than they should.

The one that matters most is the first:

  * **An accessor cannot be confined with permissions.** `effective_permissions_dict`
    returns {} for a user none of the three permission columns says anything about, and
    `require_permission` reads {} as UNRESTRICTED — deliberate backward compatibility for
    the users that predate those columns. So "an accessor with no permissions" is an
    administrator. The confinement is a path ALLOWLIST in `api/auth.get_current_user`, and
    an allowlist rather than a denylist so a route added tomorrow is refused by default.
  * **Three surfaces resolve users without going through that dependency**, so each is
    closed where it lives: `api/websocket._authenticate` (its own resolver),
    `api/entitle_rest` (a shared secret, and it can grant `dashboard:admin` — an accessor
    reaching its actor lookup is a direct escalation), and `/api/users` (an admin PATCH
    setting `is_admin` or clearing the binding).
  * **`delete_actor` may only ever delete what this feature minted.** The prefix guard is
    checked on the name in the REQUEST, before any lookup, exactly as
    `functions/fnworkloads/db_grant.py` guards its own.
  * **An accessor cannot outlive its POV**, whether or not Entitle ever calls back and
    whether or not the auto-delete timer was ever turned on.

Route-order assertions are here too: `/pov/access` and `/pov/templates` are literal
segments that `/pov/{env_id}` would otherwise capture, which fails as "No such POV
environment" with nothing else looking wrong.

Runs under pytest, or standalone:
    python tests/test_pov_accessor.py
"""
import ast
import os
import re
import sys
import tempfile
import uuid
from datetime import datetime, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="pov-accessor-test-"), "test.db")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_TMPDB}")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-pov-accessor")

_SVC = os.path.join(_ROOT, "web_dashboard", "services")
_API = os.path.join(_ROOT, "web_dashboard", "api")
_TPL = os.path.join(_ROOT, "web_dashboard", "templates")
_MAIN = os.path.join(_ROOT, "web_dashboard", "main.py")
_AUTH = os.path.join(_API, "auth.py")
_SERVICE = os.path.join(_SVC, "pov_accessor_service.py")
_REST = os.path.join(_API, "pov_accessor_rest.py")
_ACCESS_PAGE = os.path.join(_TPL, "pov", "access.html")
_APP_JS = os.path.join(_ROOT, "web_dashboard", "static", "js", "app.js")


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _code(path, name=""):
    """A module's, or one function's, source with docstrings and comments removed.

    Every assertion below is about what the code DOES, and these files argue for
    themselves at length — the first draft of four of these tests failed because a
    docstring said "never a password" and the test read it as serving one. `ast.unparse`
    drops comments for free; the docstring is dropped explicitly.
    """
    tree = ast.parse(_read(path))
    if name:
        node = next((n for n in ast.walk(tree)
                     if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                     and n.name == name), None)
        assert node is not None, f"{os.path.basename(path)} has no {name}()"
        body = list(node.body)
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                and isinstance(body[0].value.value, str):
            body = body[1:]
        return "\n".join(ast.unparse(stmt) for stmt in body)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
                             ast.Module)):
            b = getattr(node, "body", [])
            if b and isinstance(b[0], ast.Expr) and isinstance(b[0].value, ast.Constant) \
                    and isinstance(b[0].value.value, str):
                node.body = b[1:]
    return ast.unparse(tree)


def _markup(path):
    """An HTML template with its Jinja and HTML comments removed, for the same reason."""
    src = _read(path)
    src = re.sub(r"\{#.*?#\}", "", src, flags=re.S)
    return re.sub(r"<!--.*?-->", "", src, flags=re.S)


try:
    from fastapi.testclient import TestClient
    from web_dashboard.database import (PovAccessor, PovEnvironment, SessionLocal, User,
                                        get_password_hash, init_db)
    from web_dashboard.services import (config_service, pov_accessor_service,
                                        pov_share)
except Exception as exc:  # pragma: no cover — app deps missing
    try:
        import pytest
        pytest.skip(f"app dependencies unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)


_REST_SECRET = "test-entitle-ephemeral-secret"
_STANDING_SECRET = "test-entitle-standing-secret"
_SE_PASSWORD = "operator-password"

_state = {}


def _setup():
    """One POV, one operator, one app. Built once and reused by every behavioural test."""
    if _state:
        return _state
    init_db()
    config_service.set("install_profile", "pov")
    config_service.set("pov_environments_enabled", "1")
    config_service.set("setup_complete", "1")
    config_service.set("pov_accessor_rest_secret", _REST_SECRET)
    config_service.set("entitle_rest_secret", _STANDING_SECRET)
    config_service.set("entitle_user_jit_enabled", "1")

    from web_dashboard import main

    db = SessionLocal()
    env_id = str(uuid.uuid4())
    db.add(PovEnvironment(id=env_id, platform="skytap", name="acme-pov",
                          status="active", platform_environment_id="4242",
                          ps_tenant_id="t-ps", pra_tenant_id="t-pra"))
    if not db.query(User).filter(User.username == "se@example").first():
        db.add(User(id="admin-1", username="se@example", is_admin=True, is_active=True,
                    hashed_password=get_password_hash(_SE_PASSWORD)))
    db.commit()
    db.close()

    client = TestClient(main.app)
    token = client.post("/api/auth/login",
                        data={"username": "se@example", "password": _SE_PASSWORD})
    _state.update(client=client, env_id=env_id,
                  se={"Authorization": "Bearer " + token.json()["access_token"]},
                  entitle={"Authorization": f"Bearer {_REST_SECRET}"},
                  standing={"Authorization": f"Bearer {_STANDING_SECRET}"})
    return _state


def _mint(**body):
    """Mint an accessor through the SE route and log it in. Returns (headers, payload)."""
    s = _setup()
    r = s["client"].post(f"/api/pov/managed/{s['env_id']}/accessors",
                         json=body or {"email": "dana@customer.example"}, headers=s["se"])
    assert r.status_code == 201, r.text
    data = r.json()
    login = s["client"].post("/api/auth/login",
                             data={"username": data["accessor"]["username"],
                                   "password": data["password"]})
    assert login.status_code == 200, login.text
    return {"Authorization": "Bearer " + login.json()["access_token"]}, data


# ── the trap this whole design exists to avoid ───────────────────────────────

def test_an_accessor_with_no_permissions_is_not_unrestricted():
    """THE regression test. require_permission reads an empty permission map as full
    access, so an accessor confined "by having no permissions" would be an administrator on
    every route in the dashboard."""
    s = _setup()
    headers, data = _mint()

    db = SessionLocal()
    user = db.query(User).filter(User.username == data["accessor"]["username"]).first()
    assert user is not None
    assert user.permissions is None, "the accessor was given a permission map"
    assert user.effective_permissions_dict == {}, "the premise of this test has changed"
    db.close()

    # An empty map means unrestricted to require_permission — and yet:
    for path in ("/api/pov/managed", "/api/users", "/api/jobs", "/api/inventory"):
        assert s["client"].get(path, headers=headers).status_code == 403, \
            f"an accessor reached {path}"


def test_the_confinement_is_an_allowlist_not_a_denylist():
    """A route added tomorrow must be refused by default. Source-parsed, because a denylist
    would pass every behavioural test written today and fail on the next merge."""
    src = _read(_AUTH)
    assert "_ACCESSOR_ALLOWED_PREFIXES" in src
    block = src.split("_ACCESSOR_ALLOWED_PREFIXES = (", 1)[1].split(")", 1)[0]
    for word in ("DENY", "denied_prefixes", "BLOCKED"):
        assert word not in src, f"api/auth.py carries a {word} list as well"
    assert "/api/pov/accessor/self" in block and "/api/auth/me" in block
    # Nothing else. Each addition should be a deliberate act with a test beside it.
    assert len([ln for ln in block.splitlines() if '"' in ln]) == 2, \
        f"the accessor allowlist has grown: {block}"


def test_the_gate_lives_in_the_one_dependency_every_route_resolves():
    src = _read(_AUTH)
    body = src.split("async def get_current_user(", 1)[1].split("\ndef ", 1)[0]
    assert "request: Request" in body, "get_current_user cannot see the path it is guarding"
    assert "accessor_env_id" in body and "_accessor_may_reach" in body, \
        "get_current_user does not confine an accessor"


def test_require_admin_refuses_an_accessor_whatever_the_flags_say():
    src = _read(_AUTH)
    body = src.split("def require_admin(", 1)[1].split("\ndef ", 1)[0]
    assert "accessor_env_id" in body, \
        "require_admin does not refuse an accessor; a single allowlisted admin route " \
        "would then be an escalation"

    s = _setup()
    headers, data = _mint()
    db = SessionLocal()
    user = db.query(User).filter(User.username == data["accessor"]["username"]).first()
    user.is_admin = True          # the worst case, forced
    db.commit()
    db.close()
    assert s["client"].get("/api/users", headers=headers).status_code == 403, \
        "an accessor with is_admin reached an admin route"


def test_an_accessor_reaches_its_own_pov_and_only_that():
    s = _setup()
    headers, _data = _mint()
    r = s["client"].get("/api/pov/accessor/self", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["environment"]["id"] == s["env_id"]
    # The checklist is the same catalog the SE sees — that is the point of the feature.
    assert body["use_cases"]["groups"], "the accessor's view carries no use cases"


def test_the_self_route_takes_no_environment_id():
    """No path parameter means no ownership check for a later edit to forget."""
    tree = ast.parse(_read(os.path.join(_API, "pov_accessor.py")))
    node = next(n for n in ast.walk(tree)
                if isinstance(n, ast.AsyncFunctionDef) and n.name == "accessor_self")
    args = [a.arg for a in node.args.args]
    assert "env_id" not in args, f"/self takes an environment id from the caller: {args}"
    # Because it resolves the POV from the session instead.
    assert "accessor_env_id" in _code(_SERVICE, "environment_of")


def test_an_accessor_cannot_write_the_checklist_through_the_se_route():
    """Slice 3 gives them their own write path. Until then the SE route must refuse."""
    s = _setup()
    headers, _ = _mint()
    r = s["client"].post(
        f"/api/pov/managed/{s['env_id']}/use-cases/pov-security-who-has-access",
        json={"state": "done"}, headers=headers)
    assert r.status_code == 403


# ── the surfaces that do not resolve through the gate ────────────────────────

def test_the_standing_entitle_adapter_cannot_see_an_accessor():
    """The sharpest escalation path: that adapter grants dashboard permissions up to
    administrator, and resolves an actor by scanning users and matching a name."""
    s = _setup()
    _headers, data = _mint()
    username = data["accessor"]["username"]

    actors = s["client"].get("/api/entitle/rest/get_actors", headers=s["standing"])
    assert actors.status_code == 200, actors.text
    assert username not in [a["identifier"] for a in actors.json()["data"]["actors"]]

    granted = s["client"].post(
        "/api/entitle/rest/give_access", headers=s["standing"],
        json={"asset": {"identifier": "dashboard:admin"},
              "actor_identifier": username, "role_code": "admin"})
    assert granted.status_code == 404, \
        f"Entitle granted an accessor dashboard admin: {granted.text}"


def test_the_standing_adapter_filters_on_the_query_not_the_response():
    """Filtering after the fact leaves the next route that forgets to do it open."""
    src = _read(os.path.join(_API, "entitle_rest.py"))
    assert "_NOT_AN_ACCESSOR" in src
    for route in ("get_actors", "get_all_permissions", "_apply", "check_config"):
        body = src.split(f"def {route}(", 1)[1].split("\n@", 1)[0]
        assert "_NOT_AN_ACCESSOR" in body, f"{route} does not exclude accessors"


def test_the_websocket_resolver_rejects_an_accessor():
    """It is deliberately not get_current_user, so the path allowlist does not reach it —
    and job Live Output is the last thing a prospect should be able to tail."""
    src = _read(os.path.join(_API, "websocket.py"))
    body = src.split("def _authenticate(", 1)[1].split("\nclass ", 1)[0]
    assert body.count("accessor_env_id") >= 2, \
        "the websocket resolver does not refuse an accessor on both the JWT and PAT paths"


def test_an_accessor_is_hidden_from_the_users_page_and_cannot_be_edited_there():
    s = _setup()
    _headers, data = _mint()
    username = data["accessor"]["username"]

    users = s["client"].get("/api/users", headers=s["se"])
    assert users.status_code == 200, users.text
    assert username not in [u["username"] for u in users.json()]

    db = SessionLocal()
    row = db.query(User).filter(User.username == username).first()
    user_id = row.id
    db.close()
    # A PATCH here is the escalation: is_admin true, or accessor_env_id cleared.
    assert s["client"].patch(f"/api/users/{user_id}", json={"is_admin": True},
                             headers=s["se"]).status_code == 409
    assert s["client"].delete(f"/api/users/{user_id}",
                              headers=s["se"]).status_code == 409
    assert s["client"].post(f"/api/users/{user_id}/tokens", json={"name": "x"},
                            headers=s["se"]).status_code == 409


# ── the Entitle ephemeral adapter ────────────────────────────────────────────

def test_the_adapter_has_its_own_secret_and_fails_closed():
    code = _code(_REST, "_require_secret")
    assert "pov_accessor_rest_secret" in code, \
        "the ephemeral adapter does not read its own secret"
    assert "entitle_rest_secret" not in code, \
        "the ephemeral adapter reads the standing adapter's secret"
    assert "503" in code, "an unconfigured adapter is not closed"
    assert "compare_digest" in code, "the secret is compared without hmac"

    s = _setup()
    assert s["client"].post("/api/pov/accessor/rest/create_actor",
                            json={}).status_code == 401
    assert s["client"].post("/api/pov/accessor/rest/create_actor", json={},
                            headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_the_adapter_serves_only_the_ephemeral_route_set():
    """Entitle never calls give_access in Ephemeral mode — create_actor IS the grant. A
    route here that implied otherwise would describe a lifecycle Entitle will not run."""
    src = _read(_REST)
    for route in ("get_assets", "get_all_permissions", "create_actor", "delete_actor",
                  "check_config"):
        assert f'@router.post("/{route}"' in src or f'@router.get("/{route}"' in src, \
            f"the adapter is missing {route}"
    for absent in ("give_access", "revoke_access", "get_actors"):
        assert f'"/{absent}"' not in src, f"the ephemeral adapter serves /{absent}"


def test_create_actor_reads_the_identity_out_of_provisioning_data():
    """Ephemeral mode puts it there, not in `actor`. Reading only `actor` is what made
    every real ephemeral grant come back 400 in the sibling adapter."""
    s = _setup()
    r = s["client"].post("/api/pov/accessor/rest/create_actor", headers=s["entitle"],
                         json={"provisioning_data": {"email": "eve@customer.example"},
                               "asset": {"identifier": f"pov:{s['env_id']}"}})
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["username"].startswith(pov_accessor_service.USERNAME_PREFIX)
    assert data["password"], "create_actor returned no credentials to hand the requester"


def test_create_actor_needs_an_asset_naming_a_live_pov():
    s = _setup()
    for payload in ({"provisioning_data": {"email": "x@y.z"}},
                    {"provisioning_data": {"email": "x@y.z"},
                     "asset": {"identifier": "pov:does-not-exist"}}):
        r = s["client"].post("/api/pov/accessor/rest/create_actor",
                             headers=s["entitle"], json=payload)
        assert r.status_code == 404, f"{payload} was accepted"
    r = s["client"].post("/api/pov/accessor/rest/create_actor", headers=s["entitle"],
                         json={"asset": {"identifier": f"pov:{s['env_id']}"}})
    assert r.status_code == 400, "an actor with no identity was accepted"


def test_delete_actor_refuses_anything_it_did_not_mint():
    """The only thing between this integration and an operator's account."""
    s = _setup()
    r = s["client"].post("/api/pov/accessor/rest/delete_actor", headers=s["entitle"],
                         json={"actor_identifier": "se@example"})
    assert r.status_code == 403, f"delete_actor accepted an operator's username: {r.text}"

    db = SessionLocal()
    assert db.query(User).filter(User.username == "se@example").first() is not None
    db.close()


def test_delete_actor_checks_the_prefix_before_any_lookup():
    """Checked on the name in the REQUEST, so a row that disagreed could not talk it into
    a delete either — the same ordering db_grant uses."""
    src = _read(_REST)
    body = src.split("async def delete_actor(", 1)[1].split("\n@", 1)[0]
    guard = body.index("is_accessor_username")
    lookup = body.index("by_username")
    assert guard < lookup, "delete_actor looks the row up before checking the prefix"


def test_delete_actor_is_idempotent():
    """Entitle retries, and a delete that errors leaves a live login behind."""
    s = _setup()
    r = s["client"].post("/api/pov/accessor/rest/create_actor", headers=s["entitle"],
                         json={"provisioning_data": {"email": "gone@customer.example"},
                               "asset": {"identifier": f"pov:{s['env_id']}"}})
    username = r.json()["data"]["username"]
    first = s["client"].post("/api/pov/accessor/rest/delete_actor", headers=s["entitle"],
                             json={"actor_identifier": username})
    second = s["client"].post("/api/pov/accessor/rest/delete_actor", headers=s["entitle"],
                              json={"actor_identifier": username})
    assert first.json()["data"]["deleted"] is True
    assert second.status_code == 200 and second.json()["data"]["deleted"] is False


# ── lifetime ─────────────────────────────────────────────────────────────────

def test_an_accessor_cannot_outlive_its_pov():
    _setup()          # the schema; idempotent
    db = SessionLocal()
    env = PovEnvironment(id=str(uuid.uuid4()), platform="skytap", name="short-pov",
                         status="active",
                         expires_at=datetime.utcnow() + timedelta(days=3))
    db.add(env)
    db.commit()
    row, _pw = pov_accessor_service.mint(db, env, email="a@b.c", days=60)
    assert row.expires_at <= env.expires_at, \
        "an accessor was given longer than the POV it can reach"
    db.close()


def test_an_accessor_is_refused_for_a_pov_that_is_going_away():
    _setup()          # the schema; idempotent
    db = SessionLocal()
    env = PovEnvironment(id=str(uuid.uuid4()), platform="skytap", name="dying-pov",
                         status="destroying")
    db.add(env)
    db.commit()
    try:
        pov_accessor_service.mint(db, env, email="a@b.c")
        raise AssertionError("minted a login into a POV being destroyed")
    except pov_accessor_service.AccessorError:
        pass
    finally:
        db.close()


def test_the_sweep_reaps_expired_accessors_and_orphaned_ones():
    _setup()          # the schema; idempotent
    db = SessionLocal()
    live = PovEnvironment(id=str(uuid.uuid4()), platform="skytap", name="sweep-live",
                          status="active")
    dead = PovEnvironment(id=str(uuid.uuid4()), platform="skytap", name="sweep-dead",
                          status="active")
    db.add_all([live, dead])
    db.commit()

    stale, _ = pov_accessor_service.mint(db, live, email="stale@x.y")
    orphan, _ = pov_accessor_service.mint(db, dead, email="orphan@x.y")
    keep, _ = pov_accessor_service.mint(db, live, email="keep@x.y")

    stale.expires_at = datetime.utcnow() - timedelta(minutes=1)
    dead.status = "destroyed"
    db.commit()

    reaped = pov_accessor_service.sweep(db)
    assert reaped >= 2, f"the sweep revoked {reaped}"
    for row_id, gone in ((stale.id, True), (orphan.id, True), (keep.id, False)):
        row = pov_accessor_service.get(db, row_id)
        assert bool(row.revoked_at) is gone, f"{row.username}: revoked={bool(row.revoked_at)}"
        user = db.query(User).filter(User.username == row.username).first()
        assert (user is None) is gone, f"{row.username}: the login outlived the revoke"
    db.close()


def test_revoking_deletes_the_login_rather_than_deactivating_it():
    """An inactive row is still a username every actor lookup has to remember to skip."""
    src = _read(_SERVICE)
    body = src.split("def revoke(", 1)[1].split("\ndef ", 1)[0]
    assert ".delete()" in body, "revoke does not delete the user row"
    assert "is_active" not in body, "revoke deactivates instead of deleting"


def test_teardown_runs_before_the_share_link_and_removes_every_login():
    _setup()
    src = _read(os.path.join(_SVC, "pov_env_service.py"))
    body = src.split("async def run_env_destroy(", 1)[1]
    accessors = body.index("pov_accessor_service.teardown")
    share = body.index("pov_share.teardown")
    assert accessors < share, \
        "the share link is torn down before the accessor logins; the credential into THIS " \
        "dashboard is the one whose window should be shortest"

    db = SessionLocal()
    env = PovEnvironment(id=str(uuid.uuid4()), platform="skytap", name="teardown-pov",
                         status="active")
    db.add(env)
    db.commit()
    pov_accessor_service.mint(db, env, email="one@x.y")
    pov_accessor_service.mint(db, env, email="two@x.y")
    line = pov_accessor_service.teardown(db, env)
    assert "2" in line, line
    assert db.query(User).filter(User.accessor_env_id == env.id).count() == 0
    assert db.query(PovAccessor).filter(PovAccessor.environment_id == env.id,
                                        PovAccessor.revoked_at.is_(None)).count() == 0
    db.close()


def test_the_sweep_rides_the_reconcile_pass_outside_its_platform_loop():
    """A fresh POV instance starts with the auto-delete timer off, and whether a login
    should still work has nothing to do with whether the lab platform is reachable."""
    src = _read(os.path.join(_SVC, "pov_reconcile.py"))
    body = src.split("async def run_reconcile(", 1)[1].split("\ndef ", 1)[0]
    assert "pov_accessor_service.sweep" in body, "the reconcile pass does not sweep"
    assert body.index("pov_accessor_service.sweep") < body.index("for platform in"), \
        "the accessor sweep is inside the platform loop, so it is skipped when the lab " \
        "platform is unconfigured"


def test_the_login_password_is_returned_once_and_never_served_again():
    """Two different secrets live near each other here, and only one is re-readable.

    The accessor's LOGIN password is minted, handed over once and never stored in readable
    form — an accessor that lost it is replaced. The LAB LINK's password is a different
    thing entirely: `pov_share` stores it deliberately, because an SE reads it to a
    customer days later, and the accessor is that customer. So a reveal route existing is
    correct; a reveal route for the LOGIN password would not be.
    """
    api = os.path.join(_API, "pov_accessor.py")
    for route in ("list_accessors", "revoke_accessor", "accessor_self"):
        assert "password" not in _code(api, route), f"{route} can serve a password"
    # Exactly one route hands back a login password, and it is the one that created it.
    assert "password" in _code(api, "create_accessor")
    assert "password" not in _code(_SERVICE, "describe_one"), \
        "the accessor projection carries a password"
    # The one reveal route there is reads pov_share's stored link password, and nothing
    # in the accessor service can reach a login password to serve.
    reveal = _code(_SERVICE, "reveal_share_password")
    assert "pov_share.reveal_password" in reveal
    assert "hashed_password" not in reveal and "get_password_hash" not in reveal


def test_revealing_the_lab_link_password_is_audited_like_the_operator_s_own():
    """It is a second door onto a live credential, so "who has this link's password" has
    to stay answerable — the reason api/pov.reveal_share_password is a POST and audited."""
    reveal = _code(_SERVICE, "reveal_share_password")
    assert "log_audit" in reveal, "an accessor can read the link password unaudited"
    assert "pov_share_password_revealed" in reveal, \
        "the audit action does not match the operator-side reveal, so the two cannot be " \
        "read as one record"
    src_api = _read(os.path.join(_API, "pov_accessor.py"))
    assert '@self_router.post("/self/share/reveal")' in src_api, \
        "the reveal is not a POST; a GET lands in history and is prefetchable"

    # Behavioural, because the source check above passed while the function raised a
    # NameError on every call — a reveal route that 500s is not an audited one.
    from web_dashboard.database import AuditLog
    s = _setup()
    db = SessionLocal()
    env = db.query(PovEnvironment).filter(PovEnvironment.id == s["env_id"]).first()
    env.share_url = "https://lab.example/portal/xyz"
    db.commit()
    db.close()
    config_service.set(pov_share.password_config_key(s["env_id"]), "L1nkPassword")

    headers, data = _mint()
    r = s["client"].post("/api/pov/accessor/self/share/reveal", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["password"] == "L1nkPassword"

    db = SessionLocal()
    entry = (db.query(AuditLog)
               .filter(AuditLog.action == "pov_share_password_revealed",
                       AuditLog.username == data["accessor"]["username"]).first())
    db.close()
    assert entry is not None, "the accessor read the link password with no audit entry"


# ── routing, mounting and the page ───────────────────────────────────────────

def test_the_literal_pov_routes_are_declared_before_the_parameterised_one():
    """Starlette matches in declaration order. Reversed, /pov/access 404s as "No such POV
    environment" — the most alarming possible message for the one audience outside the
    account."""
    src = _read(_MAIN)
    param = src.index('@app.get("/pov/{env_id}"')
    for literal in ('@app.get("/pov/templates"', '@app.get("/pov/access"'):
        assert src.index(literal) < param, f"{literal} is declared after /pov/{{env_id}}"


def test_the_accessor_routes_carry_the_pov_gate():
    src = _read(_MAIN)
    block = src.split("from .api import pov_accessor as", 1)[1].split("except ImportError", 1)[0]
    for router in ("pov_accessor_api.router", "pov_accessor_api.self_router",
                   "pov_accessor_rest_api.router"):
        assert router in block, f"{router} is not mounted"
    assert block.count('_feature_gate("pov_environments_enabled")') >= 3, \
        "an accessor router is mounted without the POV gate"
    assert 'dependencies=[_feature_gate("pov_environments_enabled")]' in \
        src.split('@app.get("/pov/access"', 1)[1].split("async def", 1)[0]


def test_the_machine_caller_is_never_handed_a_redirect_to_the_wizard():
    src = _read(_MAIN)
    block = src.split("_SETUP_503_PREFIXES = (", 1)[1].split(")", 1)[0]
    assert "/api/pov/accessor/rest" in block, \
        "Entitle would read a 302-to-HTML as an integration failure"


def test_the_accessor_page_is_standalone_and_carries_no_nav():
    src = _markup(_ACCESS_PAGE)
    assert "{% extends" not in src, \
        "the accessor page extends base.html, which renders every nav link in the app"
    assert "_nav_links" not in src, "the accessor page includes the dashboard nav"
    assert "/api/pov/accessor/self" in src
    # No card on it may be a link: every target on the SE's page is a screen an accessor
    # is refused on.
    assert not re.search(r'<a[^>]*:href="c\.target"', src), \
        "a card on the accessor page links to a dashboard screen"


def test_the_client_side_redirect_is_documented_as_a_convenience():
    """It reads localStorage, which the holder can edit. If a later reader believes it is
    the control, the real one becomes deletable."""
    src = _read(_APP_JS)
    block = src.split("function requireAuth()", 1)[1].split("\n}", 1)[0]
    assert "isAccessor" in block, "requireAuth does not send an accessor to its own page"
    assert "not a control" in block.lower() or "convenience" in block.lower(), \
        "the redirect does not say it is not the security control"


def test_the_access_tab_exists_on_the_pov_page():
    src = _read(os.path.join(_TPL, "pov", "detail.html"))
    block = src.split("tabs: [", 1)[1].split("],", 1)[0]
    assert "'access'" in block, "the POV page has no Access tab"
    assert "mintAccessor" in src and "revokeAccessor" in src


# ── slice 3: what the accessor may do ────────────────────────────────────────

def test_no_accessor_write_route_takes_an_environment_id():
    """The property that makes "could they write to somebody else's POV?" unanswerable
    rather than merely guarded. Every route under /self resolves the POV from the session,
    so there is no ownership check that a later edit could forget."""
    tree = ast.parse(_read(os.path.join(_API, "pov_accessor.py")))
    checked = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        decorators = ast.unparse(ast.Module(body=[], type_ignores=[])) if False else \
            " ".join(ast.unparse(d) for d in node.decorator_list)
        if "self_router" not in decorators:
            continue
        checked += 1
        args = [a.arg for a in node.args.args]
        assert "env_id" not in args, f"{node.name} takes an environment id: {args}"
        # And it gets its POV from one of the two session-resolving helpers, both of
        # which read `accessor_env_id` and take no id from the caller.
        body = ast.unparse(node)
        assert any(h in body for h in ("_accessor_env", "env_for_writes", "self_view")), \
            f"{node.name} resolves its POV some other way"
    assert checked >= 4, f"only {checked} accessor routes were checked"


def test_the_write_routes_needed_no_widening_of_the_allowlist():
    """The allowlisted prefix is /self, and everything under it is env-from-session by
    construction. A write placed anywhere else would have had to widen that list — which is
    exactly the moment somebody should stop and think."""
    block = _read(_AUTH).split("_ACCESSOR_ALLOWED_PREFIXES = (", 1)[1].split(")", 1)[0]
    assert len([ln for ln in block.splitlines() if '"' in ln]) == 2, \
        f"the accessor allowlist grew for slice 3: {block}"
    assert "/api/pov/accessor/self" in block


def test_an_accessor_can_tick_a_card_and_it_is_recorded_as_theirs():
    s = _setup()
    headers, _ = _mint()
    card = "pov-security-who-has-access"
    r = s["client"].post(f"/api/pov/accessor/self/use-cases/{card}",
                         json={"state": "done"}, headers=headers)
    assert r.status_code == 200, r.text
    progress = r.json()["progress"]
    assert progress["state"] == "done"
    assert progress["by_kind"] == "accessor", \
        "the customer's tick is indistinguishable from the SE's"
    # And the SE sees it, marked as theirs.
    se = s["client"].get(f"/api/pov/managed/{s['env_id']}/use-cases", headers=s["se"])
    seen = [c for g in se.json()["groups"] for c in g["use_cases"] if c["id"] == card][0]
    assert seen["progress"]["by_kind"] == "accessor"

    # Un-ticking is a toggle, and doing it twice is not an error.
    for expected in (True, False):
        d = s["client"].delete(f"/api/pov/accessor/self/use-cases/{card}", headers=headers)
        assert d.status_code == 200 and d.json()["cleared"] is expected


def test_an_accessor_cannot_write_an_unknown_card():
    s = _setup()
    headers, _ = _mint()
    r = s["client"].post("/api/pov/accessor/self/use-cases/not-a-card",
                         json={"state": "done"}, headers=headers)
    assert r.status_code == 400
    # A demo card id is not a back door either — those name pages a POV cannot reach.
    r = s["client"].post("/api/pov/accessor/self/use-cases/cloudops-three-layers",
                         json={"state": "done"}, headers=headers)
    assert r.status_code == 400


def test_a_note_does_not_silently_mark_a_card_covered():
    """A comment is not a verdict. Defaulting one would put a claim in the customer's mouth
    because they typed in the box under it."""
    s = _setup()
    headers, _ = _mint()
    card = "pov-security-teardown-proof"
    r = s["client"].post(f"/api/pov/accessor/self/use-cases/{card}",
                         json={"state": "", "note": "could not get to this"},
                         headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["progress"]["state"] == "", "a note marked the card"
    assert r.json()["progress"]["note"] == "could not get to this"
    # And it counts as neither done nor skipped.
    before = r.json()["summary"]
    assert before["done"] == 0 and before["skipped"] == 0

    src = _read(_ACCESS_PAGE)
    body = src.split("async saveNote(", 1)[1].split("\n        },", 1)[0]
    assert "|| 'done'" not in body, "the page defaults a note-only save to done"


def test_an_operator_tick_does_not_erase_the_customers_note():
    """THE data-loss regression. Two people write these rows; the SE's tick button sends a
    state and no note, and a note written unconditionally would wipe the one piece of
    evidence in this feature that cannot be reconstructed."""
    s = _setup()
    headers, _ = _mint()
    card = "pov-cloudops-reap"
    s["client"].post(f"/api/pov/accessor/self/use-cases/{card}",
                     json={"state": "", "note": "this is what sold it for us"},
                     headers=headers)

    se = s["client"].post(f"/api/pov/managed/{s['env_id']}/use-cases/{card}",
                          json={"state": "done"}, headers=s["se"])
    assert se.status_code == 200, se.text
    assert se.json()["progress"]["note"] == "this is what sold it for us", \
        "the operator's tick erased the customer's note"
    assert se.json()["progress"]["state"] == "done"

    # And "" still clears deliberately.
    cleared = s["client"].post(f"/api/pov/managed/{s['env_id']}/use-cases/{card}",
                               json={"state": "done", "note": ""}, headers=s["se"])
    assert cleared.json()["progress"]["note"] == ""


def test_the_accessor_sees_the_lab_link_but_not_its_internal_id():
    s = _setup()
    db = SessionLocal()
    env = db.query(PovEnvironment).filter(PovEnvironment.id == s["env_id"]).first()
    env.share_url = "https://lab.example/portal/abc"
    env.share_id = "publish-set-99"
    db.commit()
    db.close()

    headers, _ = _mint()
    body = s["client"].get("/api/pov/accessor/self", headers=headers).json()
    assert body["share"]["url"] == "https://lab.example/portal/abc"
    assert "publish-set-99" not in str(body), \
        "the accessor is served the publish set's id, which is an id in the lab account"
    assert "password" not in body["share"], "the page ships the link password with the HTML"


def test_the_wired_view_names_kinds_of_access_never_artifact_ids():
    """Built FOR this audience rather than by subtracting from _serialize: those per-VM ids
    live inside a CUSTOMER'S appliance and are meaningful only to teardown."""
    s = _setup()
    db = SessionLocal()
    from web_dashboard.database import PovEnvironmentVM
    db.add(PovEnvironmentVM(environment_id=s["env_id"], platform_vm_id="vm-9",
                            name="win-1", os_family="windows", private_ip="10.9.9.9",
                            pra_jump_id="JUMP-SECRET-ID",
                            ps_managed_account_id="ACCT-SECRET-ID",
                            entitle_integration_id="INT-SECRET-ID",
                            wiring_error="an operator-facing failure"))
    db.commit()
    db.close()

    headers, _ = _mint()
    body = s["client"].get("/api/pov/accessor/self", headers=headers).json()
    vm = [v for v in body["wired"] if v["name"] == "win-1"][0]
    assert vm["brokered_session"] and vm["vaulted_credential"] and vm["requestable_access"]
    blob = str(body)
    for leaked in ("JUMP-SECRET-ID", "ACCT-SECRET-ID", "INT-SECRET-ID",
                   "an operator-facing failure"):
        assert leaked not in blob, f"the accessor view leaks {leaked!r}"


def test_the_accessor_page_renders_the_note_as_text_not_html():
    """It is prose typed by somebody outside the account, and it is rendered back on the
    operator's page."""
    for path in (_ACCESS_PAGE, os.path.join(_TPL, "pov", "detail.html")):
        src = _markup(path)
        assert "x-html" not in src, f"{os.path.basename(path)} uses x-html"
    detail = _markup(os.path.join(_TPL, "pov", "detail.html"))
    assert 'x-text="c.progress.note"' in detail, \
        "the operator's page does not show the customer's note"


def test_the_accessor_page_still_offers_no_link_into_the_dashboard():
    """Every card target on the operator's page is a screen an accessor is refused on."""
    src = _markup(_ACCESS_PAGE)
    assert not re.search(r'<a[^>]*:href="c\.target"', src)
    assert 'href="/pov' not in src and 'href="/api' not in src


# ── slice 2b: registering the adapter with Entitle ───────────────────────────

def test_register_rest_names_the_tenant_in_both_halves():
    """The gap 2b exists to close, and it is two gaps that look like one.

    Without a ctx, `register_rest` authenticates with the GLOBAL Entitle key AND writes the
    global owner/workflow ids into the HCL. Threading it through only one of them is worse
    than neither: you authenticate as one tenant and name another's ids.
    """
    ers_path = os.path.join(_SVC, "entitle_registration_service.py")
    code = _code(ers_path, "register_rest")
    assert "ctx=ctx" in code or "_apply_hcl_sync, hcl" in code
    assert "_hcl_fields(ctx)" in code, \
        "register_rest does not name the tenant's owner/workflow in the HCL"
    assert code.rstrip().endswith("ctx)"), \
        "register_rest does not apply with the tenant's key"

    # And the generator actually passes them on, rather than accepting and dropping them.
    gen = _code(ers_path, "_generate_rest_hcl")
    assert "fields=fields" in gen, "_generate_rest_hcl drops the tenant fields"


def test_the_rest_path_does_not_demand_the_ssh_connectors_prerequisites():
    """An SSH connector reaches a private target from INSIDE the network, so it needs an
    agent token and a key. A REST adapter is called by Entitle over public HTTPS and needs
    neither — demanding them would block a registration on prerequisites that do not apply.
    """
    wireup = os.path.join(_SVC, "pov_wireup.py")
    shared = _code(wireup, "entitle_tenant_ctx")
    # It resolves the tenant and the two ids every integration needs, and refuses on
    # nothing else.
    assert "owner_id" in shared and "workflow_id" in shared
    for refusal in ("names no agent token", "no Entitle SSH key", "names no SSH sudo user"):
        assert refusal not in shared, \
            f"the shared tenant context refuses with {refusal!r}, which is SSH-specific"

    # And the SSH path still demands all three, layered on top rather than duplicated.
    ssh = _code(wireup, "entitle_context")
    assert "entitle_tenant_ctx" in ssh, "entitle_context re-resolves the tenant itself"
    for refusal in ("names no agent token", "no Entitle SSH key", "names no SSH sudo user"):
        assert refusal in ssh, f"the SSH path stopped refusing with {refusal!r}"


def test_the_registration_is_refused_before_anything_is_created():
    """A half-succeeded registration leaves an integration in a customer's tenant that this
    dashboard has no state for and cannot remove."""
    from web_dashboard.services import pov_accessor_entitle as pae
    s = _setup()
    db = SessionLocal()
    env = db.query(PovEnvironment).filter(PovEnvironment.id == s["env_id"]).first()

    # No Entitle tenant on this POV.
    env.entitle_tenant_id = None
    db.commit()
    assert "Entitle tenant" in pae.blocker(db, env)

    # Tenant, but the adapter is closed because its secret is unset.
    env.entitle_tenant_id = "t-entitle"
    db.commit()
    config_service.set("pov_accessor_rest_secret", "")
    assert "pov_accessor_rest_secret" in pae.blocker(db, env)

    # Secret, but this instance does not know its own public URL.
    config_service.set("pov_accessor_rest_secret", _REST_SECRET)
    config_service.set("public_base_url", "")
    assert "public URL" in pae.blocker(db, env)

    # A URL, but plaintext — Entitle will not call it.
    config_service.set("public_base_url", "http://dash.internal")
    assert "HTTPS" in pae.blocker(db, env)

    # All four satisfied.
    config_service.set("public_base_url", "https://dash.example")
    assert pae.blocker(db, env) == ""
    assert pae.endpoint().startswith("https://dash.example/api/pov/accessor/rest")
    db.close()


def test_the_blocker_reaches_the_row_and_the_page_shows_it_instead_of_a_button():
    from web_dashboard.services import pov_accessor_entitle as pae
    s = _setup()
    db = SessionLocal()
    env = db.query(PovEnvironment).filter(PovEnvironment.id == s["env_id"]).first()
    described = pae.describe(db, env)
    db.close()
    for key in ("accessor_registered", "accessor_endpoint", "accessor_blocker",
                "accessor_integration_id"):
        assert key in described, f"describe() omits {key}"

    src = _markup(os.path.join(_TPL, "pov", "detail.html"))
    assert 'x-text="env.accessor_blocker"' in src, "the page never shows the blocker"
    panel = src.split("Let Entitle mint them", 1)[1].split("</div>", 30)[0]
    assert 'x-show="!env.accessor_blocker"' in src, \
        "the register button is not hidden when something blocks it"


def test_the_integration_is_torn_down_before_the_logins_it_mints():
    """While it is live, Entitle can mint a NEW accessor — so removing the logins first
    races a destroy against a grant. Shut the tap, then drain."""
    src = _read(os.path.join(_SVC, "pov_env_service.py"))
    body = src.split("async def run_env_destroy(", 1)[1]
    integration = body.index("pov_accessor_entitle.teardown")
    logins = body.index("pov_accessor_service.teardown")
    share = body.index("pov_share.teardown")
    assert integration < logins < share, (
        "destroy order is wrong: the Entitle integration must go before the accessor "
        "logins, and both before the share link")


def test_a_failed_deregistration_keeps_the_state_so_a_re_run_can_finish_it():
    """Clearing it optimistically is how an integration in a customer's tenant becomes
    unreachable from here — the rule the per-VM wire-up teardown already follows."""
    code = _code(os.path.join(_SVC, "pov_accessor_entitle.py"), "teardown")
    assert "accessor_tf_state = None" not in code, \
        "teardown clears the terraform state on the failure path"
    assert "WARNING" in code, "a failed teardown says nothing in the job log"


def test_the_open_question_is_recorded_where_the_person_who_can_answer_it_looks():
    """Entitle's Ephemeral-mode discriminator is unconfirmed. The operator registering the
    integration is the one who can check, so the page says so, not only the design doc."""
    svc = _read(os.path.join(_SVC, "pov_accessor_entitle.py"))
    assert "unconfirmed" in svc.lower()
    page = _markup(os.path.join(_TPL, "pov", "detail.html"))
    assert "Ephemeral Accounts" in page, \
        "the page does not ask the operator to confirm the connection mode"

def _fn_code(path, name):
    """A function's executable source: no comments, no docstring.

    Through `ast`, because this codebase documents the calls it deliberately does not make
    — "never suspend and never destroy" — and a raw-text scan reads the warning as the bug,
    then passes again the day somebody makes it.
    """
    for node in ast.walk(ast.parse(_read(path))):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            body = node.body
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                body = body[1:]
            return "\n".join(ast.unparse(s) for s in body).replace("'", '"')
    raise AssertionError(f"no function named {name} in {path}")


# ── the Wake button ──────────────────────────────────────────────────────────
#
# The other half of the suspend schedule: a POV that sleeps at 19:00 and cannot be woken
# by the person evaluating it is a broken demo.

class _WakeRow:
    """A POV, as `wake_view` reads one."""

    def __init__(self, runstate="stopped", status="active", platform="aws",
                 cap=None, spent=0.0, capped_at=None):
        self.id = "env-wake"
        self.name = "acme-pov"
        self.platform = platform
        self.status = status
        self.runstate = runstate
        self.workgroup = None
        self.spend_cap_usd = cap
        self.spend_estimate_usd = spent
        self.spend_accrued_at = None
        self.spend_warned_at = None
        self.spend_capped_at = capped_at


def _wake_view(row, in_flight=False):
    """`wake_view` with the one database question stubbed."""
    from web_dashboard.services import pov_accessor_service, pov_env_service
    original = pov_env_service.power_job_in_flight
    pov_env_service.power_job_in_flight = lambda db, env_id: in_flight
    try:
        return pov_accessor_service.wake_view(None, row)
    finally:
        pov_env_service.power_job_in_flight = original


def test_a_suspended_pov_can_be_woken_by_its_accessor():
    view = _wake_view(_WakeRow(runstate="stopped"))
    assert view["can_wake"] is True, view["reason"]
    assert view["running"] is False


def test_a_running_pov_offers_no_button():
    view = _wake_view(_WakeRow(runstate="running"))
    assert view["can_wake"] is False and view["running"] is True


def test_a_pov_already_starting_says_so_rather_than_queueing_a_second_job():
    """Two power jobs for one environment is a race over its runstate, and a prospect
    pressing a button that appears to do nothing will press it again."""
    view = _wake_view(_WakeRow(runstate="stopped"), in_flight=True)
    assert view["can_wake"] is False
    assert view["starting"] is True
    assert "few minutes" in view["reason"]


def test_a_pov_over_its_spend_cap_is_not_wakeable():
    """The important refusal. The cap LATCHES once it has acted, so the sweep will not
    re-suspend — which means a prospect who woke a capped POV would leave it running past
    its cap indefinitely, and the account owner's one cost control would be the one
    anybody could undo."""
    from datetime import datetime as _dt
    view = _wake_view(_WakeRow(runstate="stopped", cap=100.0, spent=120.0,
                               capped_at=_dt(2026, 9, 1)))
    assert view["can_wake"] is False
    assert "spend limit" in view["reason"]
    assert "your contact" in view["reason"], "the refusal gives the prospect no next step"


def test_a_pov_under_its_cap_is_still_wakeable():
    view = _wake_view(_WakeRow(runstate="stopped", cap=100.0, spent=40.0))
    assert view["can_wake"] is True, view["reason"]


def test_a_pov_that_is_not_actionable_is_refused_with_the_lifecycle_reason():
    for status in ("provisioning", "destroying", "destroyed"):
        view = _wake_view(_WakeRow(runstate="stopped", status=status))
        assert view["can_wake"] is False, f"{status} was wakeable"
        assert view["reason"], f"{status} refused with no reason"


def test_the_accessor_can_only_start_and_never_stop_or_destroy():
    """An accessor is a prospect with a login into one environment. The whole surface they
    have is this one verb."""
    body = _fn_code(_SERVICE, "wake")
    assert '"runstate": "running"' in body, "wake does not ask for a start"
    for banned in ("stopped", "suspend", "destroy"):
        assert banned not in body.lower(), f"the accessor wake path can {banned}"


def test_waking_enqueues_the_same_job_the_se_button_does():
    """One teardown path, one power path. The action gets a /jobs row, Live Output and a
    place in the failed-jobs panel like any other."""
    body = _fn_code(_SERVICE, "wake")
    assert 'job_type="pov_env_power"' in body


def test_a_customer_start_is_attributed_to_its_own_actor():
    """The /jobs row should say a prospect pressed the button, not name the SE who set the
    POV up — the same reason `pov-schedule` and `pov-spend-cap` have their own."""
    from web_dashboard.services import pov_accessor_service
    assert pov_accessor_service.WAKE_ACTOR == "pov-accessor"
    body = _fn_code(_SERVICE, "wake")
    assert "created_by=WAKE_ACTOR" in body


def test_the_page_renders_the_button_from_the_state_the_api_served():
    """Not probed. A control that fails when pressed is worse than one that explains
    itself, and the prospect is the audience least able to interpret a failure."""
    page = _read(_ACCESS_PAGE)
    assert "wake.can_wake" in page, "the page does not read the served wake state"
    assert "/api/pov/accessor/self/wake" in page
    assert "wake.reason" in page, "a refusal is never shown to the prospect"


def test_the_page_offers_no_way_to_suspend_or_destroy():
    page = _read(_ACCESS_PAGE)
    for banned in ("self/power", "self/destroy", "'stopped'", '"stopped"'):
        assert banned not in page, f"the accessor page offers {banned}"


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
