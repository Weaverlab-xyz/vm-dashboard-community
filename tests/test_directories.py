"""Managed Active Directory (AWS + GCP): what building, finding and removing one does.

What these pin:

- provision refuses before anything bills: bad names, no acceptable place for the admin
  password, an unacknowledged cost, AWS without exactly two subnets, GCP without a /24;
- a provisioned directory gets NO auto-delete timer;
- after the apply the admin password is set FRESH (AWS: ResetUserPassword, so the value
  Terraform holds is dead) and stored via the custody module — never on the row or the
  job — and Password Safe onboarding retires that copy once it holds the credential;
- a failure after the directory exists never fails the build;
- destroy is refused while dashboard VMs are joined, and never offered for a
  registered directory; unregister never touches the cloud;
- discovery maps both clouds' shapes and marks what is already registered.

Uses a real temp SQLite database; cloud and Terraform calls are stubbed.

Run: python tests/test_directories.py   (or under pytest)
"""
import asyncio
import json
import os
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="directories-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDB}"
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-directory-tests")

from web_dashboard.database import Base, Job, ManagedDirectory, SessionLocal, engine  # noqa: E402
from web_dashboard.services import (directory_service as ds, job_service,  # noqa: E402
                                    windows_admin_secret as was)

Base.metadata.create_all(bind=engine)

_CFG: dict = {}


def _cfg(**kv):
    _CFG.clear()
    _CFG.update(kv)


ds._cfg = lambda key, default="": _CFG.get(key, default)
was.resolve_backend = lambda cloud: "bt_secrets_safe" if _CFG.get("_custody", True) else \
    (_ for _ in ()).throw(was.WindowsSecretError("no secret manager"))

AWS = dict(cloud="aws", name="corp.example.com", created_by="t", acknowledge_cost=True,
           region="us-east-1", vpc_id="vpc-1", subnet_ids=["subnet-a", "subnet-b"])
GCP = dict(cloud="gcp", name="corp.example.com", created_by="t", acknowledge_cost=True,
           project="p1", reserved_ip_range="10.250.0.0/24", networks=["vpc-main"])


def _db():
    return SessionLocal()


def _refuses(fn, needle):
    try:
        fn()
    except ds.DirectoryError as e:
        assert needle in str(e), str(e)
    else:
        raise AssertionError(f"accepted; expected a refusal mentioning {needle!r}")


# ── provision ─────────────────────────────────────────────────────────────────

def test_provision_validates_before_anything_bills():
    _cfg()
    db = _db()
    before = db.query(ManagedDirectory).count()
    _refuses(lambda: ds.provision(db, **{**AWS, "name": "corp"}), "fully qualified")
    _refuses(lambda: ds.provision(db, **{**AWS, "acknowledge_cost": False}), "Confirm the cost")
    _refuses(lambda: ds.provision(db, **{**AWS, "subnet_ids": ["subnet-a"]}), "exactly two subnets")
    _refuses(lambda: ds.provision(db, **{**AWS, "edition": "Gold"}), "edition")
    _refuses(lambda: ds.provision(db, **{**GCP, "reserved_ip_range": "10.0.0.0/16"}), "/24")
    _refuses(lambda: ds.provision(db, **{**GCP, "networks": []}), "authorized VPC network")
    _cfg(_custody=False)
    _refuses(lambda: ds.provision(db, **AWS), "no secret manager")
    assert db.query(ManagedDirectory).count() == before
    db.close()


def test_provision_records_row_and_job_without_timer_or_secret():
    _cfg()
    db = _db()
    out = ds.provision(db, **AWS, register_in_passwordsafe=True)
    row = ds.get_directory(db, out["directory_id"])
    assert row.source == "provisioned" and row.status == "provisioning"
    assert row.expires_at is None
    assert row.admin_password_ref is None
    job = job_service.get_job(db, out["job_id"])
    assert job.job_type == "directory_provision"
    meta = job.metadata_dict
    assert meta["register_in_passwordsafe"] is True
    assert "password" not in json.dumps(meta).lower().replace("register_in_passwordsafe", "")
    g = ds.provision(db, **GCP)
    grow = ds.get_directory(db, g["directory_id"])
    assert json.loads(grow.networks) == ["projects/p1/global/networks/vpc-main"]
    db.close()


