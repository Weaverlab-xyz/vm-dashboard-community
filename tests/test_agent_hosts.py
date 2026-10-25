"""Remote agents the dashboard deploys into a cloud (services/agent_host_service.py,
api/agent_hosts.py): what the host is given, when it exists, and what it unlocks.

What has to hold, and what each test pins:

  * **The policy is data, never text.** Only normalised ranges reach it, Config Management
    is the only job type, and a range that smuggles YAML or a refused network is refused.
  * **The host gets one single-use secret and nothing reusable.** A code or a join token,
    decoded into a file, never on a command line; the SPIRE branch is taken exactly when
    attestation is on and the dashboard's server is in use.
  * **The schedule.** Window mode is up from lead before each occurrence to grace after it;
    a host started by hand ahead of its window stays up through it; until mode stops once;
    nothing is queued twice.
  * **Booking ahead.** A parked window-mode agent accepts a run booked into its window and
    refuses any other, naming when it is next up. An ordinary offline agent is unchanged.
  * **The NAT stays while an agent is up**, on AWS and GCP.
  * **Stop waits for running jobs**, up to the grace.
  * **Start and stop** mint and kill the identity, and record and clear the host.
  * **The API** deploys, routes, lists, starts, stops and deletes, and the agent routes
    refuse to orphan a host.

Runs under pytest, or standalone:  python tests/test_agent_hosts.py
"""
import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.environ.setdefault("DATABASE_URL",
                      "sqlite:///" + os.path.join(tempfile.mkdtemp(), "hosts.db").replace("\\", "/"))
os.environ.setdefault("JWT_SECRET_KEY", "x" * 32)

try:
    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient
except ModuleNotFoundError as exc:
    print(f"SKIP: {exc}")
    sys.exit(0)

from web_dashboard.database import (AgentHost, Base, ChangeWindow, ConfigMgmtRoute,  # noqa: E402
                                    Job, RemoteAgent, SessionLocal, engine, get_db)
from web_dashboard.services import agent_host_service as ahs  # noqa: E402
from web_dashboard.services import (agent_service, config_service,  # noqa: E402
                                    dashboard_spire, job_service)

Base.metadata.create_all(bind=engine)

URL = "https://dash.example.com"


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _fresh():
    db = SessionLocal()
    for model in (AgentHost, ConfigMgmtRoute, Job, RemoteAgent, ChangeWindow):
        db.query(model).delete()
    db.commit()
    config_service.set(agent_service.AUDIENCE_CONFIG, URL)
    config_service.set("spire_attest_enabled", "false")
    return db


def _window(db, *, start="02:00", minutes=240, enabled=True):
    w = ChangeWindow(name=f"win-{start}", start_at_local=start, duration_minutes=minutes,
                     timezone="UTC", schedule_days="1111111", enabled=enabled)
    db.add(w)
    db.commit()
    return w


def _deploy(db, *, mode="always", window=None, until=None, name="vpc-a-agent", **kw):
    lifecycle = ahs.validate_lifecycle(
        db, mode=mode, until_at=until, change_window_id=window.id if window else "",
        lead_minutes=kw.pop("lead", 20), grace_minutes=kw.pop("grace", 15))
    return ahs.create(db, name=name, cloud=kw.pop("cloud", "aws"),
                      region=kw.pop("region", "us-east-2"),
                      cidrs=kw.pop("cidrs", ["10.40.0.0/16"]), lifecycle=lifecycle, **kw)


def _host(**kw):
    """A plain stand-in for the pure schedule functions."""
    base = dict(mode="window", status="parked", lead_minutes=20, grace_minutes=15,
                until_at=None, next_start_at=None, next_stop_at=None, host_id=None)
    base.update(kw)
    return SimpleNamespace(**base)


def _win(start="02:00", minutes=240):
    return SimpleNamespace(start_at_local=start, duration_minutes=minutes, timezone="UTC",
                           schedule_days="1111111", enabled=True)


# ── the policy ────────────────────────────────────────────────────────────────

def test_the_policy_is_config_management_only_for_the_ranges():
    import yaml
    doc = yaml.safe_load(ahs.policy_yaml(["10.40.0.0/16", "10.41.0.0/16"], 3))
    assert doc["job_types"] == ["agent_ansible"]
    assert [t["cidr"] for t in doc["targets"]] == ["10.40.0.0/16", "10.41.0.0/16"]
    assert doc["ansible"]["targets"] == doc["targets"]
    assert doc["ansible"]["max_concurrent"] == 3
    assert doc["ansible"]["enabled"] is True
    assert set(doc) == {"job_types", "targets", "ansible"}


