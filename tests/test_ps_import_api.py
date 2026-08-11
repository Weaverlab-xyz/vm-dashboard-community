"""Contract tests for the Password Safe database import routes.

The route functions are called directly with a fake session and a fake user, and
Password Safe / register_database / the cache are stubbed — so there is no network,
no database, and no TestClient (which is broken in the app image by a
starlette/httpx drift).

Runs under pytest, or standalone:  python tests/test_ps_import_api.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import HTTPException                                   # noqa: E402
from web_dashboard.api import cloud_databases as api                # noqa: E402
from web_dashboard.services import (cache_service, cloud_database_service,   # noqa: E402
                                    config_service, job_service, ps_api_service)

# What a fake Password Safe tenant answers with. One postgres system with one
# requestable account, one SQL Server with none.
_PLATFORMS = [{"PlatformID": 1, "Name": "PostgreSQL"},
              {"PlatformID": 2, "Name": "MS SQL Server"}]
_SYSTEMS = [{"ManagedSystemID": 10, "SystemName": "pg-prod", "PlatformID": 1,
             "DnsName": "pg.corp.internal", "DatabaseID": 100},
            {"ManagedSystemID": 11, "SystemName": "mssql-prod", "PlatformID": 2,
             "DnsName": "sql.corp.internal", "DatabaseID": 101}]
_DATABASES = [{"DatabaseID": 100, "Port": 5432}, {"DatabaseID": 101, "Port": 1433}]
_ACCOUNTS = [{"ManagedAccountID": 50, "SystemId": 10, "AccountName": "svc-dba"}]


class FakeUser:
    def __init__(self, admin=True, perms=None, username="tester"):
        self.username = username
        self.is_effective_admin = admin
        self.effective_permissions_dict = perms if perms is not None else {}


class _Query:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class FakeDB:
    """Only ``query(CloudDatabase.private_host, CloudDatabase.engine).all()`` is
    reached — register_database is stubbed, so nothing else touches the session."""

    def __init__(self, existing=()):
        self.existing = list(existing)      # [(host, engine)]

    def query(self, *cols):
        return _Query(self.existing)


class Harness:
    """Swaps the module's collaborators out and restores them afterwards."""

    def __init__(self, *, inventory=None, raises=None, beyondtrust=True,
                 configured=True, register=None, cfg=None):
        self.inventory = inventory if inventory is not None else {
            "platforms": _PLATFORMS, "systems": _SYSTEMS,
            "databases": _DATABASES, "accounts": _ACCOUNTS,
            "workgroup_id": None, "warnings": []}
        self.raises = raises
        self.beyondtrust = beyondtrust
        self.configured = configured
        self.register = register
        self.cfg = cfg or {}
        self.registered = []               # every register_database call that got through
        self.audits = []
        self._saved = {}

    def __enter__(self):
        harness = self

        async def _read(**kw):
            if harness.raises:
                raise harness.raises
            return harness.inventory

        async def _direct(key, ttl, fetch):
            harness.cache_key = key
            return await fetch(), None

        def _register(db, **kw):
            harness.registered.append(kw)
            if harness.register:
                return harness.register(kw)
            return {"id": f"db-{len(harness.registered)}"}

        def _get_bool(key, default=False):
            if key == "cloud_database_enabled":
                return True
            if key == "password_safe_enabled":
                return harness.beyondtrust
            return default

        self._swap(ps_api_service, "read_database_inventory", _read)
        self._swap(ps_api_service, "configured", lambda: harness.configured)
        self._swap(cache_service, "get_or_refresh", _direct)
        self._swap(cloud_database_service, "register_database", _register)
        self._swap(config_service, "get_bool", _get_bool)
        self._swap(config_service, "get", lambda key: harness.cfg.get(key, ""))
        self._swap(job_service, "log_audit",
                   lambda db, user, action, **kw: harness.audits.append((action, kw)))
        return self

    def _swap(self, module, name, value):
        self._saved[(module, name)] = getattr(module, name)
        setattr(module, name, value)

    def __exit__(self, *exc):
        for (module, name), value in self._saved.items():
            setattr(module, name, value)
        return False


