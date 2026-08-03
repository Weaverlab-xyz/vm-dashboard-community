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
  GET    /api/agent/audience                — the pinned signing audience, read-only
  DELETE /api/agent/audience                — clear the pin so the next mint re-pins it
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
    pinned = _pinned_audience()
    if pinned:
        return pinned
    resolved = public_url.resolve(request)
    if not persist:
        return resolved
    config_service.set(_AUDIENCE_CONFIG, resolved)
    logger.info("agent: pinned the signing audience to %s", resolved)
    return resolved


def _pinned_audience() -> str:
    """The stored audience, or "" when nothing has been pinned yet."""
    return (config_service.get(_AUDIENCE_CONFIG) or "").rstrip("/")


def _audience_state(request: Request) -> dict:
    """What the audience is, what a mint would make it, and whether that looks wrong.

    One function behind three surfaces — the read-only display in Settings, the warnings
    on a minted enrolment code, and the refusal that blocks a mint outright — so the panel
    cannot report one audience while the install command emits another.

    The failure this exists to make visible: on a **split-vhost** deployment (UI on one
    hostname, ``/api/agent*`` on another) the origin an admin's browser is on is the *UI*
    hostname. Minting the first enrolment code from there pins the audience to it
    permanently, the agent dials the UI vhost, and an IP-gated or UI-only vhost answers
    404. From the console that is indistinguishable from a revoked agent: stuck at
    ``enrolling``, ``Last seen: never``, no policy hash — and no ``/api/agent/enroll``
    request in the application log at all, because none ever arrived.
    """
    pinned = _pinned_audience()
    stated = public_url.configured()        # public_base_url, normalised, or ""
    origin = public_url.derive(request)     # the caller's origin, post-ProxyHeaders
    # Must agree with _resolve_audience(persist=True): pin wins, else the stated origin,
    # else the request's. If these ever diverge the warning describes a different URL than
    # the one the operator is handed.
    effective = pinned or stated or origin

    conflict = ""
    warnings: list = []

    if pinned and stated and pinned != stated:
        conflict = (
            f"The signing audience is pinned to {pinned}, but Public base URL now says "
            f"{stated}. Pinning is write-once, so an install command issued now would "
            f"still tell the new agent to dial {pinned} — silently ignoring the value you "
            f"corrected. Either set Public base URL back to {pinned}, or reset the signing "
            f"audience under Settings → Integrations → Remote Agents, which re-pins it "
            f"from {stated} on the next code and requires re-enrolling every agent.")
    elif not pinned and not stated:
        warnings.append(
            f"Nothing is pinned yet, so this code permanently pins the signing audience to "
            f"{origin} — the origin your own browser is on. If agents reach this dashboard "
            f"on a different hostname, set Public base URL first and issue a new code: an "
            f"agent that dials a hostname not serving /api/agent gets a 404 and sits at "
            f"'enrolling' with Last seen: never, which looks exactly like a revoked agent.")
    elif pinned and not stated and pinned == origin:
        warnings.append(
            f"The signing audience is pinned to {pinned}, the same origin you are browsing. "
            f"That is right for a single-hostname install. If the UI and /api/agent are on "
            f"separate hostnames it means the audience was pinned to the UI one, and no "
            f"agent will ever enrol against it. Set Public base URL to the hostname that "
            f"serves /api/agent and reset the audience.")

    if effective.startswith("http://"):
        warnings.append(
            f"The audience is {effective}. The agent refuses to sign over plaintext unless "
            f"AGENT_INSECURE_TLS=1, and a proxy whose headers this dashboard does not trust "
            f"is the usual reason an https deployment pins an http audience. Terminate TLS "
            f"in front of the dashboard and set Public base URL.")

    return {
        "pinned": pinned,
        "public_base_url": stated,
        "request_origin": origin,
        "effective": effective,
        "conflict": conflict,
        "warnings": warnings,
    }


