"""
Proxmox VE API router.

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
from ..services import job_service, workgroup_service, workgroup_override_service
from ..services import proxmox_service
from ..services.proxmox_service import ProxmoxError

router = APIRouter(prefix="/api/proxmox", tags=["proxmox"])

PROVIDER = "proxmox"


def _override_key(vm: dict) -> str:
    """Composite VM identity for the workgroup-override table. Proxmox VMIDs
    aren't unique across nodes in a cluster, so node has to be in the key."""
    return f"{vm.get('node', '')}/{vm.get('vmid', '')}"


def _validate_workgroup(db: Session, user: User, workgroup: str) -> str:
    """Validate that `workgroup` exists and the user has access. Returns canonical name."""
    wg = workgroup_service.get(db, workgroup)
    if not wg:
        raise HTTPException(status_code=400, detail=f"Unknown workgroup '{workgroup}'")
    canonical = wg.name
    if not user.is_admin and canonical not in [w.lower() for w in user.workgroups_list]:
        raise HTTPException(status_code=403, detail=f"You do not have access to workgroup '{canonical}'")
    return canonical


# ── Cloud image catalog ───────────────────────────────────────────────────────

@router.get("/cloud-images")
async def get_cloud_images(current_user: User = Depends(get_current_user)):
    return proxmox_service.list_cloud_images()


# ── Cluster / node info ───────────────────────────────────────────────────────

@router.get("/nodes")
async def get_nodes(current_user: User = Depends(get_current_user)):
    try:
        return await proxmox_service.list_nodes()
    except ProxmoxError as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/storage")
async def get_storage(
    node: str,
    current_user: User = Depends(get_current_user),
):
    """List active storage pools on a node that support images/import content."""
    try:
        return await proxmox_service.list_storage(node)
    except ProxmoxError as e:
        raise HTTPException(status_code=502, detail=str(e))


# ── Resource / template listing ───────────────────────────────────────────────

