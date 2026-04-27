"""
Proxmox VE service layer — Proxmox REST API via proxmoxer.

Credential priority:
  1. API token  (PROXMOX_TOKEN_ID + PROXMOX_TOKEN_SECRET) — preferred
  2. Password   (PROXMOX_USER + PROXMOX_PASSWORD) — legacy

Supports QEMU VMs and LXC containers.  All blocking SDK calls run in
asyncio.to_thread() so the FastAPI event loop is never blocked.
"""
import asyncio
import logging
import time
import urllib.parse
from typing import Optional

logger = logging.getLogger(__name__)

# ── Cloud image catalog ───────────────────────────────────────────────────────

_CLOUD_IMAGES = [
    {
        "id": "ubuntu-2404",
        "name": "Ubuntu 24.04 LTS (Noble)",
        "distro": "ubuntu",
        "filename": "noble-server-cloudimg-amd64.img",
        "url": "https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img",
    },
    {
        "id": "ubuntu-2204",
        "name": "Ubuntu 22.04 LTS (Jammy)",
        "distro": "ubuntu",
        "filename": "jammy-server-cloudimg-amd64.img",
        "url": "https://cloud-images.ubuntu.com/jammy/current/jammy-server-cloudimg-amd64.img",
    },
    {
        "id": "debian-12",
        "name": "Debian 12 (Bookworm)",
        "distro": "debian",
        "filename": "debian-12-genericcloud-amd64.qcow2",
        "url": "https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-genericcloud-amd64.qcow2",
    },
    {
        "id": "rocky-9",
        "name": "Rocky Linux 9",
        "distro": "rocky",
        "filename": "Rocky-9-GenericCloud.latest.x86_64.qcow2",
        "url": "https://dl.rockylinux.org/pub/rocky/9/images/x86_64/Rocky-9-GenericCloud.latest.x86_64.qcow2",
    },
    {
        "id": "rocky-8",
        "name": "Rocky Linux 8",
        "distro": "rocky",
        "filename": "Rocky-8-GenericCloud.latest.x86_64.qcow2",
        "url": "https://dl.rockylinux.org/pub/rocky/8/images/x86_64/Rocky-8-GenericCloud.latest.x86_64.qcow2",
    },
    {
        "id": "almalinux-9",
        "name": "AlmaLinux 9",
        "distro": "alma",
        "filename": "AlmaLinux-9-GenericCloud-latest.x86_64.qcow2",
        "url": "https://repo.almalinux.org/almalinux/9/cloud/x86_64/images/AlmaLinux-9-GenericCloud-latest.x86_64.qcow2",
    },
    {
        "id": "centos-stream-9",
        "name": "CentOS Stream 9",
        "distro": "centos",
        "filename": "CentOS-Stream-GenericCloud-9-latest.x86_64.qcow2",
        "url": "https://cloud.centos.org/centos/9-stream/x86_64/images/CentOS-Stream-GenericCloud-9-latest.x86_64.qcow2",
    },
]


class ProxmoxError(Exception):
    pass


def _cfg(key: str) -> str:
    from . import config_service
    return config_service.get(key) or ""


def _cfg_bool(key: str, default: bool = False) -> bool:
    from . import config_service
    return config_service.get_bool(key, default)


def _require_proxmoxer():
    try:
        import proxmoxer  # noqa: F401
    except ImportError:
        raise ProxmoxError(
            "proxmoxer is not installed — run: pip install proxmoxer requests"
        )


