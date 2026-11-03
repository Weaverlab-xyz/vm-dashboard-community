"""Remote on-prem agent models: agents, their enrollment nonces and enrollment attempts."""
import json
import uuid
from sqlalchemy import Column, String, Integer, DateTime, Boolean, Text, UniqueConstraint

from ...utils.clock import utcnow
from .._engine import Base


class RemoteAgent(Base):
    """A containerised agent running inside a private network that polls this
    dashboard for work — the inverse of every other execution path, which dials out
    from the dashboard and therefore cannot reach a network the dashboard is not on.

    Deliberately NOT a ``PersonalAccessToken`` and deliberately not tied to a ``User``.
    A PAT resolves to its owner and carries that owner's full authority with no scope
    (see ``api/auth.py:_get_user_from_pat``), which is a defensible default for a human
    and a dangerous one for a machine principal sitting on someone else's network. An
    agent is its own principal, authorized by a closed allow-list in ``agent_service``,
    and never passes through ``require_permission`` — whose "empty permissions means
    unrestricted" backward-compat rule would silently grant it everything.

    Identity is an **Ed25519 public key**, not a shared secret. The agent generates the
    keypair at enrolment and the private half never leaves its host, so no replayable
    credential ever crosses the wire — which is what makes this safe to run through a
    corporate TLS-inspecting proxy, where an ``Authorization`` header would land in the
    proxy's log in the clear.
    """
    __tablename__ = "remote_agents"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    # 64 not 100: job rows record the actor as `agent:{name}` in Job.created_by,
    # which is String(100).
    name = Column(String(64), nullable=False, unique=True, index=True)
    site = Column(String(64), index=True)          # routing label, e.g. "dc1"
    description = Column(Text)

    # Enrolment: a single-use short-lived code, sha256 at rest exactly like a PAT.
    # NULLed the moment it is redeemed, which is what makes it single-use.
    enroll_code_hash = Column(String(64), unique=True, index=True, nullable=True)
    enroll_expires_at = Column(DateTime, nullable=True)

    # Base64 raw Ed25519 public key (32 bytes -> 44 chars). NULL until enrolled.
    public_key = Column(String(64), nullable=True)
    # sha256 of the agent's local policy.yaml, self-reported on every poll. The
    # dashboard cannot change the policy — it can only show the operator that the hash
    # moved, which is the point: the file is the customer's, not ours.
    policy_hash = Column(String(64), nullable=True)

    allowed_job_types = Column(Text)               # JSON list; empty/NULL = the default set
    agent_version = Column(String(32))
    # What the agent said it can run, on its last lease: the intersection of its
    # HANDLERS table and its own policy.yaml. Distinct from allowed_job_types, which is
    # what the DASHBOARD permits — one is capability, the other is trust, and the UI
    # needs both to say "granted, but this agent's policy refuses it".
    reported_job_types = Column(Text)              # JSON list
    # How many Config-Management runs the agent says it executes at once
    # (`ansible.max_concurrent` in its policy.yaml), from its lease self-report. Advisory,
    # like the two above: it sizes the per-agent request throttle and the Agents page's
    # "N at once", and grants nothing. NULL — every agent before 2.9 — reads as one.
    reported_ansible_slots = Column(Integer, nullable=True)
    enrolled_at = Column(DateTime, nullable=True)
    last_seen_at = Column(DateTime, nullable=True, index=True)
    last_seen_ip = Column(String(45))              # 45 = max INET6_ADDRSTRLEN
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    created_by = Column(String(100))

    # SPIRE attestation (docs/design/agent-and-human-identity.md). `spiffe_id` is the
    # identity an operator bound to this row; an agent presenting a JWT-SVID for it at
    # /api/agent/attest binds a key that lives only in its memory. `auth_mode` records
    # which path set the CURRENT key: NULL/"ed25519" = enrolment code + identity.json
    # (the default, and permanent), "spiffe" = attested. No backfill: every existing
    # row is NULL, which is what it already is.
    spiffe_id = Column(String(255), nullable=True, unique=True, index=True)
    auth_mode = Column(String(16), nullable=True)

    # Lifecycle (docs/platform/spiffe/remote-agents/lifecycle.md). The two `control_*`
    # flags are the agent's own self-report from its lease (2.10+): its policy.yaml opts
    # into upgrades, and an updater container is beside it. Advisory like the reported_*
    # columns above -- they gate what the dashboard OFFERS; the host still decides.
    # `paused` is the dashboard's: a paused agent heartbeats but is handed no new job.
    # `upgrade_ring` orders a rollout: "canary" agents upgrade first. NULL reads as
    # "default", so no backfill.
    control_upgrades = Column(Boolean, nullable=True)
    control_updater = Column(Boolean, nullable=True)
    # Agent 2.13+: the updater's cosign check of its policy's k8s.image ("verified",
    # "pending", "unchecked", "failed: <reason>", "off"). Kubernetes work goes to an owned
    # agent only while this reads "verified" (k8s_executor_service). NULL: not reported.
    control_k8s_signature = Column(String(240), nullable=True)
    paused = Column(Boolean, nullable=True)
    upgrade_ring = Column(String(16), nullable=True)

    # Assurance (docs/platform/spiffe/remote-agents/spire-attestation.md, "Ongoing
    # checks"): attestation is checked at every start, and these keep checking after it.
    # `previous_public_key` is the key the last attestation replaced, kept for
    # `previous_key_until`: a request still signed by it means a second copy of this agent
    # is running. `attention` is a JSON list of {kind, detail, at} an operator acknowledges.
    # `reported_reattest_hours` is the agent's own re-attest interval (2.11+), self-reported.
    last_attest_at = Column(DateTime, nullable=True)
    last_attest_ip = Column(String(45), nullable=True)
    previous_public_key = Column(String(64), nullable=True)
    previous_key_until = Column(DateTime, nullable=True)
    attention = Column(Text, nullable=True)
    reported_reattest_hours = Column(Integer, nullable=True)

    # No `status` column on purpose. Status is derived from is_active / public_key /
    # last_seen_at freshness, because a stored status is how you end up with a row
    # that still reads "online" three weeks after the container died.

    @property
    def allowed_job_types_list(self) -> list:
        return _json_list(self.allowed_job_types)

    @property
    def reported_job_types_list(self) -> list:
        return _json_list(self.reported_job_types)


