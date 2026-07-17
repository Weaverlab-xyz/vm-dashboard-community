"""
Cross-cloud image-promote runner orchestration.

Dispatches to a per-target-cloud transient task that converts (if needed)
and uploads a VM image artefact from the hub into the target cloud's
storage. The cloud SDK image-import call then consumes the local URL,
side-stepping AWS/GCP's "import source must be on our own storage"
constraint.

Runners by target cloud:
  - AWS:   ECS Fargate task
  - Azure: ACI container group
  - GCP:   Cloud Run job

Each runner uses the same public image (`chrweav/dashboard-promote-runner`
by default; operator override via `promote_runner_image`). The runner reads
the hub artefact via a short-lived presigned URL the dashboard mints at
task-launch time — no source-side credentials live in the container.
"""
import logging
from typing import Optional

from . import aws_service, azure_service, config_service, gcp_service, oci_service, storage_service
from ..config import settings

logger = logging.getLogger(__name__)


class PromoteRunnerError(Exception):
    """Runner-side failure. Raised when the task can't be launched or
    completed non-zero. Carries the log tail (if any) so the caller can
    surface it to the operator."""

    def __init__(self, message: str, log_output: str = ""):
        super().__init__(message)
        self.log_output = log_output


def _cfg(key: str, fallback: str = "") -> str:
    return config_service.get(key) or getattr(settings, key, fallback)


def _resolve_aws_runner_config() -> dict:
    """Pull the AWS promote-runner ECS knobs, falling back to the existing
    Ansible-runner knobs where it makes sense to share network plumbing.
    Returns a dict; raises PromoteRunnerError if any required field is
    missing so callers see one clear error instead of a TypeError from
    boto3 later."""
    cluster = _cfg("promote_runner_ecs_cluster") or _cfg("ansible_ecs_cluster") or "bt-jumpoint"
    task_family = _cfg("promote_runner_ecs_task_family") or "promote-runner"
    image = _cfg("promote_runner_image") or "chrweav/dashboard-promote-runner:latest"
    cpu = _cfg("promote_runner_ecs_cpu") or "1024"
    memory = _cfg("promote_runner_ecs_memory") or "4096"
    subnet_id = _cfg("promote_runner_ecs_subnet_id") or _cfg("ansible_ecs_subnet_id")
    sg_csv = _cfg("promote_runner_ecs_security_group_ids") or _cfg("ansible_ecs_security_group_ids")
    sg_ids = [s.strip() for s in sg_csv.split(",") if s.strip()]
    execution_role_arn = _cfg("promote_runner_ecs_execution_role_arn") or _cfg("ansible_ecs_execution_role_arn")
    task_role_arn = _cfg("promote_runner_ecs_task_role_arn")

    missing = []
    if not subnet_id:
        missing.append("promote_runner_ecs_subnet_id (or ansible_ecs_subnet_id)")
    if not execution_role_arn:
        missing.append("promote_runner_ecs_execution_role_arn (or ansible_ecs_execution_role_arn)")
    if not task_role_arn:
        missing.append("promote_runner_ecs_task_role_arn (S3 write to the staging bucket)")
    if missing:
        raise PromoteRunnerError(
            "Promote runner is not configured. Set on /storage: " + ", ".join(missing) + "."
        )

    return {
        "cluster": cluster,
        "task_family": task_family,
        "image": image,
        "cpu": cpu,
        "memory": memory,
        "subnet_id": subnet_id,
        "security_group_ids": sg_ids,
        "execution_role_arn": execution_role_arn,
        "task_role_arn": task_role_arn,
    }


def resolve_aws_staging(image_name: str, version: str) -> tuple[str, str]:
    """Return (bucket, key) for where the AWS promote runner should drop the
    converted artefact. Defaults to `storage_s3_bucket` under the
    `promote-staging/` prefix so operators don't need to provision a
    separate bucket per cloud."""
    bucket = _cfg("promote_runner_aws_staging_bucket") or _cfg("storage_s3_bucket")
    if not bucket:
        raise PromoteRunnerError(
            "No S3 staging bucket configured. Set promote_runner_aws_staging_bucket "
            "or storage_s3_bucket on /storage."
        )
    prefix = (_cfg("promote_runner_aws_staging_prefix") or "promote-staging").strip("/")
    key = f"{prefix}/{image_name}-{version}.vhd"
    return (bucket, key)


