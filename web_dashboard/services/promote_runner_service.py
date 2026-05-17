"""
Cross-cloud image-promote runner orchestration.

Dispatches to a per-target-cloud transient task that converts (if needed)
and uploads a VM image artefact from the hub into the target cloud's
storage. The cloud SDK image-import call then consumes the local URL,
side-stepping AWS/GCP's "import source must be on our own storage"
constraint.

Runners by target cloud:
  - AWS:   ECS Fargate task (this PR)
  - Azure: ACI container group  (TODO, PR 4)
  - GCP:   Cloud Run job        (TODO, PR 5)

Each runner uses the same public image (`weaverlab-xyz/dashboard-promote-runner`
by default; operator override via `promote_runner_image`). The runner reads
the hub artefact via a short-lived presigned URL the dashboard mints at
task-launch time — no source-side credentials live in the container.
"""
import logging
from typing import Optional

from . import aws_service, config_service, storage_service
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
    image = _cfg("promote_runner_image") or "weaverlab-xyz/dashboard-promote-runner:latest"
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
