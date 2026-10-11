"""Persona × cloud access bundles in Entitle.

What these pin:

- the catalog stays inside the systems it names: every persona key is a real persona,
  every bundle name fits Entitle's 50 characters, every duration is one the tier's
  workflow will approve, and every dashboard group is a real ``dashboard-<scope>-<level>``;
- a group named ``ent-<cloud>-<key>`` gets a suggested name, and no other group does;
- resolution finds the virtual-app role, the cloud integration's roles and the dashboard
  groups; a partial component blocks its bundle, an ambiguous role is reported rather
  than picked, and a bundle with no cloud access is not ready;
- the API is administrator-only, applies only the bundles that are ready, and the job
  stores the bundle ids.

Real temp SQLite; Entitle stubbed with httpx.MockTransport; Terraform never runs.

Run: python tests/test_entitle_persona_bundles.py   (or under pytest)
"""
import asyncio
import json
import os
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="entitle-bundles-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDB}"
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-entitle-bundle-tests")

import httpx  # noqa: E402

from web_dashboard.database import (AuditLog, Base, EntitleBundleSet,  # noqa: E402
                                    EntitleVirtualApp, SessionLocal, engine)
from web_dashboard.services import entitle_persona_bundles as bundles  # noqa: E402
from web_dashboard.services import entitle_public_api as api  # noqa: E402
from web_dashboard.services import entitle_registration_service as reg  # noqa: E402
from web_dashboard.services import entitle_tiers  # noqa: E402

Base.metadata.create_all(bind=engine)

AWS_INT, AZ_INT, DASH_INT = "int-aws", "int-azure", "int-entra"
CFG = {"entitle_api_url": "https://api.us.entitle.io/v1", "entitle_api_token": "tok",
       "entitle_workflow_id": "wf-default",
       "entitle_cloud_integration_aws": AWS_INT,
       "entitle_cloud_integration_azure": AZ_INT,
       "entitle_dashboard_groups_integration_id": DASH_INT}


def _role(rid, name, res, integ, ext=""):
    return {"id": rid, "name": name, "externalId": ext,
            "resource": {"id": f"res-{res}", "name": res, "integration": {"id": integ}}}


ROLES = {
    AWS_INT: [_role("aws-ec2", "AmazonEC2FullAccess", "prod-account", AWS_INT),
              _role("aws-ro", "ReadOnlyAccess", "prod-account", AWS_INT),
              _role("aws-sec", "SecurityAudit", "prod-account", AWS_INT)],
    AZ_INT: [_role("az-vm-1", "Virtual Machine Contributor", "sub-a", AZ_INT),
             _role("az-vm-2", "Virtual Machine Contributor", "sub-b", AZ_INT),
             _role("az-r-1", "Reader", "sub-a", AZ_INT),
             _role("az-r-2", "Reader", "sub-b", AZ_INT)],
    DASH_INT: [_role("d-aws-w", "Member", "dashboard-aws-write", DASH_INT),
               _role("d-img-w", "Member", "dashboard-images-write", DASH_INT),
               _role("d-aws-r", "Member", "dashboard-aws-read", DASH_INT)],
}


def _entitle(req):
    if req.url.path == "/public/v1/roles":
        return httpx.Response(200, json={"result": ROLES.get(req.url.params.get("integrationId"), [])})
    if req.url.path == "/public/v1/integrations":
        return httpx.Response(200, json={"result": [{"id": AWS_INT, "name": "AWS prod",
                                                     "application": {"name": "aws"}}]})
    return httpx.Response(404)


_ORIG = (bundles._cfg, api._default_cfg, api.transport, entitle_tiers._default_cfg)


def teardown_module(_module=None):
    bundles._cfg, api._default_cfg, api.transport, entitle_tiers._default_cfg = _ORIG


def _setup(**over):
    vals = {**CFG, **over}
    cfg = lambda k: vals.get(k, "")  # noqa: E731
    bundles._cfg = api._default_cfg = entitle_tiers._default_cfg = cfg
    api.transport = httpx.MockTransport(_entitle)


def _vapp(db, roles):
    db.query(EntitleVirtualApp).delete()
    db.add(EntitleVirtualApp(directory_id="d1", name="Cloud access", status="active",
                             roles_json=json.dumps(roles)))
    db.commit()


