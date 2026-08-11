"""Tests for the managed-account reads and the synced-account link in
services/ps_api_service.py — how a Password-Safe-rotated k8s ServiceAccount token
reaches the PRA Vault account a brokered session injects.

It reaches it because Password Safe puts it there. The dashboard links the two accounts
once (``link_synced_account``) and a managed account and its subscribers always share a
credential from then on. An earlier design checked the token out every 15 minutes and
wrote it across itself; the tests for that push are gone with it.

Three properties here are worth more than the rest, because each one is a silent
failure in production:

  * the link names the PARENT first in the path. Both segments are managed-account ids,
    so a swapped pair links happily and syncs backwards — pushing the PRA Vault value
    onto the cluster's token account with nothing downstream to notice;
  * linking touches no credential at all: no request, no checkout, no check-in. That is
    the property the whole change buys, so it is asserted rather than assumed;
  * a Password Safe soft-failure STRING in the credential position is refused rather
    than returned, because provisioning the tunnel with the error text breaks the
    tunnel while reporting success.

No network and no Password Safe: config_service and web_dashboard.config are stubbed
and every request goes through a fake client that records the call sequence.

Runs under pytest, or standalone:  python tests/test_ps_api_credentials.py
"""
import asyncio
import importlib.util
import json
import os
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load():
    """Import ps_api_service by file path with just enough package scaffolding.
    Same approach as tests/test_ps_api_paging.py — its ``_cfg`` reads
    ``.config_service`` then ``..config.settings``, so both must import, but neither
    needs to be real, which keeps this file runnable without SQLAlchemy or FastAPI."""
    pkg_web = sys.modules.setdefault("web_dashboard", types.ModuleType("web_dashboard"))
    pkg_web.__path__ = [os.path.join(_ROOT, "web_dashboard")]
    pkg_svc = sys.modules.setdefault("web_dashboard.services",
                                     types.ModuleType("web_dashboard.services"))
    pkg_svc.__path__ = [os.path.join(_ROOT, "web_dashboard", "services")]

    cfg_stub = types.ModuleType("web_dashboard.services.config_service")
    cfg_stub.get = lambda key: {
        "pscli_api_url": "https://ps.example.com",
        "pscli_client_id": "cid",
        "pscli_client_secret": "secret",
    }.get(key, "")
    sys.modules["web_dashboard.services.config_service"] = cfg_stub

    settings_stub = types.ModuleType("web_dashboard.config")
    settings_stub.settings = types.SimpleNamespace()
    sys.modules["web_dashboard.config"] = settings_stub

    path = os.path.join(_ROOT, "web_dashboard", "services", "ps_api_service.py")
    spec = importlib.util.spec_from_file_location(
        "web_dashboard.services.ps_api_service", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


ps = _load()

# A JWT-shaped value: three dot-separated segments, no whitespace, comfortably over
# the 40-char floor — what a real ServiceAccount token looks like in both LongLived
# and Bound mode.
TOKEN = "eyJhbGciOiJSUzI1NiIsImtpZCI6Ing" + "." + "eyJzdWIiOiJzYTpwcmEtYWNjZXNzIn0" + "." + "c2lnbmF0dXJl"


# ── fakes ───────────────────────────────────────────────────────────────────────

class FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body
        self.text = body if isinstance(body, str) else json.dumps(body)

    def json(self):
        return self._body


class FakeClient:
    """Stands in for httpx.AsyncClient, recording every call so the tests can assert
    on the request sequence — which is the point of most of them."""

    def __init__(self, *, objects=None, flat=None, pages=None, status=None,
                 put_result=(204, ""), credential=TOKEN, request_id=41):
        self.objects = objects or {}    # path -> body for a single-object GET
        self.flat = flat or {}          # path -> list for an unpaged collection
        self.pages = pages or {}        # path -> list[list[dict]] served in order
        self.status = status or {}      # path -> status override (every verb)
        self.put_result = put_result    # (status, body) for a PUT
        self.credential = credential
        self.request_id = request_id
        self.headers = {}               # _sign_in writes Authorization here
        self.calls = []                 # (METHOD, path)
        self.posts = []                 # (path, json)
        self.puts = []                  # (path, json)
        self.deletes = []               # path
        self._served = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, path, params=None):
        self.calls.append(("GET", path))
        code = self.status.get(path, 200)
        if code != 200:
            return FakeResponse(code, {"error": "nope"})
        if path.startswith("Credentials/"):
            return FakeResponse(200, self.credential)
        if path in self.objects:
            return FakeResponse(200, self.objects[path])
        if path in self.flat:
            return FakeResponse(200, self.flat[path])
        served = self._served.get(path, 0)
        pages = self.pages.get(path, [])
        body = pages[served] if served < len(pages) else []
        self._served[path] = served + 1
        return FakeResponse(200, body)

    async def post(self, path, json=None, data=None, headers=None):
        self.calls.append(("POST", path))
        self.posts.append((path, json))
        if path == "Auth/Connect/Token":
            return FakeResponse(200, {"access_token": "tok"})
        if path == "Auth/SignAppIn":
            return FakeResponse(200, {})
        code = self.status.get(path, 200)
        if path == "Requests":
            if code != 200:
                return FakeResponse(code, {"error": "denied"})
            return FakeResponse(200, {"RequestID": self.request_id})
        return FakeResponse(code, {})

    async def put(self, path, json=None):
        self.calls.append(("PUT", path))
        self.puts.append((path, json))
        if path.endswith("/Checkin"):
            return FakeResponse(204, "")
        code, body = self.put_result
        return FakeResponse(code, body)

    async def delete(self, path):
        self.calls.append(("DELETE", path))
        self.deletes.append(path)
        return FakeResponse(self.status.get(path, 200), {})


