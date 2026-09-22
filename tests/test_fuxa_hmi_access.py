"""fuxa_hmi_access as an Entitle Remote Adapter.

The first OT-native target in the Entitle catalogue: a just-in-time account on the
plant's web HMI. What is pinned here is the shape Entitle actually depends on — routes
keyed by PATH, the two response envelopes whose published examples are wrong, and the
four-operation ephemeral lifecycle — plus the FUXA wire details that its own
documentation and OpenAPI get wrong, each of which costs a debugging session:

* the token header is ``x-access-token``; no ``Authorization: Bearer`` parsing exists;
* ``POST /api/users`` takes ``params`` as a SINGLE OBJECT, not the declared array;
* ``DELETE /api/users`` keys on ``?param=<username>`` — singular, and there is no id;
* the mutating routes answer 200 with an EMPTY body, not 204.

Driven through a patched ``urlopen`` rather than by stubbing the adapter's own
``_api``, so the headers, the query string and the body wrapping are asserted as they
would go on the wire. The bitmask rules are proved separately in
tests/test_fuxa_access_rules.py. Runs entirely offline and reaches no HMI.

Run: python tests/test_fuxa_hmi_access.py   (or under pytest)
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from web_dashboard import functions  # noqa: F401,E402  (puts fnruntime on sys.path)
from fnruntime.contract import Context, Request  # noqa: E402
from fnworkloads import fuxa_hmi_access as w  # noqa: E402

_ENV_KEYS = ("FN_FUXA_URL", "FN_FUXA_HMI_URL", "FN_FUXA_ASSET_ID",
             "FN_FUXA_ASSET_NAME", "FN_FUXA_CELL", "FN_FUXA_JUMP_ITEM",
             "FN_FUXA_USER", "FN_FUXA_PASSWORD", "FN_FUXA_API_KEY",
             "FN_FUXA_ROLE_MODE", "FN_FUXA_VERIFY_SSL", "FN_FUXA_DRY_RUN")

CALLS = []
USERS = []
ROLES = []
FAIL = {}


class _Resp:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fake_urlopen(request, timeout=None, context=None):
    """A FUXA that records what it was asked and answers like the real one."""
    path = request.full_url.split("://", 1)[-1].split("/", 1)[-1]
    path = "/" + path
    body = None
    if request.data:
        body = json.loads(request.data.decode("utf-8"))
    CALLS.append({
        "method": request.get_method(),
        "path": path,
        "body": body,
        # urllib title-cases header keys it is given, so compare lower-cased.
        "headers": {k.lower(): v for k, v in request.header_items()},
    })
    base = path.split("?", 1)[0]
    key = (request.get_method(), base)
    if key in FAIL:
        code = FAIL[key]
        raise urllib.error.HTTPError(request.full_url, code, "nope", {}, None)
    if key == ("POST", "/api/signin"):
        return _Resp(json.dumps({"status": "success", "data": {
            "username": "admin", "groups": -1, "token": "tok-abc"}}).encode())
    if key == ("GET", "/api/users"):
        return _Resp(json.dumps(USERS).encode())
    if key == ("GET", "/api/roles"):
        return _Resp(json.dumps(ROLES).encode())
    # Every mutating user route: 200 with an EMPTY body.
    return _Resp(b"")


def _reset(**env):
    CALLS.clear()
    USERS.clear()
    ROLES.clear()
    FAIL.clear()
    for key in _ENV_KEYS:
        os.environ.pop(key, None)
    base = {"FN_FUXA_URL": "http://10.0.0.5:1881",
            "FN_FUXA_ASSET_ID": "fuxa:ot-cell-01:hmi",
            "FN_FUXA_JUMP_ITEM": "ot-ot-cell-01-hmi",
            "FN_FUXA_PASSWORD": "admin-pw",
            "FN_FUXA_DRY_RUN": "0"}
    base.update(env)
    for key, value in base.items():
        if value is not None:
            os.environ[key] = value
    urllib.request.urlopen = _fake_urlopen


def _call(method, path, payload=None):
    return w.handle(
        Request(method=method, path=path, headers={}, query={},
                body=json.dumps(payload or {}).encode(), source="openfaas"),
        Context.from_env(workload="fuxa_hmi_access"))


def _paths(method=None):
    return [c["path"] for c in CALLS if method is None or c["method"] == method]


# ── Routing ──────────────────────────────────────────────────────────────────

def test_every_ephemeral_route_answers():
    _reset()
    assert _call("GET", "/get_assets").status == 200
    assert _call("GET", "/get_all_permissions").status == 200
    assert _call("GET", "/get_actors").status == 200
    assert _call("POST", "/check_config").status == 200
    assert _call("POST", "/create_actor", {"role_code": "viewer"}).status == 200


def test_an_unknown_route_lists_the_ones_that_exist():
    _reset()
    resp = _call("GET", "/nope")
    assert resp.status == 404
    assert "get_assets" in json.dumps(resp.body)


def test_the_per_asset_permissions_variant_answers_the_same_rows():
    """Entitle appends the asset id to that path. One asset here, so it is the same
    answer rather than a 404 that reads as a broken integration."""
    _reset()
    resp = _call("GET", "/get_asset_permissions/fuxa:ot-cell-01:hmi")
    assert resp.status == 200
    assert "actors_permissions" in resp.body["data"]


def test_standing_mode_routes_explain_themselves_instead_of_404ing():
    _reset()
    for route in ("/give_access", "/revoke_access"):
        resp = _call("POST", route, {"actor_identifier": "x"})
        assert resp.status == 400, route
        assert "Ephemeral" in resp.body["error"], resp.body
        assert "Connection" in resp.body["error"], "the remedy is not named"


# ── get_assets ───────────────────────────────────────────────────────────────

def test_the_asset_is_single_role_with_the_published_options():
    _reset()
    asset = _call("GET", "/get_assets").body["data"]["assets"][0]
    assert asset["identifier"] == "fuxa:ot-cell-01:hmi"
    assert asset["type"] == "fuxa_hmi"
    assert asset["multirole"] is False, (
        "a FUXA account carries ONE groups value; advertising multirole would let "
        "Entitle request a combination the target cannot express")
    codes = [o["code"] for o in asset["role_options"]]
    assert codes == ["viewer", "operator"], codes
    assert all("display_name" in o and "available" in o for o in asset["role_options"])


def test_bitmask_mode_does_not_ask_fuxa_for_a_role_catalogue():
    """FUXA's default model has no catalogue to read, and a 404 from that route would
    be noise in every get_assets."""
    _reset()
    _call("GET", "/get_assets")
    assert "/api/roles" not in _paths()


def test_catalogue_mode_reads_the_plants_own_roles():
    _reset(FN_FUXA_ROLE_MODE="catalogue")
    ROLES.extend([{"id": "g-1", "name": "Viewer"}])
    asset = _call("GET", "/get_assets").body["data"]["assets"][0]
    assert "/api/roles" in _paths()
    by_code = {o["code"]: o for o in asset["role_options"]}
    assert by_code["viewer"]["available"] is True
    assert by_code["operator"]["available"] is False


# ── get_all_permissions: the shape whose published example is wrong ──────────

def test_permissions_are_maps_keyed_by_asset_id_not_lists():
    """A list fails only this one route: get_assets still syncs green, the
    integration looks configured, and the single symptom is 'Failed to fetch the
    permissions' in Entitle's audit log."""
    _reset()
    USERS.extend([{"username": "jit-a-1", "groups": w.fuxarules.groups_for("operator")},
                  {"username": "admin", "groups": -1}])
    data = _call("GET", "/get_all_permissions").body["data"]
    assert isinstance(data["actors_permissions"], dict), data
    assert isinstance(data["assets_permissions"], dict), data
    rows = data["actors_permissions"]["fuxa:ot-cell-01:hmi"]
    assert [r["actor_id"] for r in rows] == ["jit-a-1"], (
        "an operator's own account was reported as one Entitle provisioned")
    assert rows[0]["role_code"] == "operator"
    assert rows[0]["direct_member"] is True


def test_the_asset_key_is_present_even_with_no_holders():
    """An asset with an empty list says 'nobody has this'; one the map omits says
    nothing at all, and only the first is something Entitle can reconcile."""
    _reset()
    data = _call("GET", "/get_all_permissions").body["data"]
    assert data["actors_permissions"] == {"fuxa:ot-cell-01:hmi": []}
    assert data["assets_permissions"] == {}


def test_an_account_holding_something_unnameable_reports_an_empty_role():
    _reset()
    USERS.append({"username": "jit-hand-made", "groups": 4})
    rows = _call("GET", "/get_all_permissions").body["data"][
        "actors_permissions"]["fuxa:ot-cell-01:hmi"]
    assert rows[0]["role_code"] == "", rows


def test_get_actors_reports_only_our_own_accounts():
    _reset()
    USERS.extend([{"username": "admin", "fullname": "Operator"},
                  {"username": "jit-a-1", "fullname": "Entitle JIT"}])
    actors = _call("GET", "/get_actors").body["data"]["actors"]
    assert [a["identifier"] for a in actors] == ["jit-a-1"]
    assert actors[0]["type"] == "fuxa_user"


# ── create_actor: this IS the grant ──────────────────────────────────────────

def test_create_actor_returns_the_closed_nested_shape():
    """``data`` is validated against a schema with exactly two properties, and the
    flat dict every adapter first wrote is rejected wholesale — with the account
    already created, which is why this route is the worst one to get wrong."""
    _reset()
    data = _call("POST", "/create_actor", {
        "provisioning_data": {"email": "vendor@acme.example"},
        "role_code": "operator"}).body["data"]
    assert set(data) == {"actor", "login_info"}, data
    assert set(data["actor"]) == {"identifier", "name", "type", "email"}, data["actor"]
    assert data["actor"]["email"] == "vendor@acme.example", (
        "the requester is the only link from the account back to a person")
    assert data["actor"]["identifier"].startswith("jit-vendor-acme-example-")


def test_the_credential_and_the_route_to_it_are_both_handed_over():
    """The minted account is USELESS on its own: the cell admits the PRA Gateway and
    the broker and nothing else, so the requester's browser cannot reach the HMI."""
    _reset()
    info = _call("POST", "/create_actor", {
        "provisioning_data": {"email": "v@a.example"},
        "role_code": "viewer"}).body["data"]["login_info"]
    assert info["username"].startswith("jit-")
    assert len(info["password"]) >= 20
    assert info["role_code"] == "viewer"
    assert info["pra_jump_item"] == "ot-ot-cell-01-hmi"
    assert "Web Jump" in info["how"], info["how"]
    assert "reconnect" in info["revocation"], (
        "the revocation note overstates: an open tab survives until its socket drops")


