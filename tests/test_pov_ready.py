"""The pre-demo ready check: ``services/pov_ready`` and its two routes.

What is pinned here, because each fails silently:

  * **The GET makes no call.** ``describe`` reads the serialised row, stored health rows
    and the last live run. A live read on that path would make opening a POV as slow as the
    customer's appliance -- so the route is exercised with every live function replaced by
    one that fails the test if it is touched.
  * **Unknown never outranks a real answer**, and a tenant nobody has checked yet is
    unknown, not failed. A badge that goes red because a check has not run would train an
    SE to ignore it.
  * **A mashed button is one run**: in flight returns the same job, and a run that just
    started refuses another for ``RUN_MIN_SECONDS``.
  * **A live run that half fails still stores what it learnt**, with the failure named.
  * **EPM-L** has a collector now, reporting nothing on an instance that never set it up.

Runs under pytest, or standalone:
    python tests/test_pov_ready.py
"""
import asyncio
import os
import sys
import tempfile
import uuid
from datetime import datetime, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="pov-ready-test-"), "test.db")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_TMPDB}")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-pov-ready")

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover
    print("SKIP: fastapi not installed")
    sys.exit(0)

from web_dashboard import database as d  # noqa: E402

d.Base.metadata.create_all(bind=d.engine)

from web_dashboard import jobs_worker  # noqa: E402
from web_dashboard.api import pov as pov_api  # noqa: E402
from web_dashboard.api.auth import get_current_user  # noqa: E402
from web_dashboard.services import pov_ready  # noqa: E402
from web_dashboard.services.health import core  # noqa: E402
from web_dashboard.services.health.collectors import epml as epml_collector  # noqa: E402

NOW = datetime(2026, 10, 20, 14, 0, 0)


def _row(**over):
    """A serialised POV row with everything green."""
    row = {
        "status": "active", "runstate": "running", "platform_seen_at": "2026-10-20T13:55:00",
        "rate_limited": False, "platform_missing": False,
        "setup": {"busy": False, "next_label": "", "next_detail": ""},
        "vms": [{"name": "dc01", "wiring_error": ""}],
        "pra_tenant_id": "t-pra", "ps_tenant_id": "t-ps", "entitle_tenant_id": "",
        "gateway_name": "",
        "pra_ps_checked": True, "pra_ps_checked_at": "2026-10-20T13:00:00",
        "pra_ps_matched_count": 3, "pra_ps_linked_count": 3,
        "share_url": "", "expiry_enabled": True, "expires_at": "",
        "spend_priced": False, "spend": {},
    }
    row.update(over)
    return row


def _health(status=core.OK):
    item = {"name": "x", "status": status, "href": "/pov/templates",
            "checks": [core.check("credential", "Credential and API", status, "said so")],
            "checked_at": "2026-10-20T13:30:00Z"}
    return {("beyondtrust", "pra:t-pra"): item, ("beyondtrust", "password_safe:t-ps"): item}


def _by_key(result):
    return {c["key"]: c for c in result["checks"]}


# ── describe: the pure half ──────────────────────────────────────────────────

def test_everything_green_is_ready():
    r = pov_ready.describe(_row(), _health(), {}, {}, now=NOW)
    assert r["overall"] == core.OK, [c for c in r["checks"] if c["status"] != core.OK]


def test_a_suspended_lab_needs_attention_not_a_failure():
    r = pov_ready.describe(_row(runstate="suspended"), _health(), {}, {}, now=NOW)
    assert _by_key(r)["power"]["status"] == core.WARN
    assert r["overall"] == core.WARN


def test_an_unchecked_tenant_is_unknown_and_does_not_outrank_green():
    r = pov_ready.describe(_row(), {}, {}, {}, now=NOW)
    assert _by_key(r)["tenant:pra"]["status"] == core.UNKNOWN
    assert r["overall"] == core.OK, "unknown outranked a real answer"


