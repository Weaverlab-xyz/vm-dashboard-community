"""
Nutanix AHV API router.

All endpoints require authentication.  Long-running operations (image import,
deploy, delete) are dispatched as background jobs so the client gets a job ID
immediately and can poll /api/jobs/{id} for progress.
"""
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_db
from ..auth import get_current_user
from ..models.user import User
from ..services import job_service
from ..services import nutanix_service
from ..services.nutanix_service import NutanixError

router = APIRouter(prefix="/api/nutanix", tags=["nutanix"])


# ── Cloud image catalog ───────────────────────────────────────────────────────

@router.get("/cloud-images")
async def get_cloud_images(current_user: User = Depends(get_current_user)):
    return nutanix_service.list_cloud_images()


# ── Cluster / subnet / image listing ─────────────────────────────────────────

@router.get("/clusters")
async def get_clusters(current_user: User = Depends(get_current_user)):
    try:
        return await nutanix_service.list_clusters()
    except NutanixError as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/subnets")
async def get_subnets(current_user: User = Depends(get_current_user)):
    try:
        return await nutanix_service.list_subnets()
    except NutanixError as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/images")
async def get_images(current_user: User = Depends(get_current_user)):
    try:
        return await nutanix_service.list_images()
    except NutanixError as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/vms")
async def get_vms(current_user: User = Depends(get_current_user)):
    """List all VMs from Prism Central."""
    try:
        return await nutanix_service.list_vms()
    except NutanixError as e:
        raise HTTPException(status_code=502, detail=str(e))


# ── Image import ──────────────────────────────────────────────────────────────

class ImportImageRequest(BaseModel):
    name: str
    source_uri: str


async def _run_import(job_id: str, name: str, source_uri: str):
    from ..database import SessionLocal
    db = SessionLocal()
    try:
        job_service.update_progress(db, job_id, 5, f"Importing '{name}' from URL…")
        result = await nutanix_service.import_image(name, source_uri)
        job_service.set_completed(db, job_id, result)
    except Exception as e:
        job_service.set_failed(db, job_id, str(e))
    finally:
        db.close()


