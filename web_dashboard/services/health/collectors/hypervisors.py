"""Hypervisors: is each connection reachable, and are its hosts and storage healthy?

* **A direct connection** is dialled with the same one cheap authenticated read the
  Connections page's **Test** makes, and the outcome is stamped on the connection
  (``record_result``) so that page agrees with this one. vSphere hosts that are
  disconnected or in maintenance, Proxmox nodes that are offline and Proxmox storage
  running out are reported from the reads that already exist.
* **An agent-bound connection** is never dialled: the dashboard has no route to it,
  which is why it is bound. Its health is its agent's, plus whether the inventory sync
  that agent runs is keeping up.

The VMs on a hypervisor are the Resources area's, from the same sync cache.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

from .. import core
from ....utils.clock import utcnow

logger = logging.getLogger(__name__)

STORAGE_WARN_FREE = 0.15
STORAGE_FAIL_FREE = 0.05
STALE_SYNCS = 3          # a sync this many intervals overdue is not keeping up

_KIND_LABELS = {"vsphere": "vSphere", "proxmox": "Proxmox", "hyperv": "Hyper-V",
                "nutanix": "Nutanix", "xcpng": "XCP-ng"}
_PAGES = {"vsphere": "/vsphere", "proxmox": "/proxmox", "hyperv": "/hyperv",
          "nutanix": "/nutanix", "xcpng": "/xcpng"}


def sync_check(last_sync_at: Optional[datetime], interval_minutes: int, *,
               last_error: str = "", last_ok_at: Optional[datetime] = None,
               now: datetime) -> dict:
    """Whether an agent-run inventory sync is keeping up. Pure."""
    if last_error and (not last_ok_at or not last_sync_at or last_ok_at <= last_sync_at):
        return core.check("sync", "Inventory sync", core.FAIL,
                          f"The last sync failed: {core.text(last_error, 300)}")
    if not last_sync_at:
        return core.check("sync", "Inventory sync", core.UNKNOWN, "Not synced yet.")
    late = now - last_sync_at > timedelta(minutes=interval_minutes * STALE_SYNCS)
    return core.check("sync", "Inventory sync", core.WARN if late else core.OK,
                      f"Last synced {core.ago(last_sync_at, now)}."
                      + (" It should have synced again by now." if late else ""))


def vsphere_host_checks(hosts: list) -> list:
    """One check over every ESXi host. Pure."""
    down = [h["name"] for h in hosts if h.get("status") not in ("connected",)]
    maint = [h["name"] for h in hosts if h.get("in_maintenance")]
    if down:
        return [core.check("hosts", "Hosts", core.FAIL,
                           f"Not connected: {', '.join(down[:10])}.")]
    if maint:
        return [core.check("hosts", "Hosts", core.WARN,
                           f"In maintenance: {', '.join(maint[:10])}.")]
    return [core.check("hosts", "Hosts", core.OK, f"{len(hosts)} host(s) connected.")]


def proxmox_node_checks(nodes: list, storage: dict) -> list:
    """Node status, and free space on each node's image storage. ``storage`` is
    ``{node: [storage rows]}`` for the online nodes. Pure."""
    out = []
    offline = [n["node"] for n in nodes if n.get("status") != "online"]
    out.append(core.check("nodes", "Nodes", core.FAIL if offline else core.OK,
                          f"Offline: {', '.join(offline[:10])}." if offline else
                          f"{len(nodes)} node(s) online."))
    worst, notes = core.OK, []
    for node, rows in storage.items():
        for s in rows:
            total = s.get("total") or 0
            if not total:
                continue
            free = (s.get("avail") or 0) / total
            if free < STORAGE_FAIL_FREE:
                worst = core.FAIL
            elif free < STORAGE_WARN_FREE and worst != core.FAIL:
                worst = core.WARN
            else:
                continue
            notes.append(f"{node}/{s.get('storage')} {free:.0%} free")
    if storage:
        out.append(core.check("storage", "Storage", worst,
                              ("Low: " + ", ".join(notes[:10]) + ".") if notes else
                              "Every image store has room."))
    return out


async def _direct(conn) -> list:
    """Reachability plus host checks for a connection the dashboard dials itself."""
    kind = conn.kind
    if kind == "vsphere":
        from ... import vsphere_service
        return vsphere_host_checks(await vsphere_service.list_hosts(conn))
    if kind == "proxmox":
        from ... import proxmox_service
        nodes = await proxmox_service.list_nodes(conn)
        storage = {}
        for n in nodes:
            if n.get("status") == "online":
                storage[n["node"]] = await proxmox_service.list_storage(conn, n["node"])
        return proxmox_node_checks(nodes, storage)
    if kind == "nutanix":
        from ... import nutanix_service
        clusters = await nutanix_service.list_clusters(conn)
        return [core.check("clusters", "Clusters", core.OK,
                           f"{len(clusters)} cluster(s) answered.")]
    if kind == "xcpng":
        from ... import xcpng_service
        await xcpng_service.list_vms(conn)
    elif kind == "hyperv":
        from ... import hyperv_service
        await hyperv_service.list_vms(conn)
    return []


async def collect(db) -> list:
    from ... import agent_service, feature_flags, hypervisor_connection_service as hcs
    from ... import hypervisor_sync_service
    from ....database import HypervisorConnection, RemoteAgent
    now = utcnow()
    items = []
    rows = (db.query(HypervisorConnection)
            .filter(HypervisorConnection.is_active.is_(True))
            .order_by(HypervisorConnection.kind, HypervisorConnection.name).all())
    for row in rows:
        if not feature_flags.enabled(f"{row.kind}_enabled"):
            continue
        label = _KIND_LABELS.get(row.kind, row.kind)
        if row.agent_id:
            agent = db.get(RemoteAgent, row.agent_id)
            state = agent_service.status_of(agent, now=now) if agent else "missing"
            checks = [core.check(
                "agent", f"Remote agent{' ' + repr(agent.name) if agent else ''}",
                core.OK if state == "online" else core.FAIL,
                "Online." if state == "online" else
                ("The agent this connection is bound to is gone." if not agent else
                 f"{state.capitalize()}, so this hypervisor cannot be reached."))]
            checks.append(sync_check(row.last_sync_at,
                                     hypervisor_sync_service._interval_minutes(row),
                                     last_error=row.last_error or "",
                                     last_ok_at=row.last_ok_at, now=now))
        else:
            try:
                conn = hcs.to_connection(row)
                extra = await _direct(conn)
                hcs.record_result(db, row.id)
                checks = [core.check("reach", "Reachable", core.OK,
                                     "Signed in and answered.")] + extra
            except Exception as exc:  # noqa: BLE001 - every SDK raises its own type
                logger.info("health: %s connection %s failed: %s", row.kind, row.name, exc)
                message = core.text(str(exc), 300) or type(exc).__name__
                hcs.record_result(db, row.id, error=message)
                checks = [core.check("reach", "Reachable", core.FAIL, message)]
        items.append(core.Item(object_id=row.id, name=row.name, kind=label,
                               href=_PAGES.get(row.kind, "/connections"), checks=checks))
    return items
