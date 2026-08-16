"""The db_grant workload: request handling, dry run, and its safety properties.

Runs entirely offline. The SQL itself is proved in tests/test_db_grant_sql.py; what
is pinned here is everything around it — that dry run is the default, that a caller
cannot redirect the grant at another database, that a password is only ever returned
for an account that was actually created, and that the vendored SQL module is the
real one rather than a drifting copy.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from web_dashboard import functions  # noqa: F401  (puts fnruntime on sys.path)
from fnruntime.contract import Context, Request, Response
from fnworkloads import db_grant
from web_dashboard.services import cloud_db_sql_service

_ENV_KEYS = ("FN_DB_ENGINE", "FN_DB_HOST", "FN_DB_PORT", "FN_DB_NAME", "FN_DB_FLAVOR",
             "FN_DB_ADMIN_USER", "FN_DB_ADMIN_PASSWORD", "FN_DB_ADMIN_SECRET_ID",
             "FN_DB_DRY_RUN", "FN_DB_CAFILE")


def _env(**overrides):
    for key in _ENV_KEYS:
        os.environ.pop(key, None)
    base = {"FN_DB_ENGINE": "mysql", "FN_DB_HOST": "db.internal",
            "FN_DB_NAME": "appdb", "FN_DB_ADMIN_USER": "dbadmin"}
    base.update(overrides)
    for key, val in base.items():
        if val is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = val


def _call(payload, query=None):
    return db_grant.handle(
        Request(method="POST", path="/", headers={}, query=query or {},
                body=json.dumps(payload).encode(), source="aws_function_url"),
        Context.from_env(workload="db_grant"))


# ── Dry run is the default ───────────────────────────────────────────────────

def test_dry_run_is_the_default_and_opens_no_connection():
    """Executing SQL against a production database is not a safe default for a
    feature whose payload schema is still being pinned. If this regresses, the
    connect attempt fails on an unresolvable host rather than silently running."""
    _env()
    resp = _call({"action": "grant", "user_email": "alice@example.com", "role": "read"})
    assert resp.status == 200, resp.body
    assert resp.body["dry_run"] is True
    assert resp.body["plan"], "dry run must show what it would have run"


def test_dry_run_never_returns_a_password():
    """Nothing was created, so a credential in the response would be a secret with
    no account behind it."""
    _env()
    body = _call({"action": "grant", "user_email": "alice@example.com"}).body
    assert "password" not in body, body
    assert "password" not in json.dumps(body).lower().replace('"passwordless"', "")


def test_dry_run_can_be_turned_off_explicitly_only():
    _env(FN_DB_DRY_RUN="0")
    assert db_grant._dry_run() is False
    for value in ("1", "true", "yes", "on", "TRUE"):
        _env(FN_DB_DRY_RUN=value)
        assert db_grant._dry_run() is True, value
    _env()                       # unset entirely
    assert db_grant._dry_run() is True


# ── The plan the workload would run ──────────────────────────────────────────

def test_grant_plan_matches_the_service_builders_exactly():
    """The workload must not reimplement the SQL — it delegates to the same pure
    builders the SQL tests exercise."""
    _env(FN_DB_ENGINE="sqlserver", FN_DB_FLAVOR="azure_sql")
    body = _call({"action": "grant", "user_email": "alice@example.com",
                  "role": "read", "request_id": "req-abc"}).body
    expected = cloud_db_sql_service.grant_plan(
        "sqlserver", username=body["username"], password="Pw-1",
        database="appdb", role="read", flavor="azure_sql")
    assert [p["database"] for p in body["plan"]] == [db for db, _ in expected]
    assert [p["database"] for p in body["plan"]] == ["master", "appdb"]


def test_azure_sql_plan_is_two_connections_and_rds_is_one():
    for flavor, databases in (("azure_sql", ["master", "appdb"]), ("rds", ["master"])):
        _env(FN_DB_ENGINE="sqlserver", FN_DB_FLAVOR=flavor)
        body = _call({"action": "grant", "user_email": "a@example.com"}).body
        assert [p["database"] for p in body["plan"]] == databases, flavor


def test_revoke_plan_is_produced_for_a_bare_username():
    _env()
    body = _call({"action": "revoke", "username": "jit_alice_abc"}).body
    assert body["ok"] is True and body["username"] == "jit_alice_abc"
    assert any("DROP USER" in s for p in body["plan"] for s in p["statements"])


def test_revoke_can_reconstruct_the_username_from_identity_plus_request_id():
    """Entitle's revoke may not echo the account name, only the original request."""
    _env()
    granted = _call({"action": "grant", "user_email": "alice@example.com",
                     "request_id": "req-xyz"}).body["username"]
    revoked = _call({"action": "revoke", "user_email": "alice@example.com",
                     "request_id": "req-xyz"}).body["username"]
    assert granted == revoked, (granted, revoked)


# ── The caller cannot steer the grant ────────────────────────────────────────

