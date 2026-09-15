"""Hypervisor connections API.

Replaces the five singleton Settings panels: N connections per hypervisor kind, each
either dialled by the dashboard or reached through a remote agent.

Admin-only throughout. These rows hold credentials for the hosts that run everything
else, and unlike a VM or a database there is no per-workgroup ownership model that would
make a narrower grant meaningful — an operator who can edit a vCenter connection can do
anything that vCenter allows.
"""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import RemoteAgent, User, get_db
from ..services import config_mgmt_route_service as cmr
from ..services import hypervisor_connection_service as hcs
from ..services import job_service
from .auth import require_explicit_permission

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/connections", tags=["connections"])


class ConnectionRequest(BaseModel):
    """A connection to create.

    ``secret`` is write-only and never echoed back — :func:`hcs.serialize` has no field
    for it, only a ``has_secret`` boolean.

    An **agent-bound** connection (``agent_id`` set) always ignores ``host`` and
    ``username``: those aim the connection, and they stay the customer's to choose in that
    agent's own connections.yaml, with ``agent_connection_name`` as the join. It may carry
    ``secret`` or ``secret_ref`` so the agent can fetch the credential per job instead of
    storing it on the on-prem host — inert until that agent's file sets
    ``dashboard_secret: true``, so setting one here cannot change behaviour by itself. A
    ``secret_ref`` of ``ps_account://<id>`` means the dashboard holds no password either.
    """
    kind: str
    name: str
    host: str = ""
    port: Optional[int] = None
    username: str = ""
    secret: str = ""
    secret_ref: str = ""
    verify_ssl: bool = False
    options: dict = {}
    agent_id: str = ""
    agent_connection_name: str = ""
    site: str = ""
    is_default: bool = False


class ConnectionUpdate(BaseModel):
    name: Optional[str] = None
    host: Optional[str] = None
    port: Optional[int] = None
    username: Optional[str] = None
    # Blank means "leave the stored secret alone", never "clear it" — otherwise every
    # edit through a form that does not echo the password would wipe it.
    secret: Optional[str] = None
    secret_ref: Optional[str] = None
    verify_ssl: Optional[bool] = None
    options: Optional[dict] = None
    agent_id: Optional[str] = None
    agent_connection_name: Optional[str] = None
    site: Optional[str] = None
    is_active: Optional[bool] = None


def _active_agents(db: Session) -> list:
    """The agent picker's options, for every form on this page.

    One helper rather than one list comprehension per endpoint: two copies would
    eventually disagree about whether a revoked agent is offered, and the whole point of
    the picker is that an operator never types an agent id.
    """
    return [{"id": a.id, "name": a.name, "site": a.site or ""}
            for a in db.query(RemoteAgent).filter(
                RemoteAgent.is_active.is_(True)).order_by(RemoteAgent.name).all()]


@router.get("")
async def list_connections(kind: str = "",
                           db: Session = Depends(get_db),
                           current_user: User = Depends(require_explicit_permission("connections", "read"))):
    """Every configured connection, optionally filtered to one kind.

    Also returns the enrolled agents, so the connection form can offer an agent
    picker instead of asking an operator to paste a uuid.
    """
    return {"connections": hcs.list_connections(db, kind),
            "kinds": list(hcs.VALID_KINDS),
            "agents": _active_agents(db)}


# ── Config-Management execution routes ────────────────────────────────────────
#
# Which agent EXECUTES a Config-Management run against an address range, as opposed to
# which agent brokers the hypervisor it was discovered through. Served from this router,
# and on this router's `connections` scope, deliberately:
#
#   * The audience is identical. A route decides which host runs a playbook as root, so
#     it is at least as powerful as a vCenter credential, and this module's own docstring
#     already argues why these rows have no narrower grant worth having.
#   * It is not an escalation over what `connections:write` already gives. That
#     permission lets its holder create an agent-bound connection naming any agent, and
#     that binding is what decides the executing agent today.
#   * A new scope would cost a catalog entry, a display group, grid labels and a backfill
#     (tests/test_permission_catalog.py enforces those together) for no change in who
#     should hold it.
#
# Split it out only if someone genuinely needs to grant hypervisor-credential management
# WITHOUT playbook routing. That is the condition; it is not the case today.

