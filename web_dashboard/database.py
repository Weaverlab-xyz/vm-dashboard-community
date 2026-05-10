"""
Database models and session management
"""
import json
import uuid
from datetime import datetime
from typing import Optional, List
from sqlalchemy import create_engine, Column, String, Integer, DateTime, Boolean, Text, LargeBinary, ForeignKey, text
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
    # NULL = all permissions granted (backward compatible default)
    permissions = Column(Text, nullable=True)

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
        """Set workgroups from list"""
        self.workgroups = json.dumps(value)

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
    status = Column(String(20), nullable=False, default="pending", index=True)  # pending, running, completed, failed, cancelled
    progress_pct = Column(Integer, default=0)
    progress_message = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    started_at = Column(DateTime)
    completed_at = Column(DateTime)
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

    user = relationship("User")


class AppConfig(Base):
    """Encrypted key-value store for cloud credentials and feature flags.

    Values are Fernet-encrypted with a key derived from JWT_SECRET_KEY so that
    secrets at rest are protected even if someone reads the DB directly.
    Written by the setup wizard; consumed by config_service.get().
    """
    __tablename__ = "app_config"

    key = Column(String(128), primary_key=True)
    value = Column(Text, nullable=True)         # Fernet-encrypted
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)


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
            # Session-level lock: blocks concurrent callers until we exit this
            # connection.  Released automatically when the connection closes.
            conn.execute(text("SELECT pg_advisory_lock(20260101)"))

        # Pass the connection so create_all runs inside the same transaction
        # (and the same advisory-lock session on PostgreSQL).
        Base.metadata.create_all(bind=conn)

        _migrations = [
            "ALTER TABLE users ADD COLUMN is_admin BOOLEAN DEFAULT 0",
            "ALTER TABLE users ADD COLUMN permissions TEXT",
            "ALTER TABLE oauth_group_mappings ADD COLUMN default_permissions TEXT",
            "ALTER TABLE vm_state_cache ADD COLUMN os_type VARCHAR(50)",
            "ALTER TABLE vm_state_cache ADD COLUMN last_seen_running_at TIMESTAMP",
            "ALTER TABLE vm_state_cache ADD COLUMN is_online BOOLEAN",
            "ALTER TABLE vm_state_cache ADD COLUMN last_online_check_at TIMESTAMP",
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
            conn.commit()
        # Advisory lock released automatically when conn closes.

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
