"""
Database models and session management
"""
import json
import uuid
from datetime import datetime
from typing import Optional, List
from sqlalchemy import create_engine, Column, String, Integer, DateTime, Boolean, Text, LargeBinary, ForeignKey, UniqueConstraint, text
from sqlalchemy.orm import declarative_base, relationship
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.pool import NullPool, QueuePool
import bcrypt as _bcrypt

from .config import settings

# Create database engine.
#
# SQLite → NullPool: connections are just file handles so pooling adds no benefit, and
# a bounded pool is exhausted when several long-running background jobs hold sessions
# open simultaneously (each job + each broadcast_progress call takes one slot). NullPool
# creates and closes a fresh connection per Session, eliminating the timeout entirely.
#
# Postgres → QueuePool, EXPLICITLY sized. The library defaults (pool_size=5,
# max_overflow=10 = 15 max) were adequate only while the job worker ran ONE job at a time
# and the concurrency lived in `replicas`, each replica being its own process with its own
# pool. jobs_worker now runs several jobs in ONE pool, and a job is not one connection:
# _dispatch holds a session for the job's whole duration, several services open their own
# (aws_vm_service, azure_vm_service, gcp_vm_service, oci_vm_service, packer_build_service,
# ansible_local_run_service, vdesktop_service, image_promote_service), and every heartbeat
# beat, every job_service.cancel_check and every streamed terraform output line via
# api.websocket.broadcast_progress opens a transient one.
#
# pool_pre_ping / pool_recycle are not about concurrency and are worth having on their own:
# Azure Postgres Flexible Server and the load balancer in front of it close idle
# connections after minutes, and a pooled-but-dead connection surfaces as an InterfaceError
# in the middle of a job rather than at checkout. A two-hour image-export poller is exposed
# to that at concurrency 1.
#
# Budget, per PROCESS: pool_size + max_overflow. The app runs `gunicorn -w 2` → 2 pools;
# the worker → 1 per replica. So one deployment can hold 3 x (size + overflow) — 30 at the
# defaults — and that whole figure multiplies by the replica count. Keep it under
# (max_connections - 20), the 20 covering the server's management sessions plus
# superuser_reserved_connections. Verify with `SHOW max_connections;`: Azure Burstable
# B1ms is 50, which is why the defaults are 5 + 5 and jobs_worker._limits clamps
# concurrency to 3 there; B2s and every General Purpose tier are 429+.
_is_sqlite = "sqlite" in settings.database_url
_pool_kwargs = {} if _is_sqlite else {
    "pool_size":     settings.db_pool_size,
    "max_overflow":  settings.db_max_overflow,
    "pool_timeout":  settings.db_pool_timeout_s,
    "pool_pre_ping": True,
    "pool_recycle":  settings.db_pool_recycle_s,
}
engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False} if _is_sqlite else {},
    poolclass=NullPool if _is_sqlite else QueuePool,
    echo=False,
    **_pool_kwargs,
)


def pool_capacity() -> int:
    """Max connections this process's pool can hand out — what jobs_worker._limits clamps
    concurrency against. 0 for SQLite's NullPool, which is unbounded (a fresh connection
    per Session), so the caller treats 0 as "don't clamp"."""
    if _is_sqlite:
        return 0
    return settings.db_pool_size + settings.db_max_overflow

# Session factory
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Base class for models
Base = declarative_base()


# ========== DATABASE MODELS ==========

class User(Base):
    """User model for authentication and authorization"""
    __tablename__ = "users"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    username = Column(String(100), unique=True, nullable=False, index=True)
    hashed_password = Column(String(255), nullable=True)  # nullable for OAuth-only users
    full_name = Column(String(200))
    email = Column(String(200))
    workgroups = Column(Text)  # JSON array: ["Hydra", "Weaverlab"]
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    # Auth provider fields
    auth_provider = Column(String(20), default="local", nullable=False)  # 'local' | 'azure_ad'
    oauth_subject = Column(String(255), nullable=True, unique=True)  # Azure AD oid claim
    mfa_required = Column(Boolean, default=False)  # True once first FIDO2 key is registered
    is_admin = Column(Boolean, default=False)       # Can manage users via /users page
    # Fine-grained permissions: JSON dict {"vms":["read","write"],"aws":["read"],...}
    # NULL = all permissions granted (backward compatible default).
    # This is the admin-set baseline (set via /users page or wizard).
    permissions = Column(Text, nullable=True)
    # Session-scoped permissions derived from OAuth group membership.
    # Re-computed on every OIDC login as the union of default_permissions
    # across matched oauth_group_mappings. Enables the Entitle user-JIT
    # flow (see docs/design/entitle-user-jit.md Phase 0): Entitle grants
    # the user a time-bound Entra group membership; next login picks the
    # union up here; effective_permissions_dict returns
    # union(permissions, session_permissions). Group expiry → next login
    # sees the group gone → matching permissions drop.
    session_permissions = Column(Text, nullable=True)

    # Entitle REST-granted permissions, and a SEPARATE column on purpose.
    # _complete_oauth_login overwrites session_permissions with the group-derived
    # union on every login — that overwrite is load-bearing, because it is how a
    # removed Entra group actually reduces access. Writing an Entitle grant there
    # would therefore be silently wiped at the user's next login.
    #
    # Keeping the two sources independent also means both can be live at once,
    # which is what makes migrating from Entra groups to the REST integration a
    # gradual change rather than a cutover.
    jit_permissions = Column(Text, nullable=True)

    fido2_credentials = relationship("Fido2Credential", back_populates="user", cascade="all, delete-orphan")
    personal_access_tokens = relationship("PersonalAccessToken", back_populates="user", cascade="all, delete-orphan")

    @property
    def workgroups_list(self) -> List[str]:
        """Parse JSON workgroups into list"""
        if not self.workgroups:
            return []
        try:
            return json.loads(self.workgroups)
        except:
            return []

    @workgroups_list.setter
    def workgroups_list(self, value: List[str]):
        """Set workgroups from list. Names are lowercased to match the canonical
        form stored in the workgroups table. Old TitleCase rows continue to
        resolve via case-insensitive lookups in workgroup_service."""
        normalized = [v.lower() for v in (value or []) if isinstance(v, str)]
        self.workgroups = json.dumps(normalized)

    @property
    def permissions_dict(self) -> dict:
        """Parse JSON permissions into dict. Empty dict = no explicit permissions (treat as all)."""
        if not self.permissions:
            return {}
        try:
            return json.loads(self.permissions)
        except:
            return {}

    @permissions_dict.setter
    def permissions_dict(self, value: dict):
        """Set permissions from dict. Pass empty dict or None to restore full access."""
        self.permissions = json.dumps(value) if value else None

    @property
    def session_permissions_dict(self) -> dict:
        """Parse the session-scoped permissions JSON. Empty when no OIDC
        groups matched (or non-OIDC user). See effective_permissions_dict
        for the union with the admin-baseline."""
        if not self.session_permissions:
            return {}
        try:
            return json.loads(self.session_permissions)
        except Exception:
            return {}

    @session_permissions_dict.setter
    def session_permissions_dict(self, value: dict):
        self.session_permissions = json.dumps(value) if value else None

    @property
    def jit_permissions_dict(self) -> dict:
        """Permissions granted by an Entitle REST integration. See the column."""
        if not self.jit_permissions:
            return {}
        try:
            return json.loads(self.jit_permissions)
        except Exception:
            return {}

    @jit_permissions_dict.setter
    def jit_permissions_dict(self, value: dict):
        self.jit_permissions = json.dumps(value) if value else None

    @property
    def effective_permissions_dict(self) -> dict:
        """Union of admin-baseline (permissions), group-derived
        (session_permissions) and Entitle-granted (jit_permissions).
        This is what require_permission() consults. Special key ``is_admin``
        (bool) is OR'd separately in is_effective_admin; everything else is
        treated as a list of levels per scope and union-merged.

        Empty dict means "no explicit permissions" → require_permission
        treats this as unrestricted (existing pre-OIDC users keep working
        the same way they did pre-Phase-0).
        """
        baseline = self.permissions_dict
        session = self.session_permissions_dict
        jit = self.jit_permissions_dict
        if not baseline and not session and not jit:
            return {}
        out: dict = {}
        for src in (baseline, session, jit):
            for key, val in src.items():
                if key == "is_admin":
                    out[key] = out.get(key, False) or bool(val)
                elif isinstance(val, list):
                    existing = out.get(key)
                    if isinstance(existing, list):
                        out[key] = sorted(set(existing) | set(val))
                    else:
                        out[key] = sorted(set(val))
                else:
                    out[key] = val
        return out

    @property
    def is_effective_admin(self) -> bool:
        """True if the persistent is_admin flag, a current session_permissions row,
        or a live Entitle grant confers admin."""
        if bool(self.is_admin):
            return True
        return (bool(self.session_permissions_dict.get("is_admin", False))
                or bool(self.jit_permissions_dict.get("is_admin", False)))


class Fido2Credential(Base):
    """FIDO2/WebAuthn credential for MFA"""
    __tablename__ = "fido2_credentials"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    credential_id = Column(LargeBinary, nullable=False, unique=True)  # raw bytes from authenticator
    public_key = Column(LargeBinary, nullable=False)  # COSE-encoded public key
    sign_count = Column(Integer, default=0, nullable=False)
    aaguid = Column(String(36))  # authenticator device type GUID (informational)
    device_name = Column(String(100))  # user-provided label e.g. "YubiKey 5C"
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    last_used_at = Column(DateTime)
    is_active = Column(Boolean, default=True)

    user = relationship("User", back_populates="fido2_credentials")


