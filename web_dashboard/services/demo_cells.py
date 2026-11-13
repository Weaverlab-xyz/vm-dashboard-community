"""The demo cells, as rows: what the Demo hub lists and the home page's three tiles count.

One copy for both. The tiles used to carry the filtering inline (api/dashboard), and the hub
would have been a second reader of "which rows are live cells, and which may this caller
see" -- a count on the home page and a list on /demo that disagree about a cell is the
dead-tile bug in a new costume. So the tiles now call this and count its rows.

Every function here is a DATABASE read -- Job rows for the OT and network cells, AgentCell
rows for the agent cell -- and never a cloud or MCP call, which is the contract of the
dashboard endpoint that calls it.

Permission is decided by the caller and passed in, as ``may_read(scope) -> bool`` plus
the workgroup list from that page's own module. The rule lives in api/demo_gates
(`<cloud>:read` or `demo:read`), and a service importing the API layer to ask it would put
the arrow the wrong way round.

Each function returns ``{"state", "note", "cells"}`` where state is:
  * ``ok``          -- ``cells`` is the caller's list (possibly empty);
  * ``unavailable`` -- the cell cannot exist here (preview off, or a profile that serves
                       no cloud console), with ``note`` saying why;
  * ``forbidden``   -- it can exist, and this caller may read none of it.
"""
from __future__ import annotations

from typing import Callable, Iterable, Optional

from sqlalchemy.orm import Session

from ..database import Job
from . import feature_flags

_CLOUD_PAGES_OFF = "the cloud consoles are not served on this instance"


def _result(state: str, note: str = "", cells: Optional[list] = None) -> dict:
    return {"state": state, "note": note, "cells": cells or []}


def _iso(dt) -> str:
    return dt.isoformat() if dt else ""


def _in_workgroup(row_workgroup, accessible: Optional[Iterable[str]]) -> bool:
    return accessible is None or (row_workgroup or "").lower() in accessible


def _booked_start(row, meta: dict) -> str:
    """When a cell that has not started building is due to, else "" -- so a booked cell
    reads "scheduled", not "queued" (an unclaimable child looks the same as a busy one)."""
    if row.status not in ("queued", "pending"):
        return ""
    at = row.scheduled_for.isoformat() if row.scheduled_for else (meta.get("demo_start_at") or "")
    return at


def ot_cells(db: Session, may_read: Callable[[str], bool],
             accessible: Optional[Iterable[str]]) -> dict:
    """Live OT cells across every cloud this caller may read, newest first.

    A cell's VM-deploy child row IS its record (metadata ``ot_cell``), with the same
    filters ``GET /api/ot/cells`` applies: not destroyed, not cancelled, in the caller's
    workgroups. Gated on the ``cloud_pages`` profile group because every page that manages
    one is a cloud console, which a POV instance 404s.
    """
    if not feature_flags.profile_page_allowed("cloud_pages"):
        return _result("unavailable", _CLOUD_PAGES_OFF)
    from . import ot_service
    readable = {job_type: cloud for cloud, job_type in ot_service.CELL_CHILD_JOB_TYPE.items()
                if may_read(cloud)}
    if not readable:
        return _result("forbidden", "not permitted")
    cells = []
    rows = (db.query(Job).filter(Job.job_type.in_(list(readable)))
              .order_by(Job.created_at.desc()).all())
    for row in rows:
        meta = row.metadata_dict
        if not meta.get("ot_cell") or meta.get("destroyed") or row.status == "cancelled":
            continue
        if not _in_workgroup(row.workgroup, accessible):
            continue
        cloud = ot_service.cell_cloud_for_job_type(row.job_type) or readable[row.job_type]
        cells.append({
            "id": row.id,
            "kind": "ot",
            "cloud": cloud,
            "name": meta.get("instance_name") or meta.get("vm_name") or "",
            "status": row.status,
            # ot_service owns "is this cell fully wired", for the reason the tile gave
            # when it stopped carrying its own copy: several protocol tunnels.
            "wired": bool(row.status == "completed"
                          and ot_service.cell_wiring_complete(meta)),
            "workgroup": row.workgroup or "",
            "created_by": row.created_by or "",
            "created_at": _iso(row.created_at),
            "scheduled_for": _booked_start(row, meta),
            "end_at": meta.get("demo_end_at") or "",
            "manage_href": f"/{cloud}#ot",
        })
    return _result("ok", cells=cells)


