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
    vmware_enabled: bool = True         # VMs router + /vms page + VM cache warmers + VMware inventory scans
    beyondtrust_enabled: bool = True    # Password Safe secret lookups (btapi_service)
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
    cost_explorer_enabled: bool = False   # /api/costs router + dashboard spend tile (AWS Cost Explorer + Azure Cost Mgmt)
    cost_monthly_budget: float = 0.0      # overall monthly cloud-spend budget for alerts (account currency); 0 = disabled
    cost_budget_aws: float = 0.0          # optional per-cloud monthly budgets; 0 = disabled
    cost_budget_azure: float = 0.0
    cost_budget_gcp: float = 0.0
    gcp_billing_export_table: str = ""    # BigQuery billing-export table for GCP cost (project.dataset.table); blank = GCP cost off
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
    rancher_server_url: str = ""              # Rancher server-url = https://<node public IP> (set by the deploy job)
    rancher_api_token: str = ""               # Rancher API bearer token minted at bootstrap; encrypted at rest
    rancher_bootstrap_password: str = ""      # first-run admin bootstrap password; encrypted at rest
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
    rancher_ui_local_port: int = 443          # local listen port (match Rancher 443 for SNI/cert)
    rancher_ui_web_jump_id: str = ""          # PRA Web Jump id for the central Rancher UI (runtime-set)
    rancher_ui_web_jump_tfstate: str = ""     # terraform state for the Web Jump (for teardown)
    rancher_ui_jumpoint_cloud: str = "gcp"    # which dashboard-managed Jumpoint host brokers the Rancher UI (gcp|aws|azure); its egress IP is auto-whitelisted. gcp = same cloud as the node
    rancher_ui_jumpoint_egress_ip: str = ""   # dashboard-managed Web-Jump Jumpoint host egress IP (runtime-set; auto-added to the node firewall as a /32). Azure host has no public IP → left blank (add manually)
    # Entitle Rancher connector registration. The application slug is
    # tenant/connector-specific — confirm against the entitle_applications catalog
    # before use (default is best-effort). With the PUBLIC source-restricted node,
    # Entitle's cloud can reach it directly (private=False, no agent token); set
    # entitle_rancher_private for tenants who lock the node behind CIDRs Entitle
    # can't traverse.
    entitle_rancher_app_slug: str = "rancher"     # Entitle application catalog slug for the Rancher connector
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

    # PowerShell
    vm_cli_wrapper_path: str = r"C:\Scripts\VM_CLI\VM_DEMO_CLI\vm_cli_api_wrapper.ps1"
    powershell_timeout: int = 7200  # 2 hours max for long operations
    # SSH execution mode (POWERSHELL_EXECUTION_MODE=ssh): container SSHes to the
    # Windows host and runs pwsh there — mirrors the Hybrid Worker pattern for dev.
    ssh_host: str = "host.docker.internal"   # Docker's name for the Windows host
    ssh_user: str = ""                        # Windows username the container SSHes in as
    ssh_key_file: str = "/root/.ssh/dev_dashboard_key"  # path inside the container

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
    bt_api_host: str = ""        # PRA host, used by terraform_pra_service
    bt_client_id: str = ""
    bt_client_secret: str = ""
    bt_jump_group_name: str = ""  # set via setup wizard / settings panel
    bt_jumpoint_name: str = ""    # name of the pre-existing Jumpoint in PRA (required for Terraform path)
    bt_ps_deploy_key_title: str = "Docker Deploy Key"  # Password Safe secret title

    # ── Optional cloud-DATABASE Password Safe onboarding (AWS-only) ───────────
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
    # DB-client container images run on the jump host (override for a mirrored registry).
    clouddb_db_client_image_postgres: str = "postgres:16"
    clouddb_db_client_image_mysql: str = "mysql:8.4"
    clouddb_db_client_image_sqlserver: str = "mcr.microsoft.com/mssql-tools18"
    # AWS IAM user = Password Safe functional account for SSM SendCommand. EC2 mode
    # (default, no keys): username "EC2", role on the PS node/broker authorizes SSM. IAM
    # mode (set username + keys): username "{iam}", password "{AKID}:{secret}".
    clouddb_ps_ssm_iam_username: str = ""
    clouddb_ps_ssm_access_key_id: str = ""
    clouddb_ps_ssm_secret_access_key: str = ""     # encrypted at rest
    clouddb_ps_ssm_account_suffix: str = "local"   # DNS-name 6th field: "local" or a cross-account AssumeRole ARN
    clouddb_ps_ssm_public_key_path: str = ""        # DNS-name 5th field: public key path on the PS node/broker
    # PRA Configuration-API OAuth account for the PRA Vault plugin (blank → reuse bt_client_*).
    pra_config_api_client_id: str = ""
    pra_config_api_client_secret: str = ""          # encrypted at rest

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

    # Image Management (OVA / ISO / AMI building)
    # Environment-specific paths — override in .env (or the settings panel) to
    # match where your ISOs/OVAs live and where Packer should stage builds.
    ova_search_path: str = r"C:\packer\ova"
    iso_source_path: str = r"\\nas\ISO"
    packer_work_root: str = r"C:\packer\work"
    vmx_output_path: str = r"C:\packer\vmx"
    s3_bucket_prefix: str = "vm-import-ova"
    aws_iam_instance_profile: str = ""  # IAM instance profile for Packer surrogate EC2 (needs S3 read access)
    ec2_ssm_instance_profile: str = ""  # IAM instance profile to attach to dashboard-deployed EC2 instances (SSM access)
    ec2_ssh_key_secret: str = ""  # Secrets Manager secret name holding the SSH public key for EC2 deploy
    ovf_tool_path: str = r"C:\Program Files (x86)\VMware\VMware Workstation\OVFTool\ovftool.exe"
    # Guest OS credentials for cloud-prep (retrieved from Password Safe via ps-cli)
    guest_user_secret_title: str = "Guest_User"
    guest_pass_secret_title: str = "Guest_Pass"
    # ISO network share credentials (retrieved from Password Safe via ps-cli)
    iso_share_user_secret_title: str = "ISO_Share_User"
    iso_share_pass_secret_title: str = "ISO_Share_Pass"

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

    # Portainer CE integration — a single connection, configured via
    # Settings → Integrations → Portainer CE (config_service); these env vars
    # are the fallback for compose-file-driven installs.
    portainer_url: str = ""                          # e.g. "http://portainer.local:9000"
    portainer_pat: str = ""                          # API token; Settings stores it encrypted in the DB
    portainer_pat_secret_title: str = "Portainer_PAT"  # legacy fallback: BeyondTrust Password Safe secret title
    portainer_verify_ssl: bool = True                # Set False for self-signed certs
    portainer_agent_image: str = "portainer/agent:latest"
    portainer_agent_port: int = 9001
    ansible_local_image: str = "chrweav/ansible-winrm:latest"

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
    azure_ssh_keypair_secret_name: str = "azureVM-ssh-keypair"  # Unified secret: JSON {public_key, private_key}
    azure_ssh_key_secret_name: str = ""               # Legacy: separate public-key secret (fallback)
    azure_ssh_private_key_secret_name: str = ""       # Legacy: separate private-key secret (fallback)

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
    # backend abstraction. Three backends supported — S3, Azure Blob, GCS —
    # configured independently. The active backend is the one selected via
    # storage_active_backend; others can be configured-but-idle for migration.
    storage_active_backend: str = ""           # "s3" | "azure_blob" | "gcs"
    # Image-registry hub backend — the single backend that holds the canonical
    # VHD/raw artefact for every registered image regardless of build cloud.
    # When unset, falls back to storage_active_backend so single-backend installs
    # Just Work. Used by the Packer export+register flow and the (upcoming)
    # per-target-cloud promote runners.
    storage_hub_backend: str = ""              # "" | "s3" | "azure_blob" | "gcs"
    storage_s3_bucket: str = ""                # e.g. "infra-asset-store"
    storage_s3_region: str = ""                # defaults to aws_region if blank
    storage_s3_prefix: str = "config-mgmt"
    storage_azure_account: str = ""            # storage account name
    storage_azure_container: str = "playbooks"
    storage_azure_prefix: str = "config-mgmt"
    storage_gcs_bucket: str = ""
    storage_gcs_prefix: str = "config-mgmt"
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
    promote_runner_gcp_cpu: str = "2000m"                # qemu-img headroom
    promote_runner_gcp_memory: str = "4Gi"               # ~4 GiB for multi-GB VHDs + tar wrap
    promote_runner_gcp_vpc_connector: str = ""           # optional, for private-network egress
    promote_runner_gcp_service_account: str = ""         # optional: workload-identity SA email for the runner
    promote_runner_gcp_staging_bucket: str = ""          # fallback: storage_gcs_bucket
    promote_runner_gcp_staging_prefix: str = "promote-staging"
    promote_runner_gcp_image_family: str = ""            # optional family label on the resulting custom image

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
    gcp_jumpoint_machine_type: str = "e2-micro"
    gcp_jumpoint_zone: str = ""          # blank → use the deploy zone
    # Network tag(s) automatically attached to every dashboard-deployed user
    # VM. Comma-separated. Used to scope sandbox firewall rules (e.g. the
    # egress-deny rule on the sandbox VM subnet keys off this tag). Set to
    # `dashboard-sandbox-vm` when paired with scripts/sandbox/setup-gcp.sh.
    gcp_default_network_tag: str = ""
    gcp_bt_jump_group_name: str = ""     # BT jump group for GCP Shell Jumps (falls back to bt_jump_group_name)
    gcp_jumpoint_name: str = ""          # Jumpoint name for GCP Shell Jumps (falls back to bt_jumpoint_name)
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

    # Entitle integration — shared API credentials (used by machine-identity
    # JIT, user-JIT, and resource registration below).
    entitle_api_url: str = "https://api.entitle.io/v1"  # canonical Entitle API base — multi-tenant, identical for every tenant. Drives machine-identity JIT and (normalized to scheme+host) the entitleio/entitle provider endpoint.
    entitle_api_token: str = ""                     # bearer token (Key Vault secret in prod)

    # Entitle resource registration — as the dashboard builds Linux VMs and
    # cloud databases it registers each as an Entitle integration (SSH ephemeral
    # accounts / PostgreSQL / MySQL / SQL Server) via the entitleio/entitle
    # Terraform provider. OFF by default = no registration calls.
    entitle_registration_enabled: bool = False
    entitle_api_key: str = ""                       # entitleio/entitle TF provider key (ENTITLE_API_KEY); falls back to entitle_api_token
    entitle_endpoint: str = ""                       # API base; blank → provider default (https://api.entitle.io)
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

    # Entitle user-JIT (Phase 4 UI affordances) — surfaces a "Request access"
    # nav link + 403-page deep links pointing at the matching Entitle resource.
    entitle_user_jit_enabled: bool = False
    entitle_request_portal_url: str = ""
    entitle_resource_ids_json: str = "{}"

    class Config:
        env_file = os.path.join(os.path.dirname(__file__), ".env")
        env_file_encoding = "utf-8"
        case_sensitive = False


# Global settings instance
settings = Settings()