def _json_list(raw) -> list:
    """A JSON list column, read defensively. A corrupt value must not break a listing."""
    if not raw:
        return []
    try:
        value = json.loads(raw)
        return value if isinstance(value, list) else []
    except Exception:  # noqa: BLE001
        return []


class AgentNonce(Base):
    """Replay guard for signed agent requests.

    A timestamp window alone only narrows the replay opportunity to the width of the
    window; remembering the nonce is what closes it. There is no Redis in this
    deployment and a swept table is entirely adequate at this request rate — the row
    count is bounded by (agents x requests per window), not by uptime.
    """
    __tablename__ = "agent_nonces"

    id = Column(Integer, primary_key=True, autoincrement=True)
    agent_id = Column(String(36), nullable=False, index=True)
    nonce = Column(String(64), nullable=False)
    # Indexed because the sweeper's only query is a range delete on this column.
    seen_at = Column(DateTime, default=utcnow, nullable=False, index=True)

    # The uniqueness IS the guard: the second insert of a nonce raises IntegrityError,
    # and that is what rejects the replay. Scoped per agent so two agents cannot
    # collide with each other's random values.
    __table_args__ = (UniqueConstraint("agent_id", "nonce", name="uq_agent_nonce"),)


class AgentEnrollAttempt(Base):
    """One FAILED agent enrolment attempt, for the throttle in ``agent_guard``.

    ``POST /api/agent/enroll`` is the only unauthenticated route on the only vhost this
    dashboard deliberately exposes to a hostile network, and it does a database lookup
    per call. Guessing a code is infeasible (256 bits), so this table is not really
    brute-force protection — it is what stops an unauthenticated flood from writing rows
    and burning query budget indefinitely.

    Successes are not recorded, so the table only ever holds evidence of failure, and it
    is empty in normal operation. Same storage reasoning as ``LoginAttempt``: in the
    database because gunicorn runs two workers, and an in-process counter would give an
    attacker double the allowance and reset it on redeploy.
    """
    __tablename__ = "agent_enroll_attempts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # Empty string when the peer is unknown, exactly like LoginAttempt.ip — a missing
    # address must still be recorded, or it would be the one free lane.
    ip = Column(String(45), nullable=False, default="", index=True)
    # Indexed because every query is a range scan on it, in both the check and the sweep.
    attempted_at = Column(DateTime, default=utcnow, nullable=False, index=True)
