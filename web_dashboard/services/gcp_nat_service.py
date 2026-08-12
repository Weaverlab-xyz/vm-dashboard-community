"""
On-demand Cloud NAT egress for sandbox GCE VMs.

Sandbox user VMs land in the vm-subnet with no external IP, and the sandbox
deliberately gives them no outbound internet via TWO independent gates
(scripts/sandbox/Linux/setup-gcp.sh):

  1. The shared Cloud NAT lists ONLY the jumpoint + k8s subnets, so the vm-subnet
     has no NAT path at all.
  2. A priority-1000 EGRESS DENY on the VM network tag (`*-deny-vm-egress`), with a
     priority-999 ALLOW back to the sandbox supernet so SSH replies still work.

Opening one gate does nothing — both have to open. Rather than change the sandbox's
permanent posture (which would bill for egress infrastructure serving VMs that don't
exist), the dashboard opens both BY REFERENCE COUNT, mirroring `nat_instance_service`
on AWS and the per-cluster NAT the GKE module builds:

  * ensure_vm_nat(region)     — called on GCE deploy. Find-or-creates a SECOND Cloud
                                NAT gateway on the sandbox's existing Cloud Router,
                                scoped to the vm-subnet's primary range, plus a
                                priority-900 EGRESS ALLOW that outranks the deny.
  * reclaim_vm_nat(db, …)     — called on GCE destroy. Removes both, but only once no
                                live GCE VM is left IN THAT REGION.

Both halves are additive resources with their own names. The sandbox's own NAT and its
deny rule are never modified, so the default-closed posture survives untouched and a
partial failure can only leave the VM with less egress, never the sandbox with more.

Gated behind `gcp_vm_nat_enabled`. Best-effort from the caller's perspective: failures
log and leave the VM intact (egress is repaired on the next ensure). No-ops quietly when
the region has no Cloud Router configured, so non-sandbox GCP projects are unaffected.
"""
import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Deploys run as background tasks in one process, so a module lock closes the
# same-process double-create race. The underlying ensure is idempotent anyway, but the
# router read-modify-write is not atomic: two concurrent patches would each re-send the
# NAT list they read, and the loser would silently drop the winner's gateway.
_NAT_ENSURE_LOCK = asyncio.Lock()

_DEFAULT_NAT_NAME = "dashboard-sandbox-vm-nat"
_DEFAULT_RULE_NAME = "dashboard-sandbox-vm-egress-ondemand"
# Lower number = higher precedence. The sandbox deny sits at 1000, its in-VPC allow at
# 999, and the jumpoint DB allows at 998 — 900 clears all of them without collision.
_DEFAULT_RULE_PRIORITY = 900


def _cfg(key: str) -> str:
    from . import config_service
    val = config_service.get(key)
    if val:
        return val
    from ..config import settings
    return getattr(settings, key, "") or ""


def _names() -> tuple[str, str, int]:
    """(nat gateway name, egress rule name, rule priority) — all overridable."""
    raw = _cfg("gcp_vm_egress_rule_priority")
    try:
        priority = int(raw) if raw else _DEFAULT_RULE_PRIORITY
    except (TypeError, ValueError):
        logger.warning("gcp-vm-nat: gcp_vm_egress_rule_priority=%r is not an integer — "
                       "using %d", raw, _DEFAULT_RULE_PRIORITY)
        priority = _DEFAULT_RULE_PRIORITY
    return (_cfg("gcp_vm_nat_name") or _DEFAULT_NAT_NAME,
            _cfg("gcp_vm_egress_rule_name") or _DEFAULT_RULE_NAME,
            priority)


def _resolve(region: str) -> Optional[dict]:
    """Everything the two halves need, or None when this region isn't a sandbox
    (no Cloud Router) — in which case there is nothing to attach a NAT to."""
    from .region_config import resolve_region
    project = _cfg("gcp_project_id")
    if not project:
        logger.warning("gcp-vm-nat: gcp_project_id unset — skipping VM egress.")
        return None
    rc = resolve_region("gcp", region)
    router = (rc.get("router_name") or "").strip()
    subnetwork = (rc.get("subnetwork") or "").strip()
    if not router or not subnetwork:
        logger.info("gcp-vm-nat: no Cloud Router / subnetwork configured for %s — "
                    "skipping VM egress (VMs stay VPC-only).", region)
        return None
    # `default_network_tag` is what the sandbox's deny rule targets and what every
    # dashboard-deployed VM carries, so the ALLOW has to target the same tag to
    # outrank it. Without it the deny still applies and NAT alone buys nothing.
    tags = [t.strip() for t in (rc.get("default_network_tag") or "").split(",") if t.strip()]
    if not tags:
        logger.info("gcp-vm-nat: no default_network_tag for %s — skipping VM egress "
                    "(nothing to scope the egress allow rule to).", region)
        return None
    return {
        "project": project,
        "router": router,
        "subnetwork": subnetwork.rsplit("/", 1)[-1],
        "network": (rc.get("network") or "").strip() or "default",
        "tags": tags,
    }


