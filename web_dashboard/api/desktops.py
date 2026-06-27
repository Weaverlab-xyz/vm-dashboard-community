"""Virtual-desktop management API.

Gated on ``vdesktops_enabled``. CRUD over the ``virtual_desktops`` table via
``vdesktop_service``; Azure pools provision one private VM per seat (durable,
via the job runner) and seats can be brokered as PRA RDP Jump Items. AWS/GCP
create seat records only for now.

  GET    /api/desktops                       — list seats
  GET    /api/desktops/pools                 — list pool summaries
  POST   /api/desktops/pools                 — create a pool (Azure provisions VMs)
  POST   /api/desktops/pools/{name}/scale    — grow/shrink a pool
  DELETE /api/desktops/pools/{name}          — delete a pool
  GET    /api/desktops/pools/{name}/seats    — seats in one pool
  GET    /api/desktops/seats/{id}/session    — PRA connection info for a seat
"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import User, get_db
from ..models.vdesktop import PoolCreateRequest, PoolScaleRequest
from ..services import job_service, vdesktop_service
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
            "Virtual-desktop router mounted. Azure pools provision VMs via the "
            "job runner; seats can be brokered as PRA RDP Jump Items."
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
    Subnet + VM size fall back to the Virtual Desktops panel defaults
    (``azure_desktops_subnet_id`` / ``azure_desktops_vm_size``) so pools land on
    the non-delegated desktops subnet by default instead of whatever the picker
    lists.

    Multi-region (PR3): subnet / VM size / resource group resolve through the
    chosen region's config set (``resolve_azure_region(location)``), which falls
    back per-field to the flat keys when a region isn't configured — so a pool in
    westus2 gets the westus2 desktops subnet + size, while a single-region setup
    behaves exactly as before.
    """
    from ..services.region_config import resolve_azure_region

    location = payload.location or _cfg("azure_location") or "centralus"
    region = resolve_azure_region(location)
    return {
        "location": location,
        "resource_group": payload.resource_group or region["resource_group"] or "vm-cli-rg",
        "vm_size": payload.vm_size or payload.size or region["default_vm_size"],
        "image_id": payload.image_id or payload.image,
        "image_publisher": payload.image_publisher, "image_offer": payload.image_offer,
        "image_sku": payload.image_sku, "image_version": payload.image_version,
        "subnet_id": payload.subnet_id or region["desktops_subnet_id"], "nsg_ids": payload.nsg_ids,
        "create_public_ip": payload.create_public_ip,
        "os_type": payload.os_type,
        "trusted_launch": payload.trusted_launch,
        "ssh_username": payload.ssh_username, "ssh_public_key": payload.ssh_public_key,
    }


@router.post("/pools", status_code=201)
async def create_pool(
    payload: PoolCreateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Create a desktop pool. Azure provisions one private VM per seat (durable,
    via the job runner, tagged for the pool); AWS/GCP create records only
    (Phase 1 is Azure)."""
    spec = _azure_spec(payload) if payload.cloud == "azure" else None
    try:
        result = vdesktop_service.create_pool(
            db, cloud=payload.cloud, name=payload.name, count=payload.count,
            created_by=current_user.username, spec=spec,
        )
    except VDesktopError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # create_pool already enqueued the vdesktop_pool_provision job with seat_ids +
    # spec in its metadata; the in-container job runner claims it. No in-process
    # BackgroundTask (a gunicorn recycle could kill it mid-provision, stranding a
    # pending seat with an untracked VM). Mirrors clouddb/k8s.
    return result


@router.post("/pools/{name}/scale")
async def scale_pool(
    name: str,
    payload: PoolScaleRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Grow/shrink a pool to ``count`` seats (Azure provisions/terminates VMs via
    the durable job runner)."""
    try:
        result = vdesktop_service.scale_pool(db, name, payload.count)
    except VDesktopError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Enqueue durable jobs instead of in-process BackgroundTasks (see create_pool).
    if result.get("to_provision"):
        job = job_service.create_job(
            db, job_type="vdesktop_pool_provision", created_by=current_user.username,
            metadata={"pool_name": name, "seat_ids": result["to_provision"],
                      "spec": result["spec"]},
        )
        result["job_id"] = job.id
    if result.get("to_teardown"):
        job = job_service.create_job(
            db, job_type="vdesktop_pool_teardown", created_by=current_user.username,
            metadata={"pool_name": name, "seat_ids": result["to_teardown"]},
        )
        result["job_id"] = job.id
    return result


@router.delete("/pools/{name}")
async def delete_pool(
    name: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Delete a pool. Azure terminates the backing VMs (durable, via the job
    runner) then drops the rows; AWS/GCP drop the records immediately."""
    result = vdesktop_service.delete_pool(db, name)
    if result["deleted_seats"] == 0:
        raise HTTPException(status_code=404, detail=f"Pool '{name}' not found.")
    job_id = None
    if result.get("to_teardown"):
        job = job_service.create_job(
            db, job_type="vdesktop_pool_teardown", created_by=current_user.username,
            metadata={"pool_name": name, "seat_ids": result["to_teardown"]},
        )
        job_id = job.id
    return {"ok": True, "deleted_seats": result["deleted_seats"], "job_id": job_id}


@router.get("/pools/{name}/seats")
async def list_pool_seats(
    name: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Seats in one pool (id, vm_resource_id, status, pra_jump_id) — backs the
    per-pool Seats view + the Open-session action."""
    return {"seats": vdesktop_service.get_pool(db, name)}


@router.get("/seats/{seat_id}/session")
async def open_seat_session(
    seat_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """PRA connection info for a seat: the auto-registered Remote RDP Jump Item +
    a link to the PRA console. The web app can't drive the rep console directly —
    the rep launches the Jump Item there (mirrors the k8s open_console pattern)."""
    seat = vdesktop_service.get_seat(db, seat_id)
    if seat is None:
        raise HTTPException(status_code=404, detail=f"Seat '{seat_id}' not found.")
    vm_name = (seat.get("vm_resource_id") or "").split("/")[-1]
    if not seat.get("pra_jump_id"):
        return {
            "brokered": False, "vm_name": vm_name,
            "note": ("Not brokered yet — PRA registration is pending/failed, or this seat "
                     "predates Phase 2. Confirm PRA is configured, or recreate the seat."),
        }
    host = _cfg("bt_api_host")
    return {
        "brokered": True,
        "vm_name": vm_name,
        "pra": {
            "jump_id": seat.get("pra_jump_id"),
            "jump_group": _cfg("azure_bt_jump_group_name") or _cfg("bt_jump_group_name"),
            "jumpoint": _cfg("azure_jumpoint_name") or _cfg("bt_jumpoint_name"),
        },
        "console_url": f"https://{host}/login" if host else "",
        "note": ("Open the auto-registered Remote RDP Jump Item from your PRA representative "
                 "console. Credentials inject from the PRA Vault when provisioned; otherwise use "
                 "the seat's admin password (Azure → VMs → Password)."),
    }
