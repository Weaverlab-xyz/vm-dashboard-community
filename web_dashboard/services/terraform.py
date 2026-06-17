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
import contextlib
try:
    import fcntl  # POSIX-only; the app runs in Linux containers (absent on Windows dev hosts → locking is a no-op there).
except ImportError:  # pragma: no cover
    fcntl = None
from typing import Awaitable, Callable, Optional

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


def _init_lock_path() -> str:
    cache = os.environ.get("TF_PLUGIN_CACHE_DIR") or os.path.join(
        os.path.expanduser("~"), ".terraform.d", "plugin-cache")
    try:
        os.makedirs(cache, exist_ok=True)
    except OSError:
        cache = "/tmp"
    return os.path.join(cache, ".tf-init.lock")


@contextlib.contextmanager
def _plugin_cache_lock():
    """Serialize ``terraform init`` across processes/jobs.

    Terraform's shared plugin cache (``TF_PLUGIN_CACHE_DIR``, populated once at
    image build) is explicitly NOT concurrency-safe: parallel inits race to
    (re)place the same provider binary and fail with "text file busy" (ETXTBSY) —
    exactly what happens when several provisions/decommissions are kicked off at
    once. A coarse exclusive file lock around init only (apply/destroy don't touch
    the cache, so those still run in parallel) serializes provider placement
    across gunicorn workers and concurrent jobs. ``flock`` is advisory and
    auto-released if a worker dies. No-op where ``fcntl`` is absent (Windows dev).
    """
    if fcntl is None:
        yield
        return
    fd = open(_init_lock_path(), "w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            fd.close()


def _init_args(backend_type: str, backend_config: Optional[dict]) -> list:
    """`terraform init` args; remote backends get -reconfigure + -backend-config."""
    args = ["init", "-no-color", "-input=false", "-upgrade=false"]
    if backend_type != "local":
        args.append("-reconfigure")
        for k, v in (backend_config or {}).items():
            args.append(f"-backend-config={k}={v}")
    return args


def _init_sync(deploy_dir: str, env: Optional[dict] = None,
               backend_type: str = "local", backend_config: Optional[dict] = None) -> None:
    # Providers are pre-cached in deploy_dir/.terraform/providers (copied from the
    # template), so -upgrade=false keeps provider fetch offline; the remote backend
    # init still reaches the state store (that is the point).
    _write_backend_tf(deploy_dir, backend_type)
    with _plugin_cache_lock():
        r = _run(_init_args(backend_type, backend_config), deploy_dir, timeout=300, env=env)
    if r.returncode != 0:
        raise TerraformError(f"terraform init failed:\n{r.stderr}")


def _apply_sync(deploy_dir: str, var_args: list, env: Optional[dict] = None) -> dict:
    """Run terraform apply and return parsed outputs."""
    apply_args = ["apply", "-auto-approve", "-no-color", "-input=false"] + var_args
    r = _run(apply_args, deploy_dir, timeout=600, env=env)
    if r.returncode != 0:
        raise TerraformError(f"terraform apply failed:\n{r.stderr}\n{r.stdout}")

    # Parse outputs. Pass env here too: `terraform output` re-instantiates the
    # providers, and azurerm rebuilds its ARM config at that point — without the
    # ARM_* Service Principal vars it falls back to the `az` CLI (absent in the
    # container) and fails. (AWS/GCP don't authenticate on output, so this only
    # bit Azure.)
    out_r = _run(["output", "-json"], deploy_dir, timeout=30, env=env)
    if out_r.returncode != 0:
        raise TerraformError(f"terraform output failed:\n{out_r.stderr}")
    raw = json.loads(out_r.stdout)
    return {k: v["value"] for k, v in raw.items()}


def _destroy_sync(deploy_dir: str, env: Optional[dict] = None,
                  var_args: Optional[list] = None) -> None:
    cmd = ["destroy", "-auto-approve", "-no-color", "-input=false"] + (var_args or [])
    r = _run(cmd, deploy_dir, timeout=600, env=env)
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


async def _stream(tf_args: list, cwd: str, env: Optional[dict],
                  on_line: Callable[[str], Awaitable[None]]) -> tuple[int, str]:
    """Run a terraform subcommand, streaming each stdout line to the async
    ``on_line`` callback (stderr merged into stdout). Returns (returncode,
    full_output). Mirrors packer_service._stream_command; merges env OVER
    os.environ like :func:`_run` so PATH / SSL_CERT_FILE survive."""
    proc = await asyncio.create_subprocess_exec(
        settings.terraform_executable, *tf_args,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env={**os.environ, **env} if env else None,
    )
    lines: list = []
    while True:
        raw = await proc.stdout.readline()
        if not raw:
            break
        line = raw.decode(errors="replace").rstrip()
        lines.append(line)
        try:
            await on_line(line)
        except Exception:
            pass  # a UI-broadcast hiccup must never abort the terraform run
    await proc.wait()
    return proc.returncode, "\n".join(lines)


# ── Public async API ──────────────────────────────────────────────────────────

async def apply(deploy_dir: str, variables: dict, template_dir: Optional[str] = None,
                env: Optional[dict] = None,
                on_line: Optional[Callable[[str], Awaitable[None]]] = None) -> dict:
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

    # No streaming callback → preserve the exact existing (non-streamed) path.
    if on_line is None:
        await asyncio.to_thread(_init_sync, deploy_dir, merged_env, backend_type, backend_config)
        return await asyncio.to_thread(_apply_sync, deploy_dir, var_args, merged_env)

    # Streaming path: stream the apply (the long, interesting part) line-by-line to
    # on_line (e.g. the job's Live Output). Init runs first via the serialized,
    # non-streamed _init_sync — the shared plugin cache isn't concurrency-safe
    # (see _plugin_cache_lock) and init output is brief. Outputs are still captured
    # via the post-apply `output -json` (parsing them out of the live stream is fragile).
    await asyncio.to_thread(_init_sync, deploy_dir, merged_env, backend_type, backend_config)
    rc, out = await _stream(
        ["apply", "-auto-approve", "-no-color", "-input=false"] + var_args,
        deploy_dir, merged_env, on_line)
    if rc != 0:
        raise TerraformError(f"terraform apply failed:\n{out}")
    out_r = await asyncio.to_thread(_run, ["output", "-json"], deploy_dir, 30, merged_env)
    if out_r.returncode != 0:
        raise TerraformError(f"terraform output failed:\n{out_r.stderr}")
    return {k: v["value"] for k, v in json.loads(out_r.stdout).items()}


async def destroy(deploy_dir: str, env: Optional[dict] = None,
                  template_dir: Optional[str] = None,
                  variables: Optional[dict] = None,
                  on_line: Optional[Callable[[str], Awaitable[None]]] = None) -> None:
    """
    Run terraform destroy for a deployment. State lives in the user's active
    storage backend (remote), so destroy works even if the local deploy dir was
    lost to a container recreate: pass ``template_dir`` and the module is rebuilt
    from it, the remote backend re-init pulls the state, and destroy proceeds.
    ``env`` carries provider credentials, same as :func:`apply`.

    ``variables`` must be the same -var set apply used: ``terraform destroy``
    evaluates the module config and errors on any required variable that isn't
    set ("No value for required variable"). The values don't change *what* is
    destroyed (resources come from state), but provider-config vars (e.g. the
    google provider's project/region) must be correct, so callers reconstruct
    the full set rather than passing placeholders.
    """
    backend_type, backend_config, backend_env = _backend_settings(deploy_dir)
    merged_env = {**backend_env, **(env or {})}
    var_args = _build_var_args(variables) if variables else []

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

    if on_line is None:
        await asyncio.to_thread(_init_sync, deploy_dir, merged_env, backend_type, backend_config)
        await asyncio.to_thread(_destroy_sync, deploy_dir, merged_env, var_args)
        return

    # Streaming path (mirrors apply): serialized non-streamed init, then stream destroy.
    await asyncio.to_thread(_init_sync, deploy_dir, merged_env, backend_type, backend_config)
    rc, out = await _stream(
        ["destroy", "-auto-approve", "-no-color", "-input=false"] + var_args,
        deploy_dir, merged_env, on_line)
    if rc != 0:
        raise TerraformError(f"terraform destroy failed:\n{out}")
