"""Entitle virtual applications over a directory's groups.

What these pin:

- the generated HCL uses only attributes the real entitleio/entitle provider has
  (``tests/fixtures/entitle_provider_schema.json``, dumped from the provider itself), sets
  every required one, puts no agent token on a virtual app and carries no secret;
- a directory refuses a virtual app until its Entitle integration is pinned, and a site
  link refuses one outright;
- a request is checked for shape (names, tiers, duplicates) and every linked role is
  checked against Entitle as belonging to the directory's pinned integration;
- the API records the app, queues one job and audits it; a reader cannot create, another
  user cannot see, and a directory with a live app cannot be unregistered;
- the job stores each new role id, and a failure lands on the row.

Real temp SQLite; Entitle stubbed with httpx.MockTransport; Terraform never runs.

Run: python tests/test_entitle_virtual_app.py   (or under pytest)
"""
import asyncio
import json
import os
import re
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="entitle-vapp-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDB}"
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-entitle-vapp-tests")

import httpx  # noqa: E402

from web_dashboard.database import (AuditLog, Base, EntitleVirtualApp, Job,  # noqa: E402
                                    ManagedDirectory, SessionLocal, engine)
from web_dashboard.services import entitle_public_api as api  # noqa: E402
from web_dashboard.services import entitle_registration_service as reg  # noqa: E402
from web_dashboard.services import entitle_virtual_app_service as vapps  # noqa: E402

Base.metadata.create_all(bind=engine)

SCHEMA = json.load(open(os.path.join(_ROOT, "tests", "fixtures", "entitle_provider_schema.json")))
OWNER, WF, WF_AUTO = ("11111111-1111-4111-8111-111111111111",
                      "22222222-2222-4222-8222-222222222222",
                      "33333333-3333-4333-8333-333333333333")
INTEG, OTHER = "i-okta", "i-github"
CFG = {"entitle_api_url": "https://api.us.entitle.io/v1", "entitle_api_token": "tok",
       "entitle_owner_id": OWNER, "entitle_workflow_id": WF,
       "entitle_workflow_auto_approve_id": WF_AUTO}

ROLES = {
    "role-aws": {"id": "role-aws", "name": "Member", "resource": {
        "id": "res-1", "name": "ent-aws-cloudops", "integration": {"id": INTEG, "name": "Okta"}}},
    "role-az": {"id": "role-az", "name": "Member", "resource": {
        "id": "res-2", "name": "ent-azure-dba", "integration": {"id": INTEG, "name": "Okta"}}},
    "role-gh": {"id": "role-gh", "name": "Admin", "resource": {
        "id": "res-3", "name": "repo", "integration": {"id": OTHER, "name": "GitHub"}}},
}


def _entitle(req):
    path = req.url.path
    if path.startswith("/public/v1/roles/"):
        rid = path.rsplit("/", 1)[1]
        if rid in ROLES:
            return httpx.Response(200, json={"result": ROLES[rid]})
        return httpx.Response(404, json={"message": "nope"})
    if path == "/public/v1/roles":
        rows = [r for r in ROLES.values()
                if r["resource"]["integration"]["id"] == req.url.params.get("integrationId")]
        return httpx.Response(200, json={"result": rows})
    return httpx.Response(404)


_ORIG = (reg._cfg, vapps._cfg, api._default_cfg, api.transport, vapps.registration_enabled)


def teardown_module(_module=None):
    """Put the module seams back so a later test in the same pytest run is unaffected."""
    reg._cfg, vapps._cfg, api._default_cfg, api.transport, vapps.registration_enabled = _ORIG


def _setup():
    reg._cfg = vapps._cfg = api._default_cfg = lambda k: CFG.get(k, "")
    api.transport = httpx.MockTransport(_entitle)
    vapps.registration_enabled = lambda: True


def _dir(db, **kw):
    row = ManagedDirectory(name=kw.pop("name", "acme.okta.com"), cloud="saas",
                           provider=kw.pop("provider", "okta"), source="registered",
                           status="available", created_by="alice",
                           entitle_integration_id=kw.pop("integration", INTEG),
                           entitle_integration_name="Okta", **kw)
    db.add(row)
    db.commit()
    return row


