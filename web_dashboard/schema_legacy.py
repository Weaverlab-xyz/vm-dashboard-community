"""init_db's original schema statements: FROZEN. Do not add to, edit or reorder this list.

Every statement here has shipped. ``init_db`` runs the list on each boot until one boot
leaves the database with everything it adds, then records
``schema_migrations.LEGACY_KEY`` and never reads it again. So a statement added here
would run on new installs and never on upgraded ones. A new schema change is a
``Migration`` in ``schema_migrations.MIGRATIONS``.

Most statements fail harmlessly once their column or index exists ("already exists"),
and each runs in its own savepoint for that reason. The comments are the record of why
each one is written the way it is, which is why they moved here with it.

tests/test_schema_migrations.py holds the list to its count; tests/test_postgres_migrations.py
runs every ``ADD COLUMN`` against a real PostgreSQL.
"""

STATEMENTS = (
        # `false`, not `0`. PostgreSQL will not coerce an integer literal to a
        # boolean default and rejects the whole statement; SQLite accepts `0`
        # happily, so this was green everywhere and silently skipped on Postgres.
        # Because each statement runs in its own savepoint the rollback said
        # nothing, and the column simply never appeared. Harmless on a fresh
        # install -- `create_all` builds `users` from the model with `is_admin`
        # already on it, so this ALTER is a no-op there -- but an install that
        # PREDATES the column never got it, and every query against `users`
        # would then fail with UndefinedColumn. Found by
        # tests/test_postgres_migrations.py, which is the only thing in the tree
        # that runs these statements against a real PostgreSQL.
        "ALTER TABLE users ADD COLUMN is_admin BOOLEAN DEFAULT false",
        "ALTER TABLE users ADD COLUMN permissions TEXT",
        "ALTER TABLE users ADD COLUMN session_permissions TEXT",
        # POV accessors: a prospect's ephemeral login, bound to one POV. See the
        # column's comment on why it is a deny marker rather than a permission.
        "ALTER TABLE users ADD COLUMN accessor_env_id VARCHAR(36)",
        "CREATE INDEX ix_users_accessor_env_id ON users(accessor_env_id)",
        # The per-POV Entitle REST integration that mints accessors.
        "ALTER TABLE pov_environments ADD COLUMN accessor_integration_id VARCHAR(64)",
        "ALTER TABLE pov_environments ADD COLUMN accessor_tf_state TEXT",
        "ALTER TABLE oauth_group_mappings ADD COLUMN default_permissions TEXT",
        # Persona (curation only -- neither of these can grant or deny anything).
        # Two user columns because the login path REWRITES the group-derived one on
        # every login and must never touch the admin-set one; see the columns' own
        # comment on User for why that split is load-bearing.
        "ALTER TABLE users ADD COLUMN persona VARCHAR(32)",
        "ALTER TABLE users ADD COLUMN session_persona VARCHAR(32)",
        "ALTER TABLE oauth_group_mappings ADD COLUMN persona VARCHAR(32)",
        "ALTER TABLE oauth_group_mappings ADD COLUMN persona_priority INTEGER",
        # The POV checklist stopped being grouped by role, so the column that records
        # which group a card was ticked under is no longer a persona key. A RENAME
        # rather than an add-and-backfill because the column is NOT NULL: leaving the
        # old one in place would make every new insert supply a value nothing means.
        #
        # Not idempotent, like the bare CREATE INDEX statements above -- it fails on
        # every boot after the first and the per-statement savepoint swallows it. Safe
        # to fail: nothing READS this column (pov_summary groups off the live registry),
        # and set_state rewrites it on every save.
        "ALTER TABLE pov_use_case_progress RENAME COLUMN persona TO group_key",
        # The `arn=` an awspca certificate address is built from. See CertLab.ca_arn
        # for why it is not pool_id wearing a second hat.
        "ALTER TABLE cert_labs ADD COLUMN ca_arn VARCHAR(255)",
        # The Certificate plugin split its subordinate-CA half into a second
        # .psplugin, so a CA now has TWO Password Safe platforms to serve and a
        # functional account on each. See the columns' own comments on CertLab.
        #
        # `ca_path_length` backfills to 0 by the DEFAULT, and that is correct rather
        # than merely convenient: every root built before this column existed was
        # created with max_issuer_path_length = 0 and genuinely cannot sign a
        # subordinate. A NULL backfill would read as "unknown" and let the identity
        # form offer a subordinate against a root that will refuse it at the CA.
        "ALTER TABLE cert_labs ADD COLUMN ca_path_length INTEGER DEFAULT 0",
        "ALTER TABLE cert_labs ADD COLUMN ps_subca_functional_account VARCHAR(255)",
        "ALTER TABLE cert_labs ADD COLUMN ps_subca_functional_account_id VARCHAR(36)",
        # Which package the registered managed system is on. NULL on every existing
        # row, which the readers treat as the leaf package — what they all are.
        "ALTER TABLE cert_labs ADD COLUMN ps_package VARCHAR(16)",
        "ALTER TABLE jobs ADD COLUMN suspend_at_local VARCHAR(5)",
        "ALTER TABLE jobs ADD COLUMN resume_at_local VARCHAR(5)",
        "ALTER TABLE jobs ADD COLUMN schedule_timezone VARCHAR(64)",
        "ALTER TABLE jobs ADD COLUMN schedule_days VARCHAR(7)",
        "ALTER TABLE jobs ADD COLUMN schedule_last_checked_at TIMESTAMP",
        "ALTER TABLE jobs ADD COLUMN spend_cap_usd FLOAT",
        "ALTER TABLE jobs ADD COLUMN spend_estimate_usd FLOAT",
        "ALTER TABLE jobs ADD COLUMN spend_accrued_at TIMESTAMP",
        "ALTER TABLE jobs ADD COLUMN spend_warned_at TIMESTAMP",
        "ALTER TABLE jobs ADD COLUMN spend_capped_at TIMESTAMP",
        "ALTER TABLE jobs ADD COLUMN attempts INTEGER DEFAULT 0",
        "ALTER TABLE jobs ADD COLUMN retry_after TIMESTAMP",
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
        "ALTER TABLE cloud_databases ADD COLUMN ps_managed_system_id VARCHAR(64)",
        "ALTER TABLE cloud_databases ADD COLUMN ps_managed_account_id VARCHAR(64)",
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
        # Unattended check-ins (hypervisor inventory_sync today), so /jobs can hide
        # the completed ones. No DEFAULT clause, for the reason spelled out on
        # users.is_admin at the top of this list: PostgreSQL rejects a defaulted
        # ADD COLUMN in this tree and the savepoint swallows the rollback, so the
        # column would silently never appear. Every existing row reads NULL, which
        # is exactly right — none of them were classified, and NULL means "shown".
        "ALTER TABLE jobs ADD COLUMN is_checkin BOOLEAN",
        "CREATE INDEX ix_jobs_is_checkin ON jobs(is_checkin)",
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
        # On-prem directories reached through a remote agent (directory_service.
        # register_onprem). No-ops on a table create_all made with them already.
        "ALTER TABLE managed_directories ADD COLUMN agent_id VARCHAR(36)",
        "CREATE INDEX ix_managed_directories_agent_id ON managed_directories(agent_id)",
        "ALTER TABLE managed_directories ADD COLUMN host VARCHAR(255)",
        "ALTER TABLE managed_directories ADD COLUMN port INTEGER",
        "ALTER TABLE managed_directories ADD COLUMN use_ldaps BOOLEAN",
        "ALTER TABLE managed_directories ADD COLUMN base_dn VARCHAR(255)",
        "ALTER TABLE managed_directories ADD COLUMN credentials_ref TEXT",
        "ALTER TABLE managed_directories ADD COLUMN linked_directory_id VARCHAR(36)",
        # Directory health's domain controller deep check (directory_health_service).
        "ALTER TABLE directory_health ADD COLUMN deep_job_id VARCHAR(36)",
        "CREATE INDEX ix_directory_health_deep_job_id ON directory_health(deep_job_id)",
        "ALTER TABLE directory_health ADD COLUMN deep_since TIMESTAMP",
        "ALTER TABLE directory_health ADD COLUMN deep_queued_at TIMESTAMP",
        "CREATE INDEX ix_managed_directories_linked_directory_id ON managed_directories(linked_directory_id)",
        # Cloud identity providers (directory_service.register_idp). NULL on every
        # existing row, and writes_enabled NULL reads as False.
        "ALTER TABLE managed_directories ADD COLUMN endpoint VARCHAR(255)",
        "ALTER TABLE managed_directories ADD COLUMN tenant_id VARCHAR(64)",
        "ALTER TABLE managed_directories ADD COLUMN client_id VARCHAR(255)",
        "ALTER TABLE managed_directories ADD COLUMN auth_mode VARCHAR(32)",
        "ALTER TABLE managed_directories ADD COLUMN writes_enabled BOOLEAN",
        "ALTER TABLE managed_directories ADD COLUMN options TEXT",
        # LDAP vendor and the pinned Entitle integration. NULL on existing rows.
        "ALTER TABLE managed_directories ADD COLUMN vendor VARCHAR(32)",
        "ALTER TABLE managed_directories ADD COLUMN entitle_integration_id VARCHAR(64)",
        "ALTER TABLE managed_directories ADD COLUMN entitle_integration_name VARCHAR(255)",
        # VyOS site links (vyos_link_service). NULL on every other row.
        "ALTER TABLE managed_directories ADD COLUMN link_config TEXT",
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
        # SPIRE attestation. Both NULL on every existing row, which reads as "Ed25519,
        # not bound to SPIRE" -- exactly what those agents are. Not idempotent, like
        # the bare CREATE INDEX statements above: it fails harmlessly on an install
        # where create_all already built the index from the model.
        "ALTER TABLE remote_agents ADD COLUMN spiffe_id VARCHAR(255)",
        "ALTER TABLE remote_agents ADD COLUMN auth_mode VARCHAR(16)",
        "CREATE UNIQUE INDEX ix_remote_agents_spiffe_id ON remote_agents(spiffe_id)",
        # `hypervisor_connections` needs no entry: create_all makes new tables.
        # Nor does `ephemeral_state` (FIDO2 challenges + OAuth/OIDC CSRF state),
        # for the same reason. Nothing backfills that one either — every row it
        # will ever hold expires within five minutes of being written.
        # Nor does `config_mgmt_routes`, and empty is the load-bearing default
        # there: no rows means every Config-Management run still goes to the agent
        # whose connection discovered the VM, which is the behaviour that predates
        # the table. A backfill would have to invent a routing decision nobody made.
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
        # POV slice 7: when the published share link expires. The first ALTER any
        # pov_* table has needed — slices 1-6 all shipped their columns in the
        # original create_all, so an install from before this one has the table but
        # not this column. Backfills to NULL, which reads as "no link published".
        "ALTER TABLE pov_environments ADD COLUMN share_expires_at TIMESTAMP",
        # The reconcile sweep's four columns. Every one backfills to NULL, which reads
        # as "the sweep has not seen this row yet" — the correct answer for an estate
        # that predates it, and what keeps the first pass from inventing history.
        "ALTER TABLE pov_environments ADD COLUMN platform_seen_at TIMESTAMP",
        "ALTER TABLE pov_environments ADD COLUMN rate_limited BOOLEAN",
        "ALTER TABLE pov_environments ADD COLUMN platform_missing BOOLEAN",
        "ALTER TABLE pov_environments ADD COLUMN suspend_on_idle_seconds INTEGER",
        # The dashboard-driven suspend schedule. Every one backfills to NULL,
        # which reads as "no schedule" — so enabling this on an existing estate
        # suspends nothing until somebody sets one, by construction rather than
        # by a guard. Same rule `expires_at IS NULL` follows.
        "ALTER TABLE pov_environments ADD COLUMN suspend_at_local VARCHAR(5)",
        "ALTER TABLE pov_environments ADD COLUMN resume_at_local VARCHAR(5)",
        "ALTER TABLE pov_environments ADD COLUMN schedule_timezone VARCHAR(64)",
        "ALTER TABLE pov_environments ADD COLUMN schedule_days VARCHAR(7)",
        "ALTER TABLE pov_environments ADD COLUMN schedule_last_checked_at TIMESTAMP",
        # The two functional accounts a POV mints for itself. Backfill to NULL, which
        # reads as "this dashboard created none" — correct for every POV wired before
        # this landed, and what keeps the first teardown after it from deleting an
        # account somebody typed on the tenant by hand.
        "ALTER TABLE pov_environments ADD COLUMN ps_linux_functional_account_id INTEGER",
        "ALTER TABLE pov_environments ADD COLUMN ps_windows_functional_account_id INTEGER",
        # All NULL backfills to "never checked". See PovEnvironment.pra_ps_checked_at.
        "ALTER TABLE pov_environments ADD COLUMN pra_ps_matched_count INTEGER",
        "ALTER TABLE pov_environments ADD COLUMN pra_ps_linked_count INTEGER",
        "ALTER TABLE pov_environments ADD COLUMN pra_ps_checked_at TIMESTAMP",
        "ALTER TABLE pov_blueprints ADD COLUMN suspend_at_local VARCHAR(5)",
        "ALTER TABLE pov_blueprints ADD COLUMN resume_at_local VARCHAR(5)",
        "ALTER TABLE pov_blueprints ADD COLUMN schedule_timezone VARCHAR(64)",
        "ALTER TABLE pov_blueprints ADD COLUMN schedule_days VARCHAR(7)",
        # The spend cap. Every one backfills to NULL, which reads as "no cap" —
        # so enabling this on an existing estate accrues against nothing until
        # somebody sets one.
        "ALTER TABLE pov_environments ADD COLUMN spend_cap_usd FLOAT",
        "ALTER TABLE pov_environments ADD COLUMN spend_estimate_usd FLOAT",
        "ALTER TABLE pov_environments ADD COLUMN spend_accrued_at TIMESTAMP",
        "ALTER TABLE pov_environments ADD COLUMN spend_warned_at TIMESTAMP",
        "ALTER TABLE pov_environments ADD COLUMN spend_capped_at TIMESTAMP",
        "ALTER TABLE pov_blueprints ADD COLUMN spend_cap_usd FLOAT",
        # NULL backfills to "optional", PRA's own default, so no existing POV's vendor
        # policy changes when this lands. See PovEnvironment.pra_require_totp.
        "ALTER TABLE pov_environments ADD COLUMN pra_require_totp BOOLEAN",
        # NULL backfills to "the operator created this Gateway", so no existing
        # POV's Gateway becomes deletable when this lands. See
        # PovEnvironment.pra_gateway_id.
        "ALTER TABLE pov_environments ADD COLUMN pra_gateway_id VARCHAR(64)",
        # The POV's chosen use cases. NULL backfills to "nobody has chosen", which
        # reads as the whole catalog, so no existing POV's checklist changes when this
        # lands. See PovEnvironment.use_case_products.
        "ALTER TABLE pov_environments ADD COLUMN use_case_products VARCHAR(64)",
        "ALTER TABLE pov_environments ADD COLUMN use_case_excluded TEXT",
        "ALTER TABLE pov_blueprints ADD COLUMN use_case_products VARCHAR(64)",
        "ALTER TABLE pov_blueprints ADD COLUMN use_case_excluded TEXT",
        # NULL backfills to "choose by guest OS", so no existing POV changes
        # behaviour when this lands. See PovEnvironmentVM.login_username.
        "ALTER TABLE pov_environment_vms ADD COLUMN login_username VARCHAR(104)",
        # What an operator says a guest runs when the platform reported nothing.
        # NULL backfills to "believe the platform", so no existing POV changes
        # behaviour when this lands. See PovEnvironmentVM.os_family_override.
        "ALTER TABLE pov_environment_vms ADD COLUMN os_family_override VARCHAR(16)",
        # Demo-cell roles on a POV. NULL backfills to "an ordinary target", so no
        # existing POV or template changes behaviour when this lands. See
        # PovEnvironmentVM.cell_role.
        "ALTER TABLE pov_environment_vms ADD COLUMN cell_role VARCHAR(32)",
        "ALTER TABLE pov_environment_vms ADD COLUMN cell_artifacts TEXT",
        "ALTER TABLE pov_cloud_template_vms ADD COLUMN cell_role VARCHAR(32)",
        # Purdue zones on a cloud template. NULL backfills to the single subnet every
        # existing template builds. See PovCloudTemplate.purdue_zones.
        "ALTER TABLE pov_cloud_templates ADD COLUMN purdue_zones BOOLEAN",
        # The functional account a CA's identities onboard against, and — only when
        # the dashboard minted it — its id. Both backfill to NULL, which is right
        # for every CA built before this: their account was made by hand, so
        # nothing may delete it, and Add identity keeps falling back to
        # cert_ps_functional_account exactly as it did. See CertLab.
        "ALTER TABLE cert_labs ADD COLUMN ps_functional_account VARCHAR(255)",
        "ALTER TABLE cert_labs ADD COLUMN ps_functional_account_id VARCHAR(36)",
        # SPIRE lab: the connection identity the operator picked for its four Ansible
        # runs. `spire_labs`' first migration entries — the table has only ever
        # arrived via create_all, so an existing install has the model attributes and
        # not the columns without these. All NULL backfills to "auto-derive the key
        # from the host's own deploy job", which is what every lab built before this
        # did. See SpireLab.ansible_secret_ssh_key_source for why the two refs are
        # TEXT rather than VARCHAR(n).
        #
        # Bare BOOLEAN, deliberately no `DEFAULT 0`: PostgreSQL rejects an integer
        # default on a boolean column, the per-statement savepoint would roll the
        # whole ALTER back, and the column would silently never appear — a
        # Postgres-only breakage invisible to a SQLite test run. (The `is_admin`
        # entry above gets away with `DEFAULT 0` only because create_all already made
        # that column, so its ALTER never actually executes.)
        "ALTER TABLE spire_labs ADD COLUMN ansible_secret_ssh_key_source TEXT",
        "ALTER TABLE spire_labs ADD COLUMN ansible_managed_account TEXT",
        "ALTER TABLE spire_labs ADD COLUMN ansible_managed_become_self BOOLEAN",
        "ALTER TABLE spire_labs ADD COLUMN login_user VARCHAR(104)",
        # SPIRE lab, the Kubernetes half. All NULL backfills to "this lab has no k3s
        # node", which is every lab built before the feature — `k8s_status` NULL is
        # what the panel reads as "never attempted". No BOOLEAN here, so the
        # DEFAULT-on-a-new-column trap described above does not apply.
        "ALTER TABLE spire_labs ADD COLUMN k8s_vm_resource_id VARCHAR(500)",
        "ALTER TABLE spire_labs ADD COLUMN k8s_vm_name VARCHAR(120)",
        "ALTER TABLE spire_labs ADD COLUMN k8s_private_ip VARCHAR(64)",
        "ALTER TABLE spire_labs ADD COLUMN k8s_public_ip VARCHAR(64)",
        "ALTER TABLE spire_labs ADD COLUMN k8s_status VARCHAR(32)",
        "ALTER TABLE spire_labs ADD COLUMN k8s_error_message TEXT",
        "ALTER TABLE spire_labs ADD COLUMN k8s_stages_done TEXT",
        "ALTER TABLE spire_labs ADD COLUMN k8s_stage_job_ids TEXT",
        "ALTER TABLE spire_labs ADD COLUMN k8s_audience VARCHAR(120)",
        "ALTER TABLE spire_labs ADD COLUMN k8s_workload_spiffe_id VARCHAR(255)",
        "ALTER TABLE spire_labs ADD COLUMN k8s_workload_uid INTEGER",
        "ALTER TABLE spire_labs ADD COLUMN k8s_workload_role VARCHAR(120)",
        "ALTER TABLE spire_labs ADD COLUMN k8s_issuer_url VARCHAR(255)",
        # SPIFFE federation with the dashboard's SPIRE server. NULL backfills to "never
        # federated", which every lab built before the button is.
        "ALTER TABLE spire_labs ADD COLUMN federation_status VARCHAR(32)",
        "ALTER TABLE spire_labs ADD COLUMN federation_error TEXT",
        "ALTER TABLE spire_labs ADD COLUMN federated_at TIMESTAMP",
        "ALTER TABLE spire_labs ADD COLUMN federation_proof TEXT",
        "ALTER TABLE spire_labs ADD COLUMN attr_probe TEXT",
        # The k3s node's OWN connection identity. The two VMs do not share an SSH
        # key. NULL backfills to "auto-derive from this host's deploy job", which is
        # both the pre-existing behaviour and the correct one — see the model. Bare
        # BOOLEAN with no DEFAULT, for the PostgreSQL reason described above.
        "ALTER TABLE spire_labs ADD COLUMN k8s_ansible_secret_ssh_key_source TEXT",
        "ALTER TABLE spire_labs ADD COLUMN k8s_ansible_managed_account TEXT",
        "ALTER TABLE spire_labs ADD COLUMN k8s_ansible_managed_become_self BOOLEAN",
        "ALTER TABLE spire_labs ADD COLUMN k8s_login_user VARCHAR(104)",
        # The Password Safe managed system's scrubbed Terraform state, so the SPIRE
        # lab's onboarding can be REMOVED as deterministically as it is created. NULL
        # backfills to "this lab was not onboarded by the dashboard", which is every
        # lab built before the button — including one an operator onboarded by hand,
        # whose `ps_system_id` is set and whose state correctly is not.
        "ALTER TABLE spire_labs ADD COLUMN ps_tf_state TEXT",
        # Per-POV narrowing for an ordinary user. TEXT and nullable, so the
        # DEFAULT-on-a-new-column trap described above does not apply. Every
        # pre-existing row backfills to NULL = "every POV", which is what keeps
        # _backfill_new_permission_scopes from changing anybody's access.
        "ALTER TABLE users ADD COLUMN pov_env_ids TEXT",
        # `cloud_cost_cache` needs no entry: create_all makes new tables. Nothing
        # backfills it either — an empty table is exactly "no cloud has reported a
        # cost yet", which is what the first warmer pass fixes.
        # `dashboard_stat_cache` needs no entry for the same two reasons: create_all
        # makes it, and an empty table means "nothing collected yet", which every tile
        # already renders as unavailable.
        # `pov_platform_cache` likewise. Empty means "this platform has not been
        # listed yet", which the POV page renders as an empty table with a note —
        # and the first reconcile pass after a deploy fills it.
        #
        # PRA Vendor Onboarding per POV. All four backfill to NULL, which reads as
        # "this POV uses the tenant's Asset Group and has no vendor group" — the right
        # answer for every environment wired before this feature, and what makes the
        # Access tab's blocker rather than a silent cross-POV grant the thing an
        # operator meets. `pov_vendor_users` needs no entry: create_all makes new
        # tables, and empty means nobody has been let in yet.
        "ALTER TABLE pov_environments ADD COLUMN pra_jump_group_name VARCHAR(255)",
        "ALTER TABLE pov_environments ADD COLUMN pra_jump_group_id VARCHAR(36)",
        "ALTER TABLE pov_environments ADD COLUMN pra_vendor_policy_id VARCHAR(36)",
        "ALTER TABLE pov_environments ADD COLUMN pra_vendor_group_id VARCHAR(36)",
        "ALTER TABLE pov_environments ADD COLUMN pra_vendor_expires_at TIMESTAMP",
        # The one-off demo warm-up. NULL = nothing booked, which every existing row is.
        "ALTER TABLE pov_environments ADD COLUMN wake_at TIMESTAMP",
        # When each accessor last signed in. NULL = never, which every existing row
        # reads as -- correctly, since nothing recorded it before.
        "ALTER TABLE pov_accessors ADD COLUMN last_login_at TIMESTAMP",
        # `workload_cloud_credentials` needs no entry either, for the same two reasons
        # create_all covers: new table, and empty means no workload has been given a
        # dynamic cloud credential yet. Explicitly NOT backfilled from
        # `workload_credential_lease`'s rows, which look like they belong here and do
        # not: those are the credential THIS APPLICATION uses for its own cloud calls,
        # one per (cloud, purpose), and copying them in would make the dashboard's own
        # lease appear as a governed workload identity.
        # Access roles. `access_roles` itself needs NO entry -- create_all makes new
        # tables -- and needs no backfill either: an empty table is "nobody has defined
        # a role yet", which `role_service.seed_builtins` fixes on the same boot.
        #
        # The two columns DO need entries, and three things about their shape are
        # deliberate. No DEFAULT and no BOOLEAN, so the PostgreSQL trap described above
        # cannot swallow them (`is_builtin` lives on the create_all table instead). And
        # no REFERENCES clause: an upgraded install therefore has NO foreign key here,
        # while a fresh one gets a real constraint from the model via create_all. That
        # asymmetry is why deleting a role is guarded in `role_service` rather than left
        # to `ondelete="SET NULL"` -- the constraint is absent on exactly the installs
        # that have data, SQLite enforces nothing in the test suite either way, and a
        # silent SET NULL would leave `role_permissions` populated behind a NULL
        # `role_id`, which is drift no reader would notice.
        #
        # Both backfill to NULL = "this principal holds no role", which changes nobody's
        # access and is what keeps `_backfill_new_permission_scopes` honest.
        "ALTER TABLE users ADD COLUMN role_id VARCHAR(36)",
        "CREATE INDEX ix_users_role_id ON users(role_id)",
        "ALTER TABLE users ADD COLUMN role_permissions TEXT",
        "ALTER TABLE oauth_group_mappings ADD COLUMN role_id VARCHAR(36)",
                    # `workload_k8s_tokens` needs no entry: create_all makes new tables, and empty
        # means no workload identity has been onboarded yet — which is the state every
        # install is in before an operator uses the tab. Worth naming here rather than
        # leaving silent, because the table looks like it should have a backfill: it
        # references `k8s_clusters.id`, and a reader could reasonably expect the
        # existing `ps-token` registrations to migrate into it. They must NOT. Those
        # are cluster-admin accounts serving PRA's brokered sessions, and the whole
        # point of this table is that nothing in it is cluster-admin.

        # Workgroup scoping for cloud databases and K8s clusters, which until now
        # were creator-scoped only. NULL on every existing row, and that is the
        # point: inventory_service.visible_to falls back to the creator when a row
        # carries no workgroup, so this migration grants and revokes nothing. A row
        # becomes team-visible only once an admin retags it.
        #
        # No DEFAULT clause, deliberately. A new column with a DEFAULT has been
        # rejected by PostgreSQL here before, and because each statement runs in its
        # own savepoint that rollback is silent -- the column simply never appears,
        # while SQLite (which tolerates it) keeps every test green.
        "ALTER TABLE cloud_databases ADD COLUMN workgroup VARCHAR(100)",
        "CREATE INDEX ix_cloud_databases_workgroup ON cloud_databases(workgroup)",
        "ALTER TABLE k8s_clusters ADD COLUMN workgroup VARCHAR(100)",
        "CREATE INDEX ix_k8s_clusters_workgroup ON k8s_clusters(workgroup)",

        # Change windows / the job scheduler. Every one of these backfills to NULL on
        # every existing row, and NULL means "behave exactly as before": run as soon
        # as the worker can claim it, with no window and no approval. That is what
        # makes switching the scheduler on a no-op for a live queue — the same
        # construction `expires_at` uses, rather than a guard somebody can forget.
        #
        # `approval_required` is a bare BOOLEAN with NO DEFAULT, deliberately, for the
        # PostgreSQL reason described above. NULL reads as False; the claim predicate
        # in job_service.claimable_now spells that as IS NOT TRUE.
        #
        # `change_windows` and `job_schedules` need no entry: create_all makes new
        # tables, and empty means "nobody has defined a window or a recurring schedule
        # yet", which is where every install starts.
        "ALTER TABLE jobs ADD COLUMN scheduled_for TIMESTAMP",
        "CREATE INDEX ix_jobs_scheduled_for ON jobs(scheduled_for)",
        "ALTER TABLE jobs ADD COLUMN window_ends_at TIMESTAMP",
        "ALTER TABLE jobs ADD COLUMN change_window_id VARCHAR(36)",
        "ALTER TABLE jobs ADD COLUMN job_schedule_id VARCHAR(36)",
        "CREATE INDEX ix_jobs_job_schedule_id ON jobs(job_schedule_id)",
        "ALTER TABLE jobs ADD COLUMN approval_required BOOLEAN",
        "ALTER TABLE jobs ADD COLUMN approved_at TIMESTAMP",
        "ALTER TABLE jobs ADD COLUMN approved_by VARCHAR(100)",
        "ALTER TABLE jobs ADD COLUMN missed_window_at TIMESTAMP",

        # A workgroup that may only be changed inside a named window. Both
        # backfill to NULL = "unconstrained", which is every existing
        # workgroup, so turning this on blocks nothing until an administrator
        # opts a workgroup in. `require_change_window` is a bare BOOLEAN with
        # no DEFAULT for the PostgreSQL reason described on users.is_admin.
        "ALTER TABLE workgroups ADD COLUMN change_window_id VARCHAR(36)",
        "ALTER TABLE workgroups ADD COLUMN require_change_window BOOLEAN",
        # A workstream's deploy defaults, a JSON object. NULL = none set, which is
        # every existing workstream, so nothing changes until someone sets one.
        "ALTER TABLE workgroups ADD COLUMN deploy_defaults TEXT",
        # Secret references an administrator binds to a workstream. NULL = none,
        # every existing workstream, so deploys are unchanged until one is bound.
        "ALTER TABLE workgroups ADD COLUMN secret_bindings TEXT",
        # Assets run on every VM deployed into a workstream. NULL = none, every
        # existing workstream, so no deploy changes until an administrator sets one.
        "ALTER TABLE workgroups ADD COLUMN post_deploy_runs TEXT",
        "ALTER TABLE workgroups ADD COLUMN network_defaults TEXT",
        # Password Safe attributes stamped on every system the workstream onboards.
        # NULL = none, every existing workstream, so no onboarding changes until an
        # administrator sets one.
        "ALTER TABLE workgroups ADD COLUMN ps_attributes TEXT",

        # Service accounts and their OAuth clients (services/service_accounts). A bare
        # BOOLEAN for the PostgreSQL reason on users.is_admin; NULL reads as "a person",
        # which is every existing row. `oauth_clients` itself is new, so create_all
        # builds it.
        "ALTER TABLE users ADD COLUMN is_service_account BOOLEAN",
        # When the account last signed in interactively. NULL = not since this shipped.
        "ALTER TABLE users ADD COLUMN last_login_at TIMESTAMP",
        "ALTER TABLE agent_cells ADD COLUMN oauth_client_id VARCHAR(36)",
        # OAuth clients that authenticate with a SPIFFE JWT-SVID instead of a secret
        # (services/spiffe_assertion). NULL auth_method reads as "secret", which is
        # every client that predates this. `spiffe_trust_domains` is new: create_all.
        "ALTER TABLE oauth_clients ADD COLUMN auth_method VARCHAR(20)",
        "ALTER TABLE oauth_clients ADD COLUMN spiffe_id VARCHAR(500)",
        # SPIRE lab deployment modes (vm | docker | k8s); NULL = vm, every older lab.
        "ALTER TABLE spire_labs ADD COLUMN deployment_mode VARCHAR(16)",
        "ALTER TABLE spire_labs ADD COLUMN spire_version VARCHAR(32)",
        # Verify a JWKS URL's TLS for this name (the lab's oidc.<td>) while connecting
        # to the URL's address -- the name only resolves on the lab itself.
        "ALTER TABLE spiffe_trust_domains ADD COLUMN tls_server_name VARCHAR(255)",
        "CREATE INDEX ix_oauth_clients_spiffe_id ON oauth_clients(spiffe_id)",
)
