"""
Setup wizard API.

POST /api/setup/complete   — initial setup, no auth required, errors if already done
PUT  /api/setup/config     — reconfigure, admin JWT required
GET  /api/setup/status     — {complete: bool, configured_keys: list}
GET  /api/setup/config     — current config with secrets redacted (admin JWT if setup done)
"""
import logging

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from pydantic import BaseModel, field_validator

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/setup", tags=["setup"])


# ── Pydantic models ───────────────────────────────────────────────────────────

class AdminSetup(BaseModel):
    username: str
    password: str


class AWSSetup(BaseModel):
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    aws_region: str = "us-east-2"
    ec2_ssh_key_secret: str = ""
    ec2_ssm_instance_profile: str = ""
    # BeyondTrust Jumpoint Docker deploy key (used by ECS Jumpoint task launches).
    # Stored encrypted; resolved through whichever secrets backend the user picks.
    aws_ecs_docker_deploy_key: str = ""
    # Packer template archive (optional)
    packer_aws_s3_bucket: str = ""


class AzureSetup(BaseModel):
    azure_client_id: str = ""
    azure_client_secret: str = ""
    azure_tenant_id: str = ""
    azure_subscription_id: str = ""
    azure_resource_group: str = "dashboard-rg"
    azure_location: str = "centralus"
    # VNet/NSG lookup (defaults to azure_resource_group if blank)
    azure_vnet_resource_group: str = ""
    # Shared Image Gallery for private images (optional)
    azure_shared_image_gallery: str = ""
    azure_gallery_resource_group: str = ""
    # Key Vault for SSH key retrieval (optional)
    azure_key_vault_url: str = ""
    azure_ssh_keypair_secret_name: str = "azureVM-ssh-keypair"
    # Legacy single-purpose secret names (used as fallback if keypair secret unset)
    azure_ssh_key_secret_name: str = ""
    azure_ssh_private_key_secret_name: str = ""
    # Container Registry (optional) — for ACI Ansible / Jumpoint runner pulls.
    # Credentials are stored encrypted via config_service and resolved through
    # whichever secrets backend the user picked on /secrets.
    azure_acr_server: str = ""                                # e.g. myregistry.azurecr.io
    azure_ansible_aci_image: str = "chrweav/ansible-winrm:latest"
    azure_aci_jumpoint_image: str = "beyondtrust/sra-jumpoint:latest"
    azure_acr_username: str = ""
    azure_acr_password: str = ""
    # BeyondTrust Jumpoint Docker deploy key (used by ACI Jumpoint task launches).
    # Stored encrypted; resolved through whichever secrets backend the user picks.
    azure_aci_docker_deploy_key: str = ""
    # Optional: separate app registration for "Sign in with Microsoft"
    azure_oauth_client_id: str = ""
    azure_oauth_client_secret: str = ""
    azure_oauth_tenant_id: str = ""
    # Packer template archive (optional)
    packer_azure_storage_account: str = ""
    packer_azure_archive_container: str = "packer-templates"


class AzureRegionConfig(BaseModel):
    """One region's Azure resource defaults (multi-region support, Follow-on 6 PR3).

    Stored as a value inside the ``azure_region_configs`` JSON map (keyed by
    location) via config_service — see services/region_config.py. Every field is
    optional; a blank field falls back to the matching flat key
    (``resource_group`` → ``azure_resource_group``, ``desktops_subnet_id`` →
    ``azure_desktops_subnet_id``, ``gallery_name`` → ``azure_shared_image_gallery``,
    …). The flat keys remain the source of truth for the default region
    (``azure_location``), so single-region installs are unchanged.
    """
    resource_group: str = ""
    vnet_resource_group: str = ""
    default_subnet_id: str = ""
    desktops_subnet_id: str = ""
    db_subnet_id: str = ""
    db_mysql_subnet_id: str = ""
    db_private_dns_zone_id: str = ""
    gallery_name: str = ""
    gallery_resource_group: str = ""
    default_vm_size: str = ""


class AwsRegionConfig(BaseModel):
    """One region's AWS resource defaults (multi-region support). Stored inside the
    ``aws_region_configs`` JSON map; blank fields fall back to the flat keys
    (``default_subnet_id`` → ``aws_default_subnet_id``, ``ssh_key_secret`` →
    ``ec2_ssh_key_secret``, …). Fields mirror region_config._SPECS['aws']."""
    default_subnet_id: str = ""
    default_security_group_id: str = ""
    ssh_key_secret: str = ""
    ssm_instance_profile: str = ""
    db_subnet_group_name: str = ""
    vpc_id: str = ""
    vpc_cidr: str = ""
    private_route_table_id: str = ""
    db_security_group_id: str = ""
    db_parameter_group_name: str = ""
    db_mysql_parameter_group_name: str = ""
    nat_security_group_id: str = ""
    ecs_subnet_id: str = ""
    ecs_security_group_ids: str = ""
    ecs_cluster: str = ""
    jumpoint_subnet_id: str = ""
    jumpoint_security_group_id: str = ""


class GcpRegionConfig(BaseModel):
    """One region's GCP resource defaults (multi-region support). Stored inside the
    ``gcp_region_configs`` JSON map; blank fields fall back to the flat keys
    (``subnetwork`` → ``gcp_subnetwork``, ``ssh_key_secret`` →
    ``gcp_ssh_key_secret_name``, …). Fields mirror region_config._SPECS['gcp']."""
    zone: str = ""
    network: str = ""
    subnetwork: str = ""
    jumpoint_subnetwork: str = ""
    db_network: str = ""
    ssh_key_secret: str = ""
    default_network_tag: str = ""
    ecs_subnetwork: str = ""
    router_name: str = ""
    nat_name: str = ""
    k8s_subnetwork: str = ""
    k8s_pods_range: str = ""
    k8s_services_range: str = ""
    k8s_node_tag: str = ""


# Cloud → per-region-config model. Drives the /import parser and the /regions/{cloud}
# editor endpoints. Only these clouds have per-region resource sets (OCI has none).
_REGION_CONFIG_MODELS: dict[str, type[BaseModel]] = {
    "azure": AzureRegionConfig,
    "aws":   AwsRegionConfig,
    "gcp":   GcpRegionConfig,
}


def _region_config_cloud(key: str) -> "str | None":
    """Return the cloud for a ``<cloud>_region.<region>.<field>`` import key, else
    None. Matches only the dotted region-config namespace — flat keys like
    ``aws_region`` or ``azure_region_configs`` do not match (no trailing dot)."""
    for c in _REGION_CONFIG_MODELS:
        if key.startswith(f"{c}_region."):
            return c
    return None