class PersonalAccessToken(Base):
    """Long-lived API tokens for machine-to-machine access (e.g. GitHub Actions)."""
    __tablename__ = "personal_access_tokens"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(100), nullable=False)           # e.g. "github-actions-prod"
    token_hash = Column(String(64), nullable=False, unique=True, index=True)  # SHA-256 hex of raw token
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    expires_at = Column(DateTime, nullable=True)         # None = never expires
    last_used_at = Column(DateTime, nullable=True)
    is_active = Column(Boolean, default=True)

    user = relationship("User", back_populates="personal_access_tokens")


class LoginAttempt(Base):
    """One FAILED password login. Successes are not recorded here — a successful login
    deletes the username's rows, so the table only ever holds the evidence of failure.

    In the database rather than in a process, because gunicorn runs two workers: an
    in-memory counter would give an attacker double the allowance and reset it on every
    redeploy. Same reasoning as the job claim and the notification outbox.

    Doubles as the only record that a brute-force attempt happened at all — before this
    existed, a failed login left no trace anywhere in the system.
    """
    __tablename__ = "login_attempts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # Case-folded by login_guard.normalize_username, so `Admin` and `admin` cannot each
    # get their own budget. Recorded whether or not the account exists — a throttle that
    # engaged only for real users would answer "does this user exist?".
    username = Column(String(150), nullable=False, index=True)
    ip = Column(String(45), nullable=False, default="", index=True)
    # Indexed because every query is a range scan on it, in both the check and the sweep.
    attempted_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)


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
    enrolled_at = Column(DateTime, nullable=True)
    last_seen_at = Column(DateTime, nullable=True, index=True)
    last_seen_ip = Column(String(45))              # 45 = max INET6_ADDRSTRLEN
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    created_by = Column(String(100))

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
    seen_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)

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
    attempted_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)


class HypervisorConnection(Base):
    """One reachable hypervisor management endpoint.

    Replaces the singleton ``proxmox_host`` / ``vsphere_host`` / … config keys, which
    made "N sites x M hypervisors" inexpressible — there was exactly one of each, so a
    second vCenter, or the same product at a second site, had nowhere to live.

    Two rows exist for the same reason a `Job` row does: something has to be the record.
    Every integration resolves one of these instead of reading ``settings``, and
    ``agent_id`` is what makes a connection the dashboard *cannot dial* still usable —
    a remote agent on that network does the talking.

    Credentials: exactly one of ``secret_enc`` (Fernet, via ``config_service``) or
    ``secret_ref`` (an external backend reference) is set.

    For an **agent-bound** connection the rule is narrower than "no credential", and the
    line is worth stating precisely: **the dashboard may hold the secret, never the
    target.** ``host`` and ``username`` are always NULL on such a row and always come
    from the agent's own connections.yaml, because those are the fields that *aim* the
    connection — a dashboard able to set ``host`` could redirect the agent's
    authenticated session at an endpoint of its choosing and harvest the credential on
    first use, and one able to set ``username`` could spray a known password across
    accounts. ``agent_connection_name`` remains the whole join.

    The secret itself is optional and opt-in. Left NULL, the agent resolves the
    credential locally and a dashboard compromise yields a verb and a name — the original
    behaviour. Set, the agent may instead fetch it just-in-time for one job over its own
    signed poll channel, sealed to a per-fetch key (``services/agent_sealing``), which is
    what lets an on-prem host hold no standing hypervisor credential at all. Which of the
    two applies is decided by ``dashboard_secret`` in the *customer's* connections.yaml,
    not here: this row can offer a credential, it cannot impose one.

    A ``secret_ref`` of ``ps_account://<id>`` means the dashboard holds no password
    either — it checks one out of Password Safe per job and checks it back in.
    """
    __tablename__ = "hypervisor_connections"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    kind = Column(String(16), nullable=False, index=True)   # vsphere|proxmox|nutanix|xcpng|hyperv
    name = Column(String(64), nullable=False)               # operator label
    host = Column(String(255), nullable=False, default="")
    port = Column(Integer)
    username = Column(String(255))

    secret_enc = Column(Text)              # Fernet ciphertext (config_service.encrypt_value)
    secret_ref = Column(String(256))       # aws_sm:// | azure_kv:// | gcp_sm:// | bt_safe://
    verify_ssl = Column(Boolean, default=False, nullable=False)
    # Per-kind NON-SECRET extras, JSON: vsphere datacenter, hyperv transport/use_ssl,
    # proxmox token_id, sync_interval_minutes. Allowlisted per kind by the service.
    options = Column(Text)

    # NULL = the dashboard dials this endpoint itself (the behaviour before this table).
    agent_id = Column(String(36), ForeignKey("remote_agents.id", ondelete="SET NULL"),
                      index=True, nullable=True)
    # The name this connection has in that agent's own connections.yaml. The dashboard
    # never learns the credential; this string is the entire join between the two.
    agent_connection_name = Column(String(64))

    # Grouping and display only — routing is the agent_id FK. A site join would mean
    # "any agent here can service this", but only the agent that actually holds the
    # credential can, so it would lease cleanly and then refuse.
    site = Column(String(64), index=True)

    is_default = Column(Boolean, default=False, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    last_ok_at = Column(DateTime)
    last_error = Column(Text)
    last_sync_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    created_by = Column(String(100))
    updated_at = Column(DateTime, default=datetime.utcnow)

    # Unique on (kind, name), NOT on name alone: "dc1" is a reasonable label for both a
    # vSphere and a Proxmox connection. Deliberately NOT unique on (kind, host, port)
    # either — two connections to one vCenter under different service accounts (a
    # read-only sync account and a privileged deploy account) is a legitimate setup.
    #
    # `is_default` is a plain boolean with a service-enforced "at most one per kind"
    # rule rather than a partial unique index: SQLite's partial-index support is not
    # something to bet the startup path on.
    __table_args__ = (UniqueConstraint("kind", "name", name="uq_hypervisor_connection_name"),)

    @property
    def options_dict(self) -> dict:
        if not self.options:
            return {}
        try:
            value = json.loads(self.options)
            return value if isinstance(value, dict) else {}
        except Exception:  # noqa: BLE001
            return {}


class HypervisorVMCache(Base):
    """VMs an agent reported for one connection.

    Identity is ``(connection_id, vm_id)`` — the hypervisor's own opaque id. It replaced
    a ``vm_state_cache`` table keyed on a VMX path *on the dashboard host*, which had no
    meaning for a VM on a customer's vCenter and none at all once the dashboard stopped
    running on the VM host.

    A read-through cache, never a source of truth. ``synced_at`` is what makes deletion
    detectable: the last page of a sync prunes rows older than the pass that started it.
    """
    __tablename__ = "hypervisor_vm_cache"

    connection_id = Column(String(36), primary_key=True)
    vm_id = Column(String(128), primary_key=True)
    name = Column(String(256))
    power_state = Column(String(32))
    vcpus = Column(Integer)
    mem_mib = Column(Integer)
    ip_addresses = Column(Text)     # JSON list
    # Text, not String(128). For a workstation connection this carries a full Windows
    # VMX path, and one longer than 128 chars raised StringDataRightTruncation inside
    # hypervisor_sync_service._upsert on PostgreSQL — which api/agent.py swallows, so an
    # entire sync page of VMs vanished with a log line and no user-visible failure.
    scope = Column(Text)            # node / cluster / datacenter, VMX path for workstation
    vm_type = Column(String(16))
    tags = Column(Text)             # JSON list
    # The hypervisor's OWN guest-OS code (`windows9-64`), not a display label. The label
    # table lives in hypervisor_view_service.guest_os_label, on the dashboard, because an
    # agent is upgraded separately and lags: a label baked into the agent would freeze at
    # whatever build the host last pulled.
    guest_os = Column(String(64))
    synced_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)


