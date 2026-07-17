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


def _run_tf(args: list, work_dir: str, timeout: int = 120,
            extra_env: Optional[dict] = None) -> subprocess.CompletedProcess:
    env = _tf_env()
    if extra_env:
        env.update(extra_env)
    result = subprocess.run(
        [_TERRAFORM] + args,
        cwd=work_dir,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )
    return result


def _provision_sync(
    vm_name: str,
    hostname: str,
    jump_group_name: str,
    jumpoint_name: str,
    port: int,
    tag: str,
    client_secret: str = "",
) -> dict:
    """Synchronous worker — run in asyncio.to_thread."""
    # A per-deploy PRA credential overrides the configured bt_client_secret for
    # this apply only (the sensitive TF_VAR the provider block reads).
    extra_env = {"TF_VAR_bt_client_secret": client_secret} if client_secret else None
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
        apply = _run_tf(["apply", "-auto-approve"], work_dir, timeout=120, extra_env=extra_env)
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
    client_secret: str = "",
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
        _provision_sync, vm_name, hostname, jump_group_name, jumpoint_name, port, tag, client_secret
    )


async def remove_jump(tf_state_json: str) -> None:
    """Destroy a previously provisioned Shell Jump using the stored Terraform state.

    Pass the tf_state_json value returned by provision_jump (stored in job extra_data).
    """
    await asyncio.to_thread(_remove_sync, tf_state_json)


# ── Database protocol-tunnel jumps (managed-database feature) ─────────────────
#
# The managed-database feature provisions a private DB and reaches it only
# through a PRA protocol-tunnel jump. Same shape as the Shell Jump pair above —
# look up the pre-existing Jump Group + Jumpoint, manage one tunnel resource —
# but the resource is the engine-specific tunnel jump. Resource names confirmed
# against the cached provider schema (`terraform providers schema -json`).

_DB_TUNNEL_RESOURCE = {
    "postgres": "sra_postgresql_tunnel_jump",
    "mysql": "sra_my_sql_tunnel_jump",   # schema-identical to the postgres jump (verified vs cached provider)
    "sqlserver": "sra_protocol_tunnel_jump",   # generic protocol tunnel; needs tunnel_type (emitted below)
    "oracle": "sra_protocol_tunnel_jump",       # OCI Autonomous DB — generic TCP tunnel to the TLS listener
}
_DB_RESOURCE_ENGINE = {v: k for k, v in _DB_TUNNEL_RESOURCE.items()}

# tunnel_type for the GENERIC sra_protocol_tunnel_jump (the dedicated postgres/
# mysql resources don't take one). SQL Server is TDS-aware ("mssql"); Oracle
# SQL*Net has no dedicated PRA type, so it rides a raw TCP tunnel ("tcp").
_DB_TUNNEL_TYPE = {"sqlserver": "mssql", "oracle": "tcp"}

# jump_item_association `jump_items[].type` enum value per engine — confirmed
# against the cached provider schema, like the resource names above.
_DB_JUMP_ITEM_TYPE = {
    "postgres": "postgresql_tunnel_jump",
    "mysql": "my_sql_tunnel_jump",   # mirrors the resource (sra_my_sql_tunnel_jump) minus the sra_ prefix
    "sqlserver": "protocol_tunnel_jump",   # sra_protocol_tunnel_jump minus the sra_ prefix
    "oracle": "protocol_tunnel_jump",      # same generic protocol-tunnel jump-item type
}


def _generate_db_tunnel_hcl(
    engine: str,
    name: str,
    hostname: str,
    jump_group_name: str,
    jumpoint_name: str,
    username: str,
    database: str,
    tag: str,
    vault_account_name: str = "",
    vault_username: str = "",
    vault_account_group_id: Optional[int] = None,
) -> str:
    """Return the Terraform HCL for one DB protocol-tunnel jump.

    Required resource fields: name, hostname, jump_group_id, jumpoint_id.
    username / database are optional and emitted only when provided.

    When ``vault_account_name`` is set, a Vault username/password account is
    also emitted, associated to the tunnel jump for credential injection. The
    password is NEVER in the HCL — it arrives as ``TF_VAR_db_password``
    (sensitive variable), mirroring the bt_* credentials. The PRA API requires
    the ``criteria`` arrays present (even empty) when ``filter_type=criteria``
    or it 4xxes ("This value must be an array"). ``vault_account_group_id``
    places the account in a Vault account group so a group policy grants it to
    users — without it the provider's computed default lands it in Default.
    With ``vault_account_name=""`` the output is byte-identical to the pre-vault
    template, which the state-driven destroy path relies on.
    """
    resource_type = _DB_TUNNEL_RESOURCE.get(engine)
    if not resource_type:
        raise TerraformPRAError(
            f"DB tunnel for engine {engine!r} not implemented "
            f"(supported: {', '.join(sorted(_DB_TUNNEL_RESOURCE))})"
        )
    safe_name = re.sub(r"[^a-z0-9_]", "_", name.lower())
    extra = ""
    if username:
        extra += f"  username      = {json.dumps(username)}\n"
    if database:
        extra += f"  database      = {json.dumps(database)}\n"
    # The generic sra_protocol_tunnel_jump (sqlserver / oracle) requires an
    # explicit tunnel_type; the dedicated postgres/mysql resources don't take one.
    if resource_type == "sra_protocol_tunnel_jump":
        extra += f'  tunnel_type   = "{_DB_TUNNEL_TYPE.get(engine, "tcp")}"\n'

    var_block = ""
    vault_block = ""
    if vault_account_name:
        jump_item_type = _DB_JUMP_ITEM_TYPE[engine]
        var_block = 'variable "db_password"      { sensitive = true }\n'
        group_line = (f"  account_group_id = {int(vault_account_group_id)}\n"
                      if vault_account_group_id else "")
        # Schema (provider v1.3.0): jump_item_association is a SINGLE nested
        # attribute (`= { ... }` syntax); jump_items is a set of {id, type};
        # criteria arrays must be present (empty) or the PRA API 4xxes.
        vault_block = f"""
resource "sra_vault_username_password_account" "db_admin" {{
  name        = {json.dumps(vault_account_name)}
  username    = {json.dumps(vault_username)}
  password    = var.db_password
  description = "Auto-provisioned by Infrastructure Management Dashboard (managed database)"
{group_line}  jump_item_association = {{
    filter_type = "criteria"
    criteria = {{
      shared_jump_groups = []
      host               = []
      name               = []
      tag                = []
      comment            = []
    }}
    jump_items = [{{
      id   = tonumber({resource_type}.{safe_name}.id)
      type = {json.dumps(jump_item_type)}
    }}]
  }}
}}

output "vault_account_id" {{
  value = sra_vault_username_password_account.db_admin.id
}}
"""

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
{var_block}
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

