"""``create_actor``, against the schema Entitle really validates it with.

Every adapter here answered this route with a FLAT object — identifier and name
beside username, password, host, port — which is what the published example in the
OpenAPI definition shows. Entitle validates ``data`` against ``Provisioned Actor
Data``, which has exactly two properties and admits no others, and rejected the
whole response:

    Invalid response. Structure of "Provisioned Actor Data" is invalid.
    Additional properties are not allowed ('database', 'host', 'identifier',
    'name', 'password', 'port', 'role_code', 'statements_executed', 'type',
    'username' were unexpected)

Live 2026-09-04, on a real Cloud SQL MySQL grant for karen.walker@weaverlab.xyz.

This is the worst of the eight routes to fail. In Ephemeral Accounts mode
``create_actor`` IS the grant, so by the time the response is validated the account
exists and holds its role: Entitle records the request as failed, the requester is
told nothing, and ``delete_actor`` is only ever driven from what Entitle believes it
provisioned — so the account outlives the grant it was minted for. ``get_assets``,
``get_actors`` and ``get_all_permissions`` all stay green throughout.

``_assert_actor_data`` below is a transcription of the schema from that error, and
is applied to every adapter that mints an account rather than to the one that hit
it — the flat shape was uniform, so the fix has to be too. Same reasoning as
test_entitle_permissions_shape.py, one route over.

Runs offline: no database, no Portainer, nothing executed.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from web_dashboard import functions  # noqa: F401  (puts fnruntime on sys.path)
from fnruntime import entitle
from fnruntime.contract import Context, Request
from fnworkloads import db_grant, entitle_webhook_echo, portainer_access


# ── The schema, as Entitle applies it ────────────────────────────────────────

_ACTOR_REQUIRED = ("identifier", "name", "type", "email")
_ACTOR_ALLOWED = _ACTOR_REQUIRED + ("last_user",)


def _assert_actor_data(data, where):
    """Fail exactly where Entitle's validator would, and say the same thing."""
    assert isinstance(data, dict), f"{where}: data is not an object"
    assert set(data) == {"actor", "login_info"}, (
        f"{where}: additionalProperties is False and both are required; "
        f"got {sorted(data)}")
    assert isinstance(data["login_info"], dict), f"{where}: login_info is not an object"

    actor = data["actor"]
    assert isinstance(actor, dict), f"{where}: actor is not an object"
    extra = set(actor) - set(_ACTOR_ALLOWED)
    assert not extra, f"{where}: actor is closed too; {sorted(extra)} not allowed"
    for key in _ACTOR_REQUIRED:
        assert key in actor, f"{where}: actor.{key} is required"
        assert isinstance(actor[key], str), f"{where}: actor.{key} must be a string"
    for key in ("identifier", "name", "type"):
        assert actor[key], f"{where}: actor.{key} cannot be empty"


def test_the_validator_itself_rejects_the_flat_shape():
    """The bug, as a test: the payload from the live failure must not pass, and
    neither must any of the near-misses that look like a fix.
    """
    for bad in (
            # Verbatim from the live 2026-09-04 error.
            {"identifier": "jit_karen_walker_we_6a9ae8290000",
             "name": "jit_karen_walker_we_6a9ae8290000", "type": "database_account",
             "role_code": "readwrite", "username": "jit_karen_walker_we_6a9ae8290000",
             "password": "x", "host": "10.138.32.11", "port": 3306,
             "database": "app_db", "statements_executed": 2},
            # Nested, but the credentials left at the top level.
            {"actor": {"identifier": "a", "name": "a", "type": "t", "email": "e"},
             "login_info": {}, "password": "x"},
            # Nested, but the report written beside the containers.
            {"actor": {"identifier": "a", "name": "a", "type": "t", "email": "e"},
             "login_info": {}, "statements_executed": 2},
            # login_info missing entirely — it is required, not optional.
            {"actor": {"identifier": "a", "name": "a", "type": "t", "email": "e"}},
            # actor carrying a field of its own that the schema does not define.
            {"actor": {"identifier": "a", "name": "a", "type": "t", "email": "e",
                       "role_code": "read"}, "login_info": {}},
            # email is required even when the adapter has none.
            {"actor": {"identifier": "a", "name": "a", "type": "t"}, "login_info": {}},
    ):
        try:
            _assert_actor_data(bad, "self-check")
        except AssertionError:
            continue
        raise AssertionError(f"validator accepted {bad!r}")


