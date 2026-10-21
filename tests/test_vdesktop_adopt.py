"""Adopting an existing VDI into /desktops: discovery, adoption, power, and the one
rule that matters most — **the dashboard never terminates an adopted VM.**

An adopted seat is an Azure VM somebody built outside the dashboard (an AVD session
host in its own resource group). Pool delete and scale-down both route through
``teardown_seats``, which terminates the seat's VM for every other kind; a regression
there deletes a customer's persistent desktop. So the teardown guard is pinned here
against the real function, with a real temp SQLite, not against a mock of it.

Also pinned:
  * adoption accepts only ids discovery returned — the resource group a power call acts
    on never comes from the caller;
  * adoption writes nothing to Azure (no pool tag, no managed-by tag — the latter would
    make the Azure tab's Destroy willing to delete it);
  * a resource group discovery cannot list comes back as an error, not as "no VMs";
  * seat power queues the same ``azure_power`` job the Azure tab does, with the
    resource group read off the seat's ARM id.

Run: python tests/test_vdesktop_adopt.py   (or under pytest)
"""
import asyncio
import os
import sys
import tempfile
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="vdesktop-adopt-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDB}"
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-vdesktop-adopt-tests")

try:
    from web_dashboard.database import Base, Job, SessionLocal, VirtualDesktop, engine
    from web_dashboard.services import vdesktop_service as vd
