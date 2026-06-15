"""
Shared BeyondTrust Jumpoint host — on-demand EC2 lifecycle.

The tunnel-capable Jumpoint runs on an ECS-on-EC2 container instance (Fargate
can't do protocol tunneling — see aws_service / config bt_ecs_launch_type). To
avoid a standing ~$15/mo host, the dashboard manages ONE shared host's lifecycle
by reference count:

  * ensure_jumpoint_host(region)          — called when an AWS EC2 instance or a
                                            cloud database is provisioned. Creates
                                            the host (idempotent, tag find-or-create)
                                            and the jumpoint task on it.
  * teardown_jumpoint_host_if_idle(db, …) — called on EC2 destroy / DB decommission.
                                            Terminates the host only when nothing
                                            (no managed EC2 instance, no active DB)
                                            is left using it.

Prereqs (one-time, created by scripts/sandbox/Linux/setup-aws.sh): the
``ecsInstanceRole`` + instance profile, the bt-jumpoint cluster, the public
subnet + jumpoint SG, and the dashboard IAM user's ssm:GetParameter* /
iam:PassRole(ecsInstanceRole) / ecs:*ContainerInstances permissions. Everything
here is best-effort from the caller's perspective; failures log and leave the
DB/EC2 resource intact (the tunnel/jump is just unavailable until fixed).
"""
import asyncio
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

# ECS-optimized Amazon Linux 2023 AMI — public SSM parameter (the agent + tun
# module ship in it). Mirrors scripts/sandbox/Linux/setup-aws.sh.
_ECS_AMI_SSM = "/aws/service/ecs/optimized-ami/amazon-linux-2023/recommended/image_id"

_REGISTER_TIMEOUT_S = 180   # wait for the EC2 host to register as an ECS container instance
_REGISTER_POLL_S = 10


def _cfg(key: str) -> str:
    from . import config_service
    val = config_service.get(key)
    if val:
        return val
    from ..config import settings
    return getattr(settings, key, "") or ""


async def _resolve_deploy_key() -> str:
    """BeyondTrust Jumpoint Docker deploy key — direct config field first, then
    the legacy Password-Safe title (same resolution as the EC2/RDS paths)."""
    direct = _cfg("aws_ecs_docker_deploy_key")
    if direct:
        return direct
    title = _cfg("bt_ps_deploy_key_title")
    if title:
        from . import btapi_service
        try:
            return await btapi_service.get_ps_secret(title)
        except Exception as exc:
            logger.warning("jumpoint-host: deploy key fetch from Password Safe failed: %s", exc)
    return ""


async def _ensure_task(region: str, deploy_key: str) -> None:
    """Run the jumpoint task on the cluster if none is live (host must already
    have capacity). Mirrors the old cloud_database_service._ensure_jumpoint_node
    task half."""
    from . import aws_service
    cluster = _cfg("bt_ecs_cluster")
    family = _cfg("bt_ecs_task_family")
    launch_type = (_cfg("bt_ecs_launch_type") or "EC2").upper()
    tasks = await aws_service.list_ecs_tasks(region, cluster)
    live = [t for t in tasks
            if t.get("lastStatus") in ("PROVISIONING", "PENDING", "RUNNING")
            and f"task-definition/{family}:" in (t.get("taskDefinitionArn") or "")]
    if live:
        logger.info("jumpoint-host: task already live (%d) in cluster %s", len(live), cluster)
        return
    arn = await aws_service.run_ecs_jumpoint_task(
        region=region, cluster=cluster, task_family=family,
        subnet_id=_cfg("bt_ecs_jumpoint_subnet_id"),
        security_group_ids=[s.strip() for s in _cfg("bt_ecs_jumpoint_security_group_id").split(",") if s.strip()],
        deploy_key=deploy_key, cpu=_cfg("bt_ecs_cpu"), memory=_cfg("bt_ecs_memory"),
        execution_role_arn=_cfg("bt_ecs_execution_role_arn"), image=_cfg("bt_ecs_image"),
        launch_type=launch_type,
    )
    logger.info("jumpoint-host: started jumpoint task %s — registers with PRA in ~1-2 min",
                arn.split("/")[-1])


async def ensure_jumpoint_host(cloud: str, region: str) -> Optional[str]:
    """Ensure the shared tunnel-capable Jumpoint host is up for ``cloud``; return
    its instance/host id (or None). Dispatches per cloud — AWS uses ECS-on-EC2,
    GCP uses a privileged container on a COS GCE VM. Best-effort for callers."""
    if cloud == "gcp":
        return await _ensure_jumpoint_host_gcp(region)
    if cloud == "azure":
        return await _ensure_jumpoint_host_azure(region)
    return await _ensure_jumpoint_host_aws(region)