@router.get("/resources")
async def get_resources(
    node: str = "",
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List all VMs and containers. Pass ?node=<name> to filter to one node.

    Each entry's `workgroup` is resolved from the vm_workgroup_overrides table.
    Non-admin callers see only VMs whose workgroup is in their accessible list;
    VMs with no override are admin-only.
    """
    try:
        nodes = [node] if node else None
        resources = await proxmox_service.list_resources(nodes)
    except ProxmoxError as e:
        raise HTTPException(status_code=502, detail=str(e))

    keys = [_override_key(vm) for vm in resources]
    overrides = workgroup_override_service.get_many(db, PROVIDER, keys)

    accessible = None if current_user.is_admin else [w.lower() for w in current_user.workgroups_list]
    out = []
    for vm in resources:
        vm["workgroup"] = overrides.get(_override_key(vm))
        if accessible is not None:
            wg = vm["workgroup"]
            if wg is None or wg not in accessible:
                continue
        out.append(vm)
    return out


@router.get("/templates")
async def get_templates(current_user: User = Depends(get_current_user)):
    """List all QEMU templates across all nodes."""
    try:
        return await proxmox_service.list_templates()
    except ProxmoxError as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/nodes/{node}/{vm_type}/{vmid}")
async def get_vm_detail(
    node: str,
    vm_type: str,
    vmid: int,
    current_user: User = Depends(get_current_user),
):
    if vm_type not in ("qemu", "lxc"):
        raise HTTPException(status_code=400, detail="vm_type must be 'qemu' or 'lxc'")
    try:
        return await proxmox_service.get_vm_detail(node, vmid, vm_type)
    except ProxmoxError as e:
        raise HTTPException(status_code=502, detail=str(e))


# ── Image import ──────────────────────────────────────────────────────────────

class ImportImageRequest(BaseModel):
    node: str
    storage: str
    image_url: str
    image_filename: str
    template_name: str
    vcpus: int = 2
    memory_mb: int = 2048
    disk_size: str = "20G"
    username: str = "ubuntu"


async def _run_import(job_id: str, req: ImportImageRequest):
    from ..database import SessionLocal
    db = SessionLocal()
    try:
        job_service.update_progress(db, job_id, 5, f"Downloading {req.image_filename}…")
        result = await proxmox_service.import_and_create_template(
            node=req.node,
            storage=req.storage,
            image_url=req.image_url,
            image_filename=req.image_filename,
            template_name=req.template_name,
            vcpus=req.vcpus,
            memory_mb=req.memory_mb,
            disk_size=req.disk_size,
            username=req.username,
        )
        job_service.update_progress(db, job_id, 90, f"Converting to template (vmid {result['vmid']})…")
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
        job_type="proxmox_import_image",
        description=f"Proxmox: import {payload.image_filename} → {payload.template_name} on {payload.node}",
        workgroup=payload.node,
        owner_id=current_user.id,
    )
    background_tasks.add_task(_run_import, job.id, payload)
    return {"job_id": job.id, "status": "queued"}


# ── Deploy from template ──────────────────────────────────────────────────────

class DeployRequest(BaseModel):
    node: str
    template_vmid: int
    vm_name: str
    workgroup: str
    username: str = ""
    ssh_public_key: str = ""
    full_clone: bool = True


async def _run_deploy(job_id: str, req: DeployRequest):
    from ..database import SessionLocal
    db = SessionLocal()
    try:
        job_service.update_progress(db, job_id, 10, f"Cloning template {req.template_vmid}…")
        result = await proxmox_service.deploy_from_template(
            node=req.node,
            template_vmid=req.template_vmid,
            vm_name=req.vm_name,
            username=req.username,
            ssh_public_key=req.ssh_public_key,
            full_clone=req.full_clone,
        )
        job_service.update_progress(db, job_id, 90, f"Starting vmid {result['vmid']}…")
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
    canonical = _validate_workgroup(db, current_user, payload.workgroup)
    job = job_service.create_job(
        db,
        job_type="proxmox_deploy",
        description=f"Proxmox: deploy {payload.vm_name} from template {payload.template_vmid} on {payload.node}",
        workgroup=canonical,
        owner_id=current_user.id,
    )
    background_tasks.add_task(_run_deploy, job.id, payload)
    return {"job_id": job.id, "status": "queued"}


# ── Delete VM or template ─────────────────────────────────────────────────────

async def _run_delete(job_id: str, node: str, vmid: int, vm_type: str, label: str):
    from ..database import SessionLocal
    db = SessionLocal()
    try:
        job_service.update_progress(db, job_id, 10, f"Deleting {label}…")
        result = await proxmox_service.delete_vm(node, vmid, vm_type)
        job_service.set_completed(db, job_id, result)
    except Exception as e:
        job_service.set_failed(db, job_id, str(e))
    finally:
        db.close()


@router.delete("/vms/{node}/{vmid}")
async def delete_vm(
    node: str,
    vmid: int,
    vm_type: str = "qemu",
    background_tasks: BackgroundTasks = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    label = f"{vm_type}/{vmid} on {node}"
    job = job_service.create_job(
        db,
        job_type="proxmox_delete",
        description=f"Proxmox: delete {label}",
        workgroup=node,
        owner_id=current_user.id,
    )
    background_tasks.add_task(_run_delete, job.id, node, vmid, vm_type, label)
    return {"job_id": job.id, "status": "queued"}


# ── Power operations ──────────────────────────────────────────────────────────

class PowerOpRequest(BaseModel):
    node: str
    vmid: int
    vm_type: str  # "qemu" or "lxc"
    name: str = ""


async def _run_power_op(job_id: str, node: str, vmid: int, vm_type: str, op: str):
    from ..database import SessionLocal
    db = SessionLocal()
    try:
        job_service.update_progress(db, job_id, 10, f"{op.capitalize()}ing {vm_type} {vmid} on {node}…")
        result = await proxmox_service.power_op(node, vmid, vm_type, op)
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
        label = payload.name or f"{payload.vm_type}/{payload.vmid}"
        job = job_service.create_job(
            db,
            job_type=f"proxmox_{op}",
            description=f"Proxmox {op}: {label} on {payload.node}",
            workgroup=payload.node,
            owner_id=current_user.id,
        )
        background_tasks.add_task(
            _run_power_op, job.id, payload.node, payload.vmid, payload.vm_type, op,
        )
        return {"job_id": job.id, "status": "queued"}

    _handler.__name__ = f"proxmox_{op}"
    return _handler


router.add_api_route("/power/start",    _power_endpoint("start"),    methods=["POST"], summary="Start a VM or container")
router.add_api_route("/power/shutdown", _power_endpoint("shutdown"),  methods=["POST"], summary="Gracefully shut down a VM or container")
router.add_api_route("/power/stop",     _power_endpoint("stop"),      methods=["POST"], summary="Force-stop a VM or container")
router.add_api_route("/power/reboot",   _power_endpoint("reboot"),    methods=["POST"], summary="Reboot a VM (QEMU only)")
