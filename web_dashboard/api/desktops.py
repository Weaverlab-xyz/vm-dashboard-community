"""Virtual-desktop management API — Phase 0 scaffold.

Gated on ``vdesktops_enabled``. Phase 0 endpoints CRUD the ``virtual_desktops``
table via ``vdesktop_service`` — no cloud calls, no PRA.

  GET    /api/desktops/__phase0__       — health check (router-mounted probe)
  GET    /api/desktops                  — list seats
  GET    /api/desktops/pools            — list pool summaries
  POST   /api/desktops/pools            — create a pool (Phase 0: seat rows only)
  POST   /api/desktops/pools/{name}/scale
  DELETE /api/desktops/pools/{name}
"""
import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import User, get_db
from ..models.vdesktop import PoolCreateRequest, PoolScaleRequest
from ..services import vdesktop_service
from ..services.vdesktop_service import VDesktopError
from .auth import require_admin

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/desktops", tags=["desktops"])


@router.get("/__phase0__")
def phase0_status() -> dict:
    """Health check — confirms the router is mounted/reachable."""
    return {
        "phase": 0,
        "ok": True,
        "note": (
            "Virtual-desktop Phase 0 scaffold — virtual_desktops CRUD only; "
            "Phase 1 wires real VM provisioning, Phase 2 adds PRA brokering."
        ),
    }


@router.get("")
async def list_desktops(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Every desktop seat across all pools."""
    return {"desktops": vdesktop_service.list_desktops(db)}


@router.get("/pools")
async def list_pools(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Pool summaries (name, cloud, kind, seat count, status breakdown)."""
    return {"pools": vdesktop_service.list_pools(db)}


def _cfg(key: str, fallback: str = "") -> str:
    """Read a value from config_service (DB/wizard) with env-var fallback."""
    from ..config import settings
    from ..services import config_service
    return config_service.get(key) or getattr(settings, key, fallback)


def _azure_spec(payload: PoolCreateRequest) -> dict:
    """The azure_service.deploy_vm spec built from the pool request.

    Resource group + location fall back to the configured Azure defaults
    (``azure_resource_group`` / ``azure_location``) so the pool form can leave
    them blank — same resolution the Azure deploy path uses (``_rg``/``_loc``).
    """
    return {
        "location": payload.location or _cfg("azure_location") or "centralus",
        "resource_group": payload.resource_group or _cfg("azure_resource_group") or "vm-cli-rg",
        "vm_size": payload.vm_size or payload.size,
        "image_id": payload.image_id or payload.image,
        "image_publisher": payload.image_publisher, "image_offer": payload.image_offer,
        "image_sku": payload.image_sku, "image_version": payload.image_version,
        "subnet_id": payload.subnet_id, "nsg_ids": payload.nsg_ids,
        "create_public_ip": payload.create_public_ip,
        "ssh_username": payload.ssh_username, "ssh_public_key": payload.ssh_public_key,
    }


@router.post("/pools", status_code=201)
async def create_pool(
    payload: PoolCreateRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Create a desktop pool. Azure provisions one private VM per seat (async,
    tagged for the pool); AWS/GCP create records only (Phase 1 is Azure)."""
    spec = _azure_spec(payload) if payload.cloud == "azure" else None
    try:
        result = vdesktop_service.create_pool(
            db, cloud=payload.cloud, name=payload.name, count=payload.count,
            created_by=current_user.username, spec=spec,
        )
    except VDesktopError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if result.get("to_provision"):
        background_tasks.add_task(
            vdesktop_service.provision_seats,
            result["pool_name"], result.get("job_id"), result["to_provision"], result["spec"],
        )
    return result


@router.post("/pools/{name}/scale")
async def scale_pool(
    name: str,
    payload: PoolScaleRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Grow/shrink a pool to ``count`` seats (Azure provisions/terminates VMs)."""
    try:
        result = vdesktop_service.scale_pool(db, name, payload.count)
    except VDesktopError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if result.get("to_provision"):
        background_tasks.add_task(
            vdesktop_service.provision_seats, name, None, result["to_provision"], result["spec"])
    if result.get("to_teardown"):
        background_tasks.add_task(vdesktop_service.teardown_seats, result["to_teardown"])
    return result


@router.delete("/pools/{name}")
async def delete_pool(
    name: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Delete a pool. Azure terminates the backing VMs (async) then drops the
    rows; AWS/GCP drop the records immediately."""
    result = vdesktop_service.delete_pool(db, name)
    if result["deleted_seats"] == 0:
        raise HTTPException(status_code=404, detail=f"Pool '{name}' not found.")
    if result.get("to_teardown"):
        background_tasks.add_task(vdesktop_service.teardown_seats, result["to_teardown"])
    return {"ok": True, "deleted_seats": result["deleted_seats"]}
