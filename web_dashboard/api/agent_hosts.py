"""Remote agents the dashboard deploys into a cloud, and their lifecycle.

  GET    /api/agent-hosts              — every deployed agent, with its schedule
  GET    /api/agent-hosts/options      — clouds, default sizes and change windows for the form
  POST   /api/agent-hosts              — deploy one (and optionally route its ranges to it)
  PATCH  /api/agent-hosts/{id}         — lifecycle, size, ranges, runs at once
  POST   /api/agent-hosts/{id}/start   — bring its host up now
  POST   /api/agent-hosts/{id}/stop    — take its host down now, keeping the agent
  DELETE /api/agent-hosts/{id}         — take it down and delete the agent and its routes

Enqueue-only, like the Gateways API: handlers validate, write the row, and queue an
``agent_host_*`` job that ``services.agent_host_service`` runs on the job runner.

On its own prefix, not under ``/api/agent/``: the remote-agent vhost publishes that one to
the network the agents live on, and nothing here is for an agent
(tests/test_agent_vhost_surface.py checks the whole route table).
"""
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import RemoteAgent, User, get_db
from ..services import agent_host_service as ahs
from ..services import job_service, region_catalog
from ..services.agent_host_service import AgentHostError
from .auth import require_explicit_permission

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/agent-hosts", tags=["agent-hosts"])


class LifecycleFields(BaseModel):
    mode: str = "always"
    until_at: str = ""                # ISO 8601; no offset means UTC
    change_window_id: str = ""
    lead_minutes: int = 20
    grace_minutes: int = 15


class DeployRequest(LifecycleFields):
    name: str
    cloud: str
    region: str
    ranges: list[str] = []
    max_concurrent: int = 2
    subnet: str = ""
    instance_size: str = ""
    create_routes: bool = True


class PatchRequest(BaseModel):
    mode: Optional[str] = None
    until_at: Optional[str] = None
    change_window_id: Optional[str] = None
    lead_minutes: Optional[int] = None
    grace_minutes: Optional[int] = None
    ranges: Optional[list[str]] = None
    max_concurrent: Optional[int] = None
    instance_size: Optional[str] = None


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


def _parse_until(raw: str) -> Optional[datetime]:
    if not (raw or "").strip():
        return None
    try:
        dt = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=400,
                            detail=f"'{raw}' is not a date and time.") from None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _lifecycle(db: Session, body) -> dict:
    try:
        return ahs.validate_lifecycle(
            db, mode=body.mode, until_at=_parse_until(body.until_at or ""),
            change_window_id=body.change_window_id or "",
            lead_minutes=body.lead_minutes, grace_minutes=body.grace_minutes)
    except AgentHostError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


def _load(db: Session, host_id: str):
    host = ahs.get(db, host_id)
    if host is None:
        raise HTTPException(status_code=404, detail="No such deployed agent.")
    return host


def _row(db: Session, host) -> dict:
    from ..services import agent_service, change_window_service, config_mgmt_route_service
    agent = db.query(RemoteAgent).filter(RemoteAgent.id == host.agent_id).first()
    window = (change_window_service.get(db, host.change_window_id)
              if host.change_window_id else None)
    out = ahs.serialize(host, agent, window)
    out["agent_status"] = agent_service.status_of(agent) if agent is not None else "missing"
    out["routes"] = [r["cidr"] for r in config_mgmt_route_service.list_routes(db)
                     if r.get("agent_id") == host.agent_id]
    out["busy"] = ahs.busy(db, host)
    return out


def _schedule_now(db: Session, host) -> Optional[str]:
    """Record the next up/down times and queue whatever the schedule calls for now, so a
    window that is already open does not wait for the next sweep."""
    window = None
    if host.change_window_id:
        from ..services import change_window_service
        window = change_window_service.get(db, host.change_window_id)
    action, host.next_start_at, host.next_stop_at = ahs.plan(host, window, ahs._utcnow())
    db.commit()
    if action and not ahs.busy(db, host):
        return ahs.enqueue(db, host, action)
    return None


