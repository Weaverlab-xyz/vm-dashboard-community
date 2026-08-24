"""The managed Portainer / Rancher nodes on Azure.

Azure gets closer to konlet's guarantees than AWS does, and the two places it differs
are the two places to pin:

  * **A data disk is attached at CREATE time**, so cloud-init mounts it before the
    container starts with no polling. That is the guarantee konlet gives on GCE and the
    one the AWS path has to rebuild with a wait loop — so here the assertion is simply
    that the mount precedes the container.
  * **The public IP is Standard SKU, and a Standard IP must be Static**, so the node
    keeps its address across a recreate. That is a real behavioural difference from GCP
    and AWS (where Portainer Edge keys and Rancher's server-url die with the address),
    and the docs say so rather than flattening it.

And two places Azure is easy to get wrong:

  * **No NSG means no ingress at all.** A Standard public IP denies every inbound packet
    unless a rule allows it — which is *why* the Gateway VM attaches none. A node
    without one reads as a broken deploy that no allow-list change can fix, so the NSG
    has to exist before the NIC that references it.
  * **Fail-closed is deleting the rule, not emptying it.** An NSG rule with no source
    prefixes is rejected by the API, so "closed" has to mean the rule is gone — which is
    the same shape as GCE, and the opposite of AWS's revoke-everything.

Azure's SDK is not installed in CI, so only pure helpers and the config-driven
placement are exercised; the SDK calls are asserted structurally.

Run: python tests/test_node_azure.py   (or under pytest)
"""
import base64
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

try:
    from web_dashboard.services import azure_service
    from web_dashboard.services import managed_node_service as mns
except Exception as exc:  # pragma: no cover — deps absent outside CI
    try:
        import pytest
        pytest.skip(f"service import unavailable: {exc}", allow_module_level=True)
    except ModuleNotFoundError:
        print(f"SKIP: {exc}")
        sys.exit(0)

_SPECS = (mns.RANCHER, mns.PORTAINER)
_GOOD_REGION = {"resource_group": "rg-1", "jumpoint_subnet_id": "/subs/x/subnet-a"}


class _patched:
    def __init__(self, **attrs):
        self.attrs, self.saved = attrs, {}

    def __enter__(self):
        for dotted, value in self.attrs.items():
            mod_name, _, attr = dotted.rpartition("__")
            mod = {"cfg": mns.config_service, "rc": mns.region_config,
                   "cat": mns.region_catalog}[mod_name]
            self.saved[dotted] = (mod, attr, getattr(mod, attr))
            setattr(mod, attr, value)
        return self

    def __exit__(self, *exc):
        for mod, attr, old in self.saved.values():
            setattr(mod, attr, old)
        return False


def _azure_placement(spec, region_cfg, **cfg):
    def get(key, default=None):
        return cfg.get(key, "")

    def get_bool(key, default=False):
        v = cfg.get(key)
        return default if v in (None, "") else str(v).lower() in ("1", "true", "yes", "on")
    with _patched(cfg__get=get, cfg__get_bool=get_bool,
                  rc__resolve_region=lambda cloud, region: dict(region_cfg),
                  cat__default_region=lambda cloud: "eastus2",
                  cat__normalize=lambda cloud, v: v):
        return mns.resolve_placement("azure", spec)


def _cloud_init(**kw):
    return base64.b64decode(
        azure_service.container_node_cloud_init("docker run -d --name c img", **kw)).decode()


def _service_src(name):
    with open(os.path.join(_ROOT, "web_dashboard", "services", f"{name}.py"),
              encoding="utf-8") as fh:
        return fh.read()


# ── cloud-init ───────────────────────────────────────────────────────────────

def test_the_container_starts_only_after_the_data_disk_is_mounted():
    ci = _cloud_init(data_device="/dev/disk/azure/scsi1/lun0")
    assert ci.index("mount /dev/disk") < ci.index("docker run"), (
        "the container is started before the data disk is mounted — Portainer would "
        "write its database to the OS disk and lose it on the next recreate")


def test_azure_needs_no_wait_loop_because_the_disk_is_there_at_boot():
    """An Azure data disk is attached at VM create, and its device path is
    deterministic — so polling for it would be cargo-culted from the AWS path."""
    ci = _cloud_init(data_device="/dev/disk/azure/scsi1/lun0")
    assert "seq 1" not in ci, "cloud-init polls for a disk that is present at boot"
    assert "/dev/disk/azure/scsi1/lun0" in ci


