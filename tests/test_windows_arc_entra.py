"""Entra join for AWS/GCP Windows servers through Azure Arc (services/windows_arc_service).

What these pin:

- the per-deploy choice: unset follows ``windows_arc_entra_default``, "" is an explicit
  no, and Arc is refused (as a warning) with an AD join, on a non-Windows image, or with
  OpenSSH off;
- the preflight refuses unregistered Arc resource providers and a missing resource
  group, naming the command, and runs nothing;
- the minted token travels ONLY through the runner's secret channel: never in the
  rendered play, never in either job's metadata, and scrubbed from the output — and a
  cloud runner with no way to receive it is refused;
- the onboarding play marks its connect task no_log, and the sentinel is parsed;
- AADLoginForWindows goes on the Arc machine with the ``mdmId`` setting Arc requires, and
  a failed install names the endpoints;
- the configured login groups are granted on the Arc machine, and destroy removes them and
  then the machine, leaving the Entra device (by design);
- the worker, both clouds' deploy/destroy paths and all of api/aws.py's metadata copies
  are wired.

Real temp SQLite; ARM through httpx.MockTransport; the runner and Azure are stubbed.

Run: python tests/test_windows_arc_entra.py   (or under pytest)
"""
import asyncio
import base64
import inspect
import json
import os
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="arc-entra-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDB}"
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-arc-entra-tests")

import httpx  # noqa: E402

from web_dashboard.database import Base, Job, SessionLocal, engine  # noqa: E402
from web_dashboard.services import job_service  # noqa: E402
from web_dashboard.services import windows_arc_service as arc  # noqa: E402

Base.metadata.create_all(bind=engine)

SUB = "11111111-2222-3333-4444-555555555555"
TOKEN = "eyJ0eXAiOiJKV1QiLCJhbGciOi.MINTED-ARM-TOKEN.signature"
_CFG = {"arc_resource_group": "rg-arc", "arc_location": "eastus",
        "ansible_runner_aws": "ecs", "ansible_runner_gcp": "gcp"}
arc._cfg = lambda key, default="": _CFG.get(key, default)


def _run(c):
    return asyncio.run(c)


async def _no_sleep(*_a, **_k):
    return None


arc.asyncio.sleep = _no_sleep


async def _token():
    return "dashboard-arm-token"


async def _sub():
    return SUB


arc._token, arc._subscription = _token, _sub


class _Arm:
    def __init__(self, *, unregistered=(), rg=200, ext_states=("Succeeded",), machine=200,
                 delete=200):
        self.unregistered, self.rg, self.machine, self.delete = set(unregistered), rg, machine, delete
        self.ext_states = list(ext_states)
        self.calls = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(req)
        path = req.url.path
        if "/providers/Microsoft." in path and "/machines/" not in path:
            ns = path.rsplit("/", 1)[-1]
            return httpx.Response(200, json={"registrationState":
                                             "NotRegistered" if ns in self.unregistered else "Registered"})
        if "/resourcegroups/" in path:
            return httpx.Response(self.rg, json={})
        if path.endswith("/extensions/AADLogin"):
            state = self.ext_states.pop(0) if len(self.ext_states) > 1 else self.ext_states[0]
            return httpx.Response(200, json={"properties": {"provisioningState": state,
                                                            "instanceView": {"status": {"message": "dsreg failed"}}}})
        if "/machines/" in path:
            if req.method == "DELETE":
                return httpx.Response(self.delete)
            return httpx.Response(self.machine, json={"location": "eastus",
                                                      "properties": {"status": "Connected"}})
        return httpx.Response(599)


def _install(arm):
    arc.transport = httpx.MockTransport(arm)
    return arm


def _refuses(coro, needle):
    try:
        _run(coro)
    except arc.ArcError as e:
        assert needle in str(e), str(e)
    else:
        raise AssertionError(f"accepted; expected {needle!r}")


# ── the choice ────────────────────────────────────────────────────────────────

