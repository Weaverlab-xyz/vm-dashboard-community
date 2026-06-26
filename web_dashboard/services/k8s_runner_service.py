"""
Cross-cloud Kubernetes (kubectl/helm) runner orchestration.

Dispatches the dashboard's cluster-API operations (``kubectl apply``,
``kubectl delete``, ``helm …``, ``kubectl get secret``) either **in-process**
(the default — handled directly by ``k8s_service``'s subprocess helpers) or as a
one-shot transient **cloud task** that runs a stock kubectl+helm image with
clean egress to the cluster's API server. The cloud path side-steps a
TLS-inspecting corporate proxy that rejects (e.g. 526s) direct kubectl/helm to a
private-CA cluster API from the operator's machine.

Runners by cloud (mirror the Ansible + image-promote cloud-runner pattern):
  - AWS:   ECS Fargate task   (``aws_service.run_ecs_k8s_task``)
  - Azure: ACI container group (``azure_service.run_aci_k8s_task``)
  - GCP:   Cloud Run job       (``gcp_service.run_cloud_run_k8s_task``)

No custom runner image — the same stock ``dtzar/helm-kubectl`` image is used on
every cloud (operator override via ``k8s_runner_image``). The kubeconfig is
token-prepped server-side (``k8s_service._runner_kubeconfig`` swaps the cloud
exec-auth block for a static bearer token) and handed to the task base64-encoded
in a secure env var, so the throwaway container needs no cloud CLIs or creds.

The cloud infra (ECS cluster/subnet/SG/role, ACI rg/location/subnet/ACR, Cloud
Run region/VPC connector) is **shared** with the existing Ansible runner config
so single-tenant installs don't have to set anything new beyond picking a mode.
"""
import base64
import logging
from typing import Optional

from . import aws_service, azure_service, config_service, gcp_service
from ..config import settings

logger = logging.getLogger(__name__)


class K8sRunnerError(Exception):
    """Runner-side failure. Raised when the task can't be launched or completes
    non-zero. Carries the log tail (if any) so the caller can surface it to the
    operator."""

    def __init__(self, message: str, log_output: str = ""):
        super().__init__(message)
        self.log_output = log_output


def _cfg(key: str, fallback: str = "") -> str:
    return config_service.get(key) or getattr(settings, key, fallback)


def mode(target_cloud: str = "") -> str:
    """The runner mode for a target cluster's cloud: ``local`` (default) |
    ``ecs`` | ``aci`` | ``gcp``.

    Resolves the per-cloud key (``k8s_runner_aws`` / ``_azure`` / ``_gcp``) first,
    then the global ``k8s_runner``, then ``local``. ``target_cloud`` is the
    cluster's cloud (``aws`` / ``azure`` / ``gcp`` from ``K8sCluster.cloud``); a
    blank value uses the global setting (back-compat)."""
    tc = (target_cloud or "").strip().lower()
    per_cloud = _cfg(f"k8s_runner_{tc}") if tc in ("aws", "azure", "gcp") else ""
    return (per_cloud or _cfg("k8s_runner") or "local").strip().lower()


def _resolve_ecs() -> dict:
    """Resolve the AWS ECS knobs for the k8s runner, reusing the Ansible
    runner's ECS network plumbing as fallbacks. Raises ``K8sRunnerError``
    listing any missing required field so callers see one clear error instead of
    a boto3 TypeError later."""
    region = _cfg("aws_region") or "us-east-1"
    cluster = _cfg("ansible_ecs_cluster") or "bt-jumpoint"
    task_family = "k8s-runner"
    image = _cfg("k8s_runner_image") or "dtzar/helm-kubectl:latest"
    cpu = _cfg("ansible_ecs_cpu") or "256"
    memory = _cfg("ansible_ecs_memory") or "512"
    subnet_id = _cfg("ansible_ecs_subnet_id")
    sg_csv = _cfg("ansible_ecs_security_group_ids")
    sg_ids = [s.strip() for s in sg_csv.split(",") if s.strip()]
    execution_role_arn = _cfg("ansible_ecs_execution_role_arn")

    missing = []
    if not subnet_id:
        missing.append("ansible_ecs_subnet_id")
    if not execution_role_arn:
        missing.append("ansible_ecs_execution_role_arn")
    if missing:
        raise K8sRunnerError(
            "Kubernetes ECS runner is not configured. Set on /settings: "
            + ", ".join(missing) + "."
        )

    return {
        "region": region,
        "cluster": cluster,
        "task_family": task_family,
        "image": image,
        "cpu": cpu,
        "memory": memory,
        "subnet_id": subnet_id,
        "security_group_ids": sg_ids,
        "execution_role_arn": execution_role_arn,
    }


def _resolve_aci() -> dict:
    """Resolve the Azure ACI knobs for the k8s runner, reusing the dashboard's
    primary Azure resource group / location and the Ansible runner's ACI
    subnet / ACR creds as fallbacks."""
    rg = _cfg("azure_resource_group")
    location = _cfg("azure_location") or "centralus"
    subnet_id = _cfg("ansible_aci_subnet_id")
    image = _cfg("k8s_runner_image") or "dtzar/helm-kubectl:latest"
    acr_server = _cfg("ansible_aci_acr_server")
    acr_username = _cfg("ansible_aci_acr_username")
    acr_password = _cfg("ansible_aci_acr_password")

    missing = []
    if not rg:
        missing.append("azure_resource_group")
    if missing:
        raise K8sRunnerError(
            "Kubernetes ACI runner is not configured. Set on /settings: "
            + ", ".join(missing) + "."
        )

    return {
        "rg": rg,
        "location": location,
        "subnet_id": subnet_id,
        "image": image,
        "acr_server": acr_server,
        "acr_username": acr_username,
        "acr_password": acr_password,
    }


