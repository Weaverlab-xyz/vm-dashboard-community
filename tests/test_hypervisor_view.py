"""Projecting the synced inventory cache into each hypervisor page's shape.

An agent-bound connection cannot be queried live — that is why it is bound to an agent —
so the pages read `hypervisor_vm_cache` instead. The cache is one generic shape and every
page binds to a different one, and getting a projection wrong renders a BLANK TABLE rather
than an error. Nothing else would catch that, which is why the keys are pinned here
against the real normalisers.

Two properties matter more than the field names:

* `is_running` drives every page's power buttons, and each product spells its power state
  differently. Getting it wrong shows a running VM as stopped, with a Start button.
* A field the cache cannot know must be ABSENT, not zero — otherwise a cached row renders
  "0% CPU" as though it had been measured.

Pure, stdlib only. Runs under pytest, or standalone:
    python tests/test_hypervisor_view.py
"""
import ast
import importlib.util
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PATH = os.path.join(_ROOT, "web_dashboard", "services", "hypervisor_view_service.py")
_spec = importlib.util.spec_from_file_location("hypervisor_view_service", _PATH)
view = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(view)


def _row(**kw):
    base = {"vm_id": "101", "name": "web01", "power_state": "running", "vcpus": 4,
            "mem_mib": 8192, "ip_addresses": ["10.0.0.5"], "scope": "pve1",
            "vm_type": "qemu", "tags": [], "synced_at": "2026-08-08T10:00:00"}
    base.update(kw)
    return base


def _normaliser_keys(module: str, func: str) -> set:
    """The keys the LIVE path returns, read out of the service source.

    Read rather than imported: these modules pull in proxmoxer/pyVmomi/httpx, and this
    test must run without them. The point is that the projection and the normaliser
    cannot drift apart silently.
    """
    path = os.path.join(_ROOT, "web_dashboard", "services", f"{module}_service.py")
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    node = next(n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == func)
    for child in ast.walk(node):
        if isinstance(child, ast.Dict) and len(child.keys) > 4:
            return {k.value for k in child.keys if isinstance(k, ast.Constant)}
    raise AssertionError(f"no result dict found in {module}.{func}")


# ── the projections match what each page actually binds to ────────────────────

def test_every_projected_key_exists_in_the_live_shape():
    """The projection may return FEWER keys than the live path (live-only fields are
    absent by design) but never a key the page has never seen — that would be a field
    invented for the cache alone, which no template reads."""
    for kind, module, func in (("proxmox", "proxmox", "_normalise"),
                               ("nutanix", "nutanix", "_normalise_vm"),
                               ("hyperv", "hyperv", "_normalise_vm"),
                               ("vsphere", "vsphere", "_normalise_vm")):
        live = _normaliser_keys(module, func)
        projected = set(view.project(kind, [_row()])[0])
        extra = projected - live
        assert not extra, f"{kind}: projection invents {extra}, which no template reads"


def test_proxmox_projects_the_identity_fields_its_page_sorts_on():
    out = view.project("proxmox", [_row()])[0]
    assert out["vmid"] == 101 and isinstance(out["vmid"], int), "the page sorts on an int"
    assert out["node"] == "pve1" and out["type"] == "qemu"
    assert out["status"] == "running" and out["is_running"] is True


def test_proxmox_memory_is_converted_to_the_bytes_its_page_expects():
    # mem_total is bytes on that page; the cache holds MiB. Getting this wrong shows
    # 8192 bytes of RAM.
    assert view.project("proxmox", [_row(mem_mib=8192)])[0]["mem_total"] == 8192 * 1024 * 1024


def test_a_non_numeric_proxmox_id_does_not_break_the_page():
    assert view.project("proxmox", [_row(vm_id="not-a-number")])[0]["vmid"] == 0


def test_nutanix_uppercases_its_power_state():
    out = view.project("nutanix", [_row(power_state="on")])[0]
    assert out["uuid"] == "101" and out["power_state"] == "ON"
    assert out["is_running"] is True and out["cluster"] == "pve1"


def test_hyperv_projects_both_the_numeric_state_and_its_label():
    out = view.project("hyperv", [_row(power_state="Running")])[0]
    assert out["state"] == 2, "the badge logic keys on the int"
    assert out["state_label"] == "Running" and out["is_running"] is True
    assert out["processor_count"] == 4 and out["mem_assigned_mb"] == 8192


def test_vsphere_and_xcpng_project_their_own_identity_key():
    assert view.project("vsphere", [_row(power_state="poweredOn")])[0]["moref"] == "101"
    assert view.project("xcpng", [_row(power_state="Running")])[0]["uuid"] == "101"


def test_esxi_borrows_the_vsphere_shape():
    # Same product, same page — only the transport differs.
    assert view.project("esxi", [_row()])[0].keys() == view.project("vsphere", [_row()])[0].keys()


def test_an_unknown_kind_passes_rows_through_untouched():
    rows = [_row()]
    assert view.project("virtualbox", rows) == rows


# ── is_running, per product spelling ──────────────────────────────────────────

def test_is_running_understands_each_products_spelling():
    on = {"proxmox": "running", "nutanix": "ON", "vsphere": "poweredOn",
          "xcpng": "Running", "hyperv": "Running", "workstation": "poweredOn"}
    for kind, state in on.items():
        assert view.is_running(kind, state) is True, f"{kind} {state}"


