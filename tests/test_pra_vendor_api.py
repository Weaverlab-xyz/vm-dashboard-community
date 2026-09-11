"""The PRA Vendor Onboarding client — the request bodies, against the appliance's own spec.

Every field name and limit pinned here is quoted from
``openapi/bt-pra-configuration.openapi.yaml`` in BeyondTrust's ``terraform-provider-sra``
repository (v1.10), which is the same document an appliance serves at
``/api/config/v1/openapi.yaml``. That is what makes these assertions worth having: they are
not a guess this test would happily keep agreeing with, they are a copy of the contract.

What is pinned, and why each one is a bug that would otherwise ship silently:

  * **``Vendor`` requires ``name`` and ``default_policy``**, and ``account_expiration``
    must land inside 1-365. A POV that expired an hour ago computes 0 days, and the 422
    that comes back names a field nobody typed.
  * **The three notification/approval flags are always sent.** Two of them default to
    *true* in the schema and require at least one administrator or team, so omitting them
    on a group with no administrators is a guaranteed 422 — about notifications, which is
    not what the operator was doing.
  * **The paged reader follows ``X-BT-Pagination-Last-Page``.** ``POST /vendor/{id}/user``
    answers 200 with no body, so the created user's id is recovered by re-reading the list
    — and a reader that stops at page 1 turns "the login was created" into "the dashboard
    says it does not exist".
  * **A 404 on a vendor path says the appliance may be too old**, separately from a 404 on
    anything else. Vendor Onboarding is a newer Config API surface, and without this the
    two read identically.
  * **No caught exception's text and no raw response body reaches a user-facing message.**
    The CodeQL rule ``bt_tenant_verify._http_reason`` exists for; the one exception is a
    422's own field-level explanation, which is the appliance's words about the request
    this dashboard built.

No network and no FastAPI: httpx is driven through a MockTransport.

Runs under pytest, or standalone:  python tests/test_pra_vendor_api.py
"""
import asyncio
import inspect
import os
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_cfg_stub = types.ModuleType("web_dashboard.config")
_cfg_stub.settings = object()
sys.modules.setdefault("web_dashboard.config", _cfg_stub)

import httpx  # noqa: E402

from web_dashboard.services import pra_vendor_api as v  # noqa: E402
from web_dashboard.services.pra_tenant_api import PRATenantError  # noqa: E402


class _Tenant:
    """The three fields pra_tenant_api.get_token and _request read off a Tenant."""
    name = "acme"
    client_id = "cid"
    secret = "sek"
    api_base = "https://acme.beyondtrustcloud.com"


def _run(coro):
    return asyncio.run(coro)


def _serve(handler):
    """Point httpx.AsyncClient at a handler for the duration of one call.

    Patches the module's client factory rather than the transport, so the assertions are
    about what THIS module sends — not about httpx.
    """
    real = httpx.AsyncClient

    class _Client(real):
        def __init__(self, *a, **kw):
            kw["transport"] = httpx.MockTransport(handler)
            super().__init__(*a, **kw)

    httpx.AsyncClient = _Client
    return real


def _restore(real):
    httpx.AsyncClient = real


def _token_ok(request):
    return httpx.Response(200, json={"access_token": "t0ken"})


def _recorder(responses):
    """A handler that answers the token call, then serves ``responses`` in order.

    Returns ``(handler, calls)`` where calls holds every non-token request.
    """
    calls = []
    queue = list(responses)

    def handler(request):
        if request.url.path.endswith("/oauth2/token"):
            return _token_ok(request)
        calls.append(request)
        return queue.pop(0) if queue else httpx.Response(200, json={})

    return handler, calls


def _body(request):
    import json
    return json.loads(request.content.decode() or "{}")


# ── Vendor group creates ─────────────────────────────────────────────────────

def test_a_vendor_group_carries_name_and_default_policy():
    """Both are `required` in the Vendor schema; everything else is optional."""
    handler, calls = _recorder([httpx.Response(200, json={"id": 9})])
    real = _serve(handler)
    try:
        row = _run(v.create_vendor(_Tenant(), name="pov-acme-vendors", policy_id="4",
                                   account_expiration=7))
    finally:
        _restore(real)
    assert row["id"] == 9
    sent = _body(calls[0])
    assert calls[0].url.path == "/api/config/v1/vendor"
    assert sent["name"] == "pov-acme-vendors"
    # An int, not the string the column holds — the schema says integer.
    assert sent["default_policy"] == 4 and isinstance(sent["default_policy"], int)