class GCPSetup(BaseModel):
    gcp_project_id: str = ""
    gcp_region: str = "us-central1"
    gcp_zone: str = "us-central1-a"
    gcp_service_account_json: str = ""   # Full SA JSON key — stored encrypted
    gcp_network: str = "default"
    gcp_subnetwork: str = ""
    # Cloud-NAT subnet the BeyondTrust Jumpoint COS instance lands in — it needs
    # egress to reach PRA, unlike the user-VM subnet (gcp_subnetwork) above. Falls
    # back to gcp_subnetwork when blank. Emitted by setup-gcp.sh as
    # gcp_jumpoint_subnetwork.
    gcp_jumpoint_subnetwork: str = ""
    gcp_ssh_key_secret_name: str = ""
    gcp_ssh_username: str = "gcp-user"
    # BeyondTrust Jumpoint Docker deploy key — the deploy key the GCE COS Jumpoint
    # container registers with. Stored encrypted; resolved through whichever
    # secrets backend the user picks. Consumed by jumpoint_host_service when
    # provisioning the GCP cloud-database tunnel jumpoint.
    gcp_cloud_run_docker_deploy_key: str = ""
    # Packer template archive (optional)
    packer_gcs_bucket: str = ""


class OCISetup(BaseModel):
    oci_tenancy_ocid: str = ""
    oci_user_ocid: str = ""
    oci_fingerprint: str = ""
    oci_private_key: str = ""             # API signing private key PEM — stored encrypted
    oci_private_key_passphrase: str = ""  # optional passphrase — stored encrypted
    oci_region: str = "us-ashburn-1"
    oci_compartment_ocid: str = ""        # blank → tenancy root
    oci_vcn_ocid: str = ""
    oci_default_subnet_ocid: str = ""
    oci_ssh_key_secret: str = ""          # OCI Vault secret (OCID/name) holding the SSH keypair
    oci_vault_ocid: str = ""


class FeaturesSetup(BaseModel):
    vmware_enabled: bool = False
    beyondtrust_enabled: bool = False
    portainer_enabled: bool = False
    ansible_enabled: bool = False
    entitle_enabled: bool = False
    proxmox_enabled: bool = False
    vsphere_enabled: bool = False
    hyperv_enabled: bool = False
    nutanix_enabled: bool = False
    xcpng_enabled: bool = False
    cost_explorer_enabled: bool = False
    admission_control_enabled: bool = False
    cloud_database_enabled: bool = False
    k8s_management_enabled: bool = False


class SetupPayload(BaseModel):
    admin: AdminSetup
    aws: AWSSetup
    azure: AzureSetup
    gcp: GCPSetup = GCPSetup()
    oci: OCISetup = OCISetup()
    features: FeaturesSetup


# ── Internal helpers ──────────────────────────────────────────────────────────

def _require_admin(request: Request) -> None:
    """Verify the request carries a valid admin JWT. Raises 401/403 on failure."""
    from jose import JWTError, jwt
    from ..config import settings
    from ..database import SessionLocal, User

    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Authentication required")
    token = auth[7:]
    try:
        payload = jwt.decode(
            token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm]
        )
        username: str = payload.get("sub", "")
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    db = SessionLocal()
    try:
        user = (
            db.query(User)
            .filter(User.username == username, User.is_active == True)
            .first()
        )
        if not user or not user.is_admin:
            raise HTTPException(status_code=403, detail="Admin access required")
    finally:
        db.close()


def _upsert_admin(username: str, password: str) -> None:
    from ..database import SessionLocal, User, get_password_hash
    db = SessionLocal()
    try:
        existing = db.query(User).filter(User.username == username).first()
        if existing:
            existing.hashed_password = get_password_hash(password)
            existing.is_admin = True
            existing.is_active = True
        else:
            user = User(
                username=username,
                hashed_password=get_password_hash(password),
                full_name="Administrator",
                is_admin=True,
                is_active=True,
            )
            user.workgroups_list = []
            db.add(user)
        db.commit()
    finally:
        db.close()


_WIZARD_SECRET_FIELDS = frozenset({
    "aws_secret_access_key",
    "azure_client_secret",
    "azure_oauth_client_secret",
    "gcp_service_account_json",
    "oci_private_key",
    "oci_private_key_passphrase",
})


def _apply_config(payload: SetupPayload) -> None:
    """Write all wizard values to the config store and invalidate service caches."""
    from ..services import config_service

    pairs: dict = {}

    for field, value in payload.aws.model_dump().items():
        # Skip empty secret fields on reconfigure so existing DB values aren't blanked.
        if field in _WIZARD_SECRET_FIELDS and not value:
            continue
        pairs[field] = value

    # Azure
    for field, value in payload.azure.model_dump().items():
        if field in _WIZARD_SECRET_FIELDS and not value:
            continue
        pairs[field] = value

    # GCP
    for field, value in payload.gcp.model_dump().items():
        if field in _WIZARD_SECRET_FIELDS and not value:
            continue
        pairs[field] = value

    # OCI
    for field, value in payload.oci.model_dump().items():
        if field in _WIZARD_SECRET_FIELDS and not value:
            continue
        pairs[field] = value

    # Feature flags — always store (explicit true/false is meaningful)
    pairs.update({
        "vmware_enabled":       "1" if payload.features.vmware_enabled else "0",
        "beyondtrust_enabled":  "1" if payload.features.beyondtrust_enabled else "0",
        "portainer_enabled":    "1" if payload.features.portainer_enabled else "0",
        "ansible_enabled":      "1" if payload.features.ansible_enabled else "0",
        "entitle_enabled":      "1" if payload.features.entitle_enabled else "0",
        "proxmox_enabled":      "1" if payload.features.proxmox_enabled else "0",
        "vsphere_enabled":      "1" if payload.features.vsphere_enabled else "0",
        "hyperv_enabled":       "1" if payload.features.hyperv_enabled else "0",
        "nutanix_enabled":      "1" if payload.features.nutanix_enabled else "0",
        "xcpng_enabled":        "1" if payload.features.xcpng_enabled else "0",
        "cost_explorer_enabled":    "1" if payload.features.cost_explorer_enabled else "0",
        "admission_control_enabled": "1" if payload.features.admission_control_enabled else "0",
        "cloud_database_enabled":   "1" if payload.features.cloud_database_enabled else "0",
        "k8s_management_enabled":   "1" if payload.features.k8s_management_enabled else "0",
    })

    config_service.set_many(pairs)
    # Belt-and-suspenders: force the next config_service.get() to reload from DB.
    config_service.invalidate()

    # Invalidate Azure credential cache so the service picks up new values.
    try:
        from ..services import azure_service
        azure_service.invalidate_credentials()
    except Exception:
        pass


# Data caches whose payload is derived from cloud/config values written via the
# setup wizard. Invalidated after every wizard save so the next page load
# rebuilds them against the new config instead of serving stale pre-save data.
_CONFIG_DEPENDENT_CACHES = (
    "azure_images", "azure_network_opts", "azure_vms", "azure_marketplace",
    "aws_amis", "aws_network_opts", "aws_instances", "aws_ssh_key_secrets",
    "cfgmgmt_instances", "cfgmgmt_s3status",
    "portainer_endpoints", "portainer_containers", "portainer_stacks",
)


async def _invalidate_data_caches() -> None:
    from ..services import cache_service
    for name in _CONFIG_DEPENDENT_CACHES:
        await cache_service.invalidate(cache_service.key_global(name))


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/status")
def setup_status():
    from ..services import config_service
    complete = config_service.is_setup_complete()
    configured = list(config_service.get_all_public().keys()) if complete else []
    return {"complete": complete, "configured_keys": configured}