def test_is_running_is_case_insensitive():
    """A product changing capitalisation must not silently show every VM as stopped."""
    for spelling in ("poweredOn", "POWEREDON", "poweredon"):
        assert view.is_running("vsphere", spelling) is True


def test_a_stopped_or_unknown_state_is_not_running():
    for kind, state in (("proxmox", "stopped"), ("nutanix", "OFF"),
                        ("vsphere", "poweredOff"), ("xcpng", "Halted"),
                        ("hyperv", "Off"), ("workstation", "poweredOff")):
        assert view.is_running(kind, state) is False, f"{kind} {state}"
    assert view.is_running("proxmox", "") is False
    assert view.is_running("proxmox", None) is False


# ── the honesty rule ──────────────────────────────────────────────────────────

def test_live_only_fields_are_absent_rather_than_zero():
    """A cached row must not claim 0% CPU or 0s uptime — those were never measured, and
    a fabricated zero is worse than a blank cell."""
    fabricated = ("cpu_usage", "uptime", "uptime_secs", "disk_used", "disk_total",
                  "mem_used", "ngt_enabled", "ngt_reachable", "tools_status",
                  "integration_services_state", "tools_installed")
    for kind in ("proxmox", "nutanix", "hyperv", "vsphere", "xcpng"):
        out = view.project(kind, [_row()])[0]
        present = [k for k in fabricated if k in out]
        assert not present, f"{kind} fabricates {present} from a cache that cannot know it"


def test_a_field_the_cache_lacks_projects_as_none_not_zero():
    out = view.project("proxmox", [_row(vcpus=None, mem_mib=None)])[0]
    assert out["cpu_cores"] is None
    assert out["mem_total"] is None, "0 would read as a VM with no RAM"


def test_an_empty_cache_projects_to_an_empty_list():
    for kind in ("proxmox", "nutanix", "hyperv", "vsphere", "xcpng", "workstation"):
        assert view.project(kind, []) == []
        assert view.project(kind, None) == []


# ── workstation ───────────────────────────────────────────────────────────────

def test_workstation_keeps_the_vmrest_id_and_carries_the_vmx_path():
    out = view.project("workstation", [_row(vm_id="AB12", scope=r"C:\VMs\win11\win11.vmx",
                                            power_state="poweredOn")])[0]
    assert out["vm_id"] == "AB12", "vmrest's opaque id is the identity, not the path"
    assert out["vmx_path"].endswith("win11.vmx")
    assert out["is_running"] is True


def test_every_supported_kind_has_a_projector():
    """A kind that resolves but has no projector silently falls through to the generic
    shape, which renders as an empty table rather than an error."""
    import ast as _ast
    conn_path = os.path.join(_ROOT, "web_dashboard", "services",
                             "hypervisor_connection_service.py")
    with open(conn_path, encoding="utf-8") as fh:
        source = fh.read()
    kinds = set(_ast.literal_eval(
        re.search(r"^VALID_KINDS = (\([^)]*\))", source, re.M).group(1)))
    missing = kinds - set(view._PROJECTORS)
    assert not missing, f"no projection for {missing}"



# ── guest OS labels ───────────────────────────────────────────────────────────

def test_the_lying_windows_codes_are_mapped_exactly():
    """`windows9-64` is Windows 10. VMware kept the internal name when Microsoft skipped
    the number, so the single most common Workstation guest is the one a substring rule
    gets wrong — which is why exact codes are consulted first."""
    assert view.guest_os_label("windows9-64") == "Windows 10 (64-bit)"
    assert view.guest_os_label("windows11-64") == "Windows 11"


def test_server_is_matched_before_windows():
    """Every Windows Server code also contains "windows", so the looser rule ordered
    first would label a domain controller "Windows"."""
    assert view.guest_os_label("windows2019srv-64") == "Windows Server 2019"
    assert view.guest_os_label("windows2016srv-64").startswith("Windows Server")


def test_a_family_code_is_labelled_and_keeps_its_bitness():
    assert view.guest_os_label("ubuntu-64") == "Ubuntu (64-bit)"
    assert view.guest_os_label("rhel9-64") == "Red Hat Enterprise Linux (64-bit)"
    assert view.guest_os_label("freebsd") == "FreeBSD"


def test_an_unknown_code_falls_back_to_itself_not_to_unknown():
    """`nonesuch-99` at least tells an operator what the hypervisor said and tells a
    maintainer the table wants an entry. "Unknown" says neither, and hides the difference
    between "the agent reported nothing" and "we have no row for what it reported"."""
    assert view.guest_os_label("nonesuch-99") == "nonesuch-99"


def test_no_os_reported_is_empty_not_a_label():
    """An agent older than the guest_os key must render a dash, not a claim."""
    assert view.guest_os_label(None) == ""
    assert view.guest_os_label("") == ""


def test_the_workstation_projection_carries_the_os_and_the_sync_time():
    out = view.project("workstation", [_row(vm_id="AB12", power_state="poweredOn",
                                            guest_os="windows9-64",
                                            synced_at="2026-08-18T12:00:00")])[0]
    assert out["os_type"] == "Windows 10 (64-bit)"
    assert out["synced_at"] == "2026-08-18T12:00:00", (
        "the page's staleness line has nothing to read without this")


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
    sys.exit(1 if failures else 0)
