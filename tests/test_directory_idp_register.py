"""Registering and using a cloud identity provider (Entra ID, Okta, PingOne) as a
managed directory.

What these pin:

- idp_problem refuses every malformed registration, including a plaintext secret where
  a vault ref belongs and an Okta endpoint that is not an Okta org (SSRF guard);
- register_idp signs in and reads BEFORE it commits, so a failed test leaves no row;
- a duplicate tenant / org is refused;
- the row and to_dict carry only the credential's KIND, never the ref or a secret;
- the dashboard_azure mode takes its tenant from the token;
- membership writes are refused while writes are off and for groups the provider owns,
  and are audited when they happen;
- the routes are directories:read for browsing and directories:write for changes, and
  another user's directory is a 404;
- unregistering forgets the cached header.

Real temp SQLite; HTTP through httpx.MockTransport; vault reads stubbed.

Run: python tests/test_directory_idp_register.py   (or under pytest)
"""
import asyncio
import base64
import json
import os
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="idp-reg-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDB}"
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-idp-register-tests")

import httpx  # noqa: E402

from web_dashboard.database import AuditLog, Base, ManagedDirectory, SessionLocal, engine  # noqa: E402
from web_dashboard.services import config_service, directory_idp  # noqa: E402
from web_dashboard.services import directory_service as ds  # noqa: E402
from web_dashboard.services.directory_idp import base  # noqa: E402

Base.metadata.create_all(bind=engine)

TENANT = "11111111-2222-3333-4444-555555555555"
GID = "99999999-8888-7777-6666-555555555555"
UID = "12345678-1234-1234-1234-123456789abc"
REF = "bt_safe://Dashboard/entra-secret"

config_service.resolve_reference = lambda ref, workgroup=None: (
    "resolved-secret" if ref.startswith("bt_safe://") else "")


def _run(coro):
    return asyncio.run(coro)


def _jwt(claims):
    def b64(o):
        return base64.urlsafe_b64encode(json.dumps(o).encode()).decode().rstrip("=")
    return f"{b64({'alg': 'none'})}.{b64(claims)}.sig"


GOOD_ROLES = ["User.Read.All", "Group.Read.All", "GroupMember.ReadWrite.All"]


class _Graph:
    """A small Graph: one tenant, one editable group, one dynamic group."""

    def __init__(self, roles=None, tid=TENANT):
        self.roles = GOOD_ROLES if roles is None else roles
        self.tid = tid
        self.calls = []

    def __call__(self, req):
        self.calls.append(req)
        if req.url.host == "login.microsoftonline.com":
            return httpx.Response(200, json={"access_token": _jwt({"roles": self.roles,
                                                                    "tid": self.tid}),
                                             "expires_in": 3600})
        path = req.url.path
        if path == f"/v1.0/groups/{GID}":
            return httpx.Response(200, json={"id": GID, "displayName": "Lab Admins",
                                             "groupTypes": [], "securityEnabled": True})
        if path.startswith("/v1.0/groups/") and path.count("/") == 3:
            return httpx.Response(200, json={"id": path.rsplit("/", 1)[-1],
                                             "displayName": "All Staff",
                                             "groupTypes": ["DynamicMembership"]})
        if path.endswith("/members/$ref"):
            return httpx.Response(204)
        if path == "/v1.0/users":
            return httpx.Response(200, json={"value": [{"id": UID, "displayName": "Ann"}]})
        return httpx.Response(200, json={"value": []})


def _install(handler):
    base.transport = httpx.MockTransport(handler)
    return handler


def _refuses(fn, needle):
    try:
        fn()
    except ds.DirectoryError as e:
        assert needle in str(e), str(e)
    else:
        raise AssertionError(f"accepted; expected a refusal mentioning {needle!r}")


def _clear():
    db = SessionLocal()
    db.query(ManagedDirectory).delete()
    db.commit()
    db.close()


def _register(db, **kw):
    args = dict(provider="entra_id", tenant_id=TENANT, client_id="app-1",
                credentials_ref=REF, created_by="alice")
    args.update(kw)
    return _run(ds.register_idp(db, **args))


