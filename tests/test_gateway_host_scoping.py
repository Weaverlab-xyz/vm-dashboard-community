"""The idle teardown must only ever touch the gateway it manages.

Background: the dashboard auto-ensures ONE shared BeyondTrust Gateway host per cloud and
terminates it once nothing is using it. That was safe while one gateway could exist. The
moment operators can deploy their own gateways into the same ECS cluster it stops being
safe, because the AWS teardown stopped *every* running task in the cluster:

    for t in await aws_service.list_ecs_tasks(region, cluster):
        if t.get("lastStatus") in ("RUNNING", "PENDING", "PROVISIONING"):
            await aws_service.stop_ecs_jumpoint_task(region, cluster, t["taskArn"])

An idle-teardown of the managed gateway would have silently stopped the operator's
gateways too — no error, no job, just sessions dropping. These tests pin the scoping that
prevents it, and assert the unscoped query really would have caught both, so the
protection is demonstrably the scoping and not an artifact of the fixture.

`jumpoint_host_service` has no import-time dependencies (every cloud import is
function-local), so this runs the real functions against a fake ECS rather than reading
source.

Run: python tests/test_gateway_host_scoping.py   (or under pytest)
"""
import asyncio
import os
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

REGION = "us-east-2"
CLUSTER = "bt-gateway"
FAMILY = "bt-jumpoint"

MANAGED_HOST = "i-managed0000000001"
USER_HOST = "i-user00000000000002"
MANAGED_TASK = f"arn:aws:ecs:{REGION}:1:task/{CLUSTER}/managed"
USER_TASK = f"arn:aws:ecs:{REGION}:1:task/{CLUSTER}/user"


class FakeAws:
    """Two gateway hosts in one cluster, each running its own gateway task."""

    def __init__(self):
        self.stopped: list[str] = []
        self.terminated: list[str] = []
        self.ran_tasks: list[dict] = []

    async def list_ecs_tasks(self, region, cluster, include_stopped=False):
        return [
            {"taskArn": MANAGED_TASK, "lastStatus": "RUNNING",
             "taskDefinitionArn": f"arn:aws:ecs:::task-definition/{FAMILY}:7",
             "containerInstanceArn": "ci/managed"},
            {"taskArn": USER_TASK, "lastStatus": "RUNNING",
             "taskDefinitionArn": f"arn:aws:ecs:::task-definition/{FAMILY}:7",
             "containerInstanceArn": "ci/user"},
        ]

    async def list_container_instances(self, region, cluster):
        return [
            {"arn": "ci/managed", "status": "ACTIVE", "ec2_instance_id": MANAGED_HOST},
            {"arn": "ci/user", "status": "ACTIVE", "ec2_instance_id": USER_HOST},
        ]

    async def find_instances_by_tag(self, region, name_tag, states):
        # Only the managed gateway carries the managed Name tag.
        if name_tag == "managed-gateway":
            return [{"instance_id": MANAGED_HOST, "public_ip": "203.0.113.10"}]
        return []

    async def stop_ecs_jumpoint_task(self, region, cluster, task_arn):
        self.stopped.append(task_arn)

    async def terminate_instance(self, region, instance_id):
        self.terminated.append(instance_id)

    async def run_ecs_jumpoint_task(self, **kw):
        self.ran_tasks.append(kw)
        return f"arn:aws:ecs:::task/{CLUSTER}/new"


