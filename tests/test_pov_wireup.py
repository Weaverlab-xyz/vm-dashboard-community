"""Wiring a POV's VMs into its PRA tenant — the refusals, and the per-VM discipline.

Slice 6a. This is the first slice whose artifacts land in a **customer's appliance**, so
what is pinned here is mostly what stops one landing in the wrong place, or landing
somewhere this dashboard can no longer reach:

  * **The tenant is never the singleton**, and a partial override is refused rather than
    merged — a jump item created against one customer's host with another's client id is
    the silent cross-tenant mistake slice 4 exists to prevent.
  * **The POV's own Gateway, never the tenant's default.** A POV with no Gateway is
    refused, because the appliance-wide one has no route into the environment: the item
    would be created successfully and time out at session launch.
  * **The protocol follows the guest**, and an unknown OS is skipped rather than guessed.
  * **Each artifact is persisted the moment it exists**, and a VM that already has a jump
    item is never given a second one.
  * **One VM's failure is not the run's failure** — but a run where nothing worked is.
  * **The Password Safe half is independent of the PRA half.** A half-configured tenant
    skips it with a message; it never costs the jump items that already worked.
  * **The managed system names the Resource Broker as its application host** — the field
    that tells Password Safe to reach the VM THROUGH the broker rather than from the
    cloud tenant, which would fail on every rotation days later.
  * **Entitle needs an agent this dashboard does not install**, so a tenant that names no
    agent token is refused with that reason rather than registered against the install's
    own tenant — and never at the cost of the halves that worked.
  * **An Entitle state is scrubbed before it is stored.** Terraform records sensitive
    attributes as plaintext and this one holds an SSH private key INSIDE its
    `connection_json` blob — the agent-token mint is the one deliberate exception.

Uses a real SQLite database and a fake terraform layer. No network, no FastAPI.

Runs under pytest, or standalone:
    python tests/test_pov_wireup.py
"""
import asyncio
import os
import pathlib
import sys
import uuid

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-pov-wireup")

from web_dashboard import database as d  # noqa: E402

d.Base.metadata.create_all(bind=d.engine)

from web_dashboard.services import (bt_tenant_service, job_service,  # noqa: E402
                                    pov_env_service, pov_wireup as w,
                                    terraform_pra_service)