def _import(items, db=None, user=None, **kw):
    """Returns ``(status_or_None, body_or_detail, harness)``."""
    req = api.PSImportRequest(items=[api.PSImportItem(**i) for i in items])
    with Harness(**kw) as harness:
        try:
            body = asyncio.run(api.ps_import(req, db=db or FakeDB(),
                                             current_user=user or FakeUser()))
            return None, body, harness
        except HTTPException as exc:
            return exc.status_code, exc.detail, harness


def _run_candidates(db=None, user=None, **kw):
    with Harness(**kw):
        return asyncio.run(api.ps_candidates(db=db or FakeDB(),
                                             current_user=user or FakeUser()))


# ── candidates ──────────────────────────────────────────────────────────────────

def test_candidates_shapes_the_tenant_into_eligible_and_ineligible_rows():
    out = _run_candidates()
    assert out["enabled"] is True and out["configured"] is True
    by_id = {c["system_id"]: c for c in out["systems"]}
    assert by_id[10]["eligible"] is True and by_id[10]["engine"] == "postgres"
    # No requestable account — the Requestor-role problem, caught before import.
    assert by_id[11]["eligible"] is False
    assert "Requestor" in by_id[11]["reason"]


def test_candidates_marks_rows_the_dashboard_already_has():
    out = _run_candidates(db=FakeDB(existing=[("pg.corp.internal", "postgres")]))
    by_id = {c["system_id"]: c for c in out["systems"]}
    assert by_id[10]["already_imported"] is True
    assert by_id[11]["already_imported"] is False


def test_already_imported_ignores_who_created_the_row():
    # Unlike the list endpoint. A row another operator imported must still read as
    # imported, or the second of two racing operators gets an unexplainable failure.
    out = _run_candidates(db=FakeDB(existing=[("PG.CORP.INTERNAL", "POSTGRES")]),
                          user=FakeUser(admin=False, perms={"cloud_database": ["read"],
                                                            "secrets": ["use"]}))
    assert {c["system_id"]: c for c in out["systems"]}[10]["already_imported"] is True


def test_candidates_reports_beyondtrust_off_as_a_state_not_an_error():
    out = _run_candidates(beyondtrust=False)
    assert out["enabled"] is False and out["systems"] == []


def test_candidates_reports_an_unconfigured_tenant_with_a_reason():
    out = _run_candidates(configured=False)
    assert out["enabled"] is True and out["configured"] is False
    assert "Settings" in out["reason"]


def test_a_password_safe_error_never_reaches_the_caller():
    # PSApiError embeds a slice of the tenant's response body.
    boom = ps_api_service.PSApiError("GET ManagedSystems failed (500): SUPERSECRETBODY")
    out = _run_candidates(raises=boom)
    assert out["systems"] == []
    assert "SUPERSECRETBODY" not in repr(out)
    assert out["error"] == api._PS_GENERIC_ERROR


def test_an_unexpected_error_does_not_escape_as_a_500():
    out = _run_candidates(raises=RuntimeError("kaboom"))
    assert out["systems"] == [] and out["error"] == api._PS_GENERIC_ERROR
    assert "kaboom" not in repr(out)


def test_the_cache_key_is_scoped_to_the_workgroup():
    with Harness(cfg={"clouddb_ps_import_workgroup": "DBs"}) as harness:
        asyncio.run(api.ps_candidates(db=FakeDB(), current_user=FakeUser()))
    assert "workgroup=DBs" in harness.cache_key
    with Harness() as harness:
        asyncio.run(api.ps_candidates(db=FakeDB(), current_user=FakeUser()))
    assert "workgroup=*" in harness.cache_key


# ── permissions ─────────────────────────────────────────────────────────────────

