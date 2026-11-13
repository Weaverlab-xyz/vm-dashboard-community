"""The Demo hub's API: every demo cell this caller may see, and the launchpad.

The estate instance's counterpart to /pov. Mounted in main.py behind
``_profile_page_gate("demo_pages")``, so on a POV instance it 404s naming the profile, as
the page does. The summary is `demo:read`, explicit form (api/demo_gates); the cell routes
below each name their own gate.

Building, probing and re-wiring a cell stay on the cell's own routes (/api/ot,
/api/netcell), which accept `demo:<level>` beside the scope they always took. This router
adds only the two things those routes never had, both cell-scoped and both DELEGATING to
the cloud's own handler rather than re-implementing it:

* **The pickers a cell form needs** (``/cells/<cloud>/...``): images and network options,
  answered by the cloud module's own handler, so the response is byte-for-byte what the
  cloud page reads. A shared form only swaps its URL prefix. ``<cloud>:read`` or
  ``demo:write`` -- see ``demo_gates.require_cell_options``.
* **Destroying a cell** (``DELETE /cells/ot/{id}``, ``/cells/net/{id}``): the row is
  checked to BE a live, completed cell in the caller's workstreams, and then the cloud's
  own destroy handler runs on it -- so the PRA, Password Safe and Gateway teardown is
  exactly the one the cloud page's Destroy runs. ``<cloud>:delete`` or ``demo:delete``.
  The general ``DELETE /api/<cloud>/instances/...`` routes are untouched: a presenter can
  destroy a demo cell and nothing else.
"""
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..database import Job, User, get_db
from ..services import demo_cells, demo_launchpad, demo_schedule, expiry_policy
from .auth import get_current_user
from .demo_gates import may, require_cell_options, require_demo_read
from .gcp import _accessible_workgroups

router = APIRouter(prefix="/api/demo", tags=["demo"])

# Display order on the page, and the copy that says what each cell is for.
_CELLS = (
    ("ot", "OT demo cells",
     "A simulated plant -- FUXA HMI and PLC protocols -- reached only through PRA."),
    ("net", "Network demo cells",
     "A VyOS router with no inbound rule, managed through PRA and Password Safe."),
    ("agent", "Agent demo cells",
     "A non-human worker: SPIFFE identity, scoped token, and an off switch."),
)


@router.get("/summary")
def summary(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_demo_read),
):
    """The cells, by kind, and the launchpad.

    Workgroup scoping is GCP's ``_accessible_workgroups``, which every cloud module and
    the home page's tiles share, so a cell listed here is one the caller's own cloud page
    would show them.
    """
    accessible = _accessible_workgroups(current_user)

    def _may_read(scope):
        return may(current_user, scope, "read")

    results = {
        "ot": demo_cells.ot_cells(db, _may_read, accessible),
        "net": demo_cells.net_cells(db, _may_read, accessible),
        "agent": demo_cells.agent_cells(db, accessible, current_user.username),
    }
    cells = []
    for kind, title, blurb in _CELLS:
        res = results[kind]
        rows = res["cells"]
        cells.append({
            "kind": kind,
            "title": title,
            "blurb": blurb,
            "state": res["state"],
            "note": res["note"],
            "total": len(rows),
            "running": sum(1 for r in rows if r["status"] == "completed"),
            "cells": rows,
        })
    return {"cells": cells, "launchpad": demo_launchpad.describe()}


# ── The pickers a cell form needs ───────────────────────────────────────────────
#
# Each one is the cloud module's own handler, called with every parameter passed
# explicitly: a FastAPI `Query(...)` default left unpassed is a truthy object, not its
# value, and `bust=Query(False)` would silently bust the cache on every call.

@router.get("/cells/gcp/custom-images")
async def gcp_cell_images(current_user: User = Depends(require_cell_options("gcp"))):
    from . import gcp
    return await gcp.list_custom_images(current_user=current_user)


@router.get("/cells/gcp/network-options")
async def gcp_cell_network(zone: Optional[str] = None,
                           current_user: User = Depends(require_cell_options("gcp"))):
    from . import gcp
    return await gcp.network_options(zone=zone, bust=False, current_user=current_user)


