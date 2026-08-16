"""
Pydantic models for Azure API endpoints.
Mirrors web_dashboard/models/aws.py structure.
"""
from typing import List, Optional
from pydantic import BaseModel, Field

from ..services.vm_naming import MAX_DEPLOY_COUNT


# ── Azure Image (Gallery image or standalone Managed Image) ──────────────────

class AzureImageInfo(BaseModel):
    resource_id: str          # Full ARM resource ID
    name: str
    description: str = ""
    state: str = ""           # "Succeeded", "Creating", "Failed"
    creation_date: str = ""
    os_type: str = "Linux"    # "Linux" | "Windows"
    source: str = "managed"   # "gallery" | "managed"
    gallery_name: str = ""
    sku: str = ""
    location: str = ""
    resource_group: str = ""  # RG the managed image lives in (empty for gallery rows)
    # Regions the image is deployable in — gallery: the latest version's replicated
    # target regions; managed: its single [location]. Drives the picker region filter.
    regions: List[str] = []
    # Marketplace image fields (optional)
    publisher: Optional[str] = None
    offer: Optional[str] = None
    version: Optional[str] = None


# ── Azure VM ──────────────────────────────────────────────────────────────────

class AzureVMInfo(BaseModel):
    vm_id: str
    name: str
    state: str                # "running", "deallocated", "stopped", etc.
    public_ip: Optional[str] = None
    private_ip: Optional[str] = None
    location: str = ""
    size: str = ""
    os_type: str = ""
    workgroup: Optional[str] = None  # from `workgroup` resource tag; None = unassigned
    job_id: Optional[str] = None
    deployed_by: Optional[str] = None


# ── Network options (form dropdowns) ─────────────────────────────────────────

class AzureSubnetInfo(BaseModel):
    id: str
    name: str
    address_prefix: str = ""
    vnet_name: str = ""
    # Service names this subnet is delegated to (e.g.
    # "Microsoft.ContainerInstance/containerGroups"). Non-empty → the subnet
    # can't host plain VM NICs, so the Desktops pool picker greys it out.
    delegations: List[str] = []

class AzureNSGInfo(BaseModel):
    id: str
    name: str
    resource_group: str = ""

class AzureSSHKeyInfo(BaseModel):
    id: str
    name: str
    public_key: str
    resource_group: str = ""

class AzureNetworkOptions(BaseModel):
    location: str = ""        # the region these subnets/NSGs/sizes were scoped to
    locations: List[str] = []
    vm_sizes: List[str] = []
    subnets: List[AzureSubnetInfo] = []
    nsgs: List[AzureNSGInfo] = []
    ssh_keys: List[AzureSSHKeyInfo] = []
    warnings: List[str] = []


# ── Deploy request / response ─────────────────────────────────────────────────

class AzureDeployRequest(BaseModel):
    image_id: str              # Full ARM resource ID of the image
    vm_name: str
    vm_size: str = "Standard_B2s"
    location: str = ""         # defaults to settings.azure_location
    resource_group: str = ""   # defaults to settings.azure_resource_group
    subnet_id: str
    nsg_ids: List[str] = []
    create_public_ip: bool = False
    os_type: str = "Linux"     # "Linux" | "Windows" — Windows gets a generated admin password
    trusted_launch: bool = False  # Win 11 / Trusted-Launch gallery images: set SecurityProfile + Windows_Client
    ssh_username: str = "azureuser"  # admin username on Windows
    ssh_public_key: str = ""   # RSA public key text; required for Linux (endpoint enforces)
    workgroup: str             # written as `workgroup` resource tag
    # Marketplace image metadata (optional, used if present)
    image_publisher: Optional[str] = None
    image_offer: Optional[str] = None
    image_sku: Optional[str] = None
    image_version: Optional[str] = None
    # PRA/jumpoint per-deploy overrides — config defaults are the fallback. Values
    # are secrets-backend references (e.g. azure_kv://…), not raw secrets.
    jump_group: Optional[str] = None             # PRA Jump Group name override (else azure_bt_jump_group_name)
    jumpoint_name: Optional[str] = None          # PRA Jumpoint name override (else bt_jumpoint_name)
    pra_credential_ref: Optional[str] = None     # secret ref → bt_client_secret override for the shell jump
    docker_deploy_key_ref: Optional[str] = None  # secret ref → ACI Jumpoint deploy key (else azure_aci_docker_deploy_key)
    register_in_entitle: bool = False            # opt in to registering this VM as an Entitle SSH integration (Linux only)
    register_in_passwordsafe: bool = False       # opt in to onboarding this VM into Password Safe (managed system + account, Linux only)
    ssh_key_secret_override: Optional[str] = None  # optional Key Vault keypair secret to use for the SSH key (must be JSON with a public_key)
    count: int = Field(
        default=1, ge=1, le=MAX_DEPLOY_COUNT,
        description="Number of identical VMs to deploy. 1 is a plain single deploy; "
                    ">1 fans out into a batch and auto-numbers the names. Batch names "
                    "are budgeted to 15 characters because azure_service derives the "
                    "in-guest hostname as vm_name[:15] — see services/vm_naming.")