def _install(client):
    """Point both client factories at ``client`` and clear the read-shape cache."""
    ps._client = lambda: client
    ps._list_client = lambda: client
    ps._ACCOUNT_READ_SHAPE = ""
    return client


def _run(coro):
    return asyncio.run(coro)


def _acct(aid, *, platform_id=1008, last="2026-08-09T14:03:22.123Z", name="pra-access/pra-access"):
    return {"ManagedAccountID": aid, "AccountName": name, "ManagedSystemID": 77,
            "PlatformID": platform_id, "LastChangeDate": last}


# ── the poll is one session, not one per account ────────────────────────────────

def test_the_state_poll_signs_in_once_for_many_accounts():
    client = _install(FakeClient(objects={
        "ManagedAccounts/1": _acct(1), "ManagedAccounts/2": _acct(2),
        "ManagedAccounts/3": _acct(3)}))
    out = _run(ps.get_managed_account_states([1, 2, 3]))
    assert set(out) == {"1", "2", "3"}
    assert [p for p, _ in client.posts].count("Auth/Connect/Token") == 1
    assert [p for p, _ in client.posts].count("Auth/SignAppIn") == 1


def test_the_state_poll_keeps_last_change_date_verbatim():
    # Compared as an opaque string on purpose: tenants emit both "…123Z" and
    # "…+00:00", and a parse that fails either never fires or fires every pass.
    raw = "2026-08-09T14:03:22.123+00:00"
    _install(FakeClient(objects={"ManagedAccounts/5": _acct(5, last=raw)}))
    assert _run(ps.get_managed_account_states([5]))["5"]["last_change_date"] == raw


def test_an_empty_id_list_costs_no_session():
    client = _install(FakeClient())
    assert _run(ps.get_managed_account_states([])) == {}
    assert client.posts == []


# ── the by-id / scan read shapes ────────────────────────────────────────────────

def test_a_404_by_id_falls_back_to_the_paged_scan():
    client = _install(FakeClient(status={"ManagedAccounts/9": 404},
                                 pages={"ManagedAccounts": [[_acct(9)]]}))
    assert _run(ps.get_managed_account_states([9]))["9"]["account_id"] == "9"
    assert ps._ACCOUNT_READ_SHAPE == "scan"


def test_the_scan_shape_is_remembered_so_by_id_is_not_retried():
    client = _install(FakeClient(status={"ManagedAccounts/9": 404},
                                 pages={"ManagedAccounts": [[_acct(9)], [_acct(9)]]}))
    _run(ps.get_managed_account_states([9]))
    before = sum(1 for m, p in client.calls if p == "ManagedAccounts/9")
    _run(ps.get_managed_account_states([9]))
    after = sum(1 for m, p in client.calls if p == "ManagedAccounts/9")
    assert before == 1 and after == 1, "the by-id probe should not be repeated"


