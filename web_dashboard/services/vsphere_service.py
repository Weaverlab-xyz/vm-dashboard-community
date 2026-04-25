"""
VMware vSphere service layer — vSphere Web Services API via pyVmomi.

Works with both standalone ESXi hosts and vCenter Server.  Connect to either
and the same API is available — vCenter simply exposes a wider inventory.

All blocking SDK calls run in asyncio.to_thread() so the FastAPI event loop
is never blocked.  Each public function opens a fresh session and always
disconnects in a finally block to avoid session leaks.
"""
import asyncio
import logging
import ssl
import time
from typing import Optional

logger = logging.getLogger(__name__)


class VSphereError(Exception):
    pass


def _cfg(key: str) -> str:
    from . import config_service
    return config_service.get(key) or ""


def _cfg_bool(key: str, default: bool = False) -> bool:
    from . import config_service
    return config_service.get_bool(key, default)


def _require_pyvmomi():
    try:
        from pyVim.connect import SmartConnect  # noqa: F401
        from pyVmomi import vim  # noqa: F401
    except ImportError:
        raise VSphereError(
            "pyVmomi is not installed — run: pip install pyVmomi"
        )


def _connect():
    """
    Open a vSphere service instance.  Caller is responsible for calling
    Disconnect() — use _session() context manager instead.
    """
    _require_pyvmomi()
    from pyVim.connect import SmartConnect

    host = _cfg("vsphere_host")
    if not host:
        raise VSphereError("VSPHERE_HOST is not configured")

    port     = int(_cfg("vsphere_port") or "443")
    user     = _cfg("vsphere_user") or "administrator@vsphere.local"
    password = _cfg("vsphere_password")
    if not password:
        raise VSphereError("VSPHERE_PASSWORD is not configured")

    verify_ssl = _cfg_bool("vsphere_verify_ssl", False)
    if verify_ssl:
        ctx = ssl.create_default_context()
    else:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode    = ssl.CERT_NONE

    return SmartConnect(host=host, user=user, pwd=password, port=port, sslContext=ctx)


class _session:
    """Context manager: opens a vSphere session and always disconnects."""
    def __enter__(self):
        from pyVim.connect import Disconnect
        self._si = _connect()
        self._disconnect = Disconnect
        return self._si

    def __exit__(self, *_):
        try:
            self._disconnect(self._si)
        except Exception:
            pass


# ── Inventory helpers ─────────────────────────────────────────────────────────

def _datacenter_of(obj) -> str:
    """Walk the parent chain to find the containing datacenter name."""
    from pyVmomi import vim
    cur = obj
    while cur is not None:
        if isinstance(cur, vim.Datacenter):
            return cur.name
        try:
            cur = cur.parent
        except Exception:
            break
    return ""


def _normalise_vm(vm) -> dict:
    """Convert a pyVmomi VirtualMachine managed object to a plain dict."""
    from pyVmomi import vim

    cfg     = vm.config if vm.config else None
    runtime = vm.runtime
    guest   = vm.guest

    power_state = runtime.powerState if runtime else "unknown"
    is_running  = power_state == vim.VirtualMachine.PowerState.poweredOn

    # IP addresses from guest agent
    ips: list[str] = []
    if guest and guest.net:
        for nic in guest.net:
            for addr in (nic.ipAddress or []):
                # skip IPv6 and loopback
                if ":" not in addr and not addr.startswith("127."):
                    ips.append(addr)
    elif guest and guest.ipAddress:
        ips = [guest.ipAddress]

    host_name = ""
    try:
        if runtime and runtime.host:
            host_name = runtime.host.name
    except Exception:
        pass

    dc = _datacenter_of(vm)

    return {
        "moref":           vm._moId,
        "name":            cfg.name if cfg else vm.name,
        "power_state":     power_state,
        "is_running":      is_running,
        "host":            host_name,
        "datacenter":      dc,
        "cpu_count":       cfg.hardware.numCPU if cfg and cfg.hardware else 0,
        "mem_mb":          cfg.hardware.memoryMB if cfg and cfg.hardware else 0,
        "guest_id":        cfg.guestId if cfg else "",
        "guest_full_name": cfg.guestFullName if cfg else "",
        "ip_addresses":    ips,
        "tools_status":    guest.toolsStatus if guest else "toolsNotInstalled",
        "template":        bool(cfg.template) if cfg else False,
        "annotation":      cfg.annotation if cfg else "",
    }


def _all_vms_sync(datacenter_filter: str = "") -> list[dict]:
    """Return all non-template VMs, optionally filtered to one datacenter."""
    from pyVmomi import vim

    with _session() as si:
        content = si.RetrieveContent()
        root    = content.rootFolder

        # Narrow to a specific datacenter if requested
        if datacenter_filter:
            dcs = [
                dc for dc in content.viewManager.CreateContainerView(
                    root, [vim.Datacenter], False
                ).view
                if dc.name == datacenter_filter
            ]
            root = dcs[0] if dcs else root

        container = content.viewManager.CreateContainerView(root, [vim.VirtualMachine], True)
        try:
            vms = [_normalise_vm(vm) for vm in container.view if not (vm.config and vm.config.template)]
        finally:
            container.Destroy()

    return sorted(vms, key=lambda v: v["name"].lower())


def _list_datacenters_sync() -> list[str]:
    from pyVmomi import vim
    with _session() as si:
        content   = si.RetrieveContent()
        container = content.viewManager.CreateContainerView(
            content.rootFolder, [vim.Datacenter], False
        )
        try:
            names = [dc.name for dc in container.view]
        finally:
            container.Destroy()
    return sorted(names)


