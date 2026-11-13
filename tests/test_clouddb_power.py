"""Stop / start for cloud databases (services/clouddb_power).

What these pin, against the real models on SQLite with the cloud SDKs replaced by a
scripted fake backend:

  * the state machine: available -> stopping -> stopped -> starting -> available, the
    transitional status written at ENQUEUE time, and a failed job putting the row back;
  * idempotency: a stop of a stopped row is a no-op, a repeat request returns the job
    already in flight, an opposite request while one runs is refused;
  * refusals: provisioning / decommissioning rows, registered rows, providers with no
    stop (Atlas, Azure SQL Database), an unknown action;
  * the runner reads before it acts, so a retry or an already-stopped instance issues
    nothing, and it waits out a transition before requesting;
  * needs_restop: AWS/Azure after 7 days, never Cloud SQL, never without a stop time;
  * decommission from stopped: allowed (refused only mid-transition), and GCP/Azure are
    started before the destroy while AWS is not;
  * the stopped row stays alive to the expiry reaper and the Gateway's reference count;
  * the stale-job reconciler puts a mid-transition row back rather than failing it;
  * the worker registration (HANDLED_TYPES, MEDIUM tier, dispatch branch);
  * the route: 404 for a row the caller cannot see, 400 / 409 / 202 otherwise.

Runs under pytest, or standalone:  python tests/test_clouddb_power.py
"""
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
from _schema import build_schema  # noqa: E402

build_schema("clouddb-power")

from web_dashboard.database import CloudDatabase, Job, SessionLocal  # noqa: E402
from web_dashboard.services import clouddb_power as cp  # noqa: E402
from web_dashboard.services import cloud_database_service as cds  # noqa: E402
from web_dashboard.services import job_service  # noqa: E402

# Never sleep for real in the runner's poll loop.
cp.POLL_INTERVAL_S = 0.0


class FakeBackend:
    """A scripted instance: ``states`` is consumed one read at a time (the last value
    repeats), and every request is recorded."""

    def __init__(self, states, fail_on=None):
        self.states = list(states)
        self.calls = []
        self.fail_on = fail_on

    def read(self):
        self.calls.append("read")
        if self.fail_on == "read":
            raise RuntimeError("cloud said no")
        return self.states.pop(0) if len(self.states) > 1 else self.states[0]

    def request_stop(self):
        self.calls.append("stop")
        if self.fail_on == "stop":
            raise RuntimeError("InvalidDBInstanceState")

    def request_start(self):
        self.calls.append("start")


def _db():
    return SessionLocal()


def _row(db, **kw):
    fields = dict(engine="postgres", provider="rds", cloud="aws", region="us-east-2",
                  status="available", source="provisioned", created_by="alice")
    fields.update(kw)
    row = CloudDatabase(**fields)
    db.add(row)
    db.commit()
    return row


def _run(db, row, job_id, action, backend):
    cp._BACKEND_FACTORY = lambda _r: backend
    try:
        asyncio.run(cp.run_power(db, db_id=row.id, job_id=job_id, action=action))
    finally:
        cp._BACKEND_FACTORY = None
    db.expire_all()
    return db.query(CloudDatabase).filter(CloudDatabase.id == row.id).first()


def _job(db, job_id):
    db.expire_all()
    return db.query(Job).filter(Job.id == job_id).first()


# ── state machine ────────────────────────────────────────────────────────────

