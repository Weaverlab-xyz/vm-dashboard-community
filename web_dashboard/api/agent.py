"""Remote on-prem agent API.

Agent half — Ed25519-signed, no user identity involved:

  POST /api/agent/enroll                    — redeem a one-time enrolment code
  POST /api/agent/lease                     — claim the next job (also the heartbeat)
  POST /api/agent/jobs/{id}/heartbeat       — progress + the cancel signal
  POST /api/agent/jobs/{id}/logs            — Live Output lines
  POST /api/agent/jobs/{id}/complete        — terminal status + result

Operator half — admin only:

  POST   /api/agent                         — register an agent, mint an enrolment code
  GET    /api/agent                         — list, with derived status
  GET    /api/agent/{id}                    — one agent
  POST   /api/agent/{id}/enrollment-code    — re-issue for a reinstall
  POST   /api/agent/{id}/discover           — queue a discovery scan
  DELETE /api/agent/{id}                    — revoke

Two things about this module are load-bearing and easy to undo by accident.

**Route order.** Every agent-half route is declared before the operator half's
``/{agent_id}`` routes. FastAPI matches in declaration order, so moving them would make
``/api/agent/lease`` bind ``agent_id="lease"`` and 404 — or worse, 403 under the admin
dependency, which reads like a permissions bug rather than a routing one.

**No ``require_permission``.** Agent-half routes never touch it. That dependency treats
an empty permission dict as *unrestricted* (api/auth.py) for backward compatibility with
pre-OIDC users, which is exactly wrong for a machine principal. Agents authorize against
``agent_service.AGENT_JOB_TYPES`` and an ownership check on the job row; a static test
asserts this file never calls ``require_permission`` in an agent route.
"""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import Job, RemoteAgent, User, get_db
from ..services import (agent_guard, agent_job_meta, agent_service, agent_signing,
                        config_service, job_service, public_url)
from ..services.agent_guard import AgentThrottled
from ..services.agent_service import AgentError
from .auth import require_admin

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/agent", tags=["agent"])

_AUDIENCE_CONFIG = "agent_base_url"


# ── Audience ──────────────────────────────────────────────────────────────────

def _resolve_audience(request: Request, *, persist: bool = False) -> str:
    """The URL an agent signs against, pinned on first use.

    The audience binds a signature to *this* dashboard: a request captured by a rogue
    host an agent was misconfigured to trust cannot be replayed here, because the
    signature covers that host's URL and not ours.

    Resolved once and then persisted, rather than read from the live request every
    time. Deriving per-request would trust the ``Host`` header, and an attacker who can
    set that could make the audience match whatever they captured — which is the one
    way to unpick this check.

    ``public_base_url`` wins when set. Behind a reverse proxy the derived value is only
    ``https`` because ProxyHeadersMiddleware rewrote the scheme, so pinning from a
    request that arrived through an untrusted proxy would freeze an ``http://``
    audience into the config and make every agent signature fail to verify.

    **``persist`` is why this has a flag.** Pinning writes a value that every future
    signature is checked against, permanently, so *which request* is allowed to do the
    pinning matters. Only two callers may: the operator minting an enrolment code
    (authenticated admin) and a successful enrolment (a valid single-use code). The
    unauthenticated signature path must not, or the first stranger to reach
    ``/api/agent/lease`` with a forged ``Host`` — trivial against the plain-HTTP 8001 the
    base stack still publishes — pins the audience to a value of their choosing and 401s
    every real agent until an operator clears the config key by hand. Worse, an audience
    pinned to a host the attacker controls is the one thing that would let a signature
    captured there be replayed here, which is precisely what the audience exists to stop.

    When nothing is pinned yet, this still *returns* a derived value so verification has
    something to compare against; it simply does not write it down.
    """
    pinned = config_service.get(_AUDIENCE_CONFIG)
    if pinned:
        return pinned.rstrip("/")
    resolved = public_url.resolve(request)
    if not persist:
        return resolved
    config_service.set(_AUDIENCE_CONFIG, resolved)
    logger.info("agent: pinned the signing audience to %s", resolved)
    return resolved


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


