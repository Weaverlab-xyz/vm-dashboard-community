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

# Terraform state is stored in the user's ACTIVE storage backend (the same
# bucket/container + creds the /storage system uses), under this prefix and keyed
# per deployment job id, so a container recreate no longer orphans cloud resources.
# See docs/terraform-state-backend-plan.md. `local` keeps state in the deploy dir.
_TF_STATE_PREFIX = "terraform-state"


def _cfg(key: str) -> str:
    from . import config_service
    return config_service.get(key) or ""


def _state_key(deploy_dir: str) -> str:
    """`terraform-state/<job_id>` — the job id is the deploy-dir basename."""
    job = os.path.basename(os.path.normpath(deploy_dir))
    return f"{_TF_STATE_PREFIX}/{job}"


def _backend_settings(deploy_dir: str):
    """Resolve ``(backend_type, backend_config, backend_env)`` from the user's
    active storage backend. Cloud backends store state in the same bucket/container
    /storage uses, under ``terraform-state/<job_id>/``, authenticated with the same
    credentials. ``local`` (or no configured backend) → state stays in the deploy
    dir. The ``backend_env`` is merged into the terraform subprocess env so state
    access works even cross-cloud (e.g. an S3 state backend while provisioning GCP).
    """
    from . import storage_service
    backend = storage_service.active_backend()
    key = f"{_state_key(deploy_dir)}/terraform.tfstate"

    if backend == "s3":
        cfg = {
            "bucket": _cfg("storage_s3_bucket"),
            "key": key,
            "region": _cfg("storage_s3_region") or _cfg("aws_region") or "us-east-1",
            # S3-native state locking (Terraform >= 1.10, pinned in the Dockerfile)
            # — no DynamoDB table required.
            "use_lockfile": "true",
        }
        env = {}
        ak, sk = _cfg("aws_access_key_id"), _cfg("aws_secret_access_key")
        if ak and sk:
            env = {"AWS_ACCESS_KEY_ID": ak, "AWS_SECRET_ACCESS_KEY": sk}
        return ("s3", cfg, env)

    if backend == "azure_blob":
        cfg = {
            "storage_account_name": _cfg("storage_azure_account"),
            "container_name": _cfg("storage_azure_container") or "playbooks",
            "key": key,
            "use_azuread_auth": "true",
        }
        env = {}
        for ck, ak in (("azure_client_id", "ARM_CLIENT_ID"),
                       ("azure_client_secret", "ARM_CLIENT_SECRET"),
                       ("azure_tenant_id", "ARM_TENANT_ID"),
                       ("azure_subscription_id", "ARM_SUBSCRIPTION_ID")):
            v = _cfg(ck)
            if v:
                env[ak] = v
        return ("azurerm", cfg, env)

    if backend == "gcs":
        cfg = {"bucket": _cfg("storage_gcs_bucket"), "prefix": _state_key(deploy_dir)}
        env = {}
        creds = _cfg("gcp_service_account_json") or _cfg("gcp_credentials_json")
        if creds:
            env["GOOGLE_CREDENTIALS"] = creds
        return ("gcs", cfg, env)

    return ("local", {}, {})


def _write_backend_tf(deploy_dir: str, backend_type: str) -> None:
    """Write (or clear) ``backend.tf`` selecting the backend type. Values are
    supplied at init via ``-backend-config`` since backend blocks can't take vars."""
    path = os.path.join(deploy_dir, "backend.tf")
    if backend_type == "local":
        if os.path.exists(path):
            os.remove(path)
        return
    with open(path, "w") as fh:
        fh.write('terraform {\n  backend "%s" {}\n}\n' % backend_type)


def _materialize(deploy_dir: str, template_dir: str) -> None:
    """Copy a Terraform module template (incl. the cached .terraform/ providers)
    into deploy_dir. Used by apply, and by destroy to rebuild a deploy dir that a
    container recreate lost (remote state makes that destroy recoverable)."""
    os.makedirs(deploy_dir, exist_ok=True)
    for item in os.listdir(template_dir):
        src = os.path.join(template_dir, item)
        dst = os.path.join(deploy_dir, item)
        if os.path.isdir(src):
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dst)


def _run(cmd: list, cwd: str, timeout: int = 600,
         env: Optional[dict] = None) -> subprocess.CompletedProcess:
    """Run a terraform command synchronously.

    ``env`` entries are merged OVER os.environ rather than replacing it —
    terraform still needs PATH/HOME and, behind a TLS-inspecting proxy,
    SSL_CERT_FILE from the image env.
    """
    full_cmd = [settings.terraform_executable] + cmd
    return subprocess.run(
        full_cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        env={**os.environ, **env} if env else None,
    )


def _init_sync(deploy_dir: str, env: Optional[dict] = None,
               backend_type: str = "local", backend_config: Optional[dict] = None) -> None:
    # Providers are pre-cached in deploy_dir/.terraform/providers (copied from the
    # template), so -upgrade=false keeps provider fetch offline; the remote backend
    # init still reaches the state store (that is the point).
    _write_backend_tf(deploy_dir, backend_type)
    cmd = ["init", "-no-color", "-input=false", "-upgrade=false"]
    if backend_type != "local":
        cmd.append("-reconfigure")
        for k, v in (backend_config or {}).items():
            cmd.append(f"-backend-config={k}={v}")
    r = _run(cmd, deploy_dir, timeout=300, env=env)
    if r.returncode != 0:
        raise TerraformError(f"terraform init failed:\n{r.stderr}")


