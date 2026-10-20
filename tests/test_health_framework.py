"""The estate Health page: the collector framework and its first areas.

What these pin:

- every registered area keeps the contract: a unique id, real feature flags, real persona
  keys, an interval, a label;
- a run replaces an area's rows and drops objects no longer reported; a collector that
  raises records the error against the AREA and leaves its rows alone, and the error
  text shown is the exception's type, never its message;
- only enabled areas run, only when due, and Run now is throttled;
- the snapshot orders by the active persona without hiding anything;
- each first-area check at its thresholds: tenant credentials, Gateways and their hosts,
  Password Safe rotation, the Entitle sweep and applied apps, remote agents;
- an agent's identity (signing key on disk vs SPIRE-attested) is counted in an area note
  and never changes a status;
- the API is administrator-only and its GET reads stored rows only;
- no collector reads identities.

Uses a real temp SQLite database; no provider is called.

Run: python tests/test_health_framework.py   (or under pytest)
"""
import asyncio
import json
import os
import re
import sys
import tempfile
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="health-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDB}"
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-health-tests")

from web_dashboard.database import (Base, BeyondTrustTenant, HealthArea,  # noqa: E402
                                    HealthCheck, SessionLocal, engine)
from web_dashboard.utils.clock import utcnow  # noqa: E402
from web_dashboard.services import feature_flags  # noqa: E402
from web_dashboard.services.health import core, registry, runner  # noqa: E402
from web_dashboard.services.health.collectors import (agents, bt_tenants,  # noqa: E402
                                                      entitle, pra_gateways, ps_accounts)

Base.metadata.create_all(bind=engine)

NOW = datetime(2026, 10, 12, 12, 0, 0)
_HEALTH = os.path.join(_ROOT, "web_dashboard", "services", "health")


def _clear(db):
    db.query(HealthCheck).delete()
    db.query(HealthArea).delete()
    db.commit()


def _item(oid, status, name=None):
    return core.Item(object_id=oid, name=name or oid,
                     checks=[core.check("c", "Check", status, "detail")])


class _Flags:
    """Swap feature_flags.enabled for a fixed set while a block runs."""
    def __init__(self, *on):
        self.on = set(on)

    def __enter__(self):
        self.real = feature_flags.enabled
        feature_flags.enabled = lambda flag, default=False: flag in self.on
        return self

    def __exit__(self, *exc):
        feature_flags.enabled = self.real


# ── the registry contract ─────────────────────────────────────────────────────

def test_every_area_keeps_the_contract():
    from web_dashboard.services import personas
    real_flags = set(feature_flags.flags())
    areas = [c.area for c in registry.COLLECTORS]
    assert len(areas) == len(set(areas)), f"duplicate area ids: {areas}"
    for c in registry.COLLECTORS:
        assert re.fullmatch(r"[a-z_]{3,32}", c.area), c.area
        assert c.label and c.description and c.interval_minutes > 0, c.area
        for flag in c.flags:
            assert flag in real_flags, f"{c.area} names unknown flag {flag}"
        for p in c.personas:
            assert p in personas.VALID_PERSONAS, f"{c.area} names unknown persona {p}"
        assert asyncio.iscoroutinefunction(c.collect), c.area


# ── the runner ────────────────────────────────────────────────────────────────

def test_a_run_replaces_the_area_and_drops_what_is_gone():
    db = SessionLocal()
    _clear(db)
    runner.store(db, "beyondtrust", [_item("a", core.OK), _item("b", core.FAIL)])
    runner.store(db, "beyondtrust", [_item("a", core.WARN)])
    rows = db.query(HealthCheck).filter(HealthCheck.area == "beyondtrust").all()
    assert [(r.object_id, r.status) for r in rows] == [("a", "warn")]
    runner.store(db, "beyondtrust", [])
    assert not db.query(HealthCheck).filter(HealthCheck.area == "beyondtrust").all()
    db.close()


