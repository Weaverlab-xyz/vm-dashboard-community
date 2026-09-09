"""What a desktop seat's provision and teardown actually do, written down.

``vdesktop_service`` creates a VM per seat on all three clouds, vaults a Windows
password, stamps a pool tag, brokers the seat over PRA and tears all of it back down.

The Azure tests here began as CHARACTERIZATION tests, written against the pre-refactor
code and passing there first, so that extracting the per-cloud seat backend could be
shown to change nothing. The AWS and GCP sections were added with those backends. The
brokering section came last, and closed a real hole: both the Gateway warm-up and the
jump registration were gated on ``is_windows``, so every Linux seat — which is every
AWS and GCP seat — got a VM and was never brokered.

What they pin is the ORDER and the RECOVERY, because that is where this kind of code
goes wrong and none of it is obvious from a diff:

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

def _install_stubs(*, deploy_fails_on=(), tag_raises=False, pra_raises=False,
                   shell_jump_id="shell-1"):
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
        # The REGION is recorded separately rather than folded into CALLS: the
        # ordering test above pins ("ensure_gateway", cloud) and it should keep
        # meaning what it meant.
        jh.regions.append((cloud, location))
    async def teardown_jumpoint_host_if_idle(db, cloud, location):
        CALLS.append(("reap_gateway", cloud))
    jh.regions = []
    jh.ensure_jumpoint_host = ensure_jumpoint_host
    jh.teardown_jumpoint_host_if_idle = teardown_jumpoint_host_if_idle
    sys.modules["web_dashboard.services.jumpoint_host_service"] = jh

    pra = types.ModuleType("web_dashboard.services.terraform_pra_service")

    async def provision_rdp_jump(**kw):
        pra.calls.append(("rdp", kw))
        CALLS.append(("pra_register", kw.get("name")))
        if pra_raises:
            raise RuntimeError("PRA unreachable")
        return {"rdp_jump_id": "jump-1",
                "tf_state_json": '{"resources":[{"type":"sra_remote_rdp"}]}'}

    async def provision_jump(**kw):
        # Same CALLS entry as the RDP stub, so the ordering assertions stay about
        # "a seat registered" rather than about which kind of item it registered.
        pra.calls.append(("shell", kw))
        CALLS.append(("pra_register", kw.get("vm_name")))
        if pra_raises:
            raise RuntimeError("PRA unreachable")
        return {"shell_jump_id": shell_jump_id, "jump_group_name": kw.get("jump_group_name"),
                "tf_state_json": '{"resources":[{"type":"sra_shell_jump"}]}'}

    async def remove_rdp_jump(state):
        CALLS.append(("pra_remove", state))

    def _scrub_tf_state(state):
        return state or None

    pra.calls = []
    pra.provision_rdp_jump = provision_rdp_jump
    pra.provision_jump = provision_jump
    pra.remove_rdp_jump = remove_rdp_jump
    pra._scrub_tf_state = _scrub_tf_state
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
             "azure_location": "eastus",
             # Per-cloud PRA targets. Azure and GCP have their own keys; AWS has
             # none by design and must fall through to the bt_* pair above.
             "azure_bt_jump_group_name": "azure-jg",
             "azure_jumpoint_name": "azure-gw",
             "gcp_bt_jump_group_name": "gcp-jg",
             "gcp_jumpoint_name": "gcp-gw",
             "aws_region": "us-east-2",
             "gcp_zone": "us-central1-a"}
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


def _pra():
    """The terraform_pra_service stub currently bound into the service package."""
    import web_dashboard.services as pkg
    return pkg.terraform_pra_service


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


# ── GCP seats ─────────────────────────────────────────────────────────────────

GCP_SPEC = {
    "project_id": "proj-1",
    "zone": "us-central1-a",
    "machine_type": "e2-standard-2",
    "image_self_link": "projects/debian-cloud/global/images/family/debian-12",
    "subnetwork": "default",
    "ssh_public_key": "ssh-ed25519 AAAA",
    "os_type": "Linux",
}


def _install_gcp_stubs(*, launch_fails_on=()):
    _install_stubs()
    g = types.ModuleType("web_dashboard.services.gcp_service")

    async def launch_instance(**kw):
        CALLS.append(("launch_instance", kw["instance_name"]))
        if kw["instance_name"] in launch_fails_on:
            raise RuntimeError("ZONE_RESOURCE_POOL_EXHAUSTED")
        g.launch_kwargs.append(kw)
        return {"instance_name": kw["instance_name"], "zone": kw["zone"],
                "status": "RUNNING", "private_ip": "10.2.0.9",
                "public_ip": None, "self_link": "https://…"}

    async def terminate_instance(project_id, zone, instance_name):
        CALLS.append(("terminate_vm", f"{project_id}/{zone}/{instance_name}"))

    g.launch_kwargs = []
    g.launch_instance = launch_instance
    g.terminate_instance = terminate_instance
    sys.modules["web_dashboard.services.gcp_service"] = g
    import web_dashboard.services as pkg
    pkg.gcp_service = g
    return g


def test_the_gcp_pool_label_key_is_not_the_shared_constant():
    """The trap this stage was warned about: `dashboard:desktop_pool` is a valid Azure
    tag key and a valid EC2 tag key, and GCP rejects it — a colon is not permitted in a
    label key. Inheriting the constant would have failed every launch."""
    assert vd._GcpSeats.pool_tag_key != vd.POOL_TAG
    assert ":" not in vd._GcpSeats.pool_tag_key


def test_a_gcp_seat_labels_the_pool_at_launch_not_afterwards():
    """GCE takes labels on the create call, so there is no window where a seat exists
    unattributed — unlike Azure and AWS, which tag after the VM is up."""
    g = _install_gcp_stubs()
    ids = _seed(pool="pool-a", n=1, cloud="gcp")
    asyncio.run(vd.provision_seats("pool-a", None, ids, dict(GCP_SPEC)))

    labels = g.launch_kwargs[0]["labels"]
    assert labels == {"dashboard_desktop_pool": "pool-a"}, labels
    # And nothing tags afterwards.
    assert "set_pool_tag" not in _names()


def test_a_pool_name_gcp_would_reject_is_sanitised_for_the_label():
    """Pool names are free text; GCP label VALUES have the same character rules as keys.
    Without this the launch is rejected over the pool's capitalisation."""
    g = _install_gcp_stubs()
    ids = _seed(pool="Pool A!", n=1, cloud="gcp")
    asyncio.run(vd.provision_seats("Pool A!", None, ids, dict(GCP_SPEC)))
    value = g.launch_kwargs[0]["labels"]["dashboard_desktop_pool"]
    import re
    assert re.fullmatch(r"[a-z0-9_-]+", value), value