def _client():
    """Return an authenticated ProxmoxAPI client."""
    _require_proxmoxer()
    from proxmoxer import ProxmoxAPI

    host = _cfg("proxmox_host")
    if not host:
        raise ProxmoxError("PROXMOX_HOST is not configured")

    port = int(_cfg("proxmox_port") or "8006")
    verify_ssl = _cfg_bool("proxmox_verify_ssl", False)
    user = _cfg("proxmox_user") or "root@pam"

    token_id = _cfg("proxmox_token_id")
    token_secret = _cfg("proxmox_token_secret")

    if token_id and token_secret:
        return ProxmoxAPI(
            host,
            user=user,
            token_name=token_id,
            token_value=token_secret,
            port=port,
            verify_ssl=verify_ssl,
        )

    password = _cfg("proxmox_password")
    if not password:
        raise ProxmoxError(
            "Proxmox credentials not configured. "
            "Set PROXMOX_TOKEN_ID + PROXMOX_TOKEN_SECRET or PROXMOX_PASSWORD."
        )
    return ProxmoxAPI(
        host,
        user=user,
        password=password,
        port=port,
        verify_ssl=verify_ssl,
    )


# ── Task polling ──────────────────────────────────────────────────────────────

def _wait_for_task_sync(pve, node: str, upid: str, timeout: int = 600) -> None:
    """Poll a Proxmox task until stopped or timeout."""
    deadline = time.monotonic() + timeout
    while True:
        task = pve.nodes(node).tasks(upid).status.get()
        if task.get("status") == "stopped":
            exit_code = task.get("exitstatus", "OK")
            if exit_code != "OK":
                raise ProxmoxError(f"Task {upid} failed: {exit_code}")
            return
        if time.monotonic() > deadline:
            raise ProxmoxError(f"Timed out waiting for task {upid} after {timeout}s")
        time.sleep(5)


# ── Node helpers ──────────────────────────────────────────────────────────────

def _list_nodes_sync() -> list[dict]:
    pve = _client()
    nodes = []
    for n in pve.nodes.get():
        nodes.append({
            "node":   n.get("node", ""),
            "status": n.get("status", "unknown"),
            "cpu":    round(n.get("cpu", 0) * 100, 1),
            "mem_used": n.get("mem", 0),
            "mem_total": n.get("maxmem", 0),
            "uptime": n.get("uptime", 0),
        })
    return sorted(nodes, key=lambda x: x["node"])


# ── Resource listing ──────────────────────────────────────────────────────────

def _list_node_resources_sync(node: str) -> list[dict]:
    """List QEMU VMs and LXC containers on a single node."""
    pve = _client()
    resources = []

    for vm in pve.nodes(node).qemu.get():
        resources.append(_normalise(vm, node, "qemu"))

    for ct in pve.nodes(node).lxc.get():
        resources.append(_normalise(ct, node, "lxc"))

    return sorted(resources, key=lambda x: x["vmid"])


def _list_all_resources_sync(nodes: Optional[list[str]] = None) -> list[dict]:
    pve = _client()
    if nodes is None:
        nodes = [n["node"] for n in pve.nodes.get()]

    all_resources = []
    for node in nodes:
        try:
            all_resources.extend(_list_node_resources_sync(node))
        except Exception as exc:
            logger.warning("Proxmox: could not list resources on node %s: %s", node, exc)
    return all_resources


def _normalise(raw: dict, node: str, vm_type: str) -> dict:
    status = raw.get("status", "unknown")
    return {
        "vmid":      raw.get("vmid", 0),
        "name":      raw.get("name", f"{vm_type}-{raw.get('vmid', 0)}"),
        "node":      node,
        "type":      vm_type,          # "qemu" or "lxc"
        "status":    status,
        "is_running": status == "running",
        "cpu_cores": raw.get("cpus", raw.get("cores", 0)),
        "cpu_usage": round(raw.get("cpu", 0) * 100, 1),
        "mem_used":  raw.get("mem", 0),
        "mem_total": raw.get("maxmem", 0),
        "disk_used": raw.get("disk", 0),
        "disk_total": raw.get("maxdisk", 0),
        "uptime":    raw.get("uptime", 0),
        "tags":      raw.get("tags", ""),
        "template":  bool(raw.get("template", 0)),
    }


# ── Storage listing ───────────────────────────────────────────────────────────

