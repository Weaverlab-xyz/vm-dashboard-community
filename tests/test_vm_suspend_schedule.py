"""Business-hours suspend schedules for cloud VMs: who may have one, and what a pass does.

Phase 1 of the audit's recommendation 4. `suspend_schedule` (promoted from `pov_schedule`,
unchanged) answers *when*; `vm_suspend_policy` answers *whether*; `suspend_sweeper` walks
the inventory and enqueues the same `*_power` row the Suspend button creates.

The refusals are the point of this file, because each is something that breaks quietly:

  * **Azure until its address is pinned.** ARM releases a dynamic private address on
    deallocate, so the VM can return on a different one — and `terraform_pra_service` has
    `provision_jump`/`remove_jump` and no update, so the PRA jump item, Password Safe
    system and Entitle registration cannot be repaired short of destroy-and-recreate.
    Pinning ratifies the address ARM already chose (never picks one, which is what makes
    it collision-free on a shared resource group), so an Azure VM is schedulable once
    `private_ip_static` is set and refused until then.
  * **OCI**, which pinning does not reach: it wires the *public* address, released on
    stop. After one cycle the jump item can point at an address that now belongs to
    somebody else's instance.
  * **Password Safe auto-management** reaches the guest through the cloud's own agent
    (`ssm` on AWS, `gcpvm` on GCP, `azurevm` on Azure) and cannot reach a stopped one, so
    a nightly suspend means a nightly rotation failure in somebody's Password Safe.

And the NULL latch, which is the arming rule: a schedule that has never been evaluated
acts on nothing, so enabling this on an existing fleet cannot replay a backlog of
boundaries crossed while nobody was watching.

Run: python tests/test_vm_suspend_schedule.py   (or under pytest)
"""
import ast
import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="vm-suspend-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDB}"
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-vm-suspend-tests")

try:
    from web_dashboard.database import Base, Job, SessionLocal, engine
    from web_dashboard.services import (job_service, suspend_schedule, suspend_sweeper,
                                        vm_suspend_policy)
