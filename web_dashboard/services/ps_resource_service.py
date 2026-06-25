"""
BeyondTrust Password Safe resource registration via the BeyondTrust/passwordsafe
Terraform provider.

Optional, per-VM-deploy add-on (mirrors entitle_registration_service.py): when an
operator opts in, a freshly built VM is onboarded into Password Safe as a **managed
system** with **one managed account** — the ``adminuser`` account the bt-ready
provisioners baked into the image — managed by SSH key (no password).

Shaped like entitle_registration_service / terraform_pra_service: inline HCL written
to an ephemeral workdir, ``terraform apply``, ids pulled from outputs, the full
``terraform.tfstate`` returned (scrubbed of secrets) so a later ``deregister`` can
``terraform destroy`` it. Secrets ride ``TF_VAR_*`` so they never land in the HCL.

Auth reuses the Password Safe OAuth client the ps-cli / public-API integration is
configured with, plus the provider-required run-as user:
  pscli_api_url            provider ``url``
  pscli_client_id          provider ``client_id``
  pscli_client_secret      provider ``client_secret``
  pscli_api_account_name   provider ``api_account_name`` (REQUIRED run-as user)

Provider/resource schema confirmed against BeyondTrust/passwordsafe v1.3.0:
  - provider requires url + api_account_name (client_id/client_secret for OAuth);
  - passwordsafe_managed_system_by_workgroup requires workgroup_id (string),
    entity_type_id (number), host_name, platform_id (number);
  - passwordsafe_managed_account requires account_name, system_name, and password
    (sensitive) — SSH-key management is expressed via private_key (+ passphrase) and
    dss_auto_management_flag, so we pass a generated placeholder password and let
    ssh_key_enforcement_mode on the system enforce key-only auth.
"""

import asyncio
import json
import logging
import os
import re
import secrets
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Terraform binary — baked into the Docker image at build time.
_TERRAFORM = os.environ.get("TERRAFORM_EXECUTABLE", "terraform")
# Provider plugin cache written at image-build time (no runtime download).
_PLUGIN_CACHE_DIR = os.environ.get("TF_PLUGIN_CACHE_DIR", "/root/.terraform.d/plugin-cache")

_REDACTED = "**REDACTED-BY-DASHBOARD**"


class PSResourceError(Exception):
    """Raised when a Password Safe registration Terraform operation fails."""


def _cfg(key: str) -> str:
    try:
        from . import config_service
        val = config_service.get(key)
        if val:
            return val
    except Exception:
        pass
    from ..config import settings
    return getattr(settings, key, "") or ""


def _safe_name(name: str) -> str:
    return re.sub(r"[^a-z0-9_]", "_", (name or "").lower()) or "system"


def _tf_env(extra_vars: Optional[dict] = None) -> dict:
    """Environment for Terraform calls. The provider OAuth credentials + the run-as
    user ride TF_VAR_* (the destroy path needs them too), as do per-apply secrets."""
    env = dict(os.environ)
    env["TF_PLUGIN_CACHE_DIR"] = _PLUGIN_CACHE_DIR
    env["TF_IN_AUTOMATION"] = "1"
    env["TF_INPUT"] = "0"
    env["TF_CLI_ARGS"] = "-no-color"
    for cfg_key, tf_var in (
        ("pscli_api_url",          "TF_VAR_ps_url"),
        ("pscli_client_id",        "TF_VAR_ps_client_id"),
        ("pscli_client_secret",    "TF_VAR_ps_client_secret"),
        ("pscli_api_account_name", "TF_VAR_ps_api_account_name"),
    ):
        val = _cfg(cfg_key)
        if val:
            env[tf_var] = val
    for var, val in (extra_vars or {}).items():
        if val is not None:
            env[f"TF_VAR_{var}"] = str(val)
    return env