def _resolve_gcp() -> dict:
    """Resolve the GCP Cloud Run knobs for the k8s runner, reusing the existing
    gcp_* and Ansible runner Cloud Run keys as fallbacks."""
    project_id = _cfg("gcp_project_id")
    region = _cfg("gcp_region") or _cfg("gcp_ansible_cloud_run_region")
    image = _cfg("k8s_runner_image") or "dtzar/helm-kubectl:latest"
    vpc_connector = _cfg("gcp_ansible_vpc_connector")

    missing = []
    if not project_id:
        missing.append("gcp_project_id")
    if not region:
        missing.append("gcp_region (or gcp_ansible_cloud_run_region)")
    if missing:
        raise K8sRunnerError(
            "Kubernetes Cloud Run runner is not configured. Set on /settings: "
            + ", ".join(missing) + "."
        )

    return {
        "project_id": project_id,
        "region": region,
        "image": image,
        "vpc_connector": vpc_connector,
    }


async def run(
    *,
    kubeconfig: str,
    command: str,
    target_cloud: str = "",
    stdin_text: Optional[str] = None,
    job_id: str = "",
) -> str:
    """Run a kubectl/helm shell ``command`` against the cluster identified by
    ``kubeconfig`` as a one-shot cloud task, returning its combined stdout/logs.

    ``command`` is a ready-to-run shell string (e.g. ``kubectl apply -f -`` or
    ``helm repo add … && helm upgrade …``). ``KUBECONFIG`` is exported by the
    runner shell, so the command must not pass ``--kubeconfig``. ``stdin_text``,
    if given, is decoded and piped to the command's stdin (used to stream
    secret-bearing manifests / Helm values without touching disk).

    ``target_cloud`` (the cluster's cloud — ``aws`` / ``azure`` / ``gcp``) selects
    the backend via ``mode(target_cloud)``; blank uses the global ``k8s_runner``.

    Caller is expected to have already token-prepped ``kubeconfig`` via
    ``k8s_service._runner_kubeconfig``. Raises ``K8sRunnerError`` on a non-zero
    exit, carrying the log output.
    """
    m = mode(target_cloud)
    if m == "local":
        # The k8s_service helpers handle the local in-process path directly and
        # must not reach here; guard against a mis-wired caller.
        raise K8sRunnerError(
            "k8s_runner_service.run() called with mode='local' — the in-process "
            "path is handled by k8s_service, not the cloud runner."
        )

    kubeconfig_b64 = base64.b64encode(kubeconfig.encode()).decode()
    stdin_b64 = base64.b64encode(stdin_text.encode()).decode() if stdin_text is not None else ""

    if m == "ecs":
        cfg = _resolve_ecs()
        logger.info("Launching AWS ECS k8s runner task for job %s", job_id or "(adhoc)")
        exit_code, output = await aws_service.run_ecs_k8s_task(
            region=cfg["region"],
            cluster=cfg["cluster"],
            task_family=cfg["task_family"],
            image=cfg["image"],
            cpu=cfg["cpu"],
            memory=cfg["memory"],
            subnet_id=cfg["subnet_id"],
            security_group_ids=cfg["security_group_ids"],
            execution_role_arn=cfg["execution_role_arn"],
            command=command,
            kubeconfig_b64=kubeconfig_b64,
            stdin_b64=stdin_b64,
            job_id=job_id,
        )
    elif m == "aci":
        cfg = _resolve_aci()
        logger.info("Launching Azure ACI k8s runner task for job %s", job_id or "(adhoc)")
        exit_code, output = await azure_service.run_aci_k8s_task(
            rg=cfg["rg"],
            location=cfg["location"],
            subnet_id=cfg["subnet_id"],
            image=cfg["image"],
            command=command,
            kubeconfig_b64=kubeconfig_b64,
            stdin_b64=stdin_b64,
            job_id=job_id,
            acr_server=cfg["acr_server"],
            acr_username=cfg["acr_username"],
            acr_password=cfg["acr_password"],
        )
    elif m == "gcp":
        cfg = _resolve_gcp()
        logger.info("Launching GCP Cloud Run k8s runner job for job %s", job_id or "(adhoc)")
        exit_code, output = await gcp_service.run_cloud_run_k8s_task(
            project_id=cfg["project_id"],
            region=cfg["region"],
            image=cfg["image"],
            command=command,
            kubeconfig_b64=kubeconfig_b64,
            stdin_b64=stdin_b64,
            job_id=job_id,
            vpc_connector=cfg["vpc_connector"],
        )
    else:
        raise K8sRunnerError(f"Unknown k8s_runner mode: {m!r}")

    if exit_code != 0:
        raise K8sRunnerError(f"k8s runner exited {exit_code}", log_output=output)
    return output
