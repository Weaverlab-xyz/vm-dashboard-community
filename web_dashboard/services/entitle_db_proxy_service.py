"""
On-demand TCP forwarder so the Entitle agent can reach a private GCP Cloud SQL DB.

**GCP-only.** A dashboard-provisioned Cloud SQL instance is private
(``ipv4_enabled=false``); its Private-Service-Access IP is reachable ONLY from the
sandbox VPC, and GCP VPC peering is **non-transitive** — so the Entitle agent (in
its own GKE VPC, one peering hop away over the GKE↔sandbox peering) cannot route to
the Cloud SQL PSA IP. This stands up a tiny ``socat`` relay (COS-on-GCE) *inside*
the sandbox VPC that the agent CAN reach over that peering; socat forwards to the
Cloud SQL private IP. The Entitle integration's connection host is then pointed at
the forwarder instead of the unreachable private IP.

Lifecycle: **one relay VM per registered GCP DB** (``clouddb-fwd-<db-id8>``), created
when the DB is registered in Entitle and torn down on deregister / decommission. A
single shared ingress firewall rule lets the GKE agent's ranges reach any forwarder
(network tag ``bt-db-forwarder``) on the DB port. Gated by
``gcp_entitle_db_proxy_enabled`` (default off). Mirrors ``nat_instance_service.py``.

AWS RDS needs none of this — RDS sits directly in the sandbox VPC, single-hop
EKS↔RDS peering, so the agent reaches it natively.
"""
import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Shared ingress rule name (GKE agent ranges → tag bt-db-forwarder on the DB port).
_FORWARDER_FW_RULE = "dashboard-entitle-db-forwarder"
# Default GKE agent node + pod ranges (the terraform/k8s_cluster/gcp_gke defaults).
_DEFAULT_SOURCE_RANGES = "10.98.0.0/22,10.100.0.0/16"
_DEFAULT_PORTS = {"sqlserver": 1433, "postgres": 5432, "mysql": 3306}

# All registrations run as background tasks in one process, so a module lock closes
# the same-process double-create race (belt-and-braces with find-or-create by name).
_ENSURE_LOCK = asyncio.Lock()


def _cfg(key: str) -> str:
    from . import config_service
    val = config_service.get(key)
    if val:
        return val
    from ..config import settings
    return getattr(settings, key, "") or ""


def enabled() -> bool:
    from . import config_service
    return config_service.get_bool("gcp_entitle_db_proxy_enabled", False)


def _forwarder_name(db_id: str) -> str:
    # RFC1035 GCE instance name; db_id is a UUID so the first 8 hex chars are safe.
    return f"clouddb-fwd-{db_id[:8]}"


def _resolve_placement(row) -> tuple:
    """(project, zone, network, subnetwork) for the forwarder — reuses the jumpoint
    resolvers so the VM lands in the sandbox VPC's Cloud-NAT subnet (egress for the
    image pull) and can reach the Cloud SQL PSA range via the bt-jumpoint tag.

    Placed in **the database's own region**, never the configured default one. The
    entire job of this VM is to reach the Cloud SQL private IP, and that IP lives on
    the DB region's ``db_network`` — a relay built on another region's network has no
    route to it, so the forwarder comes up healthy and every connection through it
    times out. ``gcp_region`` is only the fallback for a row with no region recorded
    (a registered DB). Reading it first made ``row.region`` dead code, because the
    sandbox always emits it.

    Zone and network then resolve *for that region* through ``region_config`` (the
    region entry's ``zone``/``db_network``, else the flat keys) rather than the flat
    keys alone, so single-region installs are unchanged and a per-region set is
    honoured. ``_gcp_jumpoint_subnetwork`` derives its region from the zone we hand
    it, so it follows along.
    """
    from . import jumpoint_host_service
    from .region_config import resolve_region
    project = _cfg("gcp_project") or _cfg("gcp_project_id")
    region = (row.region or "").strip() or _cfg("gcp_region")
    # Region-scoped: gcp_jumpoint_zone applies only where it belongs, else the
    # region's own zone, else the conventional <region>-b.
    zone = jumpoint_host_service._gcp_jumpoint_zone(region)
    network = resolve_region("gcp", region)["db_network"] or "default"
    subnetwork = jumpoint_host_service._gcp_jumpoint_subnetwork(project, zone) if (project and zone) else ""
    return project, zone, network, subnetwork