def test_a_gcp_seat_stores_project_zone_and_name():
    """Terminate needs all three and is handed only `vm_resource_id`."""
    _install_gcp_stubs()
    ids = _seed(n=1, cloud="gcp")
    asyncio.run(vd.provision_seats("pool-a", None, ids, dict(GCP_SPEC)))
    row = _read(ids[0])
    assert row.status == "running"
    assert row.vm_resource_id.startswith("proj-1/us-central1-a/")


def test_a_gcp_seat_is_torn_down_in_its_own_project_and_zone():
    _install_gcp_stubs()
    ids = _seed(n=1, cloud="gcp")
    asyncio.run(vd.provision_seats("pool-a", None, ids, dict(GCP_SPEC)))
    rid = _read(ids[0]).vm_resource_id
    CALLS.clear()
    asyncio.run(vd.teardown_seats(ids))
    assert ("terminate_vm", rid) in CALLS, CALLS
    assert _read(ids[0]) is None


def test_a_gcp_pool_refuses_windows():
    try:
        vd._GcpSeats.validate_spec(dict(GCP_SPEC, os_type="Windows"))
    except vd.VDesktopError as exc:
        assert "Linux-only" in str(exc)
        assert "windows-keys" in str(exc), "name the mechanism that is missing"
    else:
        raise AssertionError("a Windows GCP pool was accepted")


