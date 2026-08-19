"""One-shot hypervisor operation, for the two transports the agent cannot carry itself.

Reads its whole instruction from the environment, does one thing, prints one JSON object
on stdout and exits. Nothing is read from argv, so there is no place for a shell to
expand anything; nothing is read from a file, so there is nothing to point at.

Two products live here and only two:

* ``hyperv`` — WinRM is SOAP with NTLM/Negotiate, and hand-rolling NTLM in the agent
  would be both a large amount of security-critical code and a dependency the agent
  image deliberately does not carry.
* ``esxi``   — a bare ESXi host serves the SOAP API only; the vSphere Automation REST
  API the agent uses is vCenter-only.

Everything else (vCenter, Proxmox, Prism, XCP-ng) is handled by the agent directly and
must not be added here — a second implementation of the same thing is a second thing to
keep correct.

The credential arrives in the environment rather than argv, so it never appears in
``ps`` output on the host. It is read once at start and the variable is cleared, which
does not make it unreadable (``/proc/self/environ`` keeps the original) but does keep it
out of any later traceback or subprocess this file might grow.
"""
import json
import os
import ssl
import sys

VALID_KINDS = ("hyperv", "esxi")
# This list is the dashboard's write-verb allowlist minus two, and neither absence is a
# to-do:
#
#   snapshot  needs per-kind APIs this runner has no path to.
#   reboot    Hyper-V has no graceful-reboot cmdlet at all — Restart-VM is documented as
#             a "hard" restart, "like powering the computer down, then back up again",
#             which is already what `power_reset` runs. ESXi *can* reboot a guest
#             gracefully, and does: that is `restart` below, RebootGuest(). So there is
#             no verb left over for either kind to want.
#
# The agent refuses `reboot` for Hyper-V before a job reaches this file, so the operator
# reads why rather than "unknown verb", which would look like a version mismatch.
VALID_VERBS = ("inventory_sync", "power_on", "power_off", "power_reset", "restart",
               "shutdown")


def _fail(message: str) -> None:
    """One shape for every failure. The agent parses stdout either way, so a refusal is
    reported rather than inferred from an exit code and an empty pipe."""
    print(json.dumps({"ok": False, "error": str(message)[:800]}))
    sys.exit(1)


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default) or default


def _text(value) -> str:
    """Decode a WinRM stream to str, whichever type pywinrm handed back.

    `Session.run_ps` REPLACES `std_err` with a cleaned *str* when it is non-empty and
    leaves it as *bytes* when it is not, so a bare `.decode()` raised AttributeError on
    exactly the path meant to report the error — the runner died with a traceback and
    the agent reported "the sibling runner produced unparseable output" instead of the
    message PowerShell had already written. `std_out` stays bytes; this covers both so
    neither has to be remembered at the call site.
    """
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value or "")


def _take_secret() -> str:
    value = os.environ.get("HV_PASSWORD", "")
    os.environ.pop("HV_PASSWORD", None)
    return value


# ── Hyper-V over WinRM ────────────────────────────────────────────────────────

# A bare `Get-VM | ConvertTo-Json` cannot say "this host has no VMs": PowerShell pipes
# nothing into ConvertTo-Json for an empty result, so stdout comes back EMPTY — which is
# also what a host whose Hyper-V module never loaded produces, and what an account that
# cannot enumerate VMs produces. All three read here as a successful scan of zero VMs,
# and on the dashboard a zero-VM pass prunes the connection's entire cached inventory
# and stamps it as synced. A Hyper-V page went from a full list to "No VMs" with no
# error anywhere and a good cache destroyed.
#
# So the script returns an ENVELOPE. An empty host is `{"enumerated":true,"count":0,
# "vms":[]}` — a positive statement the dashboard may act on — and empty stdout is a
# failure with a message rather than a zero-VM success.
#
# `Import-Module Hyper-V` is explicit because an implicit load that fails is the
# likeliest way to reach the ambiguity in the first place, and $ErrorActionPreference
# makes that a non-zero exit with a stderr line rather than a warning and a blank pipe.
# `Hyper-V\Get-VM` is module-qualified for a second reason: Az.Compute and VMware
# PowerCLI each export a `Get-VM` of their own, and on a host where one of those was
# imported later an unqualified call resolves to it.
#
# -Depth 4, not 3: the envelope adds a level above the VM objects and their properties.
_PS_LIST = (
    "$ErrorActionPreference = 'Stop'; "
    "Import-Module Hyper-V; "
    "$vms = @(Hyper-V\\Get-VM | Select-Object -Property "
    "Id,Name,State,ProcessorCount,@{N='MemoryMB';E={[int]($_.MemoryAssigned/1MB)}}); "
    "ConvertTo-Json -Compress -Depth 4 -InputObject "
    "@{enumerated=$true; count=$vms.Count; vms=$vms}"
)