resource {json.dumps(resource_type)} {json.dumps(safe_name)} {{
  name          = {json.dumps(name)}
  hostname      = {json.dumps(hostname)}
  jump_group_id = tonumber(data.sra_jump_group_list.jg.items[0].id)
  jumpoint_id   = tonumber(data.sra_jumpoint_list.jp.items[0].id)
{extra}  tag           = {json.dumps(tag)}
  comments      = "Auto-provisioned by Infrastructure Management Dashboard (managed database)"
}}

output "tunnel_jump_id" {{
  value = {resource_type}.{safe_name}.id
}}
{vault_block}"""


_REDACTED = "**REDACTED-BY-DASHBOARD**"


def _scrub_tf_state(tf_state_json: str) -> Optional[str]:
    """Redact secret attribute values from a Terraform state document before
    it is stashed in the jobs table (jobs.extra_data is served by the jobs API
    and the MCP get_job tool). The state-driven destroy deletes resources by
    id and does not need the live values. Fails CLOSED: on any parse error
    return None so the caller stashes nothing rather than a plaintext secret."""
    try:
        state = json.loads(tf_state_json)
        for res in state.get("resources", []):
            for inst in res.get("instances", []):
                attrs = inst.get("attributes") or {}
                for secret_attr in ("password", "token"):
                    if attrs.get(secret_attr):
                        attrs[secret_attr] = _REDACTED
        return json.dumps(state)
    except Exception as exc:
        logger.error(
            "failed to scrub Terraform state before stashing — dropping it; the "
            "PRA tunnel/vault account may need manual cleanup at decommission: %s", exc)
        return None


def _provision_db_tunnel_sync(
    engine, name, hostname, jump_group_name, jumpoint_name, username, database, tag,
    admin_password="", vault_account_name="", vault_account_group_id=None,
    client_secret="",
) -> dict:
    want_vault = bool(vault_account_name and admin_password)
    # A per-DB PRA credential overrides the configured bt_client_secret for this
    # apply only (the sensitive TF_VAR the provider block reads).
    _cred_env = {"TF_VAR_bt_client_secret": client_secret} if client_secret else {}
    with tempfile.TemporaryDirectory(prefix="pra_db_tf_") as work_dir:
        Path(work_dir, "main.tf").write_text(
            _generate_db_tunnel_hcl(engine, name, hostname, jump_group_name,
                                    jumpoint_name, username, database, tag,
                                    vault_account_name=vault_account_name if want_vault else "",
                                    vault_username=username,
                                    vault_account_group_id=vault_account_group_id)
        )
        init = _run_tf(["init", "-upgrade=false"], work_dir, timeout=60)
        if init.returncode != 0:
            raise TerraformPRAError(
                f"terraform init failed: {init.stderr.strip() or init.stdout.strip()}")

        extra_env = dict(_cred_env)
        if want_vault:
            extra_env["TF_VAR_db_password"] = admin_password
        apply = _run_tf(["apply", "-auto-approve"], work_dir, timeout=120, extra_env=extra_env or None)
        if apply.returncode != 0 and want_vault:
            # The vault account must not cost the user a tunnel that worked
            # before this feature existed: retry tunnel-only. The provider
            # errors (rather than removing from state) when refresh hits a 404
            # on a half-created item, so drop the vault account from local state
            # first and re-apply without refresh.
            first_err = (apply.stderr.strip() or apply.stdout.strip())[:400]
            logger.warning(
                "PRA vault account apply failed — retrying tunnel-only; if it was partially "
                "created it may need manual cleanup in PRA (check the PRA OAuth client's Vault "
                "account-management permission): %s", first_err)
            want_vault = False
            _run_tf(["state", "rm", "sra_vault_username_password_account.db_admin"],
                    work_dir, timeout=30)
            Path(work_dir, "main.tf").write_text(
                _generate_db_tunnel_hcl(engine, name, hostname, jump_group_name,
                                        jumpoint_name, username, database, tag)
            )
            apply = _run_tf(["apply", "-auto-approve", "-refresh=false"], work_dir,
                            timeout=120, extra_env=_cred_env or None)
        if apply.returncode != 0:
            # Total failure: leave nothing behind in PRA (config on disk is
            # tunnel-only at this point, so no extra env is needed).
            _run_tf(["destroy", "-auto-approve", "-refresh=false"], work_dir, timeout=120)
            raise TerraformPRAError(
                f"terraform apply failed: {apply.stderr.strip() or apply.stdout.strip()}")

        out = _run_tf(["output", "-json"], work_dir, timeout=30)
        tunnel_jump_id: Optional[str] = None
        vault_account_id: Optional[str] = None
        if out.returncode == 0 and out.stdout.strip():
            try:
                outputs = json.loads(out.stdout)
                tunnel_jump_id = str(outputs.get("tunnel_jump_id", {}).get("value", ""))
                vault_raw = outputs.get("vault_account_id", {}).get("value", "")
                vault_account_id = str(vault_raw) if vault_raw else None
            except (json.JSONDecodeError, AttributeError):
                pass

        state_path = Path(work_dir, "terraform.tfstate")
        tf_state_json = state_path.read_text() if state_path.exists() else None
        return {
            "tunnel_jump_id": tunnel_jump_id,
            "vault_account_id": vault_account_id,
            "jump_group_name": jump_group_name,
            "tf_state_json": _scrub_tf_state(tf_state_json) if tf_state_json else None,
        }


# Provider-only config: enough for `terraform destroy` to authenticate and
# remove whatever a stored state holds, without re-declaring any resources.
_PROVIDER_PREAMBLE_HCL = """\
terraform {
  required_providers {
    sra = {
      source  = "beyondtrust/sra"
      version = "~> 1.0"
    }
  }
}