def _provider_header(extra_vars: str = "") -> str:
    api_version = _cfg("passwordsafe_api_version") or "3.1"
    return f"""\
terraform {{
  required_providers {{
    passwordsafe = {{
      source  = "BeyondTrust/passwordsafe"
      version = "~> 1.0"
    }}
  }}
}}

variable "ps_url"              {{ sensitive = false }}
variable "ps_client_id"        {{ sensitive = true }}
variable "ps_client_secret"    {{ sensitive = true }}
variable "ps_api_account_name" {{ sensitive = false }}
{extra_vars}
provider "passwordsafe" {{
  url              = var.ps_url
  client_id        = var.ps_client_id
  client_secret    = var.ps_client_secret
  api_account_name = var.ps_api_account_name
  api_version      = {json.dumps(api_version)}
}}
"""


def _generate_managed_system_hcl(*, name: str, host_name: str, ip_address: str, port: int,
                                 functional_account_id: int, platform_id: int,
                                 entity_type_id: int, workgroup_id: str,
                                 managed_account_name: str, ssh_key_enforcement_mode: int,
                                 application_host_id: int = 0) -> str:
    """HCL onboarding a VM as a managed system + its existing account.

    The account's SSH private key + placeholder password ride sensitive TF_VARs.
    ``application_host_id`` (>0) routes management through a specific application
    host (the traditional Resource Broker path); 0 leaves it to the agent-plugin
    platform the functional account is tied to."""
    label = _safe_name(name)
    header = _provider_header(
        'variable "ps_account_password"    { sensitive = true }\n'
        'variable "ps_account_private_key" { sensitive = true }\n')
    ip_line = f"  ip_address               = {json.dumps(ip_address)}\n" if ip_address else ""
    app_host_lines = ""
    if application_host_id and int(application_host_id) > 0:
        app_host_lines = (f"  application_host_id      = {int(application_host_id)}\n"
                          "  is_application_host      = false\n")
    return header + f"""
resource "passwordsafe_managed_system_by_workgroup" {json.dumps(label)} {{
  workgroup_id             = {json.dumps(str(workgroup_id))}
  entity_type_id           = {int(entity_type_id)}
  host_name                = {json.dumps(host_name)}
{ip_line}  platform_id              = {int(platform_id)}
  port                     = {int(port)}
  functional_account_id    = {int(functional_account_id)}
  auto_management_flag     = true
  remote_client_type       = "ssh"
  ssh_key_enforcement_mode = {int(ssh_key_enforcement_mode)}
{app_host_lines}  description              = "Auto-onboarded by Infrastructure Management Dashboard"
}}

resource "passwordsafe_managed_account" {json.dumps(label)} {{
  system_name              = passwordsafe_managed_system_by_workgroup.{label}.managed_system_name
  account_name             = {json.dumps(managed_account_name)}
  password                 = var.ps_account_password
  private_key              = var.ps_account_private_key
  dss_auto_management_flag = true
  auto_management_flag     = true
  api_enabled              = true
}}

output "managed_system_id" {{
  value = passwordsafe_managed_system_by_workgroup.{label}.managed_system_id
}}

output "managed_account_id" {{
  value = passwordsafe_managed_account.{label}.id
}}
"""


# ── Terraform plumbing ────────────────────────────────────────────────────────

def _run_tf(args: list, work_dir: str, env: dict, timeout: int = 180) -> subprocess.CompletedProcess:
    return subprocess.run(
        [_TERRAFORM] + args, cwd=work_dir, capture_output=True, text=True,
        timeout=timeout, env=env)


def _scrub_state(tf_state_json: Optional[str]) -> Optional[str]:
    """Redact secret attribute values (password / private_key / passphrase / token)
    from state before it is stashed in the job. Destroy is by id, so values aren't
    needed. Fails CLOSED — drop the state rather than stash a plaintext secret."""
    if not tf_state_json:
        return None
    try:
        state = json.loads(tf_state_json)
        for res in state.get("resources", []):
            for inst in res.get("instances", []):
                attrs = inst.get("attributes") or {}
                for k in ("password", "private_key", "passphrase", "token"):
                    if attrs.get(k):
                        attrs[k] = _REDACTED
        return json.dumps(state)
    except Exception as exc:  # noqa: BLE001
        logger.error("PS: failed to scrub Terraform state — dropping it: %s", exc)
        return None