except Exception as exc:  # pragma: no cover — app deps missing
    try:
        import pytest
        pytest.skip(f"app dependencies unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)

Base.metadata.create_all(bind=engine)

CLEAN_AWS = {"instance_id": "i-1", "region": "us-east-1", "private_ip": "10.0.0.4"}
CLEAN_GCP = {"instance_name": "vm-1", "zone": "us-central1-a", "project_id": "p",
             "private_ip": "10.0.0.5"}
# An OCI instance deployed without a public address: its wire-up used the private one.
CLEAN_OCI = {"instance_ocid": "ocid1.instance.oc1..aaa", "instance_name": "oci-1",
             "private_ip": "10.0.1.5", "region": "us-ashburn-1"}
# An Azure VM as deployed since pinning shipped: address read back from the NIC and frozen.
CLEAN_AZURE = {"vm_name": "az-1", "resource_group": "vm-cli-rg", "nic_name": "az-1-nic",
               "private_ip": "10.0.0.6", "private_ip_static": True}


# ── Who may carry a schedule ──────────────────────────────────────────────────

def test_all_four_clouds_can_qualify():
    assert vm_suspend_policy.schedulable("ec2_deploy", CLEAN_AWS) == (True, "")
    assert vm_suspend_policy.schedulable("gce_deploy", CLEAN_GCP) == (True, "")
    assert vm_suspend_policy.schedulable("azure_deploy", CLEAN_AZURE) == (True, "")
    assert vm_suspend_policy.schedulable("oci_deploy", CLEAN_OCI) == (True, "")


def test_azure_is_refused_until_its_address_is_pinned():
    """The refusal is about `private_ip_static`, not about Azure. A dynamic address is
    released on deallocate; a pinned one is the same address the VM already has."""
    unpinned = {k: v for k, v in CLEAN_AZURE.items() if k != "private_ip_static"}
    ok, reason = vm_suspend_policy.schedulable("azure_deploy", unpinned)
    assert not ok
    assert "dynamic" in reason.lower(), reason
    # ...and the reason says what will happen rather than only what is wrong.
    assert "pins" in reason.lower(), reason
    assert vm_suspend_policy.schedulable("azure_deploy", CLEAN_AZURE)[0] is True


def test_a_password_safe_azure_vm_is_never_pinned():
    """The one that matters. `needs_address_pin` gates a real write to a real NIC, so a VM
    that will be refused for some other reason must not have its NIC touched on the way to
    being told no."""
    doomed = {k: v for k, v in CLEAN_AZURE.items() if k != "private_ip_static"}
    doomed["ps_managed_system_id"] = "SENTINEL-ps"
    assert vm_suspend_policy.needs_address_pin("azure_deploy", doomed) is False
    ok, reason = vm_suspend_policy.schedulable("azure_deploy", doomed)
    assert not ok and "password safe" in reason.lower(), reason

    # Same for a public-address wire-up: pinning a private address it does not use is a
    # pointless write, and the refusal stands either way.
    public_wired = {"vm_name": "az-2", "nic_name": "az-2-nic", "resource_group": "rg",
                    "bt_tf_state": "{}", "public_ip": "203.0.113.9"}
    assert vm_suspend_policy.needs_address_pin("azure_deploy", public_wired) is False


def test_needs_address_pin_defers_to_schedulable():
    """One implementation of the rules. It asks "would this be schedulable if pinned?"
    rather than carrying a second copy that can drift from the first."""
    unpinned = {k: v for k, v in CLEAN_AZURE.items() if k != "private_ip_static"}
    assert vm_suspend_policy.needs_address_pin("azure_deploy", unpinned) is True
    assert vm_suspend_policy.needs_address_pin("azure_deploy", CLEAN_AZURE) is False
    for job_type, meta in (("ec2_deploy", CLEAN_AWS), ("gce_deploy", CLEAN_GCP),
                           ("oci_deploy", CLEAN_OCI),
                           ("expiry_sweep", {}), ("", {})):
        assert vm_suspend_policy.needs_address_pin(job_type, meta) is False, job_type


def test_describe_says_a_pin_is_coming_before_it_happens():
    """So a control can say what pressing Save will do, rather than reporting it after."""
    unpinned = {k: v for k, v in CLEAN_AZURE.items() if k != "private_ip_static"}
    assert vm_suspend_policy.describe("azure_deploy", unpinned)["needs_address_pin"] is True
    assert vm_suspend_policy.describe("ec2_deploy", CLEAN_AWS)["needs_address_pin"] is False


def test_an_oci_instance_without_a_public_address_may_be_scheduled():
    """OCI's runner prefers the PUBLIC address — the only one of the four that does,
    because OCI has no dashboard-provisioned gateway in the VCN ("bring your own"). So an
    instance deployed with assign_public_ip=False is wired at its private address, which
    survives a stop exactly as it does on the other three."""
    assert vm_suspend_policy.schedulable("oci_deploy", CLEAN_OCI) == (True, "")


def test_an_oci_instance_wired_at_its_public_address_is_refused():
    ok, reason = vm_suspend_policy.schedulable(
        "oci_deploy", {**CLEAN_OCI, "public_ip": "203.0.113.7", "bt_tf_state": "{}"})
    assert not ok
    assert "public address" in reason.lower(), reason
    # No invented remedy: the address is already inside a jump item that cannot be
    # updated, so redeploying is genuinely the only way out and the reason says so.
    assert "redeployed" in reason.lower(), reason


def test_the_wired_address_is_recorded_not_inferred():
    """The bug this replaces: "does it have a private address?" answers the right question
    on three clouds and the wrong one on OCI. An OCI instance has BOTH addresses and was
    wired at the public one, so the old proxy passed it."""
    both = {**CLEAN_OCI, "public_ip": "203.0.113.7", "bt_tf_state": "{}"}
    assert both.get("private_ip"), "the old proxy would have said yes on this row"
    assert vm_suspend_policy.schedulable("oci_deploy", both)[0] is False

    # Recorded wins over the reconstruction, so a runner that changes its preference does
    # not silently reinterpret rows written under the old one.
    pinned_private = {**both, "wired_address": both["private_ip"]}
    assert vm_suspend_policy.schedulable("oci_deploy", pinned_private)[0] is True

    # And the refusal is about the wire-up, not about having a public address: an instance
    # that was never registered anywhere has told nothing its address, so nothing breaks
    # when it moves.
    unwired = {**CLEAN_OCI, "public_ip": "203.0.113.7"}
    assert vm_suspend_policy.schedulable("oci_deploy", unwired)[0] is True


def test_the_reconstruction_replays_each_runners_actual_preference():
    """For rows written before `wired_address` existed — which is the whole fleet. Each
    runner had one fixed preference, so replaying it gives the answer that runner gave;
    this is reconstruction, not a guess."""
    both = {"private_ip": "10.0.0.4", "public_ip": "203.0.113.7"}
    assert vm_suspend_policy.wired_address("oci_deploy", both) == "203.0.113.7"
    for job_type in ("ec2_deploy", "gce_deploy", "azure_deploy"):
        assert vm_suspend_policy.wired_address(job_type, both) == "10.0.0.4", job_type

    # And the source of truth, when present, is the record.
    assert vm_suspend_policy.wired_address(
        "oci_deploy", {**both, "wired_address": "10.0.0.4"}) == "10.0.0.4"
    # Neither address recorded: the runners fall back to an instance id or name, which is
    # not a public address either — so "" must not read as "wired publicly".
    assert vm_suspend_policy.wired_address("ec2_deploy", {}) == ""
    assert vm_suspend_policy.schedulable("ec2_deploy", {"bt_tf_state": "{}"})[0] is True


def test_every_runner_records_the_address_it_wired():
    """Four runners, one fact. A runner that computes `hostname`, hands it to PRA and then
    does not record it puts this policy back to inferring."""
    for name in ("aws_vm_service", "gcp_vm_service", "azure_vm_service", "oci_vm_service"):
        src = open(os.path.join(_ROOT, f"web_dashboard/services/{name}.py"),
                   encoding="utf-8").read()
        assert "wired_address" in src, name
        assert "hostname" in src, name


def test_a_vm_wired_at_its_public_address_is_refused_even_on_aws():
    """Both AWS and GCP prefer private_ip and fall back to public_ip. The private one
    survives a stop; an auto-assigned public one does not."""
    ok, reason = vm_suspend_policy.schedulable(
        "ec2_deploy", {"instance_id": "i-1", "bt_tf_state": "{}", "public_ip": "203.0.113.7"})
    assert not ok
    assert "public address" in reason.lower(), reason


def test_a_password_safe_managed_vm_is_refused_with_a_way_out():
    """Rotation runs on Password Safe's clock, which this dashboard cannot pause. The
    reason names the fix, because the operator — not this code — decides."""
    for meta in ({**CLEAN_AWS, "ps_managed_system_id": "42"},
                 {**CLEAN_AWS, "ps_registration_tf_state": "{}"}):
        ok, reason = vm_suspend_policy.schedulable("ec2_deploy", meta)
        assert not ok
        assert "password safe" in reason.lower() and "detach" in reason.lower(), reason


def test_a_non_vm_job_is_refused():
    assert vm_suspend_policy.schedulable("expiry_sweep", {})[0] is False
    assert vm_suspend_policy.schedulable("", {})[0] is False


def test_every_refusal_explains_itself():
    """A greyed-out control with no reason is a bug report waiting to happen."""
    for job_type, meta in (("azure_deploy", {}),
                           ("oci_deploy", {**CLEAN_OCI, "public_ip": "203.0.113.7",
                                           "bt_tf_state": "{}"}),
                           ("azure_deploy", {"vm_name": "az-1", "private_ip": "10.0.0.6"}),
                           ("ec2_deploy", {**CLEAN_AWS, "ps_managed_system_id": "1"}),
                           ("nonsense", {})):
        ok, reason = vm_suspend_policy.schedulable(job_type, meta)
        assert not ok and reason and reason.endswith("."), (job_type, reason)


# ── The sweep ─────────────────────────────────────────────────────────────────

def _vm(job_id, job_type="ec2_deploy", meta=None, **schedule):
    db = SessionLocal()
    try:
        db.query(Job).filter(Job.id == job_id).delete()
        job = Job(id=job_id, job_type=job_type, status="completed", created_by="alice",
                  workgroup="team-a", extra_data=json.dumps(meta or CLEAN_AWS),
                  created_at=datetime.utcnow(), completed_at=datetime.utcnow())
        for k, v in schedule.items():
            setattr(job, k, v)
        db.add(job)
        db.commit()
    finally:
        db.close()


def _reset():
    db = SessionLocal()
    try:
        db.query(Job).delete()
        db.commit()
    finally:
        db.close()


def _sweep():
    db = SessionLocal()
    try:
        job = job_service.create_job(db, job_type="suspend_sweep", created_by="system")
        asyncio.run(suspend_sweeper.run(db, job_id=job.id, meta={}))
        db.refresh(job)
        return job.metadata_dict
    finally:
        db.close()


def test_the_first_pass_arms_but_does_not_act():
    """The NULL latch. Setting a schedule must not suspend a VM for boundaries crossed
    before the schedule existed — the same arming rule the auto-delete timer uses."""
    _reset()
    _vm("v1", suspend_at_local="00:01", resume_at_local="23:59",
        schedule_timezone="UTC", schedule_days=suspend_schedule.DAYS_ALL,
        schedule_last_checked_at=None)
    out = _sweep()
    assert out["acted"] == [], out
    db = SessionLocal()
    try:
        assert db.query(Job).filter(Job.id == "v1").first().schedule_last_checked_at is not None, \
            "the pass must stamp the latch, or the schedule never arms"
    finally:
        db.close()


def test_a_crossed_boundary_enqueues_the_same_power_job_the_button_makes():
    _reset()
    # Latch an hour ago; a suspend boundary one minute ago has been crossed since.
    now = datetime.now(timezone.utc)
    crossed = (now - timedelta(minutes=1)).strftime("%H:%M")
    _vm("v1", suspend_at_local=crossed, schedule_timezone="UTC",
        schedule_days=suspend_schedule.DAYS_ALL,
        schedule_last_checked_at=(now - timedelta(hours=1)).replace(tzinfo=None))
    out = _sweep()
    assert len(out["acted"]) == 1, out
    assert out["acted"][0]["action"] == "stop", out

    db = SessionLocal()
    try:
        power = db.query(Job).filter(Job.job_type == "ec2_power").first()
        assert power is not None, "no power job was enqueued"
        m = power.metadata_dict
        # The same keys api/aws.py's endpoint persists — a scheduled action and a button
        # press must produce identical rows, or the runner needs two code paths.
        assert m["action"] == "stop" and m["instance_id"] == "i-1"
        assert m["region"] == "us-east-1" and m["deploy_job_id"] == "v1"
        assert power.workgroup == "team-a", "the power job inherits the VM's workgroup"
    finally:
        db.close()


def test_an_unschedulable_vm_is_reported_not_acted_on():
    _reset()
    now = datetime.now(timezone.utc)
    crossed = (now - timedelta(minutes=1)).strftime("%H:%M")
    _vm("v1", meta={**CLEAN_AWS, "ps_managed_system_id": "42"},
        suspend_at_local=crossed, schedule_timezone="UTC",
        schedule_days=suspend_schedule.DAYS_ALL,
        schedule_last_checked_at=(now - timedelta(hours=1)).replace(tzinfo=None))
    out = _sweep()
    assert out["acted"] == [], out
    assert len(out["skipped"]) == 1 and "Password Safe" in out["skipped"][0]["reason"]


def test_a_destroyed_vm_is_not_considered():
    _reset()
    now = datetime.now(timezone.utc)
    _vm("v1", meta={**CLEAN_AWS, "destroyed": True},
        suspend_at_local=(now - timedelta(minutes=1)).strftime("%H:%M"),
        schedule_timezone="UTC", schedule_days=suspend_schedule.DAYS_ALL,
        schedule_last_checked_at=(now - timedelta(hours=1)).replace(tzinfo=None))
    out = _sweep()
    assert out["considered"] == 0 and out["acted"] == [], out


def test_a_vm_with_no_schedule_is_not_even_selected():
    _reset()
    _vm("v1")  # no schedule columns set
    out = _sweep()
    assert out["considered"] == 0, out


# ── Wiring ────────────────────────────────────────────────────────────────────

def test_the_sweep_is_its_own_job_type_and_a_singleton():
    """Its own type rather than folded into expiry_sweep: that one is gated on
    resource_expiry_enabled, and a power window must work without the destructive timer."""
    src = open(os.path.join(_ROOT, "web_dashboard/jobs_worker.py"), encoding="utf-8").read()
    assert '"suspend_sweep"' in src
    singles = src.split("SINGLETON_TYPES = frozenset((")[1].split("))")[0]
    assert '"suspend_sweep"' in singles, "two concurrent passes would double-enqueue"


def test_the_pin_uses_the_sdks_real_attribute_name():
    """The test that would have caught the bug this fix was written on top of.

    `private_ip_address_allocation` is NOT an attribute of the SDK's
    NetworkInterfaceIPConfiguration — the model warns and discards it. Three call sites
    spelled it that way for their whole lives and were inert; harmless, because they were
    all asking for "Dynamic", which is ARM's default anyway. A pin written with the same
    misspelling would silently not pin, and would pass every test that does not talk to
    Azure. This is that test.
    """
    from azure.mgmt.network.models import NetworkInterfaceIPConfiguration as _Cfg
    assert "private_ip_allocation_method" in _Cfg._attribute_map
    assert "private_ip_address_allocation" not in _Cfg._attribute_map

    src = open(os.path.join(_ROOT, "web_dashboard/services/azure_service.py"),
               encoding="utf-8").read()
    tree = ast.parse(src)
    # Asked of the AST, not of the text: the module explains this bug in a comment, and a
    # substring scan cannot tell an explanation from a relapse.
    used = {k.arg for n in ast.walk(tree) if isinstance(n, ast.Call) for k in n.keywords}
    used |= {k.value for n in ast.walk(tree) if isinstance(n, ast.Dict)
             for k in n.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)}
    assert "private_ip_address_allocation" not in used, "the discarded spelling is back"
    assert "private_ip_allocation_method" in used

    fn = next(f for f in ast.walk(tree) if isinstance(f, ast.FunctionDef)
              and f.name == "_pin_nic_private_address_sync")
    assigned = {t.attr for n in ast.walk(fn) if isinstance(n, ast.Assign)
                for t in n.targets if isinstance(t, ast.Attribute)}
    assert "private_ip_allocation_method" in assigned, assigned


