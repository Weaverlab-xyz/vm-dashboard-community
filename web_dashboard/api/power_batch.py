"""Fan one power op out over a selection of VMs — shared by every provider.

Lives here rather than in ``hypervisor_deps`` because the four cloud routers use it too,
and a cloud router importing from a module named for hypervisors is the kind of thing
that reads as an accident and then gets "tidied" into a copy. There are eleven callers
across two families and one of them must not be a special case:

  on-prem   api/{proxmox,vsphere,nutanix,hyperv,xcpng}.py, api/vms.py
  cloud     api/{aws,azure,gcp,oci}.py

The two families differ in exactly one way, and it is the only branch in here: an
on-prem connection the dashboard dials itself has local work to run, so its
``queue_one`` hands back a ``task`` and :func:`run_power_batch` walks them. Every cloud
power op — and every agent-bound on-prem one — is a job row a worker claims, so ``task``
is None and nothing is scheduled.

FastAPI's ``HTTPException`` is the vocabulary here: a per-target refusal arrives as one
and leaves as a ``failed`` entry, which is what lets a mixed selection part-succeed.
"""
import uuid

from fastapi import HTTPException


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
