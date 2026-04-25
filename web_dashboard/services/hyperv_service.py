"""
Hyper-V service layer — Windows Remote Management (WinRM) via pywinrm.

Connects to a Windows host running Hyper-V and executes PowerShell Hyper-V
cmdlets remotely.  Works with standalone Hyper-V hosts (Windows 10/11 Pro,
Windows Server 2016–2025) and Failover Cluster nodes.

All blocking WinRM calls run in asyncio.to_thread() so the FastAPI event loop
is never blocked.
"""
import asyncio
import json
import logging
import re
import textwrap

logger = logging.getLogger(__name__)

_UUID_RE = re.compile(
    r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
)


class HyperVError(Exception):
    pass


def _cfg(key: str) -> str:
    from . import config_service
    return config_service.get(key) or ""


def _cfg_bool(key: str, default: bool = False) -> bool:
    from . import config_service
    return config_service.get_bool(key, default)


def _require_pywinrm():
    try:
        import winrm  # noqa: F401
    except ImportError:
        raise HyperVError("pywinrm is not installed — run: pip install pywinrm")


def _session():
    """Open a WinRM session to the configured Hyper-V host."""
    _require_pywinrm()
    import winrm

    host = _cfg("hyperv_host")
    if not host:
        raise HyperVError("HYPERV_HOST is not configured")

    port      = int(_cfg("hyperv_port") or "5985")
    username  = _cfg("hyperv_username")
    password  = _cfg("hyperv_password")
    use_ssl   = _cfg_bool("hyperv_use_ssl", False)
    verify_ssl = _cfg_bool("hyperv_verify_ssl", False)
    transport = _cfg("hyperv_transport") or "ntlm"

    if not username:
        raise HyperVError("HYPERV_USERNAME is not configured")
    if not password:
        raise HyperVError("HYPERV_PASSWORD is not configured")

    scheme = "https" if use_ssl else "http"
    target = f"{scheme}://{host}:{port}/wsman"

    return winrm.Session(
        target=target,
        auth=(username, password),
        transport=transport,
        server_cert_validation="validate" if verify_ssl else "ignore",
        read_timeout_sec=60,
        operation_timeout_sec=60,
    )


# ── State constants ───────────────────────────────────────────────────────────

_STATE_LABELS = {
    0:  "Unknown",
    2:  "Running",
    3:  "Off",
    4:  "Stopping",
    6:  "Saved",
    9:  "Paused",
    10: "Starting",
    11: "Reset",
    12: "Saving",
    13: "Pausing",
    14: "Resuming",
}

_RUNNING_STATES   = {2, 10, 14}   # Running, Starting, Resuming
_STOPPABLE_STATES = {2, 10}        # can be force-stopped / restarted


# ── WinRM helpers ─────────────────────────────────────────────────────────────

def _run_ps(sess, script: str) -> str:
    """Execute a PowerShell script and return stdout. Raises HyperVError on failure."""
    result = sess.run_ps(textwrap.dedent(script).strip())
    if result.status_code != 0:
        stderr = result.std_err.decode("utf-8", errors="replace").strip()
        raise HyperVError(f"PowerShell error: {stderr or 'non-zero exit'}")
    return result.std_out.decode("utf-8", errors="replace").strip()


# ── List VMs ──────────────────────────────────────────────────────────────────

# Targets PowerShell 5.1 (Windows Server 2016+). Uses @() to normalise empty
# results and checks Count to work around the PS5.1 ConvertTo-Json quirk where
# a single-element array serialises as an object rather than a one-item array.
_LIST_VMS_PS = r"""
$ErrorActionPreference = 'Stop'
$vms = @(Get-VM | ForEach-Object {
    $vm = $_
    $ips = @()
    try {
        $ips = @((Get-VMNetworkAdapter -VM $vm).IPAddresses |
                  Where-Object { $_ -and ($_ -notmatch ':') -and ($_ -ne '127.0.0.1') })
    } catch {}
    [PSCustomObject]@{
        VMId                     = $vm.VMId.ToString()
        Name                     = $vm.Name
        State                    = [int]$vm.State
        CPUUsage                 = $vm.CPUUsage
        MemoryAssignedMB         = [math]::Round($vm.MemoryAssigned / 1MB)
        MemoryStartupMB          = [math]::Round($vm.MemoryStartup  / 1MB)
        ProcessorCount           = $vm.ProcessorCount
        Generation               = $vm.Generation
        UptimeSecs               = [math]::Round($vm.Uptime.TotalSeconds)
        IPAddresses              = $ips
        IntegrationServicesState = [string]$vm.IntegrationServicesState
        Path                     = $vm.Path
    }
})
if ($vms.Count -eq 0) {
    '[]'
} elseif ($vms.Count -eq 1) {
    "[" + ($vms[0] | ConvertTo-Json -Depth 2 -Compress) + "]"
} else {
    $vms | ConvertTo-Json -Depth 2 -Compress
}
"""


