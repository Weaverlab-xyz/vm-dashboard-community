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



class SpireLab(Base):
    """Inventory of dashboard-provisioned SPIRE trust domains for the Password Safe
    SPIFFE SVID plugin's lab.

    One row per **trust domain**, because that is what the plugin's Managed System
    models — not per VM. Unlike the Certificate Lab there is no Terraform module and no
    standing cost of its own: a SPIRE server is a Go binary and a sqlite file on an
    ordinary Linux VM the dashboard already deploys and reaps.

    **The argument for a timer here is not cost, it is that a forgotten trust domain
    keeps minting.** 8081 is an API that issues identities, so a lab nobody remembers
    is an identity provider nobody is watching. Deregistering the managed system is
    the kill switch, since the plugin implements no Enable/Disable Managed Account.

    **Nothing secret lives on this row.** The admin credential is a PKCS#12 that
    ``spire-admin-identity.yml`` writes straight into Secrets Safe under ``no_log``;
    this row carries only the two TITLES that name it. The same rule covers the
    connection identity the operator picks for the Ansible runs: a source *ref*, a
    Password Safe *id and account name*, and a *username* — resolved at run time, never
    stored. ``trust_bundle_pem`` is public
    by construction — it is what every consumer of the trust domain has to trust, in
    the same sense as a CA chain — and it is stored because the operator has to paste
    it into the managed system and it fits nowhere else.
    """
    __tablename__ = "spire_labs"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(120), nullable=False)
    # The bare DNS-style name ('weaverlab.test'), never a spiffe:// URI. Baked into the
    # server config, every SPIFFE ID and the Managed System, so it is immutable once the
    # server is installed — which is why the build form asks for it before anything else.
    trust_domain = Column(String(255), nullable=False)
    cloud = Column(String(20), nullable=False, default="azure")
    region = Column(String(64), nullable=True)

    # The provider's own id, carrying everything teardown needs and nothing else: an ARM
    # id on Azure, "<region>/<instance-id>" on AWS, "<project>/<zone>/<name>" on GCP.
    # `terminate` gets only this string — an instance id alone does not say which
    # regional endpoint owns it. See the per-cloud host backends in spire_lab_service.
    vm_resource_id = Column(String(500), nullable=True)
    vm_name = Column(String(120), nullable=True)
    # BOTH, because they answer different questions and confusing them is the whole of
    # the reachability trap: `private_ip` is what the Resource Broker dials on 8081, and
    # `public_ip` is what the Ansible runner SSHes to when the runner is not in-subnet.
    # Either may be blank.
    private_ip = Column(String(64), nullable=True)
    public_ip = Column(String(64), nullable=True)
    bind_port = Column(Integer, nullable=False, default=8081)
    status = Column(String(32), nullable=False, default="provisioning", index=True)

    # The cloud ACL actually applied, as a comma-joined CIDR list, plus the rule's name.
    # Recorded rather than recomputed so teardown closes exactly what provision opened,
    # and so an operator can see why the broker cannot reach the server: corp egress
    # rotates between two Cloudflare addresses, so a rule pinned to one fails
    # intermittently and looks like a credential problem.
    source_cidrs = Column(Text, nullable=True)
    firewall_name = Column(String(120), nullable=True)

    # Progress through the four playbooks, as the names of the ones that finished. The
    # provision is a sequence of separate `ansible_local` job rows, so this is what tells
    # a resumed or half-failed lab apart from one that never started.
    stages_done = Column(Text, nullable=True)
    # The `ansible_local` job ids, in order, so the page can link each stage's Live
    # Output. A failed stage's log is the only place its Ansible error exists.
    stage_job_ids = Column(Text, nullable=True)
    # How SPIRE runs on the lab host: "vm" (tarball + systemd, the original), "docker"
    # (the official images under Docker Compose) or "k8s" (k3s + the official Helm
    # charts). NULL reads as "vm", which is every lab built before this existed. See
    # spire_lab_service.stages_for and the CLI-prefix note there.
    deployment_mode = Column(String(16), nullable=True)
    # The SPIRE release this lab runs, as built or last upgraded: a SPIRE version for vm /
    # docker, "chart <version> / SPIRE <version>" for k8s (the play sets the chart's
    # images; a lab recorded as a bare "chart <version>" predates that, and is offered
    # the upgrade). NULL is a lab built before this was recorded -- unknown, so the page
    # offers the upgrade.
    spire_version = Column(String(32), nullable=True)

    # ── The Kubernetes half ──────────────────────────────────────────────────
    # A SECOND HOST on the same row rather than a second row: the trust domain is still
    # the thing being modelled, and the k3s node is an attribute of this lab. It holds one
    # SPIRE agent attested into this domain and is meaningless without it.
    #
    # ATTACHED, NEVER CREATED — the same rule as the SPIRE host and for the same reason:
    # privileged playbooks against a host of the caller's choosing is not something this
    # should accept, so both hosts come from the dashboard's own deploy rows.
    k8s_vm_resource_id = Column(String(500), nullable=True)
    k8s_vm_name = Column(String(120), nullable=True)
    # The same two-address split, and the same trap: `k8s_private_ip` is what the SPIRE
    # host's ACL is opened TO and what the agent dials the server from; `k8s_public_ip` is
    # what the Ansible runner SSHes to. Either may be blank.
    k8s_private_ip = Column(String(64), nullable=True)
    k8s_public_ip = Column(String(64), nullable=True)

    # linking | linked | failed, or NULL for "never attempted". Deliberately SEPARATE from
    # `status`: the governance half is what most labs are built for and it stands on its
    # own, so a failed link must not make a working trust domain read as broken.
    k8s_status = Column(String(32), nullable=True)
    k8s_error_message = Column(Text, nullable=True)
    k8s_stages_done = Column(Text, nullable=True)
    k8s_stage_job_ids = Column(Text, nullable=True)

    # The strings that have to agree in three places at once — the registration entry, the
    # API server's AuthenticationConfiguration, and the workload's SPIFFE_JWT_AUDIENCE.
    # Stored rather than recomputed because the panel shows them and because a re-link must
    # not quietly change one: an audience that stops matching rejects every token, and
    # Kubernetes says nothing useful about why.
    k8s_audience = Column(String(120), nullable=True)
    k8s_workload_spiffe_id = Column(String(255), nullable=True)
    k8s_workload_uid = Column(Integer, nullable=True)
    k8s_workload_role = Column(String(120), nullable=True)
    # Spelled exactly as the discovery document advertises it. Kubernetes matches the
    # issuer STRING, so a trailing slash or a swapped host is a total failure with no
    # diagnostic — which is why it is recorded rather than rebuilt from parts each time.
    k8s_issuer_url = Column(String(255), nullable=True)

    # SPIFFE federation with the dashboard's own SPIRE server (docs/preview/workload-lab/spiffe.md,
    # "Federating with the dashboard"): federating | federated | failed, or NULL for never.
    # Separate from `status` for the reason k8s_status is: a failed federation must not
    # make a working trust domain read as broken.
    federation_status = Column(String(32), nullable=True)
    federation_error = Column(Text, nullable=True)
    federated_at = Column(DateTime, nullable=True)
    # What the proof showed: the linked k3s workload's own SVID response carrying the
    # dashboard's bundle (spire-federation-proof.yml), or why not. NULL = no proof run.
    federation_proof = Column(Text, nullable=True)
    # The attribute probe (docs/runbooks/spire-lab-standup.md §5): JSON holding what
    # "Prepare probe" did against Password Safe (each call's status included) and, once an
    # operator has read Verify Functional Account, the answer they recorded. L3 waits on it.
    attr_probe = Column(Text, nullable=True)

    # WHO the runner logs in as ON THE K3S NODE. A SEPARATE SET from the four
    # `ansible_*` / `login_user` fields above, which belong to the SPIRE host, because the
    # two VMs are deployed independently and **do not share an SSH key**.
    #
    # ALL NULL IS THE NORMAL STATE AND IT ALREADY WORKS: the runner auto-derives a keypair
    # from the deploy job of the VM it is CONNECTING TO (`_find_cloud_deploy_meta` matches
    # on the target address), and the Kubernetes stages target this host, so each machine
    # gets its own key with nothing configured.
    #
    # What these exist for is the explicit case. Inheriting the SPIRE host's chosen account
    # or key secret here would be the wrong fallback, not a convenient one: it would connect
    # to this VM with another VM's credential, and the failure is
    # `Permission denied (publickey)` five stages into a run that looked configured. So a
    # NULL here means auto-derive from THIS host's deploy job, never "whatever the SPIRE
    # host uses".
    k8s_ansible_secret_ssh_key_source = Column(Text, nullable=True)
    k8s_ansible_managed_account = Column(Text, nullable=True)
    k8s_ansible_managed_become_self = Column(Boolean, nullable=True)
    k8s_login_user = Column(String(104), nullable=True)

    # What the seed put in and what discovery should return. 11 in, 8 out — and the
    # count IS the assertion, not "discovery succeeded". The plugin shipped with
    # discovery defaulting its path filter to the MINTABLE prefix, which silently
    # narrowed the inventory to 2 while still reporting success.
    entries_seeded = Column(Integer, nullable=True)
    discovery_expected = Column(Integer, nullable=True)

    # Secrets Safe coordinates of the admin credential — TITLES only, never values.
    admin_secret_folder = Column(String(255), nullable=True)
    admin_spiffe_id = Column(String(255), nullable=True)
    ps_safe = Column(String(120), nullable=True)
    # Public by construction, and ~900 B for one authority / ~1.8 KB across a CA
    # rotation. It is 7x Password Safe's 255-character address column, which is why it
    # is held here and pasted rather than composed into an address like every other
    # custom plugin's configuration.
    trust_bundle_pem = Column(Text, nullable=True)
    # The REAL notAfter of the minted admin SVID, which is shorter than what was asked
    # for: SPIRE clamps a mint to the server's `ca_ttl`, so a 720h request against the
    # 168h default yields ~7 days. Once it lapses every plugin action fails
    # PERMISSION_DENIED and reads exactly like an admin_ids problem, so the date is
    # stored and surfaced rather than left in a job log.
    admin_svid_expires_at = Column(DateTime, nullable=True)

    # Password Safe onboarding. `ps_system_id` is written by `start_ps_register` and is
    # what "this trust domain is governed" means; a row carrying one an operator created by
    # hand is still recognised, which is why these predate the button.
    #
    # `ps_account_id` stays NULL BY DESIGN and is not dead weight. The SPIFFE SVID plugin
    # DISCOVERS its accounts — each is a SPIRE registration entry — so there is no single
    # account this lab owns, and creating one would move the discovery count that is the
    # lab's whole assertion (eleven seeded, eight discovered). The column remains because a
    # hand-onboarded lab may name one, and because its emptiness is the fact worth being
    # able to read.
    ps_system_id = Column(String(36), nullable=True)
    ps_account_id = Column(String(36), nullable=True)
    # Scrubbed Terraform state for the managed system, which is what makes the removal
    # deterministic: `ps_resource_service.deregister` destroys from the recorded state
    # rather than from a hand-typed id. Holding the id alone would leave nothing able to
    # remove what was created. Same split as CertLab.ps_tf_state and WorkloadK8sToken's.
    ps_tf_state = Column(Text, nullable=True)

    # WHO the Ansible runner logs in as, when the operator chose explicitly. The first
    # two are EITHER/OR — two answers to one question — and `provision` refuses both at
    # once, because a managed account's name also becomes `ansible_user`, so a run
    # carrying both would connect as one identity holding the other's key.
    #
    # All NULL is the normal state and means what every lab built before this did: the
    # runner auto-derives the keypair from this host's own deploy job. On the ROW rather
    # than in the parent job's metadata because every stage's `vars_for` builder reads
    # only the row and config (see spire_lab_service, "never the request"), which is what
    # lets a resumed provision rebuild an identical run.
    #
    # The `ansible_` prefix is load-bearing: `ps_system_id` / `ps_account_id` above are
    # the SPIFFE functional account being ONBOARDED into Password Safe, which is a
    # different account for a different purpose.
    #
    # Text, not String(n): a source is a config-secret registry key OR a raw
    # `bt_safe://<safe>/<folder>/<title>` vault ref, which has no bounded length, and the
    # ref JSON grows with the account name. Issue #830 — SQLite enforces no VARCHAR
    # length and PostgreSQL does, so a too-long value passes every test here and then
    # dies mid-INSERT with StringDataRightTruncation (see spire_lab_service.batch_id_for,
    # which is this repo's write-up of that exact failure).
    ansible_secret_ssh_key_source = Column(Text, nullable=True)
    ansible_managed_account = Column(Text, nullable=True)   # ManagedAccountRef as JSON
    # "Also use that account for sudo." All four playbooks are `become: true`, so a
    # non-root account without passwordless sudo fails on the first task. Sends the SAME
    # ref as `managed_become`; Password Safe reuses the open request rather than opening
    # a second one (btapi_service passes ConflictOption=reuse). NULL = false.
    ansible_managed_become_self = Column(Boolean, nullable=True)
    # A USERNAME, never a credential. NULL = derive it from ansible_<cloud>_user as
    # before, which is right for every host using its cloud's default login. Needed
    # because an SSH-key secret carries no user, unlike a managed account whose own name
    # becomes the login.
    login_user = Column(String(104), nullable=True)

    error_message = Column(Text, nullable=True)
    deploy_job_id = Column(String(36), nullable=True)
    workgroup = Column(String(100), nullable=True, index=True)
    created_by = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    updated_at = Column(DateTime, default=utcnow, nullable=True)
    # Auto-delete timer — NULL = never, never "inherit the default" (see Job.expires_at).
    expires_at = Column(DateTime, nullable=True, index=True)
    expiry_warned_at = Column(DateTime, nullable=True)