class Job(Base):
    """Job model for tracking long-running operations"""
    __tablename__ = "jobs"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    job_type = Column(String(50), nullable=False, index=True)  # 'ec2_deploy', 'agent_hypervisor', etc.
    workgroup = Column(String(50), index=True)
    vm_path = Column(Text)
    # Cloud SDK resource id (EC2 instance id, Azure VM name, GCP instance id) for
    # cloud-deploy jobs. Indexed so the reassign endpoints can find the originating
    # Job row when an admin rewrites a resource's Workgroup tag/label.
    cloud_resource_id = Column(String(255), index=True, nullable=True)
    # Groups the jobs of one bulk Config-Management run (api/config_mgmt.run_playbook_bulk
    # fans a single asset out to N targets, one job each). A column rather than a key in
    # extra_data for the same reason as cloud_resource_id above: extra_data is a Text
    # column holding a JSON string, so there is no operator that filters it portably
    # across SQLite and PostgreSQL — only a LIKE scan.
    batch_id = Column(String(32), index=True, nullable=True)
    # The enrolled remote agent that owns this row's execution, or NULL for the local
    # job runner. A real indexed column for the same reason as batch_id above: the
    # lease query is `WHERE status='queued' AND agent_id=:id` on every poll, and
    # extra_data cannot be filtered portably.
    #
    # Setting this forces status='queued' in job_service.create_job, which is what
    # keeps jobs_worker._claim_one (status='pending') from racing the agent for the row.
    agent_id = Column(String(36), ForeignKey("remote_agents.id", ondelete="SET NULL"),
                      index=True, nullable=True)
    # Auto-delete timer. Meaningful ONLY on the cloud VM deploy types
    # (expiry_policy.REAPABLE_VM_JOB_TYPES) — a VM has no inventory table of its own,
    # so its deploy Job row IS its record of existence, which is why `job:<id>` is
    # already its inventory id. NULL and ignored on every other job row.
    #
    # NULL means "no expiry, never auto-deleted" — NOT "inherit the global default".
    # Same meaning PersonalAccessToken.expires_at already carries. That is the
    # load-bearing safety property of the whole feature: every row that predates the
    # column is NULL, so enabling auto-delete on an existing fleet selects nothing,
    # by construction rather than by a guard.
    expires_at = Column(DateTime, nullable=True, index=True)
    # Set by the sweeper before it records an impending-deletion warning, so a warning
    # fires once. Read only by the server-side warning channels — deliberately NOT by
    # the dashboard's "Needs attention" item, which is derived client-side on every
    # poll and has its own per-browser dismissal.
    expiry_warned_at = Column(DateTime, nullable=True)
    status = Column(String(20), nullable=False, default="pending", index=True)  # pending, running, completed, failed, cancelled
    progress_pct = Column(Integer, default=0)
    progress_message = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    started_at = Column(DateTime)
    completed_at = Column(DateTime)
    # Heartbeat — bumped on every status/progress write (incl. streamed terraform
    # output); the startup reconcile uses it to tell a live job from one orphaned
    # by an app restart.
    updated_at = Column(DateTime)
    created_by = Column(String(100), index=True)  # Username
    error_message = Column(Text)
    extra_data = Column(Text)  # JSON string for flexible storage

    @property
    def metadata_dict(self) -> dict:
        """Parse JSON extra_data into dict"""
        if not self.extra_data:
            return {}
        try:
            return json.loads(self.extra_data)
        except:
            return {}

    @metadata_dict.setter
    def metadata_dict(self, value: dict):
        """Set extra_data from dict"""
        self.extra_data = json.dumps(value)

    @property
    def duration_seconds(self) -> Optional[int]:
        """Calculate job duration in seconds"""
        if self.started_at and self.completed_at:
            return int((self.completed_at - self.started_at).total_seconds())
        elif self.started_at:
            return int((datetime.utcnow() - self.started_at).total_seconds())
        return None


class JobLog(Base):
    """Per-line Live Output for a job, persisted so a separate worker process's
    terraform stream reaches WS clients connected to gunicorn (which poll the DB),
    and so a reconnecting client can replay the full output. Append-only; the
    dedicated job runner is the sole writer per job_id (one monotonic seq)."""
    __tablename__ = "job_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    job_id = Column(String(36), index=True, nullable=False)
    seq = Column(Integer, nullable=False)
    line = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (UniqueConstraint("job_id", "seq", name="uq_job_log_seq"),)


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
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
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
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)

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
    timestamp = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
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
    applied_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    job_id = Column(String(36))

    __table_args__ = (UniqueConstraint("target", "playbook_ref", name="uq_apply_target_playbook"),)


class OAuthGroupMapping(Base):
    """Maps an Entra ID group Object ID to a dashboard workgroup name."""
    __tablename__ = "oauth_group_mappings"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    entra_group_id = Column(String(255), unique=True, nullable=False, index=True)
    display_name = Column(String(200), nullable=False)   # friendly label shown in the UI
    workgroup = Column(String(100), nullable=False)       # must match a key in settings.workgroups
    # Default permissions for auto-created users from this group. NULL = all permissions.
    default_permissions = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class Workgroup(Base):
    """User-managed workgroup: scopes RBAC + cloud resource visibility.

    `name` is canonical lowercase (regex enforced in service layer) so it can be
    written verbatim into AWS instance tags (`Workgroup=<name>`), Azure resource
    tags (`workgroup=<name>`), and GCP labels (`workgroup=<name>`) — all of which
    have casing/character restrictions tighter than the dashboard UI.

    Lookups in `workgroup_service` are case-insensitive so existing TitleCase
    strings in `users.workgroups` and `oauth_group_mappings.workgroup` keep
    resolving without a data migration. `display_name` preserves the original
    casing for UI rendering.
    """
    __tablename__ = "workgroups"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(64), unique=True, nullable=False, index=True)
    display_name = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    local_vm_path = Column(Text, nullable=True)  # UNC path for VMware local VMs; null in community
    is_default = Column(Boolean, default=False, nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    created_by_user_id = Column(String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)


class VMWorkgroupOverride(Base):
    """Workgroup assignments for VMs the dashboard didn't deploy itself.

    Cloud and Proxmox/Nutanix dashboard-driven deploys already record
    `workgroup` on the corresponding Job row, but VMs that pre-existed on the
    on-prem hypervisor — or live on a hypervisor with no deploy flow at all
    (Hyper-V, vSphere, XCP-ng) — have no Job to hang a workgroup off of.
    An admin assigns those via the bulk-assign action on the provider page,
    which writes a row here.

    `vm_id` is normalized per-provider in each *_service module's
    _override_key() helper: Proxmox uses "<node>/<vmid>", everything else
    uses the VM's native uuid/moref.
    """
    __tablename__ = "vm_workgroup_overrides"

    id = Column(Integer, primary_key=True)
    provider = Column(String(20), nullable=False, index=True)   # proxmox|nutanix|hyperv|vsphere|xcpng
    vm_id = Column(String(128), nullable=False)
    workgroup = Column(String(64), ForeignKey("workgroups.name", ondelete="CASCADE"), nullable=False, index=True)
    created_by = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    __table_args__ = (UniqueConstraint("provider", "vm_id", name="uq_vm_workgroup_override"),)


class Approval(Base):
    """Entitle-brokered approval workflow state for gated endpoints.

    A row is created when a user calls a gated endpoint without an approval
    header; Entitle webhook moves status pending→approved/denied; the dep
    consumes the row on the user's retry call.
    """
    __tablename__ = "approvals"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    entitle_request_id = Column(String(255), nullable=False, unique=True, index=True)
    action = Column(String(100), nullable=False, index=True)
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    payload_hash = Column(String(64), nullable=False)
    # status values: pending | approved | denied | expired | consumed
    status = Column(String(20), nullable=False, default="pending", index=True)
    requested_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    approved_at = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=False)
    consumed_at = Column(DateTime, nullable=True)
    denial_reason = Column(Text, nullable=True)

    # principal_kind: "user" for human-facing approvals (the original flow),
    # "machine" for cloud-identity JIT elevations issued by the dashboard
    # on its own behalf. Webhook handler is shared; this column lets policy
    # routing fork on identity type.
    principal_kind = Column(String(16), nullable=False, default="user", index=True)

    user = relationship("User")


class EntitleActivation(Base):
    """Per-cloud-write machine-identity elevation issued via Entitle.

    Phase 0 of the cloud-identity JIT design ships this table empty. When
    ``cloud_identity_gate_enabled`` is False (default), ``cloud_identity_service``
    short-circuits and no rows are inserted. When the gate is on, every
    write-path cloud SDK call is preceded by an elevation request whose
    lifecycle is tracked here.

    Internal ``status`` values:
      - pending     — request submitted to Entitle, awaiting workflow
      - granted     — Entitle agent has finished granting cloud-side IAM;
                      dashboard may proceed to call the cloud SDK
      - denied      — Entitle workflow rejected the request (security alert)
      - failed      — cloud-side IAM call failed (operator alert)
      - revoked     — TTL elapsed or explicit revoke (terminal)
    """
    __tablename__ = "entitle_activations"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    cloud = Column(String(16), nullable=False, index=True)        # aws | azure | gcp
    operation = Column(String(64), nullable=False, index=True)    # e.g. "aws:ec2:deploy"
    role = Column(String(255), nullable=True)                     # IAM policy / role / binding granted
    requester_user_id = Column(String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    entitle_request_id = Column(String(255), nullable=True, unique=True, index=True)
    entitle_policy_id = Column(String(255), nullable=True)
    auto_approved = Column(Boolean, nullable=False, default=True)
    status = Column(String(20), nullable=False, default="pending", index=True)
    denial_reason = Column(Text, nullable=True)
    payload_hash = Column(String(64), nullable=False)
    # Which tenant the elevation belongs to. cloud_identity_service passes this
    # into the constructor unconditionally, so its absence made every elevation
    # raise TypeError the moment cloud_identity_gate_enabled was turned on.
    tenant_id = Column(String(64), nullable=True, index=True)
    requested_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    granted_at = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=True)
    revoked_at = Column(DateTime, nullable=True)