async def _ensure_jumpoint_host_aws(region: str) -> Optional[str]:
    """Ensure the shared AWS Jumpoint host (and its task) is up; return its
    instance id (or None on the FARGATE escape hatch / when nothing was created).
    Raises AWSError on failure — callers treat this as best-effort."""
    from . import aws_service
    deploy_key = await _resolve_deploy_key()
    if not deploy_key:
        logger.warning("jumpoint-host: aws_ecs_docker_deploy_key not set — cannot start a "
                       "jumpoint; tunnels/jumps will be unavailable until configured.")
        return None

    launch_type = (_cfg("bt_ecs_launch_type") or "EC2").upper()
    if launch_type != "EC2":
        # Legacy Fargate: no host to manage, just run the task.
        await _ensure_task(region, deploy_key)
        return None

    name = _cfg("bt_ecs_host_name") or "dashboard-sandbox-jumpoint-host"
    existing = await aws_service.find_instances_by_tag(
        region, name_tag=name, states=["pending", "running"])
    if existing:
        logger.info("jumpoint-host: reusing host %s", existing[0]["instance_id"])
        await _ensure_task(region, deploy_key)
        return existing[0]["instance_id"]

    # Create the host. Re-check the tag right before launch to shrink the
    # find-or-create race (acceptable residual window for a single-operator lab).
    ami_id = await aws_service.get_ssm_parameter(region, _ECS_AMI_SSM)
    user_data = (f"#!/bin/bash\n"
                 f"echo \"ECS_CLUSTER={_cfg('bt_ecs_cluster')}\" >> /etc/ecs/ecs.config\n"
                 f"modprobe tun || true\n")
    recheck = await aws_service.find_instances_by_tag(region, name_tag=name, states=["pending", "running"])
    if recheck:
        logger.info("jumpoint-host: host appeared concurrently (%s) — reusing",
                    recheck[0]["instance_id"])
        await _ensure_task(region, deploy_key)
        return recheck[0]["instance_id"]

    inst = await aws_service.run_container_instance(
        region,
        ami_id=ami_id,
        instance_type=_cfg("bt_ecs_host_instance_type") or "t3.small",
        subnet_id=_cfg("bt_ecs_jumpoint_subnet_id"),
        security_group_ids=[s.strip() for s in _cfg("bt_ecs_jumpoint_security_group_id").split(",") if s.strip()],
        instance_profile=_cfg("bt_ecs_host_instance_profile") or "ecsInstanceRole",
        user_data=user_data,
        name_tag=name,
    )
    host_id = inst["instance_id"]
    logger.info("jumpoint-host: launched host %s (%s) — awaiting ECS registration",
                host_id, _cfg("bt_ecs_host_instance_type") or "t3.small")

    # Wait for the instance to register with the cluster before running the task.
    cluster = _cfg("bt_ecs_cluster")
    deadline = time.monotonic() + _REGISTER_TIMEOUT_S
    while time.monotonic() < deadline:
        ci = await aws_service.list_container_instances(region, cluster)
        if any(c.get("status") == "ACTIVE" for c in ci):
            break
        await asyncio.sleep(_REGISTER_POLL_S)
    else:
        logger.warning("jumpoint-host: host %s did not register within %ds — attempting the "
                       "task anyway", host_id, _REGISTER_TIMEOUT_S)
    await _ensure_task(region, deploy_key)
    return host_id


def _active_db_count(db, cloud: Optional[str] = None) -> int:
    from ..database import CloudDatabase
    q = (db.query(CloudDatabase)
           .filter(CloudDatabase.status.in_(["available", "provisioning"])))
    if cloud:
        q = q.filter(CloudDatabase.cloud == cloud)
    return q.count()


def _active_ec2_count(db) -> int:
    # Mirrors the sibling count in api/aws.py:_run_destroy — completed ec2_deploy
    # jobs not yet marked destroyed.
    from ..database import Job
    jobs = db.query(Job).filter(Job.job_type == "ec2_deploy", Job.status == "completed").all()
    return sum(1 for j in jobs if not (j.metadata_dict or {}).get("destroyed"))


async def teardown_jumpoint_host_if_idle(db, cloud: str, region: str) -> None:
    """Terminate the shared Jumpoint host for ``cloud`` iff nothing is left using
    it. Dispatches per cloud. Best-effort; logs and returns on error."""
    if cloud == "gcp":
        return await _teardown_jumpoint_host_if_idle_gcp(db, region)
    if cloud == "azure":
        return await _teardown_jumpoint_host_if_idle_azure(db, region)
    return await _teardown_jumpoint_host_if_idle_aws(db, region)


