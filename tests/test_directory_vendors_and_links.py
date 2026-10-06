"""LDAP vendors (PingDirectory, Okta LDAP Interface), the Password Safe rules that find
them and the cloud identity providers, and pinning a directory's Entitle integration.

What these pin:

- a rootDSE vendorName / platform name maps to a vendor (Ping Identity → PingDirectory),
  and a discovery finding carries it so the register link can prefill it;
- an Okta LDAP Interface registration fills host, LDAPS 636 and base DN from the org
  name, and refuses a non-636 port, a missing org, or a vendor on an AD row;
- only read-only playbooks run against an Okta LDAP Interface, refused at target
  resolution before any job exists;
- the Password Safe catalog maps "Azure Active Directory" to Entra ID rather than AD,
  lists Okta/Entra/PingOne platforms as "complete registration" rows that the import
  route refuses, and keeps PingDirectory's vendor through an import;
- the Entitle list reads /public/v1/integrations off the API URL's host, pages, puts
  likely matches first and never echoes Entitle's error text;
- pinning checks the id against Entitle, records Entitle's name, audits, and unpins.

Real temp SQLite; Password Safe and Entitle stubbed.

Run: python tests/test_directory_vendors_and_links.py   (or under pytest)
"""
import asyncio
import os
import sys
import tempfile
import uuid
from datetime import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="dir-vendors-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDB}"
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-directory-vendor-tests")

import httpx  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from web_dashboard.database import (AuditLog, Base, ManagedDirectory,  # noqa: E402
                                    RemoteAgent, SessionLocal, engine)
from web_dashboard.services import directory_service as ds  # noqa: E402
from web_dashboard.services import entitle_directory_link as link  # noqa: E402
from web_dashboard.services import ps_directory_catalog as cat  # noqa: E402

Base.metadata.create_all(bind=engine)

ACCOUNT = {"system_id": 11, "account_id": 22, "account_name": "uid=svc,dc=acme,dc=okta,dc=com"}


def _agent(db):
    a = RemoteAgent(id=str(uuid.uuid4()), name=f"agent-{uuid.uuid4().hex[:6]}",
                    agent_version="2.8.0", is_active=True,
                    public_key="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIvend",
                    last_seen_at=datetime.utcnow(), created_at=datetime.utcnow())
    db.add(a)
    db.commit()
    return a


def _refuses(fn, needle):
    try:
        fn()
    except ds.DirectoryError as e:
        assert needle in str(e), str(e)
    else:
        raise AssertionError(f"accepted; expected a refusal mentioning {needle!r}")


# ── vendors ───────────────────────────────────────────────────────────────────

def test_vendor_for_and_okta_org():
    assert ds.vendor_for("Ping Identity Corporation 9.3.0.0") == "pingdirectory"
    assert ds.vendor_for("UnboundID Corp.") == "pingdirectory"
    assert ds.vendor_for("OpenLDAP") == "openldap"
    assert ds.vendor_for("389 Project") == "389ds"
    assert ds.vendor_for("Microsoft") == ""
    assert ds.okta_org("acme") == "acme"
    assert ds.okta_org("https://Acme.okta.com/") == "acme"
    assert ds.okta_org("acme.ldap.okta.com") == "acme"
    assert ds.okta_org("bad org!") == ""