def _audience_guard(request: Request, *, acknowledged: bool = False) -> dict:
    """``_audience_state``, refusing the mint when the pin contradicts ``public_base_url``.

    A 409 rather than a stale URL emitted with a warning beside it, because the operator who
    has just corrected Public base URL has every reason to believe the command they are
    copying reflects it. Callers must invoke this **before** they mutate anything: refusing
    after ``create_agent`` would leave a row squatting its name holding a code nobody saw,
    and after ``reissue_enroll_code`` would clear a working agent's key and hand back an
    error instead of a replacement code.

    ``acknowledged`` exists because the mismatch is not always a mistake. ``public_base_url``
    is one value doing two jobs — the OAuth callback origin *and* the agent audience — and on
    a split-vhost install those legitimately want different hostnames, so a divergence can be
    the correct steady state. Without an override this guard would block agent registration
    on that deployment permanently, with no escape but re-pinning the audience to the wrong
    value. The refusal is about not emitting a stale URL *silently*; an admin who has read the
    explanation and asked again is not silence. It is logged and audited as the exception it
    is, and the resulting install command carries a warning saying which URL it used.
    """
    state = _audience_state(request)
    if state["conflict"] and acknowledged:
        logger.warning(
            "agent: minting against the pinned audience %s despite public_base_url=%s "
            "(acknowledged by an admin)", state["pinned"], state["public_base_url"])
        state = {**state, "warnings": [
            f"Issued against the pinned audience {state['pinned']}, NOT the {state['public_base_url']} "
            f"in Public base URL — you acknowledged the mismatch. The agent must reach this "
            f"dashboard at {state['pinned']} or it will never enrol.",
            *state["warnings"]]}
        return state
    if state["conflict"]:
        # A structured detail, like the Entitle user-JIT refusals: the remedy is a paragraph
        # naming two hostnames and a settings panel, and `code` is what lets the Agents page
        # render it as something that stays on screen rather than a toast that times out
        # before it has been read. app.js maps `message` onto Error.message, so the plain
        # string path still works for any caller that only reads that.
        raise HTTPException(status_code=409, detail={
            "code": "agent_audience_conflict",
            "message": state["conflict"],
            "pinned": state["pinned"],
            "public_base_url": state["public_base_url"],
            # The override, named in the response so the caller does not have to read the
            # source to discover that the refusal is not a dead end.
            "override": "acknowledge_audience=true",
        })
    return state


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


