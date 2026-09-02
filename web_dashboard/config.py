"""
Configuration management for Infrastructure Management Dashboard
"""
import json
import os
import re
import secrets
from typing import Any, List
from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables"""

    # Environment — set APP_ENV=production in prod, leave unset (defaults to "development") in dev
    app_env: str = "development"

    # Feature flags — gate optional integrations. Defaults are True so the
    # prod repo (which has the backing infra configured) works without
    # explicit opt-in. The community edition's .env.example ships all of
    # these set to false; users turn on what they have infra for.
    # Which kind of instance this is. The two are MUTUALLY EXCLUSIVE, and the reason is
    # tenancy, not taste: a demo instance's BeyondTrust tenant is the global singleton
    # (bt_api_host / pscli_api_url / entitle_api_key), while a POV instance holds a
    # registry of many named tenants and each POV references rows in it. An instance
    # claiming both would have two answers to "which tenant?" at every call site, and the
    # wrong answer is silent -- an OT cell onboarding into a customer's Password Safe, or a
    # POV onboarding into the demo tenant. See services/feature_flags.enabled().
    install_profile: str = "demo"       # "demo" | "pov" -- see docs/pov-instance.md
    pov_environments_enabled: bool = False  # POV router + /pov page (pov profile only)

    # ── Lab platforms — where a POV environment actually runs ────────────────
    # A POV is a template instantiated whole on a lab platform. Skytap is the first
    # adapter; see services/lab_platforms.py for the registry and the capability
    # table. These are POV-profile-only and inert on a demo instance.
    skytap_enabled: bool = False        # Skytap lab platform (pov profile only)
    # Skytap authenticates with HTTP Basic: username + an API SECURITY TOKEN from the
    # account page. NOT the account password — that is the single most common setup
    # mistake, so the Settings panel and the 401 message both say so.
    skytap_base_url: str = "https://cloud.skytap.com"
    skytap_username: str = ""
    skytap_api_token: str = ""          # secret; masked in Settings
    skytap_project_id: str = ""         # OPTIONAL — templates and environments are LISTED from this project and new ones CREATED in it; blank = everything the token can see

    # Whether this POV instance may run POVs on a public cloud at all, and which one.
    # Two keys rather than one, matching skytap_enabled beside skytap_*: the flag is what
    # `feature_flags._POV_ONLY` masks on a demo instance and what an operator switches off
    # without losing which cloud they had chosen.
    pov_cloud_enabled: bool = False     # POV cloud lab platform (pov profile only)

    # Which public cloud, if any, this POV instance may ALSO run POVs on. One of
    # "aws" | "azure" | "gcp" | "oci", or blank for none. Blank is the default and keeps
    # a POV instance exactly as it was.
    #
    # **One at a time, deliberately.** A POV instance is meant to be narrow: one cloud's
    # credentials, one cloud's quota to watch, one cloud's bill to explain. The limit is
    # enforced by lab_platforms.selectable_platforms(), which is what the create form and
    # the provision endpoint both read — not by hiding the others in the UI.
    #
    # Setting this does NOT re-open the demo cloud consoles. /aws /azure /gcp /oci and
    # their API routers stay 404 on a POV instance: their deploys resolve the GLOBAL
    # BeyondTrust tenant singletons, which is the one thing the profile split exists to
    # prevent. The POV's own read-only view of the selected cloud is /pov/cloud.
    pov_cloud_platform: str = ""

    vmware_enabled: bool = True         # VMs router + /vms page
    # The three BeyondTrust products the dashboard drives are gated independently —
    # customers routinely license one or two, not all three. They replaced a single
    # `beyondtrust_enabled` flag; feature_flag_migration.seed_beyondtrust_split copies
    # an existing install's stored value into all three on first boot after the upgrade,
    # which is why these default True (matching the old flag's default).
    password_safe_enabled: bool = True  # Secret + managed-account checkout, VM/DB/K8s onboarding, rotation
    pra_enabled: bool = True            # Shell Jump / tunnel / Web Jump provisioning + Gateway hosts
    epml_enabled: bool = True           # EPM for Linux package builds + activation tokens
    portainer_enabled: bool = True      # Containers router + /containers page + portainer warmer
    ansible_enabled: bool = True        # Config-mgmt router + /config-mgmt page
    entitle_enabled: bool = True        # Entitle integration: Settings panel + user-JIT nav link
    proxmox_enabled: bool = False       # Proxmox VE router + /proxmox page
    vsphere_enabled: bool = False       # vSphere/ESXi router + /vsphere page
    hyperv_enabled: bool = False        # Hyper-V router + /hyperv page (WinRM to Windows host)
    nutanix_enabled: bool = False       # Nutanix AHV router + /nutanix page (Prism Central REST API)
    xcpng_enabled: bool = False         # XCP-ng/XenServer router + /xcpng page (XAPI XML-RPC)
    vdesktops_enabled: bool = False     # Virtual desktops router + /desktops page (Azure pools + PRA brokering)
    cloud_database_enabled: bool = False  # /api/databases router — private managed DBs brokered via a PRA tunnel
    k8s_management_enabled: bool = False  # /api/k8s router — provision/register/manage Kubernetes clusters
    cloud_functions_enabled: bool = False  # /api/functions router — Lambda / Function App / Cloud Run function lifecycle
    # /api/agent router + /agents page — containerised agents inside private networks
    # that poll OUT to this dashboard for work. Off by default and deliberately so:
    # it is the only router that serves callers outside the dashboard's trust domain,
    # and enabling it means publishing an endpoint those agents can reach.
    remote_agents_enabled: bool = False
    cost_explorer_enabled: bool = False   # /api/costs router + dashboard spend tile (AWS Cost Explorer + Azure Cost Mgmt)
    cost_monthly_budget: float = 0.0      # overall monthly cloud-spend budget for alerts (account currency); 0 = disabled
    cost_budget_aws: float = 0.0          # optional per-cloud monthly budgets; 0 = disabled
    cost_budget_azure: float = 0.0
    cost_budget_gcp: float = 0.0
    cost_budget_oci: float = 0.0
    gcp_billing_export_table: str = ""    # BigQuery billing-export table for GCP cost (project.dataset.table); blank = GCP cost off
    # Durable cost cache (services/cost_cache.py). Env/config.py only — deliberately not
    # on the Setup panel: they are throttle-safety knobs, not features, and a panel field
    # that isn't bound both ways is discarded on save without an error.
    # 24 h, not 6 h, because Cost Explorer bills ~$0.01 per request and the warmer is
    # the account's single largest untaggable line item: at a 6 h TTL the warm loop alone
    # ran ~$7.90/mo against a ~$21/mo bill. CE figures only settle a few times a day, so
    # a shorter TTL buys freshness the upstream data does not actually have.
    cost_cache_ttl_seconds: int = 86400           # 24 h — how old a good figure may get
    cost_refresh_min_interval_seconds: int = 300  # floor between two forced requeries
    cost_query_lease_seconds: int = 120           # single-flight claim expiry
    cost_query_gap_seconds: int = 2               # min spacing between queries to ONE cloud
    cost_cold_wait_seconds: int = 5               # cold-miss wait for the claim winner
    # Durable dashboard tile snapshot (services/dashboard_stat_cache.py). Env/config.py
    # only, for the same reason as the cost knobs above: they pace a collector, they are
    # not features, and a Settings field that isn't bound both ways is silently discarded
    # on save.
    dashboard_stats_ttl_seconds: int = 120         # how old a tile may get before a refetch
    dashboard_stats_interval_seconds: int = 60     # how often the collector wakes
    dashboard_stats_lease_seconds: int = 120       # single-flight claim expiry
    dashboard_stats_gap_seconds: int = 2           # min spacing between calls to ONE provider
    # The app-side fallback collector only claims a tile older than this. With dash-worker
    # running, every pass claims nothing and costs one SELECT; with no worker at all, the
    # app takes over within one window. Must stay comfortably above the worker's interval.
    dashboard_stats_stale_after_seconds: int = 300
    dashboard_refresh_min_interval_seconds: int = 30  # floor between two forced refreshes
    # Action-level policy guardrails (pre-action admission control via OPA). Master
    # flag; when off, admission_service.enforce() is a no-op. Which actions are gated
    # is the list `admission_gated_actions` (default none). The caps below are injected
    # into policies as input.limits, settable from Settings without writing Rego. All
    # list values accept JSON (["a","b"]) or CSV (a,b).
    admission_control_enabled: bool = False
    admission_gated_actions: str = ""          # e.g. aws:ec2:deploy,clouddb:provision
    admission_allowed_regions: str = ""        # allow-list; empty = no region restriction
    admission_denied_instance_types: str = ""  # block-list of sizes/classes
    admission_prod_window: str = ""            # frozen weekdays, e.g. sat,sun
    # Secret hygiene: flag stored secrets not changed in more than this many days
    # (age from AppConfig.updated_at). 0 = disabled (no staleness flagging).
    secret_max_age_days: int = 0
    # Advisory scan of uploaded playbooks/scripts for hard-coded secrets. On by
    # default — it only warns (never blocks the upload). Set false to disable.
    secret_scan_enabled: bool = True
    # Config-drift tracking: record a per-target fingerprint on each successful
    # Ansible apply (passive). A target is "unverified" once its last apply is
    # this many days old.
    config_drift_tracking_enabled: bool = True
    config_drift_stale_days: int = 14
    # K8s Phase 3b broker (community = beyondtrust/sra Terraform path). The tunnel
    # uses bt_jump_group_name + bt_jumpoint_name (per-cluster overrides fall back
    # to these). Read live via config_service.
    k8s_rancher_entitle_bundle: str = ""    # Entitle bundle/role id for time-boxed Rancher RBAC (mgmt_kind=rancher)
    k8s_entitle_duration_minutes: int = 60  # default grant window for the Rancher JIT request
    # K8s management plane = Rancher (import model). The central Rancher server
    # runs as a single privileged container on a PUBLIC (source-restricted) GCE
    # COS VM (see gcp_rancher_* above); every k8s cluster is imported into it
    # (cattle-cluster-agent dials OUT to the server-url — fits private clusters
    # on any cloud / on-prem). The dashboard calls the Rancher v3 API directly
    # over HTTPS with the stored API token. Read live via config_service.
    rancher_node_cloud: str = "gcp"           # aws|azure|gcp — WHICH cloud hosts the single Rancher node. Picked per deploy (like the region) and rewritten to where it actually landed, so teardown + bare redeploys stay put. Default gcp because every node deployed before this key existed is a GCE VM; redeploying to another cloud RELOCATES the node (the old one is deleted first)
    rancher_server_url: str = ""              # Rancher server-url = https://<node public IP> (set by the deploy job)
    rancher_api_token: str = ""               # Rancher API bearer token minted at bootstrap; encrypted at rest
    rancher_bootstrap_password: str = ""      # first-run admin bootstrap password; encrypted at rest
    rancher_admin_password: str = ""          # admin UI password set during auto first-run. Rancher FORBIDS reusing the bootstrap password, so blank = auto-generate a distinct one (persisted here + surfaced in the Containers panel + job result). ≥12 chars. encrypted at rest
    rancher_admin_password_generated: bool = False  # marker: rancher_admin_password was auto-generated (→ echo it in the login hint; cleared on teardown)
    rancher_auto_first_run: bool = True        # on a FRESH deploy, auto-complete Rancher's first-run wizard (change admin password from bootstrap + accept EULA + telemetry-opt out) so the operator lands on a logged-in UI; off = leave the manual "Welcome" wizard
    rancher_verify_tls: bool = False          # verify the Rancher TLS cert on direct-HTTPS API calls; False = accept the node's self-signed cert
    rancher_allowed_source_cidrs: str = ""    # OPTIONAL/ADDITIVE CSV CIDRs for the node's PUBLIC-IP GCE firewall (source_ranges, tcp 80/443). Dashboard-provisioned clusters' egress IPs AND (when the Web Jump is enabled) the dashboard-managed Jumpoint's egress IP are auto-added; use this only for extra operator/human IPs + pre-existing operator Jumpoints. Fully empty (no manual + no auto) = firewall NOT opened (fail closed) unless gcp_rancher_allow_open.
    # Rancher UI PRA web-broker (OPT-IN): an sra_web_jump to the node's HTTPS so
    # an operator whose IP is NOT in rancher_allowed_source_cidrs can still reach
    # the UI via the PRA rep console (zero-trust / session recording). When
    # disabled, open_console returns the direct server_url deep-link. Blank jump
    # group/jumpoint fall back to the shared bt_* defaults.
    rancher_ui_web_jump_enabled: bool = False # gate the sra_web_jump broker; False = use the direct public URL
    rancher_ui_verify_certificate: bool = False  # sra_web_jump verify_certificate (False for the node's self-signed cert)
    rancher_ui_jump_group: str = ""           # "" = bt_jump_group_name
    rancher_ui_jumpoint_name: str = ""        # "" = bt_jumpoint_name
    rancher_ui_local_port: int = 443          # UNUSED — read by nothing. An sra_web_jump has no local listen port (that's a protocol-tunnel jump; see k8s_api_tunnel_local_port). Kept so an existing RANCHER_UI_LOCAL_PORT env doesn't break; deliberately NOT on the Settings panel
    rancher_ui_web_jump_id: str = ""          # PRA Web Jump id for the central Rancher UI (runtime-set)
    rancher_ui_web_jump_tfstate: str = ""     # terraform state for the Web Jump (for teardown)
    rancher_ui_vault_account_group_id: str = ""  # PRA Vault account group (numeric id) the admin credential is vaulted into for Web-Jump injection; chosen at deploy. "" = no vault (fall back to bt_vault_account_group_id, else surface the password)
    rancher_ui_vault_account_id: str = ""     # PRA Vault account id created for the Rancher admin credential (runtime-set; cleared on teardown)
    rancher_ui_jumpoint_cloud: str = "gcp"    # which dashboard-managed Jumpoint host brokers the Rancher UI (gcp|aws|azure); its egress IP is auto-whitelisted. gcp = same cloud as the node
    rancher_ui_jumpoint_egress_ip: str = ""   # dashboard-managed Web-Jump Jumpoint host egress IP (runtime-set; auto-added to the node firewall as a /32). all three managed Gateway hosts expose one: GCP + AWS via the host's public IP, Azure via a Standard, secure-by-default public IP on its NIC (Standard IPs block all inbound unless an NSG allows it, so it is egress-only)
    rancher_dashboard_egress_cidr: str = ""   # the DASHBOARD's own public egress IP/CIDR — the source the worker uses to bootstrap + poll the node over its PUBLIC IP, so it MUST be in the firewall or the deploy can't reach its own node. Auto-detected + persisted on deploy (best-effort IP-echo); a manually-set CIDR that CONTAINS the detected IP is kept (corp proxies egress from an IP pool — set the pool's CIDR, e.g. 104.28.182.0/24). Bare IP → /32.
    # Dashboard→Rancher API transport. Corp networks that TLS-inspect (e.g.
    # Cloudflare Gateway) reject the node's self-signed cert IN TRANSIT, killing
    # every direct HTTPS call (readiness, bootstrap, import API) — verify=False
    # can't bypass a proxy-side block. "runner" executes each call as curl in a
    # one-shot GCP Cloud Run job targeting the node's INTERNAL IP via the VPC
    # connector (reuses the k8s runner's gcp_region / gcp_ansible_vpc_connector).
    rancher_api_transport: str = "direct"     # direct | runner
    rancher_internal_url: str = ""            # https://<node internal IP> (runtime-set at deploy; what the runner dials)
    rancher_runner_source_cidr: str = ""      # the VPC connector's /28 — auto-added to the node firewall when transport=runner (GCE ingress rules apply to internal traffic too)
    # Entitle Rancher connector registration. The application slug is
    # tenant/connector-specific — confirm against the entitle_applications catalog
    # before use (default is best-effort). With the PUBLIC source-restricted node,
    # Entitle's cloud can reach it directly (private=False, no agent token); set
    # entitle_rancher_private for tenants who lock the node behind CIDRs Entitle
    # can't traverse.
    entitle_rancher_app_slug: str = "rancher"     # Entitle application catalog slug for the Rancher connector
    # Catalog slug for the REST integration (Entitle Remote Adapter) — how a Cloud
    # Functions adapter is registered. Lowercase; the entitleio provider rejects any
    # uppercase at plan time and 404s a wrong name at apply. Confirm against your
    # tenant's entitle_applications data source.
    entitle_rest_app_slug: str = "rest api"
    entitle_rancher_private: bool = False         # attach the shared Entitle agent token (node unreachable from Entitle's cloud)
    entitle_rancher_url_key: str = "url"          # (unused — _generate_rancher_hcl hardcodes connection_json keys) retained for compat
    entitle_rancher_token_key: str = "api_token"  # (unused — see above)
    entitle_rancher_integration_id: str = ""      # set by register_rancher_in_entitle
    entitle_rancher_tfstate: str = ""             # terraform state for the Rancher integration (for deregister)
    # K8s Phase 4 (Feature D) — in-cluster Password Safe secret delivery via the
    # External Secrets Operator. The BeyondTrust ClusterSecretStore authenticates
    # with the configured Password Safe OAuth client (pscli_api_url / pscli_client_id
    # / pscli_client_secret). Read live via config_service.
    eso_namespace: str = "external-secrets"             # namespace ESO + the credentials Secret land in
    eso_helm_version: str = ""                          # pin the external-secrets chart version ("" = latest)
    eso_bt_credentials_secret: str = "beyondtrust-credentials"  # K8s Secret holding the BT OAuth client id/secret
    eso_bt_clustersecretstore: str = "beyondtrust-store"        # ClusterSecretStore name
    eso_bt_api_url: str = ""                            # BeyondTrust public API URL ("" = derive from pscli_api_url)
    eso_bt_retrieval_type: str = "SECRET"              # SECRET | MANAGED_ACCOUNT
    eso_bt_api_version: str = "3.1"                     # BeyondTrust API version ("3.0" | "3.1")

    # Proxmox VE connection
    proxmox_host: str = ""              # hostname or IP of the Proxmox node/cluster
    proxmox_port: int = 8006
    proxmox_user: str = "root@pam"
    proxmox_token_id: str = ""          # API token name (preferred auth)
    proxmox_token_secret: str = ""      # API token value
    proxmox_password: str = ""          # password auth (fallback if no token)
    proxmox_verify_ssl: bool = False    # set True when using a valid TLS cert

    # vSphere / ESXi connection (pyVmomi — works with vCenter and standalone ESXi)
    vsphere_host: str = ""              # hostname or IP of vCenter / ESXi host
    vsphere_port: int = 443
    vsphere_user: str = "administrator@vsphere.local"
    vsphere_password: str = ""          # encrypted at rest
    vsphere_verify_ssl: bool = False    # set True for a valid TLS cert
    vsphere_datacenter: str = ""        # optional default datacenter filter

    # Nutanix AHV connection (Prism Central REST API v3)
    nutanix_host: str = ""              # Prism Central hostname or IP
    nutanix_port: int = 9440
    nutanix_username: str = "admin"
    nutanix_password: str = ""          # encrypted at rest
    nutanix_verify_ssl: bool = False    # set True for a valid TLS cert

    # XCP-ng / XenServer connection (XAPI XML-RPC)
    xcpng_host: str = ""               # XCP-ng host or pool master hostname/IP
    xcpng_username: str = "root"
    xcpng_password: str = ""            # encrypted at rest
    xcpng_verify_ssl: bool = False      # set True for a valid TLS cert

    # Agent-brokered hypervisor inventory sync. The connections themselves live in the
    # `hypervisor_connections` table, not here — these two only set the cadence.
    hypervisor_sync_interval_minutes: int = 30   # per-connection override: options.sync_interval_minutes
    hypervisor_sync_poll_seconds: int = 300      # how often the loop checks for due syncs

    # Hyper-V connection (WinRM to Windows host running Hyper-V)
    hyperv_host: str = ""               # hostname or IP of the Hyper-V host
    hyperv_port: int = 5985             # 5985 = HTTP (default), 5986 = HTTPS
    hyperv_username: str = ""           # Windows username (DOMAIN\user or user@domain)
    hyperv_password: str = ""           # encrypted at rest
    hyperv_use_ssl: bool = False        # use HTTPS (WinRM port 5986)
    hyperv_verify_ssl: bool = False     # verify TLS cert (disable for self-signed)
    hyperv_transport: str = "ntlm"     # ntlm (default), basic, kerberos

    # Database
    database_url: str = "sqlite:///./vm_cli.db"

    # Security
    # jwt_secret_key is loaded from jwt_secret_key_file (Docker secret mount) when set,
    # or from /run/secrets/jwt_key if that path exists, then falls back to the env var.
    jwt_secret_key_file: str = ""  # path written by the onboard script; set by Compose secrets
    jwt_secret_key: str = secrets.token_hex(32)
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 480  # 8 hours

    @model_validator(mode="after")
    def _load_jwt_key_from_file(self) -> "Settings":
        path = self.jwt_secret_key_file or ""
        if not path and os.path.exists("/run/secrets/jwt_key"):
            path = "/run/secrets/jwt_key"
        if path:
            try:
                key = open(path).read().strip()  # noqa: WPS515
                if key:
                    object.__setattr__(self, "jwt_secret_key", key)
            except OSError as exc:
                raise ValueError(f"Cannot read JWT key from '{path}': {exc}") from exc
        return self

    # First-run admin bootstrap. If no users exist at startup AND
    # first_run_admin_password is set, an admin account is created with these
    # credentials. Leaving first_run_admin_password blank disables bootstrap
    # (prod clusters with pre-existing users are unaffected either way).
    first_run_admin_username: str = "admin"
    first_run_admin_password: str = ""

    # API Configuration
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    api_title: str = "Infrastructure Management API"
    api_version: str = "0.2.2"

    # CORS
    cors_origins: List[str] = ["http://localhost:8001", "http://localhost:3000"]

    # The absolute origin this dashboard is reached at, e.g. https://dash.example.com.
    # Blank = derive it from each request, which is right for a laptop install reached
    # over localhost, an IP and a hostname on different days.
    #
    # Set it whenever a reverse proxy is involved. It is what decouples the OAuth
    # callback URIs from proxy-header trust: derived URIs are only https because
    # ProxyHeadersMiddleware rewrote the scheme from X-Forwarded-Proto, so a proxy
    # that isn't in trusted_proxy_hosts below would silently produce http:// callbacks
    # that the identity provider rejects. See services/public_url.py.
    public_base_url: str = ""

    # Which peers may set X-Forwarded-For / X-Forwarded-Proto.
    #
    # Defaults to loopback (uvicorn's own default), NOT "*". A wildcard means any
    # client that can reach the socket may declare its own source address, and
    # get_remote_address — which the login throttle's per-address cap and any future
    # rate limiting key off — believes it. Rotating one header per request then walks
    # straight past the cap.
    #
    # Behind a proxy, set this to the proxy's literal IP (comma-separated for several).
    # It must be a literal: uvicorn 0.27's ProxyHeadersMiddleware does plain string
    # comparison against the peer address and understands neither hostnames nor CIDR
    # (CIDR arrived in uvicorn 0.31). Getting it wrong is not silent — the app logs a
    # warning naming the peer that sent the untrusted header. See main.py.
    trusted_proxy_hosts: str = "127.0.0.1"

    # Logging
    log_dir: str = r"C:\Scripts\Logs\VM-Dashboard"
    log_level: str = "INFO"

    # Rate Limiting
    rate_limit_per_minute: int = 60

    # Workgroups — community edition seeds a single `default` workgroup at first
    # boot. Admins create additional workgroups via the /workgroups UI; each can
    # have an optional local_vm_path for VMware local-VM scanning. The runtime
    # reads from the `workgroups` DB table; this dict is only the bootstrap
    # seed source and stays empty in the community edition.
    workgroups: dict = {}

    # AWS / Terraform
    aws_region: str = "us-east-2"
    terraform_executable: str = "terraform"  # assumes terraform is in PATH
    # Existing RDS parameter group attached to dashboard-provisioned databases.
    # The sandbox creates one with rds.force_ssl=0 (the PRA protocol tunnel needs
    # a cleartext backend) and writes its name here; empty = RDS default group.
    aws_db_parameter_group_name: str = ""
    # Managed-Kubernetes (EKS) provisioning (§1.1a). The EKS module now builds its
    # OWN VPC / subnets / NAT-instance egress (self-contained, like AKS/GKE) and
    # peers it back to the sandbox VPC for direct management-plane access. The
    # sandbox emits its VPC id / CIDR / private route-table id; the DB + default
    # (VM) SGs drive the cross-VPC ingress rules. Empty version / node type / VPC
    # CIDR → the terraform/k8s_cluster/aws_eks module defaults.
    aws_vpc_id: str = ""                     # sandbox VPC to peer the EKS VPC with
    aws_vpc_cidr: str = "10.99.0.0/16"       # sandbox VPC CIDR (peering route target)
    aws_private_route_table_id: str = ""     # sandbox private RT — gets the peering return route
    aws_eks_vpc_cidr: str = "10.97.0.0/16"   # the EKS cluster's own VPC CIDR (must not overlap the sandbox)
    aws_k8s_subnet_a_id: str = ""            # legacy (pre-self-contained EKS); no longer consumed
    aws_k8s_subnet_b_id: str = ""            # legacy; no longer consumed
    aws_eks_k8s_version: str = ""
    aws_eks_node_instance_type: str = ""

    # Managed-Kubernetes (AKS / GKE) provisioning. These modules create their own
    # network + egress (no sandbox subnets needed). Empty version / node size →
    # the module defaults. *_authorized_cidrs (comma-separated) restrict the public
    # API endpoint; empty = open to all (matches EKS's 0.0.0.0/0 default).
    azure_aks_k8s_version: str = ""
    azure_aks_node_vm_size: str = ""
    azure_aks_authorized_cidrs: str = ""
    gcp_gke_k8s_version: str = ""
    gcp_gke_machine_type: str = ""
    gcp_gke_authorized_cidrs: str = ""
    # Pool the per-cluster GKE private control-plane /28 is carved from. GCP treats
    # that range as a subnetwork of the cluster's VPC and rejects any overlap
    # VPC-wide (other regions included), so every cluster needs its own slot —
    # k8s_service._gke_master_cidr picks the lowest free one. Must not overlap the
    # sandbox subnets (10.x) or any peered network; a /16 gives 4096 clusters.
    gcp_gke_master_cidr_base: str = "172.16.0.0/16"
    # Managed-Kubernetes (OKE) provisioning. Self-contained VCN (own CIDR, distinct
    # from the sandbox 10.98/16); BASIC cluster = free control plane; the node pool
    # defaults to a single Always-Free A1.Flex node (2 OCPU / 12 GB). Empty version /
    # shape → the terraform/k8s_cluster/oci_oke module defaults.
    oci_oke_k8s_version: str = ""
    oci_oke_node_shape: str = ""       # e.g. VM.Standard.A1.Flex
    oci_oke_vcn_cidr: str = "10.96.0.0/16"

    # BeyondTrust integration. Two distinct API surfaces:
    #   • PRA (Privileged Remote Access)  — Shell Jump provisioning via the
    #     Terraform sra provider in services/terraform_pra_service.py. Uses
    #     bt_api_host / bt_client_id / bt_client_secret (OAuth2).
    #   • Password Safe / Secrets Safe    — secret + managed-account
    #     retrieval via the ps-cli binary in services/btapi_service.py. Uses
    #     pscli_api_url / pscli_client_id / pscli_client_secret.
    pscli_executable: str = "ps-cli"  # installed via beyondtrust-bips-cli; override in .env if needed
    pscli_api_url: str = ""      # e.g. "https://ps.company.com"
    pscli_client_id: str = ""
    pscli_client_secret: str = ""
    pscli_api_account_name: str = ""  # Password Safe run-as user — REQUIRED by the passwordsafe TF provider block

    # Optional Password Safe VM resource registration (per-deploy opt-in, mirrors
    # entitle_registration_*). Onboards a built VM as a managed system + the baked-in
    # adminuser account. Per-cloud onboarding methods:
    #   • AWS (passwordsafe_aws_registration_method, default "ssm") — cloud-native "AWS
    #     Systems Manager" custom plugin. Manages Linux EC2 over AWS SSM SendCommand (no
    #     per-VPC Resource Broker / SSH reachability). Managed system DNS = {instance-id}:{region}.
    #   • Azure (passwordsafe_azure_registration_method, default "azurevm") — cloud-native
    #     "Azure VM SSH Rotation" custom plugin. Writes the key onto the VM over Azure VM
    #     Run Command (no Resource Broker / SSH reachability). Managed system address =
    #     tenantId/subscriptionId/resourceGroup/vmName; the first key is minted on onboard
    #     (passwordsafe_azure_change_password_on_register) since adminuser has none baked in.
    #   • GCP (passwordsafe_gcp_registration_method, default "gcpvm") — cloud-native
    #     "GCP VM SSH Rotation" custom plugin. Writes the public key into the GCE instance's
    #     ssh-keys metadata (no Resource Broker / SSH reachability; requires OS Login
    #     disabled on the instance). Managed system address = projectId/zone/instanceName;
    #     the first key is minted on onboard (passwordsafe_gcp_change_password_on_register)
    #     since adminuser has none baked in.
    #   • "ssh" (every other cloud, and AWS/Azure/GCP when overridden) — traditional managed
    #     system keyed by hostname/IP on an SSH platform; the VM's own private key is pushed
    #     and management needs SSH line-of-sight (broker).
    # The functional account is operator-configured per cloud; its platform decides the
    # management method (agent-plugin / custom-plugin / Resource-Broker).
    passwordsafe_registration_enabled: bool = False     # global capability flag (also per-build opt-in)
    passwordsafe_api_version: str = "3.1"               # passwordsafe provider api_version
    passwordsafe_workgroup: str = ""                    # workgroup name or id the managed system lands in
    passwordsafe_vm_functional_account: str = ""        # generic fallback functional account (name or id)
    passwordsafe_vm_functional_account_aws: str = ""    # per-cloud functional account override
    passwordsafe_vm_functional_account_azure: str = ""
    passwordsafe_vm_functional_account_gcp: str = ""
    passwordsafe_vm_functional_account_oci: str = ""    # OCI VMs use the traditional "ssh" method (key pushed)
    passwordsafe_managed_account_name: str = "adminuser"  # the bt-ready account onboarded as managed
    passwordsafe_entity_type_id: int = 1                # BeyondInsight entity type (1 per provider example)
    passwordsafe_ssh_key_enforcement_mode: int = 2      # 0=none, 1=auto, 2=strict (confirm vs tenant) — SSH method only
    passwordsafe_application_host_id: int = 0           # >0 routes management via a broker/application host — SSH method only
    # AWS Systems Manager (cloud-native) onboarding — see comment block above.
    passwordsafe_aws_registration_method: str = "ssm"   # "ssm" (AWS Systems Manager plugin, default) | "ssh"
    passwordsafe_ssm_account_suffix: str = "local"      # managed-account name suffix; AssumeRole ARN for EC2 cross-account mode
    passwordsafe_ssm_change_password_on_register: bool = False  # best-effort initial key mint via PS Change Password (off; endpoint verified live)
    # Azure VM SSH Rotation (cloud-native) onboarding — Azure counterpart of the SSM plugin.
    passwordsafe_azure_registration_method: str = "azurevm"  # "azurevm" (Azure VM SSH Rotation plugin, default) | "ssh"
    passwordsafe_azure_change_password_on_register: bool = True  # mint first key via Run Command on onboard (adminuser has none baked in)
    # GCP VM SSH Rotation (cloud-native) onboarding — GCP counterpart (writes the key into GCE ssh-keys metadata).
    passwordsafe_gcp_registration_method: str = "gcpvm"  # "gcpvm" (GCP VM SSH Rotation plugin, default) | "ssh"
    passwordsafe_gcp_change_password_on_register: bool = True  # mint first key via GCE metadata on onboard (adminuser has none baked in)
    # OT demo cell → PRA checkout. When the cell's adminuser is onboarded into Password
    # Safe, the wiring also creates a PRA Vault username/password account (associated to
    # the cell's Jump Group) plus a managed-account mirror on the "PRA Vault Username
    # Password" plugin, and links the pair with SyncedAccounts — so the credential can be
    # checked out / injected in PRA and every Password Safe rotation propagates into it.
    ot_ps_pra_checkout_enabled: bool = True
    # Push one Change Password through the SyncedAccounts link right after it is made.
    # The link is born after the deploy-time initial mint, so without this PRA holds the
    # throwaway placeholder until some later rotation. NOT the cloud's
    # change-on-register flag: that one governs onboarding, this one convergence.
    ot_ps_checkout_converge: bool = True
    ot_ps_pravault_platform: str = ""            # mirror platform name; falls back to clouddb_ps_pravault_platform
    ot_ps_pravault_functional_account: str = ""  # FA on that platform; falls back to clouddb_ps_pravault_functional_account
    bt_api_host: str = ""        # PRA host, used by terraform_pra_service
    bt_client_id: str = ""
    bt_client_secret: str = ""
    bt_jump_group_name: str = ""  # set via setup wizard / settings panel
    bt_jumpoint_name: str = ""    # name of the pre-existing Jumpoint in PRA (required for Terraform path)
    bt_ps_deploy_key_title: str = "Docker Deploy Key"  # Password Safe secret title

    # ── Optional cloud-DATABASE Password Safe onboarding (AWS) ────────────────
    # When enabled, provisioning an AWS DB additionally: creates a dedicated managed DB
    # user (via the DB client run on the shared Jumpoint host over SSM), onboards the DB
    # as a Password Safe managed system + managed account on the "{engine} SSM Custom
    # Plugin" platform, and onboards the PRA Vault account as a managed account on the
    # "PRA Vault Username Password" plugin so Password Safe propagates each rotation into
    # the PRA vaulted credential. No privileged DB "functional login" is created — the
    # IAM user (below) is Password Safe's functional account (SSM transport) and the
    # managed account self-rotates. The two custom plugins + jump-host RSA prep are
    # one-time MANUAL setup (see docs); the platform names below are how the dashboard
    # finds them.
    clouddb_ps_onboarding_enabled: bool = False
    clouddb_ps_platform_postgres: str = "psql SSM Custom Plugin"
    clouddb_ps_platform_mysql: str = "mysql SSM Custom Plugin"
    clouddb_ps_platform_sqlserver: str = "mssql SSM Custom Plugin"
    clouddb_ps_pravault_platform: str = "PRA Vault Username Password"
    clouddb_ps_workgroup: str = ""                 # blank → falls back to passwordsafe_workgroup
    # Where the DB plugin's functional account comes from. Two modes, and they are
    # opposites — the mode is an explicit choice, never inferred from a blank field,
    # because this feature already has enough silent fallbacks:
    #   "create"    (default, legacy) — the dashboard mints ONE functional account PER
    #               DATABASE at provision time, packing the credential material below
    #               (AWS: the IAM user + key; Azure: the SP + the minted DB admin) into
    #               it, and DELETES it on decommission.
    #   "reference" — the operator creates the functional accounts in BeyondInsight and
    #               names them here. The dashboard resolves each one over the API, the
    #               managed system inherits ITS platform, and the account is never
    #               created and never deleted. This is how the VM
    #               (passwordsafe_vm_functional_account_*) and k8s
    #               (k8s_ps_functional_account_*) paths have always worked, and it keeps
    #               the static cloud credential out of the dashboard's config store
    #               entirely. A blank or unresolvable name is an ERROR in this mode, not
    #               a fall-through to "create".
    # One account per ENGINE per cloud: a functional account belongs to a platform, and
    # each engine is its own plugin platform — so there is no generic fallback key.
    clouddb_ps_functional_account_mode: str = "create"   # create | reference
    # Per-engine overrides of the mode above; blank falls back to it. Needed because the
    # mode is not a preference but a consequence of whether the functional account holds
    # a PER-DATABASE secret. GCP SQL Server has no IAM database authentication, so it
    # runs on cloud-run with a real per-database login and must be "create", while
    # PostgreSQL/MySQL on data-api are "-:-:-" and want "reference". One global switch
    # cannot express that, and either setting breaks the other engines.
    clouddb_ps_functional_account_mode_postgres: str = ""    # blank = use the global
    clouddb_ps_functional_account_mode_mysql: str = ""       # blank = use the global
    clouddb_ps_functional_account_mode_sqlserver: str = ""   # blank = use the global
    # Per-CLOUD overrides, resolved BETWEEN the per-engine keys above and the global
    # below. The mode follows the plugin family, and the family is chosen by cloud: the
    # Azure Run Command plugins pack THIS database's admin password into the functional
    # account, so one shared account cannot serve N databases there — "reference" on
    # Azure obliges someone to create the account's DB login on every server by hand,
    # with a password Password Safe will never hand back. The AWS SSM plugins have the
    # same shape. GCP data-api is the only family where "reference" is free.
    #
    # BELOW the per-engine keys, not above, because engine is the axis GCP genuinely
    # needs: SQL Server on Cloud SQL must be "create" while PostgreSQL/MySQL on the same
    # cloud want "reference", so a cloud-wide key must not be able to overrule the
    # engine key that expresses that. There is deliberately no cloud+engine rung — it
    # would be nine keys, one of which would silently outrank the two an operator
    # actually set.
    clouddb_ps_functional_account_mode_aws: str = ""     # blank = per-engine, then global
    clouddb_ps_functional_account_mode_azure: str = ""   # blank = per-engine, then global
    clouddb_ps_functional_account_mode_gcp: str = ""     # blank = per-engine, then global
    clouddb_ps_functional_account_postgres: str = ""     # on "psql SSM Custom Plugin"
    clouddb_ps_functional_account_mysql: str = ""        # on "mysql SSM Custom Plugin"
    clouddb_ps_functional_account_sqlserver: str = ""    # on "mssql SSM Custom Plugin"
    clouddb_ps_pravault_functional_account: str = ""     # on "PRA Vault Username Password"
    # "Change Password Using Own Credentials" on the DB managed account. The DB custom
    # plugins ship two change actions and Password Safe picks between them from this flag:
    #   on  — the managed account rotates ITSELF (no privilege needed on the target), which
    #         is what an operator-created, unprivileged functional account requires;
    #   off — Password Safe calls the via-functional-account action, which needs a
    #         privileged DB login (CREATEROLE / CREATE USER / ALTER ANY LOGIN). That is
    #         only true when the functional account IS the DB admin, i.e. "create" mode.
    # Off by default so existing installs are unchanged. Self-rotation also requires
    # Password Safe to hold the account's correct current password — it does, because the
    # dashboard seeds it at onboarding. All three Azure plugins resolve the Azure
    # control-plane credential in three tiers: (1) the functional account's own service
    # principal, used whenever the FA name carries the SP: prefix; (2) a BROKER-level SP
    # under AppSettings:Azure{Postgres,MySql,Mssql}:ControlPlane, used for MSI: mode;
    # (3) DefaultAzureCredential (an ambient managed identity). So an off-Azure broker
    # needs nothing extra as long as the FAs are SP: — tiers 2/3 are broker-side config
    # the dashboard cannot set. Note the functional account is REQUIRED for every action,
    # self-rotation included, and all three password segments are validated before the
    # credential is built: its DB password segment must be non-empty and valid even when
    # a self-rotate change never uses it.
    clouddb_ps_self_rotation: bool = False
    # Import from Password Safe (/databases → "Import from Password Safe"). Reads only —
    # nothing in Password Safe is created or changed. Password Safe already runs a
    # discovery scanner with managed credentials, so it knows a database's platform,
    # port and requestable accounts authoritatively; these four tune what the import
    # list shows. All optional: the feature is already gated on cloud_database_enabled
    # + password_safe_enabled, so a third on/off switch would only be a third thing to
    # forget. See docs/databases.md "Importing from Password Safe".
    clouddb_ps_import_workgroup: str = ""          # blank → everything the API identity can see
    clouddb_ps_import_default_cloud: str = "local"  # Location preselected in the modal
    clouddb_ps_import_max_systems: int = 500       # cap on candidates returned
    clouddb_ps_import_platform_map: str = ""       # JSON {"Percona Server": "mysql"} overrides
    # DB-client container images run on the jump host (override for a mirrored registry).
    clouddb_db_client_image_postgres: str = "postgres:16"
    clouddb_db_client_image_mysql: str = "mysql:8.4"
    clouddb_db_client_image_sqlserver: str = "mcr.microsoft.com/mssql-tools18"
    # AWS credentials packed into the Password Safe functional account for SSM
    # SendCommand. The plugin parses username "<EC2|IAM>:<dbAdminUser>" and password
    # "<AKID>:<secret>:<dbAdminPassword>" (always three parts). The mode is selected by
    # the key pair's PRESENCE: both set → IAM mode; either blank → EC2 mode (the PS
    # node/broker's own instance role authorizes SSM, parts 1–2 become "x" placeholders).
    # The IAM username itself never reaches the plugin — the field is informational.
    clouddb_ps_ssm_iam_username: str = ""
    clouddb_ps_ssm_access_key_id: str = ""
    clouddb_ps_ssm_secret_access_key: str = ""     # encrypted at rest
    # The address's assumeRole segment: "NoAssumeRole" or a cross-account AssumeRole
    # ARN. MUST be ≥ 12 characters — the plugin Substring(0,12)'s it, so the old
    # "local" default crashed every action; a persisted short value is coerced on read.
    clouddb_ps_ssm_account_suffix: str = "NoAssumeRole"
    # The address's certPath segment (field 4 for mssql, 5 for psql/mysql): RSA public
    # certificate path on the PS node/broker. Required — an empty segment fails inside
    # the plugin at the first rotation.
    clouddb_ps_ssm_public_key_path: str = ""
    # The mysql address's trailing segment: sslTRUE | sslFALSE (mysql is the only
    # engine with an ssl field; anything but the literal sslTRUE disables TLS).
    clouddb_ps_ssm_ssl: bool = True
    # Plugin RSA key material the dashboard drops onto the shared SSM jump host, the
    # AWS counterpart of clouddb_ps_azure_plugin_private_key. Use a SEPARATE key pair
    # from Azure's: the two private keys land on different hosts (this one on the ECS
    # gateway host, Azure's on clouddb-jumpoint), so one shared pair would mean a
    # compromise of either host also decrypts the other cloud's payloads.
    clouddb_ps_ssm_plugin_private_key: str = ""     # PEM, encrypted at rest
    clouddb_ps_ssm_plugin_passphrase: str = ""      # encrypted at rest
    # Directory on the jump host the SSM plugin reads private.pem/passphrase.txt from.
    # Configurable because it is the plugin's choice, not ours — Azure's counterpart is
    # /root/psplugin. Blank disables the drop entirely (leave the staging manual).
    clouddb_ps_ssm_key_directory: str = "/home/ssm-user"
    # PRA Configuration-API OAuth account for the PRA Vault plugin (blank → reuse bt_client_*).
    pra_config_api_client_id: str = ""
    pra_config_api_client_secret: str = ""          # encrypted at rest

    # ── Cloud Functions (PREVIEW) ─────────────────────────────────────────────
    # Lambda / Function App / Cloud Run function lifecycle. The deployable handler
    # source lives in web_dashboard/functions/; the dashboard builds a deterministic
    # zip, uploads it to an object store IN THE SAME CLOUD as the function, and
    # terraform references it by bucket + key + content hash. GCP forces that shape
    # (cloudfunctions2 accepts only storage_source), so AWS/Azure match it to keep
    # one transport. The on/off is the `cloud_functions_enabled` PREVIEW flag; these
    # are the connection knobs its config-only panel writes.
    function_package_s3_bucket: str = ""    # AWS: S3 bucket for lambda zips (required for cloud=aws)
    function_package_gcs_bucket: str = ""   # GCP: GCS bucket for function sources (required for cloud=gcp)
    # Azure reuses the dashboard's storage account (storage_azure_account) with a
    # dedicated container; run-from-package needs a blob + SAS, not a separate store.
    function_package_azure_container: str = "function-packages"
    # Azure App Service plan SKU for the Function App. B1 (Basic) is the default
    # because it supports BOTH regional VNet integration and WEBSITE_RUN_FROM_PACKAGE.
    # Y1 (Linux Consumption) is cheaper but CANNOT do VNet integration — the module
    # fails at plan time if Y1 is paired with a subnet.
    azure_functions_plan_sku: str = "B1"
    # VPC/VNet attachment for functions that must reach private resources. Blank →
    # the function deploys public-only. Per-region overrides resolve through
    # region_config; these are the flat fallbacks.
    aws_functions_subnet_ids: str = ""              # CSV; blank → falls back to the region default subnet
    aws_functions_security_group_ids: str = ""      # CSV; blank → falls back to aws_db_security_group_id
    azure_functions_subnet_id: str = ""             # MUST be delegated to Microsoft.Web/serverFarms
    # Runtime service account the GCP function runs as. Blank falls back to the broad
    # default compute SA — and skips the module's per-secret accessor binding, so the
    # function then cannot read its own bearer secret.
    gcp_functions_service_account: str = ""
    # GCP uses Direct VPC egress: no connector, nothing billed while idle, and the
    # attachment is created/destroyed with the function. REGION-LOCKED, so give the
    # subnet as a BARE NAME and let it resolve in whatever region the function lands.
    gcp_functions_network: str = ""                  # VPC network name for Direct VPC egress
    gcp_functions_subnetwork: str = ""               # bare subnet name, must exist in the function's region
    # Legacy escape hatch — the one thing direct egress cannot do is reach another
    # region from a region-pinned function. Costs ~$26/mo whether invoked or not.
    gcp_functions_vpc_connector: str = ""           # existing Serverless VPC Access connector name/self_link
    # ── Azure cloud-DATABASE Password Safe onboarding (Run Command plugins) ───
    # The Azure counterpart of the SSM path above: when enabled, provisioning an
    # AZURE DB onboards it onto the "{engine} Azure Run Command Plugin", whose
    # custom plugin reaches the private DB by running the DB client on the shared
    # clouddb-jumpoint VM via Azure VM Run Command. Unlike SSM, the functional
    # account is a PRIVILEGED DB login (the minted admin) bundled with an Azure
    # service principal — "SP:<admin>" / "clientId:clientSecret:adminPassword" —
    # which rotates a dedicated managed DB user. The three plugins + the RSA
    # keypair are one-time MANUAL setup (see docs); the platform names below are
    # how the dashboard finds them. Default method when enabled; "off" disables it.
    passwordsafe_azure_db_registration_method: str = "runcommand"  # runcommand | off
    clouddb_ps_platform_azure_postgres: str = "PostgreSQL Azure Run Command Plugin"
    clouddb_ps_platform_azure_mysql: str = "MySQL Azure Run Command Plugin"
    clouddb_ps_platform_azure_sqlserver: str = "MSSQL Azure Run Command Plugin"
    # Azure counterparts of clouddb_ps_functional_account_* — read only when
    # clouddb_ps_functional_account_mode is "reference" (see that key for both modes).
    clouddb_ps_functional_account_azure_postgres: str = ""   # on "PostgreSQL Azure Run Command Plugin"
    clouddb_ps_functional_account_azure_mysql: str = ""      # on "MySQL Azure Run Command Plugin"
    clouddb_ps_functional_account_azure_sqlserver: str = ""  # on "MSSQL Azure Run Command Plugin"
    # Only read in "create" mode — in "reference" mode the operator puts whatever the
    # plugin needs into the functional account itself, so these stay blank.
    clouddb_ps_azure_auth_mode: str = "SP"          # SP (service principal) | MSI (managed identity)
    clouddb_ps_azure_cert_path: str = r"C:\BeyondTrust\certs\public_cert.cer"  # address field 7: public cert path on the Resource Broker
    clouddb_ps_azure_ssl: bool = True               # address field 8: sslTRUE (Azure flex servers require TLS) | sslFALSE
    # Azure SP for the DB functional account (blank → reuse azure_client_id/secret).
    clouddb_ps_azure_sp_client_id: str = ""
    clouddb_ps_azure_sp_client_secret: str = ""     # encrypted at rest
    # Plugin RSA key material the dashboard drops onto clouddb-jumpoint (/root/psplugin)
    # so the plugin can decrypt the RSA-wrapped login password; the matching public
    # cert lives on the Resource Broker at clouddb_ps_azure_cert_path.
    clouddb_ps_azure_plugin_private_key: str = ""   # PEM, encrypted at rest
    clouddb_ps_azure_plugin_passphrase: str = ""    # encrypted at rest

    # ── GCP cloud-DATABASE Password Safe onboarding (Cloud SQL plugins) ──────
    # The GCP counterpart of the two paths above, and the one that needs no jump
    # host at all: the "GCP Cloud SQL {engine}" plugins reach a PRIVATE-IP instance
    # through Google's control plane (the Cloud SQL Data API, instances.executeSql)
    # rather than by running a DB client somewhere with line of sight. So there is
    # no cert path, no RSA key pair and no client image here — and under IAM
    # database authentication no functional-account DB password either: the
    # credential is a short-lived OAuth token minted per connection, and the
    # composite's third segment is "-".
    #
    # TWO CHANNELS, chosen per engine. postgres/mysql ride "data-api" (no
    # infrastructure at all). SQL Server rides "cloud-run", because Cloud SQL for SQL
    # Server has no IAM database authentication and data-api would need the FA
    # password mirrored into Secret Manager — a second authority for the credential.
    # cloud-run needs a small Cloud Run service the OPERATOR deploys (the plugin repo
    # ships a ps-dbops-sqlserver Terraform module); the dashboard only needs its
    # stable custom audience, below.
    #
    # Ships "off", and the reason is no longer channel readiness. Every channel is
    # implemented plugin-side now — data-api covers verify, change managed account,
    # change functional account and discovery on PostgreSQL and MySQL, cloud-run covers
    # SQL Server, and no action returns "not implemented in this build" any more — but
    # NONE of them has been exercised against a live Cloud SQL instance. "off" is the
    # honest default until one has. The three plugins are also a one-time MANUAL upload
    # (see docs); the platform names below are how the dashboard finds them.
    passwordsafe_gcp_db_registration_method: str = "off"  # dataapi | off
    clouddb_ps_platform_gcp_postgres: str = "GCP Cloud SQL PostgreSQL"
    clouddb_ps_platform_gcp_mysql: str = "GCP Cloud SQL MySQL"
    clouddb_ps_platform_gcp_sqlserver: str = "GCP Cloud SQL SQL Server"
    # "auto" takes the plugin's own recommended channel per engine (data-api for
    # postgres/mysql, cloud-run for sqlserver); an explicit value overrides it for
    # every engine.
    #
    # admin-api is a third channel the plugin implements and ps_resource_service
    # accepts, but the dashboard does not EMIT it: it needs cloudsql.users.update, which
    # among predefined roles lives only in the very broad roles/cloudsql.admin (against
    # roles/cloudsql.instanceUser for data-api), it performs no database login at all so
    # its Verify proves only the GCP identity, and it reaches only the instance's user
    # REGISTRY — a principal created inside the database with CREATE ROLE can be
    # invisible to it. Setting it here logs a warning and falls back to the per-engine
    # default rather than silently pretending.
    clouddb_ps_gcp_channel: str = "auto"  # auto | data-api | cloud-run
    # GCP counterparts of clouddb_ps_functional_account_* — read only when
    # clouddb_ps_functional_account_mode is "reference", which is the RECOMMENDED mode
    # here: unlike Azure, the GCP composite carries nothing per-database, so one
    # operator-owned account per engine covers every Cloud SQL instance.
    clouddb_ps_functional_account_gcp_postgres: str = ""   # on "GCP Cloud SQL PostgreSQL"
    clouddb_ps_functional_account_gcp_mysql: str = ""      # on "GCP Cloud SQL MySQL"
    clouddb_ps_functional_account_gcp_sqlserver: str = ""  # on "GCP Cloud SQL SQL Server"
    # The functional account's GCP identity mode — the username prefix. ADC uses the
    # Resource Broker's own credentials (an attached service account on a Compute
    # Engine broker, GOOGLE_APPLICATION_CREDENTIALS on an on-premises one) and stores
    # no key anywhere. IMP starts from ADC and impersonates the rotator service account
    # via roles/iam.serviceAccountTokenCreator. SA embeds a base64 key, which at ~3.2 KB
    # is over Password Safe's 1000-character credential limit — so it cannot survive a
    # functional-account write-back and is not a supported production mode.
    clouddb_ps_gcp_auth_mode: str = "ADC"          # ADC | IMP | SA
    clouddb_ps_gcp_impersonate_target: str = ""    # IMP mode: service account to impersonate
    # The operator-created rotation identity. The dashboard registers this as an IAM
    # database user on each instance it onboards and reads back the name the database
    # actually stored. KEEP IT SHORT: MySQL truncates an IAM database username at the
    # "@" and caps it at 32 characters, so "bt-rotator" is safe and
    # "bt-passwordsafe-cloudsql-rotator-prod" is not.
    clouddb_ps_gcp_rotator_service_account: str = ""  # e.g. bt-rotator@<project>.iam.gserviceaccount.com
    # data-api + SQL SERVER only. Cloud SQL for SQL Server has no IAM database
    # authentication, so the Data API cannot mint a token for the session — it needs the
    # functional account's real password out of Secret Manager, named by address option
    # "fasecret=". This is the address's SECOND authority for that credential, which is
    # exactly why cloud-run remains the recommendation: nothing there is mirrored.
    #
    # It must be a REGIONAL version resource name. The global form the plugin article's
    # example prints is rejected by the Data API with "does not match the expected
    # format [projects/*/locations/*/secrets/*/versions/*]"; ps_resource_service refuses
    # it at the click rather than letting a rotation discover it days later.
    #
    # Blank is fine in "create" mode — the dashboard stages the credential it minted
    # itself, per database. In "reference" mode it is REQUIRED, because the operator's
    # functional account has a password the dashboard has never seen.
    clouddb_ps_gcp_fa_secret_version: str = ""   # projects/<p>/locations/<r>/secrets/<n>/versions/latest
    # The cloud-run channel's Cloud Run service. The dashboard can DEPLOY this now
    # (clouddb_dbops_service, one per region), so the audience below is an OVERRIDE
    # rather than the only source: cloud_database_service._dbops_audience prefers the
    # recorded invoke_url of the deployed service IN THE DATABASE'S OWN REGION and
    # falls back to this key.
    #
    # That order is deliberate and is the opposite of the usual instinct. A flat key
    # answers "which service" globally; a Cloud Run service on Direct VPC egress is
    # REGION-LOCKED. An operator who sets this for a us-east1 service and later
    # onboards a database in europe-west1 would otherwise address every rotation for
    # it at a service that physically cannot reach the instance — and a rotation that
    # times out may already have applied the change.
    #
    # Address field 4 is used verbatim as BOTH the request target and the OIDC token
    # audience, so it must be a bare origin that actually resolves. When the dashboard
    # owns the service the audience simply IS the service URL, and no custom audience
    # is needed — --add-custom-audiences exists to decouple the two. Set this only for
    # a service you deployed yourself, or one behind a custom domain / Private Service
    # Connect front door.
    clouddb_ps_gcp_dbops_audience: str = ""   # override, e.g. https://bt-dbops.example.internal
    clouddb_ps_gcp_dbops_ssl: bool = True     # address field 5: sslTRUE | sslFALSE
    # Who may call the service. Comma-separated IAM members granted roles/run.invoker —
    # the Resource Brokers' own identities. NAMED SERVICE ACCOUNTS ONLY: the Terraform
    # module refuses allUsers/allAuthenticatedUsers outright, because this is an API
    # that changes credentials. Empty deploys a service nobody can call, which is the
    # safe direction and is visible immediately rather than at the first rotation.
    clouddb_ps_gcp_dbops_invokers: str = ""   # serviceAccount:broker@proj.iam.gserviceaccount.com,...
    # Ingress. "all" because an ON-PREMISES Resource Broker cannot reach an
    # internal-only Cloud Run service, and on-prem brokers are the common case. The
    # trade-off is real and is stated in docs/databases.md: a credential-changing API
    # on a globally resolvable endpoint, protected by IAM rather than by network
    # position. Compensate with constraints/iam.allowedPolicyMemberDomains at the org.
    # "internal" is correct when every broker runs on Compute Engine.
    clouddb_ps_gcp_dbops_ingress: str = "all"   # all | internal
    # Idle instances. 1 is a CORRECTNESS setting, not a latency one: Direct VPC egress
    # documents connection-establishment delays over a minute on instance start, and a
    # rotation that times out may already have applied the password change — Password
    # Safe then holds a credential the database has replaced. It bills continuously;
    # that is the trade being made deliberately. MUST stay annotated `int` (see the
    # note on the k8s token keys in api/setup.py).
    clouddb_ps_gcp_dbops_min_instances: int = 1
    # Per-request concurrency on the service. Well below Cloud Run's default of 80,
    # because each request holds a database connection and Cloud SQL's connection
    # limit is reached long before Cloud Run decides it needs another instance.
    clouddb_ps_gcp_dbops_concurrency: int = 8

    # EPM for Linux (EPM-L) — Pathfinder public API gateway.
    # The gateway base is api.beyondtrust.io (NOT app.beyondtrust.io — that host
    # only accepts browser session cookies and 401s every Bearer request). The
    # service appends /site/<epml_site_id>/epm/linux to this host; endpoint
    # paths from the EPM-L OpenAPI spec have their /api prefix replaced by that
    # base. Find your site id at https://app.beyondtrust.io/api/platform/currentSite
    # (signed in) — copy the `site_id` field.
    epml_base_url: str = "https://api.beyondtrust.io"
    epml_site_id: str = ""       # Pathfinder site UUID; PATs are bound to the site active at creation
    epml_pat: str = ""           # Personal Access Token (PAT_ prefix); encrypted at rest when set via the UI

    # EC2 deploy — instance profile + SSH keypair for dashboard-provisioned instances.
    # This block used to carry the local Packer/OVF image-building pipeline too: OVA +
    # ISO search paths, the Packer work/VMX roots, the VMware ovftool path, the
    # vm-import S3 prefix, the Packer surrogate instance profile and the ps-cli
    # guest/ISO-share secret titles. None of them had a reader left — image handling is
    # the cross-cloud registry behind /images now — so all eleven are gone. The five
    # *path* keys stay named in classify.py's _LOCAL_PATHS on purpose; see the comment
    # there before deleting them as stale.
    ec2_ssm_instance_profile: str = ""  # IAM instance profile to attach to dashboard-deployed EC2 instances (SSM access)
    ec2_ssh_key_secret: str = ""  # Secrets Manager secret name holding the SSH public key for EC2 deploy

    # ECS Jumpoint container (beyondtrust/sra-jumpoint)
    bt_ecs_cluster: str = "bt-jumpoint"
    bt_ecs_task_family: str = "bt-jumpoint"
    bt_ecs_image: str = "beyondtrust/sra-jumpoint"  # Override to use ECR mirror
    # "EC2" (default) runs the jumpoint on EC2 capacity so it can do PROTOCOL
    # TUNNELING — Fargate forbids the NET_ADMIN/NET_RAW/ipc_lock caps + /dev/net/tun
    # device the BeyondTrust Jumpoint needs for tunnels, so a Fargate jumpoint
    # registers as a node but tunnel connections time out. "FARGATE" is the legacy,
    # tunnel-incapable escape hatch. The sandbox script provisions the EC2 capacity.
    bt_ecs_launch_type: str = "EC2"
    bt_ecs_cpu: str = "256"    # 0.25 vCPU (Fargate task-size; ignored on EC2 host networking)
    bt_ecs_memory: str = "512"  # MB
    # Shared Jumpoint HOST (EC2 capacity) — the dashboard creates it on demand
    # when an EC2 instance or cloud database is provisioned, and terminates it
    # when nothing is left using it. The instance profile + role are pre-created
    # by scripts/sandbox/Linux/setup-aws.sh; bt_ecs_jumpoint_subnet_id /
    # bt_ecs_jumpoint_security_group_id below are the host's subnet + SG.
    bt_ecs_host_instance_type: str = "t3.small"
    # VM size for the managed shared Azure Gateway VM (jumpoint_host_service). Same
    # Web-Jump OOM constraint as gcp_jumpoint_machine_type: headless Chromium renders
    # ON the Gateway and needs ≥2 GB — Standard_B1ms minimum, Standard_B2s preferred.
    azure_jumpoint_vm_size: str = "Standard_B2s"
    # OT demo cell on AWS: refuse to deploy into a subnet that auto-assigns public IPs.
    # EC2 has no per-instance external-IP switch (GCE and Azure do, and the OT forms pin
    # them off there) — MapPublicIpOnLaunch decides — so this is the only place the
    # cell's air gap can be enforced rather than merely documented. Off = deploy anyway.
    ot_aws_require_private_subnet: bool = True
    # OT demo cell on GCP: fence the cell into its own Purdue zone with firewall rules
    # on its `ot-sim` network tag — no route out (outranking the on-demand NAT allow),
    # and inbound only from the PRA Gateway's `bt-jumpoint` tag. Default OFF: it changes
    # the network posture of a running demo, so it is a deliberate choice, not a surprise.
    ot_purdue_firewall_enabled: bool = False
    bt_ecs_host_instance_profile: str = "ecsInstanceRole"
    bt_ecs_host_name: str = "dashboard-sandbox-jumpoint-host"  # EC2 Name tag (find-or-create key)
    bt_ecs_execution_role_arn: str = ""  # Set to your ecsTaskExecutionRole ARN if required
    # BeyondTrust Jumpoint Docker registry deploy key for AWS ECS launches.
    # Stored encrypted via config_service; transparently resolved through whichever
    # secrets backend the user picked on /secrets. The legacy `bt_ps_deploy_key_title`
    # remains as a Password-Safe-only fallback.
    aws_ecs_docker_deploy_key: str = ""

    # Shared, on-demand NAT instance for sandbox VM egress. When enabled, the
    # dashboard creates ONE NAT instance (auto public IP, no EIP) on the first EC2
    # deploy and points the private route table's 0.0.0.0/0 at it, then terminates
    # it when the last VM is destroyed — so private-subnet VMs get outbound internet
    # with zero standing cost. Set by scripts/sandbox/Linux/setup-aws.sh. See
    # services/nat_instance_service.py. Blanks: SG → find-or-create; subnet →
    # bt_ecs_jumpoint_subnet_id (public); AMI → newest AL2023 for the arch.
    aws_nat_instance_enabled: bool = False
    aws_nat_instance_type: str = "t4g.nano"
    aws_nat_instance_name: str = "dashboard-sandbox-nat"  # EC2 Name tag (find-or-create key)
    aws_nat_security_group_id: str = ""
    aws_nat_subnet_id: str = ""
    aws_nat_ami_id: str = ""

    # Shared, on-demand SSM interface VPC endpoints (ssm, ssmmessages, ec2messages)
    # for the private-subnet SSM path (Password Safe SSM VM onboarding, cloud-DB
    # dbssm). When enabled, the dashboard creates the three interface endpoints
    # (private DNS) in the sandbox private subnet on the first EC2 deploy / AWS
    # cloud-DB provision and deletes them when the last such resource is
    # decommissioned — so private-subnet SSM works with zero standing cost (each
    # interface endpoint bills ~$7/mo while up). Set by scripts/sandbox/Linux/
    # setup-aws.sh. See services/ssm_endpoint_service.py. Blank SG → find-or-create
    # dashboard-sandbox-ssm-vpce-sg.
    aws_ssm_endpoints_enabled: bool = False
    aws_ssm_vpce_security_group_id: str = ""

    # Portainer CE integration — a single connection, configured via
    # Settings → Integrations → Portainer CE (config_service); these env vars
    # are the fallback for compose-file-driven installs.
    portainer_url: str = ""                          # e.g. "http://portainer.local:9000"
    portainer_pat: str = ""                          # API token; Settings stores it encrypted in the DB
    portainer_pat_secret_title: str = "Portainer_PAT"  # legacy fallback: BeyondTrust Password Safe secret title
    portainer_verify_ssl: bool = True                # Set False for self-signed certs
    # Managed Portainer node (deploy/teardown lifecycle; see gcp_portainer_* below).
    # A successful deploy writes portainer_url / portainer_pat / portainer_verify_ssl
    # above, so the integration wires itself up. Read live via config_service.
    portainer_node_cloud: str = "gcp"          # aws|azure|gcp — WHICH cloud hosts the single managed Portainer node. Picked per deploy (like the region) and rewritten to where it actually landed, so teardown + bare redeploys stay put. Default gcp because every node deployed before this key existed is a GCE VM; redeploying to another cloud RELOCATES the node
    portainer_allowed_source_cidrs: str = ""   # CSV of manual firewall source ranges; empty = rely on the auto-detected dashboard egress CIDR (fail closed unless gcp_portainer_allow_open)
    portainer_dashboard_egress_cidr: str = ""  # the dashboard's own public egress CIDR (auto-detected + persisted on deploy); the worker bootstraps the node over its public IP
    portainer_admin_password: str = ""         # first-run admin password; auto-generated when unset
    portainer_admin_password_generated: bool = False  # marks the password above as dashboard-generated (so it can be surfaced once)
    portainer_ready_timeout_s: int = 300       # how long the deploy waits for Portainer to serve after the VM boots (cold image pull); raise for slow disks
    # Managed Portainer node — optional PRA Web Jump brokering its UI (mirrors the
    # rancher_ui_* block above). Opt-in: lets an operator whose IP isn't in
    # portainer_allowed_source_cidrs reach the UI from the PRA representative console
    # (brokered + recorded, no CIDR change), with the admin credential injected from
    # the PRA Vault instead of being shown.
    portainer_ui_web_jump_enabled: bool = False   # gate the sra_web_jump broker; False = use the direct public URL
    portainer_ui_verify_certificate: bool = False # sra_web_jump verify_certificate (False for the node's self-signed cert on :9443)
    portainer_ui_jump_group: str = ""             # "" = bt_jump_group_name
    portainer_ui_jumpoint_name: str = ""          # "" = bt_jumpoint_name
    portainer_ui_web_jump_id: str = ""            # PRA Web Jump id for the Portainer UI (runtime-set)
    portainer_ui_web_jump_tfstate: str = ""       # terraform state for the Web Jump (for teardown)
    portainer_ui_vault_account_group_id: str = "" # PRA Vault account group (numeric id) the admin credential is vaulted into for Web-Jump injection; chosen at deploy. "" = no vault (fall back to bt_vault_account_group_id, else surface the password)
    portainer_ui_vault_account_id: str = ""       # PRA Vault account id created for the Portainer admin credential (runtime-set; cleared on teardown)
    portainer_ui_jumpoint_cloud: str = "gcp"      # which dashboard-managed Jumpoint host brokers the Portainer UI (gcp|aws|azure); its egress IP is auto-whitelisted. gcp = same cloud as the node
    portainer_ui_jumpoint_egress_ip: str = ""     # dashboard-managed Web-Jump Jumpoint host egress IP (runtime-set; auto-added to the node firewall as a /32). all three managed Gateway hosts expose one: GCP + AWS via the host's public IP, Azure via a Standard, secure-by-default public IP on its NIC (Standard IPs block all inbound unless an NSG allows it, so it is egress-only)
    ansible_local_image: str = "chrweav/ansible-winrm:latest"
    # Ansible runner image for Kubernetes-cluster / cloud-database config-management
    # targets (localhost plays that reach out via kubeconfig / DB login vars). Carries
    # kubernetes.core + community.postgresql/mysql/general and their client libs; see
    # runners/ansible-cloud/. Used for ALL cloud runners (ECS/ACI/Cloud Run) on k8s/DB
    # targets — never the winrm image (it lacks these collections).
    ansible_cloud_image: str = "chrweav/ansible-cloud:latest"

    # Azure resource-management credentials.
    # Preferred: set the four direct env vars below (community edition / simple
    # deployments). If all four are blank, the dashboard falls back to looking
    # them up by title in BeyondTrust Password Safe using the *_secret_title
    # fields that follow (enterprise / prod).
    azure_client_id: str = ""
    azure_client_secret: str = ""
    azure_tenant_id: str = ""
    azure_subscription_id: str = ""
    # BeyondTrust Password Safe secret titles (used only when the four direct
    # env vars above are blank and BeyondTrust is configured).
    azure_client_id_secret_title: str = "Azure_Client_ID"
    azure_client_secret_secret_title: str = "Azure_Client_Secret"
    azure_tenant_id_secret_title: str = "Azure_Tenant_ID"
    azure_subscription_id_secret_title: str = "Azure_Subscription_ID"
    # Non-secret config (store in .env)
    azure_resource_group: str = "vm-cli-rg"       # default RG for deployed VMs
    azure_location: str = "centralus"             # default Azure region (overridden by .env)
    azure_vnet_resource_group: str = ""           # RG containing VNets (may differ)
    azure_shared_image_gallery: str = ""          # Shared Image Gallery name (optional)
    azure_gallery_resource_group: str = ""        # RG of the gallery (optional)
    # ACI Jumpoint (mirrors bt_ecs_* settings)
    azure_aci_resource_group: str = ""            # defaults to azure_resource_group if empty
    azure_aci_subnet_id: str = ""                 # required for ACI VNet injection
    azure_aci_jumpoint_image: str = "beyondtrust/sra-jumpoint:latest"
    ansible_aci_image: str = "chrweav/ansible-winrm:latest"  # Ansible image for ACI config mgmt runner
    ansible_aci_ssh_key_secret_name: str = ""  # Azure Key Vault secret name for the Ansible SSH private key
    azure_aci_cpu: float = 1.0
    azure_aci_memory: float = 2.0
    # BeyondTrust Jumpoint Docker registry deploy key for Azure ACI launches.
    # Stored encrypted via config_service; transparently resolved through whichever
    # secrets backend the user picked on /secrets. The legacy `*_ps_deploy_key_title`
    # remains as a Password-Safe-only fallback.
    azure_aci_docker_deploy_key: str = ""
    azure_aci_ps_deploy_key_title: str = "ACI_Docker_Deploy_Key"  # Legacy: PS-only secret title (fallback)
    azure_aci_storage_account: str = ""           # Storage account name for /jpt persistent volume
    azure_aci_storage_account_rg: str = ""        # RG of the storage account (defaults to ACI RG if empty)
    azure_image_storage_account: str = ""         # Storage account for temp VHD upload during OVA→Azure image import
    azure_aci_file_share: str = "jpt"             # Azure File Share name for /jpt mount
    azure_jumpoint_name: str = ""                 # name of the pre-existing Jumpoint for Azure Shell Jumps
    # How a SINGLE Azure VM deploy reaches its Jumpoint. "shared" (default) borrows the
    # ref-counted clouddb-jumpoint VM — tunnel-capable, and no shared /jpt identity store
    # to corrupt. "aci" starts a dedicated ACI container group per deploy (Shell Jump
    # only; ACI cannot protocol-tunnel). Batches always share one ACI group.
    azure_vm_jumpoint_mode: str = "shared"        # "shared" | "aci"
    # ACR credentials (leave empty to pull from Docker Hub without auth).
    # Direct fields are preferred; values are stored encrypted in the DB and
    # transparently resolved through the chosen secrets backend (PS / AWS SM /
    # Azure KV / GCP SM) by config_service.get(). The legacy `*_secret_title`
    # fields below remain as a Password-Safe-only fallback.
    azure_acr_server: str = ""                    # e.g. myregistry.azurecr.io
    azure_acr_username: str = ""                  # ACR username / SP appId
    azure_acr_password: str = ""                  # ACR password / SP secret (encrypted at rest)
    azure_acr_username_secret_title: str = ""     # Legacy: PS-only secret title (fallback)
    azure_acr_password_secret_title: str = ""     # Legacy: PS-only secret title (fallback)
    azure_bt_jump_group_name: str = ""            # BT jump group for Azure Shell Jumps (falls back to bt_jump_group_name)
    # Azure Key Vault — SSH key retrieval (optional; leave blank to disable)
    azure_key_vault_url: str = ""                     # e.g. "https://my-vault.vault.azure.net/"
    # RG of the vault above, for the cloud_function Azure module's data lookup (what
    # lets it detect RBAC-vs-access-policy and grant the right one). Empty = the
    # vault lives in azure_resource_group, which is true of most deployments.
    azure_key_vault_resource_group: str = ""
    azure_ssh_keypair_secret_name: str = "azureVM-ssh-keypair"  # Unified secret: JSON {public_key, private_key}
    azure_ssh_key_secret_name: str = ""               # Legacy: separate public-key secret (fallback)
    azure_ssh_private_key_secret_name: str = ""       # Legacy: separate private-key secret (fallback)
    azure_ssh_username: str = "azureuser"             # Cloud-default login user (Entitle SSH-ephemeral registration; admin override)

    # Azure Automation (Hybrid Runbook Worker — set by Container App env vars from Terraform)
    azure_automation_account_name: str = ""
    azure_automation_resource_group: str = "vm-cli-hosting-rg"
    azure_hybrid_worker_group: str = "on-prem-powershell-workers"

    # SSL / HTTPS (leave empty to run plain HTTP)
    ssl_certfile: str = ""   # path to cert.pem, e.g. web_dashboard/certs/cert.pem
    ssl_keyfile: str = ""    # path to key.pem,  e.g. web_dashboard/certs/key.pem


    # FIDO2 / WebAuthn MFA
    webauthn_rp_id: str = "localhost"             # bare domain, no port (e.g. dashboard.example.com)
    webauthn_rp_name: str = "Infrastructure Management Dashboard"
    webauthn_origin: str = "http://localhost:8001"  # must exactly match scheme://host:port browser uses

    # Azure AD OAuth Login (SEPARATE app registration from resource-management service principal)
    # Create a new App Registration; required delegated permissions: openid, profile, email
    # Add redirect URI: http://localhost:8001/api/auth/oauth/azure/callback
    azure_oauth_client_id: str = ""
    azure_oauth_client_secret: str = ""
    azure_oauth_tenant_id: str = ""
    azure_oauth_redirect_uri: str = "http://localhost:8001/api/auth/oauth/azure/callback"

    # ── Generic OIDC SSO (any compliant provider) ─────────────────────────────
    # Driven entirely by the issuer's .well-known/openid-configuration, so one
    # implementation covers Okta, Auth0, Keycloak, Authentik, Authelia, Google
    # Workspace, JumpCloud, Ping, GitLab — and Entra, if you prefer it over the
    # azure_oauth_* path above. Additive: leaving these blank changes nothing.
    # Redirect URI to register with the provider:
    #   <dashboard-base-url>/api/auth/oauth/oidc/callback
    oidc_issuer: str = ""            # e.g. https://keycloak.example.com/realms/main
    oidc_client_id: str = ""
    oidc_client_secret: str = ""     # omit for a public client (PKCE is always sent)
    oidc_provider_name: str = ""     # login-button label; defaults to the issuer host
    oidc_scopes: str = "openid profile email groups"
    oidc_groups_claim: str = "groups"  # claim holding group names/ids for workgroup mapping
    # Group-to-workgroup mapping: JSON dict of { "entra_group_object_id": "WorkgroupName" }
    # Users are matched against their group claims and assigned the corresponding workgroups.
    # Members of any listed group are auto-created on first login — no pre-registration needed.
    # A user in multiple groups receives all matched workgroups.
    # Leave empty to fall back to the old behaviour (user must exist in the local DB).
    # Example: {"aaaa-...-aaaa": "Hydra", "bbbb-...-bbbb": "Weaverlab"}
    # Declared as Any so pydantic-settings doesn't pre-parse the env var as JSON;
    # the validator below handles both valid JSON and the legacy unquoted KV format.
    azure_oauth_group_map: Any = {}

    @field_validator("azure_oauth_group_map", mode="before")
    @classmethod
    def _parse_group_map(cls, v: Any) -> dict:
        if isinstance(v, dict):
            return v
        if not isinstance(v, str):
            return {}
        v = v.strip()
        if not v or v in ("{}", ""):
            return {}
        # Try standard JSON first
        try:
            return json.loads(v)
        except json.JSONDecodeError:
            pass
        # Fall back: handle unquoted {key: value, key: value} (Key Vault legacy format)
        inner = v.strip("{}").strip()
        if not inner:
            return {}
        result = {}
        for pair in re.split(r",\s*", inner):
            if ":" in pair:
                k, val = pair.split(":", 1)
                result[k.strip()] = val.strip()
        return result

    # Cloud object storage. Originally introduced for Ansible playbooks; now
    # exposed as its own /storage page so future features can reuse the same
    # backend abstraction. Four cloud backends supported — S3, Azure Blob, GCS,
    # OCI Object Storage — configured independently. The active backend is the one
    # selected via storage_active_backend; others can be configured-but-idle for
    # migration. OCI is hub-only (see storage_hub_backend): the active backend also
    # decides where Terraform state lives and Terraform has no OCI state backend.
    storage_active_backend: str = ""           # "s3" | "azure_blob" | "gcs" | "local"
    # Image-registry hub backend — the single backend that holds the canonical
    # VHD/raw artefact for every registered image regardless of build cloud.
    # When unset, falls back to storage_active_backend so single-backend installs
    # Just Work. Used by the Packer export+register flow and the (upcoming)
    # per-target-cloud promote runners.
    storage_hub_backend: str = ""              # "" | "s3" | "azure_blob" | "gcs" | "oci_object_storage"
    storage_s3_bucket: str = ""                # e.g. "infra-asset-store"
    storage_s3_region: str = ""                # defaults to aws_region if blank
    storage_s3_prefix: str = "config-mgmt"
    storage_azure_account: str = ""            # storage account name
    storage_azure_container: str = "playbooks"
    storage_azure_prefix: str = "config-mgmt"
    storage_gcs_bucket: str = ""
    storage_gcs_prefix: str = "config-mgmt"
    # OCI Object Storage. Credentials come from the oci_* API-key block below —
    # only the bucket is storage-specific. The namespace is per-tenancy and
    # auto-detected via the Object Storage API when left blank.
    storage_oci_bucket: str = ""
    storage_oci_namespace: str = ""            # blank → looked up from the tenancy
    storage_oci_prefix: str = "config-mgmt"
    # Local filesystem / SMB UNC backend. Path can be either a normal
    # filesystem path inside the container (e.g. a bind-mounted host dir)
    # or a UNC \\server\share[\subpath]. UNC paths are read via the
    # smbprotocol library — no host-side mount required. Username /
    # password / domain only apply to UNC paths. Only useful for
    # on-premises hypervisor targets — see storage-management.md.
    storage_local_path: str = ""
    storage_local_username: str = ""
    storage_local_password: str = ""           # encrypted at rest
    storage_local_domain: str = ""
    # Remote filesystem / UNC reached THROUGH a remote agent. The same kind of
    # target as storage_local_* above, minus its constraint: the dashboard never
    # touches the share, so a cloud-hosted dashboard with no route to a corporate
    # file server can still use one. That is the whole point — on ACA (and on a POV
    # instance, which has no cloud provider at all) the local backend above is the
    # one backend designed for on-prem assets that cannot be used.
    #
    # NOTE WHAT IS *NOT* HERE: no path, no username, no password. Those live in the
    # agent's own shares.yaml, which the dashboard cannot read or write, and the
    # agent's policy.yaml grants share NAMES. This field is one half of a join, the
    # same way agent_connection_name is for a hypervisor connection — the string is
    # the whole contract, and a corporate share credential never enters this DB.
    storage_agent_id: str = ""                 # RemoteAgent.id that brokers the share
    storage_agent_share: str = ""              # `name:` of an entry in its shares.yaml
    storage_agent_subpath: str = ""            # optional subdirectory within the share

    # ── Promote runner ───────────────────────────────────────────────────────
    # Transient container launched in the target cloud to convert + upload a
    # VM image artefact during cross-cloud promotion. Same image (defaulting
    # to a public build under chrweav) serves AWS / Azure / GCP targets;
    # the target's own runner orchestration (ECS task / ACI / Cloud Run job)
    # is configured separately per cloud — only AWS-target is wired today.
    promote_runner_image: str = "chrweav/dashboard-promote-runner:latest"
    promote_runner_ecs_cluster: str = ""                 # fallback: ansible_ecs_cluster
    promote_runner_ecs_task_family: str = "promote-runner"
    promote_runner_ecs_cpu: str = "1024"                 # qemu-img wants headroom
    promote_runner_ecs_memory: str = "4096"              # ~4 GiB for multi-GB VHDs
    # Fargate's DEFAULT ephemeral volume is 20 GiB, shared with the runner image's
    # own layers — and the task writes the whole source disk to /tmp (a 17 GiB VHD
    # leaves well under a GiB free). libguestfs then can't build its supermin
    # appliance and virt-customize dies with "supermin exited with error status 1",
    # which reads like a broken image rather than a full disk. Ask for room:
    # 21-200 GiB is the Fargate range, and the extra GiB-hours are pennies on a
    # task that lives for minutes.
    promote_runner_ecs_ephemeral_storage_gib: str = "100"
    promote_runner_ecs_subnet_id: str = ""               # fallback: ansible_ecs_subnet_id
    promote_runner_ecs_security_group_ids: str = ""      # fallback: ansible_ecs_security_group_ids
    promote_runner_ecs_execution_role_arn: str = ""      # required (image pull + log write)
    promote_runner_ecs_task_role_arn: str = ""           # required (S3 write to staging bucket)
    # Where the runner drops the converted artefact before AWS image-import
    # consumes it. Defaults to the storage S3 bucket under a `promote-staging/`
    # prefix so operators don't have to provision a separate bucket.
    promote_runner_aws_staging_bucket: str = ""          # fallback: storage_s3_bucket
    promote_runner_aws_staging_prefix: str = "promote-staging"

    # ── Azure-target promote runner (ACI) ────────────────────────────────────
    # Same image as the AWS path; the dashboard passes `--target azure` at
    # task-launch time. Falls back to the existing Azure-side knobs (ACI
    # Ansible runner / Azure config) so single-account installs only need to
    # set non-default values.
    promote_runner_azure_resource_group: str = ""        # fallback: azure_resource_group
    promote_runner_azure_location: str = ""              # fallback: azure_location
    promote_runner_azure_subnet_id: str = ""             # optional ACI VNet binding
    promote_runner_azure_cpu: str = "2"                  # qemu-img headroom
    promote_runner_azure_memory_gb: str = "4"            # ~4 GiB for multi-GB VHDs
    # Target staging — where the runner drops the converted VHD before the
    # image-create call consumes it. Same hub account+container by default so
    # operators don't need to provision a second account.
    promote_runner_azure_staging_account: str = ""       # fallback: storage_azure_account
    promote_runner_azure_staging_container: str = ""     # fallback: storage_azure_container
    promote_runner_azure_staging_prefix: str = "promote-staging"
    # The RG the resulting managed image lands in. Defaults to azure_resource_group.
    promote_runner_azure_target_resource_group: str = ""
    # Storage account ARM ID the resulting managed image's OS disk references.
    # Optional — if blank, Azure assigns one. Set when locking the image to a
    # specific account is required (compliance, BYOK).
    promote_runner_azure_target_storage_account_id: str = ""

    # ── GCP-target promote runner (Cloud Run job) ────────────────────────────
    # Same image as the AWS/Azure path; the dashboard passes `--target gcs`
    # at launch time. The runner additionally wraps the converted raw disk
    # into a `disk.raw` tar.gz before upload (GCP image-insert quirk —
    # documented in runners/promote/README.md). Falls back to existing
    # gcp_* / storage_gcs_* keys for single-tenant installs.
    promote_runner_gcp_region: str = ""                  # fallback: gcp_region
    promote_runner_gcp_cpu: str = "4"                    # Cloud Run requires >=4 vCPU above 8Gi
    promote_runner_gcp_memory: str = "16Gi"              # memory-backed /tmp: VHD + raw disk + tar.gz
    promote_runner_gcp_vpc_connector: str = ""           # optional, for private-network egress
    promote_runner_gcp_service_account: str = ""         # optional: workload-identity SA email for the runner
    promote_runner_gcp_staging_bucket: str = ""          # fallback: storage_gcs_bucket
    promote_runner_gcp_staging_prefix: str = "promote-staging"
    promote_runner_gcp_image_family: str = ""            # optional family label on the resulting custom image

    # ── OCI-target promote runner (Container Instances) ──────────────────────
    # Same image as the other clouds; the dashboard passes `--target oci`. The
    # runner converts to QCOW2 (OCI's custom-image import format) and uploads to
    # Object Storage; the dashboard then imports the compute image from it. Falls
    # back to the primary oci_* config so single-tenant installs need little new.
    promote_runner_oci_compartment: str = ""             # fallback: oci_compartment_ocid
    promote_runner_oci_availability_domain: str = ""     # blank → first AD in the compartment
    promote_runner_oci_subnet_ocid: str = ""             # fallback: oci_default_subnet_ocid (runner VNIC)
    promote_runner_oci_ocpus: float = 2.0                # qemu-img headroom (CI.Standard.E4.Flex)
    promote_runner_oci_memory_gbs: float = 16.0          # ~multi-GB image convert
    promote_runner_oci_staging_bucket: str = ""          # Object Storage bucket the QCOW2 stages in (required for OCI promote)
    promote_runner_oci_staging_prefix: str = "promote-staging"

    ansible_runner: str = "local"              # "local" | "ecs" | "aci" | "gcp" — global default/fallback
    # Per-target-cloud Ansible runner backend. Overrides ansible_runner for that
    # cloud's targets; blank → fall back to ansible_runner. Each cloud's only
    # sensible cloud backend is its own task service, so the value is "local" or
    # the matching service (AWS→ecs, Azure→aci, GCP→gcp). The run request's
    # `cloud` field selects the key — see web_dashboard/api/config_mgmt.py.
    ansible_runner_aws: str = ""               # "" | "local" | "ecs"
    ansible_runner_azure: str = ""             # "" | "local" | "aci"
    ansible_runner_gcp: str = ""               # "" | "local" | "gcp"
    # Per-cloud SSH user for Ansible cloud runner targets. Each cloud's stock
    # AMI / image family ships with a different default username, so a single
    # global value would be wrong for at least two of the three. Set the one
    # matching the runner you actually use; the others can stay at the default.
    # ansible_default_user is the final fallback when an unrecognised cloud
    # tag is passed (rare, ad-hoc target paths).
    ansible_aws_user: str = "ec2-user"        # Amazon Linux default; "ubuntu" / "admin" for other AMIs
    ansible_azure_user: str = "azureuser"     # Azure Linux VM convention
    ansible_gcp_user: str = "gcp-user"        # matches the gcp_ssh_username default
    ansible_default_user: str = "ec2-user"    # fallback for unknown cloud tags
    ansible_ecs_cluster: str = "bt-jumpoint"  # Shares cluster with BT Jumpoint
    ansible_ecs_task_family: str = "ansible-config-mgmt"
    ansible_ecs_image: str = "chrweav/ansible-winrm:latest"
    ansible_ecs_cpu: str = "256"
    ansible_ecs_memory: str = "512"
    ansible_ecs_subnet_id: str = ""           # Fargate task subnet (VPC private subnet recommended)
    ansible_ecs_security_group_ids: str = ""  # Comma-separated security group IDs (optional)
    ansible_ecs_execution_role_arn: str = ""  # Set if image pull requires it
    ansible_ssh_key_secret: str = "AWS_KEY"        # Password Safe secret title (legacy fallback)
    ansible_ssh_key_sm_name: str = "ec2/ssh-keypair"  # AWS Secrets Manager secret name/ARN (preferred)
    # GCP Cloud Run Jobs ansible runner (mirrors azure_ansible_aci_image / ACI runner)
    gcp_ansible_cloud_run_region: str = ""   # defaults to gcp_region if blank
    gcp_ansible_image: str = "chrweav/ansible-winrm:latest"
    gcp_ansible_vpc_connector: str = ""      # e.g. "projects/proj/locations/region/connectors/name" (optional, for private host access)
    # Direct VPC egress for Cloud Run runner jobs — the job's NIC lands straight in
    # the subnet (no Serverless-VPC-Access connector: no standing cost, and immune
    # to the connector's shared-core zonal stockouts). Set BOTH; wins over
    # gcp_ansible_vpc_connector when set. Egress stays private-ranges-only.
    gcp_run_network: str = ""                # VPC name, e.g. "dashboard-sandbox-vpc"
    gcp_run_subnetwork: str = ""             # subnet in the runner's region, e.g. "dashboard-sandbox-jumpoint-subnet"
    # Stranded-runner reaper. Each runner deletes its own Cloud Run Job in a `finally`;
    # a worker restart between the execution ending and that delete landing strands the
    # job in the project. The reaper is the safety net (see gcp_service). Disabling it
    # only stops the automatic sweep — the Containers reap action still works.
    gcp_cloud_run_job_reap_enabled: bool = True
    gcp_cloud_run_job_reap_age_minutes: int = 60   # must exceed a runner's whole lifetime

    # ── Auto-delete timer (resource expiry) ──────────────────────────────────
    # Gives dashboard-provisioned cloud VMs, databases and k8s clusters an expiry,
    # then destroys them when it passes — the same teardown the Destroy button runs.
    # See services/expiry_policy.py for the guards and services/expiry_reaper.py for
    # the sweep.
    #
    # OFF is not the only brake, because this feature deletes infrastructure:
    #   * a resource with expires_at NULL is never touched, and every resource that
    #     predates the feature is NULL — so enabling it acts on nothing;
    #   * resource_expiry_default_hours=0 (and pov_expiry_default_hours=0) means new
    #     deploys aren't stamped either, so flipping only the master switch still
    #     changes nothing;
    #   * resource_expiry_dry_run=True means even a stamped, overdue fleet is only
    #     REPORTED;
    #   * resource_expiry_enforce=False is a second, separate gate on deletion.
    # Floors an operator cannot lower (minimum lifetime, reap grace, arming delay,
    # per-pass cap) are module constants in expiry_policy, not keys here.
    resource_expiry_enabled: bool = False
    resource_expiry_enforce: bool = False          # deletion gate; on-but-inert until set
    resource_expiry_dry_run: bool = True           # report only, enqueue nothing
    resource_expiry_default_hours: int = 0         # 0 = don't stamp new deployments
    # A POV environment's own default, because it describes something different: an
    # evaluation that runs for WEEKS with a customer inside it, not a scratch VM. 0 means
    # don't stamp, same as above, so this changes nothing until an operator sets it.
    # Note resource_expiry_max_total_hours (30d) still caps the total lifetime — raise it
    # if your evaluations run longer than that.
    pov_expiry_default_hours: int = 0              # 0 = don't stamp new POVs

    # ── The per-POV spend cap ────────────────────────────────────────────────
    # The money answer to the question the auto-delete timer answers in time. Accrued from
    # list prices every reconcile pass, never read off a bill — see services/pov_spend for
    # why a billing API would report a runaway rather than stop one.
    #
    # 0 = don't stamp a cap on new POVs, the same "master switch alone changes nothing"
    # brake `resource_expiry_default_hours` uses. A POV can always be given one by hand.
    pov_spend_cap_default_usd: float = 0.0
    # "warn" | "suspend". Defaults to warn, and deliberately: the figure is a list-price
    # ESTIMATE, and one that suspended a live customer demo on its first outing would be
    # the last time anybody trusted it. Suspending is reversible in one click, which is
    # why this needs no dry-run and no arming clock of its own.
    pov_spend_cap_action: str = "warn"
    # Warn at this percentage of the cap. Clamped to 10-99 by pov_spend.warn_percent:
    # below 10 every POV warns immediately and the warning stops being read, above 99 the
    # warning and the cap arrive together.
    pov_spend_warn_percent: int = 80
    resource_expiry_extend_hours: int = 24         # what one Extend click adds
    resource_expiry_max_total_hours: int = 720     # 30d ceiling, counted from created_at
    resource_expiry_warn_hours: int = 24           # "expiring soon" window
    resource_expiry_grace_minutes: int = 30        # floored at REAP_GRACE_MIN_FLOOR
    resource_expiry_sweep_interval_minutes: int = 30
    # How long a COMPLETED sweep row survives on /jobs. 0 = keep forever. The sweep is the
    # only job type written on a timer whether or not it had work — 48 rows/day at the
    # default interval — and nothing else prunes `jobs`. Failed passes never expire.
    resource_expiry_sweep_retention_days: int = 7
    resource_expiry_max_per_pass: int = 10         # capped at MAX_PER_PASS_CEILING
    resource_expiry_allow_never: bool = False      # may an admin clear a timer outright
    resource_expiry_exempt_workgroups: str = ""    # CSV; mirrors the admission_* lists

    # ── Background job worker concurrency ────────────────────────────────────
    # How many jobs the dedicated worker (web_dashboard.jobs_worker) runs at once. It
    # used to run exactly ONE, and "run more" meant more docker-compose replicas — a
    # lever a PaaS host doesn't give you per-worker, and one that costs a whole DB
    # connection pool per replica. These caps put the concurrency inside one process.
    # Replicas still MULTIPLY them (the queue claim is atomic), so total = replicas x caps.
    #
    # Tiered because the jobs are not alike (jobs_worker.HEAVY/MEDIUM/LIGHT_TYPES):
    #   heavy  — a long LOCAL subprocess streamed line by line (terraform apply, packer
    #            build, `docker run`). One JobLog INSERT per output line.
    #   medium — cloud SDK plus a SHORT local process: a PRA/Entitle/Password-Safe
    #            terraform in a tempdir, or kubectl/helm at k8s_runner=local (default).
    #   light  — start a cloud operation, then poll its API. Image exports/promotes/
    #            copies, gateways, epml_sync, expiry_sweep. Hours of near-pure waiting;
    #            these are the ones that used to queue behind a Packer build.
    #
    # These are the BOOTSTRAP defaults. The normal way to change them is Settings →
    # Job Worker, which stores them in app_config and takes effect within ~5s with no
    # restart. Ceilings an operator cannot raise live in worker_policy, not here.
    worker_heavy_concurrency: int = 2
    worker_medium_concurrency: int = 1
    worker_light_concurrency: int = 3
    # Aggregate ceiling across the tiers, and what the pools are sized from — the tier
    # caps deliberately sum higher, since they bound COMPOSITION and this bounds TOTAL.
    # Clamped down at startup if the DB pool can't serve it (jobs_worker._limits).
    worker_max_concurrency: int = 3
    worker_executor_threads: int = 0               # 0 = derive from the caps above
    worker_drain_timeout_s: int = 20               # SIGTERM grace; see the pool note below
    # Health endpoint for the runner, which has no other port. ENV-ONLY and deliberately
    # not a Settings toggle: it is read once in jobs_worker.main() before init_db(), which
    # is BEFORE the first config read can happen, and moving it later would defeat it --
    # the wedge it exists to catch happens inside init_db(). 0 disables the listener.
    # Not 8000: that is the app's port, and the two containers read the same .env.
    worker_health_port: int = 8080

    # ── Database connection pool ─────────────────────────────────────────────
    # ENV-ONLY, and it cannot be otherwise: create_engine runs at import in database.py,
    # before any connection exists, so the pool that connects to the database can't be
    # sized from a value stored in that database. This is why the worker caps above are
    # clamped to the pool at runtime rather than the pool being grown to fit them.
    #
    # Budget, per PROCESS: db_pool_size + db_max_overflow. The app runs `gunicorn -w 2`
    # (2 pools) and the worker 1 per replica, so one deployment holds
    # 3 x (size + overflow) — 30 at the defaults. Keep it under (max_connections - 20),
    # the 20 covering the server's own management and superuser-reserved sessions.
    # Check with `SHOW max_connections;`. Azure Postgres Flexible Server Burstable B1ms
    # is 50 (so 5+5 is the ceiling there, and the worker clamps concurrency to 3); B2s
    # and every General Purpose tier are 429+ and can take much more.
    #
    # App and worker are separate deployments, so they can carry DIFFERENT values for
    # these under the same names — giving the worker a bigger pool than the request path
    # buys concurrency at no extra connection cost.
    db_pool_size: int = 5
    db_max_overflow: int = 5
    db_pool_timeout_s: int = 30                    # checkout wait before QueuePool raises
    # Under any server-side idle timeout. Azure's load balancer drops idle TCP after
    # minutes, which without this surfaces as an InterfaceError mid-job on a long
    # poller's first query after a quiet gap — reachable today at concurrency 1.
    db_pool_recycle_s: int = 1800

    # ── Blocking cloud SDK calls (services/cloud_executor.py) ────────────────
    # Threads PER PROVIDER, and the deadline on one blocking call. Every cloud SDK call
    # used to share the event loop's default executor — min(32, os.cpu_count() + 4),
    # which in a container is computed from the HOST's CPU count and came out at 8. The
    # home page fans out ~8 per-cloud reads, so one slow provider parked every thread in
    # the process and the whole dashboard hung until it was restarted (2026-08-12).
    #
    # A pool per provider is what bounds that: a wedged provider exhausts its own threads
    # and AWS, Azure and the pure-DB routes keep serving. Sizing is read once per
    # provider at first use — a live pool cannot be resized safely — so a change here
    # needs a restart, unlike the worker_* knobs.
    cloud_pool_size: int = 8
    # Request path. Far above a healthy read (~1s) and far below the 240s the ingress
    # allowed while the site was wedged.
    cloud_call_timeout_s: int = 60
    # Job path, applied only in jobs_worker (cloud_executor.use_worker_defaults). Above
    # the longest poller — a GCP image export runs to 7200s inside ONE to_thread call —
    # so this bounds runaway work without capping legitimate work.
    cloud_worker_call_timeout_s: int = 14400

    # ── Outbound notifications ───────────────────────────────────────────────
    # Sends dashboard events to webhook endpoints (Slack, Microsoft Teams via a Power
    # Automate Workflows URL, or a signed generic envelope you point at whatever you
    # like — that last one is how email is delivered; there is no SMTP client here).
    # Endpoints themselves are rows in `notification_endpoints`, not keys, because
    # their URLs are credentials and there can be several. See docs/notifications.md.
    #
    # Two brakes, because this sends messages to people:
    #   * notifications_enabled off means nothing is emitted, drained or scanned;
    #   * notify_dry_run=True — still the default once ON — records what WOULD be sent
    #     and sends nothing, so the first pass against a live estate fills a log rather
    #     than a channel.
    notifications_enabled: bool = False
    notify_dry_run: bool = True
    notify_event_types: str = ("resource.expiring,resource.reaped,job.failed,"
                               "cost.budget_exceeded,secret.stale,config.drift")
    notify_min_severity: str = "warning"           # info | warning | critical
    notify_base_url: str = ""                      # absolute origin for deep links
    notify_http_timeout_s: int = 10
    notify_flush_interval_s: int = 30              # drain cadence, re-read every pass
    notify_scan_interval_s: int = 3600             # cost / secret / drift condition scan
    notify_max_attempts: int = 4                   # then the delivery is terminal-failed
    notify_max_per_flush: int = 50
    notify_max_queue: int = 500                    # above this, emit() suppresses
    notify_retention_days: int = 30                # 0 = keep delivery rows forever

    # Ephemeral cloud secrets for managed-account checkout on the ECS / Cloud Run
    # runners. OFF by default: a checked-out Password Safe credential is written to
    # the cloud secret store as a short-lived, RBAC-locked secret, injected via the
    # provider's secret channel, then force-deleted after the run. Enabling this
    # copies a PAM-vaulted credential into the cloud store for the task's lifetime —
    # pair it with "Change Password After Release" on the managed account so a
    # missed cleanup leaves only a rotated, dead credential. See docs/secrets-management.md.
    ansible_cloud_ephemeral_secrets_enabled: bool = False
    ansible_ephemeral_secret_ttl_min: int = 30       # GC safety-net age (>= max task runtime)
    # Password Safe request duration for a managed-account checkout. Must outlast the
    # whole run so the request is still open for us to flag rotate-on-check-in and
    # then check it in afterwards (best-effort — rotation isn't enforceable, it
    # depends on the account being auto-managed). Default 60 min covers a long cloud task.
    ansible_managed_request_duration_min: int = 60
    # Same idea for a remote agent's just-in-time hypervisor credential, but the window
    # has to cover more than the job: an agent whose container dies stops heartbeating,
    # and the request is only released once `reconcile_stale_jobs` has failed the job
    # (STALE_AFTER_MINUTES=10) and the sweeper has next run (RECONCILE_INTERVAL=60s). So
    # the floor is job runtime + ~11 minutes; a request that expires before then is
    # checked in by Password Safe itself, which is safe but leaves the release audit
    # trail looking like the dashboard never tidied up. Declared here and not only as a
    # config_service key on purpose — `get_bool`/`_cfg` fall back to `getattr(settings,
    # ...)`, so a key with no field here reads as its default forever no matter what the
    # operator sets in the environment.
    agent_ps_checkout_duration_min: int = 45
    # Rotate the managed account's password when the credential is checked back in, so
    # the value the agent held is dead the moment its job ends. Default ON: it is the
    # reason to reach for a managed account rather than a stored password in the first
    # place. Turn it off only where Password Safe is not the sole owner of that account —
    # something else configured statically with the same password would break on rotation.
    agent_ps_rotate_on_release: bool = True
    ansible_ephemeral_kms_key_id: str = ""           # AWS: CMK for the ephemeral secret; its key policy
                                                     # should grant kms:Decrypt to the ECS execution role
                                                     # only (the true read-restriction on AWS). "" = default key.
    gcp_ansible_runner_service_account: str = ""     # GCP: SA the Cloud Run job runs as; REQUIRED for ephemeral
                                                     # on GCP so secretAccessor can be bound to just that SA.

    # Kubernetes (kubectl/helm) runner. "local" runs in-process; the cloud modes
    # run cluster-API ops as a one-shot stock kubectl+helm task with clean egress
    # (a TLS-inspecting corp proxy rejects/526s direct kubectl/helm). Reuses the Ansible
    # runner's per-cloud ECS/ACI/Cloud Run network settings (see k8s_runner_service).
    k8s_runner: str = "local"                # "local" | "ecs" | "aci" | "gcp" — global default/fallback
    # Per-target-cluster-cloud runner backend. Overrides k8s_runner for that
    # cloud's clusters; blank → fall back to k8s_runner. "local" or the matching
    # service (AWS/EKS→ecs, Azure/AKS→aci, GCP/GKE→gcp). The cluster's cloud
    # (K8sCluster.cloud) selects the key — see k8s_runner_service.mode().
    k8s_runner_aws: str = ""                  # "" | "local" | "ecs"
    k8s_runner_azure: str = ""                # "" | "local" | "aci"
    k8s_runner_gcp: str = ""                  # "" | "local" | "gcp"
    k8s_runner_oci: str = ""                  # "" | "local"  (an OCI Container-Instance runner is a follow-up; OKE uses in-process kubectl + oke_get_token for now)
    k8s_runner_image: str = "dtzar/helm-kubectl:latest"  # shared default for all clouds
    # Per-target-cluster-cloud image override; blank → k8s_runner_image. Lets Azure
    # pull from an ACR mirror (avoiding Docker Hub) while AWS/GCP use the shared
    # default — an AWS/GCP runner can't authenticate to an Azure ACR.
    k8s_runner_image_aws: str = ""
    k8s_runner_image_azure: str = ""
    k8s_runner_image_gcp: str = ""

    epml_rpm_path: str = ""
    epml_deb_path: str = ""
    pathfinder_script_path: str = ""

    # Packer image builder — optional object-storage archives for built templates.
    # Leave blank to skip archiving; fill in to have each successful build upload
    # the generated .pkr.hcl to your bucket for auditing and re-use.
    packer_aws_s3_bucket: str = ""
    packer_azure_storage_account: str = ""
    packer_azure_archive_container: str = "packer-templates"
    packer_gcs_bucket: str = ""

    # GCP (Google Cloud Platform)
    gcp_project_id: str = ""
    gcp_region: str = "us-central1"
    gcp_zone: str = "us-central1-a"
    gcp_service_account_json: str = ""   # Full service account JSON key content, stored encrypted
    gcp_network: str = "default"
    gcp_subnetwork: str = ""             # Full subnetwork self-link or name
    gcp_ssh_key_secret_name: str = ""    # Secret Manager secret name for SSH key pair
    gcp_ssh_username: str = "gcp-user"
    # BeyondTrust Jumpoint Docker registry deploy key. Stored encrypted via
    # config_service; transparently resolved through whichever secrets backend
    # the user picked on /secrets. The historical key name was retained when
    # the Jumpoint host moved from Cloud Run (HTTP-required) to a small
    # Container-Optimised-OS GCE instance.
    gcp_cloud_run_docker_deploy_key: str = ""
    gcp_jumpoint_image: str = "beyondtrust/sra-jumpoint:latest"
    # e2-medium (4 GB), NOT e2-micro (1 GB): a Web Jump renders the target UI in a
    # headless Chromium ON THE GATEWAY (`sra-web.bin`). At 1 GB that is OOM-killed by
    # the kernel — observed live 2026-08-17, `global_oom` with Chromium's own
    # ThreadPoolService invoking the oom-killer, which drops every session on the node
    # and reads in PRA as "the endpoint has disconnected" (or, before the session gets
    # that far, "internal timeout starting session"). A tunnel-only gateway would be
    # fine on e2-micro; nothing here knows in advance which it will be asked to carry,
    # and AWS already sizes its host t3.small for the same reason. Dial down to e2-small
    # (2 GB) if cost matters more than concurrent Web Jumps.
    gcp_jumpoint_machine_type: str = "e2-medium"
    gcp_jumpoint_zone: str = ""          # blank → use the deploy zone
    # Which Jumpoint a SINGLE GCP VM deploy gets. "shared" (default) borrows the
    # ref-counted host that cloud databases, k8s tunnels and VDI seats already use;
    # "paired" gives every VM its own bt-jumpoint-<vm> e2-micro, the pre-2026-07
    # behaviour. Batches always share. A deploy carrying its own docker_deploy_key_ref
    # is forced to "paired" regardless, since the shared host resolves its key globally.
    gcp_vm_jumpoint_mode: str = "shared"  # "shared" | "paired"
    # Network tag(s) automatically attached to every dashboard-deployed user
    # VM. Comma-separated. Used to scope sandbox firewall rules (e.g. the
    # egress-deny rule on the sandbox VM subnet keys off this tag). Set to
    # `dashboard-sandbox-vm` when paired with scripts/sandbox/setup-gcp.sh.
    gcp_default_network_tag: str = ""
    # On-demand outbound internet for sandbox VMs (the GCP analog of
    # aws_nat_instance_enabled). The sandbox leaves the vm-subnet off Cloud NAT AND
    # adds a priority-1000 egress deny on the tag above, so a VM has no internet —
    # `dnf update` / config-mgmt playbooks time out. When enabled, the dashboard adds a
    # SECOND Cloud NAT gateway on the sandbox's existing Cloud Router (scoped to the
    # vm-subnet) plus a higher-priority egress ALLOW on the first VM deploy, and removes
    # both when the last VM in that region is destroyed — so VMs get egress with zero
    # standing cost and the sandbox's own NAT + deny rule are never modified. No-ops
    # when the region has no Cloud Router. See services/gcp_nat_service.py.
    gcp_vm_nat_enabled: bool = True
    gcp_vm_nat_name: str = "dashboard-sandbox-vm-nat"
    gcp_vm_egress_rule_name: str = "dashboard-sandbox-vm-egress-ondemand"
    gcp_vm_egress_rule_priority: int = 900  # must beat the sandbox deny at 1000
    gcp_bt_jump_group_name: str = ""     # BT jump group for GCP Shell Jumps (falls back to bt_jump_group_name)
    gcp_jumpoint_name: str = ""          # Jumpoint name for GCP Shell Jumps (falls back to bt_jumpoint_name)
    # On-demand Entitle DB forwarder (GCP-only). A private Cloud SQL PSA IP is
    # unreachable from the Entitle agent's own GKE VPC (non-transitive peering);
    # when a GCP DB is registered in Entitle the dashboard stands up a tiny socat
    # relay (COS-on-GCE) in the sandbox VPC that the agent CAN reach over the
    # GKE↔sandbox peering, and tears it down on deregister/decommission. OFF by
    # default. See services/entitle_db_proxy_service.py.
    gcp_entitle_db_proxy_enabled: bool = False
    gcp_entitle_db_proxy_source_ranges: str = "10.98.0.0/22,10.100.0.0/16"  # GKE agent node+pod ranges allowed into the forwarder (terraform/k8s_cluster/gcp_gke defaults)
    gcp_entitle_db_proxy_image: str = "alpine/socat:latest"                 # socat relay container image (pulled over Cloud NAT)
    gcp_entitle_db_proxy_machine_type: str = "e2-micro"                     # forwarder VM size (free-tier eligible in us-central1)
    # Rancher management node — a single privileged Rancher container on a
    # Container-Optimized-OS GCE VM with a PUBLIC (source-restricted) IP. Same
    # COS/konlet mechanism as the Jumpoint. The node is treated as EPHEMERAL: a
    # stop/recreate reassigns the external IP and wipes /var/lib/rancher (boot
    # disk auto-deletes), so it must re-bootstrap and downstream clusters must
    # re-import. Read live via config_service.
    gcp_rancher_image: str = "rancher/rancher:latest"  # Rancher server container image
    gcp_rancher_machine_type: str = "e2-medium"        # Rancher needs ≥4 GB RAM; e2-micro/small OOM
    gcp_rancher_zone: str = ""            # blank → gcp_zone
    gcp_rancher_name: str = "rancher-server"
    gcp_rancher_boot_disk_gb: int = 30    # COS boot disk (holds /var/lib/rancher; auto-deletes on stop)
    gcp_rancher_network_tag: str = "rancher"  # network tag on the VM = firewall target tag
    gcp_rancher_allow_open: bool = False  # opt-in to open 0.0.0.0/0 when rancher_allowed_source_cidrs is empty; otherwise empty = firewall NOT opened (fail closed)
    rancher_ready_timeout_s: int = 360    # how long the deploy waits for Rancher to serve after the VM boots (cold rancher/rancher pull + bootstrap); raise for slow disks/large images
    # Managed Portainer CE server — a single (unprivileged) Portainer container on a
    # Container-Optimized-OS GCE VM with a PUBLIC (source-restricted) IP. Same
    # COS/konlet mechanism as the Rancher node above, and equally EPHEMERAL: the boot
    # disk auto-deletes, so a teardown/recreate wipes /var/lib/portainer (users,
    # environments, settings). Serves 9443 (HTTPS UI/API) + 8000 (Edge agent tunnel).
    gcp_portainer_image: str = "portainer/portainer-ce:latest"  # Portainer server container image
    gcp_portainer_machine_type: str = "e2-small"   # Portainer is light; e2-small is ample
    gcp_portainer_zone: str = ""          # blank → gcp_zone / auto-picked in the region
    gcp_portainer_name: str = "portainer-server"
    gcp_portainer_boot_disk_gb: int = 20  # COS boot disk (holds /var/lib/portainer; auto-deletes on delete)
    gcp_portainer_network_tag: str = "portainer"  # network tag on the VM = firewall target tag
    gcp_portainer_allow_open: bool = False  # opt-in to open 0.0.0.0/0 when portainer_allowed_source_cidrs is empty; otherwise empty = firewall NOT opened (fail closed)
    # Optional DURABLE state: back /data with a separate persistent disk (konlet
    # gcePersistentDisk volume) instead of the auto-delete boot disk, so users,
    # environments and settings survive a teardown/recreate. A PD is ZONAL, so an
    # existing disk PINS the node's zone — moving region needs a snapshot, not a
    # redeploy. Off by default: the disk outlives the node and costs money until
    # something deletes it.
    portainer_data_disk_enabled: bool = False   # back /data with a persistent disk that survives teardown
    gcp_portainer_data_disk_gb: int = 10        # size of that data disk (Portainer's DB is small; 10 GB is ample)

    # ── Managed nodes on AWS (same two features, EC2 instead of GCE) ──────────
    # No konlet equivalent exists on AWS, so the container is started by `docker run`
    # from EC2 user-data on the ECS-optimized Amazon Linux 2023 AMI (resolved per
    # region from its SSM public parameter — the same image the Gateway host uses, so
    # Docker is already installed). The instance deliberately does NOT join an ECS
    # cluster and gets no instance profile: like its GCE counterpart the node needs no
    # cloud API access, and joining a cluster would add a task definition plus the
    # ecsInstanceRole policy dependency for nothing.
    #
    # There is no aws_*_zone: an EC2 subnet already pins the availability zone, so a
    # zone knob could only contradict it. There is no network tag either — a dedicated
    # security group named <node>-allow-mgmt replaces GCE's target-tag model, and
    # fail-closed means "every ingress rule revoked" rather than "rule deleted",
    # because a security group in use by a running instance cannot be deleted.
    aws_rancher_image: str = "rancher/rancher:latest"    # Rancher server container image
    aws_rancher_instance_type: str = "t3.medium"         # Rancher needs ≥4 GB RAM; t3.small (2 GB) OOMs
    aws_rancher_name: str = "rancher-server"             # Name tag + security-group base name
    aws_rancher_boot_disk_gb: int = 30                   # gp3 root volume (holds /var/lib/rancher; deleted with the instance)
    aws_rancher_allow_open: bool = False                 # opt-in to open 0.0.0.0/0 when rancher_allowed_source_cidrs is empty; otherwise empty = no ingress (fail closed)
    aws_rancher_zone: str = ""                           # RECORDED, not chosen: the availability zone the node landed in (the subnet pins it). Written on deploy for diagnostics + teardown
    aws_portainer_image: str = "portainer/portainer-ce:latest"  # Portainer server container image
    aws_portainer_instance_type: str = "t3.small"        # Portainer is light; t3.small is ample
    aws_portainer_name: str = "portainer-server"         # Name tag + security-group base name
    aws_portainer_boot_disk_gb: int = 20                 # gp3 root volume (holds /data when no data volume; deleted with the instance)
    aws_portainer_allow_open: bool = False               # opt-in to open 0.0.0.0/0 when portainer_allowed_source_cidrs is empty
    aws_portainer_zone: str = ""                          # RECORDED, not chosen: the availability zone the node (and so its data volume) landed in
    # Durable /data on AWS is a separate gp3 EBS volume with DeleteOnTermination=false.
    # An EBS volume is ZONAL like a GCE PD, so an existing one PINS the node's AZ — and
    # because an existing volume cannot be attached by run_instances (block-device
    # mappings only create new ones), it is attached after the instance is running and
    # the user-data WAITS for it before starting the container. Without that wait
    # Portainer would come up against an unmounted directory and write its database to
    # the root volume, losing it on the next recreate.
    aws_portainer_data_disk_gb: int = 10                 # size of that EBS data volume

    # -- Managed nodes on Azure (same two features, an Azure VM) ---------------
    # Cloud-init runs `docker run`, the same mechanism the Gateway VM already uses. ACI
    # is deliberately NOT used: Rancher needs --privileged, which a container group
    # cannot give it, so ACI could serve at most one of the two features -- and two
    # different Azure shapes for two near-identical nodes is worse than one.
    #
    # Two differences from AWS, both in Azure's favour. The public IP is Standard SKU
    # and a Standard IP must be Static, so the node KEEPS ITS ADDRESS across a recreate
    # -- Portainer Edge keys and Rancher's server-url survive here where they do not on
    # GCP or AWS. And a data disk can be attached at CREATE time, so cloud-init mounts
    # it before the container starts with no polling (the AWS path cannot: an existing
    # EBS volume is only attachable after the instance is running).
    #
    # Ingress is a dedicated NSG on the NIC. A Standard public IP denies all inbound
    # unless a rule allows it, so fail-closed here is DELETING the allow rule -- the
    # same shape as GCE's "delete the firewall rule", not AWS's "revoke everything".
    azure_rancher_image: str = "rancher/rancher:latest"   # Rancher server container image
    azure_rancher_vm_size: str = "Standard_B2s"           # Rancher needs >=4 GB RAM; Standard_B1s (1 GB) OOMs
    azure_rancher_name: str = "rancher-server"            # VM + NSG base name
    azure_rancher_boot_disk_gb: int = 30                  # OS disk (holds /var/lib/rancher; deleted with the VM)
    azure_rancher_allow_open: bool = False                # opt-in to open 0.0.0.0/0 when rancher_allowed_source_cidrs is empty
    azure_rancher_zone: str = ""                          # RECORDED, not chosen: the location the node landed in, so a bare redeploy stays there
    azure_portainer_image: str = "portainer/portainer-ce:latest"  # Portainer server container image
    azure_portainer_vm_size: str = "Standard_B1s"         # Portainer is light; 1 GB is ample
    azure_portainer_name: str = "portainer-server"        # VM + NSG base name
    azure_portainer_boot_disk_gb: int = 30                # OS disk (holds /data when no data disk)
    azure_portainer_allow_open: bool = False              # opt-in to open 0.0.0.0/0 when portainer_allowed_source_cidrs is empty
    azure_portainer_zone: str = ""                        # RECORDED, not chosen: the location the node (and so its data disk) landed in
    azure_portainer_data_disk_gb: int = 10                # size of the durable managed data disk (delete_option=Detach, so it outlives the VM)

    # ── Oracle Cloud Infrastructure (OCI) ─────────────────────────────────────
    # The fourth cloud provider. Compute VM CRUD is SDK-based (the `oci` Python
    # SDK), mirroring aws_service / gcp_service; Autonomous DB + OKE go through
    # Terraform (oracle/oci provider). Auth = OCI API-key signing: tenancy OCID +
    # user OCID + key fingerprint + the private-key PEM (+ optional passphrase) +
    # region. Every resource lives in a compartment (oci_compartment_ocid; blank →
    # the tenancy root). "Configured" (see /api/features) = tenancy + user + key +
    # region all set. All secrets are stored encrypted via config_service.
    oci_tenancy_ocid: str = ""            # ocid1.tenancy.oc1..…
    oci_user_ocid: str = ""               # ocid1.user.oc1..… (the API-signing user)
    oci_fingerprint: str = ""             # API signing-key fingerprint (aa:bb:cc:…)
    oci_private_key: str = ""             # API signing private key PEM (encrypted at rest)
    oci_private_key_passphrase: str = ""  # optional private-key passphrase (encrypted at rest)
    oci_region: str = "us-ashburn-1"      # home/target region identifier
    oci_compartment_ocid: str = ""        # target compartment; blank → tenancy root
    # Deploy defaults (blank → resolved from the compartment / VCN at request time).
    oci_vcn_ocid: str = ""                # VCN the VM subnets live in
    oci_default_subnet_ocid: str = ""     # subnet for deployed VM VNICs
    oci_ssh_key_secret: str = ""          # OCI Vault secret (OCID or name) holding the SSH keypair JSON {public_key, private_key}
    oci_ssh_username: str = "opc"          # Cloud-default login user (Entitle SSH-ephemeral registration; admin override)
    oci_vault_ocid: str = ""              # Vault the SSH/secret material lives in (for name→OCID lookups)
    # Free-tier guardrail — the Always-Free envelope the deploy form defaults to
    # and warns when exceeded (see services/oci_freetier.py). Advisory caps, not
    # account-wide usage tracking; going beyond needs an explicit acknowledgment.
    oci_freetier_enforce: bool = True     # surface the warn+confirm gate; False = no free-tier warnings
    # BeyondTrust PRA per-cloud overrides (fall back to the shared bt_* keys).
    oci_bt_jump_group_name: str = ""      # BT jump group for OCI Shell Jumps (falls back to bt_jump_group_name)
    oci_jumpoint_name: str = ""           # Jumpoint name for OCI Shell Jumps (falls back to bt_jumpoint_name)

    # Entitle integration — shared API credentials (used by machine-identity
    # JIT, user-JIT, and resource registration below).
    # Entitle API base, and it is REGIONAL: api.entitle.io, api.us.entitle.io and
    # api.ca.entitle.io are separate deployments, not aliases. Set the one your tenant
    # lives in — every one of them answers 200 to an unauthenticated probe, so pointing
    # at the wrong region does not fail here; it fails later, as a tenant that appears
    # to hold none of your resources. Drives machine-identity JIT and (normalized to
    # scheme+host) the entitleio/entitle provider endpoint.
    #
    # The default is the US region because that is where this project's own tenant is.
    # It is a DEFAULT and not a discovery: an install in another region must set this,
    # and nothing will complain if it does not. NB the entitleio/entitle provider's own
    # built-in default is the unprefixed api.entitle.io, so the two disagree — which is
    # harmless only because _provider_endpoint derives the endpoint from this value
    # rather than letting the provider fall back. See entitle_registration_service.
    entitle_api_url: str = "https://api.us.entitle.io/v1"
    entitle_api_token: str = ""                     # bearer token (Key Vault secret in prod)

    # Entitle resource registration — as the dashboard builds Linux VMs and
    # cloud databases it registers each as an Entitle integration (SSH ephemeral
    # accounts / PostgreSQL / MySQL / SQL Server) via the entitleio/entitle
    # Terraform provider. OFF by default = no registration calls.
    entitle_registration_enabled: bool = False
    entitle_api_key: str = ""                       # entitleio/entitle TF provider key (ENTITLE_API_KEY); falls back to entitle_api_token
    # API base for the TF provider. Blank does NOT mean the provider's own built-in
    # default (the unprefixed https://api.entitle.io) — _provider_endpoint derives it
    # from entitle_api_url first, which is the regional one. Only a blank entitle_api_url
    # would reach the provider's default, and that would be the wrong region for us.
    entitle_endpoint: str = ""
    entitle_owner_id: str = ""                       # REQUIRED: UUID of the Entitle user owning created integrations
    entitle_workflow_id: str = ""                    # REQUIRED: UUID of the default approval workflow for created integrations
    entitle_agent_token_name: str = ""               # Entitle Agent token NAME/identifier for private targets (the token VALUE is supplied to the agent cluster via ESO — see docs/design/entitle-resource-registration.md)
    entitle_agent_token_ref: str = ""                # optional secrets-backend ref where the agent token VALUE is stored (for bootstrap/rotation; not the integration identifier above). Auto-set to config://entitle/agent-token by ensure_agent_token when a token is minted.
    entitle_agent_token_tf_state: str = ""           # terraform.tfstate of an auto-minted agent token (set by ensure_agent_token; enables later destroy/rotation via deregister). DB-only — never an env value.
    # Entitle agent cluster bootstrap (Task 7) — Helm-install the agent into a managed
    # K8s cluster via the k8s_service runner. See docs/design/entitle-resource-registration.md.
    entitle_agent_cluster_id: str = ""               # set on a successful install — the cluster currently hosting the shared agent
    entitle_agent_chart_repo: str = "https://anycred.github.io/entitle-charts/"  # Helm repo URL for the entitle-agent chart (BeyondTrust-published)
    entitle_agent_chart: str = "entitle-agent"       # chart name within the repo
    entitle_agent_chart_version: str = ""            # optional pinned chart version
    entitle_agent_namespace: str = "entitle"         # in-cluster namespace for the agent + its token Secret
    entitle_agent_secret_name: str = "entitle-agent-token"  # K8s Secret (key ENTITLE_TOKEN) used by the existing-Secret path
    # The published chart takes the token as a plaintext --set value (agent.token); it
    # has no existingSecret option, so the plaintext path is the default. The token is
    # still resolved server-side (never on a row/TF state), but DOES land in the
    # in-cluster Helm release Secret — a chart limitation. Clear the plaintext key +
    # set the existing-secret key to switch to the apply-Secret path if a future chart
    # version supports it.
    entitle_agent_token_plaintext_helm_key: str = "agent.token"  # Helm value the token is passed to (plaintext, server-side resolved)
    entitle_agent_existing_secret_helm_key: str = "agent.existingSecret"  # used only when the plaintext key is cleared (future chart)
    entitle_agent_helm_extra_set: str = ""           # extra `--set key=value` args, comma-separated (e.g. datadog.datadog.apiKey=…); the chart bundles Datadog
    entitle_agent_kms_type: str = "kubernetes_secret_manager"  # where the running agent vaults integration creds
    # Per-target-cloud kmsType override; blank → entitle_agent_kms_type. AKS needs
    # azure_secret_manager — the in-cluster-Secrets path 401s there; the azure_aks
    # module builds the workload-identity MI + per-cluster Key Vault it requires.
    # EKS/GKE keep kubernetes_secret_manager. Keyed off the cluster's cloud.
    entitle_agent_kms_type_aws: str = ""
    entitle_agent_kms_type_azure: str = "azure_secret_manager"
    entitle_agent_kms_type_gcp: str = ""
    # ServiceAccount the agent pod runs as (the chart's default). Must equal the AKS
    # federated-credential subject's SA (azure_aks module) — pinned on the install.
    entitle_agent_service_account: str = "entitle-agent-sa"
    # Register managed clusters as Entitle Kubernetes integrations (generic "Kubernetes"
    # app; EKS/AKS/GKE). External access mints a least-priv ServiceAccount; private API
    # clusters use the agent's In-Cluster access.
    entitle_k8s_user_prefix: str = "entitle"         # user_prefix Entitle uses for the ephemeral cluster identities
    entitle_k8s_sa_name: str = "entitle-access"      # ServiceAccount minted in-cluster for External-Access registration

    # PRA-only K8s access (no Entitle): a cluster-admin ServiceAccount whose
    # long-lived bearer token is stored in the PRA Vault and injected at session
    # launch. The dedicated namespace is safe to delete wholesale on tunnel removal
    # (token revocation). bt_vault_account_group_id (numeric) places the Vault
    # account in a group so a PRA group policy grants it to users.
    pra_k8s_namespace: str = "pra-access"            # dedicated ns for the PRA ServiceAccount (deleted on revoke)
    pra_k8s_sa_name: str = "pra-access"              # ServiceAccount minted in-cluster for PRA Vault token injection
    k8s_api_tunnel_local_port: int = 6443            # local listen port for the direct API TCP tunnel (kubeconfig points at 127.0.0.1:<this>)
    # Entra/IdP group → cluster RBAC (real-identity JIT): default group the k8s "Entra
    # group" action binds (per-cluster override in the action). Members get <role>;
    # Entitle's Entra-ID integration JIT-grants membership. group_id = Entra Object ID.
    entra_rbac_group_id: str = ""
    entra_rbac_group_name: str = ""                  # OPTIONAL friendly name (display only)
    entra_rbac_group_role: str = "cluster-admin"     # ClusterRole the group binds to
    # Entra OIDC federation for EKS (the "Entra federation" action's AWS leg): a
    # shared Entra app registration is associated as the cluster's OIDC IdP so a
    # user's Entra token authenticates and its group OIDs match the RBAC binding
    # above. client_id = the app's Application (client) ID (= token audience);
    # issuer blank → derived from azure_tenant_id (login.microsoftonline.com/<t>/v2.0).
    entra_oidc_client_id: str = ""                   # shared Entra app client id (OIDC audience); required to federate EKS
    entra_oidc_issuer_url: str = ""                  # blank → https://login.microsoftonline.com/<azure_tenant_id>/v2.0
    entra_oidc_username_claim: str = "oid"           # OIDC username claim (portable Entra user Object ID)
    entra_oidc_groups_claim: str = "groups"          # OIDC groups claim (Entra emits group Object IDs)
    # GKE Workforce Identity Federation (the "Entra federation" action's GCP leg):
    # GKE can't use an OIDC IdP (GKE Identity Service is off for new orgs), so a
    # user reaches the cluster through Connect Gateway as a workforce identity. The
    # RBAC subject is principalSet://…/workforcePools/<pool>/group/<entra-oid> — the
    # same Entra group, wrapped in the pool URI. The pool + Entra OIDC provider are
    # created once at the org level (gcloud iam workforce-pools).
    gcp_workforce_pool_id: str = ""                  # bare workforce pool id (e.g. bt-entra-pool); required to federate GKE
    gcp_workforce_provider_id: str = ""              # OIDC provider id in the pool (e.g. bt-entra-oidc); for the end-user login config
    gcp_workforce_location: str = "global"           # workforce pool location (always "global" today)
    bt_vault_account_group_id: str = ""              # OPTIONAL — PRA Vault account group id for injected k8s/DB credentials
    # ── Password-Safe-managed k8s ServiceAccount token rotation ──────────────
    # The PRA-injected SA token above becomes a Password Safe managed account on the
    # "Kubernetes Service Account Token" custom plugin, rotated on the tenant's
    # schedule; a second managed account on the "PRA Vault Token" plugin mirrors each
    # rotation into the PRA Vault copy. Operator prerequisites (manual): import both
    # .psplugins, create the platforms, create the per-cloud functional accounts, and
    # grant the pscli identity Requestor + an auto-release access policy on the new
    # managed accounts. See docs/design/k8s-sa-token-rotation.md.
    k8s_ps_token_rotation_enabled: bool = False      # master gate: row action, provision checkbox, sync loop
    k8s_ps_token_platform: str = "Kubernetes Service Account Token"  # plugin platform (name or id)
    k8s_ps_pravault_token_platform: str = "PRA Vault Token"          # mirror plugin platform (name or id)
    k8s_ps_functional_account_aws: str = ""          # per-cloud functional account (name or id); _local is the
    k8s_ps_functional_account_azure: str = ""        # generic/on-prem (and OKE) fallback
    k8s_ps_functional_account_gcp: str = ""
    k8s_ps_functional_account_local: str = ""
    k8s_ps_pravault_functional_account: str = ""     # FA for the mirror (PRA Config API OAuth client)
    k8s_ps_workgroup: str = ""                       # blank → passwordsafe_workgroup
    k8s_ps_token_mode: str = "longlived"             # longlived (rotation revokes) | bound (TokenRequest, no revoke)
    k8s_ps_token_ttl_seconds: int = 3600             # bound mode requested TTL; API-server floor is 600
    k8s_ps_token_change_on_register: bool = True     # rotate once on register — proves the whole path immediately
    k8s_ps_token_delete_legacy_secret: bool = True   # retire the dashboard-minted Secret the plugin's sweep never touches
    k8s_ps_token_register_on_provision: bool = False  # provision-form checkbox default
    k8s_ps_pravault_mirror_enabled: bool = True      # register the "PRA Vault Token" mirror when a PRA vault account exists
    k8s_ps_token_checkout_duration_min: int = 15     # Password Safe request duration for token reads
    k8s_ps_token_address_options: str = ""           # extra ;key=value appended to every address (serverName=, dnsEndpoint=true, …)
    # In-cluster rotator RBAC (the plugin's scripts/rbac.yaml). The binding subject
    # differs per cloud and mostly CANNOT be derived: AKS needs the SP's OBJECT id (not
    # the client id in the FA username); EKS needs the access-entry username + the IAM
    # principal ARN behind the FA's access key. GKE's subject IS the FA's account name
    # (the SA email), so it is derived when the override is blank.
    k8s_ps_rotator_apply_rbac: bool = True
    k8s_ps_rotator_gke_sa_email: str = ""            # blank → derived from the GCP functional account's name
    k8s_ps_rotator_aks_sp_object_id: str = ""        # the oid claim — the plugin logs it on every run
    k8s_ps_rotator_eks_username: str = "passwordsafe-rotator"   # access-entry username = RBAC User subject
    k8s_ps_rotator_eks_principal_arn: str = ""       # IAM role/user behind the FA's access key (not derivable)
    k8s_ps_rotator_eks_create_access_entry: bool = True  # create the access entry when the ARN is set (never touches aws-auth)
    k8s_ps_rotator_bootstrap_namespace: str = "beyondtrust"     # generic path bootstrap SA namespace
    k8s_ps_rotator_bootstrap_sa: str = "password-safe-rotator"  # generic path bootstrap SA name
    # No PS → PRA sync settings: Password Safe owns that. Registration links the PRA
    # Vault account to the token account with SyncedAccounts, and a managed account and
    # its subscribers always share a credential, so every rotation reaches PRA with
    # nothing here on a timer and no interval to tune.
    entitle_allowed_durations: str = "3600,43200,86400"  # JIT durations (seconds) offered on created integrations
    entitle_ssh_sudo_user: str = ""                 # OPTIONAL override — each VM deploy passes its image's cloud-default login user (ubuntu/ec2-user/azureuser/gcp-user) automatically; set this only to force a different sudo user for ALL registrations
    entitle_ssh_private_key_ref: str = ""           # OPTIONAL fallback/override only — the SSH private key is normally sourced from the VM's own per-cloud keypair (the key cloud-init injected). See docs/design/entitle-resource-registration.md
    entitle_db_service_user_ref: str = ""           # optional override; default uses the DB's minted master credential

    # Cloud-identity JIT (machine-flow elevations via Entitle)
    # See docs/design/cloud-identity-jit.md for the design.
    # Phase 0 ships the scaffolding behind this flag; default OFF means
    # cloud_identity_service.elevate() is a no-op and every cloud write
    # uses today's standing credentials.
    cloud_identity_gate_enabled: bool = False
    machine_ttl_ceiling_minutes: int = 60           # hard upper bound per elevation request
    # Synthetic machine-identity submitted as `behalfOf` on Entitle access requests.
    # Phase 1+ requires this to be set when the gate is on; empty fails closed.
    entitle_machine_identity_email: str = ""
    entitle_machine_poll_interval_ms: int = 400     # 250–500ms recommended by design

    # BeyondTrust Workload Credentials (WC; codename SMoP) — the third credential
    # posture. WC mints short-lived AWS/Azure credentials on demand, so the
    # standing cloud secret stops existing rather than being time-boxed.
    #
    # Every default below preserves today's behaviour: the master flag is off, so
    # a community install with no BeyondTrust products is untouched and keeps
    # using the static keys in app_config. Turning this on is what UNLOCKS
    # retiring an install's own static credentials — it never retires them for
    # anyone else. See docs/integrations/workload-credentials.md.
    workload_credentials_enabled: bool = False
    wlc_api_base_url: str = "https://api.beyondtrust.io"
    wlc_site_id: str = ""                           # site (tenant) GUID — the `tenant_id` claim in your access JWT
    wlc_pat: str = ""                               # Personal Access Token; SECRET — registered in secret_hygiene.SECRET_REGISTRY
    # Mandatory `bt-secrets-api-version` header. Date-based; matches the shipping
    # Terraform provider's DefaultAPIVersion. A wrong value fails in a way that
    # reads like an auth error, so it is explicit rather than inferred.
    wlc_api_version: str = "2026-04-28"
    wlc_api_path_version: str = ""                  # optional path segment (e.g. "v1"); empty = no version in the path
    # Per-cloud opt-in, mirroring cloud_identity_{cloud}_enabled. GCP is absent
    # deliberately — WC covers AWS and Azure only, so GCP stays on the static tier.
    wlc_aws_enabled: bool = False
    wlc_aws_folder: str = ""                        # dynamic-secret folder path
    wlc_aws_secret_name: str = ""                   # the provisioning dynamic secret
    wlc_aws_readonly_secret_name: str = ""          # optional read-only dynamic secret (see the job-boundary split)
    wlc_azure_enabled: bool = False
    wlc_azure_folder: str = ""
    wlc_azure_secret_name: str = ""
    # Regenerate once less than this % of a lease's TTL remains. Clamped to
    # 1..99 by the caller: 0 would mean "never refresh" and 100 would bill an
    # issuance on every check.
    wlc_refresh_margin_pct: int = 50
    # Folder used by the `wlc://` static-secret backend (Secrets page CRUD),
    # matching secrets_aws_prefix / secrets_gcp_prefix.
    secrets_wlc_folder: str = "dashboard"

    # Entitle user-JIT (Phase 4 UI affordances) — surfaces a "Request access"
    # nav link + 403-page deep links pointing at the matching Entitle resource.
    entitle_user_jit_enabled: bool = False
    entitle_request_portal_url: str = ""
    # Shared secret Entitle presents to /api/entitle/rest/* (as Authorization:
    # Bearer, or X-Entitle-Secret). Deliberately NOT a Personal Access Token: a PAT
    # inherits its owning user's permissions, and an endpoint whose whole job is
    # granting permissions must not authenticate with a credential that already has
    # some. Unset → the endpoint is closed (503), never open.
    entitle_rest_secret: str = ""            # encrypted at rest
    entitle_resource_ids_json: str = "{}"

    # POV accessors — a prospect's ephemeral login into ONE POV environment. Its own
    # secret rather than entitle_rest_secret above, deliberately: that one authenticates
    # an integration that grants DASHBOARD permissions, and this one mints logins. Sharing
    # a key would make a leak of either a leak of both, on the instance that does customer
    # work. Unset → the endpoint is closed (503), never open.
    pov_accessor_rest_secret: str = ""       # encrypted at rest
    # How long a minted accessor lasts when nothing else says. Always clamped to the POV's
    # own expiry, so this is a ceiling on a short-lived thing rather than a lifetime.
    pov_accessor_ttl_days: int = 14

    class Config:
        env_file = os.path.join(os.path.dirname(__file__), ".env")
        env_file_encoding = "utf-8"
        case_sensitive = False


# Global settings instance
settings = Settings()