# ── The helper ───────────────────────────────────────────────────────────────

def test_actor_data_is_the_right_shape_from_the_minimum():
    data = entitle.actor_data("jit_a_1", "database_account")
    _assert_actor_data(data, "fnruntime.entitle")
    assert data["actor"]["name"] == "jit_a_1", "name defaults to the identifier"
    assert data["actor"]["email"] == "", "an unknown owner is empty, not invented"
    assert data["login_info"] == {}


def test_actor_data_never_attributes_the_account_to_itself():
    """``email`` is the only link back to the person who asked. Defaulting it to the
    minted username would satisfy the schema and lose that link."""
    data = entitle.actor_data("jit_a_1", "database_account", email="alice@example.com")
    assert data["actor"]["email"] == "alice@example.com"
    assert entitle.actor_data("jit_a_1", "t")["actor"]["email"] != "jit_a_1"


def test_actor_data_puts_everything_else_in_login_info():
    data = entitle.actor_data("jit_a_1", "database_account", email="a@b.c",
                              login_info={"password": "x", "port": 3306})
    _assert_actor_data(data, "fnruntime.entitle")
    assert data["login_info"] == {"password": "x", "port": 3306}


def test_actor_data_does_not_alias_the_login_info_it_is_given():
    """``_apply`` writes the execution report into ``login_info`` after the fact; if
    that were the caller's dict, one route would be mutating another's state."""
    given = {"port": 3306}
    data = entitle.actor_data("jit_a_1", "t", login_info=given)
    data["login_info"]["statements_executed"] = 2
    assert given == {"port": 3306}, given


# ── db_grant ─────────────────────────────────────────────────────────────────

def _db_env(**overrides):
    for key in ("FN_DB_ENGINE", "FN_DB_HOST", "FN_DB_NAME", "FN_DB_NAMES",
                "FN_DB_PORT", "FN_DB_DRY_RUN", "FN_DB_ADMIN_USER"):
        os.environ.pop(key, None)
    base = {"FN_DB_ENGINE": "mysql", "FN_DB_HOST": "10.138.32.11",
            "FN_DB_PORT": "3306", "FN_DB_NAME": "app_db",
            "FN_DB_ADMIN_USER": "dbadmin", "FN_DB_DRY_RUN": "1"}
    base.update(overrides)
    os.environ.update(base)


def _db_create(payload, **env):
    """``create_actor``'s body. Live runs stub ``_execute``, so no connection is
    opened and the response is otherwise exactly what the function returns."""
    _db_env(**env)
    real = db_grant._execute
    db_grant._execute = lambda plan, target: sum(len(s) for _db, s in plan)
    try:
        return db_grant.handle(
            Request(method="POST", path="/create_actor", headers={}, query={},
                    body=json.dumps(payload).encode(), source="aws_function_url"),
            Context.from_env(workload="db_grant")).body
    finally:
        db_grant._execute = real