# ── HCL against the provider's real schema ───────────────────────────────────

def _blocks(hcl: str):
    """``[(kind, type, body)]`` for each top-level resource/data block."""
    out = []
    for m in re.finditer(r'^(resource|data) "(\w+)" "\w+" \{\n(.*?)^\}', hcl, re.M | re.S):
        out.append((m.group(1), m.group(2), m.group(3)))
    return out


def _check_against_schema(hcl: str):
    for kind, rtype, body in _blocks(hcl):
        schema = SCHEMA["resource" if kind == "resource" else "data"][rtype]
        used = set()
        for line in body.splitlines():
            m = re.match(r"^  (\w+)\s*=\s*(.*)$", line)
            if not m:
                continue
            attr, value = m.groups()
            assert attr in schema, f"{rtype}.{attr} is not in the provider schema"
            assert schema[attr]["mode"] != "computed", f"{rtype}.{attr} is read-only"
            used.add(attr)
            for inner in re.findall(r"\{\s*(\w+)\s*=", value):
                nested = schema[attr].get("nested") or {}
                assert inner in nested, f"{rtype}.{attr}.{inner} is not in the schema"
        required = {k for k, v in schema.items() if v["mode"] == "required"}
        assert required <= used, f"{rtype} is missing {sorted(required - used)}"


def _sample_resources():
    return [{"name": "AWS", "description": "AWS groups", "roles": [
        {"key": "ent-aws-cloudops", "name": "Cloud Ops — AWS", "virtualized_role_id": "role-aws",
         "workflow_id": WF, "allowed_durations": [3600, 43200]}]},
            {"name": "Azure", "roles": [
        {"key": "ent-azure-dba", "name": "DBA — Azure", "virtualized_role_id": "role-az",
         "workflow_id": WF_AUTO, "allowed_durations": [1800, 3600]}]}]


def test_virtual_app_hcl_matches_the_provider_schema():
    _setup()
    hcl = reg._generate_virtual_app_hcl(name="Cloud access", resources=_sample_resources(),
                                        tags=("vm-dashboard",))
    _check_against_schema(hcl)
    assert 'application     = { name = "virtual application" }' in hcl
    assert "agent_token" not in hcl, "a virtual app is cloud-only; no agent"
    assert "allow_creating_accounts = false" in hcl
    assert hcl.count('resource "entitle_role"') == 2
    assert hcl.count('resource "entitle_resource"') == 2
    assert 'virtualized_role  = { id = "role-aws" }' in hcl
    assert '"ent_aws_cloudops" = entitle_role.ent_aws_cloudops.id' in hcl
    assert "tok" not in hcl, "the API key rides TF_VAR only"


def test_bundle_hcl_matches_the_provider_schema_and_refuses_empty():
    _setup()
    hcl = reg._generate_bundles_hcl(bundles=[{
        "key": "cloudops-aws", "name": "Cloud Ops — AWS", "description": "d",
        "category": "Cloud access", "tags": ["persona:cloudops"], "workflow_id": WF,
        "allowed_durations": [3600], "role_ids": ["a", "b", "a"]}])
    _check_against_schema(hcl)
    assert 'roles             = [{ id = "a" }, { id = "b" }]' in hcl, "duplicates are dropped"
    for bad in ({"role_ids": []}, {"workflow_id": ""}):
        b = {"key": "k", "name": "n", "workflow_id": WF, "allowed_durations": [3600],
             "role_ids": ["a"], **bad}
        try:
            reg._generate_bundles_hcl(bundles=[b])
        except reg.EntitleRegistrationError:
            continue
        raise AssertionError(f"bundle accepted with {bad}")


def test_virtual_app_hcl_refuses_a_role_with_no_workflow():
    _setup()
    res = _sample_resources()
    res[0]["roles"][0]["workflow_id"] = ""
    try:
        reg._generate_virtual_app_hcl(name="x", resources=res)
    except reg.EntitleRegistrationError as e:
        assert "workflow" in str(e)
    else:
        raise AssertionError("a role with no workflow was rendered")


