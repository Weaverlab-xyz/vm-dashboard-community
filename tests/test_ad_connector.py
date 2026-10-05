"""Building an AWS AD Connector to a registered on-prem Active Directory.

What these pin:

- only a registered, available on-prem AD can be extended; the cost must be confirmed;
  the VPC needs two subnets and the DNS addresses must be IPv4; one connector per region;
- the build checks the service account out of Password Safe and hands the password to
  ConnectDirectory once, as a bare sAMAccountName; nothing secret lands on the row or the job;
- an Active connector becomes available and joinable on AWS; a Failed one keeps its AWS id
  so Destroy can delete it;
- destroy deletes through the API (no Terraform); the on-prem row cannot be unregistered
  while the connector exists; Reset admin password is refused.

Uses a real temp SQLite database; boto3 and Password Safe are stubbed.

Run: python tests/test_ad_connector.py   (or under pytest)
"""
import asyncio
import json
import os
import sys
import tempfile
import uuid
from datetime import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="ad-connector-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDB}"
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-ad-connector-tests")

import boto3  # noqa: E402

from web_dashboard.database import (Base, Job, ManagedDirectory, RemoteAgent,  # noqa: E402
                                    SessionLocal, engine)
from web_dashboard.services import aws_service, btapi_service  # noqa: E402
from web_dashboard.services import directory_service as ds  # noqa: E402

Base.metadata.create_all(bind=engine)
SECRET = "C0nnector!Pass"


class _FakeDS:
    class exceptions:
        class EntityDoesNotExistException(Exception):
            pass

    def __init__(self, stage="Active"):
        self.stage = stage
        self.calls = []

    def connect_directory(self, **kw):
        self.calls.append(("connect", kw))
        return {"DirectoryId": "d-1234567890"}

    def describe_directories(self, DirectoryIds):
        self.calls.append(("describe", DirectoryIds))
        return {"DirectoryDescriptions": [{
            "DirectoryId": DirectoryIds[0], "Stage": self.stage,
            "StageReason": "cannot reach DNS" if self.stage == "Failed" else "",
            "DnsIpAddrs": ["10.0.0.10", "10.0.0.11"],
            "ConnectSettings": {"SecurityGroupId": "sg-1"}}]}

    def delete_directory(self, DirectoryId):
        self.calls.append(("delete", DirectoryId))


def _patch(fake):
    saved = (boto3.client, aws_service._aws_kwargs, btapi_service.get_ps_credential_with_request,
             ds._aws_wait_active_sync)

    async def checkout(system_id, account_id, duration_min=60, **kw):
        return "req-1", SECRET

    orig_wait = ds._aws_wait_active_sync
    boto3.client = lambda name, **kw: fake
    aws_service._aws_kwargs = lambda region=None: {}
    btapi_service.get_ps_credential_with_request = checkout
    ds._aws_wait_active_sync = lambda region, did, **kw: orig_wait(
        region, did, timeout=40, interval=20, sleep=lambda s: None)
    return saved


def _unpatch(saved):
    (boto3.client, aws_service._aws_kwargs, btapi_service.get_ps_credential_with_request,
     ds._aws_wait_active_sync) = saved


def _onprem(db):
    a = RemoteAgent(id=str(uuid.uuid4()), name=f"agent-{uuid.uuid4().hex[:6]}",
                    agent_version="2.8.0", is_active=True, created_at=datetime.utcnow())
    db.add(a)
    db.commit()
    return ds.register_onprem(
        db, name=f"c{uuid.uuid4().hex[:6]}.example.com", provider="onprem_ad",
        host=f"dc-{uuid.uuid4().hex[:6]}", agent_id=a.id, created_by="t",
        managed_account={"system_id": 11, "account_id": 22, "account_name": "CORP\\svc-adc"})


def _args(onprem, **kw):
    a = dict(onprem_directory_id=onprem.id, region="us-east-2", vpc_id="vpc-1",
             subnet_ids=["subnet-a", "subnet-b"], dns_ips=["10.0.0.10"], created_by="t",
             acknowledge_cost=True)
    a.update(kw)
    return a


def _refused(fn, needle):
    try:
        fn()
    except ds.DirectoryError as e:
        assert needle in str(e), str(e)
    else:
        raise AssertionError(f"accepted; expected {needle!r}")