def test_the_notification_flags_are_always_sent_and_false_without_administrators():
    """Both default to TRUE in the schema and require an administrator or team, so
    omitting them on a group with no administrators is a guaranteed 422."""
    handler, calls = _recorder([httpx.Response(200, json={"id": 1})])
    real = _serve(handler)
    try:
        _run(v.create_vendor(_Tenant(), name="pov-x-vendors", policy_id="2",
                             account_expiration=5))
    finally:
        _restore(real)
    sent = _body(calls[0])
    for key in ("user_added_notification_enabled", "user_expired_notification_enabled",
                "user_approval_enabled"):
        assert key in sent, f"{key} was omitted, which takes the schema default"
        assert sent[key] is False
    assert "administrator_ids" not in sent


def test_administrators_turn_the_notifications_back_on():
    handler, calls = _recorder([httpx.Response(200, json={"id": 1})])
    real = _serve(handler)
    try:
        _run(v.create_vendor(_Tenant(), name="pov-x-vendors", policy_id="2",
                             account_expiration=5, administrator_ids=[3, 4]))
    finally:
        _restore(real)
    sent = _body(calls[0])
    assert sent["administrator_ids"] == [3, 4]
    assert sent["user_added_notification_enabled"] is True


def test_the_expiration_is_clamped_into_the_schemas_own_range():
    """1-365. A POV that expired an hour ago computes 0, and 0 is a 422 about a field
    nobody typed."""
    for asked, expected in ((0, 1), (-5, 1), (9999, 365), (7, 7)):
        handler, calls = _recorder([httpx.Response(200, json={"id": 1})])
        real = _serve(handler)
        try:
            _run(v.create_vendor(_Tenant(), name="pov-x-vendors", policy_id="2",
                                 account_expiration=asked))
        finally:
            _restore(real)
        assert _body(calls[0])["account_expiration"] == expected, f"asked {asked}"


def test_at_most_ten_administrators_go_out():
    """`The sum of administrator_ids and team_ids is not allowed to exceed 10.`"""
    handler, calls = _recorder([httpx.Response(200, json={"id": 1})])
    real = _serve(handler)
    try:
        _run(v.create_vendor(_Tenant(), name="pov-x-vendors", policy_id="2",
                             account_expiration=5,
                             administrator_ids=list(range(1, 30))))
    finally:
        _restore(real)
    assert len(_body(calls[0])["administrator_ids"]) == 10


# ── Group policy + the membership that IS the scoping ────────────────────────

def test_a_group_policy_defines_its_permissions_or_it_grants_nothing():
    """Left at the `not_defined` default the policy is created, looks right in the
    appliance, and applies none of its perm_* fields."""
    handler, calls = _recorder([httpx.Response(200, json={"id": 12})])
    real = _serve(handler)
    try:
        _run(v.create_group_policy(_Tenant(), name="pov-acme-vendor-access",
                                   perms={"perm_shell_jump": True,
                                          "perm_remote_rdp": False},
                                   default_jump_item_role_id=3))
    finally:
        _restore(real)
    sent = _body(calls[0])
    assert calls[0].url.path == "/api/config/v1/group-policy"
    assert sent["access_perm_status"] == "defined"
    assert sent["perm_access_allowed"] is True
    assert sent["perm_shell_jump"] is True and sent["perm_remote_rdp"] is False
    assert sent["default_jump_item_role_id"] == 3
    # Nothing else is asserted true: every permission not named keeps its false default,
    # and a vendor policy grants sessions and nothing else.
    assert not any(k.startswith("perm_") and v_ is True and k not in
                   ("perm_access_allowed", "perm_shell_jump")
                   for k, v_ in sent.items())


def test_the_membership_is_one_jump_group_and_inherits_both_roles():
    """One membership row is the entire scoping story — a policy reaches exactly the Jump
    Groups it has one for. Zero on both ids means User's Default / Set on Jump Items."""
    handler, calls = _recorder([httpx.Response(204)])
    real = _serve(handler)
    try:
        _run(v.add_policy_jump_group(_Tenant(), "12", "44"))
    finally:
        _restore(real)
    assert calls[0].url.path == "/api/config/v1/group-policy/12/jump-group"
    sent = _body(calls[0])
    assert sent == {"jump_group_id": 44, "jump_item_role_id": 0, "jump_policy_id": 0}