# ── Service ──────────────────────────────────────────────────────────────────

def test_refusals():
    _setup()
    db = SessionLocal()
    try:
        assert vapps.refusal(_dir(db)) == ""
        assert "Pin" in vapps.refusal(_dir(db, integration=None))
        assert "site link" in vapps.refusal(_dir(db, provider="dns_link"))
        vapps.registration_enabled = lambda: False
        assert "registration is off" in vapps.refusal(_dir(db))
    finally:
        vapps.registration_enabled = lambda: True
        db.close()


def test_request_shape_is_checked():
    good = {"role_id": "role-aws", "name": "Cloud Ops — AWS", "resource": "AWS",
            "tier": "single_approver"}
    name, roles = vapps.normalize_request("Cloud access", [good, {**good, "role_id": "role-az"}])
    assert len({r["key"] for r in roles}) == 2, "keys must be unique"
    for bad_name, bad_roles, why in (
            ("x", [good], "Name the application"),
            ("Cloud", [], "at least one"),
            ("Cloud", [good, good], "twice"),
            ("Cloud", [{**good, "tier": "yolo"}], "tier"),
            ("Cloud", [{**good, "role_id": "../x"}], "role it fronts"),
            ("Cloud", [{**good, "name": "a" * 60}], "2–50")):
        try:
            vapps.normalize_request(bad_name, bad_roles)
        except vapps.VirtualAppError as e:
            assert why in str(e), (why, str(e))
        else:
            raise AssertionError(f"accepted: {why}")


def test_prepare_checks_every_role_against_the_pinned_integration():
    _setup()
    db = SessionLocal()
    try:
        row = _dir(db)
        name, roles = asyncio.run(vapps.prepare(row, "Cloud access", [
            {"role_id": "role-aws", "name": "Cloud Ops — AWS", "tier": "auto_approve"}]))
        assert roles[0]["group_name"] == "ent-aws-cloudops"
        try:
            asyncio.run(vapps.prepare(row, "Cloud access", [
                {"role_id": "role-gh", "name": "Repo admin"}]))
        except vapps.VirtualAppError as e:
            assert "GitHub" in str(e)
        else:
            raise AssertionError("a role from another integration was linked")
        try:
            asyncio.run(vapps.prepare(row, "Cloud access", [
                {"role_id": "role-missing", "name": "Ghost"}]))
        except vapps.VirtualAppError as e:
            assert "no such role" in str(e)
        else:
            raise AssertionError("a role Entitle does not have was linked")
    finally:
        db.close()


def test_directory_roles_put_members_first_and_suggest_names():
    _setup()
    db = SessionLocal()
    try:
        roles = asyncio.run(vapps.directory_roles(_dir(db)))
        assert [r["id"] for r in roles] == ["role-aws", "role-az"]
        assert roles[0]["suggested"]["name"] == "Cloud Ops — AWS"
        assert roles[1]["suggested"]["persona"] == "dba"
    finally:
        db.close()


# ── API ──────────────────────────────────────────────────────────────────────

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
    from web_dashboard.api import directories as dapi
    from web_dashboard.api.auth import get_current_user
    app = FastAPI()
    app.include_router(dapi.router)
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app)