@router.post("/import-image")
async def import_image(
    payload: ImportImageRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    job = job_service.create_job(
        db,
        job_type="nutanix_import_image",
        description=f"Nutanix: import image '{payload.name}'",
        workgroup="nutanix",
        owner_id=current_user.id,
    )
    background_tasks.add_task(_run_import, job.id, payload.name, payload.source_uri)
    return {"job_id": job.id, "status": "queued"}


# ── Deploy VM from image ──────────────────────────────────────────────────────

class DeployRequest(BaseModel):
    vm_name: str
    image_uuid: str
    cluster_uuid: str
    subnet_uuid: str
    vcpus: int = 2
    num_sockets: int = 1
    memory_mib: int = 4096
    disk_size_mib: int = 40960


async def _run_deploy(job_id: str, req: DeployRequest):
    from ..database import SessionLocal
    db = SessionLocal()
    try:
        job_service.update_progress(db, job_id, 10, f"Creating VM '{req.vm_name}'…")
        result = await nutanix_service.deploy_vm(
            vm_name=req.vm_name,
            image_uuid=req.image_uuid,
            cluster_uuid=req.cluster_uuid,
            subnet_uuid=req.subnet_uuid,
            vcpus=req.vcpus,
            num_sockets=req.num_sockets,
            memory_mib=req.memory_mib,
            disk_size_mib=req.disk_size_mib,
        )
        job_service.set_completed(db, job_id, result)
    except Exception as e:
        job_service.set_failed(db, job_id, str(e))
    finally:
        db.close()


@router.post("/deploy")
async def deploy(
    payload: DeployRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    job = job_service.create_job(
        db,
        job_type="nutanix_deploy",
        description=f"Nutanix: deploy VM '{payload.vm_name}' from image {payload.image_uuid[:8]}…",
        workgroup="nutanix",
        owner_id=current_user.id,
    )
    background_tasks.add_task(_run_deploy, job.id, payload)
    return {"job_id": job.id, "status": "queued"}


# ── Delete image ──────────────────────────────────────────────────────────────

async def _run_delete_image(job_id: str, uuid: str, name: str):
    from ..database import SessionLocal
    db = SessionLocal()
    try:
        job_service.update_progress(db, job_id, 10, f"Deleting image '{name}'…")
        result = await nutanix_service.delete_image(uuid)
        job_service.set_completed(db, job_id, result)
    except Exception as e:
        job_service.set_failed(db, job_id, str(e))
    finally:
        db.close()


@router.delete("/images/{uuid}")
async def delete_image(
    uuid: str,
    name: str = "",
    background_tasks: BackgroundTasks = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    job = job_service.create_job(
        db,
        job_type="nutanix_delete_image",
        description=f"Nutanix: delete image {name or uuid}",
        workgroup="nutanix",
        owner_id=current_user.id,
    )
    background_tasks.add_task(_run_delete_image, job.id, uuid, name)
    return {"job_id": job.id, "status": "queued"}


# ── Delete VM ─────────────────────────────────────────────────────────────────

async def _run_delete_vm(job_id: str, uuid: str, name: str):
    from ..database import SessionLocal
    db = SessionLocal()
    try:
        job_service.update_progress(db, job_id, 10, f"Deleting VM '{name}'…")
        result = await nutanix_service.delete_vm(uuid, name)
        job_service.set_completed(db, job_id, result)
    except Exception as e:
        job_service.set_failed(db, job_id, str(e))
    finally:
        db.close()


@router.delete("/vms/{uuid}")
async def delete_vm(
    uuid: str,
    name: str = "",
    background_tasks: BackgroundTasks = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    job = job_service.create_job(
        db,
        job_type="nutanix_delete_vm",
        description=f"Nutanix: delete VM {name or uuid}",
        workgroup="nutanix",
        owner_id=current_user.id,
    )
    background_tasks.add_task(_run_delete_vm, job.id, uuid, name)
    return {"job_id": job.id, "status": "queued"}


# ── Power operations ──────────────────────────────────────────────────────────

class PowerOpRequest(BaseModel):
    uuid: str
    name: str = ""
    cluster: str = ""


async def _run_power_op(job_id: str, uuid: str, name: str, op: str, label: str):
    from ..database import SessionLocal
    db = SessionLocal()
    try:
        job_service.update_progress(db, job_id, 10, f"{op.capitalize()}ing {label}…")
        result = await nutanix_service.power_op(uuid, name, op)
        job_service.set_completed(db, job_id, result)
    except Exception as e:
        job_service.set_failed(db, job_id, str(e))
    finally:
        db.close()


def _power_endpoint(op: str):
    async def _handler(
        payload: PowerOpRequest,
        background_tasks: BackgroundTasks,
        db: Session = Depends(get_db),
        current_user: User = Depends(get_current_user),
    ):
        label = payload.name or payload.uuid
        job = job_service.create_job(
            db,
            job_type=f"nutanix_{op}",
            description=f"Nutanix {op}: {label}"
            + (f" ({payload.cluster})" if payload.cluster else ""),
            workgroup=payload.cluster or "nutanix",
            owner_id=current_user.id,
        )
        background_tasks.add_task(
            _run_power_op, job.id, payload.uuid, payload.name, op, label
        )
        return {"job_id": job.id, "status": "queued"}

    _handler.__name__ = f"nutanix_{op}"
    return _handler


router.add_api_route("/power/start",    _power_endpoint("start"),    methods=["POST"], summary="Power on a VM")
router.add_api_route("/power/shutdown", _power_endpoint("shutdown"),  methods=["POST"], summary="Graceful shutdown via ACPI (requires NGT)")
router.add_api_route("/power/stop",     _power_endpoint("stop"),      methods=["POST"], summary="Force power off")
router.add_api_route("/power/reboot",   _power_endpoint("reboot"),    methods=["POST"], summary="Graceful reboot via ACPI (requires NGT)")
router.add_api_route("/power/reset",    _power_endpoint("reset"),     methods=["POST"], summary="Hard reset")
router.add_api_route("/power/pause",    _power_endpoint("pause"),     methods=["POST"], summary="Pause VM")
router.add_api_route("/power/resume",   _power_endpoint("resume"),    methods=["POST"], summary="Resume paused or suspended VM")