@router.get("/config")
def get_config(request: Request):
    """Return current config with secrets redacted. Admin JWT required once setup is done."""
    from ..services import config_service
    if config_service.is_setup_complete():
        _require_admin(request)
    return config_service.get_all_public()


@router.post("/complete", status_code=201)
def complete_setup(payload: SetupPayload, request: Request, background_tasks: BackgroundTasks):
    """
    Initial wizard submission — no auth required.
    Returns 409 if setup has already been completed (use PUT /api/setup/config instead).
    """
    from ..services import config_service
    if config_service.is_setup_complete():
        raise HTTPException(
            status_code=409,
            detail="Setup is already complete. Use PUT /api/setup/config to reconfigure.",
        )
    _upsert_admin(payload.admin.username, payload.admin.password)
    _apply_config(payload)
    config_service.mark_setup_complete()
    background_tasks.add_task(_invalidate_data_caches)
    logger.info("Setup wizard completed by first-run submission.")
    return {"ok": True}


@router.put("/config")
def reconfigure(payload: SetupPayload, request: Request, background_tasks: BackgroundTasks):
    """
    Reconfigure credentials and feature flags. Admin JWT required.
    Admin account password is updated only when payload.admin.password is non-empty.
    """
    _require_admin(request)
    if payload.admin.password:
        _upsert_admin(payload.admin.username, payload.admin.password)
    _apply_config(payload)
    background_tasks.add_task(_invalidate_data_caches)
    logger.info("Configuration updated via reconfigure endpoint.")
    return {"ok": True}


class HeadlessImport(BaseModel):
    """Admin credentials + a flat key→value config map for headless onboarding.

    Used by scripts/sandbox/onboard-sandbox.* to push the full sandbox output
    (including non-wizard keys) without going through the /setup UI.
    """
    admin_username: str = ""
    admin_password: str = ""
    config: dict = {}


@router.post("/import")
def import_config(payload: HeadlessImport, request: Request, background_tasks: BackgroundTasks):
    """
    Headless setup/import for the consolidated onboarding script.

    Unlike POST /complete (which filters the payload through the typed wizard
    models), this persists the raw `config` map verbatim via
    config_service.set_many — so it carries the FULL sandbox output
    (aws_default_subnet_id, bt_ecs_*, storage_*, promote_runner_*, …) that the
    wizard models don't declare.

    - First run (setup NOT complete): admin_username/admin_password are required;
      creates the admin, writes config, marks setup complete. No auth — same
      trust model as POST /complete (a fresh stack stood up by the same operator).
    - Already complete: admin JWT required (Authorization: Bearer). Merges the
      config map only (admin + setup-complete flag untouched), so you can re-run
      to add another cloud.
    """
    from ..services import config_service

    already = config_service.is_setup_complete()
    if already:
        _require_admin(request)
    elif not payload.admin_username or not payload.admin_password:
        raise HTTPException(
            status_code=400,
            detail="admin_username and admin_password are required for first-run import.",
        )

    if not isinstance(payload.config, dict) or not payload.config:
        raise HTTPException(status_code=400, detail="config must be a non-empty object.")

    # The config store is text. Coerce booleans (e.g. feature flags) to "1"/"0";
    # stringify numbers; drop nulls. Strings (incl. embedded JSON like
    # gcp_service_account_json) pass through unchanged.
    #
    # Multi-region: keys shaped ``<cloud>_region.<region>.<field>`` (cloud in
    # aws/gcp/azure) are NOT stored as flat keys — they're collected per cloud and
    # merged into that cloud's ``<cloud>_region_configs`` JSON map below, so running
    # the sandbox in two regions populates BOTH entries without clobbering.
    pairs: dict = {}
    region_updates: dict[str, dict[str, dict]] = {}  # cloud -> region -> field -> val
    for key, value in payload.config.items():
        if value is None:
            continue
        if isinstance(value, bool):
            sval = "1" if value else "0"
        else:
            sval = value if isinstance(value, str) else str(value)

        cloud = _region_config_cloud(key)
        if cloud:
            parts = key.split(".", 2)  # ["<cloud>_region", "<region>", "<field>"]
            model = _REGION_CONFIG_MODELS[cloud]
            if len(parts) == 3 and parts[1] and parts[2] in model.model_fields:
                region_updates.setdefault(cloud, {}).setdefault(parts[1], {})[parts[2]] = sval
                continue
            # Malformed <cloud>_region.* key (bad field / shape) — drop it rather
            # than persist a stray flat key.
            logger.warning("Import: ignoring unrecognized region key %r", key)
            continue
        pairs[key] = sval

    if not already:
        _upsert_admin(payload.admin_username, payload.admin_password)

    if pairs:
        config_service.set_many(pairs)
    if region_updates:
        from ..services.region_config import merge_region_fields
        for cloud, updates in region_updates.items():
            merge_region_fields(cloud, updates)
    config_service.invalidate()
    try:
        from ..services import azure_service
        azure_service.invalidate_credentials()
    except Exception:
        pass

    if not already:
        config_service.mark_setup_complete()

    # Flat, deduped list of region names merged across every cloud (keeps the
    # response shape a single-cloud import always had).
    merged_regions = sorted({r for regions in region_updates.values() for r in regions})

    background_tasks.add_task(_invalidate_data_caches)
    logger.info(
        "Headless import: %d config keys written [%s]%s%s.",
        len(pairs),
        ", ".join(sorted(pairs.keys())),
        f"; region sets merged: {', '.join(merged_regions)}" if merged_regions else "",
        "" if already else "; admin created + setup marked complete",
    )
    return {
        "ok": True,
        "keys_written": len(pairs),
        "regions_merged": merged_regions,
    }


# ── Per-feature configuration ─────────────────────────────────────────────────
#
# Each feature has its own typed model so the UI can GET current values and
# PATCH only the keys relevant to that integration.  All require admin JWT.

class VMwareFeatureConfig(BaseModel):
    enabled: bool = False

