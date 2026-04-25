"""
Packer subprocess wrapper for cloud image building.

Supports three builders:
  - amazon-ebs   (AWS AMI)
  - azure-arm    (Azure Managed Image)
  - googlecompute (GCP Custom Image)

Credentials are injected via environment variables so they never appear
in command-line arguments or on-disk templates.
"""
import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

BUILDS_DIR = Path(__file__).parent.parent.parent / "packer" / "builds"

class PackerError(Exception):
    pass


# ── Name sanitization ─────────────────────────────────────────────────────────

def _safe_ami_name(name: str) -> str:
    """Sanitize to AMI name charset: a-z A-Z 0-9 ( ) . - / _"""
    sanitized = re.sub(r"[^a-zA-Z0-9()./_\-]", "-", name)
    return sanitized[:100]


def _safe_gcp_name(name: str) -> str:
    """GCP image names: lowercase letters, digits, hyphens only, start with letter."""
    sanitized = re.sub(r"[^a-z0-9\-]", "-", name.lower())
    # Must start with a letter
    if sanitized and not sanitized[0].isalpha():
        sanitized = "img-" + sanitized
    return sanitized[:54]  # leave room for -timestamp suffix


def _safe_azure_name(name: str) -> str:
    """Azure managed image names allow letters, digits, dots, underscores, hyphens."""
    return re.sub(r"[^a-zA-Z0-9._\-]", "-", name)[:74]


# ── HCL2 template generators ──────────────────────────────────────────────────

def generate_aws_template(
    source_ami: str,
    instance_type: str,
    ssh_username: str,
    image_name: str,
    has_provisioner: bool,
) -> str:
    safe = _safe_ami_name(image_name)
    prov = '\n  provisioner "shell" {\n    script = "provision.sh"\n  }\n' if has_provisioner else ""
    return (
        'packer {\n'
        '  required_plugins {\n'
        '    amazon = {\n'
        '      version = ">= 1.2.0"\n'
        '      source  = "github.com/hashicorp/amazon"\n'
        '    }\n'
        '  }\n'
        '}\n\n'
        'variable "region" { default = "us-east-2" }\n\n'
        'source "amazon-ebs" "build" {\n'
        '  region        = var.region\n'
        '  source_ami    = "' + source_ami + '"\n'
        '  instance_type = "' + instance_type + '"\n'
        '  ssh_username  = "' + ssh_username + '"\n'
        '  ami_name      = "' + safe + '-{{timestamp}}"\n\n'
        '  tags = {\n'
        '    Name    = "' + image_name + '"\n'
        '    BuiltBy = "vm-dashboard"\n'
        '  }\n'
        '}\n\n'
        'build {\n'
        '  name    = "vm-dashboard"\n'
        '  sources = ["source.amazon-ebs.build"]\n'
        + prov +
        '}\n'
    )


def generate_azure_template(
    image_publisher: str,
    image_offer: str,
    image_sku: str,
    vm_size: str,
    image_name: str,
    has_provisioner: bool,
) -> str:
    safe = _safe_azure_name(image_name)
    # Azure requires waagent deprovision to generalize the image
    prov = (
        '\n  provisioner "shell" {\n'
        '    execute_command = "chmod +x {{ .Path }}; {{ .Vars }} sudo -E sh \'{{ .Path }}\'"\n'
        '    script          = "provision.sh"\n'
        '  }\n'
    ) if has_provisioner else ""
    deprovision = (
        '\n  provisioner "shell" {\n'
        '    execute_command = "chmod +x {{ .Path }}; {{ .Vars }} sudo -E sh \'{{ .Path }}\'"\n'
        '    inline          = ["/usr/sbin/waagent -force -deprovision+user && export HISTSIZE=0 && sync"]\n'
        '  }\n'
    )
    return (
        'packer {\n'
        '  required_plugins {\n'
        '    azure = {\n'
        '      version = ">= 1.4.0"\n'
        '      source  = "github.com/hashicorp/azure"\n'
        '    }\n'
        '  }\n'
        '}\n\n'
        '# ARM_CLIENT_ID, ARM_CLIENT_SECRET, ARM_TENANT_ID, ARM_SUBSCRIPTION_ID\n'
        '# are read from environment variables automatically.\n\n'
        'variable "resource_group" { default = "" }\n'
        'variable "location"       { default = "centralus" }\n\n'
        'source "azure-arm" "build" {\n'
        '  managed_image_name                = "' + safe + '-{{timestamp}}"\n'
        '  managed_image_resource_group_name = var.resource_group\n\n'
        '  os_type         = "Linux"\n'
        '  image_publisher = "' + image_publisher + '"\n'
        '  image_offer     = "' + image_offer + '"\n'
        '  image_sku       = "' + image_sku + '"\n\n'
        '  location = var.location\n'
        '  vm_size  = "' + vm_size + '"\n'
        '}\n\n'
        'build {\n'
        '  name    = "vm-dashboard"\n'
        '  sources = ["source.azure-arm.build"]\n'
        + prov + deprovision +
        '}\n'
    )


