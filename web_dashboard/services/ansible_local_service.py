"""
Local Docker Ansible runner — on-premises hypervisor inventory + execution.

build_inventory()
    Returns an Ansible JSON inventory populated from every on-premises
    hypervisor integration that is both enabled and has a host configured.
    Only those hypervisors appear, keeping the target list clean.

get_configured_targets()
    Returns a list of {key, label, host} dicts for the UI target picker.

run_playbook(playbook_b64, target, extra_vars)
    Runs an Ansible playbook in a sibling Docker container (launched via the
    mounted Docker socket).  Returns (combined_output, returncode).
    Credentials are embedded in a temp inventory file that is deleted after
    the run.  Hyper-V targets use ansible_connection=winrm; all others SSH.
"""
import asyncio
import base64
import json
import logging
import os
import subprocess
import tempfile

logger = logging.getLogger(__name__)


def _cfg(key: str) -> str:
    from . import config_service
    return config_service.get(key) or ""


def _cfg_bool(key: str, default: bool = False) -> bool:
    from . import config_service
    return config_service.get_bool(key, default)


# ── Per-hypervisor hostvars builders ─────────────────────────────────────────

def _ssh_hostvars(user: str, password: str) -> dict:
    hvars: dict = {}
    if user:
        hvars["ansible_user"] = user
    if password:
        hvars["ansible_password"] = password
    return hvars


def _proxmox_user() -> str:
    u = _cfg("proxmox_user") or "root@pam"
    return u.split("@")[0]


def _vsphere_user() -> str:
    u = _cfg("vsphere_user") or "root"
    return u.split("@")[0]


def _hyperv_hostvars() -> dict:
    use_ssl   = _cfg_bool("hyperv_use_ssl", False)
    verify    = _cfg_bool("hyperv_verify_ssl", False)
    port      = int(_cfg("hyperv_port") or ("5986" if use_ssl else "5985"))
    transport = _cfg("hyperv_transport") or "ntlm"
    username  = _cfg("hyperv_username") or ""
    password  = _cfg("hyperv_password") or ""

    hvars: dict = {
        "ansible_connection":                   "winrm",
        "ansible_winrm_scheme":                 "https" if use_ssl else "http",
        "ansible_winrm_port":                   port,
        "ansible_winrm_transport":              transport,
        "ansible_winrm_server_cert_validation": "validate" if verify else "ignore",
    }
    if username:
        hvars["ansible_user"] = username
    if password:
        hvars["ansible_password"] = password
    return hvars


# ── Hypervisor registry ───────────────────────────────────────────────────────
# Each tuple: (group_key, flag_key, host_key, display_label, hostvars_fn)

_HYPERVISOR_DEFS = [
    (
        "proxmox",
        "proxmox_enabled",
        "proxmox_host",
        "Proxmox VE",
        lambda: _ssh_hostvars(_proxmox_user(), _cfg("proxmox_password")),
    ),
    (
        "vsphere",
        "vsphere_enabled",
        "vsphere_host",
        "VMware vSphere / ESXi",
        lambda: _ssh_hostvars(_vsphere_user(), _cfg("vsphere_password")),
    ),
    (
        "hyperv",
        "hyperv_enabled",
        "hyperv_host",
        "Microsoft Hyper-V",
        _hyperv_hostvars,
    ),
    (
        "nutanix",
        "nutanix_enabled",
        "nutanix_host",
        "Nutanix AHV",
        lambda: _ssh_hostvars(
            _cfg("nutanix_username") or "nutanix",
            _cfg("nutanix_password"),
        ),
    ),
    (
        "xcpng",
        "xcpng_enabled",
        "xcpng_host",
        "XCP-ng / XenServer",
        lambda: _ssh_hostvars(
            _cfg("xcpng_username") or "root",
            _cfg("xcpng_password"),
        ),
    ),
]


# ── Public helpers ────────────────────────────────────────────────────────────

def build_inventory() -> dict:
    """
    Build an Ansible JSON inventory from enabled+configured on-prem hypervisors.

    Only hypervisors with *both* the feature flag enabled *and* a host address
    configured appear in the inventory.  Hyper-V gets ansible_connection=winrm
    with its WinRM settings; all others get SSH with ansible_password.
    """
    inventory: dict = {
        "_meta": {"hostvars": {}},
        "on_premises": {"children": []},
    }

    for group, flag_key, host_key, label, hvars_fn in _HYPERVISOR_DEFS:
        if not _cfg_bool(flag_key):
            continue
        host = _cfg(host_key)
        if not host:
            continue

        hostvars = {
            "ansible_host":     host,
            "hypervisor_type":  group,
            "hypervisor_label": label,
            **hvars_fn(),
        }

        inventory[group] = {"hosts": [host]}
        inventory["_meta"]["hostvars"][host] = hostvars
        inventory["on_premises"]["children"].append(group)

    return inventory


def get_configured_targets() -> list[dict]:
    """Return [{key, label, host}] for each enabled+configured on-prem hypervisor."""
    return [
        {"key": group, "label": label, "host": _cfg(host_key)}
        for group, flag_key, host_key, label, _ in _HYPERVISOR_DEFS
        if _cfg_bool(flag_key) and _cfg(host_key)
    ]


# ── Local Docker runner ───────────────────────────────────────────────────────

def _run_sync(cmd: list[str]) -> tuple[str, int]:
    """Run a subprocess and return (combined stdout+stderr, returncode)."""
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    lines: list[str] = []
    if proc.stdout:
        for line in iter(proc.stdout.readline, ""):
            lines.append(line.rstrip())
    proc.wait()
    return "\n".join(lines), proc.returncode or 0


async def run_playbook(
    playbook_b64: str,
    target: str,
    extra_vars: dict | None = None,
) -> tuple[str, int]:
    """
    Run an Ansible playbook in a sibling Docker container.

    playbook_b64 — base64-encoded playbook YAML (from ansible_storage)
    target       — inventory group key (e.g. "proxmox") or a bare host/IP
    extra_vars   — optional dict forwarded as --extra-vars JSON

    Returns (combined_output, returncode).  Non-zero rc means the playbook
    failed; the output text contains the Ansible error details.

    The inventory JSON (including credentials) lives in a temp directory that
    is deleted as soon as the container exits.
    """
    image = _cfg("ansible_local_image") or "willhallonline/ansible:latest"
    inventory = build_inventory()
    is_group = target in inventory and target not in ("_meta", "on_premises")

    with tempfile.TemporaryDirectory(prefix="ansible_run_") as tmpdir:
        pb_path = os.path.join(tmpdir, "playbook.yml")
        with open(pb_path, "wb") as f:
            f.write(base64.b64decode(playbook_b64))

        inv_path = os.path.join(tmpdir, "inventory.json")
        with open(inv_path, "w") as f:
            json.dump(inventory, f)

        inv_arg = "/ansible/inventory.json" if is_group else f"{target},"

        cmd: list[str] = [
            "docker", "run", "--rm",
            "-v", f"{tmpdir}:/ansible:ro",
            image,
            "ansible-playbook",
            "-i", inv_arg,
            "/ansible/playbook.yml",
            "--ssh-common-args",
            "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null",
        ]
        if is_group:
            cmd += ["--limit", target]
        if extra_vars:
            cmd += ["--extra-vars", json.dumps(extra_vars)]

        logger.info(
            "ansible-local: target=%s image=%s is_group=%s", target, image, is_group
        )
        return await asyncio.to_thread(_run_sync, cmd)