def test_the_request_cannot_redirect_the_target_database_or_host():
    """Host, port, database, engine and flavor come from the function's own config.
    A payload field that could move the grant to another database would make every
    caller of this endpoint a lateral-movement primitive."""
    _env()
    body = _call({
        "action": "grant", "user_email": "alice@example.com",
        "database": "otherdb", "host": "evil.example.com", "port": 9999,
        "engine": "postgres", "flavor": "rds", "admin_user": "root",
    }).body
    grants = " ".join(s for p in body["plan"] for s in p["statements"])
    assert "otherdb" not in grants and "evil.example.com" not in grants
    assert "`appdb`" in grants, grants


def test_an_injection_in_the_identity_cannot_reach_the_sql():
    _env()
    body = _call({"action": "grant",
                  "user_email": "alice'; DROP TABLE users;--@example.com"}).body
    grants = " ".join(s for p in body["plan"] for s in p["statements"])
    assert "DROP TABLE" not in grants, grants
    assert cloud_db_sql_service._IDENT_RE.match(body["username"]), body["username"]


def test_an_unknown_role_is_rejected_rather_than_downgraded():
    _env()
    resp = _call({"action": "grant", "user_email": "a@example.com", "role": "db_owner"})
    assert resp.status == 400, resp.body


# ── Request handling ─────────────────────────────────────────────────────────

def test_entitle_action_spellings_are_accepted():
    _env()
    for spelling in ("grant", "Give Access", "give_access", "GIVEACCESS"):
        resp = _call({"action": spelling.replace(" ", "_"),
                      "user_email": "a@example.com"})
        assert resp.status == 200, (spelling, resp.body)
        assert resp.body["action"] == "grant"
    for spelling in ("revoke", "revoke_access", "REVOKEACCESS", "remove"):
        resp = _call({"action": spelling, "username": "jit_a_1"})
        assert resp.status == 200, (spelling, resp.body)
        assert resp.body["action"] == "revoke"


def test_a_missing_or_unknown_action_is_a_400():
    _env()
    for payload in ({}, {"action": ""}, {"action": "drop_database"}):
        assert _call(payload).status == 400, payload


def test_grant_without_an_identity_is_a_400():
    _env()
    assert _call({"action": "grant"}).status == 400


def test_revoke_without_enough_to_name_the_account_is_a_400():
    _env()
    assert _call({"action": "revoke"}).status == 400
    assert _call({"action": "revoke", "user_email": "a@example.com"}).status == 400


def test_misconfiguration_surfaces_rather_than_silently_defaulting():
    for env, _why in (({"FN_DB_ENGINE": "postgres"}, "engine not supported here"),
                      ({"FN_DB_HOST": ""}, "no host"),
                      ({"FN_DB_PORT": "not-a-number"}, "bad port")):
        _env(**env)
        try:
            _call({"action": "grant", "user_email": "a@example.com"})
        except RuntimeError:
            pass
        else:
            raise AssertionError(f"accepted a bad config: {env}")


# ── Generated credentials ────────────────────────────────────────────────────

def test_generated_passwords_are_accepted_by_the_sql_builders():
    """The workload generates the password but the service validates it — a
    mismatch fails every real grant at runtime, not at test time."""
    for _ in range(200):
        pwd = db_grant._generate_password()
        cloud_db_sql_service.grant_plan(
            "sqlserver", username="jit_a_1", password=pwd,
            database="appdb", role="read", flavor="azure_sql")


def test_two_grants_for_the_same_person_do_not_collide():
    _env()
    first = _call({"action": "grant", "user_email": "a@example.com",
                   "request_id": "req-1"}).body["username"]
    second = _call({"action": "grant", "user_email": "a@example.com",
                    "request_id": "req-2"}).body["username"]
    assert first != second


# ── The vendored SQL module ──────────────────────────────────────────────────

def test_the_sql_module_is_safe_to_ship_into_a_function():
    """It is copied into the zip verbatim, so it must import nothing beyond the
    stdlib and open no connection — otherwise the zero-dependency contract that
    makes this whole feature cheap is broken by the back door."""
    import ast
    path = cloud_db_sql_service.__file__
    tree = ast.parse(open(path, encoding="utf-8").read())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.level:
            raise AssertionError(f"relative import in {path} — it cannot be vendored")
    assert imported <= {"re", "secrets", "string"}, f"non-stdlib imports: {imported}"


def test_the_packager_ships_the_real_sql_module_not_a_copy():
    from web_dashboard.services import cloud_function_package as pkg
    mapped = dict(pkg._WORKLOAD_MODULES["db_grant"])
    assert mapped["services/cloud_db_sql_service.py"] == "sqlplan.py"
    entries = dict(pkg.collect_entries(cloud="aws", workload="db_grant")) \
        if os.path.isdir(pkg._VENDOR_DIR) else None
    if entries is None:
        print(f"SKIP: no vendor dir at {pkg._VENDOR_DIR}; module mapping still asserted")
        return
    with open(cloud_db_sql_service.__file__, "rb") as handle:
        assert entries["sqlplan.py"] == handle.read(), "shipped SQL differs from the tested SQL"


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