@router.get("")
def list_hosts(current_user: User = Depends(require_explicit_permission("agents", "read")),
               db: Session = Depends(get_db)):
    from ..database import AgentHost
    hosts = db.query(AgentHost).order_by(AgentHost.created_at.desc()).all()
    return {"hosts": [_row(db, h) for h in hosts]}


@router.get("/options")
def options(current_user: User = Depends(require_explicit_permission("agents", "read")),
            db: Session = Depends(get_db)):
    from ..services import change_window_service, config_service, dashboard_spire
    url_error = ahs.dashboard_url_problem()
    url = "" if url_error else ahs.dashboard_url()
    windows = [{"id": w.id, "name": w.name}
               for w in change_window_service.list_windows(db) if w.enabled is not False]
    return {
        "clouds": list(ahs.CLOUDS),
        "default_sizes": ahs.DEFAULT_SIZES,
        "max_concurrent_ceiling": ahs.MAX_CONCURRENT_CEILING,
        "windows": windows,
        "dashboard_url": url,
        "dashboard_url_error": url_error,
        "identity": ("spire" if config_service.get_bool("spire_attest_enabled")
                     and dashboard_spire.server_in_use() else "code"),
    }


@router.post("", status_code=201)
def deploy(body: DeployRequest, request: Request,
           current_user: User = Depends(require_explicit_permission("agents", "write")),
           db: Session = Depends(get_db)):
    from ..services import config_mgmt_route_service as cmr
    cloud = (body.cloud or "").strip().lower()
    if cloud in ahs.CLOUDS and not region_catalog.validate(cloud, body.region):
        raise HTTPException(status_code=400,
                            detail=f"Invalid {cloud} region '{body.region}'.")
    region = region_catalog.normalize(cloud, body.region) if cloud in ahs.CLOUDS else ""
    lifecycle = _lifecycle(db, body)
    try:
        host, agent = ahs.create(
            db, name=body.name, cloud=cloud, region=region, cidrs=body.ranges,
            max_concurrent=body.max_concurrent, subnet=body.subnet,
            instance_size=body.instance_size, lifecycle=lifecycle,
            created_by=current_user.username)
    except AgentHostError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None

    routes, route_errors = [], []
    if body.create_routes:
        # A range that is already routed in this agent's cloud and region is the one
        # refusal to expect, so it is found from the data and reported as such. Not
        # caught and stringified: CodeQL py/stack-trace-exposure fires on an exception's
        # text reaching the response. An on-premises route for the same range is no
        # clash: routes are scoped (config_mgmt_route_service).
        taken = {r["cidr"]: r for r in cmr.list_routes(db)
                 if r["scope"] == host.cloud and r["region"] == host.region.lower()}
        names = {a.id: a.name for a in db.query(RemoteAgent).all()}
        for cidr in ahs.cidrs_of(host):
            clash = taken.get(cidr)
            if clash is not None:
                route_errors.append(
                    f"{cidr} is already routed to agent "
                    f"'{names.get(clash['agent_id'], clash['agent_id'])}' for "
                    f"{cmr.scope_text(host.cloud, host.region)}; left as it is.")
                continue
            try:
                cmr.create(db, agent_id=agent.id, cidr=cidr, label=f"{host.name} (deployed)",
                           created_by=current_user.username, scope=host.cloud,
                           region=host.region)
                routes.append(cidr)
            except cmr.ConfigRouteError as exc:
                # A race with another operator saving the same range. The deploy stands.
                logger.warning("agent host %s: route %s not added: %s", host.name, cidr, exc)
                route_errors.append(f"{cidr} was not routed; see Config Routes.")

    job_id = ahs.enqueue(db, host, "start", created_by=current_user.username) \
        if host.mode in ("always", "until") else _schedule_now(db, host)
    job_service.log_audit(db, current_user.username, "agent_host.deploy",
                          ip_address=_client_ip(request),
                          details={"agent": host.name, "cloud": cloud, "region": region,
                                   "mode": host.mode, "ranges": ahs.cidrs_of(host),
                                   "routes": routes})
    return {**_row(db, ahs.get(db, host.id)), "job_id": job_id or "",
            "route_errors": route_errors}


