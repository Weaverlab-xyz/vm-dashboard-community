"""
Database models and session management
"""
import json
import uuid
from datetime import datetime
from typing import Optional, List
from sqlalchemy import create_engine, Column, String, Integer, DateTime, Boolean, Text, LargeBinary, ForeignKey, UniqueConstraint, Float, text
from sqlalchemy.orm import declarative_base, relationship
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.pool import NullPool, QueuePool

from ..config import settings
from ..utils.clock import utcnow
from ._engine import (  # noqa: F401  (re-exported: callers import these from web_dashboard.database)
    Base, SessionLocal, _is_sqlite, _pool_kwargs, engine, engine_url,
)
from .passwords import (  # noqa: F401  (re-exported: callers import these from web_dashboard.database)
    BCRYPT_MAX_BYTES, bcrypt_input, check_password_length, get_password_hash,
    password_length_error, verify_password,
)
from .rbac import (  # noqa: F401  (re-exported: callers import these from web_dashboard.database)
    _ROLE_UNRESOLVED, merge_permission_maps, narrow_to_token_scope,
)

# How long init_db's DDL transaction may wait for a table lock before giving up on one
# statement. Deliberately short: the migrations are additive and idempotent, so skipping
# one costs a boot, while WAITING costs the whole app (a pending ACCESS EXCLUSIVE on
# `jobs` blocks every later reader of it -- see the comment at the SET LOCAL in init_db).
# Not a settings field on purpose: it must hold before any config can be read out of the
# database, which is the very thing init_db is creating.
_DDL_LOCK_TIMEOUT_MS = 4000


def pool_capacity() -> int:
    """Max connections this process's pool can hand out — what jobs_worker._limits clamps
    concurrency against. 0 for SQLite's NullPool, which is unbounded (a fresh connection
    per Session), so the caller treats 0 as "don't clamp"."""
    if _is_sqlite:
        return 0
    return settings.db_pool_size + settings.db_max_overflow



# ========== DATABASE MODELS ==========

from .models.identity import (  # noqa: E402,F401  (re-exported: callers import these from web_dashboard.database)
    User, Fido2Credential, PersonalAccessToken,
    OAuthClient, ExternalWorkloadIdentity, RunnerCredentialGrant,
    SpiffeTrustDomain, SpireAdminCall, SpireAdminRelay,
    LoginAttempt,
)

from .models.operations import (  # noqa: E402,F401  (re-exported: callers import these from web_dashboard.database)
    EphemeralState, NotificationEndpoint, NotificationDelivery,
    AuditLog, ConfigApplyState,
)



from .models.agents import (  # noqa: E402,F401  (re-exported: callers import these from web_dashboard.database)
    RemoteAgent, AgentNonce, AgentEnrollAttempt,
    _json_list,
)


from .models.hypervisor import (  # noqa: E402,F401  (re-exported: callers import these from web_dashboard.database)
    HypervisorConnection, HypervisorVMCache, ConfigMgmtRoute,
)


from .models.jobs import (  # noqa: E402,F401  (re-exported: callers import these from web_dashboard.database)
    Job, JobLog, ChangeWindow,
    JobSchedule,
)

from .models.access import (  # noqa: E402,F401  (re-exported: callers import these from web_dashboard.database)
    OAuthGroupMapping, AccessRole, Workgroup,
    StorageAssetScope, VMWorkgroupOverride, Approval,
    EntitleActivation,
)

from .models.appconfig import (  # noqa: E402,F401  (re-exported: callers import these from web_dashboard.database)
    AppConfig, BrandAsset, SchemaMarker,
    SecretVault,
)

from .models.cloud import (  # noqa: E402,F401  (re-exported: callers import these from web_dashboard.database)
    ContainerStateCache, CloudCostCache, WorkloadCredentialLease,
    DashboardStatCache, PovPlatformCache, RegisteredImage,
    VirtualDesktop, CloudDatabase,
)

from .models.directories import (  # noqa: E402,F401  (re-exported: callers import these from web_dashboard.database)
    CertLab, ManagedDirectory, DirectoryHealth,
    HealthCheck, HealthArea, EntitleVirtualApp,
    EntitleBundleSet,
)

from .models.spire import (  # noqa: E402,F401  (re-exported: callers import these from web_dashboard.database)
    SpireLab, AgentCell, WorkloadK8sToken,
    WorkloadCloudCredential,
)

from .models.infrastructure import (  # noqa: E402,F401  (re-exported: callers import these from web_dashboard.database)
    CloudFunction, K8sCluster, Gateway,
    AgentRelease, AgentRollout, AgentHost,
)



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


