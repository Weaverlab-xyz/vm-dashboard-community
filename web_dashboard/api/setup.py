"""
Setup wizard API.

POST /api/setup/complete   — initial setup, no auth required, errors if already done
PUT  /api/setup/config     — reconfigure, admin JWT required
GET  /api/setup/status     — {complete: bool, configured_keys: list}
GET  /api/setup/config     — current config with secrets redacted (admin JWT if setup done)
"""
import logging

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from pydantic import BaseModel

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
    azure_ansible_aci_image: str = "willhallonline/ansible:latest"
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


class GCPSetup(BaseModel):
    gcp_project_id: str = ""
    gcp_region: str = "us-central1"
    gcp_zone: str = "us-central1-a"
    gcp_service_account_json: str = ""   # Full SA JSON key — stored encrypted
    gcp_network: str = "default"
    gcp_subnetwork: str = ""
    gcp_ssh_key_secret_name: str = ""
    gcp_ssh_username: str = "gcp-user"
    # BeyondTrust Jumpoint Docker deploy key — pre-staged for upcoming GCP
    # Cloud Run / GKE Jumpoint provisioning. Stored encrypted; resolved through
    # whichever secrets backend the user picks. No consumer wires it today.
    gcp_cloud_run_docker_deploy_key: str = ""
    # Packer template archive (optional)
    packer_gcs_bucket: str = ""


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


class SetupPayload(BaseModel):
    admin: AdminSetup
    aws: AWSSetup
    azure: AzureSetup
    gcp: GCPSetup = GCPSetup()
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
    pairs: dict = {}
    for key, value in payload.config.items():
        if value is None:
            continue
        if isinstance(value, bool):
            pairs[key] = "1" if value else "0"
        else:
            pairs[key] = value if isinstance(value, str) else str(value)

    if not already:
        _upsert_admin(payload.admin_username, payload.admin_password)

    config_service.set_many(pairs)
    config_service.invalidate()
    try:
        from ..services import azure_service
        azure_service.invalidate_credentials()
    except Exception:
        pass

    if not already:
        config_service.mark_setup_complete()

    background_tasks.add_task(_invalidate_data_caches)
    logger.info(
        "Headless import: %d config keys written [%s]%s.",
        len(pairs),
        ", ".join(sorted(pairs.keys())),
        "" if already else "; admin created + setup marked complete",
    )
    return {"ok": True, "keys_written": len(pairs)}


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
    # PRA API credentials (used by the SRA Terraform provider for Shell Jump provisioning)
    bt_api_host: str = ""
    bt_client_id: str = ""
    bt_client_secret: str = ""      # encrypted at rest
    # Shell Jump provisioning — Jump Group and Jumpoint must pre-exist in PRA
    bt_jump_group_name: str = ""
    bt_jumpoint_name: str = ""
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
    # Runner selection
    ansible_runner: str = "local"            # "local" | "ecs" | "aci" | "gcp"
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
    # Azure ACI runner
    ansible_aci_image: str = "willhallonline/ansible:latest"
    ansible_aci_subnet_id: str = ""
    ansible_aci_ssh_key_secret_name: str = ""
    ansible_aci_acr_server: str = ""
    ansible_aci_acr_username: str = ""
    ansible_aci_acr_password: str = ""      # encrypted at rest
    # GCP Cloud Run runner
    gcp_ansible_cloud_run_region: str = ""
    gcp_ansible_image: str = "willhallonline/ansible:latest"
    gcp_ansible_vpc_connector: str = ""

class EntitleFeatureConfig(BaseModel):
    enabled: bool = False
    entitle_api_url: str = ""
    entitle_api_token: str = ""         # encrypted at rest
    entitle_webhook_secret: str = ""    # encrypted at rest
    approval_gate_enabled: bool = False
    # User-JIT (Phase 4) — operator surfaces these via the Settings panel.
    entitle_user_jit_enabled: bool = False
    entitle_request_portal_url: str = ""
    entitle_resource_ids_json: str = "{}"

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
    """Config-only panel (no `enabled`) for the Cloud Databases PREVIEW feature.
    The preview toggle owns `cloud_database_enabled`; this panel only holds the
    per-cloud Managed-DB network IDs the sandbox emits (normally pushed via
    /api/setup/import). See _CONFIG_ONLY_FEATURES."""
    # AWS RDS
    aws_db_subnet_group_name: str = ""
    aws_db_parameter_group_name: str = ""
    aws_db_security_group_id: str = ""
    # Azure Flexible Server
    azure_db_subnet_id: str = ""
    azure_db_private_dns_zone_id: str = ""
    # GCP Cloud SQL
    gcp_db_network: str = ""


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
}

# Features whose panel carries config but NOT an enable toggle — their on/off
# lives elsewhere (e.g. a preview flag). _read/_write_feature skip the enabled
# key for these, so saving config can't flip the feature's flag.
_CONFIG_ONLY_FEATURES = {"cloud_database"}

_SECRET_FEATURE_KEYS = frozenset({
    "pscli_client_secret", "bt_client_secret", "epml_pat",
    "portainer_pat",
    "entitle_api_token", "entitle_webhook_secret",
    "proxmox_token_secret", "proxmox_password",
    "vsphere_password",
    "hyperv_password",
    "nutanix_password",
    "xcpng_password",
    "ansible_aci_acr_password",
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
    "cloud_database_enabled": (
        "Cloud Databases", "Private managed databases reached through a PRA tunnel."),
    "k8s_management_enabled": (
        "Kubernetes Management", "Register + manage Kubernetes clusters (Phase 1)."),
}

# Preview flags that ALSO have a config panel — maps the flag key to the
# _FEATURE_MODELS key its "Configure" link opens. The flag stays the on/off;
# the panel is config-only (see _CONFIG_ONLY_FEATURES).
_PREVIEW_FLAG_CONFIG = {
    "cloud_database_enabled": "cloud_database",
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