def test_stop_then_start_round_trip():
    db = _db()
    row = _row(db)
    out = cds.start_power(db, row.id, "stop", "alice")
    assert out["status"] == "stopping" and out["job_id"]
    job = _job(db, out["job_id"])
    assert job.job_type == "clouddb_power"
    assert job.metadata_dict["action"] == "stop" and job.metadata_dict["db_id"] == row.id
    assert job.workgroup is None and job.created_by == "alice"

    backend = FakeBackend(["running", "transition", "stopped"])
    row = _run(db, row, out["job_id"], "stop", backend)
    assert row.status == "stopped"
    assert backend.calls.count("stop") == 1
    st = cp.read_state(row)
    assert st["state"] == "stopped" and st["stopped_at"]
    assert "prior_status" not in st and "last_error" not in st
    assert _job(db, out["job_id"]).status == "completed"

    out = cds.start_power(db, row.id, "start", "alice")
    assert out["status"] == "starting"
    backend = FakeBackend(["stopped", "transition", "running"])
    row = _run(db, row, out["job_id"], "start", backend)
    assert row.status == "available"
    assert backend.calls.count("start") == 1
    st = cp.read_state(row)
    assert st["state"] == "running" and "stopped_at" not in st
    db.close()


def test_noops_and_in_flight_dedupe():
    db = _db()
    row = _row(db)
    assert cds.start_power(db, row.id, "start")["noop"] is True     # already running
    first = cds.start_power(db, row.id, "stop")
    again = cds.start_power(db, row.id, "stop")
    assert again["job_id"] == first["job_id"], "a repeat must return the job in flight"
    try:
        cds.start_power(db, row.id, "start")
        raise AssertionError("an opposite request while a stop runs must be refused")
    except cp.PowerRefused:
        pass
    _run(db, row, first["job_id"], "stop", FakeBackend(["stopped"]))
    assert cds.start_power(db, row.id, "stop")["noop"] is True      # already stopped
    db.close()


def test_refusals():
    db = _db()
    for status in ("provisioning", "decommissioning", "failed"):
        row = _row(db, status=status)
        for action in ("stop", "start"):
            try:
                cds.start_power(db, row.id, action)
                raise AssertionError(f"{action} from {status} must be refused")
            except cp.PowerRefused:
                pass
        db.refresh(row)
        assert row.status == status, "a refusal must not touch the row"
    for kw in ({"source": "registered"}, {"provider": "atlas", "engine": "mongodb"},
               {"provider": "sql_database", "cloud": "azure", "engine": "sqlserver"},
               {"provider": "autonomous", "cloud": "oci", "engine": "oracle"}):
        row = _row(db, **kw)
        try:
            cds.start_power(db, row.id, "stop")
            raise AssertionError(f"{kw} must be unsupported")
        except cp.PowerUnsupported:
            pass
    row = _row(db)
    try:
        cds.start_power(db, row.id, "hibernate")
        raise AssertionError("an unknown action must be refused")
    except cp.PowerUnsupported:
        pass
    try:
        cds.start_power(db, "no-such-id", "stop")
        raise AssertionError("an unknown id must raise CloudDatabaseError")
    except cds.CloudDatabaseError:
        pass
    db.close()


def test_a_failed_stop_puts_the_row_back():
    db = _db()
    row = _row(db)
    out = cds.start_power(db, row.id, "stop")
    row = _run(db, row, out["job_id"], "stop", FakeBackend(["running"], fail_on="stop"))
    assert row.status == "available", row.status
    assert "InvalidDBInstanceState" in cp.read_state(row)["last_error"]
    job = _job(db, out["job_id"])
    assert job.status == "failed" and "stop failed" in (job.error_message or "")
    db.close()


def test_runner_reads_before_acting():
    # Already stopped in the cloud (a retry after a dead job): nothing is requested.
    b = FakeBackend(["stopped"])
    asyncio.run(cp.drive(b, "stop", call=_sync_call, interval_s=0))
    assert b.calls == ["read"]
    # Mid-transition (RDS starting itself back up): wait it out, THEN request.
    b = FakeBackend(["transition", "transition", "running", "transition", "stopped"])
    asyncio.run(cp.drive(b, "stop", call=_sync_call, interval_s=0))
    assert b.calls.index("stop") == 3 and b.calls.count("stop") == 1
    # An unknown cloud state is refused, not acted on.
    b = FakeBackend(["incompatible-parameters"])
    try:
        asyncio.run(cp.drive(b, "stop", call=_sync_call, interval_s=0))
        raise AssertionError("an unknown state must not be acted on")
    except RuntimeError:
        pass
    assert "stop" not in b.calls


