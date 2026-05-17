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
    entitle_enabled: bool = True        # Approvals router + approval modal in base.html
    proxmox_enabled: bool = False       # Proxmox VE router + /proxmox page
    vsphere_enabled: bool = False       # vSphere/ESXi router + /vsphere page
    hyperv_enabled: bool = False        # Hyper-V router + /hyperv page (WinRM to Windows host)
    nutanix_enabled: bool = False       # Nutanix AHV router + /nutanix page (Prism Central REST API)
    xcpng_enabled: bool = False         # XCP-ng/XenServer router + /xcpng page (XAPI XML-RPC)

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
    api_version: str = "0.1.0"

    # CORS
    cors_origins: List[str] = ["http://localhost:8001", "http://localhost:3000"]

    # PowerShell
    vm_cli_wrapper_path: str = r"C:\Scripts\VM_CLI\VM_DEMO_CLI\vm_cli_api_wrapper.ps1"
    powershell_timeout: int = 7200  # 2 hours max for long operations
    # SSH execution mode (POWERSHELL_EXECUTION_MODE=ssh): container SSHes to the
    # Windows host and runs pwsh there — mirrors the Hybrid Worker pattern for dev.
    ssh_host: str = "host.docker.internal"   # Docker's name for the Windows host
    ssh_user: str = ""                        # Windows username, e.g. "chrwe"
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
    bt_api_host: str = ""        # PRA host, used by terraform_pra_service
    bt_client_id: str = ""
    bt_client_secret: str = ""
    bt_jump_group_name: str = ""  # set via setup wizard / settings panel
    bt_jumpoint_name: str = ""    # name of the pre-existing Jumpoint in PRA (required for Terraform path)
    bt_ps_deploy_key_title: str = "Docker Deploy Key"  # Password Safe secret title

    # Image Management (OVA / ISO / AMI building)
    ova_search_path: str = r"V:\packer\Weaverlab"
    iso_source_path: str = r"\\10.0.0.74\Public\VMWare\ASUSTOR\WEAVERPC@chrwe\Drive#V\ISO"
    packer_work_root: str = r"C:\Users\chrwe\AppData\Local\Temp\packer-iso-builds"
    vmx_output_path: str = r"V:\packer\VMX"
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
    bt_ecs_cpu: str = "256"    # 0.25 vCPU
    bt_ecs_memory: str = "512"  # MB
    bt_ecs_execution_role_arn: str = ""  # Set to your ecsTaskExecutionRole ARN if required
    # BeyondTrust Jumpoint Docker registry deploy key for AWS ECS launches.
    # Stored encrypted via config_service; transparently resolved through whichever
    # secrets backend the user picked on /secrets. The legacy `bt_ps_deploy_key_title`
    # remains as a Password-Safe-only fallback.
    aws_ecs_docker_deploy_key: str = ""

    # Portainer CE integration
    portainer_url: str = ""                          # e.g. "http://portainer.local:9000"  (Weaverlab)
    portainer_pat_secret_title: str = "Portainer_PAT"  # BeyondTrust Password Safe secret title (Weaverlab)
    portainer_verify_ssl: bool = True                # Set False for self-signed certs (Weaverlab)
    portainer_url_hydra: str = ""                    # Hydra workgroup Portainer CE URL
    portainer_pat_secret_title_hydra: str = "Portainer_PAT_Hydra"  # Password Safe secret title (Hydra)
    portainer_verify_ssl_hydra: bool = True          # Set False for self-signed certs (Hydra)
    portainer_agent_image: str = "portainer/agent:latest"
    portainer_agent_port: int = 9001
    ansible_local_image: str = "willhallonline/ansible:latest"

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
    ansible_aci_image: str = "willhallonline/ansible:latest"  # Ansible image for ACI config mgmt runner
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
    # to a public build under weaverlab-xyz) serves AWS / Azure / GCP targets;
    # the target's own runner orchestration (ECS task / ACI / Cloud Run job)
    # is configured separately per cloud — only AWS-target is wired today.
    promote_runner_image: str = "weaverlab-xyz/dashboard-promote-runner:latest"
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

    ansible_runner: str = "local"              # "local" | "ecs" | "aci" | "gcp"
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
    ansible_ecs_image: str = "willhallonline/ansible:latest"
    ansible_ecs_cpu: str = "256"
    ansible_ecs_memory: str = "512"
    ansible_ecs_subnet_id: str = ""           # Fargate task subnet (VPC private subnet recommended)
    ansible_ecs_security_group_ids: str = ""  # Comma-separated security group IDs (optional)
    ansible_ecs_execution_role_arn: str = ""  # Set if image pull requires it
    ansible_ssh_key_secret: str = "AWS_KEY"        # Password Safe secret title (legacy fallback)
    ansible_ssh_key_sm_name: str = "ec2/ssh-keypair"  # AWS Secrets Manager secret name/ARN (preferred)
    # GCP Cloud Run Jobs ansible runner (mirrors azure_ansible_aci_image / ACI runner)
    gcp_ansible_cloud_run_region: str = ""   # defaults to gcp_region if blank
    gcp_ansible_image: str = "willhallonline/ansible:latest"
    gcp_ansible_vpc_connector: str = ""      # e.g. "projects/proj/locations/region/connectors/name" (optional, for private host access)
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

    # Entitle approval workflows (per-endpoint gate via Depends(require_approval(...)))
    entitle_api_url: str = ""                       # e.g. "https://api.entitle.io/v1"
    entitle_api_token: str = ""                     # bearer token (Key Vault secret in prod)
    entitle_webhook_secret: str = ""                # HMAC-SHA256 shared secret (Key Vault)
    entitle_default_ttl_minutes: int = 15           # how long an approval is valid before auto-expiry
    approval_gate_enabled: bool = False             # master kill-switch — set true to activate gates

    class Config:
        env_file = os.path.join(os.path.dirname(__file__), ".env")
        env_file_encoding = "utf-8"
        case_sensitive = False


# Global settings instance
settings = Settings()
