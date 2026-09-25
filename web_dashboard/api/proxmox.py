"""
Proxmox VE API router.

All endpoints require authentication.  Long-running operations (image import,
deploy, delete) are dispatched as background jobs so the client gets a job ID
immediately and can poll /api/jobs/{id} for progress.
"""
import functools
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import Job, User, get_db
from ..models.schedule import ScheduleRequestMixin
from .auth import get_current_user, require_permission
from ..services import change_window_service, job_service, workgroup_service, workgroup_override_service
from ..services import proxmox_service
from ..services.proxmox_service import ProxmoxError
from ..services import hypervisor_view_service
from ..services import tag_policy
from . import tag_batch
from .hypervisor_deps import (agent_power_job, agent_tag_job, conn_in_task,
                              conn_or_error, queue_power_batch,
                              refuse_direct_booking)

# Every route in this module was `get_current_user` only -- including deploy,
# image import and VM delete. The router-level read gate is the floor; the
# mutating routes add their own level below.
router = APIRouter(prefix="/api/proxmox", tags=["proxmox"],
    dependencies=[Depends(require_permission("proxmox", "read"))],
)

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
async def get_nodes(connection_id: str = "",
                    db: Session = Depends(get_db),
                    current_user: User = Depends(get_current_user)):
    try:
        return await proxmox_service.list_nodes(
            conn_or_error(db, "proxmox", connection_id))
    except ProxmoxError as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/storage")
