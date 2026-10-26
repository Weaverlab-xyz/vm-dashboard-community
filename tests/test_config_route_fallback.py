"""Agent-first Config Management, phase 1 (docs/design/remote-worker-and-agent.md).

  * **Fallback is opt-in per cloud route.** With ``fallback_to_worker`` on, a run routed to
    an offline agent goes to the remote worker as the no-route run would, recording the
    agent it fell back from. Off, it is refused as before. An on-premises route cannot
    have it: the worker has no path there.
  * **It never overrides a working agent.** An online agent, another agent's route, and a
    deployed agent whose host will be up for the booking all keep the run on the agent.
  * **Only a playbook falls back,** since the worker runs nothing else against a cloud VM.
  * **Live Output from an agent is one transaction per batch,** with contiguous sequence
    numbers and the per-job cap intact.
  * **The AWS runners' public IP follows ``ansible_ecs_assign_public_ip``,** on by default.

Runs under pytest, or standalone:  python tests/test_config_route_fallback.py
"""
import asyncio
import inspect
import json
import os
import sys
import tempfile
import types
from datetime import datetime, timedelta, timezone

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.environ.setdefault("DATABASE_URL", "sqlite:///" + os.path.join(
    tempfile.mkdtemp(prefix="route-fallback-"), "t.db").replace("\\", "/"))
os.environ.setdefault("JWT_SECRET_KEY", "x" * 32)

try:
    from fastapi import HTTPException
except ModuleNotFoundError as exc:  # pragma: no cover — no app deps installed
    print(f"SKIP: {exc}")
    sys.exit(0)

from sqlalchemy import event  # noqa: E402

from web_dashboard.api import config_mgmt as cm  # noqa: E402
from web_dashboard.database import (Base, ConfigMgmtRoute, Job, JobLog,  # noqa: E402
                                    RemoteAgent, SessionLocal, engine)
from web_dashboard.services import agent_host_service, agent_service  # noqa: E402
from web_dashboard.services import aws_service, config_service, job_service  # noqa: E402
from web_dashboard.services import config_mgmt_route_service as cmr  # noqa: E402

Base.metadata.create_all(bind=engine)

PRIVATE = "10.40.1.15"
PUBLIC = "54.1.2.3"


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _user():
    return types.SimpleNamespace(username="tester", is_effective_admin=True,
                                 workgroups_list=[], effective_permissions_dict={})


def _fresh():
    db = SessionLocal()
    for model in (ConfigMgmtRoute, Job, JobLog, RemoteAgent):
        db.query(model).delete()
    db.commit()
    return db


def _agent(db, name="vpc", *, online=False):
    db.add(RemoteAgent(
        id=f"agent-{name}", name=name, is_active=True, agent_version="2.11.0",
        public_key="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI" + name,
        last_seen_at=_utcnow() if online else _utcnow() - timedelta(hours=2),
        allowed_job_types=None, created_at=_utcnow()))
    db.commit()


def _deploy(db, job_id="deploy-1"):
    meta = {"instance_name": "web-1", "instance_id": "i-123", "private_ip": PRIVATE,
            "public_ip": PUBLIC}
    db.add(Job(id=job_id, job_type="ec2_deploy", status="completed", workgroup="team-a",
               extra_data=json.dumps(meta), created_at=_utcnow()))
    db.commit()


def _scene(*, fallback=True, online=False, agent="vpc"):
    db = _fresh()
    _agent(db, "vpc", online=online)
    if agent != "vpc":
        _agent(db, agent, online=online)
    _deploy(db)
    cmr.create(db, agent_id=f"agent-{agent}", cidr="10.40.0.0/16", scope="aws",
               fallback_to_worker=fallback)
    return db


def _request(**over):
    base = dict(asset="site.yml", asset_backend="s3", agent_id="agent-vpc",
                target_kind="vm", target_id="deploy-1", target=PRIVATE, cloud="aws",
                transport="ssh")
    base.update(over)
    return cm.RunRequest(**base)


class _RunPath:
    """Stub what /run touches outside this feature, restoring it afterwards."""
    def __enter__(self):
        self.saved = (cm._scope_check, cm._cfg, cm.ansible_local_service.get_configured_targets)
        cm._scope_check = lambda *a, **k: None
        cm._cfg = lambda key: {"ansible_runner": "ecs"}.get(key, "")
        cm.ansible_local_service.get_configured_targets = lambda db: []
        return self

    def __exit__(self, *exc):
        (cm._scope_check, cm._cfg, cm.ansible_local_service.get_configured_targets) = self.saved