def _list_storage_sync(node: str) -> list[dict]:
    """List storage pools on a node that are active and support images/import."""
    pve = _client()
    stores = pve.nodes(node).storage.get()
    result = []
    for s in stores:
        content_str = s.get("content", "")
        if s.get("active", 0) and ("images" in content_str or "import" in content_str):
            result.append({
                "storage": s.get("storage", ""),
                "type":    s.get("type", ""),
                "content": content_str,
                "avail":   s.get("avail", 0),
                "total":   s.get("total", 0),
            })
    return sorted(result, key=lambda x: x["storage"])


# ── Template listing ──────────────────────────────────────────────────────────

def _list_templates_sync() -> list[dict]:
    """Return all QEMU templates across all nodes."""
    all_resources = _list_all_resources_sync()
    return [r for r in all_resources if r["template"] and r["type"] == "qemu"]


# ── Image import + template creation ─────────────────────────────────────────

def _import_and_create_template_sync(
    node: str,
    storage: str,
    image_url: str,
    image_filename: str,
    template_name: str,
    vcpus: int = 2,
    memory_mb: int = 2048,
    disk_size: str = "20G",
    username: str = "ubuntu",
) -> dict:
    """
    Download a cloud image to Proxmox storage and create a cloud-init template.

    Requires PVE 7.2+ (download-url API + import-from scsi0 syntax).
    """
    pve = _client()

    # Step 1: Download the cloud image to storage (content=import)
    logger.info("Proxmox: downloading %s to %s:%s", image_filename, node, storage)
    dl = getattr(pve.nodes(node).storage(storage), "download-url")
    upid = dl.post(url=image_url, filename=image_filename, content="import")
    _wait_for_task_sync(pve, node, upid, timeout=900)
    logger.info("Proxmox: download complete — %s", image_filename)

    # Step 2: Allocate next VMID
    vmid = int(pve.cluster.nextid.get())

    # Step 3: Create QEMU VM importing the disk
    vol_id = f"{storage}:import/{image_filename}"
    pve.nodes(node).qemu.post(
        vmid=vmid,
        name=template_name,
        memory=memory_mb,
        cores=vcpus,
        cpu="host",
        ostype="l26",
        net0="virtio,bridge=vmbr0",
        scsi0=f"{storage}:0,import-from={vol_id},format=raw",
        ide2=f"{storage}:cloudinit",
        boot="order=scsi0",
        serial0="socket",
        vga="serial0",
        agent="enabled=1",
        ciuser=username,
        ipconfig0="ip=dhcp",
        scsihw="virtio-scsi-pci",
    )
    logger.info("Proxmox: created VM %d (%s)", vmid, template_name)

    # Step 4: Resize disk
    if disk_size and disk_size not in ("0", "0G"):
        try:
            pve.nodes(node).qemu(vmid).resize.put(disk="scsi0", size=disk_size)
        except Exception as e:
            logger.warning("Proxmox: disk resize warning for vmid %d: %s", vmid, e)

    # Step 5: Convert to template
    pve.nodes(node).qemu(vmid).template.post()
    logger.info("Proxmox: vmid %d converted to template", vmid)

    return {
        "vmid": vmid,
        "name": template_name,
        "node": node,
        "storage": storage,
        "status": "created",
    }


# ── Deploy from template ──────────────────────────────────────────────────────

def _deploy_from_template_sync(
    node: str,
    template_vmid: int,
    vm_name: str,
    username: str = "",
    ssh_public_key: str = "",
    full_clone: bool = True,
) -> dict:
    """Clone a template and start the resulting VM."""
    pve = _client()

    new_vmid = int(pve.cluster.nextid.get())
    logger.info("Proxmox: cloning template %d → %d (%s)", template_vmid, new_vmid, vm_name)

    upid = pve.nodes(node).qemu(template_vmid).clone.post(
        newid=new_vmid,
        name=vm_name,
        full=1 if full_clone else 0,
    )
    _wait_for_task_sync(pve, node, upid, timeout=300)

    # Update cloud-init if caller provided credentials
    ci_params: dict = {}
    if username:
        ci_params["ciuser"] = username
    if ssh_public_key:
        ci_params["sshkeys"] = urllib.parse.quote(ssh_public_key, safe="")
    if ci_params:
        pve.nodes(node).qemu(new_vmid).config.put(**ci_params)

    # Start
    pve.nodes(node).qemu(new_vmid).status.start.post()
    logger.info("Proxmox: started vmid %d (%s)", new_vmid, vm_name)

    return {
        "vmid":         new_vmid,
        "name":         vm_name,
        "node":         node,
        "template_vmid": template_vmid,
        "status":       "starting",
    }