def test_okta_ldap_registration_fills_the_fixed_shape():
    db = SessionLocal()
    a = _agent(db)
    try:
        row = ds.register_onprem(db, name="acme", provider="ldap", vendor="okta_ldap",
                                 host="", port=0, use_ldaps=False, agent_id=a.id,
                                 managed_account=ACCOUNT, created_by="t")
        assert (row.host, row.port, row.use_ldaps) == ("acme.ldap.okta.com", 636, True)
        assert row.base_dn == "dc=acme,dc=okta,dc=com" and row.vendor == "okta_ldap"
        out = ds.to_dict(row)
        assert out["provider_label"] == "Okta LDAP Interface" and out["vendor"] == "okta_ldap"
        base = dict(provider="ldap", vendor="okta_ldap", agent_id=a.id,
                    managed_account=ACCOUNT, created_by="t")
        _refuses(lambda: ds.register_onprem(db, name="beta", host="", port=389, **base),
                 "636 only")
        _refuses(lambda: ds.register_onprem(db, name="", host="10.0.0.5", **base),
                 "org name")
        _refuses(lambda: ds.register_onprem(db, name="corp.example.com", provider="onprem_ad",
                                            vendor="pingdirectory", host="dc1", agent_id=a.id,
                                            managed_account=ACCOUNT, created_by="t"),
                 "only for an LDAP directory")
        _refuses(lambda: ds.register_onprem(db, name="x", provider="ldap", vendor="novell",
                                            host="h", base_dn="dc=x", agent_id=a.id,
                                            managed_account=ACCOUNT, created_by="t"),
                 "vendor must be one of")
        ping = ds.register_onprem(db, name="ping.example.com", provider="ldap",
                                  vendor="pingdirectory", host="pd1.example.com",
                                  base_dn="dc=example,dc=com", agent_id=a.id,
                                  managed_account=ACCOUNT, created_by="t")
        assert ds.to_dict(ping)["provider_label"] == "PingDirectory"
    finally:
        db.close()


def test_only_read_only_playbooks_run_against_okta_ldap():
    from web_dashboard.api.config_mgmt import _resolve_agent_target
    db = SessionLocal()
    a = _agent(db)

    class _P:
        def __init__(self, target_id, asset):
            self.agent_id, self.target_kind, self.target_id = a.id, "directory", target_id
            self.connection_id = self.target = self.transport = self.winrm_host = ""
            self.port, self.asset = 0, asset
    try:
        okta = ds.register_onprem(db, name="zeta", provider="ldap", vendor="okta_ldap",
                                  host="", agent_id=a.id, managed_account=ACCOUNT,
                                  created_by="t")
        out = _resolve_agent_target(_P(okta.id, "ldap-search.yml"), db)
        assert out["target_host"] == "zeta.ldap.okta.com" and out["target_port"] == 636
        for asset in ("ldap-group-membership.yml", "dir/ldap-entry.yml", "mine.yml"):
            try:
                _resolve_agent_target(_P(okta.id, asset), db)
            except HTTPException as e:
                assert e.status_code == 400 and "search-only" in e.detail, e.detail
            else:
                raise AssertionError(f"{asset} ran against an Okta LDAP Interface")
        plain = ds.register_onprem(db, name="ol.example.com", provider="ldap",
                                   vendor="openldap", host="ol1.example.com",
                                   base_dn="dc=example,dc=com", agent_id=a.id,
                                   managed_account=ACCOUNT, created_by="t")
        assert _resolve_agent_target(_P(plain.id, "ldap-entry.yml"), db)["run_kind"] == "directory"
    finally:
        db.close()


def test_discovery_finding_carries_the_ldap_vendor():
    from web_dashboard.api.agent import _annotate_findings
    db = SessionLocal()
    out = _annotate_findings(db, {"findings": [
        {"kind": "directory", "product": "ldap", "host": "10.1.1.1", "port": 636,
         "vendor": "Ping Identity Corporation 9.3"},
        {"kind": "directory", "product": "active_directory", "host": "10.1.1.2", "port": 636,
         "vendor": "Microsoft Active Directory"}]})["findings"]
    assert out[0]["ldap_vendor"] == "pingdirectory"
    assert "ldap_vendor" not in out[1]
    db.close()
    html = open(os.path.join(_ROOT, "web_dashboard", "templates", "jobs", "detail.html"),
                encoding="utf-8").read()
    assert "d.set('vendor', f.ldap_vendor)" in html


# ── Password Safe catalog ─────────────────────────────────────────────────────