def _throttled(exc: AgentThrottled) -> HTTPException:
    """A 429 carrying ``Retry-After``.

    The header is the whole point — without it a backing-off agent has to guess, and a
    fleet that all guessed the same interval would come back in lockstep and trip the cap
    again. ``scope`` stays in the log and out of the response: telling a caller whether
    it hit a per-agent or a global cap tells them about traffic that is not theirs.
    """
    return HTTPException(
        status_code=429,
        detail=f"Too many requests. Retry in {exc.retry_after}s.",
        headers={"Retry-After": str(exc.retry_after)},
    )


# ══════════════════════════════════════════════════════════════════════════════
# Agent half — declared FIRST (see module docstring)
# ══════════════════════════════════════════════════════════════════════════════

class EnrollRequest(BaseModel):
    enrollment_code: str
    public_key: str
    agent_version: str = ""
    policy_hash: str = ""


@router.post("/enroll")
async def enroll_agent(body: EnrollRequest, request: Request,
                       db: Session = Depends(get_db)):
    """Redeem a one-time enrolment code and bind the agent's public key.

    The only agent route authenticated by something other than a signature — there is
    no key on file yet, which is what enrolment is for.
    """
    try:
        agent = agent_service.enroll(
            db, code=body.enrollment_code, public_key=body.public_key,
            agent_version=body.agent_version, policy_hash=body.policy_hash,
            ip=_client_ip(request))
    except AgentThrottled as exc:
        raise _throttled(exc)
    except AgentError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    agent_service.audit(db, agent, "agent.enroll", ip=_client_ip(request),
                        details={"agent_version": agent.agent_version,
                                 "policy_hash": agent.policy_hash})
    return {
        "agent_id": agent.id,
        "name": agent.name,
        "site": agent.site or "",
        # One of only two callers allowed to pin — see _resolve_audience. A valid
        # single-use code is what makes this request trustworthy enough to do it.
        "audience": _resolve_audience(request, persist=True),
        # Pinned by the agent and used to verify every job envelope from here on.
        "dashboard_public_key": agent_service.envelope_public_key(),
        "poll_interval_s": agent_service.DEFAULT_POLL_INTERVAL_S,
        "heartbeat_interval_s": agent_service.DEFAULT_HEARTBEAT_INTERVAL_S,
        "max_log_lines_per_request": agent_service.MAX_LOG_LINES_PER_REQUEST,
    }


async def signed_agent(request: Request, db: Session = Depends(get_db)) -> RemoteAgent:
    """Dependency: authenticate a signed agent request.

    Reads the raw body so the signature is checked against the exact bytes that
    arrived — verifying a re-serialised body is the classic signing bug, and Starlette
    caches ``body()`` so the endpoint can read it again without a second await cost.

    Returns a ``RemoteAgent``, never a ``User``. That type difference is the guarantee:
    nothing downstream can accidentally hand an agent to a dependency that assumes user
    semantics.
    """
    headers = request.headers
    agent_id = headers.get(agent_signing.HEADER_AGENT_ID, "")
    timestamp = headers.get(agent_signing.HEADER_TIMESTAMP, "")
    nonce = headers.get(agent_signing.HEADER_NONCE, "")
    signature = headers.get(agent_signing.HEADER_SIGNATURE, "")
    if not (agent_id and timestamp and nonce and signature):
        raise HTTPException(status_code=401, detail="Request is not signed.")

    try:
        return agent_service.authenticate(
            db, agent_id=agent_id, timestamp=timestamp, nonce=nonce,
            signature=signature, audience=_resolve_audience(request),
            method=request.method, path=request.url.path,
            body=await request.body(), ip=_client_ip(request))
    except AgentThrottled as exc:
        # 429, never 401: the agent reads a 401 as "I was revoked" and stops for good
        # (see `lease` in runners/agent/agent.py), where a 429 makes it back off and
        # come back. Getting this wrong would turn a load spike into a dead fleet.
        raise _throttled(exc)
    except AgentError as exc:
        # One status and one shape for every failure. Distinguishing "unknown agent"
        # from "bad signature" from "replay" would tell a prober which part they got
        # right, and the agent's response to all three is the same: back off.
        logger.info("agent auth refused for %s: %s", agent_id, exc)
        raise HTTPException(status_code=401, detail="Authentication failed.")