# The same enumeration plus each guest's addresses and OS name — what Config Management
# needs, because a playbook has to reach the guest and `Get-VM` reports nothing about it.
#
# Kept as a SECOND script rather than folded into the one above, and that is deliberate on
# two counts. It costs more on the host (a KVP read per VM), so it runs only for a
# connection whose operator asked for it. And both fields depend on Integration Services
# being present in the guest: a VM without them yields nothing here, while the plain
# enumeration above still works — so the cheap path can never be made to fail by the
# expensive one.
#
# Addresses come from the network adapters. `IPAddresses` is populated by the guest
# integration components, so a powered-off VM and a VM without the services both report an
# empty list; neither is an error and neither is guessed at.
#
# The OS name comes from the KVP exchange (`GuestIntrinsicExchangeItems`), which is the only
# thing on a Hyper-V host that knows it. Each item is an XML fragment, so the name is pulled
# by XPath rather than by string matching. Wrapped in try/catch because a host with WMI
# hardened, or a VM mid-migration, must degrade to "no OS reported" rather than fail the
# whole sync — which would empty the dashboard's cache for every VM on the host.
_PS_LIST_GUEST = (
    "$ErrorActionPreference = 'Stop'; "
    "Import-Module Hyper-V; "
    "$vms = @(Hyper-V\\Get-VM | ForEach-Object { "
    "  $vm = $_; "
    "  $ips = @(); "
    "  try { $ips = @($vm | Hyper-V\\Get-VMNetworkAdapter | "
    "        Select-Object -ExpandProperty IPAddresses | Where-Object { $_ }) } catch { } "
    "  $os = ''; "
    "  try { "
    "    $kvp = Get-WmiObject -Namespace root\\virtualization\\v2 "
    "           -Class Msvm_KvpExchangeComponent "
    "           -Filter \"SystemName='$($vm.Id)'\"; "
    "    foreach ($item in @($kvp.GuestIntrinsicExchangeItems)) { "
    "      $x = [xml]$item; "
    "      $n = $x.SelectSingleNode(\"/INSTANCE/PROPERTY[@NAME='Name']/VALUE\").'#text'; "
    "      if ($n -eq 'OSName') { "
    "        $os = $x.SelectSingleNode(\"/INSTANCE/PROPERTY[@NAME='Data']/VALUE\").'#text' } "
    "    } "
    "  } catch { } "
    "  [pscustomobject]@{ Id=$vm.Id; Name=$vm.Name; State=$vm.State; "
    "    ProcessorCount=$vm.ProcessorCount; "
    "    MemoryMB=[int]($vm.MemoryAssigned/1MB); "
    "    IPAddresses=$ips; OSName=$os } "
    "}); "
    "ConvertTo-Json -Compress -Depth 4 -InputObject "
    "@{enumerated=$true; count=$vms.Count; vms=$vms}"
)

# A closed map, not a format string: the verb never reaches PowerShell as text.
#
# The switches are the whole meaning here, and the two on Stop-VM do NOT mean the same
# kind of thing — which is how `power_off` once shipped as a graceful shutdown:
#
#   -TurnOff  cuts the virtual power. This is the hard stop, and the only one.
#   -Force    on Stop-VM means "shut down regardless of any unsaved application data",
#             giving the guest five minutes before it is forced. It is a DATA promise,
#             not a prompt setting, so `shutdown` below must not carry it.
#   -Force    on Restart-VM really is only the confirmation-prompt suppressor — which a
#             non-interactive WinRM session could not answer — and Restart-VM is a hard
#             restart with or without it.
#
# So `shutdown` is bare Stop-VM: Microsoft's own example for that exact call is "shuts
# down … through the guest operating system". It matches, switch for switch, the direct
# WinRM path in hyperv_service._POWER_OPS_PS['shutdown'] — one button, one behaviour,
# either route, which tests/test_hyperv_power_parity.py pins.
#
# Every script resolves the VM object first and passes it with -VM, which is not a style
# choice: -Id exists ONLY on Get-VM. Start-VM, Stop-VM, Restart-VM, Suspend-VM, Resume-VM
# and Save-VM each take -Name or -VM and nothing else, so `Start-VM -Id '<guid>'` — which
# is what every verb here shipped as — could not bind and failed on the host with
# "NamedParameterNotFound ... Microsoft.HyperV.PowerShell.Commands.StartVM". Addressing
# the VM by -Name instead would be a second bug: names are not unique across a host and,
# unlike the id, are not validated against a closed charset before they meet the shell.
_PS_POWER = {
    "power_on":    "$vm = Get-VM -Id '{vm}' -EA Stop; Start-VM   -VM $vm -ErrorAction Stop",
    "power_off":   "$vm = Get-VM -Id '{vm}' -EA Stop; Stop-VM    -VM $vm -TurnOff -Force -ErrorAction Stop",
    "power_reset": "$vm = Get-VM -Id '{vm}' -EA Stop; Restart-VM -VM $vm -Force -ErrorAction Stop",
    "restart":     "$vm = Get-VM -Id '{vm}' -EA Stop; Restart-VM -VM $vm -ErrorAction Stop",
    "shutdown":    "$vm = Get-VM -Id '{vm}' -EA Stop; Stop-VM    -VM $vm -ErrorAction Stop",
}