@router.patch("/{host_id}")
def update(host_id: str, body: PatchRequest, request: Request,
           current_user: User = Depends(require_explicit_permission("agents", "write")),
           db: Session = Depends(get_db)):
    """Change the schedule now; ranges, size and runs at once take effect at the next
    start, because they are baked into the host when it is built."""
    host = _load(db, host_id)
    if ahs.busy(db, host):
        raise HTTPException(status_code=409,
                            detail="A start or stop is in progress. Wait for it to finish.")
    current = LifecycleFields(
        mode=host.mode, until_at=host.until_at.isoformat() if host.until_at else "",
        change_window_id=host.change_window_id or "", lead_minutes=host.lead_minutes,
        grace_minutes=host.grace_minutes)
    changes = body.model_dump(exclude_none=True)
    lifecycle_keys = set(LifecycleFields.model_fields)
    if lifecycle_keys & changes.keys():
        merged = current.model_copy(update={k: v for k, v in changes.items()
                                            if k in lifecycle_keys})
        for key, value in _lifecycle(db, merged).items():
            setattr(host, key, value)
        host.next_start_at = None          # re-planned below, dropping any manual hold
    try:
        if body.ranges is not None:
            import json
            host.ansible_cidrs = json.dumps(ahs.normalize_cidrs(body.ranges))
        if body.max_concurrent is not None:
            if not 1 <= body.max_concurrent <= ahs.MAX_CONCURRENT_CEILING:
                raise AgentHostError(
                    f"Runs at once must be 1 to {ahs.MAX_CONCURRENT_CEILING}.")
            host.max_concurrent = body.max_concurrent
        if body.instance_size is not None:
            host.instance_size = ahs.validate_size(host.cloud, body.instance_size)
    except AgentHostError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from None
    db.commit()
    job_id = _schedule_now(db, host) if host.mode == "window" else None
    job_service.log_audit(db, current_user.username, "agent_host.update",
                          ip_address=_client_ip(request),
                          details={"agent": host.name, "changed": sorted(changes)})
    return {**_row(db, host), "job_id": job_id or "",
            "applies_at_next_start": bool({"ranges", "max_concurrent", "instance_size"}
                                          & changes.keys()) and bool(host.host_id)}


def _act(db: Session, host_id: str, action: str, request: Request, user: User) -> dict:
    host = _load(db, host_id)
    if action == "start" and (host.host_id or host.status in ahs.LIVE_STATUSES):
        raise HTTPException(status_code=409, detail="This agent's host is already up.")
    if action == "stop" and not host.host_id and host.status not in ahs.LIVE_STATUSES \
            and host.status != "error":
        raise HTTPException(status_code=409, detail="This agent's host is not up.")
    try:
        job_id = ahs.enqueue(db, host, action, created_by=user.username)
    except AgentHostError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    if action == "stop":
        ahs.hold_until_next_window(db, host)
    job_service.log_audit(db, user.username, f"agent_host.{action}",
                          ip_address=_client_ip(request), details={"agent": host.name})
    return {**_row(db, host), "job_id": job_id}


@router.post("/{host_id}/start", status_code=202)
def start(host_id: str, request: Request,
          current_user: User = Depends(require_explicit_permission("agents", "write")),
          db: Session = Depends(get_db)):
    """Bring the host up now. A window-mode agent started by hand is stopped by the
    schedule once its current window (plus grace) is over."""
    return _act(db, host_id, "start", request, current_user)


@router.post("/{host_id}/stop", status_code=202)
def stop(host_id: str, request: Request,
         current_user: User = Depends(require_explicit_permission("agents", "write")),
         db: Session = Depends(get_db)):
    """Take the host down now. A window-mode agent stopped during its window stays down
    until the next occurrence."""
    return _act(db, host_id, "stop", request, current_user)


@router.delete("/{host_id}", status_code=202)
def delete(host_id: str, request: Request,
           current_user: User = Depends(require_explicit_permission("agents", "delete")),
           db: Session = Depends(get_db)):
    """Take the host down, then delete the agent, its Config-Management routes and this
    record. Runs booked onto the agent fall back to normal routing."""
    return _act(db, host_id, "delete", request, current_user)
