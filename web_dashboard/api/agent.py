"""Remote on-prem agent API.

Agent half — Ed25519-signed, no user identity involved:

  POST /api/agent/enroll                    — redeem a one-time enrolment code
  POST /api/agent/lease                     — claim the next job (also the heartbeat)
  POST /api/agent/jobs/{id}/heartbeat       — progress + the cancel signal
  POST /api/agent/jobs/{id}/logs            — Live Output lines
  POST /api/agent/jobs/{id}/complete        — terminal status + result
  POST /api/agent/jobs/{id}/secret          — a hypervisor credential, sealed
  POST /api/agent/jobs/{id}/ansible-bundle  — a Config-Management run bundle, sealed

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
import base64
import json
import logging
import re
from typing import NamedTuple, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import Job, RemoteAgent, User, get_db
from ..services import (agent_ansible_bundle, agent_ansible_meta, agent_gateway_meta,
                        agent_guard, agent_hypervisor_meta, agent_job_meta,
                        agent_ps_credential_service, agent_sealing, agent_service,
                        agent_signing, config_service, hypervisor_connection_service,
                        hypervisor_sync_service, job_service, public_url)
from ..services.hypervisor_connection_service import HypervisorConnectionError
from ..services.agent_guard import AgentThrottled
from ..services.agent_service import AgentError
from .auth import require_admin

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/agent", tags=["agent"])

# Defined in agent_service so pov_broker can read the same key without importing a
# router. Aliased rather than re-spelled: two literals is how they drift apart.
_AUDIENCE_CONFIG = agent_service.AUDIENCE_CONFIG


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


def _envelope_payload(job_type: str, meta: dict) -> dict:
    """The payload half of a signed lease envelope, per job type.

    Dispatched rather than hardcoded to discovery, and each branch goes through that
    type's OWN closed allowlist — which is what stops a field added for one job type
    from silently becoming reachable by another.

    Every member of ``agent_service.AGENT_JOB_TYPES`` gets an explicit branch, and there
    is deliberately **no fall-through default**. A default is not a safe shortcut here:
    it does not fail, it silently projects the new type's metadata through some other
    type's allowlist, so every field the new handler reads arrives missing and the agent
    runs its own defaults instead. That is exactly how ``agent_gateway`` shipped for a
    while — dropped through to the discovery allowlist, lost ``gateway_action``, and a
    POV teardown asking to *remove* a Gateway installed one and reported success.
    ``test_every_agent_job_type_has_an_envelope_branch`` pins the coverage.
    """
    if job_type == "agent_discover":
        return agent_job_meta.discover_kwargs(meta)
    if job_type == "agent_hypervisor":
        return agent_hypervisor_meta.hypervisor_kwargs(meta)
    if job_type == "agent_ansible":
        # The narrowest of the four: four scalars, and the playbook is not among them. The
        # agent fetches the rest from /jobs/{id}/ansible-bundle, sealed — see
        # agent_ansible_meta for why executable content may not be merely *signed*.
        return agent_ansible_meta.envelope_payload(meta)
    if job_type == "agent_gateway":
        # Two scalars, and `gateway_action` is the load-bearing one: it is the whole
        # difference between putting a privileged container on a host and taking it away.
        # The deploy key is NOT here — it rides the sealed per-job channel.
        return agent_gateway_meta.envelope_payload(meta)
    # Unreachable: `lease_one` filters on `allowed_job_types`, itself a subset of
    # AGENT_JOB_TYPES, so a type with no branch cannot have been claimed. Raising rather
    # than defaulting is the point — a job stuck in `running` is a bug someone chases,
    # a job that ran the wrong action and reported success is one nobody does.
    raise ValueError(f"no lease-envelope builder for job type {job_type!r}")


class LeaseRequest(BaseModel):
    """What the agent says it currently is.

    Both fields are advisory and unvalidated — a compromised agent can claim anything,
    and nothing here grants it a capability. They exist so the dashboard can *describe*
    the fleet honestly: an agent that pulled a new image reports the new version, and
    an agent whose policy.yaml omits a job type says so. Older agents send ``{}`` and
    both default, leaving the stored values untouched.
    """
    agent_version: str = ""
    job_types: list = []


@router.post("/lease")
async def lease_job(request: Request, body: LeaseRequest = LeaseRequest(),
                    agent: RemoteAgent = Depends(signed_agent),
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

    # Before the claim, so a compatibility gate elsewhere sees a fresh version even for
    # an agent that never gets given work.
    try:
        agent_service.record_self_report(
            db, agent, agent_version=body.agent_version, job_types=body.job_types)
    except Exception:  # noqa: BLE001 — bookkeeping must never fail a lease
        logger.debug("agent: self-report update failed", exc_info=True)

    claim = agent_service.lease_one(db, agent)
    if claim is None:
        return {"job": None, "poll_interval_s": agent_service.DEFAULT_POLL_INTERVAL_S}

    envelope = {
        "job_id": claim["id"],
        "job_type": claim["job_type"],
        "agent_id": agent.id,
        "audience": _resolve_audience(request),
        "payload": _envelope_payload(claim["job_type"], claim["meta"]),
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
    if job.job_type == "agent_hypervisor" and body.status == "completed":
        # Apply the page, and chain the next one if the agent handed back a cursor.
        # Bounded by MAX_SYNC_PAGES — see hypervisor_sync_service.
        try:
            result = hypervisor_sync_service.apply_page(db, job, result)
        except Exception:  # noqa: BLE001 — a bad page must not wedge the job row
            logger.exception("could not apply a hypervisor sync page for job %s", job.id)
            result = agent_hypervisor_meta.sync_page(result)
    elif job.job_type == "agent_discover" and body.status == "completed":
        # Cross-check findings against the dashboard's own inventory HERE rather than
        # sending the agent the inventory to compare against. An agent should never
        # learn what else the dashboard manages.
        result = _annotate_findings(db, result)

    applied = agent_service.complete_job(db, agent, job, status=body.status,
                                         result=result, error=body.error)
    agent_service.audit(db, agent, "agent.complete", ip=_client_ip(request),
                        details={"job_id": job.id, "status": applied})

    # Re-read the inventory the operator just changed, on their behalf. The dashboard
    # has no route to an agent-bound hypervisor, so a start or a stop left the page
    # showing the old state until someone pressed Sync Now or the timed pass came round
    # up to half an hour later — the button worked and the page said it had not.
    #
    # AFTER complete_job, not before: until this row is terminal it is itself the
    # connection's open agent_hypervisor job, and the in-flight guard inside sync_now
    # would read it and skip. A no-op for every other verb, including each page of a
    # sync — see hypervisor_sync_service.sync_after_power.
    try:
        follow, reason = hypervisor_sync_service.sync_after_power(db, job)
        if follow is not None:
            logger.info("queued inventory sync %s after power job %s", follow.id, job.id)
        elif reason:
            logger.info("no inventory sync after power job %s: %s", job.id, reason)
    except Exception:  # noqa: BLE001 — a finished job must not fail on its follow-up
        logger.exception("could not queue an inventory sync after job %s", job.id)

    # Release a Password Safe credential this job checked out. The fast path only — a job
    # whose agent was killed never gets here, which is why `agent_ps_credential_service.
    # sweep` and not this hook is the authority. Ref-counted inside, so a sibling job
    # sharing the request keeps it open. Never allowed to fail a finished job.
    try:
        if await agent_ps_credential_service.release_for_job(db, job):
            agent_service.audit(db, agent, "agent.connection_secret_released",
                                ip=_client_ip(request),
                                details={"job_id": job.id, "rotated": True})
    except Exception:  # noqa: BLE001
        logger.exception("could not release a Password Safe credential for job %s", job.id)
    return {"job_id": job.id, "status": applied}


class SecretRequest(BaseModel):
    """What the agent must say to be handed a credential.

    ``connection_ref`` is checked against the job's *own* metadata rather than trusted, and
    ``reply_key`` is the ephemeral X25519 public half the response is sealed to. Both are
    covered by the request signature — ``canonical_request`` hashes the body — so neither
    can be substituted in flight by whatever sits between the agent and here.
    """
    connection_ref: str = ""
    reply_key: str = ""


@router.post("/jobs/{job_id}/secret")
async def job_secret(job_id: str, body: SecretRequest, request: Request,
                     agent: RemoteAgent = Depends(signed_agent),
                     db: Session = Depends(get_db)):
    """Hand this job's hypervisor credential to the agent, sealed, once it has proven it
    owns the job the credential belongs to.

    This is the route that lets an on-prem host keep no standing hypervisor credential at
    all. Everything about it is scoped down to that one purpose:

    * ``statuses=("running",)`` rather than the ``WINDING_DOWN`` default every other
      per-job route uses. A *cancelled* job may still heartbeat, log and complete so
      cooperative cancel stays reachable — but it has no business being handed a fresh
      credential.
    * The connection is **derived from the job row**, never from the request. The ref in
      the body must equal the derived one, but it cannot select anything: without that,
      a stolen agent identity could enumerate every credential the dashboard holds by
      asking for arbitrary refs against a job it legitimately owns. That is the most
      important property here and it is not a cryptographic one.
    * The response is sealed to ``reply_key`` (``services/agent_sealing``) rather than
      returned in the clear. An inspecting corporate proxy is the deployment this whole
      feature exists for, and it reads response bodies.
    """
    job = _owned(db, agent, job_id, statuses=("running",))
    if job.job_type != "agent_hypervisor":
        # Equality against the one type that has a connection, not a truthy test. A
        # discovery job has none, and by design carries no credential anywhere.
        raise HTTPException(
            status_code=409,
            detail="This job type does not use a hypervisor credential.")

    meta = job.metadata_dict or {}
    ref = str(meta.get("connection_ref") or "")
    connection_id = str(meta.get("connection_id") or "")
    if not ref or not connection_id:
        raise HTTPException(
            status_code=409,
            detail="This job names no connection, so there is no credential to fetch.")
    if (body.connection_ref or "") != ref:
        # The body cannot *choose* the connection, so this can only mean the agent and the
        # job row disagree — a rewritten job row, or a confused agent. Either way, refusing
        # loudly beats handing over a credential for a connection the agent did not mean.
        raise HTTPException(
            status_code=409,
            detail="That connection is not the one this job was queued for.")

    # Checked BEFORE the credential is obtained, not after. For a Password Safe account,
    # obtaining it opens a real request that is then checked in and rotated on release — so
    # answering a request that could never be sealed would let a caller burn checkouts and
    # rotations on that account at will.
    try:
        agent_sealing.check_reply_key(body.reply_key)
    except agent_sealing.SealError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"The reply key could not be used to seal a response: {exc}")

    try:
        row = hypervisor_connection_service.agent_secret_row(
            db, agent_id=agent.id, connection_id=connection_id, ref=ref)
        if hypervisor_connection_service.is_ps_account(row):
            secret, source = await agent_ps_credential_service.acquire(db, job, row)
        else:
            secret, source = hypervisor_connection_service.resolve_agent_secret(row)
    except (HypervisorConnectionError,
            agent_ps_credential_service.PSCredentialError) as exc:
        # 409 with the detail passed through, matching `_owned`. The agent puts this
        # straight into the job's error_message, which is the only text a failed job
        # renders — so the remedy has to survive the trip intact.
        raise HTTPException(status_code=409, detail=str(exc))

    try:
        envelope = agent_sealing.seal(
            body.reply_key, secret, agent_id=agent.id,
            audience=_resolve_audience(request), job_id=job.id, ref=ref)
    except agent_sealing.SealError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"The reply key could not be used to seal a response: {exc}")

    agent_service.audit(db, agent, "agent.connection_secret", ip=_client_ip(request),
                        details={"job_id": job.id, "connection_id": row.id,
                                 "connection_ref": ref, "source": source})
    # Explicit, because nothing else in this app sets it. `no-store` and not `no-cache`:
    # the latter permits a proxy to store the body so long as it revalidates.
    return JSONResponse(
        content={"sealed": envelope},
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"})


class GatewayKeyRequest(BaseModel):
    """What the agent must say to be handed a Gateway deploy key.

    Only the ephemeral reply key, exactly like ``BundleRequest``. There is deliberately
    nothing to *select* with: the key is derived wholly from the job row, so a stolen
    agent identity cannot ask for another POV's Gateway. The body is covered by the
    request signature, so the key cannot be substituted in flight.
    """
    reply_key: str = ""


@router.post("/jobs/{job_id}/gateway-key")
async def job_gateway_key(job_id: str, body: GatewayKeyRequest, request: Request,
                          agent: RemoteAgent = Depends(signed_agent),
                          db: Session = Depends(get_db)):
    """Hand this job's BeyondTrust Gateway deploy key to the agent, sealed.

    The counterpart of :func:`job_secret`, scoped down the same way and for the same
    reasons — read that one first. What differs is only *which* secret, and why it cannot
    travel any other way:

    A PRA deploy key is **neither single-use nor short-lived.** Every Gateway node
    registered with it joins the same Gateway, which is what makes the cloud gateway
    hosts a cluster — so unlike an enrolment code it cannot ride the lab platform's
    metadata channel, where anyone with read access to the environment can see it. It
    also cannot sit in job metadata, which lands in the database and in the signed
    envelope. This route is the only way it reaches the broker VM.

    ``statuses=("running",)`` rather than the ``WINDING_DOWN`` default: a cancelled job
    may still heartbeat, log and complete so cooperative cancel stays reachable, but it
    has no business being handed a fresh credential.
    """
    job = _owned(db, agent, job_id, statuses=("running",))
    if job.job_type != "agent_gateway":
        # Equality against the one type that has a deploy key, not a truthy test — the
        # same rule job_secret follows.
        raise HTTPException(
            status_code=409,
            detail="This job type does not use a Gateway deploy key.")

    meta = job.metadata_dict or {}
    if str(meta.get("gateway_action") or "install") == "remove":
        # Removing a Gateway needs no credential, so answering would hand one over for a
        # job that has no use for it.
        raise HTTPException(
            status_code=409,
            detail="A Gateway removal needs no deploy key.")

    # Checked BEFORE the key is read, matching job_secret: answering a request that could
    # never be sealed is work done for nothing, and on the Password Safe path it burns a
    # real checkout.
    try:
        agent_sealing.check_reply_key(body.reply_key)
    except agent_sealing.SealError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"The reply key could not be used to seal a response: {exc}")

    from ..services import pov_gateway
    try:
        # Derived from the job row's environment, never from the request body.
        deploy_key = pov_gateway.deploy_key_for_job(db, job)
    except pov_gateway.GatewayInstallError as exc:
        # 409 with the detail passed through: the agent puts this straight into the job's
        # error_message, which is the only text a failed job renders.
        raise HTTPException(status_code=409, detail=str(exc))

    try:
        envelope = agent_sealing.seal(
            body.reply_key, deploy_key, agent_id=agent.id,
            audience=_resolve_audience(request), job_id=job.id,
            ref=pov_gateway.SEAL_REF)
    except agent_sealing.SealError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"The reply key could not be used to seal a response: {exc}")

    agent_service.audit(db, agent, "agent.gateway_deploy_key", ip=_client_ip(request),
                        details={"job_id": job.id,
                                 "environment_id": str(meta.get("environment_id") or "")})
    return JSONResponse(
        content={"sealed": envelope},
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"})


class BundleRequest(BaseModel):
    """What the agent must say to be handed a run bundle.

    Only the ephemeral reply key. There is deliberately nothing to *select* with: the run is
    derived wholly from the job row, so a stolen agent identity cannot ask for a different
    job's playbook or credentials. The body is covered by the request signature
    (``canonical_request`` hashes it), so the key cannot be substituted in flight.
    """
    reply_key: str = ""


@router.post("/jobs/{job_id}/ansible-bundle")
async def job_ansible_bundle(job_id: str, body: BundleRequest, request: Request,
                            agent: RemoteAgent = Depends(signed_agent),
                            db: Session = Depends(get_db)):
    """Hand this job's Config-Management run bundle to the agent, sealed.

    The counterpart of :func:`job_secret`, and scoped down the same way — read that one
    first; the reasoning is identical and only the payload differs. What differs and why:

    * A bundle rather than one string, because splitting it into a route per credential
      would cost one seal, one audit row and one **Password Safe checkout** each, for no
      gain: ``job_id`` already prevents cross-job replay, and ``seal``'s plaintext is a JSON
      object precisely so it can carry more than one field.
    * The AAD ``ref`` is :func:`agent_sealing.bundle_ref`, which names the *endpoint* rather
      than a connection. A bundle carries an SSH private key and a become password, so the
      relabelling threat ``seal_aad`` describes is at its most valuable here — and because
      the agent rebuilds the ref from the envelope it already signature-checked, a bundle
      sealed for a different host cannot make the tag verify.
    * It carries **no inventory and no ``ansible_*`` variable**. See
      ``services/agent_ansible_bundle``: an inventory is a place to write
      ``ansible_connection: local``, which would run the play inside the runner container
      rather than against the target.

    ``statuses=("running",)`` and ``Cache-Control: no-store`` are copied from
    :func:`job_secret` deliberately: a cancelled job may still log and complete, but it has
    no business being handed fresh credentials.
    """
    job = _owned(db, agent, job_id, statuses=("running",))
    if job.job_type != "agent_ansible":
        # Equality against the one type that has a run bundle, not a truthy test.
        raise HTTPException(
            status_code=409,
            detail="This job type does not use a Config-Management run bundle.")

    # Checked BEFORE the bundle is assembled, for the same reason job_secret checks it
    # first: building it can open a real Password Safe request, so answering a call that
    # could never be sealed would let a caller burn checkouts and rotations at will.
    try:
        agent_sealing.check_reply_key(body.reply_key)
    except agent_sealing.SealError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"The reply key could not be used to seal a response: {exc}")

    meta = job.metadata_dict or {}
    try:
        bundle, scrub = await agent_ansible_bundle.build(db, job=job, agent=agent)
    except agent_ansible_bundle.BundleError as exc:
        # 409 with the detail passed through, matching job_secret: the agent puts this
        # straight into the job's error_message, which is the only text a failed job
        # renders — so the remedy has to survive the trip intact.
        raise HTTPException(status_code=409, detail=str(exc))

    # Stamp the applied playbook's fingerprint onto the job row, for the config-drift
    # signals. This is the only moment the dashboard holds those bytes — the agent will run
    # them minutes from now — so recording it on completion instead would mean re-fetching
    # the asset and hashing whatever it says *then*, which is a different playbook if
    # somebody re-uploaded in between. A one-way hash, never the content.
    try:
        from ..services import config_drift
        # The ASSET's bytes, which is what the dashboard-local runner hashes — so the two
        # runners agree about whether a target is running the current playbook. For a
        # `.yml` the asset IS the playbook; for a `.sh`/`.ps1`/`.rpm`/`.deb` the playbook is
        # a generated wrapper and the asset is what the operator actually versioned.
        content = (base64.b64decode(bundle["asset_b64"]) if bundle.get("asset_b64")
                   else str(bundle.get("playbook") or "").encode())
        job_service.update_metadata(
            db, job.id, {"content_hash": config_drift.content_hash(content)})
    except Exception:  # noqa: BLE001 — drift tracking must never fail a run
        logger.warning("agent: could not stamp the content hash for job %s", job.id,
                       exc_info=True)

    ref = agent_sealing.bundle_ref(
        run_kind=bundle["run_kind"], transport=bundle["transport"],
        host=meta.get("target_host") or "", port=meta.get("target_port") or 0)
    try:
        envelope = agent_sealing.seal(
            body.reply_key,
            # A JSON *string*, not the dict: seal() does serialize({"secret": str(secret)}),
            # so handing it a dict would seal a Python repr that json.loads then rejects on
            # the far side — a silent corruption rather than a type error.
            json.dumps({"bundle": bundle, "scrub": scrub}),
            agent_id=agent.id, audience=_resolve_audience(request),
            job_id=job.id, ref=ref)
    except agent_sealing.SealError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"The reply key could not be used to seal a response: {exc}")

    # The asset name and target, never the credential fields. An audit row an operator can
    # read alongside the job, and the one durable record that material was released.
    agent_service.audit(db, agent, "agent.ansible_bundle", ip=_client_ip(request),
                        details={"job_id": job.id, "run_kind": bundle["run_kind"],
                                 "asset": meta.get("asset") or "",
                                 "target": meta.get("target_host") or ""})
    return JSONResponse(
        content={"sealed": envelope},
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"})


def _owned(db: Session, agent: RemoteAgent, job_id: str, *,
           statuses: tuple = agent_service.WINDING_DOWN) -> Job:
    try:
        return agent_service.owned_job(db, agent, job_id, statuses=statuses)
    except AgentError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


# A probe reports a PRODUCT; a connection has a KIND. ESXi and vCenter are both
# vsphere connections, and WinRM is as close as a credential-less probe gets to Hyper-V.
_PRODUCT_TO_KIND = {"vsphere": "vsphere", "esxi": "vsphere", "proxmox": "proxmox",
                    "nutanix": "nutanix", "xcpng": "xcpng", "winrm": "hyperv"}


def _annotate_findings(db: Session, result: dict) -> dict:
    """Mark each finding the dashboard already has a connection for.

    Computed HERE rather than by sending the agent the connection list to compare
    against: an agent should never learn what else the dashboard manages.

    Registration is never automatic — a connection needs a credential, and a probe by
    definition has none. So this only annotates; a human clicks "Add connection" and
    supplies the credential under their own permissions.

    One honest limitation: a connection configured with an FQDN but discovered by IP
    reads as unregistered, because the dashboard cannot resolve the customer's private
    DNS — it is not on that network, which is the whole reason the agent exists. The
    prefill writes the IP the probe used, so a second scan matches.
    """
    from ..database import HypervisorConnection

    findings = result.get("findings")
    if not isinstance(findings, list):
        return result

    known = {((row[0] or "").lower(), (row[1] or "").strip().lower(), int(row[2] or 0))
             for row in db.query(HypervisorConnection.kind, HypervisorConnection.host,
                                 HypervisorConnection.port)
             .filter(HypervisorConnection.is_active.is_(True)).all()}
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        kind = _PRODUCT_TO_KIND.get((finding.get("product") or "").lower(), "")
        try:
            port = int(finding.get("port") or 0)
        except (TypeError, ValueError):
            port = 0
        finding["already_registered"] = (
            kind, (finding.get("host") or "").strip().lower(), port) in known
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
        # Three distinct states the UI renders differently: granted and capable,
        # granted but refused by the agent's own policy.yaml, and not granted.
        "job_types": list(agent_service.AGENT_JOB_TYPES),
        "allowed_job_types": list(agent_service.allowed_job_types(agent)),
        "reported_job_types": agent.reported_job_types_list,
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
# SELinux, so this is safe everywhere and needs no host detection — including on Windows,
# which is why the PowerShell form keeps it rather than diverging.
class _Shell(NamedTuple):
    """The four things that differ between a POSIX shell and PowerShell here.

    The image is Linux-only, but it runs perfectly well on a Windows host under Docker
    Desktop's Linux VM — so what a Windows operator needs is a different *shell*, not a
    different container. Every hardening flag below is identical in both.

    ``cont`` matters most: a trailing ``\\`` is not a line continuation in PowerShell, so
    pasting the POSIX command there runs each line as its own command and fails on the
    first one. PowerShell continues with a backtick, which must be the last character on
    the line — no trailing whitespace.

    ``write_code``/``remove_code`` replace ``umask``/``printf``/``rm``. Two notes on the
    PowerShell pair: ``-Encoding ascii`` is load-bearing rather than tidy, because
    PowerShell 5.1's ``>`` and ``Out-File`` write UTF-16LE and the agent reads that file as
    text — a UTF-16 code file fails to decode. And there is no ``umask`` equivalent because
    none is needed: Docker Desktop presents bind mounts with permissive ownership, so uid
    10001 can read the file without one.
    """
    cont: str
    pwd: str
    write_code: str
    remove_code: str


_BASH = _Shell(
    cont="\\",
    pwd="$PWD",
    write_code="umask 022 && printf '%s' '{code}' > ./agent-enroll-code",
    remove_code="rm ./agent-enroll-code",
)
_POWERSHELL = _Shell(
    cont="`",
    pwd="${PWD}",
    write_code=("Set-Content -Path .\\agent-enroll-code -Value '{code}' "
                "-NoNewline -Encoding ascii"),
    remove_code="Remove-Item .\\agent-enroll-code",
)


def _run_flags(sh: _Shell) -> str:
    """The shared flag block, rendered for one shell.

    Built rather than kept as two literals so the flavours cannot drift: a hardening flag
    added to one and forgotten in the other would ship a materially weaker container to
    whichever operator happened to pick that toggle.
    """
    return (
        f"docker run -d --name dashboard-agent --restart unless-stopped {sh.cont}\n"
        f"  --read-only --cap-drop ALL --security-opt no-new-privileges:true {sh.cont}\n"
        f"  --user 10001:10001 --tmpfs /tmp {sh.cont}\n"
        f"  -v dashboard_agent_state:/var/lib/dashboard-agent {sh.cont}\n"
        f'  -v "{sh.pwd}/policy.yaml:/etc/dashboard-agent/policy.yaml:ro,Z" {sh.cont}\n'
        # For agent-brokered hypervisors, add:
        #   -v "$PWD/connections.yaml:/etc/dashboard-agent/connections.yaml:ro,Z"
        # Deliberately not in the emitted command: an agent that only does discovery has no
        # reason to have the file, and a bind mount whose source is missing is a hard
        # `docker run` failure, so including it unconditionally would break the paste.
    )


def _install_forms(sh: _Shell, base: str, code: str) -> tuple:
    """``(env_var_form, code_file_form)`` for one shell. See ``_install_hint`` for why
    there are two forms at all."""
    flags = _run_flags(sh)
    env_form = (
        flags
        + f"  -e DASHBOARD_URL={base} {sh.cont}\n"
        + f"  -e AGENT_ENROLLMENT_CODE={code} {sh.cont}\n"
        + "  chrweav/dashboard-agent:latest"
    )
    code_file_form = (
        # `.replace`, not `.format`: a PowerShell template is the natural place for someone
        # to add `${PWD}`, which `.format` would read as a field name and raise KeyError on.
        sh.write_code.replace("{code}", code) + "\n\n"
        + flags
        + f'  -v "{sh.pwd}/agent-enroll-code:/etc/dashboard-agent/enroll-code:ro,Z" {sh.cont}\n'
        + f"  -e DASHBOARD_URL={base} {sh.cont}\n"
        + f"  -e AGENT_ENROLLMENT_CODE_FILE=/etc/dashboard-agent/enroll-code {sh.cont}\n"
        + "  chrweav/dashboard-agent:latest\n\n"
        # `#` starts a comment in both shells.
        + "# Once the Agents page shows this agent Online, the code is spent:\n"
        + sh.remove_code
    )
    return env_form, code_file_form


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
    a directory the operator chose, and the trailing delete closes it. An operator who
    wants the code in neither shell history nor Docker metadata can write that file with
    an editor and skip the line that writes it. It carries ``,Z`` for the same reason the
    policy mount does: mode bits are only half of "readable" on an SELinux host.

    Each form is emitted twice, once per shell — see :class:`_Shell`. Both flavours are
    built here, from this one call, on purpose: ``_resolve_audience(persist=True)`` writes
    the write-once signing pin, and ``tests/test_agent_lease_invariants.py`` pins the set
    of callers allowed to do that. A second call to get a second flavour would be a second
    pin.
    """
    base = _resolve_audience(request, persist=True)
    bash_run, bash_code_file = _install_forms(_BASH, base, code)
    ps_run, ps_code_file = _install_forms(_POWERSHELL, base, code)
    return {
        "docker_run": bash_run,
        "docker_run_code_file": bash_code_file,
        "docker_run_powershell": ps_run,
        "docker_run_code_file_powershell": ps_code_file,
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
    scan_kind: str = "all"        # vmware|proxmox|nutanix|xcpng|winrm|all
    cidrs: list = []
    hostnames: list = []
    ports: Optional[dict] = None
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

    # A 1.x agent scans for Kubernetes API servers and database listeners. Hand it a
    # hypervisor scan and it matches no family, probes NOTHING, completes green and
    # reports zero findings — which on the findings panel is indistinguishable from a
    # clean network, and emptyReasons() will confidently blame the ports. So refuse
    # before anything is queued, and say exactly what to do.
    #
    # This is only honest because the lease body reports agent_version on every poll
    # (agent_service.record_self_report). It used to be written once at enrolment, so
    # an operator who HAD upgraded would still have been refused.
    if _major_version(agent.agent_version) < 2:
        raise HTTPException(
            status_code=409,
            detail=(f"Agent '{agent.name}' reports version "
                    f"{agent.agent_version or 'unknown'}, which scans for Kubernetes "
                    f"clusters and databases. Hypervisor discovery needs agent 2.0 or "
                    f"later — pull chrweav/dashboard-agent:latest and restart the "
                    f"container. Re-enrolment is not needed; the agent keeps its "
                    f"identity across an image update."))
    reported = agent.reported_job_types_list
    if reported and "agent_discover" not in reported:
        # Covers what a version number cannot: a current agent whose policy.yaml omits
        # the job type. The job would lease and then be refused on the far side.
        raise HTTPException(
            status_code=409,
            detail=(f"Agent '{agent.name}' does not offer discovery. Add "
                    f"`agent_discover` to `job_types` in its policy.yaml and restart it."))

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


def _major_version(raw: str) -> int:
    """Leading integer of a version string; 0 when unknown or unparseable.

    0 for unknown is deliberate: an agent that has never reported a version has also
    never leased under a build that reports one, so it is old.
    """
    match = re.match(r"\s*(\d+)", str(raw or ""))
    return int(match.group(1)) if match else 0


class AgentUpdateRequest(BaseModel):
    """Operator-editable fields. `allowed_job_types` is the dashboard's TRUST in this
    agent; what the agent can actually run is its own policy.yaml, which the dashboard
    cannot change and must not pretend to."""
    allowed_job_types: Optional[list] = None
    site: Optional[str] = None
    description: Optional[str] = None


@router.patch("/{agent_id}")
async def update_agent(agent_id: str, body: AgentUpdateRequest, request: Request,
                       current_user: User = Depends(require_admin),
                       db: Session = Depends(get_db)):
    """Grant or narrow what this agent may be given.

    An **empty** list means "the default set", not "nothing" — that sentinel predates
    this writer and changing it would silently re-authorize every existing agent. So
    "grant nothing" is deliberately not expressible: to stop an agent, revoke it.
    """
    agent = _load(db, agent_id)
    if body.allowed_job_types is not None:
        agent_service.set_allowed_job_types(db, agent, body.allowed_job_types)
    if body.site is not None:
        agent.site = str(body.site).strip()[:64]
    if body.description is not None:
        agent.description = str(body.description).strip()[:255]
    db.commit()
    db.refresh(agent)
    job_service.log_audit(db, current_user.username, "agent.update",
                          ip_address=_client_ip(request),
                          details={"agent": agent.name,
                                   "allowed_job_types": agent.allowed_job_types_list})
    return _agent_row(agent)


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
