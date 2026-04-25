"""
Configuration management for Infrastructure Management Dashboard
"""
import json
import os
import re
import secrets
from typing import Any, List
from pydantic import field_validator
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

    # Database
    database_url: str = "sqlite:///./vm_cli.db"

    # Security
    jwt_secret_key: str = secrets.token_hex(32)  # Generate random key if not provided
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 480  # 8 hours

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
    cors_origins: List[str] = ["http://localhost:8000", "http://localhost:3000"]

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

    # Workgroups (from original CLI)
    workgroups: dict = {
        "Hydra": r"P:\VMware\Hydra",
        "Weaverlab": r"P:\VMware\Shield\WeaverLab"
    }

    # AWS / Terraform
    aws_region: str = "us-east-2"
    terraform_executable: str = "terraform"  # assumes terraform is in PATH

    # BeyondTrust PRA integration (btapi binary).
    # Default points to the Linux binary baked into the Docker image;
    # override via .env on hosts where the binary lives elsewhere.
    btapi_executable: str = "/usr/local/bin/btapi"
    pscli_executable: str = "ps-cli"  # installed via beyondtrust-bips-cli; override in .env if needed
    # Password Safe / Secrets Safe credentials (used by ps-cli and REST API fallback)
    pscli_api_url: str = ""      # e.g. "https://ps.company.com"
    pscli_client_id: str = ""
    pscli_client_secret: str = ""
    # These are passed explicitly to btapi subprocesses (not inherited from session env)
    bt_api_host: str = ""
    bt_client_id: str = ""
    bt_client_secret: str = ""
    bt_jump_group_name: str = "us-east-2"
    bt_group_policy_name: str = "BeyondTrust IT User"
    bt_jumpoint_id: int = 7  # "AWS ECS" jumpoint for cloud instances
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
    azure_ansible_aci_image: str = "willhallonline/ansible:latest"  # Ansible image for ACI config mgmt runner
    azure_aci_cpu: float = 1.0
    azure_aci_memory: float = 2.0
    azure_aci_ps_deploy_key_title: str = "ACI_Docker_Deploy_Key"  # BeyondTrust Password Safe secret for ACI deploy key
    azure_aci_storage_account: str = ""           # Storage account name for /jpt persistent volume
    azure_aci_storage_account_rg: str = ""        # RG of the storage account (defaults to ACI RG if empty)
    azure_image_storage_account: str = ""         # Storage account for temp VHD upload during OVA→Azure image import
    azure_aci_file_share: str = "jpt"             # Azure File Share name for /jpt mount
    azure_jumpoint_id: int = 9                    # BeyondTrust Jumpoint ID for Azure VMs (ACI jumpoint, id=9 "ACI")
    # ACR credentials (leave empty to pull from Docker Hub without auth)
    azure_acr_server: str = ""                    # e.g. myregistry.azurecr.io
    azure_acr_username_secret_title: str = ""     # Password Safe secret title for ACR username
    azure_acr_password_secret_title: str = ""     # Password Safe secret title for ACR password
    azure_bt_jump_group_name: str = ""            # BT jump group for Azure Shell Jumps (falls back to bt_jump_group_name)
    azure_bt_group_policy_name: str = ""          # BT group policy for Azure Shell Jumps (falls back to bt_group_policy_name)
    # Azure Key Vault — SSH key retrieval (optional; leave blank to disable)
    azure_key_vault_url: str = ""                     # e.g. "https://my-vault.vault.azure.net/"
    azure_ssh_key_secret_name: str = ""               # Secret name for SSH public key (VM deploy)
    azure_ssh_private_key_secret_name: str = ""               # Secret name for SSH private key (ConfigMgmt)

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
    webauthn_origin: str = "http://localhost:8000"  # must exactly match scheme://host:port browser uses

    # Azure AD OAuth Login (SEPARATE app registration from resource-management service principal)
    # Create a new App Registration; required delegated permissions: openid, profile, email
    # Add redirect URI: http://localhost:8000/api/auth/oauth/azure/callback
    azure_oauth_client_id: str = ""
    azure_oauth_client_secret: str = ""
    azure_oauth_tenant_id: str = ""
    azure_oauth_redirect_uri: str = "http://localhost:8000/api/auth/oauth/azure/callback"
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

    # Ansible Config Management
    ansible_s3_bucket: str = ""               # Required — set in .env (e.g. "infra-config-mgmt")
    ansible_s3_region: str = ""               # S3 bucket region — defaults to aws_region if empty
    ansible_s3_prefix: str = "config-mgmt"
    ansible_ecs_cluster: str = "bt-jumpoint"  # Shares cluster with BT Jumpoint
    ansible_ecs_task_family: str = "ansible-config-mgmt"
    ansible_ecs_image: str = "willhallonline/ansible:latest"
    ansible_ecs_cpu: str = "256"
    ansible_ecs_memory: str = "512"
    ansible_ecs_execution_role_arn: str = ""  # Set if image pull requires it
    ansible_ssh_key_secret: str = "AWS_KEY"        # Password Safe secret title (legacy fallback)
    ansible_ssh_key_sm_name: str = "ec2/ssh-keypair"  # AWS Secrets Manager secret name/ARN (preferred)
    # GCP Cloud Run Jobs ansible runner (mirrors azure_ansible_aci_image / ACI runner)
    gcp_ansible_cloud_run_region: str = ""   # defaults to gcp_region if blank
    gcp_ansible_image: str = "willhallonline/ansible:latest"
    gcp_ansible_vpc_connector: str = ""      # e.g. "projects/proj/locations/region/connectors/name" (optional, for private host access)
    epml_rpm_path: str = r"C:\Scripts\VM_CLI\VM_DEMO_CLI\epml-client.x86_64.rpm"
    epml_deb_path: str = r"C:\Scripts\VM_CLI\VM_DEMO_CLI\epml-client.amd64.deb"
    pathfinder_script_path: str = r"C:\Scripts\VM_CLI\VM_DEMO_CLI\make_pathfinder_user.sh"

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