async def _sync_call(fn, *a, **k):
    return fn(*a, **k)


def test_stale_transition_is_retryable_and_reconciled():
    db = _db()
    row = _row(db)
    out = cds.start_power(db, row.id, "stop")
    job = _job(db, out["job_id"])
    job.status = "failed"            # the worker died; reconcile_stale_jobs failed it
    db.commit()
    job_service._flip_resource_row(db, job)
    db.commit()
    db.refresh(row)
    assert row.status == "available", "a dead power job puts the row back, never failed"
    # And with the row left mid-transition (no flip), a new request is still accepted.
    row.status = "stopping"
    db.commit()
    again = cds.start_power(db, row.id, "stop")
    assert again["job_id"] != out["job_id"]
    db.close()


# ── needs_restop ─────────────────────────────────────────────────────────────

def test_needs_restop():
    now = datetime(2026, 11, 13, tzinfo=timezone.utc)

    def row(cloud, status="stopped", days=8, stamp=True):
        st = {"state": "stopped"}
        if stamp:
            st["stopped_at"] = (now - timedelta(days=days)).isoformat()
        return SimpleNamespace(cloud=cloud, status=status, power_state=json.dumps(st))

    assert cp.needs_restop(row("aws"), now) is True
    assert cp.needs_restop(row("azure"), now) is True
    assert cp.needs_restop(row("aws", days=6), now) is False
    assert cp.needs_restop(row("gcp", days=30), now) is False, "Cloud SQL stays stopped"
    assert cp.needs_restop(row("aws", status="available"), now) is False
    assert cp.needs_restop(row("aws", stamp=False), now) is False
    assert cp.needs_restop(SimpleNamespace(cloud="aws", status="stopped",
                                           power_state=None), now) is False


def test_restop_reissues_a_stop_on_a_stopped_row():
    db = _db()
    row = _row(db)
    out = cds.start_power(db, row.id, "stop")
    _run(db, row, out["job_id"], "stop", FakeBackend(["stopped"]))
    assert cds.start_power(db, row.id, "stop").get("noop") is True
    re = cds.start_power(db, row.id, "stop", restop=True)
    assert re.get("job_id") and _job(db, re["job_id"]).metadata_dict.get("restop") is True
    db.close()


# ── decommission from stopped ────────────────────────────────────────────────

def test_decommission_from_stopped():
    db = _db()
    row = _row(db, status="stopped",
               power_state=json.dumps({"state": "stopped", "stopped_at": "2026-11-01T00:00:00+00:00"}))
    out = cds.start_decommission(db, row.id, created_by="alice")
    assert out["job_id"]
    db.refresh(row)
    assert row.status == "decommissioning"
    # Mid-transition is refused: the destroy would race the power job.
    busy = _row(db, status="stopping")
    try:
        cds.start_decommission(db, busy.id)
        raise AssertionError("a decommission mid-stop must be refused")
    except cp.PowerRefused:
        pass
    db.close()


def test_teardown_starts_gcp_and_azure_but_not_aws():
    db = _db()
    stopped = json.dumps({"state": "stopped"})
    for cloud, provider, want_start in (("gcp", "cloudsql", True),
                                        ("azure", "flexibleserver", True),
                                        ("aws", "rds", False)):
        row = _row(db, cloud=cloud, provider=provider, status="decommissioning",
                   power_state=stopped)
        job = job_service.create_job(db, job_type="clouddb_decommission", created_by="t",
                                     metadata={"db_id": row.id})
        backend = FakeBackend(["stopped", "running"])
        cp._BACKEND_FACTORY = lambda _r, b=backend: b
        try:
            warn = asyncio.run(cp.ensure_running_for_teardown(db, row, job.id))
        finally:
            cp._BACKEND_FACTORY = None
        assert warn is None
        assert ("start" in backend.calls) is want_start, (cloud, backend.calls)
        if want_start:
            db.refresh(row)
            assert cp.read_state(row)["state"] == "running"
    # A running row is never touched.
    row = _row(db, cloud="gcp", provider="cloudsql", status="decommissioning")
    backend = FakeBackend(["running"])
    cp._BACKEND_FACTORY = lambda _r: backend
    try:
        assert asyncio.run(cp.ensure_running_for_teardown(db, row, "j")) is None
    finally:
        cp._BACKEND_FACTORY = None
    assert backend.calls == []
    db.close()