except Exception as exc:  # pragma: no cover — app deps missing
    try:
        import pytest
        pytest.skip(f"app dependencies unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)

Base.metadata.create_all(bind=engine)

SUB = "/subscriptions/0000"


def _vm_id(rg, name):
    return f"{SUB}/resourceGroups/{rg}/providers/Microsoft.Compute/virtualMachines/{name}"


AVD_PARENT = (f"{SUB}/resourcegroups/avd-rg/providers/"
              "Microsoft.DesktopVirtualization/hostpools/hp-persistent")

CALLS = []


def _install_stubs(*, vms_by_rg=None, failing_rgs=(), discovery_rgs="avd-rg, other-rg",
                   get_vm_result=None):
    """Rebind azure_service / config_service / PRA. Returns nothing; read CALLS."""
    CALLS.clear()
    vms_by_rg = vms_by_rg if vms_by_rg is not None else {
        "avd-rg": [
            {"vm_id": _vm_id("avd-rg", "avd-host-0"), "name": "avd-host-0",
             "state": "VM running", "location": "centralus", "size": "Standard_D4s_v5",
             "os_type": "Windows", "private_ip": "10.1.0.4",
             "tags": {"cm-resource-parent": AVD_PARENT}},
            {"vm_id": _vm_id("avd-rg", "dash-built"), "name": "dash-built",
             "state": "VM running", "tags": {"managed-by": "vm-dashboard"}},
        ],
        "other-rg": [
            {"vm_id": _vm_id("other-rg", "plain-vdi"), "name": "plain-vdi",
             "state": "VM deallocated", "os_type": "Windows", "tags": {}},
        ],
    }

    az = types.ModuleType("web_dashboard.services.azure_service")

    async def list_all_vms(rg):
        CALLS.append(("list_all_vms", rg))
        if rg in failing_rgs:
            raise RuntimeError(f"AuthorizationFailed on {rg}")
        return [dict(v) for v in vms_by_rg.get(rg, [])]

    async def get_vm(rg, name):
        CALLS.append(("get_vm", rg, name))
        if get_vm_result is not None:
            return get_vm_result
        for v in vms_by_rg.get(rg, []):
            if v["name"] == name:
                return dict(v)
        return None

    async def terminate_vm(rg, name):
        CALLS.append(("terminate_vm", rg, name))

    async def set_desktop_pool_tag(rg, name, pool):
        CALLS.append(("set_pool_tag", rg, name))

    async def update_tags(*a, **kw):
        CALLS.append(("update_tags", a))

    def _is_dashboard_managed(tags):
        tags = tags or {}
        return (tags.get("managed-by") == "vm-dashboard"
                or tags.get("ManagedBy") == "vm-cli-dashboard")

    az.list_all_vms = list_all_vms
    az.get_vm = get_vm
    az.terminate_vm = terminate_vm
    az.set_desktop_pool_tag = set_desktop_pool_tag
    az.update_tags = update_tags
    az._is_dashboard_managed = _is_dashboard_managed
    sys.modules["web_dashboard.services.azure_service"] = az

    pra = types.ModuleType("web_dashboard.services.terraform_pra_service")

    async def remove_rdp_jump(state):
        CALLS.append(("pra_remove", state))
    pra.remove_rdp_jump = remove_rdp_jump
    sys.modules["web_dashboard.services.terraform_pra_service"] = pra

    jh = types.ModuleType("web_dashboard.services.jumpoint_host_service")

    async def teardown_jumpoint_host_if_idle(db, cloud, location):
        CALLS.append(("reap_gateway", cloud))
    jh.teardown_jumpoint_host_if_idle = teardown_jumpoint_host_if_idle
    sys.modules["web_dashboard.services.jumpoint_host_service"] = jh

    cfg = types.ModuleType("web_dashboard.services.config_service")
    conf = {"azure_desktops_discovery_resource_groups": discovery_rgs,
            "azure_location": "centralus"}
    cfg.get = lambda key, default="": conf.get(key, default)
    cfg.resolve_reference = lambda ref: ""
    sys.modules["web_dashboard.services.config_service"] = cfg

    import web_dashboard.services as pkg
    for name, mod in (("azure_service", az), ("terraform_pra_service", pra),
                      ("jumpoint_host_service", jh), ("config_service", cfg)):
        setattr(pkg, name, mod)


def _reset():
    db = SessionLocal()
    try:
        db.query(VirtualDesktop).delete()
        db.query(Job).delete()
        db.commit()
    finally:
        db.close()


def _adopt(name="vdi", ids=None):
    db = SessionLocal()
    try:
        return asyncio.run(vd.adopt_pool(
            db, name=name, vm_resource_ids=ids or [_vm_id("avd-rg", "avd-host-0")],
            created_by="admin"))
    finally:
        db.close()


def _names():
    return [c[0] for c in CALLS]


# ── The teardown guard ────────────────────────────────────────────────────────

def test_tearing_down_an_adopted_seat_never_terminates_its_vm():
    _install_stubs()
    _reset()
    seats = _adopt()["seats"]
    CALLS.clear()
    asyncio.run(vd.teardown_seats([s["id"] for s in seats]))
    assert "terminate_vm" not in _names(), CALLS
    db = SessionLocal()
    try:
        assert db.query(VirtualDesktop).count() == 0, "the adopted row survived teardown"
    finally:
        db.close()


def test_an_adopted_seats_pra_jump_is_still_removed_on_teardown():
    _install_stubs()
    _reset()
    seat = _adopt()["seats"][0]
    db = SessionLocal()
    try:
        row = db.query(VirtualDesktop).filter(VirtualDesktop.id == seat["id"]).first()
        row.pra_tunnel_state = '{"resources":[{"type":"sra_remote_rdp"}]}'
        db.commit()
    finally:
        db.close()
    CALLS.clear()
    asyncio.run(vd.teardown_seats([seat["id"]]))
    assert "pra_remove" in _names() and "terminate_vm" not in _names(), CALLS


def test_deleting_an_adopted_pool_routes_through_the_guarded_teardown():
    _install_stubs()
    _reset()
    _adopt()
    db = SessionLocal()
    try:
        out = vd.delete_pool(db, "vdi")
    finally:
        db.close()
    assert out["to_teardown"], "an adopted pool delete must still go through teardown"
    CALLS.clear()
    asyncio.run(vd.teardown_seats(out["to_teardown"]))
    assert "terminate_vm" not in _names(), CALLS


def test_a_built_seat_is_still_terminated():
    """The guard is on the kind, not on Azure: the ordinary path is unchanged."""
    _install_stubs()
    _reset()
    db = SessionLocal()
    try:
        row = VirtualDesktop(cloud="azure", pool_name="built", kind="vm_pool",
                             status="running", vm_resource_id=_vm_id("rg-d", "built-1"),
                             created_by="admin")
        db.add(row)
        db.commit()
        sid = row.id
    finally:
        db.close()
    asyncio.run(vd.teardown_seats([sid]))
    assert ("terminate_vm", "rg-d", "built-1") in CALLS, CALLS


def test_an_adopted_pool_cannot_be_scaled_either_way():
    _install_stubs()
    _reset()
    _adopt()
    for count in (0, 5):
        db = SessionLocal()
        try:
            try:
                vd.scale_pool(db, "vdi", count)
            except vd.VDesktopError as e:
                assert "Adopted" in str(e)
            else:
                raise AssertionError(f"scale to {count} was accepted on an adopted pool")
        finally:
            db.close()


def test_removing_one_adopted_seat_marks_it_for_teardown_only():
    _install_stubs()
    _reset()
    seat = _adopt()["seats"][0]
    db = SessionLocal()
    try:
        out = vd.remove_adopted_seat(db, seat["id"])
    finally:
        db.close()
    assert out["to_teardown"] == [seat["id"]]


# ── Adoption ──────────────────────────────────────────────────────────────────

def test_adoption_writes_nothing_to_azure():
    _install_stubs()
    _reset()
    _adopt()
    assert not {"set_pool_tag", "update_tags", "terminate_vm"} & set(_names()), CALLS


def test_adoption_records_the_arm_id_and_the_adopted_kind():
    _install_stubs()
    _reset()
    seat = _adopt()["seats"][0]
    assert seat["kind"] == vd.ADOPTED_KIND
    assert seat["vm_resource_id"] == _vm_id("avd-rg", "avd-host-0")
    assert seat["cloud"] == "azure"


def test_adoption_refuses_an_id_discovery_did_not_return():
    """A caller-chosen resource group is exactly what power must never act on."""
    _install_stubs()
    _reset()
    try:
        _adopt(ids=[_vm_id("someone-elses-rg", "avd-host-0")])
    except vd.VDesktopError as e:
        assert "Not adoptable" in str(e)
    else:
        raise AssertionError("an undiscovered id was adopted")


def test_adoption_refuses_a_dashboard_managed_vm():
    _install_stubs()
    _reset()
    try:
        _adopt(ids=[_vm_id("avd-rg", "dash-built")])
    except vd.VDesktopError:
        pass
    else:
        raise AssertionError("a dashboard-managed VM was adopted")


def test_adoption_refuses_a_vm_that_is_already_a_seat():
    _install_stubs()
    _reset()
    _adopt(name="first")
    try:
        _adopt(name="second")
    except vd.VDesktopError:
        pass
    else:
        raise AssertionError("one VM was adopted into two pools")


def test_adoption_refuses_an_existing_pool_name():
    _install_stubs()
    _reset()
    _adopt(name="vdi")
    try:
        _adopt(name="vdi", ids=[_vm_id("other-rg", "plain-vdi")])
    except vd.VDesktopError as e:
        assert "already exists" in str(e)
    else:
        raise AssertionError("a pool name was reused")


# ── Discovery ─────────────────────────────────────────────────────────────────

def test_discovery_reports_an_unlistable_resource_group_and_keeps_the_rest():
    _install_stubs(failing_rgs=("avd-rg",))
    _reset()
    db = SessionLocal()
    try:
        out = asyncio.run(vd.discover_azure(db))
    finally:
        db.close()
    assert [e["resource_group"] for e in out["errors"]] == ["avd-rg"]
    assert "AuthorizationFailed" in out["errors"][0]["message"]
    assert [v["name"] for v in out["vms"]] == ["plain-vdi"]


def test_discovery_hides_adopted_and_dashboard_managed_vms():
    _install_stubs()
    _reset()
    _adopt()
    db = SessionLocal()
    try:
        names = [v["name"] for v in asyncio.run(vd.discover_azure(db))["vms"]]
    finally:
        db.close()
    assert names == ["plain-vdi"], names


def test_discovery_names_the_avd_host_pool():
    _install_stubs()
    _reset()
    db = SessionLocal()
    try:
        vms = {v["name"]: v for v in asyncio.run(vd.discover_azure(db))["vms"]}
    finally:
        db.close()
    assert vms["avd-host-0"]["avd_host_pool"] == "hp-persistent"
    assert vms["plain-vdi"]["avd_host_pool"] == ""


def test_discovery_resource_groups_are_trimmed_and_deduplicated():
    _install_stubs(discovery_rgs=" avd-rg ;AVD-rg, other-rg,, ")
    assert vd.discovery_resource_groups() == ["avd-rg", "other-rg"]


# ── Power ─────────────────────────────────────────────────────────────────────

def _power(seat_id, action):
    db = SessionLocal()
    try:
        out = vd.queue_seat_power(db, seat_id, action, "admin")
        job = db.query(Job).filter(Job.id == out["job_id"]).first()
        return job.job_type, job.metadata_dict
    finally:
        db.close()


def test_seat_power_queues_an_azure_power_job_at_the_seats_own_resource_group():
    _install_stubs()
    _reset()
    seat = _adopt()["seats"][0]
    job_type, meta = _power(seat["id"], "stop")
    assert job_type == "azure_power"
    assert meta["resource_group"] == "avd-rg"
    assert meta["vm_name"] == "avd-host-0"
    assert meta["action"] == "stop"
    assert meta["desktop_seat_id"] == seat["id"]


def test_seat_power_accepts_only_start_and_stop():
    _install_stubs()
    _reset()
    seat = _adopt()["seats"][0]
    for bad in ("restart", "delete", "terminate", ""):
        db = SessionLocal()
        try:
            try:
                vd.queue_seat_power(db, seat["id"], bad, "admin")
            except vd.VDesktopError:
                pass
            else:
                raise AssertionError(f"power action {bad!r} was accepted")
        finally:
            db.close()


def test_seat_power_states_read_the_live_vm():
    _install_stubs()
    _reset()
    _adopt()
    db = SessionLocal()
    try:
        seats = asyncio.run(vd.seat_power_states(db, "vdi"))
    finally:
        db.close()
    assert seats[0]["power_state"] == "VM running"
    assert seats[0]["avd_host_pool"] == "hp-persistent"
    assert ("get_vm", "avd-rg", "avd-host-0") in CALLS


def _run_tests():
    tests = [(n, o) for n, o in sorted(globals().items())
             if n.startswith("test_") and callable(o)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"ok   {name}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {name}: {exc}")
        except Exception as exc:                       # noqa: BLE001
            failed += 1
            print(f"ERROR {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_tests())
