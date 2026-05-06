"""
BeyondTrust PRA Shell Jump provisioning via the beyondtrust/sra Terraform provider.

Replaces the btapi binary for jump management so the dashboard runs natively on
ARM64 containers (Apple Silicon, AWS Graviton) without Rosetta or x86-64 emulation.
HashiCorp ships Terraform as a native ARM64 binary; the SRA provider is downloaded
at build time and cached in $TF_PLUGIN_CACHE_DIR so containers have no outbound
dependency at run time.

PREREQUISITES (enforced at runtime — not created by this service):
  - A Jump Group named bt_jump_group_name must already exist in PRA.
  - A Jumpoint named bt_jumpoint_name must already exist in PRA.
  Both are looked up via data sources; this service never creates them.

Required settings (config_service / .env):
  bt_api_host      - BeyondTrust PRA appliance hostname, e.g. "pra.example.com"
  bt_client_id     - OAuth2 client ID
  bt_client_secret - OAuth2 client secret
  bt_jump_group_name  - name of the pre-existing shared Jump Group
  bt_jumpoint_name    - name of the pre-existing Jumpoint
"""

import asyncio
import json
import logging
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Terraform binary — baked into the Docker image at build time.
_TERRAFORM = os.environ.get("TERRAFORM_EXECUTABLE", "terraform")

# Provider plugin cache written at image-build time so containers never need
# to download the provider at runtime.
_PLUGIN_CACHE_DIR = os.environ.get("TF_PLUGIN_CACHE_DIR", "/root/.terraform.d/plugin-cache")


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


def _tf_env() -> dict:
    """Build the environment for Terraform subprocess calls.

    BT credentials are passed as TF_VAR_* so the HCL template never contains
    secrets in plain text. TF_PLUGIN_CACHE_DIR points at the pre-cached
    provider directory baked into the Docker image.
    """
    env = dict(os.environ)
    env["TF_PLUGIN_CACHE_DIR"] = _PLUGIN_CACHE_DIR
    env["TF_IN_AUTOMATION"] = "1"
    # Suppress interactive prompts and colour codes in CI/container output
    env["TF_INPUT"] = "0"
    env["TF_CLI_ARGS"] = "-no-color"

    # Pass BT credentials as TF_VAR_* so they never appear in HCL files
    for cfg_key, tf_var in (
        ("bt_api_host",      "TF_VAR_bt_host"),
        ("bt_client_id",     "TF_VAR_bt_client_id"),
        ("bt_client_secret", "TF_VAR_bt_client_secret"),
    ):
        val = _cfg(cfg_key)
        if val:
            env[tf_var] = val

    return env


def _generate_hcl(
    vm_name: str,
    hostname: str,
    jump_group_name: str,
    jumpoint_name: str,
    port: int,
    tag: str,
) -> str:
    """Return the Terraform HCL for one Shell Jump resource.

    Both Jump Group and Jumpoint are looked up with data sources — they must
    already exist in PRA. Only the Shell Jump itself is managed (created/destroyed)
    by this Terraform workspace.
    """
    # Derive a stable resource name that is safe for Terraform identifiers
    safe_name = re.sub(r"[^a-z0-9_]", "_", vm_name.lower())

    return f"""\
terraform {{
  required_providers {{
    sra = {{
      source  = "beyondtrust/sra"
      version = "~> 1.0"
    }}
  }}
}}

variable "bt_host"          {{ sensitive = false }}
variable "bt_client_id"     {{ sensitive = true }}
variable "bt_client_secret" {{ sensitive = true }}

provider "sra" {{
  host          = var.bt_host
  client_id     = var.bt_client_id
  client_secret = var.bt_client_secret
}}

data "sra_jump_group_list" "jg" {{
  name = {json.dumps(jump_group_name)}
}}

data "sra_jumpoint_list" "jp" {{
  name = {json.dumps(jumpoint_name)}
}}

resource "sra_shell_jump" {json.dumps(safe_name)} {{
  name          = {json.dumps(vm_name)}
  hostname      = {json.dumps(hostname)}
  jump_group_id = tonumber(data.sra_jump_group_list.jg.items[0].id)
  jumpoint_id   = tonumber(data.sra_jumpoint_list.jp.items[0].id)
  port          = {port}
  protocol      = "ssh"
  tag           = {json.dumps(tag)}
  comments      = "Auto-provisioned by Infrastructure Management Dashboard"
}}

output "shell_jump_id" {{
  value = sra_shell_jump.{safe_name}.id
}}
"""


def _run_tf(args: list, work_dir: str, timeout: int = 120) -> subprocess.CompletedProcess:
    result = subprocess.run(
        [_TERRAFORM] + args,
        cwd=work_dir,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=_tf_env(),
    )
    return result


