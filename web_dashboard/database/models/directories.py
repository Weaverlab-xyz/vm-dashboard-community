"""Directory, certificate-lab, health and Entitle models."""
import uuid
from sqlalchemy import Column, String, Integer, DateTime, Boolean, Text, ForeignKey

from ...utils.clock import utcnow
from .._engine import Base


class CertLab(Base):
    """Inventory of dashboard-provisioned certificate authorities for the Password Safe
    Certificate plugin's lab.

    One row per **CA pool**, which is the thing that carries standing cost: a GCP CAS
    DevOps-tier pool is ~$20/month whether or not it issues anything, and an AWS Private
    CA is ~$400/month. The mTLS endpoint and the CI runner are ordinary VMs deployed
    through the normal cloud pages, so they already have their own timers and their own
    Destroy — this row exists so the CA has one too.

    ``tf_state`` is the scrubbed Terraform state, which is what makes teardown
    deterministic: the destroy is fed by the recorded state rather than by hand-typed
    ids, exactly as the database and cluster paths are.

    ``ps_tf_state`` holds the separate state for the managed system + managed account, so
    deregistering the Password Safe objects and destroying the CA are independent — a
    lab can be torn down while the certificate identity's audit history stays.
    """
    __tablename__ = "cert_labs"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(120), nullable=False)
    cloud = Column(String(20), nullable=False, default="gcp")   # gcp | aws
    backend = Column(String(20), nullable=False, default="gcpcas")  # the plugin's backend name
    project = Column(String(120), nullable=True)                # GCP project / AWS account
    location = Column(String(64), nullable=True)                # CAS region — location-scoped
    pool_id = Column(String(120), nullable=True)                # the `pool=` on the address
    # The `arn=` on an awspca address, and the AWS half of what `pool_id` is for GCP. Its
    # own column rather than reusing pool_id: an ARN is not a pool, nothing on the AWS
    # side has a pool, and a field whose meaning depends on a sibling column is how a
    # reader two years from now gets it wrong. NULL until the apply returns it.
    ca_arn = Column(String(255), nullable=True)
    # How many CAs this root permits beneath it — GCP's max_issuer_path_length, and on
    # AWS the ceiling the SubordinateCACertificate_PathLen{N} templates are chosen from.
    #
    # **This is the one prerequisite that stops a subordinate-CA build dead.** A root
    # created to issue leaves has a path length of ZERO and refuses to sign a subordinate
    # at all, and the refusal comes from the CA rather than from the plugin — so it
    # arrives as a policy error a long way from the missing flag. Recording it means the
    # identity form can refuse `isca` against a root that cannot sign one, at the click.
    #
    # 0 on every pre-existing row, which is what those roots actually are.
    ca_path_length = Column(Integer, nullable=False, default=0)
    status = Column(String(32), nullable=False, default="provisioning", index=True)

    # Terraform state lives in the active storage backend under terraform-state/<job id>,
    # so destroy re-inits from there and works after a container recreate wiped the deploy
    # dir. This id is the key to it, which is why deleting a provisioning Job row would
    # orphan a billing CA pool.
    deploy_job_id = Column(String(36), nullable=True)
    # The chain the mTLS endpoint must trust. Public by definition — a CA certificate is
    # not a secret — so it is stored plainly and handed to the nginx playbook as an
    # extra_var rather than being fetched again at run time.
    ca_chain_pem = Column(Text, nullable=True)
    # The enrollment service account's email. Its KEY is never stored here: it goes
    # straight into the Password Safe functional account, which is the protected field
    # built for it. Nothing secret belongs on a row the inventory page renders.
    enroll_account = Column(String(255), nullable=True)

    # The functional account this CA's identities onboard against, recorded in BOTH
    # modes so Add identity never has to fall back to a global config key that may have
    # moved on since the build. Not secret: it is the account's NAME, which is the
    # enrollment principal plus the BeyondInsight run-as user.
    ps_functional_account = Column(String(255), nullable=True)
    # Set ONLY when the dashboard minted that account. Non-NULL is the ownership flag:
    # teardown deletes what it created and never an operator's own account, exactly how
    # the cloud-DB onboarding splits ps_db_functional_account_id from ..._ref. NULL
    # therefore means "referenced, or never created" — never "not yet looked up".
    ps_functional_account_id = Column(String(36), nullable=True)

    # The SECOND functional account, on the "Subordinate CA" platform.
    #
    # Two pairs of columns rather than one, because a functional account is
    # **platform-bound** and a managed system inherits its platform: an account on
    # "Certificate" cannot carry a managed system on "Subordinate CA" — it onboards green
    # and then fails every credential action. The CA credential inside is the same one;
    # what differs is the platform it was created on.
    #
    # Collapsing them would also undo the reason the plugin split in the first place. The
    # whole argument for two platforms is that their access control is separable, and one
    # shared functional account puts a leaf and an issuer back under one grant.
    #
    # NULL until the first subordinate identity is onboarded — the leaf account is minted
    # by the build, this one lazily, from the enrollment credential still sitting in the
    # CA's own terraform state. See cert_lab_service._ensure_package_functional_account.
    ps_subca_functional_account = Column(String(255), nullable=True)
    ps_subca_functional_account_id = Column(String(36), nullable=True)

    ps_system_id = Column(String(36), nullable=True)
    ps_account_id = Column(String(36), nullable=True)
    ps_address = Column(Text, nullable=True)                    # the composed profile
    # Which package ps_system_id was registered against — "certificate" or "subca". The
    # address alone cannot say: a subordinate profile normally carries no isca= at all,
    # that being the package's own default.
    ps_package = Column(String(16), nullable=True)
    ps_tf_state = Column(Text, nullable=True)                   # scrubbed
    error_message = Column(Text, nullable=True)

    workgroup = Column(String(100), nullable=True, index=True)
    created_by = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    updated_at = Column(DateTime, default=utcnow, nullable=True)
    # Auto-delete timer — NULL = never, never "inherit the default" (see Job.expires_at).
    # Only ever stamped on a PROVISIONED pool: a CA someone else built is inventory, not
    # something this dashboard may reap.
    expires_at = Column(DateTime, nullable=True, index=True)
    expiry_warned_at = Column(DateTime, nullable=True)


