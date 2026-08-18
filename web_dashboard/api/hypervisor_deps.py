"""Shared helper for the five hypervisor routers.

Each of ``api/{proxmox,vsphere,nutanix,hyperv,xcpng}.py`` needs the same two lines: turn
an optional ``connection_id`` query parameter into a resolved
:class:`~web_dashboard.services.hypervisor_connection_service.Connection`, and turn a
resolution failure into an HTTP status. Keeping it here means the five cannot drift on
which status a missing connection produces.

FastAPI lives here rather than in the service so ``hypervisor_connection_service`` stays
importable — and testable — without the web framework.
"""
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


def agent_power_job(db: Session, conn, *, verb: str, target_id: str,
                    target_scope: str = "", target_type: str = "vm",
                    created_by: str = "", description: str = ""):
    """Enqueue a power verb for an AGENT-BOUND connection, or return None.

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

    ``verb`` must already be one of the agent's write verbs; a caller whose op has no
    agent equivalent has to refuse it rather than pass the op name through. See the
    check below for why that cannot be left to the normaliser.
    """
    if not getattr(conn, "agent_id", None):
        return None

    from ..database import RemoteAgent
    from ..services import agent_hypervisor_meta, agent_service, job_service

    # Refused here rather than left to normalize(): an unrecognised verb falls back to
    # ``inventory_sync`` there (deliberately — see agent_hypervisor_meta), which for a
    # POWER request is the worst possible outcome. The operator would get a job that
    # completes GREEN having run a scan, while the VM never moved. A router that has no
    # agent verb for one of its ops must say so instead of passing the op through.
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

    meta = agent_hypervisor_meta.normalize({
        "verb": verb, "connection_ref": conn.agent_connection_name or "",
        "connection_id": conn.id, "kind": conn.kind,
        "target_id": target_id, "target_scope": target_scope,
        "target_type": target_type,
    })
    meta["description"] = description or f"{verb} via agent '{agent.name}'"
    job = job_service.create_job(
        db, job_type="agent_hypervisor", created_by=created_by,
        metadata=meta, agent_id=agent.id)
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
