"""Entra hybrid join for AWS/GCP Windows servers through an on-prem AD
(services/hybrid_join_service).

What these pin:

- the declaration lives on the on-prem AD row, is inherited by the AD Connector and DNS
  link that extend it, and is refused anywhere else; the OU must sit inside the domain;
- a deploy asking for hybrid join without a synced domain, or without a domain at all, is
  a warning (the domain join still happens) and the declared OU is the default;
- the check reads the Windows computer name the device is named by — from Systems Manager
  on AWS, from the instance name on GCP — and records joined / pending / unverifiable on
  the deploy job; a 403 from Graph is ``unverifiable`` with the permission named;
- the API route is directories:write, audited, and listings and /joinable expose the flag;
- the worker, both deploy paths and the forms are wired.

Real temp SQLite; Graph through httpx.MockTransport; SSM and the Azure credential stubbed.

Run: python tests/test_hybrid_entra_join.py   (or under pytest)
"""
import asyncio
import inspect
import json
import os
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="hybrid-join-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDB}"
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-hybrid-join-tests")

import httpx  # noqa: E402

from web_dashboard.database import AuditLog, Base, ManagedDirectory, SessionLocal, engine  # noqa: E402
from web_dashboard.services import directory_service as ds  # noqa: E402
from web_dashboard.services import hybrid_join_service as hj  # noqa: E402
from web_dashboard.services import job_service  # noqa: E402

Base.metadata.create_all(bind=engine)


def _run(c):
    return asyncio.run(c)


async def _no_sleep(*_a, **_k):
    return None


hj.asyncio.sleep = _no_sleep


def _rows(db):
    onprem = ManagedDirectory(name="corp.example.com", cloud="local", provider="onprem_ad",
                              source="registered", status="available",
                              base_dn="DC=corp,DC=example,DC=com", created_by="alice")
    db.add(onprem)
    db.commit()
    link = ManagedDirectory(name="corp.example.com", cloud="aws", provider="aws_ad_connector",
                            source="provisioned", status="available", region="us-east-2",
                            linked_directory_id=onprem.id, created_by="alice")
    dns = ManagedDirectory(name="corp.example.com", cloud="gcp", provider="dns_link",
                           source="provisioned", status="available",
                           linked_directory_id=onprem.id, created_by="alice")
    managed = ManagedDirectory(name="aws.example.com", cloud="aws", provider="aws_managed_ad",
                               source="provisioned", status="available", created_by="alice")
    db.add_all([link, dns, managed])
    db.commit()
    return onprem, link, dns, managed


def _refuses(fn, needle):
    try:
        fn()
    except hj.HybridError as e:
        assert needle in str(e), str(e)
    else:
        raise AssertionError(f"accepted; expected {needle!r}")


def test_declaration_is_on_the_onprem_row_and_inherited():
    db = SessionLocal()
    onprem, link, dns, managed = _rows(db)
    assert hj.settings_for(db, link)["entra_hybrid"] is False
    hj.set_settings(db, onprem, entra_hybrid=True,
                    hybrid_ou="OU=Cloud Servers,DC=corp,DC=example,DC=com")
    for row in (onprem, link, dns):
        s = hj.settings_for(db, row)
        assert s["entra_hybrid"] is True and s["hybrid_ou"].startswith("OU=Cloud Servers")
    assert hj.settings_for(db, managed)["entra_hybrid"] is False
    out = ds.to_dict(onprem)
    assert out["entra_hybrid"] is True and out["hybrid_ou"].startswith("OU=Cloud")
    assert ds.to_dict(link)["entra_hybrid"] is False, "the link only inherits; it stores nothing"
    _refuses(lambda: hj.set_settings(db, link, entra_hybrid=True), "inherit")
    _refuses(lambda: hj.set_settings(db, onprem, entra_hybrid=True, hybrid_ou="CN=Computers"),
             "starting with OU=")
    _refuses(lambda: hj.set_settings(db, onprem, entra_hybrid=True,
                                     hybrid_ou="OU=Servers,DC=other,DC=com"), "inside the domain")
    db.close()


