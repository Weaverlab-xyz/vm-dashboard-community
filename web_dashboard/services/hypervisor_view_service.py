"""Render the synced inventory cache in the shape each hypervisor page expects.

An **agent-bound** connection lives on a network the dashboard has no route to — that is
the entire reason it is bound to an agent. So the per-hypervisor pages cannot query it
live: before this module they called the service anyway and returned a 502, which made an
agent-bound connection unusable from the UI even though its inventory was being synced on
a schedule.

``hypervisor_sync_service.list_vms`` holds that inventory in one generic shape. Every page
binds to a *different* shape — Proxmox wants ``vmid``/``node``/``status``, Nutanix wants
``uuid``/``power_state``, Hyper-V wants ``vmid``/``state_label`` — so this module projects
generic → per-kind. Getting that wrong renders a blank table rather than an error, which
is why the projections are pinned key-for-key by a test.

Two rules the projections must keep, and both are about not lying to an operator:

* **``is_running`` is derived per product.** Each spells its power state differently
  (``running`` / ``ON`` / ``poweredOn`` / ``Running``) and every template drives its power
  buttons off this one boolean.
* **A field the cache cannot know is ABSENT, never zero.** CPU usage, uptime, disk figures
  and guest-tools state are live-only. Defaulting them to 0 would render "0% CPU" and
  "0 B disk" as though they had been measured, which is worse than a blank cell.

Pure: takes rows, returns rows. The database read is the caller's.
"""

# Power-state spellings that mean "this VM is on", per kind. Compared lower-case, so a
# product that changes capitalisation does not silently turn every VM off in the UI.
_RUNNING = {
    "proxmox":     {"running"},
    "nutanix":     {"on", "powered_on", "poweredon"},
    "vsphere":     {"poweredon", "powered_on", "on"},
    "esxi":        {"poweredon", "powered_on", "on"},
    "xcpng":       {"running"},
    "hyperv":      {"running", "starting", "resuming", "2", "10", "14"},
    "workstation": {"poweredon", "powered_on", "on"},
}

# Hyper-V templates read a numeric `state` plus a label. Map back from the label the
# sibling runner reports, so the page's existing badge logic keeps working.
_HYPERV_STATE_INTS = {"unknown": 0, "running": 2, "off": 3, "stopping": 4, "saved": 6,
                      "paused": 9, "starting": 10, "reset": 11, "saving": 12,
                      "pausing": 13, "resuming": 14}


def is_running(kind: str, power_state: str) -> bool:
    return str(power_state or "").strip().lower() in _RUNNING.get(kind, set())


def project(kind: str, rows: list) -> list:
    """Cache rows in the shape ``kind``'s page and template expect."""
    projector = _PROJECTORS.get(kind)
    if projector is None:
        return list(rows or [])
    return [projector(row) for row in (rows or [])]


def _proxmox(row: dict) -> dict:
    # `vmid` is an int everywhere in the Proxmox page (sorting, the power payload), and
    # the cache stores every id as a string. Coerce, and fall back rather than raise:
    # a non-numeric id means a row we cannot act on, not a broken page.
    try:
        vmid = int(row.get("vm_id") or 0)
    except (TypeError, ValueError):
        vmid = 0
    return {
        "vmid": vmid,
        "name": row.get("name") or "",
        "node": row.get("scope") or "",
        "type": row.get("vm_type") or "qemu",
        "status": row.get("power_state") or "unknown",
        "is_running": is_running("proxmox", row.get("power_state")),
        "cpu_cores": row.get("vcpus"),
        # mem_total is bytes on this page; the cache holds MiB.
        "mem_total": (row.get("mem_mib") or 0) * 1024 * 1024 if row.get("mem_mib") else None,
        "tags": "",
        "template": False,
    }


def _nutanix(row: dict) -> dict:
    return {
        "uuid": row.get("vm_id") or "",
        "name": row.get("name") or "",
        "power_state": (row.get("power_state") or "").upper(),
        "is_running": is_running("nutanix", row.get("power_state")),
        "vcpus": row.get("vcpus"),
        "mem_mib": row.get("mem_mib"),
        "ip_addresses": row.get("ip_addresses") or [],
        "cluster": row.get("scope") or "",
        "description": "",
    }


def _vsphere(row: dict) -> dict:
    return {
        "moref": row.get("vm_id") or "",
        "name": row.get("name") or "",
        "power_state": row.get("power_state") or "",
        "is_running": is_running("vsphere", row.get("power_state")),
        "host": row.get("scope") or "",
        "datacenter": row.get("scope") or "",
        "cpu_count": row.get("vcpus"),
        "mem_mb": row.get("mem_mib"),
        "ip_addresses": row.get("ip_addresses") or [],
        "template": False,
        "annotation": "",
    }


def _xcpng(row: dict) -> dict:
    return {
        "uuid": row.get("vm_id") or "",
        "name": row.get("name") or "",
        "power_state": row.get("power_state") or "",
        "is_running": is_running("xcpng", row.get("power_state")),
        "host": row.get("scope") or "",
        "vcpus": row.get("vcpus"),
        "mem_mb": row.get("mem_mib"),
        "ip_addresses": row.get("ip_addresses") or [],
        "os_version": "",
    }


def _hyperv(row: dict) -> dict:
    label = (row.get("power_state") or "Unknown").strip()
    return {
        "vmid": row.get("vm_id") or "",
        "name": row.get("name") or "",
        "state": _HYPERV_STATE_INTS.get(label.lower(), 0),
        "state_label": label.capitalize() if label.islower() else label,
        "is_running": is_running("hyperv", label),
        "processor_count": row.get("vcpus"),
        "mem_assigned_mb": row.get("mem_mib"),
        "ip_addresses": row.get("ip_addresses") or [],
    }


def _workstation(row: dict) -> dict:
    """Workstation has no page of its own — its rows are merged into /vms, which keys on
    a VMX path. The agent reports vmrest's opaque id, so that is what identifies the row;
    ``vmx_path`` carries the path when the agent could read it, for display only."""
    return {
        "vm_id": row.get("vm_id") or "",
        "vmx_path": row.get("scope") or "",
        "name": row.get("name") or "",
        "power_state": row.get("power_state") or "",
        "is_running": is_running("workstation", row.get("power_state")),
        "vcpus": row.get("vcpus"),
        "mem_mib": row.get("mem_mib"),
        "ip_addresses": row.get("ip_addresses") or [],
    }


_PROJECTORS = {
    "proxmox": _proxmox,
    "nutanix": _nutanix,
    "vsphere": _vsphere,
    "esxi": _vsphere,
    "xcpng": _xcpng,
    "hyperv": _hyperv,
    "workstation": _workstation,
}


def synced_rows(db, conn) -> list:
    """This connection's cached inventory, in its page's shape.

    Returns a plain list, matching what the live path returns, so the endpoints keep one
    response shape and no template has to branch on the payload. The "you are looking at
    a cache" signal rides on the CONNECTION instead — ``via_agent`` and ``last_sync_at``
    are already on that row and are what the banner needs — which is both less code and
    the more honest home for it: it is a property of the connection, not of one request.
    """
    from . import hypervisor_sync_service

    return project(conn.kind, hypervisor_sync_service.list_vms(db, conn.id))