def _name(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _tenant(db, **opts):
    options = {"jump_group_name": "POV", "jumpoint_name": "appliance-default"}
    options.update(opts)
    return bt_tenant_service.create(
        db, kind="pra", name=_name("pra"), base_url="acme.beyondtrustcloud.com",
        client_id="cid", secret="sekrit", created_by="t", options=options)


def _env(db, *, tenant=None, gateway="pov-gw", **kw):
    env = d.PovEnvironment(platform="skytap", name=_name("poc"),
                           platform_environment_id="sky-1",
                           status=pov_env_service.STATUS_ACTIVE,
                           gateway_name=gateway,
                           pra_tenant_id=(tenant or {}).get("id"), **kw)
    db.add(env)
    db.commit()
    return env


def _vm(db, env, *, name="web01", os_family="linux", ip="10.9.0.10"):
    row = d.PovEnvironmentVM(environment_id=env.id, platform_vm_id=_name("vm"),
                             name=name, os_family=os_family, private_ip=ip)
    db.add(row)
    db.commit()
    return row


class _FakeTF:
    """Records what terraform would have been asked to do."""

    def __init__(self, **behaviour):
        self.b = behaviour
        self.calls = []

    async def provision_jump(self, **kw):
        self.calls.append(("shell", kw))
        if self.b.get("raises"):
            raise RuntimeError(self.b["raises"])
        return {"shell_jump_id": "101",
                "tf_state_json": self.b.get("state", '{"resources":[]}')}

    async def provision_rdp_jump(self, **kw):
        self.calls.append(("rdp", kw))
        if self.b.get("raises"):
            raise RuntimeError(self.b["raises"])
        return {"rdp_jump_id": "202", "vault_account_id": "303",
                "tf_state_json": self.b.get("state", '{"resources":[]}')}

    async def remove_jump(self, state, tenant=None):
        self.calls.append(("remove_shell", tenant))
        if self.b.get("remove_raises"):
            raise RuntimeError(self.b["remove_raises"])

    async def remove_rdp_jump(self, state, tenant=None):
        self.calls.append(("remove_rdp", tenant))
        if self.b.get("remove_raises"):
            raise RuntimeError(self.b["remove_raises"])


def _install(fake):
    original = {k: getattr(w.terraform_pra_service, k)
                for k in ("provision_jump", "provision_rdp_jump", "remove_jump",
                          "remove_rdp_jump")}
    for k in original:
        setattr(w.terraform_pra_service, k, getattr(fake, k))
    return original


def _restore(original):
    for k, v in original.items():
        setattr(w.terraform_pra_service, k, v)


# ── the tenant override ──────────────────────────────────────────────────────

def test_a_partial_tenant_override_is_refused_never_merged():
    """Merging would authenticate to one customer's appliance with another's client id —
    which succeeds or fails for reasons nobody can read."""
    try:
        terraform_pra_service._tf_env({"bt_api_host": "acme.example.com"})
        raise AssertionError("a partial override was accepted")
    except terraform_pra_service.TerraformPRAError as exc:
        assert "all required" in str(exc) or "part of its credentials" in str(exc)


def test_a_full_override_replaces_the_singletons():
    env = terraform_pra_service._tf_env(
        terraform_pra_service.tenant_env("acme.example.com", "cid", "sec"))
    assert env["TF_VAR_bt_host"] == "acme.example.com"
    assert env["TF_VAR_bt_client_id"] == "cid"
    assert env["TF_VAR_bt_client_secret"] == "sec"


def test_no_override_still_reads_the_singletons():
    """The compatibility contract: every existing caller passes nothing."""
    env = terraform_pra_service._tf_env()
    assert "TF_IN_AUTOMATION" in env      # it built an environment at all


def test_a_tenant_with_no_jump_group_is_refused():
    db = d.SessionLocal()
    tenant = _tenant(db, jump_group_name="")
    env = _env(db, tenant=tenant)
    try:
        w.tenant_override(db, env)
        raise AssertionError("a tenant with no Jump Group was accepted")
    except w.WireupError as exc:
        assert "Jump Group" in str(exc)
    finally:
        db.close()


# ── which Gateway they route through ────────────────────────────────────────

def test_a_pov_with_no_gateway_is_refused_with_the_reason():
    """The appliance-wide Gateway has no route into the environment, so the item would be
    created successfully and time out at session launch."""
    db = d.SessionLocal()
    env = _env(db, tenant=_tenant(db), gateway=None)
    try:
        w.gateway_name(env)
        raise AssertionError("a POV with no Gateway was accepted")
    except w.WireupError as exc:
        assert "route through" in str(exc)
    finally:
        db.close()


def test_the_jump_item_names_the_povs_gateway_not_the_tenants_default():
    db = d.SessionLocal()
    tenant = _tenant(db)          # its appliance-wide default is "appliance-default"
    env = _env(db, tenant=tenant, gateway="pov-gw")
    vm = _vm(db, env)
    fake = _FakeTF()
    original = _install(fake)
    try:
        asyncio.run(w.wire_vm(db, env, vm, tenant=w.tenant_override(db, env),
                              gateway=w.gateway_name(env)))
    finally:
        _restore(original)
        db.close()
    kind, kw = fake.calls[0]
    assert kw["jumpoint_name"] == "pov-gw"
    assert kw["jumpoint_name"] != "appliance-default"


# ── protocol follows the guest ───────────────────────────────────────────────

def test_a_linux_vm_gets_an_ssh_shell_jump():
    db = d.SessionLocal()
    env = _env(db, tenant=_tenant(db))
    vm = _vm(db, env, os_family="linux")
    fake = _FakeTF()
    original = _install(fake)
    try:
        asyncio.run(w.wire_vm(db, env, vm, tenant=w.tenant_override(db, env),
                              gateway="pov-gw"))
    finally:
        _restore(original)
    assert fake.calls[0][0] == "shell"
    assert fake.calls[0][1]["port"] == w.SSH_PORT
    db.refresh(vm)
    assert vm.pra_jump_id == "101"
    db.close()


def test_a_windows_vm_gets_an_rdp_jump():
    db = d.SessionLocal()
    env = _env(db, tenant=_tenant(db))
    vm = _vm(db, env, name="dc01", os_family="windows")
    fake = _FakeTF()
    original = _install(fake)
    try:
        asyncio.run(w.wire_vm(db, env, vm, tenant=w.tenant_override(db, env),
                              gateway="pov-gw"))
    finally:
        _restore(original)
    assert fake.calls[0][0] == "rdp"
    db.refresh(vm)
    assert vm.pra_jump_id == "202" and vm.vault_account_id == "303"
    db.close()


def test_an_unknown_os_is_skipped_with_a_reason_not_guessed():
    """A confident wrong answer builds an SSH jump to a Windows box, which fails at
    session launch in front of whoever clicked it."""
    db = d.SessionLocal()
    env = _env(db, tenant=_tenant(db))
    vm = _vm(db, env, os_family="")
    fake = _FakeTF()
    original = _install(fake)
    try:
        line = asyncio.run(w.wire_vm(db, env, vm, tenant=w.tenant_override(db, env),
                                     gateway="pov-gw"))
    finally:
        _restore(original)
    assert fake.calls == [], "it built a jump item anyway"
    assert "skipped" in line
    db.refresh(vm)
    assert "did not report an OS" in vm.wiring_error
    db.close()


def test_a_vm_with_no_address_is_skipped():
    db = d.SessionLocal()
    env = _env(db, tenant=_tenant(db))
    vm = _vm(db, env, ip="")
    fake = _FakeTF()
    original = _install(fake)
    try:
        line = asyncio.run(w.wire_vm(db, env, vm, tenant=w.tenant_override(db, env),
                                     gateway="pov-gw"))
    finally:
        _restore(original)
    assert fake.calls == []
    assert "skipped" in line
    db.close()


# ── per-VM discipline ────────────────────────────────────────────────────────

def test_a_vm_that_is_already_wired_is_never_given_a_second_item():
    """PRA will happily create two items with the same name pointing at the same host,
    and the second is invisible in this database."""
    db = d.SessionLocal()
    env = _env(db, tenant=_tenant(db))
    vm = _vm(db, env)
    vm.pra_jump_id = "999"
    db.commit()
    fake = _FakeTF()
    original = _install(fake)
    try:
        line = asyncio.run(w.wire_vm(db, env, vm, tenant=w.tenant_override(db, env),
                                     gateway="pov-gw"))
    finally:
        _restore(original)
        db.close()
    assert fake.calls == []
    assert "already wired" in line


def test_a_failure_records_the_reason_on_the_row_and_does_not_raise():
    db = d.SessionLocal()
    env = _env(db, tenant=_tenant(db))
    vm = _vm(db, env)
    original = _install(_FakeTF(raises="Jump Group 'POV' not found"))
    try:
        line = asyncio.run(w.wire_vm(db, env, vm, tenant=w.tenant_override(db, env),
                                     gateway="pov-gw"))
    finally:
        _restore(original)
    assert "FAILED" in line
    db.refresh(vm)
    assert "Jump Group" in vm.wiring_error
    assert vm.pra_jump_id is None
    db.close()


def test_a_jump_created_with_no_state_is_recorded_loudly():
    """The item exists in PRA and this database cannot destroy it — the one outcome that
    leaves an orphan nobody can reap from here."""
    db = d.SessionLocal()
    env = _env(db, tenant=_tenant(db))
    vm = _vm(db, env)
    original = _install(_FakeTF(state=""))
    try:
        line = asyncio.run(w.wire_vm(db, env, vm, tenant=w.tenant_override(db, env),
                                     gateway="pov-gw"))
    finally:
        _restore(original)
    assert "NO STATE" in line
    db.refresh(vm)
    assert vm.pra_jump_id == "101", "the id must be kept so it can be found by hand"
    assert "by hand" in vm.wiring_error
    db.close()


# ── the run ──────────────────────────────────────────────────────────────────

def _job(env_id):
    db = d.SessionLocal()
    job = job_service.create_job(db, job_type="pov_env_wireup", created_by="t",
                                 metadata={"environment_id": env_id})
    jid = job.id
    db.close()
    return jid


def _job_status(job_id):
    db = d.SessionLocal()
    j = db.query(d.Job).filter(d.Job.id == job_id).first()
    out = (j.status, j.error_message)
    db.close()
    return out


def test_one_vms_failure_does_not_stop_the_others():
    db = d.SessionLocal()
    env = _env(db, tenant=_tenant(db))
    _vm(db, env, name="web01")
    _vm(db, env, name="broken", ip="")      # skipped, not failed
    _vm(db, env, name="web02", ip="10.9.0.11")
    eid = env.id
    db.close()
    original = _install(_FakeTF())
    try:
        asyncio.run(w.run_env_wireup(_job(eid), {"environment_id": eid}))
    finally:
        _restore(original)
    db = d.SessionLocal()
    rows = db.query(d.PovEnvironmentVM).filter(
        d.PovEnvironmentVM.environment_id == eid).all()
    assert sum(1 for r in rows if r.pra_jump_id) == 2
    db.close()


def test_a_run_where_nothing_worked_is_a_failed_job():
    """A green job with zero artifacts is the one an operator does not go back and read."""
    db = d.SessionLocal()
    env = _env(db, tenant=_tenant(db))
    _vm(db, env, name="web01")
    eid = env.id
    db.close()
    jid = _job(eid)
    original = _install(_FakeTF(raises="boom"))
    try:
        asyncio.run(w.run_env_wireup(jid, {"environment_id": eid}))
    finally:
        _restore(original)
    status, error = _job_status(jid)
    assert status == "failed"
    assert "no VM could be wired" in error


def test_the_run_refuses_once_rather_than_per_vm():
    """Thirty identical failures in a job log hide the one line that matters."""
    db = d.SessionLocal()
    env = _env(db, tenant=_tenant(db), gateway=None)
    for i in range(3):
        _vm(db, env, name=f"web{i}", ip=f"10.9.0.1{i}")
    eid = env.id
    db.close()
    jid = _job(eid)
    fake = _FakeTF()
    original = _install(fake)
    try:
        asyncio.run(w.run_env_wireup(jid, {"environment_id": eid}))
    finally:
        _restore(original)
    status, error = _job_status(jid)
    assert status == "failed" and "Gateway" in error
    assert fake.calls == [], "it started wiring before checking the Gateway"


# ── teardown ─────────────────────────────────────────────────────────────────

def test_teardown_destroys_against_the_same_tenant_it_created_in():
    """A destroy pointed at another appliance authenticates fine, deletes nothing, and
    reports success."""
    db = d.SessionLocal()
    tenant = _tenant(db)
    env = _env(db, tenant=tenant)
    vm = _vm(db, env)
    vm.pra_jump_id = "101"
    vm.pra_jump_tf_state = '{"resources":[]}'
    db.commit()
    fake = _FakeTF()
    original = _install(fake)
    try:
        line = asyncio.run(w.teardown(db, env))
    finally:
        _restore(original)
    assert "Removed 1" in line
    kind, passed = fake.calls[0]
    assert kind == "remove_shell"
    assert passed["bt_api_host"] == "acme.beyondtrustcloud.com"
    db.refresh(vm)
    assert vm.pra_jump_id is None and vm.pra_jump_tf_state is None
    db.close()


def test_a_row_whose_destroy_failed_keeps_its_state_so_a_rerun_can_finish():
    db = d.SessionLocal()
    env = _env(db, tenant=_tenant(db))
    vm = _vm(db, env)
    vm.pra_jump_id = "101"
    vm.pra_jump_tf_state = '{"resources":[]}'
    db.commit()
    original = _install(_FakeTF(remove_raises="403"))
    try:
        line = asyncio.run(w.teardown(db, env))
    finally:
        _restore(original)
    assert "could not be destroyed" in line
    db.refresh(vm)
    assert vm.pra_jump_tf_state, "the state was cleared optimistically"
    db.close()


def test_teardown_with_an_unresolvable_tenant_says_what_was_left_behind():
    """The real shape of this: the tenant row was deleted after the POV was wired.

    Deliberately NOT "the POV names no tenant" — that case falls through to
    `bt_tenant_service.resolve`'s default/single-row steps and legitimately succeeds, so a
    test written that way passes or fails depending on how many tenants the shared test
    database happens to hold. Pointing at an id that does not exist is both hermetic and
    the case an operator actually hits.
    """
    db = d.SessionLocal()
    env = _env(db, tenant={"id": "deleted-tenant-id"})
    vm = _vm(db, env)
    vm.pra_jump_tf_state = '{"resources":[]}'
    db.commit()
    line = asyncio.run(w.teardown(db, env))
    assert "left in place" in line and "by hand" in line
    db.close()


def test_a_pov_that_named_no_tenant_falls_back_rather_than_refusing():
    """Slice 4's resolution order, seen from here — and worth pinning because it is the
    one place this feature does NOT refuse.

    `resolve` with no id falls through to the default row, then the only active row, then
    the install's singletons. That is deliberate (it is what lets a demo instance ignore
    the registry), and it means a POV nobody assigned a tenant to wires into whichever one
    is the default. On an instance with two and no default it refuses instead, which is
    the case that matters — guessing between two customers' appliances.
    """
    db = d.SessionLocal()
    only = _tenant(db)
    bt_tenant_service.set_default(db, only["id"])
    env = _env(db, tenant=None)
    resolved = w.tenant_override(db, env)
    assert resolved["label"] == only["name"], (
        "a POV with no tenant should resolve the default, not refuse")
    db.close()


def test_teardown_is_a_no_op_when_nothing_was_wired():
    db = d.SessionLocal()
    env = _env(db, tenant=_tenant(db))
    _vm(db, env)
    assert "No PRA jump items" in asyncio.run(w.teardown(db, env))
    db.close()


# ── the Password Safe half ───────────────────────────────────────────────────

def _ps_tenant(db, **opts):
    options = {"api_account_name": "svc", "workgroup": "POV",
               "linux_functional_account": "fa-linux",
               "windows_functional_account": "fa-windows"}
    options.update(opts)
    return bt_tenant_service.create(
        db, kind="password_safe", name=_name("ps"),
        base_url="acme.ps.beyondtrustcloud.com", client_id="cid", secret="sekrit",
        created_by="t", options=options)


class _FakePS:
    """Stands in for the two Password Safe services."""

    def __init__(self, **behaviour):
        self.b = behaviour
        self.registered = []

    async def get_workgroup_id(self, name, tenant=None):
        return "77"

    async def get_functional_account(self, name, tenant=None):
        return {"id": 5 if "linux" in name else 6,
                "platform_id": 11 if "linux" in name else 12,
                "platform_name": name, "account_name": name}

    async def register_managed_system(self, **kw):
        self.registered.append(kw)
        if self.b.get("raises"):
            raise RuntimeError(self.b["raises"])
        return {"managed_system_id": "900", "managed_account_id": "901",
                "tf_state_json": self.b.get("state", '{"resources":[]}'),
                "initial_password_seeded": True}

    async def deregister(self, state, tenant=None):
        self.registered.append(("deregister", tenant))
        if self.b.get("remove_raises"):
            raise RuntimeError(self.b["remove_raises"])


def _install_ps(fake):
    original = {
        "api_wg": w.ps_api_service.get_workgroup_id,
        "api_fa": w.ps_api_service.get_functional_account,
        "reg": w.ps_resource_service.register_managed_system,
        "dereg": w.ps_resource_service.deregister,
    }
    w.ps_api_service.get_workgroup_id = fake.get_workgroup_id
    w.ps_api_service.get_functional_account = fake.get_functional_account
    w.ps_resource_service.register_managed_system = fake.register_managed_system
    w.ps_resource_service.deregister = fake.deregister
    return original


def _restore_ps(original):
    w.ps_api_service.get_workgroup_id = original["api_wg"]
    w.ps_api_service.get_functional_account = original["api_fa"]
    w.ps_resource_service.register_managed_system = original["reg"]
    w.ps_resource_service.deregister = original["dereg"]


def _ps_env(db, **opts):
    tenant = _tenant(db)
    ps = _ps_tenant(db, **opts)
    env = _env(db, tenant=tenant)
    env.ps_tenant_id = ps["id"]
    env.ps_application_host_id = 4242
    db.commit()
    return env


def test_a_pov_with_no_password_safe_tenant_is_not_an_error():
    """The two halves are independent — a POV wired into PRA alone is a good POV."""
    db = d.SessionLocal()
    env = _env(db, tenant=_tenant(db))
    assert asyncio.run(w.ps_context(db, env)) == {}
    db.close()


def test_no_resource_broker_is_refused_with_the_reason():
    """Without it the platform reaches a private address from the cloud tenant and fails
    on every rotation — days later, on a schedule, rather than here."""
    db = d.SessionLocal()
    env = _ps_env(db)
    env.ps_application_host_id = None
    db.commit()
    try:
        asyncio.run(w.ps_context(db, env))
        raise AssertionError("a POV with no Resource Broker was accepted")
    except w.WireupError as exc:
        assert "Resource Broker" in str(exc) and "route" in str(exc)
    finally:
        db.close()


def test_a_tenant_with_no_workgroup_is_refused():
    db = d.SessionLocal()
    env = _ps_env(db, workgroup="")
    try:
        asyncio.run(w.ps_context(db, env))
        raise AssertionError("a tenant with no workgroup was accepted")
    except w.WireupError as exc:
        assert "workgroup" in str(exc)
    finally:
        db.close()


def test_the_managed_system_names_the_resource_broker_as_its_application_host():
    """The field that tells Password Safe to manage the host THROUGH the broker."""
    db = d.SessionLocal()
    env = _ps_env(db)
    vm = _vm(db, env)
    fake = _FakePS()
    original = _install_ps(fake)
    try:
        ps = asyncio.run(w.ps_context(db, env))
        asyncio.run(w.onboard_vm(db, env, vm, ps=ps))
    finally:
        _restore_ps(original)
    assert fake.registered[0]["application_host_id"] == 4242
    db.refresh(vm)
    assert vm.ps_managed_system_id == "900" and vm.ps_managed_account_id == "901"
    db.close()


def test_the_platform_id_comes_from_the_functional_account_not_from_here():
    """Password Safe derives the managed system's platform from the functional account,
    which is why the accounts are split by guest OS."""
    db = d.SessionLocal()
    env = _ps_env(db)
    linux = _vm(db, env, name="web01", os_family="linux")
    win = _vm(db, env, name="dc01", os_family="windows", ip="10.9.0.11")
    fake = _FakePS()
    original = _install_ps(fake)
    try:
        ps = asyncio.run(w.ps_context(db, env))
        asyncio.run(w.onboard_vm(db, env, linux, ps=ps))
        asyncio.run(w.onboard_vm(db, env, win, ps=ps))
    finally:
        _restore_ps(original)
        db.close()
    assert fake.registered[0]["platform_id"] == 11
    assert fake.registered[0]["port"] == w.SSH_PORT
    assert fake.registered[1]["platform_id"] == 12
    assert fake.registered[1]["port"] == w.RDP_PORT


def test_a_guest_with_no_functional_account_is_skipped_not_failed():
    """A POV with only Linux guests has no use for a Windows functional account."""
    db = d.SessionLocal()
    env = _ps_env(db, windows_functional_account="")
    vm = _vm(db, env, name="dc01", os_family="windows")
    fake = _FakePS()
    original = _install_ps(fake)
    try:
        ps = asyncio.run(w.ps_context(db, env))
        line = asyncio.run(w.onboard_vm(db, env, vm, ps=ps))
    finally:
        _restore_ps(original)
    assert "skipped" in line and fake.registered == []
    db.close()


def test_a_vm_already_onboarded_is_not_onboarded_twice():
    """Password Safe accepts a second managed system on the same address, and the second
    is invisible in this database."""
    db = d.SessionLocal()
    env = _ps_env(db)
    vm = _vm(db, env)
    vm.ps_managed_system_id = "555"
    db.commit()
    fake = _FakePS()
    original = _install_ps(fake)
    try:
        ps = asyncio.run(w.ps_context(db, env))
        line = asyncio.run(w.onboard_vm(db, env, vm, ps=ps))
    finally:
        _restore_ps(original)
    assert "already in Password Safe" in line and fake.registered == []
    db.close()


def test_a_password_safe_failure_does_not_touch_the_jump_item():
    """The two halves are independent, so one failing must not undo the other."""
    db = d.SessionLocal()
    env = _ps_env(db)
    vm = _vm(db, env)
    vm.pra_jump_id = "101"
    db.commit()
    original = _install_ps(_FakePS(raises="workgroup 77 not found"))
    try:
        ps = asyncio.run(w.ps_context(db, env))
        line = asyncio.run(w.onboard_vm(db, env, vm, ps=ps))
    finally:
        _restore_ps(original)
    assert "FAILED" in line
    db.refresh(vm)
    assert vm.pra_jump_id == "101", "the jump item was cleared by a Password Safe failure"
    assert "Password Safe" in vm.wiring_error
    db.close()


def test_a_half_configured_password_safe_tenant_does_not_fail_the_run():
    """The PRA half is independently useful — a POV whose tenant is half-configured
    should get its jump items rather than nothing."""
    db = d.SessionLocal()
    env = _ps_env(db, workgroup="")
    _vm(db, env, name="web01")
    eid = env.id
    db.close()
    jid = _job(eid)
    original = _install(_FakeTF())
    ps_original = _install_ps(_FakePS())
    try:
        asyncio.run(w.run_env_wireup(jid, {"environment_id": eid}))
    finally:
        _restore(original)
        _restore_ps(ps_original)
    status, _ = _job_status(jid)
    assert status == "completed"
    db = d.SessionLocal()
    row = db.query(d.PovEnvironmentVM).filter(
        d.PovEnvironmentVM.environment_id == eid).first()
    assert row.pra_jump_id, "the PRA half was skipped because Password Safe was misconfigured"
    db.close()


def test_teardown_offboards_the_managed_systems_too():
    db = d.SessionLocal()
    env = _ps_env(db)
    vm = _vm(db, env)
    vm.pra_jump_tf_state = '{"resources":[]}'
    vm.ps_registration_tf_state = '{"resources":[]}'
    vm.ps_managed_system_id = "900"
    db.commit()
    original = _install(_FakeTF())
    ps_original = _install_ps(_FakePS())
    try:
        line = asyncio.run(w.teardown(db, env))
    finally:
        _restore(original)
        _restore_ps(ps_original)
    assert "Off-boarded 1" in line and "Removed 1 PRA jump item" in line
    db.refresh(vm)
    assert vm.ps_registration_tf_state is None and vm.pra_jump_tf_state is None
    db.close()


def test_a_password_safe_offboard_failure_does_not_stop_the_jump_item_removal():
    """A managed system left behind is untidy; stopping the jump-item removal over it
    leaves something worse."""
    db = d.SessionLocal()
    env = _ps_env(db)
    vm = _vm(db, env)
    vm.pra_jump_tf_state = '{"resources":[]}'
    vm.ps_registration_tf_state = '{"resources":[]}'
    db.commit()
    original = _install(_FakeTF())
    ps_original = _install_ps(_FakePS(remove_raises="403"))
    try:
        line = asyncio.run(w.teardown(db, env))
    finally:
        _restore(original)
        _restore_ps(ps_original)
    assert "could not be" in line
    assert "Removed 1 PRA jump item" in line
    db.refresh(vm)
    assert vm.ps_registration_tf_state, "the state was cleared despite the failure"
    assert vm.pra_jump_tf_state is None
    db.close()


# ── the two tenant overrides ─────────────────────────────────────────────────

def test_a_partial_password_safe_override_is_refused_in_both_services():
    """Same rule as PRA, in both places a Password Safe credential is read."""
    from web_dashboard.services import ps_api_service, ps_resource_service
    try:
        ps_resource_service._tf_env(None, {"pscli_api_url": "x"})
        raise AssertionError("ps_resource_service accepted a partial override")
    except ps_resource_service.PSResourceError as exc:
        assert "all" in str(exc)
    try:
        ps_api_service._tcfg("pscli_api_url", {"pscli_api_url": "x"})
        raise AssertionError("ps_api_service accepted a partial override")
    except ps_api_service.PSApiError as exc:
        assert "all" in str(exc)


def test_a_full_password_safe_override_replaces_the_singletons():
    from web_dashboard.services import ps_resource_service
    env = ps_resource_service._tf_env(
        None, ps_resource_service.tenant_creds("https://t.example", "cid", "sec", "svc"))
    assert env["TF_VAR_ps_url"] == "https://t.example"
    assert env["TF_VAR_ps_api_account_name"] == "svc"


# ── the Entitle half ─────────────────────────────────────────────────────────

def _ent_tenant(db, **opts):
    options = {"owner_id": "own-1", "workflow_id": "wf-1",
               "agent_token_name": "pov-agent", "ssh_sudo_user": "ec2-user"}
    options.update(opts)
    return bt_tenant_service.create(
        db, kind="entitle", name=_name("ent"), base_url="https://api.entitle.io",
        client_id="", secret="ent-key", created_by="t", options=options)


class _FakeEntitle:
    def __init__(self, **behaviour):
        self.b = behaviour
        self.calls = []

    async def register_ssh_host(self, **kw):
        self.calls.append(kw)
        if self.b.get("raises"):
            raise RuntimeError(self.b["raises"])
        return {"integration_id": "int-1",
                "tf_state_json": self.b.get("state", '{"resources":[]}')}

    async def deregister(self, state, ctx=None):
        self.calls.append(("deregister", ctx))
        if self.b.get("remove_raises"):
            raise RuntimeError(self.b["remove_raises"])


def _install_ent(fake):
    original = {"reg": w.entitle_registration_service.register_ssh_host,
                "dereg": w.entitle_registration_service.deregister}
    w.entitle_registration_service.register_ssh_host = fake.register_ssh_host
    w.entitle_registration_service.deregister = fake.deregister
    return original


def _restore_ent(original):
    w.entitle_registration_service.register_ssh_host = original["reg"]
    w.entitle_registration_service.deregister = original["dereg"]


def _ent_env(db, *, key="-----BEGIN KEY-----", **opts):
    env = _env(db, tenant=_tenant(db))
    ent = _ent_tenant(db, **opts)
    env.entitle_tenant_id = ent["id"]
    db.commit()
    if key:
        w.set_entitle_key(env, key)
    return env


def test_a_pov_with_no_entitle_tenant_is_not_an_error():
    """Three independent halves — a POV without Entitle is a perfectly good POV."""
    db = d.SessionLocal()
    env = _env(db, tenant=_tenant(db))
    assert asyncio.run(w.entitle_context(db, env)) == {}
    db.close()


def test_a_tenant_with_no_agent_token_is_refused_naming_what_is_missing():
    """The one prerequisite this dashboard does not install — an Entitle agent inside the
    POV's network. Said in front of the operator rather than left to the provider."""
    db = d.SessionLocal()
    env = _ent_env(db, agent_token_name="")
    try:
        asyncio.run(w.entitle_context(db, env))
        raise AssertionError("a tenant with no agent token was accepted")
    except w.WireupError as exc:
        assert "agent" in str(exc) and "does not install" in str(exc)
    finally:
        db.close()


def test_a_tenant_missing_owner_or_workflow_is_refused():
    db = d.SessionLocal()
    env = _ent_env(db, owner_id="")
    try:
        asyncio.run(w.entitle_context(db, env))
        raise AssertionError("a tenant with no owner id was accepted")
    except w.WireupError as exc:
        assert "owner id" in str(exc)
    finally:
        db.close()


def test_a_pov_with_no_ssh_key_is_refused_with_the_reason():
    """Entitle authenticates with a key, not the password the platform holds."""
    db = d.SessionLocal()
    env = _ent_env(db, key="")
    try:
        asyncio.run(w.entitle_context(db, env))
        raise AssertionError("a POV with no key was accepted")
    except w.WireupError as exc:
        assert "key, not a password" in str(exc)
    finally:
        db.close()


def test_the_integration_is_always_private_and_carries_the_tenant_context():
    db = d.SessionLocal()
    env = _ent_env(db)
    vm = _vm(db, env)
    fake = _FakeEntitle()
    original = _install_ent(fake)
    try:
        ent = asyncio.run(w.entitle_context(db, env))
        asyncio.run(w.register_vm_entitle(db, env, vm, ent=ent))
    finally:
        _restore_ent(original)
    call = fake.calls[0]
    assert call["private"] is True, "a POV VM is on a private network by construction"
    assert call["ctx"].api_key == "ent-key"
    assert call["ctx"].hcl["agent_token_name"] == "pov-agent"
    assert call["sudo_user"] == "ec2-user"
    db.refresh(vm)
    assert vm.entitle_integration_id == "int-1"
    db.close()


def test_a_windows_vm_is_skipped_because_the_app_mints_accounts_over_ssh():
    db = d.SessionLocal()
    env = _ent_env(db)
    vm = _vm(db, env, name="dc01", os_family="windows")
    fake = _FakeEntitle()
    original = _install_ent(fake)
    try:
        ent = asyncio.run(w.entitle_context(db, env))
        line = asyncio.run(w.register_vm_entitle(db, env, vm, ent=ent))
    finally:
        _restore_ent(original)
    assert "skipped Entitle" in line and fake.calls == []
    db.close()


def test_a_vm_already_registered_is_not_registered_twice():
    db = d.SessionLocal()
    env = _ent_env(db)
    vm = _vm(db, env)
    vm.entitle_integration_id = "int-existing"
    db.commit()
    fake = _FakeEntitle()
    original = _install_ent(fake)
    try:
        ent = asyncio.run(w.entitle_context(db, env))
        line = asyncio.run(w.register_vm_entitle(db, env, vm, ent=ent))
    finally:
        _restore_ent(original)
    assert "already in Entitle" in line and fake.calls == []
    db.close()


def test_an_entitle_failure_does_not_touch_the_other_two_artifacts():
    db = d.SessionLocal()
    env = _ent_env(db)
    vm = _vm(db, env)
    vm.pra_jump_id = "101"
    vm.ps_managed_system_id = "900"
    db.commit()
    original = _install_ent(_FakeEntitle(raises="owner own-1 not found"))
    try:
        ent = asyncio.run(w.entitle_context(db, env))
        line = asyncio.run(w.register_vm_entitle(db, env, vm, ent=ent))
    finally:
        _restore_ent(original)
    assert "FAILED" in line
    db.refresh(vm)
    assert vm.pra_jump_id == "101" and vm.ps_managed_system_id == "900"
    assert "Entitle" in vm.wiring_error
    db.close()


def test_an_entitle_tenant_with_no_agent_does_not_fail_the_run():
    """The PRA half is independently useful, and the agent is a prerequisite the operator
    supplies rather than a fault in the POV."""
    db = d.SessionLocal()
    env = _ent_env(db, agent_token_name="")
    _vm(db, env, name="web01")
    eid = env.id
    db.close()
    jid = _job(eid)
    original = _install(_FakeTF())
    ent_original = _install_ent(_FakeEntitle())
    try:
        asyncio.run(w.run_env_wireup(jid, {"environment_id": eid}))
    finally:
        _restore(original)
        _restore_ent(ent_original)
    status, _ = _job_status(jid)
    assert status == "completed"
    db = d.SessionLocal()
    row = db.query(d.PovEnvironmentVM).filter(
        d.PovEnvironmentVM.environment_id == eid).first()
    assert row.pra_jump_id, "the PRA half was skipped because Entitle was not ready"
    assert row.entitle_integration_id is None
    db.close()


def test_teardown_removes_the_integrations_first_and_clears_the_key():
    """An integration is standing ACCESS, so it is the artifact whose lingering matters
    most."""
    db = d.SessionLocal()
    env = _ent_env(db)
    vm = _vm(db, env)
    vm.pra_jump_tf_state = '{"resources":[]}'
    vm.entitle_tf_state = '{"resources":[]}'
    vm.entitle_integration_id = "int-1"
    db.commit()
    original = _install(_FakeTF())
    ent_original = _install_ent(_FakeEntitle())
    try:
        line = asyncio.run(w.teardown(db, env))
    finally:
        _restore(original)
        _restore_ent(ent_original)
    assert line.index("Entitle integration") < line.index("PRA jump item")
    assert "Cleared the stored Entitle SSH key" in line
    db.refresh(vm)
    assert vm.entitle_tf_state is None and vm.entitle_integration_id is None
    assert not w.has_entitle_key(env)
    db.close()


def test_a_failed_entitle_removal_keeps_its_state_and_does_not_stop_the_rest():
    db = d.SessionLocal()
    env = _ent_env(db)
    vm = _vm(db, env)
    vm.pra_jump_tf_state = '{"resources":[]}'
    vm.entitle_tf_state = '{"resources":[]}'
    db.commit()
    original = _install(_FakeTF())
    ent_original = _install_ent(_FakeEntitle(remove_raises="403"))
    try:
        line = asyncio.run(w.teardown(db, env))
    finally:
        _restore(original)
        _restore_ent(ent_original)
    assert "could not be" in line and "Removed 1 PRA jump item" in line
    db.refresh(vm)
    assert vm.entitle_tf_state, "the state was cleared despite the failure"
    db.close()


def test_an_entitle_context_with_no_api_key_is_refused_by_the_service():
    """The same partial-override rule the PRA and Password Safe services follow."""
    from web_dashboard.services import entitle_registration_service as ers
    try:
        ers._tf_env(None, ers.tenant_ctx(api_key="", endpoint="https://api.entitle.io"))
        raise AssertionError("a context with no api key was accepted")
    except ers.EntitleRegistrationError as exc:
        assert "no API key" in str(exc)


def test_the_keyless_refusal_fires_before_anything_is_written():
    """The refusal moved off the HCL narrowing and onto the key accessor, so this pins
    the property that actually matters: every apply and destroy path reaches `_tf_env`
    before it writes a file, so a cross-tenant context is refused with nothing on disk."""
    from web_dashboard.services import entitle_registration_service as ers
    for fn, args in ((ers._apply_hcl_sync, ("# hcl", {})),
                     (ers._destroy_sync, ('{"resources": []}',))):
        try:
            fn(*args, ers.tenant_ctx(api_key=" "))
            raise AssertionError(f"{fn.__name__} accepted a keyless context")
        except ers.EntitleRegistrationError as exc:
            assert "no API key" in str(exc)


def test_the_hcl_generators_are_never_handed_the_api_key():
    """The generators turn their input into a file on disk. The key rides
    `TF_VAR_entitle_api_key` — that is what the `variable` block is for — so it lives
    on a SEPARATE attribute of the context rather than another key of the same dict. That
    is the point: `hcl` is an object the key was never in, so this is a property of the
    shape rather than of which lookups the current code happens to perform."""
    from web_dashboard.services import entitle_registration_service as ers
    ctx = ers.tenant_ctx(api_key="SUPERSECRET", endpoint="https://t.example",
                         owner_id="o", workflow_id="wf", agent_token_name="a")
    assert "SUPERSECRET" not in str(ctx.hcl)
    fields = ers._hcl_fields(ctx)
    assert "SUPERSECRET" not in str(fields)
    hcl = ers._generate_ssh_hcl(name="n", hostname="h", sudo_user="u", port=22,
                                private=True, fields=fields)
    assert "SUPERSECRET" not in hcl
    # …and everything the HCL legitimately needs still arrives. Asserted on the
    # rendered attribute rather than the bare URL: a substring test for a URL anywhere in
    # a blob is the shape of a sanitization check, which is both a weaker assertion and
    # one CodeQL flags on sight.
    assert 'endpoint = "https://t.example"' in hcl
    assert '"a"' in hcl and '"o"' in hcl


def test_the_entitle_context_reaches_both_the_env_and_the_hcl():
    """Unlike PRA and Password Safe, an Entitle registration's DESTINATION is written into
    the HCL rather than the environment — so a credential-only override would have left it
    pointing at the install's own tenant."""
    from web_dashboard.services import entitle_registration_service as ers
    ctx = ers.tenant_ctx(api_key="k", endpoint="https://tenant.example",
                         owner_id="o", workflow_id="wf", agent_token_name="a")
    assert ers._tf_env(None, ctx)["TF_VAR_entitle_api_key"] == "k"
    fields = ers._hcl_fields(ctx)
    assert "k" not in fields.values(), "the key does not travel with the destination"
    assert ers._provider_endpoint(fields) == "https://tenant.example"
    attrs = ers._common_attrs_hcl(True, fields=fields)
    assert "o" in attrs and "wf" in attrs and "a" in attrs


def test_the_entitle_state_is_scrubbed_before_it_is_stored():
    """Terraform records sensitive attributes as PLAINTEXT, and this state is stashed in
    the database for the life of the POV. The SSH private key lives INSIDE the
    `connection_json` blob, so redacting attribute names alone would reach nothing —
    which is what CodeQL caught.
    """
    import json as _json
    from web_dashboard.services import entitle_registration_service as ers
    state = _json.dumps({"resources": [{"type": "entitle_integration", "instances": [
        {"attributes": {
            "name": "poc-web01",
            "connection_json": _json.dumps({
                "host": "10.9.0.10", "user": "ec2-user",
                "key": "-----BEGIN OPENSSH PRIVATE KEY-----abc123"}),
        }}]}]})
    out = ers._scrub_state(state)
    assert "BEGIN OPENSSH" not in out and "abc123" not in out
    # Everything that is not a secret survives, because a destroy reads the resource ids
    # out of this and an operator reads the rest.
    assert "10.9.0.10" in out and "ec2-user" in out and "poc-web01" in out


def test_the_scrubber_fails_closed():
    """Dropping the state costs an automated teardown; keeping an unscrubbable one costs
    a plaintext key at rest. The first is the better failure."""
    from web_dashboard.services import entitle_registration_service as ers
    assert ers._scrub_state("not json at all") is None
    assert ers._scrub_state("") is None


def test_an_unknown_connector_blob_is_left_alone_rather_than_mangled():
    import json as _json
    from web_dashboard.services import entitle_registration_service as ers
    state = _json.dumps({"resources": [{"type": "entitle_integration", "instances": [
        {"attributes": {"connection_json": "a plain string, not an object"}}]}]})
    assert "a plain string, not an object" in ers._scrub_state(state)


def test_the_destroy_path_scrubs_before_writing_the_state_to_disk():
    """Two real cases reach a destroy with secrets intact: a row written before the
    scrubber existed, and a state this module did not produce. The destroy works from
    resource ids, so redacting first means no credential is written to disk at all."""
    import json as _json
    from web_dashboard.services import entitle_registration_service as ers

    written = {}

    class _FakePath:
        def __init__(self, *parts):
            self.name = parts[-1]

        def write_text(self, text):
            written[self.name] = text

    original_path, original_run = ers.Path, ers._run_tf
    original_chmod = ers._chmod_600
    ers.Path = _FakePath
    ers._chmod_600 = lambda p: None

    class _Done:
        returncode = 0
        stderr = stdout = ""
    ers._run_tf = lambda *a, **k: _Done()
    try:
        ers._destroy_sync(_json.dumps({"resources": [{"type": "entitle_integration",
            "instances": [{"attributes": {"connection_json": _json.dumps(
                {"host": "10.9.0.10", "key": "-----BEGIN KEY-----leaky"})}}]}]}))
    finally:
        ers.Path, ers._run_tf = original_path, original_run
        ers._chmod_600 = original_chmod

    state = written["terraform.tfstate"]
    assert "leaky" not in state and "BEGIN KEY" not in state
    assert "10.9.0.10" in state, "the destroy still needs the resource to be identifiable"


def test_the_agent_token_mint_is_deliberately_not_scrubbed():
    """Entitle returns an agent token's value only at creation, and
    `_agent_token_from_state` recovers it when the stored ref resolves empty. Redacting it
    would turn a recoverable mint into a hard `400 Resource already exists`."""
    src = (pathlib.Path(_ROOT) / "web_dashboard" / "services"
           / "entitle_registration_service.py").read_text(encoding="utf-8")
    mint = src.split("_agent_token_hcl(name)", 1)[1][:200]
    assert "False" in mint, "the agent-token mint no longer opts out of scrubbing"


# ── the job type ─────────────────────────────────────────────────────────────

def test_the_job_type_runs_locally_and_not_on_an_agent():
    """Terraform runs in the worker process, which is why a POV instance needs no Docker
    socket for this — and why it must not be an agent job type."""
    from web_dashboard import jobs_worker
    from web_dashboard.services import agent_service
    assert "pov_env_wireup" in jobs_worker.HANDLED_TYPES
    assert "pov_env_wireup" not in agent_service.AGENT_JOB_TYPES


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
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