class BeyondTrustFeatureConfig(BaseModel):
    enabled: bool = False
    pscli_api_url: str = ""
    pscli_client_id: str = ""
    pscli_client_secret: str = ""   # encrypted at rest
    pscli_api_account_name: str = ""  # run-as user, required by the passwordsafe TF provider (VM registration)
    # Optional Password Safe VM resource registration (per-deploy opt-in).
    passwordsafe_registration_enabled: bool = False
    passwordsafe_workgroup: str = ""                      # workgroup name or id the managed system lands in
    passwordsafe_vm_functional_account_aws: str = ""      # functional account (name or id) per cloud
    passwordsafe_vm_functional_account_azure: str = ""
    passwordsafe_vm_functional_account_gcp: str = ""
    passwordsafe_managed_account_name: str = "adminuser"  # the baked-in account onboarded as managed
    passwordsafe_ssh_key_enforcement_mode: str = "2"      # 0=none, 1=auto, 2=strict (SSH method only)
    # AWS Systems Manager (cloud-native) onboarding — see config.py for details.
    passwordsafe_aws_registration_method: str = "ssm"     # "ssm" (AWS Systems Manager plugin) | "ssh"
    passwordsafe_ssm_account_suffix: str = "local"        # managed-account name suffix; AssumeRole ARN for EC2 mode
    passwordsafe_ssm_change_password_on_register: bool = False  # best-effort initial key mint via Change Password
    # Azure VM SSH Rotation (cloud-native) onboarding — Azure counterpart of the SSM plugin.
    passwordsafe_azure_registration_method: str = "azurevm"  # "azurevm" (Azure VM SSH Rotation plugin) | "ssh"
    passwordsafe_azure_change_password_on_register: bool = True  # mint first key on onboard (adminuser has none baked in)
    # GCP VM SSH Rotation (cloud-native) onboarding — GCP counterpart (writes the key into GCE ssh-keys metadata).
    passwordsafe_gcp_registration_method: str = "gcpvm"   # "gcpvm" (GCP VM SSH Rotation plugin) | "ssh"
    passwordsafe_gcp_change_password_on_register: bool = True  # mint first key on onboard (adminuser has none baked in)
    # PRA API credentials (used by the SRA Terraform provider for Shell Jump provisioning)
    bt_api_host: str = ""
    bt_client_id: str = ""
    bt_client_secret: str = ""      # encrypted at rest
    # Shell Jump provisioning — Jump Group and Jumpoint must pre-exist in PRA
    bt_jump_group_name: str = ""
    bt_jumpoint_name: str = ""
    # Optional cloud-DATABASE Password Safe onboarding (AWS-only) — see config.py.
    # The two custom plugins + jump-host RSA prep are one-time MANUAL setup.
    clouddb_ps_onboarding_enabled: bool = False
    clouddb_ps_platform_postgres: str = "psql SSM Custom Plugin"
    clouddb_ps_platform_mysql: str = "mysql SSM Custom Plugin"
    clouddb_ps_platform_sqlserver: str = "mssql SSM Custom Plugin"
    clouddb_ps_pravault_platform: str = "PRA Vault Username Password"
    clouddb_ps_workgroup: str = ""                 # blank → falls back to passwordsafe_workgroup
    clouddb_db_client_image_postgres: str = "postgres:16"
    clouddb_db_client_image_mysql: str = "mysql:8.4"
    clouddb_db_client_image_sqlserver: str = "mcr.microsoft.com/mssql-tools18"
    clouddb_ps_ssm_iam_username: str = ""           # blank → EC2 role mode
    clouddb_ps_ssm_access_key_id: str = ""
    clouddb_ps_ssm_secret_access_key: str = ""      # encrypted at rest
    clouddb_ps_ssm_account_suffix: str = "local"    # "local" or a cross-account AssumeRole ARN
    clouddb_ps_ssm_public_key_path: str = ""         # public key path on the PS node/broker
    pra_config_api_client_id: str = ""              # blank → reuse bt_client_id
    pra_config_api_client_secret: str = ""          # encrypted at rest; blank → reuse bt_client_secret
    # Azure-specific overrides (leave blank to fall back to the AWS values above)
    azure_bt_jump_group_name: str = ""
    azure_jumpoint_name: str = ""
    # GCP-specific overrides (leave blank to fall back to the AWS values above)
    gcp_bt_jump_group_name: str = ""
    gcp_jumpoint_name: str = ""
    # EPM for Linux (EPM-L) — Pathfinder public API gateway at api.beyondtrust.io
    epml_site_id: str = ""          # Pathfinder site UUID; PATs are bound to the site active at creation
    epml_pat: str = ""              # encrypted at rest; Bearer token for EPML API

class PortainerFeatureConfig(BaseModel):
    enabled: bool = False
    portainer_url: str = ""
    portainer_pat: str = ""             # encrypted at rest; token or vault ref (bt_safe:// etc.)
    portainer_verify_ssl: bool = True

class AnsibleFeatureConfig(BaseModel):
    enabled: bool = False
    # Runner selection — global default plus per-target-cloud overrides.
    ansible_runner: str = "local"            # "local" | "ecs" | "aci" | "gcp" (global fallback)
    ansible_runner_aws: str = ""             # "" | "local" | "ecs"  (AWS targets)
    ansible_runner_azure: str = ""           # "" | "local" | "aci"  (Azure targets)
    ansible_runner_gcp: str = ""             # "" | "local" | "gcp"  (GCP targets)
    # Per-cloud SSH user (each cloud ships with its own stock username
    # convention — see config.py for context).
    ansible_aws_user: str = "ec2-user"
    ansible_azure_user: str = "azureuser"
    ansible_gcp_user: str = "gcp-user"
    ansible_default_user: str = "ec2-user"   # fallback for unknown cloud tags
    # NOTE: storage backend config moved to its own /storage page; the
    # Ansible feature requires a configured + active storage backend before
    # `enabled` can be flipped on (UI-enforced; see /api/storage/backends).
    # AWS ECS runner
    ansible_ecs_cluster: str = "bt-jumpoint"
    ansible_ecs_task_family: str = "ansible-config-mgmt"
    ansible_ecs_subnet_id: str = ""
    ansible_ecs_security_group_ids: str = ""
    ansible_ecs_execution_role_arn: str = ""
    ansible_ecs_cpu: str = "256"
    ansible_ecs_memory: str = "512"
    # Azure ACI runner
    ansible_aci_image: str = "chrweav/ansible-winrm:latest"
    ansible_aci_subnet_id: str = ""
    ansible_aci_ssh_key_secret_name: str = ""
    ansible_aci_acr_server: str = ""
    ansible_aci_acr_username: str = ""
    ansible_aci_acr_password: str = ""      # encrypted at rest
    # GCP Cloud Run runner
    gcp_ansible_cloud_run_region: str = ""
    gcp_ansible_image: str = "chrweav/ansible-winrm:latest"
    gcp_ansible_vpc_connector: str = ""
    # Direct VPC egress (preferred over the connector — no standing infra; the
    # Cloud Run job's NIC lands straight in the subnet). Set BOTH; wins over
    # gcp_ansible_vpc_connector. Egress stays private-ranges-only.
    gcp_run_network: str = ""
    gcp_run_subnetwork: str = ""
    gcp_ansible_runner_service_account: str = ""   # SA the Cloud Run job runs as (required for GCP ephemeral secrets)
    # Ephemeral cloud secrets — managed-account checkout on ECS / Cloud Run. OFF by
    # default; copies a PAM credential into the cloud store (RBAC-locked) for the run.
    ansible_cloud_ephemeral_secrets_enabled: bool = False
    ansible_ephemeral_secret_ttl_min: int = 30     # GC safety-net age
    ansible_ephemeral_kms_key_id: str = ""         # AWS CMK for the ephemeral secret (true read-restriction)
    ansible_managed_request_duration_min: int = 60  # PS request duration (must outlast the run)
    # Kubernetes (kubectl/helm) runner — reuses the ECS/ACI/Cloud Run network
    # settings above. "local" runs in-process; cloud modes run cluster-API ops
    # as a one-shot stock kubectl+helm task with clean egress.
    k8s_runner: str = "local"                # "local" | "ecs" | "aci" | "gcp" (global fallback)
    k8s_runner_aws: str = ""                 # "" | "local" | "ecs"  (EKS clusters)
    k8s_runner_azure: str = ""               # "" | "local" | "aci"  (AKS clusters)
    k8s_runner_gcp: str = ""                 # "" | "local" | "gcp"  (GKE clusters)
    k8s_runner_image: str = "dtzar/helm-kubectl:latest"  # shared default for all clouds
    k8s_runner_image_aws: str = ""    # per-cloud override; blank → k8s_runner_image
    k8s_runner_image_azure: str = ""  # e.g. an ACR mirror, to avoid Docker Hub pulls
    k8s_runner_image_gcp: str = ""
    # Ansible image for Kubernetes-cluster / cloud-database targets (localhost plays;
    # carries kubernetes.core + community.postgresql/mysql/general + client libs).
    # Used for ALL cloud runners on k8s/DB targets — never the winrm image.
    ansible_cloud_image: str = "chrweav/ansible-cloud:latest"
    # Image-promote runner — always runs as a one-shot task in the target cloud
    # (ECS / ACI / Cloud Run); no per-cloud selector. Blank → the public Docker
    # Hub image; set a full registry path to use a private mirror (e.g. an ACR
    # copy that dodges Docker Hub pull limits). Read by promote_runner_service.
    promote_runner_image: str = ""