def test_a_range_that_is_not_a_range_is_refused():
    for bad in ("10.40.0.0/16\njob_types: [agent_discover]", "127.0.0.0/8", "169.254.0.0/16",
                "10.40.0.1/16", "not-a-cidr", ""):
        try:
            ahs.normalize_cidrs([bad] if bad else [])
        except ahs.AgentHostError:
            continue
        raise AssertionError(f"{bad!r} was accepted")


def test_runs_at_once_outside_the_agent_ceiling_is_refused():
    for bad in (0, 9):
        try:
            ahs.policy_yaml(["10.40.0.0/16"], bad)
        except ahs.AgentHostError:
            continue
        raise AssertionError(f"{bad} runs at once was accepted")


def test_the_name_becomes_a_vm_name_so_it_is_strict():
    assert ahs.validate_name("VPC-A-Agent") == "vpc-a-agent"
    for bad in ("a", "-x-agent", "agent-", "has space", "x" * 50, "a;rm"):
        try:
            ahs.validate_name(bad)
        except ahs.AgentHostError:
            continue
        raise AssertionError(f"{bad!r} was accepted")


def test_the_agent_address_must_be_pinned_and_https():
    _fresh()
    assert ahs.dashboard_url() == URL
    config_service.set(agent_service.AUDIENCE_CONFIG, "http://dash.example.com")
    try:
        ahs.dashboard_url()
    except ahs.AgentHostError as exc:
        assert "TLS" in str(exc)
    else:
        raise AssertionError("a plain-HTTP agent address was accepted")
    config_service.set(agent_service.AUDIENCE_CONFIG, URL)


# ── the bootstrap ─────────────────────────────────────────────────────────────

def _decoded_files(script):
    import base64
    import re
    return {m.group(2): base64.b64decode(m.group(1)).decode()
            for m in re.finditer(r"echo (\S+) \| base64 -d > \"\$D/([^\"]+)\"", script)}


def test_the_code_reaches_a_file_and_never_a_command_line():
    code = "ENROL-abc123-secret"
    script = ahs.bootstrap_script(url=URL, policy=ahs.policy_yaml(["10.40.0.0/16"], 2),
                                  material={"mode": "code", "code": code})
    assert code not in script, "the code is in the script in the clear"
    files = _decoded_files(script)
    assert files["code/enroll.code"] == code
    assert "agent_ansible" in files["etc/policy.yaml"]
    assert "AGENT_ENROLLMENT_CODE_FILE=/etc/dashboard-agent/code/enroll.code" in script
    assert "AGENT_ENROLLMENT_CODE=" not in script
    assert 'rm -f "$D/code/enroll.code"' in script
    import re
    assert not re.search(r"(^|\s)(-p|--publish)\s+\d", script), "the agent publishes a port"


def test_the_join_token_is_spent_and_never_in_the_agents_environment():
    token = "join-token-0f3c"
    script = ahs.bootstrap_script(
        url=URL, policy=ahs.policy_yaml(["10.40.0.0/16"], 2),
        material={"mode": "spire", "join_token": token, "trust_domain": "dash.example.com",
                  "bootstrap_pem": "-----BEGIN CERTIFICATE-----\n",
                  "server_address": "spire.example.com", "server_port": 8081})
    assert token not in script
    files = _decoded_files(script)
    assert files["spire/join.token"] == token
    agent_line = next(line for line in script.splitlines()
                      if line.startswith("docker run -d --name dashboard-agent "))
    assert "SPIRE_JOIN_TOKEN" not in agent_line
    assert "AGENT_SPIFFE_JWT_FILE" in agent_line
    assert script.index('rm -f "$D/spire/join.token"') > script.index("--name dashboard-agent-spire")
    assert ahs.SPIRE_AGENT_IMAGE.split("@")[1].startswith("sha256:")


