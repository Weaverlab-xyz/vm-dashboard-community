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
# For SQLite we use NullPool: connections are just file handles so pooling
# adds no benefit, and the default QueuePool (5 + 10 overflow = 15 max) is
# exhausted when several long-running background jobs hold sessions open
# simultaneously (each job + each broadcast_progress call takes one slot).
# NullPool creates and closes a fresh connection on every Session open/close,
# eliminating the timeout entirely.
_is_sqlite = "sqlite" in settings.database_url
engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False} if _is_sqlite else {},
    poolclass=NullPool if _is_sqlite else QueuePool,
    echo=False,
)

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
    def effective_permissions_dict(self) -> dict:
        """Union of admin-baseline (permissions) and group-derived
        (session_permissions). This is what require_permission()
        consults. Special key ``is_admin`` (bool) is OR'd separately
        in is_effective_admin; everything else is treated as a list
        of levels per scope and union-merged.

        Empty dict means "no explicit permissions" → require_permission
        treats this as unrestricted (existing pre-OIDC users keep working
        the same way they did pre-Phase-0).
        """
        baseline = self.permissions_dict
        session = self.session_permissions_dict
        if not baseline and not session:
            return {}
        out: dict = {}
        for src in (baseline, session):
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
        """True if either the persistent is_admin flag OR a current
        session_permissions row grants admin."""
        if bool(self.is_admin):
            return True
        return bool(self.session_permissions_dict.get("is_admin", False))


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


class Job(Base):
    """Job model for tracking long-running operations"""
    __tablename__ = "jobs"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    job_type = Column(String(50), nullable=False, index=True)  # 'vm_start', 'vm_stop', 'bulk_start', etc.
    workgroup = Column(String(50), index=True)
    vm_path = Column(Text)
    # Cloud SDK resource id (EC2 instance id, Azure VM name, GCP instance id) for
    # cloud-deploy jobs. Indexed so the reassign endpoints can find the originating
    # Job row when an admin rewrites a resource's Workgroup tag/label.
    cloud_resource_id = Column(String(255), index=True, nullable=True)
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


class VMStateCache(Base):
    """Cache for VM state to improve dashboard performance"""
    __tablename__ = "vm_state_cache"

    vmx_path = Column(Text, primary_key=True)
    vm_name = Column(String(200))
    workgroup = Column(String(50), index=True)
    os_type = Column(String(50))
    is_running = Column(Boolean, default=False)
    ip_address = Column(String(45))
    bt_managed_system_id = Column(Integer)
    bt_asset_id = Column(Integer)
    last_updated = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    last_seen_running_at = Column(DateTime, nullable=True)   # last PS-confirmed running time
    is_online = Column(Boolean, nullable=True)               # Python socket reachability
    last_online_check_at = Column(DateTime, nullable=True)   # when ping was last attempted


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
    cloud = Column(String(20), nullable=False)             # aws | azure | gcp
    region = Column(String(64), nullable=True)

    instance_id = Column(String(255), nullable=True)       # cloud resource id (filled on apply)
    private_host = Column(String(255), nullable=True)      # private endpoint host (no public endpoint)
    port = Column(Integer, nullable=True)
    status = Column(String(32), nullable=False, default="provisioning", index=True)

    credentials_ref = Column(Text, nullable=True)          # backend-agnostic ref (resolved via config_service)
    jump_item_id = Column(String(64), nullable=True)       # PRA protocol-tunnel jump (Phase 2)
    # Per-DB PRA broker overrides — config defaults are the fallback.
    jump_group = Column(String(128), nullable=True)        # PRA Jump Group name override (else bt_jump_group_name)
    jumpoint_name = Column(String(128), nullable=True)     # PRA Jumpoint name override (else bt_jumpoint_name)
    pra_credential_ref = Column(String(256), nullable=True)  # secret ref → bt_client_secret override
    entitle_integration_id = Column(String(64), nullable=True)  # Entitle DB integration registered on apply

    created_by = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


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

    mgmt_kind = Column(String(20), nullable=True)          # portainer | rancher | argocd | headlamp (Phase 2)
    mgmt_endpoint = Column(String(255), nullable=True)     # management-plane URL (Phase 2)
    pra_jump_id = Column(String(64), nullable=True)        # sra_protocol_tunnel_jump id (tunnel_type=k8s, Phase 3b)
    pra_tunnel_state = Column(Text, nullable=True)         # scrubbed Terraform state for the tunnel (drives teardown)
    # Per-cluster broker overrides — config defaults are the fallback (Phase 3b).
    jump_group = Column(String(128), nullable=True)        # PRA Jump Group name override (else bt_jump_group_name)
    jumpoint_name = Column(String(128), nullable=True)     # PRA Jumpoint name override (else bt_jumpoint_name) — the "separate jumpoint"
    pra_credential_ref = Column(String(256), nullable=True)  # secret ref → bt_client_secret override (else config)
    secrets_delivery_kind = Column(String(20), nullable=True)  # eso | secrets_agent (Phase 4)

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
            "ALTER TABLE vm_state_cache ADD COLUMN os_type VARCHAR(50)",
            "ALTER TABLE vm_state_cache ADD COLUMN last_seen_running_at TIMESTAMP",
            "ALTER TABLE vm_state_cache ADD COLUMN is_online BOOLEAN",
            "ALTER TABLE vm_state_cache ADD COLUMN last_online_check_at TIMESTAMP",
            "ALTER TABLE jobs ADD COLUMN cloud_resource_id VARCHAR(255)",
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
            # Job heartbeat — drives the startup reconcile of restart-orphaned jobs.
            "ALTER TABLE jobs ADD COLUMN updated_at TIMESTAMP",
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
