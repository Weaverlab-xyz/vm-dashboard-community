"""What a Windows server build does after its VM exists, and undoes at destroy.

Shared by ``azure_vm_service``, ``aws_vm_service`` and ``gcp_vm_service`` so the clouds
cannot drift.
Linux builds have their own per-cloud steps (Shell Jump, Entitle SSH, the SSH-rotation
Password Safe plugins); none of those apply to a Windows guest, which until this module
existed simply got none of them.

:func:`wire` runs, in order:

1. **Password Safe** (per-build opt-in + global flag): onboard the VM as a
   password-managed system, seeded with the build-time administrator password and then
   rotated (``ps_vm_hook.register_windows``). Once Password Safe holds a working
   credential, the build-time copy in the secret manager is deleted — after a rotation
   it is wrong, and a wrong password that LOOKS authoritative is worse than none. If
   onboarding fails, the copy stays as the break-glass credential.
2. **PRA**: a **Shell Jump** (SSH 22, OpenSSH Server) by default, and a Remote RDP jump
   item only when the build asks for RDP (``enable_rdp``, default
   ``windows_rdp_default``) — see :func:`access_modes`. When Password Safe owns the
   credential the jumps get no PRA Vault copy (PRA's Password Safe integration injects
   the current one, and a copy would go stale at the first rotation); otherwise the
   build-time password is vaulted in PRA for injection, as VDI seats do.

   OpenSSH is switched on at first boot by :data:`WINDOWS_SSH_BOOTSTRAP_PS1`, delivered
   per cloud (AWS user data, Azure Run Command, GCP startup-script metadata) and read
   back as a :data:`SSHD_SENTINEL` line. A build whose sshd is confirmed FAILED gets the
   RDP jump instead (``bt_rdp_fallback``); one that could not be confirmed either way
   gets both. Either is a warning on the job (``windows_ssh_error``), so the server is
   never left with no way in and the reason is never hidden.

Everything here is best-effort: the VM exists and its password is stored before any of
it runs, so a failure is recorded on the job and never fails the deploy.

Job-metadata keys written: ``admin_password_custody`` ("secret_manager" |
``"passwordsafe_managed"``), ``admin_password_retired``, ``windows_access`` (what was
asked for), ``windows_ssh_status``, ``windows_ssh_error``, ``bt_shell_jump_id``,
``bt_tf_state``, ``bt_error`` (the keys a Linux build's Shell Jump uses, so every destroy
path already removes it), ``bt_ssh_vault_account_id``, ``bt_ssh_vault_error``,
``bt_rdp_jump_id``, ``bt_rdp_tf_state``, ``bt_rdp_error``, ``bt_rdp_fallback``,
``windows_secret_cleanup_error`` and the ``ps_*`` keys ``ps_vm_hook`` writes.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

CUSTODY_SECRET_MANAGER = "secret_manager"
CUSTODY_PASSWORD_SAFE = "passwordsafe_managed"


def _cfg(key: str) -> str:
    from . import config_service
    from ..config import settings
    return str(config_service.get(key) or getattr(settings, key, "") or "").strip()


def _vault_group_id():
    raw = _cfg("pra_windows_vault_account_group_id")
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


# ── Remote access: SSH by default, RDP on request ────────────────────────────

def access_modes(req) -> tuple[bool, bool]:
    """``(ssh, rdp)`` for one Windows build.

    SSH follows ``windows_ssh_enabled`` (default on). RDP is the request's
    ``enable_rdp`` when it says, else ``windows_rdp_default`` (default off). With SSH
    switched off globally RDP is forced on: a build with neither would be a server
    nothing can open a session to."""
    from . import config_service
    ssh = config_service.get_bool("windows_ssh_enabled", True)
    choice = getattr(req, "enable_rdp", None)
    rdp = bool(choice) if choice is not None else config_service.get_bool(
        "windows_rdp_default", False)
    return ssh, (rdp or not ssh)


SSHD_SENTINEL = "VMDASH-SSHD:"
SSHD_STATUS_FILE = r"C:\ProgramData\vm-dashboard\sshd-bootstrap.txt"

SSH_OK, SSH_FAILED, SSH_UNVERIFIED = "ok", "failed", "unverified"

# First-boot script that turns on OpenSSH Server. Not secret. Idempotent, because GCP
# runs a startup script on EVERY boot. It installs the in-box Feature-on-Demand (already
# present on Server 2025; 2019/2022 fetch it from Windows Update, so the VM needs
# egress), starts sshd, opens 22 and makes PowerShell the login shell, then reports ONE
# sentinel line -- on stdout for Run Command and the GCP serial log, and in
# SSHD_STATUS_FILE for the AWS check. RDP is left exactly as the image has it: not
# creating an RDP jump is what makes RDP optional; hardening the guest is not this
# script's call.
WINDOWS_SSH_BOOTSTRAP_PS1 = r"""
$ErrorActionPreference = 'Stop'
$statusFile = '__STATUS_FILE__'
function Report($line) {
  New-Item -ItemType Directory -Force -Path (Split-Path $statusFile) | Out-Null
  Set-Content -Path $statusFile -Value $line -Encoding ascii
  Write-Output $line
}
try {
  $svc = Get-Service sshd -ErrorAction SilentlyContinue
  if (-not $svc) {
    $cap = Get-WindowsCapability -Online -Name 'OpenSSH.Server*' | Select-Object -First 1
    if ($cap -and $cap.State -ne 'Installed') { Add-WindowsCapability -Online -Name $cap.Name | Out-Null }
    $svc = Get-Service sshd -ErrorAction SilentlyContinue
  }
  if (-not $svc) { throw 'OpenSSH Server is not installed and the Feature-on-Demand could not be added (no route to Windows Update?)' }
  Set-Service sshd -StartupType Automatic
  Start-Service sshd
  if (-not (Get-NetFirewallRule -Name 'vmdash-sshd' -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -Name 'vmdash-sshd' -DisplayName 'OpenSSH Server (vm-dashboard)' -Enabled True -Direction Inbound -Protocol TCP -LocalPort 22 -Action Allow | Out-Null
  }
  if (-not (Test-Path 'HKLM:\SOFTWARE\OpenSSH')) { New-Item -Path 'HKLM:\SOFTWARE\OpenSSH' | Out-Null }
  New-ItemProperty -Path 'HKLM:\SOFTWARE\OpenSSH' -Name DefaultShell -Value "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" -PropertyType String -Force | Out-Null
  if ((Get-Service sshd).Status -ne 'Running') { throw 'sshd is installed but did not start' }
  Report '__SENTINEL__OK'
} catch {
  Report ('__SENTINEL__FAIL ' + ($_.Exception.Message -replace '\s+', ' '))
}
""".strip().replace("__STATUS_FILE__", SSHD_STATUS_FILE).replace("__SENTINEL__", SSHD_SENTINEL)

# What the AWS check runs over SSM: the bootstrap's verdict, or nothing while it runs.
SSHD_STATUS_READ_PS1 = (f"if (Test-Path '{SSHD_STATUS_FILE}') "
                        f"{{ Get-Content '{SSHD_STATUS_FILE}' }}")


def ssh_bootstrap_user_data() -> str:
    """EC2 user data that runs the bootstrap once, at first boot (EC2Launch)."""
    return f"<powershell>\n{WINDOWS_SSH_BOOTSTRAP_PS1}\n</powershell>\n"


def ssh_bootstrap_metadata() -> dict:
    """GCE instance metadata that runs the bootstrap. A STARTUP script rather than the
    sysprep-specialize one, which the on-prem AD join already owns
    (``domain_join_service.agent_join_metadata``) and which runs before Windows Update
    is usable."""
    return {"windows-startup-script-ps1": WINDOWS_SSH_BOOTSTRAP_PS1}


def parse_sshd_status(text: str) -> tuple[str, str]:
    """``(status, detail)`` from the LAST sentinel line in ``text``, or ``("", "")``
    when there is none yet. Matched anywhere in a line, because the GCP serial log
    prefixes each one with the script's name. Last, because a startup script that runs
    on every boot reports once per boot and only the newest is the current state."""
    for line in reversed((text or "").splitlines()):
        i = line.find(SSHD_SENTINEL)
        if i < 0:
            continue
        verdict = line[i + len(SSHD_SENTINEL):].strip()
        if verdict == "OK":
            return SSH_OK, ""
        if verdict.startswith("FAIL"):
            return SSH_FAILED, verdict[4:].strip() or "the bootstrap reported a failure"
    return "", ""


async def _poll_status(read, *, timeout_s: int, interval_s: int) -> tuple[str, str]:
    import asyncio
    import time as _time
    deadline = _time.monotonic() + timeout_s
    last_err = ""
    while True:
        try:
            status, detail = parse_sshd_status(await read())
            if status:
                return status, detail
        except Exception as e:  # noqa: BLE001 — not readable yet is normal early on
            last_err = str(e)
        if _time.monotonic() >= deadline:
            return SSH_UNVERIFIED, (
                f"no OpenSSH bootstrap result after {timeout_s // 60} min"
                + (f" ({last_err})" if last_err else ""))
        await asyncio.sleep(interval_s)


async def confirm_ssh_aws(region: str, instance_id: str, *, timeout_s: int = 900,
                          interval_s: int = 20) -> tuple[str, str]:
    """Read the user-data bootstrap's verdict through Systems Manager."""
    from . import aws_service
    try:
        await aws_service.wait_ssm_online(region, instance_id, timeout_s=min(timeout_s, 600))
    except Exception as e:  # noqa: BLE001
        return SSH_UNVERIFIED, f"could not read the OpenSSH bootstrap result: {e}"

    async def read():
        res = await aws_service.ssm_send_document(
            region, instance_id, "AWS-RunPowerShellScript",
            {"commands": [SSHD_STATUS_READ_PS1]}, timeout=120,
            comment="vm-dashboard sshd check")
        return res.get("stdout") or ""
    return await _poll_status(read, timeout_s=timeout_s, interval_s=interval_s)


async def run_ssh_bootstrap_azure(rg: str, vm_name: str) -> tuple[str, str]:
    """Run the bootstrap through Azure Run Command and read its verdict directly."""
    from . import azure_service
    try:
        res = await azure_service.vm_run_powershell(rg, vm_name, WINDOWS_SSH_BOOTSTRAP_PS1,
                                                    timeout=1200)
    except Exception as e:  # noqa: BLE001
        return SSH_UNVERIFIED, f"Azure Run Command could not run the OpenSSH bootstrap: {e}"
    status, detail = parse_sshd_status(res.get("stdout") or "")
    if status:
        return status, detail
    tail = (res.get("stderr") or res.get("stdout") or "").strip()[-400:]
    return SSH_UNVERIFIED, ("the OpenSSH bootstrap returned no result"
                            + (f": {tail}" if tail else ""))


async def confirm_ssh_gcp(project_id: str, zone: str, instance_name: str, *,
                          timeout_s: int = 900, interval_s: int = 20) -> tuple[str, str]:
    """Read the startup script's verdict from serial port 1, where GCE's metadata-script
    runner logs every line a startup script prints."""
    from . import gcp_service

    async def read():
        return await gcp_service.serial_port_output(project_id, zone, instance_name, port=1)
    return await _poll_status(read, timeout_s=timeout_s, interval_s=interval_s)


def plan_jumps(ssh: bool, rdp: bool, ssh_status: str) -> tuple[bool, bool]:
    """``(shell_jump, rdp_jump)`` to create, given what was asked for and what the sshd
    bootstrap reported. A confirmed failure swaps the Shell Jump for an RDP jump; an
    unconfirmed one gets both, because the Shell Jump may well work and the RDP jump is
    the way in if it does not."""
    if not ssh:
        return False, True
    if ssh_status == SSH_OK:
        return True, rdp
    if ssh_status == SSH_FAILED:
        return False, True
    return True, True


async def wire(db, job_id: str, *, vm_name: str, hostname: str, username: str,
               password: str, result: dict, tag: str, register_in_passwordsafe: bool,
               pra_enabled: bool, jump_group: str, jumpoint_name: str,
               client_secret: str = "", ssh: bool = False, rdp: bool = True,
               ssh_status: str = "", ssh_detail: str = "") -> None:
    """Password Safe, then the PRA jumps. See the module docstring.

    ``ssh`` / ``rdp`` are :func:`access_modes`; ``ssh_status`` / ``ssh_detail`` are the
    bootstrap's verdict (:func:`parse_sshd_status`, or ``SSH_UNVERIFIED``). The defaults
    are the RDP-only build this module did before SSH existed."""
    from . import job_service, ps_vm_hook, windows_admin_secret

    result["admin_password_custody"] = CUSTODY_SECRET_MANAGER
    want_shell, want_rdp = plan_jumps(ssh, rdp, ssh_status)
    result["windows_access"] = {"ssh": bool(ssh), "rdp": bool(rdp)}
    if ssh:
        result["windows_ssh_status"] = ssh_status or SSH_UNVERIFIED
        if ssh_status != SSH_OK:
            result["windows_ssh_error"] = (
                (ssh_detail or "the OpenSSH bootstrap did not report")
                + ("; an RDP jump was created instead" if not want_shell else
                   "; an RDP jump was created as well, in case SSH does not answer"))
            if not rdp:
                result["bt_rdp_fallback"] = True

    if register_in_passwordsafe and ps_vm_hook.registration_enabled():
        await ps_vm_hook.register_windows(
            db, job_id, vm_name, hostname, result=result, tag=tag,
            username=username, password=password,
            port=22 if (want_shell and not want_rdp) else 3389)
        if ps_vm_hook.password_safe_holds_credential(result):
            result["admin_password_custody"] = CUSTODY_PASSWORD_SAFE
            backend = result.get("admin_password_backend") or ""
            ref = result.get("admin_password_ref") or ""
            err = windows_admin_secret.delete(backend, ref)
            if err:
                # Not fatal, and not silent: an orphaned copy that no longer matches the
                # account is something an operator should go and remove.
                result["windows_secret_cleanup_error"] = (
                    f"Password Safe now manages the account, but the build-time copy "
                    f"{ref!r} in {backend} could not be deleted: {err}")
            else:
                result["admin_password_retired"] = True
                result.pop("admin_password_backend", None)
                result.pop("admin_password_ref", None)
    elif register_in_passwordsafe:
        result["ps_error"] = ("Password Safe registration is disabled globally "
                              "(passwordsafe_registration_enabled).")

    if not pra_enabled:
        return
    if not (jump_group and jumpoint_name):
        missing = "no PRA Jump Group / Jumpoint configured for this cloud"
        if want_shell:
            result["bt_error"] = missing
        if want_rdp:
            result["bt_rdp_error"] = missing
        return
    from . import terraform_pra_service
    ps_owned = result["admin_password_custody"] == CUSTODY_PASSWORD_SAFE

    if want_shell:
        job_service.update_progress(db, job_id, 96, "Creating the PRA Shell Jump (SSH)…")
        try:
            # A plain username: `.\user` is an NLA convention, and OpenSSH on Windows
            # resolves a bare name to the local account itself.
            jump = await terraform_pra_service.provision_jump(
                vm_name=vm_name, hostname=hostname,
                jump_group_name=jump_group, jumpoint_name=jumpoint_name,
                port=22, tag=tag, client_secret=client_secret,
                admin_password="" if ps_owned else password,
                vault_account_name="" if ps_owned else f"{vm_name}-ssh-admin",
                vault_username=username,
                vault_account_group_id=_vault_group_id(),
            )
            result["bt_shell_jump_id"] = jump.get("shell_jump_id") or None
            result["bt_tf_state"] = jump.get("tf_state_json")
            result["bt_jump_group_name"] = jump.get("jump_group_name")
            if jump.get("vault_account_id"):
                result["bt_ssh_vault_account_id"] = jump["vault_account_id"]
            if jump.get("vault_error"):
                result["bt_ssh_vault_error"] = jump["vault_error"]
        except Exception as e:  # noqa: BLE001
            logger.warning("PRA Shell Jump for %s failed: %s", vm_name, e)
            result["bt_error"] = str(e)

    if not want_rdp:
        return
    job_service.update_progress(db, job_id, 97, "Creating the PRA Remote RDP jump item…")
    try:
        jump = await terraform_pra_service.provision_rdp_jump(
            name=vm_name, hostname=hostname,
            jump_group_name=jump_group, jumpoint_name=jumpoint_name,
            rdp_username=username, tag=tag,
            admin_password="" if ps_owned else password,
            vault_account_name="" if ps_owned else f"{vm_name}-admin",
            vault_account_group_id=_vault_group_id(),
            client_secret=client_secret,
        )
        result["bt_rdp_jump_id"] = jump.get("rdp_jump_id") or None
        result["bt_rdp_vault_account_id"] = jump.get("vault_account_id")
        result["bt_rdp_tf_state"] = jump.get("tf_state_json")
        result["bt_jump_group_name"] = jump.get("jump_group_name")
    except Exception as e:  # noqa: BLE001
        logger.warning("PRA RDP jump for %s failed: %s", vm_name, e)
        result["bt_rdp_error"] = str(e)


def entra_requested(req) -> tuple[bool, bool]:
    """``(join, intune)`` for one Azure Windows deploy: the request's own choice, else the
    ``azure_windows_entra_join`` / ``azure_windows_entra_intune_enroll`` defaults."""
    from . import config_service

    def pick(field, key):
        v = getattr(req, field, None)
        return bool(v) if v is not None else config_service.get_bool(key, False)

    join = pick("entra_join", "azure_windows_entra_join")
    return join, join and pick("entra_intune_enroll", "azure_windows_entra_intune_enroll")


def _group_ids(key: str) -> list:
    return [g.strip() for g in _cfg(key).replace(";", ",").split(",") if g.strip()]


async def entra_join_azure(db, job_id: str, *, rg: str, vm_name: str, location: str,
                           vm_id: str, intune: bool, result: dict) -> None:
    """Finish an Entra ID join on an Azure VM created with a system identity.

    Installs AADLoginForWindows, then grants the configured Entra groups login on THIS
    VM: ``azure_entra_vm_admin_group_ids`` → Virtual Machine Administrator Login,
    ``azure_entra_vm_user_group_ids`` → Virtual Machine User Login. Assignments are
    recorded in ``entra_role_assignments`` so destroy can remove them.

    Never fails the deploy: the local administrator (vaulted / Password Safe-managed)
    still reaches the VM, so a failure is a warning on the job (``entra_error``,
    ``entra_role_errors``)."""
    from . import azure_service, job_service
    job_service.update_progress(db, job_id, 92, "Joining the VM to Entra ID…")
    try:
        result["entra_join"] = await azure_service.enable_entra_login(
            rg, vm_name, location, intune=intune)
    except Exception as e:  # noqa: BLE001
        logger.warning("Entra join of %s failed: %s", vm_name, e)
        result["entra_error"] = str(e)
        return

    assignments, errors = [], []
    for key, role in (("azure_entra_vm_admin_group_ids", "admin"),
                      ("azure_entra_vm_user_group_ids", "user")):
        for gid in _group_ids(key):
            try:
                r = await azure_service.ensure_role_assignment(
                    scope=vm_id, role=azure_service.ENTRA_VM_LOGIN_ROLES[role],
                    principal_id=gid, principal_type="Group")
                assignments.append({"scope": vm_id, "name": r["name"], "role": role,
                                    "group": gid, "created": r.get("created", True)})
            except Exception as e:  # noqa: BLE001
                hint = (" — the dashboard's service principal needs "
                        "Microsoft.Authorization/roleAssignments/write (Role Based Access "
                        "Control Administrator or User Access Administrator) on the VM's "
                        "resource group" if "403" in str(e) else "")
                errors.append(f"{role} login for group {gid}: {e}{hint}")
    if assignments:
        result["entra_role_assignments"] = assignments
    if errors:
        result["entra_role_errors"] = errors
    if not (_group_ids("azure_entra_vm_admin_group_ids")
            or _group_ids("azure_entra_vm_user_group_ids")):
        result["entra_note"] = ("joined, but no Entra groups are configured for login "
                                "(azure_entra_vm_admin_group_ids / _user_group_ids) — "
                                "assign Virtual Machine Administrator/User Login by hand")


async def teardown_entra(meta: dict, result: dict) -> None:
    """Remove the role assignments :func:`entra_join_azure` CREATED. Run before the VM
    is deleted, while their scope still exists. One that already existed (``created``
    False) was someone else's and stays."""
    from . import azure_service
    errors = []
    for a in (meta or {}).get("entra_role_assignments") or []:
        if not a.get("created", True):
            continue
        try:
            await azure_service.delete_role_assignment(a["scope"], a["name"])
        except Exception as e:  # noqa: BLE001
            errors.append(f"{a.get('name')}: {e}")
    if errors:
        result["entra_role_errors"] = errors


async def teardown(meta: dict, result: dict) -> None:
    """Undo :func:`wire` and the stored password. Best-effort, never raises.

    Order: the RDP jump (and its PRA Vault copy) first, then the stored password. The
    Password Safe managed system is NOT handled here — ``ps_vm_hook.deregister``, which
    every destroy path already calls, removes it from the same ``ps_*`` keys."""
    from . import windows_admin_secret
    meta = meta or {}
    state = meta.get("bt_rdp_tf_state")
    if state:
        try:
            from . import terraform_pra_service
            await terraform_pra_service.remove_rdp_jump(state)
            result["bt_rdp_jump_removed"] = meta.get("bt_rdp_jump_id") or True
        except Exception as e:  # noqa: BLE001
            logger.warning("RDP jump removal failed: %s", e)
            result["bt_rdp_error"] = f"RDP jump removal failed: {e}"
    backend, ref = meta.get("admin_password_backend"), meta.get("admin_password_ref")
    if backend and ref:
        err = windows_admin_secret.delete(backend, ref)
        if err:
            result["windows_secret_cleanup_error"] = f"{backend}:{ref}: {err}"
        else:
            result["admin_password_deleted"] = ref