async def get_storage(
    node: str,
    connection_id: str = "",
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List active storage pools on a node that support images/import content."""
    try:
        return await proxmox_service.list_storage(
            conn_or_error(db, "proxmox", connection_id), node)
    except ProxmoxError as e:
        raise HTTPException(status_code=502, detail=str(e))


# ── Resource / template listing ───────────────────────────────────────────────

@router.get("/resources")
async def get_resources(
    connection_id: str = "",
    node: str = "",
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List all VMs and containers. Pass ?node=<name> to filter to one node.

    Each entry's `workgroup` is resolved in this order:
      1. vm_workgroup_overrides — an admin's explicit re-tag wins.
      2. The matching proxmox_deploy Job — for VMs the dashboard deployed.
      3. None.

    Non-admin callers see only VMs whose workgroup is in their accessible list;
    VMs with no resolved workgroup are admin-only.
    """
    try:
        nodes = [node] if node else None
        conn = conn_or_error(db, "proxmox", connection_id)
        # An agent-bound connection is on a network the dashboard has no route
        # to — that is why it is bound to an agent. Calling the live API here
        # returned a 502 and made the page unusable; serve what the agent last
        # synced instead. The banner says so.
        if conn.via_agent:
            resources = hypervisor_view_service.synced_rows(db, conn)
        else:
            resources = await proxmox_service.list_resources(conn, nodes)
    except ProxmoxError as e:
        raise HTTPException(status_code=502, detail=str(e))

    keys = [_override_key(vm) for vm in resources]
    overrides = workgroup_override_service.get_many(db, PROVIDER, keys)

    # Build {(node, vm_name): workgroup} from proxmox_deploy jobs so VMs
    # deployed through PR #30's flow inherit their deploy-time workgroup
    # even before an admin bulk-assigns one. vm_name + node are stored in
    # the job's metadata at deploy time.
    job_workgroups: dict[tuple[str, str], str] = {}
    deploy_jobs = (
        db.query(Job)
        .filter(Job.job_type == "proxmox_deploy", Job.workgroup.isnot(None))
        .all()
    )
    for j in deploy_jobs:
        meta = j.metadata_dict or {}
        vm_name = meta.get("vm_name")
        vm_node = meta.get("node")
        if vm_name and vm_node and j.workgroup:
            job_workgroups[(vm_node, vm_name)] = j.workgroup

    accessible = None if current_user.is_admin else [w.lower() for w in current_user.workgroups_list]
    out = []
    for vm in resources:
        wg = overrides.get(_override_key(vm))
        if wg is None:
            wg = job_workgroups.get((vm.get("node", ""), vm.get("name", "")))
        vm["workgroup"] = wg
        if accessible is not None:
            if wg is None or wg not in accessible:
                continue
        # After the visibility filter, so a row the caller may not see costs nothing.
        # Live gives a semicolon-joined string, the synced cache gives a JSON list;
        # normalising here rather than in either producer keeps both contracts intact
        # (tests/test_hypervisor_view.py pins the projector against the live shape) and
        # gives the page the same chip list every cloud VM listing binds to.
        vm["tags"] = tag_policy.normalise(vm.get("tags"), "proxmox")
        out.append(vm)
    return out


# ── Tags ──────────────────────────────────────────────────────────────────────

class TagTarget(BaseModel):
    vmid: int
    node: str
    vm_type: str = "qemu"


class TagEditRequest(BaseModel):
    """One edit, applied to every target. The per-VM editor posts a list of one."""
    targets: List[TagTarget]
    add: dict = {}
    remove: List[str] = []
    connection_id: str = ""


def _desired_tags(current: list, add: dict, remove: list) -> list:
    """The tag set a guest should end up with. Order preserved, no duplicates.

    Proxmox replaces the whole field on write, so every path needs the FINAL set rather
    than a delta — and computing it in one place is what stops the direct and the
    agent-brokered paths disagreeing about what an edit means.
    """
    dropped = {str(k) for k in (remove or [])}
    out = [t for t in current if t not in dropped]
    for key in (add or {}):
        if str(key) not in out:
            out.append(str(key))
    return out


@router.post("/instances/tags", summary="Add or remove tags across a selection")
async def edit_vm_tags(
    payload: TagEditRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("proxmox", "write")),
):
    """Apply one tag edit to one or many Proxmox guests.

    Same path as the four clouds' route so one editor serves every provider, but the
    OUTCOME differs by connection and the response says which happened:

    * a connection the dashboard dials directly is written now, and `updated` carries the
      new chips;
    * an agent-bound one cannot be dialled — that is the reason it is bound to an agent —
      so each write is queued as an `agent_hypervisor` job and comes back under `queued`
      with a job id. Reporting those as `updated` would claim a change that has not
      happened yet, and the page says "queued" for exactly that reason.

    A Proxmox tag is a bare label: `add` is read for its KEYS only, and `tag_policy`
    refuses a value rather than silently dropping one.
    """
    conn = conn_or_error(db, "proxmox", payload.connection_id)

    # The same whole-request guard both paths run — a protected key, an illegal key, an
    # empty edit. Shared rather than repeated, because a guard only one path runs is no
    # guard: /costs and POV teardown select on keys this refuses.
    add, remove = tag_batch.assert_edit_allowed("proxmox", payload.add, payload.remove)

    if getattr(conn, "agent_id", None):
        return await _queue_tag_jobs(db, conn, payload, add, remove, current_user)

    async def _apply(target: TagTarget):
        before, after = await proxmox_service.update_tags(
            conn, target.node, target.vmid, target.vm_type, add, remove)
        # Lists in, dicts out: tag_batch compares and audits dicts, and a Proxmox tag
        # carries no value, so each maps to an empty one — which is what
        # tag_policy.normalise renders as a bare chip.
        return {t: "" for t in before}, {t: "" for t in after}

    return await tag_batch.apply_tag_edit(
        db, cloud="proxmox", targets=payload.targets, add=add, remove=remove,
        apply_one=_apply, label_of=lambda t: f"{t.node}/{t.vmid}",
        created_by=current_user.username)


