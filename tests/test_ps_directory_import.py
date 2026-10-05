"""Importing directories from Password Safe.

What these pin:

- the catalog keeps directory-linked and directory-platform systems, drops the rest, and
  takes domain, port and SSL from Password Safe's Directories row;
- a candidate with no requestable account, no host or a non-FQDN AD domain is listed but
  ineligible, with a reason;
- the import route re-resolves every id from its own read: an account that is not on the
  chosen system is refused, and host/port never come from the request;
- import goes through register_onprem, so an old agent is refused per item and nothing
  secret is stored;
- a duplicate selection is refused (400) before anything is written; per-item failures,
  such as an already-registered directory, come back in `failed` with a count of 0;
- both routes need directories:write explicitly and secrets:use.

Uses a real temp SQLite database; Password Safe is stubbed.

Run: python tests/test_ps_directory_import.py   (or under pytest)
"""
import os
import sys
import tempfile
import uuid
from datetime import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="ps-dir-import-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDB}"
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-ps-directory-import")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from web_dashboard.database import (Base, ManagedDirectory, RemoteAgent,  # noqa: E402
                                    SessionLocal, engine, get_db)
from web_dashboard.services import ps_directory_catalog as cat  # noqa: E402

Base.metadata.create_all(bind=engine)

PLATFORMS = [{"PlatformID": 25, "Name": "Active Directory"},
             {"PlatformID": 26, "Name": "LDAP"},
             {"PlatformID": 2, "Name": "Linux"}]
DIRECTORIES = [{"DirectoryID": 7, "DomainName": "corp.example.com", "UseSSL": True,
                "Port": 636, "PlatformID": 25},
               {"DirectoryID": 8, "DomainName": "ldap.example.com", "UseSSL": False,
                "Port": 389, "PlatformID": 26},
               {"DirectoryID": 9, "DomainName": "CORP", "PlatformID": 25}]
SYSTEMS = [{"ManagedSystemID": 101, "SystemName": "corp.example.com", "DirectoryID": 7},
           {"ManagedSystemID": 102, "SystemName": "ldap01", "DirectoryID": 8},
           {"ManagedSystemID": 103, "SystemName": "web01", "PlatformID": 2,
            "HostName": "web01.example.com"},
           {"ManagedSystemID": 104, "SystemName": "nobody", "DirectoryID": 7},
           {"ManagedSystemID": 105, "SystemName": "CORP", "DirectoryID": 9}]
ACCOUNTS = [{"ManagedAccountID": 501, "ManagedSystemID": 101, "AccountName": "svc-ansible",
             "DomainName": "corp.example.com"},
            {"ManagedAccountID": 502, "ManagedSystemID": 102,
             "AccountName": "cn=admin,dc=example,dc=com"},
            {"ManagedAccountID": 503, "ManagedSystemID": 103, "AccountName": "root"},
            {"ManagedAccountID": 505, "ManagedSystemID": 105, "AccountName": "svc"}]
RAW = {"platforms": PLATFORMS, "systems": SYSTEMS, "directories": DIRECTORIES,
       "accounts": ACCOUNTS, "warnings": []}


def _cands():
    rows, _ = cat.build_candidates(platforms=PLATFORMS, systems=SYSTEMS,
                                   directories=DIRECTORIES, accounts=ACCOUNTS)
    return {r["system_id"]: r for r in rows}


def test_catalog_shapes_directories_only():
    c = _cands()
    assert set(c) == {101, 102, 104, 105}, "the Linux system is not a directory"
    ad = c[101]
    assert ad["provider"] == "onprem_ad" and ad["name"] == "corp.example.com"
    assert ad["host"] == "corp.example.com" and ad["port"] == 636 and ad["use_ldaps"]
    assert ad["eligible"] and ad["accounts"][0]["name"] == "svc-ansible"
    ldap = c[102]
    assert ldap["provider"] == "ldap" and ldap["port"] == 389 and not ldap["use_ldaps"]
    assert ldap["name"] == "ldap01"


def test_catalog_reasons():
    c = _cands()
    assert not c[104]["eligible"] and "requestable" in c[104]["reason"]
    assert not c[105]["eligible"] and "DNS name" in c[105]["reason"]
    assert cat.managed_account(c[101], 999) == {}
    assert cat.managed_account(c[101], 501) == {"system_id": 101, "account_id": 501,
                                                "account_name": "svc-ansible"}


# ── routes ───────────────────────────────────────────────────────────────────