def test_a_spire_address_that_is_not_plain_is_refused():
    try:
        ahs.bootstrap_script(url=URL, policy="x", material={
            "mode": "spire", "join_token": "t", "trust_domain": "td",
            "bootstrap_pem": "", "server_address": "spire;curl evil", "server_port": 8081})
    except ahs.AgentHostError:
        return
    raise AssertionError("a shell-shaped SPIRE address was accepted")


# ── identity ──────────────────────────────────────────────────────────────────

def test_identity_is_a_code_unless_spire_attestation_is_on_and_in_use():
    db = _fresh()
    host, agent = _deploy(db)
    assert agent.enroll_code_hash is None, "the code minted with the row was kept"
    material = ahs.identity_material(db, agent)
    assert material["mode"] == "code" and material["code"]
    assert agent.enroll_code_hash, "the code was not recorded"

    config_service.set("spire_attest_enabled", "true")
    saved = (dashboard_spire.server_in_use, dashboard_spire.migrate_agent,
             dashboard_spire.server_address, dashboard_spire.server_port)
    try:
        dashboard_spire.server_in_use = lambda: False
        assert ahs.identity_material(db, agent)["mode"] == "code"
        dashboard_spire.server_in_use = lambda: True
        dashboard_spire.migrate_agent = lambda db, a: {
            "join_token": "jt", "trust_domain": "dash.example.com", "bootstrap_pem": "PEM"}
        dashboard_spire.server_address = lambda: "spire.example.com:9081"
        dashboard_spire.server_port = lambda: 8081
        m = ahs.identity_material(db, agent)
        assert m == {"mode": "spire", "join_token": "jt", "trust_domain": "dash.example.com",
                     "bootstrap_pem": "PEM", "server_address": "spire.example.com",
                     "server_port": 9081}
    finally:
        (dashboard_spire.server_in_use, dashboard_spire.migrate_agent,
         dashboard_spire.server_address, dashboard_spire.server_port) = saved
        config_service.set("spire_attest_enabled", "false")


def test_a_deployed_agent_is_granted_config_management_and_passes_the_version_gate():
    db = _fresh()
    _host_row, agent = _deploy(db)
    assert agent_service.allowed_job_types(agent) == {"agent_ansible"} or \
        list(agent_service.allowed_job_types(agent)) == ["agent_ansible"]
    assert agent_service.supports_ansible(agent)


# ── the schedule ──────────────────────────────────────────────────────────────

DAY = datetime(2026, 10, 20)          # a Tuesday; the window is daily 02:00–06:00 UTC


def test_a_window_host_starts_lead_minutes_before_and_not_sooner():
    w = _win()
    assert ahs.plan(_host(), w, DAY.replace(hour=1, minute=39))[0] is None
    action, up, down = ahs.plan(_host(), w, DAY.replace(hour=1, minute=40))
    assert action == "start"
    assert up == DAY.replace(hour=1, minute=40) and down == DAY.replace(hour=6, minute=15)


def test_a_window_host_stops_grace_minutes_after_the_window():
    w = _win()
    running = _host(status="running", next_stop_at=DAY.replace(hour=6, minute=15))
    assert ahs.plan(running, w, DAY.replace(hour=6, minute=14))[0] is None
    assert ahs.plan(running, w, DAY.replace(hour=6, minute=15))[0] == "stop"


def test_a_host_started_by_hand_ahead_of_its_window_stays_up_through_it():
    w = _win()
    started = DAY.replace(hour=12)                   # well outside any window
    up_until = ahs.window_span(_host(), w, started)[1]
    assert up_until == (DAY + timedelta(days=1)).replace(hour=6, minute=15)
    running = _host(status="running", next_stop_at=up_until)
    assert ahs.plan(running, w, DAY.replace(hour=13))[0] is None
    assert ahs.plan(running, w, up_until)[0] == "stop"


def test_a_host_stopped_by_hand_in_its_window_stays_down_until_the_next():
    w = _win()
    tomorrow = (DAY + timedelta(days=1)).replace(hour=1, minute=40)
    held = _host(next_start_at=tomorrow)
    action, next_start, _ = ahs.plan(held, w, DAY.replace(hour=3))
    assert action is None and next_start == tomorrow
    assert ahs.plan(held, w, tomorrow)[0] == "start"


