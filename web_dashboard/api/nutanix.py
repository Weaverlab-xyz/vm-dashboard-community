"""
Nutanix AHV API router.

All endpoints require authentication.  Long-running operations (image import,
deploy, delete) are dispatched as background jobs so the client gets a job ID
immediately and can poll /api/jobs/{id} for progress.
"""
import functools
from typing import List

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import Job, User, get_db
from .auth import get_current_user, require_permission
from ..services import job_service, workgroup_service, workgroup_override_service
from ..services import nutanix_service
from ..services.nutanix_service import NutanixError
from ..services import hypervisor_view_service
from .hypervisor_deps import conn_in_task, conn_or_error, queue_power_batch

# Every route in this module was `get_current_user` only -- including deploy,
# image import and VM delete. The router-level read gate is the floor; the
# mutating routes add their own level below.
router = APIRouter(prefix="/api/nutanix", tags=["nutanix"],
    dependencies=[Depends(require_permission("nutanix", "read"))],
)

PROVIDER = "nutanix"


def _override_key(vm: dict) -> str:
    """Composite VM identity for the workgroup-override table."""
    return str(vm.get("uuid", ""))


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
    return nutanix_service.list_cloud_images()


# ── Cluster / subnet / image listing ─────────────────────────────────────────

@router.get("/clusters")
async def get_clusters(connection_id: str = "",
                     db: Session = Depends(get_db),
                     current_user: User = Depends(get_current_user)):
    try:
        return await nutanix_service.list_clusters(
            conn_or_error(db, "nutanix", connection_id))
    except NutanixError as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/subnets")
async def get_subnets(connection_id: str = "",
                     db: Session = Depends(get_db),
                     current_user: User = Depends(get_current_user)):
    try:
        return await nutanix_service.list_subnets(
            conn_or_error(db, "nutanix", connection_id))
    except NutanixError as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/images")
async def get_images(connection_id: str = "",
                     db: Session = Depends(get_db),
                     current_user: User = Depends(get_current_user)):
    try:
        return await nutanix_service.list_images(
            conn_or_error(db, "nutanix", connection_id))
    except NutanixError as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/vms")
