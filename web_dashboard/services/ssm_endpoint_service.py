"""
Shared, on-demand SSM interface VPC endpoints for the sandbox private subnet.

Password Safe SSM VM onboarding (and the cloud-DB ``dbssm`` path) manages private
EC2/RDS targets over AWS Systems Manager. A sandbox VM whose security group egresses
to the VPC only (internet revoked) reaches the SSM control plane ONLY through the
three interface endpoints ``ssm`` / ``ssmmessages`` / ``ec2messages`` with private DNS.

Each interface endpoint bills ~$7/mo while it exists, so — instead of standing them
up in the sandbox setup script (which left ~$22/mo running even when idle) — the
dashboard manages them by reference count, exactly like ``nat_instance_service`` and
``jumpoint_host_service``:

  * ensure_ssm_endpoints(region)  — called on EC2 deploy / AWS cloud-DB provision.
                                    Find-or-creates the three endpoints (private DNS)
                                    in the sandbox private subnet.
  * reclaim_ssm_endpoints(db, …)  — called on EC2 destroy / AWS cloud-DB decommission.
                                    Deletes the endpoints + their SG only when no
                                    EC2 instance and no AWS cloud database is left.

Gated behind ``aws_ssm_endpoints_enabled`` (default off; set true by
scripts/sandbox/Linux/setup-aws.sh). Best-effort from the caller's perspective —
failures log and leave the deploy intact (SSM reach is repaired on the next ensure).
"""
import asyncio
import logging
from typing import List, Optional

logger = logging.getLogger(__name__)

# The three SSM control-plane services the agent needs on a private-subnet target.
SSM_SERVICES = ("ssm", "ssmmessages", "ec2messages")

# Find-or-create key for the endpoint SG (matches the sandbox script's name).
_VPCE_SG_NAME = "dashboard-sandbox-ssm-vpce-sg"

# All deploys run as background tasks in one process, so a module lock fully closes
# the same-process double-create race (belt-and-braces with reuse-before-create).
_SSM_ENSURE_LOCK = asyncio.Lock()


def _cfg(key: str) -> str:
    from . import config_service
    val = config_service.get(key)
    if val:
        return val
    from ..config import settings
    return getattr(settings, key, "") or ""


def _resolve_network(region: str):
    """Return (vpc_id, private_subnet_id, vpc_cidr) for ``region`` from the region
    config (per-region entry or flat fallback), or None if VPC/subnet are unset."""
    from . import region_config
    cfg = region_config.resolve_region("aws", region)
    vpc_id = cfg.get("vpc_id")
    subnet_id = cfg.get("default_subnet_id")  # the sandbox private subnet
    if not vpc_id or not subnet_id:
        return None
    return vpc_id, subnet_id, cfg.get("vpc_cidr") or "10.99.0.0/16"


async def ensure_ssm_endpoints(region: str) -> Optional[List[str]]:
    """Ensure the three SSM interface endpoints exist in the sandbox private subnet;
    return their endpoint ids (or None when disabled / not configured). Best-effort
    for callers — raises only on hard AWS errors, which the caller catches."""
    from . import aws_service, config_service
    if not config_service.get_bool("aws_ssm_endpoints_enabled"):
        return None
    net = _resolve_network(region)
    if not net:
        logger.warning("ssm-endpoints: aws_vpc_id / private subnet not set — skipping "
                       "(private-subnet SSM targets won't reach the control plane).")
        return None
    vpc_id, subnet_id, vpc_cidr = net
    service_names = [f"com.amazonaws.{region}.{s}" for s in SSM_SERVICES]

    async with _SSM_ENSURE_LOCK:
        sg_id = _cfg("aws_ssm_vpce_security_group_id") or await aws_service.ensure_ssm_vpce_security_group(
            region, vpc_id=vpc_id, vpc_cidr=vpc_cidr, name=_VPCE_SG_NAME)
        existing = await aws_service.find_ssm_endpoints(
            region, vpc_id=vpc_id, service_names=service_names)
        ids: List[str] = []
        for svc, full in zip(SSM_SERVICES, service_names):
            hit = existing.get(full)
            if hit:
                ids.append(hit["endpoint_id"])
                continue
            epid = await aws_service.create_ssm_endpoint(
                region, vpc_id=vpc_id, service_name=full, subnet_id=subnet_id,
                security_group_ids=[sg_id], name_tag=f"dashboard-sandbox-{svc}")
            ids.append(epid)
            logger.info("ssm-endpoints: created %s endpoint %s", svc, epid)
        return ids


async def reclaim_ssm_endpoints(db, region: str) -> None:
    """Delete the SSM endpoints + their SG iff no EC2 instance and no AWS cloud
    database is left using them. Best-effort; logs and returns on error."""
    from . import aws_service, config_service
    from .jumpoint_host_service import _active_db_count, _active_ec2_count
    try:
        if not config_service.get_bool("aws_ssm_endpoints_enabled"):
            return
        active = _active_ec2_count(db) + _active_db_count(db, "aws")
        if active > 0:
            logger.info("ssm-endpoints: keeping endpoints (%d active EC2/DB resource(s))", active)
            return
        net = _resolve_network(region)
        if not net:
            return
        vpc_id, _subnet_id, _cidr = net
        service_names = [f"com.amazonaws.{region}.{s}" for s in SSM_SERVICES]
        found = await aws_service.find_ssm_endpoints(
            region, vpc_id=vpc_id, service_names=service_names)
        ep_ids = [v["endpoint_id"] for v in found.values()]
        if ep_ids:
            await aws_service.delete_ssm_endpoints(region, ep_ids)
            logger.info("ssm-endpoints: deleted idle endpoints %s", ep_ids)
        # Drop the SG once its endpoints are gone (skip a pre-provisioned/shared one).
        if not _cfg("aws_ssm_vpce_security_group_id"):
            sg_id = await aws_service.find_security_group_id(
                region, vpc_id=vpc_id, name=_VPCE_SG_NAME)
            if sg_id:
                await aws_service.delete_security_group(region, sg_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("ssm-endpoints: idle teardown failed (non-fatal): %s", exc)