PLATFORMS = [{"PlatformID": 1, "Name": "Azure Active Directory"},
             {"PlatformID": 2, "Name": "Active Directory"},
             {"PlatformID": 3, "Name": "Okta"},
             {"PlatformID": 4, "Name": "PingDirectory"},
             {"PlatformID": 5, "Name": "PingOne"}]
SYSTEMS = [{"ManagedSystemID": 1, "SystemName": "Contoso tenant", "PlatformID": 1},
           {"ManagedSystemID": 2, "SystemName": "corp.example.com", "PlatformID": 2,
            "DnsName": "corp.example.com"},
           {"ManagedSystemID": 3, "SystemName": "acme", "PlatformID": 3,
            "DnsName": "acme.okta.com"},
           {"ManagedSystemID": 4, "SystemName": "pd1", "PlatformID": 4,
            "DnsName": "pd-import.example.com", "Port": 636},
           {"ManagedSystemID": 5, "SystemName": "ping env", "PlatformID": 5}]
ACCOUNTS = [{"ManagedAccountID": 10 + i, "ManagedSystemID": i, "AccountName": f"acct{i}"}
            for i in (1, 2, 3, 4)]


def test_catalog_classifies_idps_and_pingdirectory():
    rows, _ = cat.build_candidates(platforms=PLATFORMS, systems=SYSTEMS, directories=[],
                                   accounts=ACCOUNTS)
    c = {r["system_id"]: r for r in rows}
    assert c[1]["provider"] == "entra_id", "Azure Active Directory must not land on AD"
    assert c[2]["provider"] == "onprem_ad"
    for sid, prov in ((1, "entra_id"), (3, "okta")):
        assert c[sid]["provider"] == prov and c[sid]["idp"] is True
        assert not c[sid]["eligible"] and c[sid]["reason"] == cat.REASON_NEEDS_IDP_DETAILS
    assert c[5]["provider"] == "pingone" and c[5]["reason"] == cat.REASON_NO_ACCOUNT
    assert c[4]["provider"] == "ldap" and c[4]["vendor"] == "pingdirectory"
    assert c[4]["idp"] is False
    assert cat.provider_for_platform("OpenLDAP") == "ldap"


# ── Entitle ───────────────────────────────────────────────────────────────────

class _Ent:
    def __init__(self, pages=None, status=200):
        self.pages = pages or [[{"id": "i-ad", "name": "Corp AD",
                                 "application": {"name": "active directory"}},
                                {"id": "i-okta", "name": "Acme Okta",
                                 "application": {"name": "okta"}}]]
        self.status = status
        self.calls = []

    def __call__(self, req):
        self.calls.append(req)
        if self.status != 200:
            return httpx.Response(self.status, json={"message": "SECRET-INTERNAL-DETAIL"})
        page = int(req.url.params.get("page", "1"))
        rows = self.pages[page - 1] if page <= len(self.pages) else []
        return httpx.Response(200, json={"result": rows})


def _ent_cfg(api_url="https://api.us.entitle.io/v1", token="ent-token"):
    vals = {"entitle_api_url": api_url, "entitle_api_token": token}
    link._cfg = lambda key: vals.get(key, "")


def _run(c):
    return asyncio.run(c)


def test_entitle_list_reads_the_public_api_and_orders_likely_first():
    _ent_cfg()
    e = _Ent(pages=[[{"id": f"x{i}", "name": f"z{i}", "application": {"name": "postgres"}}
                     for i in range(100)],
                    [{"id": "i-okta", "name": "Acme Okta", "application": {"name": "okta"}}]])
    link.transport = httpx.MockTransport(e)
    try:
        items = _run(link.list_integrations("okta"))
        assert e.calls[0].url.host == "api.us.entitle.io"
        assert e.calls[0].url.path == "/public/v1/integrations"
        assert e.calls[0].headers["Authorization"] == "Bearer ent-token"
        assert len(e.calls) == 2 and len(items) == 101
        assert items[0] == {"id": "i-okta", "name": "Acme Okta", "application": "okta",
                            "suggested": True}
        link.transport = httpx.MockTransport(_Ent(status=401))
        try:
            _run(link.list_integrations("okta"))
        except link.EntitleLinkError as err:
            assert "SECRET-INTERNAL-DETAIL" not in str(err) and "token" in str(err)
        else:
            raise AssertionError("a 401 was not reported")
        _ent_cfg(api_url="http://api.us.entitle.io/v1")
        assert not link.configured(), "plain http must not carry the token"
    finally:
        link.transport = None