async def _teardown_jumpoint_host_if_idle_aws(db, region: str) -> None:
    """Terminate the shared AWS host iff nothing is left using it (no managed EC2
    instance, no active AWS cloud database). Best-effort; logs and returns on error."""
    from . import aws_service
    try:
        active = _active_db_count(db, "aws") + _active_ec2_count(db)
        if active > 0:
            logger.info("jumpoint-host: keeping host (%d active resource(s))", active)
            return
        name = _cfg("bt_ecs_host_name") or "dashboard-sandbox-jumpoint-host"
        hosts = await aws_service.find_instances_by_tag(
            region, name_tag=name, states=["pending", "running", "stopping", "stopped"])
        if not hosts:
            return
        # Stop the jumpoint task(s) first (graceful PRA deregistration), then
        # terminate the host.
        cluster = _cfg("bt_ecs_cluster")
        try:
            for t in await aws_service.list_ecs_tasks(region, cluster):
                if t.get("lastStatus") in ("RUNNING", "PENDING", "PROVISIONING"):
                    await aws_service.stop_ecs_jumpoint_task(region, cluster, t["taskArn"])
        except Exception as exc:
            logger.warning("jumpoint-host: stopping jumpoint task(s) failed (non-fatal): %s", exc)
        for h in hosts:
            await aws_service.terminate_instance(region, h["instance_id"])
            logger.info("jumpoint-host: terminated idle host %s", h["instance_id"])
    except Exception as exc:
        logger.warning("jumpoint-host: idle teardown failed (non-fatal): %s", exc)


# ── GCP: privileged BeyondTrust Jumpoint container on a COS GCE VM ─────────────
# Cloud Run / serverless can't grant NET_ADMIN/NET_RAW/IPC_LOCK + /dev/net/tun,
# so the tunnel host is a Container-Optimised-OS GCE instance running the
# jumpoint container PRIVILEGED (gcp_service sets securityContext.privileged).
# One shared, ref-counted instance, mirroring the AWS host lifecycle.

def _gcp_jumpoint_name() -> str:
    return _cfg("gcp_jumpoint_name") or "clouddb-shared-jumpoint"


def _gcp_project() -> str:
    return _cfg("gcp_project") or _cfg("gcp_project_id")


def _gcp_jumpoint_zone(region: str) -> str:
    # Explicit jumpoint zone wins; else the generic gcp_zone; else derive a
    # conventional zone from the region (region-b) as a last resort.
    return _cfg("gcp_jumpoint_zone") or _cfg("gcp_zone") or (f"{region}-b" if region else "")


async def _resolve_gcp_deploy_key() -> str:
    """BeyondTrust Jumpoint deploy key for GCP launches — resolved through whichever
    secrets backend the user picked on /secrets (same keys the GCP deploy flow uses)."""
    from . import config_service
    return (config_service.get("gcp_cloud_run_docker_deploy_key")
            or config_service.get("gcp_jumpoint_docker_deploy_key")
            or config_service.get("gcp_jumpoint_deploy_key")
            or "")


async def _ensure_jumpoint_host_gcp(region: str) -> Optional[str]:
    """Ensure the shared COS GCE Jumpoint VM is up (idempotent on name); return its
    name. Best-effort — logs and returns None when prerequisites are missing."""
    from . import gcp_service
    project = _gcp_project()
    if not project:
        logger.warning("jumpoint-host(gcp): gcp_project not set — cannot start a jumpoint.")
        return None
    deploy_key = await _resolve_gcp_deploy_key()
    if not deploy_key:
        logger.warning("jumpoint-host(gcp): jumpoint deploy key not set "
                       "(gcp_cloud_run_docker_deploy_key) — tunnels unavailable until configured.")
        return None
    name = _gcp_jumpoint_name()
    zone = _gcp_jumpoint_zone(region)
    try:
        meta = await gcp_service.run_gce_jumpoint(
            project_id=project,
            zone=zone,
            name=name,
            container_image=_cfg("gcp_jumpoint_image") or "beyondtrust/sra-jumpoint:latest",
            deploy_key=deploy_key,
            network=_cfg("gcp_db_network") or _cfg("gcp_network") or "",
            subnetwork=_cfg("gcp_subnetwork") or "",
            machine_type=_cfg("gcp_jumpoint_machine_type") or "e2-micro",
            create_external_ip=True,
        )
        logger.info("jumpoint-host(gcp): jumpoint %s %s in %s",
                    name, "reused" if meta.get("reused") else "started", zone)
        return name
    except Exception as exc:
        logger.warning("jumpoint-host(gcp): ensure failed (non-fatal): %s", exc)
        return None