def test_without_a_jump_item_the_requester_is_told_what_is_missing():
    _reset(FN_FUXA_JUMP_ITEM=None)
    info = _call("POST", "/create_actor", {"role_code": "viewer"}).body["data"]["login_info"]
    assert "pra_jump_item" not in info
    assert "plant boundary" in info["how"], info["how"]


def test_create_actor_posts_a_single_object_under_params():
    _reset()
    _call("POST", "/create_actor", {"role_code": "operator",
                                    "provisioning_data": {"email": "v@a.example"}})
    post = next(c for c in CALLS if c["method"] == "POST" and c["path"] == "/api/users")
    assert isinstance(post["body"]["params"], dict), (
        "params must be a single object — FUXA's own OpenAPI declares an array and "
        "setUsers rejects one with a bare 400")
    params = post["body"]["params"]
    assert params["groups"] == w.fuxarules.groups_for("operator")
    assert params["password"], "the password must be sent, in plaintext"
    json.loads(params["info"])  # never invalid, or FUXA drops the user after a 200


def test_the_session_token_goes_in_x_access_token():
    _reset()
    _call("POST", "/create_actor", {"role_code": "viewer"})
    signin = next(c for c in CALLS if c["path"] == "/api/signin")
    assert signin["body"] == {"username": "admin", "password": "admin-pw"}
    post = next(c for c in CALLS if c["method"] == "POST" and c["path"] == "/api/users")
    assert post["headers"].get("x-access-token") == "tok-abc", post["headers"]
    assert "authorization" not in post["headers"], (
        "there is no Authorization: Bearer parsing anywhere in the FUXA server")