def test_a_gcp_pool_names_every_missing_field_at_once():
    try:
        vd._GcpSeats.validate_spec({"project_id": "p"})
    except vd.VDesktopError as exc:
        for field in ("zone", "machine_type", "image_self_link", "subnetwork",
                      "ssh_public_key"):
            assert field in str(exc), f"{field} missing from {exc}"
    else:
        raise AssertionError("an empty GCP spec was accepted")


# ── The staging guard ─────────────────────────────────────────────────────────

def test_no_cloud_is_advertised_without_a_backend_behind_it():
    """The bug this whole item exists to fix: a cloud that accepts a pool and then
    creates seat records with no VMs. `PROVISIONING_CLOUDS` is derived rather than
    maintained, so this asserts the derivation rather than a hand-written list."""
    assert set(vd.PROVISIONING_CLOUDS) == set(vd._SEAT_BACKENDS)
    assert set(vd.PROVISIONING_CLOUDS) <= set(vd.VALID_CLOUDS)
    # All three now provision. The derivation is what this asserts, so this line does
    # not need editing again when a fourth cloud lands.
    assert set(vd.PROVISIONING_CLOUDS) == set(vd.VALID_CLOUDS)


def test_every_backend_implements_the_whole_interface():
    """A backend missing a method fails at provision time, on a real pool, halfway
    through — which is the worst possible place to discover a typo."""
    required_attrs = ("cloud", "gateway_cloud", "pool_tag_key", "supports_windows",
                      "default_username", "pra_tag")
    required_methods = ("validate_spec", "deploy", "terminate", "tag_pool",
                        "generate_password", "store_password", "reap_idle_gateway",
                        # Where this cloud's Gateway is warmed. The spec key differs
                        # per cloud (location / region / zone), so a shared
                        # spec.get("location") silently mis-placed two of the three.
                        "gateway_region")
    import inspect
    for cloud, backend in vd._SEAT_BACKENDS.items():
        for attr in required_attrs:
            assert hasattr(backend, attr), f"{cloud} backend has no {attr}"
        for meth in required_methods:
            assert callable(getattr(backend, meth, None)), f"{cloud} backend has no {meth}()"
        assert backend.cloud == cloud, f"{cloud} backend disagrees about its own name"
        # The shared path calls these positionally; a backend whose signature drifted
        # would fail on a real pool, mid-provision.
        assert list(inspect.signature(backend.deploy).parameters) == [
            "spec", "vm_name", "admin_password", "pool_name"], cloud
        assert list(inspect.signature(backend.tag_pool).parameters) == [
            "spec", "vm_name", "vm_resource_id", "pool_name"], cloud
        # Per-cloud PRA config keys. "" is a legal value (see _AwsSeats) but the
        # attribute must EXIST, or `_resolve_pra_targets` silently uses bt_* for a
        # cloud that has its own keys.
        for attr in ("pra_jump_group_key", "pra_jumpoint_key", "pra_vault_group_key",
                     "region_spec_key"):
            assert hasattr(backend, attr), f"{cloud} backend has no {attr}"
        assert backend.region_spec_key, f"{cloud} declares no region spec key"
    # And the conversion actually works from that cloud's own spec shape.
    assert vd._AzureSeats.gateway_region({"location": "westus2"}) == "westus2"
    assert vd._AwsSeats.gateway_region({"region": "eu-west-1"}) == "eu-west-1"
    assert vd._GcpSeats.gateway_region({"zone": "europe-west1-b"}) == "europe-west1", (
        "GCP must hand a REGION to the gateway, never the zone from its spec")


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


# ─ Brokering: which jump item a seat gets, and against whose config ──────
#
# Until this section existed, `provision_seats` gated BOTH the Gateway warm-up and the
# jump registration on `is_windows`. AWS and GCP are Linux-only, so no seat on either
# cloud was ever brokered — it got a VM, `pra_jump_id` stayed NULL, and the UI greyed
# out "Open session" forever. Azure Linux seats had the same hole.