class EntitleFeatureConfig(BaseModel):
    enabled: bool = False
    entitle_api_url: str = "https://api.entitle.io/v1"
    entitle_api_token: str = ""         # encrypted at rest
    # Resource registration — register built VMs/DBs as Entitle integrations.
    entitle_registration_enabled: bool = False
    entitle_api_key: str = ""           # entitleio/entitle TF provider key; encrypted at rest
    entitle_owner_id: str = ""          # REQUIRED for registration: Entitle user UUID owning created integrations
    entitle_workflow_id: str = ""       # REQUIRED for registration: default approval workflow UUID
    entitle_endpoint: str = ""          # optional provider endpoint override; blank → derived from the API URL host
    entitle_agent_token_name: str = ""  # read-only display only; auto-set by ensure_agent_token (not edited here)
    # Agent KMS backend (where the agent vaults integration creds). Per-cloud override;
    # blank → entitle_agent_kms_type. AKS needs azure_secret_manager (the azure_aks
    # module provisions the workload-identity MI + Key Vault it requires); EKS/GKE keep
    # kubernetes_secret_manager.
    entitle_agent_kms_type: str = "kubernetes_secret_manager"
    entitle_agent_kms_type_aws: str = ""
    entitle_agent_kms_type_azure: str = "azure_secret_manager"
    entitle_agent_kms_type_gcp: str = ""
    entitle_agent_service_account: str = "entitle-agent-sa"  # agent pod SA; must match the AKS federated subject
    entitle_ssh_sudo_user: str = ""
    entitle_ssh_private_key_ref: str = ""
    # User-JIT (Phase 4) — operator surfaces these via the Settings panel.
    entitle_user_jit_enabled: bool = False
    entitle_request_portal_url: str = ""
    entitle_resource_ids_json: str = "{}"
    # Rancher connector — register the central Rancher as an Entitle integration.
    # Slug + connection field names are tenant/connector-specific; confirm against
    # the entitle_applications catalog (defaults best-effort).
    entitle_rancher_app_slug: str = "rancher"
    entitle_rancher_url_key: str = "url"
    entitle_rancher_token_key: str = "api_token"

class ProxmoxFeatureConfig(BaseModel):
    enabled: bool = False
    proxmox_host: str = ""
    proxmox_port: int = 8006
    proxmox_user: str = "root@pam"
    proxmox_token_id: str = ""
    proxmox_token_secret: str = ""      # encrypted at rest
    proxmox_password: str = ""          # encrypted at rest
    proxmox_verify_ssl: bool = False

class VSphereFeatureConfig(BaseModel):
    enabled: bool = False
    vsphere_host: str = ""
    vsphere_port: int = 443
    vsphere_user: str = "administrator@vsphere.local"
    vsphere_password: str = ""          # encrypted at rest
    vsphere_verify_ssl: bool = False
    vsphere_datacenter: str = ""

class HyperVFeatureConfig(BaseModel):
    enabled: bool = False
    hyperv_host: str = ""
    hyperv_port: int = 5985
    hyperv_username: str = ""
    hyperv_password: str = ""           # encrypted at rest
    hyperv_use_ssl: bool = False
    hyperv_verify_ssl: bool = False
    hyperv_transport: str = "ntlm"

class NutanixFeatureConfig(BaseModel):
    enabled: bool = False
    nutanix_host: str = ""
    nutanix_port: int = 9440
    nutanix_username: str = "admin"
    nutanix_password: str = ""          # encrypted at rest
    nutanix_verify_ssl: bool = False

class XcpNgFeatureConfig(BaseModel):
    enabled: bool = False
    xcpng_host: str = ""
    xcpng_username: str = "root"
    xcpng_password: str = ""            # encrypted at rest
    xcpng_verify_ssl: bool = False


class CloudDatabaseFeatureConfig(BaseModel):
    """Config panel for the Cloud Databases feature. Graduated from preview to GA
    once every engine (PostgreSQL / MySQL / SQL Server) was validated end-to-end on
    all three clouds. The toggle owns `cloud_database_enabled` via its own `enabled`
    field (feature name → key through _feature_to_cfg_key, like cost_explorer /
    admission_control); the rest hold the per-cloud Managed-DB network IDs the
    sandbox emits (normally pushed via /api/setup/import)."""
    enabled: bool = False
    # AWS RDS
    aws_db_subnet_group_name: str = ""
    aws_db_parameter_group_name: str = ""            # postgres: rds.force_ssl=0 group
    aws_db_mysql_parameter_group_name: str = ""      # mysql: require_secure_transport=0 group
    aws_db_security_group_id: str = ""
    # Azure Flexible Server
    azure_db_subnet_id: str = ""                      # postgres delegated subnet
    azure_db_private_dns_zone_id: str = ""            # postgres DNS zone
    azure_db_mysql_subnet_id: str = ""                # mysql delegated subnet (own)
    azure_db_mysql_private_dns_zone_id: str = ""      # mysql DNS zone (own)
    azure_db_sqlserver_subnet_id: str = ""            # sqlserver private-endpoint subnet (plain, not delegated)
    azure_db_sqlserver_private_dns_zone_id: str = ""  # sqlserver privatelink.database.windows.net zone
    # GCP Cloud SQL
    gcp_db_network: str = ""


