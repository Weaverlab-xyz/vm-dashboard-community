"""Shared helpers for the hypervisor routers.

Each of ``api/{proxmox,vsphere,nutanix,hyperv,xcpng}.py`` — and ``api/vms.py`` for
VMware Workstation — needs the same few things: turn an optional ``connection_id``
query parameter into a resolved
:class:`~web_dashboard.services.hypervisor_connection_service.Connection`, turn a
resolution failure into an HTTP status, offer that connection to its bound agent before
dialling it directly, and now queue one power op across a whole selection. Keeping all
of it here means the routers cannot drift on any of them — and every one of those four
has drifted at least once when they each kept a copy.

FastAPI lives here rather than in the service so ``hypervisor_connection_service`` stays
importable — and testable — without the web framework.
"""
import uuid
from typing import Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

from ..services import hypervisor_connection_service as hcs


def conn_or_error(db: Session, kind: str, connection_id: Optional[str] = None):
    """The connection this request means, or an HTTP error saying what to do.

    404 rather than 400: from the caller's point of view a connection id that does not
    resolve is a missing resource, and the message already names the fix ("add one on
    the Connections page", "set one as the default"). A 500 here would be wrong — every
    branch of :func:`hcs.resolve` is a configuration state, not a fault.
    """
    try:
        return hcs.resolve(db, kind, connection_id)
    except hcs.HypervisorConnectionError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def agent_power_job(db: Session, conn, *, op: str, target_id: str,
                    target_scope: str = "", target_type: str = "vm",
                    created_by: str = "", description: str = "", batch_id=None):
    """Enqueue a power op for an AGENT-BOUND connection, or return None.

    The dashboard has no route to these endpoints — that is the whole reason the
    connection is bound to an agent — so the power buttons on the hypervisor pages
    cannot dial them. They enqueue an ``agent_hypervisor`` job instead, and it lands on
    /jobs with Live Output and Cancel exactly like a discovery scan, because the agent
    protocol already carries both.

    Returns None for a connection the dashboard *can* dial, so a caller can keep its
    existing direct path unchanged.

    Three grants must line up before this runs: the dashboard's ``allowed_job_types``,
    the customer's ``policy.yaml`` verb list for this connection, and the connection
    actually existing in that agent's ``connections.yaml``. Only the first is checked
    here — the other two are the agent's, deliberately, and their refusal shows up in
    Live Output naming the file and the line to add.

    Takes the **page op** (`shutdown`, `hard_reboot`, …), not an agent verb, because the
    op-to-verb map is per hypervisor kind — the agent's `restart` is a graceful shutdown
    on Proxmox, a hard reset on vSphere and a reboot on XCP-ng. See
    :data:`~web_dashboard.services.agent_hypervisor_meta.PAGE_OPS` for what went wrong
    while each router kept its own copy. Translating here also puts the translation
    *after* the agent-bound check below, so refusing an op the agent cannot express can
    never touch a connection the dashboard dials directly.

    ``batch_id`` groups the jobs of one bulk selection so /jobs can roll them up. It is
    a plain label and nothing authorises off it. Note what it does NOT change: the row
    still gets ``set_cloud_resource_id(conn.id)`` below, which is what
    ``hypervisor_sync_service._has_open_job`` reads — so during a burst of power ops on
    one connection only the LAST job to finish queues an inventory sync, rather than
    each of them queueing a sync of the same inventory. That behaviour is bulk power's
    load-bearing assumption and it comes for free from going through here.
    """
    if not getattr(conn, "agent_id", None):
        return None

    from ..database import RemoteAgent
    from ..services import agent_hypervisor_meta, agent_service, job_service

    # Refused here rather than left to normalize(): an unrecognised verb falls back to
    # ``inventory_sync`` there (deliberately — see agent_hypervisor_meta), which for a
    # POWER request is the worst possible outcome. The operator would get a job that
    # completes GREEN having run a scan, while the VM never moved. An op with no agent
    # verb has to be said out loud instead of passed through.
    verb = agent_hypervisor_meta.agent_verb(conn.kind, op)
    if verb is None:
        # 501, not 400: the request is well formed and the operator is entitled to make
        # it — this build simply cannot carry it out. The message names the substitution
        # that would have been wrong and the buttons that do work, because a page whose
        # button just failed is all the operator can see.
        raise HTTPException(
            status_code=501,
            detail=agent_hypervisor_meta.no_verb_reason(conn.kind, op))

    # Belt and braces, and not redundant: the check above trusts PAGE_OPS to contain
    # only real verbs, and this one is what fails loudly if it ever does not — a typo
    # in that table would otherwise reach normalize() and become a scan.
    if verb not in agent_hypervisor_meta.WRITE_VERBS:
        raise HTTPException(
            status_code=501,
            detail=(f"'{verb}' is not something an agent can be asked to do. The agent "
                    f"verbs are: {', '.join(agent_hypervisor_meta.WRITE_VERBS)}."))

    agent = db.query(RemoteAgent).filter(RemoteAgent.id == conn.agent_id).first()
    if agent is None:
        raise HTTPException(status_code=409,
                            detail="The agent this connection is bound to no longer exists.")
    if agent_service.status_of(agent) != "online":
        raise HTTPException(
            status_code=409,
            detail=f"Agent '{agent.name}' is offline, so {conn.name} cannot be reached.")
    if "agent_hypervisor" not in agent_service.allowed_job_types(agent):
        raise HTTPException(
            status_code=409,
            detail=(f"Agent '{agent.name}' is not granted the agent_hypervisor job "
                    f"type. Grant it on the Agents page."))

    # Still before create_job, for the same reason every refusal above is: a job row
    # created and then failed reads as an agent fault, and there is no field on it in which
    # to say otherwise.
    for problem in hcs.dashboard_secret_blockers(db, conn.id, agent):
        raise HTTPException(status_code=409, detail=problem)

    meta = agent_hypervisor_meta.normalize({
        "verb": verb, "connection_ref": conn.agent_connection_name or "",
        "connection_id": conn.id, "kind": conn.kind,
        "target_id": target_id, "target_scope": target_scope,
        "target_type": target_type,
    })
    meta["description"] = description or f"{verb} via agent '{agent.name}'"
    job = job_service.create_job(
        db, job_type="agent_hypervisor", created_by=created_by,
        metadata=meta, agent_id=agent.id, batch_id=batch_id)
    job_service.set_cloud_resource_id(db, job.id, conn.id)
    return job