def test_a_linux_aws_seat_is_brokered_over_a_shell_jump():
    _install_aws_stubs()
    pra = _pra()
    ids = _seed(n=1, cloud="aws")
    asyncio.run(vd.provision_seats("pool-a", None, ids, dict(AWS_SPEC)))

    assert len(pra.calls) == 1, "exactly one jump item per seat"
    kind, kw = pra.calls[0]
    assert kind == "shell", "a Linux seat gets a Shell Jump, not Remote RDP"
    assert kw["port"] == 22
    assert kw["hostname"] == "10.1.2.3", "the jump targets the seat's PRIVATE ip"
    assert kw["tag"] == "AWS VDI"
    assert _read(ids[0]).pra_jump_id == "shell-1"


def test_a_linux_gcp_seat_is_brokered_over_a_shell_jump():
    _install_gcp_stubs()
    pra = _pra()
    ids = _seed(n=1, cloud="gcp")
    asyncio.run(vd.provision_seats("pool-a", None, ids, dict(GCP_SPEC)))

    kind, kw = pra.calls[0]
    assert kind == "shell"
    assert kw["hostname"] == "10.2.0.9"
    assert kw["tag"] == "GCP VDI"
    assert _read(ids[0]).pra_jump_id == "shell-1"


def test_a_linux_azure_seat_is_brokered_too_not_just_windows():
    """The third seat this closed. Azure CAN do Windows, so the `is_windows` gate hid
    the hole here: an Azure Linux pool provisioned VMs and brokered nothing."""
    _install_stubs()

    ids = _seed(n=1, cloud="azure")
    spec = dict(SPEC, os_type="Linux", ssh_public_key="ssh-ed25519 AAAA")
    asyncio.run(vd.provision_seats("pool-a", None, ids, spec))

    kind, kw = _pra().calls[0]
    assert kind == "shell"
    assert kw["tag"] == "Azure VDI"
    assert "generate_password" not in _names(), "a Linux seat has no password to vault"


def test_a_windows_azure_seat_still_gets_a_remote_rdp_jump():
    """Regression: the Windows path is untouched, vault kwargs and all."""
    _install_stubs()

    ids = _seed(n=1)
    asyncio.run(vd.provision_seats("pool-a", None, ids, dict(SPEC)))

    kind, kw = _pra().calls[0]
    assert kind == "rdp"
    assert kw["admin_password"] == "P@ssw0rd-generated"
    assert kw["vault_account_name"].endswith("-admin")
    assert _read(ids[0]).pra_jump_id == "jump-1"


def test_a_linux_seat_vaults_no_password_and_asks_for_no_injection():
    """A Linux seat authenticates with an SSH KEY. There is no password to vault, and
    this provider wrapper has no SSH-key vault resource, so the item must register with
    NO credential injection rather than with an empty one."""
    _install_aws_stubs()
    pra = _pra()
    ids = _seed(n=1, cloud="aws")
    asyncio.run(vd.provision_seats("pool-a", None, ids, dict(AWS_SPEC)))

    assert "generate_password" not in _names()
    assert "store_password" not in _names()
    injected = {"admin_password", "vault_account_name", "vault_account_group_id"}
    assert not (injected & set(pra.calls[0][1])), "no vault kwargs on a Shell Jump"


def test_the_gateway_is_warmed_for_a_linux_pool_too():
    """The other half of the `is_windows` gate. A Shell Jump needs a Gateway exactly as
    much as an RDP jump does; without one the item registers and reads "Unavailable"."""
    _install_aws_stubs()
    ids = _seed(n=1, cloud="aws")
    asyncio.run(vd.provision_seats("pool-a", None, ids, dict(AWS_SPEC)))

    names = _names()
    assert "ensure_gateway" in names, "no Gateway was warmed for a Linux pool"
    assert names.index("ensure_gateway") < names.index("pra_register")


