"""Dashboard user identity as an Entitle Remote Adapter.

The one integration the dashboard hosts itself, because here the dashboard IS the
target system. It replaces the Entra-group indirection, which had two costs: it only
worked for Entra (local and other-OIDC users could be granted nothing), and grants
and revokes both took effect only at the user's NEXT LOGIN.

The properties pinned here are the ones that would be dangerous or silent:

  * a grant must survive the user's next login
  * Entitle must not be able to revoke what it did not grant
  * the endpoint must be closed, not open, when unconfigured
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

os.environ.setdefault("JWT_SECRET_KEY", "test-secret-entitle-rest")

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
except ImportError:
    print("SKIP: fastapi not installed")
    sys.exit(0)

from web_dashboard.database import User, get_db
from web_dashboard.api import entitle_rest
from web_dashboard.services import config_service

SECRET = "entitle-shared-secret"


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *_a, **_k):
        return self

    def all(self):
        return list(self._rows)

    def count(self):
        return len(self._rows)


class _FakeSession:
    def __init__(self, users):
        self.users = users
        self.commits = 0

    def query(self, _model):
        return _FakeQuery(self.users)

    def commit(self):
        self.commits += 1


def _user(username="alice", email="alice@example.com", **kw):
    user = User(username=username, email=email)
    user.is_active = True
    user.is_admin = False
    for key, value in kw.items():
        setattr(user, key, value)
    return user


def _client(users, secret=SECRET):
    app = FastAPI()
    app.include_router(entitle_rest.router)
    session = _FakeSession(users)
    app.dependency_overrides[get_db] = lambda: session
    if secret:
        config_service.set("entitle_rest_secret", secret)
    else:
        # DELETE, not set(""): config_service persists to the encrypted app_config
        # table, and writing an empty value does not necessarily clear a previous
        # one — so the "unconfigured" test would otherwise see whatever an earlier
        # test left behind and never exercise the closed path at all.
        config_service.delete("entitle_rest_secret")
    return TestClient(app), session


def _hdr(secret=SECRET):
    return {"Authorization": f"Bearer {secret}"}


# ── Auth: closed, not open ───────────────────────────────────────────────────

def test_the_endpoint_is_closed_when_no_secret_is_configured():
    """Never serve an unauthenticated grant endpoint just because it is
    unconfigured — a 503 says 'not set up', which is true and actionable."""
    client, _ = _client([_user()], secret="")
    resp = client.get("/api/entitle/rest/get_assets", headers=_hdr())
    assert resp.status_code == 503, resp.text
    body = json.dumps(resp.json())
    assert "entitle_rest_not_configured" in body, body


def test_missing_and_wrong_secrets_are_indistinguishable():
    client, _ = _client([_user()])
    missing = client.get("/api/entitle/rest/get_assets")
    wrong = client.get("/api/entitle/rest/get_assets", headers=_hdr("nope"))
    assert missing.status_code == wrong.status_code == 401
    assert missing.json() == wrong.json()


def test_either_header_carries_the_secret():
    """Entitle's headers config is free-form; both spellings must work."""
    client, _ = _client([_user()])
    assert client.get("/api/entitle/rest/get_assets", headers=_hdr()).status_code == 200
    assert client.get("/api/entitle/rest/get_assets",
                      headers={"X-Entitle-Secret": SECRET}).status_code == 200


def test_no_write_route_is_reachable_without_the_secret():
    client, _ = _client([_user()])
    for path in ("/give_access", "/revoke_access", "/check_config"):
        resp = client.post(f"/api/entitle/rest{path}", json={})
        assert resp.status_code == 401, path


# ── The contract ─────────────────────────────────────────────────────────────

def test_assets_are_scopes_plus_administrator():
    client, _ = _client([_user()])
    assets = client.get("/api/entitle/rest/get_assets", headers=_hdr()).json()["data"]["assets"]
    identifiers = {a["identifier"] for a in assets}
    assert "dashboard:scope:aws" in identifiers
    assert "dashboard:admin" in identifiers
    scope_asset = next(a for a in assets if a["identifier"] == "dashboard:scope:aws")
    assert {o["code"] for o in scope_asset["role_options"]} == set(entitle_rest.PERMISSION_LEVELS)


def test_actors_include_local_users_not_just_entra_ones():
    """The whole point: the Entra-group path could only ever grant to Entra users."""
    client, _ = _client([_user("localbob", "bob@example.com")])
    actors = client.get("/api/entitle/rest/get_actors", headers=_hdr()).json()["data"]["actors"]
    assert [a["identifier"] for a in actors] == ["localbob"]


def test_check_config_reports_the_scopes_it_serves():
    client, _ = _client([_user()])
    data = client.post("/api/entitle/rest/check_config", json={"config": {}},
                       headers=_hdr()).json()["data"]
    assert data["valid"] is True
    assert "aws" in data["scopes"]


# ── Grant and revoke ─────────────────────────────────────────────────────────

def _give(client, actor="alice", scope="aws", role="write"):
    return client.post("/api/entitle/rest/give_access", headers=_hdr(), json={
        "asset": {"identifier": f"dashboard:scope:{scope}"},
        "actor_identifier": actor, "role_code": role})