def _normalise_vm(raw: dict) -> dict:
    state_int = int(raw.get("State", 0))
    ips = raw.get("IPAddresses") or []
    if isinstance(ips, str):
        ips = [ips] if ips else []
    is_state = raw.get("IntegrationServicesState") or ""

    return {
        "vmid":                      raw.get("VMId", ""),
        "name":                      raw.get("Name", ""),
        "state":                     state_int,
        "state_label":               _STATE_LABELS.get(state_int, "Unknown"),
        "is_running":                state_int in _RUNNING_STATES,
        "cpu_usage":                 raw.get("CPUUsage", 0),
        "mem_assigned_mb":           raw.get("MemoryAssignedMB", 0),
        "mem_startup_mb":            raw.get("MemoryStartupMB", 0),
        "processor_count":           raw.get("ProcessorCount", 0),
        "generation":                raw.get("Generation", 1),
        "uptime_secs":               raw.get("UptimeSecs", 0),
        "ip_addresses":              ips,
        "integration_services_state": is_state,
        "path":                      raw.get("Path", ""),
    }


def _list_vms_sync() -> list[dict]:
    sess = _session()
    output = _run_ps(sess, _LIST_VMS_PS)
    if not output or output.lower() == "null":
        return []
    try:
        data = json.loads(output)
    except json.JSONDecodeError as e:
        raise HyperVError(f"Failed to parse VM list: {e}\nOutput: {output[:400]}")
    if isinstance(data, dict):
        data = [data]
    return sorted([_normalise_vm(v) for v in data], key=lambda v: v["name"].lower())


# ── Power operations ──────────────────────────────────────────────────────────

_POWER_OPS_PS = {
    "start":    "$vm = Get-VM -Id '{vmid}' -EA Stop; Start-VM   -VM $vm -ErrorAction Stop",
    "shutdown": "$vm = Get-VM -Id '{vmid}' -EA Stop; Stop-VM    -VM $vm -ErrorAction Stop",
    "stop":     "$vm = Get-VM -Id '{vmid}' -EA Stop; Stop-VM    -VM $vm -Force -ErrorAction Stop",
    "restart":  "$vm = Get-VM -Id '{vmid}' -EA Stop; Restart-VM -VM $vm -Force -ErrorAction Stop",
    "pause":    "$vm = Get-VM -Id '{vmid}' -EA Stop; Suspend-VM -VM $vm -ErrorAction Stop",
    "resume":   "$vm = Get-VM -Id '{vmid}' -EA Stop; Resume-VM  -VM $vm -ErrorAction Stop",
    "save":     "$vm = Get-VM -Id '{vmid}' -EA Stop; Save-VM    -VM $vm -ErrorAction Stop",
}


def _power_op_sync(vmid: str, name: str, op: str) -> dict:
    if not _UUID_RE.match(vmid):
        raise HyperVError(f"Invalid VMId format: {vmid}")
    if op not in _POWER_OPS_PS:
        raise HyperVError(f"Unknown operation: {op}")

    sess = _session()
    script = f"$ErrorActionPreference = 'Stop'\n{_POWER_OPS_PS[op].format(vmid=vmid)}"
    logger.info("Hyper-V: %s on %s (%s)", op, name or vmid, vmid)
    _run_ps(sess, script)
    return {"vmid": vmid, "name": name, "op": op, "status": "OK"}


# ── Async public API ──────────────────────────────────────────────────────────

async def list_vms() -> list[dict]:
    try:
        return await asyncio.to_thread(_list_vms_sync)
    except HyperVError:
        raise
    except Exception as e:
        raise HyperVError(f"Failed to list VMs: {e}") from e


async def power_op(vmid: str, name: str, op: str) -> dict:
    valid = set(_POWER_OPS_PS)
    if op not in valid:
        raise HyperVError(
            f"Invalid operation '{op}'. Must be one of: {', '.join(sorted(valid))}"
        )
    try:
        return await asyncio.to_thread(_power_op_sync, vmid, name, op)
    except HyperVError:
        raise
    except Exception as e:
        raise HyperVError(f"Power operation '{op}' on {name or vmid} failed: {e}") from e