def test_a_failing_collector_keeps_the_rows_and_shows_only_the_type():
    db = SessionLocal()
    _clear(db)
    runner.store(db, "password_safe", [_item("x", core.OK)])

    async def boom(_db):
        raise RuntimeError("https://ps.internal/secret-path?token=abc")
    collector = core.Collector(area="password_safe", label="PS", collect=boom,
                               interval_minutes=60)
    result = asyncio.run(runner.run_area(db, collector))
    area = db.get(HealthArea, "password_safe")
    assert result["ok"] is False and "RuntimeError" in area.last_error
    assert "secret-path" not in area.last_error and "token" not in area.last_error
    assert db.get(HealthCheck, ("password_safe", "x")).status == "ok", "rows were touched"
    db.close()


def test_only_enabled_and_due_areas_run():
    db = SessionLocal()
    _clear(db)
    calls = []

    def fake(area):
        async def collect(_db):
            calls.append(area)
            return [_item(f"{area}-1", core.OK)]
        return collect
    real = registry.COLLECTORS
    try:
        registry.COLLECTORS = runner.COLLECTORS = (
            core.Collector(area="on_area", label="On", collect=fake("on_area"),
                           interval_minutes=10, flags=("pra_enabled",)),
            core.Collector(area="off_area", label="Off", collect=fake("off_area"),
                           interval_minutes=10, flags=("entitle_enabled",)),
        )
        with _Flags("pra_enabled"):
            asyncio.run(runner.run_due(db))
            asyncio.run(runner.run_due(db))          # not due again yet
            assert calls == ["on_area"], calls
            db.get(HealthArea, "on_area").last_run_at = utcnow() - timedelta(minutes=11)
            db.commit()
            asyncio.run(runner.run_due(db))
            assert calls == ["on_area", "on_area"], calls
            snap = runner.snapshot(db)
            assert [a["area"] for a in snap["areas"]] == ["on_area"], \
                "a disabled area with no rows was shown"
    finally:
        registry.COLLECTORS = runner.COLLECTORS = real
    db.close()


def test_run_now_is_throttled():
    db = SessionLocal()
    _clear(db)
    db.add(HealthArea(area="remote_agents", last_run_at=utcnow()))
    db.commit()
    result = asyncio.run(runner.run_now(db, "remote_agents"))
    assert result.get("throttled") is True
    db.close()


def test_the_snapshot_orders_by_persona_and_hides_nothing():
    db = SessionLocal()
    _clear(db)
    with _Flags("pra_enabled", "password_safe_enabled", "entitle_enabled",
                "remote_agents_enabled", "directories_enabled"):
        plain = [a["area"] for a in runner.snapshot(db)["areas"]]
        iam = [a["area"] for a in runner.snapshot(db, persona="iamops")["areas"]]
    assert sorted(plain) == sorted(iam), "a persona changed WHICH areas are shown"
    assert iam[:3] == ["password_safe", "remote_agents", "directories"], iam
    assert plain[0] == "resources"
    db.close()


def test_worst_objects_first_and_counted():
    db = SessionLocal()
    _clear(db)
    runner.store(db, "remote_agents", [_item("a", core.OK, "a"), _item("b", core.FAIL, "b"),
                                       _item("c", core.WARN, "c")])
    with _Flags("remote_agents_enabled"):
        area = [a for a in runner.snapshot(db)["areas"] if a["area"] == "remote_agents"][0]
    assert [i["name"] for i in area["items"]] == ["b", "c", "a"]
    assert area["counts"] == {"ok": 1, "warn": 1, "fail": 1, "unknown": 0}
    db.close()


# ── BeyondTrust tenants ───────────────────────────────────────────────────────

def test_tenants_are_verified_and_the_result_recorded():
    from web_dashboard.services import bt_tenant_verify, config_service
    db = SessionLocal()
    good = BeyondTrustTenant(id=str(uuid.uuid4()), kind="pra", name="lab",
                             base_url="pra.example.com", client_id="c",
                             secret_enc=config_service.encrypt_value("s"), is_active=True)
    bad = BeyondTrustTenant(id=str(uuid.uuid4()), kind="pra", name="old",
                            base_url="old.example.com", client_id="c",
                            secret_enc=config_service.encrypt_value("s"), is_active=True)
    db.add_all([good, bad])
    db.commit()
    real = bt_tenant_verify.verify

    async def fake(tenant):
        return (True, "PRA issued a token.") if tenant.name == "lab" else \
            (False, "PRA rejected the client credentials.")
    try:
        bt_tenant_verify.verify = fake
        with _Flags("pra_enabled"):
            items = asyncio.run(bt_tenants.collect(db))
    finally:
        bt_tenant_verify.verify = real
    by = {i.name: i.status for i in items}
    assert by == {"Privileged Remote Access: lab": "ok",
                  "Privileged Remote Access: old": "fail"}, by
    db.refresh(bad)
    assert bad.last_error and bad.last_checked_at, "the tenant row did not record the check"
    db.delete(good)
    db.delete(bad)
    db.commit()
    db.close()