def test_absent_by_id_and_absent_from_the_scan_is_a_deleted_account():
    # The distinction that matters: {} means "genuinely gone" (so the caller parks the
    # cluster as unregistered), NOT "this build has no by-id route".
    _install(FakeClient(status={"ManagedAccounts/9": 404},
                        pages={"ManagedAccounts": [[]]}))
    assert _run(ps.get_managed_account_states([9]))["9"] == {}


def test_a_transport_failure_is_not_reported_as_absence():
    # "Password Safe is down" must never look like "every account was deleted".
    _install(FakeClient(status={"ManagedAccounts/9": 500}))
    try:
        _run(ps.get_managed_account_states([9]))
    except ps.PSApiError as exc:
        assert "500" in str(exc)
    else:
        raise AssertionError("a 500 must raise, not return {}")


# ── the synced-account link ─────────────────────────────────────────────────────
#
# Nothing below checks out a credential, and that is the headline: Password Safe moves
# the value between the two accounts, so the dashboard's whole involvement is one POST.

def _linked(parent=1, sub=7, sub_platform=1010, platform_name="PRA Vault Token",
            listed=(7,), sub_last="2026-08-11T09:00:00Z"):
    """A client where account ``sub`` is on the PRA Vault Token platform and the
    parent's synced list contains ``listed``."""
    return FakeClient(
        objects={f"ManagedAccounts/{sub}": _acct(sub, platform_id=sub_platform),
                 f"ManagedAccounts/{parent}": _acct(parent, last="2026-08-11T08:00:00Z")},
        flat={"Platforms": [{"PlatformID": sub_platform, "Name": platform_name}],
              f"ManagedAccounts/{parent}/SyncedAccounts":
                  [_acct(a, last=sub_last) for a in listed]})


def test_the_link_puts_the_parent_first_in_the_path():
    """THE test in this file. Both segments are managed-account ids, so a swapped pair
    links successfully and syncs BACKWARDS — the PRA Vault account's value would be
    pushed onto the cluster's token account, and nothing downstream would notice."""
    client = _install(_linked())
    out = _run(ps.link_synced_account(parent_account_id=1, synced_account_id=7))
    assert ("POST", "ManagedAccounts/1/SyncedAccounts/7") in client.calls, (
        f"expected POST ManagedAccounts/1/SyncedAccounts/7, got {client.calls}. "
        f"{{id}} is the PARENT (ps-cli -ma-id); {{syncedAccountID}} the subscriber (-sa-id)")
    assert out["linked"] is True and out["confirmed"] is True


def test_the_link_is_confirmed_by_re_reading_the_subscriber_list():
    client = _install(_linked())
    _run(ps.link_synced_account(parent_account_id=1, synced_account_id=7))
    assert ("GET", "ManagedAccounts/1/SyncedAccounts") in client.calls, (
        "a 200 on the POST is not proof it took — the confirm read is the point")


def test_an_accepted_post_that_did_not_take_is_reported_unconfirmed():
    # 200 back, but the parent's synced list does not contain the subscriber.
    _install(_linked(listed=()))
    out = _run(ps.link_synced_account(parent_account_id=1, synced_account_id=7))
    assert out["linked"] is True and out["confirmed"] is False, (
        "an unconfirmed link must be reported so registration can refuse to continue")


def test_a_refused_link_names_both_documented_grants():
    # The REST reference says Account Management (Full control); ps-cli says Role
    # Management (Read/Write). An operator chasing a 403 needs to be told both.
    _install(FakeClient(status={"ManagedAccounts/1/SyncedAccounts/7": 403}))
    try:
        _run(ps.link_synced_account(parent_account_id=1, synced_account_id=7))
    except ps.PSApiError as exc:
        msg = str(exc)
        assert "403" in msg and "Account Management" in msg and "Role Management" in msg
    else:
        raise AssertionError("a 403 on the link must raise")


def test_linking_an_account_to_itself_is_refused():
    client = _install(_linked())
    try:
        _run(ps.link_synced_account(parent_account_id=7, synced_account_id=7))
    except ps.PSApiError as exc:
        assert "itself" in str(exc)
    else:
        raise AssertionError("a self-link must be refused")
    assert client.posts == [] or all("SyncedAccounts" not in p for p, _ in client.posts)