class ManagedDirectory(Base):
    """A Microsoft Active Directory that Windows servers can be domain-joined to.

    AWS (Directory Service: Managed Microsoft AD, AD Connector, Simple AD) and GCP
    (Managed Service for Microsoft Active Directory). Azure is absent on purpose: Windows
    VMs there join Entra ID directly (windows_server_hook.entra_join_azure).

    Cloud identity providers (Entra ID, Okta, PingOne) are rows too, with
    ``cloud="saas"``: not joinable, but browsable and with opt-in group-membership
    writes through ``services/directory_idp``.

    ``source`` follows the CloudDatabase / K8sCluster convention. A ``provisioned`` row
    was built here from ``terraform/directory/<module>`` and can be destroyed here; a
    ``registered`` row is a directory that already existed, found by discovery, and
    deleting it only forgets it.

    No secret is stored on this row. The administrator password of a provisioned
    directory lives in Password Safe / an external secret manager (never the dashboard
    database); only its (backend, ref) pair is kept here. A registered directory carries
    none at all — joining a server needs no domain credential on either cloud.
    """
    __tablename__ = "managed_directories"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(255), nullable=False)                 # FQDN, e.g. corp.example.com
    netbios = Column(String(15), nullable=True)
    cloud = Column(String(20), nullable=False)                 # aws | gcp | local (on-prem) | saas
    # aws_managed_ad | aws_ad_connector | aws_simple_ad | gcp_managed_ad
    # | onprem_ad | ldap  (the last two: on-prem, reached through a remote agent)
    provider = Column(String(32), nullable=False)
    source = Column(String(16), nullable=False, default="provisioned")  # provisioned | registered
    status = Column(String(32), nullable=False, default="provisioning", index=True)
    edition = Column(String(32), nullable=True)                # AWS Standard | Enterprise

    region = Column(String(64), nullable=True)                 # AWS region
    locations = Column(Text, nullable=True)                    # GCP: JSON array of regions
    project = Column(String(120), nullable=True)               # GCP project
    directory_id = Column(String(64), nullable=True)           # AWS d-xxxxxxxxxx
    resource_name = Column(String(255), nullable=True)         # GCP projects/…/domains/…
    vpc_id = Column(String(64), nullable=True)                 # AWS
    subnet_ids = Column(Text, nullable=True)                   # AWS: JSON array
    networks = Column(Text, nullable=True)                     # GCP: JSON array (authorized)
    reserved_ip_range = Column(String(32), nullable=True)      # GCP
    dns_ips = Column(Text, nullable=True)                      # JSON array
    security_group_id = Column(String(64), nullable=True)      # AWS

    # ── on-prem (cloud="local") ─────────────────────────────────────────────────
    # The remote agent that can reach this directory. Only meaningful on a local row —
    # mirrors CloudDatabase.agent_id. SET NULL, so retiring an agent leaves the row
    # visibly unreachable rather than deleting an inventory record.
    agent_id = Column(String(36), ForeignKey("remote_agents.id", ondelete="SET NULL"),
                      index=True, nullable=True)
    host = Column(String(255), nullable=True)                  # a domain controller / LDAP server
    port = Column(Integer, nullable=True)                      # 389 | 636
    use_ldaps = Column(Boolean, nullable=True)
    base_dn = Column(String(255), nullable=True)               # e.g. DC=corp,DC=example,DC=com
    # "psmanaged:{system_id, account_id, account_name}" — the Password Safe account the
    # playbooks bind as, checked out per run. Never a secret.
    credentials_ref = Column(Text, nullable=True)
    # A cloud-side extension of an on-prem directory (an AWS AD Connector, a GCP DNS
    # link) points at the on-prem row it extends. Not a FK: unregistering the on-prem
    # row is refused while a link exists, and a dangling id reads as "on-prem row gone".
    linked_directory_id = Column(String(36), nullable=True, index=True)
    # A VyOS site link's non-secret settings as JSON (services/vyos_link_service): zone,
    # subnets, both WireGuard PUBLIC keys, the peer's addresses, and the Secret Manager
    # id that holds the peer's private key. Never a secret.
    link_config = Column(Text, nullable=True)

    # ── cloud identity providers (cloud="saas": entra_id | okta | pingone) ──────────
    # Reached over the provider's REST API from the dashboard itself, no agent. The
    # credential is still never on the row: ``credentials_ref`` holds a vault ref
    # (bt_safe:// aws_sm:// azure_kv:// gcp_sm://) or the psmanaged: pointer, or nothing
    # for auth_mode="dashboard_azure" (the dashboard's own Azure identity).
    endpoint = Column(String(255), nullable=True)              # Okta org URL | PingOne TLD
    tenant_id = Column(String(64), nullable=True)              # Entra tenant | PingOne env
    client_id = Column(String(255), nullable=True)
    auth_mode = Column(String(32), nullable=True)              # client_secret | private_key_jwt | ssws | dashboard_azure
    # Group-membership writes are opt-in per directory: registering one grants nothing.
    writes_enabled = Column(Boolean, nullable=True, default=False)
    options = Column(Text, nullable=True)                      # JSON, closed key set per provider
    # Which LDAP server a provider="ldap" row is (openldap | pingdirectory | okta_ldap |
    # 389ds | freeipa). NULL = unknown/generic. Decides defaults and, for okta_ldap, that
    # only read-only playbooks run (directory_service.run_refusal).
    vendor = Column(String(32), nullable=True)
    # The Entitle integration an operator PINNED as governing this directory. Never
    # matched automatically: a name match is a guess, and a wrong link reads as truth.
    entitle_integration_id = Column(String(64), nullable=True)
    entitle_integration_name = Column(String(255), nullable=True)   # as Entitle named it when pinned

    admin_username = Column(String(64), nullable=True)
    admin_password_backend = Column(String(32), nullable=True)
    admin_password_ref = Column(String(255), nullable=True)
    admin_password_custody = Column(String(32), nullable=True)  # secret_manager | passwordsafe_managed

    ps_system_id = Column(String(36), nullable=True)
    ps_account_id = Column(String(36), nullable=True)
    ps_tf_state = Column(Text, nullable=True)                  # scrubbed
    ps_error = Column(Text, nullable=True)

    # Terraform state lives in the storage backend under terraform-state/<job id>.
    deploy_job_id = Column(String(36), nullable=True)
    error_message = Column(Text, nullable=True)

    workgroup = Column(String(100), nullable=True, index=True)
    created_by = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    updated_at = Column(DateTime, default=utcnow, nullable=True)
    # NULL by default: a directory is long-lived infrastructure that joined servers
    # depend on. An operator may set one through /api/expiry/set.
    expires_at = Column(DateTime, nullable=True, index=True)
    expiry_warned_at = Column(DateTime, nullable=True)


