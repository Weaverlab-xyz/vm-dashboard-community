"""Microsoft Entra Domain Services as a managed directory, and joining Azure VMs to it.

What these pin:

- a build records the resource group, VNet, /24 and the VNet-DNS choice, needs no place
  for an administrator password, and refuses Password Safe onboarding (there is no
  administrator to onboard);
- the Terraform variables and outputs round-trip, and the apply completes WITHOUT
  setting an administrator password, listing what is still to do instead;
- reset-admin-password is refused for Entra DS;
- the preflight refuses an unregistered Microsoft.AAD provider, a missing Domain
  Services service principal and an existing instance, names the fix, and lets an
  unreadable Graph (403) through;
- discovery parses ARM's domainServices list and registration takes the resource group
  from the ARM id;
- the join account is a Password Safe pin only, checked out per join, and the password
  reaches only the extension's protected settings — never the job result;
- a joined Azure VM is seen by the destroy guard;
- the Azure deploy path resolves the directory first, never joins with Entra ID join on,
  and the API offers azure in /joinable with a join_ready flag.

Real temp SQLite; Terraform, ARM, Graph, Password Safe and the VM extension are stubbed.

Run: python tests/test_entra_domain_services.py   (or under pytest)
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

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="entra-ds-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDB}"
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-entra-ds-tests")

from web_dashboard.database import Base, Job, ManagedDirectory, SessionLocal, engine  # noqa: E402
from web_dashboard.services import (directory_service as ds, job_service,  # noqa: E402
                                    windows_admin_secret as was)

Base.metadata.create_all(bind=engine)

_CFG: dict = {}
ds._cfg = lambda key, default="": _CFG.get(key, default)


def _no_backend(cloud):
    raise was.WindowsSecretError("no secret manager")


was.resolve_backend = _no_backend     # Entra DS must not need one

ACCOUNT = {"system_id": 7, "account_id": 70, "account_name": "joiner@contoso.com"}
AZ = dict(cloud="azure", name="aadds.contoso.com", created_by="alice", acknowledge_cost=True,
          region="eastus", resource_group="rg-dir", vnet_name="vnet-main",
          subnet_cidr="10.0.250.0/24")
ARM_ID = ("/subscriptions/sub-1/resourceGroups/rg-found/providers/Microsoft.AAD/"
          "domainServices/aadds-found")


def _db():
    return SessionLocal()


def _clear():
    db = _db()
    db.query(ManagedDirectory).delete()
    db.query(Job).delete()
    db.commit()
    db.close()


def _refuses(fn, needle):
    try:
        out = fn()
        if asyncio.iscoroutine(out):
            asyncio.run(out)
    except ds.DirectoryError as e:
        assert needle in str(e), str(e)
    else:
        raise AssertionError(f"accepted; expected a refusal mentioning {needle!r}")


# ── build ─────────────────────────────────────────────────────────────────────

def test_build_records_the_vnet_and_needs_no_password_store():
    _clear()
    db = _db()
    out = ds.provision(db, **AZ, managed_account=ACCOUNT, manage_vnet_dns=True)
    row = ds.get_directory(db, out["directory_id"])
    assert row.provider == "azure_managed_ad" and row.status == "provisioning"
    assert row.project == "rg-dir" and row.region == "eastus" and row.edition == "Standard"
    assert json.loads(row.networks) == ["rg-dir/vnet-main"]
    assert row.reserved_ip_range == "10.0.250.0/24"
    assert json.loads(row.options) == {"manage_vnet_dns": True}
    assert row.credentials_ref.startswith("psmanaged:")
    assert row.admin_username == "joiner@contoso.com"
    assert ds.template_dir("azure").endswith(os.path.join("directory", "azure_managed_ad"))
    assert "azure" in ds.PROVISIONING_CLOUDS
    db.close()


def test_build_refusals():
    _clear()
    db = _db()
    _refuses(lambda: ds.provision(db, **{**AZ, "subnet_cidr": "10.0.0.0/16"}), "unused /24")
    _refuses(lambda: ds.provision(db, **{**AZ, "vnet_name": ""}), "VNet")
    _refuses(lambda: ds.provision(db, **{**AZ, "resource_group": ""}), "resource group")
    _refuses(lambda: ds.provision(db, **{**AZ, "edition": "Basic"}), "SKU")
    _refuses(lambda: ds.provision(db, **{**AZ, "vnet_name": "bad name;"}), "not a valid")
    _refuses(lambda: ds.provision(db, **AZ, register_in_passwordsafe=True), "no administrator")
    _refuses(lambda: ds.provision(db, **{**AZ, "acknowledge_cost": False}), "$110/month")
    db.close()


def test_terraform_variables_and_outputs():
    _clear()
    db = _db()
    out = ds.provision(db, **AZ, vnet_resource_group="rg-net")
    row = ds.get_directory(db, out["directory_id"])
    v = ds._tf_variables(row)
    assert v == {"resource_group_name": "rg-dir", "location": "eastus",
                 "domain_name": "aadds.contoso.com", "sku": "Standard",
                 "vnet_resource_group": "rg-net", "vnet_name": "vnet-main",
                 "subnet_cidr": "10.0.250.0/24", "manage_vnet_dns": False,
                 "directory_row_id": row.id}
    ds._read_outputs(row, {"resource_name": {"value": ARM_ID},
                           "dns_ip_addresses": {"value": ["10.0.250.4", "10.0.250.5"]}})
    assert row.resource_name == ARM_ID and json.loads(row.dns_ips) == ["10.0.250.4", "10.0.250.5"]
    tf = open(os.path.join(_ROOT, "terraform", "directory", "azure_managed_ad", "main.tf"),
              encoding="utf-8").read()
    for name in v:
        assert f'variable "{name}"' in tf, name
    for out_name in ("resource_name", "dns_ip_addresses"):
        assert f'output "{out_name}"' in tf
    assert 'count              = var.manage_vnet_dns ? 1 : 0' in tf
    db.close()


def test_apply_completes_without_an_admin_password():
    _clear()
    db = _db()
    out = ds.provision(db, **AZ)

    async def apply(deploy_dir, variables, template_dir=None, env=None, on_line=None):
        return {"resource_name": {"value": ARM_ID},
                "dns_ip_addresses": {"value": ["10.0.250.4"]}}

    async def broadcast(*a, **k):
        pass
    ds.terraform.apply = apply
    ds.terraform_provider_env.provider_env = lambda cloud: {}
    import web_dashboard.api.websocket as ws
    ws.broadcast_progress = broadcast

    async def boom(row):
        raise AssertionError("set_admin_password must not run for Entra DS")
    orig = ds.set_admin_password
    ds.set_admin_password = boom
    try:
        asyncio.run(ds.run_provision_apply(db, directory_id=out["directory_id"],
                                           job_id=out["job_id"]))
    finally:
        ds.set_admin_password = orig
    db.expire_all()
    row = ds.get_directory(db, out["directory_id"])
    assert row.status == "available" and row.resource_name == ARM_ID
    job = job_service.get_job(db, out["job_id"])
    assert job.status == "completed"
    warnings = " ".join(job.metadata_dict.get("warnings") or [])
    assert "join account" in warnings and "10.0.250.4" in warnings
    assert "change their password" in warnings
    db.close()


def test_reset_admin_password_is_refused():
    _clear()
    db = _db()
    row = ManagedDirectory(name="aadds.contoso.com", cloud="azure", provider="azure_managed_ad",
                           source="provisioned", status="available")
    db.add(row)
    db.commit()
    _refuses(lambda: ds.reset_admin_password(db, directory_id=row.id), "no administrator password")
    _refuses(lambda: ds.set_admin_password(row), "no administrator of its own")
    db.close()


# ── preflight ─────────────────────────────────────────────────────────────────

def _stub_azure(*, registered="Registered", sp=200, existing=None):
    calls = []

    async def sub():
        return "sub-1"

    async def arm_get(path, params=None):
        calls.append(path)
        if path.endswith("/providers/Microsoft.AAD"):
            return 200, {"registrationState": registered}
        return 200, {"value": existing or []}

    async def graph_get(path):
        calls.append(path)
        return sp
    ds._azure_subscription, ds._arm_get, ds._graph_get = sub, arm_get, graph_get
    return calls


def _found(stage="Succeeded", dns=("10.0.250.4",)):
    return {"id": ARM_ID, "location": "eastus",
            "properties": {"domainName": "AADDS.contoso.com", "sku": "Enterprise",
                           "provisioningState": stage,
                           "replicaSets": [{"subnetId": "/sub/x", "domainControllerIpAddress": list(dns)}]}}


def test_preflight_names_each_fix_and_changes_nothing():
    db = _db()
    _stub_azure(registered="NotRegistered")
    _refuses(lambda: ds.azure_preflight(db), "az provider register --namespace Microsoft.AAD")
    _stub_azure(sp=404)
    _refuses(lambda: ds.azure_preflight(db), "az ad sp create --id 2565bd9d")
    _stub_azure(existing=[_found()])
    _refuses(lambda: ds.azure_preflight(db), "register it with Discover")
    calls = _stub_azure(sp=403)
    asyncio.run(ds.azure_preflight(db))     # an unreadable Graph is not an answer
    assert any("servicePrincipals(appId='2565bd9d" in c for c in calls)
    db.close()


# ── discover / register / join account ───────────────────────────────────────

def test_discover_and_register_take_the_resource_group_from_the_arm_id():
    _clear()
    db = _db()
    _stub_azure(existing=[_found(), {**_found(stage="Provisioning", dns=()), "id": ARM_ID + "2"}])
    found = asyncio.run(ds.discover(db, cloud="azure"))
    assert found[0]["name"] == "aadds.contoso.com" and found[0]["joinable"]
    assert found[0]["dns_ips"] == ["10.0.250.4"] and found[0]["edition"] == "Enterprise"
    assert not found[1]["joinable"]
    row = asyncio.run(ds.register(db, cloud="azure", identifier=ARM_ID, created_by="alice",
                                  managed_account=ACCOUNT))
    assert row.project == "rg-found" and row.resource_name == ARM_ID
    assert row.source == "registered" and row.credentials_ref.startswith("psmanaged:")
    again = asyncio.run(ds.discover(db, cloud="azure"))
    assert again[0]["registered"] is True
    _refuses(lambda: ds.register(db, cloud="azure", identifier=ARM_ID + "2",
                                 created_by="alice"), "not ready")
    db.close()


def test_join_account_is_azure_only_and_never_a_secret():
    _clear()
    db = _db()
    row = ManagedDirectory(name="aadds.contoso.com", cloud="azure", provider="azure_managed_ad",
                           source="registered", status="available")
    aws = ManagedDirectory(name="corp.example.com", cloud="aws", provider="aws_managed_ad",
                           source="registered", status="available")
    db.add_all([row, aws])
    db.commit()
    ds.set_join_account(db, row, ACCOUNT)
    assert json.loads(row.credentials_ref[len("psmanaged:"):]) == {
        "account_id": 70, "account_name": "joiner@contoso.com", "system_id": 7}
    ds.set_join_account(db, row, None)
    assert row.credentials_ref is None and row.admin_username is None
    _refuses(lambda: ds.set_join_account(db, aws, ACCOUNT), "needs no join account")
    assert ds.join_identity(row, "joiner") == "joiner@aadds.contoso.com"
    assert ds.join_identity(row, "joiner@contoso.com") == "joiner@contoso.com"
    db.close()


# ── joining an Azure VM ───────────────────────────────────────────────────────

def test_join_azure_passes_the_password_only_to_protected_settings():
    from web_dashboard.services import azure_service, btapi_service, domain_join_service as dj
    _clear()
    db = _db()
    row = ManagedDirectory(name="aadds.contoso.com", cloud="azure", provider="azure_managed_ad",
                           source="registered", status="available",
                           dns_ips=json.dumps(["10.0.250.4"]))
    db.add(row)
    db.commit()
    ds.set_join_account(db, row, ACCOUNT)
    job = job_service.create_job(db, "azure_deploy", "alice", metadata={"vm_name": "win1"})

    async def checkout(system_id, account_id, duration_min=30, uses_ssh_key=False):
        return 9, "S3cret-Join-Pw"
    seen = {}

    async def join_domain(rg, vm_name, location, *, domain, user, password, ou=""):
        seen.update(rg=rg, vm=vm_name, domain=domain, user=user, password=password, ou=ou,
                    settings=azure_service.domain_join_settings(domain, user, ou))
        return {"extension": "JsonADDomainExtension"}
    origs = (btapi_service.get_ps_credential_with_request, azure_service.join_domain)
    btapi_service.get_ps_credential_with_request = checkout
    azure_service.join_domain = join_domain
    try:
        result = {}
        asyncio.run(dj.join_azure(db, job.id, row=row, ou="OU=Servers,DC=aadds,DC=contoso,DC=com",
                                  rg="rg-vm", vm_name="win1", location="eastus", result=result))
        assert result["ad_joined"] is True and result["ad_domain"] == "aadds.contoso.com"
        assert result["ad_ou"].startswith("OU=Servers")
        assert seen["user"] == "joiner@contoso.com" and seen["password"] == "S3cret-Join-Pw"
        assert "S3cret-Join-Pw" not in json.dumps(seen["settings"])
        assert seen["settings"]["Options"] == "3" and seen["settings"]["Restart"] == "true"
        assert "S3cret-Join-Pw" not in json.dumps(result)

        async def fail(*a, **k):
            raise azure_service.AzureError("extension failed: 1355 domain not found")
        azure_service.join_domain = fail
        result = {}
        asyncio.run(dj.join_azure(db, job.id, row=row, ou="", rg="rg-vm", vm_name="win1",
                                  location="eastus", result=result))
        assert "ad_joined" not in result and "VNet's DNS" in result["ad_join_error"]
        assert "S3cret-Join-Pw" not in json.dumps(result)

        ds.set_join_account(db, row, None)
        result = {}
        asyncio.run(dj.join_azure(db, job.id, row=row, ou="", rg="rg-vm", vm_name="win1",
                                  location="eastus", result=result))
        assert "no domain-join account" in result["ad_join_error"]
    finally:
        btapi_service.get_ps_credential_with_request, azure_service.join_domain = origs
        db.close()


def test_a_joined_azure_vm_blocks_destroy():
    _clear()
    db = _db()
    row = ManagedDirectory(name="aadds.contoso.com", cloud="azure", provider="azure_managed_ad",
                           source="provisioned", status="available")
    db.add(row)
    db.commit()
    job_service.create_job(db, "azure_deploy", "alice", metadata={
        "vm_name": "win-joined", "ad_directory_id": row.id, "ad_joined": True})
    assert ds.joined_vms(db, row.id) == ["win-joined"]
    _refuses(lambda: ds.start_decommission(db, directory_id=row.id, created_by="alice"),
             "win-joined")
    db.close()


def test_azure_deploy_wiring():
    from web_dashboard.models.azure import AzureBulkDeployRequest, AzureDeployRequest
    from web_dashboard.services import azure_vm_service
    for model in (AzureDeployRequest, AzureBulkDeployRequest):
        assert {"ad_directory_id", "ad_ou"} <= set(model.model_fields), model.__name__
    src = inspect.getsource(azure_vm_service._run_deploy)
    assert 'domain_join_service.resolve(db, req.ad_directory_id, "azure", loc)' in src
    assert "domain_join_service.join_azure(" in src
    assert src.index("elif entra_join:") < src.index("domain_join_service.resolve(")
    html = open(os.path.join(_ROOT, "web_dashboard", "templates", "azure", "index.html"),
                encoding="utf-8").read()
    assert "/api/directories/joinable?cloud=azure" in html
    assert "ad_directory_id: m.osType === 'Windows'" in html


def test_joinable_offers_azure_with_join_ready():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from web_dashboard.api import directories as api
    from web_dashboard.api.auth import get_current_user
    _clear()
    db = _db()
    db.add_all([ManagedDirectory(name="ready.contoso.com", cloud="azure",
                                 provider="azure_managed_ad", source="registered",
                                 status="available", credentials_ref="psmanaged:{}"),
                ManagedDirectory(name="bare.contoso.com", cloud="azure",
                                 provider="azure_managed_ad", source="registered",
                                 status="available")])
    db.commit()

    class _U:
        username, is_admin, is_effective_admin = "alice", True, True
        effective_permissions_dict = {}
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[get_current_user] = lambda: _U()
    orig = api.has_permission
    api.has_permission = lambda user, scope, level: True
    try:
        r = TestClient(app).get("/api/directories/joinable?cloud=azure")
    finally:
        api.has_permission = orig
    assert r.status_code == 200, r.text
    ready = {d["name"]: d["join_ready"] for d in r.json()["directories"]}
    assert ready == {"ready.contoso.com": True, "bare.contoso.com": False}
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
