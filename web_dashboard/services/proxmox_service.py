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
from typing import Optional

logger = logging.getLogger(__name__)


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

    # Poll until the task finishes (max 5 min)
    import time
    for _ in range(60):
        task = pve.nodes(node).tasks(upid).status.get()
        if task.get("status") == "stopped":
            exit_code = task.get("exitstatus", "OK")
            if exit_code != "OK":
                raise ProxmoxError(f"Task {upid} failed: {exit_code}")
            return {"task": upid, "status": "OK"}
        time.sleep(5)

    raise ProxmoxError(f"Timed out waiting for task {upid}")


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
    # LXC does not support reboot/reset/suspend the same way
    if vm_type == "lxc" and op in ("reset", "suspend"):
        raise ProxmoxError(f"Operation '{op}' is not supported for LXC containers.")
    try:
        return await asyncio.to_thread(_power_op_sync, node, vmid, vm_type, op)
    except ProxmoxError:
        raise
    except Exception as e:
        raise ProxmoxError(f"Power operation '{op}' on {node}/{vmid} failed: {e}") from e
