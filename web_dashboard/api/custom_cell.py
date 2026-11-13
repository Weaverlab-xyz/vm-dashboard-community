"""Custom demo cells: blueprint authoring (Cell Builder) and, on top, launching cells.

Routes under ``/api/demo/custom-cells``, mounted behind the estate profile's
``demo_pages`` gate like the rest of the Demo hub's API.

Gates (api/demo_gates):

| route                        | gate                                              |
|------------------------------|---------------------------------------------------|
| GET  /blueprints[/{id}]      | `demo_blueprints:read`, or `demo:write` (to launch) |
| POST /blueprints             | `demo_blueprints:write`                           |
| PUT  /blueprints/{id}        | `demo_blueprints:write`                           |
| DELETE /blueprints/{id}      | `demo_blueprints:delete`                          |
| POST /cells (launch)         | `demo:write`                                      |
| GET  /cells[/{id}]           | `demo:read`                                       |
| POST /cells/{id}/end-at      | `demo:write`                                      |
| DELETE /cells/{id}           | `demo:delete`                                     |

A cell is visible to its workgroup; anything else is a 404.

A blueprint outside the caller's workgroups answers 404, the same as one that does not
exist. A blueprint with no workgroup is visible to everyone who passes the gate.
"""
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..database import User, get_db
from ..services import custom_cell_service as svc
from ..models.schedule import DemoScheduleMixin
from .demo_gates import require_blueprints, require_demo
from .gcp import _accessible_workgroups, _validate_workgroup

router = APIRouter(prefix="/api/demo/custom-cells", tags=["demo"])


class BlueprintItem(BaseModel):
    kind: str
    cloud: str
    region: str = ""
    label: str
    count: int = 1
    spec: Dict[str, Any] = Field(default_factory=dict)


class BlueprintRequest(BaseModel):
    name: str
    description: str = ""
    workgroup: str = ""
    items: List[BlueprintItem]
    ttl_hours: Optional[float] = None
    suspend_at_local: str = ""
    resume_at_local: str = ""
    schedule_timezone: str = ""
    schedule_days: str = ""


def _bad(exc: Exception):
    raise HTTPException(status_code=400, detail=str(exc))


def _blueprint_or_404(db: Session, user: User, blueprint_id: str):
    bp = svc.get_blueprint(db, blueprint_id, _accessible_workgroups(user))
    if bp is None:
        raise HTTPException(status_code=404, detail="Blueprint not found")
    return bp


@router.get("/blueprints")
def list_blueprints(db: Session = Depends(get_db),
                    current_user: User = Depends(require_blueprints("read"))):
    return {"blueprints": svc.list_blueprints(db, _accessible_workgroups(current_user))}


@router.get("/blueprints/{blueprint_id}")
def get_blueprint(blueprint_id: str, db: Session = Depends(get_db),
                  current_user: User = Depends(require_blueprints("read"))):
    return svc.serialize(db, _blueprint_or_404(db, current_user, blueprint_id))


@router.post("/blueprints", status_code=201)
def create_blueprint(body: BlueprintRequest, db: Session = Depends(get_db),
                     current_user: User = Depends(require_blueprints("write"))):
    wg = _validate_workgroup(db, current_user, body.workgroup) if body.workgroup.strip() else None
    try:
        bp = svc.create_blueprint(db, body.model_dump(), created_by=current_user.username,
                                  workgroup=wg)
    except svc.CellError as exc:
        _bad(exc)
    from ..services import job_service
    job_service.log_audit(db, current_user.username, "cell_blueprint.create",
                          details={"blueprint": bp.name, "id": bp.id})
    return svc.serialize(db, bp)


@router.put("/blueprints/{blueprint_id}")
def update_blueprint(blueprint_id: str, body: Dict[str, Any], db: Session = Depends(get_db),
                     current_user: User = Depends(require_blueprints("write"))):
    """Partial update from a raw dict, as the POV blueprint PUT does: only the keys sent
    change, and ``items`` (when sent) replaces the whole list."""
    bp = _blueprint_or_404(db, current_user, blueprint_id)
    data = {k: v for k, v in (body or {}).items()
            if k in BlueprintRequest.model_fields and k != "workgroup"}
    if "items" in data:
        try:
            data["items"] = [BlueprintItem(**i).model_dump() for i in (data["items"] or [])]
        except Exception as exc:  # noqa: BLE001 — pydantic.ValidationError
            _bad(exc)
    if "workgroup" in (body or {}):
        raw = (body.get("workgroup") or "").strip()
        bp.workgroup = _validate_workgroup(db, current_user, raw) if raw else None
    try:
        bp = svc.update_blueprint(db, bp, data)
    except svc.CellError as exc:
        db.rollback()
        _bad(exc)
    from ..services import job_service
    job_service.log_audit(db, current_user.username, "cell_blueprint.update",
                          details={"blueprint": bp.name, "id": bp.id})
    return svc.serialize(db, bp)


@router.delete("/blueprints/{blueprint_id}")
def delete_blueprint(blueprint_id: str, db: Session = Depends(get_db),
                     current_user: User = Depends(require_blueprints("delete"))):
    bp = _blueprint_or_404(db, current_user, blueprint_id)
    name = bp.name
    svc.delete_blueprint(db, bp)
    from ..services import job_service
    job_service.log_audit(db, current_user.username, "cell_blueprint.delete",
                          details={"blueprint": name, "id": blueprint_id})
    return {"deleted": blueprint_id}


