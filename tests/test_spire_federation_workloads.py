"""SPIFFE federation reaching the workloads, and the proof (L4, part 2).

A federation relationship makes a SERVER hold the other trust domain's bundle; an entry's
``federatesWith`` is what hands it to a WORKLOAD. Pinned here:

  * ``set_federates_with`` rewrites an entry with every field the server reported and only
    federatesWith changed (``entry update`` replaces the entry); no-op when already so;
  * a cell minted on the dashboard's trust domain federates with every federated lab from
    the start, and federating / unfederating a lab updates the cells already there;
  * the lab's k8s workload entry carries the dashboard's trust domain while federated, at
    creation (spire-k8s-entry.yml) and on an existing entry (spire-federation.yml);
  * the proof runs on the linked k3s node, as the workload, NEVER logs the SVID response
    (it carries the private key), and reports trust domain names only.

Run: python tests/test_spire_federation_workloads.py   (or under pytest)
"""
import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
import types
import uuid

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="spire-fed-workloads-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDB}"
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-spire-fed-workloads")

try:
    import jinja2
    import sqlalchemy  # noqa: F401
    import yaml
except ModuleNotFoundError as exc:  # pragma: no cover -- app deps missing
    try:
        import pytest
        pytest.skip(f"app dependencies unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)

# First-party imports UNGUARDED: a broken module must fail this file, not skip it.
from web_dashboard.database import (AgentCell, Base, SessionLocal,  # noqa: E402
                                    SpiffeTrustDomain, SpireLab, engine)
from web_dashboard.services import dashboard_spire, spire_lab_service as svc  # noqa: E402

Base.metadata.create_all(bind=engine)

DASH_TD = "dash.wl.test"
LAB_TD = "lab.wl.test"
_SPIRE = os.path.join(_ROOT, "examples", "playbooks", "spire")


def _entry(fed=()):
    return {"id": "e-1",
            "spiffe_id": {"trust_domain": DASH_TD, "path": "/demo/agent-cell/c1"},
            "parent_id": {"trust_domain": DASH_TD, "path": "/demo/agent-cell/c1/node"},
            "selectors": [{"type": "unix", "value": "uid:1000"}],
            "x509_svid_ttl": 3600, "jwt_svid_ttl": 300, "federates_with": list(fed)}


class FakeSpire:
    def __init__(self, entry):
        self.entry = entry
        self.calls = []

    def __call__(self, argv, timeout, input=None):
        a = argv[argv.index(dashboard_spire.SPIRE_BIN) + 1:]
        self.calls.append(a)
        ok = lambda d: subprocess.CompletedProcess(argv, 0, json.dumps(d), "")  # noqa: E731
        if a[:2] == ["entry", "show"]:
            return ok({"entries": [self.entry] if self.entry else []})
        if a[:2] in (["entry", "update"], ["entry", "create"]):
            return ok({"results": [{"status": {"code": 0, "message": "OK"}}]})
        if a[:2] == ["token", "generate"]:
            return ok({"value": "tok"})
        raise AssertionError(a)


def _flag_values(args, flag):
    return [args[i + 1] for i, x in enumerate(args) if x == flag]


# ── the dashboard's entries ───────────────────────────────────────────────────

def test_an_update_keeps_every_field_and_changes_only_federates_with():
    f = FakeSpire(_entry())
    dashboard_spire._run = f
    assert dashboard_spire.set_federates_with("spiffe://dash.wl.test/demo/agent-cell/c1",
                                              LAB_TD) == 1
    upd = next(c for c in f.calls if c[:2] == ["entry", "update"])
    assert _flag_values(upd, "-entryID") == ["e-1"]
    assert _flag_values(upd, "-parentID") == [f"spiffe://{DASH_TD}/demo/agent-cell/c1/node"]
    assert _flag_values(upd, "-spiffeID") == [f"spiffe://{DASH_TD}/demo/agent-cell/c1"]
    assert _flag_values(upd, "-selector") == ["unix:uid:1000"]
    assert _flag_values(upd, "-x509SVIDTTL") == ["3600"]
    assert _flag_values(upd, "-jwtSVIDTTL") == ["300"]
    assert _flag_values(upd, "-federatesWith") == [f"spiffe://{LAB_TD}"]


def test_an_entry_already_so_is_left_alone_and_removal_drops_only_that_domain():
    f = FakeSpire(_entry(fed=[f"spiffe://{LAB_TD}"]))
    dashboard_spire._run = f
    assert dashboard_spire.set_federates_with("x", LAB_TD) == 0
    assert not any(c[:2] == ["entry", "update"] for c in f.calls)
    f = FakeSpire(_entry(fed=[LAB_TD, "other.test"]))
    dashboard_spire._run = f
    assert dashboard_spire.set_federates_with("x", LAB_TD, present=False) == 1
    upd = next(c for c in f.calls if c[:2] == ["entry", "update"])
    assert _flag_values(upd, "-federatesWith") == ["spiffe://other.test"]


def test_a_new_workload_can_federate_from_creation():
    f = FakeSpire(None)
    dashboard_spire._run = f
    dashboard_spire.register_workload("spiffe://d/node", "spiffe://d/w", 1000,
                                      federates_with=[LAB_TD, "b.test"])
    create = next(c for c in f.calls if c[:2] == ["entry", "create"])
    assert _flag_values(create, "-federatesWith") == [f"spiffe://{LAB_TD}", "spiffe://b.test"]


def test_a_cell_minted_on_the_dashboard_td_federates_with_every_federated_lab():
    import inspect
    from web_dashboard.api import agentcell
    src = inspect.getsource(agentcell)
    assert "federates_with=spire_lab_service.federated_lab_tds(db)" in src
    db = SessionLocal()
    try:
        for td, status in (("a.wl.test", "federated"), ("b.wl.test", None),
                           ("c.wl.test", "failed")):
            db.add(SpireLab(id=str(uuid.uuid4()), name=td, trust_domain=td, cloud="azure",
                            status="available", federation_status=status,
                            created_by="t"))
        db.commit()
        assert svc.federated_lab_tds(db) == ["a.wl.test"]
    finally:
        db.close()


# ── the lab's workload ────────────────────────────────────────────────────────

def _lab(**kw):
    base = dict(id=str(uuid.uuid4()), name="lab", trust_domain=LAB_TD, cloud="azure",
                bind_port=8081, status="available", deployment_mode="vm",
                private_ip="10.0.0.5", public_ip="203.0.113.5",
                vm_resource_id=json.dumps({"resource_group": "rg", "vm_name": "v"}),
                source_cidrs="198.51.100.7/32", admin_secret_folder="spire/lab",
                k8s_status="linked", k8s_private_ip="10.0.0.9",
                k8s_workload_spiffe_id=f"spiffe://{LAB_TD}/ns/kube-system/sa/deploy-bot",
                created_by="tester")
    base.update(kw)
    return SpireLab(**base)


def test_the_k8s_entry_federates_only_while_the_lab_is():
    saved = dashboard_spire.trust_domain
    dashboard_spire.trust_domain = lambda timeout=30: DASH_TD
    try:
        assert svc._k8s_entry_vars(_lab(federation_status="federated"))["federates_with"] == [DASH_TD]
        assert svc._k8s_entry_vars(_lab(federation_status=None))["federates_with"] == []
        assert svc._k8s_entry_vars(_lab(federation_status="failed"))["federates_with"] == []
    finally:
        dashboard_spire.trust_domain = saved
    play = yaml.safe_load(open(os.path.join(_SPIRE, "spire-k8s-entry.yml"),
                               encoding="utf-8"))[0]
    task = next(t for t in play["tasks"] if t.get("name") == "Create the workload entry")
    cmd = jinja2.Template(task["ansible.builtin.command"]).render(
        spire_cli_prefix="", spire_root="/opt/spire", _node_id="spiffe://l/n",
        _workload_id="spiffe://l/w", workload_uid=1010, jwt_svid_ttl=300,
        federates_with=[DASH_TD])
    assert f"-federatesWith spiffe://{DASH_TD}" in cmd
    assert play["vars"]["federates_with"] == []


def _env():
    env = jinja2.Environment()
    env.filters["regex_replace"] = lambda v, pat, rep: re.sub(pat, rep, v)
    env.filters["from_json"] = json.loads
    return env


def _render_update(entry, state):
    """Render spire-federation.yml's workload update exactly as Ansible would: the task's
    own vars first, then its command."""
    play = yaml.safe_load(open(os.path.join(_SPIRE, "spire-federation.yml"),
                               encoding="utf-8"))[0]
    task = next(t for t in play["tasks"] if t.get("name") == "Set the workload's federatesWith")
    env = _env()
    ctx = {"workload_entry": {"stdout": json.dumps({"entries": [entry]})},
           "dashboard_trust_domain": DASH_TD, "state": state,
           "_cli": "/opt/spire/bin/spire-server"}
    for k in ("_entry", "_have", "_want"):
        ctx[k] = _eval(env, task["vars"][k], ctx)
    return env.from_string(task["ansible.builtin.command"]["cmd"]).render(**ctx), ctx


def _eval(env, raw, ctx):
    """A `{{ expr }}` var as Ansible holds it: the expression's VALUE, not its string."""
    expr = raw.strip()
    assert expr.startswith("{{") and expr.endswith("}}"), raw
    return env.compile_expression(expr[2:-2].strip())(**ctx)


def test_the_federation_play_adds_and_removes_the_dashboard_on_the_workload():
    lab_entry = {"id": "w-1",
                 "spiffe_id": {"trust_domain": LAB_TD, "path": "/ns/kube-system/sa/deploy-bot"},
                 "parent_id": {"trust_domain": LAB_TD, "path": "/node/k3s-01"},
                 "selectors": [{"type": "unix", "value": "uid:1010"}],
                 "jwt_svid_ttl": 300, "x509_svid_ttl": 0, "federates_with": []}
    cmd, ctx = _render_update(lab_entry, "present")
    assert ctx["_want"] == [DASH_TD]
    assert "-entryID w-1" in cmd and "-selector unix:uid:1010" in cmd
    assert f"-parentID spiffe://{LAB_TD}/node/k3s-01" in cmd
    assert "-jwtSVIDTTL 300" in cmd and "-x509SVIDTTL" not in cmd
    assert f"-federatesWith spiffe://{DASH_TD}" in cmd
    lab_entry["federates_with"] = [f"spiffe://{DASH_TD}"]
    cmd, ctx = _render_update(lab_entry, "absent")
    assert ctx["_want"] == [] and "-federatesWith" not in cmd


def test_the_federation_stage_names_the_linked_workload_only():
    saved = (dashboard_spire.trust_domain, dashboard_spire.live_bundle,
             dashboard_spire.server_address)
    dashboard_spire.trust_domain = lambda timeout=30: DASH_TD
    dashboard_spire.live_bundle = lambda: {"keys": []}
    dashboard_spire.server_address = lambda: "agents.wl.test"
    try:
        assert svc._federation_vars(_lab())["workload_spiffe_id"].endswith("/sa/deploy-bot")
        assert svc._federation_vars(_lab(k8s_status=None))["workload_spiffe_id"] == ""
    finally:
        (dashboard_spire.trust_domain, dashboard_spire.live_bundle,
         dashboard_spire.server_address) = saved


# ── the proof ─────────────────────────────────────────────────────────────────

def test_the_proof_never_logs_the_svid_response_and_reports_names_only():
    play = yaml.safe_load(open(os.path.join(_SPIRE, "spire-federation-proof.yml"),
                               encoding="utf-8"))[0]
    tasks = {t["name"]: t for t in play["tasks"]}
    fetch = tasks["Fetch the workload's X.509 SVID response, as the workload"]
    assert fetch.get("no_log") is True, "the response carries the SVID's private key"
    assert fetch["become_user"] == "{{ workload_user }}", "the uid IS the attestation"
    assert "federated_bundles" in fetch["until"]
    assert tasks["Keep only the trust domain names"].get("no_log") is True
    for name, t in tasks.items():
        if t.get("no_log"):
            continue
        assert "fetch.stdout" not in yaml.safe_dump(t), f"{name} would print the response"
    assert svc.FEDERATION_PROOF_STAGE["host"] == "k8s"


def test_the_proof_parses_a_real_shaped_response():
    response = {"svids": [{"spiffe_id": "spiffe://lab/w", "x509_svid_key": "SECRET"}],
                "federated_bundles": {f"spiffe://{DASH_TD}": "bundle-bytes"}}
    play = yaml.safe_load(open(os.path.join(_SPIRE, "spire-federation-proof.yml"),
                               encoding="utf-8"))[0]
    fetch = next(t for t in play["tasks"] if t["name"].startswith("Fetch"))
    ok = _env().compile_expression(fetch["until"])(
        fetch={"rc": 0, "stdout": json.dumps(response)}, federated_trust_domain=DASH_TD)
    assert ok is True
    ok = _env().compile_expression(fetch["until"])(
        fetch={"rc": 0, "stdout": json.dumps({"svids": []})}, federated_trust_domain=DASH_TD)
    assert ok is False


# ── the run ───────────────────────────────────────────────────────────────────

def _save(row, cells=()):
    db = SessionLocal()
    db.add(row)
    db.add(SpiffeTrustDomain(trust_domain=row.trust_domain,
                             bundle_json=json.dumps({"keys": [{"use": "jwt-svid"}]}),
                             spire_lab_id=row.id, created_by="t"))
    for sid, status in cells:
        db.add(AgentCell(id=str(uuid.uuid4()), name="c", status=status,
                         trust_domain=DASH_TD, spiffe_id=sid, spire_lab_id=None))
    db.commit()
    db.refresh(row)
    return db, row


def _run(db, row, job_id, action="federate", proof="completed"):
    calls = []

    async def fake_stage(db_, *, row, stage, actor, asset_backend, parent_job_id):
        calls.append(("stage", stage["key"]))
        return proof if stage["key"] == "federation_proof" else "completed"

    class Backend:
        acl_label = "NSG"

        @staticmethod
        async def apply_ingress(placement, ports, cidrs):
            return {"opened": True}

    ws = types.ModuleType("web_dashboard.api.websocket")

    async def broadcast_progress(*a, **kw):
        pass
    ws.broadcast_progress = broadcast_progress
    saved = (svc._run_stage, svc.require_backend, svc._cfg, dashboard_spire.federate,
             dashboard_spire.unfederate, dashboard_spire.trust_domain,
             dashboard_spire.set_federates_with,
             sys.modules.get("web_dashboard.api.websocket"))
    sys.modules["web_dashboard.api.websocket"] = ws
    svc._run_stage = fake_stage
    svc.require_backend = lambda cloud: Backend
    svc._cfg = lambda key, default="": default
    dashboard_spire.trust_domain = lambda timeout=30: DASH_TD
    dashboard_spire.federate = lambda *a: {"action": "create"}
    dashboard_spire.unfederate = lambda td: ["relationship"]
    dashboard_spire.set_federates_with = lambda sid, td, present=True: \
        calls.append(("cell", sid, td, present)) or 1
    try:
        asyncio.run(svc.run_federation(db, lab_id=row.id, job_id=job_id, action=action))
    finally:
        (svc._run_stage, svc.require_backend, svc._cfg, dashboard_spire.federate,
         dashboard_spire.unfederate, dashboard_spire.trust_domain,
         dashboard_spire.set_federates_with, prev) = saved
        if prev is not None:
            sys.modules["web_dashboard.api.websocket"] = prev
    db.expire_all()
    return calls


def _dash_on():
    from web_dashboard.services import agent_service, config_service
    config_service.set("spire_attest_enabled", "1")
    config_service.set(agent_service.AUDIENCE_CONFIG, "https://agents.wl.test")


def test_federating_updates_live_dashboard_cells_and_proves_on_the_node():
    _dash_on()
    db, row = _save(_lab(trust_domain=f"{uuid.uuid4().hex[:6]}.wl.test"),
                    cells=(("spiffe://dash.wl.test/demo/agent-cell/live", "active"),
                           ("spiffe://dash.wl.test/demo/agent-cell/gone", "revoked")))
    try:
        job = svc.start_federation(db, lab_id=row.id, created_by="t")
        calls = _run(db, row, job["job_id"])
        cells = [c for c in calls if c[0] == "cell"]
        assert cells == [("cell", "spiffe://dash.wl.test/demo/agent-cell/live",
                          row.trust_domain, True)], "revoked cells are left alone"
        assert calls[-1] == ("stage", "federation_proof")
        row = db.query(SpireLab).filter(SpireLab.id == row.id).one()
        assert row.federation_proof and f"spiffe://{DASH_TD}" in row.federation_proof
        assert "holds the bundle" in row.federation_proof
    finally:
        db.close()


def test_a_failed_proof_is_recorded_but_the_servers_stay_federated():
    _dash_on()
    db, row = _save(_lab(trust_domain=f"{uuid.uuid4().hex[:6]}.wl.test"))
    try:
        job = svc.start_federation(db, lab_id=row.id, created_by="t")
        _run(db, row, job["job_id"], proof="failed")
        row = db.query(SpireLab).filter(SpireLab.id == row.id).one()
        assert row.federation_status == "federated"
        assert "does not carry" in row.federation_proof
    finally:
        db.close()


def test_no_linked_node_means_no_proof_run():
    _dash_on()
    db, row = _save(_lab(trust_domain=f"{uuid.uuid4().hex[:6]}.wl.test", k8s_status=None))
    try:
        job = svc.start_federation(db, lab_id=row.id, created_by="t")
        calls = _run(db, row, job["job_id"])
        assert ("stage", "federation_proof") not in calls
        assert db.query(SpireLab).filter(SpireLab.id == row.id).one().federation_proof is None
    finally:
        db.close()


def test_unfederating_takes_the_lab_out_of_the_cells():
    _dash_on()
    db, row = _save(_lab(trust_domain=f"{uuid.uuid4().hex[:6]}.wl.test",
                         federation_status="federated", federation_proof="x"),
                    cells=(("spiffe://dash.wl.test/demo/agent-cell/u", "active"),))
    try:
        job = svc.start_federation(db, lab_id=row.id, created_by="t", enable=False)
        calls = _run(db, row, job["job_id"], action="unfederate")
        assert ("cell", "spiffe://dash.wl.test/demo/agent-cell/u", row.trust_domain,
                False) in calls
        row = db.query(SpireLab).filter(SpireLab.id == row.id).one()
        assert row.federation_status is None and row.federation_proof is None
    finally:
        db.close()


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
