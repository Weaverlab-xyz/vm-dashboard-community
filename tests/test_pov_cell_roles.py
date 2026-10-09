"""Demo-cell roles on a POV guest -- what the wire-up adds, and where it goes.

The demo profile's cells wire against the install's global tenant and the shared Gateway
host. A POV guest playing a cell role must not: every item it adds goes to the POV's own
tenant, behind the POV's own Gateway, into the POV's own Jump Group, and comes out with
the POV. That is most of what is pinned here:

  * **The items use the POV's tenant, Gateway and Jump Group** -- the same three the base
    jump item is built with -- and are named after the POV, so two POVs cloned from one
    template never collide.
  * **The ports are the demo cell's**, read off ``ot_service.OT_PORT_PRESETS``, so a POV
    tunnels to what the image actually serves.
  * **Each item is persisted the moment it exists**, a re-run builds only what is
    missing, and teardown clears an item only once it is really gone.
  * **A role's items ride the jump-item teardown**, including on a row with no shell jump.
  * **A VyOS guest stays out of Entitle**, for the netcell's reason.
  * **A role is refused where it cannot work** -- a Windows guest, the broker VM, or a
    guest whose items are still in PRA.
  * **A cloud template's role reaches the POV's VM once**, and never over an answer.
  * **The PRA terraform helpers thread a tenant** through every subcommand of the Web Jump
    and the protocol tunnel, which is what lets the POV path reuse them.

Uses a real SQLite database and a fake terraform layer. No network, no FastAPI.

Runs under pytest, or standalone:
    python tests/test_pov_cell_roles.py
"""
import asyncio
import os
import sys
import uuid

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-pov-cell-roles")

from web_dashboard import database as d  # noqa: E402

d.Base.metadata.create_all(bind=d.engine)

from web_dashboard.services import (bt_tenant_service, job_service,  # noqa: E402
                                    ot_service, pov_cards, pov_cell_roles as cr,
                                    pov_cloud_template_service, pov_env_service,
                                    pov_use_cases, pov_wireup as w,
                                    terraform_pra_service)

_STATE = '{"resources":[]}'


