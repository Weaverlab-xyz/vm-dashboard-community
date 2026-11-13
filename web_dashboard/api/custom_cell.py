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

A blueprint outside the caller's workgroups answers 404, the same as one that does not
exist. A blueprint with no workgroup is visible to everyone who passes the gate.
"""
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..database import User, get_db
from ..services import custom_cell_service as svc
from .demo_gates import require_blueprints
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
