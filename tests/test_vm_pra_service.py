"""PRA on a cloud VM after deploy: services/vm_pra_service and the four /instances/pra routes.

What these pin:

- attach writes the jump onto the VM's DEPLOY row under the keys a deploy-time jump
  uses, so every existing destroy path removes it with no code of its own;
- a GCP/Azure VM deployed with PRA off gains a ``jumpoint_host_id`` reference, so the
  idle sweep cannot reap the gateway from under the new jump;
- re-apply removes the old jump before creating the new one, and clears the row's keys
  in between, so a failed create never leaves the row naming a deleted jump;
- detach removes the jump and clears the keys; Windows and PRA-off are refused up front;
- each cloud router has the route, and it is ownership-checked like Destroy.

Uses a real temp SQLite database; Terraform and the gateway are stubbed.

Run: python tests/test_vm_pra_service.py   (or under pytest)
"""
import asyncio
import os
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="vm-pra-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDB}"
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-vm-pra-tests")

from web_dashboard.database import Base, SessionLocal, engine  # noqa: E402
from web_dashboard.services import (config_service, job_service,  # noqa: E402
                                    jumpoint_host_service, terraform_pra_service)
from web_dashboard.services import vm_pra_service as vps  # noqa: E402

Base.metadata.create_all(bind=engine)


class _Calls:
    def __init__(self):
        self.provisioned = []
        self.removed = []
        self.ensured = []
        self.fail_provision = False


def _stub(pra_on=True):
    calls = _Calls()

    async def provision_jump(**kw):
        if calls.fail_provision:
            raise RuntimeError("terraform apply failed")
        calls.provisioned.append(kw)
        n = len(calls.provisioned)
        return {"shell_jump_id": f"sj-{n}", "jump_group_name": kw["jump_group_name"],
                "tf_state_json": f'{{"state": {n}}}'}

    async def remove_jump(tf_state_json, tenant=None):
        calls.removed.append(tf_state_json)

    async def ensure_jumpoint_host(cloud, region, name="", placement=None):
        calls.ensured.append((cloud, region))
        return f"gw-{cloud}"

    terraform_pra_service.provision_jump = provision_jump
    terraform_pra_service.remove_jump = remove_jump
    jumpoint_host_service.ensure_jumpoint_host = ensure_jumpoint_host
    config_service.get_bool = lambda key, default=False: pra_on if key == "pra_enabled" else default
    return calls


def _deploy(db, job_type="gce_deploy", **meta):
    base = {"instance_name": "vm-1", "zone": "us-central1-a",
            "wired_address": "10.0.0.5", "private_ip": "10.0.0.5"}
    base.update(meta)
    job = job_service.create_job(db, job_type=job_type, created_by="alice",
                                 workgroup="team-a", metadata=base)
    job_service.set_completed(db, job.id)
    return job_service.get_job(db, job.id)


def _run(coro):
    return asyncio.run(coro)


def test_attach_writes_the_deploy_time_keys_and_a_gateway_reference():
    calls = _stub()
    db = SessionLocal()
    job = _deploy(db)
    _run(vps.attach(db, job, cloud="gcp", jump_group="ops"))
    meta = job_service.get_job(db, job.id).metadata_dict
    assert meta["bt_shell_jump_id"] == "sj-1"
    assert meta["bt_tf_state"] == '{"state": 1}'
    assert meta["bt_jump_group_name"] == "ops"
    assert meta["jumpoint_host_id"] == "gw-gcp", "GCP counts only rows holding a reference"
    kw = calls.provisioned[0]
    assert kw["hostname"] == "10.0.0.5" and kw["tag"] == "GCP" and kw["vm_name"] == "vm-1"
    assert calls.ensured == [("gcp", "us-central1")]
    db.close()


def test_an_existing_gateway_reference_is_kept():
    _stub()
    db = SessionLocal()
    job = _deploy(db, jumpoint_host_id="gw-original")
    _run(vps.attach(db, job, cloud="gcp"))
    assert job_service.get_job(db, job.id).metadata_dict["jumpoint_host_id"] == "gw-original"
    db.close()


def test_reapply_replaces_the_jump_rather_than_adding_a_second():
    calls = _stub()
    db = SessionLocal()
    job = _deploy(db, job_type="ec2_deploy", instance_id="i-1", region="us-east-1")
    _run(vps.attach(db, job, cloud="aws"))
    _run(vps.attach(db, job, cloud="aws", jump_group="other"))
    assert calls.removed == ['{"state": 1}']
    meta = job_service.get_job(db, job.id).metadata_dict
    assert meta["bt_shell_jump_id"] == "sj-2" and meta["bt_jump_group_name"] == "other"
    db.close()


