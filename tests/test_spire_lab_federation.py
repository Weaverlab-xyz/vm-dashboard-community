"""SPIFFE federation between a Workload Lab and the dashboard's own SPIRE server (L4, PR A).

docs/design/dashboard-workload-identity.md, L4. Each SPIRE server serves its trust bundle
on tcp/8082 (https_spiffe) and holds a relationship naming the other's. No SPIRE runs
here: the dashboard's server is a fake behind ``dashboard_spire._run``, and the lab's half
is a playbook rendered or stubbed. Pinned:

  * ``federate`` creates, then updates; seeds over stdin with ``docker exec -i``; refreshes
    once, so a relationship that cannot fetch is an error naming tcp/8082; refuses an
    endpoint ID that is not the trust domain's own SPIRE server, an http URL, no seed;
  * ``unfederate`` removes the relationship AND the bundle, tolerating either missing;
  * every argument comes from the lab row and the dashboard's server, never a request;
  * the refusals: k8s mode, no captured bundle, the dashboard's SPIRE off, no address,
    not available;
  * the run: the lab's half (install re-applied, ACL with 8082 kept beside 8081/8443 and a
    linked node, host firewall, relationship) BEFORE the dashboard's; teardown unfederates;
  * both servers publish the bundle endpoint, directly and not through Caddy.

Run: python tests/test_spire_lab_federation.py   (or under pytest)
"""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import types
import uuid

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="spire-federation-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDB}"
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-spire-federation")

try:
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
from web_dashboard.database import (Base, Job, SessionLocal,  # noqa: E402
                                    SpiffeTrustDomain, SpireLab, engine)
from web_dashboard.services import (config_service, dashboard_spire,  # noqa: E402
                                    spire_lab_service as svc)

Base.metadata.create_all(bind=engine)

LAB_TD = "lab.fed.test"
DASH_TD = "dash.fed.test"
BUNDLE = json.dumps({"keys": [{"use": "x509-svid", "kty": "EC"},
                              {"use": "jwt-svid", "kty": "EC", "kid": "k1"}]})


# ── the dashboard's SPIRE server, faked ───────────────────────────────────────

class FakeSpire:
    def __init__(self):
        self.calls = []            # (args, input, argv)
        self.relationships = set()
        self.bundles = set()
        self.refresh_fails = False

    def __call__(self, argv, timeout, input=None):
        i = argv.index(dashboard_spire.SPIRE_BIN)
        a = argv[i + 1:]
        self.calls.append((a, input, argv))
        ok = lambda out="": subprocess.CompletedProcess(argv, 0, out, "")  # noqa: E731
        err = lambda msg: subprocess.CompletedProcess(argv, 1, "", msg)  # noqa: E731
        if a[:2] == ["federation", "show"]:
            td = a[a.index("-trustDomain") + 1]
            return ok("{}") if td in self.relationships else err("not found")
        if a[:2] in (["federation", "create"], ["federation", "update"]):
            self.relationships.add(a[a.index("-trustDomain") + 1])
            return ok(json.dumps({"results": [{"status": {"code": 0, "message": "OK"}}]}))
        if a[:2] == ["federation", "refresh"]:
            return err("connection refused") if self.refresh_fails else ok()
        if a[:2] == ["federation", "delete"]:
            td = a[a.index("-id") + 1]
            if td not in self.relationships:
                return err("not found")
            self.relationships.discard(td)
            return ok()
        if a[:2] == ["bundle", "delete"]:
            if a[a.index("-id") + 1] not in self.bundles:
                return err("not found")
            self.bundles.discard(a[a.index("-id") + 1])
            return ok()
        raise AssertionError(f"unexpected spire-server call {a}")


def _fake():
    f = FakeSpire()
    dashboard_spire._run = f
    return f


# ── federate / unfederate ─────────────────────────────────────────────────────

def test_federate_creates_then_updates_seeding_over_stdin():
    f = _fake()
    url = "https://203.0.113.9:8082"
    out = dashboard_spire.federate(LAB_TD, url, f"spiffe://{LAB_TD}/spire/server", BUNDLE)
    assert out["action"] == "create"
    create = next(c for c in f.calls if c[0][:2] == ["federation", "create"])
    args, stdin, argv = create
    assert stdin == BUNDLE, "the seed bundle goes in on stdin"
    assert argv[:3] == ["docker", "exec", "-i"], "stdin needs docker exec -i"
    assert args[args.index("-trustDomainBundlePath") + 1] == "/dev/stdin"
    assert args[args.index("-bundleEndpointProfile") + 1] == "https_spiffe"
    assert args[args.index("-bundleEndpointURL") + 1] == url
    assert any(c[0][:2] == ["federation", "refresh"] for c in f.calls)
    assert dashboard_spire.federate(LAB_TD, url, f"spiffe://{LAB_TD}/spire/server",
                                    BUNDLE)["action"] == "update"