def test_the_data_disk_is_never_reformatted_blindly():
    ci = _cloud_init(data_device="/dev/disk/azure/scsi1/lun0")
    assert "if ! blkid" in ci and "mkfs.ext4" in ci, (
        "mkfs is not gated on blkid — a redeploy would reformat the disk holding the "
        "node's only copy of its users, environments and settings")
    assert ci.index("blkid") < ci.index("mkfs.ext4")


def test_no_disk_means_no_mount_machinery():
    ci = _cloud_init()
    assert "mkfs" not in ci and "blkid" not in ci and "fstab" not in ci


def test_cloud_init_is_valid_yaml_shaped_and_base64():
    ci = _cloud_init()
    assert ci.startswith("#cloud-config"), ci[:40]
    assert "packages:" in ci and "runcmd:" in ci
    # Read the packages block as a list rather than searching the whole document for
    # the package name: a bare substring test also passes on a mention inside the
    # docker command, so it would not notice the install entry going missing.
    block = ci[ci.index("packages:"):ci.index("runcmd:")]
    packages = [ln[4:] for ln in block.splitlines() if ln.startswith("  - ")]
    assert packages.count("docker.io") == 1, (
        f"cloud-init does not install Docker exactly once: {packages}")


def test_a_command_with_quotes_survives_the_runcmd_encoding():
    """runcmd entries are JSON-encoded, which is what lets a docker command carrying
    single-quoted values (a bcrypt hash) through unmangled."""
    raw = base64.b64decode(azure_service.container_node_cloud_init(
        "docker run img '--admin-password' '$2y$05$a\"b'")).decode()
    assert '\\"' in raw or "'$2y$05$a" in raw, raw


# ── the NSG ──────────────────────────────────────────────────────────────────

def test_fail_closed_deletes_the_rule_rather_than_emptying_it():
    """An NSG rule with no source prefixes is rejected by the API, so 'closed' has to
    mean the rule is gone — and a Standard public IP with no allow rule denies all
    inbound, which is exactly as closed as GCE deleting its firewall rule."""
    src = _service_src("azure_service")
    body = src[src.index("def _ensure_node_nsg_sync("):]
    body = body[:body.index("\nasync def ")]
    assert "security_rules.begin_delete" in body, (
        "the empty-source-set path does not remove the rule, so the node would stay "
        "open — or the API call would simply fail and leave it open")
    assert "begin_create_or_update" in body


def test_the_nsg_itself_survives_being_closed():
    """It is attached to the node's NIC, so it could not be deleted anyway — and
    recreating it on the next open would detach and reattach for no gain."""
    src = _service_src("azure_service")
    body = src[src.index("def _ensure_node_nsg_sync("):]
    body = body[:body.index("\nasync def ")]
    assert "network_security_groups.begin_delete" not in body, (
        "closing the node deletes its NSG, which detaches it from a live NIC")


def test_an_empty_source_set_creates_nothing_on_a_first_run():
    """Creating the group for a deploy that never opens it leaves litter on an install
    that failed before it got anywhere."""
    src = _service_src("azure_service")
    body = src[src.index("def _ensure_node_nsg_sync("):]
    body = body[:body.index("\nasync def ")]
    assert "if not source_cidrs:" in body and '"created": False' in body


def test_the_nsg_is_attached_to_the_nic_at_creation():
    """Attaching afterwards leaves a window where the Standard public IP denies
    everything, which reads as a broken deploy rather than a missing rule."""
    src = _service_src("azure_service")
    body = src[src.index("def _run_vm_container_node_sync("):]
    body = body[:body.index("\nasync def ")]
    assert 'nic_params["network_security_group"]' in body, (
        "the node's NIC gets no NSG, so a Standard public IP denies every inbound "
        "packet no matter what the allow-list says")
    assert body.index('nic_params["network_security_group"]') < body.index(
        "network_interfaces.begin_create_or_update")


