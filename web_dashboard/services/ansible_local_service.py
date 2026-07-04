"""
Local Docker Ansible runner — on-premises hypervisor inventory + execution.

build_inventory()
    Returns an Ansible JSON inventory populated from every on-premises
    hypervisor integration that is both enabled and has a host configured.
    Only those hypervisors appear, keeping the target list clean.

get_configured_targets()
    Returns a list of {key, label, host} dicts for the UI target picker.

asset_type(name)
    Returns the asset type based on file extension: playbook | script | rpm | deb.

generate_playbook_yaml(asset_name)
    Generates an Ansible playbook YAML that runs/installs a non-playbook asset.
    The asset is expected at /ansible/assets/{asset_name} inside the container.

fetch_ssh_key(cloud)
    Retrieves the SSH private key PEM for a cloud provider from the appropriate
    secret store (AWS Secrets Manager for "aws", GCP Secret Manager for "gcp").

run_playbook(asset_b64, target, extra_vars, asset_name, ssh_key_pem)
    Runs an Ansible playbook or provisioning asset in a sibling Docker container
    (launched via the mounted Docker socket). Returns (combined_output, returncode).
    Credentials and keys are embedded in a temp directory that is deleted after run.
    Hyper-V targets use ansible_connection=winrm; all others SSH.
"""
import asyncio
import base64
import json
import logging
import os
import shlex
import subprocess
import tempfile

logger = logging.getLogger(__name__)

_EXT_TYPE: dict[str, str] = {
    ".yml": "playbook", ".yaml": "playbook",
    ".sh": "script", ".ps1": "powershell",
    ".rpm": "rpm", ".deb": "deb",
}


def _cfg(key: str) -> str:
    from . import config_service
    return config_service.get(key) or ""


def _cfg_bool(key: str, default: bool = False) -> bool:
    from . import config_service
    return config_service.get_bool(key, default)


# ── Asset type helpers ────────────────────────────────────────────────────────

def asset_type(name: str) -> str:
    """Return asset type based on file extension: playbook | script | rpm | deb."""
    ext = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
    return _EXT_TYPE.get(ext, "playbook")


def generate_playbook_yaml(asset_name: str) -> str:
    """
    Generate an Ansible playbook that runs/installs the given asset.

    The asset is available at /ansible/assets/{basename} inside the container
    (bind-mounted from {tmpdir}/assets/).  Raises ValueError for .yml assets
    since those should be used as-is.
    """
    atype = asset_type(asset_name)
    base = os.path.basename(asset_name)
    container_path = f"/ansible/assets/{base}"

    if atype == "script":
        return f"""\
- hosts: all
  become: yes
  tasks:
    - name: Run {base}
      ansible.builtin.script:
        cmd: {container_path}
        executable: /bin/bash
"""

    if atype == "powershell":
        # Targets Windows hosts via WinRM. The host's inventory entry must
        # set ansible_connection=winrm (set in your hypervisor hostvars).
        # win_script copies the .ps1 to the remote temp dir, runs it under
        # PowerShell.exe, and removes it afterwards.
        return f"""\
- hosts: all
  tasks:
    - name: Run {base}
      ansible.windows.win_script:
        cmd: {container_path}
"""

    if atype == "rpm":
        return f"""\
- hosts: all
  become: yes
  tasks:
    - name: Copy {base} to remote
      ansible.builtin.copy:
        src: {container_path}
        dest: /tmp/{base}
    - name: Install {base}
      ansible.builtin.dnf:
        name: /tmp/{base}
        state: present
        disable_gpg_check: true
"""

    if atype == "deb":
        return f"""\
- hosts: all
  become: yes
  tasks:
    - name: Copy {base} to remote
      ansible.builtin.copy:
        src: {container_path}
        dest: /tmp/{base}
    - name: Install {base}
      ansible.builtin.apt:
        deb: /tmp/{base}
"""

    raise ValueError(f"Cannot auto-generate playbook for type {atype!r} — supply a .yml file")