def test_db_grant_ephemeral_create_actor_is_the_shape_that_failed_live():
    """The exact call that produced the error at the top of this file: Ephemeral
    mode, an asset, a role, a real address, and nothing in dry run."""
    _db_env(FN_DB_DRY_RUN="0")
    asset = list(db_grant._targets())[0]
    body = _db_create({"provisioning_data": {"email": "karen.walker@weaverlab.xyz"},
                       "asset": {"identifier": asset},
                       "role_code": "readwrite"},
                      FN_DB_DRY_RUN="0")
    _assert_actor_data(body["data"], "db_grant ephemeral")
    actor, login = body["data"]["actor"], body["data"]["login_info"]
    assert actor["identifier"].startswith("jit_karen_walker"), actor
    assert actor["email"] == "karen.walker@weaverlab.xyz", actor
    assert actor["type"] == "database_account", actor
    # The credentials are still returned — they are the point of the route. They
    # just live one level down now, which is what Entitle hands the requester.
    assert login["username"] == actor["identifier"], login
    assert login["password"] and login["host"] == "10.138.32.11", login
    assert login["port"] == 3306 and login["database"] == "app_db", login
    assert login["role_code"] == "readwrite", login
    # And the execution report goes with them, not beside the containers.
    assert login["statements_executed"] > 0, login


def test_db_grant_standing_create_actor_is_the_same_shape():
    """The Standing path builds its response at a different call site. One of the
    two being right is not enough."""
    body = _db_create({"actor": {"email": "alice@example.com"}}, FN_DB_DRY_RUN="0")
    _assert_actor_data(body["data"], "db_grant standing")
    assert body["data"]["actor"]["email"] == "alice@example.com"
    assert body["data"]["login_info"]["database"] == "app_db"


def test_db_grant_multi_database_standing_create_actor_is_the_same_shape():
    """The multi-database branch reports ``databases`` rather than ``database``, and
    a list at the top level is an additional property just the same."""
    body = _db_create({"actor": {"email": "alice@example.com"}},
                      FN_DB_NAMES="app_db,reporting", FN_DB_DRY_RUN="0")
    _assert_actor_data(body["data"], "db_grant standing multi")
    assert body["data"]["login_info"]["databases"] == ["app_db", "reporting"]


def test_db_grant_dry_run_create_actor_is_the_same_shape():
    """Dry run is the DEFAULT, so this is the first create_actor Entitle ever
    validates — and the plan it reports is an additional property too."""
    body = _db_create({"actor": {"email": "alice@example.com"}})
    _assert_actor_data(body["data"], "db_grant dry run")
    assert body["data"]["login_info"]["dry_run"] is True
    assert body["data"]["login_info"]["plan"]
    assert "password" not in json.dumps(body).lower(), body


def test_db_grant_returns_an_identifier_its_own_guard_accepts():
    """Nesting moved the identifier. If ``_is_minted`` no longer recognised what
    ``actor.identifier`` carries, every follow-up route would 403."""
    body = _db_create({"actor": {"email": "alice@example.com"}})
    assert db_grant._is_minted(body["data"]["actor"]["identifier"]), body


def test_db_grants_other_write_routes_keep_the_flat_report():
    """Only create_actor has the closed schema. Nesting the other three would break
    the shape their own tests and the audit log read, for no reason."""
    for path, payload in (("/give_access", {"actor_identifier": "jit_a_1",
                                            "role_code": "read"}),
                          ("/revoke_access", {"actor_identifier": "jit_a_1",
                                              "role_code": "read"}),
                          ("/delete_actor", {"actor_identifier": "jit_a_1"})):
        _db_env()
        body = db_grant.handle(
            Request(method="POST", path=path, headers={}, query={},
                    body=json.dumps(payload).encode(), source="aws_function_url"),
            Context.from_env(workload="db_grant")).body
        assert body["data"]["plan"], (path, body)
        assert "login_info" not in body["data"], (path, body)


# ── portainer_access ─────────────────────────────────────────────────────────

def _portainer_create(dry_run):
    os.environ.update({"FN_PORTAINER_URL": "https://portainer.internal",
                       "FN_PORTAINER_API_KEY": "ptr_key",
                       "FN_PORTAINER_DRY_RUN": "1" if dry_run else "0"})
    real = portainer_access._api
    portainer_access._api = lambda config, method, path, body=None: {"Id": 4}
    try:
        return portainer_access.handle(
            Request(method="POST", path="/create_actor", headers={}, query={},
                    body=json.dumps({"actor": {"email": "alice@example.com"}}).encode(),
                    source="aws_function_url"),
            Context.from_env(workload="portainer_access")).body
    finally:
        portainer_access._api = real