# ── apply + admin credential ──────────────────────────────────────────────────

def _stub_apply(outputs):
    calls = {}

    async def apply(deploy_dir, variables, template_dir=None, env=None, on_line=None):
        calls["vars"] = variables
        return outputs

    async def broadcast(*a, **k):
        pass

    ds.terraform.apply = apply
    ds.terraform_provider_env.provider_env = lambda cloud: {}
    import web_dashboard.api.websocket as ws
    ws.broadcast_progress = broadcast
    return calls


def test_apply_sets_a_fresh_admin_password_and_stores_it():
    _cfg()
    db = _db()
    out = ds.provision(db, **AWS)
    calls = _stub_apply({"directory_id": {"value": "d-123"},
                         "dns_ip_addresses": {"value": ["10.0.1.10", "10.0.2.10"]},
                         "security_group_id": {"value": "sg-9"}})
    reset = {}
    ds._aws_reset_admin_sync = lambda region, did, pw: reset.update(did=did, pw=pw)
    stored = {}

    def store(cloud, name, suffix, password, *, prefix):
        stored.update(cloud=cloud, name=name, pw=password, prefix=prefix)
        return "bt_secrets_safe", f"Dashboard/{prefix}-{name}-{suffix}"

    was.store = store
    asyncio.run(ds.run_provision_apply(db, directory_id=out["directory_id"],
                                       job_id=out["job_id"]))
    db.expire_all()
    row = ds.get_directory(db, out["directory_id"])
    assert row.status == "available" and row.directory_id == "d-123"
    assert json.loads(row.dns_ips) == ["10.0.1.10", "10.0.2.10"]
    # The create-time password and the stored one differ: the state value is dead.
    assert reset["did"] == "d-123" and reset["pw"] == stored["pw"]
    assert calls["vars"]["admin_password"] != stored["pw"]
    assert stored["prefix"] == "ad-admin"
    assert row.admin_password_backend == "bt_secrets_safe"
    job = job_service.get_job(db, out["job_id"])
    assert job.status == "completed"
    assert stored["pw"] not in json.dumps(job.metadata_dict)
    db.close()


def test_credential_failure_after_build_is_a_warning():
    _cfg()
    db = _db()
    out = ds.provision(db, **AWS)
    _stub_apply({"directory_id": {"value": "d-456"}, "dns_ip_addresses": {"value": []},
                 "security_group_id": {"value": "sg"}})

    def boom(*a, **k):
        raise RuntimeError("AccessDenied: ds:ResetUserPassword")

    ds._aws_reset_admin_sync = boom
    asyncio.run(ds.run_provision_apply(db, directory_id=out["directory_id"],
                                       job_id=out["job_id"]))
    db.expire_all()
    row = ds.get_directory(db, out["directory_id"])
    assert row.status == "available"
    assert "Reset admin password" in row.error_message
    assert job_service.get_job(db, out["job_id"]).status == "completed"
    db.close()


def test_password_safe_onboarding_retires_the_stored_copy():
    _cfg()
    db = _db()
    out = ds.provision(db, **GCP, register_in_passwordsafe=True)
    _stub_apply({"resource_name": {"value": "projects/p1/locations/global/domains/corp.example.com"}})
    ds._gcp_reset_admin_sync = lambda name: "Gcp-Chosen-Pw1"
    was.store = lambda cloud, name, suffix, pw, *, prefix: ("gcp_sm", "ad-admin-x")
    deleted = []
    was.delete = lambda b, r: deleted.append((b, r)) or ""
    from web_dashboard.services import ps_vm_hook
    ps_vm_hook.registration_enabled = lambda: True
    seen = {}

    async def reg(db_, job_id, **kw):
        seen.update(kw)
        kw["result"].update(ps_managed_system_id=7, ps_managed_account_id=8,
                            ps_registration_tf_state="{}", ps_initial_password_seeded=True,
                            ps_change_password_triggered=True)

    ps_vm_hook.register_password_managed = reg
    asyncio.run(ds.run_provision_apply(db, directory_id=out["directory_id"],
                                       job_id=out["job_id"]))
    db.expire_all()
    row = ds.get_directory(db, out["directory_id"])
    assert seen["username"] == "setupadmin" and seen["password"] == "Gcp-Chosen-Pw1"
    assert "directory" in seen["platform_tokens"]
    assert row.admin_password_custody == "passwordsafe_managed"
    assert ("gcp_sm", "ad-admin-x") in deleted
    assert row.admin_password_ref is None
    db.close()