# ── SSH key retrieval ─────────────────────────────────────────────────────────

def _normalize_key(value: str) -> str:
    """Strip CR characters and normalize line endings. Some secret stores
    (and copy-paste from PRA / Key Vault portals) deliver PEM blobs with
    CRLF, which `cryptography` rejects via its line-based regex matchers."""
    return (value or "").replace("\r\n", "\n").replace("\r", "\n")


def _extract_private_key(raw: str) -> str:
    """If `raw` is a JSON `{private_key, public_key}` envelope, return the
    private_key field; otherwise return the raw value. Always normalized."""
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return _normalize_key(data.get("private_key") or data.get("key") or raw)
    except (json.JSONDecodeError, TypeError, AttributeError):
        pass
    return _normalize_key(raw)


def _fetch_aws_ssh_key_sync(secret_name: str) -> str:
    from .aws_service import _get_secret_sync
    region = _cfg("aws_region") or "us-east-1"
    return _extract_private_key(_get_secret_sync(secret_name, region))


def _fetch_gcp_ssh_key_sync(secret_name: str) -> str:
    from .gcp_service import _get_secret_sync
    project_id = _cfg("gcp_project_id")
    return _extract_private_key(_get_secret_sync(project_id, secret_name))


def _fetch_azure_ssh_key_sync(cred, vault_url: str, secret_name: str) -> str:
    from .azure_service import _get_ssh_key_from_vault_sync
    # _get_ssh_key_from_vault_sync already runs _normalize_pem on the raw value
    # but returns it whole — so when the secret is a JSON keypair envelope we
    # still need to pull the private_key field out here.
    return _extract_private_key(_get_ssh_key_from_vault_sync(cred, vault_url, secret_name))