class AzureBulkDeployItem(BaseModel):
    """One VM in a bulk request.

    The image fields are per item because bulk means one VM *per selected image* —
    the UI multi-selects images, and previously every VM was built from the first
    one. All of them are optional and fall back to the request-level value, so a
    client posting the old shape (names only, one top-level image_id) is unchanged.
    """
    vm_name: str
    image_id: Optional[str] = None
    image_publisher: Optional[str] = None
    image_offer: Optional[str] = None
    image_sku: Optional[str] = None
    image_version: Optional[str] = None
    os_type: Optional[str] = None          # falls back to the request-level os_type
    trusted_launch: Optional[bool] = None


class AzureBulkDeployRequest(BaseModel):
    items: List[AzureBulkDeployItem]
    # Batch-level default for items that don't carry their own image. Relaxed from a
    # required field so per-item images are expressible; the endpoint still rejects a
    # request where any item resolves to no image at all.
    image_id: str = ""
    vm_size: str = "Standard_B2s"
    location: str = ""
    resource_group: str = ""
    subnet_id: str
    nsg_ids: List[str] = []
    create_public_ip: bool = False
    os_type: str = "Linux"     # "Linux" | "Windows" — Windows gets a generated password per VM
    trusted_launch: bool = False  # Win 11 / Trusted-Launch gallery images: set SecurityProfile + Windows_Client
    ssh_username: str = "azureuser"  # admin username on Windows
    ssh_public_key: str = ""   # required for Linux (endpoint enforces)
    workgroup: str             # written as `workgroup` resource tag on all VMs
    register_in_entitle: bool = False  # opt in to registering each VM as an Entitle SSH integration (Linux only)
    register_in_passwordsafe: bool = False  # opt in to onboarding each VM into Password Safe (managed system + account, Linux only)
    ssh_key_secret_override: Optional[str] = None  # optional Key Vault keypair secret to use for the SSH key (must be JSON with a public_key)
    # Marketplace image metadata (optional, used if present)
    image_publisher: Optional[str] = None
    image_offer: Optional[str] = None
    image_sku: Optional[str] = None
    image_version: Optional[str] = None
    # PRA/jumpoint overrides, mirroring AzureDeployRequest. The bulk runner resolved
    # these from config only, so a batch ignored whatever the form's pickers said.
    jump_group: Optional[str] = None             # else azure_bt_jump_group_name / bt_jump_group_name
    jumpoint_name: Optional[str] = None          # else azure_jumpoint_name / bt_jumpoint_name
    pra_credential_ref: Optional[str] = None     # secret ref → bt_client_secret for the shell jump
    docker_deploy_key_ref: Optional[str] = None  # secret ref → ACI Jumpoint deploy key


class AzureDeployResponse(BaseModel):
    job_id: str          # for a batch (count > 1) this is the PARENT job
    vm_name: str
    message: str = "Deployment started"
    # Batch fields, defaulted so a count-1 response is unchanged for existing callers.
    count: int = 1
    batch_id: Optional[str] = None
    job_ids: List[str] = []   # child job ids, in name order
    names: List[str] = []     # the expanded names actually used, post-truncation


class AzureBulkDeployResponse(BaseModel):
    jobs: List[AzureDeployResponse]
    # Already minted by the endpoint, never returned until now — without it the page
    # can't link to the /jobs?batch_id= rollup.
    batch_id: Optional[str] = None


# ── Image capture ─────────────────────────────────────────────────────────────

class AzureCreateImageRequest(BaseModel):
    name: str
    description: str = ""
    generalize: bool = False   # True = deallocate+generalize (VM unusable after)