def test_idp_problem_refusals():
    _clear()
    db = SessionLocal()
    p = lambda **kw: ds.idp_problem(db, **kw)  # noqa: E731
    assert "provider must be" in p(provider="google")
    assert "needs tenant id" in p(provider="entra_id", client_id="c", credentials_ref=REF)
    assert "GUID" in p(provider="entra_id", tenant_id="contoso", client_id="c",
                       credentials_ref=REF)
    assert "signs in with" in p(provider="okta", endpoint="https://a.okta.com",
                                auth_mode="client_secret", credentials_ref=REF)
    assert "org URL" in p(provider="okta", endpoint="https://10.0.0.1", credentials_ref=REF)
    assert "org URL" in p(provider="okta", endpoint="http://a.okta.com", credentials_ref=REF)
    assert "needs client id" in p(provider="okta", endpoint="https://a.okta.com",
                                  auth_mode="private_key_jwt", credentials_ref=REF)
    assert "region" in p(provider="pingone", endpoint="example.org", tenant_id=TENANT,
                         client_id="c", credentials_ref=REF)
    assert "UUID" in p(provider="pingone", endpoint="eu", tenant_id="env", client_id="c",
                       credentials_ref=REF)
    assert "unknown Okta option" in p(provider="okta", endpoint="https://a.okta.com",
                                      credentials_ref=REF, options={"password": "x"})
    assert "credential is required" in p(provider="entra_id", tenant_id=TENANT,
                                         client_id="c")
    assert "not the secret itself" in p(provider="entra_id", tenant_id=TENANT,
                                        client_id="c", credentials_ref="hunter2")
    assert p(provider="entra_id", tenant_id=TENANT, client_id="c", credentials_ref=REF) == ""
    assert p(provider="entra_id", auth_mode="dashboard_azure") == ""
    assert p(provider="okta", endpoint="acme.okta.com", auth_mode="ssws",
             managed_account={"system_id": 1, "account_id": 2}) == ""
    db.close()


def test_register_tests_first_and_stores_no_secret():
    _clear()
    _install(_Graph())
    db = SessionLocal()
    try:
        row, result = _register(db, name="Contoso")
        assert result["ok"] and row.status == "available" and row.cloud == "saas"
        assert row.credentials_ref == REF and not row.writes_enabled
        out = ds.to_dict(row)
        assert out["credential_kind"] == "vault" and out["is_idp"] is True
        blob = json.dumps(out)
        assert REF not in blob and "resolved-secret" not in blob
        assert out["provider_label"] == "Microsoft Entra ID"
        _refuses(lambda: _register(db), "already registered")
        assert row.id not in directory_idp._cache
    finally:
        base.transport = None
        db.close()


def test_failed_test_leaves_no_row():
    _clear()
    _install(_Graph(roles=[]))
    db = SessionLocal()
    try:
        _refuses(lambda: _register(db), "User.Read.All")
        _install(lambda req: httpx.Response(401, json={"error": "invalid_client"}))
        _refuses(lambda: _register(db), "HTTP 401")
        assert db.query(ManagedDirectory).count() == 0
    finally:
        base.transport = None
        db.close()


def test_psmanaged_credential_is_checked_out_not_stored():
    _clear()
    from web_dashboard.services import btapi_service
    orig = btapi_service.get_ps_credential_with_request
    seen = []

    async def fake(system_id, account_id, duration_min=30, uses_ssh_key=False):
        seen.append((system_id, account_id))
        return 7, "okta-token"
    btapi_service.get_ps_credential_with_request = fake
    calls = []

    def okta_api(req):
        calls.append(req)
        return httpx.Response(200, json=[])
    _install(okta_api)
    db = SessionLocal()
    try:
        row, _ = _register(db, provider="okta", endpoint="acme.okta.com", tenant_id="",
                           client_id="", auth_mode="ssws", credentials_ref="",
                           managed_account={"system_id": 5, "account_id": 9,
                                            "account_name": "okta-api"})
        assert seen == [(5, 9)]
        assert calls[0].headers["Authorization"] == "SSWS okta-token"
        assert row.endpoint == "https://acme.okta.com" and row.name == "acme.okta.com"
        assert row.credentials_ref.startswith("psmanaged:")
        assert "okta-token" not in (row.credentials_ref or "")
        out = ds.to_dict(row)
        assert out["credential_kind"] == "psmanaged" and out["bind_account"] == "okta-api"
    finally:
        btapi_service.get_ps_credential_with_request = orig
        base.transport = None
        db.close()


