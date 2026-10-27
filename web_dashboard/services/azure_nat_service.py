"""
On-demand NAT gateway egress for Azure VMs.

A VM the dashboard deploys into a private subnet often has no way out: `apt update`,
`dnf update`, image pulls and config-mgmt playbooks time out. The sandbox closes it on
purpose, through two independent gates (scripts/sandbox/Linux/setup-azure.sh):

  1. Each VM subnet's NSG carries `deny-internet-out` (priority 200) under
     `allow-vnet-out` (100), so nothing but the VNet is reachable.
  2. Azure is retiring default outbound access. A subnet with
     `defaultOutboundAccess=false` (a private subnet) has no path out at all without a
     NAT gateway, a public IP or a load balancer, whatever its NSG says.

A standing NAT gateway bills by the hour whether or not a VM exists, so the dashboard
opens both gates BY REFERENCE COUNT, as `nat_instance_service` does on AWS and
`gcp_nat_service` on GCP:

  * ensure(subnet_id, nsgs)  — before every Azure VM is created (azure_service.deploy_vm
                               and run_vm_container_node, so VMs, desktops, OT hosts,
                               Portainer and Rancher nodes and deployed agents alike).
                               Find-or-creates ONE NAT gateway per VNet with a Standard
                               public IP, attaches it to the subnet, and adds an allow
                               rule that outranks each NSG's internet deny, scoped to the
                               subnet's own range.
  * release(subnet_id)       — after a VM is deleted. Undoes all of it once the subnet
                               holds no NIC (Azure's own count, so a VM removed by any
                               path, or by hand, is counted correctly).
  * reclaim_loop()           — a worker pass every ten minutes that releases whatever a
                               missed or deferred release left behind.

It acts ONLY where egress is closed. A subnet with its own NAT gateway, or whose NSGs
already let HTTPS out to the internet over default outbound access, is left exactly as
it is: attaching a gateway there would change the address every VM in it leaves from,
and bill for it, to fix nothing. The sandbox's own NSG rules are never modified; the
allow is an additive rule with its own name, so a partial failure can only leave a VM
with less egress, never the network with more.

Gated behind `azure_vm_nat_enabled` (default off; the sandbox scripts turn it on).
Best-effort from the caller's side: failures log and the VM still deploys.
"""
import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)

ENABLED = "azure_vm_nat_enabled"

# A release that lands between a deploy's ensure and the creation of its NIC would see
# an empty subnet and remove egress from under the VM about to boot. The NIC follows the
# ensure within a minute or two, so a release defers to the next pass inside this window.
GRACE_S = 600
RECLAIM_INTERVAL_S = 600

# Deploys run as background tasks in one worker, so a module lock serialises the
# subnet read-modify-write within a process. Across replicas the ARM operations are
# idempotent PUTs on deterministic names.
_LOCK = asyncio.Lock()


def enabled() -> bool:
    from . import config_service
    return config_service.get_bool(ENABLED)


async def ensure(subnet_id: str, nsg_ids: Optional[list] = None) -> Optional[dict]:
    """Open egress for a VM about to be created in ``subnet_id``. Returns the
    azure_service.ensure_vm_egress result, or None when the feature is off or the
    attempt failed (logged). Never raises: a VM without internet is a working VM."""
    if not (enabled() and subnet_id):
        return None
    from . import azure_service
    try:
        async with _LOCK:
            res = await azure_service.ensure_vm_egress(subnet_id, [n for n in (nsg_ids or []) if n])
    except Exception as exc:  # noqa: BLE001
        logger.warning("azure-vm-nat: could not open egress for %s (the VM deploys "
                       "anyway, without internet): %s", subnet_id, exc)
        return None
    if res.get("status") == "ready":
        logger.info("azure-vm-nat: %s %s via %s (%s); NSG allows: %s%s",
                    "attached" if res.get("attached") else "kept", subnet_id.rsplit("/", 1)[-1],
                    res.get("nat_gateway"), res.get("public_ip") or "no address yet",
                    ", ".join(res.get("nsg_rules") or []) or "none needed",
                    ("; " + "; ".join(res["problems"])) if res.get("problems") else "")
    else:
        logger.info("azure-vm-nat: left %s alone: %s", subnet_id.rsplit("/", 1)[-1],
                    res.get("reason"))
    return res


async def release(subnet_id: str, *, grace_s: int = GRACE_S) -> str:
    """Remove the subnet's on-demand egress if nothing is left in it. Never raises;
    returns what happened, in words, for a job's result."""
    if not (enabled() and subnet_id):
        return ""
    from . import azure_service
    try:
        async with _LOCK:
            outcome = await azure_service.release_vm_egress(subnet_id, grace_s=grace_s)
    except Exception as exc:  # noqa: BLE001
        logger.warning("azure-vm-nat: release of %s failed (the next pass retries): %s",
                       subnet_id, exc)
        return f"failed: {exc}"
    logger.info("azure-vm-nat: %s: %s", subnet_id.rsplit("/", 1)[-1], outcome)
    return outcome


async def reclaim_once() -> int:
    """Release every subnet behind an on-demand NAT gateway that has emptied.
    Returns how many were released."""
    if not enabled():
        return 0
    from . import azure_service
    try:
        nats = await azure_service.list_vm_egress_nats()
    except Exception as exc:  # noqa: BLE001
        logger.debug("azure-vm-nat: reclaim pass skipped: %s", exc)
        return 0
    released = 0
    for nat in nats:
        for sid in nat["subnets"]:
            if (await release(sid)).startswith(("released", "detached")):
                released += 1
    return released


async def reclaim_loop(interval: float = RECLAIM_INTERVAL_S) -> None:
    """The worker's safety net: a release deferred by the grace window, or skipped
    because a VM was deleted outside the dashboard, is caught within ``interval``."""
    while True:
        try:
            await reclaim_once()
        except Exception:  # noqa: BLE001
            logger.warning("azure-vm-nat: reclaim pass failed", exc_info=True)
        await asyncio.sleep(interval)