async def _queue_tag_jobs(db: Session, conn, payload: TagEditRequest,
                          add: dict, remove: list, current_user: User) -> dict:
    """The agent-brokered half: one job per guest, nothing written here.

    Deliberately NOT routed through `tag_batch.apply_tag_edit`. That helper's contract is
    `(before, after)` per VM and it audits on the difference — both of which would be a
    lie for a write that has not happened yet. The agent's own completion is what makes
    it true, and `set_tags` is in RESYNC_VERBS so the cache is re-read afterwards.
    """
    if len(payload.targets) > tag_batch.TAG_MAX_TARGETS:
        raise HTTPException(
            status_code=400,
            detail=(f"{len(payload.targets)} VMs selected; the limit for one tag edit "
                    f"is {tag_batch.TAG_MAX_TARGETS}."))

    # The cache is the only reading of "current" available — the dashboard cannot dial
    # this host. That is also why `set_tags` triggers a resync: an edit computed against
    # a stale cache is how a tag someone else added gets silently removed.
    by_key = {}
    for row in hypervisor_view_service.synced_rows(db, conn):
        by_key[(str(row.get("node") or ""), str(row.get("vmid")))] = row

    queued, failed = [], []
    for target in payload.targets:
        label = f"{target.node}/{target.vmid}"
        row = by_key.get((str(target.node), str(target.vmid)))
        if row is None:
            failed.append({"name": label,
                           "error": "not in the last synced inventory for this "
                                    "connection — run Sync Now and try again"})
            continue
        current = [c["key"] for c in tag_policy.normalise(row.get("tags"), "proxmox")]
        desired = _desired_tags(current, add, remove)
        if desired == current:
            continue
        try:
            job = agent_tag_job(
                db, conn, tags=desired, target_id=str(target.vmid),
                target_scope=target.node, target_type=target.vm_type,
                created_by=current_user.username,
                description=f"set tags on {label}")
        except HTTPException as exc:
            # An offline agent or a missing grant is the same for every target, but it is
            # reported per VM rather than raised: a selection of twenty should not lose
            # nineteen queued jobs because the twentieth was not in the inventory.
            failed.append({"name": label, "error": str(exc.detail)})
            continue
        queued.append({"name": label, "job_id": job.id, "tags": desired})

    return {"cloud": "proxmox", "count": 0, "updated": [], "unchanged": [],
            "failed": failed, "queued": queued,
            "agent": True, "connection": conn.name}


