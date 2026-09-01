"""``get_all_permissions``, against the schema Entitle really validates it with.

Every adapter here answered this route with LISTS, which is what the published
example in the OpenAPI definition shows. Entitle's validator disagrees: both
permission fields are maps keyed by asset id, and a list is rejected with

    Invalid response. Structure of "Get All Permission Response" is invalid.
    [{'asset_id': ..., 'role_code': 'read'}, ...] is not of type 'object'

The failure is quiet in the way that matters — ``get_assets`` and ``get_actors``
sync green, the integration looks healthy, and Entitle silently keeps whatever it
last believed about who holds access, which is the set it reconciles against. So the
shape is pinned here for every adapter at once rather than per adapter, and
``_assert_permissions`` below is a transcription of the schema from that error.

Runs offline: fake transports for Portainer and ARM, no database for db_grant.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from web_dashboard import functions  # noqa: F401  (puts fnruntime on sys.path)
from fnruntime import entitle
from fnruntime.contract import Context, Request
from fnworkloads import azure_role_grant, db_grant, entitle_webhook_echo, portainer_access


# ── The schema, as Entitle applies it ────────────────────────────────────────

def _assert_permissions(data, where, assets=None):
    """Fail exactly where Entitle's validator would, and say the same thing.

    ``assets`` — the identifiers ``get_assets`` published — is checked too when it is
    known: the map key IS the asset, so a key that is not one is a permission
    reported against something Entitle has never heard of.
    """
    assert isinstance(data, dict), f"{where}: data is not an object"
    assert "actors_permissions" in data, f"{where}: actors_permissions is required"
    assert set(data) <= {"actors_permissions", "assets_permissions"}, \
        f"{where}: additionalProperties is False; got {sorted(data)}"

    for field, required in (("actors_permissions", ("actor_id", "role_code")),
                            ("assets_permissions", ("asset_id", "role_code"))):
        block = data.get(field, {})
        assert isinstance(block, dict), \
            f"{where}: {field} is {type(block).__name__}, not the map Entitle requires"
        for asset_id, rows in block.items():
            assert isinstance(asset_id, str) and asset_id, f"{where}: {field} key {asset_id!r}"
            if assets is not None:
                assert asset_id in assets, \
                    f"{where}: {field} keyed on {asset_id!r}, which get_assets never listed"
            assert isinstance(rows, list), f"{where}: {field}[{asset_id!r}] is not an array"
            for row in rows:
                assert isinstance(row, dict), f"{where}: {field}[{asset_id!r}] item"
                for key in required:
                    assert isinstance(row.get(key), str), \
                        f"{where}: {field}[{asset_id!r}] item needs a string {key}"
    # assets_permissions items are additionalProperties: False in the schema.
    for asset_id, rows in data.get("assets_permissions", {}).items():
        for row in rows:
            extra = set(row) - {"asset_id", "role_code"}
            assert not extra, f"{where}: assets_permissions[{asset_id!r}] has {sorted(extra)}"


def test_the_helper_itself_rejects_the_list_shape():
    """The bug, as a test: guard against a helper that would pass anything."""
    for bad in ({"actors_permissions": [], "assets_permissions": []},
                {"actors_permissions": [{"actor_id": "a", "role_code": "read",
                                         "direct_member": True}]},
                {"actors_permissions": {"db:1": {"actor_id": "a"}}},
                {"assets_permissions": {}}):
        try:
            _assert_permissions(bad, "self-check")
        except AssertionError:
            continue
        raise AssertionError(f"validator accepted {bad!r}")


def test_permissions_data_builds_the_map_even_from_nothing():
    empty = entitle.permissions_data()
    assert empty == {"actors_permissions": {}, "assets_permissions": {}}
    _assert_permissions(empty, "fnruntime.entitle")


# ── db_grant ─────────────────────────────────────────────────────────────────

def _db_env(**overrides):
    for key in ("FN_DB_ENGINE", "FN_DB_HOST", "FN_DB_NAME", "FN_DB_NAMES",
                "FN_DB_DRY_RUN", "FN_DB_ADMIN_USER"):
        os.environ.pop(key, None)
    base = {"FN_DB_ENGINE": "mysql", "FN_DB_HOST": "db.internal",
            "FN_DB_NAMES": "app_db,reporting", "FN_DB_ADMIN_USER": "dbadmin",
            "FN_DB_DRY_RUN": "0"}
    base.update(overrides)
    os.environ.update(base)


def test_db_grant_keys_permissions_by_database():
    """The adapter that hit this live, on the configuration that hit it: several
    databases on one server, read without touching one."""
    _db_env()
    targets = db_grant._targets()
    real = db_grant._list_actors_in
    db_grant._list_actors_in = lambda target: (
        [{"identifier": "jit_alice_1"}] if target["database"] == "app_db" else [])
    try:
        data = db_grant._permissions_body(targets)
    finally:
        db_grant._list_actors_in = real
    _assert_permissions(data, "db_grant", assets=set(targets))
    assert set(data["actors_permissions"]) == set(targets), "every database is a key"
    app_db = next(i for i in targets if i.endswith(":app_db"))
    assert [r["actor_id"] for r in data["actors_permissions"][app_db]] == ["jit_alice_1"]
    assert data["actors_permissions"][next(i for i in targets
                                           if i.endswith(":reporting"))] == []


def test_db_grant_reports_an_account_under_every_database_it_holds():
    """The flat list had to de-duplicate across databases; keyed by asset, doing so
    would report the second grant as absent."""
    _db_env()
    targets = db_grant._targets()
    real = db_grant._list_actors_in
    db_grant._list_actors_in = lambda target: [{"identifier": "jit_alice_1"}]
    try:
        data = db_grant._permissions_body(targets)
    finally:
        db_grant._list_actors_in = real
    assert all(rows and rows[0]["actor_id"] == "jit_alice_1"
               for rows in data["actors_permissions"].values()), data


def test_db_grant_dry_run_is_still_the_right_shape():
    """Dry run is the DEFAULT, so this is the first response Entitle ever validates."""
    _db_env(FN_DB_DRY_RUN="1")
    for path in ("/get_all_permissions", f"/get_asset_permissions/{list(db_grant._targets())[0]}"):
        body = db_grant.handle(
            Request(method="GET", path=path, headers={}, query={}, body=b"{}",
                    source="aws_function_url"),
            Context.from_env(workload="db_grant")).body
        _assert_permissions(body["data"], f"db_grant {path}")


# ── portainer_access ─────────────────────────────────────────────────────────

def test_portainer_keys_permissions_by_team():
    os.environ.update({"FN_PORTAINER_URL": "https://portainer.internal",
                       "FN_PORTAINER_API_KEY": "ptr_key", "FN_PORTAINER_DRY_RUN": "0"})
    state = {
        "/api/users": [{"Id": 1, "Username": "admin"},
                       {"Id": 2, "Username": "jit-alice-example-com-abc"}],
        "/api/teams": [{"Id": 7, "Name": "Platform"}, {"Id": 8, "Name": "Empty"}],
        "/api/team_memberships": [{"Id": 11, "UserID": 2, "TeamID": 7, "Role": 2}],
    }
    real = portainer_access._api
    portainer_access._api = lambda config, method, path, body=None: state.get(path, [])
    try:
        body = portainer_access.handle(
            Request(method="GET", path="/get_all_permissions", headers={}, query={},
                    body=b"{}", source="aws_function_url"),
            Context.from_env(workload="portainer_access")).body
        assets = {a["identifier"] for a in portainer_access.handle(
            Request(method="GET", path="/get_assets", headers={}, query={}, body=b"{}",
                    source="aws_function_url"),
            Context.from_env(workload="portainer_access")).body["data"]["assets"]}
    finally:
        portainer_access._api = real
    data = body["data"]
    _assert_permissions(data, "portainer_access", assets=assets)
    assert [r["actor_id"] for r in data["actors_permissions"]["portainer:team:7"]] == \
        ["jit-alice-example-com-abc"]
    assert data["actors_permissions"]["portainer:team:8"] == [], "a team nobody is in"


# ── azure_role_grant ─────────────────────────────────────────────────────────

def test_azure_keys_permissions_by_scope():
    sub = "11111111-2222-3333-4444-555555555555"
    scope = f"/subscriptions/{sub}/resourceGroups/prod"
    os.environ.update({
        "FN_AZURE_TENANT_ID": "t", "FN_AZURE_CLIENT_ID": "c",
        "FN_AZURE_CLIENT_SECRET": "s", "FN_AZURE_SUBSCRIPTION_ID": sub,
        "FN_AZURE_SCOPES": scope, "FN_AZURE_ROLES": "reader",
        "FN_AZURE_PRINCIPALS": "99999999-8888-7777-6666-555555555555=deploy-sp",
        "FN_AZURE_DRY_RUN": "0"})
    existing = set()

    def _fake_arm(config, method, path, body=None, ok_missing=False):
        if method == "GET":
            return {"id": path} if path in existing else None
        if method == "PUT":
            existing.add(path)
            return {"id": path}
        return None

    real = azure_role_grant._arm
    azure_role_grant._arm = _fake_arm
    try:
        def _call(method, path, payload=None):
            return azure_role_grant.handle(
                Request(method=method, path=path, headers={}, query={},
                        body=json.dumps(payload or {}).encode(),
                        source="aws_function_url"),
                Context.from_env(workload="azure_role_grant")).body

        assets = {a["identifier"] for a in _call("GET", "/get_assets")["data"]["assets"]}
        empty = _call("GET", "/get_all_permissions")["data"]
        _assert_permissions(empty, "azure_role_grant (nothing granted)", assets=assets)
        assert set(empty["actors_permissions"]) == assets, "every scope is a key"

        _call("POST", "/give_access", {"asset": {"identifier": f"azure:scope:{scope}"},
                                       "actor_identifier": "deploy-sp",
                                       "role_code": "reader"})
        data = _call("GET", "/get_all_permissions")["data"]
    finally:
        azure_role_grant._arm = real
    _assert_permissions(data, "azure_role_grant", assets=assets)
    held = data["actors_permissions"][f"azure:scope:{scope}"]
    assert [r["role_code"] for r in held] == ["reader"], data


# ── entitle_webhook_echo ─────────────────────────────────────────────────────

def test_the_echo_adapter_teaches_the_right_shape():
    """It exists to prove the contract before a real target is involved; an echo
    that answers in the invalid shape proves the opposite."""
    for path in ("/get_all_permissions", "/get_asset_permissions/demo:asset:1"):
        body = entitle_webhook_echo.handle(
            Request(method="GET", path=path, headers={}, query={}, body=b"{}",
                    source="aws_function_url"), Context.from_env()).body
        _assert_permissions(body["data"], f"entitle_webhook_echo {path}",
                            assets={entitle_webhook_echo._SAMPLE_ASSET["identifier"]})


# ── The dashboard-hosted adapters ────────────────────────────────────────────

def test_the_dashboard_hosted_adapters_are_keyed_by_asset_too():
    """``api/entitle_rest.py`` and ``api/pov_accessor_rest.py`` serve the same
    contract from the dashboard itself, and were wrong in the same way. Their routes
    need FastAPI and a session; what is checked here is that neither builds a list,
    which is the part that failed.
    """
    import re
    root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "web_dashboard", "api")
    for name in ("entitle_rest.py", "pov_accessor_rest.py"):
        with open(os.path.join(root, name), encoding="utf-8") as handle:
            source = handle.read()
        for field in ("actors_permissions", "assets_permissions"):
            for match in re.finditer(rf'"{field}":\s*(.)', source):
                assert match.group(1) != "[", \
                    f"{name}: {field} is built as a list; Entitle requires a map"


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
