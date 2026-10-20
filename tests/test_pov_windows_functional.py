"""The AWS Windows functional account: a local account the wire-up puts on every guest.

EC2 gives every Windows instance its own Administrator password, so the "guests agree on
one login" rule ``pov_functional_account`` mints from can never hold on AWS. What is
pinned here is the replacement and what keeps it from going quietly wrong:

  * **The password never reaches a job row.** The prepare run binds a var NAME to a
    config key, and the bundle resolves it when the agent asks.
  * **The functional account is the dashboard's own account**, minted only once a guest
    has it. Linux on AWS keeps today's refusal.
  * **Administrator is what gets onboarded**, seeded with EC2's password, and rotated
    against the POV's tenant at once.
  * **Later runs log on as the local account**, because EC2's copy is stale after that
    rotation. A typed login still wins.
  * **A guest that could not be prepared is skipped, never failed.**
  * **Teardown forgets the password** only once nothing in the tenant still uses it.

Uses a real SQLite database and fakes for Password Safe and EC2. No network, no FastAPI.

Runs under pytest, or standalone:
    python tests/test_pov_windows_functional.py
"""
import asyncio
import base64
import json
import os
import sys
import uuid

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-pov-winfa")

from web_dashboard import database as d  # noqa: E402
from web_dashboard.utils.clock import utcnow  # noqa: E402

d.Base.metadata.create_all(bind=d.engine)

from web_dashboard.services import (agent_ansible_bundle, agent_service,  # noqa: E402
                                    bt_tenant_service, config_service, job_service,
                                    pov_cloud_aws, pov_cloud_env, pov_env_service,
                                    pov_functional_account as fa, pov_resource_broker as rb,
                                    pov_windows_functional as fn, ps_api_service,
                                    pov_wireup as w)

_PS_HOST = "acme.ps.beyondtrustcloud.com"
_EC2_PASSWORD = "Ec2-Generated-Admin9"