# ── PRA Gateways ──────────────────────────────────────────────────────────────

def test_gateway_hosts_and_pra_gateways():
    rows = [SimpleNamespace(id="1", name="gw-a", status="running", managed=True, cloud="aws",
                            region="us-east-2", zone=None, error=None),
            SimpleNamespace(id="2", name="gw-b", status="missing", managed=False, cloud="gcp",
                            region="us-central1", zone=None, error="instance not found"),
            SimpleNamespace(id="3", name="gw-c", status="deleted", managed=True, cloud="aws",
                            region="", zone=None, error=None)]
    hosts = {i.name: i.status for i in pra_gateways.host_items(rows)}
    assert hosts == {"gw-a": "ok", "gw-b": "fail"}, hosts
    gws = pra_gateways.gateway_items("lab", "t1", [
        {"id": 1, "name": "east", "connected": True},
        {"id": 2, "name": "west", "connected": False},
        {"id": 3, "name": "old", "connected": None}])
    assert [g.status for g in gws] == ["ok", "fail", "unknown"]


# ── Password Safe ─────────────────────────────────────────────────────────────

def _rotation(state):
    return ps_accounts.account_checks(state, now=NOW)[0]


def test_password_safe_rotation_thresholds():
    assert _rotation({})["status"] == "fail"
    ok = _rotation({"last_change_date": "2026-10-01T00:00:00.1234567Z",
                    "next_change_date": "2026-10-31T00:00:00Z"})
    assert ok["status"] == "ok" and "2026-10-31" in ok["detail"]
    assert _rotation({"last_change_date": "", "next_change_date": ""})["status"] == "warn"
    assert _rotation({"last_change_date": "2026-09-01T00:00:00Z",
                      "next_change_date": "2026-10-10T00:00:00Z"})["status"] == "warn"
    assert _rotation({"last_change_date": "2026-09-01T00:00:00Z",
                      "next_change_date": "2026-10-01T00:00:00+00:00"})["status"] == "fail"


def test_password_safe_down_is_an_area_error_not_failed_accounts():
    from web_dashboard.database import CloudDatabase
    from web_dashboard.services import ps_api_service
    db = SessionLocal()
    _clear(db)
    row = CloudDatabase(engine="postgres", provider="rds", cloud="aws", region="us-east-2",
                        source="provisioned", status="available", instance_id="db-1",
                        ps_managed_account_id="42", created_by="t")
    db.add(row)
    db.commit()
    real = ps_api_service.get_managed_account_states

    async def down(_ids):
        raise ps_api_service.PSApiError("sign-in failed (401)")
    try:
        ps_api_service.get_managed_account_states = down
        asyncio.run(runner.run_area(db, registry.get("password_safe")))
    finally:
        ps_api_service.get_managed_account_states = real
    assert db.get(HealthArea, "password_safe").last_error
    assert not db.query(HealthCheck).filter(HealthCheck.area == "password_safe").all()

    async def up(ids):
        return {"42": {"last_change_date": "2026-10-11T00:00:00Z",
                       "next_change_date": "2099-01-01T00:00:00Z"}}
    try:
        ps_api_service.get_managed_account_states = up
        asyncio.run(runner.run_area(db, registry.get("password_safe")))
    finally:
        ps_api_service.get_managed_account_states = real
    stored = db.query(HealthCheck).filter(HealthCheck.area == "password_safe").all()
    assert [(r.status, r.href) for r in stored] == [("ok", "/databases")]
    assert db.get(HealthArea, "password_safe").last_error is None
    db.delete(row)
    db.commit()
    db.close()


# ── Entitle ───────────────────────────────────────────────────────────────────