class AppConfig(Base):
    """Encrypted key-value store for cloud credentials and feature flags.

    Values are Fernet-encrypted with a key derived from JWT_SECRET_KEY so that
    secrets at rest are protected even if someone reads the DB directly.
    Written by the setup wizard; consumed by config_service.get().

    The optional `workgroup` column lets prod-style multi-tenant deployments
    have per-workgroup overrides for the same key. NULL means "global";
    config_service.get() falls back to the NULL row when no workgroup-scoped
    row exists. Community installs leave `workgroup` NULL always and behave
    as before.
    """
    __tablename__ = "app_config"

    key = Column(String(128), primary_key=True)
    value = Column(Text, nullable=True)         # Fernet-encrypted
    workgroup = Column(String(64), nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class SecretVault(Base):
    """Registry of external secret-store endpoints the dashboard can resolve
    references against.

    Used by the multi-vault reference scheme: a config row with
    ``azure_kv://<vault-id>/<secret-name>`` looks up the vault row by
    (id, backend) and routes the read to that vault's endpoint with that
    vault's credentials. When the table is empty (community / fresh install)
    the legacy ``azure_kv://<secret-name>`` shape continues to resolve via
    the singleton config_service.get('azure_kv_*') keys, so behaviour is
    unchanged until an operator registers a vault.
    """
    __tablename__ = "secret_vaults"

    id = Column(String(64), primary_key=True)              # e.g. "primary", "tenant-alpha-eu"
    backend = Column(String(32), nullable=False)           # azure_kv | aws_sm | gcp_sm | bt_secrets_safe
    endpoint = Column(Text, nullable=False)                # e.g. https://my-vault.vault.azure.net
    credentials_ref = Column(Text, nullable=True)          # optional reference to creds (e.g. distinct SP per vault)
    workgroup = Column(String(64), nullable=True)          # if set, only this workgroup resolves here
    is_default = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    created_by_user_id = Column(String(36), nullable=True)


class ContainerStateCache(Base):
    """Cache for Portainer container state to improve dashboard performance."""
    __tablename__ = "container_state_cache"

    container_id = Column(String(64), primary_key=True)
    short_id = Column(String(12))
    name = Column(String(200))
    image = Column(String(500))
    state = Column(String(50))
    status = Column(String(200))
    ports = Column(Text)                              # JSON list[str]
    endpoint_id = Column(Integer, index=True)
    endpoint_name = Column(String(200))
    workgroup = Column(String(50), index=True)
    created_ts = Column(Integer)                      # unix epoch from Docker
    last_updated = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)


class CloudCostCache(Base):
    """Last-known-good month-to-date cost for one (cloud, view), plus the throttle
    state that decides whether we are allowed to ask that cloud again.

    In the database rather than in ``services/cache_service`` for three reasons, each
    sufficient on its own. The app runs ``gunicorn -w 2``, so a process-local dict gives
    each worker its own throttle budget and its own 429 — the same hazard that put
    :class:`EphemeralState` and :class:`LoginAttempt` here. ``jobs_worker`` is a third
    process that warms nothing, so the budget-alert scan could only ever read an empty
    dict there. And Azure Cost Management rate-limits per SUBSCRIPTION, which is a
    property of the account, not of a process — so "we were just throttled" has to
    outlive a redeploy, or every image rebuild re-earns the 429.

    ``payload`` is written ONLY when a cloud returned ``status="ok"``. A failure writes
    the error and cooldown columns and leaves ``payload``/``fetched_at`` untouched. That
    asymmetry is the whole reason this table exists: a 429 must never be able to replace
    a working number. The row is therefore not a cache entry with a TTL — it is a
    last-known-good value plus an expiry *opinion*.
    """
    __tablename__ = "cloud_cost_cache"

    # PK is (cloud, view) and deliberately EXCLUDES the month: throttle state is a
    # property of the API, not of the calendar, so it has to survive a rollover.
    # `period` guards the payload instead — see cost_cache._is_usable.
    cloud = Column(String(16), primary_key=True)   # aws | azure | gcp | oci
    view = Column(String(16), primary_key=True)    # summary | breakdown

    # ── last-known-good (written only on status="ok") ────────────────────────
    payload = Column(Text, nullable=True)                      # JSON: one cloud's entry
    payload_version = Column(Integer, nullable=False, default=0)
    period = Column(String(7), nullable=True)                  # "2026-08"; MTD is month-scoped
    fetched_at = Column(DateTime, nullable=True, index=True)
    # Set by a Setup save. Read as "not fresh" while the payload is still SERVED, so
    # fixing a credential re-queries without blanking the page in the meantime.
    stale = Column(Boolean, nullable=False, default=False)

    # ── failure / throttle (written only on a failed attempt) ────────────────
    last_attempt_at = Column(DateTime, nullable=True)
    last_error = Column(Text, nullable=True)
    consecutive_failures = Column(Integer, nullable=False, default=0)
    # Hard gate: while this is in the future NOTHING queries this cloud, including an
    # explicit ?refresh=true. Set from the provider's Retry-After when it gave one.
    cooldown_until = Column(DateTime, nullable=True, index=True)

    # ── single-flight ────────────────────────────────────────────────────────
    # A liveness bound, not a mutex: a process that dies mid-fetch releases its claim by
    # expiry. The advisory lock only makes the claim's read-modify-write atomic; it is
    # never held across the network call.
    lease_until = Column(DateTime, nullable=True)
    lease_owner = Column(String(64), nullable=True)   # "host:pid" — diagnostics only
    # Per-CLOUD pacing, written to every row of the cloud: Cost Management throttles per
    # subscription, so this cloud's summary and breakdown queries must not overlap.
    next_query_allowed_at = Column(DateTime, nullable=True)

    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class WorkloadCredentialLease(Base):
    """One live Workload Credentials lease per (cloud, purpose).

    Deliberately the same shape as :class:`CloudCostCache` — read that class and
    ``services/cost_cache``'s module docstring first. Same three reasons for being in the
    database rather than in a process-local dict, plus one this table has and that one
    does not.

    The app runs ``gunicorn -w 2`` and ``jobs_worker`` is a third process, so a
    process-local dict would give each one **its own lease**. For a cost figure that is
    wasteful; here it is billable. Workload Credentials charges per credential
    *issuance*, so three processes each minting their own lease is three times the
    invoice for the same credential, and every image rebuild would throw them all away
    and re-mint. The row is what makes one issuance serve the whole deployment.

    ``payload`` is written ONLY when a generate succeeded. A failure writes the error and
    cooldown columns and leaves ``payload``/``expires_at`` untouched. Same asymmetry, same
    reason: a 429 must never be able to replace a working credential. It matters more here
    than for costs, because a blank credential does not degrade a tile — it makes the
    dashboard indistinguishable from one that was never configured for the dynamic tier.

    ``payload`` is **Fernet-encrypted** (``config_service.encrypt_value``), unlike
    ``CloudCostCache.payload``. It holds a live cloud credential, so it gets the same
    at-rest treatment as ``app_config``, and therefore the same rotation hazard: losing the
    JWT root key makes stored leases unreadable. That is survivable in a way losing
    ``app_config`` is not — the next refresh simply mints a new one.

    ``expires_at`` is **not** a TTL opinion. It is the expiry the provider issued, and the
    credential stops working at it whether or not anything here agrees. That is the
    opposite of ``CloudCostCache``, where a stale payload is still servable.

    The single-flight columns are ``claim_*`` rather than ``lease_*`` as in
    ``CloudCostCache``. The word "lease" already means the provider-issued credential lease
    throughout this feature, and having it also mean "this process's short claim on the
    right to refresh" would be genuinely confusing in the one module that handles both.
    """
    __tablename__ = "workload_credential_lease"

    # aws | azure. GCP is absent because Workload Credentials does not mint GCP
    # credentials — a GCP deployment stays on the static tier by design, not by omission.
    cloud = Column(String(16), primary_key=True)
    # provision | readonly. Splitting by lifecycle rather than by operation is what lets
    # the request path hold a read-only credential while write privilege exists only for
    # the duration of a job. See docs/integrations/workload-credentials.md.
    purpose = Column(String(32), primary_key=True)

    # ── last-known-good (written ONLY on a successful generate) ──────────────
    payload = Column(Text, nullable=True)              # Fernet-encrypted JSON of the credential
    lease_id = Column(String(128), nullable=True)      # the provider's id; needed to revoke
    issued_at = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=True, index=True)

    # ── failure (written ONLY on a failed attempt) ───────────────────────────
    last_attempt_at = Column(DateTime, nullable=True)
    last_error = Column(Text, nullable=True)
    consecutive_failures = Column(Integer, nullable=False, default=0)
    # While this is in the future nothing tries to mint for this (cloud, purpose). Guards
    # against a misconfigured dynamic secret turning every page load into a billable
    # failed attempt.
    cooldown_until = Column(DateTime, nullable=True)

    # ── single-flight ───────────────────────────────────────────────────────
    # A liveness bound, not a mutex: a process that dies mid-generate releases its claim by
    # expiry. The advisory lock only makes the claim's read-modify-write atomic and is
    # never held across the network call.
    claim_until = Column(DateTime, nullable=True)
    claim_owner = Column(String(64), nullable=True)    # "host:pid" — diagnostics only

    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class DashboardStatCache(Base):
    """One dashboard tile's last-known-good counts, plus the state that decides whether we
    are allowed to ask that provider again.

    Same three reasons as :class:`CloudCostCache`, and the same invariants — read that
    class and ``services/cost_cache``'s module docstring first, because this is
    deliberately the same shape. The app runs ``gunicorn -w 2`` and ``jobs_worker`` is a
    third process, so a process-local dict gives each one its own copy and its own idea of
    the throttle; and every image rebuild throws all of them away.

    What it buys: the dashboard home page fans out to ~33 endpoints, ~22 of them at once,
    and every one holds a pooled connection for its whole duration — against
    ``pool_size=5 + max_overflow=5``. That is the ``QueuePool limit ... reached`` failure
    the 26.8.5 mitigation could only reduce, not remove. Reading tiles from this table
    makes a page load ONE indexed query and no cloud calls at all.

    ``payload`` is written ONLY on a successful collection. A failure writes the error and
    cooldown columns and leaves ``payload``/``fetched_at`` exactly where they were. That
    asymmetry is the reason this is a table and not a cache entry with a TTL: it is a
    last-known-good value plus an expiry *opinion*.

    NO ``period`` column, unlike CloudCostCache — a VM count is not month-scoped, so a
    calendar rollover is not a miss. ``payload_version`` is the only shape gate.
    """
    __tablename__ = "dashboard_stat_cache"

    # Matches the tile `key` in templates/dashboard.html's tileSections.
    tile_key = Column(String(48), primary_key=True)
    # "" for a tile with one global source. Reserved for per-connection tiles: the
    # hypervisor routers resolve `conn_or_error(db, kind, "")` to the DEFAULT connection,
    # so an install with two Proxmox clusters has a tile describing one of them. Summing a
    # tile's scopes is how that gets fixed. It is in the PRIMARY KEY from the start
    # deliberately — adding a column later is an ALTER, but widening a primary key is not.
    scope = Column(String(64), primary_key=True, default="")

    # Which provider this tile calls, e.g. "gcp" / "proxmox" / "local". A COLUMN rather
    # than a lookup in the collector's spec table, so pacing is a self-contained
    # `UPDATE ... WHERE provider = :p` and this module needs to import nothing to do it.
    provider = Column(String(24), nullable=False, default="", index=True)

    # ── last-known-good (written ONLY on a successful collection) ────────────
    payload = Column(Text, nullable=True)          # JSON, shape owned by the collector
    payload_version = Column(Integer, nullable=False, default=0)
    fetched_at = Column(DateTime, nullable=True, index=True)
    # Set by a Setup save. Read as "not fresh" while the payload is still SERVED, so
    # fixing a credential re-collects without blanking the tile in the meantime.
    stale = Column(Boolean, nullable=False, default=False)

    # ── failure / backoff (written ONLY on a failed attempt) ─────────────────
    last_attempt_at = Column(DateTime, nullable=True)
    last_error = Column(Text, nullable=True)
    consecutive_failures = Column(Integer, nullable=False, default=0)
    # Hard gate: while this is in the future nothing collects this tile, including an
    # explicit refresh. Mashing Refresh at a saturated provider must not compound it.
    cooldown_until = Column(DateTime, nullable=True, index=True)

    # ── single-flight ────────────────────────────────────────────────────────
    # A liveness bound, not a mutex: a process that dies mid-collection releases its claim
    # by expiry. The advisory lock only makes the claim's read-modify-write atomic; it is
    # never held across the network call.
    lease_until = Column(DateTime, nullable=True)
    lease_owner = Column(String(64), nullable=True)   # "host:pid" — diagnostics only
    # Per-PROVIDER pacing, written to every tile of that provider: GCP alone owns seven
    # tiles against an 8-thread pool, so pacing per tile would let one pass hand itself
    # CloudProviderBusy.
    next_query_allowed_at = Column(DateTime, nullable=True)

    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class RegisteredImage(Base):
    """Operator-registered image artefacts. The dashboard's source-of-truth
    record for "this image exists, here's where the artefact lives, here's
    what cloud-native images derive from it." Cross-cloud promotion records
    are stored in `promotions` as JSON because each target carries a
    different shape (AMI ID + region for AWS, resource ID for Azure, full
    self_link for GCP)."""
    __tablename__ = "registered_images"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(200), nullable=False, index=True)
    version = Column(String(64), nullable=False)
    description = Column(Text, nullable=True)

    # Where the image was first built / registered.
    source_cloud = Column(String(20), nullable=False)        # "aws" | "azure" | "gcp"
    source_image_id = Column(String(500), nullable=True)     # AMI / managed image / custom image ID
    source_region = Column(String(64), nullable=True)        # AWS region / Azure location / GCP region

    # Optional storage URL for the portable artefact (e.g. s3://bucket/key,
    # https://acct.blob.core.windows.net/c/k, gs://bucket/key). Lets the
    # promote flow re-import without re-running Packer.
    artefact_url = Column(String(1000), nullable=True)
    artefact_format = Column(String(20), nullable=True)      # "vhd" | "raw" | "vmdk" | "ova"
    # Guest OS of the artefact ("Linux" | "Windows"). Promote targets need it —
    # Azure managed-image import rejects/boots wrong with a mismatched os_type.
    os_type = Column(String(20), nullable=True)

    # Per-target promotion records. Shape:
    #   { "aws":   {"image_id": "ami-…", "region": "us-east-2", "status": "completed"|"manual"|"failed", "notes": "..."},
    #     "azure": {"image_id": "/subscriptions/.../images/…", "status": ...},
    #     "gcp":   {"self_link": "...", "status": ...} }
    promotions = Column(Text, nullable=True)                 # JSON

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    created_by = Column(String(100), nullable=False)

    @property
    def promotions_dict(self) -> dict:
        if not self.promotions:
            return {}
        try:
            import json
            return json.loads(self.promotions)
        except Exception:
            return {}