def net_cells(db: Session, may_read: Callable[[str], bool],
              accessible: Optional[Iterable[str]]) -> dict:
    """Live network (VyOS) cells, GCP only, newest first. A cell IS its gce_deploy row."""
    if not feature_flags.profile_page_allowed("cloud_pages"):
        return _result("unavailable", _CLOUD_PAGES_OFF)
    # Unavailable rather than an empty list: zero says "go make one", on a tab that is not
    # rendered and behind a router that 404s.
    if not feature_flags.enabled("netcell_enabled"):
        return _result("unavailable", "the Network Demo Cell preview is off")
    if not may_read("gcp"):
        return _result("forbidden", "not permitted")
    from . import netcell_service
    cells = []
    rows = (db.query(Job).filter(Job.job_type == "gce_deploy")
              .order_by(Job.created_at.desc()).all())
    for row in rows:
        meta = row.metadata_dict
        if (not netcell_service.is_cell(meta) or meta.get("destroyed")
                or row.status == "cancelled"):
            continue
        if not _in_workgroup(row.workgroup, accessible):
            continue
        cells.append({
            "id": row.id,
            "kind": "net",
            "cloud": "gcp",
            "name": meta.get("instance_name") or "",
            "status": row.status,
            "wired": None,   # nothing to wire beyond the deploy -- see the tile's note
            "workgroup": row.workgroup or "",
            "created_by": row.created_by or "",
            "created_at": _iso(row.created_at),
            "scheduled_for": _booked_start(row, meta),
            "end_at": meta.get("demo_end_at") or "",
            "manage_href": "/gcp#net",
        })
    return _result("ok", cells=cells)


def agent_cells(db: Session, accessible: Optional[Iterable[str]],
                username: str) -> dict:
    """Agent cells this caller may see, newest first.

    No permission branch, because ``GET /api/agentcell/agents`` has none: any
    authenticated caller, scoped per row by ``agentcell_service.visible_to`` (workgroup
    OR creator). No ``cloud_pages`` gate either -- its page is /workload-lab, which is
    gated on a derived flag this preview is a constituent of.
    """
    if not feature_flags.enabled("agentcell_enabled"):
        return _result("unavailable", "the Agent Demo Cell preview is off")
    from ..database import AgentCell
    from . import agentcell_service
    cells = []
    for row in db.query(AgentCell).order_by(AgentCell.created_at.desc()).all():
        if not agentcell_service.visible_to(row, accessible, username):
            continue
        cells.append({
            "id": row.id,
            "kind": "agent",
            "cloud": row.cloud or "",
            "name": row.name or "",
            "status": row.status or "",
            # `authorized` rather than `wired`, as the tile explains: whether the token
            # it issued would still be accepted.
            "wired": bool(agentcell_service.token_live(row)),
            "workgroup": row.workgroup or "",
            "created_by": row.created_by or "",
            "created_at": _iso(row.created_at),
            "manage_href": "/workload-lab#agent",
        })
    return _result("ok", cells=cells)


def custom_cells(db: Session, accessible: Optional[Iterable[str]]) -> dict:
    """Live custom cells (services/custom_cell_service) in this caller's workgroups.

    No flag and no cloud gate of their own: each member was built through its own cloud's
    path, which checked that cloud when it ran. The row is the cell; its members are on
    ``GET /api/demo/custom-cells/cells/{id}``.
    """
    from . import custom_cell_service
    cells = []
    for row in custom_cell_service.list_cells(db, accessible):
        items = row.snapshot
        cells.append({
            "id": row.id,
            "kind": "custom",
            "cloud": ",".join(sorted({i.get("cloud", "") for i in items})),
            "name": row.name or "",
            "status": row.status or "",
            "wired": row.status == "active",
            "workgroup": row.workgroup or "",
            "created_by": row.created_by or "",
            "created_at": _iso(row.created_at),
            "scheduled_for": _iso(row.scheduled_for) if row.status == "scheduled" else "",
            "end_at": _iso(row.expires_at),
            "blueprint_name": row.blueprint_name or "",
            "summary": [f"{i['count']}x {i['kind']}" if i.get("count", 1) > 1 else i["kind"]
                        for i in items],
            "manage_href": "/demo#cells",
        })
    return _result("ok", cells=cells)