def generate_gcp_template(
    source_image: str,
    machine_type: str,
    ssh_username: str,
    image_name: str,
    project_id: str,
    zone: str,
    has_provisioner: bool,
) -> str:
    safe = _safe_gcp_name(image_name)
    prov = '\n  provisioner "shell" {\n    script = "provision.sh"\n  }\n' if has_provisioner else ""
    return (
        'packer {\n'
        '  required_plugins {\n'
        '    googlecompute = {\n'
        '      version = ">= 1.1.0"\n'
        '      source  = "github.com/hashicorp/googlecompute"\n'
        '    }\n'
        '  }\n'
        '}\n\n'
        '# GOOGLE_APPLICATION_CREDENTIALS points to the service account key file.\n\n'
        'source "googlecompute" "build" {\n'
        '  project_id   = "' + project_id + '"\n'
        '  zone         = "' + zone + '"\n'
        '  machine_type = "' + machine_type + '"\n'
        '  source_image = "' + source_image + '"\n'
        '  image_name   = "' + safe + '-{{timestamp}}"\n'
        '  ssh_username = "' + ssh_username + '"\n\n'
        '  image_labels = {\n'
        '    built-by = "vm-dashboard"\n'
        '  }\n'
        '}\n\n'
        'build {\n'
        '  name    = "vm-dashboard"\n'
        '  sources = ["source.googlecompute.build"]\n'
        + prov +
        '}\n'
    )


# ── Packer execution ──────────────────────────────────────────────────────────

async def _stream_command(
    args: list[str],
    cwd: Path,
    env: dict,
    on_line: Callable[[str], None],
) -> tuple[int, str]:
    """Run a command, streaming each output line to on_line. Returns (returncode, full_output)."""
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=env,
    )
    lines = []
    while True:
        raw = await proc.stdout.readline()
        if not raw:
            break
        line = raw.decode(errors="replace").rstrip()
        lines.append(line)
        on_line(line)
    await proc.wait()
    return proc.returncode, "\n".join(lines)


def _parse_artifact(cloud: str, output: str) -> Optional[str]:
    """
    Extract the artifact ID from machine-readable Packer output.
    Line format: timestamp,target,type,index,key,value
    """
    for line in output.splitlines():
        parts = line.split(",", 5)
        if len(parts) >= 6 and parts[2] == "artifact" and parts[3] == "0" and parts[4] == "id":
            raw_id = parts[5].strip()
            if cloud == "aws":
                # "us-east-2:ami-0abc123"  → "ami-0abc123"
                return raw_id.split(":")[-1] if ":" in raw_id else raw_id
            return raw_id
    return None


def _human_readable(line: str) -> Optional[str]:
    """
    Convert a machine-readable Packer CSV line to a human-readable message,
    or return None to suppress noisy/internal lines.
    """
    parts = line.split(",", 4)
    if len(parts) < 3:
        return line  # plain text (e.g. packer init)
    msg_type = parts[2]
    if msg_type == "ui" and len(parts) >= 5:
        subtype = parts[3]
        if subtype in ("say", "message", "error"):
            return parts[4].replace("%!(PACKER_COMMA)", ",")
    if msg_type == "error":
        return line
    return None