_STATE = {0: "Unknown", 2: "Running", 3: "Off", 4: "Stopping", 6: "Saved",
          9: "Paused", 10: "Starting", 11: "Reset", 12: "Saving", 13: "Pausing",
          14: "Resuming"}


def _hyperv(verb: str, host: str, port: int, user: str, secret: str,
            verify: bool, target: str) -> dict:
    import winrm

    scheme = "https" if _env("HV_USE_SSL") == "1" else "http"
    session = winrm.Session(
        target=f"{scheme}://{host}:{port}/wsman",
        auth=(user, secret),
        transport=_env("HV_TRANSPORT", "ntlm"),
        server_cert_validation="validate" if verify else "ignore",
        read_timeout_sec=70, operation_timeout_sec=60,
    )

    if verb == "inventory_sync":
        # Guest details are opt-in per connection: they cost a KVP read per VM, and only a
        # connection whose VMs are Config-Management targets needs them.
        want_guest = _env("HV_GUEST_DETAILS") == "1"
        result = session.run_ps(_PS_LIST_GUEST if want_guest else _PS_LIST)
        if result.status_code != 0:
            _fail(f"Get-VM failed: {_text(result.std_err)[:400]}")
        raw = _text(result.std_out).strip()
        if not raw:
            _fail("Get-VM produced no output at all. The script prints a JSON envelope "
                  "even for a host with no VMs, so this is a session that could not "
                  "read the host rather than a host with nothing on it — check that "
                  "the Hyper-V PowerShell module is present and that this account may "
                  "enumerate VMs.")
        try:
            doc = json.loads(raw)
        except ValueError:
            _fail(f"Get-VM returned output that is not JSON: {raw[:300]}")
        if not isinstance(doc, dict) or "vms" not in doc:
            _fail(f"Get-VM returned an unexpected shape: {raw[:300]}")
        rows = doc.get("vms") or []
        if isinstance(rows, dict):          # a single VM is not wrapped in a list
            rows = [rows]
        vms = []
        for vm in rows:
            state = vm.get("State")
            row = {
                "vm_id": str(vm.get("Id") or ""),
                "name": vm.get("Name") or "",
                "power_state": _STATE.get(state, str(state)),
                "vcpus": vm.get("ProcessorCount"),
                "mem_mib": vm.get("MemoryMB"),
                "vm_type": "vm",
            }
            # Only ever ADD these keys, never a blank one. The dashboard's sanitiser drops
            # absent keys and leaves the cached value alone, so an older agent — or a guest
            # with no Integration Services — keeps whatever address was last known instead of
            # having it overwritten with nothing.
            ips = vm.get("IPAddresses")
            if isinstance(ips, str):        # a single address is not wrapped in a list
                ips = [ips]
            ips = [str(i).strip() for i in (ips or []) if str(i).strip()]
            if ips:
                row["ip_addresses"] = ips
            if vm.get("OSName"):
                row["guest_os"] = str(vm["OSName"])[:64]
            vms.append(row)
        # Hyper-V has no cursor: Get-VM returns the whole host in one call, and a host
        # with enough VMs to need paging is not a Hyper-V host.
        #
        # `enumerated` is the envelope's whole point: the host answered, and this is its
        # answer — so an empty `vms` really does mean an empty host and the dashboard may
        # prune its cache to nothing. Every way of failing to read the host exits above.
        return {"ok": True, "vms": vms, "next_cursor": "", "complete": True,
                "scanned": len(vms), "enumerated": True}

    script = _PS_POWER.get(verb)
    if script is None:
        _fail(f"{verb!r} is not available for Hyper-V")
    # The id is validated by the agent against a closed charset before it gets here;
    # validate again rather than trust it, because this is where it meets a shell.
    if not target or not all(c.isalnum() or c in "-:_." for c in target):
        _fail("target_id is missing or not a valid Hyper-V VM id")
    result = session.run_ps(script.format(vm=target))
    if result.status_code != 0:
        _fail(f"{verb} failed: {_text(result.std_err)[:400]}")
    return {"ok": True, "verb": verb, "target_id": target}