def test_both_launchers_look_the_nsg_up_rather_than_re_ensuring_it():
    """The ingress refresh creates the NSG — carrying the real allow-list — just before
    the launch. Re-ensuring it here with an empty source set would DELETE that rule, and
    the node would come up denying every packet: a broken deploy no allow-list change
    can fix. So the launcher resolves the id, and fails loudly when it is missing."""
    for module in ("rancher_node_service", "portainer_node_service"):
        src = _service_src(module)
        body = src[src.index("async def _launch_node_azure("):]
        body = body[:body.index("\n\ndef ") if "\n\ndef " in body else len(body)]
        assert "_node_nsg_id(" in body, f"{module} does not resolve the node's NSG"
        assert "ensure_node_nsg" not in body, (
            f"{module} re-ensures the NSG at launch, which would revoke the allow rule "
            f"the ingress refresh just wrote")
        assert body.index("_node_nsg_id(") < body.index("run_vm_container_node"), (
            f"{module} launches the VM before resolving its NSG")


def test_a_missing_nsg_fails_the_launch_rather_than_shipping_a_dead_node():
    for module in ("rancher_node_service", "portainer_node_service"):
        src = _service_src(module)
        body = src[src.index("async def _node_nsg_id("):]
        body = body[:body.index("\ndef ")]
        assert "ManagedNodeError" in body, (
            f"{module} launches with no NSG when the lookup misses — a Standard public "
            f"IP then denies all inbound and the node is unreachable")


def test_the_ingress_refresh_uses_the_placement_the_deploy_resolved():
    """Re-resolving here would drop the deploy's region pick, and on Azure/AWS that puts
    the allow-list on a DIFFERENT resource group / VPC than the node — so the node comes
    up closed while a stray NSG somewhere else holds the rules."""
    for module, fn in (("rancher_node_service", "refresh_rancher_firewall"),
                       ("portainer_node_service", "refresh_portainer_firewall")):
        src = _service_src(module)
        assert f"{fn}(db, placement=p)" in src, (
            f"{module}'s deploy does not hand its resolved placement to {fn}, so the "
            f"ingress rule can land in the wrong region")
        body = src[src.index(f"async def {fn}("):]
        body = body[:body.index("\ndef ")]
        assert "placement or _node_params()" in body, (
            f"{fn} ignores a caller-supplied placement")


# ── the public IP ────────────────────────────────────────────────────────────

def test_the_public_ip_is_standard_and_static():
    """Standard is secure-by-default (no inbound without an NSG rule) and must be
    Static — which is what makes an Azure node keep its address across a recreate, so
    Edge keys and a pinned server-url survive here."""
    src = _service_src("azure_service")
    body = src[src.index("def _run_vm_container_node_sync("):]
    body = body[:body.index("\nasync def ")]
    assert '"sku": {"name": "Standard"}' in body
    assert '"public_ip_allocation_method": "Static"' in body


def test_the_os_disk_goes_with_the_vm_but_the_data_disk_does_not():
    """That asymmetry IS durable state: delete the OS disk, detach the data disk."""
    src = _service_src("azure_service")
    body = src[src.index("def _run_vm_container_node_sync("):]
    body = body[:body.index("\nasync def ")]
    assert '"delete_option": "Delete"' in body, "the OS disk would outlive the VM"
    assert '"delete_option": "Detach"' in body, (
        "the data disk is deleted with the VM, so durable state is not durable")


def test_the_data_disk_is_attached_at_create_not_after():
    src = _service_src("azure_service")
    body = src[src.index("def _run_vm_container_node_sync("):]
    body = body[:body.index("\nasync def ")]
    assert 'storage_profile["data_disks"]' in body, (
        "the data disk is not part of the create call, so cloud-init would have to poll "
        "for it — the AWS compromise, imported for no reason")


# ── placement ────────────────────────────────────────────────────────────────

def test_a_location_with_no_resource_group_refuses_and_names_the_key():
    for spec in _SPECS:
        try:
            _azure_placement(spec, {"jumpoint_subnet_id": "/subs/x/s"})
        except mns.ManagedNodeError as exc:
            assert "azure_resource_group" in str(exc), exc
        else:
            raise AssertionError(f"{spec.feature} placed a node with no resource group")


def test_a_location_with_no_subnet_refuses_and_names_the_key():
    for spec in _SPECS:
        try:
            _azure_placement(spec, {"resource_group": "rg-1"})
        except mns.ManagedNodeError as exc:
            assert "subnet" in str(exc), exc
        else:
            raise AssertionError(f"{spec.feature} placed a node with no subnet")