# ── AWS target ───────────────────────────────────────────────────────────────

async def run_for_aws_target(
    *,
    job_id: str,
    hub_backend: str,
    hub_key: str,
    source_format: str,
    target_format: str,
    dest_bucket: str,
    dest_key: str,
    aws_region: str,
    install_aws_guest_env: bool = False,
    presign_expiry_seconds: int = 7200,
) -> tuple:
    """Launch the AWS promote-runner ECS task to copy `hub_backend://hub_key`
    into `s3://dest_bucket/dest_key`, converting format along the way if
    requested. Returns (exit_code, log_output).

    The runner pulls the hub artefact via a presigned URL minted here so the
    container never holds hub-side credentials. Default expiry is 2 hours
    which comfortably covers the multi-GB transfer + qemu-img convert step.
    """
    cfg = _resolve_aws_runner_config()

    source_url = await storage_service.presigned_url(
        hub_backend, hub_key, expiry_seconds=presign_expiry_seconds, method="GET",
    )

    runner_args = [
        "--source-url", source_url,
        "--source-format", source_format,
        "--target-format", target_format,
        "--target", "s3",
        "--dest-s3-bucket", dest_bucket,
        "--dest-s3-key", dest_key,
        "--dest-s3-region", aws_region,
    ]
    # Foreign Linux images (esp. GCP-built, which ship no cloud-init) don't
    # consume the EC2 UserData on AWS — no launch-key injection, no SSM-agent
    # install. Baking an Ec2-datasource cloud-init in during promotion fixes that.
    # Windows images provision via their own agent, so skip.
    if install_aws_guest_env:
        runner_args.append("--install-aws-guest-env")

    logger.info(
        "Launching AWS promote-runner ECS task for job %s: hub=%s://%s -> s3://%s/%s",
        job_id, hub_backend, hub_key, dest_bucket, dest_key,
    )
    exit_code, output = await aws_service.run_promote_runner_ecs(
        region=aws_region,
        cluster=cfg["cluster"],
        task_family=cfg["task_family"],
        image=cfg["image"],
        cpu=cfg["cpu"],
        memory=cfg["memory"],
        subnet_id=cfg["subnet_id"],
        security_group_ids=cfg["security_group_ids"],
        execution_role_arn=cfg["execution_role_arn"],
        task_role_arn=cfg["task_role_arn"],
        runner_args=runner_args,
        job_id=job_id,
    )
    if exit_code != 0:
        raise PromoteRunnerError(
            f"Promote runner exited with code {exit_code}. See log_output for details.",
            log_output=output,
        )
    return (exit_code, output)


# ── Azure target ─────────────────────────────────────────────────────────────