async def ensure_vm_nat(region: str) -> Optional[str]:
    """Ensure the on-demand Cloud NAT gateway + egress ALLOW exist for ``region``.

    Returns the NAT gateway name (or None when disabled / not configured). Raises only
    on hard GCP errors, which the caller catches — egress is best-effort so a VM never
    fails to deploy because its internet path could not be opened."""
    from . import config_service, gcp_service
    if not config_service.get_bool("gcp_vm_nat_enabled"):
        return None
    res = _resolve(region)
    if not res:
        return None
    nat_name, rule_name, priority = _names()

    async with _NAT_ENSURE_LOCK:
        created = await gcp_service.ensure_vm_egress_nat(
            project=res["project"], region=region, router=res["router"],
            nat_name=nat_name, subnetwork=res["subnetwork"])
        logger.info("gcp-vm-nat: %s Cloud NAT %s on %s (%s → %s)",
                    "created" if created else "reusing", nat_name, res["router"],
                    res["subnetwork"], region)
        # The NAT is useless while the priority-1000 deny still wins, so the ALLOW is
        # part of the same ensure rather than a separate opt-in.
        await gcp_service.ensure_egress_allow_rule(
            project=res["project"], name=rule_name, network=res["network"],
            target_tags=res["tags"], priority=priority)
        return nat_name


def _active_gce_count(db, region: str) -> int:
    """Live GCE VMs in ``region`` — completed gce_deploy jobs not yet destroyed.

    Region-scoped on purpose. The AWS sibling counts VPC-wide because that sandbox is
    single-region, but the GCP sandbox is explicitly multi-region (each region gets its
    own subnets and its own Cloud Router), so a VM in us-central1 must not pin the
    us-east1 NAT — that would reintroduce exactly the standing cost this avoids.

    Counts every live VM rather than only the ones that needed NAT: over-counting keeps
    egress alive slightly longer, while under-counting would cut the internet out from
    under a running VM."""
    from ..database import Job
    jobs = db.query(Job).filter(Job.job_type == "gce_deploy",
                                Job.status == "completed").all()
    n = 0
    for j in jobs:
        meta = j.metadata_dict or {}
        if meta.get("destroyed"):
            continue
        # Matched by prefix rather than via gcp_vm_service._region_from_zone, which
        # falls back to the CONFIGURED default region for anything it can't parse —
        # that would read the app DB once per row, and would quietly attribute every
        # zone-less row to whichever region happens to be the default. A row with no
        # zone counts for EVERY region instead: over-counting only delays teardown,
        # while under-counting would cut egress from under a running VM.
        zone = (meta.get("zone") or "").strip()
        if not zone or zone == region or zone.startswith(f"{region}-"):
            n += 1
    return n


async def reclaim_vm_nat(db, region: str) -> None:
    """Remove the on-demand Cloud NAT + egress ALLOW iff no live GCE VM is left in
    ``region``. Best-effort; logs and returns on error."""
    from . import config_service, gcp_service
    try:
        if not config_service.get_bool("gcp_vm_nat_enabled"):
            return
        active = _active_gce_count(db, region)
        if active > 0:
            logger.info("gcp-vm-nat: keeping egress in %s (%d active VM(s))", region, active)
            return
        res = _resolve(region)
        if not res:
            return
        nat_name, rule_name, _ = _names()
        # Close the firewall first: with the ALLOW gone the VMs are already denied, so
        # the window where a gateway exists without a matching rule costs nothing. The
        # reverse order would briefly allow egress with no NAT — harmless, but this way
        # a failure between the two leaves the sandbox CLOSED rather than open.
        await gcp_service.delete_firewall_rule(res["project"], rule_name)
        removed = await gcp_service.delete_vm_egress_nat(
            project=res["project"], region=region, router=res["router"], nat_name=nat_name)
        if removed:
            logger.info("gcp-vm-nat: removed idle Cloud NAT %s (%s)", nat_name, region)
    except Exception as exc:  # noqa: BLE001
        logger.warning("gcp-vm-nat: idle teardown failed (non-fatal): %s", exc)
