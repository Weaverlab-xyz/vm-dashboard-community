"""
Hyper-V API router.

All endpoints require authentication.  Power operations are dispatched as
background jobs so the client gets a job ID immediately and can poll
/api/jobs/{id} for progress.
"""
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import User, get_db
from .auth import get_current_user
from ..services import job_service, workgroup_override_service
from ..services import hyperv_service
from ..services.hyperv_service import HyperVError
from ..services import hypervisor_view_service
from .hypervisor_deps import agent_power_job, conn_in_task, conn_or_error

router = APIRouter(prefix="/api/hyperv", tags=["hyperv"])

PROVIDER = "hyperv"

# This page's ops -> the agent's closed verb allowlist, for an agent-bound connection.
# The three that are here map exactly onto what the sibling runner issues over WinRM
# (runners/hypervisor/run.py::_PS_POWER): Start-VM, Stop-VM -TurnOff -Force, and
# Restart-VM -Force.
#
# `shutdown`, `pause`, `resume` and `save` are absent ON PURPOSE. The agent's allowlist
# has no graceful-shutdown and no suspend verb, and every way of papering over that is
# worse than refusing: mapping `shutdown` to power_off silently turns a request to ask
# the guest nicely into a hard power cut, and passing `shutdown` through unmapped would
# be normalised to `inventory_sync` and report a scan as a successful shutdown.
# _power_endpoint refuses them with a 501 that names what is available instead.
_AGENT_VERBS = {"start": "power_on", "stop": "power_off", "restart": "power_reset"}


def _override_key(vm: dict) -> str:
    """Composite VM identity for the workgroup-override table."""
    return str(vm.get("vmid", ""))


# ── List endpoints ────────────────────────────────────────────────────────────

@router.get("/vms")
async def get_vms(
    connection_id: str = "",
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List all Hyper-V VMs on the configured host.

    Each entry's `workgroup` is resolved from the vm_workgroup_overrides table.
    Non-admin callers see only VMs whose workgroup is in their accessible list;
    VMs with no override are admin-only.
    """
    try:
        conn = conn_or_error(db, "hyperv", connection_id)
        vms = (hypervisor_view_service.synced_rows(db, conn) if conn.via_agent
               else await hyperv_service.list_vms(conn))
    except HyperVError as e:
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


# ── Power operations ──────────────────────────────────────────────────────────

class PowerOpRequest(BaseModel):
    vmid: str
    name: str = ""


async def _run_power_op(job_id: str, connection_id: str, vmid: str, name: str, op: str, label: str):
    from ..database import SessionLocal
    db = SessionLocal()
    try:
        job_service.update_progress(db, job_id, 10, f"{op.capitalize()}ing {label}…")
        result = await hyperv_service.power_op(conn_in_task(db, "hyperv", connection_id), vmid, name, op)
        job_service.set_completed(db, job_id, result)
    except Exception as e:
        job_service.set_failed(db, job_id, str(e))
    finally:
        db.close()


def _power_endpoint(op: str):
    async def _handler(
        payload: PowerOpRequest,
        background_tasks: BackgroundTasks,
        connection_id: str = "",
        db: Session = Depends(get_db),
        current_user: User = Depends(get_current_user),
    ):
        label = payload.name or payload.vmid
        # Resolve now so a bad id is a 404 the caller sees, not a job that fails later,
        # then carry the ID (never the credential) into the background task.
        conn = conn_or_error(db, "hyperv", connection_id)
        # An agent-bound connection is on a network the dashboard cannot dial — there is
        # no WinRM route to it and no credential for it here — so the button enqueues an
        # agent job instead of calling the service. Ops with no agent verb are refused
        # rather than approximated; see _AGENT_VERBS.
        if getattr(conn, "agent_id", None) and op not in _AGENT_VERBS:
            raise HTTPException(
                status_code=501,
                detail=(f"'{op}' is not available for {conn.name}: it is reached through "
                        f"a remote agent, and the agent implements "
                        f"{', '.join(sorted(_AGENT_VERBS))} for Hyper-V only. Use Force "
                        f"Off in place of Shutdown, or run this one on the host."))
        agent_job = agent_power_job(
            db, conn, verb=_AGENT_VERBS.get(op, op), target_id=payload.vmid,
            target_scope="", target_type="vm",
            created_by=current_user.username,
            description=f"{op} {label} via agent")
        if agent_job is not None:
            return {"job_id": agent_job.id, "status": agent_job.status}

        job = job_service.create_job(
            db,
            job_type=f"hyperv_{op}",
            created_by=current_user.username,
            workgroup="hyperv",
            metadata={"vmid": payload.vmid, "vm_name": payload.name, "op": op},
        )
        background_tasks.add_task(
            _run_power_op, job.id, conn.id, payload.vmid, payload.name, op, label
        )
        return {"job_id": job.id, "status": "queued"}

    _handler.__name__ = f"hyperv_{op}"
    return _handler


router.add_api_route(
    "/power/start",
    _power_endpoint("start"),
    methods=["POST"],
    summary="Power on a VM",
)
router.add_api_route(
    "/power/shutdown",
    _power_endpoint("shutdown"),
    methods=["POST"],
    summary="Graceful shutdown (requires Integration Services)",
)
router.add_api_route(
    "/power/stop",
    _power_endpoint("stop"),
    methods=["POST"],
    summary="Force power off",
)
router.add_api_route(
    "/power/restart",
    _power_endpoint("restart"),
    methods=["POST"],
    summary="Force restart",
)
router.add_api_route(
    "/power/pause",
    _power_endpoint("pause"),
    methods=["POST"],
    summary="Pause (Suspend-VM)",
)
router.add_api_route(
    "/power/resume",
    _power_endpoint("resume"),
    methods=["POST"],
    summary="Resume a paused or saved VM",
)
router.add_api_route(
    "/power/save",
    _power_endpoint("save"),
    methods=["POST"],
    summary="Save VM state to disk",
)