class VirtualDesktop(Base):
    """A single virtual-desktop seat in a dashboard-managed desktop pool.

    Phase 0 of the virtual-desktop plan ships this table empty; the
    vdesktop_service scaffold writes/reads rows but does no cloud provisioning
    yet. Phase 1 fans pool creation out to the existing VM provisioning path
    (one VM per seat, tagged dashboard:desktop_pool=<name>) and fills
    vm_resource_id; Phase 2 registers each seat on the PRA Jumpoint and fills
    pra_jump_id. One row per desktop (seat), not per pool.
    """
    __tablename__ = "virtual_desktops"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    cloud = Column(String(20), nullable=False)              # aws | azure | gcp
    pool_name = Column(String(200), nullable=False, index=True)
    # Backing kind: vm_pool (Phase 1) | avd | workspaces (Phase 4).
    kind = Column(String(20), nullable=False, default="vm_pool")
    # Cloud-native id of the backing VM once provisioned (Phase 1). Null until then.
    vm_resource_id = Column(String(500), nullable=True)
    # pending | running | stopped | deprovisioning
    status = Column(String(20), nullable=False, default="pending", index=True)
    assigned_user = Column(String(200), nullable=True)
    # PRA Jumpoint registration id once the seat is brokered (Phase 2).
    pra_jump_id = Column(String(200), nullable=True)
    # Scrubbed Terraform state for the seat's PRA RDP jump item (+ vault account)
    # so teardown can destroy them deterministically (Phase 2). Secret values are
    # redacted before storage; never returned by the API.
    pra_tunnel_state = Column(Text, nullable=True)
    created_by = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class CloudDatabase(Base):
    """Inventory of dashboard-provisioned managed databases — cloud-database
    infrastructure, Phase 1.

    One row per provisioned database (Postgres / MySQL / SQL Server), always
    **private** and reached only through a BeyondTrust PRA tunnel. In the
    community edition the PRA tunnel (Phase 2) is brokered with the
    ``beyondtrust/sra`` Terraform provider (``terraform_pra_service``) — never
    ``btapi`` — so MongoDB is not offered until the provider ships a resource.
    The PRA / Password-Safe fields are populated by later phases:
    ``jump_item_id`` by the tunnel brokering (Phase 2); ``ps_*`` are unused in
    community (Password-Safe onboarding is a prod-only path).
    """
    __tablename__ = "cloud_databases"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    engine = Column(String(20), nullable=False)            # postgres | mysql | sqlserver
    provider = Column(String(40), nullable=True)           # e.g. rds | azure_flexible | cloud_sql
    cloud = Column(String(20), nullable=False)             # aws | azure | gcp | local
    region = Column(String(64), nullable=True)
    # Mirrors K8sCluster.source. A `registered` row is a database that already existed —
    # on-prem (cloud='local') or one the dashboard didn't provision — recorded so it can
    # be a Config Management target. It has no Terraform state and no provisioning job,
    # so delete deregisters it rather than destroying it, and its admin credential comes
    # from a Password Safe managed account instead of the job's tf_variables.
    source = Column(String(16), nullable=False, default="provisioned")  # provisioned | registered

    instance_id = Column(String(255), nullable=True)       # cloud resource id (filled on apply)
    private_host = Column(String(255), nullable=True)      # private endpoint host (no public endpoint)
    port = Column(Integer, nullable=True)
    # The database catalog on this instance. Written on BOTH paths: a registered row
    # records the operator's entry, and a provisioned one mirrors the `db_name` handed
    # to Terraform (cloud_database_service.provision), so neither the Databases page
    # nor the Entitle cloud-function adapter depends on the provisioning job surviving.
    # Pre-existing provisioned rows are filled by backfill_provisioned_db_names.
    #
    # NULL means no user database exists to name: RDS SQL Server rejects db_name at
    # creation, and a registered row may legitimately leave it blank. The SQL Server
    # -> `master` substitution is applied at READ time by
    # cloud_database_service.connection_db_name and never stored — on Azure/GCP a real
    # catalog exists alongside master, and the adapter's FN_DB_NAME must name that
    # catalog or nothing at all.
    db_name = Column(String(128), nullable=True)
    status = Column(String(32), nullable=False, default="provisioning", index=True)

    credentials_ref = Column(Text, nullable=True)          # backend-agnostic ref (resolved via config_service)
    jump_item_id = Column(String(64), nullable=True)       # PRA protocol-tunnel jump (Phase 2)
    # Per-DB PRA broker overrides — config defaults are the fallback.
    jump_group = Column(String(128), nullable=True)        # PRA Jump Group name override (else bt_jump_group_name)
    jumpoint_name = Column(String(128), nullable=True)     # PRA Jumpoint name override (else bt_jumpoint_name)
    pra_credential_ref = Column(String(256), nullable=True)  # secret ref → bt_client_secret override
    entitle_integration_id = Column(String(64), nullable=True)  # Entitle DB integration registered on apply

    # Which remote agent can reach this database, for a Config-Management run. NULL = the
    # dashboard runs it itself, which is the behaviour before this column and the only one
    # available to a provisioned cloud database.
    #
    # Only meaningful on a `registered` row with cloud='local'. Such a database sits on the
    # corporate LAN, so the run used to require a sibling container on the DASHBOARD host —
    # which a cloud-hosted dashboard (ECS / ACI / Container Apps) does not have and could
    # not route from anyway. Naming an agent moves that one-shot container onto a host that
    # does have a route. Unlike HypervisorConnection this row keeps `private_host`: the
    # address is not a credential-aiming risk here, because the operator registered it and
    # the agent re-checks it against its own policy.yaml before connecting.
    agent_id = Column(String(36), ForeignKey("remote_agents.id", ondelete="SET NULL"),
                      index=True, nullable=True)

    created_by = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    # Auto-delete timer — NULL = never (see Job.expires_at). Only ever stamped on a
    # `provisioned` row: "deleting" a registered database merely deregisters it, and a
    # timer that silently forgets somebody's registered production database is worse
    # than no timer at all.
    expires_at = Column(DateTime, nullable=True, index=True)
    expiry_warned_at = Column(DateTime, nullable=True)


