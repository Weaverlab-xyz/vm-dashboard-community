"""
Hyper-V service layer — Windows Remote Management (WinRM) via pywinrm.

Connects to a Windows host running Hyper-V and executes PowerShell Hyper-V
cmdlets remotely.  Works with standalone Hyper-V hosts (Windows 10/11 Pro,
Windows Server 2016–2025) and Failover Cluster nodes.

All blocking WinRM calls run in _to_thread() so the FastAPI event loop
is never blocked.
"""
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


async def _to_thread(fn, /, *args, **kwargs):
    """Run a blocking WinRM call on hyperv's OWN bounded thread pool.

    These calls used to go to the event loop's default ThreadPoolExecutor — one unbounded
    queue shared by every provider, about 8 threads in-container, with no deadline. That is
    the shape that took the whole dashboard down for 30 minutes on 2026-08-12 when two
    clouds went slow upstream: requests that needed nothing from hyperv queued behind
    calls that never returned. See services/cloud_executor.py, which is explicit that a
    bigger shared pool is not the fix, because a shared pool of any size is still a shared
    failure domain.

    Refusals become HyperVError so every existing ``except HyperVError`` — which is what turns a
    failure into a 503 or an unavailable tile — keeps working unchanged. Anything WinRM
    itself raises propagates untouched.

    ``cloud_executor`` is imported INSIDE the function on purpose: this module is loaded by
    file path under a non-dotted name in its own tests, and a top-level relative import
    fails there with "attempted relative import with no known parent package".
    """
    from . import cloud_executor
    try:
        return await cloud_executor.run("hyperv", fn, *args, **kwargs)
    except cloud_executor.CloudCallError as exc:
        raise HyperVError(str(exc)) from exc


# No _cfg here any more. This module used to read the singleton config keys directly,
# which meant there could only ever be ONE Hyper-V host. It now takes a resolved
# `Connection` (services/hypervisor_connection_service) as its first argument, and the
# router is the only layer that chooses which one.


def _require_pywinrm():
    try:
        import winrm  # noqa: F401
    except ImportError:
        raise HyperVError("pywinrm is not installed — run: pip install pywinrm")


def _session(conn):
    """Open a WinRM session to ONE Hyper-V host."""
    _require_pywinrm()
    import winrm

    host = conn.host
    if not host:
        raise HyperVError(f"Connection {conn.name!r} has no host configured")

    opts       = conn.options or {}
    port       = int(conn.port or 5985)
    username   = conn.username
    password   = conn.secret
    use_ssl    = bool(opts.get("use_ssl"))
    verify_ssl = conn.verify_ssl
    transport  = opts.get("transport") or "ntlm"

    if not username:
        raise HyperVError(f"Connection {conn.name!r} has no username configured")
    if not password:
        raise HyperVError(
            f"Connection {conn.name!r} has no password. Edit it on the Connections page.")

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

def _text(value) -> str:
    """One decode for both of the shapes pywinrm hands back.

    `Session.run_ps` does not leave `std_err` alone: whenever it is non-empty it is
    REPLACED with the return of `_clean_error_msg`, which unwraps PowerShell's CLIXML
    error envelope and yields a **str**. It stays `bytes` only while it is empty — that
    is, only when there is nothing to report. So `.decode()` on it raised
    `AttributeError: 'str' object has no attribute 'decode'` on the one path whose whole
    job is to report the PowerShell error, and the operator read a type error from this
    file instead of the message PowerShell had already written for them.

    Mirrors `runners/hypervisor/run.py::_text` — same trap, same fix, both transports.
    """
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value or "")


def _run_ps(sess, script: str) -> str:
    """Execute a PowerShell script and return stdout. Raises HyperVError on failure."""
    result = sess.run_ps(textwrap.dedent(script).strip())
    if result.status_code != 0:
        stderr = _text(result.std_err).strip()
        raise HyperVError(f"PowerShell error: {stderr or 'non-zero exit'}")
    return _text(result.std_out).strip()


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