async def fetch_ssh_key(cloud: str) -> str | None:
    """
    Fetch the SSH private key PEM for the given cloud.

    "aws"   → AWS Secrets Manager (ansible_ssh_key_sm_name config key)
    "gcp"   → GCP Secret Manager  (gcp_ssh_key_secret_name config key)
    "azure" → Azure Key Vault     (ansible_aci_ssh_key_secret_name config key)
    ""      → None

    All three paths handle either a raw PEM secret or a JSON
    `{public_key, private_key}` envelope and return CRLF-normalized PEM.
    """
    if cloud == "aws":
        secret_name = _cfg("ansible_ssh_key_sm_name")
        if not secret_name:
            return None
        return await asyncio.to_thread(_fetch_aws_ssh_key_sync, secret_name)
    if cloud == "gcp":
        secret_name = _cfg("gcp_ssh_key_secret_name")
        if not secret_name:
            return None
        return await asyncio.to_thread(_fetch_gcp_ssh_key_sync, secret_name)
    if cloud == "azure":
        secret_name = _cfg("ansible_aci_ssh_key_secret_name")
        vault_url = _cfg("azure_key_vault_url")
        if not secret_name or not vault_url:
            return None
        from .azure_service import _ensure_creds
        cred, _ = await _ensure_creds()
        return await asyncio.to_thread(_fetch_azure_ssh_key_sync, cred, vault_url, secret_name)
    return None


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
    asset_b64: str,
    target: str,
    extra_vars: dict | None = None,
    asset_name: str = "playbook.yml",
    ssh_key_pem: str | None = None,
    secret_extra_vars: dict | None = None,
) -> tuple[str, int]:
    """
    Run an Ansible playbook or provisioning asset in a sibling Docker container.

    asset_b64    — base64-encoded asset bytes (.yml playbook, .sh script, .rpm, .deb)
    target       — inventory group key (e.g. "proxmox") or bare host/IP for cloud
    extra_vars   — optional dict forwarded as --extra-vars JSON
    asset_name   — original filename; drives whether to generate a wrapper playbook
    ssh_key_pem  — PEM private key for cloud targets; written to tmpdir/id_rsa

    Returns (combined_output, returncode).  Non-zero rc means Ansible failed;
    the output text contains the error details.

    The temp directory (containing credentials and any SSH key) is deleted as
    soon as the container exits.
    """
    image = _cfg("ansible_local_image") or "willhallonline/ansible:latest"
    inventory = build_inventory()
    is_group = target in inventory and target not in ("_meta", "on_premises")
    atype = asset_type(asset_name)

    with tempfile.TemporaryDirectory(prefix="ansible_run_") as tmpdir:

        # ── write asset and playbook ──────────────────────────────────────────
        if atype == "playbook":
            pb_path = os.path.join(tmpdir, "playbook.yml")
            with open(pb_path, "wb") as f:
                f.write(base64.b64decode(asset_b64))
        else:
            assets_dir = os.path.join(tmpdir, "assets")
            os.makedirs(assets_dir, exist_ok=True)
            asset_path = os.path.join(assets_dir, os.path.basename(asset_name))
            with open(asset_path, "wb") as f:
                f.write(base64.b64decode(asset_b64))
            pb_path = os.path.join(tmpdir, "playbook.yml")
            with open(pb_path, "w") as f:
                f.write(generate_playbook_yaml(asset_name))

        # ── write inventory ───────────────────────────────────────────────────
        inv_path = os.path.join(tmpdir, "inventory.json")
        with open(inv_path, "w") as f:
            json.dump(inventory, f)

        inv_arg = "/ansible/inventory.json" if is_group else f"{target},"

        # ── write SSH key if provided ─────────────────────────────────────────
        has_key = bool(ssh_key_pem)
        if ssh_key_pem:
            key_path = os.path.join(tmpdir, "id_rsa")
            with open(key_path, "w") as f:
                f.write(ssh_key_pem)
            # chmod 600 on host side; container will also chmod to satisfy SSH
            try:
                os.chmod(key_path, 0o600)
            except OSError:
                pass  # Windows NTFS — container will handle it

        # ── write secret extra-vars to a 0600 file (never on the command line) ──
        has_secret_vars = bool(secret_extra_vars)
        if has_secret_vars:
            sv_path = os.path.join(tmpdir, "secret_vars.json")
            with open(sv_path, "w") as f:
                json.dump(secret_extra_vars, f)
            try:
                os.chmod(sv_path, 0o600)
            except OSError:
                pass  # Windows NTFS — the file is in the per-run tmpdir either way

        # ── build ansible-playbook args ───────────────────────────────────────
        ansible_args: list[str] = [
            "ansible-playbook",
            "-i", inv_arg,
            "/ansible/playbook.yml",
            "--ssh-common-args",
            "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null",
        ]
        if is_group:
            ansible_args += ["--limit", target]
        if has_key:
            ansible_args += ["--private-key", "/ansible/id_rsa"]
        if extra_vars:
            ansible_args += ["--extra-vars", json.dumps(extra_vars)]
        if has_secret_vars:
            # @file keeps secret values off the process args and logs; it's read
            # inside the container and deleted with the tmpdir when the run ends.
            # Comes after the inline extra-vars so a secret var wins on conflict.
            ansible_args += ["--extra-vars", "@/ansible/secret_vars.json"]

        # Wrap in sh -c so we can chmod the key inside the container (needed on
        # Windows Docker Desktop where host-side chmod may not propagate).
        ansible_cmd_str = " ".join(shlex.quote(a) for a in ansible_args)
        if has_key:
            shell_cmd = f"chmod 600 /ansible/id_rsa 2>/dev/null; {ansible_cmd_str}"
        else:
            shell_cmd = ansible_cmd_str

        cmd: list[str] = [
            "docker", "run", "--rm",
            "-v", f"{tmpdir}:/ansible",
            image,
            "sh", "-c", shell_cmd,
        ]

        logger.info(
            "ansible-local: target=%s image=%s is_group=%s atype=%s has_key=%s",
            target, image, is_group, atype, has_key,
        )
        return await asyncio.to_thread(_run_sync, cmd)
