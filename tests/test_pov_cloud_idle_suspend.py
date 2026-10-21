"""Suspending a cloud POV, by hand and on an idle timer.

Two bugs and one feature, pinned here:

  * **Suspend never worked on a cloud.** The POV page has one Suspend button and it sends
    "suspended" on every platform; `pov_cloud_env.set_runstate` accepted only running and
    stopped, so every press failed its job with "a cloud instance has no suspend-to-RAM".
    A cloud stop is the suspend, and the row records the "stopped" the cloud reached.
  * **The reconcile pass blanked a cloud POV's idle window.** It copied `suspend_on_idle`
    off every platform listing, and a cloud listing never carries one — so the row's only
    copy of the window would be gone ten minutes after it was set.
  * **The dashboard is the idle timer on AWS.** CloudWatch CPU and network, every VM, the
    whole window; missing data is never idle; it only ever ENQUEUES the power job.

Runs against SQLite in a temp file with stubbed drivers. No network, no cloud.

Runs under pytest, or standalone:
    python tests/test_pov_cloud_idle_suspend.py
"""
import asyncio
import os
import sys
import tempfile
from datetime import datetime, timezone

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-pov-idle-suspend")

_DB_FD, _DB_PATH = tempfile.mkstemp(suffix=".db")
os.close(_DB_FD)
os.environ["DATABASE_URL"] = f"sqlite:///{_DB_PATH}"

from web_dashboard import database as db_mod  # noqa: E402
from web_dashboard.database import Base, Job, PovEnvironment  # noqa: E402
from web_dashboard.services import lab_platforms  # noqa: E402
from web_dashboard.services import pov_cloud_aws  # noqa: E402
from web_dashboard.services import pov_cloud_env as cloud  # noqa: E402
from web_dashboard.services import pov_reconcile as pr  # noqa: E402

Base.metadata.create_all(bind=db_mod.engine)

PERIOD = cloud.IDLE_PERIOD_S
NOW = 1_800_000_000.0


# ── 1. Suspend is a stop ─────────────────────────────────────────────────────

class _Driver:
    def __init__(self, vm_state="stopped"):
        self.calls = []
        self.vm_state = vm_state

    async def power(self, env_id, region, action):
        self.calls.append(action)

    async def read_environment(self, env_id, region):
        return {"id": env_id, "runstate": self.vm_state,
                "vms": [{"runstate": self.vm_state}], "vm_count": 1}


def _with_driver(drv, fn):
    saved = (cloud.driver, cloud.recorded_region)
    cloud.driver = lambda _c: drv
    cloud.recorded_region = lambda _e, _c: "us-east-1"
    try:
        return asyncio.run(fn())
    finally:
        cloud.driver, cloud.recorded_region = saved


def test_suspended_is_a_stop_on_a_cloud():
    """The bug in the screenshot: the Suspend button's word was refused outright."""
    drv = _Driver()
    out = _with_driver(drv, lambda: cloud.set_runstate("aws", "povenv-acme", "suspended"))
    assert drv.calls == ["stop"], drv.calls
    assert out["runstate"] == "stopped", out


def test_halted_is_a_stop_too():
    drv = _Driver()
    _with_driver(drv, lambda: cloud.set_runstate("gcp", "povenv-acme", "halted"))
    assert drv.calls == ["stop"], drv.calls


def test_waiting_for_suspended_settles_on_stopped():
    """`run_env_power` waits on the word it was given. Waiting for a "suspended" no cloud
    VM ever reports would time out after fifteen minutes on every press."""
    drv = _Driver("stopped")
    out = _with_driver(drv, lambda: cloud.wait_for_runstate(
        "aws", "povenv-acme", "suspended", timeout_s=1))
    assert out["runstate"] == "stopped", out


def test_running_is_still_a_start():
    drv = _Driver("running")
    _with_driver(drv, lambda: cloud.set_runstate("aws", "povenv-acme", "running"))
    assert drv.calls == ["start"], drv.calls


def test_an_unknown_runstate_is_still_refused():
    drv = _Driver()
    try:
        _with_driver(drv, lambda: cloud.set_runstate("aws", "povenv-acme", "rebooting"))
    except cloud.CloudEnvError:
        pass
    else:
        raise AssertionError("an unknown runstate reached the driver")
    assert drv.calls == [], drv.calls


# ── 2. The verdict ───────────────────────────────────────────────────────────

