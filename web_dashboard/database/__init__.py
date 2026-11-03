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
    created_at = Column(DateTime, default=utcnow, nullable=False)
    updated_at = Column(DateTime, default=utcnow, nullable=True)


class K8sCluster(Base):
    """Inventory of dashboard-managed Kubernetes clusters — Kubernetes
    management (docs/cloud-services/kubernetes.md).

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
    jump_group = Column(String(128), nullable=True)        # PRA Asset Group name override (else bt_jump_group_name)
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

    # Workgroup scoping, same contract as CloudDatabase.workgroup: NULL means
    # creator-scoped, a name means everyone in that workgroup. The kubeconfig behind
    # this row is cluster-admin, so tagging one is a real grant, not a label.
    workgroup = Column(String(100), nullable=True, index=True)
    # Which agent runs this cluster's Kubernetes work: NULL is the remote worker. Only an
    # OWNED agent may be named — one the dashboard deployed (an agent_hosts row),
    # SPIRE-attested, with Kubernetes work allowed on its host — because the work hands it
    # a cluster-admin kubeconfig. Checked when set and again at every use
    # (k8s_service.agent_qualifies); one that stops qualifying sends the work back to the
    # worker. Migration 0009.
    executor_agent_id = Column(String(36), nullable=True)
    created_by = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)
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
    created_at = Column(DateTime, default=utcnow, nullable=False)


class AgentRelease(Base):
    """A release of the remote agent image the dashboard may offer as an upgrade target.

    A version and the image DIGEST it is, nothing more. The dashboard never verifies the
    signature: the updater beside each agent does, against the identity pinned in that
    host's policy.yaml, so a wrong or forged row here can at most offer an upgrade every
    host refuses (docs/platform/spiffe/remote-agents/lifecycle.md).
    """
    __tablename__ = "agent_releases"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    version = Column(String(32), nullable=False, unique=True, index=True)
    # Where the image is: the project's repository, or a mirror's (an air-gapped site
    # mirrors the image and pins the mirror in its policy.yaml `upgrades.repository`).
    repository = Column(String(200), nullable=False, default="chrweav/dashboard-agent")
    digest = Column(String(71), nullable=False)        # "sha256:" + 64 hex
    notes = Column(Text)
    source = Column(String(16), nullable=False, default="manual")   # manual | registry
    created_at = Column(DateTime, default=utcnow, nullable=False)
    created_by = Column(String(100))


class AgentRollout(Base):
    """One release rolled out across the eligible agents, canary ring first.

    ``status``: ``canary`` (canary upgrades queued) -> ``default`` (canary all done, the
    rest queued) -> ``done``; or ``stopped`` the moment any upgrade it queued fails, with
    ``detail`` naming it. ``job_ids`` and ``skipped`` (agent -> reason) are JSON, so the
    page can say which agent got what without a join table.
    """
    __tablename__ = "agent_rollouts"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    release_id = Column(String(36), ForeignKey("agent_releases.id"), nullable=False)
    change_window_id = Column(String(36), nullable=True)   # provenance only, like Job's
    status = Column(String(16), nullable=False, default="canary")
    canary_job_ids = Column(Text)                       # JSON list
    default_job_ids = Column(Text)                      # JSON list
    skipped = Column(Text)                              # JSON {agent name: reason}
    detail = Column(Text)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    created_by = Column(String(100))
    finished_at = Column(DateTime, nullable=True)


class AgentHost(Base):
    """A remote agent the dashboard deployed into a cloud, and that host's lifecycle.

    The agent itself is an ordinary ``remote_agents`` row: it enrols or attests, leases and
    is routed to exactly like one an operator built by hand. This row is the part only a
    DEPLOYED agent has — where its host runs, the policy the dashboard wrote for it, and
    when it should exist at all:

      * ``always`` — up until someone stops or deletes it;
      * ``until``  — up until ``until_at``, then stopped for good;
      * ``window`` — "persistent just in time": started ``lead_minutes`` before each
                     occurrence of a change window and stopped ``grace_minutes`` after it
                     closes.

    **Stopping destroys the host and keeps the agent.** Nothing runs and nothing is at rest
    between windows; the agent row, its Config-Management routes and the runs booked into
    the next window all survive, and each start is a fresh host with a fresh identity
    re-bound to the same agent row. ``status`` is ``parked`` in between.

    Config Management by construction: the dashboard writes this agent's ``policy.yaml``
    at launch from ``ansible_cidrs`` and ``max_concurrent``, with ``job_types:
    [agent_ansible]`` — plus ``agent_k8s`` and a ``k8s:`` block when ``k8s_enabled``
    (services/agent_host_service).
    """
    __tablename__ = "agent_hosts"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    # One host record per agent. CASCADE is for the ORM; agent_host_service deletes the
    # row explicitly before the agent, for the same SQLite reason delete_agent gives.
    agent_id = Column(String(36), ForeignKey("remote_agents.id", ondelete="CASCADE"),
                      nullable=False, unique=True, index=True)
    name = Column(String(63), nullable=False)               # the cloud resource name
    cloud = Column(String(20), nullable=False, index=True)  # aws | azure | gcp
    region = Column(String(40), nullable=False)
    zone = Column(String(40), nullable=True)                # where it actually landed (GCP)
    # The VPC / VNet / GCP network its host runs in (config_mgmt_route_service.network_of
    # form): the agent can reach only that network, so its routes are narrowed to it.
    network = Column(String(200), nullable=True)
    subnet = Column(String(400), nullable=True)             # blank = the cloud's VM subnet
    instance_size = Column(String(64), nullable=True)       # blank = the per-cloud default

    # What the dashboard writes into the agent's policy.yaml at launch.
    ansible_cidrs = Column(Text, nullable=False, default="[]")   # JSON list of ranges
    max_concurrent = Column(Integer, nullable=False, default=1)
    # Kubernetes work (agent_k8s, and k8s Config-Management plays) for the clusters that
    # name this agent. NULL is off. Takes effect at the next start: the policy is written
    # at launch. Migration 0009.
    k8s_enabled = Column(Boolean, nullable=True)

    mode = Column(String(16), nullable=False, default="always")  # always | until | window
    until_at = Column(DateTime, nullable=True)                   # UTC, mode "until"
    change_window_id = Column(String(36), nullable=True)         # mode "window"
    lead_minutes = Column(Integer, nullable=False, default=20)
    grace_minutes = Column(Integer, nullable=False, default=15)

    # provisioning | starting | running | stopping | parked | error | deleted
    status = Column(String(32), nullable=False, default="parked", index=True)
    host_id = Column(String(200), nullable=True)            # EC2 id / VM name / GCE name
    last_job_id = Column(String(36), nullable=True)         # the latest start/stop job
    error = Column(Text, nullable=True)
    # The schedule as the last sweep computed it, for the Agents page. Display only:
    # the sweep recomputes from the window every pass.
    next_start_at = Column(DateTime, nullable=True)
    next_stop_at = Column(DateTime, nullable=True)
    started_at = Column(DateTime, nullable=True)

    created_by = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)


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