def _take(client, actor="alice", scope="aws", role="write"):
    return client.post("/api/entitle/rest/revoke_access", headers=_hdr(), json={
        "asset": {"identifier": f"dashboard:scope:{scope}"},
        "actor_identifier": actor, "role_code": role})


def test_a_grant_lands_in_the_effective_permissions():
    user = _user()
    client, _ = _client([user])
    assert _give(client).status_code == 200
    assert user.jit_permissions_dict == {"aws": ["write"]}
    assert "write" in user.effective_permissions_dict["aws"]


def test_a_grant_survives_the_users_next_login():
    """_complete_oauth_login OVERWRITES session_permissions on every login. Writing
    the grant there would silently wipe it — which is why jit_permissions exists."""
    user = _user()
    client, _ = _client([user])
    _give(client)
    # Simulate exactly what the OIDC login path does.
    user.session_permissions_dict = {"gcp": ["read"]}
    assert "write" in user.effective_permissions_dict["aws"], \
        "the Entitle grant did not survive a login"
    assert "read" in user.effective_permissions_dict["gcp"]


def test_revoke_removes_only_the_granted_level():
    user = _user()
    client, _ = _client([user])
    _give(client, role="read")
    _give(client, role="write")
    _take(client, role="write")
    assert user.jit_permissions_dict == {"aws": ["read"]}


def test_revoking_the_last_level_drops_the_scope_entirely():
    user = _user()
    client, _ = _client([user])
    _give(client, role="write")
    _take(client, role="write")
    assert user.jit_permissions_dict == {}


def test_entitle_cannot_revoke_what_it_did_not_grant():
    """An operator's own admin grant, and anything an Entra group conferred, must
    survive an Entitle revoke."""
    user = _user()
    user.permissions_dict = {"aws": ["write"]}
    user.session_permissions_dict = {"aws": ["delete"]}
    client, _ = _client([user])
    _take(client, role="write")
    _take(client, role="delete")
    assert user.permissions_dict == {"aws": ["write"]}
    assert user.session_permissions_dict == {"aws": ["delete"]}
    assert "write" in user.effective_permissions_dict["aws"]
    assert "delete" in user.effective_permissions_dict["aws"]


def test_granting_twice_is_idempotent_and_reports_no_change():
    user = _user()
    client, _ = _client([user])
    assert _give(client).json()["data"]["changed"] is True
    assert _give(client).json()["data"]["changed"] is False
    assert user.jit_permissions_dict == {"aws": ["write"]}


def test_revoking_what_is_not_held_is_success_not_a_404():
    """Entitle retries, and a revoke that errors leaves standing access behind."""
    user = _user()
    client, _ = _client([user])
    resp = _take(client)
    assert resp.status_code == 200 and resp.json()["data"]["changed"] is False


def test_admin_is_its_own_asset_and_round_trips():
    user = _user()
    client, _ = _client([user])
    grant = client.post("/api/entitle/rest/give_access", headers=_hdr(), json={
        "asset": {"identifier": "dashboard:admin"},
        "actor_identifier": "alice", "role_code": "admin"})
    assert grant.status_code == 200
    assert user.is_effective_admin is True
    client.post("/api/entitle/rest/revoke_access", headers=_hdr(), json={
        "asset": {"identifier": "dashboard:admin"},
        "actor_identifier": "alice", "role_code": "admin"})
    assert user.is_effective_admin is False


def test_an_operator_set_admin_flag_survives_an_entitle_revoke():
    user = _user()
    user.is_admin = True
    client, _ = _client([user])
    client.post("/api/entitle/rest/revoke_access", headers=_hdr(), json={
        "asset": {"identifier": "dashboard:admin"},
        "actor_identifier": "alice", "role_code": "admin"})
    assert user.is_effective_admin is True, "Entitle revoked an operator's admin flag"


def test_an_actor_resolves_by_username_or_email_case_insensitively():
    user = _user("Alice", "Alice@Example.com")
    client, _ = _client([user])
    for identifier in ("alice", "ALICE", "alice@example.com", "Alice@Example.COM"):
        assert _give(client, actor=identifier).status_code == 200, identifier


# ── Refusals ─────────────────────────────────────────────────────────────────

def test_unknown_actor_scope_and_role_are_refused():
    client, _ = _client([_user()])
    assert _give(client, actor="nobody").status_code == 404
    assert _give(client, scope="not_a_scope").status_code == 404
    assert _give(client, role="superuser").status_code == 400
    assert client.post("/api/entitle/rest/give_access", headers=_hdr(), json={
        "asset": {"identifier": "something:else"},
        "actor_identifier": "alice", "role_code": "read"}).status_code == 404


def test_get_all_permissions_reports_only_entitles_own_grants():
    """Reporting the baseline or group-derived sets would invite Entitle to
    reconcile away access it never granted."""
    user = _user()
    user.permissions_dict = {"aws": ["delete"]}
    user.session_permissions_dict = {"gcp": ["read"]}
    client, _ = _client([user])
    _give(client, scope="azure", role="write")
    data = client.get("/api/entitle/rest/get_all_permissions",
                      headers=_hdr()).json()["data"]
    roles = {p["role_code"] for p in data["actors_permissions"]}
    assert roles == {"write"}, roles


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as exc:
            failures += 1
            print(f"FAIL {fn.__name__}: {exc}")
    sys.exit(1 if failures else 0)
