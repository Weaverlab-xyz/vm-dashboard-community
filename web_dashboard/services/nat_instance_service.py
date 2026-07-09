"""
Shared, on-demand NAT instance for sandbox EC2 egress.

Sandbox user VMs land in the private subnet (`dashboard-sandbox-private`), whose
route table has no `0.0.0.0/0` route and there is no NAT — so they have no outbound
internet (apt / package installs / config-mgmt playbooks fail). To avoid a standing
NAT gateway (or an idle NAT instance + Elastic IP), the dashboard manages ONE shared
NAT instance by reference count, mirroring `jumpoint_host_service`:

  * ensure_nat_instance(region)   — called on EC2 deploy. Find-or-creates the NAT
                                    instance (auto-assigned public IP, NO EIP,
                                    source/dest check off, IP-forward + MASQUERADE
                                    via user-data) and points the private route
                                    table's 0.0.0.0/0 at its ENI.
  * reclaim_nat_instance(db, …)   — called on EC2 destroy. Terminates the NAT and
                                    removes the route only when no EC2 instance is
                                    left (DBs/K8s make no outbound and don't need it).

Gated behind `aws_nat_instance_enabled` (default off). Best-effort from the caller's
perspective — failures log and leave the VM intact (egress is repaired on the next
ensure). NAT mechanics mirror terraform/k8s_cluster/aws_eks/main.tf.
"""
import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# All deploys run as background tasks in one process, so a module lock fully closes
# the same-process double-create race (belt-and-braces with recheck-before-launch).
_NAT_ENSURE_LOCK = asyncio.Lock()

# NAT user-data — verbatim from the EKS module (main.tf). AL2023 is minimal and
# ships WITHOUT iptables, so install it BEFORE any iptables use.
_NAT_USER_DATA = """#!/bin/bash
set -euxo pipefail
dnf install -y iptables-services
sysctl -w net.ipv4.ip_forward=1
echo 'net.ipv4.ip_forward=1' > /etc/sysctl.d/99-nat.conf
IFACE="$(ip route | awk '/default/{print $5; exit}')"
iptables -t nat -A POSTROUTING -o "$IFACE" -j MASQUERADE
iptables -P FORWARD ACCEPT
service iptables save
systemctl enable --now iptables
"""


def _cfg(key: str) -> str:
    from . import config_service
    val = config_service.get(key)
    if val:
        return val
    from ..config import settings
    return getattr(settings, key, "") or ""


def _nat_arch(instance_type: str) -> str:
    """AMI architecture for the NAT instance type. Graviton families carry a 'g' in
    the generation token (t4g, m7g, c6g, …) or are the a1 family → arm64; else x86_64."""
    fam = (instance_type or "").split(".")[0].lower()
    return "arm64" if ("g" in fam or fam.startswith("a1")) else "x86_64"


async def _resolve_nat_sg(region: str) -> str:
    """The NAT instance's security group: the sandbox-provisioned id if set, else
    find-or-create one (ingress all from the VPC, egress all)."""
    from . import aws_service
    sg = _cfg("aws_nat_security_group_id")
    if sg:
        return sg
    vpc_id = _cfg("aws_vpc_id")
    if not vpc_id:
        raise aws_service.AWSError(
            "aws_nat_security_group_id and aws_vpc_id are both unset — cannot resolve "
            "a NAT security group.")
    return await aws_service.ensure_nat_security_group(
        region, vpc_id=vpc_id, vpc_cidr=_cfg("aws_vpc_cidr") or "10.99.0.0/16",
        name="dashboard-sandbox-nat-sg")


async def _create_nat(region: str, subnet_id: str, name: str) -> str:
    from . import aws_service
    sg_id = await _resolve_nat_sg(region)
    instance_type = _cfg("aws_nat_instance_type") or "t4g.nano"
    ami_id = _cfg("aws_nat_ami_id") or await aws_service.find_nat_ami(region, _nat_arch(instance_type))
    inst = await aws_service.run_nat_instance(
        region, ami_id=ami_id, instance_type=instance_type, subnet_id=subnet_id,
        security_group_ids=[sg_id], user_data=_NAT_USER_DATA, name_tag=name)
    inst_id = inst["instance_id"]
    # A NAT must not drop traffic it forwards for other hosts.
    await aws_service.set_source_dest_check(region, inst_id, False)
    logger.info("nat-instance: launched %s (%s) in %s", inst_id, instance_type, subnet_id)
    return inst_id


async def ensure_nat_instance(region: str) -> Optional[str]:
    """Ensure the shared NAT instance is up and the private route table routes
    0.0.0.0/0 through it; return the NAT instance id (or None when disabled / not
    configured). Best-effort for callers — raises only on hard AWS errors, which the
    caller catches."""
    from . import aws_service, config_service
    if not config_service.get_bool("aws_nat_instance_enabled"):
        return None
    rt_id = _cfg("aws_private_route_table_id")
    subnet_id = _cfg("aws_nat_subnet_id") or _cfg("bt_ecs_jumpoint_subnet_id")
    if not rt_id or not subnet_id:
        logger.warning("nat-instance: aws_private_route_table_id / NAT subnet not set — "
                       "skipping NAT egress (VMs stay VPC-only).")
        return None
    name = _cfg("aws_nat_instance_name") or "dashboard-sandbox-nat"

    async with _NAT_ENSURE_LOCK:
        existing = await aws_service.find_instances_by_tag(
            region, name_tag=name, states=["pending", "running"])
        if existing:
            inst_id = existing[0]["instance_id"]
            logger.info("nat-instance: reusing NAT %s", inst_id)
        else:
            # A stopped/stopping NAT forwards nothing — reap and recreate.
            for s in await aws_service.find_instances_by_tag(
                    region, name_tag=name, states=["stopping", "stopped"]):
                try:
                    await aws_service.terminate_instance(region, s["instance_id"])
                except Exception:  # noqa: BLE001
                    logger.warning("nat-instance: failed to reap stopped NAT %s (non-fatal)",
                                   s["instance_id"], exc_info=True)
            # Re-check right before launch to shrink the find-or-create race.
            recheck = await aws_service.find_instances_by_tag(
                region, name_tag=name, states=["pending", "running"])
            inst_id = (recheck[0]["instance_id"] if recheck
                       else await _create_nat(region, subnet_id, name))

        # Always (re)assert the route — repairs a stale target after a NAT replace.
        eni = await aws_service.get_instance_primary_eni(region, inst_id)
        await aws_service.upsert_default_route_via_eni(region, rt_id, eni)
        return inst_id


async def reclaim_nat_instance(db, region: str) -> None:
    """Terminate the shared NAT and remove its route iff no EC2 instance is left
    using it. Best-effort; logs and returns on error."""
    from . import aws_service, config_service
    from .jumpoint_host_service import _active_ec2_count
    try:
        if not config_service.get_bool("aws_nat_instance_enabled"):
            return
        active = _active_ec2_count(db)
        if active > 0:
            logger.info("nat-instance: keeping NAT (%d active EC2 instance(s))", active)
            return
        rt_id = _cfg("aws_private_route_table_id")
        name = _cfg("aws_nat_instance_name") or "dashboard-sandbox-nat"
        # Remove the default route first so nothing points at a terminating ENI.
        if rt_id:
            await aws_service.delete_default_route(region, rt_id)
        for n in await aws_service.find_instances_by_tag(
                region, name_tag=name, states=["pending", "running", "stopping", "stopped"]):
            await aws_service.terminate_instance(region, n["instance_id"])
            logger.info("nat-instance: terminated idle NAT %s", n["instance_id"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("nat-instance: idle teardown failed (non-fatal): %s", exc)