class AgentCell(Base):
    """One non-human principal the agent demo cell installed, and what governs it.

    **The row records an authorization, not a credential.** Same rule as ``SpireLab``,
    which holds the *titles* of its admin credential and never the PKCS#12: the raw PAT
    is shown once by ``api/tokens.create_token`` and never again, and this row carries
    only its id, its name and when it expires. The dashboard is not the vault, and a
    table that held a working token would make every backup one.

    **Two identifiers, deliberately, because they are two different things.** The SPIFFE
    ID is who the worker *is* -- attested, re-fetched every loop, stored nowhere on the
    host. The PAT is what it may *do* here -- scoped to ``pat_user_id``'s RBAC, expiring,
    revocable. They are not bridged: ``/mcp`` takes a Bearer PAT and has no mTLS path, so
    nothing mints one from the other. ``docs/profiles/demo/agent-demo-cell.md`` says why
    that gap is stated rather than closed.

    **Attached, never created** -- the host is a VM this dashboard already deployed, and
    ``host_deploy_job_id`` is the proof of it. ``spire_lab_service.resolve_host`` is what
    re-derives it, because a privileged playbook against a host of the caller's choosing
    is not something this should accept. The consequence is the good one: the host keeps
    its auto-delete timer, its Password Safe onboarding and its Destroy button, and this
    row adds no teardown of its own beyond revoking what it issued.
    """
    __tablename__ = "agent_cells"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(120), nullable=False)
    status = Column(String(32), nullable=False, default="provisioning", index=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    created_by = Column(String(100), nullable=True)
    workgroup = Column(String(100), nullable=True)

    # ── The host it attached to ──────────────────────────────────────────────
    # The deploy job is the proof the host is one this dashboard built. Resolved through
    # spire_lab_service.resolve_host rather than taken from the request.
    host_deploy_job_id = Column(String(36), nullable=True)
    host_name = Column(String(120), nullable=True)
    cloud = Column(String(20), nullable=True)
    # What the Ansible runner SSHes to. Blank is legal -- an in-subnet runner uses the
    # private address -- which is why the two are not collapsed into one column.
    private_ip = Column(String(64), nullable=True)
    public_ip = Column(String(64), nullable=True)

    # ── Identity: who the worker is ──────────────────────────────────────────
    # The SPIRE lab whose trust domain attests it, and the ID it will name in its logs.
    spire_lab_id = Column(String(36), nullable=True)
    trust_domain = Column(String(255), nullable=True)
    spiffe_id = Column(String(500), nullable=True)

    # ── Authorization: what it may do here ───────────────────────────────────
    # The PAT's ID and NAME only. Never the token. `pat_user_id` is what bounds every MCP
    # tool the worker can call, so it is the actual scope of the agent's access and the
    # first thing to look at when asking what this agent could see.
    pat_id = Column(String(36), nullable=True)
    pat_name = Column(String(100), nullable=True)
    pat_user_id = Column(String(36), nullable=True)
    # Always set by the cell, never None. A non-expiring token for a non-human principal
    # is the thing this demo argues against, so agentcell_service refuses to mint one.
    pat_expires_at = Column(DateTime, nullable=True)
    pat_revoked_at = Column(DateTime, nullable=True)
    # The OAuth client the worker authenticates with, when the cell minted one instead of
    # a PAT. Its id only -- the secret was returned once and is not here. Revoking the cell
    # deactivates that client, which ends every access token it issued on the next call.
    oauth_client_id = Column(String(36), nullable=True)

    # ── What this agent is answerable for in the Workload Lab ───────────────
    # Which lab identity an operator has made this agent answerable for, so that "what
    # does this agent have access to" is one lookup rather than a conversation.
    # `linked_mechanism` is a Workload Lab tab name; `linked_credential_id` is that tab's
    # own row.
    #
    # ALL THREE SHIPPED MECHANISMS ARE CAPABILITIES, and what they differ in is WHICH
    # AUTHORITY the worker reaches -- which is the distinction that replaced "does this
    # confer anything at all":
    #   * `kubernetes` and `certificates` reach PASSWORD SAFE holding nothing (a workload
    #     identity brokered by Workload Credentials), so the worker can genuinely REQUEST
    #     that credential -- subject to an access policy that can hold the request for a
    #     human;
    #   * `cloud` reaches WORKLOAD CREDENTIALS DIRECTLY and mints. No vault in the chain
    #     and no approval, because there is nothing standing to release -- and every
    #     mint is billed. See `agentcell_service.SPENDABLE_MECHANISMS`.
    #
    # This comment has been wrong twice, in two different ways, and both are worth
    # keeping. It said no worker could spend ANY of them, which stopped being true when
    # the worker got `--token-source ps`. It then said `cloud` was accountability only
    # because "that tab returns its credential to nobody" -- a sentence that was always
    # about the DASHBOARD, is still true of the dashboard, and was being read as a
    # statement about the mechanism. The worker calling WC itself is exactly what keeps
    # the dashboard out of that audit trail.
    # See docs/design/next-demo-cells.md sections 5b, 5d, 5e and 5f.
    linked_mechanism = Column(String(32), nullable=True)
    linked_credential_id = Column(String(36), nullable=True)
    linked_at = Column(DateTime, nullable=True)

    # ── The current cluster-access episode, and NEVER its credential ─────────
    # One bounded request at a time: the agent asks Password Safe for the linked token,
    # waits for whatever the access policy requires, probes the cluster, and gives the
    # slot back. What is recorded is the REQUEST -- its id, its state, when it started
    # and when it was released -- because that is what an operator needs to answer "is
    # something out right now" and what makes an abandoned slot findable.
    #
    # THERE IS NO TOKEN COLUMN HERE AND THERE MUST NOT BE. The same rule the four lab
    # models state: the row names the credential, Password Safe holds it. A token that
    # landed here would outlive the request that fetched it, which is the entire property
    # the recorded-request flow exists to provide.
    # "requested" or "released" -- the only two the DASHBOARD writes. The waiting, the
    # approval and the probes happen on the host, and the worker cannot report them back:
    # its PAT belongs to a non-admin user, and these endpoints need `config_mgmt:write`.
    # Granting it that so it could file progress would give a non-human principal more
    # authority than this cell argues for. The journal is the truth for what happened;
    # this answers "is something out right now". See agentcell_service.ROW_STATES.
    episode_state = Column(String(20), nullable=True)
    episode_started_at = Column(DateTime, nullable=True)
    episode_released_at = Column(DateTime, nullable=True)
    # THERE IS NO `episode_request_id` OR `episode_result` HERE, and their absence is a
    # decision rather than an omission. A first draft had both. Neither can ever be
    # filled: the WORKER opens the Password Safe request and runs the probes, so the
    # dashboard never learns the id or the outcome, and the worker has no way to tell it
    # (see above). A column that is structurally always NULL is worse than no column --
    # it reads as "nothing happened yet" on a row where plenty did, and it invites a
    # later change to grant the worker the authority needed to fill it.
    #
    # `journalctl -u mcp-agent` holds both, and the SPIFFE ID in the request's reason is
    # what correlates the two.

    # Progress through the two playbooks, as the names of the ones that finished, and
    # their job ids -- the same arrangement SpireLab uses, and for the same reason: a
    # failed stage's Ansible error exists only in that job's Live Output.
    stages_done = Column(Text, nullable=True)
    stage_job_ids = Column(Text, nullable=True)
    error_message = Column(Text, nullable=True)


class WorkloadK8sToken(Base):
    """A Password-Safe-brokered ServiceAccount token for a workload OUTSIDE the cluster.

    The Workload Lab's third answer, and the only one that works on a managed cluster.
    The SPIRE lab proves a workload can reach an API server with a credential stored
    nowhere, but that rests on ``--authentication-config``, a kube-apiserver flag — so it
    is available on self-managed k3s and on **none** of EKS, AKS or GKE. This is the path
    that is: a bound ServiceAccount token, minted by the API server, rotated and audited
    by Password Safe, retrieved by the consumer for each run.

    **One row per IDENTITY, not per cluster**, which is why this is its own table rather
    than more columns on ``K8sCluster``. That model has room for exactly one registration
    (``ps_token_account_id`` plus one state key), and it is already spoken for by the PRA
    tunnel's ``pra-access`` account. A Deployer and a Reader on the same cluster are two
    ServiceAccounts, two managed accounts and two sets of RBAC, so they are two rows.

    ``cluster_id`` references ``k8s_clusters.id`` and is deliberately **not** a foreign
    key, matching how the sibling labs reference their hosts: the two delete paths stay
    independent, so unregistering a cluster cannot cascade away the audit record of a
    token that existed, and this row's own teardown is the only thing that removes the
    ServiceAccount it created.

    **THE PROFILE IS THE POINT, not the token.** ``profile`` picks the RBAC, and the RBAC
    is the whole demonstration:

      * ``deployer`` — ClusterRole ``edit`` through a **RoleBinding** in one namespace. A
        CI build that can deploy where it is supposed to and is refused everywhere else.
      * ``reader`` — ClusterRole ``view`` through a **ClusterRoleBinding**. A fleet scan
        that can read the whole cluster and **cannot read Secrets**, because upstream
        ``view`` omits them by design.

    Neither is ``cluster-admin``. The existing ``ps-token`` path onboards a cluster-admin
    ServiceAccount because it serves a human's brokered session through PRA; a *vaulted*
    cluster-admin token is a vaulted skeleton key and demonstrates nothing about scoping.

    **Nothing secret lives on this row** — the rule both sibling models state. The
    credential is the bearer token, it is held in Password Safe, and what is here is
    ``ps_system_id`` / ``ps_account_id``: ids that NAME it. There is no kubeconfig column
    either, deliberately: a kubeconfig containing the token would be a copy of the
    credential with no expiry and no audit trail, which is the artefact this feature
    exists to remove. The consumer assembles one at run time from what it retrieved.

    **The timer is not about cost.** It is the same argument ``docs/preview/workload-lab/spiffe.md`` makes for
    the SPIRE lab: a forgotten workload token keeps authenticating, and this row is the
    only page it appears on.
    """
    __tablename__ = "workload_k8s_tokens"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(120), nullable=False)

    # The registered cluster this identity lives in. Denormalised alongside it because
    # the row has to stay readable after the cluster is unregistered: an inventory entry
    # naming a cluster id nobody can resolve is a record of nothing, and the whole reason
    # the timer exists is to be read long after the fact.
    cluster_id = Column(String(36), nullable=False, index=True)
    cluster_name = Column(String(200), nullable=True)
    cloud = Column(String(20), nullable=True)

    # deployer | reader. See the class docstring — this selects the RBAC, and changing it
    # on an existing row would leave the old binding in the cluster, so it is set once.
    profile = Column(String(20), nullable=False, default="deployer")
    namespace = Column(String(253), nullable=False, default="default")
    service_account = Column(String(253), nullable=False)

    # bound | longlived, and the reason to record it is that it changes what the ROTATOR
    # is granted: bound gets `serviceaccounts/token` create and no access to Secrets at
    # all. `ttl_seconds` is meaningful in bound mode only, where the API server's own
    # floor is 600 (ps_k8s_token_service._MIN_BOUND_TTL clamps it).
    mode = Column(String(20), nullable=False, default="bound")
    ttl_seconds = Column(Integer, nullable=True)

    # Password Safe coordinates — IDS ONLY, never the credential. `ps_account_id` being
    # set is what "onboarded" means, and it is what the rotate path acts on.
    ps_system_id = Column(String(64), nullable=True)
    ps_account_id = Column(String(64), nullable=True, index=True)
    # The managed system address the plugin parses, e.g. `eks;us-east-2;prod;bound;ttl=600`.
    # Stored because the operator has to be able to see it: when it disagrees with the
    # cluster the rotation fails inside Password Safe, where this dashboard sees only a
    # generic failure.
    ps_address = Column(String(255), nullable=True)
    # `<namespace>/<serviceaccount>` — the managed ACCOUNT name, which is a name and not
    # a credential. Recorded so the consumer playbooks can be told what to look up.
    ps_account_name = Column(String(255), nullable=True)
    # The scrubbed Terraform state for the managed system + account, which is what makes
    # the teardown deterministic: `ps_resource_service.deregister` is fed the recorded
    # state rather than hand-typed ids, exactly as the CA and cluster paths are. Holding
    # the ids alone would leave nothing able to remove them — the ids are for display and
    # for the rotate call, this is what destroys. Scrubbed, so it carries no credential
    # (see CertLab.ps_tf_state, the same split for the same reason).
    ps_tf_state = Column(Text, nullable=True)
    # Whether a rotation has ever completed. A bearer token is far longer than the 128
    # characters Password Safe's create API accepts, so the account is always created
    # holding a placeholder — until this is true, the vault holds nothing that
    # authenticates, and that is indistinguishable from success on the page.
    rotated = Column(Boolean, nullable=True)

    status = Column(String(32), nullable=False, default="onboarding", index=True)
    error_message = Column(Text, nullable=True)
    # Progress through the onboarding steps, as the names of the ones that finished, and
    # the job ids behind them. Same purpose as the sibling labs': a half-failed onboard
    # has to be distinguishable from one that never started, and a failed step's error
    # exists only in its own job log.
    stages_done = Column(Text, nullable=True)
    stage_job_ids = Column(Text, nullable=True)

    workgroup = Column(String(100), nullable=True, index=True)
    created_by = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    updated_at = Column(DateTime, default=utcnow, nullable=True)
    # Auto-delete timer — NULL = never, never "inherit the default" (see Job.expires_at).
    expires_at = Column(DateTime, nullable=True, index=True)
    expiry_warned_at = Column(DateTime, nullable=True)


