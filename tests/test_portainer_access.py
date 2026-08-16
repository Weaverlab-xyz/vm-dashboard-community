"""Portainer access management — users, teams, memberships.

Groundwork for just-in-time Portainer access (Cloud Functions Phase 2, integration
#3). Everything else in portainer_service manages WORKLOADS; this manages WHO CAN
REACH THEM, so the properties worth pinning are the ones that fail dangerously:
roles that silently invert, revokes that error on an already-removed grant, and
duplicate memberships that make the first revoke look successful while access
remains.

Exercised against a fake transport, so no Portainer instance is needed.
"""
import asyncio
import json
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

try:
    import httpx  # noqa: F401
except ImportError:
    # Stub it rather than skipping. Access management decides WHO CAN REACH the
    # container fleet, so "these tests didn't run here" is not an acceptable state
    # to ship in — and nothing under test opens a socket anyway.
    _httpx = types.ModuleType("httpx")

    class _HTTPError(Exception):
        pass

    _httpx.HTTPError = _HTTPError
    _httpx.TimeoutException = type("TimeoutException", (_HTTPError,), {})
    _httpx.ConnectError = type("ConnectError", (_HTTPError,), {})
    _httpx.Response = type("Response", (), {})
    _httpx.AsyncClient = type("AsyncClient", (), {})
    sys.modules["httpx"] = _httpx