def _legacy_default_zone() -> str:
    """The zone :func:`_resolve_placement` returned for *every* DB before placement
    followed the database's own region — the default region's. A forwarder created
    back then is parked there under the same name, so teardown has to sweep it too
    or this fix strands a running VM nobody will ever look for again."""
    from . import jumpoint_host_service
    return jumpoint_host_service._gcp_jumpoint_zone(_cfg("gcp_region"))


async def ensure_db_forwarder(db, row) -> Optional[tuple]:
    """Ensure a socat forwarder VM (+ the shared ingress firewall rule) for this GCP
    DB and return ``(forwarder_internal_ip, db_port)`` for Entitle to use as its
    connection host. Returns ``None`` when disabled or not a GCP DB. **Raises** on a
    hard failure — the register caller decides whether that's fatal."""
    from . import gcp_service
    if not enabled() or row.cloud != "gcp":
        return None
    if not row.private_host:
        raise ValueError("database has no private host yet — cannot build a forwarder")

    project, zone, network, subnetwork = _resolve_placement(row)
    if not project:
        raise ValueError("gcp_project_id is not configured")
    if not zone:
        raise ValueError("cannot resolve a GCP zone for the forwarder — the database has "
                         "no region recorded and neither gcp_region nor gcp_zone is set")
    image = _cfg("gcp_entitle_db_proxy_image") or "alpine/socat:latest"
    machine_type = _cfg("gcp_entitle_db_proxy_machine_type") or "e2-micro"
    port = int(row.port or _DEFAULT_PORTS.get(row.engine, 0) or 0)
    if not port:
        raise ValueError(f"cannot resolve a port for engine {row.engine!r}")
    name = _forwarder_name(row.id)

    async with _ENSURE_LOCK:
        # Shared ingress rule: GKE agent node+pod ranges → tag bt-db-forwarder on the DB port.
        src = [s.strip() for s in (_cfg("gcp_entitle_db_proxy_source_ranges")
                                   or _DEFAULT_SOURCE_RANGES).split(",") if s.strip()]
        await gcp_service.ensure_firewall_rule(
            project=project, name=_FORWARDER_FW_RULE, network=network,
            source_ranges=src, target_tags=["bt-db-forwarder"], protocol="tcp", ports=[port])
        res = await gcp_service.run_gce_db_forwarder(
            project_id=project, zone=zone, name=name,
            listen_port=port, target_host=row.private_host, target_port=port,
            image=image, network=network, subnetwork=subnetwork,
            machine_type=machine_type, create_external_ip=True)

    ip = res.get("internal_ip")
    if not ip:
        raise RuntimeError(f"db-forwarder {name} returned no internal IP")
    logger.info("entitle-db-proxy: forwarder %s ready ip=%s → %s:%s (reused=%s)",
                name, ip, row.private_host, port, res.get("reused"))
    return (ip, port)


async def teardown_db_forwarder(db, row) -> None:
    """Best-effort: delete the per-DB forwarder VM (name derived from the DB id) in
    the DB's own zone, plus :func:`_legacy_default_zone` for anything an older
    release parked in the default region. Leaves the shared firewall rule in place
    (free/harmless, reused by others). No-op for non-GCP DBs or when nothing was
    ever created (404 is benign)."""
    from . import gcp_service
    if row.cloud != "gcp":
        return
    project, zone, _network, _subnetwork = _resolve_placement(row)
    if not (project and zone):
        return
    name = _forwarder_name(row.id)
    # Plus the pre-fix default-region zone when it differs — a 404 there is a quiet
    # no-op, so the sweep costs nothing on every other teardown.
    zones = [zone]
    legacy = _legacy_default_zone()
    if legacy and legacy != zone:
        zones.append(legacy)
    for z in zones:
        try:
            await gcp_service.stop_gce_db_forwarder(project, z, name)
            logger.info("entitle-db-proxy: forwarder %s deleted in %s (db_id=%s)",
                        name, z, row.id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("entitle-db-proxy: forwarder teardown failed db_id=%s zone=%s "
                           "(non-fatal): %s", row.id, z, exc)