def _run(db, payload):
    with _RunPath():
        return asyncio.run(cm.run_playbook(payload, db, _user()))


def _refused(db, payload):
    try:
        _run(db, payload)
    except HTTPException as exc:
        return exc
    raise AssertionError("the run was accepted")


# ── the fallback ──────────────────────────────────────────────────────────────

def test_an_offline_agent_with_fallback_sends_the_run_to_the_worker():
    db = _scene()
    out = _run(db, _request())
    job = db.query(Job).filter(Job.id == out["job_id"]).first()
    meta = job.metadata_dict
    assert job.job_type == "ansible_local" and job.agent_id in (None, ""), job.job_type
    assert meta["fallback_from_agent"] == "vpc"
    assert meta["target"] == PUBLIC and meta["cloud"] == "aws", meta
    lines = [line for _, line in job_service.get_job_logs(db, job.id)]
    assert any("'vpc' is not online" in line and "remote worker" in line for line in lines)


def test_without_fallback_an_offline_agent_is_still_refused():
    db = _scene(fallback=False)
    exc = _refused(db, _request())
    assert exc.status_code == 400 and "not online" in exc.detail
    assert db.query(Job).filter(Job.job_type == "ansible_local").count() == 0


def test_an_online_agent_keeps_the_run():
    db = _scene(online=True)
    assert cm._worker_fallback(_request(), db, _user()) is None


def test_another_agents_route_does_not_fall_back_for_this_agent():
    db = _scene(agent="other")
    assert cm._worker_fallback(_request(), db, _user()) is None


def test_a_deployed_agent_booked_into_its_window_stays_on_the_agent():
    db = _scene()
    saved = agent_host_service.for_agent, agent_host_service.booking_refusal
    try:
        agent_host_service.for_agent = lambda db, agent_id: object()
        agent_host_service.booking_refusal = lambda db, agent, at: ""
        assert cm._worker_fallback(_request(), db, _user()) is None
        agent_host_service.booking_refusal = lambda db, agent, at: "parked until Saturday"
        assert cm._worker_fallback(_request(), db, _user())["target"] == PUBLIC
    finally:
        agent_host_service.for_agent, agent_host_service.booking_refusal = saved


def test_only_a_playbook_falls_back():
    db = _scene()
    exc = _refused(db, _request(asset="fix.sh"))
    assert exc.status_code == 400 and "only playbooks" in exc.detail, exc.detail


def test_a_hypervisor_guest_or_cluster_run_is_never_a_fallback():
    # A cloud database can fall back: tests/test_config_mgmt_cloud_db_routes.py.
    db = _scene()
    assert cm._worker_fallback(_request(cloud=""), db, _user()) is None
    assert cm._worker_fallback(_request(target_kind="k8s"), db, _user()) is None


def test_a_vm_the_caller_cannot_see_is_not_a_fallback():
    db = _scene()
    stranger = types.SimpleNamespace(username="x", is_effective_admin=False,
                                     workgroups_list=["team-b"])
    assert cm._worker_fallback(_request(), db, stranger) is None


# ── the route flag ────────────────────────────────────────────────────────────

def test_an_on_premises_route_cannot_fall_back():
    db = _fresh()
    _agent(db)
    try:
        cmr.create(db, agent_id="agent-vpc", cidr="10.40.0.0/16", scope="onprem",
                   fallback_to_worker=True)
    except cmr.ConfigRouteError as exc:
        assert "cloud route" in str(exc)
    else:
        raise AssertionError("an on-premises route was given a worker fallback")


def test_the_flag_reads_back_and_moving_to_on_premises_drops_it():
    db = _fresh()
    _agent(db)
    out = cmr.create(db, agent_id="agent-vpc", cidr="10.40.0.0/16", scope="aws",
                     fallback_to_worker=True)
    assert out["fallback_to_worker"] is True
    assert cmr.load_table(db).routes[0].fallback_to_worker is True
    assert cmr.update(db, out["id"], label="x")["fallback_to_worker"] is True
    assert cmr.update(db, out["id"], scope="onprem")["fallback_to_worker"] is False
    try:
        cmr.update(db, out["id"], fallback_to_worker=True)
    except cmr.ConfigRouteError:
        pass
    else:
        raise AssertionError("an on-premises route was given a worker fallback")


