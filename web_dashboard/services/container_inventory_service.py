"""
Container Inventory Service — DB-first container list management.

Responsibilities:
  - get_containers_from_db: instant DB read, returns ContainerInfo objects
  - count_containers_in_db: used for cold-start detection in the API
  - sync_from_portainer: per-endpoint Portainer pull + upsert + prune
  - populate_all: full sweep across the Portainer connection's endpoints (warmer)
"""
import json
import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from ..database import ContainerStateCache, SessionLocal
from ..models.containers import ContainerInfo
from ..services import portainer_service

logger = logging.getLogger(__name__)

# Single Portainer connection — rows are scoped to one constant workgroup.
# The column stays for schema parity; nothing user-facing keys off it.
_WORKGROUP = "default"


# ── Port helper (duplicated to avoid circular import with api/containers.py) ──

def _fmt_ports(port_bindings: list[dict]) -> list[str]:
    """Convert Docker port binding objects to readable strings."""
    result = []
    for p in port_bindings or []:
        ip = p.get("IP", "0.0.0.0") or "0.0.0.0"
        host = p.get("PublicPort", "")
        container = p.get("PrivatePort", "")
        proto = p.get("Type", "tcp")
        if host:
            result.append(f"{ip}:{host}->{container}/{proto}")
        else:
            result.append(f"{container}/{proto}")
    return result


# ── Row converter ─────────────────────────────────────────────────────────────

def _row_to_info(row: ContainerStateCache) -> ContainerInfo:
    """Convert a ContainerStateCache ORM row to a ContainerInfo Pydantic model."""
    try:
        ports = json.loads(row.ports) if row.ports else []
    except (json.JSONDecodeError, TypeError):
        ports = []
    return ContainerInfo(
        id=row.container_id,
        short_id=row.short_id or row.container_id[:12],
        names=[row.name] if row.name else [],
        image=row.image or "",
        status=row.status or "",
        state=row.state or "",
        ports=ports,
        created=row.created_ts or 0,
    )


# ── Public helpers ────────────────────────────────────────────────────────────

def get_containers_from_db(
    db: Session,
    endpoint_id: int,
    all_containers: bool = True,
) -> list[ContainerInfo]:
    """Return all cached rows for the given endpoint as ContainerInfo objects."""
    q = db.query(ContainerStateCache).filter(
        ContainerStateCache.workgroup == _WORKGROUP,
        ContainerStateCache.endpoint_id == endpoint_id,
    )
    if not all_containers:
        q = q.filter(ContainerStateCache.state == "running")
    rows = q.order_by(ContainerStateCache.name).all()
    return [_row_to_info(r) for r in rows]


def count_containers_in_db(db: Session, endpoint_id: int) -> int:
    """Return the number of cached containers for the given endpoint."""
    return (
        db.query(ContainerStateCache)
        .filter(
            ContainerStateCache.workgroup == _WORKGROUP,
            ContainerStateCache.endpoint_id == endpoint_id,
        )
        .count()
    )


async def sync_from_portainer(db: Session, endpoint_id: int) -> int:
    """
    Pull containers from Portainer for one endpoint, upsert into DB, prune stale rows.
    Returns the number of containers now cached.
    """
    logger.info("container_inventory: syncing endpoint_id=%d", endpoint_id)
    try:
        raw = await portainer_service.list_containers(endpoint_id, all_containers=True)
    except portainer_service.PortainerError as exc:
        logger.warning(
            "container_inventory: Portainer error for endpoint %d: %s", endpoint_id, exc,
        )
        return count_containers_in_db(db, endpoint_id)

    _upsert_containers(db, raw, endpoint_id, endpoint_name="")
    return count_containers_in_db(db, endpoint_id)