def test_each_cloud_warms_its_gateway_in_its_own_region():
    """`spec["location"]` is an AZURE key. Passing it for every cloud warmed AWS and
    GCP against a blank region, which does not fail — it silently places the Gateway
    somewhere nobody chose. GCP is the sharp one: its spec carries a ZONE and
    `ensure_jumpoint_host` wants a REGION."""
    import web_dashboard.services as pkg

    _install_stubs()
    asyncio.run(vd.provision_seats("pool-a", None, _seed(n=1), dict(SPEC)))
    assert pkg.jumpoint_host_service.regions == [("azure", "eastus")]

    _install_aws_stubs()
    asyncio.run(vd.provision_seats("pool-a", None, _seed(n=1, cloud="aws"), dict(AWS_SPEC)))
    assert pkg.jumpoint_host_service.regions == [("aws", "us-east-2")]

    _install_gcp_stubs()
    asyncio.run(vd.provision_seats("pool-a", None, _seed(n=1, cloud="gcp"), dict(GCP_SPEC)))
    assert pkg.jumpoint_host_service.regions == [("gcp", "us-central1")], (
        "a GCP gateway is placed by REGION; us-central1-a is a zone")


def test_a_seat_brokers_against_its_own_clouds_pra_config():
    """`_resolve_pra_targets` read the `azure_*` keys for every cloud, so a GCP seat's
    jump item landed in the AZURE Jump Group."""
    _install_stubs()
    asyncio.run(vd.provision_seats("pool-a", None, _seed(n=1), dict(SPEC)))
    assert _pra().calls[0][1]["jump_group_name"] == "azure-jg"
    assert _pra().calls[0][1]["jumpoint_name"] == "azure-gw"

    _install_gcp_stubs()
    asyncio.run(vd.provision_seats("pool-a", None, _seed(n=1, cloud="gcp"), dict(GCP_SPEC)))
    assert _pra().calls[0][1]["jump_group_name"] == "gcp-jg"
    assert _pra().calls[0][1]["jumpoint_name"] == "gcp-gw"

    _install_aws_stubs()
    asyncio.run(vd.provision_seats("pool-a", None, _seed(n=1, cloud="aws"), dict(AWS_SPEC)))
    assert _pra().calls[0][1]["jump_group_name"] == "jg", "AWS falls through to bt_*"
    assert _pra().calls[0][1]["jumpoint_name"] == "jp"


def test_aws_has_no_cloud_specific_pra_key_on_purpose():
    """"" here is an answer, not a gap: `aws_vm_service` resolves straight from `bt_*`,
    so inventing an `aws_bt_jump_group_name` would land VDI jump items somewhere the
    AWS Shell Jump path does not. The second half of this assertion is the point — the
    day somebody DOES add that setting, this test says "now wire it up here too"."""
    from web_dashboard.config import settings
    assert vd._AwsSeats.pra_jump_group_key == ""
    assert vd._AwsSeats.pra_jumpoint_key == ""
    for key in ("aws_bt_jump_group_name", "aws_jumpoint_name"):
        assert not hasattr(settings, key), (
            f"{key} now exists in config; wire it into _AwsSeats.pra_* keys")


def test_a_blank_jump_id_is_stored_as_null_not_empty_string():
    """Both provisioners return "" when the Terraform output is missing. A non-NULL ""
    makes `_active_vdesktop_count` (pra_jump_id.isnot(None)) pin the shared Gateway
    forever, while the UI's `:disabled="!s.pra_jump_id"` still greys the button out —
    two symptoms and no visible cause."""
    _install_aws_stubs()
    async def _blank(**kw):
        return {"shell_jump_id": "", "tf_state_json": '{"resources":[]}'}
    _pra().provision_jump = _blank

    ids = _seed(n=1, cloud="aws")
    asyncio.run(vd.provision_seats("pool-a", None, ids, dict(AWS_SPEC)))
    assert _read(ids[0]).pra_jump_id is None


def test_a_failing_shell_jump_registration_does_not_fail_the_seat():
    """Brokering is best-effort. A running seat with no jump item is debuggable; a
    seat marked failed because PRA was down is a VM nobody will clean up."""
    _install_aws_stubs()
    async def _boom(**kw):
        raise RuntimeError("PRA unreachable")
    _pra().provision_jump = _boom

    ids = _seed(n=1, cloud="aws")
    asyncio.run(vd.provision_seats("pool-a", None, ids, dict(AWS_SPEC)))
    row = _read(ids[0])
    assert row.status == "running"
    assert row.pra_jump_id is None


# ─ Teardown, jump kind, and the session payload ───────────────@