class _Admin:
    username = "tester"
    is_admin = True
    is_effective_admin = True
    effective_permissions_dict = {}


def _client(user=None):
    from web_dashboard.api import directories as api
    from web_dashboard.api.auth import get_current_user
    from web_dashboard.services import ps_api_service

    async def read():
        return RAW

    ps_api_service.read_directory_inventory = read
    ps_api_service.configured = lambda: True
    api._ps_ready = lambda: ""
    app = FastAPI()
    app.include_router(api.router)

    def _db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_user] = lambda: (user or _Admin())
    return TestClient(app)


def _agent(version="2.8.0"):
    db = SessionLocal()
    a = RemoteAgent(id=str(uuid.uuid4()), name=f"agent-{uuid.uuid4().hex[:6]}",
                    agent_version=version, is_active=True, created_at=datetime.utcnow())
    db.add(a)
    db.commit()
    aid = a.id
    db.close()
    return aid


def _clear():
    db = SessionLocal()
    db.query(ManagedDirectory).delete()
    db.commit()
    db.close()


def test_candidates_route():
    _clear()
    r = _client().get("/api/directories/ps-candidates")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["configured"] is True
    assert {s["system_id"] for s in body["systems"]} == {101, 102, 104, 105}


def test_import_registers_through_register_onprem():
    _clear()
    agent = _agent()
    c = _client()
    r = c.post("/api/directories/ps-import", json={"items": [
        {"system_id": 101, "account_id": 501, "agent_id": agent},
        {"system_id": 102, "account_id": 502, "agent_id": agent, "base_dn": "dc=example,dc=com"},
        {"system_id": 104, "account_id": 501, "agent_id": agent},
        {"system_id": 101 + 1000, "account_id": 1, "agent_id": agent}]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 2
    errors = {f["system_id"]: f["error"] for f in body["failed"]}
    assert "requestable" in errors[104]
    assert "no longer present" in errors[1101]
    db = SessionLocal()
    ad = db.query(ManagedDirectory).filter(ManagedDirectory.host == "corp.example.com").one()
    assert ad.cloud == "local" and ad.agent_id == agent and ad.port == 636
    assert ad.credentials_ref.startswith("psmanaged:") and "svc-ansible" in ad.credentials_ref
    db.close()
    # Annotated on the next read, and refused on a second import.
    rows = {s["system_id"]: s for s in c.get("/api/directories/ps-candidates").json()["systems"]}
    assert rows[101]["already_registered"] is True
    r = c.post("/api/directories/ps-import", json={"items": [
        {"system_id": 101, "account_id": 501, "agent_id": agent}]})
    assert r.status_code == 200 and r.json()["count"] == 0 and "already registered" in r.text


def test_an_account_from_another_system_is_refused():
    _clear()
    agent = _agent()
    r = _client().post("/api/directories/ps-import", json={"items": [
        {"system_id": 101, "account_id": 503, "agent_id": agent}]})
    assert r.status_code == 200 and r.json()["count"] == 0 and "not a requestable account on that directory" in r.text


def test_old_agent_is_refused_per_item():
    _clear()
    old = _agent("2.7.0")
    r = _client().post("/api/directories/ps-import", json={"items": [
        {"system_id": 101, "account_id": 501, "agent_id": old}]})
    assert r.status_code == 200 and r.json()["count"] == 0 and "2.8" in r.text


def test_selection_problems_refuse_the_whole_request():
    agent = _agent()
    c = _client()
    r = c.post("/api/directories/ps-import", json={"items": [
        {"system_id": 101, "account_id": 501, "agent_id": agent},
        {"system_id": 101, "account_id": 501, "agent_id": agent}]})
    assert r.status_code == 400 and "more than once" in r.text
    assert c.post("/api/directories/ps-import", json={"items": []}).status_code == 400


def test_grants():
    class _NoSecrets(_Admin):
        is_admin = False
        is_effective_admin = False
        effective_permissions_dict = {"directories": ["read", "write"]}

    class _NoDirs(_Admin):
        is_admin = False
        is_effective_admin = False
        effective_permissions_dict = {"secrets": ["use"]}

    r = _client(_NoSecrets()).get("/api/directories/ps-candidates")
    assert r.status_code == 403 and "secrets:use" in r.text
    r = _client(_NoDirs()).get("/api/directories/ps-candidates")
    assert r.status_code == 403 and "directories:write" in r.text


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
            traceback.print_exc()
    sys.exit(1 if failures else 0)