async def populate_all(db: Session) -> dict:
    """
    Full sweep: list endpoints → list containers per endpoint → upsert.
    Returns {"endpoints": int, "containers": int}.
    """
    # Hygiene: drop rows from the retired multi-workgroup era (live containers
    # are re-keyed by the upsert anyway — container_id is the PK).
    stale = (
        db.query(ContainerStateCache)
        .filter(ContainerStateCache.workgroup != _WORKGROUP)
        .delete(synchronize_session=False)
    )
    if stale:
        db.commit()
        logger.info("container_inventory: pruned %d stale rows from old workgroups", stale)

    total_endpoints = total_containers = 0
    try:
        endpoints = await portainer_service.list_endpoints()
    except portainer_service.PortainerNotConfigured as exc:
        logger.debug("container_inventory: skipping sweep — %s", exc)
        return {"endpoints": 0, "containers": 0}
    except portainer_service.PortainerError as exc:
        logger.warning("container_inventory: cannot list endpoints: %s", exc)
        return {"endpoints": 0, "containers": 0}

    for ep in endpoints:
        ep_id = ep.get("Id") if isinstance(ep, dict) else getattr(ep, "id", None)
        ep_name = (ep.get("Name") if isinstance(ep, dict) else getattr(ep, "name", "")) or ""
        if ep_id is None:
            continue
        try:
            raw = await portainer_service.list_containers(ep_id, all_containers=True)
            _upsert_containers(db, raw, ep_id, ep_name)
            total_endpoints += 1
            total_containers += len(raw)
        except portainer_service.PortainerError as exc:
            logger.warning(
                "container_inventory: cannot sync endpoint %d (%s): %s",
                ep_id, ep_name, exc,
            )

    logger.info(
        "container_inventory: full sweep done — endpoints=%d containers=%d",
        total_endpoints, total_containers,
    )
    return {"endpoints": total_endpoints, "containers": total_containers}


# ── Internal helpers ──────────────────────────────────────────────────────────

def _upsert_containers(
    db: Session,
    raw_list: list[dict],
    endpoint_id: int,
    endpoint_name: str,
) -> None:
    """
    Upsert all containers from raw_list for the endpoint.
    Prunes any DB rows for this scope whose container_id is not in raw_list.
    Single transaction.
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)  # naive UTC, SQLite-safe
    incoming_ids: set[str] = set()

    for raw in raw_list:
        cid = raw.get("Id", "")
        if not cid:
            continue

        names = raw.get("Names", [])
        primary_name = names[0].lstrip("/") if names else ""
        ports_str = json.dumps(_fmt_ports(raw.get("Ports", [])))
        incoming_ids.add(cid)

        row = db.get(ContainerStateCache, cid)
        if row is None:
            db.add(ContainerStateCache(
                container_id=cid,
                short_id=cid[:12],
                name=primary_name,
                image=raw.get("Image", ""),
                state=raw.get("State", ""),
                status=raw.get("Status", ""),
                ports=ports_str,
                endpoint_id=endpoint_id,
                endpoint_name=endpoint_name,
                workgroup=_WORKGROUP,
                created_ts=raw.get("Created", 0),
                last_updated=now,
            ))
        else:
            row.short_id = cid[:12]
            row.name = primary_name
            row.image = raw.get("Image", row.image)
            row.state = raw.get("State", row.state)
            row.status = raw.get("Status", row.status)
            row.ports = ports_str
            row.endpoint_id = endpoint_id
            row.endpoint_name = endpoint_name
            row.workgroup = _WORKGROUP
            row.created_ts = raw.get("Created", row.created_ts)
            row.last_updated = now

    # Prune containers no longer present on this endpoint
    if incoming_ids:
        (
            db.query(ContainerStateCache)
            .filter(
                ContainerStateCache.workgroup == _WORKGROUP,
                ContainerStateCache.endpoint_id == endpoint_id,
                ContainerStateCache.container_id.notin_(incoming_ids),
            )
            .delete(synchronize_session=False)
        )
    else:
        # Portainer returned empty list — clear the entire endpoint scope
        (
            db.query(ContainerStateCache)
            .filter(
                ContainerStateCache.workgroup == _WORKGROUP,
                ContainerStateCache.endpoint_id == endpoint_id,
            )
            .delete(synchronize_session=False)
        )

    db.commit()


def get_fresh_db() -> Session:
    """Open a new DB session for use in background tasks."""
    return SessionLocal()