def test_portainer_create_actor_is_the_same_shape():
    """It had the identical flat response and would have failed identically the
    first time anyone requested Portainer access through Entitle."""
    body = _portainer_create(dry_run=False)
    _assert_actor_data(body["data"], "portainer_access")
    assert body["data"]["actor"]["email"] == "alice@example.com"
    assert body["data"]["actor"]["type"] == "portainer_user"
    login = body["data"]["login_info"]
    assert login["username"] == body["data"]["actor"]["identifier"], login
    assert login["password"] and login["portainer_user_id"] == 4, login
    assert login["url"] == "https://portainer.internal", login


def test_portainer_dry_run_create_actor_is_the_same_shape():
    body = _portainer_create(dry_run=True)
    _assert_actor_data(body["data"], "portainer_access dry run")
    assert body["data"]["login_info"]["dry_run"] is True
    assert "password" not in json.dumps(body).lower(), body


# ── entitle_webhook_echo ─────────────────────────────────────────────────────

def test_the_echo_adapter_teaches_the_right_shape():
    """This adapter exists to prove the path end to end with well-formed empty
    responses. Acknowledging create_actor with an ok flag failed the one route it
    is wired up to validate."""
    body = entitle_webhook_echo.handle(
        Request(method="POST", path="/create_actor", headers={}, query={},
                body=json.dumps({"actor": {"email": "alice@example.com"}}).encode(),
                source="aws_function_url"),
        Context.from_env(workload="entitle_webhook_echo")).body
    _assert_actor_data(body["data"], "entitle_webhook_echo")
    assert body["data"]["actor"]["email"] == "alice@example.com"
    assert body["data"]["login_info"]["granted"] is False, "it grants nothing"
    assert body["observed"]["path"] == "/create_actor", "still echoes what arrived"


# ── The dashboard-hosted adapter ─────────────────────────────────────────────

def test_the_dashboard_hosted_create_actor_is_nested_too():
    """``api/pov_accessor_rest.py`` serves the same contract from the dashboard
    itself, in Ephemeral mode, and was flat in the same way.

    Its route needs FastAPI and a session, so what is checked here is the literal it
    returns: read out of the AST rather than by regex, because the failure is about
    which keys sit at which level and a substring match cannot see that. The live
    round trip is asserted in test_pov_accessor.py.
    """
    import ast
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                        "web_dashboard", "api", "pov_accessor_rest.py")
    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())

    route = next((node for node in ast.walk(tree)
                  if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                  and node.name == "create_actor"), None)
    assert route is not None, "pov_accessor_rest has no create_actor to check"

    returned = [node.value for node in ast.walk(route)
                if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict)]
    assert returned, "create_actor no longer returns a dict literal; check it by hand"
    for literal in returned:
        keys = {k.value for k in literal.keys if isinstance(k, ast.Constant)}
        assert keys == {"data"}, f"unexpected response envelope {sorted(keys)}"
        data = literal.values[0]
        assert isinstance(data, ast.Dict), "data is not a literal; check it by hand"
        data_keys = {k.value for k in data.keys if isinstance(k, ast.Constant)}
        assert data_keys == {"actor", "login_info"}, (
            "Provisioned Actor Data is additionalProperties: False and both are "
            f"required; this returns {sorted(data_keys)}")
        actor = data.values[[k.value for k in data.keys].index("actor")]
        actor_keys = {k.value for k in actor.keys if isinstance(k, ast.Constant)}
        assert actor_keys <= set(_ACTOR_ALLOWED), (
            f"actor is closed too; {sorted(actor_keys - set(_ACTOR_ALLOWED))} not allowed")
        for key in _ACTOR_REQUIRED:
            assert key in actor_keys, f"actor.{key} is required"


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
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