def _resolve_azure_runner_config() -> dict:
    """Resolve ACI plumbing + dest staging knobs for the Azure promote runner.
    Falls back to the dashboard's primary Azure resource group / location /
    storage account so single-tenant installs Just Work without setting any
    new keys."""
    rg = _cfg("promote_runner_azure_resource_group") or _cfg("azure_resource_group")
    location = _cfg("promote_runner_azure_location") or _cfg("azure_location") or "centralus"
    subnet_id = _cfg("promote_runner_azure_subnet_id")
    image = _cfg("promote_runner_image") or "chrweav/dashboard-promote-runner:latest"
    try:
        cpu = float(_cfg("promote_runner_azure_cpu") or "2")
        memory_gb = float(_cfg("promote_runner_azure_memory_gb") or "4")
    except ValueError as e:
        raise PromoteRunnerError(f"Invalid CPU/memory in promote runner config: {e}")

    staging_account = _cfg("promote_runner_azure_staging_account") or _cfg("storage_azure_account")
    staging_container = (
        _cfg("promote_runner_azure_staging_container")
        or _cfg("storage_azure_container")
        or "playbooks"
    )
    staging_prefix = (_cfg("promote_runner_azure_staging_prefix") or "promote-staging").strip("/")

    # ACR creds for pulling the runner image when operators host a private build.
    acr_server = _cfg("azure_acr_server")
    acr_username = _cfg("azure_acr_username")
    acr_password = _cfg("azure_acr_password")

    missing = []
    if not rg:
        missing.append("promote_runner_azure_resource_group (or azure_resource_group)")
    if not staging_account:
        missing.append("promote_runner_azure_staging_account (or storage_azure_account)")
    if missing:
        raise PromoteRunnerError(
            "Azure promote runner is not configured. Set on /storage: "
            + ", ".join(missing) + "."
        )

    return {
        "rg": rg,
        "location": location,
        "subnet_id": subnet_id,
        "image": image,
        "cpu": cpu,
        "memory_gb": memory_gb,
        "staging_account": staging_account,
        "staging_container": staging_container,
        "staging_prefix": staging_prefix,
        "acr_server": acr_server,
        "acr_username": acr_username,
        "acr_password": acr_password,
    }


def resolve_azure_staging(image_name: str, version: str) -> tuple[str, str, str]:
    """Return (storage_account, container, blob_name) for where the Azure
    promote runner should drop the converted VHD before image-create."""
    cfg = _resolve_azure_runner_config()
    blob_name = f"{cfg['staging_prefix']}/{image_name}-{version}.vhd"
    return (cfg["staging_account"], cfg["staging_container"], blob_name)


async def run_for_azure_target(
    *,
    job_id: str,
    hub_backend: str,
    hub_key: str,
    source_format: str,
    target_format: str,
    dest_account: str,
    dest_container: str,
    dest_blob: str,
    install_linux_agent: bool = False,
    presign_expiry_seconds: int = 7200,
) -> tuple:
    """Launch the Azure promote-runner ACI container group to copy
    `hub_backend://hub_key` into `https://<dest_account>.blob.core.windows.net/
    <dest_container>/<dest_blob>`, converting format if needed. Returns
    (exit_code, log_output).

    Azure SP credentials (tenant/client/secret) are passed to the runner as
    secure env vars so the container can write to the dest blob via the
    same identity the dashboard uses elsewhere — no extra IAM plumbing.
    """
    cfg = _resolve_azure_runner_config()

    source_url = await storage_service.presigned_url(
        hub_backend, hub_key, expiry_seconds=presign_expiry_seconds, method="GET",
    )

    runner_args = [
        "--source-url", source_url,
        "--source-format", source_format,
        "--target-format", target_format,
        "--target", "azure",
        "--dest-azure-account", dest_account,
        "--dest-azure-container", dest_container,
        "--dest-azure-blob", dest_blob,
    ]
    # Linux images need the Azure Linux Agent baked in or the VM never finishes
    # Azure OS provisioning (deploy hangs). Windows images bring their own agent.
    if install_linux_agent:
        runner_args.append("--install-linux-agent")
    azure_env = {
        "AZURE_TENANT_ID":     _cfg("azure_tenant_id"),
        "AZURE_CLIENT_ID":     _cfg("azure_client_id"),
        "AZURE_CLIENT_SECRET": _cfg("azure_client_secret"),
    }
    if not all(azure_env.values()):
        raise PromoteRunnerError(
            "Azure service-principal credentials (azure_tenant_id / azure_client_id / "
            "azure_client_secret) must be set so the runner can authenticate to the "
            "dest storage account."
        )

    logger.info(
        "Launching Azure promote-runner ACI for job %s: hub=%s://%s -> "
        "https://%s.blob.core.windows.net/%s/%s",
        job_id, hub_backend, hub_key, dest_account, dest_container, dest_blob,
    )
    exit_code, output = await azure_service.run_aci_promote_runner_task(
        rg=cfg["rg"],
        location=cfg["location"],
        subnet_id=cfg["subnet_id"],
        image=cfg["image"],
        cpu=cfg["cpu"],
        memory_gb=cfg["memory_gb"],
        runner_args=runner_args,
        azure_env=azure_env,
        job_id=job_id,
        acr_server=cfg["acr_server"],
        acr_username=cfg["acr_username"],
        acr_password=cfg["acr_password"],
    )
    if exit_code != 0:
        raise PromoteRunnerError(
            f"Promote runner exited with code {exit_code}. See log_output for details.",
            log_output=output,
        )
    return (exit_code, output)