def _series(n_periods, *, cpu=1.0, net=10_000.0, newest_missing=0, start_offset=0):
    """``n_periods`` five-minute points ending now, oldest first."""
    pts = []
    for i in range(n_periods):
        at = NOW - (n_periods - i) * PERIOD + start_offset
        pts.append({"at": at, "cpu": cpu, "net": net})
    return pts[:len(pts) - newest_missing] if newest_missing else pts


def _verdict(activity, window=3600):
    return cloud.idle_verdict(activity, window_s=window, now_ts=NOW)


def test_quiet_for_the_whole_window_is_idle():
    idle, why = _verdict({"dc": _series(13), "win": _series(13)})
    assert idle, why


def test_the_newest_periods_may_still_be_in_flight():
    """CloudWatch trails by up to ten minutes; a lab must not stay up forever for it."""
    idle, why = _verdict({"dc": _series(13, newest_missing=2)})
    assert idle, why


def test_one_busy_cpu_period_keeps_it_up():
    pts = _series(13)
    pts[-1]["cpu"] = 40.0
    idle, why = _verdict({"dc": _series(13), "win": pts})
    assert not idle and "win" in why and "CPU" in why, why


def test_network_traffic_keeps_it_up():
    """An RDP session through PRA is mostly network; CPU alone would miss it."""
    pts = _series(13)
    pts[5]["net"] = 8 * 1024 * 1024
    idle, why = _verdict({"win": pts})
    assert not idle and "MB" in why, why


def test_a_vm_started_inside_the_window_is_not_idle():
    """A stopped instance reports nothing. Started by hand 20 minutes ago, it has no
    history for the rest of the hour, and must get a full window first."""
    idle, why = _verdict({"win": _series(4)})
    assert not idle, why


def test_a_vm_with_a_gap_at_the_start_is_not_idle():
    pts = _series(13)[3:]
    idle, why = _verdict({"win": pts})
    assert not idle, why


def test_missing_metrics_are_never_idle():
    assert _verdict({})[0] is False
    assert _verdict({"win": []})[0] is False
    pts = _series(13)
    for p in pts:
        p["net"] = None
    assert _verdict({"win": pts})[0] is False, "CPU alone was read as the whole picture"


def test_one_quiet_vm_does_not_speak_for_a_busy_one():
    busy = _series(13, cpu=60.0)
    assert _verdict({"dc": _series(13), "win": busy})[0] is False


def test_points_older_than_the_window_do_not_count():
    """A busy hour yesterday must not keep a lab up today, and a quiet one must not
    stand in for coverage it does not give."""
    old = [{"at": NOW - 7200 - i * PERIOD, "cpu": 90.0, "net": 1e9} for i in range(5)]
    idle, why = _verdict({"win": old + _series(13)})
    assert idle, why


# ── 3. The AWS read ──────────────────────────────────────────────────────────

class _CW:
    def __init__(self, results_pages):
        self.pages = list(results_pages)
        self.calls = []

    def get_metric_data(self, **kwargs):
        self.calls.append(kwargs)
        return self.pages.pop(0)


def _ts(at):
    return datetime.fromtimestamp(at, tz=timezone.utc)


def test_aws_reads_cpu_and_both_network_directions_per_instance():
    insts = [
        {"InstanceId": "i-1", "State": {"Name": "running"},
         "Tags": [{"Key": "Name", "Value": "dc"}]},
        {"InstanceId": "i-2", "State": {"Name": "stopped"},
         "Tags": [{"Key": "Name", "Value": "win"}]},
    ]
    t = 1_700_000_000
    cw = _CW([
        {"MetricDataResults": [
            {"Id": "cpu0", "Timestamps": [_ts(t)], "Values": [2.5]},
            {"Id": "nin0", "Timestamps": [_ts(t)], "Values": [100.0]}],
         "NextToken": "more"},
        {"MetricDataResults": [
            {"Id": "nout0", "Timestamps": [_ts(t)], "Values": [50.0]}]},
    ])
    saved = (pov_cloud_aws._describe_sync, pov_cloud_aws.aws_service.aws_client)
    pov_cloud_aws._describe_sync = lambda region, filters: insts
    pov_cloud_aws.aws_service.aws_client = lambda svc, region, pinned=False: cw
    try:
        out = pov_cloud_aws._idle_activity_sync("povenv-acme", "us-east-1", 3600)
    finally:
        pov_cloud_aws._describe_sync, pov_cloud_aws.aws_service.aws_client = saved
    assert out["dc"] == [{"at": float(t), "cpu": 2.5, "net": 150.0}], out
    assert out["win"] == [], "a stopped instance must be listed, with nothing to show"
    queries = cw.calls[0]["MetricDataQueries"]
    assert len(queries) == 3, "only the running instance is asked about"
    assert {q["MetricStat"]["Metric"]["MetricName"] for q in queries} == {
        "CPUUtilization", "NetworkIn", "NetworkOut"}
    assert all(q["MetricStat"]["Period"] == PERIOD for q in queries)
    assert cw.calls[1].get("NextToken") == "more", "the second page was never asked for"