@router.get("/cells/aws/amis")
async def aws_cell_images(region: Optional[str] = None,
                          current_user: User = Depends(require_cell_options("aws"))):
    from . import aws
    return await aws.list_amis(region=region, current_user=current_user)


@router.get("/cells/aws/network-options")
async def aws_cell_network(region: Optional[str] = None,
                           current_user: User = Depends(require_cell_options("aws"))):
    from . import aws
    return await aws.network_options(region=region, current_user=current_user)


@router.get("/cells/azure/images")
async def azure_cell_images(location: Optional[str] = None,
                            current_user: User = Depends(require_cell_options("azure"))):
    from . import azure
    return await azure.list_images(location=location, current_user=current_user)


@router.get("/cells/azure/network-options")
async def azure_cell_network(location: Optional[str] = None,
                             current_user: User = Depends(require_cell_options("azure"))):
    from . import azure
    return await azure.network_options(location=location, bust=False,
                                       current_user=current_user)


# ── Destroying a cell ───────────────────────────────────────────────────────────

_NOT_A_CELL = "{} is not a live {} cell"


def _live_cell(db: Session, user: User, job_id: str, marker: str, kind: str,
               statuses: tuple = ("completed",)) -> Job:
    """The completed, not-destroyed cell row ``job_id``, in this caller's workstreams.

    Anything else is a 404, including a row in another workstream -- the same answer
    GET /api/ot/cells gives by not listing it. ``completed`` only: a failed cell has no
    VM to destroy and is retired with Clear (DELETE /api/ot/cell/{id}) instead.
    """
    row = db.query(Job).filter(Job.id == job_id).first()
    meta = row.metadata_dict if row else {}
    if (row is None or not meta.get(marker) or meta.get("destroyed")
            or row.status not in statuses):
        raise HTTPException(status_code=404, detail=_NOT_A_CELL.format(job_id, kind))
    accessible = _accessible_workgroups(user)
    if accessible is not None and (row.workgroup or "").lower() not in accessible:
        raise HTTPException(status_code=404, detail=_NOT_A_CELL.format(job_id, kind))
    return row


def _require_destroy(user: User, cloud: str) -> None:
    if not may(user, cloud, "delete"):
        raise HTTPException(status_code=403,
                            detail=f"Requires '{cloud}:delete' or 'demo:delete' permission.")


async def _destroy_vm(db: Session, user: User, cloud: str, meta: dict) -> dict:
    """Run the cloud's own destroy handler on one VM, identified the way it wants."""
    blank = {"run_at": "", "run_timezone": "", "change_window_id": ""}
    if cloud == "gcp":
        from . import gcp
        return gcp.destroy_instance(instance_name=meta.get("instance_name") or "",
                                    zone=meta.get("zone") or "", current_user=user,
                                    db=db, **blank)
    if cloud == "aws":
        from . import aws
        return aws.destroy_instance(instance_id=str(meta.get("instance_id") or ""),
                                    db=db, current_user=user, **blank)
    from . import azure
    return await azure.destroy_vm(vm_name=meta.get("instance_name") or meta.get("vm_name") or "",
                                  db=db, current_user=user, **blank)


@router.delete("/cells/ot/{vm_job_id}")
async def destroy_ot_cell(vm_job_id: str, db: Session = Depends(get_db),
                          current_user: User = Depends(get_current_user)):
    """Destroy a running OT cell, and its DMZ broker when it has one.

    The cell first, the broker second, as the cloud pages do: the cell's teardown is
    the one that destroys the plant's Entitle agent token, and a broker deleted first
    would leave that agent pointing at nothing while it ran.
    """
    from ..services import ot_service
    row = _live_cell(db, current_user, vm_job_id, "ot_cell", "OT")
    cloud = ot_service.cell_cloud_for_job_type(row.job_type)
    if not cloud:
        raise HTTPException(status_code=404, detail=_NOT_A_CELL.format(vm_job_id, "OT"))
    _require_destroy(current_user, cloud)
    meta = row.metadata_dict
    out = {"cell": await _destroy_vm(db, current_user, cloud, meta)}
    broker_id = meta.get("ot_broker_job_id") or ""
    if broker_id:
        broker = db.query(Job).filter(Job.id == broker_id).first()
        bmeta = (broker.metadata_dict if broker else None) or {}
        if broker is not None and broker.status == "completed" and not bmeta.get("destroyed"):
            out["broker"] = await _destroy_vm(db, current_user, cloud, bmeta)
    return out