# ── OCI target ───────────────────────────────────────────────────────────────


def _resolve_oci_runner_config() -> dict:
    """Resolve OCI Container-Instances + staging knobs for the OCI promote runner.
    Falls back to the primary oci_* config so single-tenant installs need only set
    a staging bucket."""
    compartment = _cfg("promote_runner_oci_compartment") or _cfg("oci_compartment_ocid") or _cfg("oci_tenancy_ocid")
    subnet_ocid = _cfg("promote_runner_oci_subnet_ocid") or _cfg("oci_default_subnet_ocid")
    availability_domain = _cfg("promote_runner_oci_availability_domain")
    image = _cfg("promote_runner_image") or "chrweav/dashboard-promote-runner:latest"
    region = _cfg("oci_region") or "us-ashburn-1"
    try:
        ocpus = float(_cfg("promote_runner_oci_ocpus") or "2")
        memory_gbs = float(_cfg("promote_runner_oci_memory_gbs") or "16")
    except ValueError as e:
        raise PromoteRunnerError(f"Invalid OCPU/memory in OCI promote runner config: {e}")

    staging_bucket = _cfg("promote_runner_oci_staging_bucket")
    staging_prefix = (_cfg("promote_runner_oci_staging_prefix") or "promote-staging").strip("/")

    missing = []
    if not compartment:
        missing.append("promote_runner_oci_compartment (or oci_compartment_ocid)")
    if not subnet_ocid:
        missing.append("promote_runner_oci_subnet_ocid (or oci_default_subnet_ocid — the runner VNIC)")
    if not staging_bucket:
        missing.append("promote_runner_oci_staging_bucket (Object Storage bucket for the QCOW2)")
    if missing:
        raise PromoteRunnerError(
            "OCI promote runner is not configured. Set on /storage: " + ", ".join(missing) + "."
        )

    return {
        "compartment": compartment,
        "subnet_ocid": subnet_ocid,
        "availability_domain": availability_domain,
        "image": image,
        "region": region,
        "ocpus": ocpus,
        "memory_gbs": memory_gbs,
        "staging_bucket": staging_bucket,
        "staging_prefix": staging_prefix,
    }


def resolve_oci_staging(image_name: str, version: str) -> tuple[str, str]:
    """Return (bucket, object_name) for the QCOW2 the OCI promote runner stages in
    Object Storage before the compute-image import reads it. (The namespace is
    fetched live by the orchestrator.)"""
    cfg = _resolve_oci_runner_config()
    object_name = f"{cfg['staging_prefix']}/{image_name}-{version}.qcow2"
    return (cfg["staging_bucket"], object_name)