# ── Jump groups ──────────────────────────────────────────────────────────────

def test_a_jump_group_name_is_rechecked_after_the_server_side_filter():
    """The `name` query parameter filters, but a filter that is a substring match on some
    version would hand back a neighbour — and a Jump Group is what scopes a vendor."""
    handler, calls = _recorder([
        httpx.Response(200, json=[{"id": 1, "name": "pov-acme-two"},
                                  {"id": 2, "name": "pov-acme"}])])
    real = _serve(handler)
    try:
        row = _run(v.find_jump_group(_Tenant(), "pov-acme"))
    finally:
        _restore(real)
    assert row["id"] == 2


def test_a_jump_group_code_name_is_trimmed_to_the_schema_limit():
    handler, calls = _recorder([httpx.Response(200, json={"id": 1})])
    real = _serve(handler)
    try:
        _run(v.create_jump_group(_Tenant(), name="n" * 400, code_name="c" * 200))
    finally:
        _restore(real)
    sent = _body(calls[0])
    assert len(sent["name"]) == v.NAME_MAX == 255
    assert len(sent["code_name"]) == v.CODE_NAME_MAX == 64


# ── Vendor users, and the reason the list is re-read ─────────────────────────

def test_a_created_vendor_user_id_is_recovered_from_the_list():
    """POST /vendor/{id}/user answers 200 with NO body, so the id genuinely is not in the
    response — the only way to learn it is to look the username back up."""
    handler, calls = _recorder([
        httpx.Response(200),                                        # the create
        httpx.Response(200, json=[{"id": 5, "username": "povvnd_acme_aaaa"},
                                  {"id": 6, "username": "povvnd_acme_bbbb"}])])
    real = _serve(handler)
    try:
        got = _run(v.create_vendor_user(_Tenant(), "9", username="povvnd_acme_bbbb",
                                        password="pw", email="a@b.c"))
    finally:
        _restore(real)
    assert got == "6"
    sent = _body(calls[0])
    assert sent["username"] == "povvnd_acme_bbbb"
    assert sent["password_reset_next_login"] is True
    assert sent["enabled"] is True
    assert sent["email_address"] == "a@b.c"


def test_a_created_user_that_does_not_appear_is_reported_rather_than_recorded_blank():
    """A row that cannot be deleted later is worse than a failed create — the account
    exists either way."""
    handler, _calls = _recorder([httpx.Response(200), httpx.Response(200, json=[])])
    real = _serve(handler)
    try:
        _run(v.create_vendor_user(_Tenant(), "9", username="povvnd_x", password="pw"))
        raise AssertionError("a missing user was accepted")
    except PRATenantError as exc:
        assert "does not list it" in str(exc)
    finally:
        _restore(real)


def test_the_paged_reader_follows_every_page():
    """Stopping at page 1 loses the user this dashboard is trying to find by username."""
    pages = {
        1: httpx.Response(200, json=[{"id": 1, "username": "a"}],
                          headers={"X-BT-Pagination-Last-Page": "3"}),
        2: httpx.Response(200, json=[{"id": 2, "username": "b"}],
                          headers={"X-BT-Pagination-Last-Page": "3"}),
        3: httpx.Response(200, json=[{"id": 3, "username": "c"}],
                          headers={"X-BT-Pagination-Last-Page": "3"}),
    }
    seen = []

    def handler(request):
        if request.url.path.endswith("/oauth2/token"):
            return _token_ok(request)
        page = int(request.url.params.get("current_page", 1))
        seen.append(page)
        return pages[page]

    real = _serve(handler)
    try:
        rows = _run(v.list_vendor_users(_Tenant(), "9"))
    finally:
        _restore(real)
    assert seen == [1, 2, 3]
    assert [r["username"] for r in rows] == ["a", "b", "c"]


# ── refusals ─────────────────────────────────────────────────────────────────