def test_the_pin_is_read_modify_write():
    """ARM treats a PUT as the whole desired state. Building a fresh NetworkInterface here
    would post one with no NSG association, no public IP and no accelerated networking —
    a pin that silently strips the VM's firewall. The NIC must be fetched and mutated."""
    src = open(os.path.join(_ROOT, "web_dashboard/services/azure_service.py"),
               encoding="utf-8").read()
    fn = next(f for f in ast.walk(ast.parse(src)) if isinstance(f, ast.FunctionDef)
              and f.name == "_pin_nic_private_address_sync")

    calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute)]
    names = [c.func.attr for c in calls]
    assert "get" in names, "the NIC must be read before it is written"
    assert names.index("get") < names.index("begin_create_or_update"), names
    assert not any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                   and n.func.id == "NetworkInterface" for n in ast.walk(fn)), \
        "constructing a fresh NIC drops everything the GET carries"


def test_an_already_static_nic_is_not_rewritten():
    """Scheduling a VM twice should issue one write, not two. Driven against a fake so the
    idempotence is a property of the code rather than of Azure."""
    from web_dashboard.services import azure_service

    class _Cfg:
        def __init__(self, method, address):
            self.private_ip_allocation_method = method
            self.private_ip_address = address

    class _Nic:
        def __init__(self, cfg):
            self.ip_configurations = [cfg]

    class _Nics:
        def __init__(self, nic):
            self._nic, self.writes = nic, 0

        def get(self, rg, name):
            return self._nic

        def begin_create_or_update(self, rg, name, nic):
            self.writes += 1
            return type("P", (), {"result": lambda self: nic})()

    class _Net:
        def __init__(self, nics):
            self.network_interfaces = nics

    static = _Nics(_Nic(_Cfg("Static", "10.0.0.6")))
    out = azure_service._pin_nic_private_address_sync(_Net(static), "rg", "nic")
    assert out == {"address": "10.0.0.6", "already_static": True}
    assert static.writes == 0, "an already-pinned NIC must not be rewritten"

    dynamic = _Nics(_Nic(_Cfg("Dynamic", "10.0.0.7")))
    out = azure_service._pin_nic_private_address_sync(_Net(dynamic), "rg", "nic")
    assert out == {"address": "10.0.0.7", "already_static": False}
    assert dynamic.writes == 1
    # The address is unchanged; only ARM's freedom to reclaim it is.
    assert dynamic._nic.ip_configurations[0].private_ip_address == "10.0.0.7"
    assert dynamic._nic.ip_configurations[0].private_ip_allocation_method == "Static"