def test_a_new_route_defaults_to_no_fallback():
    db = _fresh()
    _agent(db)
    assert cmr.create(db, agent_id="agent-vpc", cidr="10.40.0.0/16",
                      scope="aws")["fallback_to_worker"] is False


def test_the_migration_is_recorded():
    from web_dashboard import schema_migrations as sm
    mig = [m for m in sm.MIGRATIONS if m.name == "0007_config_route_fallback"]
    assert len(mig) == 1 and len(mig[0].steps) == 1


# ── Live Output ───────────────────────────────────────────────────────────────

def _commits(db, fn):
    count = [0]

    def _on(session):
        count[0] += 1
    event.listen(db, "after_commit", _on)
    try:
        fn()
    finally:
        event.remove(db, "after_commit", _on)
    return count[0]


def test_a_batch_of_agent_output_is_one_commit_with_contiguous_seqs():
    db = _fresh()
    job = Job(id="job-logs", job_type="agent_ansible", status="running",
              created_at=_utcnow())
    db.add(job)
    db.commit()
    first = [f"line {i}" for i in range(500)]
    assert _commits(db, lambda: agent_service.append_logs(db, job, first)) == 1
    agent_service.append_logs(db, job, ["more \x1b[31m", "end"])
    rows = job_service.get_job_logs(db, job.id)
    assert [s for s, _ in rows] == list(range(1, 503))
    assert rows[0][1] == "line 0" and rows[500][1] == "more [31m", rows[500]


def test_the_per_job_cap_still_holds():
    db = _fresh()
    job = Job(id="job-cap", job_type="agent_ansible", status="running",
              created_at=_utcnow())
    db.add(job)
    db.commit()
    saved = agent_service.MAX_LOG_LINES_PER_JOB
    agent_service.MAX_LOG_LINES_PER_JOB = 3
    try:
        assert agent_service.append_logs(db, job, ["a", "b"]) == 2
        assert agent_service.append_logs(db, job, ["c", "d", "e"]) == 1
        assert agent_service.append_logs(db, job, ["f"]) == 0
    finally:
        agent_service.MAX_LOG_LINES_PER_JOB = saved
    assert [line for _, line in job_service.get_job_logs(db, job.id)] == ["a", "b", "c"]


def test_a_seq_taken_meanwhile_is_retried_not_dropped():
    db = _fresh()
    other = SessionLocal()
    real_query = db.query
    raced = []

    def racing_query(*a, **k):
        # After this batch reads MAX(seq), another writer takes the next seq.
        q = real_query(*a, **k)
        if not raced and a and a[0] is JobLog.seq:
            raced.append(1)
            other.add(JobLog(job_id="job-race", seq=1, line="other", created_at=_utcnow()))
            other.commit()
        return q
    db.query = racing_query
    try:
        job_service.append_job_logs(db, "job-race", ["mine-1", "mine-2"])
    finally:
        db.query = real_query
        other.close()
    assert job_service.get_job_logs(db, "job-race") == [
        (1, "other"), (2, "mine-1"), (3, "mine-2")]


# ── the AWS runner's public IP ────────────────────────────────────────────────

def test_the_aws_runners_public_ip_follows_the_setting_and_defaults_on():
    try:
        assert aws_service._runner_public_ip() == "ENABLED"
        config_service.set("ansible_ecs_assign_public_ip", "0")
        assert aws_service._runner_public_ip() == "DISABLED"
        config_service.set("ansible_ecs_assign_public_ip", "1")
        assert aws_service._runner_public_ip() == "ENABLED"
    finally:
        config_service.set("ansible_ecs_assign_public_ip", "")
    for fn in (aws_service._run_ecs_ansible_sync, aws_service._run_ecs_k8s_sync,
               aws_service._run_ecs_ansible_local_sync):
        src = inspect.getsource(fn)
        assert '"assignPublicIp": _runner_public_ip()' in src, fn.__name__
        assert '"assignPublicIp": "ENABLED"' not in src, fn.__name__


if __name__ == "__main__":
    fns = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {e!r}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
