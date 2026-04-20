"""
Terraform subprocess wrapper.
Manages per-deployment state directories and runs terraform apply/destroy.
Uses asyncio.to_thread() so long-running applies don't block the event loop.
"""
import asyncio
import json
import os
import shutil
import subprocess
from typing import Optional

from ..config import settings


class TerraformError(Exception):
    """Raised when a Terraform command fails."""


# Path to the ec2_instance template (relative to this file → ../../terraform/ec2_instance)
_TEMPLATE_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "terraform", "ec2_instance")
)


def _run(cmd: list, cwd: str, timeout: int = 600) -> subprocess.CompletedProcess:
    """Run a terraform command synchronously."""
    full_cmd = [settings.terraform_executable] + cmd
    return subprocess.run(
        full_cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _init_sync(deploy_dir: str) -> None:
    # If the provider is already cached in deploy_dir/.terraform/providers, use
    # -upgrade=false so Terraform doesn't contact the registry at all.
    provider_cache = os.path.join(deploy_dir, ".terraform", "providers")
    upgrade_flag = "-upgrade=false" if os.path.isdir(provider_cache) else "-upgrade=false"
    r = _run(["init", "-no-color", "-input=false", upgrade_flag], deploy_dir, timeout=300)
    if r.returncode != 0:
        raise TerraformError(f"terraform init failed:\n{r.stderr}")


def _apply_sync(deploy_dir: str, var_args: list) -> dict:
    """Run terraform apply and return parsed outputs."""
    apply_args = ["apply", "-auto-approve", "-no-color", "-input=false"] + var_args
    r = _run(apply_args, deploy_dir, timeout=600)
    if r.returncode != 0:
        raise TerraformError(f"terraform apply failed:\n{r.stderr}\n{r.stdout}")

    # Parse outputs
    out_r = _run(["output", "-json"], deploy_dir, timeout=30)
    if out_r.returncode != 0:
        raise TerraformError(f"terraform output failed:\n{out_r.stderr}")
    raw = json.loads(out_r.stdout)
    return {k: v["value"] for k, v in raw.items()}


def _destroy_sync(deploy_dir: str) -> None:
    r = _run(["destroy", "-auto-approve", "-no-color", "-input=false"], deploy_dir, timeout=600)
    if r.returncode != 0:
        raise TerraformError(f"terraform destroy failed:\n{r.stderr}\n{r.stdout}")


def _build_var_args(variables: dict) -> list:
    """Convert a variables dict to a list of -var flags for the CLI."""
    args = []
    for k, v in variables.items():
        if isinstance(v, list):
            # Terraform list: -var 'security_group_ids=["sg-xxx","sg-yyy"]'
            encoded = json.dumps(v)
            args += ["-var", f"{k}={encoded}"]
        else:
            args += ["-var", f"{k}={v}"]
    return args


# ── Public async API ──────────────────────────────────────────────────────────

async def apply(deploy_dir: str, variables: dict) -> dict:
    """
    Copy the EC2 instance template into deploy_dir, init, and apply.
    Returns a dict of Terraform outputs: instance_id, public_ip, private_ip.

    deploy_dir should be unique per deployment (e.g. based on job_id).

    The template directory is expected to have been pre-initialized once via
    `terraform init` so the provider cache (.terraform/) can be copied and
    re-used without requiring internet access on every deployment.
    """
    # Copy the full template directory including the pre-cached .terraform/
    # providers directory (populated by running `terraform init` in the
    # template directory once).  shutil.copytree handles subdirectories.
    os.makedirs(deploy_dir, exist_ok=True)
    for item in os.listdir(_TEMPLATE_DIR):
        src = os.path.join(_TEMPLATE_DIR, item)
        dst = os.path.join(deploy_dir, item)
        if os.path.isdir(src):
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dst)

    var_args = _build_var_args(variables)

    await asyncio.to_thread(_init_sync, deploy_dir)
    outputs = await asyncio.to_thread(_apply_sync, deploy_dir, var_args)
    return outputs


async def destroy(deploy_dir: str) -> None:
    """
    Run terraform destroy in the given deployment directory.
    The state must still be present in that directory.
    """
    if not os.path.isdir(deploy_dir):
        raise TerraformError(f"Deployment directory not found: {deploy_dir}")
    if not os.path.exists(os.path.join(deploy_dir, "terraform.tfstate")):
        raise TerraformError(
            f"No Terraform state found in {deploy_dir}. "
            "Cannot destroy — the instance may need to be terminated manually via AWS console."
        )
    await asyncio.to_thread(_destroy_sync, deploy_dir)