async def run_for_oci_target(
    *,
    job_id: str,
    hub_backend: str,
    hub_key: str,
    source_format: str,
    dest_namespace: str,
    dest_bucket: str,
    dest_object: str,
    presign_expiry_seconds: int = 7200,
) -> tuple:
    """Launch the OCI promote-runner Container Instance to copy
    `hub_backend://hub_key` into `oci://<namespace>/<bucket>/<object>`, converting
    to QCOW2 (OCI's custom-image import format) along the way. Returns
    (exit_code, log_output).

    OCI API-key credentials are passed to the runner as secure env vars (the
    OCI_CLI_* names the runner's upload_oci reads) so the container writes to
    Object Storage as the same identity the dashboard uses."""
    cfg = _resolve_oci_runner_config()

    source_url = await storage_service.presigned_url(
        hub_backend, hub_key, expiry_seconds=presign_expiry_seconds, method="GET",
    )

    runner_args = [
        "--source-url", source_url,
        "--source-format", source_format,
        # OCI custom-image import reads QCOW2 (or VMDK); always hand it QCOW2.
        "--target-format", "qcow2",
        "--target", "oci",
        "--dest-oci-namespace", dest_namespace,
        "--dest-oci-bucket", dest_bucket,
        "--dest-oci-object", dest_object,
        "--dest-oci-region", cfg["region"],
    ]
    oci_env = {
        "OCI_CLI_USER":        _cfg("oci_user_ocid"),
        "OCI_CLI_TENANCY":     _cfg("oci_tenancy_ocid"),
        "OCI_CLI_FINGERPRINT": _cfg("oci_fingerprint"),
        "OCI_CLI_KEY_CONTENT": _cfg("oci_private_key"),
        "OCI_CLI_REGION":      cfg["region"],
        "OCI_CLI_PASSPHRASE":  _cfg("oci_private_key_passphrase"),
    }
    if not all(oci_env[k] for k in ("OCI_CLI_USER", "OCI_CLI_TENANCY", "OCI_CLI_FINGERPRINT", "OCI_CLI_KEY_CONTENT")):
        raise PromoteRunnerError(
            "OCI API-key credentials (oci_tenancy_ocid / oci_user_ocid / oci_fingerprint / "
            "oci_private_key) must be set so the runner can write to Object Storage."
        )

    # Resolve the AD if not pinned in config (Container Instances need one).
    availability_domain = cfg["availability_domain"]
    if not availability_domain:
        ads = await oci_service.list_availability_domains(cfg["compartment"])
        if not ads:
            raise PromoteRunnerError("No availability domains found for the OCI promote runner compartment.")
        availability_domain = ads[0]

    logger.info(
        "Launching OCI promote-runner Container Instance for job %s: hub=%s://%s -> oci://%s/%s/%s",
        job_id, hub_backend, hub_key, dest_namespace, dest_bucket, dest_object,
    )
    exit_code, output = await oci_service.run_container_instance_promote_runner_task(
        compartment_id=cfg["compartment"],
        availability_domain=availability_domain,
        subnet_ocid=cfg["subnet_ocid"],
        image=cfg["image"],
        runner_args=runner_args,
        env=oci_env,
        job_id=job_id,
        ocpus=cfg["ocpus"],
        memory_gbs=cfg["memory_gbs"],
    )
    if exit_code != 0:
        raise PromoteRunnerError(
            f"Promote runner exited with code {exit_code}. See log_output for details.",
            log_output=output,
        )
    return (exit_code, output)


# ── GCP target ───────────────────────────────────────────────────────────────


def _resolve_gcp_runner_config() -> dict:
    """Resolve Cloud Run + staging knobs for the GCP promote runner. Falls
    back to existing gcp_* and storage_gcs_* keys so single-tenant installs
    don't have to set anything beyond enabling a runner-capable SA."""
    project_id = _cfg("gcp_project_id")
    region = _cfg("promote_runner_gcp_region") or _cfg("gcp_region")
    image = _cfg("promote_runner_image") or "chrweav/dashboard-promote-runner:latest"
    # 16Gi / 4 vCPU default: the GCP path materialises source.vhd + a full raw +
    # the tar.gz in Cloud Run's memory-backed /tmp, which OOMs at the old 4Gi
    # default for any real multi-GB image. Cloud Run requires >=4 vCPU for >8Gi.
    cpu = _cfg("promote_runner_gcp_cpu") or "4"
    memory = _cfg("promote_runner_gcp_memory") or "16Gi"
    vpc_connector = _cfg("promote_runner_gcp_vpc_connector")
    sa_email = _cfg("promote_runner_gcp_service_account")

    staging_bucket = _cfg("promote_runner_gcp_staging_bucket") or _cfg("storage_gcs_bucket")
    staging_prefix = (_cfg("promote_runner_gcp_staging_prefix") or "promote-staging").strip("/")

    missing = []
    if not project_id:
        missing.append("gcp_project_id")
    if not region:
        missing.append("promote_runner_gcp_region (or gcp_region)")
    if not staging_bucket:
        missing.append("promote_runner_gcp_staging_bucket (or storage_gcs_bucket)")
    if missing:
        raise PromoteRunnerError(
            "GCP promote runner is not configured. Set on /storage: "
            + ", ".join(missing) + "."
        )

    return {
        "project_id": project_id,
        "region": region,
        "image": image,
        "cpu": cpu,
        "memory": memory,
        "vpc_connector": vpc_connector,
        "service_account_email": sa_email,
        "staging_bucket": staging_bucket,
        "staging_prefix": staging_prefix,
    }


