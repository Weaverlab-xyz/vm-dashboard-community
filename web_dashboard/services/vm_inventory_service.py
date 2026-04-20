"""
VM Inventory Service — DB-first VM list management.

Responsibilities:
  - get_vms_from_db: instant DB read, returns VMInfo objects
  - populate_db_from_ps: full PS scan → upsert all rows, prune stale entries
  - delta_sync: fast path — only asks PS for VMX paths NOT already in DB
  - sync_running_state: after list_running_vms returns, stamp is_running + ip_address
  - check_vm_online / check_workgroup_online: reachability checks
      dev/ssh mode  → Python socket.connect_ex (container is on the same LAN as VMs)
      automation    → PS check_online_batch runbook via Hybrid Worker (Container App
                      cannot reach private VM IPs directly)
"""
import asyncio
import logging
import os
import socket
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from ..database import VMStateCache, SessionLocal
from ..models.vm import VMInfo
from ..services import powershell

_EXECUTION_MODE = os.getenv("POWERSHELL_EXECUTION_MODE", "local")

logger = logging.getLogger(__name__)

# How often (seconds) a full reconciliation runs in the background.
# Delta sync runs on every request; full sync catches deleted/renamed VMX files.
FULL_SYNC_INTERVAL = 1800  # 30 minutes

# Timestamp of last full sync (per process — good enough for single-worker setups)
_last_full_sync: Optional[datetime] = None
_full_sync_lock = asyncio.Lock()


# ── Public helpers ────────────────────────────────────────────────────────────

def get_vms_from_db(db: Session, workgroups: list[str]) -> list[VMInfo]:
    """Return all VMStateCache rows for the given workgroups as VMInfo objects."""
    q = db.query(VMStateCache)
    if workgroups:
        q = q.filter(VMStateCache.workgroup.in_(workgroups))
    rows = q.order_by(VMStateCache.vm_name).all()
    return [
        VMInfo(
            vmx_path=r.vmx_path,
            vm_name=r.vm_name or "",
            workgroup=r.workgroup or "",
            os_type=r.os_type,
            is_running=r.is_running,
            ip_address=r.ip_address,
            last_seen_running_at=r.last_seen_running_at.isoformat() if r.last_seen_running_at else None,
            is_online=r.is_online,
            last_online_check_at=r.last_online_check_at.isoformat() if r.last_online_check_at else None,
        )
        for r in rows
    ]


def count_vms_in_db(db: Session, workgroups: list[str]) -> int:
    """Return how many VMs are currently stored for the given workgroups."""
    q = db.query(VMStateCache)
    if workgroups:
        q = q.filter(VMStateCache.workgroup.in_(workgroups))
    return q.count()


async def populate_db_from_ps(db: Session, workgroups: list[str]) -> dict:
    """
    Full PS scan: call list_vms, upsert every row, delete rows no longer on disk.
    Returns {"added": int, "updated": int, "removed": int}.
    """
    logger.info("vm_inventory: starting full PS scan")
    result = await powershell.execute("list_vms", {})
    ps_vms = result.get("vms", [])

    # Filter to user's workgroups
    if workgroups:
        ps_vms = [v for v in ps_vms if v.get("workgroup") in workgroups]

    ps_paths = {v["vmx_path"] for v in ps_vms}

    # Existing DB paths for these workgroups
    existing = {
        row.vmx_path
        for row in db.query(VMStateCache.vmx_path)
        .filter(VMStateCache.workgroup.in_(workgroups) if workgroups else True)
        .all()
    }

    added = updated = removed = 0
    now = datetime.now(timezone.utc).replace(tzinfo=None)  # SQLite-safe naive UTC

    for vm in ps_vms:
        path = vm["vmx_path"]
        row = db.get(VMStateCache, path)
        if row is None:
            db.add(VMStateCache(
                vmx_path=path,
                vm_name=vm.get("vm_name", ""),
                workgroup=vm.get("workgroup", ""),
                os_type=vm.get("os_type"),
                is_running=False,
                last_updated=now,
            ))
            added += 1
        else:
            row.vm_name = vm.get("vm_name", row.vm_name)
            row.workgroup = vm.get("workgroup", row.workgroup)
            row.os_type = vm.get("os_type", row.os_type)
            row.last_updated = now
            updated += 1

    # Remove stale rows (VMX deleted from disk)
    stale = existing - ps_paths
    if stale:
        db.query(VMStateCache).filter(VMStateCache.vmx_path.in_(stale)).delete(synchronize_session=False)
        removed = len(stale)

    db.commit()
    logger.info("vm_inventory: full sync done — added=%d updated=%d removed=%d", added, updated, removed)
    return {"added": added, "updated": updated, "removed": removed}


