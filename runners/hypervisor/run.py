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


def _take_secret() -> str:
    value = os.environ.get("HV_PASSWORD", "")
    os.environ.pop("HV_PASSWORD", None)
    return value


# ── Hyper-V over WinRM ────────────────────────────────────────────────────────

_PS_LIST = (
    "Get-VM | Select-Object -Property "
    "Id,Name,State,ProcessorCount,@{N='MemoryMB';E={[int]($_.MemoryAssigned/1MB)}} "
    "| ConvertTo-Json -Compress -Depth 3"
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
        result = session.run_ps(_PS_LIST)
        if result.status_code != 0:
            _fail(f"Get-VM failed: {result.std_err.decode('utf-8', 'replace')[:400]}")
        raw = (result.std_out or b"").decode("utf-8", "replace").strip()
        rows = json.loads(raw) if raw else []
        if isinstance(rows, dict):          # a single VM is not wrapped in a list
            rows = [rows]
        vms = []
        for vm in rows:
            state = vm.get("State")
            vms.append({
                "vm_id": str(vm.get("Id") or ""),
                "name": vm.get("Name") or "",
                "power_state": _STATE.get(state, str(state)),
                "vcpus": vm.get("ProcessorCount"),
                "mem_mib": vm.get("MemoryMB"),
                "vm_type": "vm",
            })
        # Hyper-V has no cursor: Get-VM returns the whole host in one call, and a host
        # with enough VMs to need paging is not a Hyper-V host.
        return {"ok": True, "vms": vms, "next_cursor": "", "complete": True,
                "scanned": len(vms)}

    script = _PS_POWER.get(verb)
    if script is None:
        _fail(f"{verb!r} is not available for Hyper-V")
    # The id is validated by the agent against a closed charset before it gets here;
    # validate again rather than trust it, because this is where it meets a shell.
    if not target or not all(c.isalnum() or c in "-:_." for c in target):
        _fail("target_id is missing or not a valid Hyper-V VM id")
    result = session.run_ps(script.format(vm=target))
    if result.status_code != 0:
        _fail(f"{verb} failed: {result.std_err.decode('utf-8', 'replace')[:400]}")
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
            return {"ok": True, "vms": vms, "next_cursor": "", "complete": True,
                    "scanned": len(vms)}

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