def test_a_deallocated_vm_is_refused_rather_than_pinned_at_a_remembered_address():
    """A Dynamic config on a deallocated VM may report no address. Pinning the value we
    remember could claim one that now belongs to someone else — the exact failure this
    whole feature exists to prevent."""
    from web_dashboard.services import azure_service

    class _Nics:
        writes = 0

        def get(self, rg, name):
            return type("N", (), {"ip_configurations": [
                type("C", (), {"private_ip_allocation_method": "Dynamic",
                               "private_ip_address": None})()]})()

        def begin_create_or_update(self, *a):        # pragma: no cover — must not run
            _Nics.writes += 1
            raise AssertionError("pinned a VM with no address")

    net = type("Net", (), {"network_interfaces": _Nics()})()
    try:
        azure_service._pin_nic_private_address_sync(net, "rg", "nic")
        raise AssertionError("expected a refusal")
    except azure_service.AzureError as exc:
        assert "deallocated" in str(exc).lower() and "start it" in str(exc).lower(), exc
    assert _Nics.writes == 0


def test_the_sweep_enqueues_azure_with_the_keys_its_runner_reads():
    """`azure_vm_service._run_power` reads meta["action"], ["vm_name"] and
    ["resource_group"]. The GCP branch was the fallback for every non-AWS cloud, so an
    Azure VM would have been enqueued with `instance_name`/`zone` and failed on a KeyError
    inside the worker, hours after anybody was watching."""
    _reset()
    now = datetime.now(timezone.utc)
    crossed = (now - timedelta(minutes=1)).strftime("%H:%M")
    _vm("az-job", job_type="azure_deploy", meta=CLEAN_AZURE,
        suspend_at_local=crossed, schedule_timezone="UTC",
        schedule_days=suspend_schedule.DAYS_ALL,
        schedule_last_checked_at=(now - timedelta(hours=1)).replace(tzinfo=None))
    out = _sweep()
    assert len(out["acted"]) == 1, out

    db = SessionLocal()
    try:
        power = db.query(Job).filter(Job.job_type == "azure_power").all()
        assert len(power) == 1, [j.job_type for j in db.query(Job).all()]
        meta = power[0].metadata_dict
        assert meta["action"] == "stop"
        assert meta["vm_name"] == "az-1"
        assert meta["resource_group"] == "vm-cli-rg"
        assert meta["deploy_job_id"] == "az-job"
        assert power[0].workgroup == "team-a"
    finally:
        db.close()

    # And the selection filter is derived, not restated: a cloud that can be powered but
    # is missing from the query is never even looked at.
    assert "azure_deploy" in suspend_sweeper._DEPLOY_TYPES
    assert "oci_deploy" in suspend_sweeper._DEPLOY_TYPES