class CloudFunction(Base):
    """Inventory of dashboard-deployed cloud functions — Cloud Functions, Phase 1
    (docs/design/cloud-functions.md).

    One row per deployed function: an AWS Lambda, an Azure Linux Function App, or
    a GCP Cloud Run function, all running the same dashboard-authored handler from
    ``web_dashboard/functions/``. Unlike every other compute the dashboard drives,
    this one has a **stable inbound HTTPS endpoint**, which is what an Entitle REST
    integration needs in order to POST Give/Revoke Access (Phase 2).

    Two auth references, because the auth is layered and the halves are
    independent: ``invoke_secret_ref`` is the shared bearer secret the handler
    itself verifies (always set), and ``invoke_key_ref`` is the cloud's own front
    door where it produces a retrievable key (Azure host key only — AWS uses SigV4
    and GCP uses an OIDC token, neither of which is a stored credential).
    """
    __tablename__ = "cloud_functions"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(200), nullable=False, index=True)
    workload = Column(String(64), nullable=False)          # fnworkloads/<workload>.py
    cloud = Column(String(20), nullable=False)             # aws | azure | gcp
    region = Column(String(64), nullable=True)
    provider = Column(String(40), nullable=True)           # lambda | function_app | cloudrun_function
    runtime = Column(String(20), nullable=True)            # cloud-specific spelling of python 3.12
    status = Column(String(32), nullable=False, default="deploying", index=True)

    resource_id = Column(String(255), nullable=True)       # ARN / Azure resource id / cloudfunctions2 id
    invoke_url = Column(String(500), nullable=True)        # the endpoint Entitle posts to
    package_sha256 = Column(String(64), nullable=True)     # deployed artifact hash — drives update detection
    package_uri = Column(String(500), nullable=True)       # s3:// | https://…blob… | gs://

    auth_mode = Column(String(32), nullable=True)          # AWS_IAM | NONE | function_key | run_invoker | none
    invoke_secret_ref = Column(Text, nullable=True)        # config://cloudfn/{id}/bearer  (ALWAYS set)
    invoke_key_ref = Column(Text, nullable=True)           # config://cloudfn/{id}/invoke-key (Azure only)

    network_mode = Column(String(16), nullable=False, default="public")  # public | vpc | vnet
    network_ref = Column(Text, nullable=True)              # JSON: subnet ids / connector / security groups
    env_ref = Column(Text, nullable=True)                  # JSON of the NON-secret env applied

    # Where the handler source came from, as JSON: the source-tree hash (always
    # present), plus git commit/ref/origin when the image was built with them.
    # Recorded at deploy so "what is running in that function, and who reviewed it?"
    # stays answerable after the image that produced it is gone.
    provenance = Column(Text, nullable=True)

    # Stored on the row (as K8sCluster does) rather than re-derived by scanning Job
    # metadata: it is a direct lookup and it survives job pruning.
    deploy_job_id = Column(String(36), nullable=True)
    entitle_integration_id = Column(String(64), nullable=True)  # Phase 2

    created_by = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=True)


class K8sCluster(Base):
    """Inventory of dashboard-managed Kubernetes clusters — Kubernetes
    management (docs/saas-kubernetes-management-plan.md).

    One row per managed cluster. **Phase 1** records a cluster the dashboard
    can reach (provisioned out-of-band or registered from an existing
    kubeconfig) — lifecycle + kubeconfig-as-reference only, no kubectl
    wrapping. The kubeconfig is **cluster-admin**, so it's written to a
    secrets backend and only ``kubeconfig_ref`` is stored — resolved by
    ``config_service.get()``. Later phases fill ``mgmt_kind`` /
    ``mgmt_endpoint`` (management-plane launch, Phase 2), ``pra_jump_id`` (the
    native ``tunnel_type=k8s`` PRA jump, Phase 3) and ``secrets_delivery_kind``
    (in-cluster Password Safe ESO / Secrets-Agent, Phase 4).
    """
    __tablename__ = "k8s_clusters"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    cloud = Column(String(20), nullable=False)             # aws | azure | gcp | local
    name = Column(String(200), nullable=False, index=True)
    status = Column(String(32), nullable=False, default="registered", index=True)
    # §1.1a provisioning: source distinguishes a registered cluster (kubeconfig
    # only — deregister on delete) from a dashboard-provisioned one (terraform
    # destroy on delete); deploy_job_id locates its terraform/deployments/<id> state.
    source = Column(String(16), nullable=False, default="registered")  # registered | provisioned
    region = Column(String(40), nullable=True)             # cloud region (provisioned clusters)

    api_server = Column(String(255), nullable=True)        # cluster API URL (parsed from kubeconfig)
    kubeconfig_ref = Column(Text, nullable=True)           # backend-agnostic ref (resolved via config_service)
    deploy_job_id = Column(String(36), nullable=True)      # provisioning Job id → deploy/state dir (§1.1a destroy)
    # NAT/outbound public IP of a dashboard-PROVISIONED cluster (from the module's
    # nat_public_ip output). Auto-added to the Rancher node firewall as a /32 so the
    # cluster's cattle-cluster-agent can dial out to import. NULL for registered clusters.
    egress_ip = Column(String(45), nullable=True)

    mgmt_kind = Column(String(20), nullable=True)          # portainer | rancher | argocd | headlamp (Phase 2)
    mgmt_endpoint = Column(String(255), nullable=True)     # management-plane URL (Phase 2)
    pra_jump_id = Column(String(64), nullable=True)        # sra_protocol_tunnel_jump id (tunnel_type=k8s, Phase 3b)
    pra_tunnel_state = Column(Text, nullable=True)         # scrubbed Terraform state for the tunnel (drives teardown)
    # Per-cluster broker overrides — config defaults are the fallback (Phase 3b).
    jump_group = Column(String(128), nullable=True)        # PRA Jump Group name override (else bt_jump_group_name)
    jumpoint_name = Column(String(128), nullable=True)     # PRA Jumpoint name override (else bt_jumpoint_name) — the "separate jumpoint"
    pra_credential_ref = Column(String(256), nullable=True)  # secret ref → bt_client_secret override (else config)
    secrets_delivery_kind = Column(String(20), nullable=True)  # eso | secrets_agent (Phase 4)
    # Password-Safe-managed ServiceAccount token rotation. ps_token_account_id being
    # SET is the discriminator that stops the dashboard minting a second token: the
    # rotation plugin sweeps only Secrets carrying ITS labels, so a dashboard-minted
    # Secret alongside a managed one is a cluster-admin credential nothing ever rotates.
    ps_token_account_id = Column(String(64), nullable=True)      # PS ManagedAccount id of <ns>/<sa>
    ps_pra_vault_account_id = Column(String(64), nullable=True)  # PS ManagedAccount id of the "PRA Vault Token" mirror
    pra_vault_account_id = Column(String(64), nullable=True)     # sra_vault_token_account id the rotation is mirrored into

    created_by = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    # Auto-delete timer — NULL = never (see Job.expires_at). Only ever stamped on a
    # `provisioned` row, for the same reason as CloudDatabase.expires_at: deleting a
    # registered cluster only drops the dashboard's record of it.
    expires_at = Column(DateTime, nullable=True, index=True)
    expiry_warned_at = Column(DateTime, nullable=True)