# ── Catalog ──────────────────────────────────────────────────────────────────

def test_catalog_stays_inside_the_systems_it_names():
    from web_dashboard.api.auth import PERMISSION_SCOPE_LEVELS
    from web_dashboard.services.personas import VALID_PERSONAS
    rows = bundles.catalog()
    assert len(rows) == len(bundles.CATALOG) * len(bundles.CLOUDS)
    assert len({r["key"] for r in rows}) == len(rows)
    for p in bundles.CATALOG:
        assert p.persona in VALID_PERSONAS, p.key
        assert p.tier in entitle_tiers.TIERS
        assert p.durations(), f"{p.key} offers no duration"
        assert set(p.durations()) <= set(entitle_tiers.TIER_DURATIONS[p.tier]), \
            f"{p.key} offers a duration its tier's workflow rule would not match"
    for r in rows:
        assert 2 <= len(r["name"]) <= 50, r["name"]
        for g in r["dashboard_groups"]:
            scope, level = g[len("dashboard-"):].rsplit("-", 1)
            assert level in PERMISSION_SCOPE_LEVELS.get(scope, ()), f"{g} grants nothing"
        if r["cloud"] == "oci":
            assert not r["cloud_roles"] and r["oci_policy"]


def test_group_names_suggest_only_on_the_convention():
    s = bundles.suggest_for_group("ent-azure-dba")
    assert s == {"name": "DBA — Azure", "resource": "Azure", "tier": "single_approver",
                 "persona": "dba", "cloud": "azure"}
    assert bundles.suggest_for_group("ENT-gcp-sre-breakglass")["tier"] == "two_approver"
    for g in ("ent-aws-unknown", "aws-cloudops", "ent-ibm-cloudops", ""):
        assert bundles.suggest_for_group(g) is None, g


def test_durations_up_to_refuses_a_duration_entitle_lacks():
    assert entitle_tiers.durations_up_to(3600) == (1800, 3600)
    try:
        entitle_tiers.durations_up_to(28800)
    except ValueError:
        pass
    else:
        raise AssertionError("8h is not an Entitle duration and must not pass")


# ── Resolution ───────────────────────────────────────────────────────────────

def test_resolution_ready_partial_ambiguous_and_no_access():
    _setup()
    db = SessionLocal()
    try:
        _vapp(db, [{"key": "k1", "name": "Cloud Ops — AWS", "persona": "cloudops",
                    "cloud": "aws", "role_id": "v-aws-cloudops"}])
        rows = {r["key"]: r for r in asyncio.run(bundles.resolve(
            db, ["cloudops-aws", "cloudops-azure", "security-aws", "dba-gcp", "nope-aws"]))}
        assert "nope-aws" not in rows

        ok = rows["cloudops-aws"]
        assert ok["ready"], ok["blockers"]
        assert ok["role_ids"] == ["v-aws-cloudops", "aws-ec2", "aws-ro", "d-aws-w", "d-img-w"]
        assert ok["durations"] == [3600, 10800, 43200]
        assert ok["workflow_id"] == "wf-default" and ok["workflow_fallback"]

        az = rows["cloudops-azure"]
        assert not az["ready"]
        assert az["components"]["cloud"]["state"] == "absent", "ambiguous parts are not found"
        assert any("2 resources" in m["reason"] for m in az["components"]["cloud"]["missing"])
        assert any("no cloud access" in b for b in az["blockers"])

        sec = rows["security-aws"]
        assert sec["components"]["dashboard"]["state"] == "partial"
        assert not sec["ready"] and "dashboard is partial" in sec["blockers"]

        assert not rows["dba-gcp"]["ready"], "no GCP integration and no group role"
    finally:
        db.close()