def test_the_sweep_enqueues_oci_with_the_keys_its_runner_reads():
    """OCI's power job needs only an OCID — no region, no resource group. The GCP branch
    was the fallback for every non-AWS cloud, so this would have been enqueued with
    `instance_name`/`zone` and failed inside the worker."""
    _reset()
    now = datetime.now(timezone.utc)
    crossed = (now - timedelta(minutes=1)).strftime("%H:%M")
    _vm("oci-job", job_type="oci_deploy", meta=CLEAN_OCI,
        suspend_at_local=crossed, schedule_timezone="UTC",
        schedule_days=suspend_schedule.DAYS_ALL,
        schedule_last_checked_at=(now - timedelta(hours=1)).replace(tzinfo=None))
    out = _sweep()
    assert len(out["acted"]) == 1, out

    db = SessionLocal()
    try:
        power = db.query(Job).filter(Job.job_type == "oci_power").all()
        assert len(power) == 1, [j.job_type for j in db.query(Job).all()]
        meta = power[0].metadata_dict
        assert meta["action"] == "stop"
        assert meta["instance_ocid"] == "ocid1.instance.oc1..aaa"
        assert meta["deploy_job_id"] == "oci-job"
        assert power[0].workgroup == "team-a"
    finally:
        db.close()


def test_every_schedulable_cloud_has_a_power_job_and_a_metadata_shape():
    """The two halves that must move together. A cloud added to the policy but not to the
    sweeper's map raises a KeyError mid-pass; one with no metadata branch silently gets
    another cloud's keys, which is worse because it fails later and elsewhere."""
    for cloud in vm_suspend_policy.SCHEDULABLE_CLOUDS:
        assert cloud in suspend_sweeper._POWER_JOB, cloud
    row = type("R", (), {"id": "r1"})()
    shapes = {c: set(suspend_sweeper._power_meta(c, row, {}, "stop"))
              for c in vm_suspend_policy.SCHEDULABLE_CLOUDS}
    # Each cloud names its instance differently; two clouds sharing a shape means one of
    # them fell through to another's branch.
    assert len({frozenset(v) for v in shapes.values()}) == len(shapes), shapes