def test_a_failed_reapply_leaves_no_stale_jump_on_the_row():
    calls = _stub()
    db = SessionLocal()
    job = _deploy(db)
    _run(vps.attach(db, job, cloud="gcp"))
    calls.fail_provision = True
    try:
        _run(vps.attach(db, job, cloud="gcp"))
        raise AssertionError("the failed create should raise")
    except RuntimeError:
        pass
    meta = job_service.get_job(db, job.id).metadata_dict
    assert not meta.get("bt_shell_jump_id") and not meta.get("bt_tf_state"), (
        "the old jump is gone; a row still naming it would make Destroy remove it twice")
    db.close()


def test_detach_removes_and_clears():
    calls = _stub()
    db = SessionLocal()
    job = _deploy(db)
    _run(vps.attach(db, job, cloud="gcp"))
    out = _run(vps.detach(db, job))
    assert out == {"bt_shell_jump_removed": "sj-1"}
    assert calls.removed == ['{"state": 1}']
    meta = job_service.get_job(db, job.id).metadata_dict
    assert not any(meta.get(k) for k in vps.JUMP_KEYS)
    db.close()


def test_detach_without_a_jump_is_refused():
    _stub()
    db = SessionLocal()
    job = _deploy(db)
    try:
        _run(vps.detach(db, job))
        raise AssertionError("expected VmPraError")
    except vps.VmPraError:
        pass
    db.close()


def test_windows_and_pra_off_are_refused_before_any_terraform():
    calls = _stub()
    db = SessionLocal()
    for meta in ({"os_type": "windows"}, {"admin_password_custody": "secret_manager"},
                 {"req": {"os_type": "Windows"}}):
        job = _deploy(db, **meta)
        try:
            _run(vps.attach(db, job, cloud="gcp"))
            raise AssertionError(f"Windows row {meta} should be refused")
        except vps.VmPraError:
            pass
    _stub(pra_on=False)
    try:
        _run(vps.attach(db, _deploy(db), cloud="gcp"))
        raise AssertionError("PRA off should be refused")
    except vps.VmPraError:
        pass
    assert calls.provisioned == []
    db.close()


def test_resolve_target_prefers_explicit_then_cloud_key_then_shared():
    saved = config_service.get
    try:
        cfg = {"gcp_bt_jump_group_name": "gcp-group", "bt_jump_group_name": "shared-group",
               "bt_jumpoint_name": "shared-gw"}
        config_service.get = lambda key, default="", workgroup=None: cfg.get(key, default)
        assert vps.resolve_target("gcp") == ("gcp-group", "shared-gw")
        assert vps.resolve_target("aws") == ("shared-group", "shared-gw")
        assert vps.resolve_target("gcp", " mine ", "gw") == ("mine", "gw")
    finally:
        config_service.get = saved


def test_the_worker_runs_a_queued_job_end_to_end():
    _stub()
    from web_dashboard.api import websocket

    async def _quiet(*a, **k):
        return None
    websocket.broadcast_progress = _quiet
    db = SessionLocal()
    job = _deploy(db, job_type="oci_deploy", instance_ocid="ocid1.instance.x",
                  region="us-ashburn-1")

    class _P:
        action, instance = "register", "ocid1.instance.x"
        jump_group = jumpoint_name = pra_credential_ref = None
    q = vps.queue(db, cloud="oci", deploy_job=job, payload=_P(), username="alice")
    assert q.job_type == "vm_pra" and q.workgroup == "team-a"
    _run(vps.run(db, q.id, q.metadata_dict))
    done = job_service.get_job(db, q.id)
    assert done.status == "completed", done.error_message
    assert job_service.get_job(db, job.id).metadata_dict["bt_shell_jump_id"] == "sj-1"
    db.close()


def test_every_cloud_router_has_the_route_and_checks_ownership():
    import inspect
    from web_dashboard.api import aws, azure, gcp, oci
    for mod, deploy_type in ((aws, "ec2_deploy"), (azure, "azure_deploy"),
                             (gcp, "gce_deploy"), (oci, "oci_deploy")):
        src = inspect.getsource(mod.set_instance_pra)
        assert f'"{deploy_type}"' in src, mod.__name__
        assert "_assert_can_act(" in src, f"{mod.__name__} must check ownership like Destroy"
        paths = {r.path for r in mod.router.routes}
        assert f"{mod.router.prefix}/instances/pra" in paths, mod.__name__


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