def test_a_404_on_a_vendor_path_names_the_appliance_version():
    """Vendor Onboarding is a newer Config API surface than Jump Groups. Without this,
    'your PRA is too old' and 'that group was deleted' read identically."""
    handler, _calls = _recorder([httpx.Response(404, json={"message": "not found"})])
    real = _serve(handler)
    try:
        _run(v.create_vendor(_Tenant(), name="x", policy_id="1", account_expiration=5))
        raise AssertionError("a 404 on /vendor was accepted")
    except PRATenantError as exc:
        assert "newer appliance" in str(exc) and "vendor API" in str(exc)
    finally:
        _restore(real)


def test_a_404_on_a_non_vendor_path_does_not_blame_the_version():
    handler, _calls = _recorder([httpx.Response(404, json={})])
    real = _serve(handler)
    try:
        _run(v.create_group_policy(_Tenant(), name="x", perms={}))
        raise AssertionError("a 404 was accepted")
    except PRATenantError as exc:
        assert "newer appliance" not in str(exc)
    finally:
        _restore(real)


def test_a_403_names_the_api_account_permission_not_the_credentials():
    handler, _calls = _recorder([httpx.Response(403, json={})])
    real = _serve(handler)
    try:
        _run(v.create_group_policy(_Tenant(), name="x", perms={}))
        raise AssertionError("a 403 was accepted")
    except PRATenantError as exc:
        assert "Configuration API permission" in str(exc)
    finally:
        _restore(real)


def test_a_delete_tolerates_an_object_somebody_already_removed_by_hand():
    """Otherwise a teardown fails forever because an operator tidied up in the appliance,
    and this dashboard holds an id it can never clear."""
    handler, _calls = _recorder([httpx.Response(404, json={})])
    real = _serve(handler)
    try:
        _run(v.delete_vendor(_Tenant(), "9"))        # must not raise
    finally:
        _restore(real)


def test_a_transport_failure_names_the_type_and_never_the_exception_text():
    def handler(request):
        if request.url.path.endswith("/oauth2/token"):
            return _token_ok(request)
        raise httpx.ConnectError("https://acme.internal/secret-ish-url refused")

    real = _serve(handler)
    try:
        _run(v.list_vendor_users(_Tenant(), "9"))
        raise AssertionError("a transport failure was accepted")
    except PRATenantError as exc:
        assert "ConnectError" in str(exc)
        assert "secret-ish-url" not in str(exc)
    finally:
        _restore(real)


# ── the source rules ─────────────────────────────────────────────────────────

def test_no_user_facing_message_interpolates_a_caught_exception():
    """`{exc}` inside a message is the CodeQL rule bt_tenant_verify._http_reason exists
    for: a caught exception's str() can carry the URL and is not a sentence anybody can
    act on. `type(exc).__name__` is the allowed form."""
    src = inspect.getsource(v)
    assert "{exc}" not in src, "a caught exception's text reaches a message"


def test_only_the_422_path_reads_the_response_body():
    """resp.text is where pra_tenant_api and pra_api_service deliberately differ, and this
    module follows the tenant-scoped one — except for _detail, which carries the
    appliance's own field-level words about the request WE built."""
    src = inspect.getsource(v)
    hits = [ln.strip() for ln in src.splitlines()
            if "resp.text" in ln and not ln.strip().startswith("#")]
    in_detail = inspect.getsource(v._detail)
    assert all(ln in in_detail for ln in hits), f"resp.text read outside _detail: {hits}"


def test_every_path_constant_is_under_the_config_api():
    for name in ("_JUMP_GROUP_PATH", "_JUMP_ITEM_ROLE_PATH", "_GROUP_POLICY_PATH",
                 "_VENDOR_PATH"):
        assert getattr(v, name).startswith("/api/config/v1/"), name


def test_the_spec_is_named_so_the_next_person_can_check_these():
    doc = v.__doc__ or ""
    assert "bt-pra-configuration.openapi.yaml" in doc
    assert "openapi.yaml" in doc


def test_this_module_reuses_the_one_token_handshake():
    """A handshake that drifts from the one the real work makes turns a green Verify into
    a lie — which is why pra_tenant_api owns it and nothing copies it."""
    from web_dashboard.services import pra_tenant_api
    assert v.get_token is pra_tenant_api.get_token
    src = inspect.getsource(v)
    assert "oauth2/token" not in src, "a second token handshake was written here"


if __name__ == "__main__":
    fns = [f for name, f in sorted(globals().items()) if name.startswith("test_")]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
    sys.exit(1 if failures else 0)