def _name(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _tenant(db):
    return bt_tenant_service.create(
        db, kind="pra", name=_name("pra"), base_url="acme.beyondtrustcloud.com",
        client_id="cid", secret="sekrit", created_by="t",
        options={"jump_group_name": "POV", "jumpoint_name": "appliance-default"})


def _env(db, *, platform="skytap", template_id=None, group="pov-own"):
    env = d.PovEnvironment(platform=platform, name=_name("poc"),
                           platform_environment_id="env-1",
                           status=pov_env_service.STATUS_ACTIVE,
                           gateway_name="pov-gw", template_id=template_id,
                           pra_tenant_id=_tenant(db)["id"],
                           pra_jump_group_name=group, pra_jump_group_id="44")
    db.add(env)
    db.commit()
    return env


def _vm(db, env, *, name="plc01", role=None, os_family="linux", ip="10.9.0.20"):
    row = d.PovEnvironmentVM(environment_id=env.id, platform_vm_id=_name("vm"),
                             name=name, os_family=os_family, private_ip=ip,
                             cell_role=role)
    db.add(row)
    db.commit()
    return row


class _FakeTF:
    def __init__(self, **behaviour):
        self.b = behaviour
        self.calls = []

    async def provision_jump(self, **kw):
        self.calls.append(("shell", kw))
        return {"shell_jump_id": "101", "tf_state_json": _STATE}

    async def provision_rdp_jump(self, **kw):
        self.calls.append(("rdp", kw))
        return {"rdp_jump_id": "202", "tf_state_json": _STATE}

    async def provision_web_jump(self, **kw):
        self.calls.append(("web", kw))
        if self.b.get("web_raises"):
            raise RuntimeError(self.b["web_raises"])
        return {"web_jump_id": "301", "tf_state_json": _STATE}

    async def provision_api_tunnel(self, **kw):
        self.calls.append(("tunnel", kw))
        return {"tunnel_jump_id": f"4{len(self.calls)}", "tf_state_json": _STATE}

    async def remove_jump(self, state, tenant=None):
        self.calls.append(("remove_shell", tenant))

    async def remove_rdp_jump(self, state, tenant=None):
        self.calls.append(("remove_rdp", tenant))

    async def remove_web_jump(self, state, tenant=None):
        self.calls.append(("remove_web", tenant))
        if self.b.get("remove_raises"):
            raise RuntimeError(self.b["remove_raises"])

    async def remove_api_tunnel(self, state, tenant=None):
        self.calls.append(("remove_tunnel", tenant))
        if self.b.get("remove_raises"):
            raise RuntimeError(self.b["remove_raises"])


_PATCHED = ("provision_jump", "provision_rdp_jump", "provision_web_jump",
            "provision_api_tunnel", "remove_jump", "remove_rdp_jump", "remove_web_jump",
            "remove_api_tunnel")


class _NoJumpGroups:
    async def find_jump_group(self, tenant, name):
        return None

    async def create_jump_group(self, tenant, *, name, code_name, comments=""):
        return {"id": 44}

    async def delete_jump_group(self, tenant, group_id):
        return None


def _install(fake):
    original = {k: getattr(terraform_pra_service, k) for k in _PATCHED}
    for k in _PATCHED:
        setattr(terraform_pra_service, k, getattr(fake, k))
    jg = _NoJumpGroups()
    original["_jg"] = {k: getattr(w.pra_vendor_api, k)
                       for k in ("find_jump_group", "create_jump_group",
                                 "delete_jump_group")}
    for k in original["_jg"]:
        setattr(w.pra_vendor_api, k, getattr(jg, k))
    return original


def _restore(original):
    for k, v in original.items():
        if k == "_jg":
            for name, fn in v.items():
                setattr(w.pra_vendor_api, name, fn)
        else:
            setattr(terraform_pra_service, k, v)


def _wire(env_id):
    db = d.SessionLocal()
    job = job_service.create_job(db, job_type="pov_env_wireup", created_by="t",
                                 metadata={"environment_id": env_id})
    jid = job.id
    db.close()
    asyncio.run(w.run_env_wireup(jid, {"environment_id": env_id}))
    return jid


# ── the OT simulator's items ─────────────────────────────────────────────────

def test_the_ot_protocols_are_the_demo_cells_plc_presets():
    """A POV tunnels to what the image serves, so the list comes off the same table the
    demo cell and tests/test_ot_ports.py use -- never a second copy here."""
    protocols = cr.ot_protocols()
    assert protocols == [k for k, v in ot_service.OT_PORT_PRESETS.items()
                         if v.get("cell") and v.get("plc")]
    assert "modbus" in protocols and "opcua" in protocols
    assert "dnp3" not in protocols, "nothing in the image answers DNP3"
    assert "k3s" not in protocols, "the cluster API is the cell's platform, not the plant"


def test_an_ot_sim_guest_gets_an_hmi_web_jump_and_one_tunnel_per_protocol():
    db = d.SessionLocal()
    env = _env(db)
    vm = _vm(db, env, role=cr.OT_SIM)
    eid, vid, ename = env.id, vm.id, env.name
    db.close()
    fake = _FakeTF()
    original = _install(fake)
    try:
        _wire(eid)
    finally:
        _restore(original)

    web = [kw for kind, kw in fake.calls if kind == "web"]
    tunnels = [kw for kind, kw in fake.calls if kind == "tunnel"]
    assert len(web) == 1 and len(tunnels) == len(cr.ot_protocols())
    for kw in web + tunnels:
        # The POV's three, never the tenant default or the shared demo Gateway host.
        assert kw["jumpoint_name"] == "pov-gw"
        assert kw["jump_group_name"] == "pov-own"
        assert kw["tenant"]["bt_api_host"] == "acme.beyondtrustcloud.com"
        assert kw["tag"] == w.JUMP_TAG
    assert web[0]["name"] == f"{ename}-plc01-hmi"
    assert web[0]["url"] == f"http://10.9.0.20:{cr.OT_HMI_PORT}"
    modbus = next(kw for kw in tunnels if kw["name"].endswith("-modbus"))
    assert modbus["remote_port"] == 502 and modbus["hostname"] == "10.9.0.20"

    db = d.SessionLocal()
    row = db.get(d.PovEnvironmentVM, vid)
    have = row.cell_artifacts_dict
    assert set(have) == {"hmi"} | {f"tunnel:{p}" for p in cr.ot_protocols()}
    assert all(v["tf_state"] for v in have.values())
    state = cr.describe(row)
    assert state["cell_wired"] == state["cell_items"] == len(have)
    db.close()


def test_a_rerun_builds_only_the_missing_items():
    db = d.SessionLocal()
    env = _env(db)
    vm = _vm(db, env, role=cr.OT_SIM)
    eid = env.id
    db.close()
    first = _FakeTF(web_raises="403 web jump denied")
    original = _install(first)
    try:
        _wire(eid)
    finally:
        _restore(original)
    second = _FakeTF()
    original = _install(second)
    try:
        _wire(eid)
    finally:
        _restore(original)
    kinds = [kind for kind, _ in second.calls]
    assert kinds.count("web") == 1, "the failed Web Jump was not retried"
    assert "tunnel" not in kinds, "a re-run rebuilt tunnels that already existed"
    assert "shell" not in kinds, "a re-run rebuilt the base jump item"


def test_an_ordinary_target_gets_nothing_extra():
    db = d.SessionLocal()
    env = _env(db)
    _vm(db, env)
    eid = env.id
    db.close()
    fake = _FakeTF()
    original = _install(fake)
    try:
        _wire(eid)
    finally:
        _restore(original)
    assert [k for k, _ in fake.calls] == ["shell"]


# ── teardown ─────────────────────────────────────────────────────────────────

def _wired_ot(db, env, **kw):
    vm = _vm(db, env, role=cr.OT_SIM, **kw)
    vm.cell_artifacts_dict = {
        "hmi": {"kind": "web_jump", "id": "301", "tf_state": _STATE},
        "tunnel:modbus": {"kind": "tunnel", "id": "401", "tf_state": _STATE},
    }
    db.commit()
    return vm


def test_teardown_removes_the_role_items_against_the_povs_tenant():
    db = d.SessionLocal()
    env = _env(db)
    vm = _wired_ot(db, env)          # role items only: no shell jump on this row
    fake = _FakeTF()
    original = _install(fake)
    try:
        line = asyncio.run(w.teardown(db, env))
    finally:
        _restore(original)
    kinds = sorted(k for k, _ in fake.calls)
    assert kinds == ["remove_tunnel", "remove_web"], kinds
    assert all(t["bt_api_host"] == "acme.beyondtrustcloud.com" for _, t in fake.calls)
    assert "Removed 2" in line, line
    db.refresh(vm)
    assert vm.cell_artifacts_dict == {}
    db.close()


def test_a_role_item_whose_destroy_failed_keeps_its_state_and_the_group():
    db = d.SessionLocal()
    env = _env(db)
    vm = _wired_ot(db, env)
    original = _install(_FakeTF(remove_raises="403"))
    try:
        line = asyncio.run(w.teardown(db, env))
    finally:
        _restore(original)
    db.refresh(vm)
    assert cr.has_artifacts(vm), "a failed destroy cleared the item's state"
    assert "left in place" in line, "the Jump Group was deleted with items still in it"
    db.close()


# ── VyOS ─────────────────────────────────────────────────────────────────────

def test_a_vyos_guest_adds_no_items_and_stays_out_of_entitle():
    db = d.SessionLocal()
    env = _env(db)
    vm = _vm(db, env, role=cr.VYOS)
    assert cr.planned(vm) == []
    # Refused before the Entitle context is touched, so an empty one is enough.
    line = asyncio.run(w.register_vm_entitle(db, env, vm, ent={}))
    assert "skipped Entitle" in line and "VyOS" in line, line
    db.close()


# ── where a role is refused ──────────────────────────────────────────────────

def test_a_role_on_a_windows_guest_is_refused():
    db = d.SessionLocal()
    env = _env(db)
    vm = _vm(db, env, os_family="windows")
    try:
        cr.set_role(db, env, vm, "ot-sim")
        raise AssertionError("a Windows guest was made an OT simulator")
    except cr.CellRoleError as exc:
        assert "Linux" in str(exc)
    db.close()


def test_an_unknown_role_is_refused():
    try:
        cr.normalize("plc")
        raise AssertionError("an unknown role was accepted")
    except cr.CellRoleError as exc:
        assert "ot-sim" in str(exc)


def test_a_role_cannot_change_while_its_items_are_in_pra():
    db = d.SessionLocal()
    env = _env(db)
    vm = _wired_ot(db, env)
    try:
        cr.set_role(db, env, vm, "")
        raise AssertionError("the role changed under live items")
    except cr.CellRoleError as exc:
        assert "destroyed" in str(exc)
    db.close()


def test_clearing_a_role_stores_a_blank_so_a_seed_cannot_restore_it():
    db = d.SessionLocal()
    env = _env(db)
    vm = _vm(db, env, role=cr.OT_SIM)
    cr.set_role(db, env, vm, "")
    db.refresh(vm)
    assert vm.cell_role == "", "a cleared role was stored as NULL"
    db.close()


def test_a_template_refuses_a_cell_role_on_the_broker_or_a_windows_vm():
    base = {"image_id": "ami-1", "instance_type": "t3.medium"}
    for vm, needle in (
            ({"name": "b", "role": "broker", "cell_role": "ot-sim", **base}, "broker"),
            ({"name": "w", "os_family": "windows", "cell_role": "vyos", **base}, "Linux")):
        try:
            pov_cloud_template_service._validate_vms([vm])
            raise AssertionError(f"{vm} was accepted")
        except pov_cloud_template_service.CloudTemplateError as exc:
            assert needle in str(exc), str(exc)
    clean = pov_cloud_template_service._validate_vms(
        [{"name": "plc", "cell_role": "OT-SIM", **base}])
    assert clean[0]["cell_role"] == "ot-sim"


# ── the template seed ────────────────────────────────────────────────────────

def test_a_cloud_templates_role_reaches_the_pov_vm_once_and_never_over_an_answer():
    db = d.SessionLocal()
    tmpl = pov_cloud_template_service.create(
        db, cloud="aws", name=_name("tmpl"),
        vms=[{"name": "plc01", "cell_role": "ot-sim", "image_id": "ami-1",
              "instance_type": "t3.medium"},
             {"name": "edge01", "cell_role": "vyos", "image_id": "ami-2",
              "instance_type": "t3.small"}])
    env = _env(db, platform="aws", template_id=tmpl.id)
    plc = _vm(db, env, name="plc01")
    edge = _vm(db, env, name="edge01")
    edge.cell_role = ""             # an operator already said "ordinary target"
    db.commit()
    assert cr.seed_from_template(db, env) == 1
    db.refresh(plc)
    db.refresh(edge)
    assert plc.cell_role == "ot-sim"
    assert edge.cell_role == "", "the seed overwrote an operator's answer"
    assert cr.seed_from_template(db, env) == 0, "a second seed changed something"
    db.close()


def test_a_skytap_pov_is_never_seeded():
    db = d.SessionLocal()
    env = _env(db, platform="skytap", template_id="tmpl-on-skytap")
    _vm(db, env)
    assert cr.seed_from_template(db, env) == 0
    db.close()


# ── the use-case cards ───────────────────────────────────────────────────────

def test_the_product_mix_reports_a_cell_guest_and_whether_it_is_wired():
    db = d.SessionLocal()
    env = _env(db)
    vm = _wired_ot(db, env)
    mix = pov_use_cases.products_for(db, env)
    assert mix[pov_cards.cell_key("ot-sim")] is True
    # Two of its five items, and no base jump item: present, not wired.
    assert mix[pov_cards.cell_key("ot-sim", wired=True)] is False
    assert mix[pov_cards.cell_key("vyos")] is False
    vm.pra_jump_id = "101"
    vm.cell_artifacts_dict = {key: {"kind": spec["kind"], "tf_state": _STATE}
                              for key, spec in cr.planned(vm)}
    db.commit()
    mix = pov_use_cases.products_for(db, env)
    assert mix[pov_cards.cell_key("ot-sim", wired=True)] is True
    db.close()


# ── the terraform helpers ────────────────────────────────────────────────────

def test_the_tunnel_and_web_jump_helpers_thread_the_tenant_through_every_subcommand():
    """The POV path's whole premise: without this the items would authenticate as the
    install's own appliance. Every subcommand, including the rollback destroy."""
    seen = []
    tenant = terraform_pra_service.tenant_env("acme.example.com", "cid", "sec")

    class _Done:
        returncode = 1          # fail the apply so the rollback destroy runs too
        stdout = ""
        stderr = "boom"

    class _Init(_Done):
        returncode = 0

    def fake_run(args, work_dir, timeout=120, extra_env=None, tenant=None):
        seen.append((args[0], tenant))
        return _Init() if args[0] == "init" else _Done()

    original = terraform_pra_service._run_tf
    terraform_pra_service._run_tf = fake_run
    try:
        for call in (
                lambda: terraform_pra_service.provision_api_tunnel(
                    name="t", hostname="10.0.0.1", jump_group_name="g",
                    jumpoint_name="j", tenant=tenant),
                lambda: terraform_pra_service.provision_web_jump(
                    name="w", url="http://10.0.0.1:1881", jump_group_name="g",
                    jumpoint_name="j", tenant=tenant)):
            try:
                asyncio.run(call())
                raise AssertionError("a failed apply did not raise")
            except terraform_pra_service.TerraformPRAError:
                pass
    finally:
        terraform_pra_service._run_tf = original
    assert {cmd for cmd, _ in seen} >= {"init", "apply", "destroy"}
    assert all(t is tenant for _, t in seen), seen


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