# ── bare ESXi over SOAP ───────────────────────────────────────────────────────

def _esxi(verb: str, host: str, port: int, user: str, secret: str,
          verify: bool, target: str) -> dict:
    from pyVim.connect import Disconnect, SmartConnect
    from pyVmomi import vim

    if verify:
        context = ssl.create_default_context()
    else:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE

    si = SmartConnect(host=host, user=user, pwd=secret, port=port, sslContext=context)
    try:
        content = si.RetrieveContent()
        view = content.viewManager.CreateContainerView(
            content.rootFolder, [vim.VirtualMachine], True)
        try:
            machines = list(view.view)
        finally:
            view.Destroy()

        if verb == "inventory_sync":
            vms = []
            for vm in machines:
                summary = vm.summary
                config = summary.config
                vms.append({
                    "vm_id": str(vm._moId),
                    "name": config.name or "",
                    "power_state": str(summary.runtime.powerState or ""),
                    "vcpus": config.numCpu,
                    "mem_mib": config.memorySizeMB,
                    "ip_addresses": [summary.guest.ipAddress] if (
                        summary.guest and summary.guest.ipAddress) else [],
                    "vm_type": "vm",
                })
            # A standalone ESXi host tops out in the low hundreds of VMs, so one page.
            # `enumerated` is unconditional here because there is no quiet empty to
            # guard against: the container view either enumerated the host or pyVmomi
            # raised on the way.
            return {"ok": True, "vms": vms, "next_cursor": "", "complete": True,
                    "scanned": len(vms), "enumerated": True}

        match = next((vm for vm in machines if vm._moId == target), None)
        if match is None:
            _fail(f"no VM with moref {target!r} on this host")
        if verb == "power_on":
            match.PowerOnVM_Task()
        elif verb == "power_off":
            match.PowerOffVM_Task()
        elif verb == "power_reset":
            match.ResetVM_Task()
        elif verb == "restart":
            match.RebootGuest()
        elif verb == "shutdown":
            match.ShutdownGuest()
        # The two above are the graceful pair, and the only verbs here that need the
        # guest agent: both go through VMware Tools and both fail if it is not running.
        # `shutdown` is reachable on this kind and not only on vCenter — the dashboard
        # has no `esxi` connection kind, so a vSphere page's Shutdown button becomes a
        # `vsphere` job that an agent may serve from an `esxi` connection.
        else:
            _fail(f"{verb!r} is not available for ESXi")
        # Deliberately not waiting on the task: the agent's job is "issued", and a
        # power operation that takes minutes should not hold a container open.
        return {"ok": True, "verb": verb, "target_id": target}
    finally:
        try:
            Disconnect(si)
        except Exception:  # noqa: BLE001
            pass


def main() -> int:
    kind = _env("HV_KIND")
    verb = _env("HV_VERB")
    if kind not in VALID_KINDS:
        _fail(f"unknown kind {kind!r}")
    if verb not in VALID_VERBS:
        _fail(f"unknown verb {verb!r}")

    host = _env("HV_HOST")
    if not host:
        _fail("HV_HOST is empty")
    try:
        port = int(_env("HV_PORT", "0")) or (5985 if kind == "hyperv" else 443)
    except ValueError:
        _fail("HV_PORT is not a number")
    secret = _take_secret()
    if not secret:
        _fail("no credential was supplied")

    handler = _hyperv if kind == "hyperv" else _esxi
    try:
        result = handler(verb, host, port, _env("HV_USERNAME"), secret,
                         _env("HV_VERIFY_SSL") == "1", _env("HV_TARGET_ID"))
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        # Never let a library traceback reach stdout: it can carry the credential in a
        # frame local, and stdout is what the agent parses and streams onward.
        _fail(f"{type(exc).__name__}: {str(exc)[:400]}")
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