async def delta_sync(db: Session, workgroups: list[str]) -> int:
    """
    Fast incremental sync: ask PS only for VMX paths NOT already in the DB.
    Returns number of new VMs inserted.
    """
    existing_paths = [
        row.vmx_path
        for row in db.query(VMStateCache.vmx_path)
        .filter(VMStateCache.workgroup.in_(workgroups) if workgroups else True)
        .all()
    ]

    result = await powershell.execute("list_vms_delta", {"known_paths": existing_paths})
    new_vms = result.get("vms", [])

    if not new_vms:
        return 0

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for vm in new_vms:
        path = vm["vmx_path"]
        if db.get(VMStateCache, path) is None:
            db.add(VMStateCache(
                vmx_path=path,
                vm_name=vm.get("vm_name", ""),
                workgroup=vm.get("workgroup", ""),
                os_type=vm.get("os_type"),
                is_running=False,
                last_updated=now,
            ))

    db.commit()
    logger.info("vm_inventory: delta sync inserted %d new VM(s)", len(new_vms))
    return len(new_vms)


def sync_running_state(db: Session, running_vms: list[dict]) -> None:
    """
    After list_running_vms returns, update is_running + ip_address on DB rows.
    Marks all DB rows as stopped first, then stamps running ones.
    Called synchronously inside background tasks — no await needed.
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    # Mark everything stopped
    db.query(VMStateCache).filter(VMStateCache.is_running == True).update(  # noqa: E712
        {"is_running": False, "ip_address": None, "last_updated": now},
        synchronize_session=False,
    )

    # Stamp running VMs
    for vm in running_vms:
        path = vm.get("vmx_path")
        if not path:
            continue
        row = db.get(VMStateCache, path)
        if row:
            row.is_running = True
            row.ip_address = vm.get("ip_address")
            row.last_seen_running_at = now
            row.last_updated = now

    db.commit()


async def maybe_full_sync(db: Session, workgroups: list[str]) -> None:
    """
    Trigger a full PS reconciliation if FULL_SYNC_INTERVAL has elapsed.
    Uses a process-level lock so only one worker runs it at a time.
    """
    global _last_full_sync
    now = datetime.now(timezone.utc)

    if _last_full_sync is not None:
        elapsed = (now - _last_full_sync).total_seconds()
        if elapsed < FULL_SYNC_INTERVAL:
            return

    if _full_sync_lock.locked():
        return  # another coroutine is already syncing

    async with _full_sync_lock:
        # Re-check after acquiring lock
        if _last_full_sync is not None and (now - _last_full_sync).total_seconds() < FULL_SYNC_INTERVAL:
            return
        try:
            await populate_db_from_ps(db, workgroups)
            _last_full_sync = datetime.now(timezone.utc)
        except Exception as exc:
            logger.warning("vm_inventory: full sync failed: %s", exc)


def get_fresh_db() -> Session:
    """Open a new DB session for use in background tasks."""
    return SessionLocal()


# ── Post-operation DB stamps ──────────────────────────────────────────────────

def set_vm_running_in_db(db: Session, vmx_path: str, ip_address: Optional[str] = None) -> None:
    """Optimistically mark a single VM as running after a successful start job."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    row = db.get(VMStateCache, vmx_path)
    if row:
        row.is_running = True
        row.last_seen_running_at = now
        row.last_updated = now
        if ip_address:
            row.ip_address = ip_address
        db.commit()