def test_a_relationship_that_cannot_fetch_is_an_error_naming_the_port():
    f = _fake()
    f.refresh_fails = True
    try:
        dashboard_spire.federate(LAB_TD, "https://203.0.113.9:8082",
                                 f"spiffe://{LAB_TD}/spire/server", BUNDLE)
        raise AssertionError("accepted a relationship that cannot fetch")
    except dashboard_spire.DashboardSpireError as exc:
        assert "tcp/8082" in str(exc)


def test_federate_refuses_what_it_should_never_be_given():
    _fake()
    good = ("https://203.0.113.9:8082", f"spiffe://{LAB_TD}/spire/server", BUNDLE)
    for args, why in (
            ((LAB_TD, "http://203.0.113.9:8082", good[1], BUNDLE), "https"),
            ((LAB_TD, good[0], f"spiffe://{LAB_TD}/workload", BUNDLE), "spire/server"),
            ((LAB_TD, good[0], "spiffe://other.test/spire/server", BUNDLE), "spire/server"),
            ((LAB_TD, good[0], good[1], ""), "Refresh keys"),
            (("spiffe://x/y", good[0], good[1], BUNDLE), "bare")):
        try:
            dashboard_spire.federate(*args)
            raise AssertionError(f"accepted {args}")
        except dashboard_spire.DashboardSpireError as exc:
            assert why in str(exc), (why, str(exc))


def test_unfederate_removes_the_relationship_and_the_bundle_and_tolerates_neither():
    f = _fake()
    f.relationships.add(LAB_TD)
    f.bundles.add(f"spiffe://{LAB_TD}")
    assert dashboard_spire.unfederate(LAB_TD) == ["relationship", "bundle"]
    assert not f.relationships and not f.bundles
    assert dashboard_spire.unfederate(LAB_TD) == []


def test_a_plain_call_gets_no_stdin_flag():
    f = _fake()
    f.relationships.add(LAB_TD)
    dashboard_spire.unfederate(LAB_TD)
    assert all(c[2][:3] != ["docker", "exec", "-i"] for c in f.calls)


# ── the lab side ──────────────────────────────────────────────────────────────

def _lab(**kw) -> SpireLab:
    base = dict(id=str(uuid.uuid4()), name="lab", trust_domain=f"{uuid.uuid4().hex[:6]}.fed.test",
                cloud="azure", bind_port=8081, status="available", vm_name="spire-01",
                private_ip="10.0.0.5", public_ip="203.0.113.5", deployment_mode="vm",
                vm_resource_id=json.dumps({"resource_group": "rg", "vm_name": "spire-01"}),
                source_cidrs="198.51.100.7/32", admin_secret_folder="spire/lab",
                stages_done="install,ports,seed,identity,oidc", created_by="tester")
    base.update(kw)
    return SpireLab(**base)


def _save(row, bundle=True):
    db = SessionLocal()
    db.add(row)
    if bundle:
        db.add(SpiffeTrustDomain(trust_domain=row.trust_domain, bundle_json=BUNDLE,
                                 spire_lab_id=row.id, created_by="tester"))
    db.commit()
    db.refresh(row)
    return db, row


def _dashboard_on(on=True):
    config_service.set("spire_attest_enabled", "1" if on else "")
    config_service.set("agent_audience", "https://agents.fed.test" if on else "")
    from web_dashboard.services import agent_service
    config_service.set(agent_service.AUDIENCE_CONFIG, "https://agents.fed.test" if on else "")


def test_the_refusals_each_name_their_remedy():
    _dashboard_on()
    cases = (
        (dict(deployment_mode="k8s"), True, "vm and docker"),
        (dict(status="building"), True, "not available"),
        (dict(public_ip="", private_ip=""), True, "no address"),
        ({}, False, "Refresh keys"),
    )
    for kw, bundle, why in cases:
        db, row = _save(_lab(**kw), bundle=bundle)
        try:
            assert why in svc.federation_problem(db, row), (kw, svc.federation_problem(db, row))
        finally:
            db.close()
    db, row = _save(_lab())
    try:
        assert svc.federation_problem(db, row) == ""
        _dashboard_on(False)
        assert "not in use" in svc.federation_problem(db, row)
    finally:
        _dashboard_on()
        db.close()


