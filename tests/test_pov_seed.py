"""Sample activity for a demo: ``services/pov_seed`` and its routes.

It is the one feature here whose whole purpose is to write into a customer's tenant, so
what is pinned is mostly what it must NOT do:

  * **a fixed catalog** -- a seed is an id, never a request body somebody typed;
  * **only this POV's own accounts**, at most ``MAX_ACCOUNTS`` of them;
  * **tagged** -- every reason carries ``[pov-seed:<pov>:<seed>]``;
  * **no rotation, no credential kept** (``ps_api_service.request_cycle``, tested in
    tests/test_ps_api_credentials.py), and no Entitle client at all;
  * **never silent** -- the enqueue is audited, and a run that Password Safe refused
    fails its job with Password Safe's words rather than completing green.

Runs under pytest, or standalone:
    python tests/test_pov_seed.py
"""
import asyncio
import os
import sys
import tempfile
import uuid
from datetime import timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="pov-seed-test-"), "test.db")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_TMPDB}")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-pov-seed")

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
from web_dashboard.services import job_service, pov_seed  # noqa: E402
from web_dashboard.utils.clock import utcnow  # noqa: E402

_SEED = "ps_checkout_cycle"


def _env(*, ps=True, accounts=2, status="active"):
    db = d.SessionLocal()
    try:
        env = d.PovEnvironment(platform="skytap", name="pov-" + uuid.uuid4().hex[:8],
                               status=status, ps_tenant_id="t-ps" if ps else None)
        db.add(env)
        db.flush()
        for i in range(accounts):
            db.add(d.PovEnvironmentVM(environment_id=env.id, platform_vm_id=f"vm-{i}",
                                      name=f"guest-{i:02d}", ps_managed_system_id=str(100 + i),
                                      ps_managed_account_id=str(200 + i)))
        db.commit()
        return env.id
    finally:
        db.close()


def _with_env(env_id, fn):
    db = d.SessionLocal()
    try:
        return fn(db, db.get(d.PovEnvironment, env_id))
    finally:
        db.close()


# ── the catalog ──────────────────────────────────────────────────────────────

def test_the_catalog_is_fixed_and_says_what_it_writes():
    ids = [s.id for s in pov_seed.SEEDS]
    assert len(ids) == len(set(ids))
    for s in pov_seed.SEEDS:
        assert s.writes and s.description and s.product


def test_every_request_is_tagged_and_short():
    env_id = _env()
    reason = _with_env(env_id, lambda db, e: pov_seed.reason_for(e, pov_seed.get(_SEED)))
    assert reason.startswith("[pov-seed:pov-") and f":{_SEED}]" in reason
    assert len(reason) <= 100


def test_it_never_reaches_entitle_or_a_rotating_checkin():
    src = open(pov_seed.__file__, encoding="utf-8").read().lower()
    assert "import entitle" not in src and "entitle_service." not in src
    assert "rotateoncheckin" not in src


# ── availability and the accounts it touches ─────────────────────────────────

def test_it_needs_a_tenant_onboarded_accounts_and_an_active_pov():
    seed = pov_seed.get(_SEED)
    for kw, needle in (({"ps": False}, "no Password Safe tenant"),
                       ({"accounts": 0}, "onboarded"),
                       ({"status": "destroyed"}, "active")):
        why = _with_env(_env(**kw), lambda db, e: pov_seed.availability(db, e, seed))
        assert needle in why, (kw, why)
    assert _with_env(_env(), lambda db, e: pov_seed.availability(db, e, seed)) == ""


def test_only_this_povs_accounts_and_never_more_than_the_cap():
    env_id = _env(accounts=pov_seed.MAX_ACCOUNTS + 3)
    _env(accounts=2)  # another POV's accounts must not appear
    got = _with_env(env_id, lambda db, e: pov_seed._accounts(db, e))
    assert len(got) == pov_seed.MAX_ACCOUNTS
    assert all(200 <= a < 200 + pov_seed.MAX_ACCOUNTS + 3 for a, _ in got)


# ── enqueue ──────────────────────────────────────────────────────────────────

def test_an_unknown_seed_is_refused():
    try:
        _with_env(_env(), lambda db, e: pov_seed.enqueue(db, e, "drop_tables", by="se"))
    except pov_seed.SeedError:
        pass
    else:
        raise AssertionError("an unknown seed id was queued")