def test_entitle_sweep_states():
    assert entitle.sweep_item({}, gate_on=False, interval_s=3600, now=NOW) is None
    never = entitle.sweep_item({"never_run": True}, gate_on=True, interval_s=3600, now=NOW)
    assert never.status == "unknown"
    off = entitle.sweep_item({"skipped": "off"}, gate_on=True, interval_s=3600, now=NOW)
    assert off.status == "warn"
    fresh = {"ended_at": "2026-10-12T11:30:00", "processed": 4, "by_cloud": {}, "orphans": []}
    assert entitle.sweep_item(fresh, gate_on=True, interval_s=3600, now=NOW).status == "ok"
    late = dict(fresh, ended_at="2026-10-12T05:00:00")
    assert entitle.sweep_item(late, gate_on=True, interval_s=3600, now=NOW).status == "warn"
    broken = dict(fresh, by_cloud={"aws": {"error": "AccessDenied"}})
    assert entitle.sweep_item(broken, gate_on=True, interval_s=3600, now=NOW).status == "fail"
    orphans = dict(fresh, orphans=[{"id": 1}])
    assert entitle.sweep_item(orphans, gate_on=True, interval_s=3600, now=NOW).status == "warn"


def test_entitle_applied_apps():
    rows = [SimpleNamespace(id="1", name="ad-groups", status="active", error=None),
            SimpleNamespace(id="2", name="broken", status="failed", error="terraform: 403"),
            SimpleNamespace(id="3", name="gone", status="deleted", error=None)]
    items = entitle.applied_items(rows, kind="virtual_app", name_of=lambda r: r.name)
    assert {i.name: i.status for i in items} == {"ad-groups": "ok", "broken": "fail"}


# ── remote agents ─────────────────────────────────────────────────────────────

def test_agent_checks():
    agent = SimpleNamespace(last_seen_at=NOW - timedelta(minutes=1), agent_version="2.7.0")
    online = agents.agent_checks(agent, state="online", directories=0, can_directory=False,
                                 hint="", now=NOW)
    assert core.overall(online) == "ok"
    offline = agents.agent_checks(agent, state="offline", directories=0, can_directory=True,
                                  hint="", now=NOW)
    assert core.overall(offline) == "fail"
    old = agents.agent_checks(agent, state="online", directories=2, can_directory=False,
                              hint="Upgrade it.", now=NOW)
    assert core.overall(old) == "warn" and "Upgrade it." in old[1]["detail"]


def _agent(auth_mode, **kw):
    return SimpleNamespace(last_seen_at=NOW - timedelta(minutes=1), agent_version="2.7.0",
                           auth_mode=auth_mode, **kw)


def test_agent_identity_is_a_recommendation_never_a_failure():
    for mode, want in ((None, "ed25519"), ("ed25519", "ed25519"), ("spiffe", "spiffe"),
                       ("SPIFFE", "spiffe")):
        checks = agents.agent_checks(_agent(mode), state="online", directories=0,
                                     can_directory=True, hint="", now=NOW)
        ident = [c for c in checks if c["key"] == "identity"]
        assert len(ident) == 1 and ident[0]["mode"] == want, (mode, ident)
        # A key on disk is supported: the agent stays Healthy.
        assert ident[0]["status"] == core.OK and core.overall(checks) == core.OK, mode
    on_disk = agents.identity_check(_agent(None))
    assert "on disk" in on_disk["detail"] and "SPIRE" in on_disk["detail"]


def _stored(oid, mode):
    return core.Item(object_id=oid, name=oid, kind="Remote agent",
                     checks=[core.check("online", "Online", core.OK, ""),
                             agents.identity_check(_agent(mode))])


def test_identity_note_counts_the_fleet():
    assert agents.identity_note([]) is None
    # An area whose items carry no identity check has nothing to say.
    assert agents.identity_note([{"checks": [{"key": "online", "status": "ok"}]}]) is None
    mixed = [{"checks": _stored(str(i), m).checks} for i, m in
             enumerate((None, "ed25519", "spiffe"))]
    note = agents.identity_note(mixed)
    assert note["text"].startswith("2 of 3 agent(s) still keep"), note
    assert "1 are SPIRE-attested" in note["text"], note
    allspire = agents.identity_note([{"checks": _stored("a", "spiffe").checks}])
    assert allspire["text"].startswith("All 1 agent(s) are SPIRE-attested"), allspire


