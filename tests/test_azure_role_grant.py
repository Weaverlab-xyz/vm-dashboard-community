"""Azure machine-identity access: the rules, and the Entitle adapter.

AWS machine identity works because Entitle attaches an IAM policy to the dashboard's
IAM user. Azure has no equivalent — a machine identity there is an application, and
an application cannot be the account privileges are requested for — so this adapter
is the only route.

It is also, if unbounded, a privilege-escalation primitive. Most of what is pinned
here is the bounding: allowlisted scopes, allowlisted roles, and an outright refusal
of the three roles that let a grantee grant themselves more.

Runs offline against a fake ARM transport.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from web_dashboard import functions  # noqa: F401  (puts fnruntime on sys.path)
from fnruntime.contract import Context, Request
from web_dashboard.services import azure_role_rules as rules
from fnworkloads import azure_role_grant as adapter

SUB = "11111111-2222-3333-4444-555555555555"
SCOPE = f"/subscriptions/{SUB}/resourceGroups/prod"
PRINCIPAL = "99999999-8888-7777-6666-555555555555"

_ENV_KEYS = ("FN_AZURE_TENANT_ID", "FN_AZURE_CLIENT_ID", "FN_AZURE_CLIENT_SECRET",
             "FN_AZURE_SUBSCRIPTION_ID", "FN_AZURE_SCOPES", "FN_AZURE_ROLES",
             "FN_AZURE_PRINCIPALS", "FN_AZURE_DRY_RUN")

CALLS = []
EXISTING = set()


def _env(**overrides):
    for key in _ENV_KEYS:
        os.environ.pop(key, None)
    base = {
        "FN_AZURE_TENANT_ID": "t", "FN_AZURE_CLIENT_ID": "c",
        "FN_AZURE_CLIENT_SECRET": "s", "FN_AZURE_SUBSCRIPTION_ID": SUB,
        "FN_AZURE_SCOPES": SCOPE, "FN_AZURE_ROLES": "reader,contributor",
        "FN_AZURE_PRINCIPALS": f"{PRINCIPAL}=deploy-sp",
        "FN_AZURE_DRY_RUN": "0",
    }
    base.update(overrides)
    for key, value in base.items():
        os.environ[key] = value
    CALLS.clear()
    EXISTING.clear()


def _fake_arm(config, method, path, body=None, ok_missing=False):
    CALLS.append((method, path, body))
    if method == "GET":
        return {"id": path} if path in EXISTING else None
    if method == "PUT":
        EXISTING.add(path)
        return {"id": path}
    if method == "DELETE":
        EXISTING.discard(path)
    return None


adapter._arm = _fake_arm


def _call(method, path, payload=None):
    return adapter.handle(
        Request(method=method, path=path, headers={}, query={},
                body=json.dumps(payload or {}).encode(), source="aws_function_url"),
        Context.from_env(workload="azure_role_grant"))


def _grant(role="reader", scope=SCOPE, actor=PRINCIPAL):
    return _call("POST", "/give_access", {
        "asset": {"identifier": f"azure:scope:{scope}"},
        "actor_identifier": actor, "role_code": role})


# ── The escalation guard ─────────────────────────────────────────────────────

def test_escalation_roles_are_refused_even_when_allowlisted():
    """A time-boxed grant of Owner is not time-boxed: the grantee can make it
    permanent before it expires. Configuration must not be able to turn this off."""
    for role in ("owner", "user access administrator",
                 "role based access control administrator"):
        _env(FN_AZURE_ROLES=f"reader,{role}")
        resp = _grant(role=role)
        assert resp.status == 403, (role, resp.body)
        assert "grant further access" in resp.body["error"]


def test_escalation_roles_are_refused_by_guid_too():
    """Naming Owner by its GUID must not slip past a name-based check."""
    _env(FN_AZURE_ROLES=f"reader,{rules.BUILTIN_ROLES['owner']}")
    assert _grant(role=rules.BUILTIN_ROLES["owner"]).status == 403


def test_escalation_roles_are_never_offered_as_assets():
    _env(FN_AZURE_ROLES="reader,owner")
    asset = _call("GET", "/get_assets").body["data"]["assets"][0]
    codes = {o["code"].lower() for o in asset["role_options"]}
    assert "owner" not in codes and "reader" in codes


# ── Allowlists ───────────────────────────────────────────────────────────────

def test_a_role_outside_the_allowlist_is_refused():
    _env(FN_AZURE_ROLES="reader")
    assert _grant(role="contributor").status == 403


def test_a_scope_outside_the_allowlist_is_refused():
    _env()
    other = f"/subscriptions/{SUB}/resourceGroups/staging"
    assert _grant(scope=other).status == 403


def test_scope_prefix_matching_is_anchored_on_a_separator():
    """An allowlisted .../prod must not also permit .../prod-secrets."""
    _env()
    assert _grant(scope=SCOPE + "-secrets").status == 403
    assert _grant(scope=SCOPE + "/providers/Microsoft.Storage/x").status == 200


def test_management_group_and_tenant_scopes_are_refused():
    _env(FN_AZURE_SCOPES="/")
    for scope in ("/", "/providers/Microsoft.Management/managementGroups/root"):
        assert _grant(scope=scope).status == 403, scope


def test_a_principal_outside_the_allowlist_is_refused():
    _env()
    other = "12121212-3434-5656-7878-909090909090"
    assert _grant(actor=other).status == 403


def test_an_empty_allowlist_grants_nothing():
    """A missing allowlist must fail closed, not mean 'anything'."""
    _env(FN_AZURE_ROLES="")
    assert _grant().status == 403
    _env(FN_AZURE_SCOPES="")
    assert _grant().status == 403


# ── Assignment naming ────────────────────────────────────────────────────────

def test_the_assignment_name_is_deterministic():
    """Same inputs, same GUID — so a grant is an idempotent upsert and a revoke can
    DELETE by name without listing anything."""
    first = rules.assignment_name(SCOPE, PRINCIPAL, rules.BUILTIN_ROLES["reader"])
    second = rules.assignment_name(SCOPE, PRINCIPAL, rules.BUILTIN_ROLES["reader"])
    assert first == second
    assert first != rules.assignment_name(SCOPE, PRINCIPAL,
                                          rules.BUILTIN_ROLES["contributor"])
    assert first != rules.assignment_name(SCOPE + "/x", PRINCIPAL,
                                          rules.BUILTIN_ROLES["reader"])


def test_grant_then_revoke_addresses_the_same_assignment():
    _env()
    _grant()
    put = [p for m, p, _b in CALLS if m == "PUT"][0]
    CALLS.clear()
    _call("POST", "/revoke_access", {
        "asset": {"identifier": f"azure:scope:{SCOPE}"},
        "actor_identifier": PRINCIPAL, "role_code": "reader"})
    delete = [p for m, p, _b in CALLS if m == "DELETE"][0]
    assert put == delete


def test_the_grant_states_the_principal_type():
    """Without it ARM looks the principal up in Graph, and a freshly created service
    principal can 400 purely from replication lag."""
    _env()
    _grant()
    body = [b for m, _p, b in CALLS if m == "PUT"][0]
    assert body["properties"]["principalType"] == "ServicePrincipal"
    assert body["properties"]["principalId"] == PRINCIPAL


# ── Idempotence ──────────────────────────────────────────────────────────────

def test_granting_twice_is_reported_as_already_present():
    _env()
    assert _grant().body["data"]["already_present"] is False
    # A second PUT on the same deterministic name is the same assignment.
    adapter._arm = lambda c, m, p, body=None, ok_missing=False: (
        CALLS.append((m, p, body)) or {"conflict": True})
    try:
        assert _grant().body["data"]["already_present"] is True
    finally:
        adapter._arm = _fake_arm


def test_revoking_an_absent_assignment_is_success():
    _env()
    resp = _call("POST", "/revoke_access", {
        "asset": {"identifier": f"azure:scope:{SCOPE}"},
        "actor_identifier": PRINCIPAL, "role_code": "reader"})
    assert resp.status == 200 and resp.body["data"]["removed"] is True


# ── Contract ─────────────────────────────────────────────────────────────────

def test_every_contract_route_is_served():
    _env()
    for method, path in [("GET", "/get_assets"), ("GET", "/get_actors"),
                         ("GET", "/get_all_permissions"), ("POST", "/give_access"),
                         ("POST", "/revoke_access"), ("POST", "/check_config")]:
        payload = {"asset": {"identifier": f"azure:scope:{SCOPE}"},
                   "actor_identifier": PRINCIPAL, "role_code": "reader"}
        assert _call(method, path, payload).status != 404, f"{method} {path}"


def test_actors_carry_their_label_for_a_readable_request():
    _env()
    actors = _call("GET", "/get_actors").body["data"]["actors"]
    assert actors[0]["identifier"] == PRINCIPAL
    assert actors[0]["name"] == "deploy-sp"


def test_an_actor_can_be_named_by_its_label():
    _env()
    assert _grant(actor="deploy-sp").status == 200


def test_permissions_report_only_assignments_this_adapter_made():
    """Listing everything at the scope would report assignments a human made and
    invite Entitle to reconcile them away."""
    _env()
    _grant(role="reader")
    data = _call("GET", "/get_all_permissions").body["data"]
    assert [p["role_code"] for p in data["actors_permissions"]] == ["reader"]


def test_check_config_reports_each_missing_piece():
    _env(FN_AZURE_ROLES="", FN_AZURE_PRINCIPALS="")
    data = _call("POST", "/check_config", {"config": {}}).body["data"]
    assert data["valid"] is False
    joined = " ".join(data["problems"])
    assert "roles" in joined and "principals" in joined


def test_dry_run_touches_nothing():
    _env(FN_AZURE_DRY_RUN="1")
    assert _grant().body["data"]["dry_run"] is True
    _call("POST", "/revoke_access", {
        "asset": {"identifier": f"azure:scope:{SCOPE}"},
        "actor_identifier": PRINCIPAL, "role_code": "reader"})
    assert not [m for m, _p, _b in CALLS if m in ("PUT", "DELETE")], CALLS


# ── Rules hygiene ────────────────────────────────────────────────────────────

def test_a_principal_must_be_an_object_id_guid():
    """Azure accepts an application id here and silently creates an assignment that
    grants nothing, because the assignment binds to the object id."""
    for bad in ("not-a-guid", "", "deploy-sp", None):
        try:
            rules.check_principal(bad)
        except rules.AzureRoleRuleError as exc:
            assert "OBJECT id" in str(exc) or "GUID" in str(exc)
        else:
            raise AssertionError(f"accepted principal {bad!r}")


def test_role_names_and_guids_both_resolve():
    assert rules.resolve_role("Reader") == rules.BUILTIN_ROLES["reader"]
    assert rules.resolve_role(rules.BUILTIN_ROLES["reader"]) == rules.BUILTIN_ROLES["reader"]
    try:
        rules.resolve_role("Wizard")
    except rules.AzureRoleRuleError:
        pass
    else:
        raise AssertionError("accepted an unknown role name")


def test_the_rules_module_is_safe_to_ship_into_a_function():
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
    assert imported <= {"re", "uuid"}, f"non-stdlib imports: {imported}"


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