def _denied(fn):
    try:
        fn()
    except HTTPException as exc:
        return exc
    return None


def test_both_routes_require_secrets_use_on_top_of_the_database_scope():
    # cloud_database:read/write alone enumerates every managed system and account
    # name in the tenant — strictly more than the host-scoped managed-accounts route
    # hands out, which requires secrets:use.
    reader = FakeUser(admin=False, perms={"cloud_database": ["read", "write"]})
    exc = _denied(lambda: _run_candidates(user=reader))
    assert exc is not None and exc.status_code == 403 and "secrets:use" in exc.detail

    status, detail, _ = _import([{"system_id": 10, "account_id": 50}], user=reader)
    assert status == 403 and "secrets:use" in detail


def test_secrets_use_is_enough_alongside_the_database_scope():
    ok = FakeUser(admin=False, perms={"cloud_database": ["read", "write"],
                                      "secrets": ["use"]})
    assert _run_candidates(user=ok)["enabled"] is True


# ── import: selection errors refuse the whole batch, before any write ───────────

def _selection_refusal(items, **kw):
    status, detail, harness = _import(items, **kw)
    assert status == 400, f"expected 400, got {status}: {detail}"
    assert harness.registered == [], "a selection error must write nothing"
    return detail


def test_an_empty_selection_is_refused():
    _selection_refusal([])


def test_an_oversized_batch_is_refused():
    items = [{"system_id": i, "account_id": 1} for i in range(api._MAX_IMPORT_BATCH + 1)]
    assert str(api._MAX_IMPORT_BATCH) in _selection_refusal(items)


def test_the_same_system_twice_is_refused():
    _selection_refusal([{"system_id": 10, "account_id": 50},
                        {"system_id": 10, "account_id": 50}])


def test_an_invalid_location_is_refused():
    detail = _selection_refusal([{"system_id": 10, "account_id": 50, "cloud": "mars"}])
    assert "mars" in detail


def test_an_engine_that_disagrees_with_password_safe_is_refused():
    # The client may send engine, but only to be checked — never trusted.
    detail = _selection_refusal([{"system_id": 10, "account_id": 50,
                                  "engine": "sqlserver"}])
    assert "postgres" in detail


def test_two_systems_colliding_on_host_and_engine_are_refused_as_a_batch():
    # Letting the second fail on the uniqueness row the first just created reads as
    # a Password Safe fault, and which of the two "wins" would be arbitrary.
    twins = {"platforms": _PLATFORMS, "databases": _DATABASES, "accounts": [
                 {"ManagedAccountID": 50, "SystemId": 10, "AccountName": "a"},
                 {"ManagedAccountID": 51, "SystemId": 12, "AccountName": "b"}],
             "workgroup_id": None, "warnings": [],
             "systems": [_SYSTEMS[0],
                         {"ManagedSystemID": 12, "SystemName": "pg-dupe", "PlatformID": 1,
                          "DnsName": "pg.corp.internal", "DatabaseID": 102}]}
    detail = _selection_refusal([{"system_id": 10, "account_id": 50},
                                 {"system_id": 12, "account_id": 51}],
                                inventory=twins)
    assert "same database" in detail


def test_beyondtrust_off_refuses_an_import():
    status, _, harness = _import([{"system_id": 10, "account_id": 50}], beyondtrust=False)
    assert status == 400 and harness.registered == []


def test_a_password_safe_outage_during_import_is_a_503_not_a_leak():
    boom = ps_api_service.PSApiError("GET ManagedSystems failed (500): SUPERSECRETBODY")
    status, detail, harness = _import([{"system_id": 10, "account_id": 50}], raises=boom)
    assert status == 503 and harness.registered == []
    assert "SUPERSECRETBODY" not in detail


# ── import: per-target failures give a partial success ─────────────────────────