class RouteRequest(BaseModel):
    """``cidr`` is normalised and validated by the service, never stored as typed."""
    agent_id: str
    cidr: str
    label: str = ""


class RouteUpdate(BaseModel):
    agent_id: Optional[str] = None
    cidr: Optional[str] = None
    label: Optional[str] = None
    is_active: Optional[bool] = None


@router.get("/config-mgmt-routes")
async def list_config_routes(db: Session = Depends(get_db),
                             current_user: User = Depends(require_explicit_permission("connections", "read"))):
    """Every route, the agents that can be named by one, and how many VMs each covers.

    ``matches`` is the answer to "did my range actually bind to anything", and it is why
    a route needs no Test button: there is nothing to dial, so coverage is the only
    useful feedback. ``ansible_agent_ids`` lets the form mark an agent that would refuse
    the job type, rather than letting the operator find out on save.
    """
    from ..services import agent_service

    counts = cmr.match_counts(db)
    routes = cmr.list_routes(db)
    for row in routes:
        row["matches"] = counts.get(row["id"], 0)
    granted = [a.id for a in db.query(RemoteAgent).filter(
        RemoteAgent.is_active.is_(True)).all()
        if "agent_ansible" in agent_service.allowed_job_types(a)]
    return {"routes": routes, "agents": _active_agents(db),
            "ansible_agent_ids": granted}


@router.get("/config-mgmt-routes/resolve")
async def resolve_config_route(address: str,
                               db: Session = Depends(get_db),
                               current_user: User = Depends(require_explicit_permission("connections", "read"))):
    """Which agent would execute a run against ``address``.

    The question operators actually ask, answered without queueing anything. ``source``
    is ``"route"`` when a range decided it and ``"none"`` when nothing did — in which
    case the executor is whichever agent brokers the target's connection, which this
    endpoint cannot know from an address alone.
    """
    route = cmr.match_for(db, address)
    if route is None:
        return {"agent_id": "", "agent_name": "", "cidr": "", "source": "none"}
    agent = db.query(RemoteAgent).filter(RemoteAgent.id == route.agent_id).first()
    return {"agent_id": route.agent_id,
            "agent_name": agent.name if agent else "",
            "cidr": route.cidr, "label": route.label, "source": "route"}


@router.post("/config-mgmt-routes", status_code=201)
async def create_config_route(req: RouteRequest,
                              db: Session = Depends(get_db),
                              current_user: User = Depends(require_explicit_permission("connections", "write"))):
    try:
        out = cmr.create(db, agent_id=req.agent_id, cidr=req.cidr, label=req.label,
                         created_by=current_user.username)
    except cmr.ConfigRouteError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    job_service.log_audit(db, current_user.username, "config_mgmt_route_create",
                          details={"id": out["id"], "cidr": out["cidr"],
                                   "agent_id": out["agent_id"]})
    return out


@router.patch("/config-mgmt-routes/{route_id}")
async def update_config_route(route_id: str, req: RouteUpdate,
                              db: Session = Depends(get_db),
                              current_user: User = Depends(require_explicit_permission("connections", "write"))):
    fields = req.model_dump(exclude_unset=True)
    try:
        out = cmr.update(db, route_id, **fields)
    except cmr.ConfigRouteError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    job_service.log_audit(db, current_user.username, "config_mgmt_route_update",
                          details={"id": route_id, "fields": sorted(fields)})
    return out


@router.delete("/config-mgmt-routes/{route_id}")
async def delete_config_route(route_id: str,
                              db: Session = Depends(get_db),
                              current_user: User = Depends(require_explicit_permission("connections", "delete"))):
    try:
        cmr.delete(db, route_id)
    except cmr.ConfigRouteError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    job_service.log_audit(db, current_user.username, "config_mgmt_route_delete",
                          details={"id": route_id})
    return {"ok": True}


