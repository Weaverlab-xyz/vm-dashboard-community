"""Enqueue-and-await bridge between the storage abstraction and a remote agent.

Every other agent caller in this codebase is fire-and-forget: it creates a job row, returns
a job id, and the operator watches Live Output on /jobs. Storage cannot work that way. When
``/config-mgmt`` asks for a playbook's bytes, or an asset picker asks what is on a share,
there is a caller sitting on the other end of an HTTP request that needs an answer, not a
job id. So this module is the one place that creates an agent job and then waits for it.

WHAT THE WAIT COSTS
-------------------
The agent polls every ``agent_service.DEFAULT_POLL_INTERVAL_S`` (5s), so a round trip is
five to fifteen seconds — a very long time to hold a request open. Two things keep that
tolerable, and both live outside this module:

* ``storage_service`` caches listings, which is what the asset pickers on six pages
  actually call. Without that cache every one of them would sit on a round trip.
* Fetches during a Config-Management run happen in the job worker, not the request path.

Anything else calling in here should expect to be slow and should say so in its UI.

WHY EVERY PREFLIGHT IS BEFORE ``create_job``
--------------------------------------------
The same reason ``api/hypervisor_deps.agent_power_job`` puts its checks there: a job row
that is created and then immediately failed reads as an agent fault, and a failed job
renders its ``error_message`` and nothing else — there is no field on it in which to say
"actually your dashboard never should have asked". A refusal here leaves no row behind and
lands on the page that asked, where the remedy is.
"""
import asyncio
import logging
from typing import Optional

from ..database import RemoteAgent, SessionLocal
from . import agent_service, agent_storage_meta, config_service, job_service

logger = logging.getLogger(__name__)

# How long to wait for an agent to lease, run and report one file operation. Generous
# against the 5s poll interval — three missed polls plus the work — but bounded, because
# this is held open on a request thread.
STORAGE_JOB_DEADLINE_S = 90

# How often to look at the job row while waiting. Not a heartbeat and not tunable: it is
# the resolution of the deadline above, and each tick costs one indexed primary-key read.
_POLL_INTERVAL_S = 1.0

_TERMINAL = ("completed", "failed", "cancelled")


class AgentStorageError(Exception):
    """A storage operation could not be carried out through the agent.

    ``storage_service`` re-raises this as ``StorageError`` so that the dozens of existing
    ``except StorageError`` handlers — which are what turn a failure into a 503 or an
    unavailable tile — keep working without knowing an agent was involved.
    """


def configured() -> tuple:
    """``(agent_id, share, subpath)`` for the agent-backed backend, from config."""
    return (
        (config_service.get("storage_agent_id") or "").strip(),
        (config_service.get("storage_agent_share") or "").strip(),
        (config_service.get("storage_agent_subpath") or "").strip(),
    )


def _preflight(db, meta: dict) -> RemoteAgent:
    """Resolve the agent and refuse anything that cannot possibly work.

    Each refusal is its own sentence naming its own remedy. They are deliberately not
    collapsed into one "storage is unavailable" — an offline agent, an ungranted job type
    and a too-old build are three different afternoons for whoever has to fix it.
    """
    agent_id, share, _subpath = configured()
    if not agent_id:
        raise AgentStorageError(
            "No agent is configured for the remote filesystem backend. Pick one on "
            "/storage.")
    if not share:
        raise AgentStorageError(
            "No share name is configured for the remote filesystem backend. It must match "
            "a `name:` in that agent's shares.yaml. Set it on /storage.")

    problem = agent_storage_meta.check(meta)
    if problem:
        raise AgentStorageError(problem)

    agent = db.query(RemoteAgent).filter(RemoteAgent.id == agent_id).first()
    if agent is None:
        raise AgentStorageError(
            "The agent configured for the remote filesystem backend no longer exists. "
            "Pick another on /storage.")
    if agent_service.status_of(agent) != "online":
        raise AgentStorageError(
            f"Agent '{agent.name}' is offline, so the share '{share}' cannot be reached. "
            f"Assets already uploaded are safe — this is a connectivity problem, not a "
            f"missing file.")
    if "agent_storage" not in agent_service.allowed_job_types(agent):
        raise AgentStorageError(
            f"Agent '{agent.name}' is not granted the agent_storage job type. Grant it on "
            f"the Agents page.")
    if not agent_service.supports_storage(agent):
        raise AgentStorageError(agent_service.storage_upgrade_hint(agent))
    return agent