def test_two_vms_with_one_name_are_kept_apart():
    """Merged under one key, two half-covered series look like one fully covered VM."""
    insts = [{"InstanceId": f"i-{n}", "State": {"Name": "stopped"},
              "Tags": [{"Key": "Name", "Value": "web"}]} for n in (1, 2)]
    saved = pov_cloud_aws._describe_sync
    pov_cloud_aws._describe_sync = lambda region, filters: insts
    try:
        out = pov_cloud_aws._idle_activity_sync("povenv-acme", "us-east-1", 3600)
    finally:
        pov_cloud_aws._describe_sync = saved
    assert len(out) == 2, out


# ── 4. The sweep ─────────────────────────────────────────────────────────────

def _session():
    return db_mod.SessionLocal()


def _clean(db):
    db.query(Job).delete()
    db.query(PovEnvironment).delete()
    db.commit()


def _env(db, **kw):
    kw.setdefault("platform", "aws")
    kw.setdefault("name", "acme")
    kw.setdefault("status", "active")
    kw.setdefault("platform_environment_id", "povenv-acme")
    kw.setdefault("runstate", "running")
    kw.setdefault("suspend_on_idle_seconds", 3600)
    row = PovEnvironment(**kw)
    db.add(row)
    db.commit()
    return row


class _IdleAdapter:
    def __init__(self, idle=True):
        self.idle = idle
        self.asked = []

    async def idle_check(self, env_id, window_s):
        self.asked.append((env_id, window_s))
        return self.idle, "" if self.idle else "busy"


def _sweep(db, adapter):
    saved = lab_platforms.adapter
    lab_platforms.adapter = lambda _p: adapter
    try:
        return asyncio.run(pr.sweep_idle(db))
    finally:
        lab_platforms.adapter = saved


def _power_jobs(db):
    return [j for j in db.query(Job).all() if j.job_type == "pov_env_power"]


def test_an_idle_aws_pov_gets_a_stop_job():
    db = _session()
    try:
        _clean(db)
        row = _env(db)
        ad = _IdleAdapter(True)
        assert _sweep(db, ad) == 1
        assert ad.asked == [("povenv-acme", 3600)], ad.asked
        jobs = _power_jobs(db)
        assert len(jobs) == 1, jobs
        assert jobs[0].metadata_dict == {"environment_id": row.id, "runstate": "stopped"}
        assert jobs[0].created_by == pr.IDLE_ACTOR
    finally:
        _clean(db)
        db.close()


def test_a_busy_pov_is_left_alone():
    db = _session()
    try:
        _clean(db)
        _env(db)
        assert _sweep(db, _IdleAdapter(False)) == 0
        assert _power_jobs(db) == []
    finally:
        _clean(db)
        db.close()


def test_the_sweep_only_asks_about_rows_it_could_act_on():
    """No metrics read for a row with no window, one that is not running, or Skytap —
    whose own timer this would be second-guessing."""
    db = _session()
    try:
        _clean(db)
        _env(db, name="off", platform_environment_id="povenv-off",
             suspend_on_idle_seconds=0)
        _env(db, name="down", platform_environment_id="povenv-down", runstate="stopped")
        _env(db, name="sky", platform="skytap", platform_environment_id="123")
        _env(db, name="gone", platform_environment_id="povenv-gone", status="destroyed")
        ad = _IdleAdapter(True)
        assert _sweep(db, ad) == 0
        assert ad.asked == [], ad.asked
    finally:
        _clean(db)
        db.close()