@router.post("/lease")
async def lease_job(request: Request, agent: RemoteAgent = Depends(signed_agent),
                    db: Session = Depends(get_db)):
    """Claim the next queued job for this agent, or report an empty queue.

    Also the heartbeat: ``authenticate`` bumped ``last_seen_at`` on the way in, so an
    idle agent stays visibly online without a second endpoint to authorize.

    Returns 200 with ``{"job": null}`` rather than 204 when idle, so the agent parses
    one response shape either way.
    """
    # Opportunistic, cheap, and keeps the replay table bounded without a second timer
    # process. Nonces older than the signature window cannot be replayed anyway.
    try:
        agent_service.sweep_nonces(db)
    except Exception:  # noqa: BLE001 — housekeeping must never fail a lease
        logger.debug("agent: nonce sweep failed", exc_info=True)

    claim = agent_service.lease_one(db, agent)
    if claim is None:
        return {"job": None, "poll_interval_s": agent_service.DEFAULT_POLL_INTERVAL_S}

    envelope = {
        "job_id": claim["id"],
        "job_type": claim["job_type"],
        "agent_id": agent.id,
        "audience": _resolve_audience(request),
        "payload": agent_job_meta.discover_kwargs(claim["meta"]),
    }
    agent_service.audit(db, agent, "agent.lease", ip=_client_ip(request),
                        details={"job_id": claim["id"], "job_type": claim["job_type"]})
    return {
        "job": envelope,
        # Provenance. An attacker who can INSERT a job row still cannot make the agent
        # run it, because they cannot produce this signature.
        "signature": agent_signing.sign_envelope(
            agent_service.envelope_signing_key(), envelope),
        "heartbeat_interval_s": agent_service.DEFAULT_HEARTBEAT_INTERVAL_S,
    }


class HeartbeatRequest(BaseModel):
    progress_pct: int = 0
    message: str = ""


@router.post("/jobs/{job_id}/heartbeat")
async def heartbeat(job_id: str, body: HeartbeatRequest,
                    agent: RemoteAgent = Depends(signed_agent),
                    db: Session = Depends(get_db)):
    """Report progress and collect the cancel signal.

    Progress and heartbeat are one write because ``update_progress`` already bumps
    ``updated_at``, which is the liveness value ``reconcile_stale_jobs`` reads.

    ``cancel_requested`` is what makes the existing Cancel button on /jobs work against
    a remote agent with no frontend change at all: the operator's DELETE flips the row
    to ``cancelled``, and the agent notices on its next beat.
    """
    job = _owned(db, agent, job_id)
    pct = max(0, min(100, int(body.progress_pct or 0)))
    job_service.update_progress(db, job.id, pct, (body.message or "")[:2000])
    return {"cancel_requested": job_service.is_cancelled(db, job.id)}


class LogsRequest(BaseModel):
    lines: list = []


@router.post("/jobs/{job_id}/logs")
async def push_logs(job_id: str, body: LogsRequest,
                    agent: RemoteAgent = Depends(signed_agent),
                    db: Session = Depends(get_db)):
    """Append Live Output. Lands in ``job_logs``, which is what the existing
    WebSocket endpoint polls — so an agent's output appears in the Live Output pane
    with no frontend work."""
    job = _owned(db, agent, job_id)
    written = agent_service.append_logs(db, job, body.lines or [])
    return {"written": written}


class CompleteRequest(BaseModel):
    status: str                       # "completed" | "failed"
    result: Optional[dict] = None
    error: str = ""


@router.post("/jobs/{job_id}/complete")
async def complete(job_id: str, body: CompleteRequest, request: Request,
                   agent: RemoteAgent = Depends(signed_agent),
                   db: Session = Depends(get_db)):
    """Finish a leased job."""
    job = _owned(db, agent, job_id)
    if body.status not in ("completed", "failed"):
        raise HTTPException(status_code=400, detail="status must be 'completed' or 'failed'.")

    result = body.result or {}
    if job.job_type == "agent_discover" and body.status == "completed":
        # Cross-check findings against the dashboard's own inventory HERE rather than
        # sending the agent the inventory to compare against. An agent should never
        # learn what else the dashboard manages.
        result = _annotate_findings(db, result)

    applied = agent_service.complete_job(db, agent, job, status=body.status,
                                         result=result, error=body.error)
    agent_service.audit(db, agent, "agent.complete", ip=_client_ip(request),
                        details={"job_id": job.id, "status": applied})
    return {"job_id": job.id, "status": applied}