async def run_op(op: str, *, name: str = "", content_b64: str = "") -> dict:
    """Run one file operation on the configured share and return the agent's result.

    Raises :class:`AgentStorageError` for every failure, including the timeout — a caller
    in the request path needs one exception type it can turn into a message, not a status
    code it has to interpret.
    """
    agent_id, share, subpath = configured()
    meta = agent_storage_meta.storage_meta(
        op, share=share, subpath=subpath, name=name, content_b64=content_b64)

    db = SessionLocal()
    try:
        agent = _preflight(db, meta)
        job = job_service.create_job(
            db, job_type="agent_storage", created_by="storage",
            metadata=meta, agent_id=agent.id)
        job_id, agent_name = job.id, agent.name
    finally:
        db.close()

    try:
        return await _await_result(job_id, op, share, agent_name)
    finally:
        # Both directions leave file bytes on the row: an upload's are in the metadata
        # because that is where the envelope is built from, and a fetch's arrive there
        # because `set_completed` merges the agent's result into the same dict. Keeping
        # either serves nothing and outlives every retention rule the share itself has.
        # Best-effort: a scrub that fails must not turn a successful transfer into an
        # error.
        if op in ("upload", "fetch"):
            _scrub_content(job_id)


async def _await_result(job_id: str, op: str, share: str, agent_name: str) -> dict:
    """Poll the job row to a terminal state, or give up at the deadline.

    A fresh, short-lived session per tick rather than one held open for the whole wait.
    This runs on the request path and the app DB pool has a hard ceiling; a session parked
    for ninety seconds is a connection ninety seconds of other requests cannot have.
    """
    waited = 0.0
    while waited < STORAGE_JOB_DEADLINE_S:
        await asyncio.sleep(_POLL_INTERVAL_S)
        waited += _POLL_INTERVAL_S

        db = SessionLocal()
        try:
            job = job_service.get_job(db, job_id)
            if job is None:
                raise AgentStorageError(
                    f"The {op} job against share '{share}' disappeared before it finished.")
            status = job.status
            # `set_completed` MERGES the agent's result into the row's metadata rather
            # than storing it separately — there is no result column — so the completed
            # row carries the request keys and the answer in one dict. Harmless here:
            # the readers below take the keys they want by name.
            result = job.metadata_dict if status == "completed" else None
            error = job.error_message or ""
        finally:
            db.close()

        if status == "completed":
            return result if isinstance(result, dict) else {}
        if status in _TERMINAL:
            raise AgentStorageError(
                f"Agent '{agent_name}' could not {op} on share '{share}': "
                f"{error or 'the job ended without reporting a reason.'}")

    _cancel_if_unclaimed(job_id)
    raise AgentStorageError(
        f"Agent '{agent_name}' did not answer a {op} on share '{share}' within "
        f"{STORAGE_JOB_DEADLINE_S}s. Job {job_id[:8]} on /jobs has the detail.")


def _cancel_if_unclaimed(job_id: str) -> None:
    """Cancel a timed-out job, but only while it is still queued.

    A ``queued`` row the agent has not leased yet is the dangerous case: left alone it
    could be picked up minutes later and perform an upload or a delete the operator was
    already told had failed. A ``running`` row is left exactly as it is — the agent is
    mid-operation, and rewriting the row would not stop it, it would only lie about it.
    """
    db = SessionLocal()
    try:
        job = job_service.get_job(db, job_id)
        if job is not None and job.status == "queued":
            job_service.set_cancelled(db, job_id)
    except Exception:  # noqa: BLE001 — the timeout is the error; this is tidying
        logger.warning("could not cancel timed-out storage job %s", job_id, exc_info=True)
    finally:
        db.close()


def _scrub_content(job_id: str) -> None:
    """Blank ``content_b64`` on a finished transfer's job row."""
    db = SessionLocal()
    try:
        job = job_service.get_job(db, job_id)
        if job is None:
            return
        meta = job.metadata_dict
        if meta.get("content_b64"):
            meta["content_b64"] = ""
            job.metadata_dict = meta
            db.commit()
    except Exception:  # noqa: BLE001 — never fail a completed transfer over housekeeping
        logger.warning("could not scrub file content from job %s", job_id, exc_info=True)
    finally:
        db.close()


def agent_name_for(agent_id: str) -> Optional[str]:
    """Display name for a configured agent id, or None. Used by the /storage page."""
    db = SessionLocal()
    try:
        agent = db.query(RemoteAgent).filter(RemoteAgent.id == agent_id).first()
        return agent.name if agent else None
    finally:
        db.close()