async def _teardown_jumpoint_host_if_idle_gcp(db, region: str) -> None:
    """Delete the shared GCE Jumpoint VM iff no active GCP cloud database is left
    using it. Best-effort; logs and returns on error."""
    from . import gcp_service
    try:
        active = _active_db_count(db, "gcp")
        if active > 0:
            logger.info("jumpoint-host(gcp): keeping jumpoint (%d active DB(s))", active)
            return
        project = _gcp_project()
        if not project:
            return
        await gcp_service.stop_gce_jumpoint(project, _gcp_jumpoint_zone(region), _gcp_jumpoint_name())
        logger.info("jumpoint-host(gcp): deleted idle jumpoint %s", _gcp_jumpoint_name())
    except Exception as exc:
        logger.warning("jumpoint-host(gcp): idle teardown failed (non-fatal): %s", exc)


# ── Azure: privileged BeyondTrust Jumpoint on an Azure VM ─────────────────────
# ACI (run_aci_jumpoint_task) is serverless and can't grant NET_ADMIN/NET_RAW/
# IPC_LOCK + /dev/net/tun, so the tunnel host is a real Azure VM running the
# jumpoint container privileged (azure_service.run_vm_jumpoint). One shared,
# ref-counted VM, mirroring the AWS/GCP host lifecycle.

_AZURE_JUMPOINT_VM_NAME = "clouddb-jumpoint"


async def _resolve_azure_deploy_key() -> str:
    """BeyondTrust Jumpoint deploy key for Azure launches — resolved through
    whichever secrets backend the user picked on /secrets (same keys the ACI
    jumpoint path uses)."""
    from . import config_service
    return (config_service.get("azure_aci_deploy_key")
            or config_service.get("azure_aci_docker_deploy_key")
            or "")


def _azure_compliant_password() -> str:
    """A random password meeting Azure's VM complexity rules (3 of 4 categories).
    The jumpoint VM has no public IP and accepts no inbound SSH — this just
    satisfies the API; it is never used to log in."""
    import secrets
    import string
    symbols = "!@#%^*-_"
    alphabet = string.ascii_letters + string.digits + symbols
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(24))
        if (any(c.islower() for c in pw) and any(c.isupper() for c in pw)
                and any(c.isdigit() for c in pw) and any(c in symbols for c in pw)):
            return pw


async def _ensure_jumpoint_host_azure(region: str) -> Optional[str]:
    """Ensure the shared Azure VM Jumpoint is up (idempotent on name); return its
    name. Best-effort — logs and returns None when prerequisites are missing."""
    from . import azure_service
    rg = _cfg("azure_resource_group")
    location = _cfg("azure_location") or region
    subnet = _cfg("azure_jumpoint_subnet_id") or _cfg("azure_aci_subnet_id")
    if not (rg and location and subnet):
        logger.warning("jumpoint-host(azure): azure_resource_group / azure_location / "
                       "azure_jumpoint_subnet_id not set — cannot start a jumpoint.")
        return None
    deploy_key = await _resolve_azure_deploy_key()
    if not deploy_key:
        logger.warning("jumpoint-host(azure): jumpoint deploy key not set "
                       "(azure_aci_deploy_key) — tunnels unavailable until configured.")
        return None
    try:
        meta = await azure_service.run_vm_jumpoint(
            rg=rg, location=location, subnet_id=subnet, name=_AZURE_JUMPOINT_VM_NAME,
            container_image=_cfg("azure_aci_jumpoint_image") or "beyondtrust/sra-jumpoint:latest",
            deploy_key=deploy_key,
            vm_size=_cfg("azure_jumpoint_vm_size") or "Standard_B1s",
            admin_password=_azure_compliant_password(),
        )
        logger.info("jumpoint-host(azure): jumpoint VM %s %s in %s",
                    _AZURE_JUMPOINT_VM_NAME, "reused" if meta.get("reused") else "started", location)
        return _AZURE_JUMPOINT_VM_NAME
    except Exception as exc:
        logger.warning("jumpoint-host(azure): ensure failed (non-fatal): %s", exc)
        return None


async def _teardown_jumpoint_host_if_idle_azure(db, region: str) -> None:
    """Delete the shared Azure Jumpoint VM iff no active Azure cloud database is
    left using it. Best-effort; logs and returns on error."""
    from . import azure_service
    try:
        active = _active_db_count(db, "azure")
        if active > 0:
            logger.info("jumpoint-host(azure): keeping jumpoint (%d active DB(s))", active)
            return
        rg = _cfg("azure_resource_group")
        if not rg:
            return
        await azure_service.stop_vm_jumpoint(rg, _AZURE_JUMPOINT_VM_NAME)
        logger.info("jumpoint-host(azure): deleted idle jumpoint %s", _AZURE_JUMPOINT_VM_NAME)
    except Exception as exc:
        logger.warning("jumpoint-host(azure): idle teardown failed (non-fatal): %s", exc)