def _owned(db: Session, agent: RemoteAgent, job_id: str, *,
           statuses: tuple = agent_service.WINDING_DOWN) -> Job:
    try:
        return agent_service.owned_job(db, agent, job_id, statuses=statuses)
    except AgentError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


def _annotate_findings(db: Session, result: dict) -> dict:
    """Mark each finding that the dashboard already knows about.

    Registration is never automatic — ``register_cluster`` needs a full kubeconfig and
    ``register_database`` needs a Password Safe managed account, and an agent able to
    supply either would have to hold cluster-admin or a privileged credential. So this
    only annotates; a human clicks Register, and the existing endpoints run under that
    human's own permissions.
    """
    from ..database import CloudDatabase, K8sCluster

    findings = result.get("findings")
    if not isinstance(findings, list):
        return result

    api_servers = {(row[0] or "").strip().rstrip("/")
                   for row in db.query(K8sCluster.api_server).all()}
    databases = {((row[0] or "").strip().lower(), (row[1] or "").strip().lower())
                 for row in db.query(CloudDatabase.private_host, CloudDatabase.engine).all()}

    for finding in findings:
        if not isinstance(finding, dict):
            continue
        if finding.get("kind") == "k8s":
            known = (finding.get("api_server") or "").strip().rstrip("/") in api_servers
        else:
            known = ((finding.get("host") or "").strip().lower(),
                     (finding.get("engine") or "").strip().lower()) in databases
        finding["already_registered"] = known
    return result


# ══════════════════════════════════════════════════════════════════════════════
# Operator half — admin only, declared AFTER the agent routes
# ══════════════════════════════════════════════════════════════════════════════

class CreateAgentRequest(BaseModel):
    name: str
    site: str = ""
    description: str = ""


def _agent_row(agent: RemoteAgent, running: int = 0) -> dict:
    return {
        "id": agent.id,
        "name": agent.name,
        "site": agent.site or "",
        "description": agent.description or "",
        "status": agent_service.status_of(agent),
        "agent_version": agent.agent_version or "",
        "policy_hash": agent.policy_hash or "",
        "enrolled_at": agent.enrolled_at,
        "last_seen_at": agent.last_seen_at,
        "last_seen_ip": agent.last_seen_ip or "",
        "enroll_expires_at": agent.enroll_expires_at,
        "created_at": agent.created_at,
        "created_by": agent.created_by or "",
        "running_jobs": running,
    }


# The hardening flags and mounts both install forms share. `$PWD` rather than `./`
# because `docker run -v` requires an absolute source path — a relative one is rejected
# outright, so the command as previously emitted could not run at all. Quoted so a
# directory with a space in it survives.
#
# `,Z` on the bind mounts is the SELinux relabel, and it is not optional on Fedora, RHEL,
# CentOS, Rocky or Alma — that is a large share of on-prem Linux, which is exactly where
# an agent runs. A file in the operator's home directory is labelled `user_home_t`, which
# `container_t` may not read, so without it the agent dies on its own policy file
# (`Permission denied` on a mode 644 file) *before* making any network call: the row sits
# at `enrolling` with no source IP and nothing at all reaches the dashboard, so every
# visible symptom points at DNS or TLS. Docker and Podman ignore `z`/`Z` on hosts without
# SELinux, so this is safe everywhere and needs no host detection.
_RUN_FLAGS = (
    "docker run -d --name dashboard-agent --restart unless-stopped \\\n"
    "  --read-only --cap-drop ALL --security-opt no-new-privileges:true \\\n"
    "  --user 10001:10001 --tmpfs /tmp \\\n"
    "  -v dashboard_agent_state:/var/lib/dashboard-agent \\\n"
    '  -v "$PWD/policy.yaml:/etc/dashboard-agent/policy.yaml:ro,Z" \\\n'
)