class DirectoryHealth(Base):
    """The last health check of one :class:`ManagedDirectory`: is it up and answering.

    Infrastructure health only — the control plane's own state, domain controller status,
    whether the identity provider signs in, whether an on-prem DC answers LDAP. Nothing
    here describes accounts, groups or privilege: identity posture is BeyondTrust Identity
    Security Insights' data, and ``services/directory_health_service`` says why it is not
    duplicated here.

    One row per directory, overwritten by each check, so the page is one query and no
    provider calls. ``checks`` is a JSON list of ``{key, label, status, detail}``.

    ``pending_job_id`` is the agent_discover job probing an on-prem directory. The probe
    finishes on the agent's schedule, not the checker's, so its result is folded in when
    that job completes (``api/agent.complete``) rather than awaited.
    """
    __tablename__ = "directory_health"

    # Not a FK: a directory is soft-deleted (status="deleted"), and the health page skips
    # those rows by joining on the directory, so a stale row here is harmless.
    directory_id = Column(String(36), primary_key=True)
    status = Column(String(16), nullable=False, default="unknown")  # ok | warn | fail | unknown
    checks = Column(Text, nullable=True)
    checked_at = Column(DateTime, nullable=True, index=True)
    duration_ms = Column(Integer, nullable=True)
    pending_job_id = Column(String(36), nullable=True, index=True)
    pending_since = Column(DateTime, nullable=True)
    # When the last probe was queued. Separate from checked_at, which every pass moves,
    # because on-prem probes run on their own, slower cadence.
    probe_queued_at = Column(DateTime, nullable=True)
    # The domain controller deep check (builtin:ad-dc-health, an agent_ansible run): the job
    # in flight, since when, and when the last one was queued. Its own cadence again --
    # it checks out a Password Safe credential per run, so it runs hours apart.
    deep_job_id = Column(String(36), nullable=True, index=True)
    deep_since = Column(DateTime, nullable=True)
    deep_queued_at = Column(DateTime, nullable=True)