def _provision_sync(
    vm_name: str,
    hostname: str,
    jump_group_name: str,
    jumpoint_name: str,
    port: int,
    tag: str,
) -> dict:
    """Synchronous worker — run in asyncio.to_thread."""
    with tempfile.TemporaryDirectory(prefix="pra_tf_") as work_dir:
        # Write HCL
        Path(work_dir, "main.tf").write_text(
            _generate_hcl(vm_name, hostname, jump_group_name, jumpoint_name, port, tag)
        )

        # terraform init (uses pre-cached provider — should be fast)
        init = _run_tf(["init", "-upgrade=false"], work_dir, timeout=60)
        if init.returncode != 0:
            raise TerraformPRAError(
                f"terraform init failed: {init.stderr.strip() or init.stdout.strip()}"
            )

        # terraform apply
        apply = _run_tf(["apply", "-auto-approve"], work_dir, timeout=120)
        if apply.returncode != 0:
            raise TerraformPRAError(
                f"terraform apply failed: {apply.stderr.strip() or apply.stdout.strip()}"
            )

        # Parse shell_jump_id from outputs
        out = _run_tf(["output", "-json"], work_dir, timeout=30)
        shell_jump_id: Optional[str] = None
        if out.returncode == 0 and out.stdout.strip():
            try:
                outputs = json.loads(out.stdout)
                shell_jump_id = str(outputs.get("shell_jump_id", {}).get("value", ""))
            except (json.JSONDecodeError, AttributeError):
                pass

        # Persist full Terraform state so destroy can run later without
        # network access to any backend.
        state_path = Path(work_dir, "terraform.tfstate")
        tf_state_json: Optional[str] = None
        if state_path.exists():
            tf_state_json = state_path.read_text()

        return {
            "shell_jump_id": shell_jump_id,
            "jump_group_name": jump_group_name,
            "tf_state_json": tf_state_json,
        }


def _remove_sync(tf_state_json: str) -> None:
    """Synchronous worker — run in asyncio.to_thread."""
    try:
        state = json.loads(tf_state_json)
    except json.JSONDecodeError as e:
        raise TerraformPRAError(f"tf_state_json is not valid JSON: {e}") from e

    # Re-derive HCL from state resources so we can re-run destroy
    resources = state.get("resources", [])
    shell_jump_res = next(
        (r for r in resources if r.get("type") == "sra_shell_jump"), None
    )
    if shell_jump_res is None:
        logger.warning("No sra_shell_jump resource found in Terraform state — nothing to destroy")
        return

    instances = shell_jump_res.get("instances", [])
    if not instances:
        logger.warning("sra_shell_jump resource has no instances in state — nothing to destroy")
        return

    attrs = instances[0].get("attributes", {})
    vm_name      = attrs.get("name", "unknown")
    hostname     = attrs.get("hostname", "")
    port         = int(attrs.get("port", 22))
    tag          = attrs.get("tag", "")

    # Read jump group / jumpoint names from provider config embedded in state
    # (stored under root_module outputs or as data source in state).
    # We can't reconstruct them from attrs alone, so fall back to config.
    jump_group_name = _cfg("bt_jump_group_name")
    jumpoint_name   = _cfg("bt_jumpoint_name")

    with tempfile.TemporaryDirectory(prefix="pra_tf_destroy_") as work_dir:
        Path(work_dir, "main.tf").write_text(
            _generate_hcl(vm_name, hostname, jump_group_name, jumpoint_name, port, tag)
        )
        # Restore state so Terraform knows what to destroy
        Path(work_dir, "terraform.tfstate").write_text(tf_state_json)

        init = _run_tf(["init", "-upgrade=false"], work_dir, timeout=60)
        if init.returncode != 0:
            raise TerraformPRAError(
                f"terraform init (destroy) failed: {init.stderr.strip() or init.stdout.strip()}"
            )

        destroy = _run_tf(["destroy", "-auto-approve"], work_dir, timeout=120)
        if destroy.returncode != 0:
            raise TerraformPRAError(
                f"terraform destroy failed: {destroy.stderr.strip() or destroy.stdout.strip()}"
            )


# ── Public async API ──────────────────────────────────────────────────────────

class TerraformPRAError(Exception):
    """Raised when a Terraform PRA operation fails."""


async def provision_jump(
    vm_name: str,
    hostname: str,
    jump_group_name: str,
    jumpoint_name: str,
    port: int = 22,
    tag: str = "AWS",
) -> dict:
    """Provision a BeyondTrust PRA Shell Jump via Terraform.

    The Jump Group and Jumpoint must already exist in PRA — this function
    only creates the Shell Jump resource itself.

    Returns a dict with:
      shell_jump_id  - PRA numeric ID of the new Shell Jump (str)
      jump_group_name - name of the Jump Group used
      tf_state_json  - full Terraform state JSON (store in job extra_data for destroy)
    """
    return await asyncio.to_thread(
        _provision_sync, vm_name, hostname, jump_group_name, jumpoint_name, port, tag
    )


async def remove_jump(tf_state_json: str) -> None:
    """Destroy a previously provisioned Shell Jump using the stored Terraform state.

    Pass the tf_state_json value returned by provision_jump (stored in job extra_data).
    """
    await asyncio.to_thread(_remove_sync, tf_state_json)