def test_requested_follows_the_default_only_when_unset():
    from web_dashboard.services import config_service
    orig = config_service.get_bool
    config_service.get_bool = lambda key, default=False: key == "windows_arc_entra_default"
    try:
        assert arc.requested({}) is True
        assert arc.requested({"entra_join_mode": None}) is True
        assert arc.requested({"entra_join_mode": ""}) is False
        assert arc.requested({"entra_join_mode": "hybrid"}) is False
        config_service.get_bool = lambda key, default=False: False
        assert arc.requested({}) is False
        assert arc.requested({"entra_join_mode": "arc"}) is True

        class P:
            entra_join_mode = "arc"
        assert arc.requested(P()) is True
    finally:
        config_service.get_bool = orig


def test_deploy_problems_are_warnings_with_reasons():
    assert "Windows images only" in arc.deploy_problem(is_windows=False)
    assert "not both" in arc.deploy_problem(is_windows=True, ad_directory_id="d1")
    assert "windows_ssh_enabled" in arc.deploy_problem(is_windows=True, ssh=False)
    assert arc.deploy_problem(is_windows=True) == ""
    assert arc.machine_id(SUB, "win1") == (f"/subscriptions/{SUB}/resourceGroups/rg-arc/"
                                           f"providers/Microsoft.HybridCompute/machines/win1")


# ── preflight ─────────────────────────────────────────────────────────────────

def test_preflight_names_the_fix_and_runs_nothing():
    arm = _install(_Arm(unregistered=("Microsoft.HybridCompute", "Microsoft.HybridConnectivity")))
    try:
        _refuses(arc.preflight(), "az provider register --namespace Microsoft.HybridCompute")
        assert all(c.method == "GET" for c in arm.calls)
        _install(_Arm(rg=404))
        _refuses(arc.preflight(), "rg-arc does not exist")
        _install(_Arm())
        assert _run(arc.preflight()) == SUB
    finally:
        arc.transport = None


# ── the play ──────────────────────────────────────────────────────────────────

def test_play_marks_the_connect_task_no_log_and_never_carries_the_token():
    import yaml
    plays = yaml.safe_load(open(arc.PLAYBOOK, encoding="utf-8"))
    tasks = plays[0]["tasks"]
    connect = [t for t in tasks if "access-token" in json.dumps(t)]
    assert len(connect) == 1 and connect[0].get("no_log") is True
    assert "arc_access_token" not in plays[0]["vars"], "the token must arrive as a secret var"
    rendered = arc.render_playbook(arc.play_vars(subscription=SUB, tenant="t1", name="win1",
                                                 deploy_job_id="job-1"))
    assert TOKEN not in rendered
    again = yaml.safe_load(rendered)[0]["vars"]
    assert again["arc_resource_group"] == "rg-arc" and again["arc_resource_name"] == "win1"
    assert again["arc_tags"] == "managed-by=vm-dashboard,dashboard-job=job-1"


def test_sentinel_parsing():
    out = 'ok: [10.0.0.5] => {"msg": "VMDASH-ARC:OK build=26100"}\n' \
          'ok: [10.0.0.5] => {"msg": "VMDASH-ARC:CONNECTED Connected host=WIN-ABC12 connect_rc=0"}'
    state, detail = arc.parse_result(out)
    assert state == "connected" and arc.host_from(detail) == "WIN-ABC12"
    assert arc.parse_result('"msg": "VMDASH-ARC:UNSUPPORTED build=20348 type=Server"')[0] == "unsupported"
    assert arc.parse_result("no sentinel at all") == ("", "")


