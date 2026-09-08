"""What a desktop seat's provision and teardown actually do, written down.

``vdesktop_service`` is 517 lines that create a VM per seat, vault a Windows password,
stamp a pool tag, broker the seat over PRA and tear all of it back down — and it had no
tests. Its own docstring says AWS and GCP "create seat *records* only (not provisioned
until their Phase 1)", so two more backends are coming through this code.

These are CHARACTERIZATION tests: they describe the Azure path as it behaves today, so the
refactor that makes room for those backends can be shown to change nothing. They were
written against the pre-refactor code and passed there first — a "no behaviour change"
claim is worth nothing otherwise.

What they pin is the ORDER and the RECOVERY, because that is where this kind of code goes
wrong and none of it is obvious from a diff:

  * the PRA Gateway is warmed BEFORE any seat registers a jump item — register first and
    the items exist but read "Unavailable", with no Gateway to broker them;
  * one seat failing marks that seat failed and keeps going, and the JOB still ends failed
    with the reason, because a green job whose failure only shows on a seat row is how
    nobody notices;
  * teardown terminates, then removes the PRA jump, then drops the row — a row dropped
    first is a VM nobody can find again;
  * every cloud call is best-effort where the docstring says best-effort (pool tag, PRA
    registration) and fatal where it does not (the VM itself).

Heavy deps are stubbed in sys.modules; the database is a real temp SQLite, so the rows and
commits are the real ones.

Run: python tests/test_vdesktop_seats.py   (or under pytest)
"""
import asyncio
import os
import sys
import tempfile
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_TMPDB = os.path.join(tempfile.mkdtemp(prefix="vdesktop-"), "test.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDB}"
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-for-vdesktop-tests")

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

SPEC = {
    "location": "eastus",
    "resource_group": "rg-desktops",
    "subnet_id": "/subscriptions/s/…/subnets/desktops",
    "vm_size": "Standard_D2s_v5",
    "os_type": "Windows",
    "ssh_username": "azureuser",
    "image_publisher": "MicrosoftWindowsDesktop",
    "image_offer": "Windows-11",
    "image_sku": "win11-23h2-pro",
}

# Every cloud/PRA call the provision path makes, in the order it made them.
CALLS = []


# ── Stubs ─────────────────────────────────────────────────────────────────────

def _install_stubs(*, deploy_fails_on=(), tag_raises=False, pra_raises=False):
    """Rebind the service's collaborators. Returns the azure stub for assertions."""
    CALLS.clear()

    az = types.ModuleType("web_dashboard.services.azure_service")

    def generate_windows_admin_password():
        CALLS.append(("generate_password", None))
        return "P@ssw0rd-generated"

    def store_windows_admin_password(vm_name, suffix, password):
        CALLS.append(("store_password", vm_name))
        return ("database", f"ref/{vm_name}")

    async def deploy_vm(**kw):
        CALLS.append(("deploy_vm", kw["vm_name"]))
        if kw["vm_name"] in deploy_fails_on:
            raise RuntimeError("SkuNotAvailable")
        az.deploy_kwargs.append(kw)
        return {"vm_id": f"/subscriptions/s/resourceGroups/{kw['rg']}"
                         f"/providers/Microsoft.Compute/virtualMachines/{kw['vm_name']}",
                "private_ip": "10.0.0.5"}

    async def set_desktop_pool_tag(rg, vm_name, pool_name):
        CALLS.append(("set_pool_tag", vm_name))
        if tag_raises:
            raise RuntimeError("tag api down")

    async def terminate_vm(rg, name):
        CALLS.append(("terminate_vm", name))

    az.deploy_kwargs = []
    az.generate_windows_admin_password = generate_windows_admin_password
    az.store_windows_admin_password = store_windows_admin_password
    az.deploy_vm = deploy_vm
    az.set_desktop_pool_tag = set_desktop_pool_tag
    az.terminate_vm = terminate_vm
    sys.modules["web_dashboard.services.azure_service"] = az

    jh = types.ModuleType("web_dashboard.services.jumpoint_host_service")

    async def ensure_jumpoint_host(cloud, location):
        CALLS.append(("ensure_gateway", cloud))
    async def teardown_jumpoint_host_if_idle(db, cloud, location):
        CALLS.append(("reap_gateway", cloud))
    jh.ensure_jumpoint_host = ensure_jumpoint_host
    jh.teardown_jumpoint_host_if_idle = teardown_jumpoint_host_if_idle
    sys.modules["web_dashboard.services.jumpoint_host_service"] = jh

    pra = types.ModuleType("web_dashboard.services.terraform_pra_service")

    async def provision_rdp_jump(**kw):
        CALLS.append(("pra_register", kw.get("name")))
        if pra_raises:
            raise RuntimeError("PRA unreachable")
        return {"rdp_jump_id": "jump-1", "tf_state_json": '{"state":1}'}

    async def remove_rdp_jump(state):
        CALLS.append(("pra_remove", state))
    pra.provision_rdp_jump = provision_rdp_jump
    pra.remove_rdp_jump = remove_rdp_jump
    sys.modules["web_dashboard.services.terraform_pra_service"] = pra

    cfg = types.ModuleType("web_dashboard.services.config_service")
    # The keys _pra_configured() actually reads. Getting these wrong silently skips the
    # whole PRA half of the path — the gateway warm-up and the jump registration — and
    # the tests still "pass" while asserting nothing, which is how the first draft of
    # this file was wrong.
    _CONF = {"bt_api_host": "pra.example.com",
             "bt_client_id": "cid",
             "bt_client_secret": "csecret",
             "bt_jump_group_name": "jg",
             "bt_jumpoint_name": "jp",
             "azure_location": "eastus"}
    cfg.get = lambda key, default="": _CONF.get(key, default)
    cfg.resolve_reference = lambda ref: "secret"
    sys.modules["web_dashboard.services.config_service"] = cfg

    import web_dashboard.services as pkg
    for name, mod in (("azure_service", az), ("jumpoint_host_service", jh),
                      ("terraform_pra_service", pra), ("config_service", cfg)):
        setattr(pkg, name, mod)
    return az


def _seed(pool="pool-a", n=2, cloud="azure"):
    """Create N seat rows and return their ids."""
    db = SessionLocal()
    try:
        db.query(VirtualDesktop).delete()
        db.commit()
        ids = []
        for _ in range(n):
            row = VirtualDesktop(cloud=cloud, pool_name=pool, kind="pooled",
                                 status="pending", created_by="tester")
            db.add(row)
            db.flush()
            ids.append(row.id)
        db.commit()
        return ids
    finally:
        db.close()


def _read(seat_id):
    db = SessionLocal()
    try:
        return db.query(VirtualDesktop).filter(VirtualDesktop.id == seat_id).first()
    finally:
        db.close()


def _names():
    return [n for n, _ in CALLS]


# ── The Azure provision path, as it behaves today ─────────────────────────────

def test_a_vm_is_created_per_seat_from_the_spec():
    az = _install_stubs()
    ids = _seed(n=2)
    asyncio.run(vd.provision_seats("pool-a", None, ids, dict(SPEC)))

    assert len(az.deploy_kwargs) == 2, "one VM per seat"
    kw = az.deploy_kwargs[0]
    # The spec's fields reach the SDK call under Azure's own parameter names. This is the
    # mapping a per-cloud adapter has to preserve.
    assert kw["rg"] == SPEC["resource_group"]
    assert kw["location"] == SPEC["location"]
    assert kw["vm_size"] == SPEC["vm_size"]
    assert kw["subnet_id"] == SPEC["subnet_id"]
    assert kw["image_publisher"] == SPEC["image_publisher"]
    assert kw["os_type"] == "Windows"


def test_the_seat_records_the_vm_id_and_goes_running():
    _install_stubs()
    ids = _seed(n=1)
    asyncio.run(vd.provision_seats("pool-a", None, ids, dict(SPEC)))
    row = _read(ids[0])
    assert row.status == "running"
    assert row.vm_resource_id and "virtualMachines/" in row.vm_resource_id


def test_the_gateway_is_warmed_before_any_seat_registers():
    """Register a jump item before the Gateway is up and it exists but reads
    "Unavailable" — the docstring says so, and nothing else enforces the order."""
    _install_stubs()
    asyncio.run(vd.provision_seats("pool-a", None, _seed(n=2), dict(SPEC)))
    names = _names()
    assert "ensure_gateway" in names, "the Gateway warm-up did not happen"
    assert names.index("ensure_gateway") < names.index("pra_register"), (
        "a seat registered its jump item before the Gateway was brought online")


def test_the_windows_password_is_vaulted_before_the_vm_exists():
    """The seat's password must be recoverable even if everything after it fails."""
    _install_stubs()
    asyncio.run(vd.provision_seats("pool-a", None, _seed(n=1), dict(SPEC)))
    names = _names()
    assert names.index("store_password") < names.index("deploy_vm")


def test_the_vaulted_password_reaches_the_pool_job_for_lookup():
    """GET /api/azure/vms/{name}/admin-password resolves through this map, so a seat whose
    (backend, ref) never lands there is a Windows desktop nobody can sign into."""
    _install_stubs()
    db = SessionLocal()
    try:
        job = Job(id="pooljob1", job_type="vdesktop_pool_provision", status="running",
                  created_by="tester")
        db.add(job)
        db.commit()
    finally:
        db.close()

    ids = _seed(n=1)
    asyncio.run(vd.provision_seats("pool-a", "pooljob1", ids, dict(SPEC)))

    db = SessionLocal()
    try:
        md = db.get(Job, "pooljob1").metadata_dict
        seats = md.get("seat_passwords") or {}
        assert seats, "no seat_passwords were merged into the pool job"
        entry = next(iter(seats.values()))
        assert entry["backend"] == "database" and entry["ref"].startswith("ref/")
        assert entry["username"] == "azureuser"
    finally:
        db.close()


def test_the_pool_tag_is_stamped_on_every_seat():
    """Live pool state is recoverable from the cloud only because of this tag."""
    _install_stubs()
    asyncio.run(vd.provision_seats("pool-a", None, _seed(n=2), dict(SPEC)))
    assert _names().count("set_pool_tag") == 2


def test_a_failing_pool_tag_does_not_fail_the_seat():
    """Best-effort by contract: a seat that runs but is untagged is recoverable; a seat
    refused over a tag call is not."""
    _install_stubs(tag_raises=True)
    ids = _seed(n=1)
    asyncio.run(vd.provision_seats("pool-a", None, ids, dict(SPEC)))
    assert _read(ids[0]).status == "running"


def test_a_failing_pra_registration_does_not_fail_the_seat():
    """"A running seat with no jump item is debuggable; never fail the seat over
    brokering" — the code's own words."""
    _install_stubs(pra_raises=True)
    ids = _seed(n=1)
    asyncio.run(vd.provision_seats("pool-a", None, ids, dict(SPEC)))
    row = _read(ids[0])
    assert row.status == "running"
    assert row.pra_jump_id is None


def test_one_bad_seat_does_not_abort_the_others():
    _install_stubs(deploy_fails_on=(vd._vm_name_for("pool-a", "x"),))
    ids = _seed(n=3)
    # Fail the middle seat by name.
    doomed = vd._vm_name_for("pool-a", ids[1])
    _install_stubs(deploy_fails_on=(doomed,))
    asyncio.run(vd.provision_seats("pool-a", None, ids, dict(SPEC)))
    statuses = [_read(i).status for i in ids]
    assert statuses.count("running") == 2, statuses
    assert statuses.count("failed") == 1, statuses


def test_a_partly_failed_provision_marks_the_JOB_failed():
    """A green job whose failure shows only on a seat row is how nobody notices."""
    _install_stubs()
    db = SessionLocal()
    try:
        db.add(Job(id="pooljob2", job_type="vdesktop_pool_provision", status="running",
                   created_by="tester"))
        db.commit()
    finally:
        db.close()
    ids = _seed(n=2)
    _install_stubs(deploy_fails_on=(vd._vm_name_for("pool-a", ids[0]),))
    asyncio.run(vd.provision_seats("pool-a", "pooljob2", ids, dict(SPEC)))

    db = SessionLocal()
    try:
        job = db.get(Job, "pooljob2")
        assert job.status == "failed", job.status
        assert "1/2" in (job.error_message or ""), job.error_message
    finally:
        db.close()


# ── Teardown ──────────────────────────────────────────────────────────────────

def test_teardown_terminates_then_removes_the_jump_then_drops_the_row():
    """Order matters: a row dropped before the VM is terminated is a VM nobody can find."""
    _install_stubs()
    ids = _seed(n=1)
    asyncio.run(vd.provision_seats("pool-a", None, ids, dict(SPEC)))
    CALLS.clear()
    asyncio.run(vd.teardown_seats(ids))

    names = _names()
    assert names.index("terminate_vm") < names.index("pra_remove"), names
    assert _read(ids[0]) is None, "the seat row survived teardown"


def test_teardown_drops_the_row_even_when_terminate_fails():
    """Best-effort by contract. A row kept because the cloud call failed is a seat the
    pool can never be rid of."""
    az = _install_stubs()
    ids = _seed(n=1)
    asyncio.run(vd.provision_seats("pool-a", None, ids, dict(SPEC)))

    async def _boom(rg, name):
        CALLS.append(("terminate_vm", name))
        raise RuntimeError("cloud unavailable")
    az.terminate_vm = _boom

    asyncio.run(vd.teardown_seats(ids))
    assert _read(ids[0]) is None


# ── AWS seats ─────────────────────────────────────────────────────────────────

AWS_SPEC = {
    "region": "us-east-2",
    "ami_id": "ami-0abc",
    "instance_type": "t3.medium",
    "subnet_id": "subnet-123",
    "security_group_ids": ["sg-1"],
    "ssh_public_key": "ssh-ed25519 AAAA",
    "os_type": "Linux",
}


def _install_aws_stubs(*, launch_fails_on=()):
    _install_stubs()          # keeps jumpoint/pra/config in place
    aws = types.ModuleType("web_dashboard.services.aws_service")

    async def launch_instance(**kw):
        CALLS.append(("launch_instance", kw["instance_name"]))
        if kw["instance_name"] in launch_fails_on:
            raise RuntimeError("InsufficientInstanceCapacity")
        aws.launch_kwargs.append(kw)
        return {"instance_id": "i-0abcdef", "state": "pending",
                "private_ip": "10.1.2.3", "public_ip": None}

    async def set_desktop_pool_tag(region, instance_id, pool_name):
        CALLS.append(("set_pool_tag", f"{region}/{instance_id}"))

    async def terminate_instance(region, instance_id):
        CALLS.append(("terminate_vm", f"{region}/{instance_id}"))
        return {}

    aws.launch_kwargs = []
    aws.launch_instance = launch_instance
    aws.set_desktop_pool_tag = set_desktop_pool_tag
    aws.terminate_instance = terminate_instance
    sys.modules["web_dashboard.services.aws_service"] = aws
    import web_dashboard.services as pkg
    pkg.aws_service = aws
    return aws


def test_an_aws_seat_launches_an_ec2_instance_from_the_spec():
    aws = _install_aws_stubs()
    ids = _seed(n=2, cloud="aws")
    asyncio.run(vd.provision_seats("pool-a", None, ids, dict(AWS_SPEC)))

    assert len(aws.launch_kwargs) == 2
    kw = aws.launch_kwargs[0]
    assert kw["region"] == AWS_SPEC["region"]
    assert kw["ami_id"] == AWS_SPEC["ami_id"]
    assert kw["instance_type"] == AWS_SPEC["instance_type"]
    assert kw["security_group_ids"] == ["sg-1"]
    assert kw["public_key"] == AWS_SPEC["ssh_public_key"]


def test_an_aws_seat_stores_its_region_with_the_instance_id():
    """Teardown gets only `vm_resource_id` — no spec, so no region. An instance id
    alone does not say which regional endpoint owns it."""
    _install_aws_stubs()
    ids = _seed(n=1, cloud="aws")
    asyncio.run(vd.provision_seats("pool-a", None, ids, dict(AWS_SPEC)))
    row = _read(ids[0])
    assert row.status == "running"
    assert row.vm_resource_id == "us-east-2/i-0abcdef"


def test_an_aws_seat_is_torn_down_in_its_own_region():
    _install_aws_stubs()
    ids = _seed(n=1, cloud="aws")
    asyncio.run(vd.provision_seats("pool-a", None, ids, dict(AWS_SPEC)))
    CALLS.clear()
    asyncio.run(vd.teardown_seats(ids))
    assert ("terminate_vm", "us-east-2/i-0abcdef") in CALLS, CALLS
    assert _read(ids[0]) is None


def test_an_aws_pool_refuses_windows_rather_than_shipping_a_seat_nobody_can_use():
    """EC2 returns Windows credentials as password data encrypted to the launch key
    pair. Until that is decrypted and vaulted, a Windows seat would provision fine and
    be unusable — so the pool is refused at create time, with the reason."""
    try:
        vd._AwsSeats.validate_spec(dict(AWS_SPEC, os_type="Windows"))
    except vd.VDesktopError as exc:
        assert "Linux-only" in str(exc), exc
        assert "Azure" in str(exc), "the refusal should name what DOES work"
    else:
        raise AssertionError("a Windows AWS pool was accepted")


def test_an_aws_pool_names_every_missing_field_at_once():
    try:
        vd._AwsSeats.validate_spec({"region": "us-east-2"})
    except vd.VDesktopError as exc:
        for field in ("ami_id", "instance_type", "subnet_id", "ssh_public_key"):
            assert field in str(exc), f"{field} missing from {exc}"
    else:
        raise AssertionError("an empty AWS spec was accepted")


def test_one_bad_aws_seat_does_not_abort_the_others():
    ids = _seed(n=3, cloud="aws")
    _install_aws_stubs(launch_fails_on=(vd._vm_name_for("pool-a", ids[1]),))
    asyncio.run(vd.provision_seats("pool-a", None, ids, dict(AWS_SPEC)))
    statuses = [_read(i).status for i in ids]
    assert statuses.count("running") == 2, statuses
    assert statuses.count("failed") == 1, statuses


def test_an_aws_seat_is_tagged_with_its_instance_id_not_its_name():
    """The interface passes the resource id precisely because EC2 addresses instances
    by id — tagging by name would silently tag nothing."""
    _install_aws_stubs()
    ids = _seed(n=1, cloud="aws")
    asyncio.run(vd.provision_seats("pool-a", None, ids, dict(AWS_SPEC)))
    tagged = [v for n, v in CALLS if n == "set_pool_tag"]
    assert tagged == ["us-east-2/i-0abcdef"], tagged


# ── The staging guard ─────────────────────────────────────────────────────────

def test_no_cloud_is_advertised_without_a_backend_behind_it():
    """The bug this whole item exists to fix: a cloud that accepts a pool and then
    creates seat records with no VMs. `PROVISIONING_CLOUDS` is derived rather than
    maintained, so this asserts the derivation rather than a hand-written list."""
    assert set(vd.PROVISIONING_CLOUDS) == set(vd._SEAT_BACKENDS)
    assert set(vd.PROVISIONING_CLOUDS) <= set(vd.VALID_CLOUDS)
    # GCP is still records-only — Stage 3.
    assert "gcp" not in vd.PROVISIONING_CLOUDS
    assert vd.seat_backend("gcp") is None


def test_every_backend_implements_the_whole_interface():
    """A backend missing a method fails at provision time, on a real pool, halfway
    through — which is the worst possible place to discover a typo."""
    required_attrs = ("cloud", "gateway_cloud", "pool_tag_key", "supports_windows",
                      "default_username", "pra_tag")
    required_methods = ("validate_spec", "deploy", "terminate", "tag_pool",
                        "generate_password", "store_password", "reap_idle_gateway")
    for cloud, backend in vd._SEAT_BACKENDS.items():
        for attr in required_attrs:
            assert hasattr(backend, attr), f"{cloud} backend has no {attr}"
        for meth in required_methods:
            assert callable(getattr(backend, meth, None)), f"{cloud} backend has no {meth}()"
        assert backend.cloud == cloud, f"{cloud} backend disagrees about its own name"


def test_the_pool_tag_key_is_legal_for_its_cloud():
    """`dashboard:desktop_pool` is a fine Azure and AWS tag key. It is an INVALID GCP
    label key — lowercase letters, digits, `-` and `_` only — so Stage 3 must not
    inherit this constant, and this test is what will say so."""
    import re
    for cloud, backend in vd._SEAT_BACKENDS.items():
        key = backend.pool_tag_key
        assert key, f"{cloud} has no pool tag key"
        if cloud == "gcp":
            assert re.fullmatch(r"[a-z][a-z0-9_-]*", key), (
                f"{key!r} is not a valid GCP label key")


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