def test_a_failing_tenant_credential_makes_it_not_ready():
    r = pov_ready.describe(_row(), _health(core.FAIL), {}, {}, now=NOW)
    assert _by_key(r)["tenant:password_safe"]["status"] == core.FAIL
    assert r["overall"] == core.FAIL


def test_a_tenant_this_pov_does_not_use_is_not_checked():
    r = pov_ready.describe(_row(ps_tenant_id=""), _health(), {}, {}, now=NOW)
    keys = _by_key(r)
    assert "tenant:password_safe" not in keys
    assert "pra_ps" not in keys, "the PRA->PS link only matters with both tenants"


def test_a_guest_wiring_error_fails():
    r = pov_ready.describe(_row(vms=[{"name": "web01", "wiring_error": "boom"}]),
                           _health(), {}, {}, now=NOW)
    assert _by_key(r)["wiring"]["status"] == core.FAIL
    assert "web01" in _by_key(r)["wiring"]["detail"]


def test_unlinked_accounts_need_attention():
    r = pov_ready.describe(_row(pra_ps_linked_count=1), _health(), {}, {}, now=NOW)
    assert _by_key(r)["pra_ps"]["status"] == core.WARN


def test_deadlines_are_judged_against_the_demo_time():
    soon = (NOW + timedelta(hours=1)).isoformat()
    later = (NOW + timedelta(days=3)).isoformat()
    row = _row(share_url="https://x", share_expired=False, share_expires_at=later,
               expires_at=soon)
    r = _by_key(pov_ready.describe(row, _health(), {}, {}, now=NOW))
    assert r["expiry"]["status"] == core.WARN, "deletes itself during the demo"
    assert r["share"]["status"] == core.OK
    # A demo in three days: the share link now expires mid-demo too.
    r = _by_key(pov_ready.describe(row, _health(), {}, {}, now=NOW,
                                   demo_at=NOW + timedelta(days=3) - timedelta(minutes=30)))
    assert r["share"]["status"] == core.WARN
    assert r["expiry"]["status"] == core.WARN


def test_an_expired_share_link_fails():
    r = pov_ready.describe(_row(share_url="https://x", share_expired=True),
                           _health(), {}, {}, now=NOW)
    assert _by_key(r)["share"]["status"] == core.FAIL


def test_a_spend_cap_that_was_hit_fails():
    spend = {"capped": True, "over": True, "percent": 100, "warn_percent": 80,
             "summary": "$50.00 of $50.00 — suspended"}
    r = pov_ready.describe(_row(spend_priced=True, spend=spend), _health(), {}, {}, now=NOW)
    assert _by_key(r)["spend"]["status"] == core.FAIL


def test_the_gateway_and_preflight_come_from_the_last_live_run():
    row = _row(gateway_name="gw-1")
    r = _by_key(pov_ready.describe(row, _health(), {}, {}, now=NOW))
    assert r["gateway"]["status"] == core.UNKNOWN, "never asked is not disconnected"
    assert "preflight" not in r
    run = {"finished_at": "2026-10-20T13:59:00Z",
           "gateway": {"state": "disconnected", "detail": "no node"},
           "preflight": [{"key": "storage", "label": "Storage", "ok": False, "detail": "403"},
                         {"key": "oidc", "label": "OIDC", "ok": True, "detail": ""}]}
    r = _by_key(pov_ready.describe(row, _health(), {}, run, now=NOW))
    assert r["gateway"]["status"] == core.FAIL
    assert r["preflight"]["status"] == core.FAIL and "Storage" in r["preflight"]["detail"]


def test_epml_and_entitle_appear_only_when_they_have_rows():
    areas = {"epml": [{"name": "EPM for Linux site", "status": core.WARN,
                       "checked_at": "2026-10-20T13:00:00Z"}],
             "entitle": [{"name": "JIT", "status": core.OK, "checked_at": ""}]}
    r = _by_key(pov_ready.describe(_row(), _health(), areas, {}, now=NOW))
    assert r["epml"]["status"] == core.WARN
    assert "entitle" not in r, "this POV has no Entitle tenant"
    r = _by_key(pov_ready.describe(_row(entitle_tenant_id="t-ent"), _health(), areas, {},
                                   now=NOW))
    assert r["entitle"]["status"] == core.OK