def _name(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


# ── fixtures ─────────────────────────────────────────────────────────────────

def _agent(db):
    row, _code = agent_service.create_agent(db, name=_name("brk"), created_by="t")
    row.public_key = "k"
    row.agent_version = "2.4.0"
    row.reported_job_types = json.dumps(["agent_discover", "agent_gateway",
                                         "agent_ansible"])
    row.last_seen_at = utcnow()
    db.commit()
    return row


def _env(db, *, tenant=True, keyed=True):
    """An AWS POV with a control node, a Password Safe tenant and the Windows key."""
    ps_id = None
    if tenant:
        ps_id = bt_tenant_service.create(
            db, kind="password_safe", name=_name("ps"), base_url=_PS_HOST,
            client_id="ps-cid", secret="ps-sekrit", created_by="t",
            options={"api_account_name": "svc", "workgroup": "POV",
                     "linux_functional_account": "",
                     "windows_functional_account": ""})["id"]
    env = d.PovEnvironment(platform="aws", name=_name("poc"),
                           platform_environment_id=f"pov-{uuid.uuid4().hex[:8]}",
                           status=pov_env_service.STATUS_ACTIVE,
                           broker_agent_id=_agent(db).id, ps_tenant_id=ps_id)
    db.add(env)
    db.commit()
    if keyed:
        pov_cloud_aws._windows_key_pem(env.platform_environment_id)
    return env


def _vm(db, env, *, name="app01", os_family="windows", ip="10.9.0.20"):
    row = d.PovEnvironmentVM(environment_id=env.id, platform_vm_id=_name("i"),
                             name=name, os_family=os_family, private_ip=ip)
    db.add(row)
    db.commit()
    return row


def _published(env_id, password=_EC2_PASSWORD):
    """What GetPasswordData returns: the password encrypted to the POV's key."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    pem = pov_cloud_aws._windows_key_pem(env_id)
    public = serialization.load_pem_private_key(pem.encode(), password=None).public_key()
    return base64.b64encode(public.encrypt(password.encode(), padding.PKCS1v15())).decode()


class _Patched:
    """Swap attributes for a `with` block and put them all back."""

    def __init__(self, *swaps):
        self.swaps = swaps

    def __enter__(self):
        self.saved = [(obj, name, getattr(obj, name)) for obj, name, _ in self.swaps]
        for obj, name, value in self.swaps:
            setattr(obj, name, value)
        return self

    def __exit__(self, *exc):
        for obj, name, value in reversed(self.saved):
            setattr(obj, name, value)
        return False


def _ec2(env):
    """EC2 publishing the Administrator password for every instance of this POV."""
    blob = _published(env.platform_environment_id)
    return ((pov_cloud_aws, "_password_data_sync", lambda _r, _v: blob),
            (pov_cloud_env, "recorded_region", lambda _e, _c: "us-east-1"))


class _FakePS:
    def __init__(self, **behaviour):
        self.b = behaviour
        self.created, self.registered, self.rotated = [], [], []

    async def get_workgroup_id(self, name, tenant=None):
        return "77"

    async def get_platform_id(self, name_or_id, tenant=None):
        return 12 if str(name_or_id).lower() == "windows" else 11

    async def get_functional_account(self, name, tenant=None):
        return {"id": int(name), "platform_id": 12, "platform_name": "Windows",
                "account_name": fn.ACCOUNT}

    async def create_functional_account_on_platform(self, **kw):
        self.created.append(kw)
        return 900 + len(self.created)

    async def delete_functional_account(self, account_id, tenant=None):
        pass

    async def register_managed_system(self, **kw):
        self.registered.append(kw)
        return {"managed_system_id": "800", "managed_account_id": "801",
                "tf_state_json": '{"resources":[]}', "initial_password_seeded": True}

    async def deregister(self, state, tenant=None):
        pass

    async def change_managed_account_password(self, account_id, tenant=None):
        self.rotated.append((int(account_id), tenant))
        if self.b.get("rotate_raises"):
            raise RuntimeError("tenant says no")

    def swaps(self):
        return ((ps_api_service, "get_workgroup_id", self.get_workgroup_id),
                (ps_api_service, "get_platform_id", self.get_platform_id),
                (ps_api_service, "get_functional_account", self.get_functional_account),
                (ps_api_service, "create_functional_account_on_platform",
                 self.create_functional_account_on_platform),
                (ps_api_service, "delete_functional_account",
                 self.delete_functional_account),
                (ps_api_service, "change_managed_account_password",
                 self.change_managed_account_password),
                (w.ps_resource_service, "register_managed_system",
                 self.register_managed_system),
                (w.ps_resource_service, "deregister", self.deregister))


# ── the play ─────────────────────────────────────────────────────────────────

def test_the_play_is_a_registered_builtin_and_hides_its_password():
    import yaml
    text = agent_ansible_bundle.builtin_playbook(fn.ASSET)
    play = yaml.safe_load(text)[0]
    tasks = {t["name"]: t for t in play["tasks"]}
    user = next(t for t in tasks.values() if "ansible.windows.win_user" in t)
    assert user["no_log"] is True, "the module args carry the password"
    assert fn.PASSWORD_VAR in user["ansible.windows.win_user"]["password"]
    assert fn.USER_VAR in user["ansible.windows.win_user"]["name"]
    reg = next(t for t in tasks.values() if "ansible.windows.win_regedit" in t)
    assert reg["ansible.windows.win_regedit"]["name"] == "LocalAccountTokenFilterPolicy"


def test_the_prepare_job_carries_a_var_name_and_never_the_password():
    db = d.SessionLocal()
    env = _env(db)
    vm = _vm(db, env)
    job = fn.queue_prepare(db, env, vm)
    meta = job.metadata_dict
    secret = fn.password(env)
    assert secret not in json.dumps(meta)
    assert meta["secret_vars"] == {fn.PASSWORD_VAR: fn.password_config_key(env.id)}
    assert meta["extra_vars"][fn.USER_VAR] == fn.ACCOUNT
    assert meta["asset"] == fn.ASSET and meta["transport"] == "winrm"
    assert meta["target_host"] == vm.private_ip
    assert meta["pov_vm_id"] == vm.platform_vm_id
    db.close()


def test_the_password_is_generated_once():
    db = d.SessionLocal()
    env = _env(db)
    assert fn.password(env) == fn.password(env)
    db.close()


# ── waiting on the runs ──────────────────────────────────────────────────────

def test_ensure_prepared_marks_successes_and_returns_failures():
    db = d.SessionLocal()
    env = _env(db)
    good = _vm(db, env, name="app01")
    bad = _vm(db, env, name="app02", ip="10.9.0.21")
    real_queue = fn.queue_prepare

    def queue_and_finish(db_, env_, vm, **kw):
        job = real_queue(db_, env_, vm, **kw)
        if vm.id == good.id:
            job_service.set_completed(db_, job.id, {})
        else:
            job.status, job.error_message = "failed", "WinRM timed out"
            db_.commit()
        return job

    lines = []
    with _Patched((fn, "queue_prepare", queue_and_finish)):
        problems = asyncio.run(fn.ensure_prepared(db, env, log=lines.append, poll_s=0))
    assert fn.prepared(env, good) and not fn.prepared(env, bad)
    assert list(problems) == ["app02"] and "WinRM timed out" in problems["app02"]
    # A second pass re-runs only the guest that still lacks it.
    queued = []
    with _Patched((fn, "queue_prepare",
                   lambda db_, env_, vm, **kw: queued.append(vm.name) or
                   queue_and_finish(db_, env_, vm, **kw))):
        asyncio.run(fn.ensure_prepared(db, env, poll_s=0))
    assert queued == ["app02"]
    db.close()


def test_a_run_still_waiting_at_the_deadline_is_a_problem_not_a_hang():
    db = d.SessionLocal()
    env = _env(db)
    _vm(db, env)
    problems = asyncio.run(fn.ensure_prepared(db, env, poll_s=0, timeout_s=0))
    assert "still waiting" in problems["app01"]
    db.close()


# ── the grant ────────────────────────────────────────────────────────────────

def test_every_windows_guest_is_granted_when_there_is_a_tenant():
    db = d.SessionLocal()
    env = _env(db)
    _vm(db, env, name="app01", ip="10.9.0.20")
    _vm(db, env, name="dc01", ip="10.9.0.21")
    _vm(db, env, name="lin", os_family="linux", ip="10.9.0.22")
    assert fn.windows_targets(db, env) == ["10.9.0.20", "10.9.0.21"]
    bare = _env(db, tenant=False)
    _vm(db, bare)
    assert fn.windows_targets(db, bare) == [], "no tenant, nothing to onboard into"
    db.close()


# ── the functional account in Password Safe ──────────────────────────────────

def test_the_windows_account_is_minted_from_the_local_account():
    db = d.SessionLocal()
    env = _env(db)
    _vm(db, env)
    ps = _FakePS()
    with _Patched(*ps.swaps()):
        got = asyncio.run(fa.ensure(db, env, "windows"))
    assert got["account_name"] == fn.ACCOUNT
    assert ps.created[0]["account_name"] == fn.ACCOUNT
    assert ps.created[0]["password"] == fn.password(env)
    db.close()


def test_linux_on_aws_keeps_its_refusal():
    db = d.SessionLocal()
    env = _env(db)
    _vm(db, env, name="lin", os_family="linux")
    with _Patched(*_FakePS().swaps()):
        try:
            asyncio.run(fa.ensure(db, env, "linux"))
            raise AssertionError("a Linux account was minted from nothing")
        except fa.FunctionalAccountError as exc:
            assert "does not store VM credentials" in str(exc)
    db.close()


def test_no_prepared_guest_means_nothing_is_minted():
    db = d.SessionLocal()
    env = _env(db)
    _vm(db, env)
    ps = _FakePS()
    with _Patched(*ps.swaps()):
        ctx = asyncio.run(w.ps_context(db, env))
    assert not ps.created
    assert fn.ACCOUNT in ctx["account_problems"]["windows"]
    db.close()


# ── onboarding and rotation ──────────────────────────────────────────────────

def _ctx(db, env, ps):
    with _Patched(*ps.swaps()):
        return asyncio.run(w.ps_context(db, env))


def test_an_unprepared_guest_is_skipped_with_the_runs_reason():
    db = d.SessionLocal()
    env = _env(db)
    vm = _vm(db, env)
    ps = _FakePS()
    ctx = _ctx(db, env, ps)
    ctx["prep_problems"] = {vm.name: "the run that creates povpsfunc failed: timeout"}
    with _Patched(*ps.swaps()):
        line = asyncio.run(w.onboard_vm(db, env, vm, ps=ctx))
    assert "skipped" in line and "timeout" in line and "FAILED" not in line
    assert not ps.registered
    db.close()


def test_administrator_is_onboarded_with_ec2s_password_then_rotated_on_the_tenant():
    db = d.SessionLocal()
    env = _env(db)
    vm = _vm(db, env)
    fn.mark_prepared(db, env, vm)
    ps = _FakePS()
    ctx = _ctx(db, env, ps)
    with _Patched(*ps.swaps(), *_ec2(env)):
        line = asyncio.run(w.onboard_vm(db, env, vm, ps=ctx))
    reg = ps.registered[0]
    assert reg["managed_account_name"] == "Administrator"
    assert reg["initial_password"] == _EC2_PASSWORD, "seeded the service account instead"
    assert ps.rotated == [(801, ctx["api"])], "rotated against the wrong tenant"
    assert fn.rotated(env, vm) and "Rotated" in line
    db.close()


def test_a_failed_rotation_is_reported_and_retried_not_failed():
    db = d.SessionLocal()
    env = _env(db)
    vm = _vm(db, env)
    fn.mark_prepared(db, env, vm)
    ps = _FakePS(rotate_raises=True)
    ctx = _ctx(db, env, ps)
    with _Patched(*ps.swaps(), *_ec2(env)):
        line = asyncio.run(w.onboard_vm(db, env, vm, ps=ctx))
    assert "Rotating it failed" in line and "FAILED" not in line
    assert not fn.rotated(env, vm)
    ps.b["rotate_raises"] = False
    with _Patched(*ps.swaps()):
        again = asyncio.run(w.onboard_vm(db, env, vm, ps=ctx))
    assert "already in Password Safe" in again and fn.rotated(env, vm)
    db.close()


def test_the_wireup_prepares_before_it_mints():
    """Source-level: a run that minted first would put a credential in the tenant that
    works on no guest yet."""
    import inspect
    body = inspect.getsource(w.run_env_wireup)
    assert body.index("ensure_prepared(") < body.index("ps_context(db, env)")


# ── which login later runs use ───────────────────────────────────────────────

def test_a_prepared_guest_logs_on_as_the_local_account():
    db = d.SessionLocal()
    env = _env(db)
    vm = _vm(db, env)
    with _Patched(*_ec2(env)):
        before = asyncio.run(rb.platform_login(db, env.id, vm.platform_vm_id))
        fn.password(env)   # queue_prepare generates it before the account exists
        fn.mark_prepared(db, env, vm)
        after = asyncio.run(rb.platform_login(db, env.id, vm.platform_vm_id))
        admin = asyncio.run(rb.platform_login(db, env.id, vm.platform_vm_id,
                                              service_account=False))
    assert before == ("Administrator", _EC2_PASSWORD)
    assert after == (fn.ACCOUNT, fn.password(env))
    assert admin == ("Administrator", _EC2_PASSWORD)
    db.close()


def test_a_typed_login_still_wins():
    db = d.SessionLocal()
    env = _env(db)
    vm = _vm(db, env)
    fn.mark_prepared(db, env, vm)
    fn.password(env)
    pov_env_service.set_vm_login(db, env, vm.platform_vm_id, "rbadmin", "Fixed-1")
    got = asyncio.run(rb.platform_login(db, env.id, vm.platform_vm_id))
    assert got == ("rbadmin", "Fixed-1")
    db.close()


# ── teardown ─────────────────────────────────────────────────────────────────

def test_teardown_forgets_the_password_once_the_tenant_no_longer_needs_it():
    db = d.SessionLocal()
    env = _env(db)
    vm = _vm(db, env)
    fn.mark_prepared(db, env, vm)
    fn.password(env)
    vm.ps_registration_tf_state = '{"resources":[]}'
    env.ps_windows_functional_account_id = 901
    db.commit()
    ps = _FakePS()
    with _Patched(*ps.swaps()):
        asyncio.run(w._teardown_password_safe(db, env, [vm]))
    assert config_service.get_opt(fn.password_config_key(env.id)) is None
    assert not fn.prepared(env, vm)
    db.close()


def test_a_surviving_functional_account_keeps_its_password():
    db = d.SessionLocal()
    env = _env(db)
    vm = _vm(db, env)
    fn.password(env)
    vm.ps_registration_tf_state = '{"resources":[]}'
    env.ps_windows_functional_account_id = 901
    db.commit()
    ps = _FakePS()

    async def stuck(account_id, tenant=None):
        raise RuntimeError("still referenced")
    with _Patched(*ps.swaps(), (ps_api_service, "delete_functional_account", stuck)):
        asyncio.run(w._teardown_password_safe(db, env, [vm]))
    assert config_service.get_opt(fn.password_config_key(env.id))
    db.close()


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for f in fns:
        try:
            f()
            print(f"ok   {f.__name__}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"FAIL {f.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