# ── Delete VM or template ─────────────────────────────────────────────────────

def _delete_vm_sync(node: str, vmid: int, vm_type: str = "qemu") -> dict:
    """Stop (if running) and delete a QEMU VM or LXC container."""
    pve = _client()
    endpoint = pve.nodes(node).qemu(vmid) if vm_type == "qemu" else pve.nodes(node).lxc(vmid)

    try:
        status = endpoint.status.current.get()
        if status.get("status") == "running":
            stop_upid = endpoint.status.stop.post()
            _wait_for_task_sync(pve, node, stop_upid, timeout=60)
    except Exception:
        pass  # already stopped or template

    upid = endpoint.delete()
    _wait_for_task_sync(pve, node, upid, timeout=120)
    logger.info("Proxmox: deleted %s %d on %s", vm_type, vmid, node)

    return {"vmid": vmid, "node": node, "vm_type": vm_type, "status": "deleted"}


# ── Power operations ──────────────────────────────────────────────────────────

def _power_op_sync(node: str, vmid: int, vm_type: str, op: str) -> dict:
    """
    Execute a power operation and wait for the resulting task to complete.
    op must be one of: start, stop, shutdown, reboot, reset, suspend.
    Returns {"task": upid, "status": "OK"} on success.
    """
    pve = _client()
    endpoint = pve.nodes(node).qemu(vmid) if vm_type == "qemu" else pve.nodes(node).lxc(vmid)

    upid = getattr(endpoint.status, op).post()
    logger.info("Proxmox: %s %s/%s → task %s", op, node, vmid, upid)
    _wait_for_task_sync(pve, node, upid, timeout=300)
    return {"task": upid, "status": "OK"}


# ── VM config / detail ────────────────────────────────────────────────────────

def _get_config_sync(node: str, vmid: int, vm_type: str) -> dict:
    pve = _client()
    endpoint = pve.nodes(node).qemu(vmid) if vm_type == "qemu" else pve.nodes(node).lxc(vmid)
    try:
        cfg = endpoint.config.get()
    except Exception:
        cfg = {}
    try:
        status = endpoint.status.current.get()
    except Exception:
        status = {}

    # Extract IP addresses from agent (QEMU only)
    ips: list[str] = []
    if vm_type == "qemu":
        try:
            ifaces = endpoint.agent("network-get-interfaces").get()
            for iface in ifaces.get("result", []):
                for addr in iface.get("ip-addresses", []):
                    ip = addr.get("ip-address", "")
                    if ip and not ip.startswith("127.") and not ip.startswith("::1") and ":" not in ip:
                        ips.append(ip)
        except Exception:
            pass

    return {
        "vmid":      vmid,
        "node":      node,
        "type":      vm_type,
        "name":      cfg.get("name", status.get("name", "")),
        "status":    status.get("status", "unknown"),
        "is_running": status.get("status") == "running",
        "cpu_cores": cfg.get("cores", cfg.get("cpus", 0)),
        "cpu_usage": round(status.get("cpu", 0) * 100, 1),
        "mem_total": cfg.get("memory", 0),
        "mem_used":  status.get("mem", 0),
        "os_type":   cfg.get("ostype", cfg.get("ostemplate", "")),
        "ip_addresses": ips,
        "description": cfg.get("description", cfg.get("hostname", "")),
        "tags":      cfg.get("tags", ""),
        "uptime":    status.get("uptime", 0),
    }


# ── Async public API ──────────────────────────────────────────────────────────