def _stub_runner(runner="gcp", delivery=True, fetch=False):
    from web_dashboard.services import ansible_local_run_service as alr, runner_credential as rc
    seen = {}
    saved = dict(windows_target=alr.windows_target, find=alr._find_cloud_deploy_meta,
                 key=alr._resolve_cloud_ssh_key, eph=alr._add_ephemeral_managed_entries,
                 disp=alr._dispatch_cloud_runner, dele=alr._delete_ephemeral,
                 rf=alr._runner_fetch, avail=rc.cloud_delivery_available, use=rc.use_for,
                 revoke=rc.revoke_for_job)
    alr.windows_target = lambda meta: {"user": "Administrator"}
    alr._find_cloud_deploy_meta = lambda db, cloud, ip: {"os_type": "windows"}

    async def key(db, cloud, ip):
        return "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----"
    alr._resolve_cloud_ssh_key = key

    def eph(runner_, entries, manifest, values, job_id):
        seen["store_values"] = dict(values)
        return [{"env": "S0", "secret_name": "eph-1"}], "bWFuaWZlc3Q=", [("gcp", "eph-1")]
    alr._add_ephemeral_managed_entries = eph
    alr._delete_ephemeral = lambda cleanup: seen.setdefault("deleted", list(cleanup))

    def rfetch(db, runner_, job_id, values):
        seen["fetch_values"] = dict(values)
        return {"script_b64": "", "env": {}, "token": "GRANT-TOKEN-XYZ"}
    alr._runner_fetch = rfetch

    async def disp(**kw):
        seen["dispatch"] = kw
        return 0, ('ok => {"msg": "VMDASH-ARC:CONNECTED Connected host=WIN-1 connect_rc=0"}\n'
                   f"debug leaked {TOKEN} and GRANT-TOKEN-XYZ")
    alr._dispatch_cloud_runner = disp
    rc.cloud_delivery_available = lambda r="": delivery
    rc.use_for = lambda r: fetch
    rc.revoke_for_job = lambda db, job_id: seen.setdefault("revoked", job_id)
    _CFG["ansible_runner_gcp"] = runner

    def restore():
        alr.windows_target, alr._find_cloud_deploy_meta = saved["windows_target"], saved["find"]
        alr._resolve_cloud_ssh_key = saved["key"]
        alr._add_ephemeral_managed_entries = saved["eph"]
        alr._dispatch_cloud_runner, alr._delete_ephemeral = saved["disp"], saved["dele"]
        alr._runner_fetch = saved["rf"]
        rc.cloud_delivery_available, rc.use_for = saved["avail"], saved["use"]
        rc.revoke_for_job = saved["revoke"]
        _CFG["ansible_runner_gcp"] = "gcp"
    return seen, restore


def test_onboard_sends_the_token_only_through_the_secret_channel():
    db = SessionLocal()
    variables = arc.play_vars(subscription=SUB, tenant="t1", name="win1", deploy_job_id="d")
    seen, restore = _stub_runner()
    try:
        code, out = _run(arc.onboard(db, job_id="arc-1", cloud="gcp", target="10.0.0.5",
                                     token=TOKEN, variables=variables))
        assert code == 0
        assert seen["store_values"] == {"arc_access_token": TOKEN}
        assert seen["deleted"] == [("gcp", "eph-1")], "the store copy is reaped after the run"
        kw = seen["dispatch"]
        assert kw["windows"] is True and kw["ansible_user"] == "Administrator"
        assert TOKEN not in base64.b64decode(kw["playbook_b64"]).decode()
        assert TOKEN not in json.dumps({k: v for k, v in kw.items() if k != "ssh_key_b64"})
        assert TOKEN not in out and "VMDASH-ARC:CONNECTED" in out
    finally:
        restore()
    seen, restore = _stub_runner(fetch=True)
    try:
        code, out = _run(arc.onboard(db, job_id="arc-2", cloud="gcp", target="10.0.0.5",
                                     token=TOKEN, variables=variables))
        assert seen["fetch_values"] == {"arc_access_token": TOKEN}
        assert seen["revoked"] == "arc-2" and "GRANT-TOKEN-XYZ" not in out
    finally:
        restore()
    seen, restore = _stub_runner(delivery=False)
    try:
        _refuses(arc.onboard(db, job_id="arc-3", cloud="gcp", target="10.0.0.5",
                             token=TOKEN, variables=variables), "collect-from-dashboard")
        assert "dispatch" not in seen
    finally:
        restore()
        db.close()


# ── the extension ─────────────────────────────────────────────────────────────