class WorkloadCloudCredential(Base):
    """A short-lived AWS or Azure credential for a workload, minted by Workload Credentials.

    The Workload Lab's fourth answer, and the one that replaces the most common non-human
    credential in existence: an `AWS_ACCESS_KEY_ID` sitting in a CI secret store with no
    expiry, no revocation, and no record of who read it. Workload Credentials mints an
    assumed-role triple (AWS) or a service-principal secret (Azure) on demand, against a
    **dynamic secret** an operator defined in WC, and hands back a LEASE.

    **THE SCOPE IS NOT DEFINED HERE, and that is the honest difference from the sibling
    tabs.** The Kubernetes tab picks a RoleBinding and the Certificate tab picks a profile,
    so the dashboard decides what the identity may do. Here the dynamic secret's own
    definition in Workload Credentials decides — which role is assumed, which subscription,
    which permissions. This row names the secret it draws from; it cannot widen or narrow
    it, and it must not pretend to.

    **NOT the dashboard's own lease.** ``workload_credential_lease`` is a singleton per
    ``(cloud, purpose)`` holding the credential THIS APPLICATION uses for its own cloud
    calls, configured by ``wlc_{cloud}_secret_name``. Minting into it from here would
    overwrite a working credential the dashboard is mid-deployment with — and issuance is
    billed, so it would also charge for the privilege. Rows here call
    ``workload_credentials_service`` directly and share none of that state.

    **Nothing secret lives on this row.** ``lease_id`` is a correlation handle, not a
    credential: it identifies an issuance in Workload Credentials' own audit log and is
    what a revoke acts on. The access key, secret, session token, client secret and tenant
    id are returned to the caller that asked and are never written here.

    **REVOCATION IS ASYMMETRIC AND THE ROW RECORDS WHICH.** Azure leases can be released
    early. AWS cannot — STS refuses with ``lease_not_revocable``, because a credential it
    has already signed cannot be withdrawn before its expiry. So on AWS the TTL is the only
    control, which makes a short one matter more there rather than less. ``revocable`` is
    stored rather than inferred at render time so the page cannot promise a kill switch the
    provider does not have.
    """
    __tablename__ = "workload_cloud_credentials"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(120), nullable=False)

    # aws | azure. Decides the credential SHAPE (an assumed-role triple versus a
    # service-principal secret) and whether a lease can be revoked at all.
    cloud = Column(String(20), nullable=False, index=True)

    # The Workload Credentials dynamic secret this identity draws from — a FOLDER and a
    # NAME, which are addresses and not secrets. The scope lives behind them, in WC.
    dynamic_name = Column(String(255), nullable=False)
    dynamic_folder = Column(String(255), nullable=True)
    # What this identity is for, in the operator's words. Free text on purpose: the
    # dashboard cannot enforce a purpose here (see the class docstring), so claiming a
    # closed vocabulary would imply a guarantee it cannot make.
    purpose = Column(String(120), nullable=True)
    # The TTL asked for. Recorded because what comes BACK may differ — the provider clamps,
    # and AWS caps a role-chained credential at one hour — so the gap between requested and
    # actual is worth being able to see.
    ttl_seconds = Column(Integer, nullable=True)

    # ── the current lease ────────────────────────────────────────────────────
    # A correlation handle to a LIVE credential, not the credential. Cleared when the lease
    # is revoked or retired so the row never names an issuance that no longer exists.
    lease_id = Column(String(128), nullable=True, index=True)
    lease_issued_at = Column(DateTime, nullable=True)
    # The provider's OWN expiry, as returned. Never computed from the requested TTL: the two
    # disagree whenever the provider clamps, and a page showing the ask rather than the
    # answer would tell an operator the credential lives longer than it does.
    lease_expires_at = Column(DateTime, nullable=True)
    # Whether THIS cloud's leases can be released early. See the class docstring.
    revocable = Column(Boolean, nullable=True)
    # How many times this identity has minted. Issuance is METERED, so this is a cost
    # figure as much as an audit one — and a number climbing on an identity nobody is using
    # is the specific shape of a misconfigured consumer retrying.
    issue_count = Column(Integer, nullable=False, default=0)

    status = Column(String(32), nullable=False, default="registered", index=True)
    error_message = Column(Text, nullable=True)
    # The issue/revoke job ids, in order, so each attempt's output stays readable.
    job_ids = Column(Text, nullable=True)

    workgroup = Column(String(100), nullable=True, index=True)
    created_by = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    updated_at = Column(DateTime, default=utcnow, nullable=True)
    # Auto-delete timer — NULL = never, never "inherit the default" (see Job.expires_at).
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