@router.get("/templates")
async def get_templates(connection_id: str = "",
                        db: Session = Depends(get_db),
                        current_user: User = Depends(get_current_user)):
    """List all QEMU templates across all nodes."""
    try:
        return await proxmox_service.list_templates(
            conn_or_error(db, "proxmox", connection_id))
    except ProxmoxError as e:
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/nodes/{node}/{vm_type}/{vmid}")
async def get_vm_detail(
    node: str,
    vm_type: str,
    vmid: int,
    connection_id: str = "",
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if vm_type not in ("qemu", "lxc"):
        raise HTTPException(status_code=400, detail="vm_type must be 'qemu' or 'lxc'")
    try:
        return await proxmox_service.get_vm_detail(
            conn_or_error(db, "proxmox", connection_id), node, vmid, vm_type)
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


async def _run_import(job_id: str, connection_id: str, req: ImportImageRequest):
    from ..database import SessionLocal
    db = SessionLocal()
    try:
        job_service.update_progress(db, job_id, 5, f"Downloading {req.image_filename}…")
        result = await proxmox_service.import_and_create_template(
            conn_in_task(db, "proxmox", connection_id),
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


@router.post("/import-image", dependencies=[Depends(require_permission("proxmox", "write"))])
def import_image(
    payload: ImportImageRequest,
    background_tasks: BackgroundTasks,
    connection_id: str = "",
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    conn = conn_or_error(db, "proxmox", connection_id)
    job = job_service.create_job(
        db,
        job_type="proxmox_import_image",
        created_by=current_user.username,
        workgroup=payload.node,
        metadata={
            "node": payload.node,
            "image_filename": payload.image_filename,
            "template_name": payload.template_name,
            "connection_id": conn.id,
        },
    )
    background_tasks.add_task(_run_import, job.id, conn.id, payload)
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


async def _run_deploy(job_id: str, connection_id: str, req: DeployRequest):
    from ..database import SessionLocal
    db = SessionLocal()
    try:
        job_service.update_progress(db, job_id, 10, f"Cloning template {req.template_vmid}…")
        result = await proxmox_service.deploy_from_template(
            conn_in_task(db, "proxmox", connection_id),
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


@router.post("/deploy", dependencies=[Depends(require_permission("proxmox", "write"))])
def deploy(
    payload: DeployRequest,
    background_tasks: BackgroundTasks,
    connection_id: str = "",
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    canonical = _validate_workgroup(db, current_user, payload.workgroup)
    conn = conn_or_error(db, "proxmox", connection_id)
    job = job_service.create_job(
        db,
        job_type="proxmox_deploy",
        created_by=current_user.username,
        workgroup=canonical,
        # vm_name + node are what /api/proxmox/resources joins on to surface
        # the deploy-time workgroup on the matching live VM. template_vmid is
        # informational.
        metadata={
            "vm_name": payload.vm_name,
            "node": payload.node,
            "template_vmid": payload.template_vmid,
            # Pinned at enqueue, never re-chosen at execution: flipping the
            # default mid-flight must not redirect a queued deploy.
            "connection_id": conn.id,
        },
    )
    background_tasks.add_task(_run_deploy, job.id, conn.id, payload)
    return {"job_id": job.id, "status": "queued"}


# ── Delete VM or template ─────────────────────────────────────────────────────

async def _run_delete(job_id: str, connection_id: str, node: str, vmid: int, vm_type: str, label: str):
    from ..database import SessionLocal
    db = SessionLocal()
    try:
        job_service.update_progress(db, job_id, 10, f"Deleting {label}…")
        result = await proxmox_service.delete_vm(
            conn_in_task(db, "proxmox", connection_id), node, vmid, vm_type)
        job_service.set_completed(db, job_id, result)
    except Exception as e:
        job_service.set_failed(db, job_id, str(e))
    finally:
        db.close()


@router.delete("/vms/{node}/{vmid}", dependencies=[Depends(require_permission("proxmox", "delete"))])
def delete_vm(
    node: str,
    vmid: int,
    vm_type: str = "qemu",
    connection_id: str = "",
    background_tasks: BackgroundTasks = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    label = f"{vm_type}/{vmid} on {node}"
    conn = conn_or_error(db, "proxmox", connection_id)
    job = job_service.create_job(
        db,
        job_type="proxmox_delete",
        created_by=current_user.username,
        workgroup=node,
        metadata={"node": node, "vmid": vmid, "vm_type": vm_type,
                  "connection_id": conn.id},
    )
    background_tasks.add_task(_run_delete, job.id, conn.id, node, vmid, vm_type, label)
    return {"job_id": job.id, "status": "queued"}


# ── Power operations ──────────────────────────────────────────────────────────

class PowerOpRequest(BaseModel):
    node: str
    vmid: int
    vm_type: str  # "qemu" or "lxc"
    name: str = ""


async def _run_power_op(job_id: str, connection_id: str, node: str, vmid: int, vm_type: str, op: str):
    from ..database import SessionLocal
    db = SessionLocal()
    try:
        job_service.update_progress(db, job_id, 10, f"{op.capitalize()}ing {vm_type} {vmid} on {node}…")
        result = await proxmox_service.power_op(
            conn_in_task(db, "proxmox", connection_id), node, vmid, vm_type, op)
        job_service.set_completed(db, job_id, result)
    except Exception as e:
        job_service.set_failed(db, job_id, str(e))
    finally:
        db.close()


async def _queue_one(db, current_user, *, op: str, payload: PowerOpRequest,
                     connection_id: str = "", batch_id=None,
                     sched: Optional[dict] = None) -> dict:
    """Queue ONE Proxmox power op. The only path that does, single or bulk.

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
    label = payload.name or f"{payload.vm_type}/{payload.vmid}"
    conn = conn_or_error(db, "proxmox", connection_id)
    # An agent-bound connection is on a network the dashboard cannot dial, so the
    # button enqueues an agent job instead of calling the service. Ops the agent's
    # verb allowlist cannot honestly express are a 501 naming what it can; verbs it has
    # no implementation for are refused by the agent itself, in Live Output, naming why.
    agent_job = agent_power_job(
        db, conn, op=op, target_id=str(payload.vmid),
        target_scope=payload.node, target_type=payload.vm_type,
        created_by=current_user.username,
        description=f"{op} {label} via agent",
        batch_id=batch_id, sched=sched)
    if agent_job is not None:
        return {"job_id": agent_job.id, "status": agent_job.status, "task": None}

    # Past here the work is a coroutine THIS process runs, so a booking cannot be
    # honoured. Refused before create_job, per target, so an operator whose selection
    # mixes agent-bound and directly-dialled connections still gets the bookable half
    # queued. See hypervisor_deps.refuse_direct_booking.
    if sched:
        refuse_direct_booking(conn, op)

    job = job_service.create_job(
        db,
        job_type=f"proxmox_{op}",
        created_by=current_user.username,
        workgroup=payload.node,
        batch_id=batch_id,
        metadata={
            "node": payload.node,
            "vmid": payload.vmid,
            "vm_type": payload.vm_type,
            "vm_name": payload.name,
            "op": op,
        },
    )
    return {
        "job_id": job.id,
        "status": "queued",
        "task": functools.partial(_run_power_op, job.id, conn.id, payload.node,
                                  payload.vmid, payload.vm_type, op),
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

    _handler.__name__ = f"proxmox_{op}"
    return _handler


# The ops the selection toolbar offers. A subset of the per-row buttons on purpose:
# every op this page offers is here, because Proxmox's four are exactly the four the toolbar wants. Named here rather than derived from PAGE_OPS because this is a decision about
# the toolbar, not a statement about what the agent can express — PAGE_OPS still decides
# that, inside _queue_one.
BULK_OPS = ("start", "shutdown", "stop", "reboot")


class BulkPowerRequest(ScheduleRequestMixin, BaseModel):
    """One op, many VMs. `targets` carries the same payload the single route takes.

    The schedule fields come from the mixin. They are honoured only for agent-bound
    connections: an `agent_hypervisor` row waits in the queue until
    `agent_service.lease_one` offers it, and that query filters on
    `job_service.claimable_now()`. A directly-dialled connection is refused per target
    by `hypervisor_deps.refuse_direct_booking`.
    """
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
    current_user: User = Depends(require_permission("proxmox", "write")),
):
    """Queue one power op per selected VM, all sharing a ``batch_id``.

    Auth is deliberately the same as the single route's rather than `require_admin`: a
    user entitled to power one VM must not be refused for powering ten. The workgroup
    override endpoints sitting next to this in the same toolbar ARE admin-only, which is
    why the page gates those two buttons and not these.
    """
    op = payload.op.strip().lower()
    # Resolved BEFORE anything is created, like every other booked route: a bad
    # time must 400 with no job rows behind it. `{}` for an immediate run, which
    # leaves the create_job calls below byte-for-byte what they were.
    _sched = change_window_service.schedule_kwargs(db, **payload.schedule_fields())
    return await queue_power_batch(
        db, kind="proxmox", op=payload.op, targets=payload.targets,
        allowed_ops=BULK_OPS,
        queue_one=lambda target, batch_id: _queue_one(
            db, current_user, op=op, payload=target,
            connection_id=payload.connection_id, batch_id=batch_id,
            sched=_sched),
        label_of=lambda target: target.name or f"{target.vm_type}/{target.vmid}",
        created_by=current_user.username,
        scheduled=bool(_sched),
        background_tasks=background_tasks)


router.add_api_route("/power/start",    _power_endpoint("start"),    methods=["POST"], summary="Start a VM or container")
router.add_api_route("/power/shutdown", _power_endpoint("shutdown"),  methods=["POST"], summary="Gracefully shut down a VM or container")
router.add_api_route("/power/stop",     _power_endpoint("stop"),      methods=["POST"], summary="Force-stop a VM or container")
router.add_api_route("/power/reboot",   _power_endpoint("reboot"),    methods=["POST"], summary="Reboot a VM (QEMU only)")