@router.post("", status_code=201)
async def create_connection(req: ConnectionRequest,
                            db: Session = Depends(get_db),
                            current_user: User = Depends(require_explicit_permission("connections", "write"))):
    try:
        out = hcs.create(
            db, kind=req.kind, name=req.name, created_by=current_user.username,
            host=req.host, port=req.port, username=req.username, secret=req.secret,
            secret_ref=req.secret_ref, verify_ssl=req.verify_ssl, options=req.options,
            agent_id=req.agent_id, agent_connection_name=req.agent_connection_name,
            site=req.site, is_default=req.is_default)
    except hcs.HypervisorConnectionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    job_service.log_audit(db, current_user.username, "hypervisor_connection_create",
                          details={"id": out["id"], "kind": out["kind"], "name": out["name"]})
    return out


@router.patch("/{connection_id}")
async def update_connection(connection_id: str, req: ConnectionUpdate,
                            db: Session = Depends(get_db),
                            current_user: User = Depends(require_explicit_permission("connections", "write"))):
    try:
        out = hcs.update(db, connection_id, **req.model_dump(exclude_unset=True))
    except hcs.HypervisorConnectionError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    job_service.log_audit(db, current_user.username, "hypervisor_connection_update",
                          details={"id": connection_id,
                                   "fields": sorted(req.model_dump(exclude_unset=True))})
    return out


@router.post("/{connection_id}/default")
async def make_default(connection_id: str,
                       db: Session = Depends(get_db),
                       current_user: User = Depends(require_explicit_permission("connections", "write"))):
    try:
        return hcs.set_default(db, connection_id)
    except hcs.HypervisorConnectionError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete("/{connection_id}")
async def delete_connection(connection_id: str,
                            db: Session = Depends(get_db),
                            current_user: User = Depends(require_explicit_permission("connections", "delete"))):
    try:
        hcs.delete(db, connection_id)
    except hcs.HypervisorConnectionError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    job_service.log_audit(db, current_user.username, "hypervisor_connection_delete",
                          details={"id": connection_id})
    return {"ok": True}


@router.post("/{connection_id}/test")
async def test_connection(connection_id: str,
                          db: Session = Depends(get_db),
                          current_user: User = Depends(require_explicit_permission("connections", "read"))):
    """Dial the endpoint once and stamp the outcome on the row.

    Agent-bound connections are not testable from here and say so rather than
    failing: the dashboard has no route to them, which is the entire reason they are
    bound to an agent. Their liveness shows up as the agent's own status.

    Errors come back as ``{"ok": false, "error": ...}`` with a 200, not an exception —
    "I could not reach it" is the answer to the question, not a fault in the API.
    """
    try:
        conn = hcs.resolve(db, _kind_of(db, connection_id), connection_id)
    except hcs.HypervisorConnectionError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    if conn.agent_id:
        return {"ok": None, "error": "",
                "detail": "This connection is reached through a remote agent, so the "
                          "dashboard cannot dial it. Check the agent's status instead."}

    try:
        await _probe(conn)
    except Exception as exc:  # noqa: BLE001 — every SDK raises its own type
        message = str(exc)[:500]
        hcs.record_result(db, connection_id, error=message)
        logger.info("connection test failed for %s: %s", conn.name, exc)
        return {"ok": False, "error": message}
    hcs.record_result(db, connection_id)
    return {"ok": True, "error": ""}


def _kind_of(db: Session, connection_id: str) -> str:
    from ..database import HypervisorConnection
    row = db.query(HypervisorConnection.kind).filter(
        HypervisorConnection.id == connection_id).first()
    if row is None:
        raise hcs.HypervisorConnectionError("that connection no longer exists")
    return row[0]


async def _probe(conn) -> None:
    """One cheap read per kind, chosen to prove auth as well as reachability."""
    if conn.kind == "proxmox":
        from ..services import proxmox_service
        await proxmox_service.list_nodes(conn)
    elif conn.kind == "vsphere":
        from ..services import vsphere_service
        await vsphere_service.list_datacenters(conn)
    elif conn.kind == "nutanix":
        from ..services import nutanix_service
        await nutanix_service.list_clusters(conn)
    elif conn.kind == "xcpng":
        from ..services import xcpng_service
        await xcpng_service.list_vms(conn)
    elif conn.kind == "hyperv":
        from ..services import hyperv_service
        await hyperv_service.list_vms(conn)
    else:
        raise hcs.HypervisorConnectionError(f"no probe for kind {conn.kind!r}")
