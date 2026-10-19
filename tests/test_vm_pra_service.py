"""PRA on a cloud VM after deploy: services/vm_pra_service and the four /instances/pra routes.

What these pin:

- attach writes the jump onto the VM's DEPLOY row under the keys a deploy-time jump
  uses, so every existing destroy path removes it with no code of its own;
- a GCP/Azure VM deployed with PRA off gains a ``jumpoint_host_id`` reference, so the
  idle sweep cannot reap the gateway from under the new jump;
- re-apply removes the old jump before creating the new one, and clears the row's keys
  in between, so a failed create never leaves the row naming a deleted jump;
- detach removes the jump and clears the keys; PRA-off, and a Windows server whose
  password was neither kept nor handed to Password Safe, are refused up front;
- a Windows server gets the jumps its deploy would have built (windows_server_hook.
  wire_pra) from the password the deploy stored, or with no Vault account when Password
  Safe holds the account, and re-apply and detach remove its RDP jump too;
- every bulk form sends the opt-in, and Azure reads the PRA switch where the others do;
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


def test_a_windows_server_with_no_credential_and_pra_off_are_refused_before_any_terraform():
    calls = _stub()
    db = SessionLocal()
    # Windows, but the build-time password was not kept and Password Safe has no account.
    for meta in ({"os_type": "windows"}, {"admin_password_custody": "secret_manager"},
                 {"req": {"os_type": "Windows"}}):
        job = _deploy(db, **meta)
        try:
            _run(vps.attach(db, job, cloud="gcp"))
            raise AssertionError(f"Windows row {meta} should be refused")
        except vps.VmPraError as e:
            assert "password" in str(e)
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


# ── The deploy-form opt-in ────────────────────────────────────────────────────

def test_opted_in_is_on_unless_explicitly_off():
    assert vps.opted_in(None) and vps.opted_in(True)
    assert not vps.opted_in(False)


def test_every_deploy_model_carries_the_opt_in_defaulting_to_none():
    from web_dashboard.models import aws, azure, gcp, oci
    for model in (aws.DeployRequest, aws.BulkDeployRequest,
                  azure.AzureDeployRequest, azure.AzureBulkDeployRequest,
                  gcp.GCPDeployRequest, gcp.GCPBulkDeployRequest,
                  oci.OCIDeployRequest, oci.OCIBulkDeployRequest):
        f = model.model_fields.get("enable_pra")
        assert f is not None and f.default is None, model.__name__


def _src(*parts):
    with open(os.path.join(_ROOT, "web_dashboard", *parts), encoding="utf-8") as fh:
        return fh.read()


def test_every_runner_gates_its_jump_on_the_opt_in():
    for cloud in ("aws", "azure", "gcp", "oci"):
        assert "opted_in(" in _src("services", f"{cloud}_vm_service.py"), cloud


def test_the_opt_in_survives_every_fan_out():
    """AWS stores flat metadata per child; GCP and OCI rebuild a request per bulk item.
    A field missed in either is silently dropped — the VM gets a jump it opted out of."""
    assert _src("api", "aws.py").count('"enable_pra": req.enable_pra') == 3
    assert "enable_pra=req.enable_pra" in _src("api", "gcp.py")
    assert "enable_pra=req.enable_pra" in _src("api", "oci.py")


def test_each_modal_offers_the_opt_in_and_folds_the_rest_away():
    for cloud, obj in (("aws", "deployForm"), ("azure", "deployModal"),
                       ("gcp", "deployForm"), ("oci", "deployForm")):
        src = _src("templates", cloud, "index.html")
        box = f'x-model="{obj}.enable_pra"'
        # AWS's bulk form shares deployForm, so it carries a second box.
        assert src.count(box) == (2 if cloud == "aws" else 1), cloud
        i = src.index(box)
        assert src.rfind("{% if pra_enabled %}", 0, i) > src.rfind("{% endif %}", 0, i), (
            f"{cloud}: the PRA checkbox must only render while PRA is enabled")
        adv = src.index("Advanced options", i)
        assert "ssh_key_secret_override" in src[adv:adv + 3000], (
            f"{cloud}: the SSH key override belongs under Advanced")


# ── Windows after deploy ──────────────────────────────────────────────────────

def _stub_windows(calls, password="P@ss-1"):
    from web_dashboard.services import windows_admin_secret

    async def provision_rdp_jump(**kw):
        calls.provisioned.append({"rdp": True, **kw})
        return {"rdp_jump_id": "rdp-1", "vault_account_id": "va-1",
                "tf_state_json": '{"rdp": 1}', "jump_group_name": kw["jump_group_name"]}

    async def remove_rdp_jump(tf_state_json, tenant=None):
        calls.removed.append(tf_state_json)

    terraform_pra_service.provision_rdp_jump = provision_rdp_jump
    terraform_pra_service.remove_rdp_jump = remove_rdp_jump
    windows_admin_secret.read = lambda backend, ref: password
    vps.resolve_target = lambda cloud, jg=None, jp=None, workgroup=None: ("ops", "gw")


def test_a_windows_server_gets_its_jumps_from_the_stored_password():
    calls = _stub()
    saved = vps.resolve_target
    _stub_windows(calls)
    db = SessionLocal()
    try:
        job = _deploy(db, job_type="ec2_deploy", instance_id="i-w", instance_name="win-1",
                      region="us-east-1", os_type="windows", admin_username="Administrator",
                      admin_password_custody="secret_manager", admin_password_backend="aws_sm",
                      admin_password_ref="dash/win-1",
                      windows_access={"ssh": True, "rdp": True}, windows_ssh_status="ok")
        _run(vps.attach(db, job, cloud="aws"))
        shell = [c for c in calls.provisioned if not c.get("rdp")]
        rdp = [c for c in calls.provisioned if c.get("rdp")]
        assert len(shell) == 1 and len(rdp) == 1
        assert shell[0]["port"] == 22 and shell[0]["admin_password"] == "P@ss-1"
        assert shell[0]["vault_account_name"] == "win-1-ssh-admin"
        assert rdp[0]["rdp_username"] == "Administrator" and rdp[0]["admin_password"] == "P@ss-1"
        meta = job_service.get_job(db, job.id).metadata_dict
        assert meta["bt_rdp_tf_state"] == '{"rdp": 1}' and meta["bt_shell_jump_id"]
        assert vps.state(meta)["attached"]

        # Re-apply removes both before creating again; detach removes both and clears.
        _run(vps.attach(db, job, cloud="aws"))
        assert '{"rdp": 1}' in calls.removed
        _run(vps.detach(db, job_service.get_job(db, job.id)))
        meta = job_service.get_job(db, job.id).metadata_dict
        assert not vps.jump_id(meta) and not meta.get("bt_rdp_tf_state")
    finally:
        vps.resolve_target = saved
        db.close()


def test_rdp_only_and_password_safe_custody():
    calls = _stub()
    saved = vps.resolve_target
    _stub_windows(calls)
    db = SessionLocal()
    try:
        # A row from before SSH support: no access record means an RDP-only build. With
        # Password Safe holding the account there is no Vault account and no password.
        job = _deploy(db, os_type="windows", admin_username="gcpadmin",
                      admin_password_custody="passwordsafe_managed")
        _run(vps.attach(db, job, cloud="gcp"))
        assert len(calls.provisioned) == 1 and calls.provisioned[0].get("rdp")
        assert calls.provisioned[0]["admin_password"] == ""
        assert calls.provisioned[0]["vault_account_name"] == ""
        meta = job_service.get_job(db, job.id).metadata_dict
        assert vps.jump_id(meta) == "rdp-1", "an RDP-only server still reads as PRA ✓"
    finally:
        vps.resolve_target = saved
        db.close()


def test_every_bulk_form_sends_the_opt_in():
    for cloud, expr in (("aws", "enable_pra: this.deployForm.enable_pra !== false"),
                        ("azure", "enable_pra: m.enable_pra !== false"),
                        ("gcp", "enable_pra: m.enable_pra !== false"),
                        ("oci", "enable_pra: m.enable_pra !== false")):
        src = _src("templates", cloud, "index.html")
        i = src.index(f"/api/{cloud}/bulk-deploy")
        assert expr in src[i:i + 2500], f"{cloud}: the bulk form drops the PRA opt-in"


def test_azure_reads_the_pra_switch_where_the_other_clouds_do():
    src = _src("services", "azure_vm_service.py")
    assert "settings.pra_enabled" not in src, (
        "Azure must read PRA from Settings (config_service), not the env setting alone")
    assert 'config_service.get_bool("pra_enabled")' in src


def test_rows_show_an_rdp_only_jump():
    for cloud in ("aws", "azure", "gcp", "oci"):
        assert "vm_pra_service.jump_id(" in _src("api", f"{cloud}.py"), cloud


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