def list_cloud_images() -> list[dict]:
    return _CLOUD_IMAGES


async def list_nodes() -> list[dict]:
    try:
        return await asyncio.to_thread(_list_nodes_sync)
    except ProxmoxError:
        raise
    except Exception as e:
        raise ProxmoxError(f"Failed to list Proxmox nodes: {e}") from e


async def list_resources(nodes: Optional[list[str]] = None) -> list[dict]:
    try:
        return await asyncio.to_thread(_list_all_resources_sync, nodes)
    except ProxmoxError:
        raise
    except Exception as e:
        raise ProxmoxError(f"Failed to list Proxmox resources: {e}") from e


async def list_storage(node: str) -> list[dict]:
    try:
        return await asyncio.to_thread(_list_storage_sync, node)
    except ProxmoxError:
        raise
    except Exception as e:
        raise ProxmoxError(f"Failed to list Proxmox storage: {e}") from e


async def list_templates() -> list[dict]:
    try:
        return await asyncio.to_thread(_list_templates_sync)
    except ProxmoxError:
        raise
    except Exception as e:
        raise ProxmoxError(f"Failed to list Proxmox templates: {e}") from e


async def import_and_create_template(
    node: str,
    storage: str,
    image_url: str,
    image_filename: str,
    template_name: str,
    vcpus: int = 2,
    memory_mb: int = 2048,
    disk_size: str = "20G",
    username: str = "ubuntu",
) -> dict:
    try:
        return await asyncio.to_thread(
            _import_and_create_template_sync,
            node, storage, image_url, image_filename,
            template_name, vcpus, memory_mb, disk_size, username,
        )
    except ProxmoxError:
        raise
    except Exception as e:
        raise ProxmoxError(f"Image import failed: {e}") from e


async def deploy_from_template(
    node: str,
    template_vmid: int,
    vm_name: str,
    username: str = "",
    ssh_public_key: str = "",
    full_clone: bool = True,
) -> dict:
    try:
        return await asyncio.to_thread(
            _deploy_from_template_sync,
            node, template_vmid, vm_name, username, ssh_public_key, full_clone,
        )
    except ProxmoxError:
        raise
    except Exception as e:
        raise ProxmoxError(f"Deploy from template failed: {e}") from e


async def delete_vm(node: str, vmid: int, vm_type: str = "qemu") -> dict:
    try:
        return await asyncio.to_thread(_delete_vm_sync, node, vmid, vm_type)
    except ProxmoxError:
        raise
    except Exception as e:
        raise ProxmoxError(f"Delete VM {vmid} on {node} failed: {e}") from e


async def get_vm_detail(node: str, vmid: int, vm_type: str) -> dict:
    try:
        return await asyncio.to_thread(_get_config_sync, node, vmid, vm_type)
    except ProxmoxError:
        raise
    except Exception as e:
        raise ProxmoxError(f"Failed to get VM detail: {e}") from e


async def power_op(node: str, vmid: int, vm_type: str, op: str) -> dict:
    """
    Run a power operation: start | stop | shutdown | reboot | reset | suspend.
    'stop' is force-off; prefer 'shutdown' for graceful guest shutdown.
    """
    valid = {"start", "stop", "shutdown", "reboot", "reset", "suspend"}
    if op not in valid:
        raise ProxmoxError(f"Invalid operation '{op}'. Must be one of: {', '.join(sorted(valid))}")
    if vm_type not in ("qemu", "lxc"):
        raise ProxmoxError(f"Invalid vm_type '{vm_type}'. Must be 'qemu' or 'lxc'.")
    if vm_type == "lxc" and op in ("reset", "suspend"):
        raise ProxmoxError(f"Operation '{op}' is not supported for LXC containers.")
    try:
        return await asyncio.to_thread(_power_op_sync, node, vmid, vm_type, op)
    except ProxmoxError:
        raise
    except Exception as e:
        raise ProxmoxError(f"Power operation '{op}' on {node}/{vmid} failed: {e}") from e