class HealthCheck(Base):
    """The last health of one object, in one health area (``services/health``).

    The general form of :class:`DirectoryHealth`: one row per ``(area, object_id)``,
    overwritten by the area's collector, so the Health page is one query and no provider
    calls. ``object_id`` is the collector's own stable id for the object (a tenant id, a
    gateway id, an inventory item id). ``checks`` is a JSON list of
    ``{key, label, status, detail}``, exactly as on DirectoryHealth.

    Infrastructure health only, by the same rule: nothing here describes accounts, groups
    or privilege. That is BeyondTrust Identity Security Insights' data.
    """
    __tablename__ = "health_checks"

    area = Column(String(32), primary_key=True)
    object_id = Column(String(160), primary_key=True)
    name = Column(String(255), nullable=False, default="")
    kind = Column(String(64), nullable=True)          # what the object is, for display
    href = Column(String(255), nullable=True)         # where to act on it
    status = Column(String(16), nullable=False, default="unknown", index=True)
    checks = Column(Text, nullable=True)
    checked_at = Column(DateTime, nullable=True)


class HealthArea(Base):
    """When each health area's collector last ran, and how that went.

    Separate from the rows because a collector can fail as a whole -- Password Safe down,
    say -- and that must read as "could not check", not as every account failing. A failed
    run records ``last_error`` here and leaves the area's rows as they were.
    """
    __tablename__ = "health_areas"

    area = Column(String(32), primary_key=True)
    last_run_at = Column(DateTime, nullable=True)
    last_ok_at = Column(DateTime, nullable=True)
    last_error = Column(Text, nullable=True)
    duration_ms = Column(Integer, nullable=True)


class EntitleVirtualApp(Base):
    """An Entitle **Virtual Application** the dashboard created over a directory's groups.

    Each role on it fronts a role Entitle already synced from the directory's pinned
    integration (a group's membership role), under a friendly name and a tier's workflow.
    Nothing is created in the directory itself. ``tf_state_json`` is the scrubbed state
    ``entitle_registration_service.deregister`` destroys it from; the app holds no
    credential, so the scrub has nothing to remove and is applied anyway.

    ``directory_id`` is not a FK, for the same reason ``linked_directory_id`` is not:
    unregistering the directory is refused while an app is live, and a dangling id reads
    as "directory gone" rather than cascading a delete Entitle would never hear about.
    """
    __tablename__ = "entitle_virtual_apps"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    directory_id = Column(String(36), nullable=False, index=True)
    name = Column(String(50), nullable=False)
    # applying | active | failed | destroying | deleted
    status = Column(String(16), nullable=False, default="applying", index=True)
    integration_id = Column(String(64), nullable=True)
    # JSON list: {key, name, resource, tier, virtualized_role_id, virtualized_role_name,
    # group_name, persona, cloud, role_id}. role_id is filled once Entitle created it.
    roles_json = Column(Text, nullable=False, default="[]")
    tf_state_json = Column(Text, nullable=True)
    error = Column(Text, nullable=True)
    created_by = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)


class EntitleBundleSet(Base):
    """One apply of persona × cloud bundles (``services/entitle_persona_bundles``).

    A set rather than a row per bundle because they are applied, and destroyed, as one
    Terraform state. ``bundles_json`` records what each bundle resolved to at apply time
    (its role ids, and which components were missing) so the page can show it later
    without asking Entitle again.
    """
    __tablename__ = "entitle_bundle_sets"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    status = Column(String(16), nullable=False, default="applying", index=True)
    bundles_json = Column(Text, nullable=False, default="[]")
    tf_state_json = Column(Text, nullable=True)
    error = Column(Text, nullable=True)
    created_by = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)