def test_an_api_key_skips_sign_in_entirely():
    _reset(FN_FUXA_API_KEY="key-123", FN_FUXA_PASSWORD=None)
    _call("GET", "/get_actors")
    assert "/api/signin" not in _paths(), "an API key needs no session"
    assert CALLS[0]["headers"].get("x-api-key") == "key-123"


def test_dry_run_is_on_by_default_and_touches_nothing():
    _reset(FN_FUXA_DRY_RUN=None)
    data = _call("POST", "/create_actor", {"role_code": "operator"}).body["data"]
    assert "/api/users" not in _paths("POST"), "dry run wrote to the HMI"
    assert "dry_run" in data["login_info"], (
        "a credential that does not work must say so, or it reads as a broken grant")
    assert data["actor"]["identifier"].startswith("jit-")


def test_an_unpublished_role_is_refused_before_anything_is_created():
    _reset()
    resp = _call("POST", "/create_actor", {"role_code": "administrator"})
    assert resp.status == 400, resp.body
    assert "/api/users" not in _paths("POST"), "an account was created anyway"


# ── delete_actor ─────────────────────────────────────────────────────────────

def test_delete_refuses_an_account_this_adapter_did_not_mint():
    """The only thing between a grant integration and the operator's own admin."""
    _reset()
    USERS.append({"username": "admin"})
    resp = _call("POST", "/delete_actor", {"actor_identifier": "admin"})
    assert resp.status == 403, resp.body
    assert "only" in resp.body["error"]
    assert not [c for c in CALLS if c["method"] == "DELETE"], "it called FUXA anyway"