def test_the_hold_is_set_only_inside_the_window():
    db = _fresh()
    w = _window(db, start="02:00")
    host, _ = _deploy(db, mode="window", window=w)
    ahs.hold_until_next_window(db, host, DAY.replace(hour=12))
    assert host.next_start_at is None
    ahs.hold_until_next_window(db, host, DAY.replace(hour=3))
    assert host.next_start_at == (DAY + timedelta(days=1)).replace(hour=1, minute=40)


def test_a_window_too_nearly_over_is_not_worth_a_host():
    assert ahs.plan(_host(), _win(), DAY.replace(hour=6, minute=10))[0] is None


def test_a_host_that_is_already_starting_is_not_started_again():
    for status in ("provisioning", "starting", "stopping", "error"):
        assert ahs.plan(_host(status=status), _win(), DAY.replace(hour=3))[0] is None


def test_a_failed_host_still_in_the_cloud_is_taken_down_after_its_window():
    failed = _host(status="error", host_id="i-1")
    assert ahs.plan(failed, _win(), DAY.replace(hour=3))[0] is None, "taken down mid-window"
    assert ahs.plan(failed, _win(), DAY.replace(hour=7))[0] == "stop"
    assert ahs.plan(_host(status="error"), _win(), DAY.replace(hour=7))[0] is None


def test_until_stops_once_at_its_time():
    until = DAY.replace(hour=9)
    h = _host(mode="until", status="running", until_at=until)
    assert ahs.plan(h, None, until - timedelta(minutes=1))[0] is None
    assert ahs.plan(h, None, until)[0] == "stop"
    assert ahs.plan(_host(mode="until", status="parked", until_at=until), None, until)[0] is None


def test_always_is_never_scheduled():
    assert ahs.plan(_host(mode="always", status="running"), None, DAY)[0] is None


def test_the_sweep_queues_once_and_records_the_next_times():
    db = _fresh()
    w = _window(db, start="02:00")
    host, _ = _deploy(db, mode="window", window=w)
    now = DAY.replace(hour=1, minute=45)
    assert ahs.sweep(db, now) == [(host.name, "start")]
    db.refresh(host)
    assert host.status == "provisioning" and host.last_job_id
    assert host.next_start_at == DAY.replace(hour=1, minute=40)
    assert ahs.sweep(db, now) == [], "a second pass queued the start again"
    assert db.query(Job).filter(Job.job_type == "agent_host_start").count() == 1


def test_the_schedule_sweep_has_work_while_a_host_follows_a_window():
    from web_dashboard.services import schedule_sweeper
    db = _fresh()
    assert not schedule_sweeper.has_work(db)
    _deploy(db, mode="window", window=_window(db))
    assert schedule_sweeper.has_work(db)


# ── booking ahead ─────────────────────────────────────────────────────────────

class _Payload:
    def __init__(self, agent_id, **kw):
        self.agent_id = agent_id
        self.target_kind = "vm"
        self.target_id = "deploy-1"
        self.connection_id = ""
        self.cloud = "aws"
        self.target = "10.40.1.15"
        self.transport = ""
        self.port = 0
        self.ansible_user = ""
        self.run_at = kw.get("run_at", "")
        self.run_timezone = "UTC"
        self.change_window_id = kw.get("change_window_id", "")


def _gate(db, payload):
    from web_dashboard.api.config_mgmt import _resolve_agent_target
    user = SimpleNamespace(is_effective_admin=True, workgroups_list=[], username="t")
    try:
        _resolve_agent_target(payload, db, user)
    except HTTPException as exc:
        return exc.detail
    return ""


def test_a_run_booked_into_the_window_passes_the_online_gate():
    db = _fresh()
    w = _window(db, start="02:00")
    _host_row, agent = _deploy(db, mode="window", window=w)
    detail = _gate(db, _Payload(agent.id, change_window_id=w.id))
    # Past the online gate: whatever refuses it now is about the target, not the agent.
    assert "not online" not in detail and "parked" not in detail, detail


def test_a_run_outside_the_window_is_refused_naming_when_it_is_next_up():
    db = _fresh()
    w = _window(db, start="02:00")
    _host_row, agent = _deploy(db, mode="window", window=w)
    noon = (_utcnow() + timedelta(days=1)).strftime("%Y-%m-%dT12:00")
    for payload in (_Payload(agent.id), _Payload(agent.id, run_at=noon)):
        detail = _gate(db, payload)
        assert "parked until its change window" in detail and "next up" in detail, detail