def resolve_gcp_staging(image_name: str, version: str) -> tuple[str, str]:
    """Return (bucket, object_name) for where the GCP promote runner should
    drop the wrapped tar.gz before images.insert consumes it. The `.tar.gz`
    extension is significant — the runner produces a gzip-tar containing
    `disk.raw` and GCP image-insert requires that exact shape."""
    cfg = _resolve_gcp_runner_config()
    object_name = f"{cfg['staging_prefix']}/{image_name}-{version}.tar.gz"
    return (cfg["staging_bucket"], object_name)


async def run_for_gcp_target(
    *,
    job_id: str,
    hub_backend: str,
    hub_key: str,
    source_format: str,
    dest_bucket: str,
    dest_object: str,
    install_guest_agent: bool = False,
    presign_expiry_seconds: int = 7200,
) -> tuple:
    """Launch the Cloud Run promote-runner job to copy `hub_backend://hub_key`
    into `gs://dest_bucket/dest_object`, converting vhd → raw and tar.gz-
    wrapping along the way. Returns (exit_code, log_output).

    `dest_object` must end in `.tar.gz` because GCP's images.insert reads
    the source as a gzip-tar with a single `disk.raw` entry. The runner
    handles the tar wrap automatically when `--target gcs --target-format raw`.
    Caller (orchestrator) is responsible for naming the object accordingly.
    """
    cfg = _resolve_gcp_runner_config()

    source_url = await storage_service.presigned_url(
        hub_backend, hub_key, expiry_seconds=presign_expiry_seconds, method="GET",
    )

    runner_args = [
        "--source-url", source_url,
        "--source-format", source_format,
        # Forced to "raw" for GCP — images.insert source must be a gzip-tar
        # whose single entry is `disk.raw`. The runner does the tar wrap.
        "--target-format", "raw",
        "--target", "gcs",
        "--dest-gcs-bucket", dest_bucket,
        "--dest-gcs-object", dest_object,
    ]
    # Linux images need google-guest-agent baked in or a promoted foreign image
    # never applies ssh-keys metadata (key-based SSH silently fails). Windows
    # images use the separate GCEWindowsAgent, so skip.
    if install_guest_agent:
        runner_args.append("--install-gcp-guest-agent")

    logger.info(
        "Launching GCP promote-runner Cloud Run job for %s: hub=%s://%s -> gs://%s/%s",
        job_id, hub_backend, hub_key, dest_bucket, dest_object,
    )
    exit_code, output = await gcp_service.run_cloud_run_promote_runner_task(
        project_id=cfg["project_id"],
        region=cfg["region"],
        image=cfg["image"],
        runner_args=runner_args,
        job_id=job_id,
        cpu=cfg["cpu"],
        memory=cfg["memory"],
        vpc_connector=cfg["vpc_connector"],
        service_account_email=cfg["service_account_email"],
    )
    if exit_code != 0:
        raise PromoteRunnerError(
            f"Promote runner exited with code {exit_code}. See log_output for details.",
            log_output=output,
        )
    return (exit_code, output)
