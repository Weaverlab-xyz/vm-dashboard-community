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
    """Strip CR characters, normalize line endings, and guarantee exactly one
    trailing newline. Some secret stores (and copy-paste from PRA / Key Vault
    portals) deliver PEM blobs with CRLF, which `cryptography` rejects via its
    line-based regex matchers. And OpenSSH refuses a private-key file that lacks a
    final newline with ``Load key "…": error in libcrypto`` → the connection then
    fails ``Permission denied (publickey)``; many stores hold the PEM with no
    trailing newline, so add one."""
    v = (value or "").replace("\r\n", "\n").replace("\r", "\n").rstrip()
    return v + "\n" if v else ""


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


async def fetch_ssh_key(cloud: str, secret_name: str = "") -> str | None:
    """
    Fetch the SSH private key PEM for the given cloud.

    ``secret_name`` — when given, fetch that specific secret (used to retrieve the
    keypair a cloud VM was actually built with, resolved from its deploy metadata).
    When empty, fall back to the per-cloud global Ansible key config:

    "aws"   → AWS Secrets Manager (ansible_ssh_key_sm_name config key)
    "gcp"   → GCP Secret Manager  (gcp_ssh_key_secret_name config key)
    "azure" → Azure Key Vault     (ansible_aci_ssh_key_secret_name config key)
    ""      → None

    All three paths handle either a raw PEM secret or a JSON
    `{public_key, private_key}` envelope and return CRLF-normalized PEM.
    """
    if cloud == "aws":
        secret_name = secret_name or _cfg("ansible_ssh_key_sm_name")
        if not secret_name:
            return None
        return await asyncio.to_thread(_fetch_aws_ssh_key_sync, secret_name)
    if cloud == "gcp":
        secret_name = secret_name or _cfg("gcp_ssh_key_secret_name")
        if not secret_name:
            return None
        return await asyncio.to_thread(_fetch_gcp_ssh_key_sync, secret_name)
    if cloud == "azure":
        secret_name = secret_name or _cfg("ansible_aci_ssh_key_secret_name")
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


def _split_user(raw: str, default: str) -> str:
    """The OS login half of a management username: ``root@pam`` -> ``root``."""
    return (raw or default).split("@")[0]


def _hyperv_hostvars(conn) -> dict:
    opts      = conn.options or {}
    use_ssl   = bool(opts.get("use_ssl"))
    transport = opts.get("transport") or "ntlm"
    port      = int(conn.port or (5986 if use_ssl else 5985))

    hvars: dict = {
        "ansible_connection":                   "winrm",
        "ansible_winrm_scheme":                 "https" if use_ssl else "http",
        "ansible_winrm_port":                   port,
        "ansible_winrm_transport":              transport,
        "ansible_winrm_server_cert_validation": "validate" if conn.verify_ssl else "ignore",
    }
    if conn.username:
        hvars["ansible_user"] = conn.username
    if conn.secret:
        hvars["ansible_password"] = conn.secret
    return hvars


# ── Hypervisor registry ───────────────────────────────────────────────────────
#
# Keyed by connection kind. Each entry is (flag_key, label, hostvars_fn(conn)).
# Reads a resolved Connection rather than the singleton config keys, so an install
# with three Proxmox clusters now gets three inventory hosts instead of silently
# only ever the first one.

_HYPERVISOR_DEFS = {
    "proxmox": ("proxmox_enabled", "Proxmox VE",
                lambda c: _ssh_hostvars(_split_user(c.username, "root@pam"), c.secret)),
    "vsphere": ("vsphere_enabled", "VMware vSphere / ESXi",
                lambda c: _ssh_hostvars(_split_user(c.username, "root"), c.secret)),
    "hyperv":  ("hyperv_enabled", "Microsoft Hyper-V", _hyperv_hostvars),
    "nutanix": ("nutanix_enabled", "Nutanix AHV",
                lambda c: _ssh_hostvars(c.username or "nutanix", c.secret)),
    "xcpng":   ("xcpng_enabled", "XCP-ng / XenServer",
                lambda c: _ssh_hostvars(c.username or "root", c.secret)),
}


def _enabled_connections(db) -> list:
    """Every enabled, dashboard-reachable hypervisor connection.

    Agent-bound connections are deliberately skipped: they have no host and no
    credential here by design, and this inventory is for plays the dashboard runs
    itself. Including them would produce an entry nothing could ever connect to.

    ``db`` may be None — callers that have no session (and installs mid-migration)
    fall back to the legacy singletons through ``resolve``'s COMPAT branch.
    """
    from . import hypervisor_connection_service as hcs
    out = []
    for kind, (flag_key, label, hvars_fn) in _HYPERVISOR_DEFS.items():
        if not _cfg_bool(flag_key):
            continue
        try:
            if db is None:
                conns = [hcs.resolve(db, kind)]
            else:
                rows = [r for r in db.query(hcs.HypervisorConnection).filter(
                    hcs.HypervisorConnection.kind == kind,
                    hcs.HypervisorConnection.is_active.is_(True)).all()
                    if not r.agent_id]
                conns = [hcs.to_connection(r) for r in rows] or [hcs.resolve(db, kind)]
        except Exception:  # noqa: BLE001 — an unconfigured kind is not an error here
            continue
        for conn in conns:
            if conn.host and not conn.agent_id:
                out.append((kind, label, conn, hvars_fn))
    return out