def test_identity_note_links_the_docs_only_when_the_page_exists():
    items = [{"checks": _stored("a", None).checks}]
    real = agents._DOCS_PAGE
    try:
        agents._DOCS_PAGE = real.with_name("no-such-page.md")
        assert agents.identity_note(items)["href"] == ""
        agents._DOCS_PAGE = real
        if real.is_file():
            note = agents.identity_note(items)
            assert note["href"] == agents.DOCS_HREF and note["link"]
            # The fragment is the slug of this heading; renaming it strands the link.
            assert "\n### Remote agents\n" in real.read_text(encoding="utf-8")
    finally:
        agents._DOCS_PAGE = real
    page, _, frag = agents.DOCS_HREF.partition("#")
    assert page == "/docs/access/workload-identity" and frag == "remote-agents"


def test_the_identity_note_reaches_the_snapshot_without_changing_counts():
    db = SessionLocal()
    _clear(db)
    runner.store(db, "remote_agents", [_stored("a", None), _stored("b", None),
                                       _stored("c", "spiffe")])
    with _Flags("remote_agents_enabled"):
        snap = runner.snapshot(db)
    area = [a for a in snap["areas"] if a["area"] == "remote_agents"][0]
    assert area["counts"] == {"ok": 3, "warn": 0, "fail": 0, "unknown": 0}
    assert snap["counts"]["fail"] == 0 and snap["counts"]["warn"] == 0
    assert area["note"]["text"].startswith("2 of 3"), area["note"]
    # Areas without a note say so, and a note that raises is dropped, not the page.
    assert all(a["note"] is None for a in snap["areas"] if a["area"] != "remote_agents")
    broken = core.Collector(area="remote_agents", label="x", collect=None,
                            interval_minutes=5, note=lambda items: 1 / 0)
    assert runner._note(broken, area["items"]) is None
    db.close()


def test_the_agents_collector_counts_only_active_agents():
    from web_dashboard.database import RemoteAgent
    db = SessionLocal()
    db.query(RemoteAgent).delete()
    for name, mode, active in (("keyed", None, True), ("attested", "spiffe", True),
                               ("revoked", None, False)):
        db.add(RemoteAgent(id=str(uuid.uuid4()), name=name, auth_mode=mode,
                           is_active=active, agent_version="2.7.0",
                           public_key="x" * 44, last_seen_at=utcnow()))
    db.commit()
    items = asyncio.run(agents.collect(db))
    assert sorted(i.name for i in items) == ["attested", "keyed"]
    note = agents.identity_note([{"checks": i.checks} for i in items])
    assert note["text"].startswith("1 of 2"), note
    db.query(RemoteAgent).delete()
    db.commit()
    db.close()


# ── the API and the boundary ──────────────────────────────────────────────────

def test_the_api_is_admin_only_and_reads_stored_rows():
    src = open(os.path.join(_ROOT, "web_dashboard", "api", "health_checks.py"),
               encoding="utf-8").read()
    assert src.count("Depends(require_admin)") == 2
    get_body = src.split("def health_checks(", 1)[1].split("\n@router", 1)[0]
    assert "runner.snapshot(" in get_body
    assert "collect" not in get_body and "run_now" not in get_body
    assert 'prefix="/api/estate-health"' in src, \
        "/api/health* rides the setup-guard bypass for the liveness probe"


def test_no_collector_reads_identities():
    for name in sorted(os.listdir(os.path.join(_HEALTH, "collectors"))):
        if not name.endswith(".py"):
            continue
        src = open(os.path.join(_HEALTH, "collectors", name), encoding="utf-8").read()
        for forbidden in ("list_users", "list_groups", "group_members", "user_groups",
                          "checkout_credential", "get_ps_credential", "roleAssignments",
                          "directoryRoles", "signIns"):
            assert forbidden not in src, f"collectors/{name} uses {forbidden}"


def test_stored_checks_round_trip():
    db = SessionLocal()
    _clear(db)
    runner.store(db, "entitle", [_item("e", core.WARN)])
    row = db.get(HealthCheck, ("entitle", "e"))
    assert json.loads(row.checks)[0]["status"] == "warn"
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
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