class _Admin:
    """Passes both rules the suspend API applies: `has_permission` (satisfied by
    is_effective_admin) and api/azure's `_assert_can_act` (which keys on is_admin). Set
    independently on purpose — see tests/test_dashboard_stats_api.py for why the two admin
    rules in this app must not be unified."""
    username = "alice"
    is_admin = True
    is_effective_admin = True
    workgroups_list = ["team-a"]
    effective_permissions_dict = {}


def _put_schedule(job_id, pin):
    """Drive the PUT handler with a stubbed pin. Returns (response, raised)."""
    import web_dashboard.services.azure_service as az
    from web_dashboard.api import suspend as api

    original = az.pin_private_address
    az.pin_private_address = pin
    db = SessionLocal()
    try:
        payload = api.ScheduleRequest(suspend_at="19:00", resume_at="07:00",
                                      timezone="UTC", days=suspend_schedule.DAYS_ALL)
        try:
            return asyncio.run(api.set_schedule(job_id, payload, db, _Admin())), None
        except api.HTTPException as exc:
            return None, exc
    finally:
        az.pin_private_address = original
        db.close()


def test_a_schedule_is_not_set_when_the_pin_fails():
    """Fail closed. A VM must never carry a schedule its address cannot survive — so a pin
    that does not happen leaves the schedule columns NULL rather than half-applied."""
    _reset()
    unpinned = {k: v for k, v in CLEAN_AZURE.items() if k != "private_ip_static"}
    _vm("az-fail", job_type="azure_deploy", meta=unpinned)

    async def _boom(rg, nic_name):
        raise RuntimeError("SENTINEL-arm-conflict")

    out, exc = _put_schedule("az-fail", _boom)
    assert out is None and exc is not None, out
    assert exc.status_code == 409, exc.status_code
    assert "SENTINEL-arm-conflict" in str(exc.detail), exc.detail

    db = SessionLocal()
    try:
        job = db.query(Job).filter(Job.id == "az-fail").first()
        assert job.suspend_at_local is None, "a schedule was written despite the failed pin"
        assert job.schedule_timezone is None and job.schedule_days is None
        assert not job.metadata_dict.get("private_ip_static")
    finally:
        db.close()


