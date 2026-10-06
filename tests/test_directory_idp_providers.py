"""Cloud identity provider modules (services/directory_idp): Entra ID, Okta, PingOne.

What these pin, each against an httpx.MockTransport (no network):

- every module signs in the way its provider documents (Entra client credentials, Okta
  SSWS or a signed client assertion, PingOne client_secret_basic) and sends the header;
- search uses the provider's own syntax (Graph $search + ConsistencyLevel, Okta and
  PingOne `sw` filters) and paging follows the provider's next link;
- a page cursor pointing anywhere but the provider's own API is refused, so a browser
  cannot aim the dashboard's bearer token at another host;
- ids are validated before they reach a URL path;
- group editability refuses dynamic, synced, role-assignable and app/built-in groups;
- membership writes use the documented verbs, and "already a member" is not an error;
- a 429 is retried once, honouring the provider's wait;
- the header cache signs in once for many calls and again after expiry or a 401.

Run: python tests/test_directory_idp_providers.py   (or under pytest)
"""
import asyncio
import base64
import json
import os
import sys
import tempfile
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="idp-prov-"), "test.db")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_TMPDB}")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-idp-provider-tests")

import httpx  # noqa: E402

from web_dashboard.services import directory_idp  # noqa: E402
from web_dashboard.services.directory_idp import base, entra, okta, pingone  # noqa: E402
from web_dashboard.services.directory_idp.base import IdPConnection, IdPError  # noqa: E402

TENANT = "11111111-2222-3333-4444-555555555555"
ENV = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
GID = "99999999-8888-7777-6666-555555555555"
UID = "12345678-1234-1234-1234-123456789abc"


def _run(coro):
    return asyncio.run(coro)