class K8sManagementFeatureConfig(BaseModel):
    """Config panel for the Kubernetes Management feature. Graduated from preview to
    GA once provision (EKS/AKS/GKE), register, PRA broker/tunnel, and Entitle
    real-identity JIT were validated end-to-end. The toggle owns
    `k8s_management_enabled` via its own `enabled` field (feature name → key through
    _feature_to_cfg_key, like cost_explorer / cloud_database); the rest hold the AWS
    EKS cluster-provisioning defaults the sandbox emits (the two private k8s subnet
    ids) plus optional version / node-size defaults and the Rancher / Entra-RBAC
    knobs. Normally pushed via /api/setup/import. Registering an existing cluster
    needs none of this — it's only for §1.1a provisioning."""
    enabled: bool = False
    # AWS EKS provisioning (§1.1a) — the two private k8s subnets (2 AZs) the
    # cluster + node group land in.
    aws_k8s_subnet_a_id: str = ""
    aws_k8s_subnet_b_id: str = ""
    # Optional defaults (blank → the terraform/k8s_cluster/aws_eks module defaults).
    aws_eks_k8s_version: str = ""
    aws_eks_node_instance_type: str = ""
    # Rancher management plane (import model). The central Rancher server runs as
    # a single privileged container on a PUBLIC GCE COS VM (gcp_rancher_* below);
    # runtime ids (rancher_server_url / rancher_api_token) are set by the deploy
    # job, not entered here. Only the bootstrap password + node knobs are input.
    rancher_bootstrap_password: str = ""      # first-run admin bootstrap; encrypted at rest
    rancher_admin_password: str = ""          # admin UI password for auto first-run; blank = auto-generate a distinct one (Rancher forbids reusing the bootstrap password), surfaced in the Containers panel + job result; ≥12 chars; encrypted at rest
    rancher_auto_first_run: bool = True       # auto-complete Rancher's first-run wizard on a fresh deploy (change admin password + accept EULA/telemetry); off = leave the manual Welcome wizard
    rancher_verify_tls: bool = False          # verify the node's TLS cert on API calls (False = self-signed)
    rancher_allowed_source_cidrs: str = ""    # OPTIONAL/ADDITIVE CSV CIDRs for the node's public-IP GCE firewall (tcp 80/443). Provisioned clusters' egress IPs + the dashboard-managed Web-Jump Jumpoint IP are auto-added; use this only for extra operator IPs + pre-existing operator Jumpoints. Fully empty (manual + auto) = NOT opened unless gcp_rancher_allow_open
    rancher_dashboard_egress_cidr: str = ""   # the dashboard's own egress IP/CIDR (auto-detected; a manually-set pool CIDR that contains the detected IP is kept)
    rancher_ready_timeout_s: int = 360        # deploy readiness poll budget
    rancher_api_transport: str = "direct"     # direct | runner (in-cloud Cloud Run curl — for corp networks whose TLS inspection blocks the node's self-signed cert)
    rancher_runner_source_cidr: str = ""      # VPC connector /28 auto-added to the node firewall when transport=runner
    # GCE COS Rancher node deploy knobs (see config.py gcp_rancher_*).
    gcp_rancher_image: str = "rancher/rancher:latest"
    gcp_rancher_machine_type: str = "e2-medium"   # ≥4 GB required
    gcp_rancher_zone: str = ""                # blank → gcp_zone
    gcp_rancher_name: str = "rancher-server"
    gcp_rancher_boot_disk_gb: int = 30
    gcp_rancher_network_tag: str = "rancher"
    gcp_rancher_allow_open: bool = False      # open 0.0.0.0/0 when allowed_source_cidrs is empty
    # Rancher UI PRA web-broker (opt-in zero-trust access without opening CIDRs).
    rancher_ui_web_jump_enabled: bool = False
    rancher_ui_verify_certificate: bool = False
    rancher_ui_jump_group: str = ""
    rancher_ui_jumpoint_name: str = ""
    rancher_ui_local_port: int = 443
    rancher_ui_jumpoint_cloud: str = "gcp"    # which dashboard-managed Jumpoint host brokers the Rancher UI (gcp|aws|azure); its egress IP is auto-whitelisted
    rancher_ui_vault_account_group_id: str = ""  # default PRA Vault account group (numeric id) for the vaulted admin credential; usually chosen per-deploy instead
    # Entra/IdP group → cluster RBAC (real-identity JIT demo): default group the
    # per-cluster "Entra group" action binds (overridable in the action). Members get
    # entra_rbac_group_role; Entitle's Entra-ID integration JIT-grants membership.
    entra_rbac_group_id: str = ""             # Entra group Object ID (GUID)
    entra_rbac_group_name: str = ""           # OPTIONAL friendly name (display only)
    entra_rbac_group_role: str = "cluster-admin"  # ClusterRole the group binds to
    # Subject prefix Entitle's Kubernetes integration binds for a JIT grant
    # (<prefix>:<email>) — also what a user passes to `kubectl --as=` when consuming
    # an "Impersonation access" grant. Default "entitle".
    entitle_k8s_user_prefix: str = "entitle"
    # Entra OIDC federation for EKS (the "Entra federation" action's AWS leg): a
    # shared Entra app registration associated as the cluster's OIDC IdP so a user's
    # Entra token authenticates and its group OIDs match the binding above.
    entra_oidc_client_id: str = ""            # shared Entra app client id (OIDC audience); required to federate EKS
    entra_oidc_issuer_url: str = ""           # blank → https://login.microsoftonline.com/<azure_tenant_id>/v2.0
    entra_oidc_username_claim: str = "oid"    # OIDC username claim (portable Entra user Object ID)
    entra_oidc_groups_claim: str = "groups"   # OIDC groups claim (Entra emits group Object IDs)
    # GKE Workforce Identity Federation (the "Entra federation" action's GCP leg):
    # users reach GKE via Connect Gateway as workforce identities; RBAC subject is
    # principalSet://…/workforcePools/<pool>/group/<entra-oid>. Pool + provider are
    # created once at the org level (gcloud iam workforce-pools).
    gcp_workforce_pool_id: str = ""           # bare workforce pool id (e.g. bt-entra-pool); required to federate GKE
    gcp_workforce_provider_id: str = ""       # OIDC provider id in the pool (for the end-user login config)
    gcp_workforce_location: str = "global"    # workforce pool location (always "global" today)


class VirtualDesktopsFeatureConfig(BaseModel):
    """Config-only panel (no `enabled`) for the Virtual Desktops PREVIEW feature.
    The preview toggle owns `vdesktops_enabled`; this panel holds the Azure VDI
    pool defaults the sandbox emits. Normally pushed via /api/setup/import. See
    _CONFIG_ONLY_FEATURES."""
    # The dedicated non-delegated desktops subnet (sandbox: desktops-subnet,
    # 10.99.6.0/24). Pools default here. Its NSG allows outbound 443 so the RS
    # jump client can register at first boot. aci-subnet (ACI-delegated) can't
    # host VM NICs — picking it is what broke the first Win 11 pool deploy.
    azure_desktops_subnet_id: str = ""
    # Default pool VM size (blank → the pool form's own default). Win 11 / Trusted
    # Launch needs a Gen2 size (e.g. Standard_D2s_v3) — NOT a B-series.
    azure_desktops_vm_size: str = ""
    # PRA Vault account-group id (numeric) for RDP credential injection on Windows
    # seats (Phase 2). Blank → the account lands in PRA's Default group.
    azure_desktops_vault_account_group_id: str = ""


class CostExplorerFeatureConfig(BaseModel):
    """Cloud cost tracking. The toggle owns `cost_explorer_enabled` (the feature
    name maps to it via _feature_to_cfg_key). `cost_monthly_budget` is the monthly
    spend budget used for the over/approaching alerts (0 = no budget)."""
    enabled: bool = False
    cost_monthly_budget: float = 0.0
    cost_budget_aws: float = 0.0
    cost_budget_azure: float = 0.0
    cost_budget_gcp: float = 0.0
    gcp_billing_export_table: str = ""

    @field_validator("cost_monthly_budget", "cost_budget_aws", "cost_budget_azure",
                     "cost_budget_gcp", mode="before")
    @classmethod
    def _blank_to_zero(cls, v):
        # An empty/blank input (no budget) round-trips as "" / null — treat as 0.
        return 0.0 if v in (None, "") else v