def test_api_creates_audits_and_guards_the_directory():
    _setup()
    db = SessionLocal()
    try:
        row = _dir(db, name="corp-api.okta.com")
        w = _client(_User({"directories": ["read", "write", "delete"]}))
        body = {"name": "Cloud access", "roles": [
            {"role_id": "role-aws", "name": "Cloud Ops — AWS", "resource": "AWS",
             "tier": "single_approver", "persona": "cloudops", "cloud": "aws"}]}
        r = w.post(f"/api/directories/{row.id}/virtual-apps", json=body)
        assert r.status_code == 202, r.text
        app_id, job_id = r.json()["app"]["id"], r.json()["job_id"]
        db.expire_all()
        job = db.query(Job).filter(Job.id == job_id).one()
        assert job.job_type == "entitle_virtual_app"
        assert db.query(AuditLog).filter(
            AuditLog.action == "entitle_virtual_app_create").count() == 1

        listed = w.get(f"/api/directories/{row.id}/virtual-apps").json()
        assert listed["apps"][0]["id"] == app_id
        assert {t["id"] for t in listed["tiers"]} == {"auto_approve", "single_approver",
                                                      "two_approver"}

        r = w.delete(f"/api/directories/{row.id}")
        assert r.status_code == 409 and "Cloud access" in r.json()["detail"]

        reader = _client(_User({"directories": ["read"]}))
        assert reader.post(f"/api/directories/{row.id}/virtual-apps", json=body).status_code == 403
        other = _client(_User({"directories": ["read", "write"]}, username="bob"))
        assert other.get(f"/api/directories/{row.id}/virtual-apps").status_code == 404

        r = w.delete(f"/api/directories/{row.id}/virtual-apps/{app_id}")
        assert r.status_code == 409, "an app still applying cannot be removed yet"
        db.query(EntitleVirtualApp).filter(EntitleVirtualApp.id == app_id).update(
            {"status": "active"})
        db.commit()
        r = w.delete(f"/api/directories/{row.id}/virtual-apps/{app_id}")
        assert r.status_code == 202, r.text
        assert db.query(AuditLog).filter(
            AuditLog.action == "entitle_virtual_app_delete").count() == 1

        r = w.post(f"/api/directories/{row.id}/virtual-apps",
                   json={**body, "roles": [{**body["roles"][0], "role_id": "role-gh"}]})
        assert r.status_code == 400 and "GitHub" in r.json()["detail"]
    finally:
        db.close()


# ── Job ──────────────────────────────────────────────────────────────────────

def test_job_records_role_ids_and_failures():
    _setup()
    from web_dashboard.api import websocket
    from web_dashboard.services import job_service

    async def _noop(*a, **k):
        return None

    orig_bp = websocket.broadcast_progress
    websocket.broadcast_progress = _noop
    db = SessionLocal()
    orig = reg.register_virtual_app
    try:
        row = _dir(db, name="job.okta.com")
        _, roles = vapps.normalize_request("Cloud access", [
            {"role_id": "role-aws", "name": "Cloud Ops — AWS", "resource": "AWS"}])
        app, job = vapps.start_create(db, row, name="Cloud access", roles=roles,
                                      created_by="alice")
        seen = {}

        async def fake(*, name, resources, tags):
            seen.update(name=name, resources=resources, tags=tags)
            return {"integration_id": "int-new", "tf_state_json": "{}",
                    "outputs": {"role_ids": {roles[0]["key"]: "new-role"}}}

        reg.register_virtual_app = fake
        asyncio.run(vapps.run_job(db, job_id=job.id, meta={"action": "create", "app_id": app.id}))
        db.refresh(app)
        assert app.status == "active" and app.integration_id == "int-new"
        assert json.loads(app.roles_json)[0]["role_id"] == "new-role"
        assert seen["resources"][0]["roles"][0]["workflow_id"] == WF, \
            "single_approver has no own workflow set here, so it falls back"
        assert "directory:job.okta.com" in seen["tags"]
        assert vapps.active_roles(db)[0]["role_id"] == "new-role"

        async def boom(**kw):
            raise reg.EntitleRegistrationError("terraform apply failed: 400")

        reg.register_virtual_app = boom
        app2, job2 = vapps.start_create(db, row, name="Second", roles=roles, created_by="alice")
        asyncio.run(vapps.run_job(db, job_id=job2.id, meta={"action": "create", "app_id": app2.id}))
        db.refresh(app2)
        assert app2.status == "failed" and "400" in app2.error
        assert job_service.get_job(db, job2.id).status == "failed"
    finally:
        reg.register_virtual_app = orig
        websocket.broadcast_progress = orig_bp
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
    teardown_module()
    sys.exit(1 if failures else 0)