def _jwt(claims: dict) -> str:
    def b64(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")
    return f"{b64({'alg': 'none'})}.{b64(claims)}.sig"


class _Mock:
    """Records requests; answers through a list of (predicate, response-factory)."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        for match, respond in self.routes:
            if match(request):
                return respond(request)
        return httpx.Response(599, json={"message": f"unrouted {request.method} {request.url}"})

    def install(self):
        base.transport = httpx.MockTransport(self)
        return self


def _uninstall():
    base.transport = None


def _conn(provider, **kw):
    args = dict(directory_id="d1", provider=provider, endpoint="", tenant_id="",
                client_id="cid", auth_mode="client_secret", secret="s3cret")
    args.update(kw)
    return IdPConnection(**args)


# ── shared guards ─────────────────────────────────────────────────────────────

def test_checked_next_refuses_foreign_cursors():
    b = "https://graph.microsoft.com/v1.0"
    assert base.checked_next(f"{b}/users?$skiptoken=x", b).endswith("skiptoken=x")
    assert base.checked_next("", b) == ""
    for bad in ("https://evil.example/v1.0/users", "http://graph.microsoft.com/v1.0/users",
                "https://graph.microsoft.com/beta/users",
                "https://user:pw@graph.microsoft.com/v1.0/users",
                "https://graph.microsoft.com.evil.example/v1.0/users"):
        try:
            base.checked_next(bad, b)
        except IdPError:
            continue
        raise AssertionError(f"accepted foreign cursor {bad}")


def test_ids_are_validated_before_reaching_a_path():
    conn = _conn("entra_id", tenant_id=TENANT)
    for fn, bad in ((entra.get_group, "../users"), (okta.get_group, "00g/../x"),
                    (pingone.get_group, "not-a-uuid")):
        c = conn if fn is entra.get_group else (
            _conn("okta", endpoint="https://acme.okta.com") if fn is okta.get_group
            else _conn("pingone", endpoint="com", tenant_id=ENV))
        try:
            _run(fn(c, "Bearer x", bad))
        except IdPError:
            continue
        raise AssertionError(f"{fn.__module__} accepted id {bad!r}")


def test_429_is_retried_once_then_reported():
    orig = asyncio.sleep
    waited = []

    async def fake_sleep(s):
        waited.append(s)
    base.asyncio.sleep = fake_sleep
    state = {"n": 0}

    def flaky(req):
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "3"})
        return httpx.Response(200, json={"value": []})
    _Mock([(lambda r: True, flaky)]).install()
    try:
        out = _run(entra.list_users(_conn("entra_id", tenant_id=TENANT), "Bearer t"))
        assert out == {"items": [], "next": ""}
        assert waited == [3.0], waited
        _Mock([(lambda r: True, lambda r: httpx.Response(429, json={"errorSummary": "slow"}))]).install()
        try:
            _run(okta.list_users(_conn("okta", endpoint="https://acme.okta.com"), "SSWS t"))
        except IdPError as e:
            assert "HTTP 429" in str(e) and "slow" in str(e), str(e)
        else:
            raise AssertionError("a second 429 was not reported")
    finally:
        base.asyncio.sleep = orig
        _uninstall()


def test_error_text_never_echoes_the_secret():
    m = _Mock([(lambda r: "oauth2" in r.url.path,
                lambda r: httpx.Response(401, json={"error": "invalid_client",
                                                    "error_description": "AADSTS7000215 bad secret"}))]).install()
    try:
        _run(entra.auth_header(_conn("entra_id", tenant_id=TENANT, secret="TOPSECRET")))
    except IdPError as e:
        assert "TOPSECRET" not in str(e) and "HTTP 401" in str(e), str(e)
    else:
        raise AssertionError("bad secret accepted")
    finally:
        _uninstall()
    assert b"TOPSECRET" in m.calls[0].content    # it was sent, just never reflected


# ── Entra ID ──────────────────────────────────────────────────────────────────

def test_entra_signs_in_searches_and_pages():
    page2 = "https://graph.microsoft.com/v1.0/users?$skiptoken=abc"
    m = _Mock([
        (lambda r: r.url.host == "login.microsoftonline.com",
         lambda r: httpx.Response(200, json={"access_token": "AT", "expires_in": 3600})),
        (lambda r: "skiptoken" in str(r.url),
         lambda r: httpx.Response(200, json={"value": [{"id": UID, "displayName": "B"}]})),
        (lambda r: r.url.path == "/v1.0/users",
         lambda r: httpx.Response(200, json={"value": [
             {"id": UID, "displayName": "Ann", "userPrincipalName": "ann@contoso.com",
              "mail": "ann@contoso.com", "accountEnabled": True}],
             "@odata.nextLink": page2})),
    ]).install()
    try:
        conn = _conn("entra_id", tenant_id=TENANT)
        header, exp = _run(entra.auth_header(conn))
        assert header == "Bearer AT" and exp > time.monotonic()
        tok = m.calls[0]
        assert tok.url.path == f"/{TENANT}/oauth2/v2.0/token"
        assert b"grant_type=client_credentials" in tok.content
        out = _run(entra.list_users(conn, header, q='an"n'))
        req = m.calls[-1]
        assert req.headers["ConsistencyLevel"] == "eventual"
        assert req.headers["Authorization"] == "Bearer AT"
        assert req.url.params["$search"] == '"displayName:ann" OR "userPrincipalName:ann"'
        assert out["items"][0]["login"] == "ann@contoso.com" and out["next"] == page2
        out2 = _run(entra.list_users(conn, header, cursor=page2))
        assert out2["items"][0]["display_name"] == "B"
        _run(entra.list_users(conn, header))
        assert "ConsistencyLevel" not in m.calls[-1].headers
    finally:
        _uninstall()


def test_entra_group_editability():
    ok = {"groupTypes": [], "securityEnabled": True, "mailEnabled": False}
    assert entra.group_editable(ok) == ""
    assert entra.group_editable({"groupTypes": ["Unified"], "mailEnabled": True}) == ""
    assert "dynamic" in entra.group_editable({"groupTypes": ["DynamicMembership"]})
    assert "on-premises" in entra.group_editable({**ok, "onPremisesSyncEnabled": True})
    assert "role-assignable" in entra.group_editable({**ok, "isAssignableToRole": True})
    assert "distribution" in entra.group_editable({"groupTypes": [], "mailEnabled": True})


def test_entra_membership_writes():
    m = _Mock([
        (lambda r: r.method == "POST" and r.url.path.endswith("/members/$ref"),
         lambda r: httpx.Response(204)),
        (lambda r: r.method == "DELETE", lambda r: httpx.Response(404, json={
            "error": {"code": "Request_ResourceNotFound", "message": "not found"}})),
    ]).install()
    try:
        conn = _conn("entra_id", tenant_id=TENANT)
        assert _run(entra.add_member(conn, "Bearer t", GID, UID)) == {"changed": True}
        body = json.loads(m.calls[-1].content)
        assert body == {"@odata.id": f"https://graph.microsoft.com/v1.0/directoryObjects/{UID}"}
        assert m.calls[-1].url.path == f"/v1.0/groups/{GID}/members/$ref"
        assert _run(entra.remove_member(conn, "Bearer t", GID, UID)) == {"changed": False}
        assert m.calls[-1].url.path == f"/v1.0/groups/{GID}/members/{UID}/$ref"
        _Mock([(lambda r: True, lambda r: httpx.Response(400, json={"error": {
            "message": "One or more added object references already exist for the following "
                       "modified properties: 'members'."}}))]).install()
        assert _run(entra.add_member(conn, "Bearer t", GID, UID)) == {"changed": False}
    finally:
        _uninstall()


def test_entra_test_names_missing_graph_roles():
    _Mock([(lambda r: True, lambda r: httpx.Response(200, json={"value": []}))]).install()
    try:
        conn = _conn("entra_id", tenant_id=TENANT)
        none = _run(entra.test(conn, "Bearer " + _jwt({"roles": []})))
        assert not none["ok"] and none["missing"] == ["User.Read.All", "Group.Read.All"]
        read = _run(entra.test(conn, "Bearer " + _jwt({"roles": ["User.Read.All", "Group.Read.All"]})))
        assert read["ok"] and read["writes_missing"] == ["GroupMember.ReadWrite.All"]
        full = _run(entra.test(conn, "Bearer " + _jwt({"roles": ["Directory.Read.All",
                                                                 "GroupMember.ReadWrite.All"]})))
        assert full["ok"] and full["writes_missing"] == []
    finally:
        _uninstall()


# ── Okta ──────────────────────────────────────────────────────────────────────

def test_okta_endpoint_must_be_an_okta_org():
    good = {"acme.okta.com": "https://acme.okta.com",
            "https://acme.okta.com/": "https://acme.okta.com",
            "https://Dev-1.oktapreview.com": "https://dev-1.oktapreview.com",
            "https://acme.okta-emea.com": "https://acme.okta-emea.com"}
    for raw, want in good.items():
        assert okta.normalize_endpoint(raw) == want, raw
    for bad in ("http://acme.okta.com", "https://10.0.0.5", "https://login.acme.com",
                "https://acme.okta.com.evil.io", "https://acme.okta.com/api/v1",
                "https://user@acme.okta.com", "https://acme.okta.com:8443",
                "https://metadata.google.internal", "https://acme.okta.com?x=1"):
        assert okta.normalize_endpoint(bad) == "", bad


def test_okta_ssws_search_and_link_paging():
    nxt = "https://acme.okta.com/api/v1/users?after=00u2&limit=50"
    m = _Mock([(lambda r: r.url.path == "/api/v1/users",
                lambda r: httpx.Response(200, headers={"Link": f'<{nxt}>; rel="next"'}, json=[
                    {"id": "00u1", "status": "ACTIVE",
                     "profile": {"login": "ann@acme.com", "email": "ann@acme.com",
                                 "firstName": "Ann", "lastName": "Lee"}}]))]).install()
    try:
        conn = _conn("okta", endpoint="https://acme.okta.com", auth_mode="ssws", secret=" tok ")
        header, exp = _run(okta.auth_header(conn))
        assert header == "SSWS tok" and exp == 0.0
        out = _run(okta.list_users(conn, header, q="an"))
        req = m.calls[-1]
        assert req.headers["Authorization"] == "SSWS tok"
        assert 'profile.login sw "an"' in req.url.params["search"]
        assert out["items"][0] == {"id": "00u1", "display_name": "Ann Lee",
                                   "login": "ann@acme.com", "email": "ann@acme.com",
                                   "enabled": True}
        assert out["next"] == nxt
        _run(okta.list_users(conn, header, cursor=nxt))
        assert str(m.calls[-1].url) == nxt
    finally:
        _uninstall()


def test_okta_group_types_and_writes():
    assert okta.group_editable({"type": "OKTA_GROUP"}) == ""
    assert "source" in okta.group_editable({"type": "APP_GROUP"})
    assert "Everyone" in okta.group_editable({"type": "BUILT_IN"})
    m = _Mock([(lambda r: r.method in ("PUT", "DELETE"), lambda r: httpx.Response(204))]).install()
    try:
        conn = _conn("okta", endpoint="https://acme.okta.com", auth_mode="ssws")
        _run(okta.add_member(conn, "SSWS t", "00g1", "00u1"))
        assert (m.calls[-1].method, m.calls[-1].url.path) == ("PUT", "/api/v1/groups/00g1/users/00u1")
        _run(okta.remove_member(conn, "SSWS t", "00g1", "00u1"))
        assert m.calls[-1].method == "DELETE"
    finally:
        _uninstall()


def _rsa_pem():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(serialization.Encoding.PEM,
                             serialization.PrivateFormat.TraditionalOpenSSL,
                             serialization.NoEncryption()).decode()


def test_okta_private_key_jwt_assertion_and_scopes():
    from urllib.parse import parse_qs
    from jose import jwt
    m = _Mock([(lambda r: r.url.path == "/oauth2/v1/token",
                lambda r: httpx.Response(200, json={"access_token": "OAT", "token_type": "Bearer",
                                                    "expires_in": 3600}))]).install()
    try:
        conn = _conn("okta", endpoint="https://acme.okta.com", auth_mode="private_key_jwt",
                     client_id="0oa1", secret=_rsa_pem(), options={"key_id": "kid1"})
        header, _ = _run(okta.auth_header(conn))
        assert header == "Bearer OAT"
        form = parse_qs(m.calls[-1].content.decode())
        assert form["scope"] == ["okta.users.read okta.groups.read"]
        assertion = form["client_assertion"][0]
        assert jwt.get_unverified_header(assertion)["kid"] == "kid1"
        claims = jwt.get_unverified_claims(assertion)
        assert claims["iss"] == claims["sub"] == "0oa1"
        assert claims["aud"] == "https://acme.okta.com/oauth2/v1/token"
        w = _conn("okta", endpoint="https://acme.okta.com", auth_mode="private_key_jwt",
                  client_id="0oa1", secret=conn.secret, writes_enabled=True)
        _run(okta.auth_header(w))
        assert "okta.groups.manage" in parse_qs(m.calls[-1].content.decode())["scope"][0]
        _Mock([(lambda r: True, lambda r: httpx.Response(200, json={
            "access_token": "x", "token_type": "DPoP"}))]).install()
        try:
            _run(okta.auth_header(conn))
        except IdPError as e:
            assert "DPoP" in str(e)
        else:
            raise AssertionError("a DPoP-bound token was accepted")
        try:
            _run(okta.auth_header(_conn("okta", endpoint="https://acme.okta.com",
                                        auth_mode="private_key_jwt", secret="not a key")))
        except IdPError as e:
            assert "PEM" in str(e) and "not a key" not in str(e)
        else:
            raise AssertionError("a bad key signed")
    finally:
        _uninstall()


# ── PingOne ───────────────────────────────────────────────────────────────────

def test_pingone_signs_in_with_basic_and_lists_members_by_filter():
    m = _Mock([
        (lambda r: r.url.host == "auth.pingone.eu",
         lambda r: httpx.Response(200, json={"access_token": "PAT", "expires_in": 3600})),
        (lambda r: r.url.host == "api.pingone.eu" and r.url.path.endswith("/users"),
         lambda r: httpx.Response(200, json={"_embedded": {"users": [
             {"id": UID, "username": "ann", "email": "a@x.eu", "enabled": True,
              "name": {"given": "Ann", "family": "Lee"}}]},
             "_links": {"next": {"href": f"https://api.pingone.eu/v1/environments/{ENV}/users?cursor=2"}}})),
    ]).install()
    try:
        conn = _conn("pingone", endpoint="eu", tenant_id=ENV, client_id="wk", secret="ps")
        header, _ = _run(pingone.auth_header(conn))
        tok = m.calls[0]
        assert tok.url.path == f"/{ENV}/as/token"
        assert tok.headers["Authorization"] == "Basic " + base64.b64encode(b"wk:ps").decode()
        out = _run(pingone.group_members(conn, header, GID))
        assert m.calls[-1].url.params["filter"] == f'memberOfGroups[id eq "{GID}"]'
        assert out["items"][0]["display_name"] == "Ann Lee"
        assert out["next"].endswith("cursor=2")
        try:
            _run(pingone.list_users(conn, header, cursor="https://api.pingone.com/v1/environments/x/users"))
        except IdPError:
            pass
        else:
            raise AssertionError("a cross-region cursor was followed")
    finally:
        _uninstall()


def test_pingone_regions_dynamic_groups_and_writes():
    try:
        _run(pingone.auth_header(_conn("pingone", endpoint="evil.example", tenant_id=ENV)))
    except IdPError as e:
        assert "region" in str(e)
    else:
        raise AssertionError("an unknown region was used")
    assert "dynamic" in pingone.group_editable({"userFilter": 'email ew "@x.com"'})
    assert pingone.group_editable({"name": "static"}) == ""
    m = _Mock([(lambda r: r.method == "POST", lambda r: httpx.Response(201, json={"id": GID})),
               (lambda r: r.method == "DELETE", lambda r: httpx.Response(204))]).install()
    try:
        conn = _conn("pingone", endpoint="com", tenant_id=ENV)
        assert _run(pingone.add_member(conn, "Bearer t", GID, UID)) == {"changed": True}
        assert m.calls[-1].url.path == f"/v1/environments/{ENV}/users/{UID}/memberOfGroups"
        assert json.loads(m.calls[-1].content) == {"id": GID}
        _run(pingone.remove_member(conn, "Bearer t", GID, UID))
        assert m.calls[-1].url.path.endswith(f"/memberOfGroups/{GID}")
    finally:
        _uninstall()


# ── header cache (directory_idp.authorize / call) ─────────────────────────────

class _Row:
    def __init__(self, **kw):
        self.id = kw.pop("id", "row-1")
        self.provider = "entra_id"
        self.endpoint = ""
        self.tenant_id = TENANT
        self.client_id = "cid"
        self.auth_mode = "client_secret"
        self.credentials_ref = "bt_safe://Dashboard/entra"
        self.options = "{}"
        self.writes_enabled = False
        self.workgroup = None
        self.__dict__.update(kw)


def test_header_cache_signs_in_once_and_again_after_401():
    from web_dashboard.services import config_service
    orig = config_service.resolve_reference
    resolved = []

    def fake_resolve(ref, workgroup=None):
        resolved.append(ref)
        return "the-secret"
    config_service.resolve_reference = fake_resolve
    state = {"tokens": 0, "fail_next": False}

    def token(req):
        state["tokens"] += 1
        return httpx.Response(200, json={"access_token": f"AT{state['tokens']}", "expires_in": 3600})

    def users(req):
        if state["fail_next"]:
            state["fail_next"] = False
            return httpx.Response(401, json={"error": {"message": "expired"}})
        return httpx.Response(200, json={"value": []})
    _Mock([(lambda r: r.url.host == "login.microsoftonline.com", token),
           (lambda r: True, users)]).install()
    row = _Row()
    try:
        directory_idp.forget(row.id)
        for _ in range(3):
            _run(directory_idp.call(row, "list_users"))
        assert state["tokens"] == 1 and len(resolved) == 1
        state["fail_next"] = True
        _run(directory_idp.call(row, "list_users"))
        assert state["tokens"] == 2
        row.credentials_ref = "bt_safe://Dashboard/entra-v2"    # an edit is a cache miss
        _run(directory_idp.call(row, "list_users"))
        assert state["tokens"] == 3
        fp, header, _exp = directory_idp._cache[row.id]
        directory_idp._cache[row.id] = (fp, header, time.monotonic() - 1)   # expired
        _run(directory_idp.call(row, "list_users"))
        assert state["tokens"] == 4
        directory_idp.forget(row.id)
        assert row.id not in directory_idp._cache
    finally:
        config_service.resolve_reference = orig
        _uninstall()


def test_unresolvable_vault_ref_and_unknown_scheme_refuse():
    from web_dashboard.services import config_service
    orig = config_service.resolve_reference
    config_service.resolve_reference = lambda ref, workgroup=None: ""
    try:
        for ref, needle in (("bt_safe://x/y", "could not be resolved"),
                            ("plaintext-secret", "no usable credential")):
            try:
                _run(directory_idp.authorize(_Row(id=f"r-{needle}", credentials_ref=ref)))
            except IdPError as e:
                assert needle in str(e), str(e)
                assert "plaintext-secret" not in str(e)
            else:
                raise AssertionError(f"{ref} authorized")
    finally:
        config_service.resolve_reference = orig


def test_option_keys_are_closed_and_pinned():
    assert directory_idp.OPTION_KEYS == {"entra_id": (), "okta": ("key_id",),
                                         "pingone": ("token_endpoint_auth",)}
    row = _Row(provider="okta", options=json.dumps({"key_id": "k", "password": "leak"}))
    assert directory_idp.options_of(row) == {"key_id": "k"}
    assert set(directory_idp.PROVIDERS) == {"entra_id", "okta", "pingone"}


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