def conn_in_task(db: Session, kind: str, connection_id: str):
    """Re-resolve a connection inside a background task's own session.

    Background jobs carry the connection **id**, never the resolved object and never the
    credential — a `Connection` holds plaintext, and closing over one would keep it
    alive in the task for the life of the job.

    Re-resolving at execution time is also the point: a job queued against connection B
    must still run against B if someone flips the default while it waits. An empty id
    means "whatever the default was", which is exactly what an un-migrated install wants.
    """
    return hcs.resolve(db, kind, connection_id or None)


# ── Bulk power ────────────────────────────────────────────────────────────────
#
# The selection toolbar on every on-prem VM page can power a set of VMs at once. The
# rule this section exists to keep is the one `api/config_mgmt.py::run_playbook_bulk`
# states for its own bulk run: *selection, not a second code path*. Every target here
# goes through the router's ordinary single-VM queue function, so every permission
# check, every agent gate and every per-kind op→verb translation behaves exactly as it
# does for one button press. What bulk adds is a shared `batch_id`, a cap, and the
# decision to run a direct batch SERIALLY.

# The same number, and deliberately the same sentence, as
# `inventory_service.MAX_BULK_TARGETS` — the other bulk fan-out in this codebase. Not
# `vm_naming.MAX_DEPLOY_COUNT` (20): that cap is about cloud quota and minutes of
# wall-clock per item, which is the wrong analogy for an op that takes seconds and
# creates one row. 50 also keeps a batch inside one page of /api/jobs (page_size caps
# at 100), so the batch view the operator lands on is never paginated.
BULK_MAX_TARGETS = 50


async def run_power_batch(tasks):
    """Walk a direct batch's power ops ONE AT A TIME.

    **Why this exists when Starlette would already do it.** ``BackgroundTasks.__call__``
    is itself ``for task in self.tasks: await task()``, so adding N tasks to one
    request's collection is serial today with no help from here. This function is six
    lines that make the serial order *ours* instead of inherited, and the reason is the
    one written at the top of ``services/cloud_executor.py``: that module exists because
    an upstream default — the event loop's thread-pool size — was load-bearing,
    invisible, and took the site down for thirty minutes when it turned out not to be
    what anyone assumed. Concurrent background tasks have been proposed upstream more
    than once. If that lands, a batch here would fan out into a provider pool that
    **refuses** at ``pool_size`` rather than queueing, and a 50-VM Force Off would
    complete 8 VMs and fail 42 with "hyperv is saturated (8/8 threads busy)" — a real
    failure, on a reasonable request, blamed on the operator. Nothing in the test suite
    would have noticed, because every job row would still have been created correctly.
    ``test_a_direct_batch_runs_serially`` pins this loop instead.

    Serial also matches what an agent-bound connection already does: the agent leases
    and executes one job at a time, so both paths pace the same way and the batch view
    reads the same on either.

    Each task is the router's own ``_run_power_op``, which owns its session and turns
    its own exceptions into ``set_failed``. One VM failing therefore does not abandon
    the walk — which is why this loop has no try/except of its own. The cost, and it is
    a real one: a hung SDK call stalls the rest of the batch behind it, bounded by
    ``cloud_executor.call_timeout()`` (60s in the app process).
    """
    for task in tasks:
        await task()


