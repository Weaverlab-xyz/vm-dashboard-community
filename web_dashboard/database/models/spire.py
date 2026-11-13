"""SPIRE lab, agent cell and workload credential models."""
import uuid
from sqlalchemy import Column, String, Integer, DateTime, Boolean, Text

from ...utils.clock import utcnow
from .._engine import Base


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
    # True when the cell CREATED its token user -- the per-cell service account a Demo
    # Presenter's mint makes (api/agentcell.create_agent). Revoking the cell then disables
    # that account too, since nothing else owns it. Bare BOOLEAN, no default, for the
    # PostgreSQL reason on users.is_admin; NULL reads as False, every existing row.
    owns_agent_account = Column(Boolean, nullable=True)

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