def test_an_ordinary_offline_agent_is_refused_as_before():
    db = _fresh()
    db.add(RemoteAgent(id="agent-plain", name="plain", is_active=True, agent_version="2.9.0",
                       allowed_job_types=json.dumps(["agent_ansible"]),
                       created_at=_utcnow()))
    db.commit()
    assert "is not online" in _gate(db, _Payload("agent-plain"))


# ── NAT reclaim ───────────────────────────────────────────────────────────────

def test_the_nat_is_kept_while_a_deployed_agent_is_up():
    from web_dashboard.services import (aws_service, gcp_nat_service, gcp_service,
                                        jumpoint_host_service, nat_instance_service)
    db = _fresh()
    host, _ = _deploy(db)
    calls = []

    async def _rec(*a, **k):
        calls.append(a)
        return []
    saved = (config_service.get_bool, jumpoint_host_service._active_ec2_count,
             gcp_nat_service._active_gce_count, aws_service.find_instances_by_tag,
             aws_service.delete_default_route, gcp_nat_service._resolve,
             gcp_service.delete_firewall_rule)
    try:
        config_service.get_bool = lambda key, default=False: True
        jumpoint_host_service._active_ec2_count = lambda db: 0
        gcp_nat_service._active_gce_count = lambda db, region: 0
        aws_service.find_instances_by_tag = _rec
        aws_service.delete_default_route = _rec
        gcp_nat_service._resolve = lambda region: calls.append(("gcp", region)) or None
        for status, expect_calls in (("running", False), ("parked", True)):
            host.status = status
            db.commit()
            calls.clear()
            asyncio.run(nat_instance_service.reclaim_nat_instance(db, "us-east-2"))
            assert bool(calls) is expect_calls, (status, calls)
        host.cloud, host.region, host.status = "gcp", "us-central1", "running"
        db.commit()
        calls.clear()
        asyncio.run(gcp_nat_service.reclaim_vm_nat(db, "us-central1"))
        assert not calls, "the Cloud NAT was reclaimed under a live agent"
        asyncio.run(gcp_nat_service.reclaim_vm_nat(db, "europe-west1"))
        assert calls, "another region's NAT was held by this agent"
    finally:
        (config_service.get_bool, jumpoint_host_service._active_ec2_count,
         gcp_nat_service._active_gce_count, aws_service.find_instances_by_tag,
         aws_service.delete_default_route, gcp_nat_service._resolve,
         gcp_service.delete_firewall_rule) = saved


# ── start, stop, drain ────────────────────────────────────────────────────────

class _FakeCloud:
    """Stands in for launch/destroy and egress, and enrols the agent as the host would."""

    def __init__(self):
        self.launched, self.destroyed, self.scripts = [], [], []

    def __enter__(self):
        self._saved = (ahs.launch, ahs.destroy, ahs._ensure_egress, ahs._release_egress,
                       ahs._ONLINE_POLL_S)

        async def launch(host, script):
            self.launched.append(host.name)
            self.scripts.append(script)
            s = SessionLocal()
            a = s.query(RemoteAgent).filter(RemoteAgent.id == host.agent_id).first()
            a.public_key = "ssh-ed25519 AAAAfresh"
            a.last_seen_at = _utcnow()
            s.commit()
            s.close()
            return {"host_id": "i-" + host.name, "zone": ""}

        async def destroy(host):
            self.destroyed.append(host.host_id)

        async def nothing(*a, **k):
            return None
        ahs.launch, ahs.destroy = launch, destroy
        ahs._ensure_egress, ahs._release_egress = nothing, nothing
        ahs._ONLINE_POLL_S = 0
        return self

    def __exit__(self, *exc):
        (ahs.launch, ahs.destroy, ahs._ensure_egress, ahs._release_egress,
         ahs._ONLINE_POLL_S) = self._saved


def _run(db, host, action):
    job_id = ahs.enqueue(db, host, action)
    meta = json.loads(db.query(Job).filter(Job.id == job_id).first().extra_data)
    asyncio.run(ahs.run(db, job_id=job_id, meta=meta))
    db.expire_all()
    return db.query(Job).filter(Job.id == job_id).first()