class AdmissionControlFeatureConfig(BaseModel):
    """Action-level policy guardrails (pre-action admission control). The toggle owns
    `admission_control_enabled` (feature name → key via _feature_to_cfg_key).
    `admission_gated_actions` selects which deploy actions are gated; the rest are
    caps injected into the Rego policies as input.limits. All lists accept JSON
    (["a","b"]) or CSV (a,b)."""
    enabled: bool = False
    admission_gated_actions: str = ""
    admission_allowed_regions: str = ""
    admission_denied_instance_types: str = ""
    admission_prod_window: str = ""


class MultiRegionFeatureConfig(BaseModel):
    """Config-only panel that hosts the per-region config-set editors for AWS, GCP
    and Azure. The region maps themselves live under ``<cloud>_region_configs`` and
    are read/written via GET/PUT ``/api/setup/regions/{cloud}`` — this model carries
    no flat keys, it just gives the Settings UI a panel to mount those editors in."""
    enabled: bool = False


class OidcFeatureConfig(BaseModel):
    """Generic OpenID Connect SSO. Config-only: there is no separate enable flag —
    SSO is live once an issuer and client id are set, which is exactly what
    ``oidc_service.is_configured()`` checks, so a second source of truth would
    only be able to disagree with it."""
    enabled: bool = False
    oidc_issuer: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    oidc_provider_name: str = ""
    oidc_scopes: str = ""
    oidc_groups_claim: str = ""


_FEATURE_MODELS = {
    "vmware":       VMwareFeatureConfig,
    "beyondtrust":  BeyondTrustFeatureConfig,
    "portainer":    PortainerFeatureConfig,
    "ansible":      AnsibleFeatureConfig,
    "entitle":      EntitleFeatureConfig,
    "proxmox":      ProxmoxFeatureConfig,
    "vsphere":      VSphereFeatureConfig,
    "hyperv":       HyperVFeatureConfig,
    "nutanix":      NutanixFeatureConfig,
    "xcpng":        XcpNgFeatureConfig,
    "cloud_database": CloudDatabaseFeatureConfig,
    "k8s_management": K8sManagementFeatureConfig,
    "vdesktops":      VirtualDesktopsFeatureConfig,
    "cost_explorer":  CostExplorerFeatureConfig,
    "admission_control": AdmissionControlFeatureConfig,
    "multi_region":   MultiRegionFeatureConfig,
    "oidc":           OidcFeatureConfig,
}

# Features whose panel carries config but NOT an enable toggle — their on/off
# lives elsewhere (e.g. a preview flag). _read/_write_feature skip the enabled
# key for these, so saving config can't flip the feature's flag.
_CONFIG_ONLY_FEATURES = {"vdesktops", "multi_region", "oidc"}

_SECRET_FEATURE_KEYS = frozenset({
    "pscli_client_secret", "bt_client_secret", "epml_pat",
    "clouddb_ps_ssm_secret_access_key", "pra_config_api_client_secret",
    "portainer_pat",
    "entitle_api_token", "entitle_api_key",
    "proxmox_token_secret", "proxmox_password",
    "vsphere_password",
    "hyperv_password",
    "nutanix_password",
    "xcpng_password",
    "ansible_aci_acr_password",
    "rancher_bootstrap_password", "rancher_admin_password", "rancher_api_token",
    "oidc_client_secret",
})


def _feature_to_cfg_key(feature: str) -> str:
    return f"{feature}_enabled"


def _read_feature(feature: str, model_cls) -> dict:
    """Build a response dict for a feature by reading config_service."""
    from ..services import config_service
    enabled_key = _feature_to_cfg_key(feature)
    data = {} if feature in _CONFIG_ONLY_FEATURES else {"enabled": config_service.get_bool(enabled_key)}
    for field, info in model_cls.model_fields.items():
        if field == "enabled":
            continue
        # Bool fields must round-trip as real booleans: a raw "" would render
        # the toggle wrong AND fail pydantic validation when PATCHed back.
        if info.annotation is bool:
            data[field] = config_service.get_bool(field, bool(info.default))
            continue
        # Int fields have the same problem: an unset key reads back as "" (get's
        # default), which fails int validation on the PATCH round-trip and blocks
        # the whole panel save. Coerce to int, falling back to the model default.
        if info.annotation is int:
            raw = config_service.get(field)
            try:
                data[field] = int(raw)
            except (TypeError, ValueError):
                data[field] = info.default
            continue
        val = config_service.get(field)
        # Redact secrets for display
        data[field] = "••••••••" if (field in _SECRET_FEATURE_KEYS and val) else val
    return data


def _write_feature(feature: str, payload_dict: dict) -> None:
    """Persist a feature's config to config_service."""
    from ..services import config_service
    pairs: dict = {}
    if feature in _CONFIG_ONLY_FEATURES:
        payload_dict.pop("enabled", None)  # the feature's flag is owned elsewhere
    else:
        enabled_key = _feature_to_cfg_key(feature)
        enabled = payload_dict.pop("enabled", False)
        pairs[enabled_key] = "1" if enabled else "0"
    for key, value in payload_dict.items():
        # Skip placeholder values (user left secret field as bullets)
        if isinstance(value, str) and value.startswith("••"):
            continue
        if isinstance(value, bool):
            pairs[key] = "1" if value else "0"   # get_bool's canonical form
        else:
            pairs[key] = value if isinstance(value, str) else str(value)
    config_service.set_many(pairs)

    if feature == "azure":
        try:
            from ..services import azure_service
            azure_service.invalidate_credentials()
        except Exception:
            pass

    if feature == "oidc":
        # Discovery + JWKS are cached for an hour; without this an operator who
        # fixes a typo'd issuer would keep hitting the old one until it expired.
        try:
            from ..services import oidc_service
            oidc_service.clear_cache()
        except Exception:
            pass


@router.get("/feature/{feature_name}")
def get_feature_config(feature_name: str, request: Request):
    """Return current config for a single feature (secrets redacted). Admin JWT required."""
    _require_admin(request)
    model_cls = _FEATURE_MODELS.get(feature_name)
    if model_cls is None:
        raise HTTPException(status_code=404, detail=f"Unknown feature: {feature_name}")
    return _read_feature(feature_name, model_cls)


@router.patch("/feature/{feature_name}")
def patch_feature_config(feature_name: str, payload: dict, request: Request):
    """Save config for a single feature. Admin JWT required."""
    _require_admin(request)
    model_cls = _FEATURE_MODELS.get(feature_name)
    if model_cls is None:
        raise HTTPException(status_code=404, detail=f"Unknown feature: {feature_name}")
    # Validate through the model for type safety, but only persist keys that were
    # explicitly sent — this prevents a toggle-only call ({enabled: true}) from
    # blanking out credential fields that weren't included in the payload.
    validated = model_cls(**payload).model_dump()
    filtered = {k: v for k, v in validated.items() if k in payload}
    _write_feature(feature_name, filtered)
    logger.info("Feature '%s' configuration updated.", feature_name)
    return {"ok": True}