# ══ Cells ═════════════════════════════════════════════════════════════════════

class LaunchRequest(DemoScheduleMixin):
    """Launch a cell. ``run_at``/``run_timezone`` book it for later (blank = now) and
    ``ttl_hours`` is how long it lives from that start (blank = the blueprint's, else the
    auto-delete default). The suspend schedule, when sent, replaces the blueprint's."""
    blueprint_id: str
    name: str
    workgroup: str
    suspend_at_local: Optional[str] = None
    resume_at_local: Optional[str] = None
    schedule_timezone: Optional[str] = None
    schedule_days: Optional[str] = None


def _cell_or_404(db: Session, user: User, cell_id: str):
    cell = svc.get_cell(db, cell_id, _accessible_workgroups(user))
    if cell is None:
        raise HTTPException(status_code=404, detail="Cell not found")
    return cell


@router.post("/cells", status_code=201)
async def launch_cell(body: LaunchRequest, db: Session = Depends(get_db),
                      current_user: User = Depends(require_demo("write"))):
    bp = _blueprint_or_404(db, current_user, body.blueprint_id)
    workgroup = _validate_workgroup(db, current_user, body.workgroup)
    sched_keys = ("suspend_at_local", "resume_at_local", "schedule_timezone", "schedule_days")
    schedule = None
    if any(getattr(body, k) is not None for k in sched_keys):
        schedule = {k: getattr(body, k) or "" for k in sched_keys}
    try:
        cell = await svc.launch(db, bp, user=current_user, name=body.name,
                                workgroup=workgroup, schedule_fields=body.schedule_fields(),
                                ttl_hours=body.ttl_hours, schedule=schedule)
    except svc.CellError as exc:
        db.rollback()
        _bad(exc)
    from ..services import job_service
    job_service.log_audit(db, current_user.username, "custom_cell.launch",
                          details={"cell": cell.name, "id": cell.id, "blueprint": bp.name,
                                   "scheduled_for": cell.scheduled_for.isoformat()
                                   if cell.scheduled_for else "",
                                   "end_at": cell.expires_at.isoformat()
                                   if cell.expires_at else ""})
    return svc.serialize_cell(db, cell)


@router.get("/cells")
def list_cells(include_destroyed: bool = False, db: Session = Depends(get_db),
               current_user: User = Depends(require_demo("read"))):
    cells = svc.list_cells(db, _accessible_workgroups(current_user),
                           include_destroyed=include_destroyed)
    return {"cells": [svc.serialize_cell(db, c, members=False) for c in cells]}


@router.get("/cells/{cell_id}")
def get_cell(cell_id: str, db: Session = Depends(get_db),
             current_user: User = Depends(require_demo("read"))):
    cell = _cell_or_404(db, current_user, cell_id)
    svc.refresh(db, cell)
    return svc.serialize_cell(db, cell)


class EndAtRequest(BaseModel):
    """When the cell ends: ``ttl_hours`` from now, or an ISO ``end_at``. Same contract
    as the OT/network cells' ``end-at`` (api/demo.set_cell_end), and the same ceiling."""
    ttl_hours: Optional[float] = Field(default=None, gt=0, le=720)
    end_at: str = ""


@router.post("/cells/{cell_id}/end-at")
def set_cell_end(cell_id: str, body: EndAtRequest, db: Session = Depends(get_db),
                 current_user: User = Depends(require_demo("write"))):
    from datetime import timedelta

    from ..services import expiry_policy, job_service
    from .demo import _parse_end
    cell = _cell_or_404(db, current_user, cell_id)
    if cell.status in ("destroying", "destroyed"):
        raise HTTPException(status_code=409, detail="This cell is already being destroyed.")
    if bool(body.ttl_hours) == bool(body.end_at.strip()):
        raise HTTPException(status_code=400, detail="Send ttl_hours or end_at (one of them).")
    now = svc._now()
    base = cell.scheduled_for if cell.scheduled_for and cell.scheduled_for > now else now
    asked = (_parse_end(body.end_at) if body.end_at.strip()
             else base + timedelta(hours=float(body.ttl_hours)))
    hours = max(0.0, (asked - base).total_seconds() / 3600.0)
    clamped = expiry_policy.clamp_hours(hours, created_at=cell.created_at,
                                        is_admin=current_user.is_effective_admin, now=now)
    end = base + timedelta(hours=clamped)
    svc.set_end(db, cell, end)
    job_service.log_audit(db, current_user.username, "custom_cell.end_at",
                          details={"cell": cell.name, "id": cell.id, "end_at": end.isoformat()})
    return {"end_at": end.isoformat(), "clamped": abs(clamped - hours) > 1e-6}


@router.delete("/cells/{cell_id}")
def destroy_cell(cell_id: str, db: Session = Depends(get_db),
                 current_user: User = Depends(require_demo("delete"))):
    from ..services import job_service
    cell = _cell_or_404(db, current_user, cell_id)
    cell = svc.start_destroy(db, cell, actor=current_user.username)
    job_service.log_audit(db, current_user.username, "custom_cell.destroy",
                          details={"cell": cell.name, "id": cell.id})
    return svc.serialize_cell(db, cell)