def set_vm_stopped_in_db(db: Session, vmx_path: str) -> None:
    """Optimistically mark a single VM as stopped after a successful stop job."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    row = db.get(VMStateCache, vmx_path)
    if row:
        row.is_running = False
        row.last_updated = now
        db.commit()


# ── Network reachability checks (stdlib only, no PS) ─────────────────────────

def _tcp_check(ip: str, port: int, timeout: float = 1.5) -> bool:
    """Try a TCP connect to ip:port. Returns True if connection succeeds."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            return s.connect_ex((ip, port)) == 0
    except Exception:
        return False


def _choose_port(os_type: Optional[str]) -> int:
    """Return the most likely open port for a given os_type string."""
    if os_type and ("windows" in os_type.lower() or "freebsd" in os_type.lower()):
        return 3389  # RDP
    return 22  # SSH for Linux


def check_vm_online(db: Session, vmx_path: str) -> dict:
    """
    Check network reachability for a single VM.
    In automation mode this delegates to check_workgroup_online internally (batch is more
    efficient), but the public signature is unchanged so the API endpoint stays simple.
    """
    results = _check_rows(db, db.query(VMStateCache).filter(VMStateCache.vmx_path == vmx_path).all())
    return results[0] if results else {"vmx_path": vmx_path, "is_online": None, "error": "not found"}


def check_workgroup_online(db: Session, workgroup: str) -> list[dict]:
    """Check network reachability for every VM in a workgroup."""
    rows = db.query(VMStateCache).filter(VMStateCache.workgroup == workgroup).all()
    return _check_rows(db, rows)


def _check_rows(db: Session, rows: list) -> list[dict]:
    """
    Core dispatcher: socket checks in dev/ssh mode, PS runbook in automation mode.
    Updates is_online + last_online_check_at on each DB row, then commits once.
    """
    if not rows:
        return []

    now = datetime.now(timezone.utc).replace(tzinfo=None)

    if _EXECUTION_MODE == "automation":
        return _check_rows_via_ps(db, rows, now)
    else:
        return _check_rows_via_socket(db, rows, now)


def _check_rows_via_socket(db: Session, rows: list, now: datetime) -> list[dict]:
    """Local TCP connect — works when the app is on the same network as the VMs (dev/ssh)."""
    results = []
    for row in rows:
        ip = row.ip_address
        if not ip:
            row.last_online_check_at = now
            results.append({"vmx_path": row.vmx_path, "vm_name": row.vm_name, "is_online": None, "reason": "no_ip"})
            continue
        online = _tcp_check(ip, _choose_port(row.os_type))
        row.is_online = online
        row.last_online_check_at = now
        results.append({
            "vmx_path": row.vmx_path,
            "vm_name": row.vm_name,
            "ip_address": ip,
            "is_online": online,
            "checked_at": now.isoformat(),
        })
    db.commit()
    return results


def _check_rows_via_ps(db: Session, rows: list, now: datetime) -> list[dict]:
    """
    Batch PS runbook via Hybrid Worker — used in prod (automation mode) where the
    Container App cannot reach private VM IPs directly.
    Runs synchronously inside asyncio.to_thread; spawns its own event loop.
    """
    vms_payload = [
        {"vmx_path": r.vmx_path, "ip_address": r.ip_address or "", "port": _choose_port(r.os_type)}
        for r in rows
    ]

    try:
        loop = asyncio.new_event_loop()
        try:
            ps_result = loop.run_until_complete(
                powershell.execute("check_online_batch", {"vms": vms_payload})
            )
        finally:
            loop.close()
    except Exception as exc:
        logger.warning("check_online_batch PS call failed: %s", exc)
        return [{"vmx_path": r.vmx_path, "vm_name": r.vm_name, "is_online": None, "error": str(exc)} for r in rows]

    # Index DB rows by vmx_path for quick lookup
    row_by_path = {r.vmx_path: r for r in rows}
    results = []
    for item in ps_result.get("results", []):
        path = item.get("vmx_path", "")
        row = row_by_path.get(path)
        if row:
            row.is_online = item.get("is_online")
            row.last_online_check_at = now
        results.append({
            "vmx_path": path,
            "vm_name": row.vm_name if row else "",
            "ip_address": item.get("ip_address"),
            "is_online": item.get("is_online"),
            "checked_at": item.get("checked_at", now.isoformat()),
        })
    db.commit()
    return results