def test_dashboard_azure_reads_the_tenant_from_the_token():
    _clear()
    from web_dashboard.services import azure_service
    origs = (azure_service._ensure_creds, azure_service._to_thread)

    class _Tok:
        token = _jwt({"roles": GOOD_ROLES, "tid": TENANT})
        expires_on = 4102444800

    class _Cred:
        def get_token(self, scope):
            assert scope == "https://graph.microsoft.com/.default"
            return _Tok()

    async def ensure():
        return _Cred(), "sub"

    async def to_thread(fn, *a, **k):
        return fn(*a, **k)
    azure_service._ensure_creds, azure_service._to_thread = ensure, to_thread
    _install(_Graph())
    db = SessionLocal()
    try:
        row, _ = _register(db, tenant_id="", client_id="", credentials_ref="",
                           auth_mode="dashboard_azure")
        assert row.tenant_id == TENANT and row.credentials_ref is None
        assert ds.to_dict(row)["credential_kind"] == "dashboard_azure"
        _refuses(lambda: _register(db, tenant_id="", client_id="", credentials_ref="",
                                   auth_mode="dashboard_azure"), "already registered")
        _refuses(lambda: _register(db, tenant_id="22222222-2222-3333-4444-555555555555",
                                   client_id="", credentials_ref="",
                                   auth_mode="dashboard_azure"), "signs in to tenant")
    finally:
        azure_service._ensure_creds, azure_service._to_thread = origs
        base.transport = None
        db.close()


def test_membership_writes_need_the_toggle_and_an_editable_group():
    _clear()
    g = _install(_Graph())
    db = SessionLocal()
    try:
        row, _ = _register(db)
        _refuses(lambda: _run(ds.change_membership(row, group_id=GID, user_id=UID,
                                                   action="add")), "writes are off")
        row = ds.update_idp(db, row, writes_enabled=True)
        assert row.writes_enabled
        dynamic = "77777777-8888-7777-6666-555555555555"
        _refuses(lambda: _run(ds.change_membership(row, group_id=dynamic, user_id=UID,
                                                   action="add")), "dynamic group")
        out = _run(ds.change_membership(row, group_id=GID, user_id=UID, action="add"))
        assert out == {"group_id": GID, "group_name": "Lab Admins", "user_id": UID,
                       "action": "add", "changed": True}
        assert any(c.url.path.endswith("/members/$ref") for c in g.calls)
        _refuses(lambda: ds.update_idp(db, row, credentials_ref="plain"), "not the secret")
    finally:
        base.transport = None
        db.close()


def test_idp_rows_have_no_join_or_config_mgmt_path():
    _clear()
    _install(_Graph())
    db = SessionLocal()
    try:
        row, _ = _register(db)
        assert ds.joinable_for(db, "aws") == [] and ds.joinable_for(db, "gcp") == []
        from web_dashboard.services import inventory_service
        item = inventory_service._directory_item(row)
        assert isinstance(inventory_service._target_spec(item), str)
        _refuses(lambda: _run(ds.directory_connection_vars(row)), "Password Safe")
    finally:
        base.transport = None
        db.close()


def test_unregister_forgets_the_cached_header():
    _clear()
    _install(_Graph())
    db = SessionLocal()
    try:
        row, _ = _register(db)
        _run(ds.idp_call(row, "list_users"))
        assert row.id in directory_idp._cache
        ds.unregister(db, directory_id=row.id)
        assert row.id not in directory_idp._cache
        assert db.query(ManagedDirectory).count() == 0
    finally:
        base.transport = None
        db.close()


# ── API ───────────────────────────────────────────────────────────────────────

class _User:
    def __init__(self, perms, admin=False, username="alice"):
        self.username = username
        self.is_admin = admin
        self.is_effective_admin = admin
        self.effective_permissions_dict = perms or {}
        self.permissions = json.dumps(perms) if perms is not None else None
        self.is_active = True
        self.workgroups = "[]"
        self.must_change_password = False


