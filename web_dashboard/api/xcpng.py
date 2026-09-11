"""
XCP-ng / XenServer API router.

All endpoints require authentication. Power operations are dispatched as
background jobs so the client gets a job ID immediately and can poll
/api/jobs/{id} for progress.
"""
import functools
from typing import List

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import User, get_db
from .auth import get_current_user, require_permission
from ..services import job_service, workgroup_override_service
from ..services import xcpng_service
from ..services.xcpng_service import XcpNgError
from ..services import hypervisor_view_service
from .hypervisor_deps import (agent_power_job, conn_in_task, conn_or_error,
                              queue_power_batch)

# Every route in this module was `get_current_user` only -- including deploy,
# image import and VM delete. The router-level read gate is the floor; the
# mutating routes add their own level below.
router = APIRouter(prefix="/api/xcpng", tags=["xcpng"],
    dependencies=[Depends(require_permission("xcpng", "read"))],
)

PROVIDER = "xcpng"


def _override_key(vm: dict) -> str:
    """Composite VM identity for the workgroup-override table."""
    return str(vm.get("uuid", ""))


@router.get("/vms")
async def get_vms(
    connection_id: str = "",
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List all VMs from the XCP-ng / XenServer host or pool.

    Each entry's `workgroup` is resolved from the vm_workgroup_overrides table.
    Non-admin callers see only VMs whose workgroup is in their accessible list;
    VMs with no override are admin-only.
    """
    try:
        conn = conn_or_error(db, "xcpng", connection_id)
        vms = (hypervisor_view_service.synced_rows(db, conn) if conn.via_agent
               else await xcpng_service.list_vms(conn))
    except XcpNgError as e:
        raise HTTPException(status_code=502, detail=str(e))

    keys = [_override_key(vm) for vm in vms]
    overrides = workgroup_override_service.get_many(db, PROVIDER, keys)

    accessible = None if current_user.is_admin else [w.lower() for w in current_user.workgroups_list]
    out = []
    for vm in vms:
        vm["workgroup"] = overrides.get(_override_key(vm))
        if accessible is not None:
            wg = vm["workgroup"]
            if wg is None or wg not in accessible:
                continue
        out.append(vm)
    return out


class PowerOpRequest(BaseModel):
    uuid: str
    name: str = ""


async def _run_power_op(job_id: str, connection_id: str, uuid: str, name: str, op: str, label: str):
    from ..database import SessionLocal
    db = SessionLocal()
    try:
        job_service.update_progress(db, job_id, 10, f"{op.replace('_', ' ').capitalize()}ing {label}…")
        result = await xcpng_service.power_op(conn_in_task(db, "xcpng", connection_id), uuid, name, op)
        job_service.set_completed(db, job_id, result)
    except Exception as e:
        job_service.set_failed(db, job_id, str(e))
    finally:
        db.close()


async def _queue_one(db, current_user, *, op: str, payload: PowerOpRequest,
                     connection_id: str = "", batch_id=None) -> dict:
    """Queue ONE XCP-ng power op. The only path that does, single or bulk.

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
    # Resolve now so a bad id is a 404 the caller sees, not a job that fails later,
    # then carry the ID (never the credential) into the background task.
    conn = conn_or_error(db, "xcpng", connection_id)
    # An agent-bound connection is on a network the dashboard cannot dial, so the
    # button enqueues an agent job instead of calling the service. Ops the agent's
    # verb allowlist cannot honestly express — `suspend`, `resume`, `pause` and
    # `unpause` here — are a 501 naming what it can; verbs it has no implementation for
    # are refused by the agent itself, in Live Output, naming why.
    agent_job = agent_power_job(
        db, conn, op=op, target_id=payload.uuid,
        target_scope="", target_type="vm",
        created_by=current_user.username,
        description=f"{op} {label} via agent",
        batch_id=batch_id)
    if agent_job is not None:
        return {"job_id": agent_job.id, "status": agent_job.status, "task": None}

    job = job_service.create_job(
        db,
        job_type=f"xcpng_{op}",
        created_by=current_user.username,
        workgroup="xcpng",
        batch_id=batch_id,
        metadata={"uuid": payload.uuid, "vm_name": payload.name, "op": op},
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

    _handler.__name__ = f"xcpng_{op}"
    return _handler


# The ops the selection toolbar offers. A subset of the per-row buttons on purpose:
# `hard_reboot` is left per-row because `reboot` is the honest bulk equivalent of Restart, and `suspend`/`resume`/`pause`/`unpause` are per-VM-state operations nobody applies to a selection. Named here rather than derived from PAGE_OPS because this is a decision about
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
    current_user: User = Depends(require_permission("xcpng", "write")),
):
    """Queue one power op per selected VM, all sharing a ``batch_id``.

    Auth is deliberately the same as the single route's rather than `require_admin`: a
    user entitled to power one VM must not be refused for powering ten. The workgroup
    override endpoints sitting next to this in the same toolbar ARE admin-only, which is
    why the page gates those two buttons and not these.
    """
    op = payload.op.strip().lower()
    return await queue_power_batch(
        db, kind="xcpng", op=payload.op, targets=payload.targets,
        allowed_ops=BULK_OPS,
        queue_one=lambda target, batch_id: _queue_one(
            db, current_user, op=op, payload=target,
            connection_id=payload.connection_id, batch_id=batch_id),
        label_of=lambda target: target.name or target.uuid,
        created_by=current_user.username,
        background_tasks=background_tasks)


router.add_api_route("/power/start",      _power_endpoint("start"),      methods=["POST"], summary="Start a halted VM")
router.add_api_route("/power/shutdown",   _power_endpoint("shutdown"),   methods=["POST"], summary="Graceful shutdown (requires xe-guest-utilities)")
router.add_api_route("/power/stop",       _power_endpoint("stop"),       methods=["POST"], summary="Force power off (hard shutdown)")
router.add_api_route("/power/reboot",     _power_endpoint("reboot"),     methods=["POST"], summary="Graceful reboot (requires xe-guest-utilities)")
router.add_api_route("/power/hard_reboot",_power_endpoint("hard_reboot"),methods=["POST"], summary="Force reboot (hard reboot)")
router.add_api_route("/power/suspend",    _power_endpoint("suspend"),    methods=["POST"], summary="Suspend VM to disk")
router.add_api_route("/power/resume",     _power_endpoint("resume"),     methods=["POST"], summary="Resume a suspended VM")
router.add_api_route("/power/pause",      _power_endpoint("pause"),      methods=["POST"], summary="Pause VM in memory")
router.add_api_route("/power/unpause",    _power_endpoint("unpause"),    methods=["POST"], summary="Unpause a paused VM")