def test_refusals():
    db = SessionLocal()
    onprem = _onprem(db)
    _refused(lambda: ds.provision_ad_connector(db, **_args(onprem, onprem_directory_id="x")),
             "register the domain")
    _refused(lambda: ds.provision_ad_connector(db, **_args(onprem, acknowledge_cost=False)),
             "Confirm the cost")
    _refused(lambda: ds.provision_ad_connector(db, **_args(onprem, subnet_ids=["s"])),
             "exactly two subnets")
    _refused(lambda: ds.provision_ad_connector(db, **_args(onprem, dns_ips=["dc01"])),
             "not addresses: dc01")
    _refused(lambda: ds.provision_ad_connector(db, **_args(onprem, size="Huge")), "size")
    ds.provision_ad_connector(db, **_args(onprem))
    _refused(lambda: ds.provision_ad_connector(db, **_args(onprem)), "already has an AD Connector")
    db.close()


def _build(stage="Active"):
    db = SessionLocal()
    onprem = _onprem(db)
    out = ds.provision_ad_connector(db, **_args(onprem, netbios="CORP"))
    fake = _FakeDS(stage)
    saved = _patch(fake)
    try:
        asyncio.run(ds.run_provision_apply(db, directory_id=out["directory_id"],
                                           job_id=out["job_id"]))
    finally:
        _unpatch(saved)
    db.expire_all()
    row = db.query(ManagedDirectory).filter(ManagedDirectory.id == out["directory_id"]).one()
    job = db.query(Job).filter(Job.id == out["job_id"]).one()
    return db, onprem, row, job, fake


def test_build_uses_the_password_once_and_stores_none():
    db, onprem, row, job, fake = _build()
    connect = [kw for name, kw in fake.calls if name == "connect"][0]
    assert connect["Password"] == SECRET
    assert connect["ConnectSettings"]["CustomerUserName"] == "svc-adc"
    assert connect["ConnectSettings"]["CustomerDnsIps"] == ["10.0.0.10"]
    assert connect["Name"] == onprem.name and connect["ShortName"] == "CORP"
    assert row.status == "available" and row.directory_id == "d-1234567890"
    assert row.linked_directory_id == onprem.id and row.provider == "aws_ad_connector"
    assert json.loads(row.dns_ips) == ["10.0.0.10", "10.0.0.11"]
    assert job.status == "completed"
    blob = json.dumps(ds.to_dict(row)) + (row.credentials_ref or "") + json.dumps(
        {c.name: str(getattr(job, c.name)) for c in Job.__table__.columns})
    assert SECRET not in blob
    assert [r.id for r in ds.joinable_for(db, "aws", "us-east-2")].count(row.id) == 1
    db.close()


def test_failed_connector_keeps_its_id_for_destroy():
    db, _onprem_row, row, job, _fake = _build(stage="Failed")
    assert row.status == "failed" and row.directory_id == "d-1234567890"
    assert "cannot reach DNS" in row.error_message
    assert job.status == "failed"
    db.close()


def test_destroy_deletes_through_the_api_and_guards_the_onprem_row():
    db, onprem, row, _job, _fake = _build()
    _refused(lambda: ds.unregister(db, directory_id=onprem.id), "destroy that first")
    _refused(lambda: asyncio.run(ds.reset_admin_password(db, directory_id=row.id)),
             "no administrator of its own")
    out = ds.start_decommission(db, directory_id=row.id, created_by="t")
    fake = _FakeDS()
    saved = _patch(fake)
    try:
        asyncio.run(ds.run_decommission(db, directory_id=row.id, job_id=out["job_id"]))
    finally:
        _unpatch(saved)
    assert ("delete", "d-1234567890") in fake.calls
    db.expire_all()
    assert db.query(ManagedDirectory).filter(ManagedDirectory.id == row.id).one().status == "deleted"
    ds.unregister(db, directory_id=onprem.id)
    db.close()


def test_connector_username():
    assert ds.connector_username("CORP\\svc") == "svc"
    assert ds.connector_username("svc@corp.example.com") == "svc"
    assert ds.connector_username("svc") == "svc"


def test_api_route_needs_explicit_grant():
    import inspect
    from web_dashboard.api import directories as api
    assert 'require_explicit_permission("directories", "write")' in inspect.getsource(
        api.build_ad_connector)


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