class _User:
    def __init__(self, perms, username="alice"):
        self.username = username
        self.is_admin = self.is_effective_admin = False
        self.effective_permissions_dict = perms
        self.is_active = True
        self.workgroups = "[]"


def _client(user):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from web_dashboard.api import directories as api
    from web_dashboard.api.auth import get_current_user
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app)


def test_pinning_checks_entitle_records_its_name_and_audits():
    _ent_cfg()
    link.transport = httpx.MockTransport(_Ent())
    db = SessionLocal()
    row = ManagedDirectory(name="corp.example.com", cloud="local", provider="onprem_ad",
                           source="registered", status="available", created_by="alice")
    db.add(row)
    db.commit()
    try:
        w = _client(_User({"directories": ["read", "write"]}))
        r = w.get(f"/api/directories/{row.id}/entitle-candidates")
        assert r.status_code == 200 and r.json()["integrations"][0]["id"] == "i-ad"
        r = w.put(f"/api/directories/{row.id}/entitle-integration", json={"integration_id": "nope"})
        assert r.status_code == 400
        r = w.put(f"/api/directories/{row.id}/entitle-integration", json={"integration_id": "i-ad"})
        assert r.status_code == 200, r.text
        assert r.json()["entitle_integration_name"] == "Corp AD"
        db.expire_all()
        assert db.query(AuditLog).filter(AuditLog.action == "directory_entitle_pin").count() == 1
        r = w.put(f"/api/directories/{row.id}/entitle-integration", json={"integration_id": ""})
        assert r.json()["entitle_integration_id"] is None
        reader = _client(_User({"directories": ["read"]}))
        assert reader.get(f"/api/directories/{row.id}/entitle-candidates").status_code == 403
        other = _client(_User({"directories": ["read", "write"]}, username="bob"))
        assert other.put(f"/api/directories/{row.id}/entitle-integration",
                         json={"integration_id": "i-ad"}).status_code == 404
        _ent_cfg(token="")
        r = w.get(f"/api/directories/{row.id}/entitle-candidates")
        assert r.json()["configured"] is False
    finally:
        link.transport = None
        db.close()


def test_ps_import_refuses_idp_rows_and_keeps_the_vendor():
    from web_dashboard.api import directories as api
    db = SessionLocal()
    a = _agent(db)

    async def fake_read(_db):
        rows, _ = cat.build_candidates(platforms=PLATFORMS, systems=SYSTEMS, directories=[],
                                       accounts=ACCOUNTS)
        for r in rows:
            r["already_registered"] = False
        return {"systems": rows, "truncated": False, "warnings": []}
    origs = (api._read_ps_candidates, api._ps_ready, api._require_secrets_use)
    api._read_ps_candidates, api._ps_ready = fake_read, lambda: ""
    api._require_secrets_use = lambda user: None
    try:
        w = _client(_User({"directories": ["read", "write"]}))
        r = w.post("/api/directories/ps-import", json={"items": [
            {"system_id": 3, "account_id": 13, "agent_id": a.id},
            {"system_id": 4, "account_id": 14, "agent_id": a.id,
             "base_dn": "dc=example,dc=com"}]})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["count"] == 1 and body["failed"][0]["system_id"] == 3
        assert "Complete registration" in body["failed"][0]["error"]
        db.expire_all()
        pd = db.query(ManagedDirectory).filter(ManagedDirectory.host == "pd-import.example.com").one()
        assert pd.vendor == "pingdirectory"
    finally:
        api._read_ps_candidates, api._ps_ready, api._require_secrets_use = origs
        db.close()


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