def test_the_endpoint_urls_come_from_the_row_and_the_dashboards_own_address():
    _dashboard_on()
    row = _lab()
    assert svc.lab_federation_url(row) == "https://203.0.113.5:8082"
    assert dashboard_spire.federation_url(dashboard_spire.server_address()) == \
        "https://agents.fed.test:8082"
    assert svc.FEDERATION_PORT == dashboard_spire.FEDERATION_PORT == 8082


def test_one_acl_port_list_carries_all_three():
    assert svc._acl_ports(_lab()) == [8081, 8443, 8082]


def test_the_routes_take_no_body_and_need_write():
    import inspect
    from web_dashboard.api import spire_lab as api
    for fn in (api.federate_lab, api.unfederate_lab):
        params = set(inspect.signature(fn).parameters)
        assert params == {"lab_id", "db", "user"}, f"{fn.__name__} must take no request body"
        assert 'require_permission("cloud_function", "write")' in inspect.getsource(fn)


# ── the run ───────────────────────────────────────────────────────────────────

def _run(db, row, job_id, action="federate", *, fail_stage=None, sources=None):
    calls = []

    async def fake_stage(db_, *, row, stage, actor, asset_backend, parent_job_id):
        calls.append(("stage", stage["key"], stage["asset"]))
        return "failed" if stage["key"] == fail_stage else "completed"

    class Backend:
        acl_label = "NSG"

        @staticmethod
        async def apply_ingress(placement, ports, cidrs):
            calls.append(("acl", tuple(ports), tuple(cidrs)))
            return {"opened": bool(cidrs)}

    ws = types.ModuleType("web_dashboard.api.websocket")

    async def broadcast_progress(*a, **kw):
        pass
    ws.broadcast_progress = broadcast_progress
    saved = (svc._run_stage, svc.require_backend, dashboard_spire.federate,
             dashboard_spire.unfederate, dashboard_spire.trust_domain, svc._cfg,
             sys.modules.get("web_dashboard.api.websocket"))
    sys.modules["web_dashboard.api.websocket"] = ws
    svc._run_stage = fake_stage
    svc.require_backend = lambda cloud: Backend
    svc._cfg = lambda key, default="": {"spire_lab_asset_backend": "local"}.get(key, default)
    dashboard_spire.trust_domain = lambda timeout=30: DASH_TD
    dashboard_spire.federate = lambda *a: calls.append(("dashboard", a)) or {"action": "create"}
    dashboard_spire.unfederate = lambda td: calls.append(("unfederate", td)) or ["relationship"]
    try:
        asyncio.run(svc.run_federation(db, lab_id=row.id, job_id=job_id, action=action))
    finally:
        (svc._run_stage, svc.require_backend, dashboard_spire.federate,
         dashboard_spire.unfederate, dashboard_spire.trust_domain, svc._cfg, prev) = saved
        if prev is not None:
            sys.modules["web_dashboard.api.websocket"] = prev
    db.expire_all()
    return calls


def test_the_labs_half_runs_first_and_the_acl_keeps_every_port_and_source():
    _dashboard_on()
    db, row = _save(_lab(k8s_status="linked", k8s_private_ip="10.0.0.9"))
    try:
        job = svc.start_federation(db, lab_id=row.id, created_by="tester")
        calls = _run(db, row, job["job_id"])
        order = [c[1] if c[0] == "stage" else c[0] for c in calls]
        assert order == ["acl", "install", "ports", "federation", "dashboard",
                         "federation_proof"], order
        acl = calls[0]
        assert acl[1] == (8081, 8443, 8082)
        assert acl[2] == ("198.51.100.7/32", "10.0.0.9/32"), (
            "re-applying the converged rule must keep the operator's sources AND a linked node")
        dash = next(c for c in calls if c[0] == "dashboard")[1]
        assert dash == (row.trust_domain, "https://203.0.113.5:8082",
                        f"spiffe://{row.trust_domain}/spire/server", BUNDLE)
        row = db.query(SpireLab).filter(SpireLab.id == row.id).one()
        assert row.federation_status == "federated" and row.federated_at
        assert db.query(Job).filter(Job.id == job["job_id"]).one().status == "completed"
    finally:
        db.close()


def test_a_failed_lab_stage_never_reaches_the_dashboard():
    _dashboard_on()
    db, row = _save(_lab())
    try:
        job = svc.start_federation(db, lab_id=row.id, created_by="tester")
        calls = _run(db, row, job["job_id"], fail_stage="federation")
        assert not any(c[0] == "dashboard" for c in calls)
        row = db.query(SpireLab).filter(SpireLab.id == row.id).one()
        assert row.federation_status == "failed" and "spire-federation.yml" in row.federation_error
    finally:
        db.close()


