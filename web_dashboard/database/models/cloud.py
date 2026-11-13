"""Cloud resource models: state and cost caches, workload credential leases, dashboard stats, images, virtual desktops, cloud databases."""
import uuid
from sqlalchemy import Column, String, Integer, DateTime, Boolean, Text, ForeignKey

from ...utils.clock import utcnow
from .._engine import Base


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
    last_updated = Column(DateTime, default=utcnow, nullable=False, index=True)


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

    updated_at = Column(DateTime, default=utcnow, nullable=False)


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
    # the duration of a job. See docs/beyondtrust/workload-credentials.md.
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

    updated_at = Column(DateTime, default=utcnow, nullable=False)


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

    updated_at = Column(DateTime, default=utcnow, nullable=False)


class PovPlatformCache(Base):
    """One lab platform's last-known-good inventory listing — its environments or its
    templates — plus the throttle state that decides whether we may ask it again.

    Same three reasons as :class:`CloudCostCache` and :class:`DashboardStatCache`; read
    ``services/cost_cache``'s module docstring first, because this is deliberately the
    same shape. What makes it necessary here is who the caller is: the POV page's two
    platform listings went out on an INSTANCE-WIDE credential, so every SE on the install
    shared one account's rate limit, and the number of calls scaled with how many of them
    happened to have the page open. Skytap answers a saturated account with 423 plus a
    Retry-After — a property of the account, not of a process, so "we were just throttled"
    has to outlive a redeploy or every image rebuild re-earns it.

    NOT a second use of :class:`DashboardStatCache`, though the columns would have fitted.
    That table is read with a single ``read_all`` and maintained with table-wide
    ``mark_stale`` / ``clear_cooldowns``, and ``api/dashboard.dashboard_refresh`` derives
    its minimum-refresh floor from the NEWEST row in the whole table. Rows for a POV
    listing would therefore have suppressed the dashboard's Refresh button whenever
    somebody opened /pov — one store serving two unrelated consumers, where a maintenance
    call on either silently reaches the other.

    ``payload`` is written ONLY on a successful listing. A failure writes the error and
    cooldown columns and leaves ``payload``/``fetched_at`` exactly where they were. That
    asymmetry is the reason this is a table and not a cache entry with a TTL: a 423 during
    a rate-limit window must not be able to blank the environments table for a TTL, and a
    generic cache cannot tell that answer from an empty account.
    """
    __tablename__ = "pov_platform_cache"

    # environments | templates. The two collection reads templates/pov/index.html makes.
    kind = Column(String(24), primary_key=True)
    # The lab platform, e.g. "skytap" | "azure". Also what pacing keys on, so a platform
    # whose account is saturated is left alone for BOTH its listings at once.
    platform = Column(String(24), primary_key=True)
    # The configured project id the listing was taken under, "" for the account-wide
    # scope. IN THE PRIMARY KEY and load-bearing: `skytap_project_id` is a Settings field,
    # blank is the widest scope, and a key that omitted it would serve the previous
    # project's environment names — on a shared lab account, other customers' POVs —
    # until the TTL expired. Widening a primary key later is not an ALTER.
    project_id = Column(String(64), primary_key=True, default="")

    # ── last-known-good (written ONLY on a successful listing) ───────────────
    payload = Column(Text, nullable=True)          # JSON list, shape owned by the adapter
    payload_version = Column(Integer, nullable=False, default=0)
    fetched_at = Column(DateTime, nullable=True, index=True)
    # Set by a Settings save. Read as "not fresh" while the payload is still SERVED, so
    # fixing a credential re-lists without emptying the table in the meantime.
    stale = Column(Boolean, nullable=False, default=False)

    # ── failure / backoff (written ONLY on a failed attempt) ─────────────────
    last_attempt_at = Column(DateTime, nullable=True)
    last_error = Column(Text, nullable=True)
    consecutive_failures = Column(Integer, nullable=False, default=0)
    # Hard gate: while this is in the future nothing lists this platform, including an
    # explicit ?refresh=true. Set from the platform's own Retry-After when it gave one.
    cooldown_until = Column(DateTime, nullable=True, index=True)

    # ── single-flight ────────────────────────────────────────────────────────
    # A liveness bound, not a mutex: a process that dies mid-listing releases its claim by
    # expiry. The advisory lock only makes the claim's read-modify-write atomic; it is
    # never held across the network call.
    lease_until = Column(DateTime, nullable=True)
    lease_owner = Column(String(64), nullable=True)   # "host:pid" — diagnostics only
    next_query_allowed_at = Column(DateTime, nullable=True)

    updated_at = Column(DateTime, default=utcnow, nullable=False)


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
    # The POV standard recipe that built it ("standard-linux" | "standard-windows",
    # services/pov_image_recipes), or NULL for anything else. Display and one behaviour:
    # a standard-linux target gets the POV's own login at launch (pov_cloud_aws).
    recipe = Column(String(40), nullable=True)

    # Per-target promotion records. Shape:
    #   { "aws":   {"image_id": "ami-…", "region": "us-east-2", "status": "completed"|"manual"|"failed", "notes": "..."},
    #     "azure": {"image_id": "/subscriptions/.../images/…", "status": ...},
    #     "gcp":   {"self_link": "...", "status": ...} }
    promotions = Column(Text, nullable=True)                 # JSON

    created_at = Column(DateTime, default=utcnow, nullable=False)
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

    One row per desktop (seat), not per pool. AWS, Azure and GCP each provision one
    private VM per seat through their own seat backend in ``vdesktop_service`` and
    fill ``vm_resource_id``, then broker the seat on the PRA Gateway and fill
    ``pra_jump_id``.

    Two things about this table are load-bearing and not obvious from the columns:

      * ``cloud`` is an unconstrained ``String(20)``. Code that dispatches on it must
        tolerate a value with no seat backend rather than assume one of three.
      * The POOL TAG stamped on the backing VM is not one string. Azure and EC2 take
        ``dashboard:desktop_pool``; a colon is ILLEGAL in a GCP label key, so GCP uses
        ``dashboard_desktop_pool``. Each backend owns its own.
    """
    __tablename__ = "virtual_desktops"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    cloud = Column(String(20), nullable=False)              # aws | azure | gcp
    pool_name = Column(String(200), nullable=False, index=True)
    # Backing kind. Only vm_pool exists; avd / workspaces are the cloud-native
    # services this table was shaped to leave room for.
    kind = Column(String(20), nullable=False, default="vm_pool")
    # Cloud-native id of the backing VM once provisioned. Null until then. The shape
    # is per cloud and carries what teardown needs, since teardown gets only this
    # string: an Azure ARM id, "<region>/<instance-id>" on AWS,
    # "<project>/<zone>/<name>" on GCP.
    vm_resource_id = Column(String(500), nullable=True)
    # pending | running | stopped | deprovisioning
    status = Column(String(20), nullable=False, default="pending", index=True)
    assigned_user = Column(String(200), nullable=True)
    # PRA jump-item id once the seat is brokered. NULL until then, and NULL rather
    # than "" on a failed registration: jumpoint_host_service._active_vdesktop_count
    # counts a non-NULL value as a live reference and would pin the shared Gateway.
    pra_jump_id = Column(String(200), nullable=True)
    # Scrubbed Terraform state for the seat's PRA jump item (a Remote RDP jump plus
    # its vault account for a Windows seat, a Shell Jump for a Linux one) so teardown
    # can destroy it deterministically. `vdesktop_service.jump_kind` reads WHICH kind
    # off this state rather than storing it in a column that could disagree. Secret
    # values are redacted before storage; never returned by the API.
    pra_tunnel_state = Column(Text, nullable=True)
    created_by = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)


class CloudDatabase(Base):
    """Inventory of dashboard-provisioned managed databases — cloud-database
    infrastructure, Phase 1.

    One row per provisioned or registered database (Postgres / MySQL / SQL Server /
    Oracle / MongoDB), always **private** and reached only through a BeyondTrust PRA
    tunnel. In the community edition the PRA tunnel (Phase 2) is brokered with the
    ``beyondtrust/sra`` Terraform provider (``terraform_pra_service``) — never
    ``btapi``. Oracle and MongoDB ride the provider's generic tcp tunnel until it
    ships their dedicated PRA 26.3 tunnel resources.
    The PRA / Password-Safe fields are populated by later phases:
    ``jump_item_id`` by the tunnel brokering (Phase 2); ``ps_*`` are unused in
    community (Password-Safe onboarding is a prod-only path).
    """
    __tablename__ = "cloud_databases"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    engine = Column(String(20), nullable=False)            # postgres | mysql | sqlserver | oracle | mongodb
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
    jump_group = Column(String(128), nullable=True)        # PRA Asset Group name override (else bt_jump_group_name)
    jumpoint_name = Column(String(128), nullable=True)     # PRA Jumpoint name override (else bt_jumpoint_name)
    pra_credential_ref = Column(String(256), nullable=True)  # secret ref → bt_client_secret override
    entitle_integration_id = Column(String(64), nullable=True)  # Entitle DB integration registered on apply

    # Password Safe DB onboarding (optional; the AWS SSM / Azure Run Command custom
    # plugins). Ids, not credentials — exactly what an operator reads back in the
    # Password Safe UI — so the Databases page can show whether a row is onboarded
    # without loading every clouddb_provision job on each poll.
    #
    # The AUTHORITATIVE teardown state stays on the provisioning job's metadata
    # (ps_db_* / ps_pravault_*, which decommission reads); these two are written in
    # the same commit as that stash and cleared by the same teardown, so the pair
    # cannot drift into disagreeing about whether onboarding happened.
    ps_managed_system_id = Column(String(64), nullable=True)
    ps_managed_account_id = Column(String(64), nullable=True)

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

    # Where a cloud database lives, for Config-Management routes (migration 0008). A route
    # governs a cloud database when its range contains EVERY one of `route_cidrs`: the DB
    # subnet group's subnets on AWS (a failover may land in any), the delegated subnet on
    # Azure, the private IP as a /32 on GCP. Read from the cloud by clouddb_network, at
    # provisioning or by the route network backfill. NULL means not recorded, and such a
    # database matches no route: it stays on the remote worker.
    route_cidrs = Column(Text, nullable=True)              # JSON list of CIDRs
    route_network = Column(String(200), nullable=True)     # cmr.normalize_network form
    # The workstream it was provisioned in. reassign_database_workgroup moves `workgroup`
    # but never this, for the reason a VM deploy keeps `deploy_workgroup`: a route narrowed
    # to a workstream stands for that team's network, which a reassignment does not move.
    deploy_workgroup = Column(String(100), nullable=True)

    # Workgroup scoping. NULL (the state every row predating this column is in)
    # falls back to creator-scoping in inventory_service.visible_to, so adding the
    # column changed nobody's access -- a row becomes shared only when someone tags it.
    workgroup = Column(String(100), nullable=True, index=True)
    created_by = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    # Auto-delete timer — NULL = never (see Job.expires_at). Only ever stamped on a
    # `provisioned` row: "deleting" a registered database merely deregisters it, and a
    # timer that silently forgets somebody's registered production database is worse
    # than no timer at all.
    expires_at = Column(DateTime, nullable=True, index=True)
    expiry_warned_at = Column(DateTime, nullable=True)
    # Stop/start bookkeeping (services/clouddb_power), JSON: the status to return to on
    # start, when it was stopped (AWS RDS and Azure Flexible Server restart a stopped
    # instance on their own after 7 days -- clouddb_power.needs_restop reads this), the
    # last power job and its error. NULL means never powered, which reads as running.
    # Migration 0015.
    power_state = Column(Text, nullable=True)