def test_failed_apply_marks_row_failed():
    _cfg()
    db = _db()
    out = ds.provision(db, **AWS)

    async def apply(*a, **k):
        raise RuntimeError("InsufficientSubnets")

    _stub_apply({})
    ds.terraform.apply = apply
    asyncio.run(ds.run_provision_apply(db, directory_id=out["directory_id"],
                                       job_id=out["job_id"]))
    db.expire_all()
    assert ds.get_directory(db, out["directory_id"]).status == "failed"
    assert job_service.get_job(db, out["job_id"]).status == "failed"
    db.close()


# ── destroy guard, unregister ─────────────────────────────────────────────────

def _joined_vm(db, directory_id, destroyed=False):
    job = job_service.create_job(db, "ec2_deploy", "t", metadata={
        "instance_id": "i-1", "instance_name": "win01", "ad_directory_id": directory_id,
        "ad_joined": True, "destroyed": destroyed})
    db.commit()
    return job


def test_destroy_refused_while_servers_are_joined():
    _cfg()
    db = _db()
    out = ds.provision(db, **AWS)
    row = ds.get_directory(db, out["directory_id"])
    row.status = "available"
    db.commit()
    job = _joined_vm(db, row.id)
    _refuses(lambda: ds.start_decommission(db, directory_id=row.id, created_by="t"),
             "joined server")
    meta = job.metadata_dict
    meta["destroyed"] = True
    job.metadata_dict = meta
    db.commit()
    res = ds.start_decommission(db, directory_id=row.id, created_by="t")
    assert job_service.get_job(db, res["job_id"]).job_type == "directory_decommission"
    db.close()


def test_registered_directories_are_never_destroyed_and_unregister_is_local():
    _cfg()
    db = _db()
    row = ManagedDirectory(name="ad.example.org", cloud="aws", provider="aws_ad_connector",
                           source="registered", status="available", directory_id="d-reg")
    db.add(row)
    db.commit()
    _refuses(lambda: ds.start_decommission(db, directory_id=row.id, created_by="t"),
             "unregister")
    ds.unregister(db, directory_id=row.id)
    assert ds.get_directory(db, row.id) is None
    db.close()


# ── discovery / registration ──────────────────────────────────────────────────

def test_aws_discovery_maps_types_and_marks_registered():
    _cfg(aws_region="us-east-1")
    db = _db()
    db.add(ManagedDirectory(name="known.example.com", cloud="aws", provider="aws_managed_ad",
                            source="registered", status="available", directory_id="d-known"))
    db.commit()
    ds._aws_describe_sync = lambda region: [
        {"DirectoryId": "d-known", "Name": "known.example.com", "Type": "MicrosoftAD",
         "Stage": "Active", "VpcSettings": {"VpcId": "vpc-1", "SubnetIds": ["a", "b"]},
         "DnsIpAddrs": ["10.0.0.2"]},
        {"DirectoryId": "d-conn", "Name": "CORP.LOCAL", "Type": "ADConnector",
         "Stage": "Active", "ConnectSettings": {"VpcId": "vpc-2", "SubnetIds": ["c"],
                                                "ConnectIps": ["10.1.0.5"]}},
        {"DirectoryId": "d-new", "Name": "new.example.com", "Type": "MicrosoftAD",
         "Stage": "Creating", "VpcSettings": {"VpcId": "vpc-1"}}]
    found = {c["identifier"]: c for c in asyncio.run(ds.discover(db, cloud="aws"))}
    assert found["d-known"]["registered"] is True
    assert found["d-conn"]["provider"] == "aws_ad_connector"
    assert found["d-conn"]["dns_ips"] == ["10.1.0.5"] and found["d-conn"]["name"] == "corp.local"
    assert found["d-new"]["joinable"] is False
    _refuses(lambda: asyncio.run(ds.register(db, cloud="aws", identifier="d-new",
                                             created_by="t")), "not ready")
    row = asyncio.run(ds.register(db, cloud="aws", identifier="d-conn", created_by="t"))
    assert row.source == "registered" and row.admin_password_ref is None
    assert row.expires_at is None
    db.close()