async def get_vms(
    connection_id: str = "",
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List all VMs from Prism Central.

    Each entry's `workgroup` is resolved in this order:
      1. vm_workgroup_overrides — an admin's explicit re-tag wins.
      2. The matching nutanix_deploy Job — for VMs the dashboard deployed.
      3. None.

    Non-admin callers see only VMs whose workgroup is in their accessible list;
    VMs with no resolved workgroup are admin-only.
    """
    try:
        conn = conn_or_error(db, "nutanix", connection_id)
        vms = (hypervisor_view_service.synced_rows(db, conn) if conn.via_agent
               else await nutanix_service.list_vms(conn))
    except NutanixError as e:
        raise HTTPException(status_code=502, detail=str(e))

    keys = [_override_key(vm) for vm in vms]
    overrides = workgroup_override_service.get_many(db, PROVIDER, keys)

    # Build {vm_name: workgroup} from nutanix_deploy jobs so VMs deployed
    # through PR #30's flow inherit their deploy-time workgroup even before
    # an admin bulk-assigns one. vm_name is stored in the job's metadata at
    # deploy time. We can't key on uuid because _run_deploy doesn't write
    # the created VM's uuid back to the job yet.
    job_workgroups: dict[str, str] = {}
    deploy_jobs = (
        db.query(Job)
        .filter(Job.job_type == "nutanix_deploy", Job.workgroup.isnot(None))
        .all()
    )
    for j in deploy_jobs:
        meta = j.metadata_dict or {}
        vm_name = meta.get("vm_name")
        if vm_name and j.workgroup:
            job_workgroups[vm_name] = j.workgroup

    accessible = None if current_user.is_admin else [w.lower() for w in current_user.workgroups_list]
    out = []
    for vm in vms:
        wg = overrides.get(_override_key(vm))
        if wg is None:
            wg = job_workgroups.get(vm.get("name", ""))
        vm["workgroup"] = wg
        if accessible is not None:
            if wg is None or wg not in accessible:
                continue
        out.append(vm)
    return out


# ── Image import ──────────────────────────────────────────────────────────────

class ImportImageRequest(BaseModel):
    name: str
    source_uri: str


async def _run_import(job_id: str, connection_id: str, name: str, source_uri: str):
    from ..database import SessionLocal
    db = SessionLocal()
    try:
        job_service.update_progress(db, job_id, 5, f"Importing '{name}' from URL…")
        result = await nutanix_service.import_image(
            conn_in_task(db, "nutanix", connection_id), name, source_uri)
        job_service.set_completed(db, job_id, result)
    except Exception as e:
        job_service.set_failed(db, job_id, str(e))
    finally:
        db.close()


@router.post("/import-image", dependencies=[Depends(require_permission("nutanix", "write"))])
async def import_image(
    payload: ImportImageRequest,
    background_tasks: BackgroundTasks,
    connection_id: str = "",
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    conn = conn_or_error(db, "nutanix", connection_id)
    job = job_service.create_job(
        db,
        job_type="nutanix_import_image",
        created_by=current_user.username,
        workgroup="nutanix",
        metadata={"image_name": payload.name, "source_uri": payload.source_uri,
                  "connection_id": conn.id},
    )
    background_tasks.add_task(_run_import, job.id, conn.id, payload.name, payload.source_uri)
    return {"job_id": job.id, "status": "queued"}


# ── Deploy VM from image ──────────────────────────────────────────────────────

class DeployRequest(BaseModel):
    vm_name: str
    image_uuid: str
    cluster_uuid: str
    subnet_uuid: str
    workgroup: str
    vcpus: int = 2
    num_sockets: int = 1
    memory_mib: int = 4096
    disk_size_mib: int = 40960


async def _run_deploy(job_id: str, connection_id: str, req: DeployRequest):
    from ..database import SessionLocal
    db = SessionLocal()
    try:
        job_service.update_progress(db, job_id, 10, f"Creating VM '{req.vm_name}'…")
        result = await nutanix_service.deploy_vm(
            conn_in_task(db, "nutanix", connection_id),
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


@router.post("/deploy", dependencies=[Depends(require_permission("nutanix", "write"))])
async def deploy(
    payload: DeployRequest,
    background_tasks: BackgroundTasks,
    connection_id: str = "",
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    canonical = _validate_workgroup(db, current_user, payload.workgroup)
    conn = conn_or_error(db, "nutanix", connection_id)
    job = job_service.create_job(
        db,
        job_type="nutanix_deploy",
        created_by=current_user.username,
        workgroup=canonical,
        # vm_name is what /api/nutanix/vms joins on to surface the deploy-time
        # workgroup on the matching live VM (vm_uuid isn't known until the
        # deploy completes, and _run_deploy doesn't currently write it back).
        metadata={
            "vm_name": payload.vm_name,
            "image_uuid": payload.image_uuid,
            "cluster_uuid": payload.cluster_uuid,
            # Pinned at enqueue, never re-chosen at execution: flipping the
            # default mid-flight must not redirect a queued deploy.
            "connection_id": conn.id,
        },
    )
    background_tasks.add_task(_run_deploy, job.id, conn.id, payload)
    return {"job_id": job.id, "status": "queued"}


# ── Delete image ──────────────────────────────────────────────────────────────

async def _run_delete_image(job_id: str, connection_id: str, uuid: str, name: str):
    from ..database import SessionLocal
    db = SessionLocal()
    try:
        job_service.update_progress(db, job_id, 10, f"Deleting image '{name}'…")
        result = await nutanix_service.delete_image(
            conn_in_task(db, "nutanix", connection_id), uuid)
        job_service.set_completed(db, job_id, result)
    except Exception as e:
        job_service.set_failed(db, job_id, str(e))
    finally:
        db.close()


@router.delete("/images/{uuid}", dependencies=[Depends(require_permission("nutanix", "delete"))])
async def delete_image(
    uuid: str,
    name: str = "",
    connection_id: str = "",
    background_tasks: BackgroundTasks = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    conn = conn_or_error(db, "nutanix", connection_id)
    job = job_service.create_job(
        db,
        job_type="nutanix_delete_image",
        created_by=current_user.username,
        workgroup="nutanix",
        metadata={"image_uuid": uuid, "image_name": name},
    )
    background_tasks.add_task(_run_delete_image, job.id, conn.id, uuid, name)
    return {"job_id": job.id, "status": "queued"}


# ── Delete VM ─────────────────────────────────────────────────────────────────

async def _run_delete_vm(job_id: str, connection_id: str, uuid: str, name: str):
    from ..database import SessionLocal
    db = SessionLocal()
    try:
        job_service.update_progress(db, job_id, 10, f"Deleting VM '{name}'…")
        result = await nutanix_service.delete_vm(
            conn_in_task(db, "nutanix", connection_id), uuid, name)
        job_service.set_completed(db, job_id, result)
    except Exception as e:
        job_service.set_failed(db, job_id, str(e))
    finally:
        db.close()


@router.delete("/vms/{uuid}", dependencies=[Depends(require_permission("nutanix", "delete"))])
async def delete_vm(
    uuid: str,
    name: str = "",
    connection_id: str = "",
    background_tasks: BackgroundTasks = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    conn = conn_or_error(db, "nutanix", connection_id)
    job = job_service.create_job(
        db,
        job_type="nutanix_delete_vm",
        created_by=current_user.username,
        workgroup="nutanix",
        metadata={"vm_uuid": uuid, "vm_name": name, "connection_id": conn.id},
    )
    background_tasks.add_task(_run_delete_vm, job.id, conn.id, uuid, name)
    return {"job_id": job.id, "status": "queued"}


# ── Power operations ──────────────────────────────────────────────────────────

class PowerOpRequest(BaseModel):
    uuid: str
    name: str = ""
    cluster: str = ""


async def _run_power_op(job_id: str, connection_id: str, uuid: str, name: str, op: str, label: str):
    from ..database import SessionLocal
    db = SessionLocal()
    try:
        job_service.update_progress(db, job_id, 10, f"{op.capitalize()}ing {label}…")
        result = await nutanix_service.power_op(
            conn_in_task(db, "nutanix", connection_id), uuid, name, op)
        job_service.set_completed(db, job_id, result)
    except Exception as e:
        job_service.set_failed(db, job_id, str(e))
    finally:
        db.close()


async def _queue_one(db, current_user, *, op: str, payload: PowerOpRequest,
                     connection_id: str = "", batch_id=None) -> dict:
    """Queue ONE Nutanix power op. The only path that does, single or bulk.

    Returns ``{"job_id", "status", "task"}``. ``task`` is a zero-arg coroutine function
    for a connection the dashboard dials itself and None for an agent-bound one:
    deciding *how* that work runs belongs to the caller, and it is the whole difference
    between the single route (one background task) and the bulk route (one background
    task for the whole batch, walked serially — see
    :func:`~web_dashboard.api.hypervisor_deps.run_power_batch`).

    This function exists so that bulk power adds selection and not a second code path.
    Every gate below applied to one button press before bulk existed and applies
    unchanged to each VM in a selection.
    """
    label = payload.name or payload.uuid
    conn = conn_or_error(db, "nutanix", connection_id)
    # No agent branch, and that is a standing decision rather than an omission: a
    # Nutanix power change is a full spec PUT carrying a metadata version, not a simple
    # action, so getting one wrong writes to the VM instead of failing —
    # docs/remote-agents.md records it as worth doing carefully rather than quickly.
    # Nutanix therefore has no entry in agent_hypervisor_meta.PAGE_OPS, `_power_nutanix`
    # in the agent refuses before reading a table, and calling `agent_power_job` here
    # would fail closed and 501 every button on this page.
    # tests/test_hypervisor_power_routing.py::test_nutanix_power_is_still_unimplemented_in_the_agent
    # is what keeps that true on purpose. Bulk inherits exactly the reach the single
    # button has — no wider, no narrower.
    job = job_service.create_job(
        db,
        job_type=f"nutanix_{op}",
        created_by=current_user.username,
        workgroup=payload.cluster or "nutanix",
        batch_id=batch_id,
        metadata={
            "vm_uuid": payload.uuid,
            "vm_name": payload.name,
            "cluster": payload.cluster,
            "op": op,
        },
    )
    return {
        "job_id": job.id,
        "status": "queued",
        "task": functools.partial(_run_power_op, job.id, conn.id, payload.uuid,
                                  payload.name, op, label),
    }


def _power_endpoint(op: str):
    async def _handler(
        payload: PowerOpRequest,
        background_tasks: BackgroundTasks,
        connection_id: str = "",
        db: Session = Depends(get_db),
        current_user: User = Depends(get_current_user),
    ):
        result = await _queue_one(db, current_user, op=op, payload=payload,
                                  connection_id=connection_id)
        if result["task"] is not None:
            background_tasks.add_task(result["task"])
        return {"job_id": result["job_id"], "status": result["status"]}

    _handler.__name__ = f"nutanix_{op}"
    return _handler


# The ops the selection toolbar offers. A subset of the per-row buttons on purpose:
# `reset` is left per-row because `reboot` is the honest bulk equivalent of Restart on AHV, and `pause`/`resume` are per-VM-state operations nobody applies to a selection. Named here rather than derived from PAGE_OPS because this is a decision about
# the toolbar, not a statement about what the agent can express — PAGE_OPS still decides
# that, inside _queue_one.
BULK_OPS = ("start", "shutdown", "stop", "reboot")


class BulkPowerRequest(BaseModel):
    """One op, many VMs. `targets` carries the same payload the single route takes."""
    op: str
    targets: List[PowerOpRequest]
    connection_id: str = ""


@router.post("/power/bulk", summary="Power op across a selection of VMs")
async def bulk_power(
    payload: BulkPowerRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    # `<hv>:write`, in the permissive form, because this route was `get_current_user`
    # only -- so a legacy NULL-permission user keeps it, and an explicitly-permissioned
    # one gets it from the backfill. The docstring below is still the rule: the same
    # authority as powering one VM, never require_admin.
    current_user: User = Depends(require_permission("nutanix", "write")),
):
    """Queue one power op per selected VM, all sharing a ``batch_id``.

    Auth is deliberately the same as the single route's rather than `require_admin`: a
    user entitled to power one VM must not be refused for powering ten. The workgroup
    override endpoints sitting next to this in the same toolbar ARE admin-only, which is
    why the page gates those two buttons and not these.
    """
    op = payload.op.strip().lower()
    return await queue_power_batch(
        db, kind="nutanix", op=payload.op, targets=payload.targets,
        allowed_ops=BULK_OPS,
        queue_one=lambda target, batch_id: _queue_one(
            db, current_user, op=op, payload=target,
            connection_id=payload.connection_id, batch_id=batch_id),
        label_of=lambda target: target.name or target.uuid,
        created_by=current_user.username,
        background_tasks=background_tasks)


router.add_api_route("/power/start",    _power_endpoint("start"),    methods=["POST"], summary="Power on a VM")
router.add_api_route("/power/shutdown", _power_endpoint("shutdown"),  methods=["POST"], summary="Graceful shutdown via ACPI (requires NGT)")
router.add_api_route("/power/stop",     _power_endpoint("stop"),      methods=["POST"], summary="Force power off")
router.add_api_route("/power/reboot",   _power_endpoint("reboot"),    methods=["POST"], summary="Graceful reboot via ACPI (requires NGT)")
router.add_api_route("/power/reset",    _power_endpoint("reset"),     methods=["POST"], summary="Hard reset")
router.add_api_route("/power/pause",    _power_endpoint("pause"),     methods=["POST"], summary="Pause VM")
router.add_api_route("/power/resume",   _power_endpoint("resume"),    methods=["POST"], summary="Resume paused or suspended VM")