# ── Public helpers ────────────────────────────────────────────────────────────

def build_inventory(db=None) -> dict:
    """
    Build an Ansible JSON inventory from enabled, dashboard-reachable hypervisors.

    One host per *connection*, not per kind: an install with two Proxmox clusters
    gets both. Hyper-V gets ansible_connection=winrm with its WinRM settings; all
    others get SSH with ansible_password. Agent-bound connections are excluded —
    see :func:`_enabled_connections`.
    """
    inventory: dict = {
        "_meta": {"hostvars": {}},
        "on_premises": {"children": []},
    }

    for kind, label, conn, hvars_fn in _enabled_connections(db):
        hostvars = {
            "ansible_host":     conn.host,
            "hypervisor_type":  kind,
            "hypervisor_label": label,
            **hvars_fn(conn),
        }
        group = inventory.setdefault(kind, {"hosts": []})
        if conn.host not in group["hosts"]:
            group["hosts"].append(conn.host)
        inventory["_meta"]["hostvars"][conn.host] = hostvars
        if kind not in inventory["on_premises"]["children"]:
            inventory["on_premises"]["children"].append(kind)

    return inventory


def get_configured_targets(db=None) -> list[dict]:
    """[{key, label, host, connection_id, connection_name}] per reachable connection."""
    return [
        {"key": kind, "label": label, "host": conn.host,
         "connection_id": conn.id, "connection_name": conn.name}
        for kind, label, conn, _ in _enabled_connections(db)
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
    ps_env: dict | None = None,
    db=None,
) -> tuple[str, int]:
    """
    Run an Ansible playbook or provisioning asset in a sibling Docker container.

    asset_b64    — base64-encoded asset bytes (.yml playbook, .sh script, .rpm, .deb)
    target       — inventory group key (e.g. "proxmox") or bare host/IP for cloud
    extra_vars   — optional dict forwarded as --extra-vars JSON
    asset_name   — original filename; drives whether to generate a wrapper playbook
    ssh_key_pem  — PEM private key for cloud targets; written to tmpdir/id_rsa
    ps_env       — optional auto-injected credential env (PASSWORD_SAFE_* and/or
                   PORTAINER_*) for an in-playbook beyondtrust.secrets_safe
                   lookup; written to a 0600 --env-file so the client secret never lands
                   on the command line (see services/password_safe_runner.py)

    Returns (combined_output, returncode).  Non-zero rc means Ansible failed;
    the output text contains the error details.

    The temp directory (containing credentials and any SSH key) is deleted as
    soon as the container exits.
    """
    image = _cfg("ansible_local_image") or "chrweav/ansible-winrm:latest"
    inventory = build_inventory(db)
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

        # ── write PASSWORD_SAFE_* to a 0600 docker env-file (never on the cmd line) ──
        # Fed to `docker run --env-file`; only the file *path* appears in the argv, and
        # the file is deleted with the per-run tmpdir. Docker env-file lines are literal
        # KEY=VALUE (no shell interpolation), so a client secret is carried verbatim.
        has_ps_env = bool(ps_env)
        if has_ps_env:
            ps_env_path = os.path.join(tmpdir, "ps_env")
            with open(ps_env_path, "w", newline="\n") as f:
                for k, v in ps_env.items():
                    f.write(f"{k}={v}\n")
            try:
                os.chmod(ps_env_path, 0o600)
            except OSError:
                pass  # Windows NTFS — the file is in the per-run tmpdir either way

        # ── build ansible-playbook args ───────────────────────────────────────
        # Shared with the remote agent's one-shot sibling, which runs the same play shape
        # from a different directory. The flag ORDER is the part that must not drift: the
        # `@secret_vars.json` after the inline `--extra-vars` is what makes a resolved
        # secret win a name conflict. See services/ansible_vm_cmd.
        from . import ansible_vm_cmd
        ansible_args = ansible_vm_cmd.build_vm_argv(
            job_dir="/ansible", inventory=inv_arg,
            limit=target if is_group else "",
            private_key=has_key, extra_vars=extra_vars,
            secret_vars_file=has_secret_vars)

        # Wrap in sh -c so we can chmod the key inside the container (needed on
        # Windows Docker Desktop where host-side chmod may not propagate).
        ansible_cmd_str = ansible_vm_cmd.quote(ansible_args)
        if has_key:
            shell_cmd = f"chmod 600 /ansible/id_rsa 2>/dev/null; {ansible_cmd_str}"
        else:
            shell_cmd = ansible_cmd_str

        cmd: list[str] = ["docker", "run", "--rm", "-v", f"{tmpdir}:/ansible"]
        if has_ps_env:
            # --env-file is read by the docker CLI (client-side) from this process's
            # filesystem, so pass the local tmpdir path — not the /ansible bind-mount path.
            cmd += ["--env-file", ps_env_path]
        cmd += [image, "sh", "-c", shell_cmd]

        logger.info(
            "ansible-local: target=%s image=%s is_group=%s atype=%s has_key=%s ps_env=%s",
            target, image, is_group, atype, has_key, has_ps_env,
        )
        return await asyncio.to_thread(_run_sync, cmd)