def test_start_then_stop_records_the_host_and_kills_the_identity():
    db = _fresh()
    host, agent = _deploy(db)
    with _FakeCloud() as cloud:
        job = _run(db, host, "start")
        assert job.status == "completed", job.error_message if hasattr(job, "error_message") else job
        host = ahs.get(db, host.id)
        assert host.status == "running" and host.host_id == "i-vpc-a-agent"
        assert "enroll.code" in cloud.scripts[0]

        job = _run(db, host, "stop")
        assert job.status == "completed"
        host = ahs.get(db, host.id)
        assert host.status == "parked" and host.host_id is None
        assert cloud.destroyed == ["i-vpc-a-agent"]
        agent = db.query(RemoteAgent).filter(RemoteAgent.id == agent.id).first()
        assert agent.public_key is None and agent.enroll_code_hash is None
        assert agent.is_active, "stopping revoked the agent"


def test_a_failed_launch_cleans_up_and_marks_the_host():
    db = _fresh()
    host, agent = _deploy(db)
    with _FakeCloud() as cloud:
        async def boom(host, script):
            raise RuntimeError("InsufficientInstanceCapacity")
        ahs.launch = boom
        job = _run(db, host, "start")
        assert job.status == "failed"
        host = ahs.get(db, host.id)
        assert host.status == "error" and "InsufficientInstanceCapacity" in host.error
        assert cloud.destroyed, "nothing cleaned up after a failed launch"
        agent = db.query(RemoteAgent).filter(RemoteAgent.id == agent.id).first()
        assert agent.enroll_code_hash is None, "a failed start left a live code"


def test_delete_takes_the_host_down_and_removes_agent_and_routes():
    from web_dashboard.services import config_mgmt_route_service as cmr
    db = _fresh()
    host, agent = _deploy(db)
    cmr.create(db, agent_id=agent.id, cidr="10.40.0.0/16", include_cloud_vms=True)
    with _FakeCloud() as cloud:
        _run(db, host, "start")
        job = _run(db, ahs.get(db, host.id), "delete")
        assert job.status == "completed"
        assert cloud.destroyed
    assert ahs.get(db, host.id) is None
    assert db.query(RemoteAgent).filter(RemoteAgent.id == agent.id).first() is None
    assert db.query(ConfigMgmtRoute).count() == 0


def test_stop_waits_for_running_jobs_up_to_the_grace():
    db = _fresh()
    host, agent = _deploy(db, grace=1)
    db.add(Job(id="busy-1", job_type="agent_ansible", status="running", agent_id=agent.id,
               created_at=_utcnow()))
    db.commit()
    saved = ahs._STOP_DRAIN_POLL_S
    ahs._STOP_DRAIN_POLL_S = 0.02

    async def scenario():
        drain = asyncio.ensure_future(ahs._drain(db, host, "none"))
        await asyncio.sleep(0.1)
        assert not drain.done(), "stop did not wait for the running job"
        s = SessionLocal()
        s.query(Job).filter(Job.id == "busy-1").update({Job.status: "completed"})
        s.commit()
        s.close()
        await asyncio.wait_for(drain, 2)

    try:
        asyncio.run(scenario())
        host.grace_minutes = 0
        db.add(Job(id="busy-2", job_type="agent_ansible", status="running",
                   agent_id=agent.id, created_at=_utcnow()))
        db.commit()
        asyncio.run(asyncio.wait_for(ahs._drain(db, host, "none"), 1))
    finally:
        ahs._STOP_DRAIN_POLL_S = saved


# ── the API ───────────────────────────────────────────────────────────────────

class _Admin:
    username = "tester"
    is_admin = True
    is_effective_admin = True
    effective_permissions_dict = {}


class _Reader:
    username = "reader"
    is_admin = False
    is_effective_admin = False
    effective_permissions_dict = {"agents": ["read"]}


def _client(user=_Admin):
    from web_dashboard.api import agent as agent_api
    from web_dashboard.api import agent_hosts
    from web_dashboard.api.auth import get_current_user
    app = FastAPI()
    app.include_router(agent_hosts.router)
    app.include_router(agent_api.admin_router)

    def _db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_user] = lambda: user()
    return TestClient(app)


def test_deploy_routes_the_ranges_and_queues_the_start():
    db = _fresh()
    c = _client()
    r = c.post("/api/agent-hosts", json={"name": "vpc-b-agent", "cloud": "aws",
                                         "region": "us-east-2", "ranges": ["10.50.0.0/16"]})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["job_id"] and body["status"] == "provisioning"
    assert body["routes"] == ["10.50.0.0/16"]
    route = db.query(ConfigMgmtRoute).first()
    assert route.include_cloud_vms and route.agent_id == body["agent_id"]
    assert c.get("/api/agent-hosts").json()["hosts"][0]["name"] == "vpc-b-agent"