def test_resource_setting_disambiguates_and_two_tags_are_refused():
    _setup(entitle_cloud_resource_azure="sub-b")
    db = SessionLocal()
    try:
        _vapp(db, [{"key": "a", "name": "x", "persona": "cloudops", "cloud": "azure", "role_id": "v1"},
                   {"key": "b", "name": "y", "persona": "cloudops", "cloud": "azure", "role_id": "v2"}])
        row = asyncio.run(bundles.resolve(db, ["cloudops-azure"]))[0]
        assert [f["role_id"] for f in row["components"]["cloud"]["found"]] == ["az-vm-2", "az-r-2"]
        assert row["components"]["group"]["state"] == "absent"
        assert "2 virtual-app roles" in row["components"]["group"]["missing"][0]["reason"]
        assert row["components"]["dashboard"]["state"] == "partial"
    finally:
        db.close()


# ── API and job ──────────────────────────────────────────────────────────────

class _User:
    def __init__(self, admin):
        self.username = "root" if admin else "alice"
        self.is_admin = self.is_effective_admin = admin
        self.accessor_env_id = None
        self.effective_permissions_dict = {}
        self.is_active = True
        self.workgroups = "[]"


def _client(user):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from web_dashboard.api import entitle_bundles
    from web_dashboard.api.auth import get_current_user
    app = FastAPI()
    app.include_router(entitle_bundles.router)
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app)


def test_api_is_admin_only_and_applies_only_ready_bundles():
    _setup()
    db = SessionLocal()
    try:
        _vapp(db, [{"key": "k1", "name": "Cloud Ops — AWS", "persona": "cloudops",
                    "cloud": "aws", "role_id": "v-aws-cloudops"}])
        assert _client(_User(False)).get("/api/entitle/persona-bundles").status_code == 403
        w = _client(_User(True))
        over = w.get("/api/entitle/persona-bundles").json()
        assert len(over["catalog"]) == 36 and over["clouds"]["aws"]["integration_id"] == AWS_INT
        r = w.post("/api/entitle/persona-bundles/apply", json={"keys": ["cloudops-azure"]})
        assert r.status_code == 400, "nothing ready, nothing applied"
        r = w.post("/api/entitle/persona-bundles/apply",
                   json={"keys": ["cloudops-aws", "cloudops-azure"]})
        assert r.status_code == 202, r.text
        assert [s["key"] for s in r.json()["skipped"]] == ["cloudops-azure"]
        set_id = r.json()["set"]["id"]
        db.expire_all()
        bset = db.query(EntitleBundleSet).filter(EntitleBundleSet.id == set_id).one()
        assert [b["key"] for b in json.loads(bset.bundles_json)] == ["cloudops-aws"]
        assert db.query(AuditLog).filter(AuditLog.action == "entitle_bundles_apply").count() == 1
        r = w.post("/api/entitle/persona-bundles/preview", json={"keys": []})
        assert r.status_code == 400
        r = w.get("/api/entitle/persona-bundles/cloud-candidates?cloud=aws")
        assert r.json()["integrations"][0]["suggested"] is True
    finally:
        db.close()


def test_job_stores_bundle_ids():
    _setup()
    from web_dashboard.api import websocket

    async def _noop(*a, **k):
        return None

    orig_bp, orig_reg = websocket.broadcast_progress, reg.register_bundles
    websocket.broadcast_progress = _noop
    db = SessionLocal()
    try:
        _vapp(db, [{"key": "k1", "name": "Cloud Ops — AWS", "persona": "cloudops",
                    "cloud": "aws", "role_id": "v-aws-cloudops"}])
        rows = asyncio.run(bundles.resolve(db, ["cloudops-aws"]))
        bset, job = bundles.start_apply(db, rows, created_by="root")
        sent = {}

        async def fake(*, bundles):
            sent["bundles"] = bundles
            return {"tf_state_json": "{}", "outputs": {"bundle_ids": {"cloudops_aws": "b-1"}}}

        reg.register_bundles = fake
        asyncio.run(bundles.run_job(db, job_id=job.id, meta={"action": "apply", "set_id": bset.id}))
        db.refresh(bset)
        assert bset.status == "active"
        assert json.loads(bset.bundles_json)[0]["bundle_id"] == "b-1"
        b = sent["bundles"][0]
        assert b["category"] == bundles.CATEGORY and "tier:single_approver" in b["tags"]
        assert b["allowed_durations"] == [3600, 10800, 43200]
    finally:
        websocket.broadcast_progress, reg.register_bundles = orig_bp, orig_reg
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