def _list_hosts_sync() -> list[dict]:
    from pyVmomi import vim
    with _session() as si:
        content   = si.RetrieveContent()
        container = content.viewManager.CreateContainerView(
            content.rootFolder, [vim.HostSystem], True
        )
        try:
            hosts = []
            for h in container.view:
                runtime = h.runtime
                summary = h.summary
                hosts.append({
                    "name":       h.name,
                    "status":     runtime.connectionState if runtime else "unknown",
                    "in_maintenance": bool(runtime.inMaintenanceMode) if runtime else False,
                    "cpu_mhz":    summary.hardware.cpuMhz if summary and summary.hardware else 0,
                    "cpu_cores":  summary.hardware.numCpuCores if summary and summary.hardware else 0,
                    "mem_total_mb": summary.hardware.memorySize // 1048576 if summary and summary.hardware else 0,
                    "mem_used_mb":  (summary.quickStats.overallMemoryUsage or 0) if summary and summary.quickStats else 0,
                    "datacenter": _datacenter_of(h),
                })
        finally:
            container.Destroy()
    return sorted(hosts, key=lambda h: h["name"])


def _get_vm_sync(moref: str) -> dict:
    from pyVmomi import vim
    with _session() as si:
        content   = si.RetrieveContent()
        container = content.viewManager.CreateContainerView(
            content.rootFolder, [vim.VirtualMachine], True
        )
        try:
            for vm in container.view:
                if vm._moId == moref:
                    return _normalise_vm(vm)
        finally:
            container.Destroy()
    raise VSphereError(f"VM {moref} not found")


# ── Power operations ──────────────────────────────────────────────────────────

def _wait_for_task(task, timeout: int = 300):
    from pyVmomi import vim
    deadline = time.monotonic() + timeout
    while task.info.state not in (
        vim.TaskInfo.State.success, vim.TaskInfo.State.error
    ):
        if time.monotonic() > deadline:
            raise VSphereError(f"Task timed out after {timeout}s")
        time.sleep(2)
    if task.info.state == vim.TaskInfo.State.error:
        msg = task.info.error.msg if task.info.error else "unknown error"
        raise VSphereError(f"Task failed: {msg}")


def _power_op_sync(moref: str, op: str) -> dict:
    """
    Execute a power operation on a VM identified by its managed object reference.
    op: start | shutdown | stop | reset | suspend
    """
    from pyVmomi import vim

    with _session() as si:
        content   = si.RetrieveContent()
        container = content.viewManager.CreateContainerView(
            content.rootFolder, [vim.VirtualMachine], True
        )
        vm = None
        try:
            for v in container.view:
                if v._moId == moref:
                    vm = v
                    break
        finally:
            container.Destroy()

        if vm is None:
            raise VSphereError(f"VM {moref} not found")

        name = vm.config.name if vm.config else moref
        logger.info("vSphere: %s on %s (%s)", op, name, moref)

        if op == "start":
            task = vm.PowerOnVM_Task()
            _wait_for_task(task)
        elif op == "shutdown":
            # Graceful — requires VMware Tools; no task object returned
            vm.ShutdownGuest()
            # Poll until powered off (max 3 min)
            for _ in range(90):
                time.sleep(2)
                if vm.runtime.powerState != vim.VirtualMachine.PowerState.poweredOn:
                    break
        elif op == "stop":
            task = vm.PowerOffVM_Task()
            _wait_for_task(task)
        elif op == "reset":
            task = vm.ResetVM_Task()
            _wait_for_task(task)
        elif op == "suspend":
            task = vm.SuspendVM_Task()
            _wait_for_task(task)
        else:
            raise VSphereError(f"Unknown operation: {op}")

    return {"moref": moref, "op": op, "status": "OK"}


# ── Async public API ──────────────────────────────────────────────────────────

async def list_datacenters() -> list[str]:
    try:
        return await asyncio.to_thread(_list_datacenters_sync)
    except VSphereError:
        raise
    except Exception as e:
        raise VSphereError(f"Failed to list datacenters: {e}") from e


async def list_hosts() -> list[dict]:
    try:
        return await asyncio.to_thread(_list_hosts_sync)
    except VSphereError:
        raise
    except Exception as e:
        raise VSphereError(f"Failed to list hosts: {e}") from e


async def list_vms(datacenter: str = "") -> list[dict]:
    try:
        return await asyncio.to_thread(_all_vms_sync, datacenter)
    except VSphereError:
        raise
    except Exception as e:
        raise VSphereError(f"Failed to list VMs: {e}") from e


async def get_vm(moref: str) -> dict:
    try:
        return await asyncio.to_thread(_get_vm_sync, moref)
    except VSphereError:
        raise
    except Exception as e:
        raise VSphereError(f"Failed to get VM detail: {e}") from e


async def power_op(moref: str, op: str) -> dict:
    """
    Power operation: start | shutdown | stop | reset | suspend
    'stop' is force power-off; prefer 'shutdown' for a graceful guest OS halt.
    """
    valid = {"start", "shutdown", "stop", "reset", "suspend"}
    if op not in valid:
        raise VSphereError(f"Invalid operation '{op}'. Must be one of: {', '.join(sorted(valid))}")
    try:
        return await asyncio.to_thread(_power_op_sync, moref, op)
    except VSphereError:
        raise
    except Exception as e:
        raise VSphereError(f"Power operation '{op}' on {moref} failed: {e}") from e
