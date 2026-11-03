"""Operational state models: ephemeral state, notification endpoints and deliveries, the audit log, config apply state."""
import json
import uuid
from sqlalchemy import Column, String, Integer, DateTime, Boolean, Text, UniqueConstraint

from ...utils.clock import utcnow
from .._engine import Base


class EphemeralState(Base):
    """One short-lived, single-use, opaque server-side value: a FIDO2/WebAuthn
    ceremony challenge, or an OAuth/OIDC CSRF ``state`` (with its PKCE verifier).

    In the database for the same reason as :class:`LoginAttempt` above, and it is
    the same bug that motivated that one. This started life as a module-level dict
    in ``services/fido2_service`` guarded by a ``threading.Lock`` — which is the
    correct guard for the wrong hazard. The app runs ``gunicorn -w 2``, so a lock
    makes the dict safe against the *threads* of one worker while leaving the two
    worker **processes** with a private copy each.

    Every ceremony this holds state for is split across two requests — begin/complete
    for FIDO2, login/callback for OAuth — and nothing pins a browser to a worker. So
    the second leg landed on the process that had never stored the state roughly half
    the time, and the user got ``/login?error=invalid_state`` or "Invalid or expired
    FIDO2 challenge" with nothing wrong on either side. More replicas, worse odds.

    **Rows are deleted on read, and the delete is the lock.** These values are
    single-use by definition: a CSRF state that survives its first presentation is
    not a CSRF defence. Concurrent consumers race on ``DELETE ... WHERE key = ?``
    and only the one whose rowcount is 1 gets the value — the same portable
    atomic-claim the job queue uses, no ``SKIP LOCKED``.
    """
    __tablename__ = "ephemeral_state"

    # Opaque and namespaced by the service that owns it (`vmcli:oauth:state:<uuid>`,
    # `vmcli:fido2:challenge:<uuid>`), so one table serves both ceremonies without
    # either being able to consume the other's rows.
    key = Column(String(200), primary_key=True)
    # Text, not String(n): the OIDC entry packs the redirect URI and the PKCE
    # verifier together, and a FIDO2 entry is a serialised state dict.
    value = Column(Text, nullable=False, default="")
    # Wall clock, not time.monotonic() as the dict used — monotonic is per-process
    # and meaningless to a reader that did not start the same process.
    # Indexed because the sweep is a range scan on it.
    expires_at = Column(DateTime, nullable=False, index=True)


class NotificationEndpoint(Base):
    """One outbound webhook sink: a URL plus the payload shape to POST to it.

    Transport is always HTTP POST; `fmt` picks the body. `slack` and `teams` exist
    because those endpoints reject anything that isn't their own shape — a Slack
    incoming webhook wants {"text": ...} and a Teams Power Automate Workflows URL
    wants the Adaptive Card envelope. `custom` is the signed generic envelope, which
    is how email gets delivered here: point it at a Flow / automation platform and
    let that fan out. There is deliberately no SMTP client in this codebase.

    `url` and `secret` are Fernet-encrypted with the same key as app_config, because
    a Slack or Teams webhook URL *is* a bearer credential — anyone holding it can post
    to the channel. They are never returned by the API; the endpoints router hands out
    a scheme+host hint instead.
    """
    __tablename__ = "notification_endpoints"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(100), nullable=False)
    url = Column(Text, nullable=False)              # Fernet-encrypted
    fmt = Column(String(16), nullable=False, default="custom")   # custom | slack | teams
    secret = Column(Text)                           # Fernet-encrypted; HMAC key, custom only
    enabled = Column(Boolean, default=True, nullable=False)
    # CSV override of notify_event_types. Empty/NULL = inherit the global list, which
    # is what almost every endpoint wants — the per-endpoint filter exists so one noisy
    # sink can be narrowed without narrowing everyone.
    event_types = Column(Text)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    created_by = Column(String(100))
    last_success_at = Column(DateTime)
    # The verbatim transport error from the most recent failure. Kept on the endpoint
    # (not just on the delivery rows) so the Settings panel can show "this sink is
    # broken" without joining.
    last_error = Column(Text)