@router.delete("/cells/net/{job_id}")
async def destroy_net_cell(job_id: str, db: Session = Depends(get_db),
                           current_user: User = Depends(get_current_user)):
    """Destroy a network cell. GCP only; the deploy row IS the cell."""
    row = _live_cell(db, current_user, job_id, "netcell", "network")
    _require_destroy(current_user, "gcp")
    return {"cell": await _destroy_vm(db, current_user, "gcp", row.metadata_dict)}


class EndAtRequest(BaseModel):
    """When a cell should be destroyed. Exactly one of the two, or neither to go back to
    the auto-delete policy's default."""
    ttl_hours: Optional[float] = Field(default=None, gt=0, le=720)   # from now
    end_at: str = ""                                                  # ISO 8601; UTC unless it has an offset


def _parse_end(raw: str) -> datetime:
    try:
        at = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=400,
                            detail=f"{raw!r} is not a date and time (ISO 8601).")
    if at.tzinfo is not None:
        at = at.astimezone(timezone.utc).replace(tzinfo=None)
    return at


@router.post("/cells/{kind}/{job_id}/end-at")
def set_cell_end(kind: str, job_id: str, body: EndAtRequest, db: Session = Depends(get_db),
                 current_user: User = Depends(get_current_user)):
    """Set, move or clear when a demo cell is destroyed, for a presenter's own cells.

    This is NOT ``POST /api/expiry/set``, which authorises by who can SEE a resource and so
    only lets a non-admin extend: a presenter must be able to set a first timer and bring
    one forward, on a cell in their own workstream and nothing else. The ceiling still
    applies to them (``expiry_policy.clamp_hours``), measured from when the cell was made.

    A cell that has not started building yet is accepted too -- its end time then counts
    from its booked start.
    """
    if kind not in ("ot", "net"):
        raise HTTPException(status_code=404, detail=f"No such cell kind {kind!r}.")
    marker, label = ("ot_cell", "OT") if kind == "ot" else ("netcell", "network")
    row = _live_cell(db, current_user, job_id, marker, label,
                     statuses=("completed", "queued", "pending"))
    cloud = "gcp"
    if kind == "ot":
        from ..services import ot_service
        cloud = ot_service.cell_cloud_for_job_type(row.job_type) or ""
    if not cloud or not may(current_user, cloud, "write"):
        raise HTTPException(status_code=403,
                            detail=f"Requires '{cloud or 'cloud'}:write' or 'demo:write' permission.")
    now = demo_schedule.utcnow()
    if body.ttl_hours and body.end_at.strip():
        raise HTTPException(status_code=400, detail="Send ttl_hours or end_at, not both.")
    if not body.ttl_hours and not body.end_at.strip():
        # Back to the policy default: no demo end time, the same timer a plain deploy gets.
        end = expiry_policy.default_expiry_for(row.job_type, workgroup=row.workgroup, now=now)
        demo_schedule.set_end(db, row, None, fallback_expiry=end)
        return {"end_at": "", "expires_at": end.isoformat() if end else ""}
    asked = _parse_end(body.end_at) if body.end_at.strip() else now + _hours(body.ttl_hours)
    hours = max(0.0, (asked - now).total_seconds() / 3600.0)
    clamped = expiry_policy.clamp_hours(hours, created_at=row.created_at,
                                        is_admin=current_user.is_effective_admin, now=now)
    end = now + _hours(clamped)
    demo_schedule.set_end(db, row, end)
    from ..services import job_service
    job_service.log_audit(db, current_user.username, "demo_cell_end_at",
                          details={"job_id": job_id, "kind": kind, "end_at": end.isoformat(),
                                   "clamped": abs(clamped - hours) > 1e-6})
    return {"end_at": end.isoformat(), "clamped": abs(clamped - hours) > 1e-6}


def _hours(h: float):
    from datetime import timedelta
    return timedelta(hours=float(h))