def test_the_link_needs_no_credential_at_all():
    """The whole reason this change exists: no request, no checkout, no check-in."""
    client = _install(_linked())
    _run(ps.link_synced_account(parent_account_id=1, synced_account_id=7))
    for method, path in client.calls:
        assert not path.startswith(("Requests", "Credentials")), (
            f"the link touched {method} {path} — Password Safe moves the value between "
            f"the two accounts; the dashboard must never hold it")
    assert client.puts == []


# ── the unlink ──────────────────────────────────────────────────────────────────

def test_the_unlink_uses_the_same_path_shape():
    client = _install(FakeClient())
    assert _run(ps.unlink_synced_account(parent_account_id=1, synced_account_id=7)) is True
    assert client.deletes == ["ManagedAccounts/1/SyncedAccounts/7"]


def test_an_already_absent_link_is_not_an_error():
    # deregister() unlinks before off-boarding; a link that is already gone is the
    # desired end state, and raising would mask the errors that matter.
    _install(FakeClient(status={"ManagedAccounts/1/SyncedAccounts/7": 404}))
    assert _run(ps.unlink_synced_account(parent_account_id=1, synced_account_id=7)) is False


def test_a_refused_unlink_still_raises():
    _install(FakeClient(status={"ManagedAccounts/1/SyncedAccounts/7": 403}))
    try:
        _run(ps.unlink_synced_account(parent_account_id=1, synced_account_id=7))
    except ps.PSApiError as exc:
        assert "403" in str(exc)
    else:
        raise AssertionError("a 403 on the unlink must raise")


# ── the live status read ────────────────────────────────────────────────────────

def test_the_status_read_reports_linked_and_both_change_dates():
    _install(_linked())
    out = _run(ps.synced_account_status(parent_account_id=1, synced_account_id=7))
    assert out["linked"] is True
    assert out["parent_exists"] is True
    assert out["parent_last_change"] == "2026-08-11T08:00:00Z"
    assert out["subscriber_last_change"] == "2026-08-11T09:00:00Z"
    assert out["subscriber_count"] == 1


def test_the_status_read_reports_unlinked_when_the_subscriber_is_absent():
    # An admin unlinking in the Password Safe console is exactly what this surfaces.
    _install(_linked(listed=(9,)))
    out = _run(ps.synced_account_status(parent_account_id=1, synced_account_id=7))
    assert out["linked"] is False
    assert out["subscriber_count"] == 1, "another account is synced, just not ours"


def test_the_status_read_takes_one_session_and_no_credential():
    client = _install(_linked())
    _run(ps.synced_account_status(parent_account_id=1, synced_account_id=7))
    assert sum(1 for _, p in client.calls if p == "Auth/SignAppIn") == 1
    for _, path in client.calls:
        assert not path.startswith(("Requests", "Credentials"))


def test_rotate_on_check_in_is_never_requested():
    # A static assertion because the consequence is silent, and truer under synced
    # accounts than before: a credential change on EITHER member of a synced pair
    # re-rotates both, so rotate-on-release would rotate the real cluster token every
    # time the tunnel reads it.
    src = open(os.path.join(_ROOT, "web_dashboard", "services", "ps_api_service.py"),
               encoding="utf-8").read().lower()
    assert "rotateoncheckin" not in src


# ── the shape guard ─────────────────────────────────────────────────────────────

def test_a_soft_failure_string_is_refused_instead_of_returned():
    soft = "It was not possible to get a credential for Request ID: 5"
    _install(FakeClient(credential=soft))
    try:
        _run(ps.checkout_credential(1))
    except ps.PSApiError as exc:
        assert "not a ServiceAccount token" in str(exc)
    else:
        raise AssertionError(
            "a non-token value must be refused — provisioning the tunnel with the error "
            "text breaks the tunnel while the job reports success")


def test_the_shape_guard_accepts_a_real_looking_token_and_rejects_the_obvious():
    assert ps._looks_like_sa_token(TOKEN)
    assert not ps._looks_like_sa_token("")
    assert not ps._looks_like_sa_token("short.a.b")
    assert not ps._looks_like_sa_token("a" * 60)                     # no segments
    assert not ps._looks_like_sa_token("a" * 40 + ".b.c\n")          # whitespace


# ── the platform guard fails closed ─────────────────────────────────────────────