def _stub(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


try:
    import pydantic  # noqa: F401
except ImportError:
    # portainer_service pulls in ..config (pydantic) and two sibling services it
    # does not use on any path under test here.
    _stub("web_dashboard.config", settings=types.SimpleNamespace(
        portainer_url="", portainer_pat="", portainer_verify_ssl=False,
        portainer_pat_secret_title=""))
    _stub("web_dashboard.services.btapi_service", get_ps_secret=lambda *a, **k: "")
    _stub("web_dashboard.services.cache_service",
          key_param=lambda *a, **k: "k", TTL={}, get_or_refresh=None,
          invalidate_prefix=None)

from web_dashboard.services import portainer_service as ps

CALLS = []          # every (method, path, body) the service issued
RESPONSES = {}      # (method, path) -> body to return


def _reset():
    CALLS.clear()
    RESPONSES.clear()
    ps._EXECUTION_MODE = "local"


async def _fake_api(method, path, *, json_body=None, ok=(200, 201, 204), context=""):
    CALLS.append((method, path, json_body))
    return RESPONSES.get((method, path), RESPONSES.get(path))


def _patch_api():
    ps._api = _fake_api


_REAL_API = ps._api
_patch_api()


def _run(coro):
    return asyncio.run(coro)


def _paths():
    return [(m, p) for m, p, _b in CALLS]


# ── Lookups ──────────────────────────────────────────────────────────────────

def test_find_user_is_case_insensitive():
    """Portainer stores the name as given but treats logins case-insensitively, so
    a case-sensitive lookup would create a second 'Alice' beside 'alice' and grant
    access to the wrong one."""
    _reset()
    RESPONSES["/api/users"] = [{"Id": 3, "Username": "Alice"}]
    assert _run(ps.find_user("alice"))["Id"] == 3
    assert _run(ps.find_user("ALICE"))["Id"] == 3
    assert _run(ps.find_user("  alice  "))["Id"] == 3


def test_find_user_returns_empty_for_unknown_and_blank():
    _reset()
    RESPONSES["/api/users"] = [{"Id": 3, "Username": "alice"}]
    assert _run(ps.find_user("bob")) == {}
    assert _run(ps.find_user("")) == {}
    assert _run(ps.find_user(None)) == {}


def test_find_team_is_case_insensitive():
    _reset()
    RESPONSES["/api/teams"] = [{"Id": 7, "Name": "Platform"}]
    assert _run(ps.find_team("platform"))["Id"] == 7
    assert _run(ps.find_team("nope")) == {}


def test_list_helpers_tolerate_a_non_list_body():
    """A proxy error page or an empty 200 must not blow up a grant."""
    _reset()
    for value in (None, {}, "", {"message": "boom"}):
        RESPONSES["/api/users"] = value
        RESPONSES["/api/teams"] = value
        RESPONSES["/api/team_memberships"] = value
        assert _run(ps.list_users()) == []
        assert _run(ps.list_teams()) == []
        assert _run(ps.list_team_memberships()) == []


# ── Roles: the numeric footgun ───────────────────────────────────────────────

def test_user_creation_defaults_to_standard_never_admin():
    """1 = administrator and 2 = standard. A JIT grant that hands out
    administrator is not an error Portainer reports."""
    _reset()
    _run(ps.create_user("alice", "pw"))
    _method, _path, body = CALLS[-1]
    assert body["Role"] == ps.USER_ROLE_STANDARD == 2
    assert body["Role"] != ps.USER_ROLE_ADMIN


def test_team_membership_defaults_to_member_never_leader():
    _reset()
    RESPONSES["/api/team_memberships"] = []
    _run(ps.add_team_member(3, 7))
    _method, _path, body = CALLS[-1]
    assert body["Role"] == ps.TEAM_ROLE_MEMBER == 2
    assert body["Role"] != ps.TEAM_ROLE_LEADER


def test_an_out_of_range_role_is_refused_rather_than_passed_through():
    """Portainer accepts whatever integer it is given; an unknown value must not
    reach it."""
    _reset()
    for role in (0, 3, -1, 99):
        try:
            _run(ps.create_user("alice", "pw", role=role))
        except ps.PortainerError:
            pass
        else:
            raise AssertionError(f"create_user accepted role {role}")
        try:
            RESPONSES["/api/team_memberships"] = []
            _run(ps.add_team_member(3, 7, role=role))
        except ps.PortainerError:
            pass
        else:
            raise AssertionError(f"add_team_member accepted role {role}")


# ── Idempotence, in both directions ──────────────────────────────────────────

def test_adding_an_existing_member_does_not_duplicate_it():
    """Portainer will happily create a second membership row, and then the FIRST
    revoke looks successful while access remains."""
    _reset()
    RESPONSES["/api/team_memberships"] = [{"Id": 11, "UserID": 3, "TeamID": 7, "Role": 2}]
    result = _run(ps.add_team_member(3, 7))
    assert result["Id"] == 11
    assert ("POST", "/api/team_memberships") not in _paths(), \
        "a duplicate membership was created"


def test_removing_a_member_who_is_not_one_is_success_not_an_error():
    """Entitle retries. A revoke that errors on an already-removed grant leaves the
    caller retrying forever and the access looking un-revoked."""
    _reset()
    RESPONSES["/api/team_memberships"] = []
    assert _run(ps.remove_team_member(3, 7)) is False
    assert not [p for m, p in _paths() if m == "DELETE"]


def test_removing_an_existing_member_deletes_by_membership_id():
    _reset()
    RESPONSES["/api/team_memberships"] = [{"Id": 11, "UserID": 3, "TeamID": 7}]
    assert _run(ps.remove_team_member(3, 7)) is True
    assert ("DELETE", "/api/team_memberships/11") in _paths()


def test_membership_lookup_matches_both_ids_not_just_one():
    """Matching on the user alone would revoke every team they belong to."""
    _reset()
    RESPONSES["/api/team_memberships"] = [
        {"Id": 11, "UserID": 3, "TeamID": 7},
        {"Id": 12, "UserID": 3, "TeamID": 8},
        {"Id": 13, "UserID": 4, "TeamID": 7},
    ]
    assert _run(ps.find_membership(3, 8))["Id"] == 12
    assert _run(ps.find_membership(4, 7))["Id"] == 13
    assert _run(ps.find_membership(5, 7)) == {}


def test_membership_lookup_survives_string_ids():
    """Portainer's JSON has returned these as strings in some versions; an int/str
    mismatch would silently never find the membership, so every revoke would be a
    no-op that reports success."""
    _reset()
    RESPONSES["/api/team_memberships"] = [{"Id": 11, "UserID": "3", "TeamID": "7"}]
    assert _run(ps.find_membership(3, 7))["Id"] == 11


def test_deleting_a_missing_user_is_success():
    _reset()
    _run(ps.delete_user(3))
    assert ("DELETE", "/api/users/3") in _paths()


# ── The automation transport ─────────────────────────────────────────────────

def test_the_request_body_actually_reaches_the_automation_proxy():
    """The proxy takes the body as an already-serialized STRING, not an object. A
    dict passed there is silently dropped and surfaces at the far end as a
    Portainer validation error rather than a missing payload — so this asserts on
    the real _api, not the fake."""
    ps._api = _REAL_API
    ps._EXECUTION_MODE = "automation"
    seen = {}

    async def _fake_proxy(method, url, headers, body="", content_type="application/json",
                          form_data=""):
        seen.update({"method": method, "url": url, "body": body})
        return {"status_code": 200, "body": {"Id": 9}}

    async def _fake_url_headers():
        return ("https://portainer.example", {"X-API-Key": "k"})

    real_proxy, real_url = ps._proxy_request, ps._portainer_url_and_headers
    ps._proxy_request, ps._portainer_url_and_headers = _fake_proxy, _fake_url_headers
    try:
        _run(ps.create_user("alice", "pw"))
    finally:
        ps._proxy_request, ps._portainer_url_and_headers = real_proxy, real_url
        ps._EXECUTION_MODE = "local"
        _patch_api()

    assert seen["method"] == "POST"
    assert seen["url"].endswith("/api/users")
    assert isinstance(seen["body"], str), "the body must be serialized for the runbook"
    assert json.loads(seen["body"])["Username"] == "alice"
    assert json.loads(seen["body"])["Role"] == ps.USER_ROLE_STANDARD


def test_role_constants_are_distinct_and_documented():
    """Both scales are ' 1 is the powerful one', which is exactly why bare integers
    are banned at call sites."""
    assert ps.USER_ROLE_ADMIN == 1 and ps.USER_ROLE_STANDARD == 2
    assert ps.TEAM_ROLE_LEADER == 1 and ps.TEAM_ROLE_MEMBER == 2
    source = open(ps.__file__, encoding="utf-8").read()
    assert "ROLE IDS ARE NUMERIC AND EASY TO INVERT" in source


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