def test_run_decommission_starts_a_stopped_gcp_instance_before_destroy():
    """The ordering inside run_decommission, read as source: the start must come before
    the terraform destroy, or the destroy fails on the stopped instance's children."""
    src = open(os.path.join(os.path.dirname(_HERE), "web_dashboard", "services",
                            "cloud_database_service.py"), encoding="utf-8").read()
    body = src[src.index("async def run_decommission"):]
    body = body[:body.index("\ndef list_databases")]
    assert body.index("ensure_running_for_teardown") < body.index("await terraform.destroy(")


# ── the rest of the app treats stopped as alive ──────────────────────────────

def test_stopped_is_reapable_and_keeps_the_gateway():
    from web_dashboard.services import expiry_policy, jumpoint_host_service
    assert "stopped" in expiry_policy._REAPABLE_STATES["database"]
    assert "stopping" not in expiry_policy._REAPABLE_STATES["database"]
    db = _db()
    before = jumpoint_host_service._active_db_count(db, "gcp")
    _row(db, cloud="gcp", provider="cloudsql", status="stopped")
    assert jumpoint_host_service._active_db_count(db, "gcp") == before + 1, \
        "a stopped database must keep the shared Gateway alive"
    db.close()


def test_serialize_exposes_power_fields():
    db = _db()
    row = _row(db, status="stopped",
               power_state=json.dumps({"stopped_at": "2026-11-01T00:00:00+00:00"}))
    out = cds._serialize(row)
    assert out["powerable"] is True and out["stopped_at"].startswith("2026-11-01")
    assert cds._serialize(_row(db, source="registered"))["powerable"] is False
    db.close()


# ── worker + route ───────────────────────────────────────────────────────────

def test_worker_registration():
    from web_dashboard import jobs_worker as jw
    assert "clouddb_power" in jw.HANDLED_TYPES
    assert "clouddb_power" in jw.MEDIUM_TYPES
    assert "clouddb_power" not in jw.LIGHT_TYPES + jw.HEAVY_TYPES
    src = open(jw.__file__, encoding="utf-8").read()
    assert 'job_type == "clouddb_power"' in src and "cloud_database_service.run_power" in src


def test_route():
    from fastapi import HTTPException
    from web_dashboard.api import cloud_databases as api
    from web_dashboard.services import config_service
    config_service.set("cloud_database_enabled", "true")
    db = _db()
    alice = SimpleNamespace(username="alice", is_effective_admin=False, workgroups_list=[])
    mallory = SimpleNamespace(username="mallory", is_effective_admin=False, workgroups_list=[])
    row = _row(db)

    def call(user, action):
        return api.power_database(row.id, api.PowerRequest(action=action), db=db,
                                  current_user=user)

    for user, action, code in ((mallory, "stop", 404),       # not theirs: hidden
                               (alice, "hibernate", 400)):
        try:
            call(user, action)
            raise AssertionError(f"expected {code}")
        except HTTPException as exc:
            assert exc.status_code == code, (exc.status_code, exc.detail)
    out = call(alice, "stop")
    assert out["job_id"] and out["status"] == "stopping"
    try:
        call(alice, "start")
        raise AssertionError("expected 409")
    except HTTPException as exc:
        assert exc.status_code == 409
    # The route is gated on the existing write scope.
    import inspect
    src = inspect.getsource(api.power_database)
    assert 'require_permission("cloud_database", "write")' in src
    db.close()


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