def test_a_range_already_routed_is_left_and_reported():
    from web_dashboard.services import config_mgmt_route_service as cmr
    db = _fresh()
    _first, other = _deploy(db, name="first-agent")
    cmr.create(db, agent_id=other.id, cidr="10.50.0.0/16", include_cloud_vms=True)
    r = _client().post("/api/agent-hosts", json={"name": "second-agent", "cloud": "aws",
                                                 "region": "us-east-2",
                                                 "ranges": ["10.50.0.0/16", "10.51.0.0/16"]})
    assert r.status_code == 201, r.text
    assert r.json()["routes"] == ["10.51.0.0/16"]
    assert r.json()["route_errors"] == [
        "10.50.0.0/16 is already routed to agent 'first-agent'; left as it is."]


def test_the_options_name_the_agent_address_problem():
    _fresh()
    assert _client().get("/api/agent-hosts/options").json()["dashboard_url"] == URL
    config_service.set(agent_service.AUDIENCE_CONFIG, "")
    body = _client().get("/api/agent-hosts/options").json()
    assert body["dashboard_url"] == "" and "no pinned agent address" in body["dashboard_url_error"]
    config_service.set(agent_service.AUDIENCE_CONFIG, URL)


def test_a_window_deploy_waits_for_its_window():
    db = _fresh()
    w = _window(db, start=(_utcnow() + timedelta(hours=6)).strftime("%H:00"), minutes=60)
    r = _client().post("/api/agent-hosts", json={
        "name": "win-agent", "cloud": "gcp", "region": "us-central1",
        "ranges": ["10.60.0.0/16"], "mode": "window", "change_window_id": w.id})
    assert r.status_code == 201, r.text
    assert r.json()["job_id"] == "" and r.json()["status"] == "parked"
    assert r.json()["next_start_at"]


def test_the_api_refuses_bad_input_and_needs_write():
    _fresh()
    c = _client()
    for bad in ({"cloud": "oci"}, {"ranges": ["127.0.0.0/8"]}, {"mode": "window"},
                {"mode": "until", "until_at": "2001-01-01T00:00:00Z"}, {"max_concurrent": 9}):
        body = {"name": "bad-agent", "cloud": "aws", "region": "us-east-2",
                "ranges": ["10.70.0.0/16"], **bad}
        assert c.post("/api/agent-hosts", json=body).status_code == 400, bad
    r = _client(_Reader).post("/api/agent-hosts", json={
        "name": "nope-agent", "cloud": "aws", "region": "us-east-2", "ranges": ["10.70.0.0/16"]})
    assert r.status_code == 403
    assert _client(_Reader).get("/api/agent-hosts").status_code == 200


def test_start_stop_and_delete_are_refused_while_one_is_in_progress():
    db = _fresh()
    host, _ = _deploy(db)
    c = _client()
    assert c.post(f"/api/agent-hosts/{host.id}/start").status_code == 202
    assert c.post(f"/api/agent-hosts/{host.id}/start").status_code == 409
    assert c.delete(f"/api/agent-hosts/{host.id}").status_code == 409


def test_a_deployed_agents_record_cannot_be_removed_from_under_its_host():
    db = _fresh()
    host, agent = _deploy(db)
    host.status, host.host_id = "running", "i-1"
    db.commit()
    c = _client()
    r = c.delete(f"/api/agents/{agent.id}")
    assert r.status_code == 200 and r.json()["host_stop_job"], r.text
    r = c.delete(f"/api/agents/{agent.id}/record")
    assert r.status_code == 409 and "Deployed agents" in r.json()["detail"]


def test_the_worker_dispatches_the_three_job_types():
    from web_dashboard import jobs_worker
    for t in ("agent_host_start", "agent_host_stop", "agent_host_delete"):
        assert t in jobs_worker.HANDLED_TYPES and t in jobs_worker.LIGHT_TYPES
    assert job_service is not None


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            import traceback
            traceback.print_exc()
            print(f"FAIL {fn.__name__}: {e!r}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
