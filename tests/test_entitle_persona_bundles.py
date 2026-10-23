"""Persona × cloud access bundles in Entitle.

What these pin:

- the catalog stays inside the systems it names: every persona key is a real persona,
  every bundle name fits Entitle's 50 characters, every duration is one the tier's
  workflow will approve, and every dashboard group is a real ``dashboard-<scope>-<level>``;
- a group named ``ent-<cloud>-<key>`` gets a suggested name, and no other group does;
- the on-prem AD bundles name only the personas' own delegated ``ent-ad-*`` groups, never
  a built-in privileged group, and refuse one at run time too;
- the AD target resolves against the AD directory's pinned integration — the only one,
  or the one chosen — and never guesses between several;
- resolution finds the virtual-app role, the cloud integration's roles and the dashboard
  groups; a partial component blocks its bundle, an ambiguous role is reported rather
  than picked, and a bundle with no cloud access is not ready;
- the API is administrator-only, applies only the bundles that are ready, and the job
  stores the bundle ids;
- every bundle carries delegation steps, and group creation takes names only from the
  catalog, runs only on an on-prem AD through its agent, accepts only an OU inside the
  domain, and queues the built-in ad-ensure-groups playbook — which never sets members
  or removes a group.

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
                                    EntitleVirtualApp, ManagedDirectory, SessionLocal,
                                    engine)
from web_dashboard.utils.clock import utcnow  # noqa: E402
from web_dashboard.services import entitle_persona_bundles as bundles  # noqa: E402
from web_dashboard.services import entitle_public_api as api  # noqa: E402
from web_dashboard.services import entitle_registration_service as reg  # noqa: E402
from web_dashboard.services import entitle_tiers  # noqa: E402
from _settings_src import settings_source

Base.metadata.create_all(bind=engine)

AWS_INT, AZ_INT, DASH_INT, AD_INT = "int-aws", "int-azure", "int-entra", "int-ad"
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
               _role("d-aws-r", "Member", "dashboard-aws-read", DASH_INT),
               _role("d-vms-w", "Member", "dashboard-vms-write", DASH_INT),
               _role("d-cm-w", "Member", "dashboard-config_mgmt-write", DASH_INT),
               _role("d-dir-r", "Member", "dashboard-directories-read", DASH_INT)],
    AD_INT: [_role("ad-itops", "Member", "CN=ent-ad-itops,OU=Groups,DC=corp,DC=example,DC=com",
                   AD_INT),
             _role("ad-da", "Member", "Domain Admins", AD_INT)],
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
    assert len(rows) == sum(len(p.targets) for p in bundles.CATALOG)
    assert len({r["key"] for r in rows}) == len(rows)
    for p in bundles.CATALOG:
        assert p.persona in VALID_PERSONAS, p.key
        assert p.tier in entitle_tiers.TIERS
        assert p.targets and set(p.targets) <= set(bundles.TARGETS), p.key
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
        if r["cloud"] == bundles.AD:
            assert r["category"] == "On-prem access"
            for g in r["cloud_roles"]:
                assert g.startswith("ent-ad-"), f"{g}: AD bundles use delegated groups only"
                assert g.casefold() not in bundles.PRIVILEGED_AD_GROUPS, g
    on_prem = {r["access"] for r in rows if r["cloud"] == bundles.AD}
    assert {"itops", "ot", "hypervisor"} <= on_prem


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
        assert any("no Azure access" in b for b in az["blockers"])

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


def _ad_dir(db, name, integration):
    db.add(ManagedDirectory(name=name, cloud="local", provider="onprem_ad",
                            source="registered", status="available", created_by="root",
                            entitle_integration_id=integration))
    db.commit()


def test_ad_target_uses_the_pinned_directory_and_never_a_builtin():
    _setup()
    db = SessionLocal()
    try:
        db.query(ManagedDirectory).delete()
        db.query(EntitleVirtualApp).delete()
        db.commit()
        row = asyncio.run(bundles.resolve(db, ["itops-ad", "itops-aws"]))
        assert [r["key"] for r in row] == ["itops-ad"], "IT has no cloud bundle"
        assert not row[0]["ready"] and "pin an AD directory" in row[0]["blockers"][-1]

        _ad_dir(db, "corp.example.com", AD_INT)
        ok = asyncio.run(bundles.resolve(db, ["itops-ad"]))[0]
        assert ok["ready"], ok["blockers"]
        assert ok["role_ids"] == ["ad-itops", "d-vms-w", "d-cm-w", "d-dir-r"]
        assert ok["components"]["cloud"]["found"][0]["resource"].startswith("CN=ent-ad-itops")

        # A virtual-app role over the same group: granted once, through the app.
        _vapp(db, [{"key": "k", "name": "IT engineer — On-prem AD", "persona": "itops",
                    "cloud": "ad", "role_id": "v-itops", "group_name": "CN=ent-ad-itops,OU=Groups,DC=corp,DC=example,DC=com"}])
        ok = asyncio.run(bundles.resolve(db, ["itops-ad"]))[0]
        assert ok["role_ids"][0] == "v-itops" and "ad-itops" not in ok["role_ids"]

        _ad_dir(db, "lab.example.com", "int-ad-2")
        db.query(EntitleVirtualApp).delete()
        db.commit()
        two = asyncio.run(bundles.resolve(db, ["itops-ad"]))[0]
        assert not two["ready"] and any("choose one" in b for b in two["blockers"])
        _setup(entitle_cloud_integration_ad=AD_INT)
        assert asyncio.run(bundles.resolve(db, ["itops-ad"]))[0]["ready"]

        assert bundles._match_group(ROLES[AD_INT], "Domain Admins")[0] is None
        assert "never bundled" in bundles._match_group(ROLES[AD_INT], "domain admins")[1]
    finally:
        db.query(ManagedDirectory).delete()
        db.query(EntitleVirtualApp).delete()
        db.commit()
        db.close()


# ── Delegation and group creation ────────────────────────────────────────────

def test_every_bundle_has_delegation_steps_with_names_filled_in():
    for r in bundles.catalog():
        assert r["delegation"], f"{r['key']} has no delegation steps"
        text = " ".join(r["delegation"])
        assert "{group}" not in text and "{roles}" not in text
        assert r["group"] in text or r["cloud"] == "ad", r["key"]


def test_the_docs_print_exactly_the_catalogs_delegation_steps():
    path = os.path.join(_ROOT, "docs", "integrations", "beyondtrust", "entitle-persona-bundles.md")
    with open(path, encoding="utf-8") as fh:
        doc = fh.read()
    for p in bundles.CATALOG:
        targets = [bundles.AD] if bundles.AD in p.targets else []
        if p.key == "cloudops":
            targets += list(bundles.CLOUDS)
        for t in targets:
            for step in bundles.delegation_steps(p, t):
                assert step in doc, f"docs lack {p.key}/{t} step: {step[:60]}"


def test_plan_groups_only_from_the_catalog():
    import re
    ad_only = bundles.plan_groups(["itops-ad", "cloudops-aws", "nope-ad", "itops-ad"],
                                  include_cloud=False)
    assert [g["name"] for g in ad_only] == ["ent-ad-itops"]
    both = bundles.plan_groups(["itops-ad", "cloudops-aws", "dba-oci"], include_cloud=True)
    assert [g["name"] for g in both] == ["ent-ad-itops", "ent-aws-cloudops", "ent-oci-dba"]
    for g in bundles.plan_groups([b["key"] for b in bundles.catalog()], include_cloud=True):
        assert re.match(bundles.GROUP_NAME_RE, g["name"]), g["name"]
        assert g["name"] not in bundles.PRIVILEGED_AD_GROUPS


def test_check_ou_accepts_only_a_dn_inside_the_domain():
    ok = "OU=Entitle,OU=Groups,DC=corp,DC=example,DC=com"
    assert bundles.check_ou(ok, "corp.example.com") == ok
    assert bundles.check_ou("", "corp.example.com") == ""
    for bad in ('OU=x",DC=corp,DC=example,DC=com', "OU=x\nDC=corp,DC=example,DC=com",
                "OU=x,DC=other,DC=com", "Entitle", "OU=a;rm,DC=corp,DC=example,DC=com"):
        try:
            bundles.check_ou(bad, "corp.example.com")
        except bundles.GroupCreationError:
            continue
        raise AssertionError(f"accepted OU {bad!r}")


def _agent(db, version="2.8.0"):
    import uuid
    from datetime import datetime
    from web_dashboard.database import RemoteAgent
    a = RemoteAgent(id=str(uuid.uuid4()), name=f"agent-{uuid.uuid4().hex[:6]}",
                    agent_version=version, is_active=True,
                    public_key="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIent",
                    last_seen_at=utcnow(), created_at=utcnow())
    db.add(a)
    db.commit()
    return a


def test_group_creation_queues_the_builtin_on_the_directorys_agent():
    _setup()
    from web_dashboard.database import Job
    db = SessionLocal()
    try:
        agent = _agent(db)
        onprem = ManagedDirectory(name="corp.example.com", cloud="local", provider="onprem_ad",
                                  source="registered", status="available", created_by="root",
                                  host="dc1.corp.example.com", agent_id=agent.id)
        managed = ManagedDirectory(name="aws.example.com", cloud="aws",
                                   provider="aws_managed_ad", source="registered",
                                   status="available", created_by="root")
        db.add_all([onprem, managed])
        db.commit()
        assert [d["id"] for d in bundles.onprem_ad_directories(db)] == [onprem.id]

        out = bundles.queue_group_creation(
            db, directory_id=onprem.id, keys=["itops-ad", "cloudops-aws"],
            include_cloud=True, ou="OU=Entitle,DC=corp,DC=example,DC=com", created_by="root")
        job = db.query(Job).filter(Job.id == out["job_id"]).one()
        meta = job.metadata_dict
        assert job.job_type == "agent_ansible" and job.agent_id == agent.id
        assert meta["asset"] == "builtin:ad-ensure-groups"
        assert meta["run_kind"] == "directory" and meta["transport"] == "winrm"
        assert meta["target_host"] == "dc1.corp.example.com" and meta["target_id"] == onprem.id
        assert [g["name"] for g in meta["extra_vars"]["ad_groups"]] == \
            ["ent-ad-itops", "ent-aws-cloudops"]
        assert meta["extra_vars"]["ad_groups_path"] == "OU=Entitle,DC=corp,DC=example,DC=com"

        for kwargs, why in (
                ({"directory_id": managed.id}, "not an on-premises"),
                ({"directory_id": onprem.id, "keys": ["cloudops-aws"]}, "None of the selected"),
                ({"directory_id": onprem.id, "ou": "OU=x,DC=evil,DC=com"}, "inside")):
            args = {"keys": ["itops-ad"], "include_cloud": False, "ou": "",
                    "created_by": "root", **kwargs}
            try:
                bundles.queue_group_creation(db, **args)
            except bundles.GroupCreationError as e:
                assert why in str(e), (why, str(e))
            else:
                raise AssertionError(f"queued: {why}")
    finally:
        db.query(ManagedDirectory).delete()
        db.commit()
        db.close()


def test_the_builtin_playbook_only_ever_creates():
    from web_dashboard.services import agent_ansible_bundle
    assert "ad-ensure-groups" in agent_ansible_bundle.BUILTIN_PLAYBOOKS
    text = agent_ansible_bundle.builtin_playbook("builtin:ad-ensure-groups")
    body = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
    assert "members:" not in body and "state: absent" not in body
    assert "state: present" in body and "no_log: true" in body
    assert bundles.GROUP_NAME_RE in body, "the agent side re-checks the same name pattern"


def test_create_groups_api_is_admin_only_and_audited():
    _setup()
    db = SessionLocal()
    try:
        agent = _agent(db)
        row = ManagedDirectory(name="corp.example.com", cloud="local", provider="onprem_ad",
                               source="registered", status="available", created_by="root",
                               host="dc1.corp.example.com", agent_id=agent.id)
        db.add(row)
        db.commit()
        body = {"directory_id": row.id, "keys": ["ot-ad"]}
        assert _client(_User(False)).post("/api/entitle/persona-bundles/create-groups",
                                          json=body).status_code == 403
        r = _client(_User(True)).post("/api/entitle/persona-bundles/create-groups", json=body)
        assert r.status_code == 202, r.text
        assert [g["name"] for g in r.json()["groups"]] == ["ent-ad-ot"]
        db.expire_all()
        assert db.query(AuditLog).filter(AuditLog.action == "entitle_groups_create").count() == 1
        over = _client(_User(True)).get("/api/entitle/persona-bundles").json()
        assert over["ad_directories"][0]["id"] == row.id
    finally:
        db.query(ManagedDirectory).delete()
        db.commit()
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
        assert len(over["catalog"]) == sum(len(p.targets) for p in bundles.CATALOG)
        assert over["clouds"]["aws"]["integration_id"] == AWS_INT
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
        _ad_dir(db, "corp-api.example.com", AD_INT)
        r = w.get("/api/entitle/persona-bundles/cloud-candidates?cloud=ad")
        assert [i["id"] for i in r.json()["integrations"]] == [AD_INT]
        r = w.put("/api/entitle/persona-bundles/clouds",
                  json={"cloud": "ad", "integration_id": AWS_INT})
        assert r.status_code == 400, "only an AD directory's integration may be the AD target"
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



def test_the_settings_cards_start_with_every_target_bound():
    """The Settings bundles section renders one card per target and every card binds
    clouds[c].integration_id and .resource. Started as `clouds: {}`, each of the five
    cards threw three times on every open, before load() had returned; so the page seeds
    one blank pin per target and merges the server's answer over it. Its target list
    has to be this module's, or a target added here would render a card that throws."""
    import re
    src = settings_source()
    m = re.search(r"const ENTITLE_TARGETS = \[([^\]]*)\];", src)
    assert m, "settings.html no longer declares ENTITLE_TARGETS"
    page = re.findall(r"'([a-z]+)'", m.group(1))
    assert page == list(bundles.TARGETS), (page, bundles.TARGETS)
    factory = src.split("function entitleBundles() {", 1)[1].split("\n}\n", 1)[0]
    assert "clouds: blankEntitleClouds()" in factory, "clouds starts empty again"
    assert "cloudKeys: ENTITLE_TARGETS" in factory
    # Both places that replace `clouds` keep every target present.
    assert factory.count("{ ...blankEntitleClouds(), ...(r.clouds || {}) }") == 2
    assert "this.clouds = r.clouds" not in factory

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
