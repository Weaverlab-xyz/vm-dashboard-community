"""Scheduled hypervisor inventory sync, brokered by a remote agent.

An agent-bound connection is on a network the dashboard cannot reach, so its inventory
has to be pulled by the agent and pushed back. This module owns both halves of that:
enqueueing the due syncs, and applying the pages an agent returns.

**Why the pages.** ``agent_service.MAX_RESULT_BYTES`` is 256 KB, and a 3000-VM vCenter
is well over a megabyte of normalised JSON. Raising that cap is not an option — it is
the only bound on an agent's write path into the database. So ``inventory_sync`` returns
one page plus an opaque cursor, and :func:`apply_page` enqueues the follow-on job. The
chain is capped at :data:`MAX_SYNC_PAGES`, which is what stops a lying agent making the
dashboard enqueue work forever. Every page of one sync shares a ``batch_id`` so the N
job rows roll up as one run.

**Why a power op queues one of these.** The dashboard cannot see a hypervisor it reaches
through an agent, so a start or a stop leaves the cache saying the opposite of the truth
until the next timed pass — up to ``hypervisor_sync_interval_minutes`` later. Operators
were pressing Sync Now by hand after every button. :func:`sync_after_power` closes that
loop from the completion path instead; see it for why it fires on a failed op too.

**Why this is not a worker handler.** ``agent_hypervisor`` must stay disjoint from
``jobs_worker.HANDLED_TYPES`` or the local worker would race the agent for the row. So
the periodic pass is an asyncio loop in ``main`` (the same shape as the expiry sweeper),
and single-flight across two gunicorn workers comes from ``lease_one``'s
``UPDATE … WHERE status='queued' AND agent_id=:id`` rowcount — the first worker to claim
it wins, exactly as the expiry design leans on ``_claim_one``.
"""
import json
import logging
from datetime import datetime

from sqlalchemy.orm import Session

from ..config import settings
from ..database import HypervisorConnection, HypervisorVMCache, Job
from . import agent_hypervisor_meta, agent_service, config_service, job_service

logger = logging.getLogger(__name__)

MAX_SYNC_PAGES = agent_hypervisor_meta.MAX_SYNC_PAGES
DEFAULT_INTERVAL_MINUTES = 30


def _interval_minutes(conn: HypervisorConnection) -> int:
    """Per-connection cadence, falling back to a config key read live each pass."""
    try:
        per_conn = int((conn.options_dict or {}).get("sync_interval_minutes") or 0)
    except (TypeError, ValueError):
        per_conn = 0
    if per_conn > 0:
        return per_conn
    try:
        return max(1, int(config_service.get("hypervisor_sync_interval_minutes")
                          or getattr(settings, "hypervisor_sync_interval_minutes",
                                     DEFAULT_INTERVAL_MINUTES)))
    except (TypeError, ValueError):
        return DEFAULT_INTERVAL_MINUTES


def enqueue_due_syncs(db: Session) -> int:
    """Queue one inventory_sync per agent-bound connection that is due. Returns the count.

    Skips rather than queues when the agent is offline, and records why on the
    connection: a job that silently waits three days is worse than a visible refusal.
    """
    if not config_service.get_bool("remote_agents_enabled",
                                   getattr(settings, "remote_agents_enabled", False)):
        return 0

    rows = db.query(HypervisorConnection).filter(
        HypervisorConnection.is_active.is_(True),
        HypervisorConnection.agent_id.isnot(None)).all()
    if not rows:
        return 0

    now = datetime.utcnow()
    queued = 0
    for conn in rows:
        interval = _interval_minutes(conn)
        if conn.last_sync_at and (now - conn.last_sync_at).total_seconds() < interval * 60:
            continue
        # Everything past the cadence check is `sync_now`'s job, and the cadence check is
        # the ONLY thing the manual path drops — which is the whole point of a manual
        # sync. Two copies of "is this connection syncable" would drift, and the one that
        # drifted would be the one the operator reaches for when the timed pass is not
        # producing rows.
        job, reason = sync_now(db, conn)
        if job is None:
            if reason:
                _note(db, conn, reason)
            continue
        queued += 1
    return queued


def hcs_blockers(db: Session, connection_id: str, agent) -> list:
    from . import hypervisor_connection_service as hcs
    return hcs.dashboard_secret_blockers(db, connection_id, agent)