def _apply_sync(deploy_dir: str, var_args: list, env: Optional[dict] = None) -> dict:
    """Run terraform apply and return parsed outputs."""
    apply_args = ["apply", "-auto-approve", "-no-color", "-input=false"] + var_args
    r = _run(apply_args, deploy_dir, timeout=600, env=env)
    if r.returncode != 0:
        raise TerraformError(f"terraform apply failed:\n{r.stderr}\n{r.stdout}")

    # Parse outputs
    out_r = _run(["output", "-json"], deploy_dir, timeout=30)
    if out_r.returncode != 0:
        raise TerraformError(f"terraform output failed:\n{out_r.stderr}")
    raw = json.loads(out_r.stdout)
    return {k: v["value"] for k, v in raw.items()}


def _destroy_sync(deploy_dir: str, env: Optional[dict] = None) -> None:
    r = _run(["destroy", "-auto-approve", "-no-color", "-input=false"], deploy_dir, timeout=600, env=env)
    if r.returncode != 0:
        raise TerraformError(f"terraform destroy failed:\n{r.stderr}\n{r.stdout}")


def _build_var_args(variables: dict) -> list:
    """Convert a variables dict to a list of -var flags for the CLI."""
    args = []
    for k, v in variables.items():
        if isinstance(v, (list, dict, bool)) or v is None:
            # Non-string values must be HCL expressions, and JSON is valid HCL:
            #   -var 'security_group_ids=["sg-xxx"]'  -var 'tags={"team":"se"}'
            #   -var 'multi_az=true'
            # str(dict)/str(bool) would produce Python syntax ({'k': 'v'}, True),
            # which terraform rejects ("Single quotes are not valid").
            encoded = json.dumps(v)
            args += ["-var", f"{k}={encoded}"]
        else:
            args += ["-var", f"{k}={v}"]
    return args


# ── Public async API ──────────────────────────────────────────────────────────

async def apply(deploy_dir: str, variables: dict, template_dir: Optional[str] = None,
                env: Optional[dict] = None) -> dict:
    """
    Copy a Terraform template into deploy_dir, init, and apply. Returns a dict
    of the module's Terraform outputs.

    ``template_dir`` selects the module (defaults to the EC2 instance template
    for back-compat); the cloud-database service passes ``terraform/db_<engine>``.
    ``env`` is merged over the process environment for the terraform subprocess —
    callers use it to inject provider credentials (e.g. AWS_ACCESS_KEY_ID) the
    same way the packer flow does.
    deploy_dir should be unique per deployment (e.g. based on job_id).

    The template directory is expected to have been pre-initialized once via
    `terraform init` so the provider cache (.terraform/) can be copied and
    re-used without requiring internet access on every deployment.
    """
    src_template = template_dir or _TEMPLATE_DIR
    # Copy the full template directory including the pre-cached .terraform/
    # providers directory (populated by running `terraform init` in the
    # template directory once).
    _materialize(deploy_dir, src_template)

    var_args = _build_var_args(variables)

    # State goes to the user's active storage backend; merge its creds OVER the
    # caller's provider env so both the backend and provider authenticate (they
    # can differ, e.g. an S3 state backend while provisioning GCP).
    backend_type, backend_config, backend_env = _backend_settings(deploy_dir)
    merged_env = {**backend_env, **(env or {})}

    await asyncio.to_thread(_init_sync, deploy_dir, merged_env, backend_type, backend_config)
    outputs = await asyncio.to_thread(_apply_sync, deploy_dir, var_args, merged_env)
    return outputs


async def destroy(deploy_dir: str, env: Optional[dict] = None,
                  template_dir: Optional[str] = None) -> None:
    """
    Run terraform destroy for a deployment. State lives in the user's active
    storage backend (remote), so destroy works even if the local deploy dir was
    lost to a container recreate: pass ``template_dir`` and the module is rebuilt
    from it, the remote backend re-init pulls the state, and destroy proceeds.
    ``env`` carries provider credentials, same as :func:`apply`.
    """
    backend_type, backend_config, backend_env = _backend_settings(deploy_dir)
    merged_env = {**backend_env, **(env or {})}

    # Rebuild the module if the deploy dir was lost (only possible with a remote
    # backend — a local backend's state lived in that dir and is gone with it).
    if not os.path.exists(os.path.join(deploy_dir, "main.tf")):
        if template_dir and os.path.isdir(template_dir) and backend_type != "local":
            _materialize(deploy_dir, template_dir)
        elif not os.path.isdir(deploy_dir):
            raise TerraformError(f"Deployment directory not found: {deploy_dir}")
        elif backend_type == "local":
            raise TerraformError(
                f"No Terraform module/state in {deploy_dir} and backend is local — "
                "cannot destroy; the resource may need manual termination."
            )

    await asyncio.to_thread(_init_sync, deploy_dir, merged_env, backend_type, backend_config)
    await asyncio.to_thread(_destroy_sync, deploy_dir, merged_env)