class NotificationDelivery(Base):
    """One outbound message attempt: one event × one endpoint.

    This table is simultaneously the outbox, the dedupe latch, the retry state, and
    the operator's record of what was sent — each of which alone would justify
    persisting it.

    The UNIQUE on `dedupe_key` is the only correct dedupe here: the app runs under
    `gunicorn -w 2` and the worker at `replicas: 3`, so an in-process set would be
    worthless across five processes. job_service.log_audit already absorbs an
    IntegrityError on a unique index the same way.

    Delivery is at-least-once, not exactly-once: a worker killed after the POST but
    before the `sent` write re-sends on the next reclaim. A duplicate alert beats
    silence, and pretending otherwise is how these systems rot.
    """
    __tablename__ = "notification_deliveries"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    created_at = Column(DateTime, default=utcnow, nullable=False, index=True)

    # ── the event (denormalised: fan-out is a handful of rows, so a join buys nothing) ──
    event_type = Column(String(64), nullable=False, index=True)   # "resource.expiring"
    severity = Column(String(16), nullable=False, default="info")
    # inventory_service's id scheme ("job:<uuid>" / "clouddb:<id>" / "k8s:<id>"), so
    # expiry_reaper._resolve_row can already map a delivery back to its resource.
    resource_id = Column(String(128), index=True)
    resource_kind = Column(String(24))
    resource_name = Column(String(255))
    cloud = Column(String(24))
    region = Column(String(40))
    workgroup = Column(String(64), index=True)
    url = Column(Text)                              # deep link

    # ── routing ──
    endpoint_id = Column(String(36), index=True)
    channel = Column(String(32))                    # the fmt used; free-form on purpose
    # JSON. Phase 1 always writes {"route": "global_sink"}. Owner routing or a rules
    # engine writes {"route": "owner", ...} / {"route": "rule", "rule_id": ...} into
    # this same column with no schema change — which is why it beats a nullable FK to
    # a table that does not exist yet.
    reason = Column(Text)

    # ── what was sent ──
    subject = Column(Text)
    body = Column(Text)
    payload = Column(Text)                          # exact JSON posted (never holds the secret)

    # ── delivery state ──
    status = Column(String(16), nullable=False, default="pending", index=True)
    # pending | sending | sent | failed | dry_run | suppressed
    attempts = Column(Integer, nullable=False, default=0)
    next_attempt_at = Column(DateTime, index=True)
    # Exists solely so a row stuck in `sending` (worker SIGKILLed mid-POST) can be
    # reclaimed. Without it that notification is silently lost forever.
    claimed_at = Column(DateTime)
    sent_at = Column(DateTime)
    error = Column(Text)

    dedupe_key = Column(String(200), nullable=False)

    __table_args__ = (UniqueConstraint("dedupe_key", name="uq_notification_dedupe"),)

    @property
    def reason_dict(self) -> dict:
        if not self.reason:
            return {}
        try:
            return json.loads(self.reason)
        except Exception:
            return {}

    @reason_dict.setter
    def reason_dict(self, value: dict):
        self.reason = json.dumps(value)


class AuditLog(Base):
    """Audit log for security-relevant operations"""
    __tablename__ = "audit_log"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    timestamp = Column(DateTime, default=utcnow, nullable=False, index=True)
    username = Column(String(100), index=True)
    action = Column(String(100), nullable=False, index=True)  # 'vm_start', 'vm_stop', 'user_login', etc.
    target_vm = Column(Text)
    details = Column(Text)  # JSON string
    ip_address = Column(String(45))  # IPv4 or IPv6

    # Tamper-evident hash chain (see services/audit_chain.py + /api/audit/verify).
    # Nullable so the ALTER-TABLE migration + one-time backfill can populate
    # pre-existing rows; uniqueness of seq is enforced via ix_audit_log_seq.
    seq = Column(Integer)                # global monotonic sequence
    prev_hash = Column(String(64))       # previous entry's entry_hash (genesis = "0"*64)
    entry_hash = Column(String(64))      # sha256 over this entry's fields + prev_hash

    @property
    def details_dict(self) -> dict:
        """Parse JSON details into dict"""
        if not self.details:
            return {}
        try:
            return json.loads(self.details)
        except:
            return {}

    @details_dict.setter
    def details_dict(self, value: dict):
        """Set details from dict"""
        self.details = json.dumps(value)


class ConfigApplyState(Base):
    """Per-(target, playbook) fingerprint of the last successful Ansible apply —
    powers config-drift visibility (backlog #5). `content_hash` fingerprints the
    applied asset bytes so a later edit is detectable; `applied_at` drives the
    'unverified since' staleness signal. Upserted by config_drift.record_apply."""
    __tablename__ = "config_apply_state"

    id = Column(Integer, primary_key=True, autoincrement=True)
    target = Column(String(255), nullable=False)
    playbook_ref = Column(String(255), nullable=False)
    content_hash = Column(String(64), nullable=False)   # sha256 of the applied asset bytes
    inputs_hash = Column(String(64))                    # sha256 of resolved extra_vars (one-way)
    applied_at = Column(DateTime, default=utcnow, nullable=False)
    job_id = Column(String(36))

    __table_args__ = (UniqueConstraint("target", "playbook_ref", name="uq_apply_target_playbook"),)