class Gateway(Base):
    """Inventory of dashboard-deployed BeyondTrust Gateway hosts.

    Until now a gateway was found, not tracked: the dashboard auto-ensured exactly
    one per cloud by a fixed name tag and reference-counted it against the resources
    using it. That answers "is there a gateway?" but not "what gateways do we have",
    which is the question an operator running several for session load actually has.

    Two kinds of row, distinguished by ``managed``:

      * ``managed=True`` — the auto-ensured shared gateway. Still reference-counted
        and still torn down when idle; the row is a record of it, not the control.
        Adopted on first ensure, so a gateway that already exists gets registered
        rather than duplicated.
      * ``managed=False`` — deployed on request from the Gateways tab. Never
        reference-counted, never auto-torn-down, no cap: three in us-central1 and
        two in us-east-2 is a normal configuration. Removed only when asked.

    ``name`` is the cloud resource name (EC2 ``Name`` tag / GCE instance / Azure VM)
    and is what keeps the two kinds apart in the cloud itself — the managed teardown
    acts on the managed name alone, so it can never reach a user-deployed host.
    """
    __tablename__ = "gateways"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    cloud = Column(String(20), nullable=False, index=True)   # aws | azure | gcp
    region = Column(String(40), nullable=True, index=True)
    zone = Column(String(40), nullable=True)                 # GCP/Azure placement
    name = Column(String(200), nullable=False, index=True)   # cloud resource name
    status = Column(String(32), nullable=False, default="provisioning", index=True)
    # provisioning | running | error | deleting | deleted
    # …plus two the reconcile pass writes from what it observed rather than from what the
    # dashboard did (see gateway_service.reconcile):
    #   degraded — the host is up but no gateway task is running on it (AWS only, the one
    #              cloud where host and gateway are separate facts behind separate APIs)
    #   missing  — a REQUESTED gateway whose host is gone without anything having asked.
    #              A managed one goes to `deleted` instead: its host coming and going with
    #              demand is the normal life of that lifecycle, not an anomaly to report.
    managed = Column(Boolean, nullable=False, default=False, index=True)

    host_id = Column(String(128), nullable=True)             # EC2 instance id / VM name
    egress_ip = Column(String(45), nullable=True)            # what a node firewall allows
    deploy_job_id = Column(String(36), nullable=True)
    error = Column(Text, nullable=True)

    created_by = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


# ========== DATABASE UTILITIES ==========

