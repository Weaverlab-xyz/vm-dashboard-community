"""
Database models and session management
"""
from typing import List
from sqlalchemy.orm import Session

from ..config import settings
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
    AuditLog, ConfigApplyState, FeedbackSubmission,
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
    OAuthGroupMapping, AccessRole, UserAccessRole, Workgroup,
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
)

from .models.agent_lifecycle import (  # noqa: E402,F401  (re-exported: callers import these from web_dashboard.database)
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

from .models.beyondtrust import (  # noqa: E402,F401  (re-exported: callers import these from web_dashboard.database)
    BeyondTrustTenant,
)



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