def test_delete_is_checked_before_any_lookup():
    """A refusal that depended on reading the user list would still be a refusal, but
    it would leak whether the account exists — and would fail open if the list call
    did."""
    _reset()
    FAIL[("GET", "/api/users")] = 500
    assert _call("POST", "/delete_actor", {"actor_identifier": "admin"}).status == 403


def test_delete_keys_on_the_singular_param_query():
    _reset()
    USERS.append({"username": "jit-a-1"})
    resp = _call("POST", "/delete_actor", {"actor_identifier": "jit-a-1"})
    assert resp.status == 200 and resp.body["data"]["deleted"] is True
    deleted = next(c for c in CALLS if c["method"] == "DELETE")
    query = urllib.parse.parse_qs(deleted["path"].split("?", 1)[1])
    assert query == {"param": ["jit-a-1"]}, (
        "the query key is `param`, singular, and it keys on the username")


def test_an_already_absent_account_is_success_not_an_error():
    """Entitle retries a failed delete, so an error here leaves it retrying forever
    over an account somebody removed by hand."""
    _reset()
    resp = _call("POST", "/delete_actor", {"actor_identifier": "jit-gone-1"})
    assert resp.status == 200
    assert resp.body["data"]["already_absent"] is True
    assert not [c for c in CALLS if c["method"] == "DELETE"]


def test_a_404_on_the_delete_itself_is_also_success():
    _reset()
    USERS.append({"username": "jit-a-1"})
    FAIL[("DELETE", "/api/users")] = 404
    assert _call("POST", "/delete_actor", {"actor_identifier": "jit-a-1"}).status == 200