def test_azure_placement_carries_the_cloud_neutral_fields():
    p = _azure_placement(mns.PORTAINER, _GOOD_REGION)
    for key in ("cloud", "account", "region", "name", "firewall_name",
                "resource_group", "subnet_id", "vm_size", "boot_disk_gb"):
        assert key in p, f"azure placement has no {key!r}"
    assert p["cloud"] == "azure"
    # The resource group is the honest truthiness check: every resource is created in it.
    assert p["account"] == "rg-1"


def test_a_bare_redeploy_stays_in_the_location_the_node_is_in():
    """Without this a redeploy with no region picked would silently relocate the node to
    the configured default — the same stickiness gcp_<feature>_zone gives GCE."""
    p = _azure_placement(mns.RANCHER, _GOOD_REGION, azure_rancher_zone="westeurope")
    assert p["region"] == "westeurope", p["region"]


def test_a_picked_region_beats_the_recorded_one():
    def get(key, default=None):
        return {"azure_rancher_zone": "westeurope"}.get(key, "")
    with _patched(cfg__get=get, cfg__get_bool=lambda k, d=False: d,
                  rc__resolve_region=lambda c, r: dict(_GOOD_REGION),
                  cat__default_region=lambda c: "eastus2",
                  cat__normalize=lambda c, v: v):
        p = mns.resolve_placement("azure", mns.RANCHER, region="uksouth")
    assert p["region"] == "uksouth", p["region"]


def test_the_zone_column_shows_the_location_rather_than_sitting_blank():
    """Azure has one location per node and no separate zone in this shape, so showing
    the location is more use than an empty cell."""
    p = _azure_placement(mns.RANCHER, _GOOD_REGION)
    assert p["zone"] == p["region"]


def test_the_rancher_vm_size_default_has_enough_memory():
    """Rancher OOMs under 4 GB. Standard_B1s (1 GB) is the Gateway VM's size and would
    ship the same defect the GCE side already refuses e2-small for."""
    from web_dashboard.config import settings
    assert settings.azure_rancher_vm_size not in (
        "Standard_B1s", "Standard_B1ls", "Standard_A1_v2"), (
        f"azure_rancher_vm_size={settings.azure_rancher_vm_size} is under 4 GB")


# ── teardown ordering ───────────────────────────────────────────────────────

def test_the_nsg_is_removed_after_the_nic_that_references_it():
    for module in ("rancher_node_service", "portainer_node_service"):
        src = _service_src(module)
        body = src[src.index("async def _stop_node("):]
        body = body[:body.index("\nasync def ", 1)]
        az = body[body.index('if cloud == "azure":'):]
        assert az.index("stop_vm_container_node") < az.index("delete_node_nsg"), (
            f"{module} tries to delete the NSG while the node's NIC still references it")


def test_the_vm_delete_takes_its_nic_and_public_ip():
    """Left behind, the NIC pins the NSG and the public IP keeps billing — and neither
    is named after anything the sandbox rollback sweeps."""
    src = _service_src("azure_service")
    body = src[src.index("def _stop_vm_container_node_sync("):]
    body = body[:body.index("\nasync def ")]
    for what in ("virtual_machines.begin_delete", "network_interfaces.begin_delete",
                 "public_ip_addresses.begin_delete"):
        assert what in body, f"the teardown leaves the node's {what.split('.')[0]} behind"


def test_the_azure_data_disk_is_only_deleted_when_asked():
    src = _service_src("portainer_node_service")
    body = src[src.index("async def _stop_node("):]
    body = body[:body.index("\nasync def ", 1)]
    az = body[body.index('if cloud == "azure":'):]
    assert "if delete_data_disk and data_disk_name:" in az, (
        "a routine teardown deletes the data disk — the one part that cannot be undone")


# ── the rollback guard ──────────────────────────────────────────────────────

def test_the_azure_rollback_refuses_to_cascade_over_a_managed_node():
    """`az group delete` cascades the whole resource group, including a Portainer data
    disk. The AWS and GCP arms refuse under a live node; this one used to just do it."""
    for path in ("scripts/sandbox/Linux/rollback.sh",
                 "scripts/sandbox/Windows/Rollback-Sandbox.ps1"):
        with open(os.path.join(_ROOT, path), encoding="utf-8") as fh:
            src = fh.read()
        assert "az vm list -g" in src and "az disk list -g" in src, (
            f"{path} cascades the resource group without checking for a managed node "
            f"or an orphaned data disk")


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