async def queue_power_batch(db, *, kind: str, op: str, targets: list, allowed_ops,
                            queue_one, label_of, created_by: str = "",
                            background_tasks=None) -> dict:
    """Queue one power op across many VMs, one job each, sharing a ``batch_id``.

    ``queue_one(target, batch_id)`` is the router's own single-VM path and must return
    ``{"job_id", "status", "task"}`` — ``task`` being a zero-arg coroutine function for
    a direct connection and None for an agent-bound one. ``label_of(target)`` names the
    VM for the operator; it is per-kind because the payload field is.

    Two levels of failure, and they differ on purpose (the same split
    ``run_playbook_bulk`` draws):

    * **Selection** problems — no targets, too many, an op this page does not offer —
      refuse the whole request before any job row exists.
    * **Per-target** problems do not necessarily apply to the rest. An op with no agent
      verb 501s, a workgroup a caller cannot reach 403s, and on a mixed selection those
      can be true of one VM and not another. Those land in ``failed`` and the remaining
      targets still run, rather than aborting a batch that is already part-queued.

    Exactly ONE background task is scheduled for the whole batch, never one per VM —
    see :func:`run_power_batch` for why that is this module's decision rather than
    Starlette's.

    Returns ``{batch_id, op, count, jobs, failed}``; raises 400 when nothing queued,
    because a response saying ``count: 0`` next to a 200 reads as success.
    """
    op = str(op or "").strip().lower()
    if op not in allowed_ops:
        # 400, not 501: 501 is `agent_power_job`'s answer for "this build cannot express
        # that op on this connection", which is a per-connection fact. This is the page
        # asking for an op it never offers at all.
        raise HTTPException(
            status_code=400,
            detail=(f"'{op}' is not a bulk power operation on a {kind} connection. "
                    f"Available: {', '.join(sorted(allowed_ops))}."))
    if not targets:
        raise HTTPException(
            status_code=400,
            detail="Select at least one VM. An empty selection would do nothing and "
                   "report success.")
    if len(targets) > BULK_MAX_TARGETS:
        raise HTTPException(
            status_code=400,
            detail=(f"{len(targets)} VMs selected; the limit for one bulk power "
                    f"operation is {BULK_MAX_TARGETS}. Narrow the selection with the "
                    f"filters and run again."))

    from ..services import job_service

    # One VM named twice is one job. A select-all across a filter change, or a page
    # whose key function collides for two rows, would otherwise send two power ops to
    # the same guest — and for `restart` that is a second power cut arriving during the
    # first boot. Order is preserved so the batch reads in the order it was selected.
    seen, unique = set(), []
    for target in targets:
        fingerprint = target.model_dump_json() if hasattr(target, "model_dump_json") \
            else repr(target)
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        unique.append(target)
    targets = unique

    batch_id = uuid.uuid4().hex[:12]
    jobs, failed, tasks = [], [], []
    try:
        for target in targets:
            label = label_of(target)
            try:
                result = await queue_one(target, batch_id)
            except HTTPException as exc:
                # An expected, per-target refusal: a 501 for an op with no verb on this
                # kind, a 403 for a workgroup this caller cannot reach, a 409 for an
                # offline agent. None of them necessarily applies to the next VM.
                failed.append({"name": label, "error": str(exc.detail)})
                continue
            jobs.append({"name": label, "job_id": result["job_id"],
                         "status": result.get("status") or "queued"})
            if result.get("task") is not None:
                tasks.append(result["task"])
    finally:
        # In a `finally`, and this is the part worth keeping. Anything that is NOT an
        # HTTPException — a DB error, a bug in a router's queue function — is deliberately
        # not caught: swallowing it would report a partial batch as a success. But by the
        # time it is raised the earlier targets already HAVE job rows, and on the direct
        # path those rows only ever run because something scheduled this walk. Letting the
        # exception skip the scheduling would leave them `pending` until
        # `reconcile_stale_jobs` failed them ~12 minutes later, having powered nothing —
        # a VM the operator watched go grey for no reason. So the work already committed
        # to the database gets scheduled either way, and the caller still gets the 500.
        if tasks:
            if background_tasks is None:
                # A programming error, said out loud. A router with a direct path that
                # forgets to pass its BackgroundTasks would otherwise create every job
                # row and run none of them: the quietest possible failure, indistinguishable
                # from an agent that never picked the work up.
                raise RuntimeError(
                    f"{kind}: queue_power_batch was given {len(tasks)} direct power "
                    f"task(s) and no background_tasks to run them on. The job rows "
                    f"exist and nothing would execute them.")
            background_tasks.add_task(run_power_batch, tasks)

    if not jobs:
        # Surface the first reason rather than a misleading empty success. The operator
        # is looking at a toolbar that did nothing and has no other evidence.
        #
        # Raised BEFORE the audit row below, deliberately: a batch that queued nothing
        # must not leave a record that looks like one that did. The per-VM refusals are
        # already visible to the caller in this message.
        detail = failed[0]["error"] if failed else "No VMs could be powered."
        raise HTTPException(
            status_code=400,
            detail=f"No jobs were queued. The first VM failed with: {detail}")

    job_service.log_audit(
        db, created_by, f"{kind}_power_bulk",
        details={"batch_id": batch_id, "op": op, "count": len(jobs),
                 "targets": [j["name"] for j in jobs],
                 "failed": [f["name"] for f in failed]})

    return {"batch_id": batch_id, "op": op, "count": len(jobs),
            "jobs": jobs, "failed": failed}