def test_deploy_check_warns_and_supplies_the_ou():
    db = SessionLocal()
    onprem, link, dns, managed = _rows(db)
    problem, _ = hj.deploy_check(db, is_windows=True, ad_directory_id=link.id)
    assert "not marked as synced" in problem
    hj.set_settings(db, onprem, entra_hybrid=True, hybrid_ou="OU=Cloud,DC=corp,DC=example,DC=com")
    assert hj.deploy_check(db, is_windows=True, ad_directory_id=link.id) == \
        ("", "OU=Cloud,DC=corp,DC=example,DC=com")
    assert "pick its AD" in hj.deploy_check(db, is_windows=True, ad_directory_id="")[0]
    assert "Windows images only" in hj.deploy_check(db, is_windows=False, ad_directory_id=link.id)[0]
    assert "not marked" in hj.deploy_check(db, is_windows=True, ad_directory_id=managed.id)[0]
    db.close()


def test_computer_names():
    assert hj.computer_name("aws", ssm_computer_name="EC2AMAZ-AB12CD3.corp.example.com") == "EC2AMAZ-AB12CD3"
    assert hj.computer_name("gcp", instance_name="win-server-long-name-1") == "WIN-SERVER-LONG"
    assert hj.computer_name("aws") == ""


class _Graph:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, req):
        self.calls.append(req)
        status, value = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        return httpx.Response(status, json={"value": value})


def _stub_creds():
    from web_dashboard.services import azure_service
    saved = (azure_service._ensure_creds, azure_service._to_thread)

    class _Tok:
        token = "graph-token"

    class _Cred:
        def get_token(self, scope):
            assert scope == "https://graph.microsoft.com/.default"
            return _Tok()

    async def ensure():
        return _Cred(), "sub"

    async def to_thread(fn, *a, **k):
        return fn(*a, **k)
    azure_service._ensure_creds, azure_service._to_thread = ensure, to_thread

    def restore():
        azure_service._ensure_creds, azure_service._to_thread = saved
    return restore


def _check(db, cloud, replies, **meta):
    deploy = job_service.create_job(db, "ec2_deploy" if cloud == "aws" else "gce_deploy",
                                    "alice", metadata={"instance_name": meta.get("instance_name", "win1")})
    check_id = hj.queue_check(db, deploy_job_id=deploy.id, cloud=cloud,
                              instance_name=meta.get("instance_name", "win1"),
                              region="us-east-2", instance_id="i-123", created_by="alice")
    graph = _Graph(replies)
    hj.transport = httpx.MockTransport(graph)
    try:
        _run(hj.run(db, job_id=check_id, meta=job_service.get_job(db, check_id).metadata_dict))
    finally:
        hj.transport = None
    db.expire_all()
    return (job_service.get_job(db, deploy.id).metadata_dict,
            job_service.get_job(db, check_id), graph)


def test_check_records_joined_pending_and_unverifiable():
    db = SessionLocal()
    restore = _stub_creds()
    orig = hj._ssm_computer_name_sync
    hj._ssm_computer_name_sync = lambda region, iid: "EC2AMAZ-AB12CD3.corp.example.com"
    try:
        md, job, graph = _check(db, "aws", [
            (200, []),
            (200, [{"id": "o1", "deviceId": "dev-1", "displayName": "EC2AMAZ-AB12CD3",
                    "trustType": "Workplace"}]),
            (200, [{"id": "o1", "deviceId": "dev-2", "displayName": "EC2AMAZ-AB12CD3",
                    "trustType": "ServerAd"}])])
        assert md["entra_hybrid_state"] == "joined" and md["entra_device_id"] == "dev-2"
        assert job.status == "completed" and len(graph.calls) == 3
        assert graph.calls[0].url.params["$filter"] == "displayName eq 'EC2AMAZ-AB12CD3'"
        assert graph.calls[0].headers["Authorization"] == "Bearer graph-token"

        md, job, _ = _check(db, "gcp", [(403, [])], instance_name="win-gcp-1")
        assert md["entra_hybrid_state"] == "unverifiable" and "Device.Read.All" in md["entra_hybrid_note"]

        md, job, graph = _check(db, "gcp", [(200, [])], instance_name="win-gcp-2")
        assert md["entra_hybrid_state"] == "pending" and "sync scope" in md["entra_hybrid_note"]
        assert len(graph.calls) == hj.POLL_LIMIT and job.status == "completed"
    finally:
        hj._ssm_computer_name_sync = orig
        restore()
        db.close()