def get_db() -> Session:
    """
    Dependency for FastAPI endpoints to get database session.
    Usage: def my_endpoint(db: Session = Depends(get_db))
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """Initialize database — create all tables and run lightweight migrations.

    On PostgreSQL, multiple Gunicorn workers start concurrently and both call
    init_db().  A session-level advisory lock serializes them so only one worker
    runs the DDL at a time; the second worker proceeds after the first commits,
    at which point create_all's checkfirst logic skips existing tables.

    On PostgreSQL, a failed ALTER TABLE aborts the enclosing transaction — use
    savepoints per migration so a "column already exists" error doesn't prevent
    subsequent migrations from running.
    """
    with engine.connect() as conn:
        if not _is_sqlite:
            # Transaction-scoped lock: serializes concurrent init_db callers (the
            # app's Gunicorn workers AND the jobs_worker container) and releases
            # when this transaction commits below. A *session*-level
            # pg_advisory_lock leaks here: QueuePool keeps the connection open
            # after this block, so the lock would be held for the life of the
            # process and wedge every other caller (seen as app workers blocked
            # forever acquiring 20260101 once the jobs_worker held it).
            conn.execute(text("SELECT pg_advisory_xact_lock(20260101)"))

        # Pass the connection so create_all runs inside the same transaction
        # (and the same advisory-lock session on PostgreSQL).
        Base.metadata.create_all(bind=conn)

        _migrations = [
            "ALTER TABLE users ADD COLUMN is_admin BOOLEAN DEFAULT 0",
            "ALTER TABLE users ADD COLUMN permissions TEXT",
            "ALTER TABLE users ADD COLUMN session_permissions TEXT",
            "ALTER TABLE oauth_group_mappings ADD COLUMN default_permissions TEXT",
            "ALTER TABLE jobs ADD COLUMN cloud_resource_id VARCHAR(255)",
            "ALTER TABLE hypervisor_vm_cache ADD COLUMN guest_os VARCHAR(64)",
            # A workstation VMX path outgrows VARCHAR(128); see HypervisorVMCache.scope.
            # A syntax error on SQLite, which the per-statement guard below swallows —
            # and harmless there, because SQLite does not enforce VARCHAR lengths.
            "ALTER TABLE hypervisor_vm_cache ALTER COLUMN scope TYPE TEXT",
            "CREATE INDEX ix_jobs_cloud_resource_id ON jobs(cloud_resource_id)",
            "ALTER TABLE app_config ADD COLUMN workgroup VARCHAR(64)",
            "CREATE INDEX ix_app_config_key_workgroup ON app_config(key, workgroup)",
            "ALTER TABLE approvals ADD COLUMN principal_kind VARCHAR(16) DEFAULT 'user' NOT NULL",
            "CREATE INDEX ix_approvals_principal_kind ON approvals(principal_kind)",
            # VDI Phase 1: seats track who/when for newest-first scale-down + listing.
            "ALTER TABLE virtual_desktops ADD COLUMN created_by VARCHAR(100)",
            "ALTER TABLE virtual_desktops ADD COLUMN created_at TIMESTAMP",
            # VDI Phase 2: scrubbed TF state for the seat's PRA RDP jump + vault account.
            "ALTER TABLE virtual_desktops ADD COLUMN pra_tunnel_state TEXT",
            # Windows image builds: registry rows carry the guest OS so promotes
            # don't default Windows VHDs to Linux managed images.
            "ALTER TABLE registered_images ADD COLUMN os_type VARCHAR(20)",
            # K8s management Phase 3b — sra tunnel_type=k8s jump + per-cluster
            # broker overrides (config defaults as fallback).
            "ALTER TABLE k8s_clusters ADD COLUMN pra_tunnel_state TEXT",
            "ALTER TABLE k8s_clusters ADD COLUMN jump_group VARCHAR(128)",
            "ALTER TABLE k8s_clusters ADD COLUMN jumpoint_name VARCHAR(128)",
            "ALTER TABLE k8s_clusters ADD COLUMN pra_credential_ref VARCHAR(256)",
            # K8s management §1.1a — cluster provisioning: source (registered|
            # provisioned) + region + the deploy job id that locates the Terraform
            # state dir, so delete knows whether to destroy or just drop the record.
            "ALTER TABLE k8s_clusters ADD COLUMN source VARCHAR(16) DEFAULT 'registered'",
            "ALTER TABLE k8s_clusters ADD COLUMN deploy_job_id VARCHAR(36)",
            "ALTER TABLE k8s_clusters ADD COLUMN region VARCHAR(40)",
            # Rancher firewall automation: NAT egress IP of a provisioned cluster,
            # auto-added to the Rancher node firewall as a /32.
            "ALTER TABLE k8s_clusters ADD COLUMN egress_ip VARCHAR(45)",
            # Job heartbeat — drives the startup reconcile of restart-orphaned jobs.
            "ALTER TABLE jobs ADD COLUMN updated_at TIMESTAMP",
            # Registered vs dashboard-provisioned databases. Every pre-existing row was
            # provisioned by Terraform, so that is the backfill value — the opposite of
            # k8s_clusters, whose rows all predate provisioning and default to
            # 'registered'.
            "ALTER TABLE cloud_databases ADD COLUMN source VARCHAR(16) DEFAULT 'provisioned'",
            "ALTER TABLE cloud_databases ADD COLUMN db_name VARCHAR(128)",
            # Cloud-db per-DB PRA broker overrides (config defaults as fallback).
            "ALTER TABLE cloud_databases ADD COLUMN jump_group VARCHAR(128)",
            "ALTER TABLE cloud_databases ADD COLUMN jumpoint_name VARCHAR(128)",
            "ALTER TABLE cloud_databases ADD COLUMN pra_credential_ref VARCHAR(256)",
            "ALTER TABLE cloud_databases ADD COLUMN entitle_integration_id VARCHAR(64)",
            # Tamper-evident audit log: hash-chain columns + unique seq. Existing
            # rows are chained by the one-time backfill in init_db (below).
            "ALTER TABLE audit_log ADD COLUMN seq INTEGER",
            "ALTER TABLE audit_log ADD COLUMN prev_hash VARCHAR(64)",
            "ALTER TABLE audit_log ADD COLUMN entry_hash VARCHAR(64)",
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_audit_log_seq ON audit_log(seq)",
            # cloud_identity_service._new_activation_row has always passed
            # tenant_id=; without the column every elevation raised TypeError.
            "ALTER TABLE entitle_activations ADD COLUMN tenant_id VARCHAR(64)",
            # Entitle REST-granted permissions. Separate from session_permissions
            # because the OIDC login path overwrites that column on every login.
            "ALTER TABLE users ADD COLUMN jit_permissions TEXT",
            # Cloud Functions build provenance (source tree hash + git commit).
            "ALTER TABLE cloud_functions ADD COLUMN provenance TEXT",
            # Bulk Config-Management runs: group the N jobs of one run so the jobs
            # page can filter to a batch and roll up its status.
            "ALTER TABLE jobs ADD COLUMN batch_id VARCHAR(32)",
            "CREATE INDEX ix_jobs_batch_id ON jobs(batch_id)",
            # Auto-delete timer (resource expiry). Real indexed columns rather than
            # keys in extra_data, for exactly the reason given on batch_id above:
            # extra_data is a Text column holding a JSON string, so no operator
            # filters it portably across SQLite and PostgreSQL — and the sweeper's
            # whole job is `WHERE expires_at <= :cutoff`.
            #
            # Every pre-existing row backfills to NULL, which means "never expires".
            # That is deliberate and load-bearing: turning the feature on cannot
            # select a single resource that already existed.
            "ALTER TABLE jobs ADD COLUMN expires_at TIMESTAMP",
            "ALTER TABLE jobs ADD COLUMN expiry_warned_at TIMESTAMP",
            "CREATE INDEX ix_jobs_expires_at ON jobs(expires_at)",
            "ALTER TABLE cloud_databases ADD COLUMN expires_at TIMESTAMP",
            "ALTER TABLE cloud_databases ADD COLUMN expiry_warned_at TIMESTAMP",
            # Which remote agent can reach a cloud='local' (on-prem) registered database
            # for a Config-Management run. NULL on every existing row, which is the right
            # answer — none of them had an agent, and the dashboard-local runner is still
            # what they get. No FK in the raw DDL, matching every entry here.
            "ALTER TABLE cloud_databases ADD COLUMN agent_id VARCHAR(36)",
            "CREATE INDEX ix_cloud_databases_agent_id ON cloud_databases(agent_id)",
            "CREATE INDEX ix_cloud_databases_expires_at ON cloud_databases(expires_at)",
            "ALTER TABLE k8s_clusters ADD COLUMN expires_at TIMESTAMP",
            "ALTER TABLE k8s_clusters ADD COLUMN expiry_warned_at TIMESTAMP",
            "CREATE INDEX ix_k8s_clusters_expires_at ON k8s_clusters(expires_at)",
            # Remote on-prem agent: which enrolled agent owns this job's execution.
            # The `remote_agents` and `agent_nonces` tables need no entry here —
            # create_all makes new tables; only new columns on existing tables do.
            #
            # No FK in the raw DDL, matching every entry above: SQLite cannot add a
            # constraint to an existing table, and nothing reads jobs through one. The
            # relationship is declared on the model for the ORM's benefit only.
            "ALTER TABLE jobs ADD COLUMN agent_id VARCHAR(36)",
            "CREATE INDEX ix_jobs_agent_id ON jobs(agent_id)",
            # What the agent reports it can run, refreshed on every lease. Existing rows
            # read NULL until their agent next polls, which is the correct answer — the
            # dashboard genuinely does not know yet.
            "ALTER TABLE remote_agents ADD COLUMN reported_job_types TEXT",
            # `hypervisor_connections` needs no entry: create_all makes new tables.
            # Nor does `ephemeral_state` (FIDO2 challenges + OAuth/OIDC CSRF state),
            # for the same reason. Nothing backfills that one either — every row it
            # will ever hold expires within five minutes of being written.
            #
            # Password-Safe-managed k8s ServiceAccount token rotation. Two Password Safe
            # managed-account ids (the token account, and the "PRA Vault Token" mirror the
            # rotation is pushed through), plus the PRA Vault account id — which the tunnel
            # registration already computed and threw away, so only pra_tunnel_state held it.
            # Columns rather than config keys because ps_token_account_id is a WHERE clause:
            # the sync sweep selects the registered clusters, and the mint path checks it on
            # every tunnel register. NULL backfills to "not Password Safe managed", which is
            # the right answer — enabling the feature cannot capture a cluster nobody bound.
            "ALTER TABLE k8s_clusters ADD COLUMN ps_token_account_id VARCHAR(64)",
            "ALTER TABLE k8s_clusters ADD COLUMN ps_pra_vault_account_id VARCHAR(64)",
            "ALTER TABLE k8s_clusters ADD COLUMN pra_vault_account_id VARCHAR(64)",
            "CREATE INDEX ix_k8s_clusters_ps_token_account_id "
            "ON k8s_clusters(ps_token_account_id)",
            # `cloud_cost_cache` needs no entry: create_all makes new tables. Nothing
            # backfills it either — an empty table is exactly "no cloud has reported a
            # cost yet", which is what the first warmer pass fixes.
            # `dashboard_stat_cache` needs no entry for the same two reasons: create_all
            # makes it, and an empty table means "nothing collected yet", which every tile
            # already renders as unavailable.
        ]
        for stmt in _migrations:
            if _is_sqlite:
                try:
                    conn.execute(text(stmt))
                    conn.commit()
                except Exception:
                    pass  # column already present
            else:
                # Use a savepoint per statement: on failure PostgreSQL puts the
                # transaction into an aborted state; rolling back to the savepoint
                # recovers it so the remaining migrations can still run.
                conn.execute(text("SAVEPOINT _mig"))
                try:
                    conn.execute(text(stmt))
                    conn.execute(text("RELEASE SAVEPOINT _mig"))
                except Exception:
                    conn.execute(text("ROLLBACK TO SAVEPOINT _mig"))

        if not _is_sqlite:
            conn.commit()  # ends the txn → releases pg_advisory_xact_lock(20260101)

# Seed workgroups table on first boot. Imported here (not at module top)
    # to avoid a circular import: workgroup_service imports from database.
    from .services import workgroup_service
    with SessionLocal() as _seed_db:
        workgroup_service.seed_if_empty(_seed_db)
        # Copy the legacy singleton hypervisor config into hypervisor_connections.
        # Here and not in the migration block above on purpose: this is a data seed,
        # not DDL, and it must stay OUTSIDE the advisory-locked transaction — a
        # session-level lock around it is the QueuePool leak that hung cold
        # app+worker co-deploys. The unique constraint arbitrates the race instead.
        try:
            from .services import hypervisor_connection_service
            hypervisor_connection_service.seed_from_settings(_seed_db)
        except Exception:  # noqa: BLE001 — a seed must never stop the app booting
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "hypervisor connection seed skipped", exc_info=True)
        # Translate the retired `beyondtrust_enabled` flag into the three product flags
        # that replaced it. Writes app_config only, so it takes no db session — it lives
        # here because this is the block that runs after the schema is ready and outside
        # the advisory lock.
        try:
            from .services import feature_flag_migration
            feature_flag_migration.seed_beyondtrust_split()
        except Exception:  # noqa: BLE001 — a seed must never stop the app booting
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "BeyondTrust flag split seed skipped", exc_info=True)

    # One-time: chain any pre-existing (pre-upgrade) audit rows so the whole
    # history is tamper-evident, not just entries written after this upgrade.
    # Guarded + advisory-locked inside the service; a no-op once done.
    from .services import job_service
    with SessionLocal() as _audit_db:
        try:
            n = job_service.backfill_audit_chain(_audit_db)
            if n:
                print(f"Audit chain: backfilled {n} pre-existing entr{'y' if n == 1 else 'ies'}.")
        except Exception as e:  # never block startup on backfill
            print(f"Audit chain backfill skipped: {e}")

    # One-time: copy each provisioned cloud database's catalog out of its provisioning
    # job onto the row itself. Here rather than in the migration block above for the
    # reason spelled out at the hypervisor seed: this is data, not DDL, and it must stay
    # OUTSIDE the advisory-locked transaction. Convergent, so it takes no lock of its own.
    with SessionLocal() as _dbname_db:
        try:
            # Inside the try, unlike the audit chain above: cloud_database_service pulls
            # in terraform + region_config, and a heavy import that fails must not be
            # the thing that stops the app booting either.
            from .services import cloud_database_service as _clouddb
            n = _clouddb.backfill_provisioned_db_names(_dbname_db)
            if n:
                print(f"Cloud databases: backfilled {n} database name(s).")
        except Exception as e:  # never block startup on backfill
            print(f"Cloud database name backfill skipped: {e}")

    print("Database initialized successfully!")


def create_admin_user(username: str, password: str, workgroups: List[str] = None) -> User:
    """
    Create an admin user with access to all workgroups.

    Args:
        username: Admin username
        password: Plain text password (will be hashed)
        workgroups: List of workgroups to grant access to (default: all)

    Returns:
        Created User object
    """
    if workgroups is None:
        workgroups = list(settings.workgroups.keys())

    db = SessionLocal()
    try:
        # Check if user already exists
        existing_user = db.query(User).filter(User.username == username).first()
        if existing_user:
            print(f"User '{username}' already exists!")
            return existing_user

        # Create new user
        user = User(
            username=username,
            hashed_password=get_password_hash(password),
            full_name="Administrator",
            is_active=True,
            is_admin=True,
        )
        user.workgroups_list = workgroups

        db.add(user)
        db.commit()
        db.refresh(user)

        print(f"Admin user '{username}' created successfully with access to: {', '.join(workgroups)}")
        return user
    finally:
        db.close()


def verify_password(plain_password: str, hashed_password: Optional[str]) -> bool:
    """Verify password against bcrypt hash. Returns False for OAuth-only users (no hash)."""
    if not hashed_password:
        return False
    return _bcrypt.checkpw(
        plain_password.encode("utf-8"),
        hashed_password.encode("utf-8") if isinstance(hashed_password, str) else hashed_password,
    )


def get_password_hash(password: str) -> str:
    """Hash a password with bcrypt"""
    return _bcrypt.hashpw(password.encode("utf-8"), _bcrypt.gensalt()).decode("utf-8")


# ========== CLI UTILITIES ==========

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage:")
        print("  python database.py init                          # Initialize database")
        print("  python database.py create-user <username> <password> [workgroups]")
        sys.exit(1)

    command = sys.argv[1]

    if command == "init":
        init_db()
    elif command == "create-user":
        if len(sys.argv) < 4:
            print("Usage: python database.py create-user <username> <password> [Hydra,Weaverlab]")
            sys.exit(1)

        username = sys.argv[2]
        password = sys.argv[3]
        workgroups = sys.argv[4].split(",") if len(sys.argv) > 4 else None

        create_admin_user(username, password, workgroups)
    else:
        print(f"Unknown command: {command}")
        sys.exit(1)