def sync_now(db: Session, conn: HypervisorConnection, *, created_by: str = "",
             trigger: str = ""):
    """Queue an inventory_sync for ONE connection, ignoring its cadence.

    Returns ``(job, "")`` when queued, or ``(None, reason)`` when it cannot be — the
    reason being operator-facing prose, because both callers surface it: the timed pass
    records it on the connection, and the manual one puts it in front of whoever pressed
    the button. A silent skip is what makes a sync look like it ran.

    Every refusal the timed pass makes lives here, and the cadence check is the only
    thing that does not: the manual path exists precisely to skip the clock, not the
    guards. Two copies would drift, and the one that drifted would be the one an operator
    reaches for when the timed pass is not producing rows.
    """
    if not getattr(conn, "agent_id", None):
        return None, ("this connection is not bound to an agent, and the dashboard has "
                      "no route to it")
    if _has_open_job(db, conn.id):
        return None, ""      # already queued or running — not a fault, and not news

    from ..database import RemoteAgent
    agent = db.query(RemoteAgent).filter(RemoteAgent.id == conn.agent_id).first()
    if agent is None or agent_service.status_of(agent) != "online":
        return None, "the bound agent is not online, so the sync was skipped"
    if "agent_hypervisor" not in agent_service.allowed_job_types(agent):
        return None, "the bound agent is not granted the agent_hypervisor job type"
    # A dashboard-held credential the bound agent could not collect. Queueing anyway
    # fails against the hypervisor's own "wrong username or password" — every thirty
    # minutes on the timed pass, until the service account locks out. A button that
    # queued it on demand would do the same thing faster, so this guard belongs to both.
    blockers = hcs_blockers(db, conn.id, agent)
    if blockers:
        return None, blockers[0]

    return _queue(db, conn, cursor="", batch_id=None, created_by=created_by,
                  trigger=trigger), ""


# The verbs after which the cached row is a lie. Every one of them moves a VM's power
# state, which is the only thing on this page an operator watches change.
#
# `snapshot` is the one WRITE verb deliberately absent: it creates something the
# inventory cache does not store a single column of, so re-reading a whole vCenter for
# it would be minutes of work with nothing to show for it.
# tests/test_power_resync.py pins that split, so a verb added to WRITE_VERBS later has
# to be classified here rather than silently defaulting to "no resync".
RESYNC_VERBS = ("power_on", "power_off", "power_reset", "restart", "shutdown", "reboot")


def sync_after_power(db: Session, job: Job):
    """Queue an inventory sync for the connection a just-finished power job acted on.

    Returns the same ``(job, reason)`` pair as :func:`sync_now`, and ``(None, "")`` when
    the finished job was not a power op at all — which is most of them, since every
    inventory_sync page is also an ``agent_hypervisor`` row.

    **Why it runs on a FAILED op too.** A refusal is cheap to re-read and an *uncertain*
    outcome is the case that matters: the agent issuing the power call and then losing
    the response is indistinguishable, from here, from one that never left — and the VM
    may well have moved. "Make the cache match the hypervisor" is the right answer to
    both, and the wrong time to ask for it is exactly when nobody is sure.

    **Why the in-flight guard is not bypassed.** ``sync_now`` refuses while another sync
    is open on the connection, and for a burst of power ops that is the behaviour worth
    having: the LAST one to finish queues the sync, and it sees every VM the earlier ones
    moved. Bypassing it would queue N syncs of the same inventory instead.

    Called after the job row reaches a terminal status, never before — until then the
    power job is itself the connection's open ``agent_hypervisor`` row, and the guard
    above would read it and skip.
    """
    if getattr(job, "job_type", "") != "agent_hypervisor":
        return None, ""
    meta = job.metadata_dict or {}
    if str(meta.get("verb") or "") not in RESYNC_VERBS:
        return None, ""

    # The job's own metadata first, `cloud_resource_id` as the fallback: both are
    # written by `agent_power_job`, and the meta copy is the one that survives a row
    # whose resource id was never stamped.
    connection_id = str(meta.get("connection_id") or job.cloud_resource_id or "")
    if not connection_id:
        return None, ""
    conn = db.query(HypervisorConnection).filter(
        HypervisorConnection.id == connection_id).first()
    if conn is None or not conn.is_active:
        return None, ""

    return sync_now(db, conn, created_by=job.created_by or "",
                    trigger=f"after {meta['verb']}")


def _has_open_job(db: Session, connection_id: str) -> bool:
    """Is a sync already queued or running for this connection?

    Keyed on ``Job.cloud_resource_id``, which is indexed and portable — an
    ``extra_data LIKE`` would work on neither SQLite nor PostgreSQL consistently. The
    column's name is a mild stretch for a hypervisor connection; its meaning ("which
    resource is this job about") is exactly right.
    """
    return db.query(Job.id).filter(
        Job.job_type == "agent_hypervisor",
        Job.cloud_resource_id == connection_id,
        Job.status.in_(("queued", "running"))).first() is not None


