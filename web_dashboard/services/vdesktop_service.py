"""Virtual-desktop pool lifecycle — Phase 0 scaffold (DB only).

Phase 0 of the virtual-desktop plan. This manages ``virtual_desktops`` rows
only — **no cloud calls, no PRA**. ``create_pool`` inserts ``count`` seat rows in
``status="pending"``. Phase 1 fans pool creation out to the existing VM
provisioning path (one VM per seat, tagged ``POOL_TAG=<name>``) and fills
``vm_resource_id``; Phase 2 registers each seat on the PRA Jumpoint and fills
``pra_jump_id``.
"""
import logging

from sqlalchemy.orm import Session

from ..database import VirtualDesktop

logger = logging.getLogger(__name__)

# Tag stamped on each backing VM (Phase 1) so live pool state is recoverable
# from the cloud the same way dashboard-deployed VMs are.
POOL_TAG = "dashboard:desktop_pool"

VALID_CLOUDS = ("aws", "azure", "gcp")


class VDesktopError(Exception):
    pass


def _row_to_dict(row: VirtualDesktop) -> dict:
    return {
        "id":             row.id,
        "cloud":          row.cloud,
        "pool_name":      row.pool_name,
        "kind":           row.kind,
        "vm_resource_id": row.vm_resource_id,
        "status":         row.status,
        "assigned_user":  row.assigned_user,
        "pra_jump_id":    row.pra_jump_id,
        "created_by":     row.created_by,
        "created_at":     row.created_at.isoformat() if row.created_at else "",
    }


# ── Reads ─────────────────────────────────────────────────────────────────────

def list_desktops(db: Session) -> list[dict]:
    """Every seat, newest first."""
    rows = db.query(VirtualDesktop).order_by(VirtualDesktop.created_at.desc()).all()
    return [_row_to_dict(r) for r in rows]


def get_pool(db: Session, name: str) -> list[dict]:
    """All seats in one pool (oldest first)."""
    rows = (
        db.query(VirtualDesktop)
        .filter(VirtualDesktop.pool_name == name)
        .order_by(VirtualDesktop.created_at)
        .all()
    )
    return [_row_to_dict(r) for r in rows]


def list_pools(db: Session) -> list[dict]:
    """Group seats into pool summaries (pool_name, cloud, kind, count, statuses)."""
    rows = db.query(VirtualDesktop).all()
    pools: dict[str, dict] = {}
    for r in rows:
        p = pools.setdefault(r.pool_name, {
            "pool_name": r.pool_name, "cloud": r.cloud, "kind": r.kind,
            "count": 0, "statuses": {},
        })
        p["count"] += 1
        p["statuses"][r.status] = p["statuses"].get(r.status, 0) + 1
    return list(pools.values())


# ── Writes (Phase 0: rows only) ───────────────────────────────────────────────

def create_pool(db: Session, *, cloud: str, name: str, image: str, size: str,
                count: int, created_by: str) -> dict:
    """Create a desktop pool. Phase 0 inserts ``count`` pending seat rows.

    Phase 1 TODO: fan out to the VM provisioning path — one Terraform apply per
    seat from ``image``/``size``, tagged ``POOL_TAG=<name>`` — and fill
    ``vm_resource_id``."""
    name = (name or "").strip()
    if cloud not in VALID_CLOUDS:
        raise VDesktopError(f"Unknown cloud '{cloud}'. Valid: {', '.join(VALID_CLOUDS)}.")
    if not name:
        raise VDesktopError("pool name is required.")
    if count < 1:
        raise VDesktopError("count must be >= 1.")
    if get_pool(db, name):
        raise VDesktopError(f"Pool '{name}' already exists.")
    for _ in range(count):
        db.add(VirtualDesktop(
            cloud=cloud, pool_name=name, kind="vm_pool",
            status="pending", created_by=created_by,
        ))
    db.commit()
    logger.info("Created desktop pool %s (%s x%d) — Phase 0 (rows only)", name, cloud, count)
    return {"pool_name": name, "cloud": cloud, "count": count, "seats": get_pool(db, name)}


def scale_pool(db: Session, name: str, count: int) -> dict:
    """Grow/shrink a pool to ``count`` seats. Phase 0 adds/removes pending rows
    (Phase 1 will provision / deprovision the backing VM)."""
    if count < 0:
        raise VDesktopError("count must be >= 0.")
    seats = (
        db.query(VirtualDesktop)
        .filter(VirtualDesktop.pool_name == name)
        .order_by(VirtualDesktop.created_at)
        .all()
    )
    if not seats:
        raise VDesktopError(f"Pool '{name}' not found.")
    cur = len(seats)
    if count > cur:
        for _ in range(count - cur):
            db.add(VirtualDesktop(
                cloud=seats[0].cloud, pool_name=name, kind=seats[0].kind,
                status="pending", created_by=seats[0].created_by,
            ))
    elif count < cur:
        # Remove newest *pending* seats first — never silently drop a running one.
        removable = [s for s in reversed(seats) if s.status == "pending"]
        for s in removable[: cur - count]:
            db.delete(s)
    db.commit()
    return {"pool_name": name, "count": len(get_pool(db, name))}


def delete_pool(db: Session, name: str) -> int:
    """Delete a pool's seat rows. Returns the count removed. Phase 1 will
    deprovision the backing VMs first."""
    seats = db.query(VirtualDesktop).filter(VirtualDesktop.pool_name == name).all()
    n = len(seats)
    for s in seats:
        db.delete(s)
    db.commit()
    logger.info("Deleted desktop pool %s (%d seats) — Phase 0", name, n)
    return n
