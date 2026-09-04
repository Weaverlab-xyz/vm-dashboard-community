"""Portainer just-in-time access: the shared rules, and the Entitle adapter.

Portainer has no Entitle connector at all, so this adapter is the only route to JIT
access for it. Two things are pinned here:

  * the matching rules, which are where the dangerous bugs live — a case-sensitive
    lookup grants the wrong Alice, a membership matched on the user alone revokes
    every team they belong to, string ids make every revoke a silent no-op
  * the adapter's blast-radius guards — it must only ever see, and only ever
    delete, accounts it minted itself

Runs offline against a fake transport; no Portainer instance needed.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from web_dashboard import functions  # noqa: F401  (puts fnruntime on sys.path)
from fnruntime.contract import Context, Request, Response
from web_dashboard.services import portainer_access_rules as rules
from fnworkloads import portainer_access as adapter

_ENV_KEYS = ("FN_PORTAINER_URL", "FN_PORTAINER_API_KEY", "FN_PORTAINER_VERIFY_SSL",
             "FN_PORTAINER_DRY_RUN")

STATE = {}
CALLS = []


def _env(**overrides):
    for key in _ENV_KEYS:
        os.environ.pop(key, None)
    base = {"FN_PORTAINER_URL": "https://portainer.internal",
            "FN_PORTAINER_API_KEY": "ptr_key",
            "FN_PORTAINER_DRY_RUN": "0"}
    base.update(overrides)
    for key, value in base.items():
        os.environ[key] = value
    CALLS.clear()
    STATE.clear()
    STATE.update({
        "/api/users": [
            {"Id": 1, "Username": "admin"},
            {"Id": 2, "Username": "jit-alice-example-com-abc"},
        ],
        "/api/teams": [{"Id": 7, "Name": "Platform"}],
        "/api/team_memberships": [{"Id": 11, "UserID": 2, "TeamID": 7, "Role": 2}],
    })


def _fake_api(config, method, path, body=None):
    CALLS.append((method, path, body))
    if method == "GET":
        return STATE.get(path, [])
    return {"Id": 99}


adapter._api = _fake_api


def _call(method, path, payload=None):
    return adapter.handle(
        Request(method=method, path=path, headers={}, query={},
                body=json.dumps(payload or {}).encode(), source="aws_function_url"),
        Context.from_env(workload="portainer_access"))


def _paths():
    return [(m, p) for m, p, _b in CALLS]


# ── The shared rules ─────────────────────────────────────────────────────────

def test_user_matching_is_case_insensitive():
    users = [{"Id": 3, "Username": "Alice"}]
    for probe in ("alice", "ALICE", "  Alice  "):
        assert rules.match_user(users, probe)["Id"] == 3, probe
    assert rules.match_user(users, "bob") == {}
    assert rules.match_user(users, "") == {}
    assert rules.match_user(None, "alice") == {}


def test_membership_matching_needs_both_ids():
    """Matching on the user alone would revoke every team they belong to."""
    memberships = [{"Id": 11, "UserID": 3, "TeamID": 7},
                   {"Id": 12, "UserID": 3, "TeamID": 8}]
    assert rules.match_membership(memberships, 3, 8)["Id"] == 12
    assert rules.match_membership(memberships, 3, 9) == {}
    assert rules.match_membership(memberships, 4, 7) == {}


def test_membership_matching_survives_string_ids():
    """Portainer has returned these as strings; an int/str mismatch makes every
    revoke a silent no-op that reports success."""
    memberships = [{"Id": 11, "UserID": "3", "TeamID": "7"}]
    assert rules.match_membership(memberships, 3, 7)["Id"] == 11
    assert rules.match_membership(memberships, "3", "7")["Id"] == 11


def test_role_validation_refuses_anything_off_the_scale():
    """Portainer accepts any integer, and 1 is the powerful one on both scales."""
    assert rules.validate_user_role(rules.USER_ROLE_STANDARD) == 2
    assert rules.validate_team_role(rules.TEAM_ROLE_MEMBER) == 2
    for bad in (0, 3, -1, 99, "x", None):
        for validate in (rules.validate_user_role, rules.validate_team_role):
            try:
                validate(bad)
            except rules.PortainerRuleError:
                pass
            else:
                raise AssertionError(f"{validate.__name__} accepted {bad!r}")


def test_ephemeral_usernames_are_traceable_safe_and_unique():
    name = rules.ephemeral_username("alice@example.com", "ABC123")
    assert name.startswith("jit-alice-example-com")
    assert rules.validate_username(name) == name
    assert rules.is_ephemeral_username(name)
    assert name != rules.ephemeral_username("alice@example.com", "ZZZ999")


def test_ephemeral_usernames_survive_hostile_input():
    for identity in ("", "!!!", "a" * 300, "123", "../../etc/passwd"):
        name = rules.ephemeral_username(identity, "tok")
        rules.validate_username(name)
        assert rules.is_ephemeral_username(name)


def test_a_real_account_is_not_mistaken_for_an_ephemeral_one():
    for username in ("admin", "alice", "jitter", "", None):
        assert not rules.is_ephemeral_username(username), username


def test_the_rules_module_is_safe_to_ship_into_a_function():
    """It is vendored into the zip verbatim, so it must be stdlib-only."""
    import ast
    tree = ast.parse(open(rules.__file__, encoding="utf-8").read())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "a relative import cannot be vendored"
            if node.module:
                imported.add(node.module.split(".")[0])
    assert imported <= {"re"}, f"non-stdlib imports: {imported}"


def test_the_dashboard_client_uses_the_same_rules():
    """One copy, so the client and the adapter cannot disagree about which user a
    name refers to."""
    source = open(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "web_dashboard",
                     "services", "portainer_service.py"), encoding="utf-8").read()
    for call in ("_rules.match_user(", "_rules.match_team(", "_rules.match_membership(",
                 "_rules.validate_user_role(", "_rules.validate_team_role("):
        assert call in source, f"portainer_service does not delegate {call}"


# ── Blast radius ─────────────────────────────────────────────────────────────

def test_get_actors_lists_only_accounts_this_adapter_minted():
    """Listing an operator's real users to Entitle would offer them up for deletion
    through a grant integration."""
    _env()
    actors = _call("GET", "/get_actors").body["data"]["actors"]
    names = {a["identifier"] for a in actors}
    assert names == {"jit-alice-example-com-abc"}
    assert "admin" not in names


def test_delete_actor_refuses_an_account_it_did_not_create():
    """The guard that stops a grant integration deleting a real Portainer account."""
    _env()
    resp = _call("POST", "/delete_actor", {"actor_identifier": "admin"})
    assert resp.status == 403, resp.body
    assert not [p for m, p in _paths() if m == "DELETE"]


def test_membership_changes_refuse_a_foreign_account():
    _env()
    for path in ("/give_access", "/revoke_access"):
        resp = _call("POST", path, {"actor_identifier": "admin", "role_code": "Platform"})
        assert resp.status == 403, (path, resp.body)


def test_create_actor_makes_a_standard_user_in_no_team():
    """An actor whose give_access never arrives must be able to reach nothing."""
    _env()
    body = _call("POST", "/create_actor",
                 {"actor": {"email": "bob@example.com"}}).body["data"]
    posted = [b for m, p, b in CALLS if m == "POST" and p == "/api/users"]
    assert posted and posted[0]["Role"] == rules.USER_ROLE_STANDARD
    assert not [p for m, p in _paths() if p == "/api/team_memberships"], \
        "create_actor put the user in a team"
    assert rules.is_ephemeral_username(body["actor"]["identifier"])


# ── Idempotence, both directions ─────────────────────────────────────────────

def test_giving_access_twice_does_not_duplicate_the_membership():
    """A duplicate row makes the FIRST revoke look successful while access remains."""
    _env()
    body = _call("POST", "/give_access", {
        "actor_identifier": "jit-alice-example-com-abc", "role_code": "Platform"}).body
    assert body["data"]["already_member"] is True
    assert ("POST", "/api/team_memberships") not in _paths()


def test_revoking_access_that_is_not_held_is_success():
    _env()
    STATE["/api/team_memberships"] = []
    body = _call("POST", "/revoke_access", {
        "actor_identifier": "jit-alice-example-com-abc", "role_code": "Platform"}).body
    assert body["data"]["already_absent"] is True
    assert not [p for m, p in _paths() if m == "DELETE"]


def test_revoking_a_held_membership_deletes_by_membership_id():
    _env()
    _call("POST", "/revoke_access", {
        "actor_identifier": "jit-alice-example-com-abc", "role_code": "Platform"})
    assert ("DELETE", "/api/team_memberships/11") in _paths()


def test_deleting_an_absent_actor_is_success():
    _env()
    STATE["/api/users"] = [{"Id": 1, "Username": "admin"}]
    body = _call("POST", "/delete_actor",
                 {"actor_identifier": "jit-gone-abc"}).body
    assert body["data"]["already_absent"] is True


# ── Routing and contract ─────────────────────────────────────────────────────

def test_every_contract_route_is_served():
    _env()
    for method, path in [
        ("GET", "/get_assets"), ("GET", "/get_actors"), ("GET", "/get_all_permissions"),
        ("POST", "/create_actor"), ("POST", "/delete_actor"),
        ("POST", "/give_access"), ("POST", "/revoke_access"), ("POST", "/check_config"),
    ]:
        payload = {"actor": {"email": "a@example.com"},
                   "actor_identifier": "jit-alice-example-com-abc",
                   "role_code": "Platform"}
        assert _call(method, path, payload).status != 404, f"{method} {path}"


def test_assets_are_teams_and_offer_the_team_name_as_the_role():
    """An operator reading an Entitle request should see the team they configured
    access on."""
    _env()
    asset = _call("GET", "/get_assets").body["data"]["assets"][0]
    assert asset["identifier"] == "portainer:team:7"
    assert asset["role_options"][0]["code"] == "Platform"


def test_an_unknown_team_is_a_404_not_a_silent_grant():
    _env()
    resp = _call("POST", "/give_access", {
        "actor_identifier": "jit-alice-example-com-abc", "role_code": "NoSuchTeam"})
    assert resp.status == 404, resp.body


def test_check_config_flags_a_portainer_with_no_teams():
    """Access is granted through team membership, so no teams means nothing to
    grant — worth saying at setup rather than at the first request."""
    _env()
    STATE["/api/teams"] = []
    body = _call("POST", "/check_config", {"config": {}}).body["data"]
    assert body["valid"] is False
    assert any("no teams" in p for p in body["problems"])


def test_dry_run_touches_nothing():
    _env(FN_PORTAINER_DRY_RUN="1")
    for path, payload in (("/create_actor", {"actor": {"email": "a@example.com"}}),
                          ("/give_access", {"actor_identifier": "jit-a-1",
                                            "role_code": "Platform"}),
                          ("/delete_actor", {"actor_identifier": "jit-a-1"})):
        body = _call("POST", path, payload).body["data"]
        # create_actor nests its report in login_info (fnruntime.entitle); the other
        # two routes keep it at the top of data.
        assert body.get("login_info", body)["dry_run"] is True, path
    assert not [p for m, p in _paths() if m in ("POST", "DELETE")], CALLS


def test_a_missing_url_surfaces_rather_than_defaulting():
    """Never a default, and never a raise either.

    A raise out of handle() is what dispatch turns into ``500 internal error``, so it
    surfaces to the LOG and not to the operator. The setting has to reach the caller.
    """
    _env(FN_PORTAINER_URL="")
    resp = _call("GET", "/get_assets")
    assert resp.status == 500, resp.status
    assert resp.body["error"] == "function not configured", resp.body
    assert "FN_PORTAINER_URL" in resp.body["problem"], resp.body


def test_check_config_reports_a_missing_url_instead_of_500ing_on_it():
    """_config() used to run BEFORE routing, so an unconfigured adapter raised on
    every path — including this one, whose only job is to say what is wrong."""
    _env(FN_PORTAINER_URL="")
    resp = _call("POST", "/check_config")
    assert resp.status == 200, (resp.status, resp.body)
    data = resp.body["data"]
    assert data["valid"] is False, data
    assert any("FN_PORTAINER_URL" in p for p in data["problems"]), data


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