def _list_vms_sync(conn) -> list[dict]:
    sess = _session(conn)
    output = _run_ps(sess, _LIST_VMS_PS)
    # A host with no VMs is not silence. `_LIST_VMS_PS` prints a literal '[]' for that
    # case and a JSON array in every other branch, so empty stdout — or a bare 'null' —
    # means the script never got as far as printing, and returning [] for it renders an
    # empty VM table with no error at all: indistinguishable from a host that really has
    # no VMs. The two ways it happens both leave the exit code at 0, which is how they
    # reach here past _run_ps: the Hyper-V PowerShell module not loading on the host, and
    # an account that can open a WinRM session but cannot enumerate VMs.
    #
    # Less costly than the same mistake on the agent path, where a zero-VM page pruned
    # the cache and still recorded the connection as synced — the `enumerated` envelope in
    # runners/hypervisor/run.py::_PS_LIST and hypervisor_sync_service is that half. This
    # one is a live read: no cached row is destroyed and a Refresh self-corrects once the
    # cause is fixed. Same wrong answer though, so it gets the same treatment.
    if not output or output.lower() == "null":
        raise HyperVError(
            f"{conn.name!r} returned no VM data — Get-VM printed nothing. The host "
            f"answered, so this is not the connection itself: either the Hyper-V "
            f"PowerShell module is not available to it (a Windows client host needs the "
            f"Hyper-V Management Tools feature), or "
            f"{conn.username or 'the configured account'} cannot enumerate VMs — add it "
            f"to the host's local Hyper-V Administrators group."
        )
    try:
        data = json.loads(output)
    except json.JSONDecodeError as e:
        raise HyperVError(f"Failed to parse VM list: {e}\nOutput: {output[:400]}")
    if isinstance(data, dict):
        data = [data]
    return sorted([_normalise_vm(v) for v in data], key=lambda v: v["name"].lower())


# ── Power operations ──────────────────────────────────────────────────────────

# `stop` is the page's "Force Off", and the switch that makes it one is -TurnOff, not
# -Force: -Force only suppresses the confirmation prompt. Without -TurnOff this is the
# same graceful, Integration-Services-dependent shutdown as `shutdown` above, so it does
# nothing at all on a VM whose guest cannot answer — which is exactly when an operator
# reaches for it. The sibling runner (runners/hypervisor/run.py::_PS_POWER, "power_off")
# carries -TurnOff for the same reason; tests/test_hyperv_power_parity.py pins both.
_POWER_OPS_PS = {
    "start":    "$vm = Get-VM -Id '{vmid}' -EA Stop; Start-VM   -VM $vm -ErrorAction Stop",
    "shutdown": "$vm = Get-VM -Id '{vmid}' -EA Stop; Stop-VM    -VM $vm -ErrorAction Stop",
    "stop":     "$vm = Get-VM -Id '{vmid}' -EA Stop; Stop-VM    -VM $vm -TurnOff -Force -ErrorAction Stop",
    "restart":  "$vm = Get-VM -Id '{vmid}' -EA Stop; Restart-VM -VM $vm -Force -ErrorAction Stop",
    "pause":    "$vm = Get-VM -Id '{vmid}' -EA Stop; Suspend-VM -VM $vm -ErrorAction Stop",
    "resume":   "$vm = Get-VM -Id '{vmid}' -EA Stop; Resume-VM  -VM $vm -ErrorAction Stop",
    "save":     "$vm = Get-VM -Id '{vmid}' -EA Stop; Save-VM    -VM $vm -ErrorAction Stop",
}


def _power_op_sync(conn, vmid: str, name: str, op: str) -> dict:
    if not _UUID_RE.match(vmid):
        raise HyperVError(f"Invalid VMId format: {vmid}")
    if op not in _POWER_OPS_PS:
        raise HyperVError(f"Unknown operation: {op}")

    sess = _session(conn)
    script = f"$ErrorActionPreference = 'Stop'\n{_POWER_OPS_PS[op].format(vmid=vmid)}"
    logger.info("Hyper-V: %s on %s (%s)", op, name or vmid, vmid)
    _run_ps(sess, script)
    return {"vmid": vmid, "name": name, "op": op, "status": "OK"}


# ── Async public API ──────────────────────────────────────────────────────────

async def list_vms(conn) -> list[dict]:
    try:
        return await _to_thread(_list_vms_sync, conn)
    except HyperVError:
        raise
    except Exception as e:
        raise HyperVError(f"Failed to list VMs: {e}") from e


async def power_op(conn, vmid: str, name: str, op: str) -> dict:
    valid = set(_POWER_OPS_PS)
    if op not in valid:
        raise HyperVError(
            f"Invalid operation '{op}'. Must be one of: {', '.join(sorted(valid))}"
        )
    try:
        return await _to_thread(_power_op_sync, conn, vmid, name, op)
    except HyperVError:
        raise
    except Exception as e:
        raise HyperVError(f"Power operation '{op}' on {name or vmid} failed: {e}") from e