# ── the routes ───────────────────────────────────────────────────────────────

class _Operator:
    username = "operator"
    is_admin = True
    is_effective_admin = True
    effective_permissions_dict = {}
    accessor_env_id = None
    pov_env_ids_list = []
    workgroups_list = []


def _db():
    db = d.SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _client():
    app = FastAPI()
    app.include_router(pov_api.router)
    app.dependency_overrides[get_current_user] = lambda: _Operator()
    app.dependency_overrides[d.get_db] = _db
    return TestClient(app, raise_server_exceptions=False)


def _env(**over):
    db = d.SessionLocal()
    try:
        env = d.PovEnvironment(platform="skytap", name="pov-" + uuid.uuid4().hex[:8],
                               status="active", runstate="running", **over)
        db.add(env)
        db.commit()
        return env.id
    finally:
        db.close()


def _forbid_live_calls():
    """Replace every live function the ready check could reach with a tripwire."""
    from web_dashboard.services import (pov_gateway, pov_pra_ps_link, pov_reconcile,
                                        preflight)
    from web_dashboard.services.health import runner

    def trip(*_a, **_k):
        raise AssertionError("the GET path made a live call")
    saved = []
    for mod, name in ((pov_gateway, "status"), (pov_pra_ps_link, "check"),
                      (pov_reconcile, "reconcile"), (preflight, "run_all"),
                      (runner, "run_now"), (runner, "run_area")):
        saved.append((mod, name, getattr(mod, name)))
        setattr(mod, name, trip)
    return saved


def _restore(saved):
    for mod, name, fn in saved:
        setattr(mod, name, fn)


def test_the_get_is_served_from_storage_alone():
    env_id = _env(gateway_name="gw-1")
    saved = _forbid_live_calls()
    try:
        res = _client().get(f"/api/pov/managed/{env_id}/ready")
    finally:
        _restore(saved)
    assert res.status_code == 200, res.text
    ready = res.json()["ready"]
    assert ready["overall"] in core.STATUSES
    assert {c["key"] for c in ready["checks"]} >= {"environment", "power", "setup", "gateway"}


def test_a_bad_demo_time_is_a_400():
    res = _client().get(f"/api/pov/managed/{_env()}/ready", params={"demo_at": "tuesday"})
    assert res.status_code == 400


def test_an_unknown_pov_is_a_404():
    assert _client().get("/api/pov/managed/nope/ready").status_code == 404
    assert _client().post("/api/pov/managed/nope/ready/run").status_code == 404


def test_a_mashed_button_is_one_run():
    env_id = _env()
    c = _client()
    first = c.post(f"/api/pov/managed/{env_id}/ready/run")
    assert first.status_code == 202, first.text
    again = c.post(f"/api/pov/managed/{env_id}/ready/run")
    assert again.json() == {"job_id": first.json()["job_id"], "already_running": True}
    assert c.get(f"/api/pov/managed/{env_id}/ready").json()["ready"]["running"] == \
        first.json()["job_id"]


def test_a_check_booked_for_later_is_not_in_flight():
    """A warm-up books one for just before the demo. Counting it as running would grey
    out Check now until then."""
    from web_dashboard.services import job_service
    env_id = _env()
    db = d.SessionLocal()
    try:
        job_service.create_job(db, job_type=pov_ready.JOB_TYPE, created_by="pov-warmup",
                               metadata={"environment_id": env_id},
                               scheduled_for=datetime.utcnow() + timedelta(minutes=20))
    finally:
        db.close()
    c = _client()
    ready = c.get(f"/api/pov/managed/{env_id}/ready").json()["ready"]
    assert ready["running"] == "" and ready["booked_for"].endswith("Z")
    res = c.post(f"/api/pov/managed/{env_id}/ready/run")
    assert res.status_code == 202 and res.json()["already_running"] is False