def test_a_second_press_returns_the_run_in_flight():
    env_id = _env()
    a = _with_env(env_id, lambda db, e: pov_seed.enqueue(db, e, _SEED, by="se"))
    b = _with_env(env_id, lambda db, e: pov_seed.enqueue(db, e, _SEED, by="se"))
    assert a == b


def test_a_run_moments_ago_refuses_another():
    env_id = _env()

    def mark(db, e):
        e.metadata_dict = {pov_seed.META_KEY: {_SEED: {"at": utcnow().isoformat()}}}
        db.commit()
    _with_env(env_id, mark)
    try:
        _with_env(env_id, lambda db, e: pov_seed.enqueue(db, e, _SEED, by="se"))
    except pov_seed.SeedError as exc:
        assert "moments ago" in str(exc)
    else:
        raise AssertionError("a second run inside the floor was queued")


# ── the job ──────────────────────────────────────────────────────────────────

def _run_job(env_id, results=None, raises=None):
    job_id = _with_env(env_id, lambda db, e: job_service.create_job(
        db, job_type=pov_seed.JOB_TYPE, created_by="se",
        metadata={"environment_id": env_id, "seed": _SEED}).id)
    saved = pov_seed._run_ps

    async def fake(db, env, seed):
        if raises:
            raise raises
        return results
    pov_seed._run_ps = fake
    try:
        asyncio.run(pov_seed.run_seed(job_id, {"environment_id": env_id, "seed": _SEED}))
    finally:
        pov_seed._run_ps = saved
    db = d.SessionLocal()
    try:
        job = db.get(d.Job, job_id)
        env = db.get(d.PovEnvironment, env_id)
        return job.status, job.error_message or "", env.metadata_dict[pov_seed.META_KEY][_SEED]
    finally:
        db.close()


def test_a_run_records_what_it_did_and_completes():
    status, _err, rec = _run_job(_env(), results=[
        {"account_id": "200", "ok": True, "detail": ""},
        {"account_id": "201", "ok": False, "detail": "4035 cap"}])
    assert status == "completed"
    assert rec["ok"] == 1 and rec["failed"] == 1 and rec["runs"] == 1
    assert "4035" in rec["error"]


def test_a_run_password_safe_refused_entirely_fails_in_its_words():
    status, err, rec = _run_job(_env(), results=[
        {"account_id": "200", "ok": False, "detail": "4031: needs the Requestor role"}])
    assert status == "failed" and "4031" in err, (status, err)
    assert rec["ok"] == 0


def test_a_tenant_that_will_not_resolve_fails_and_is_recorded():
    from web_dashboard.services import bt_tenant_service
    status, err, rec = _run_job(
        _env(), raises=bt_tenant_service.BTTenantError("tenant t-ps is inactive"))
    assert status == "failed" and "inactive" in err
    assert "inactive" in rec["error"]


def test_the_job_type_is_wired_into_the_worker():
    assert pov_seed.JOB_TYPE in jobs_worker.HANDLED_TYPES
    assert pov_seed.JOB_TYPE in jobs_worker.LIGHT_TYPES
    src = open(jobs_worker.__file__, encoding="utf-8").read()
    assert '"pov_env_seed": pov_seed.run_seed,' in src


# ── the routes ───────────────────────────────────────────────────────────────

class _Operator:
    username = "se1"
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


def test_the_routes_list_queue_audit_and_refuse():
    env_id = _env()
    c = _client()
    listed = c.get(f"/api/pov/managed/{env_id}/seeds").json()
    assert [s["id"] for s in listed["seeds"]] == [s.id for s in pov_seed.SEEDS]
    assert listed["seeds"][0]["unavailable"] == ""
    db = d.SessionLocal()
    try:
        before = db.query(d.AuditLog).filter(d.AuditLog.action == "pov_seed_requested").count()
    finally:
        db.close()
    res = c.post(f"/api/pov/managed/{env_id}/seeds/{_SEED}")
    assert res.status_code == 202, res.text
    db = d.SessionLocal()
    try:
        after = db.query(d.AuditLog).filter(d.AuditLog.action == "pov_seed_requested").count()
    finally:
        db.close()
    assert after == before + 1, "writing into a customer's tenant was not audited"
    assert c.post(f"/api/pov/managed/{_env(ps=False)}/seeds/{_SEED}").status_code == 409
    assert c.post("/api/pov/managed/nope/seeds/x").status_code == 404


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