def test_a_successful_pin_is_recorded_audited_and_named_in_the_response():
    """The operator asked for a schedule and got a NIC write. That is defensible only if
    it is visible: metadata, an audit row, and the response all name the address."""
    _reset()
    unpinned = {k: v for k, v in CLEAN_AZURE.items() if k != "private_ip_static"}
    _vm("az-ok", job_type="azure_deploy", meta=unpinned)
    seen = []

    async def _pin(rg, nic_name):
        seen.append((rg, nic_name))
        return {"address": "10.0.0.99", "already_static": False}

    out, exc = _put_schedule("az-ok", _pin)
    assert exc is None, exc.detail if exc else None
    assert out["ok"] is True and out["schedule"], out
    assert out["pinned"] == {"address": "10.0.0.99", "already_static": False}, out
    # Read from the deploy job's own record of its NIC, not guessed from the VM name.
    assert seen == [("vm-cli-rg", "az-1-nic")], seen

    db = SessionLocal()
    try:
        job = db.query(Job).filter(Job.id == "az-ok").first()
        assert job.metadata_dict["private_ip_static"] is True
        # The live address wins over the remembered one — a VM already power-cycled may
        # have moved, and writing it back repairs stale metadata.
        assert job.metadata_dict["private_ip"] == "10.0.0.99"
        assert job.suspend_at_local == "19:00"

        from web_dashboard.database import AuditLog
        row = (db.query(AuditLog).filter(AuditLog.action == "azure_address_pinned")
               .order_by(AuditLog.id.desc()).first())
        assert row is not None, "the pin must leave a trace"
        assert row.details_dict["address"] == "10.0.0.99"
        assert row.details_dict["nic_name"] == "az-1-nic"
    finally:
        db.close()


