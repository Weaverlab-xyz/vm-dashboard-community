"""Remote on-prem agent: enrolment, authentication, and the job lease.

The dashboard reaches every other target by dialling out to it. That works for cloud
APIs and it works for a LAN the dashboard container happens to sit on, but it cannot
work for a private network the dashboard is not in — which is why
``ansible_cloud_run_service`` raises "only this host has a route to the cluster" when
its Docker socket is missing. An agent inverts the direction: it lives inside that
network, dials out, and asks for work.

Three properties hold this together, and each is enforced here rather than by
convention:

1. **An agent is its own principal.** It is never resolved to a ``User``, so it never
   passes through ``require_permission`` — whose "empty permissions means unrestricted"
   backward-compat rule (api/auth.py) would silently grant a bare machine account
   everything. Authorization is the closed :data:`AGENT_JOB_TYPES` allow-list plus an
   ownership check on every job row.

2. **The lease cannot race the local runner.** ``jobs_worker._claim_one`` claims
   ``status='pending'``; :func:`lease_one` claims ``status='queued' AND agent_id=:id``.
   ``job_service.create_job`` forces ``queued`` whenever ``agent_id`` is set, and
   :data:`AGENT_JOB_TYPES` is disjoint from ``HANDLED_TYPES`` — two independent guards,
   both pinned by ``tests/test_agent_lease_invariants.py``.

3. **The lease IS the job row.** ``status='running'`` + ``agent_id`` + ``updated_at``.
   There is no separate lease table because ``job_service.reconcile_stale_jobs``
   already fails ``running`` jobs whose heartbeat went stale — that is lease expiry,
   already written and already called at worker startup.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..database import AgentNonce, Job, RemoteAgent
from . import agent_signing, config_service, job_service

logger = logging.getLogger(__name__)

# Enrolment codes are prefixed so they are recognisable in a paste and impossible to
# confuse with a PAT. Note `"agte_x".startswith("agt_")` is False — position 3 is "e",
# not "_" — which a test pins, because renaming this to `agt_e` would silently make an
# enrolment code look like something else.
ENROLL_CODE_PREFIX = "agte_"
ENROLL_TTL_MINUTES = 15

# Job types a remote agent may execute. MUST stay disjoint from
# jobs_worker.HANDLED_TYPES; the static test enforces it.
AGENT_JOB_TYPES = ("agent_discover",)

# An agent is "online" if it polled within this many seconds. Three times the default
# poll interval, so one dropped request does not flap the badge.
ONLINE_WINDOW_SECONDS = 90

# Handed to the agent at enrolment so cadence is server-controlled and can be tuned
# without redeploying every container.
DEFAULT_POLL_INTERVAL_S = 5
DEFAULT_HEARTBEAT_INTERVAL_S = 60

# Server-side caps on what an agent may push back. These bound a compromised or buggy
# agent's ability to fill the database, which is the one denial-of-service it has.
MAX_LOG_LINES_PER_REQUEST = 500
MAX_LOG_LINE_CHARS = 8192
MAX_LOG_LINES_PER_JOB = 20000
MAX_RESULT_BYTES = 256 * 1024

# Config key holding the dashboard's own Ed25519 signing key (encrypted at rest by
# config_service). Distinct from jwt_secret_key: this one signs job envelopes, and
# mixing the two would mean a leak of either forged both sessions and work.
_SIGNING_KEY_CONFIG = "agent_envelope_signing_key"


class AgentError(Exception):
    """Refusal with an operator-readable reason."""


# ── Hashing / codes ───────────────────────────────────────────────────────────

def hash_code(raw: str) -> str:
    """SHA-256 hex, same shape and storage discipline as ``api/tokens.hash_pat``."""
    return hashlib.sha256(raw.encode()).hexdigest()


def new_enroll_code() -> str:
    return ENROLL_CODE_PREFIX + secrets.token_hex(32)


# ── Dashboard signing key ─────────────────────────────────────────────────────

def envelope_signing_key() -> str:
    """The dashboard's Ed25519 private key, minted on first use.

    Mint-once-then-stash mirrors ``entitle_registration_service.ensure_agent_token``.
    Kept in the encrypted config store rather than an env var so it survives a restart
    — if it rotated per process, every enrolled agent would reject every envelope after
    a redeploy, which looks exactly like an attack and is not one.
    """
    existing = config_service.get(_SIGNING_KEY_CONFIG)
    if existing:
        return existing
    private_b64, _ = agent_signing.generate_keypair()
    config_service.set(_SIGNING_KEY_CONFIG, private_b64)
    logger.info("agent: minted the dashboard envelope signing key")
    return private_b64


def envelope_public_key() -> str:
    """The public half agents pin at enrolment."""
    private = agent_signing.load_private_key(envelope_signing_key())
    return agent_signing.public_key_b64(private)


# ── Registration (operator side) ──────────────────────────────────────────────

def create_agent(db: Session, *, name: str, site: str = "", description: str = "",
                 created_by: str = "") -> tuple[RemoteAgent, str]:
    """Register an agent and return it with its one-time enrolment code.

    The code is returned exactly once, like a PAT — only its hash is stored.
    """
    name = (name or "").strip()
    if not name:
        raise AgentError("Agent name is required.")
    if len(name) > 64:
        raise AgentError("Agent name must be 64 characters or fewer.")
    if db.query(RemoteAgent).filter(RemoteAgent.name == name).first():
        raise AgentError(f"An agent named '{name}' already exists.")

    code = new_enroll_code()
    agent = RemoteAgent(
        name=name,
        site=(site or "").strip(),
        description=(description or "").strip(),
        enroll_code_hash=hash_code(code),
        enroll_expires_at=datetime.utcnow() + timedelta(minutes=ENROLL_TTL_MINUTES),
        created_by=created_by,
    )
    db.add(agent)
    db.commit()
    db.refresh(agent)
    return agent, code


def reissue_enroll_code(db: Session, agent: RemoteAgent) -> str:
    """A fresh enrolment code for a reinstall, keeping the row and its history.

    Clears the public key too: the agent is being replaced, and leaving the old key
    valid would mean the retired container could still lease work.
    """
    code = new_enroll_code()
    agent.enroll_code_hash = hash_code(code)
    agent.enroll_expires_at = datetime.utcnow() + timedelta(minutes=ENROLL_TTL_MINUTES)
    agent.public_key = None
    agent.enrolled_at = None
    db.commit()
    return code


def revoke_agent(db: Session, agent: RemoteAgent) -> int:
    """Deactivate an agent and fail every job it currently holds.

    Failing the in-flight jobs is the point. Deactivating alone would leave rows
    ``running`` until ``reconcile_stale_jobs`` noticed ten minutes later, and an
    operator who just hit Revoke should not have to wonder whether something is still
    executing out there.
    """
    agent.is_active = False
    agent.public_key = None
    agent.enroll_code_hash = None
    db.commit()

    held = db.query(Job).filter(Job.agent_id == agent.id,
                                Job.status.in_(("queued", "running"))).all()
    for job in held:
        if job.status == "running":
            job_service.set_failed(db, job.id, "Agent was revoked while it held this job.")
        else:
            job_service.set_cancelled(db, job.id)
    return len(held)


def status_of(agent: RemoteAgent, *, now: Optional[datetime] = None) -> str:
    """Derived, never stored — a stored status is how a row still reads 'online' three
    weeks after the container died."""
    if not agent.is_active:
        return "revoked"
    if not agent.public_key:
        return "enrolling"
    if not agent.last_seen_at:
        return "offline"
    now = now or datetime.utcnow()
    fresh = (now - agent.last_seen_at).total_seconds() <= ONLINE_WINDOW_SECONDS
    return "online" if fresh else "offline"


# ── Enrolment (agent side) ────────────────────────────────────────────────────

def enroll(db: Session, *, code: str, public_key: str, agent_version: str = "",
           policy_hash: str = "", ip: str = "") -> RemoteAgent:
    """Redeem a one-time enrolment code, binding the agent's public key to the row.

    Looked up by hash, then confirmed with a constant-time compare. The lookup is
    already an indexed equality on a hash so it leaks nothing by itself; the compare is
    belt-and-braces against a future refactor that reaches for a different query shape.
    """
    code = (code or "").strip()
    if not code.startswith(ENROLL_CODE_PREFIX):
        raise AgentError("Invalid enrolment code.")

    code_hash = hash_code(code)
    agent = db.query(RemoteAgent).filter(RemoteAgent.enroll_code_hash == code_hash).first()
    if not agent or not hmac.compare_digest(agent.enroll_code_hash or "", code_hash):
        raise AgentError("Invalid enrolment code.")
    if not agent.is_active:
        raise AgentError("This agent has been revoked.")
    if agent.enroll_expires_at and agent.enroll_expires_at < datetime.utcnow():
        raise AgentError("Enrolment code has expired. Issue a new one from the Agents page.")

    # Reject a malformed key here rather than storing it and failing every later poll
    # with an unexplained 401. Translated to AgentError so the endpoint answers 400
    # like every other bad-input case, instead of leaking a 500.
    try:
        agent_signing.load_public_key(public_key)
    except agent_signing.SignatureError as exc:
        raise AgentError(f"Invalid public key: {exc}")

    agent.public_key = public_key
    agent.policy_hash = (policy_hash or "")[:64]
    agent.agent_version = (agent_version or "")[:32]
    agent.enrolled_at = datetime.utcnow()
    agent.last_seen_at = datetime.utcnow()
    agent.last_seen_ip = (ip or "")[:45]
    # Single-use: consumed on success, so a captured code cannot enroll a second,
    # attacker-controlled container against the same row.
    agent.enroll_code_hash = None
    agent.enroll_expires_at = None
    db.commit()
    db.refresh(agent)
    return agent


# ── Request authentication ────────────────────────────────────────────────────

def authenticate(db: Session, *, agent_id: str, timestamp: str, nonce: str,
                 signature: str, audience: str, method: str, path: str,
                 body: bytes, ip: str = "") -> RemoteAgent:
    """Verify a signed agent request. Raises :class:`AgentError` on any failure.

    Order matters: signature first, nonce second. Recording a nonce before knowing the
    signature is valid would let an unauthenticated caller who can guess an agent id
    burn that agent's nonces and lock it out of its own queue.
    """
    agent = db.query(RemoteAgent).filter(RemoteAgent.id == agent_id).first()
    if not agent or not agent.is_active or not agent.public_key:
        raise AgentError("Unknown or inactive agent.")

    if not agent_signing.verify_request(
        agent.public_key, agent_id=agent_id, timestamp=timestamp, nonce=nonce,
        signature=signature, audience=audience, method=method, path=path, body=body,
    ):
        raise AgentError("Signature verification failed.")

    if not _consume_nonce(db, agent_id, nonce):
        raise AgentError("Replayed request.")

    agent.last_seen_at = datetime.utcnow()
    if ip:
        agent.last_seen_ip = ip[:45]
    db.commit()
    return agent


def _consume_nonce(db: Session, agent_id: str, nonce: str) -> bool:
    """Record a nonce, returning False if it was already used.

    The unique constraint IS the check — an INSERT that raises IntegrityError is a
    replay. Doing it as a SELECT-then-INSERT would leave a window in which two
    concurrent copies of the same captured request both pass.
    """
    if not nonce or len(nonce) > 64:
        return False
    try:
        db.add(AgentNonce(agent_id=agent_id, nonce=nonce, seen_at=datetime.utcnow()))
        db.commit()
        return True
    except IntegrityError:
        db.rollback()
        return False


def sweep_nonces(db: Session, *, older_than_seconds: int = 0) -> int:
    """Drop nonces older than the signature window — they can no longer be replayed
    because the timestamp check would reject them anyway, so keeping them is pure
    storage. Called opportunistically from the lease path."""
    cutoff = datetime.utcnow() - timedelta(
        seconds=older_than_seconds or (agent_signing.SIGNATURE_WINDOW_SECONDS * 4))
    deleted = db.query(AgentNonce).filter(AgentNonce.seen_at < cutoff).delete(
        synchronize_session=False)
    db.commit()
    return deleted


# ── The lease ─────────────────────────────────────────────────────────────────

def lease_one(db: Session, agent: RemoteAgent) -> Optional[dict]:
    """Atomically lease this agent's oldest queued job, or return None.

    A near-copy of ``jobs_worker._claim_one`` with the filter changed from
    ``status='pending'`` to ``status='queued' AND agent_id=:id``. The
    ``UPDATE … WHERE`` **rowcount is the lock**: only the caller whose UPDATE matched
    the row owns it, so two copies of an agent — or an agent racing its own retry —
    never both execute one job. Portable across SQLite and PostgreSQL; no
    ``SKIP LOCKED``.

    Returns a plain dict, not the ORM row, so the caller can build a response without
    detached-instance surprises.
    """
    allowed = allowed_job_types(agent)
    if not allowed:
        return None

    while True:
        job = (
            db.query(Job)
            .filter(Job.status == "queued", Job.agent_id == agent.id,
                    Job.job_type.in_(allowed))
            .order_by(Job.created_at.asc())
            .first()
        )
        if job is None:
            return None
        now = datetime.utcnow()
        claimed = (
            db.query(Job)
            .filter(Job.id == job.id, Job.status == "queued", Job.agent_id == agent.id)
            .update({Job.status: "running", Job.started_at: now, Job.updated_at: now},
                    synchronize_session=False)
        )
        db.commit()
        if claimed == 1:
            fresh = db.query(Job).filter(Job.id == job.id).first()
            return {"id": fresh.id, "job_type": fresh.job_type,
                    "meta": fresh.metadata_dict or {}}
        # Lost the race — try the next candidate.


def allowed_job_types(agent: RemoteAgent) -> tuple:
    """The intersection of what agents may run and what THIS agent may run.

    Intersected, never replaced: a per-agent list can only ever narrow
    :data:`AGENT_JOB_TYPES`, so granting an agent a type the dashboard does not support
    is not expressible.
    """
    per_agent = agent.allowed_job_types_list
    if not per_agent:
        return AGENT_JOB_TYPES
    return tuple(t for t in AGENT_JOB_TYPES if t in per_agent)


# A cancelled job is still the agent's until it winds down. Cooperative cancel works
# by the agent LEARNING about the cancel on its next heartbeat, so refusing these
# endpoints the moment the status flips would make cancel unreachable: the agent would
# keep working, never be told, and only discover it when its completion was rejected.
WINDING_DOWN = ("running", "cancelled")


def owned_job(db: Session, agent: RemoteAgent, job_id: str,
              *, statuses: tuple = WINDING_DOWN) -> Job:
    """Fetch a job this agent currently holds, or refuse.

    Every per-job endpoint goes through here. The ``agent_id`` equality is what stops
    one agent from reading or completing another's job — the highest-severity bug this
    API could have, and the reason it lives in one function rather than being repeated
    at each call site.
    """
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job or job.agent_id != agent.id:
        # Deliberately identical to the not-found case: an agent must not be able to
        # probe for the existence of jobs belonging to another agent.
        raise AgentError("Job not found for this agent.")
    if job.status not in statuses:
        raise AgentError(f"Job is not leased by this agent (status '{job.status}').")
    return job


# ── Reporting back ────────────────────────────────────────────────────────────

def append_logs(db: Session, job: Job, lines: list) -> int:
    """Persist agent log lines as Live Output, capped and sanitised.

    Output originates from an untrusted target on the far side of the agent and is
    rendered in the dashboard UI, so control characters are stripped here — the one
    place every agent-produced line passes through.
    """
    from ..database import JobLog
    if not lines:
        return 0
    existing = db.query(JobLog).filter(JobLog.job_id == job.id).count()
    budget = max(0, MAX_LOG_LINES_PER_JOB - existing)
    written = 0
    for line in lines[:MAX_LOG_LINES_PER_REQUEST]:
        if budget <= 0:
            break
        job_service.append_job_log(db, job.id, _sanitize_line(line))
        written += 1
        budget -= 1
    return written


def _sanitize_line(line) -> str:
    """Strip C0/C1 control characters (except tab) and truncate.

    ANSI escapes from a compromised vCenter or apiserver would otherwise be replayed
    into an operator's browser and terminal.
    """
    text = str(line if line is not None else "")
    cleaned = "".join(
        ch for ch in text
        if ch == "\t" or (ord(ch) >= 32 and not (0x7F <= ord(ch) <= 0x9F))
    )
    if len(cleaned) > MAX_LOG_LINE_CHARS:
        cleaned = cleaned[:MAX_LOG_LINE_CHARS] + " …[truncated]"
    return cleaned


def complete_job(db: Session, agent: RemoteAgent, job: Job, *, status: str,
                 result: Optional[dict] = None, error: str = "") -> str:
    """Finish a leased job. Returns the terminal status actually applied.

    A job the operator already cancelled stays cancelled. The agent is expected to
    report `failed` when it winds down after seeing the cancel flag, and rewriting the
    row would replace the operator's deliberate action with a failure they did not
    cause — the /jobs page would show a red error for something that worked as asked.
    """
    if job.status == "cancelled":
        return "cancelled"
    if status == "completed":
        payload = result or {}
        encoded = json.dumps(payload)
        if len(encoded.encode("utf-8")) > MAX_RESULT_BYTES:
            job_service.set_failed(
                db, job.id,
                f"Agent result exceeded {MAX_RESULT_BYTES // 1024} KB and was rejected.")
            return "failed"
        job_service.set_completed(db, job.id, payload)
        return "completed"

    job_service.set_failed(db, job.id, (error or "The agent reported a failure.")[:2000])
    return "failed"


def audit(db: Session, agent: RemoteAgent, action: str, *, details: Optional[dict] = None,
          ip: str = "") -> None:
    """Record an agent action on the tamper-evident chain.

    Actor is ``agent:{name}`` so agent activity is distinguishable from a human's at a
    glance and cannot be confused with a user account of the same name.
    """
    try:
        job_service.log_audit(db, f"agent:{agent.name}", action,
                              ip_address=ip or None, details=details or {})
    except Exception:  # noqa: BLE001 — an audit hiccup must not fail the agent's work
        logger.warning("agent: could not write audit entry for %s", action, exc_info=True)