def _install_hint(request: Request, code: str) -> dict:
    """Copy-paste install commands built from this dashboard's own URL, so the operator
    never has to type it and cannot typo the one value that must match the signing
    audience.

    Two forms, because where the enrolment code goes is a real trade-off:

    ``docker_run`` passes it as an environment variable. Convenient, and the code is
    single-use with a 15-minute TTL — but an env var is baked into the container's
    config, so it stays readable via ``docker inspect`` for the life of the container,
    long after it is worthless.

    ``docker_run_code_file`` mounts it as a file instead, so nothing durable in Docker's
    metadata holds the value. The file itself is world-readable on purpose: the container
    runs as uid 10001 and a bind-mounted 0600 file owned by the host user would simply be
    unreadable inside it. That is an acceptable trade for a single-use 15-minute secret in
    a directory the operator chose, and the trailing ``rm`` closes it. An operator who
    wants the code in neither shell history nor Docker metadata can write that file with
    an editor and skip the first line entirely. It carries ``,Z`` for the same reason the
    policy mount does: mode bits are only half of "readable" on an SELinux host.
    """
    base = _resolve_audience(request, persist=True)
    return {
        "docker_run": (
            _RUN_FLAGS
            + f"  -e DASHBOARD_URL={base} \\\n"
            + f"  -e AGENT_ENROLLMENT_CODE={code} \\\n"
            + "  chrweav/dashboard-agent:latest"
        ),
        "docker_run_code_file": (
            f"umask 022 && printf '%s' '{code}' > ./agent-enroll-code\n\n"
            + _RUN_FLAGS
            + '  -v "$PWD/agent-enroll-code:/etc/dashboard-agent/enroll-code:ro,Z" \\\n'
            + f"  -e DASHBOARD_URL={base} \\\n"
            + "  -e AGENT_ENROLLMENT_CODE_FILE=/etc/dashboard-agent/enroll-code \\\n"
            + "  chrweav/dashboard-agent:latest\n\n"
            + "# Once the Agents page shows this agent Online, the code is spent:\n"
            + "rm ./agent-enroll-code"
        ),
        "dashboard_url": base,
    }


@router.post("", status_code=201)
async def create_agent(body: CreateAgentRequest, request: Request,
                       current_user: User = Depends(require_admin),
                       db: Session = Depends(get_db)):
    """Register an agent and return its one-time enrolment code.

    The code is shown exactly once, like a PAT — only its hash is stored.
    """
    try:
        agent, code = agent_service.create_agent(
            db, name=body.name, site=body.site, description=body.description,
            created_by=current_user.username)
    except AgentError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    job_service.log_audit(db, current_user.username, "agent.create",
                          ip_address=_client_ip(request),
                          details={"agent": agent.name, "site": agent.site or ""})
    return {**_agent_row(agent), "enrollment_code": code,
            "install": _install_hint(request, code)}


@router.get("")
async def list_agents(current_user: User = Depends(require_admin),
                      db: Session = Depends(get_db)):
    """Every registered agent, with derived status and its running-job count."""
    agents = db.query(RemoteAgent).order_by(RemoteAgent.created_at.desc()).all()
    counts = {}
    for agent_id, in db.query(Job.agent_id).filter(
            Job.agent_id.isnot(None), Job.status == "running").all():
        counts[agent_id] = counts.get(agent_id, 0) + 1
    return {"agents": [_agent_row(a, counts.get(a.id, 0)) for a in agents]}


def _load(db: Session, agent_id: str) -> RemoteAgent:
    agent = db.query(RemoteAgent).filter(RemoteAgent.id == agent_id).first()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    return agent


@router.get("/{agent_id}")
async def get_agent(agent_id: str, current_user: User = Depends(require_admin),
                    db: Session = Depends(get_db)):
    return _agent_row(_load(db, agent_id))


@router.post("/{agent_id}/enrollment-code")
async def reissue_code(agent_id: str, request: Request,
                       current_user: User = Depends(require_admin),
                       db: Session = Depends(get_db)):
    """Issue a fresh enrolment code for a reinstall.

    Clears the stored public key, so the container being replaced stops being able to
    lease work the moment the code is issued rather than whenever it is shut down.
    """
    agent = _load(db, agent_id)
    if not agent.is_active:
        raise HTTPException(status_code=409, detail="This agent has been revoked.")
    code = agent_service.reissue_enroll_code(db, agent)
    job_service.log_audit(db, current_user.username, "agent.reissue_code",
                          ip_address=_client_ip(request), details={"agent": agent.name})
    return {**_agent_row(agent), "enrollment_code": code,
            "install": _install_hint(request, code)}