def _client(user):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from web_dashboard.api import directories as api
    from web_dashboard.api.auth import get_current_user
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app)


def test_api_read_write_split_visibility_and_audit():
    _clear()
    graph = _install(_Graph())
    db = SessionLocal()
    try:
        writer = _client(_User({"directories": ["read", "write"]}))
        r = writer.post("/api/directories/register-idp", json={
            "provider": "entra_id", "tenant_id": TENANT, "client_id": "app-1",
            "credentials_ref": REF, "writes_enabled": True})
        assert r.status_code == 200, r.text
        did = r.json()["id"]
        assert REF not in r.text and r.json()["test"]["ok"]

        reader = _client(_User({"directories": ["read"]}))
        assert reader.get(f"/api/directories/{did}/users?q=an").status_code == 200
        assert reader.get(f"/api/directories/{did}/users").json()["items"][0]["id"] == UID
        assert reader.post(f"/api/directories/{did}/groups/{GID}/members/{UID}").status_code == 403
        assert reader.patch(f"/api/directories/{did}", json={"writes_enabled": False}).status_code == 403

        stranger = _client(_User({"directories": ["read", "write"]}, username="bob"))
        assert stranger.get(f"/api/directories/{did}/groups").status_code == 404
        assert stranger.post(f"/api/directories/{did}/groups/{GID}/members/{UID}").status_code == 404

        before = db.query(AuditLog).filter(AuditLog.action == "directory_member_add").count()
        r = writer.post(f"/api/directories/{did}/groups/{GID}/members/{UID}")
        assert r.status_code == 200, r.text
        db.expire_all()
        rows = db.query(AuditLog).filter(AuditLog.action == "directory_member_add").all()
        assert len(rows) == before + 1
        details = rows[-1].details if isinstance(rows[-1].details, str) else json.dumps(rows[-1].details)
        assert GID in details and UID in details and TENANT in details

        r = writer.patch(f"/api/directories/{did}", json={"writes_enabled": False})
        assert r.status_code == 200 and r.json()["writes_enabled"] is False
        r = writer.post(f"/api/directories/{did}/groups/{GID}/members/{UID}")
        assert r.status_code == 400 and "writes are off" in r.json()["detail"]

        r = writer.get(f"/api/directories/{did}/users?cursor=https://evil.example/v1.0/users")
        assert r.status_code == 502 and "cursor" in r.json()["detail"]
        assert all(c.url.host != "evil.example" for c in graph.calls)

        onprem = ManagedDirectory(name="corp.example.com", cloud="local", provider="onprem_ad",
                                  source="registered", status="available", created_by="alice")
        db.add(onprem)
        db.commit()
        assert writer.get(f"/api/directories/{onprem.id}/users").status_code == 404
    finally:
        base.transport = None
        db.close()


def test_every_idp_route_needs_an_explicit_grant():
    import inspect
    from web_dashboard.api import directories as api
    for fn in (api.register_idp, api.update_idp, api.test_idp, api.idp_users, api.idp_groups,
               api.idp_group_members, api.idp_user_groups, api.idp_add_member,
               api.idp_remove_member, api.idp_options):
        src = inspect.getsource(fn)
        assert 'require_explicit_permission("directories"' in src, fn.__name__
    for fn in (api.register_idp, api.update_idp, api.idp_add_member, api.idp_remove_member):
        assert 'require_explicit_permission("directories", "write")' in inspect.getsource(fn)


def test_directories_is_a_preview_flag_with_a_config_only_panel():
    from web_dashboard.api import setup
    assert "directories_enabled" in setup._PREVIEW_FLAGS
    assert setup._PREVIEW_FLAG_CONFIG["directories_enabled"] == "directories"
    assert "directories" in setup._CONFIG_ONLY_FEATURES
    assert "directories_enabled" not in setup.FeaturesSetup.model_fields
    html = open(os.path.join(_ROOT, "web_dashboard", "templates", "settings.html"),
                encoding="utf-8").read()
    assert "key: 'directories'" not in html, "a preview feature has no integrations-list toggle"


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
            traceback.print_exc()
    sys.exit(1 if failures else 0)
