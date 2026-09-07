"""Business-hours suspend schedules for cloud VMs: who may have one, and what a pass does.

Phase 1 of the audit's recommendation 4. `suspend_schedule` (promoted from `pov_schedule`,
unchanged) answers *when*; `vm_suspend_policy` answers *whether*; `suspend_sweeper` walks
the inventory and enqueues the same `*_power` row the Suspend button creates.

The refusals are the point of this file, because each is something that breaks quietly:

  * **Azure** allocates estate private addresses `Dynamic` where POV allocates `Static`, so
    a deallocated VM can return on a different one — and `terraform_pra_service` has
    `provision_jump`/`remove_jump` and no update, so the PRA jump item, Password Safe
    system and Entitle registration cannot be repaired short of destroy-and-recreate.
  * **OCI** wires the *public* address, which is released on stop. After one cycle the
    jump item can point at an address that now belongs to somebody else's instance.
  * **Password Safe auto-management** reaches the guest through the cloud's own agent
    (`ssm` on AWS, `gcpvm` on GCP) and cannot reach a stopped one, so a nightly suspend
    means a nightly rotation failure in somebody's Password Safe.

And the NULL latch, which is the arming rule: a schedule that has never been evaluated
acts on nothing, so enabling this on an existing fleet cannot replay a backlog of
boundaries crossed while nobody was watching.

Run: python tests/test_vm_suspend_schedule.py   (or under pytest)
"""
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


# ── Who may carry a schedule ──────────────────────────────────────────────────

def test_aws_and_gcp_may_be_scheduled():
    assert vm_suspend_policy.schedulable("ec2_deploy", CLEAN_AWS) == (True, "")
    assert vm_suspend_policy.schedulable("gce_deploy", CLEAN_GCP) == (True, "")


def test_azure_is_refused_because_its_address_can_move():
    ok, reason = vm_suspend_policy.schedulable("azure_deploy", {"private_ip": "10.0.0.4"})
    assert not ok
    assert "dynamic" in reason.lower(), reason


def test_oci_is_refused_because_its_public_address_is_released():
    ok, reason = vm_suspend_policy.schedulable("oci_deploy", {"public_ip": "203.0.113.7"})
    assert not ok
    assert "public address" in reason.lower(), reason


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
    for job_type, meta in (("azure_deploy", {}), ("oci_deploy", {}),
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