class DiscoverRequest(BaseModel):
    scan_kind: str = "both"
    cidrs: list = []
    hostnames: list = []
    ports: Optional[dict] = None
    use_local_kubeconfig: bool = True
    timeout_s: int = 3
    max_hosts: int = 1024
    concurrency: int = 32


@router.post("/{agent_id}/discover", status_code=202)
async def queue_discovery(agent_id: str, body: DiscoverRequest, request: Request,
                          current_user: User = Depends(require_admin),
                          db: Session = Depends(get_db)):
    """Queue a discovery scan on one agent.

    Enqueue-only, like every other long operation. ``create_job(agent_id=…)`` forces
    ``status='queued'``, which is what keeps the local job runner from claiming a row
    destined for a network it cannot reach.
    """
    agent = _load(db, agent_id)
    if not agent.is_active:
        raise HTTPException(status_code=409, detail="This agent has been revoked.")
    if not agent.public_key:
        raise HTTPException(status_code=409,
                            detail="This agent has not completed enrolment yet.")
    if agent_service.status_of(agent) != "online":
        # Not fatal — the row would simply wait — but silently queueing work for a dead
        # agent is a worse experience than saying so.
        raise HTTPException(
            status_code=409,
            detail=f"Agent '{agent.name}' is not online. Start it and try again.")

    # `ports=None` needs no special case: normalize() falls back to the defaults for
    # anything that is not a dict, and clamps whatever is.
    meta = agent_job_meta.discover_meta(
        body, description=f"Discovery scan via agent '{agent.name}'")

    job = job_service.create_job(
        db, job_type="agent_discover", created_by=current_user.username,
        metadata=meta, agent_id=agent.id)
    job_service.log_audit(db, current_user.username, "agent.discover",
                          ip_address=_client_ip(request),
                          details={"agent": agent.name, "job_id": job.id,
                                   "scan_kind": meta.get("scan_kind")})
    return {"job_id": job.id, "status": job.status, "agent": agent.name}


@router.delete("/{agent_id}")
async def revoke(agent_id: str, request: Request,
                 current_user: User = Depends(require_admin),
                 db: Session = Depends(get_db)):
    """Revoke an agent and clear any work it was holding."""
    agent = _load(db, agent_id)
    affected = agent_service.revoke_agent(db, agent)
    job_service.log_audit(db, current_user.username, "agent.revoke",
                          ip_address=_client_ip(request),
                          details={"agent": agent.name, "jobs_cleared": affected})
    return {"detail": f"Agent '{agent.name}' revoked.", "jobs_cleared": affected}


@router.delete("/{agent_id}/record")
async def remove_record(agent_id: str, request: Request,
                        current_user: User = Depends(require_admin),
                        db: Session = Depends(get_db)):
    """Permanently delete a **revoked** agent's row.

    Revocation deliberately keeps the row: it is the evidence that the agent existed and
    what happened to it. But `create_agent` enforces name uniqueness across *all* rows,
    so a revoked agent squats its name forever — you can never register `lab-dc1` again —
    and the list grows without bound. This is the tidy-up.

    **Refuses unless the agent is already revoked**, so removal cannot be the first
    action taken on a live agent. That ordering is the point: revoke is what stops the
    container working and settles its in-flight jobs, and collapsing the two into one
    click would let an operator delete the row out from under a running job, leaving it
    to time out ten minutes later with no indication why.

    History survives. `Job.agent_id` is ``ondelete="SET NULL"``, so the agent's jobs keep
    their logs and results and simply stop naming an agent; audit entries record the
    agent by name, not by id, so they are unaffected.
    """
    agent = _load(db, agent_id)
    if agent.is_active:
        raise HTTPException(
            status_code=409,
            detail=f"Revoke '{agent.name}' before removing its record.")

    name = agent.name
    orphaned = agent_service.delete_agent(db, agent)
    job_service.log_audit(db, current_user.username, "agent.remove",
                          ip_address=_client_ip(request),
                          details={"agent": name, "jobs_orphaned": orphaned})
    return {"detail": f"Removed the record for '{name}'. The name is free to reuse.",
            "jobs_orphaned": orphaned}