def _queue(db: Session, conn: HypervisorConnection, *, cursor: str, batch_id,
           page: int = 1, started: datetime = None, created_by: str = "",
           trigger: str = "") -> Job:
    """One inventory_sync job. Bookkeeping fields ride alongside the allowlisted meta.

    ``sync_started_at`` is carried forward through the whole chain rather than being
    re-derived per page, so the prune at the end removes rows that predate the *pass*
    rather than the last page. ``created_by`` and ``trigger`` are carried the same way,
    for a plainer reason: a non-admin only sees their OWN rows on /jobs, so a sync
    attributed to ``system:sync`` is one they cannot see at all — and pages 2..N of the
    sync they caused would drop out from under them mid-run.

    ``trigger`` reaches the description and nothing else. It is composed here from a
    closed verb allowlist, never from a VM name: a hypervisor's own strings are the one
    thing on this path the dashboard did not write.
    """
    meta = agent_hypervisor_meta.normalize({
        "verb": "inventory_sync",
        "connection_ref": conn.agent_connection_name or "",
        "connection_id": conn.id,
        "kind": conn.kind,
        "cursor": cursor,
    })
    meta["description"] = (f"Inventory sync: {conn.name} (page {page})"
                           + (f" — {trigger}" if trigger else ""))
    meta["sync_page"] = page
    meta["sync_started_at"] = (started or datetime.utcnow()).isoformat()
    meta["sync_trigger"] = trigger
    job = job_service.create_job(
        db, job_type="agent_hypervisor", created_by=created_by or "system:sync",
        metadata=meta, agent_id=conn.agent_id, batch_id=batch_id)
    job_service.set_cloud_resource_id(db, job.id, conn.id)
    return job


def _note(db: Session, conn: HypervisorConnection, message: str) -> None:
    from . import hypervisor_connection_service as hcs
    hcs.record_result(db, conn.id, error=message)


def apply_page(db: Session, job: Job, result: dict) -> dict:
    """Apply one returned page and chain the next, if there is one.

    Returns the sanitised page so the caller can store it on the job row.
    """
    page = agent_hypervisor_meta.sync_page(result)
    meta = job.metadata_dict or {}
    connection_id = meta.get("connection_id") or job.cloud_resource_id or ""
    conn = db.query(HypervisorConnection).filter(
        HypervisorConnection.id == connection_id).first()
    if conn is None:
        return page

    started = _sync_started_at(db, job, meta)
    _upsert(db, connection_id, page["vms"], started)

    page_no = int(meta.get("sync_page") or 1)
    if page["next_cursor"] and not page["complete"] and page_no < MAX_SYNC_PAGES:
        _queue(db, conn, cursor=page["next_cursor"], batch_id=job.batch_id or job.id,
               page=page_no + 1, started=started, created_by=job.created_by or "",
               trigger=str(meta.get("sync_trigger") or ""))
        logger.info("hypervisor sync: queued page %d for %s", page_no + 1, conn.name)
    else:
        if page_no >= MAX_SYNC_PAGES and page["next_cursor"]:
            logger.warning(
                "hypervisor sync for %s stopped at the %d-page cap with a cursor still "
                "outstanding — the inventory is larger than the chain allows",
                conn.name, MAX_SYNC_PAGES)
        _prune(db, connection_id, started)
        from . import hypervisor_connection_service as hcs
        hcs.record_result(db, connection_id, synced=True)
    return page


def _sync_started_at(db: Session, job: Job, meta: dict) -> datetime:
    raw = meta.get("sync_started_at")
    if raw:
        try:
            return datetime.fromisoformat(str(raw))
        except ValueError:
            pass
    return job.created_at or datetime.utcnow()


def _upsert(db: Session, connection_id: str, vms: list, started: datetime) -> None:
    for vm in vms:
        vm_id = vm.get("vm_id")
        if not vm_id:
            continue
        row = db.query(HypervisorVMCache).filter(
            HypervisorVMCache.connection_id == connection_id,
            HypervisorVMCache.vm_id == vm_id).first()
        if row is None:
            row = HypervisorVMCache(connection_id=connection_id, vm_id=vm_id)
            db.add(row)
        row.name = vm.get("name")
        row.power_state = vm.get("power_state")
        row.vcpus = vm.get("vcpus")
        row.mem_mib = vm.get("mem_mib")
        row.ip_addresses = json.dumps(vm.get("ip_addresses") or [])
        row.scope = vm.get("scope")
        row.vm_type = vm.get("vm_type")
        row.tags = json.dumps(vm.get("tags") or [])
        row.guest_os = vm.get("guest_os")
        row.synced_at = max(started, datetime.utcnow())
    db.commit()


def _prune(db: Session, connection_id: str, started: datetime) -> None:
    """Drop rows this pass did not touch — that is deletion detection.

    Only ever after the LAST page: pruning per page would delete every VM that happened
    to be on a later one.
    """
    db.query(HypervisorVMCache).filter(
        HypervisorVMCache.connection_id == connection_id,
        HypervisorVMCache.synced_at < started).delete(synchronize_session=False)
    db.commit()


def list_vms(db: Session, connection_id: str = "") -> list:
    query = db.query(HypervisorVMCache)
    if connection_id:
        query = query.filter(HypervisorVMCache.connection_id == connection_id)
    out = []
    for row in query.order_by(HypervisorVMCache.name).all():
        out.append({
            "connection_id": row.connection_id, "vm_id": row.vm_id,
            "name": row.name or "", "power_state": row.power_state or "",
            "vcpus": row.vcpus, "mem_mib": row.mem_mib,
            "ip_addresses": json.loads(row.ip_addresses or "[]"),
            "scope": row.scope or "", "vm_type": row.vm_type or "",
            "tags": json.loads(row.tags or "[]"),
            "guest_os": row.guest_os or "",
            "synced_at": row.synced_at.isoformat() if row.synced_at else None,
        })
    return out