def test_delete_requires_an_identifier():
    _reset()
    assert _call("POST", "/delete_actor", {}).status == 400


# ── check_config ─────────────────────────────────────────────────────────────

def test_check_config_reads_and_never_writes():
    _reset()
    USERS.extend([{"username": "jit-a-1"}, {"username": "admin"}])
    data = _call("POST", "/check_config").body["data"]
    assert data["valid"] is True, data
    assert data["managed_accounts"] == 1
    assert data["authenticated"] is True
    assert not [c for c in CALLS if c["method"] in ("POST", "DELETE")
                and c["path"] != "/api/signin"]


def test_check_config_reports_every_problem_not_the_first():
    _reset(FN_FUXA_JUMP_ITEM=None, FN_FUXA_PASSWORD=None)
    data = _call("POST", "/check_config").body["data"]
    assert data["valid"] is False
    assert len(data["problems"]) >= 2, data["problems"]
    assert any("JUMP_ITEM" in p for p in data["problems"])
    assert any("secureEnabled" in p for p in data["problems"]), (
        "the no-credential case must name WHY it might be fine — FUXA with auth off "
        "applies no authorization at all")


def test_an_unconfigured_function_can_still_say_what_is_missing():
    """_config runs AFTER routing precisely so this route survives it failing — it is
    the one route whose whole job is naming the problem."""
    _reset(FN_FUXA_URL=None)
    resp = _call("POST", "/check_config")
    assert resp.status == 200, resp.body
    assert resp.body["data"]["valid"] is False
    assert "FN_FUXA_URL" in resp.body["data"]["reason"]


def test_an_unconfigured_function_fails_closed_on_every_other_route():
    _reset(FN_FUXA_URL=None)
    resp = _call("POST", "/create_actor", {"role_code": "viewer"})
    assert resp.status == 500
    assert resp.body["error"] == "function not configured"
    assert "FN_FUXA_URL" in resp.body["problem"]
    assert resp.body["request_id"]


def test_an_unreachable_hmi_names_the_firewall_and_the_pod():
    """The failure this will actually hit first, and it is not the adapter's fault:
    the DMZ-to-plant rule, which has to be asked from a POD because the node's own
    address is a different answer.

    Note WHERE that message surfaces. A grant route raises, and ``dispatch`` — not
    this workload — turns that into a deliberately generic 500 with the detail in the
    function's log only, so a caller learns nothing about how the function is wired.
    ``check_config`` is the route that exists to say it out loud, which is why the
    wiring play probes that one on every deploy.
    """
    from fnruntime import dispatch

    _reset()

    def _refuse(request, timeout=None, context=None):
        raise urllib.error.URLError("connection refused")

    urllib.request.urlopen = _refuse

    # The grant route, through dispatch, as a caller sees it.
    os.environ["FN_SHARED_SECRET"] = "s"
    request = Request(method="GET", path="/get_actors",
                      headers={"authorization": "Bearer s"}, query={},
                      body=b"{}", source="openfaas")
    resp = dispatch.handle_request(request, w)
    assert resp.status == 500, resp.body
    assert resp.body == {"error": "internal error",
                         "request_id": resp.body.get("request_id")}, resp.body
    assert "firewall" not in json.dumps(resp.body), (
        "how the function is wired leaked to an unauthenticated-ish caller")
    os.environ.pop("FN_SHARED_SECRET", None)

    # And the diagnostic route, which is where the operator is meant to look.
    data = _call("POST", "/check_config").body["data"]
    assert data["valid"] is False
    detail = "; ".join(data["problems"])
    assert "firewall" in detail and "POD" in detail, detail


if __name__ == "__main__":
    _real = urllib.request.urlopen
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as exc:
            failures += 1
            print(f"FAIL {fn.__name__}: {exc}")
        finally:
            urllib.request.urlopen = _real
    sys.exit(1 if failures else 0)