def test_a_power_job_in_flight_is_not_raced():
    db = _session()
    try:
        _clean(db)
        row = _env(db)
        pr.job_service.create_job(
            db, job_type="pov_env_power", created_by="someone",
            metadata={"environment_id": row.id, "runstate": "running"})
        ad = _IdleAdapter(True)
        assert _sweep(db, ad) == 0
        assert ad.asked == [], "the SE pressed Start; metrics are not asked to overrule it"
    finally:
        _clean(db)
        db.close()


def test_a_failing_metrics_read_skips_the_row_and_not_the_sweep():
    class _Boom(_IdleAdapter):
        async def idle_check(self, env_id, window_s):
            if env_id == "povenv-bad":
                raise RuntimeError("AccessDenied: cloudwatch:GetMetricData")
            return await super().idle_check(env_id, window_s)

    db = _session()
    try:
        _clean(db)
        _env(db, name="bad", platform_environment_id="povenv-bad")
        _env(db, name="good", platform_environment_id="povenv-good")
        assert _sweep(db, _Boom(True)) == 1
    finally:
        _clean(db)
        db.close()


def test_reconcile_keeps_a_cloud_rows_idle_window():
    """The row is the only copy on a cloud. A listing that never mentions
    `suspend_on_idle` must not blank it."""
    class _List:
        def configured(self):
            return True

        async def list_environments(self):
            return [{"id": "povenv-acme", "runstate": "running"}]

    db = _session()
    try:
        _clean(db)
        row = _env(db)
        saved = lab_platforms.adapter
        lab_platforms.adapter = lambda _p: _List()
        try:
            asyncio.run(pr.reconcile(db, "aws"))
        finally:
            lab_platforms.adapter = saved
        db.refresh(row)
        assert row.suspend_on_idle_seconds == 3600, row.suspend_on_idle_seconds
    finally:
        _clean(db)
        db.close()


def test_the_row_says_who_runs_the_idle_timer():
    assert pr._idle_suspend_by("skytap") == "platform"
    assert pr._idle_suspend_by("aws") == "dashboard"
    assert pr._idle_suspend_by("azure") == ""


# ── 5. The registry ──────────────────────────────────────────────────────────

def test_a_platform_claiming_a_dashboard_idle_timer_can_read_metrics():
    """`sweep_idle` asks the adapter; the adapter asks the driver. A claim with neither
    would log "could not read" every ten minutes and never stop anything."""
    for name in lab_platforms.VALID_PLATFORMS:
        caps = lab_platforms.capabilities(name)
        assert not (caps["idle_suspend"] and caps["dashboard_idle_suspend"]), (
            f"{name} claims two idle timers")
        if not caps["dashboard_idle_suspend"]:
            continue
        assert name in lab_platforms.CLOUD_PLATFORMS, name
        assert hasattr(lab_platforms.adapter(name), "idle_check"), name
        assert hasattr(cloud.driver(name), "idle_activity"), name


# ── 6. The endpoint ──────────────────────────────────────────────────────────

def test_the_endpoint_sets_clears_and_refuses():
    try:
        import fastapi  # noqa: F401
    except ModuleNotFoundError:
        print("SKIP: fastapi not installed (endpoint half only)")
        return
    from fastapi import HTTPException
    from web_dashboard.api import pov as pov_api

    class _User:
        username = "se"

    db = _session()
    try:
        _clean(db)
        row = _env(db, suspend_on_idle_seconds=None)
        sky = _env(db, name="sky", platform="skytap", platform_environment_id="123")
        req = pov_api.IdleSuspendRequest
        out = pov_api.set_idle_suspend(row.id, req(seconds=7200), db=db, current_user=_User())
        assert out == {"suspend_on_idle_seconds": 7200}
        db.refresh(row)
        assert row.suspend_on_idle_seconds == 7200
        pov_api.set_idle_suspend(row.id, req(seconds=0), db=db, current_user=_User())
        db.refresh(row)
        assert row.suspend_on_idle_seconds is None
        for env_id, seconds, status in ((row.id, 300, 400), (sky.id, 3600, 409)):
            try:
                pov_api.set_idle_suspend(env_id, req(seconds=seconds), db=db,
                                         current_user=_User())
            except HTTPException as exc:
                assert exc.status_code == status, (exc.status_code, exc.detail)
            else:
                raise AssertionError(f"{seconds}s on {env_id} was accepted")
    finally:
        _clean(db)
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
            print(f"FAIL {fn.__name__}: {e!r}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    try:
        os.unlink(_DB_PATH)
    except OSError:
        pass
    sys.exit(1 if failures else 0)