def test_gcp_discovery_shape():
    _cfg(gcp_project="p1")
    db = _db()
    ds._gcp_list_sync = lambda project: [
        {"name": "projects/p1/locations/global/domains/ad.example.net",
         "fqdn": "ad.example.net", "state": "READY", "locations": ["us-central1"],
         "authorizedNetworks": ["projects/p1/global/networks/main"],
         "reservedIpRange": "10.250.0.0/24", "admin": "setupadmin"}]
    found = asyncio.run(ds.discover(db, cloud="gcp"))
    assert found[0]["joinable"] and found[0]["name"] == "ad.example.net"
    row = asyncio.run(ds.register(db, cloud="gcp", identifier=found[0]["identifier"],
                                  created_by="t"))
    assert row.resource_name.endswith("/domains/ad.example.net")
    assert json.loads(row.networks) == ["projects/p1/global/networks/main"]
    db.close()


def test_expiry_honours_provisioned_and_refuses_registered():
    from web_dashboard.services import expiry_policy, inventory_service
    assert "directory" in expiry_policy.REAPABLE_KINDS
    assert expiry_policy.ttl_capable({"kind": "directory", "cloud": "aws",
                                      "source": "provisioned"})[0]
    assert not expiry_policy.ttl_capable({"kind": "directory", "cloud": "gcp",
                                          "source": "registered"})[0]
    row = ManagedDirectory(id="x", name="a.example.com", cloud="aws",
                           provider="aws_managed_ad", source="registered", status="available")
    item = inventory_service._directory_item(row)
    assert item["id"] == "directory:x" and item["source"] == "registered"


def _client(user):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from web_dashboard.api import directories as api
    from web_dashboard.api.auth import get_current_user
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app)


class _User:
    def __init__(self, perms, admin=False, username="alice"):
        import json as _j
        self.username = username
        self.is_admin = admin
        self.is_effective_admin = admin
        self.permissions = _j.dumps(perms) if perms is not None else None
        self.is_active = True
        self.workgroups = "[]"
        self.must_change_password = False


def test_api_routes_need_an_explicit_grant():
    from web_dashboard.api import directories as api
    import inspect
    src = inspect.getsource(api)
    assert 'require_permission("directories"' not in src
    assert src.count('require_explicit_permission("directories"') >= 8


def test_admin_password_route_refuses_password_safe_custody():
    _cfg()
    db = _db()
    row = ManagedDirectory(name="ps.example.com", cloud="aws", provider="aws_managed_ad",
                           source="provisioned", status="available", created_by="alice",
                           admin_password_custody="passwordsafe_managed", ps_account_id="8")
    db.add(row)
    db.commit()
    c = _client(_User(None, admin=True))
    r = c.get(f"/api/directories/{row.id}/admin-password")
    assert r.status_code == 409, r.text
    assert "Password Safe" in r.json()["detail"]
    db.close()


def test_joinable_needs_the_clouds_write_permission():
    _cfg()
    db = _db()
    db.add(ManagedDirectory(name="join.example.com", cloud="aws", provider="aws_managed_ad",
                            source="registered", status="available", region="us-west-2",
                            directory_id="d-join"))
    db.commit()
    from web_dashboard.api import directories as api
    orig = api.has_permission
    api.has_permission = lambda user, scope, level: level in (user.grants.get(scope) or [])
    try:
        u = _User(None)
        u.grants = {"aws": ["read"]}
        r = _client(u).get("/api/directories/joinable?cloud=aws")
        assert r.status_code == 403, r.text
        u.grants = {"aws": ["read", "write"]}
        r = _client(u).get("/api/directories/joinable?cloud=aws&region=us-west-2")
    finally:
        api.has_permission = orig
    assert r.status_code == 200, r.text
    names = [d["name"] for d in r.json()["directories"]]
    assert names == ["join.example.com"]
    db.close()


def test_password_generator_rules():
    for _ in range(50):
        pw = ds.generate_admin_password()
        assert len(pw) == 24 and "admin" not in pw.lower()
        assert any(c.isupper() for c in pw) and any(c.isdigit() for c in pw)


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