def test_a_subscriber_on_the_wrong_platform_is_refused():
    client = _install(_linked(sub_platform=1009,
                              platform_name="PRA Vault Username Password"))
    try:
        _run(ps.link_synced_account(parent_account_id=1, synced_account_id=7,
                                    expect_subscriber_platform="PRA Vault Token"))
    except ps.PSApiError as exc:
        assert "PRA Vault Username Password" in str(exc)
    else:
        raise AssertionError("a wrong-platform subscriber must be refused")
    assert all("SyncedAccounts" not in p for p, _ in client.posts), (
        "syncing a Kubernetes bearer token to an account managed by some other plugin "
        "puts a secret somewhere it does not belong")


def test_a_subscriber_on_the_right_platform_is_accepted():
    client = _install(_linked())
    out = _run(ps.link_synced_account(parent_account_id=1, synced_account_id=7,
                                      expect_subscriber_platform="PRA Vault Token"))
    assert out["confirmed"] is True
    assert ("POST", "ManagedAccounts/1/SyncedAccounts/7") in client.calls


def test_a_missing_subscriber_account_is_named():
    _install(FakeClient(status={"ManagedAccounts/7": 404},
                        pages={"ManagedAccounts": [[]]}))
    try:
        _run(ps.link_synced_account(parent_account_id=1, synced_account_id=7,
                                    expect_subscriber_platform="PRA Vault Token"))
    except ps.PSApiError as exc:
        assert "does not exist" in str(exc)
    else:
        raise AssertionError("a deleted subscriber account must raise")


def test_platform_matching_tolerates_a_rename_but_not_a_different_plugin():
    # "Azure VM SSH Rotation" once became "Azure Waagent VM SSH Rotation" overnight and
    # silently switched VM onboarding off; a contiguous-substring check is too strict.
    assert ps._platform_matches("PRA Vault Token (k8s)", "PRA Vault Token")
    assert ps._platform_matches("pra vault token", "PRA Vault Token")
    assert not ps._platform_matches("PRA Vault Username Password", "PRA Vault Token")
    assert not ps._platform_matches("", "PRA Vault Token")


# ── the refusal message is the deliverable ──────────────────────────────────────

def test_a_denied_request_names_the_tenant_side_grant():
    _install(FakeClient(status={"Requests": 403}))
    try:
        _run(ps.checkout_credential(1))
    except ps.PSApiError as exc:
        msg = str(exc)
        assert "Requestor" in msg and "Smart Rule" in msg and "View" in msg
    else:
        raise AssertionError("a 403 on POST Requests must raise")


def test_a_withheld_credential_says_the_request_may_await_approval():
    _install(FakeClient(status={"Credentials/41": 403}))
    try:
        _run(ps.checkout_credential(1))
    except ps.PSApiError as exc:
        assert "awaiting approval" in str(exc) or "auto-release" in str(exc)
    else:
        raise AssertionError("a withheld credential must raise")


def test_the_tunnel_checkout_is_reuse_and_view_and_checks_back_in():
    client = _install(FakeClient())
    assert _run(ps.checkout_credential(1)) == TOKEN
    body = next(b for p, b in client.posts if p == "Requests")
    assert body["ConflictOption"] == "reuse", (
        "without reuse a second attempt 409s, and a 409 body was once mis-parsed as a "
        "request id")
    assert body["AccessType"] == "View" and body["SystemID"] == 0
    assert any(p == "Requests/41/Checkin" for _, p in client.calls)


# ── the functional account exposes its own name ─────────────────────────────────

def test_a_functional_account_reports_its_account_name():
    # The GKE rotator ClusterRoleBinding subject IS this value (the service-account
    # email), so returning it here removes the need for a config key that only repeats
    # the functional account's name.
    client = _install(FakeClient(
        flat={"FunctionalAccounts": [{"FunctionalAccountID": 88, "PlatformID": 1008,
                                      "AccountName": "psafe-rotator@p.iam.gserviceaccount.com"}],
              "Platforms": [{"PlatformID": 1008, "Name": "Kubernetes Service Account Token"}]}))
    out = _run(ps.get_functional_account("psafe-rotator@p.iam.gserviceaccount.com"))
    assert out["account_name"] == "psafe-rotator@p.iam.gserviceaccount.com"
    assert out["id"] == 88
    assert out["platform_name"] == "Kubernetes Service Account Token"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
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