class BeyondTrustTenant(Base):
    """One BeyondTrust tenant a POV can be wired into.

    The reason the POV profile exists at all. A demo instance resolves its tenant from
    the global singletons — ``bt_api_host``, ``pscli_api_url``, ``entitle_api_url`` — and
    that is correct there, because there is exactly one. A POV instance runs several POVs
    at once and **each has its own PRA appliance and its own Password Safe Cloud tenant**,
    so "which tenant?" stops having one answer. See docs/profiles/pov/README.md.

    Modelled on ``HypervisorConnection``, which solved the same shape one layer down: N
    named rows replacing a singleton, credentials as Fernet ciphertext *or* an external
    reference, an ``is_default``, and a resolver that still falls back to the old config
    keys so nothing breaks on an install that has not seeded yet.

    **One row per product, not per customer.** ``PovEnvironment`` carries three separate
    FKs because the three are genuinely independent: a POV needs its own PRA appliance and
    its own Password Safe tenant, but Entitle is multi-tenant behind a REGIONAL API URL
    and is usually shared across every POV. A single row holding all three would force a
    duplicate Entitle credential per customer, and duplicated credentials rotate apart.

    **Why this is not the workgroup.** ``PovEnvironment.workgroup`` already exists and
    ``config_service`` already scopes values by it, so "just put the tenant on the
    workgroup" is the tempting shortcut. It is wrong: workgroup answers *who may see this*
    — RBAC, the inventory filters, the expiry exempt list all key off it — while a tenant
    answers *whose PRA appliance this is*. An SE running two customers' POVs inside one
    workgroup would get whichever tenant the workgroup named, silently, for both.

    **On customer data.** ``PovEnvironment`` deliberately has no customer field and adding
    one should be treated as a schema regression. This table is the one place that rule
    bends, and only exactly as far as it must: a PRA appliance is reached at a hostname
    like ``acme.beyondtrustcloud.com``, so the connection target inherently identifies
    who it belongs to and there is no way to hold one without that. A *connection target*
    you cannot avoid; a free-text "customer" box you can. ``name`` is therefore a
    constrained slug, like a POV's, and not somewhere to type a company.
    """
    __tablename__ = "beyondtrust_tenants"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    # pra | password_safe | entitle — bt_tenant_service.VALID_KINDS
    kind = Column(String(16), nullable=False, index=True)
    name = Column(String(64), nullable=False)               # operator slug, unique per kind

    # PRA appliance host, Password Safe URL, or the Entitle API base.
    base_url = Column(String(255), nullable=False, default="")
    # OAuth client id for PRA and Password Safe. Blank for Entitle, which is a bearer
    # token with no paired id — a column that is empty for one kind beats a second table.
    client_id = Column(String(255))

    # Exactly one of these carries the credential. Same rule and the same two mechanisms
    # as HypervisorConnection, so there is one secret-at-rest story in this database and
    # one rotation hazard rather than two.
    secret_enc = Column(Text)              # Fernet (config_service.encrypt_value)
    secret_ref = Column(String(256))       # aws_sm:// | azure_kv:// | gcp_sm:// | bt_safe://

    # Per-kind NON-SECRET extras, JSON. Allowlisted per kind by the service and pinned by
    # a test: this is a free-form blob on a row that holds credentials, which is exactly
    # where a password ends up by accident.
    options = Column(Text)

    is_default = Column(Boolean, default=False, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)

    # Set by the Verify action. Derived state would be better, but a live credential check
    # is an outbound round trip nobody wants on a page load, so this is the cached answer
    # and `last_checked_at` is what stops it being read as current.
    last_checked_at = Column(DateTime)
    last_ok_at = Column(DateTime)
    last_error = Column(Text)

    created_at = Column(DateTime, default=utcnow, nullable=False)
    created_by = Column(String(100))
    updated_at = Column(DateTime, default=utcnow)

    @property
    def options_dict(self) -> dict:
        if not self.options:
            return {}
        try:
            value = json.loads(self.options)
            return value if isinstance(value, dict) else {}
        except Exception:  # noqa: BLE001
            return {}


from .models.pov import (  # noqa: E402,F401  (re-exported: callers import these from web_dashboard.database)
    PovEnvironment, PovEnvironmentVM, PovUseCaseProgress,
    PovSignoff, PovAccessor, PovVendorUser,
    PovTemplateBuild, PovBlueprint, PovCloudTemplate,
    PovCloudTemplateVM,
)


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
    check_password_length(password)
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


# Imported last: both modules read the models above, and ``bootstrap`` reads this package.
from .rbac_backfills import (  # noqa: E402,F401  (re-exported: init_db and tests use these names)
    _BACKFILL_V1_DELIBERATELY_EMPTY, _BACKFILL_V1_MARKER, _BACKFILL_V1_PHANTOM_ADMIN,
    _BACKFILL_V1_SCOPES, _EMPTY_MEANS_NOTHING_MARKER, _PRESENTER_OWN_ONLY_MARKER,
    _PRESENTER_OWN_POV_MARKER, _RETIRE_LEVELS_MARKER, _backfill_new_permission_scopes,
    _commit_marked, _grant_presenter_own_pov, _materialise_empty_permission_maps,
    _narrow_presenter_to_own_povs, _retire_unenforced_levels, _strip_retired,
)
from .bootstrap import (  # noqa: E402,F401  (re-exported: callers import init_db from here)
    _SQLITE_CREATE_ATTEMPTS, _create_all_sqlite, init_db,
)