def _apply_hcl_sync(hcl: str, tf_vars: dict) -> dict:
    env = _tf_env(tf_vars)
    with tempfile.TemporaryDirectory(prefix="ps_tf_") as work_dir:
        Path(work_dir, "main.tf").write_text(hcl)
        init = _run_tf(["init", "-upgrade=false"], work_dir, env, timeout=60)
        if init.returncode != 0:
            raise PSResourceError(
                f"terraform init failed: {init.stderr.strip() or init.stdout.strip()}")
        apply = _run_tf(["apply", "-auto-approve"], work_dir, env, timeout=180)
        if apply.returncode != 0:
            raise PSResourceError(
                f"terraform apply failed: {apply.stderr.strip() or apply.stdout.strip()}")
        out = _run_tf(["output", "-json"], work_dir, env, timeout=30)
        outputs: dict = {}
        if out.returncode == 0 and out.stdout.strip():
            try:
                outputs = {k: v.get("value") for k, v in json.loads(out.stdout).items()}
            except (json.JSONDecodeError, AttributeError):
                pass
        state_path = Path(work_dir, "terraform.tfstate")
        tf_state_json = state_path.read_text() if state_path.exists() else None
        return {
            "managed_system_id": str(outputs.get("managed_system_id") or "") or None,
            "managed_account_id": str(outputs.get("managed_account_id") or "") or None,
            "tf_state_json": _scrub_state(tf_state_json),
        }


def _destroy_sync(tf_state_json: str) -> None:
    """Off-board: restore stored state + provider-only config and destroy (the
    managed account, then the managed system)."""
    try:
        json.loads(tf_state_json)
    except json.JSONDecodeError as e:
        raise PSResourceError(f"tf_state_json is not valid JSON: {e}") from e
    env = _tf_env()
    with tempfile.TemporaryDirectory(prefix="ps_tf_destroy_") as work_dir:
        Path(work_dir, "main.tf").write_text(_provider_header())
        Path(work_dir, "terraform.tfstate").write_text(tf_state_json)
        init = _run_tf(["init", "-upgrade=false"], work_dir, env, timeout=60)
        if init.returncode != 0:
            raise PSResourceError(
                f"terraform init (destroy) failed: {init.stderr.strip() or init.stdout.strip()}")
        destroy = _run_tf(["destroy", "-auto-approve", "-refresh=false"], work_dir, env, timeout=180)
        if destroy.returncode != 0:
            raise PSResourceError(
                f"terraform destroy failed: {destroy.stderr.strip() or destroy.stdout.strip()}")


# ── Public async API ──────────────────────────────────────────────────────────

async def register_managed_system(*, name: str, host_name: str, private_key: str,
                                   functional_account_id: int, platform_id: int,
                                   workgroup_id: str, ip_address: str = "", port: int = 22,
                                   entity_type_id: int = 1, managed_account_name: str = "adminuser",
                                   ssh_key_enforcement_mode: int = 2,
                                   application_host_id: int = 0) -> dict:
    """Onboard a VM as a Password Safe managed system + SSH-key-managed account.
    Returns ``{managed_system_id, managed_account_id, tf_state_json}``."""
    if not private_key:
        raise PSResourceError(
            "no SSH private key available for the managed account — Password Safe "
            "manages the account by key; check the VM keypair secret")
    hcl = _generate_managed_system_hcl(
        name=name, host_name=host_name, ip_address=ip_address, port=port,
        functional_account_id=functional_account_id, platform_id=platform_id,
        entity_type_id=entity_type_id, workgroup_id=workgroup_id,
        managed_account_name=managed_account_name,
        ssh_key_enforcement_mode=ssh_key_enforcement_mode,
        application_host_id=application_host_id)
    # The provider requires a password even for a key-managed account; supply a
    # strong placeholder it never uses (SSH-key enforcement lives on the system).
    tf_vars = {
        "ps_account_password": secrets.token_urlsafe(24),
        "ps_account_private_key": private_key,
    }
    return await asyncio.to_thread(_apply_hcl_sync, hcl, tf_vars)


async def deregister(tf_state_json: str) -> None:
    """Off-board a managed system + account previously registered (best-effort)."""
    await asyncio.to_thread(_destroy_sync, tf_state_json)