def test_extension_carries_mdm_id_and_waits():
    arm = _install(_Arm(ext_states=("Creating", "Creating", "Succeeded")))
    mid = arc.machine_id(SUB, "win1")
    try:
        out = _run(arc.enable_entra_login(mid))
        assert out["provisioning_state"] == "Succeeded"
        put = [c for c in arm.calls if c.method == "PUT"][0]
        body = json.loads(put.content)
        assert put.url.path == f"{mid}/extensions/AADLogin"
        assert put.url.params["api-version"] == "2024-07-10"
        assert body["properties"]["type"] == "AADLoginForWindows"
        assert body["properties"]["publisher"] == "Microsoft.Azure.ActiveDirectory"
        assert body["properties"]["settings"] == {"mdmId": ""}
        _install(_Arm(ext_states=("Failed",)))
        _refuses(arc.enable_entra_login(mid), "enterpriseregistration.windows.net")
        _install(_Arm(machine=404))
        _refuses(arc.enable_entra_login(mid), "not found after onboarding")
    finally:
        arc.transport = None


# ── the job end to end ────────────────────────────────────────────────────────

def _stub_job(state_line):
    from web_dashboard.services import azure_service
    saved = (arc.preflight, arc.onboard, arc.enable_entra_login, azure_service.own_identity,
             azure_service.ensure_role_assignment)

    async def preflight():
        return SUB

    async def onboard(db, *, job_id, cloud, target, token, variables):
        assert token == "dashboard-arm-token"
        return 0, f'ok => {{"msg": "{state_line}"}}'

    async def ext(mid):
        return {"extension": "AADLoginForWindows", "provisioning_state": "Succeeded"}

    async def ident():
        return {"tid": "tenant-1"}
    granted = []

    async def grant(*, scope, role, principal_id, principal_type="ServicePrincipal"):
        granted.append((scope, role, principal_id))
        return {"created": True, "name": f"ra-{principal_id}", "scope": scope}
    arc.preflight, arc.onboard, arc.enable_entra_login = preflight, onboard, ext
    azure_service.own_identity, azure_service.ensure_role_assignment = ident, grant

    def restore():
        (arc.preflight, arc.onboard, arc.enable_entra_login, azure_service.own_identity,
         azure_service.ensure_role_assignment) = saved
    return granted, restore


def _deploy(db, cloud="gcp"):
    return job_service.create_job(db, "gce_deploy" if cloud == "gcp" else "ec2_deploy",
                                  "alice", metadata={"instance_name": "win1"})


def test_job_records_the_join_on_the_deploy_and_never_the_token():
    from web_dashboard.services import config_service, windows_server_hook as wsh
    db = SessionLocal()
    deploy = _deploy(db)
    arc_job_id = arc.queue(db, deploy_job_id=deploy.id, cloud="gcp", vm_name="win1",
                           target="10.0.0.5", created_by="alice")
    db.expire_all()
    assert job_service.get_job(db, deploy.id).metadata_dict["arc_join_job_id"] == arc_job_id
    granted, restore = _stub_job("VMDASH-ARC:CONNECTED Connected host=WIN-1 connect_rc=0")
    orig = config_service.get
    config_service.get = lambda key, *a, **k: {"azure_entra_vm_admin_group_ids": "g-admin",
                                               "azure_entra_vm_user_group_ids": "g-user"}.get(key, "")
    try:
        meta = job_service.get_job(db, arc_job_id).metadata_dict
        _run(arc.run(db, job_id=arc_job_id, meta=meta))
    finally:
        config_service.get = orig
        restore()
    db.expire_all()
    arc_row, dep = job_service.get_job(db, arc_job_id), job_service.get_job(db, deploy.id)
    assert arc_row.status == "completed", arc_row.error_message
    md = dep.metadata_dict
    mid = arc.machine_id(SUB, "win1")
    assert md["arc_machine_id"] == mid and md["arc_host"] == "WIN-1"
    assert md["entra_join"]["provisioning_state"] == "Succeeded"
    assert {a["group"] for a in md["entra_role_assignments"]} == {"g-admin", "g-user"}
    assert all(s == mid for s, _r, _p in granted)
    for row in db.query(Job).all():
        assert "dashboard-arm-token" not in json.dumps(row.metadata_dict)
        assert "dashboard-arm-token" not in (row.error_message or "")
    assert wsh.assign_login_roles is not None
    db.close()


