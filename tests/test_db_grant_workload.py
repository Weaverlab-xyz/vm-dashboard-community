"""db_grant as an Entitle Remote Adapter.

The contract is docs.beyondtrust.com/entitle/docs/open-api-definition. What is
pinned here is the shape Entitle actually depends on — routes keyed by PATH, the
response envelopes, and the four-operation ephemeral lifecycle — plus the safety
properties: dry run by default, a caller that cannot steer the target, and
credentials returned only for an account that was really created.

The SQL itself is proved in tests/test_db_grant_sql.py. Runs entirely offline.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from web_dashboard import functions  # noqa: F401  (puts fnruntime on sys.path)
from fnruntime.contract import Context, Request, Response
from fnworkloads import db_grant
from web_dashboard.services import cloud_db_sql_service

_ENV_KEYS = ("FN_DB_ENGINE", "FN_DB_HOST", "FN_DB_PORT", "FN_DB_NAME", "FN_DB_NAMES",
             "FN_DB_FLAVOR",
             "FN_DB_ADMIN_USER", "FN_DB_ADMIN_PASSWORD", "FN_DB_ADMIN_SECRET_ID",
             "FN_DB_DRY_RUN", "FN_DB_CAFILE")


def _env(**overrides):
    for key in _ENV_KEYS:
        os.environ.pop(key, None)
    base = {"FN_DB_ENGINE": "mysql", "FN_DB_HOST": "db.internal",
            "FN_DB_NAME": "appdb", "FN_DB_ADMIN_USER": "dbadmin"}
    base.update(overrides)
    for key, val in base.items():
        os.environ[key] = val


def _call(method, path, payload=None):
    return db_grant.handle(
        Request(method=method, path=path, headers={}, query={},
                body=json.dumps(payload or {}).encode(), source="aws_function_url"),
        Context.from_env(workload="db_grant"))


def _asset_id(database=None):
    """The asset identifier for one served database (the first, by default)."""
    identifiers = list(db_grant._targets())
    if database is None:
        return identifiers[0]
    return next(i for i in identifiers if i.endswith(f":{database}"))


def _statements(body):
    return " ".join(s for p in body["data"]["plan"] for s in p["statements"])


# ── Routing: the verb is the path ────────────────────────────────────────────

def test_every_contract_route_is_served():
    """Entitle's integration config names each path separately; a missing one is a
    404 at setup time with no obvious cause."""
    _env()
    for method, path in [
        ("GET", "/get_assets"), ("GET", "/get_actors"), ("GET", "/get_all_permissions"),
        ("POST", "/create_actor"), ("POST", "/delete_actor"),
        ("POST", "/give_access"), ("POST", "/revoke_access"), ("POST", "/check_config"),
    ]:
        payload = {"actor": {"email": "a@example.com"}, "actor_identifier": "jit_a_1",
                   "role_code": "read", "config": {}}
        resp = _call(method, path, payload)
        assert resp.status != 404, f"{method} {path} is not routed"


def test_asset_permissions_route_takes_an_identifier_in_the_path():
    _env()
    resp = _call("GET", f"/get_asset_permissions/{_asset_id()}")
    assert resp.status == 200
    assert "actors_permissions" in resp.body["data"]


def test_an_unknown_route_404s_and_says_what_it_serves():
    """Entitle's *_path fields are configurable, so a 404 almost always means they
    disagree with these routes — the response should say so."""
    _env()
    resp = _call("POST", "/grant")
    assert resp.status == 404
    assert any("/give_access" in r for r in resp.body["routes"])


def test_the_method_is_part_of_the_route():
    _env()
    assert _call("POST", "/get_assets").status == 404
    assert _call("GET", "/give_access").status == 404


def test_a_trailing_slash_still_routes():
    _env()
    assert _call("GET", "/get_assets/").status == 200


# ── Response envelopes ───────────────────────────────────────────────────────

def test_read_routes_use_the_next_plus_data_envelope():
    _env()
    for path, key in (("/get_assets", "assets"), ("/get_actors", "actors")):
        body = _call("GET", path).body
        assert "next" in body and "data" in body, (path, body)
        assert key in body["data"], (path, body)


def test_write_routes_use_the_data_envelope():
    _env()
    for path in ("/create_actor", "/give_access", "/revoke_access", "/delete_actor"):
        payload = {"actor": {"email": "a@example.com"}, "actor_identifier": "jit_a_1",
                   "role_code": "read"}
        body = _call("POST", path, payload).body
        assert "data" in body, (path, body)


def test_check_config_reports_valid_and_the_resolved_target():
    _env()
    body = _call("POST", "/check_config", {"config": {}}).body
    assert body["data"]["valid"] is True
    assert body["data"]["assets"] == [_asset_id()]
    assert body["data"]["engine"] == "mysql"


def test_get_assets_advertises_the_role_options_entitle_offers():
    """role_options is how an operator picks a role_code; an empty list means the
    request form has nothing to choose."""
    _env()
    asset = _call("GET", "/get_assets").body["data"]["assets"][0]
    codes = {opt["code"] for opt in asset["role_options"]}
    assert codes == {"read", "readwrite"}
    for option in asset["role_options"]:
        assert option["available"] is True and option["permissions"]
    assert asset["identifier"] and asset["type"] == "mysql"


# ── The four-operation ephemeral lifecycle ───────────────────────────────────

def test_create_actor_makes_an_account_with_no_privileges():
    """If the follow-up give_access never arrives, what is left behind must be able
    to reach nothing."""
    _env()
    body = _call("POST", "/create_actor", {"actor": {"email": "alice@example.com"}}).body
    sql = _statements(body)
    assert "CREATE USER" in sql
    for verb in ("GRANT", "SELECT", "INSERT"):
        assert verb not in sql, f"create_actor leaked {verb}: {sql}"


def test_create_actor_returns_the_identifier_entitle_then_uses():
    _env()
    body = _call("POST", "/create_actor", {"actor": {"email": "alice@example.com"}}).body
    identifier = body["data"]["identifier"]
    assert cloud_db_sql_service._IDENT_RE.match(identifier), identifier
    # That identifier is what give_access is called with next.
    give = _call("POST", "/give_access", {
        "asset": {"identifier": _asset_id()}, "actor_identifier": identifier,
        "role_code": "read"}).body
    assert identifier in _statements(give)


def test_give_access_grants_without_creating():
    _env()
    sql = _statements(_call("POST", "/give_access", {
        "asset": {"identifier": _asset_id()}, "actor_identifier": "jit_a_1",
        "role_code": "read"}).body)
    assert "GRANT SELECT" in sql
    assert "CREATE USER" not in sql, "give_access must not mint an account"


def test_revoke_access_removes_the_role_but_leaves_the_account():
    """Keeping the two separate is what lets Entitle revoke one role on one asset
    without disturbing other access the same actor holds."""
    _env()
    sql = _statements(_call("POST", "/revoke_access", {
        "asset": {"identifier": _asset_id()}, "actor_identifier": "jit_a_1",
        "role_code": "read"}).body)
    assert "REVOKE" in sql
    assert "DROP USER" not in sql, "revoke_access must not drop the account"


def test_delete_actor_drops_the_account_idempotently():
    _env()
    sql = _statements(_call("POST", "/delete_actor",
                            {"actor_identifier": "jit_a_1"}).body)
    assert "DROP USER IF EXISTS" in sql


def test_the_azure_sql_split_survives_the_adapter():
    _env(FN_DB_ENGINE="sqlserver", FN_DB_FLAVOR="azure_sql")
    create = _call("POST", "/create_actor", {"actor": {"email": "a@example.com"}}).body
    assert [p["database"] for p in create["data"]["plan"]] == ["master", "appdb"]
    give = _call("POST", "/give_access", {
        "asset": {"identifier": _asset_id()}, "actor_identifier": "jit_a_1",
        "role_code": "read"}).body
    assert [p["database"] for p in give["data"]["plan"]] == ["appdb"]
    assert "USE " not in _statements(give)


# ── Safety ───────────────────────────────────────────────────────────────────

def test_dry_run_is_the_default():
    _env()
    body = _call("POST", "/create_actor", {"actor": {"email": "a@example.com"}}).body
    assert body["data"]["dry_run"] is True and body["data"]["plan"]


def test_dry_run_never_returns_a_credential():
    _env()
    body = _call("POST", "/create_actor", {"actor": {"email": "a@example.com"}}).body
    assert "password" not in body["data"]
    assert "password" not in json.dumps(body).lower()


def test_the_asset_in_the_request_cannot_redirect_the_grant():
    """Entitle echoes the whole asset object. Honouring a host or database from it
    would make every caller a lateral-movement primitive."""
    _env()
    body = _call("POST", "/give_access", {
        "asset": {"identifier": _asset_id(), "name": "otherdb",
                  "host": "evil.example.com", "database": "otherdb"},
        "actor_identifier": "jit_a_1", "role_code": "read"}).body
    sql = _statements(body)
    assert "otherdb" not in sql and "evil.example.com" not in sql
    assert "`appdb`" in sql


def test_a_request_for_a_different_asset_is_refused():
    """A mismatch means the integration is pointed at the wrong function — worth
    failing loudly rather than quietly granting on the only database we have."""
    _env()
    resp = _call("POST", "/give_access", {
        "asset": {"identifier": "mysql:other.host:otherdb"},
        "actor_identifier": "jit_a_1", "role_code": "read"})
    assert resp.status == 404, resp.body


def test_an_unknown_role_code_is_refused_rather_than_downgraded():
    _env()
    resp = _call("POST", "/give_access", {
        "asset": {"identifier": _asset_id()}, "actor_identifier": "jit_a_1",
        "role_code": "db_owner"})
    assert resp.status == 400, resp.body


def test_an_injection_in_the_actor_cannot_reach_the_sql():
    _env()
    body = _call("POST", "/create_actor",
                 {"actor": {"email": "alice'; DROP TABLE users;--@example.com"}}).body
    assert "DROP TABLE" not in _statements(body)


def test_a_hostile_actor_identifier_is_refused_not_escaped():
    _env()
    for evil in ("a'; DROP TABLE x;--", 'a" OR 1=1', "1abc", "a b", ""):
        resp = _call("POST", "/delete_actor", {"actor_identifier": evil})
        # 400 for a missing name, 403 for one this adapter did not mint. Either way
        # it is refused before any SQL is built, which is the property that matters.
        assert resp.status in (400, 403), (evil, resp.body)
        assert "plan" not in (resp.body.get("data") or {}), (evil, resp.body)


def test_missing_required_fields_are_400s():
    _env()
    assert _call("POST", "/create_actor", {"actor": {}}).status == 400
    assert _call("POST", "/give_access",
                 {"asset": {"identifier": _asset_id()}}).status == 400
    assert _call("POST", "/delete_actor", {}).status == 400


def test_misconfiguration_surfaces_rather_than_silently_defaulting():
    for env in ({"FN_DB_ENGINE": "postgres"}, {"FN_DB_HOST": ""},
                {"FN_DB_PORT": "not-a-number"}, {"FN_DB_NAME": ""}):
        _env(**env)
        try:
            _call("GET", "/get_assets")
        except RuntimeError:
            pass
        else:
            raise AssertionError(f"accepted a bad config: {env}")


def test_no_route_carries_a_ttl():
    """Entitle owns expiry and calls revoke/delete when the grant ends, so the
    adapter is stateless with respect to time. A duration field appearing here would
    mean someone had built scheduling into a function that cannot schedule."""
    _env()
    plain = _statements(_call("POST", "/give_access", {
        "asset": {"identifier": _asset_id()}, "actor_identifier": "jit_a_1",
        "role_code": "read"}).body)
    with_ttl = _statements(_call("POST", "/give_access", {
        "asset": {"identifier": _asset_id()}, "actor_identifier": "jit_a_1",
        "role_code": "read", "duration": 3600, "ttl": 60}).body)
    assert plain == with_ttl


def test_generated_passwords_are_accepted_by_the_sql_builders():
    for _ in range(200):
        pwd = db_grant._generate_password()
        cloud_db_sql_service.create_actor_plan(
            "sqlserver", username="jit_a_1", password=pwd,
            database="appdb", flavor="azure_sql")


# ── The vendored SQL module ──────────────────────────────────────────────────

def test_the_sql_module_is_safe_to_ship_into_a_function():
    """It is copied into the zip verbatim, so it must import nothing beyond the
    stdlib and open no connection."""
    import ast
    tree = ast.parse(open(cloud_db_sql_service.__file__, encoding="utf-8").read())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "a relative import cannot be vendored"
            if node.module:
                imported.add(node.module.split(".")[0])
    assert imported <= {"re", "secrets", "string"}, f"non-stdlib imports: {imported}"


def test_the_packager_ships_the_real_sql_module_not_a_copy():
    from web_dashboard.services import cloud_function_package as pkg
    assert dict(pkg._WORKLOAD_MODULES["db_grant"])[
        "services/cloud_db_sql_service.py"] == "sqlplan.py"


def test_the_adapter_delegates_every_plan_to_the_service():
    """Four Entitle operations, four plan builders, no fifth implementation."""
    for name in ("create_actor_plan", "delete_actor_plan",
                 "give_access_plan", "revoke_access_plan"):
        assert hasattr(cloud_db_sql_service, name), name
        assert name in open(db_grant.__file__, encoding="utf-8").read(), name


# ── One function, several databases on one server ─────────────────────────────

def _multi(**overrides):
    _env(FN_DB_NAMES="appdb,reporting,billing", **overrides)


def test_one_function_serves_every_configured_database():
    _multi()
    assets = _call("GET", "/get_assets").body["data"]["assets"]
    assert [a["identifier"] for a in assets] == [
        "mysql:db.internal:appdb", "mysql:db.internal:reporting",
        "mysql:db.internal:billing"]
    # Distinct identifiers are what make a request able to name one unambiguously.
    assert len({a["identifier"] for a in assets}) == 3


def test_a_grant_lands_on_the_database_the_asset_names():
    """The whole point: the identifier SELECTS a target from the allowlist."""
    _multi()
    sql = _statements(_call("POST", "/give_access", {
        "asset": {"identifier": _asset_id("billing")},
        "actor_identifier": "jit_a_1", "role_code": "read"}).body)
    assert "`billing`" in sql
    assert "`appdb`" not in sql and "`reporting`" not in sql


def test_a_missing_asset_identifier_is_refused_when_several_are_served():
    """Choosing one for the caller would be exactly the silent mis-grant the
    adapter exists to prevent."""
    _multi()
    resp = _call("POST", "/give_access", {"actor_identifier": "jit_a_1",
                                          "role_code": "read"})
    assert resp.status == 400, resp.body
    assert len(resp.body["assets"]) == 3


def test_a_missing_asset_identifier_still_resolves_for_a_single_database():
    """Back-compat: every existing single-database deployment calls this way."""
    _env()
    resp = _call("POST", "/give_access", {"actor_identifier": "jit_a_1",
                                          "role_code": "read"})
    assert resp.status == 200, resp.body
    assert "`appdb`" in _statements(resp.body)


def test_a_database_that_is_not_served_is_refused():
    """The request can only ever pick from FN_DB_NAMES — it can never add to it."""
    _multi()
    for identifier in ("mysql:db.internal:secrets", "mysql:other.host:appdb"):
        resp = _call("POST", "/give_access", {
            "asset": {"identifier": identifier},
            "actor_identifier": "jit_a_1", "role_code": "read"})
        assert resp.status == 404, (identifier, resp.body)


def test_create_actor_covers_every_served_database():
    """Entitle's create_actor carries no asset, so the account has to exist
    everywhere before anything knows which database the grant is for."""
    _multi(FN_DB_ENGINE="sqlserver", FN_DB_FLAVOR="azure_sql")
    plan = _call("POST", "/create_actor",
                 {"actor": {"email": "alice@example.com"}}).body["data"]["plan"]
    assert [entry["database"] for entry in plan] == [
        "master", "appdb", "reporting", "billing"]
    assert all("CREATE USER" in " ".join(entry["statements"])
               for entry in plan[1:])
    # Created with no role anywhere — give_access is still the only thing granting.
    assert "ALTER ROLE" not in _statements({"data": {"plan": plan}})


def test_delete_actor_undoes_create_everywhere():
    """A database user left behind when its login goes is an ORPHANED USER, which a
    later login of the same name silently re-adopts."""
    _multi(FN_DB_ENGINE="sqlserver", FN_DB_FLAVOR="azure_sql")
    plan = _call("POST", "/delete_actor",
                 {"actor_identifier": "jit_a_1"}).body["data"]["plan"]
    assert [entry["database"] for entry in plan] == [
        "appdb", "reporting", "billing", "master"]
    assert "DROP LOGIN" in " ".join(plan[-1]["statements"])


def test_mysql_needs_no_per_database_account_work():
    """CREATE USER is server-scoped on MySQL and only the GRANT is per-database, so
    the multi-database plan is the single-database one."""
    _multi()
    plan = _call("POST", "/create_actor",
                 {"actor": {"email": "alice@example.com"}}).body["data"]["plan"]
    assert len(plan) == 1 and "CREATE USER" in " ".join(plan[0]["statements"])


# ── Only ever accounts this adapter minted ────────────────────────────────────

def test_destructive_routes_refuse_an_account_it_did_not_mint():
    """Without this, delete_actor is a DROP USER for any name the caller likes, and
    give_access grants a role to any existing login — an application account, or
    the admin. Both are privilege-escalation primitives rather than JIT grants."""
    _multi()
    for path, payload in (
            ("/delete_actor", {"actor_identifier": "dbadmin"}),
            ("/give_access", {"asset": {"identifier": _asset_id()},
                              "actor_identifier": "dbadmin", "role_code": "readwrite"}),
            ("/revoke_access", {"asset": {"identifier": _asset_id()},
                                "actor_identifier": "app_user", "role_code": "read"})):
        resp = _call("POST", path, payload)
        assert resp.status == 403, (path, resp.body)
        assert "plan" not in (resp.body.get("data") or {}), (path, resp.body)


def test_the_names_this_adapter_mints_pass_its_own_guard():
    """A guard that rejected the adapter's own accounts would break every grant."""
    _multi()
    identifier = _call("POST", "/create_actor",
                       {"actor": {"email": "alice@example.com"}}).body["data"]["identifier"]
    assert db_grant._is_minted(identifier), identifier
    resp = _call("POST", "/give_access", {
        "asset": {"identifier": _asset_id()}, "actor_identifier": identifier,
        "role_code": "read"})
    assert resp.status == 200, resp.body


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