def _load(fake, active=0):
    """Import the service with a fake aws_service and stubbed config reads.

    ``active`` is the reference count the teardown sees. The real counters query the
    ORM, and their import failure is swallowed by the teardown's best-effort
    try/except — which would make every teardown assertion below pass without the
    teardown ever running. Stubbing them is what keeps these tests honest."""
    for mod in [m for m in sys.modules if m.startswith("web_dashboard.services.jumpoint")]:
        del sys.modules[mod]
    stub = types.ModuleType("web_dashboard.services.aws_service")
    for name in ("list_ecs_tasks", "list_container_instances", "find_instances_by_tag",
                 "stop_ecs_jumpoint_task", "terminate_instance", "run_ecs_jumpoint_task"):
        setattr(stub, name, getattr(fake, name))
    sys.modules["web_dashboard.services.aws_service"] = stub

    import importlib
    m = importlib.import_module("web_dashboard.services.jumpoint_host_service")
    cfg = {"bt_ecs_host_name": "managed-gateway", "bt_ecs_task_family": FAMILY,
           "bt_ecs_launch_type": "EC2"}
    m._cfg = lambda key: cfg.get(key, "")
    m._aws_region_cfg = lambda region: {
        "ecs_cluster": CLUSTER,
        "jumpoint_subnet_id": "subnet-abc",
        "jumpoint_security_group_id": "sg-abc",
    }
    m._active_db_count = lambda db, cloud=None: active
    m._active_ec2_count = lambda db: 0
    m._active_k8s_count = lambda db, cloud=None: 0
    m._active_vdesktop_count = lambda db, cloud=None: 0
    return m


_DB = object()  # never touched — the counters are stubbed


# ── the scoping itself ────────────────────────────────────────────────────────

def test_live_task_query_scoped_to_a_host_returns_only_that_hosts_task():
    fake = FakeAws()
    m = _load(fake)
    live = asyncio.run(m._live_gateway_tasks(REGION, CLUSTER, FAMILY, MANAGED_HOST))
    assert [t["taskArn"] for t in live] == [MANAGED_TASK], (
        "host-scoped query returned another host's task")


def test_the_unscoped_query_really_does_return_both():
    """Without this the test above proves nothing — it would pass just as well if the
    fixture only ever had one task."""
    fake = FakeAws()
    m = _load(fake)
    live = asyncio.run(m._live_gateway_tasks(REGION, CLUSTER, FAMILY))
    assert {t["taskArn"] for t in live} == {MANAGED_TASK, USER_TASK}, (
        "the cluster fixture must contain a task on each host for the scoping to matter")


# ── the teardown that used to stop everything ─────────────────────────────────

def test_idle_teardown_stops_only_the_managed_gateways_task():
    """The regression that motivated this file."""
    fake = FakeAws()
    m = _load(fake)
    asyncio.run(m._teardown_jumpoint_host_if_idle_aws(_DB, REGION))
    assert USER_TASK not in fake.stopped, (
        "idle teardown stopped a user-deployed gateway's task — the operator's sessions "
        "drop with no error and no job to explain it")
    assert fake.stopped == [MANAGED_TASK], f"expected only the managed task, got {fake.stopped}"


def test_idle_teardown_terminates_only_the_managed_host():
    fake = FakeAws()
    m = _load(fake)
    asyncio.run(m._teardown_jumpoint_host_if_idle_aws(_DB, REGION))
    assert fake.terminated == [MANAGED_HOST], (
        f"expected only the managed host terminated, got {fake.terminated}")


def test_teardown_keeps_everything_when_a_resource_is_still_active():
    fake = FakeAws()
    m = _load(fake, active=1)
    asyncio.run(m._teardown_jumpoint_host_if_idle_aws(_DB, REGION))
    assert not fake.stopped and not fake.terminated, (
        "teardown ran while a resource was still using the gateway")


# ── a second gateway gets its own task, pinned to its own host ────────────────

def test_ensure_task_pins_the_task_to_the_host_that_asked_for_it():
    """Several hosts in one cluster: ECS default placement is free to stack two tasks
    on one host and leave another bare, so each gateway names its own host."""
    class NoTasks(FakeAws):
        async def list_ecs_tasks(self, region, cluster, include_stopped=False):
            return []

    fake = NoTasks()
    m = _load(fake)
    asyncio.run(m._ensure_task(REGION, "deploy-key", USER_HOST))
    assert len(fake.ran_tasks) == 1
    assert fake.ran_tasks[0]["host_instance_id"] == USER_HOST, (
        "the task was not pinned to the host that requested it")


def test_ensure_task_is_a_noop_when_that_host_already_runs_one():
    fake = FakeAws()
    m = _load(fake)
    asyncio.run(m._ensure_task(REGION, "deploy-key", USER_HOST))
    assert not fake.ran_tasks, "started a second task on a host that already had one"