def test_unsupported_os_is_a_named_warning_on_the_deploy():
    db = SessionLocal()
    deploy = _deploy(db, "aws")
    arc_job_id = arc.queue(db, deploy_job_id=deploy.id, cloud="aws", vm_name="win2",
                           target="10.0.0.6", created_by="alice")
    _granted, restore = _stub_job("VMDASH-ARC:UNSUPPORTED build=20348 type=Server")
    try:
        _run(arc.run(db, job_id=arc_job_id, meta=job_service.get_job(db, arc_job_id).metadata_dict))
    finally:
        restore()
    db.expire_all()
    assert job_service.get_job(db, arc_job_id).status == "failed"
    err = job_service.get_job(db, deploy.id).metadata_dict["entra_error"]
    assert "Windows Server 2025" in err and "arc_machine_id" not in job_service.get_job(db, deploy.id).metadata_dict
    db.close()


# ── destroy ───────────────────────────────────────────────────────────────────

def test_teardown_removes_roles_then_the_machine_and_leaves_the_device():
    from web_dashboard.services import azure_service
    mid = arc.machine_id(SUB, "win1")
    order = []

    async def del_ra(scope, name):
        order.append(("role", name))
    orig = azure_service.delete_role_assignment
    azure_service.delete_role_assignment = del_ra
    arm = _install(_Arm())
    try:
        meta = {"arc_machine_id": mid, "arc_host": "WIN-1",
                "entra_role_assignments": [{"scope": mid, "name": "ra-1", "created": True},
                                           {"scope": mid, "name": "ra-2", "created": False}]}
        result = {}
        _run(arc.teardown(meta, result))
        deletes = [c for c in arm.calls if c.method == "DELETE"]
        assert order == [("role", "ra-1")], "only assignments this join created"
        assert len(deletes) == 1 and deletes[0].url.path == mid
        assert result["arc_machine_deleted"] == mid and "WIN-1" in result["arc_note"]
        _install(_Arm(delete=409))
        result = {}
        _run(arc.teardown(meta, result))
        assert "was not deleted" in result["arc_error"]
        result = {}
        _run(arc.teardown({}, result))
        assert result == {}
    finally:
        azure_service.delete_role_assignment = orig
        arc.transport = None


# ── wiring ────────────────────────────────────────────────────────────────────

def test_worker_deploys_destroys_and_api_are_wired():
    from web_dashboard import jobs_worker
    from web_dashboard.models.aws import DeployRequest as AwsReq
    from web_dashboard.models.gcp import GCPBulkDeployRequest, GCPDeployRequest
    from web_dashboard.services import aws_vm_service, gcp_vm_service
    assert arc.JOB_TYPE in jobs_worker.MEDIUM_TYPES
    src = inspect.getsource(jobs_worker)
    assert 'job_type == "windows_arc_join"' in src and "windows_arc_service.run(" in src
    for mod in (aws_vm_service, gcp_vm_service):
        s = inspect.getsource(mod)
        assert "windows_arc_service.requested(" in s and "_queue_arc_join(" in s
        assert "windows_arc_service.teardown(" in s
    api = open(os.path.join(_ROOT, "web_dashboard", "api", "aws.py"), encoding="utf-8").read()
    assert api.count('"entra_join_mode": req.entra_join_mode') == 3
    for model in (AwsReq, GCPDeployRequest, GCPBulkDeployRequest):
        assert "entra_join_mode" in model.model_fields, model.__name__
    try:
        GCPDeployRequest.model_validate({**{k: "x" for k in ()}, "entra_join_mode": "bogus"})
    except Exception as e:  # noqa: BLE001
        assert "entra_join_mode" in str(e)
    for page in ("aws", "gcp"):
        html = open(os.path.join(_ROOT, "web_dashboard", "templates", page, "index.html"),
                    encoding="utf-8").read()
        assert html.count('value="arc">Entra join through Azure Arc') == 2, page
        assert "windows_arc_entra_default" in html


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
