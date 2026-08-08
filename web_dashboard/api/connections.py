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
from ..services import hypervisor_connection_service as hcs
from ..services import job_service
from .auth import require_admin

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/connections", tags=["connections"])


class ConnectionRequest(BaseModel):
    """A connection to create.

    ``secret`` is write-only and never echoed back — :func:`hcs.serialize` has no field
    for it, only a ``has_secret`` boolean. An **agent-bound** connection (``agent_id``
    set) ignores ``host``/``username``/``secret`` entirely: the credential lives in that
    agent's own connections.yaml and ``agent_connection_name`` is the whole join.
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


@router.get("")
async def list_connections(kind: str = "",
                           db: Session = Depends(get_db),
                           current_user: User = Depends(require_admin)):
    """Every configured connection, optionally filtered to one kind.

    Also returns the enrolled agents, so the connection form can offer an agent
    picker instead of asking an operator to paste a uuid.
    """
    agents = [{"id": a.id, "name": a.name, "site": a.site or ""}
              for a in db.query(RemoteAgent).filter(
                  RemoteAgent.is_active.is_(True)).order_by(RemoteAgent.name).all()]
    return {"connections": hcs.list_connections(db, kind),
            "kinds": list(hcs.VALID_KINDS),
            "agents": agents}


@router.post("", status_code=201)
async def create_connection(req: ConnectionRequest,
                            db: Session = Depends(get_db),
                            current_user: User = Depends(require_admin)):
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
                            current_user: User = Depends(require_admin)):
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
                       current_user: User = Depends(require_admin)):
    try:
        return hcs.set_default(db, connection_id)
    except hcs.HypervisorConnectionError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete("/{connection_id}")
async def delete_connection(connection_id: str,
                            db: Session = Depends(get_db),
                            current_user: User = Depends(require_admin)):
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
                          current_user: User = Depends(require_admin)):
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
