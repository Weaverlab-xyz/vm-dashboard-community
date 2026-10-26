"""Where a cloud database lives, for Config-Management routes.

A route matches on addresses, and a cloud database's endpoint is a DNS name on AWS and
Azure. So a database is matched by its SUBNETS instead (config_mgmt_route_service.
match_for_ranges): a route governs it when the route's range contains every one of them.
That survives an RDS failover, which moves the address but not the subnet group, and needs
no DNS from the dashboard, which often cannot resolve an Azure private-DNS name at all.

  * AWS: every subnet in the instance's DB subnet group, and its VPC.
  * Azure: the delegated subnet (Flexible Server) or the private-endpoint subnet (SQL),
    and its VNet.
  * GCP: the private IP Cloud SQL reports, as a /32, and the VPC network.

Read-only. Recorded at provisioning (best-effort) and by route_network_backfill.
"""
import ipaddress
import json
import logging

logger = logging.getLogger(__name__)

CLOUDS = ("aws", "azure", "gcp")


class NetworkUnknown(Exception):
    """A database whose location cannot be read from what the dashboard recorded."""


def _tf_variables(db, row) -> dict:
    from .cloud_database_service import _provision_job_for
    job = _provision_job_for(db, row.id)
    return dict(((job.metadata_dict or {}).get("tf_variables") if job else None) or {})


def _cidrs(values) -> list:
    out = []
    for v in values or []:
        try:
            out.append(str(ipaddress.ip_network(str(v).strip(), strict=False)))
        except ValueError:
            continue
    return sorted(set(out))


async def lookup(db, row) -> tuple:
    """``(cidrs, network)`` for a provisioned cloud database, or ``([], "")`` when the cloud
    no longer has it. Raises NetworkUnknown (or the cloud's own error) when it cannot tell."""
    from . import config_mgmt_route_service as cmr
    cloud = (row.cloud or "").lower()
    if cloud not in CLOUDS or (row.provider or "") == "atlas":
        raise NetworkUnknown(f"{cloud or 'this'} database has no network of ours to record")
    tf = _tf_variables(db, row)
    region = (row.region or tf.get("region") or "").strip()
    if cloud == "aws":
        from . import aws_service
        identifier = str(tf.get("identifier") or "").strip()
        resource_id = str(row.instance_id or "").strip()
        if not region or not (identifier or resource_id):
            raise NetworkUnknown("no region or instance identifier recorded")
        found = await aws_service.rds_subnet_ranges(region, identifier, resource_id)
        if found is None:
            return [], ""
        return _cidrs(found["cidrs"]), cmr.normalize_network("aws", found["vpc_id"])
    if cloud == "azure":
        from . import azure_service
        subnet_id = str(tf.get("delegated_subnet_id") or tf.get("subnet_id") or "").strip()
        if not subnet_id:
            raise NetworkUnknown("no subnet recorded")
        prefixes = await azure_service.subnet_prefixes(subnet_id)
        if prefixes is None:
            return [], ""
        return _cidrs(prefixes), cmr.normalize_network("azure", subnet_id)
    host = str(row.private_host or "").strip()
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        raise NetworkUnknown(f"its private host {host!r} is not an address")
    network = str(tf.get("private_network") or "").strip()
    if network and "/" not in network:
        project = str(tf.get("project") or "").strip()
        network = f"projects/{project}/global/networks/{network}" if project else ""
    return [f"{ip}/{ip.max_prefixlen}"], cmr.normalize_network("gcp", network) if network else ""


def record(row, cidrs: list, network: str) -> None:
    row.route_cidrs = json.dumps(list(cidrs)) if cidrs else None
    row.route_network = network or None


async def record_on_provision(db, row, log=None) -> None:
    """Best-effort, after the apply: a failure is logged and leaves the database on the
    remote worker, never failing the provision."""
    row.deploy_workgroup = (getattr(row, "deploy_workgroup", None)
                            or getattr(row, "workgroup", None))
    try:
        cidrs, network = await lookup(db, row)
        record(row, cidrs, network)
    except Exception as exc:  # noqa: BLE001 — never fails a provision
        logger.warning("clouddb %s: could not record its subnets: %s", row.id, exc)
        if log:
            try:
                log(f"Could not record this database's subnets for Config routes ({exc}). "
                    f"Look up networks on Remote Agents → Config Routes retries it.")
            except Exception:  # noqa: BLE001 — the note is best-effort too
                pass
    db.commit()


def candidates(db) -> list:
    """Provisioned cloud databases with no recorded subnets."""
    from ..database import CloudDatabase
    return [r for r in db.query(CloudDatabase)
            .filter(CloudDatabase.cloud.in_(CLOUDS),
                    CloudDatabase.source == "provisioned",
                    CloudDatabase.route_cidrs.is_(None)).all()
            if (r.provider or "") != "atlas" and (r.status or "") == "available"]