variable "bt_host"          { sensitive = false }
variable "bt_client_id"     { sensitive = true }
variable "bt_client_secret" { sensitive = true }

provider "sra" {
  host          = var.bt_host
  client_id     = var.bt_client_id
  client_secret = var.bt_client_secret
}
"""


def _destroy_state_only_sync(tf_state_json: str) -> None:
    """Destroy every resource in a stored state with a provider-only config."""
    with tempfile.TemporaryDirectory(prefix="pra_db_tf_destroy_") as work_dir:
        Path(work_dir, "main.tf").write_text(_PROVIDER_PREAMBLE_HCL)
        Path(work_dir, "terraform.tfstate").write_text(tf_state_json)
        init = _run_tf(["init", "-upgrade=false"], work_dir, timeout=60)
        if init.returncode != 0:
            raise TerraformPRAError(
                f"terraform init (destroy) failed: {init.stderr.strip() or init.stdout.strip()}")
        # -refresh=false: the provider errors on refresh when an item already
        # 404s (e.g. deleted in the console), which would block teardown.
        destroy = _run_tf(["destroy", "-auto-approve", "-refresh=false"], work_dir, timeout=120)
        if destroy.returncode != 0:
            raise TerraformPRAError(
                f"terraform destroy failed: {destroy.stderr.strip() or destroy.stdout.strip()}")


def _remove_db_tunnel_sync(tf_state_json: str) -> None:
    try:
        state = json.loads(tf_state_json)
    except json.JSONDecodeError as e:
        raise TerraformPRAError(f"tf_state_json is not valid JSON: {e}") from e

    res = next((r for r in state.get("resources", [])
                if r.get("type") in _DB_RESOURCE_ENGINE), None)
    if res is None or not res.get("instances"):
        # No tunnel in state — but other sra resources (e.g. the vault account
        # after a partial cleanup) may remain. Destroy whatever the state holds
        # using a provider-only config; `terraform destroy` removes state-only
        # resources as long as the provider is configured.
        if any(str(r.get("type", "")).startswith("sra_") and r.get("instances")
               for r in state.get("resources", [])):
            logger.info("No DB tunnel in state but other sra resources remain — "
                        "destroying state contents with a provider-only config")
            _destroy_state_only_sync(tf_state_json)
        else:
            logger.warning("No sra DB tunnel resource in Terraform state — nothing to destroy")
        return
    engine = _DB_RESOURCE_ENGINE[res["type"]]
    instances = res.get("instances", [])
    attrs = instances[0].get("attributes", {})
    name     = attrs.get("name", "unknown")
    hostname = attrs.get("hostname", "")
    username = attrs.get("username", "") or ""
    database = attrs.get("database", "") or ""
    tag      = attrs.get("tag", "") or ""
    jump_group_name = _cfg("bt_jump_group_name")
    jumpoint_name   = _cfg("bt_jumpoint_name")

    with tempfile.TemporaryDirectory(prefix="pra_db_tf_destroy_") as work_dir:
        Path(work_dir, "main.tf").write_text(
            _generate_db_tunnel_hcl(engine, name, hostname, jump_group_name,
                                    jumpoint_name, username, database, tag)
        )
        Path(work_dir, "terraform.tfstate").write_text(tf_state_json)
        init = _run_tf(["init", "-upgrade=false"], work_dir, timeout=60)
        if init.returncode != 0:
            raise TerraformPRAError(
                f"terraform init (destroy) failed: {init.stderr.strip() or init.stdout.strip()}")
        # -refresh=false: the provider errors on refresh when an item already
        # 404s (e.g. deleted in the console), which would block teardown.
        destroy = _run_tf(["destroy", "-auto-approve", "-refresh=false"], work_dir, timeout=120)
        if destroy.returncode != 0:
            raise TerraformPRAError(
                f"terraform destroy failed: {destroy.stderr.strip() or destroy.stdout.strip()}")


async def provision_db_tunnel(
    engine: str,
    name: str,
    hostname: str,
    jump_group_name: str,
    jumpoint_name: str,
    username: str = "",
    database: str = "",
    tag: str = "DB",
    admin_password: str = "",
    vault_account_name: str = "",
    vault_account_group_id: Optional[int] = None,
    client_secret: str = "",
) -> dict:
    """Provision a BeyondTrust PRA protocol-tunnel jump for a managed database.

    The Jump Group and Jumpoint must already exist in PRA. When both
    ``admin_password`` and ``vault_account_name`` are given, a Vault
    username/password account is created in the same workspace and associated
    to the tunnel jump for credential injection (the password travels as a
    sensitive TF_VAR, never in HCL; the PRA OAuth client needs Vault
    account-management permission — on failure the tunnel is kept and the
    vault account is skipped with a warning).

    Returns ``{tunnel_jump_id, vault_account_id, jump_group_name,
    tf_state_json}`` — ``tf_state_json`` is SCRUBBED of secret values (safe to
    stash in the provisioning job's extra_data) and still drives
    ``remove_db_tunnel``'s destroy later.
    """
    return await asyncio.to_thread(
        _provision_db_tunnel_sync, engine, name, hostname, jump_group_name,
        jumpoint_name, username, database, tag,
        admin_password, vault_account_name, vault_account_group_id, client_secret,
    )


async def remove_db_tunnel(tf_state_json: str) -> None:
    """Destroy a previously provisioned DB tunnel jump using its stored state."""
    await asyncio.to_thread(_remove_db_tunnel_sync, tf_state_json)


# ── k8s API TCP tunnel (direct-kubectl feature) ───────────────────────────────
#
# A GENERIC tunnel_type="tcp" protocol-tunnel jump straight to the cluster's API
# endpoint (host:443), with a PINNED local listen port. Unlike the tunnel_type="k8s"
# jump (created over REST because the sra provider blocks "k8s", and which injects
# a fixed pra-access token + strips client `--as` impersonation), a raw TCP tunnel
# forwards bytes only: kubectl authenticates end-to-end with its own kubeconfig
# (the cloud-native exec plugin) and can impersonate. tunnel_definitions is a
# semicolon-separated list of local;remote port pairs (e.g. "6443;443") and
# tunnel_listen_address must be within 127.0.0.0/24 — both confirmed against a
# live appliance jump. No Vault account, no credential injection.

def _generate_api_tunnel_hcl(name: str, hostname: str, jump_group_name: str,
                             jumpoint_name: str, tunnel_definitions: str,
                             tag: str = "Kubernetes") -> str:
    """HCL for one sra_protocol_tunnel_jump with tunnel_type="tcp". No url/
    ca_certificates (those are k8s-tunnel-only) and no Vault account — the
    kubeconfig carries its own (token-free, exec-plugin) auth."""
    safe_name = re.sub(r"[^a-z0-9_]", "_", name.lower())
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

resource "sra_protocol_tunnel_jump" {json.dumps(safe_name)} {{
  name                  = {json.dumps(name)}
  hostname              = {json.dumps(hostname)}
  jump_group_id         = tonumber(data.sra_jump_group_list.jg.items[0].id)
  jumpoint_id           = tonumber(data.sra_jumpoint_list.jp.items[0].id)
  tunnel_type           = "tcp"
  tunnel_definitions    = {json.dumps(tunnel_definitions)}
  tunnel_listen_address = "127.0.0.1"
  tag                   = {json.dumps(tag)}
  comments              = "Auto-provisioned by Infrastructure Management Dashboard (k8s API tunnel)"
}}

output "tunnel_jump_id" {{
  value = sra_protocol_tunnel_jump.{safe_name}.id
}}
"""


def _provision_api_tunnel_sync(name, hostname, jump_group_name, jumpoint_name,
                               tunnel_definitions, tag="Kubernetes", client_secret="") -> dict:
    # A per-cluster PRA credential overrides the configured bt_client_secret for
    # this apply only (the sensitive TF_VAR the provider block reads).
    _cred_env = {"TF_VAR_bt_client_secret": client_secret} if client_secret else {}
    with tempfile.TemporaryDirectory(prefix="pra_api_tf_") as work_dir:
        Path(work_dir, "main.tf").write_text(
            _generate_api_tunnel_hcl(name, hostname, jump_group_name, jumpoint_name,
                                     tunnel_definitions, tag))
        init = _run_tf(["init", "-upgrade=false"], work_dir, timeout=60)
        if init.returncode != 0:
            raise TerraformPRAError(
                f"terraform init failed: {init.stderr.strip() or init.stdout.strip()}")
        apply = _run_tf(["apply", "-auto-approve"], work_dir, timeout=120,
                        extra_env=_cred_env or None)
        if apply.returncode != 0:
            _run_tf(["destroy", "-auto-approve", "-refresh=false"], work_dir, timeout=120,
                    extra_env=_cred_env or None)
            raise TerraformPRAError(
                f"terraform apply failed: {apply.stderr.strip() or apply.stdout.strip()}")

        out = _run_tf(["output", "-json"], work_dir, timeout=30)
        tunnel_jump_id: Optional[str] = None
        if out.returncode == 0 and out.stdout.strip():
            try:
                outputs = json.loads(out.stdout)
                tunnel_jump_id = str(outputs.get("tunnel_jump_id", {}).get("value", ""))
            except (json.JSONDecodeError, AttributeError):
                pass

        state_path = Path(work_dir, "terraform.tfstate")
        tf_state_json = state_path.read_text() if state_path.exists() else None
        return {
            "tunnel_jump_id": tunnel_jump_id,
            "jump_group_name": jump_group_name,
            "tf_state_json": _scrub_tf_state(tf_state_json) if tf_state_json else None,
        }


async def provision_api_tunnel(
    name: str,
    hostname: str,
    jump_group_name: str,
    jumpoint_name: str,
    local_port: int = 6443,
    remote_port: int = 443,
    tag: str = "Kubernetes",
    client_secret: str = "",
) -> dict:
    """Provision a generic tunnel_type="tcp" PRA protocol-tunnel jump to a k8s
    API server, with a pinned local listen port. The Jump Group + Jumpoint must
    already exist. Returns ``{tunnel_jump_id, jump_group_name, tf_state_json}``
    (state SCRUBBED, safe to stash; drives ``remove_api_tunnel`` later)."""
    tunnel_definitions = f"{int(local_port)};{int(remote_port)}"
    return await asyncio.to_thread(
        _provision_api_tunnel_sync, name, hostname, jump_group_name, jumpoint_name,
        tunnel_definitions, tag, client_secret)


async def remove_api_tunnel(tf_state_json: str) -> None:
    """Destroy a previously provisioned k8s API TCP tunnel using its stored state.
    The state holds a single sra_protocol_tunnel_jump, so a provider-only destroy
    is sufficient (no engine-specific re-derivation)."""
    await asyncio.to_thread(_destroy_state_only_sync, tf_state_json)


# ── Web Jump (Rancher management UI) ──────────────────────────────────────────
# OPT-IN broker for the central Rancher UI (the sra provider's sra_web_jump): the
# rep opens it from the PRA representative console. The Rancher node is publicly
# reachable at its source-restricted server-url, so this isn't required for
# reachability — it's for brokered/recorded access without adding an operator's IP
# to the CIDR allowlist. Simpler than the DB tunnel: no credential injection
# (Rancher does its own login), so no Vault account / no sensitive resource TF_VARs.

def _generate_web_jump_hcl(name: str, url: str, jump_group_name: str,
                           jumpoint_name: str, tag: str = "rancher",
                           verify_certificate: bool = False) -> str:
    """HCL for one sra_web_jump. Required: name, url, jump_group_id, jumpoint_id
    (ids resolved from the jump-group/jumpoint list data sources, same as the DB
    tunnel). verify_certificate defaults false (Rancher's cert-manager CA is
    self-signed)."""
    safe_name = re.sub(r"[^a-z0-9_]", "_", name.lower())
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

resource "sra_web_jump" {json.dumps(safe_name)} {{
  name               = {json.dumps(name)}
  url                = {json.dumps(url)}
  jump_group_id      = tonumber(data.sra_jump_group_list.jg.items[0].id)
  jumpoint_id        = tonumber(data.sra_jumpoint_list.jp.items[0].id)
  verify_certificate = {str(bool(verify_certificate)).lower()}
  tag                = {json.dumps(tag)}
  comments           = "Auto-provisioned by Infrastructure Management Dashboard (Rancher management UI)"
}}

output "web_jump_id" {{
  value = sra_web_jump.{safe_name}.id
}}
"""


def _provision_web_jump_sync(name, url, jump_group_name, jumpoint_name,
                             tag="rancher", verify_certificate=False, client_secret="") -> dict:
    _cred_env = {"TF_VAR_bt_client_secret": client_secret} if client_secret else {}
    with tempfile.TemporaryDirectory(prefix="pra_web_tf_") as work_dir:
        Path(work_dir, "main.tf").write_text(
            _generate_web_jump_hcl(name, url, jump_group_name, jumpoint_name, tag, verify_certificate))
        init = _run_tf(["init", "-upgrade=false"], work_dir, timeout=60)
        if init.returncode != 0:
            raise TerraformPRAError(
                f"terraform init failed: {init.stderr.strip() or init.stdout.strip()}")
        apply = _run_tf(["apply", "-auto-approve"], work_dir, timeout=120, extra_env=_cred_env or None)
        if apply.returncode != 0:
            _run_tf(["destroy", "-auto-approve", "-refresh=false"], work_dir, timeout=120)
            raise TerraformPRAError(
                f"terraform apply failed: {apply.stderr.strip() or apply.stdout.strip()}")
        out = _run_tf(["output", "-json"], work_dir, timeout=30)
        web_jump_id: Optional[str] = None
        if out.returncode == 0 and out.stdout.strip():
            try:
                web_jump_id = str(json.loads(out.stdout).get("web_jump_id", {}).get("value", "")) or None
            except (json.JSONDecodeError, AttributeError):
                pass
        state_path = Path(work_dir, "terraform.tfstate")
        tf_state_json = state_path.read_text() if state_path.exists() else None
        return {
            "web_jump_id": web_jump_id,
            "jump_group_name": jump_group_name,
            "tf_state_json": _scrub_tf_state(tf_state_json) if tf_state_json else None,
        }


async def provision_web_jump(
    *, name: str, url: str, jump_group_name: str, jumpoint_name: str,
    tag: str = "rancher", verify_certificate: bool = False, client_secret: str = "",
) -> dict:
    """Provision a PRA Web Jump to a web UI (the central Rancher). The Jump Group +
    Jumpoint must already exist. Returns {web_jump_id, jump_group_name,
    tf_state_json} — stash tf_state_json for remove_web_jump."""
    return await asyncio.to_thread(
        _provision_web_jump_sync, name, url, jump_group_name, jumpoint_name,
        tag, verify_certificate, client_secret)


async def remove_web_jump(tf_state_json: str) -> None:
    """Destroy a previously provisioned Web Jump using its stored state
    (provider-only state destroy; the web jump carries no secrets)."""
    await asyncio.to_thread(_destroy_state_only_sync, tf_state_json)


# ── Remote RDP jump (VDI desktops, Phase 2) ──────────────────────────────────
# A VDI seat is reached over PRA via an agentless Remote RDP jump item on the
# Jumpoint. Mirrors the DB-tunnel template (resource + optional Vault account +
# jump_item_association for credential injection); the only shapes that differ
# are the sra_remote_rdp resource (no port/protocol/database; optional
# rdp_username) and the association type "remote_rdp".

def _qualify_local_windows_user(username: str) -> str:
    """Qualify a Windows LOCAL account for NLA/CredSSP over RDP.

    A VDI seat is a standalone workgroup VM whose only admin is a local account
    (e.g. ``azureuser``). A bare username fails the NLA handshake — the injected
    credential must name the local machine, so prefix ``.\\`` ("this computer").
    Left unchanged when already qualified (``domain\\user``) or a UPN
    (``user@domain`` — a future Entra/AD-joined seat), and when blank.
    """
    u = (username or "").strip()
    if not u or "\\" in u or "@" in u:
        return u
    return ".\\" + u


def _generate_rdp_hcl(
    name: str,
    hostname: str,
    jump_group_name: str,
    jumpoint_name: str,
    rdp_username: str = "",
    tag: str = "RDP",
    vault_account_name: str = "",
    vault_account_group_id: Optional[int] = None,
) -> str:
    """Return the Terraform HCL for one Remote RDP jump item.

    Required resource fields: name, hostname, jump_group_id, jumpoint_id.
    rdp_username is optional and emitted only when provided.

    When ``vault_account_name`` is set, a Vault username/password account is
    also emitted, associated to the RDP jump for credential injection (mirrors
    the DB-tunnel template). The password is NEVER in the HCL — it arrives as
    ``TF_VAR_rdp_password`` (sensitive). ``vault_account_group_id`` places the
    account in a Vault account group so a group policy grants it to users;
    without it the provider's default lands it in Default. With
    ``vault_account_name=""`` the output is byte-identical to the no-vault
    template, which the state-driven destroy path relies on.
    """
    safe_name = re.sub(r"[^a-z0-9_]", "_", name.lower())
    # Local-account seats need the username domain-qualified (`.\user`) or NLA
    # rejects the RDP handshake — the injected credential and the jump's default
    # username both use the qualified form.
    q_user = _qualify_local_windows_user(rdp_username)
    extra = ""
    if q_user:
        extra += f"  rdp_username  = {json.dumps(q_user)}\n"

    var_block = ""
    vault_block = ""
    if vault_account_name:
        var_block = 'variable "rdp_password"     { sensitive = true }\n'
        group_line = (f"  account_group_id = {int(vault_account_group_id)}\n"
                      if vault_account_group_id else "")
        # Schema (provider v1.3.0): jump_item_association is a SINGLE nested
        # attribute. `criteria` and every sub-field are optional+COMPUTED — force
        # them to empty [] and the provider recomputes a different value →
        # "Provider produced inconsistent result after apply", which left the seat
        # with a credential-less jump and NLA "Unknown connection error (10001)".
        # So set ONLY the matcher we need: the jump item's unique name (== this
        # VM's name) scopes the account to exactly this RDP jump; leave the other
        # criteria fields computed. `jump_items` pins it by id too. The association
        # `type` is the resource name minus the sra_ prefix (→ "remote_rdp").
        vault_block = f"""
resource "sra_vault_username_password_account" "rdp_admin" {{
  name        = {json.dumps(vault_account_name)}
  username    = {json.dumps(q_user)}
  password    = var.rdp_password
  description = "Auto-provisioned by Infrastructure Management Dashboard (VDI desktop)"
{group_line}  jump_item_association = {{
    filter_type = "criteria"
    criteria = {{
      name = [{json.dumps(name)}]
    }}
    jump_items = [{{
      id   = tonumber(sra_remote_rdp.{safe_name}.id)
      type = "remote_rdp"
    }}]
  }}
}}

output "vault_account_id" {{
  value = sra_vault_username_password_account.rdp_admin.id
}}
"""

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
{var_block}
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

resource "sra_remote_rdp" {json.dumps(safe_name)} {{
  name          = {json.dumps(name)}
  hostname      = {json.dumps(hostname)}
  jump_group_id = tonumber(data.sra_jump_group_list.jg.items[0].id)
  jumpoint_id   = tonumber(data.sra_jumpoint_list.jp.items[0].id)
{extra}  tag           = {json.dumps(tag)}
  comments      = "Auto-provisioned by Infrastructure Management Dashboard (VDI desktop)"
}}

output "rdp_jump_id" {{
  value = sra_remote_rdp.{safe_name}.id
}}
{vault_block}"""


def _provision_rdp_jump_sync(
    name, hostname, jump_group_name, jumpoint_name, rdp_username, tag,
    admin_password="", vault_account_name="", vault_account_group_id=None,
    client_secret="",
) -> dict:
    want_vault = bool(vault_account_name and admin_password)
    _cred_env = {"TF_VAR_bt_client_secret": client_secret} if client_secret else {}
    with tempfile.TemporaryDirectory(prefix="pra_rdp_tf_") as work_dir:
        Path(work_dir, "main.tf").write_text(
            _generate_rdp_hcl(name, hostname, jump_group_name, jumpoint_name,
                              rdp_username, tag,
                              vault_account_name=vault_account_name if want_vault else "",
                              vault_account_group_id=vault_account_group_id)
        )
        init = _run_tf(["init", "-upgrade=false"], work_dir, timeout=60)
        if init.returncode != 0:
            raise TerraformPRAError(
                f"terraform init failed: {init.stderr.strip() or init.stdout.strip()}")

        extra_env = dict(_cred_env)
        if want_vault:
            extra_env["TF_VAR_rdp_password"] = admin_password
        apply = _run_tf(["apply", "-auto-approve"], work_dir, timeout=120, extra_env=extra_env or None)
        if apply.returncode != 0 and want_vault:
            # The vault account must not cost a seat its working jump item: retry
            # jump-only. The provider errors (rather than removing from state) on a
            # half-created item, so drop the vault account from local state first
            # and re-apply without refresh.
            first_err = (apply.stderr.strip() or apply.stdout.strip())[:400]
            logger.warning(
                "PRA vault account apply failed — retrying RDP-jump-only; if it was partially "
                "created it may need manual cleanup in PRA (check the PRA OAuth client's Vault "
                "account-management permission): %s", first_err)
            want_vault = False
            _run_tf(["state", "rm", "sra_vault_username_password_account.rdp_admin"],
                    work_dir, timeout=30)
            Path(work_dir, "main.tf").write_text(
                _generate_rdp_hcl(name, hostname, jump_group_name, jumpoint_name,
                                  rdp_username, tag)
            )
            apply = _run_tf(["apply", "-auto-approve", "-refresh=false"], work_dir,
                            timeout=120, extra_env=_cred_env or None)
        if apply.returncode != 0:
            # Total failure: leave nothing behind in PRA (config on disk is
            # jump-only at this point, so no extra env is needed).
            _run_tf(["destroy", "-auto-approve", "-refresh=false"], work_dir, timeout=120)
            raise TerraformPRAError(
                f"terraform apply failed: {apply.stderr.strip() or apply.stdout.strip()}")

        out = _run_tf(["output", "-json"], work_dir, timeout=30)
        rdp_jump_id: Optional[str] = None
        vault_account_id: Optional[str] = None
        if out.returncode == 0 and out.stdout.strip():
            try:
                outputs = json.loads(out.stdout)
                rdp_jump_id = str(outputs.get("rdp_jump_id", {}).get("value", ""))
                vault_raw = outputs.get("vault_account_id", {}).get("value", "")
                vault_account_id = str(vault_raw) if vault_raw else None
            except (json.JSONDecodeError, AttributeError):
                pass

        state_path = Path(work_dir, "terraform.tfstate")
        tf_state_json = state_path.read_text() if state_path.exists() else None
        return {
            "rdp_jump_id": rdp_jump_id,
            "vault_account_id": vault_account_id,
            "jump_group_name": jump_group_name,
            "tf_state_json": _scrub_tf_state(tf_state_json) if tf_state_json else None,
        }


async def provision_rdp_jump(
    name: str,
    hostname: str,
    jump_group_name: str,
    jumpoint_name: str,
    rdp_username: str = "",
    tag: str = "RDP",
    admin_password: str = "",
    vault_account_name: str = "",
    vault_account_group_id: Optional[int] = None,
    client_secret: str = "",
) -> dict:
    """Provision a BeyondTrust PRA Remote RDP jump item for a VDI desktop seat.

    The Jump Group and Jumpoint must already exist in PRA. When both
    ``admin_password`` and ``vault_account_name`` are given, a Vault
    username/password account is created and associated to the RDP jump for
    credential injection (the password travels as a sensitive TF_VAR, never in
    HCL; the PRA OAuth client needs Vault account-management permission — on
    failure the jump item is kept and the vault account is skipped with a
    warning).

    Returns ``{rdp_jump_id, vault_account_id, jump_group_name, tf_state_json}``
    — ``tf_state_json`` is SCRUBBED of secret values (safe to stash) and still
    drives ``remove_rdp_jump``'s destroy later.
    """
    return await asyncio.to_thread(
        _provision_rdp_jump_sync, name, hostname, jump_group_name, jumpoint_name,
        rdp_username, tag, admin_password, vault_account_name,
        vault_account_group_id, client_secret,
    )


async def remove_rdp_jump(tf_state_json: str) -> None:
    """Destroy a previously provisioned RDP jump (and its vault account, if any)
    using its stored state — state-driven, so it removes whatever sra resources
    the state holds with a provider-only config."""
    await asyncio.to_thread(_destroy_state_only_sync, tf_state_json)


# ── Kubernetes protocol-tunnel jump (K8s management feature) ──────────────────
#
# A managed cluster's API is reached through a native PRA `tunnel_type=k8s`
# protocol-tunnel jump — the community edition provisions it with the
# `beyondtrust/sra` provider's `sra_protocol_tunnel_jump` resource (never btapi,
# matching the DB-tunnel path above). Field names confirmed against the provider
# docs (registry.terraform.io/providers/BeyondTrust/sra → protocol_tunnel_jump):
# tunnel_type ∈ {tcp, mssql, k8s}; `url` + `ca_certificates` are required when
# tunnel_type=k8s; `hostname` is required by the provider even for k8s, so we
# pass the API host. Routed through a configurable Jumpoint (the "separate
# Jumpoint") looked up by name, same as the DB tunnel.

# jump_item_association `jump_items[].type` for a generic protocol tunnel jump
# (the k8s tunnel is sra_protocol_tunnel_jump → type minus the sra_ prefix).
_K8S_JUMP_ITEM_TYPE = "protocol_tunnel_jump"


def _generate_k8s_vault_account_hcl(
    vault_account_name: str,
    vault_account_group_id: Optional[int] = None,
    jump_group_id: Optional[int] = None,
) -> str:
    """HCL for a PRA Vault **token** account (``sra_vault_token_account``) holding a
    cluster's ServiceAccount bearer token, scoped to the k8s tunnel's **Jump Group**
    for credential injection.

    The tunnel jump is created over the REST API (the sra provider's ``tunnel_type``
    validator blocks ``"k8s"``; see pra_api_service). A per-jump-item association
    (``jump_items = [{id, type}]``) is REJECTED by PRA for a tunnel_type=k8s jump
    (422 ``jump_items.0.type … The selected value is invalid``), so the account is
    associated to the jump's Jump Group instead via ``criteria.shared_jump_groups``
    (``TF_VAR_k8s_jump_group_id``). The token rides ``TF_VAR_k8s_sa_token``
    (sensitive) and never lands in the HCL."""
    group_line = (f"  account_group_id = {int(vault_account_group_id)}\n"
                  if vault_account_group_id else "")
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
variable "k8s_sa_token"       {{ sensitive = true }}
variable "k8s_jump_group_id"  {{ sensitive = false }}

provider "sra" {{
  host          = var.bt_host
  client_id     = var.bt_client_id
  client_secret = var.bt_client_secret
}}

resource "sra_vault_token_account" "k8s_access" {{
  name        = {json.dumps(vault_account_name)}
  token       = var.k8s_sa_token
  description = "Auto-provisioned by Infrastructure Management Dashboard (k8s tunnel)"
{group_line}  jump_item_association = {{
    filter_type = "criteria"
    criteria = {{
      shared_jump_groups = [tonumber(var.k8s_jump_group_id)]
      host               = []
      name               = []
      tag                = []
      comment            = []
    }}
  }}
}}

output "vault_account_id" {{
  value = sra_vault_token_account.k8s_access.id
}}
"""


def _provision_k8s_vault_sync(jump_id, vault_account_name, sa_token,
                              vault_account_group_id=None, client_secret="",
                              jump_group_id=None) -> dict:
    """TF-apply a Vault token account scoped (via jump_item_association criteria) to
    the k8s jump's Jump Group. Returns ``{vault_account_id, tf_state_json}`` (state
    scrubbed of the token)."""
    extra_env = {"TF_VAR_k8s_sa_token": sa_token, "TF_VAR_k8s_jump_group_id": str(jump_group_id)}
    if client_secret:
        extra_env["TF_VAR_bt_client_secret"] = client_secret
    with tempfile.TemporaryDirectory(prefix="pra_k8s_vault_tf_") as work_dir:
        Path(work_dir, "main.tf").write_text(
            _generate_k8s_vault_account_hcl(vault_account_name, vault_account_group_id, jump_group_id))
        init = _run_tf(["init", "-upgrade=false"], work_dir, timeout=60)
        if init.returncode != 0:
            raise TerraformPRAError(
                f"terraform init failed: {init.stderr.strip() or init.stdout.strip()}")
        apply = _run_tf(["apply", "-auto-approve"], work_dir, timeout=120, extra_env=extra_env)
        if apply.returncode != 0:
            _run_tf(["destroy", "-auto-approve", "-refresh=false"], work_dir, timeout=120,
                    extra_env=extra_env)
            raise TerraformPRAError(
                f"vault account apply failed: {apply.stderr.strip() or apply.stdout.strip()}")
        out = _run_tf(["output", "-json"], work_dir, timeout=30)
        vault_account_id: Optional[str] = None
        if out.returncode == 0 and out.stdout.strip():
            try:
                vault_raw = json.loads(out.stdout).get("vault_account_id", {}).get("value", "")
                vault_account_id = str(vault_raw) if vault_raw else None
            except (json.JSONDecodeError, AttributeError):
                pass
        state_path = Path(work_dir, "terraform.tfstate")
        tf_state_json = state_path.read_text() if state_path.exists() else None
        return {
            "vault_account_id": vault_account_id,
            "tf_state_json": _scrub_tf_state(tf_state_json) if tf_state_json else None,
        }


async def provision_k8s_tunnel(
    name: str,
    hostname: str,
    api_url: str,
    ca_certificates: str,
    jump_group_name: str,
    jumpoint_name: str,
    tag: str = "Kubernetes",
    client_secret: str = "",
    vault_account_name: str = "",
    sa_token: str = "",
    vault_account_group_id: Optional[int] = None,
) -> dict:
    """Provision a PRA ``tunnel_type=k8s`` protocol-tunnel jump to a managed cluster's
    API. The jump is created over the **REST API** (``pra_api_service``) because the
    sra Terraform provider's ``tunnel_type`` validator rejects ``"k8s"`` client-side
    (see docs/notes/sra-provider-k8s-tunnel-bug.md). The Jump Group + Jumpoint must
    already exist in PRA.

    When ``vault_account_name`` + ``sa_token`` are given, a ``sra_vault_token_account``
    holding the cluster's ServiceAccount bearer token is created via Terraform and
    associated to the REST-created jump, so PRA injects the credential at session
    launch (PRA-only access, no Entitle). The Vault step is best-effort: a failure
    leaves the working tunnel in place (no injection) rather than failing the broker.
    Returns ``{tunnel_jump_id, vault_account_id, jump_group_name, tf_state_json}`` —
    ``tunnel_jump_id`` + ``tf_state_json`` drive ``remove_k8s_tunnel`` later."""
    from . import pra_api_service
    jump_id, jump_group_id = await pra_api_service.create_k8s_tunnel_jump(
        name=name, hostname=hostname, url=api_url, ca_certificates=ca_certificates,
        jump_group_name=jump_group_name, jumpoint_name=jumpoint_name, tag=tag)

    vault_account_id: Optional[str] = None
    tf_state_json: Optional[str] = None
    if vault_account_name and sa_token:
        try:
            v = await asyncio.to_thread(
                _provision_k8s_vault_sync, jump_id, vault_account_name, sa_token,
                vault_account_group_id, client_secret, jump_group_id)
            vault_account_id = v.get("vault_account_id")
            tf_state_json = v.get("tf_state_json")
        except Exception as exc:  # noqa: BLE001 — vault is an enhancement, tunnel stands
            logger.warning("PRA k8s Vault token-account failed (tunnel kept, no injection): %s", exc)

    return {
        "tunnel_jump_id": str(jump_id),
        "vault_account_id": vault_account_id,
        "jump_group_name": jump_group_name,
        "tf_state_json": tf_state_json,
    }


async def remove_k8s_tunnel(tf_state_json: Optional[str] = None, jump_id=None) -> None:
    """Tear down a k8s tunnel: TF-destroy the Vault token account (if one was created)
    from its stored state, then DELETE the protocol-tunnel jump over REST by id."""
    if tf_state_json:
        try:
            await asyncio.to_thread(_destroy_state_only_sync, tf_state_json)
        except Exception as exc:  # noqa: BLE001 — still try to drop the jump below
            logger.warning("PRA k8s Vault account destroy failed (non-fatal): %s", exc)
    if jump_id:
        from . import pra_api_service
        await pra_api_service.delete_protocol_tunnel_jump(jump_id)