def test_a_run_that_just_started_refuses_another():
    env_id = _env()
    db = d.SessionLocal()
    try:
        env = db.get(d.PovEnvironment, env_id)
        env.metadata_dict = {pov_ready.RUN_KEY: {
            "started_at": datetime.utcnow().isoformat() + "Z"}}
        db.commit()
    finally:
        db.close()
    res = _client().post(f"/api/pov/managed/{env_id}/ready/run")
    assert res.status_code == 429
    assert int(res.headers["Retry-After"]) > 0


def test_a_destroyed_pov_cannot_be_checked():
    env_id = _env()
    db = d.SessionLocal()
    try:
        db.get(d.PovEnvironment, env_id).status = "destroyed"
        db.commit()
    finally:
        db.close()
    assert _client().post(f"/api/pov/managed/{env_id}/ready/run").status_code == 409


# ── the live run ─────────────────────────────────────────────────────────────

def test_a_live_run_that_half_fails_still_stores_what_it_learnt():
    from web_dashboard.services import (job_service, pov_gateway, pov_pra_ps_link,
                                        pov_reconcile, preflight)
    env_id = _env(gateway_name="gw-1")
    db = d.SessionLocal()
    try:
        job = job_service.create_job(db, job_type=pov_ready.JOB_TYPE, created_by="op",
                                     metadata={"environment_id": env_id})
        job_id = job.id
    finally:
        db.close()

    async def reconcile(*_a, **_k):
        raise RuntimeError("platform said 503")

    async def gateway(*_a, **_k):
        return {"state": "connected", "detail": "fine"}

    async def run_all():
        return [preflight.Result("storage", "Storage", True, True, "")]

    saved = [(pov_reconcile, "reconcile", pov_reconcile.reconcile),
             (pov_gateway, "status", pov_gateway.status),
             (preflight, "run_all", preflight.run_all),
             (pov_pra_ps_link, "check", pov_pra_ps_link.check)]
    pov_reconcile.reconcile, pov_gateway.status, preflight.run_all = reconcile, gateway, run_all
    try:
        asyncio.run(pov_ready.run_live(job_id, {"environment_id": env_id}))
    finally:
        _restore(saved)

    db = d.SessionLocal()
    try:
        env = db.get(d.PovEnvironment, env_id)
        run = pov_ready.last_run(env)
        assert run.get("finished_at"), "the run never recorded finishing"
        assert run["gateway"]["state"] == "connected"
        assert run["preflight"][0]["ok"] is True
        assert any("503" in e for e in run["errors"]), run["errors"]
        assert db.get(d.Job, job_id).status == "completed"
    finally:
        db.close()


def test_the_job_type_is_wired_into_the_worker():
    assert pov_ready.JOB_TYPE in jobs_worker.HANDLED_TYPES
    assert pov_ready.JOB_TYPE in jobs_worker.LIGHT_TYPES
    src = open(jobs_worker.__file__, encoding="utf-8").read()
    assert '"pov_env_ready_check": pov_ready.run_live,' in src


# ── the EPM-L collector ──────────────────────────────────────────────────────

def test_epml_item_reports_each_state():
    assert epml_collector.item_from(packages=None, error="401").status == core.FAIL
    assert epml_collector.item_from(packages=[{"file": "a.rpm"}]).status == core.OK
    building = epml_collector.item_from(packages=[], build="building")
    assert building.checks[-1]["status"] == core.UNKNOWN
    assert building.status == core.OK, "a running build is not a problem with the site"
    assert epml_collector.item_from(packages=[], build="idle").status == core.WARN


def test_epml_reports_nothing_when_it_was_never_set_up():
    saved = epml_collector.configured
    epml_collector.configured = lambda: False
    try:
        assert asyncio.run(epml_collector.collect(None)) == []
    finally:
        epml_collector.configured = saved


def test_epml_collector_never_triggers_a_build_or_mints_a_token():
    src = open(epml_collector.__file__, encoding="utf-8").read()
    for name in ("trigger_build(", "get_installation_token(", "ensure_packages("):
        assert name not in src, f"the health collector calls {name}"


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