def test_a_busy_neighbour_does_not_satisfy_a_bare_host():
    """The old check returned early if ANY task was live in the cluster, so the second
    gateway host would never get one."""
    class OnlyManagedBusy(FakeAws):
        async def list_ecs_tasks(self, region, cluster, include_stopped=False):
            return [t for t in await FakeAws.list_ecs_tasks(self, region, cluster)
                    if t["containerInstanceArn"] == "ci/managed"]

    fake = OnlyManagedBusy()
    m = _load(fake)
    asyncio.run(m._ensure_task(REGION, "deploy-key", USER_HOST))
    assert len(fake.ran_tasks) == 1, (
        "a task running on another host suppressed this host's gateway task")


# ── the ECS registration wait ─────────────────────────────────────────────────
# This helper is where two changes met: the wait itself was added so a REUSED host
# that is still mid-registration doesn't get a RunTask that 400s with "No Container
# Instances", and it then had to become host-scoped so several gateways can share a
# cluster. Merging those wrong is silent — the wait would still "work", just on the
# wrong host's readiness — so both halves are pinned here.

def test_registration_wait_returns_once_the_named_host_is_active():
    fake = FakeAws()
    m = _load(fake)
    m._REGISTER_TIMEOUT_S = 2
    m._REGISTER_POLL_S = 0.01
    asyncio.run(m._await_ecs_registration(REGION, MANAGED_HOST))  # must not hang


def test_registration_wait_is_not_satisfied_by_a_different_host():
    """A neighbour being ready says nothing about this host, and the task is
    placement-constrained to this host anyway — so accepting any ACTIVE instance
    would just move the RunTask failure later."""
    class OnlyOtherHostActive(FakeAws):
        def __init__(self):
            super().__init__()
            self.polls = 0

        async def list_container_instances(self, region, cluster):
            self.polls += 1
            return [{"arn": "ci/user", "status": "ACTIVE", "ec2_instance_id": USER_HOST}]

    fake = OnlyOtherHostActive()
    m = _load(fake)
    m._REGISTER_TIMEOUT_S = 0.05
    m._REGISTER_POLL_S = 0.01
    asyncio.run(m._await_ecs_registration(REGION, MANAGED_HOST))
    assert fake.polls > 1, (
        f"returned after {fake.polls} poll(s) — a different host being ACTIVE satisfied "
        "the wait, so RunTask would fire before this host had registered")


def test_registration_wait_gives_up_rather_than_raising():
    """Best-effort, like every other call in this module: a listing failure must not
    take down the deploy that asked for a gateway."""
    class Broken(FakeAws):
        async def list_container_instances(self, region, cluster):
            raise RuntimeError("ECS unavailable")

    fake = Broken()
    m = _load(fake)
    m._REGISTER_TIMEOUT_S = 1
    m._REGISTER_POLL_S = 0.01
    asyncio.run(m._await_ecs_registration(REGION, MANAGED_HOST))  # must not raise


# ── region resolution ─────────────────────────────────────────────────────────

def test_aws_gateway_settings_come_from_region_config():
    """'2 in us-east-2' only works if the cluster and subnet are resolved per region;
    the flat bt_ecs_* keys describe one region only."""
    src = open(os.path.join(_ROOT, "web_dashboard", "services",
                            "jumpoint_host_service.py")).read()
    assert "resolve_region(\"aws\", region)" in src or "_aws_region_cfg" in src
    body = src[src.index("def _aws_region_cfg"):src.index("async def _live_gateway_tasks")]
    assert "resolve_region" in body, "AWS gateway settings are not region-resolved"
    for flat in ('_cfg("bt_ecs_jumpoint_subnet_id")', '_cfg("bt_ecs_cluster")',
                 '_cfg("bt_ecs_jumpoint_security_group_id")'):
        assert flat not in src, (
            f"{flat} is still read flat — a gateway outside the default region would get "
            "the wrong subnet/cluster")


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
    sys.exit(1 if failures else 0)