def test_a_clean_import_registers_and_reports_it():
    _, body, harness = _import([{"system_id": 10, "account_id": 50}])
    assert body["count"] == 1 and body["failed"] == []
    assert len(harness.registered) == 1
    call = harness.registered[0]
    assert call["engine"] == "postgres"
    assert call["host"] == "pg.corp.internal"
    assert call["cloud"] == "local"                     # the configured default
    assert call["managed_account"]["account_id"] == 50
    assert call["region"] == "" and call["instance_id"] == ""


def test_an_ineligible_system_fails_alone_and_the_rest_still_import():
    _, body, harness = _import([{"system_id": 10, "account_id": 50},   # fine
                                {"system_id": 11, "account_id": 99}])  # no account
    assert body["count"] == 1
    assert [f["system_id"] for f in body["failed"]] == [11]
    assert "Requestor" in body["failed"][0]["error"]
    assert len(harness.registered) == 1


def test_an_account_from_another_system_is_refused_per_target():
    _, body, _ = _import([{"system_id": 10, "account_id": 50},
                          {"system_id": 11, "account_id": 50}])   # 50 belongs to 10
    assert body["count"] == 1 and len(body["failed"]) == 1


def test_a_system_that_vanished_fails_per_target():
    _, body, _ = _import([{"system_id": 10, "account_id": 50},
                          {"system_id": 999, "account_id": 1}])
    assert body["count"] == 1
    assert "no longer present" in body["failed"][0]["error"]


def test_a_register_refusal_passes_its_own_message_through():
    def _boom(kw):
        raise cloud_database_service.CloudDatabaseError("a postgres database at 'x' is already registered")
    status, detail, _ = _import([{"system_id": 10, "account_id": 50}], register=_boom)
    assert status == 400 and "already registered" in detail


def test_when_nothing_imports_the_call_fails_quoting_the_first_reason():
    status, detail, harness = _import([{"system_id": 11, "account_id": 99}])
    assert status == 400
    assert "No databases were imported" in detail and "Requestor" in detail
    assert harness.registered == []


def test_an_already_imported_row_is_refused_per_target():
    # Guards the race the annotation exists for: the candidate list said importable,
    # someone else imported it, and this batch must fail that row rather than let
    # register_database raise a duplicate error the operator cannot place.
    status, detail, harness = _import([{"system_id": 10, "account_id": 50}],
                                      db=FakeDB(existing=[("pg.corp.internal", "postgres")]))
    assert status == 400 and "already registered" in detail
    assert harness.registered == []


def test_the_import_is_audited():
    _, _, harness = _import([{"system_id": 10, "account_id": 50}])
    assert harness.audits and harness.audits[0][0] == "clouddb_ps_import"
    assert harness.audits[0][1]["details"]["count"] == 1


# ── the request model is the injection boundary ────────────────────────────────

def test_no_import_field_lets_a_caller_supply_a_target():
    # The server re-resolves every system_id from its own read. If a host, port or
    # account name were ever accepted here, a cloud_database:write holder could
    # register an arbitrary (host, managed account) pair and skip every rule above.
    fields = set(api.PSImportItem.model_fields)
    assert fields == {"system_id", "account_id", "cloud", "engine", "db_name"}
    for banned in ("host", "hostname", "address", "ip", "port", "account_name",
                   "managed_account", "credentials_ref", "region", "instance_id"):
        assert banned not in fields, f"{banned} must not be settable by a client"


def test_an_extra_field_is_ignored_rather_than_honoured():
    item = api.PSImportItem(**{"system_id": 10, "account_id": 50,
                               "host": "evil.example.com"})
    assert not hasattr(item, "host")


def test_the_routes_are_declared_before_the_db_id_routes():
    # FastAPI matches in declaration order, so a future GET /{db_id} must not be
    # able to swallow /ps-candidates.
    paths = [r.path for r in api.router.routes]
    assert paths.index("/api/databases/ps-candidates") < paths.index(
        "/api/databases/{db_id}/connection")
    assert paths.index("/api/databases/ps-import") < paths.index("/api/databases/{db_id}")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
    sys.exit(1 if failures else 0)