# ── OIDC discovery probe ──────────────────────────────────────────────────────

@router.post("/oidc/test")
def test_oidc_discovery(request: Request):
    """Fetch the configured issuer's discovery document and report what came back.

    A typo'd issuer is the single most common misconfiguration, and without this
    the only feedback is a failed login redirect that says nothing useful. Reports
    the endpoints the provider advertises plus whether it can issue the groups
    claim the workgroup mapping depends on.
    """
    _require_admin(request)
    from ..services import config_service, oidc_service
    if not oidc_service.is_configured():
        raise HTTPException(status_code=400,
                            detail="Set an issuer and client id first, then save.")
    oidc_service.clear_cache()   # always probe live, never a cached answer
    try:
        doc = oidc_service.discovery()
    except oidc_service.OIDCError as e:
        raise HTTPException(status_code=502, detail=str(e))

    supported = doc.get("claims_supported") or []
    groups_claim = config_service.get("oidc_groups_claim") or "groups"
    return {
        "ok": True,
        "issuer": doc.get("issuer", ""),
        "authorization_endpoint": doc.get("authorization_endpoint", ""),
        "token_endpoint": doc.get("token_endpoint", ""),
        "jwks_uri": doc.get("jwks_uri", ""),
        "scopes_supported": doc.get("scopes_supported") or [],
        # Advisory only: many providers omit claims_supported entirely, and some
        # emit groups without advertising them, so absence is not a failure.
        "groups_claim": groups_claim,
        "groups_claim_advertised": (groups_claim in supported) if supported else None,
    }


# ── Azure per-region config sets (multi-region, Follow-on 6 PR3) ──────────────
#
# Each cloud's region map lives as ONE JSON value (``<cloud>_region_configs``) rather
# than flat keys, so it gets its own GET/PUT pair instead of riding the _FEATURE_MODELS
# panel. The Configure UI lists configured regions and edits a region's fields; blank
# fields fall back to the flat keys at resolve time (region_config.py).

class RegionConfigsPayload(BaseModel):
    """Full region map for replace-on-save: {region: {field: value}}."""
    regions: dict[str, dict] = {}


def _region_configs_response(cloud: str) -> dict:
    """Shared body for the region-config editor GET: the stored map (each entry
    reshaped through the cloud's model so every field key is present), the configured
    default region, the field list, and each field's flat-key fallback value."""
    from ..services import config_service, region_catalog
    from ..services.region_config import load_region_configs, region_fields, field_fallbacks

    model = _REGION_CONFIG_MODELS[cloud]
    stored = load_region_configs(cloud)
    regions = {
        loc: model(**{k: v for k, v in fields.items() if k in model.model_fields}).model_dump()
        for loc, fields in stored.items()
    }
    return {
        "cloud": cloud,
        "default_region": region_catalog.default_region(cloud),
        "fields": list(region_fields(cloud)),
        "fallbacks": {fld: config_service.get(flat)
                      for fld, flat in field_fallbacks(cloud).items()},
        "regions": regions,
    }


def _save_region_configs(cloud: str, payload: RegionConfigsPayload) -> dict:
    """Shared body for the region-config editor PUT (replace-on-save). Each entry is
    coerced through the cloud's model so only known fields persist; blank fields and
    empty regions are dropped by region_config.save_region_configs."""
    from ..services.region_config import save_region_configs
    model = _REGION_CONFIG_MODELS[cloud]
    cleaned = {
        loc: model(**{k: v for k, v in (vals or {}).items() if k in model.model_fields}).model_dump()
        for loc, vals in payload.regions.items()
    }
    save_region_configs(cloud, cleaned)
    logger.info("%s region config sets updated (%d regions).", cloud, len(payload.regions))
    return {"ok": True, "cloud": cloud, "regions": sorted(payload.regions.keys())}


@router.get("/regions/{cloud}")
def get_region_configs(cloud: str, request: Request):
    """Return the configured per-region map for ``cloud`` + its default region + the
    flat-key fallbacks (so the editor can show effective defaults). Admin JWT required."""
    _require_admin(request)
    cloud = (cloud or "").strip().lower()
    if cloud not in _REGION_CONFIG_MODELS:
        raise HTTPException(status_code=404, detail=f"no per-region config for cloud {cloud!r}")
    return _region_configs_response(cloud)


@router.put("/regions/{cloud}")
def put_region_configs(cloud: str, payload: RegionConfigsPayload, request: Request):
    """Replace the per-region map for ``cloud``. Admin JWT required."""
    _require_admin(request)
    cloud = (cloud or "").strip().lower()
    if cloud not in _REGION_CONFIG_MODELS:
        raise HTTPException(status_code=404, detail=f"no per-region config for cloud {cloud!r}")
    return _save_region_configs(cloud, payload)


# Back-compat alias for the original Azure-only editor endpoints (settings.html still
# calls /azure-regions until the UI is parameterised). GET keeps the original
# ``default_location`` response key.
@router.get("/azure-regions")
def get_azure_regions(request: Request):
    _require_admin(request)
    resp = _region_configs_response("azure")
    resp["default_location"] = resp["default_region"]
    return resp


@router.put("/azure-regions")
def put_azure_regions(payload: RegionConfigsPayload, request: Request):
    _require_admin(request)
    return _save_region_configs("azure", payload)


# ── Preview feature flags ─────────────────────────────────────────────────────
#
# Simple on/off toggles for features that gate a router + nav but carry NO
# connection config, so they don't fit the _FEATURE_MODELS panel above. Stored
# in config_service as "1"/"0" (same app_config table); the routers/nav read
# them live via config_service.get_bool, so toggling here takes effect with no
# restart.
_PREVIEW_FLAGS = {
    "vdesktops_enabled": (
        "Virtual Desktops", "Desktop pools brokered as PRA sessions (Phase 1: Azure)."),
}

# Preview flags that ALSO have a config panel — maps the flag key to the
# _FEATURE_MODELS key its "Configure" link opens. The flag stays the on/off;
# the panel is config-only (see _CONFIG_ONLY_FEATURES).
_PREVIEW_FLAG_CONFIG = {
    "vdesktops_enabled": "vdesktops",
}


@router.get("/flags")
def get_preview_flags(request: Request):
    """Current on/off state for each preview feature flag. Admin JWT required."""
    _require_admin(request)
    from ..config import settings
    from ..services import config_service
    return {
        key: {
            "label": label,
            "description": desc,
            "enabled": config_service.get_bool(key, getattr(settings, key, False)),
            "config_feature": _PREVIEW_FLAG_CONFIG.get(key),
        }
        for key, (label, desc) in _PREVIEW_FLAGS.items()
    }


@router.patch("/flag/{key}")
def patch_preview_flag(key: str, payload: dict, request: Request):
    """Toggle a single preview feature flag on/off. Admin JWT required."""
    _require_admin(request)
    if key not in _PREVIEW_FLAGS:
        raise HTTPException(status_code=404, detail=f"Unknown preview flag: {key}")
    from ..services import config_service
    enabled = bool(payload.get("enabled"))
    config_service.set(key, "1" if enabled else "0")
    logger.info("Preview flag '%s' set to %s.", key, enabled)
    return {"ok": True, "key": key, "enabled": enabled}