def _install_hint(request: Request, code: str, audience: dict) -> dict:
    """Copy-paste install commands built from this dashboard's own URL, so the operator
    never has to type it and cannot typo the one value that must match the signing
    audience.

    ``audience`` comes from ``_audience_guard`` and must be computed *before* this runs:
    the pin is written here, so a state read afterwards can no longer tell that this mint
    is what created it — and "about to pin permanently" is the warning that matters most.

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
        # Rendered beside the command in the enrolment modal. Advisory, not fatal — a
        # single-hostname install trips the first of these legitimately.
        "warnings": audience["warnings"],
    }


@router.post("", status_code=201)
async def create_agent(body: CreateAgentRequest, request: Request,
                       acknowledge_audience: bool = False,
                       current_user: User = Depends(require_admin),
                       db: Session = Depends(get_db)):
    """Register an agent and return its one-time enrolment code.

    The code is shown exactly once, like a PAT — only its hash is stored.
    """
    # First, before the row exists: a refusal after create_agent would leave an agent
    # squatting its name holding a code the operator never saw.
    audience = _audience_guard(request, acknowledged=acknowledge_audience)
    try:
        agent, code = agent_service.create_agent(
            db, name=body.name, site=body.site, description=body.description,
            created_by=current_user.username)
    except AgentError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    job_service.log_audit(db, current_user.username, "agent.create",
                          ip_address=_client_ip(request),
                          details={"agent": agent.name, "site": agent.site or "",
                                   "audience": audience["effective"],
                                   "audience_mismatch_acknowledged": bool(audience["conflict"])})
    return {**_agent_row(agent), "enrollment_code": code,
            "install": _install_hint(request, code, audience)}


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


# ── The signing audience ──────────────────────────────────────────────────────
# Declared before the ``/{agent_id}`` routes below. ``audience`` is a single path segment,
# so FastAPI would otherwise bind ``agent_id="audience"`` and answer 404 — see the module
# docstring on route order.

def _enrolled_count(db: Session) -> int:
    """Live agents holding a key — exactly the set a reset would break."""
    return db.query(RemoteAgent).filter(
        RemoteAgent.is_active.is_(True),
        RemoteAgent.public_key.isnot(None),
        RemoteAgent.public_key != "").count()


@router.get("/audience")
async def read_audience(request: Request, current_user: User = Depends(require_admin),
                        db: Session = Depends(get_db)):
    """The pinned signing audience and why it might be wrong. **Read-only.**

    This endpoint exists because the pin had no reader at all: ``agent_base_url`` appeared
    in exactly one file, nothing displayed it, and its value is invisible in the one
    deployment shape where it goes wrong. Correcting Public base URL afterwards looks like
    it worked and changes nothing, because the pin wins — recovering from it meant a shell
    inside the container calling ``config_service.set`` by hand.

    Read-only is the whole design. Anything that accepted an audience from a caller would
    be the write-once rule with extra steps; see ``_resolve_audience`` for why that rule is
    load-bearing. The only mutation offered is a full reset.
    """
    return {**_audience_state(request), "agents_enrolled": _enrolled_count(db)}


@router.delete("/audience")
async def reset_audience(request: Request, current_user: User = Depends(require_admin),
                         db: Session = Depends(get_db)):
    """Clear the pin so the next minted code pins the audience again.

    The recovery ``docs/remote-agents.md`` has always told operators to perform — "clear
    the stale ``agent_base_url`` config key and re-enrol" — and never gave them a way to.

    **Deliberately not a way to set the audience.** It takes no value: it deletes the key
    and leaves the existing write-once path to re-pin from ``public_base_url`` on the next
    admin mint. That distinction is the security property. Write-once pinning is what stops
    the first stranger to reach ``/api/agent/lease`` with a forged ``Host`` from choosing an
    audience they control, which is the one thing that would make a signature captured
    against a host of theirs replayable here. Guarded by ``require_admin``, the same check
    that guards minting a code, so the unauthenticated signature path cannot reach it.

    **Invalidates every enrolled agent.** Each one signs against the audience it was handed
    at enrolment, so all of them start failing authentication — as a 401, which the agent
    reads as "revoked" and stops on. Every agent needs a fresh enrolment code afterwards.
    """
    previous = _pinned_audience()
    affected = _enrolled_count(db)
    if previous:
        config_service.delete(_AUDIENCE_CONFIG)
        logger.warning(
            "agent: %s reset the signing audience pin (was %s); %d enrolled agent(s) "
            "must re-enrol", current_user.username, previous, affected)
    job_service.log_audit(db, current_user.username, "agent.audience_reset",
                          ip_address=_client_ip(request),
                          details={"previous": previous, "agents_affected": affected})

    if not previous:
        detail = "No signing audience was pinned, so there was nothing to reset."
    else:
        detail = (
            f"Signing audience reset — it was pinned to {previous}. The next enrolment code "
            f"pins it again, so set Public base URL first if agents reach this dashboard on "
            f"a different hostname than the one you are browsing.")
        if affected:
            detail += (f" {affected} enrolled agent(s) will now fail authentication and "
                       f"must be re-enrolled with a fresh code.")
    # State recomputed after the delete, so the panel redraws from what is true now.
    return {"detail": detail, "previous": previous, "agents_affected": affected,
            **_audience_state(request), "agents_enrolled": affected}


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
                       acknowledge_audience: bool = False,
                       current_user: User = Depends(require_admin),
                       db: Session = Depends(get_db)):
    """Issue a fresh enrolment code for a reinstall.

    Clears the stored public key, so the container being replaced stops being able to
    lease work the moment the code is issued rather than whenever it is shut down.
    """
    agent = _load(db, agent_id)
    if not agent.is_active:
        raise HTTPException(status_code=409, detail="This agent has been revoked.")
    # Before reissue_enroll_code, which clears the public key. Refusing after it would lock
    # out the container being replaced and hand back an error rather than its new code.
    audience = _audience_guard(request, acknowledged=acknowledge_audience)
    code = agent_service.reissue_enroll_code(db, agent)
    job_service.log_audit(db, current_user.username, "agent.reissue_code",
                          ip_address=_client_ip(request),
                          details={"agent": agent.name, "audience": audience["effective"],
                                   "audience_mismatch_acknowledged": bool(audience["conflict"])})
    return {**_agent_row(agent), "enrollment_code": code,
            "install": _install_hint(request, code, audience)}


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