def test_a_bad_computer_name_never_reaches_a_graph_filter():
    db = SessionLocal()
    restore = _stub_creds()
    orig = hj._ssm_computer_name_sync
    hj._ssm_computer_name_sync = lambda region, iid: "x' or 1 eq 1 or displayName eq 'y"
    try:
        md, job, graph = _check(db, "aws", [(200, [])])
        assert md["entra_hybrid_state"] == "unverifiable" and not graph.calls
    finally:
        hj._ssm_computer_name_sync = orig
        restore()
        db.close()


def test_api_route_rbac_audit_and_joinable_flag():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from web_dashboard.api import directories as api
    from web_dashboard.api.auth import get_current_user
    db = SessionLocal()
    onprem, link, dns, managed = _rows(db)

    class _U:
        def __init__(self, perms, name="alice"):
            self.username, self.is_admin, self.is_effective_admin = name, False, False
            self.effective_permissions_dict = perms

    def client(user):
        app = FastAPI()
        app.include_router(api.router)
        app.dependency_overrides[get_current_user] = lambda: user
        return TestClient(app)
    w = client(_U({"directories": ["read", "write"]}))
    r = w.put(f"/api/directories/{onprem.id}/hybrid",
              json={"entra_hybrid": True, "hybrid_ou": "OU=Cloud,DC=corp,DC=example,DC=com"})
    assert r.status_code == 200 and r.json()["entra_hybrid"] is True, r.text
    assert w.put(f"/api/directories/{link.id}/hybrid", json={"entra_hybrid": True}).status_code == 400
    assert client(_U({"directories": ["read"]})).put(
        f"/api/directories/{onprem.id}/hybrid", json={"entra_hybrid": False}).status_code == 403
    assert client(_U({"directories": ["read", "write"]}, "bob")).put(
        f"/api/directories/{onprem.id}/hybrid", json={"entra_hybrid": False}).status_code == 404
    db.expire_all()
    assert db.query(AuditLog).filter(AuditLog.action == "directory_hybrid_join").count() >= 1
    orig = api.has_permission
    api.has_permission = lambda user, scope, level: True
    try:
        rows = w.get("/api/directories/joinable?cloud=aws&region=us-east-2").json()["directories"]
    finally:
        api.has_permission = orig
    flags = {d["id"]: d["entra_hybrid"] for d in rows}
    assert flags.get(link.id) is True
    db.close()


def test_worker_deploys_and_forms_are_wired():
    from web_dashboard import jobs_worker
    from web_dashboard.services import aws_vm_service, gcp_vm_service
    assert hj.JOB_TYPE in jobs_worker.LIGHT_TYPES
    assert 'job_type == "windows_hybrid_check"' in inspect.getsource(jobs_worker)
    for mod in (aws_vm_service, gcp_vm_service):
        src = inspect.getsource(mod)
        assert "hybrid_join_service.deploy_check(" in src and "_queue_hybrid_check(" in src
    aws_src = inspect.getsource(aws_vm_service)
    assert 'if hybrid_check and result.get("ad_joined"):' in aws_src, \
        "AWS checks only after its synchronous join succeeded"
    for page in ("aws", "gcp"):
        html = open(os.path.join(_ROOT, "web_dashboard", "templates", page, "index.html"),
                    encoding="utf-8").read()
        assert html.count('<option value="hybrid">') == 2, page
        assert "d.entra_hybrid ? ' · Entra hybrid'" in html
    html = open(os.path.join(_ROOT, "web_dashboard", "templates", "directories", "index.html"),
                encoding="utf-8").read()
    assert "/hybrid`" in html and "openHybrid(d)" in html


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