def test_a_malformed_schedule_never_reaches_azure():
    """The pin writes to a real NIC. A request that is going to be rejected for a bad time
    must not have moved anything first — so validation runs before the cloud call."""
    _reset()
    unpinned = {k: v for k, v in CLEAN_AZURE.items() if k != "private_ip_static"}
    _vm("az-bad", job_type="azure_deploy", meta=unpinned)

    async def _never(rg, nic_name):                  # pragma: no cover — must not run
        raise AssertionError("called Azure for a request that was about to 400")

    import web_dashboard.services.azure_service as az
    from web_dashboard.api import suspend as api

    original = az.pin_private_address
    az.pin_private_address = _never
    db = SessionLocal()
    try:
        payload = api.ScheduleRequest(suspend_at="25:99", timezone="UTC",
                                      days=suspend_schedule.DAYS_ALL)
        try:
            asyncio.run(api.set_schedule("az-bad", payload, db, _Admin()))
            raise AssertionError("expected a 400")
        except api.HTTPException as exc:
            assert exc.status_code == 400, exc.status_code
    finally:
        az.pin_private_address = original
        db.close()

    db = SessionLocal()
    try:
        assert not db.query(Job).filter(Job.id == "az-bad").first() \
                 .metadata_dict.get("private_ip_static")
    finally:
        db.close()


def test_an_already_pinned_vm_makes_no_cloud_call_at_all():
    """Scheduling a VM deployed since pinning shipped must not touch Azure."""
    _reset()
    _vm("az-new", job_type="azure_deploy", meta=CLEAN_AZURE)

    async def _never(rg, nic_name):                  # pragma: no cover — must not run
        raise AssertionError("pinned an already-pinned VM")

    out, exc = _put_schedule("az-new", _never)
    assert exc is None, exc.detail if exc else None
    assert out["pinned"] is None, out


def test_a_deploy_that_recorded_no_nic_says_what_to_do_instead():
    """Azure VMs deployed before `nic_name` was persisted. Refused, but with the portal
    step named — an operator cannot act on "missing metadata key"."""
    _reset()
    _vm("az-old", job_type="azure_deploy",
        meta={"vm_name": "legacy-1", "private_ip": "10.0.0.8"})

    async def _never(rg, nic_name):                  # pragma: no cover — must not run
        raise AssertionError("called Azure with no NIC name")

    out, exc = _put_schedule("az-old", _never)
    assert out is None and exc.status_code == 409
    assert "legacy-1" in str(exc.detail) and "portal" in str(exc.detail).lower(), exc.detail


def test_the_permission_predicate_has_exactly_one_implementation():
    """api/auth.has_permission is the rule; the MCP tools and the suspend API call it
    rather than restating it. Two copies of a permission rule drift invisibly."""
    from web_dashboard.api import auth, mcp_server
    assert callable(auth.has_permission)
    src = open(os.path.join(_ROOT, "web_dashboard/api/mcp_server.py"), encoding="utf-8").read()
    assert "from .auth import has_permission" in src, "mcp_server should delegate"
    src2 = open(os.path.join(_ROOT, "web_dashboard/api/suspend.py"), encoding="utf-8").read()
    assert "has_permission" in src2


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
            traceback.print_exc()
    sys.exit(1 if failures else 0)