def test_teardown_removes_a_linux_seats_shell_jump_from_its_state():
    """`remove_rdp_jump` is a misnomer: it destroys whatever `sra_*` resource the
    stored state holds, so it is correct for a Shell Jump too and no per-kind dispatch
    is needed. The ORDER still matters — a row dropped first is a VM nobody can find."""
    _install_aws_stubs()
    ids = _seed(n=1, cloud="aws")
    asyncio.run(vd.provision_seats("pool-a", None, ids, dict(AWS_SPEC)))
    state = _read(ids[0]).pra_tunnel_state
    assert "sra_shell_jump" in state

    CALLS.clear()
    asyncio.run(vd.teardown_seats(ids))
    names = _names()
    assert ("pra_remove", state) in CALLS
    assert names.index("terminate_vm") < names.index("pra_remove")
    assert _read(ids[0]) is None


def test_the_seats_jump_kind_is_read_off_its_state():
    """Derived, not stored: a column could disagree with the state it describes, and
    nothing needs it for teardown. A seat with no state predates Linux brokering, when
    only Windows seats were ever registered — hence the RDP default."""
    _install_aws_stubs()
    ids = _seed(n=1, cloud="aws")
    asyncio.run(vd.provision_seats("pool-a", None, ids, dict(AWS_SPEC)))
    assert vd.jump_kind(_read(ids[0])) == "shell_jump"

    _install_stubs()
    wids = _seed(n=1)
    asyncio.run(vd.provision_seats("pool-a", None, wids, dict(SPEC)))
    assert vd.jump_kind(_read(wids[0])) == "remote_rdp"

    legacy = _seed(n=1)
    assert vd.jump_kind(_read(legacy[0])) == "remote_rdp", "no state == the old world"


def test_the_session_targets_follow_the_seats_own_cloud():
    """The endpoint used to hard-code the `azure_*` keys, so an AWS or GCP seat's
    session named a Jump Group its item was never in."""
    _install_gcp_stubs()
    ids = _seed(n=1, cloud="gcp")
    asyncio.run(vd.provision_seats("pool-a", None, ids, dict(GCP_SPEC)))

    db = SessionLocal()
    try:
        info = vd.session_info(db, ids[0])
    finally:
        db.close()
    assert info["brokered"] is True
    assert info["cloud"] == "gcp"
    assert info["pra"]["kind"] == "shell_jump"
    assert info["pra"]["jump_group"] == "gcp-jg"
    assert info["pra"]["jumpoint"] == "gcp-gw"


def test_a_linux_seats_session_note_does_not_promise_an_admin_password():
    """There is no admin password on a Linux seat and nothing is injected from the
    Vault. Saying otherwise sends a rep looking for a credential that does not exist."""
    _install_aws_stubs()
    ids = _seed(n=1, cloud="aws")
    asyncio.run(vd.provision_seats("pool-a", None, ids, dict(AWS_SPEC)))
    db = SessionLocal()
    try:
        linux = vd.session_info(db, ids[0])
    finally:
        db.close()
    note = linux["note"]
    assert "Shell Jump" in note
    # It may MENTION a password only to say there isn't one. What it must never do is
    # send the rep to the Azure VM password lookup, which is what the single shared
    # note used to do for every seat on every cloud.
    assert "no admin password" in note
    assert "VMs" not in note and "Vault when provisioned" not in note
    assert "ec2-user" in note, "the note names the login user the seat actually has"

    _install_stubs()
    wids = _seed(n=1)
    asyncio.run(vd.provision_seats("pool-a", None, wids, dict(SPEC)))
    db = SessionLocal()
    try:
        win = vd.session_info(db, wids[0])
    finally:
        db.close()
    assert win["pra"]["kind"] == "remote_rdp"
    assert "password" in win["note"].lower(), "the Windows note still names one"


def test_an_unbrokered_seat_reports_why_rather_than_404ing():
    _install_aws_stubs()
    ids = _seed(n=1, cloud="aws")
    db = SessionLocal()
    try:
        info = vd.session_info(db, ids[0])
        assert info["brokered"] is False
        assert vd.session_info(db, "no-such-seat") is None
    finally:
        db.close()


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
