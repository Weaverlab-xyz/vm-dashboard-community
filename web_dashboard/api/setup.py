"""
Setup wizard API.

POST /api/setup/complete   — initial setup, no auth required, errors if already done
PUT  /api/setup/config     — reconfigure, admin JWT required
GET  /api/setup/status     — {complete: bool, configured_keys: list}
GET  /api/setup/config     — current config with secrets redacted (admin JWT if setup done)
"""
import logging

from fastapi import APIRouter, HTTPException, Request
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

    # Invalidate Azure credential cache so the service picks up new values.
    try:
        from ..services import azure_service
        azure_service.invalidate_credentials()
    except Exception:
        pass


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
def complete_setup(payload: SetupPayload, request: Request):
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
    logger.info("Setup wizard completed by first-run submission.")
    return {"ok": True}


@router.put("/config")
def reconfigure(payload: SetupPayload, request: Request):
    """
    Reconfigure credentials and feature flags. Admin JWT required.
    Admin account password is updated only when payload.admin.password is non-empty.
    """
    _require_admin(request)
    if payload.admin.password:
        _upsert_admin(payload.admin.username, payload.admin.password)
    _apply_config(payload)
    logger.info("Configuration updated via reconfigure endpoint.")
    return {"ok": True}


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
    bt_api_host: str = ""
    bt_client_id: str = ""
    bt_client_secret: str = ""      # encrypted at rest
    # EPM for Linux (EPM-L) — SaaS API at app.beyondtrust.io
    epml_pat: str = ""              # encrypted at rest; Bearer token for EPML API

class PortainerFeatureConfig(BaseModel):
    enabled: bool = False
    portainer_url: str = ""
    portainer_verify_ssl: bool = True

class AnsibleFeatureConfig(BaseModel):
    enabled: bool = False
    # Runner selection
    ansible_runner: str = "local"            # "local" | "ecs" | "aci" | "gcp"
    ansible_default_user: str = "ec2-user"  # SSH user for cloud runner targets
    # S3 storage
    ansible_s3_bucket: str = ""
    ansible_s3_region: str = ""
    ansible_s3_prefix: str = "config-mgmt"
    # Azure Blob storage
    ansible_azure_storage_account: str = ""
    ansible_azure_container: str = "playbooks"
    ansible_azure_prefix: str = "config-mgmt"
    # GCS storage
    ansible_gcs_bucket: str = ""
    ansible_gcs_prefix: str = "config-mgmt"
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
}

_SECRET_FEATURE_KEYS = frozenset({
    "pscli_client_secret", "bt_client_secret", "epml_pat",
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
    data = {"enabled": config_service.get_bool(enabled_key)}
    for field in model_cls.model_fields:
        if field == "enabled":
            continue
        val = config_service.get(field)
        # Redact secrets for display
        data[field] = "••••••••" if (field in _SECRET_FEATURE_KEYS and val) else val
    return data


def _write_feature(feature: str, payload_dict: dict) -> None:
    """Persist a feature's config to config_service."""
    from ..services import config_service
    pairs: dict = {}
    enabled_key = _feature_to_cfg_key(feature)
    enabled = payload_dict.pop("enabled", False)
    pairs[enabled_key] = "1" if enabled else "0"
    for key, value in payload_dict.items():
        # Skip placeholder values (user left secret field as bullets)
        if isinstance(value, str) and value.startswith("••"):
            continue
        pairs[key] = str(value) if not isinstance(value, str) else value
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
