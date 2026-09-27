"""Service accounts and the OAuth 2.0 client_credentials grant.

A workload used to authenticate with a PAT minted against an ordinary user row: a person's
long-lived bearer secret on a principal whose empty permission map means UNRESTRICTED.
These tests pin the replacement (services/service_accounts, api/oauth, and the one bearer
resolver in api/auth.resolve_bearer):

  * a service account with nothing granted can reach NOTHING -- the inverse of a person;
  * it can never sign in, never be an administrator, and never hold a password;
  * the token endpoint speaks RFC 6749 (Basic or form client auth, section 5.2 errors);
  * a requested scope can only narrow what the account holds;
  * revoking the client ends tokens it already issued, on REST, WebSocket and /mcp alike;
  * rotation keeps the previous secret working for its grace window and no longer.

Run: python tests/test_service_account_oauth.py   (or under pytest)
"""
import base64
import os
import sys
import tempfile
import uuid
from datetime import datetime, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="sa-oauth-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDB}"
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-service-account-tests")

# Third-party deps probed by name, first-party imported UNGUARDED: a broken first-party
# import must fail this file, not skip it (tests/test_import_guard_narrowness.py).
try:
    import fastapi  # noqa: F401
    import jose  # noqa: F401
except ModuleNotFoundError as exc:  # pragma: no cover -- app deps missing
    try:
        import pytest
        pytest.skip(f"app dependencies unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)

from fastapi import Depends, FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from jose import jwt  # noqa: E402

from web_dashboard.config import settings  # noqa: E402
from web_dashboard.database import (Base, OAuthClient, SessionLocal, User,  # noqa: E402
                                    engine, get_password_hash)
from web_dashboard.api import auth as auth_api  # noqa: E402
from web_dashboard.api import mcp_server, oauth as oauth_api, users as users_api  # noqa: E402
from web_dashboard.api import mfa as mfa_api, tokens as tokens_api  # noqa: E402
from web_dashboard.api import websocket as ws_api  # noqa: E402
from web_dashboard.api.auth import (create_access_token, get_current_user,  # noqa: E402
                                    has_permission, require_admin, require_permission)
from web_dashboard.services import service_accounts  # noqa: E402

Base.metadata.create_all(bind=engine)


# ── Fixtures ─────────────────────────────────────────────────────────────────

def _admin() -> User:
    db = SessionLocal()
    admin = db.query(User).filter(User.username == "_sa_admin").first()
    if not admin:
        admin = User(username="_sa_admin", hashed_password="x", is_admin=True, is_active=True)
        db.add(admin)
        db.commit()
        db.refresh(admin)
    db.expunge(admin)
    db.close()
    return admin


def _app() -> TestClient:
    app = FastAPI()
    app.include_router(oauth_api.router)
    app.include_router(oauth_api.wellknown_router)
    app.include_router(users_api.router)
    app.include_router(auth_api.router)
    app.include_router(tokens_api.router)
    app.include_router(mfa_api.router)

    @app.get("/probe/vms")
    def _vms(user: User = Depends(require_permission("vms", "read"))):
        return {"user": user.username}

    @app.get("/probe/aws")
    def _aws(user: User = Depends(require_permission("aws", "write"))):
        return {"user": user.username}

    @app.get("/probe/me")
    def _me(user: User = Depends(get_current_user)):
        return {"user": user.username}

    admin = _admin()
    app.dependency_overrides[require_admin] = lambda: admin
    return TestClient(app)


_CLIENT = None


def _c() -> TestClient:
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = _app()
    return _CLIENT


def _make_sa(permissions=None) -> dict:
    name = "sa_" + uuid.uuid4().hex[:8]
    r = _c().post("/api/users/service-accounts",
                  json={"username": name, "permissions": permissions})
    assert r.status_code == 201, r.text
    return r.json()


def _make_client(sa_id: str, **kw) -> dict:
    r = _c().post(f"/api/users/{sa_id}/oauth-clients", json={"name": "worker", **kw})
    assert r.status_code == 201, r.text
    return r.json()


def _token(cid: str, secret: str, scope: str = None, basic: bool = True):
    data = {"grant_type": "client_credentials"}
    if scope is not None:
        data["scope"] = scope
    headers = {}
    if basic:
        headers["Authorization"] = "Basic " + base64.b64encode(
            f"{cid}:{secret}".encode()).decode()
    else:
        data.update(client_id=cid, client_secret=secret)
    return _c().post("/api/oauth/token", data=data, headers=headers)


def _bearer(tok: str) -> dict:
    return {"Authorization": f"Bearer {tok}"}


_GRANTS = {"vms": ["read"], "aws": ["read", "write"]}


# ── The principal ────────────────────────────────────────────────────────────

def test_a_service_account_with_nothing_granted_reaches_nothing():
    """The trap this whole feature is arranged around: NULL permissions on a person are
    UNRESTRICTED, and a forgotten workload must not inherit that."""
    sa = _make_sa()
    db = SessionLocal()
    user = db.query(User).filter(User.id == sa["id"]).first()
    assert user.is_service_account
    assert user.permissions is None
    assert not has_permission(user, "vms", "read")
    assert not has_permission(user, "jobs", "read")
    db.close()

    cl = _make_client(sa["id"])
    tok = _token(cl["client_id"], cl["client_secret"]).json()["access_token"]
    assert _c().get("/probe/vms", headers=_bearer(tok)).status_code == 403


def test_a_service_account_is_never_admin_whatever_the_columns_say():
    sa = _make_sa(_GRANTS)
    db = SessionLocal()
    user = db.query(User).filter(User.id == sa["id"]).first()
    user.is_admin = True                     # a hand-written row
    user.jit_permissions_dict = {"is_admin": True}
    db.commit()
    assert user.is_effective_admin is False
    user.is_admin = False
    user.jit_permissions = None
    db.commit()
    db.close()
    r = _c().patch(f"/api/users/{sa['id']}", json={"is_admin": True})
    assert r.status_code == 400, r.text


def test_a_service_account_cannot_be_given_a_password_or_sign_in():
    sa = _make_sa(_GRANTS)
    r = _c().patch(f"/api/users/{sa['id']}", json={"password": "hunter2hunter2"})
    assert r.status_code == 400, r.text
    # Even a row with a hash written behind the page's back cannot sign in.
    db = SessionLocal()
    user = db.query(User).filter(User.id == sa["id"]).first()
    user.hashed_password = get_password_hash("hunter2hunter2")
    user.auth_provider = "local"
    db.commit()
    db.close()
    r = _c().post("/api/auth/login",
                  data={"username": sa["username"], "password": "hunter2hunter2"})
    assert r.status_code == 401, r.text


def test_a_login_jwt_naming_a_service_account_is_refused():
    """Nothing issues one, so one is forged or mis-issued -- and it would skip the client
    check that makes revocation immediate."""
    sa = _make_sa(_GRANTS)
    tok = create_access_token({"sub": sa["username"]})
    assert _c().get("/probe/me", headers=_bearer(tok)).status_code == 401


def test_a_workload_token_cannot_mint_itself_a_standing_credential():
    """A scoped, 15-minute token must not be exchangeable for an unscoped PAT or a FIDO2
    key -- that would trade away both properties the token exists for."""
    sa = _make_sa(_GRANTS)
    cl = _make_client(sa["id"])
    tok = _token(cl["client_id"], cl["client_secret"], scope="vms:read").json()["access_token"]
    r = _c().post("/api/tokens", json={"name": "escape"}, headers=_bearer(tok))
    assert r.status_code == 403, r.text
    r = _c().post("/api/mfa/register/begin", headers=_bearer(tok))
    assert r.status_code == 403, r.text


# ── The token endpoint ───────────────────────────────────────────────────────

def test_basic_and_form_client_auth_both_issue_a_bearer_token():
    sa = _make_sa(_GRANTS)
    cl = _make_client(sa["id"])
    assert cl["client_id"].startswith("vmsa_")
    assert cl["client_secret"].startswith("vmss_")
    for basic in (True, False):
        r = _token(cl["client_id"], cl["client_secret"], basic=basic)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["token_type"] == "Bearer"
        assert body["expires_in"] == service_accounts.DEFAULT_TOKEN_TTL_SECONDS
        assert r.headers.get("cache-control") == "no-store"
        claims = jwt.decode(body["access_token"], settings.jwt_secret_key,
                            algorithms=[settings.jwt_algorithm])
        assert claims["type"] == "workload"
        assert claims["client_id"] == cl["client_id"]
        assert _c().get("/probe/vms", headers=_bearer(body["access_token"])).json() == {
            "user": sa["username"]}


def test_the_secret_is_stored_only_as_a_hash():
    sa = _make_sa(_GRANTS)
    cl = _make_client(sa["id"])
    db = SessionLocal()
    row = db.query(OAuthClient).filter(OAuthClient.id == cl["id"]).first()
    assert row.secret_hash == service_accounts.hash_secret(cl["client_secret"])
    assert cl["client_secret"] not in (row.secret_hash, row.client_id)
    db.close()
    listed = _c().get(f"/api/users/{sa['id']}/oauth-clients").json()
    assert listed and "client_secret" not in listed[0]


def test_bad_credentials_get_an_rfc6749_error():
    sa = _make_sa(_GRANTS)
    cl = _make_client(sa["id"])
    r = _token(cl["client_id"], "vmss_wrong")
    assert r.status_code == 401
    assert r.json()["error"] == "invalid_client"
    assert r.headers.get("www-authenticate", "").startswith("Basic")
    r = _token("vmsa_doesnotexist", cl["client_secret"])
    assert r.status_code == 401 and r.json()["error"] == "invalid_client"
    r = _c().post("/api/oauth/token", data={"grant_type": "password",
                                            "client_id": cl["client_id"],
                                            "client_secret": cl["client_secret"]})
    assert r.status_code == 400 and r.json()["error"] == "unsupported_grant_type"


def test_an_expired_secret_is_refused():
    sa = _make_sa(_GRANTS)
    cl = _make_client(sa["id"])
    db = SessionLocal()
    row = db.query(OAuthClient).filter(OAuthClient.id == cl["id"]).first()
    row.secret_expires_at = datetime.utcnow() - timedelta(minutes=1)
    db.commit()
    db.close()
    assert _token(cl["client_id"], cl["client_secret"]).status_code == 401


def test_a_person_cannot_hold_an_oauth_client():
    db = SessionLocal()
    person = User(username="person_" + uuid.uuid4().hex[:6], hashed_password="x",
                  is_active=True)
    db.add(person)
    db.commit()
    pid = person.id
    db.close()
    r = _c().post(f"/api/users/{pid}/oauth-clients", json={"name": "nope"})
    assert r.status_code == 400, r.text


def test_the_secret_and_token_lifetimes_are_bounded():
    sa = _make_sa(_GRANTS)
    r = _c().post(f"/api/users/{sa['id']}/oauth-clients",
                  json={"name": "w", "secret_days": 10_000})
    assert r.status_code == 400
    r = _c().post(f"/api/users/{sa['id']}/oauth-clients",
                  json={"name": "w", "token_ttl_seconds": 86_400})
    assert r.status_code == 400


def test_the_metadata_document_names_the_token_endpoint():
    r = _c().get("/.well-known/oauth-authorization-server")
    assert r.status_code == 200
    doc = r.json()
    assert doc["token_endpoint"].endswith("/api/oauth/token")
    assert doc["grant_types_supported"] == ["client_credentials"]


# ── Scope ────────────────────────────────────────────────────────────────────

def test_a_requested_scope_narrows_and_never_widens():
    sa = _make_sa(_GRANTS)
    cl = _make_client(sa["id"])
    # Asks for more than it holds: vms:write is clipped, vms:read survives.
    r = _token(cl["client_id"], cl["client_secret"], scope="vms:read vms:write")
    assert r.status_code == 200, r.text
    assert r.json()["scope"] == "vms:read"
    tok = r.json()["access_token"]
    assert _c().get("/probe/vms", headers=_bearer(tok)).status_code == 200
    # The account holds aws:write, but this token did not ask for it.
    assert _c().get("/probe/aws", headers=_bearer(tok)).status_code == 403


def test_a_scope_holding_nothing_is_refused_not_issued():
    sa = _make_sa(_GRANTS)
    cl = _make_client(sa["id"])
    r = _token(cl["client_id"], cl["client_secret"], scope="jobs:read")
    assert r.status_code == 400 and r.json()["error"] == "invalid_scope"
    r = _token(cl["client_id"], cl["client_secret"], scope="nonsense:read")
    assert r.status_code == 400 and r.json()["error"] == "invalid_scope"
    r = _token(cl["client_id"], cl["client_secret"], scope="vms")
    assert r.status_code == 400 and r.json()["error"] == "invalid_scope"


def test_an_empty_scope_claim_is_never_read_as_unscoped():
    """A forged-by-mistake token with ``scope: ""`` must mean nothing, not everything."""
    sa = _make_sa(_GRANTS)
    db = SessionLocal()
    user = db.query(User).filter(User.id == sa["id"]).first()
    user._token_scope = {}
    assert not has_permission(user, "vms", "read")
    db.close()


# ── Revocation, rotation, and every surface ──────────────────────────────────

def test_revoking_the_client_ends_tokens_it_already_issued():
    sa = _make_sa(_GRANTS)
    cl = _make_client(sa["id"])
    tok = _token(cl["client_id"], cl["client_secret"]).json()["access_token"]
    assert _c().get("/probe/vms", headers=_bearer(tok)).status_code == 200
    assert _c().delete(f"/api/users/{sa['id']}/oauth-clients/{cl['id']}").status_code == 200
    assert _c().get("/probe/vms", headers=_bearer(tok)).status_code == 401
    assert _token(cl["client_id"], cl["client_secret"]).status_code == 401


def test_disabling_the_service_account_ends_its_tokens():
    sa = _make_sa(_GRANTS)
    cl = _make_client(sa["id"])
    tok = _token(cl["client_id"], cl["client_secret"]).json()["access_token"]
    _c().patch(f"/api/users/{sa['id']}", json={"is_active": False})
    assert _c().get("/probe/vms", headers=_bearer(tok)).status_code == 401


def test_rotation_keeps_the_old_secret_for_its_grace_window_only():
    sa = _make_sa(_GRANTS)
    cl = _make_client(sa["id"])
    r = _c().post(f"/api/users/{sa['id']}/oauth-clients/{cl['id']}/rotate",
                  json={"grace_minutes": 30})
    assert r.status_code == 200, r.text
    new_secret = r.json()["client_secret"]
    assert new_secret != cl["client_secret"]
    assert _token(cl["client_id"], new_secret).status_code == 200
    assert _token(cl["client_id"], cl["client_secret"]).status_code == 200
    db = SessionLocal()
    row = db.query(OAuthClient).filter(OAuthClient.id == cl["id"]).first()
    row.previous_expires_at = datetime.utcnow() - timedelta(seconds=1)
    db.commit()
    db.close()
    assert _token(cl["client_id"], cl["client_secret"]).status_code == 401
    assert _token(cl["client_id"], new_secret).status_code == 200


def test_rotation_with_no_grace_drops_the_old_secret_at_once():
    sa = _make_sa(_GRANTS)
    cl = _make_client(sa["id"])
    r = _c().post(f"/api/users/{sa['id']}/oauth-clients/{cl['id']}/rotate",
                  json={"grace_minutes": 0})
    assert r.status_code == 200
    assert _token(cl["client_id"], cl["client_secret"]).status_code == 401


class _FakeWS:
    def __init__(self, token):
        self.headers = {"sec-websocket-protocol": f"{ws_api._WS_AUTH_SUBPROTOCOL}, {token}"}


def test_the_same_token_works_on_websocket_and_mcp_and_dies_on_both():
    sa = _make_sa(_GRANTS)
    cl = _make_client(sa["id"])
    tok = _token(cl["client_id"], cl["client_secret"], scope="vms:read").json()["access_token"]

    db = SessionLocal()
    user, proto = ws_api._authenticate(_FakeWS(tok), db)
    db.close()
    assert user is not None and user.username == sa["username"] and proto

    mcp_user = mcp_server._validate_pat(tok)
    assert mcp_user is not None and mcp_user.username == sa["username"]
    # Detached, and the scope still narrows -- the property MCP depends on.
    assert has_permission(mcp_user, "vms", "read")
    assert not has_permission(mcp_user, "aws", "write")

    _c().delete(f"/api/users/{sa['id']}/oauth-clients/{cl['id']}")
    db = SessionLocal()
    assert ws_api._authenticate(_FakeWS(tok), db) == (None, None)
    db.close()
    assert mcp_server._validate_pat(tok) is None


def test_pats_still_work_everywhere():
    """The resolver was unified, not replaced: a person's PAT is untouched."""
    db = SessionLocal()
    person = User(username="pat_" + uuid.uuid4().hex[:6], hashed_password="x",
                  is_active=True)
    db.add(person)
    db.commit()
    pid = person.id
    db.close()
    raw = _c().post(f"/api/users/{pid}/tokens", json={"name": "ci"}).json()["token"]
    assert _c().get("/probe/me", headers=_bearer(raw)).status_code == 200
    assert mcp_server._validate_pat(raw) is not None
    db = SessionLocal()
    assert ws_api._authenticate(_FakeWS(raw), db)[0] is not None
    db.close()


def test_a_persons_login_jwt_still_works():
    db = SessionLocal()
    person = User(username="jwt_" + uuid.uuid4().hex[:6], hashed_password="x",
                  is_active=True)
    db.add(person)
    db.commit()
    name = person.username
    db.close()
    tok = create_access_token({"sub": name})
    assert _c().get("/probe/me", headers=_bearer(tok)).json() == {"user": name}


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {type(e).__name__}: {e}")
    sys.exit(1 if failures else 0)
