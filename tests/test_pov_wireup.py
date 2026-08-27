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

Uses a real SQLite database and a fake terraform layer. No network, no FastAPI.

Runs under pytest, or standalone:
    python tests/test_pov_wireup.py
"""
import asyncio
import os
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
    assert "TF_PLUGIN_CACHE_DIR" in env   # it built an environment at all


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