async def run_build(
    cloud: str,
    build_dir: Path,
    env: dict,
    on_progress: Callable[[int, str], None],
) -> dict[str, Any]:
    """
    Run `packer init` then `packer build` in build_dir.
    Calls on_progress(percent, message) with live log lines.
    Returns a dict with at minimum {"artifact_id": ...}.
    Raises PackerError on failure.
    """
    # Step 1: packer init (fast, validates/installs plugins)
    on_progress(8, "Running packer init…")
    rc, init_out = await _stream_command(
        ["packer", "init", "."],
        cwd=build_dir,
        env=env,
        on_line=lambda l: logger.debug("[packer init] %s", l),
    )
    if rc != 0:
        raise PackerError(f"packer init failed (exit {rc}):\n{init_out[-2000:]}")

    # Step 2: packer build
    on_progress(12, "Starting Packer build — this typically takes 5–15 minutes…")
    build_output: list[str] = []
    progress_pct = 12

    def _on_line(line: str) -> None:
        nonlocal progress_pct
        build_output.append(line)
        readable = _human_readable(line)
        if readable:
            # Advance progress slowly from 12 → 90 as lines arrive
            if progress_pct < 90:
                progress_pct = min(90, progress_pct + 1)
            on_progress(progress_pct, readable[:200])

    rc, _ = await _stream_command(
        ["packer", "build", "-machine-readable", "-on-error=abort", "."],
        cwd=build_dir,
        env=env,
        on_line=_on_line,
    )
    full_output = "\n".join(build_output)

    if rc != 0:
        # Surface the last meaningful error lines
        errors = [l for l in build_output[-60:] if l.strip()]
        raise PackerError(f"packer build failed (exit {rc}):\n" + "\n".join(errors[-20:]))

    artifact_id = _parse_artifact(cloud, full_output)
    on_progress(95, f"Build complete. Artifact: {artifact_id or '(see job log)'}")
    return {"artifact_id": artifact_id, "raw_output": full_output[-4000:]}


# ── Template archive helpers ──────────────────────────────────────────────────

async def archive_to_s3(
    template_path: Path,
    job_id: str,
    image_name: str,
    bucket: str,
    credentials: dict,
) -> str:
    """Upload the Packer template to S3. Returns the S3 URI."""
    import boto3
    key = f"packer-templates/{job_id}/{image_name}.pkr.hcl"

    def _upload():
        client = boto3.client(
            "s3",
            aws_access_key_id=credentials.get("aws_access_key_id"),
            aws_secret_access_key=credentials.get("aws_secret_access_key"),
            region_name=credentials.get("aws_region", "us-east-2"),
        )
        client.put_object(
            Bucket=bucket,
            Key=key,
            Body=template_path.read_bytes(),
            ContentType="text/plain",
        )
    await asyncio.to_thread(_upload)
    return f"s3://{bucket}/{key}"


async def archive_to_azure_blob(
    template_path: Path,
    job_id: str,
    image_name: str,
    storage_account: str,
    container: str,
    credentials: dict,
) -> str:
    """Upload the Packer template to Azure Blob Storage. Returns the blob URL."""
    from azure.identity import ClientSecretCredential
    from azure.storage.blob import BlobServiceClient

    blob_name = f"{job_id}/{image_name}.pkr.hcl"
    account_url = f"https://{storage_account}.blob.core.windows.net"

    def _upload():
        cred = ClientSecretCredential(
            tenant_id=credentials["azure_tenant_id"],
            client_id=credentials["azure_client_id"],
            client_secret=credentials["azure_client_secret"],
        )
        svc = BlobServiceClient(account_url=account_url, credential=cred)
        container_client = svc.get_container_client(container)
        try:
            container_client.create_container()
        except Exception:
            pass  # already exists
        blob_client = container_client.get_blob_client(blob_name)
        blob_client.upload_blob(template_path.read_bytes(), overwrite=True)
        return f"{account_url}/{container}/{blob_name}"

    return await asyncio.to_thread(_upload)


async def archive_to_gcs(
    template_path: Path,
    job_id: str,
    image_name: str,
    bucket: str,
    credentials: dict,
) -> str:
    """Upload the Packer template to Google Cloud Storage. Returns the GCS URI."""
    import json as _json
    from google.cloud import storage
    from google.oauth2 import service_account

    object_name = f"packer-templates/{job_id}/{image_name}.pkr.hcl"

    def _upload():
        sa_info = _json.loads(credentials["gcp_service_account_json"])
        creds = service_account.Credentials.from_service_account_info(
            sa_info,
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
        client = storage.Client(credentials=creds, project=sa_info.get("project_id"))
        gcs_bucket = client.bucket(bucket)
        blob = gcs_bucket.blob(object_name)
        blob.upload_from_string(template_path.read_bytes(), content_type="text/plain")
        return f"gs://{bucket}/{object_name}"

    return await asyncio.to_thread(_upload)