def test_the_same_trust_domain_on_both_sides_is_refused():
    _dashboard_on()
    db, row = _save(_lab(trust_domain=DASH_TD))
    try:
        job = svc.start_federation(db, lab_id=row.id, created_by="tester")
        calls = _run(db, row, job["job_id"])
        assert calls == []
        row = db.query(SpireLab).filter(SpireLab.id == row.id).one()
        assert "two different trust domains" in row.federation_error
    finally:
        db.close()


def test_unfederating_removes_the_dashboards_half_then_the_labs():
    _dashboard_on()
    db, row = _save(_lab(federation_status="federated"))
    try:
        job = svc.start_federation(db, lab_id=row.id, created_by="tester", enable=False)
        calls = _run(db, row, job["job_id"], action="unfederate")
        assert calls[0] == ("unfederate", row.trust_domain)
        assert ("stage", "unfederation", "spire-federation.yml") in calls
        row = db.query(SpireLab).filter(SpireLab.id == row.id).one()
        assert row.federation_status is None
    finally:
        db.close()


def test_teardown_unfederates():
    import inspect
    src = inspect.getsource(svc.run_decommission)
    assert "dashboard_spire.unfederate(row.trust_domain)" in src


# ── the play and the config ───────────────────────────────────────────────────

def _play():
    path = os.path.join(_ROOT, "examples", "playbooks", "spire", "spire-federation.yml")
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh.read())[0]


def test_the_play_seeds_on_stdin_through_an_interactive_exec():
    import re
    import jinja2
    env = jinja2.Environment()
    env.filters["regex_replace"] = lambda v, pat, rep: re.sub(pat, rep, v)  # Ansible's
    play = _play()
    tmpl = env.from_string(play["vars"]["_cli"])
    cli = tmpl.render(spire_cli_prefix="docker exec spire-server ", spire_root="/opt/spire")
    assert cli == "docker exec -i spire-server /opt/spire/bin/spire-server"
    vm = tmpl.render(spire_cli_prefix="", spire_root="/opt/spire")
    assert vm == "/opt/spire/bin/spire-server"
    src = yaml.safe_dump(play)
    assert "-trustDomainBundlePath /dev/stdin" in src and "stdin:" in src
    assert "federation refresh" in src, "the lab proves its fetch before reporting success"
    assert play["vars"]["_endpoint_id"] == "spiffe://{{ dashboard_trust_domain }}/spire/server"


def test_the_stage_vars_carry_the_dashboards_live_side():
    _dashboard_on()
    saved = (dashboard_spire.trust_domain, dashboard_spire.live_bundle)
    dashboard_spire.trust_domain = lambda timeout=30: DASH_TD
    dashboard_spire.live_bundle = lambda: json.loads(BUNDLE)
    try:
        v = svc.FEDERATION_STAGE["vars_for"](_lab())
        assert v["dashboard_trust_domain"] == DASH_TD and v["state"] == "present"
        assert v["dashboard_bundle_endpoint_url"] == "https://agents.fed.test:8082"
        assert json.loads(v["dashboard_bundle_json"]) == json.loads(BUNDLE)
        gone = svc.UNFEDERATION_STAGE["vars_for"](_lab())
        assert gone["state"] == "absent" and "dashboard_bundle_json" not in gone
    finally:
        dashboard_spire.trust_domain, dashboard_spire.live_bundle = saved


def test_both_servers_serve_their_bundle_on_8082_directly():
    conf = open(os.path.join(_ROOT, "examples", "spire-server", "server.conf"),
                encoding="utf-8").read()
    assert "bundle_endpoint {" in conf and "port    = 8082" in conf
    compose = yaml.safe_load(open(os.path.join(_ROOT, "docker-compose.spire.yml"),
                                  encoding="utf-8").read())
    assert "${SPIRE_FEDERATION_PORT:-8082}:8082" in compose["services"]["spire-server"]["ports"]
    for caddy in ("Caddyfile", "Caddyfile.agent"):
        path = os.path.join(_ROOT, caddy)
        if os.path.exists(path):
            assert "8082" not in open(path, encoding="utf-8").read(), (
                "https_spiffe is TLS with the server's own SVID; a proxy would terminate it")
    for play in ("spire-server-install.yml", "spire-docker-server.yml"):
        src = open(os.path.join(_ROOT, "examples", "playbooks", "spire", play),
                   encoding="utf-8").read()
        assert "port    = {{ federation_port }}" in src, play
    assert svc._install_vars(_lab())["federation_port"] == 8082


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
