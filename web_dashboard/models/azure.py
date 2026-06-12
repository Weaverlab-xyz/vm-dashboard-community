"""
Pydantic models for Azure API endpoints.
Mirrors web_dashboard/models/aws.py structure.
"""
from typing import List, Optional
from pydantic import BaseModel


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
    ssh_username: str = "azureuser"  # admin username on Windows
    ssh_public_key: str = ""   # RSA public key text; required for Linux (endpoint enforces)
    workgroup: str             # written as `workgroup` resource tag
    # Marketplace image metadata (optional, used if present)
    image_publisher: Optional[str] = None
    image_offer: Optional[str] = None
    image_sku: Optional[str] = None
    image_version: Optional[str] = None


class AzureBulkDeployItem(BaseModel):
    vm_name: str


class AzureBulkDeployRequest(BaseModel):
    items: List[AzureBulkDeployItem]
    image_id: str
    vm_size: str = "Standard_B2s"
    location: str = ""
    resource_group: str = ""
    subnet_id: str
    nsg_ids: List[str] = []
    create_public_ip: bool = False
    os_type: str = "Linux"     # "Linux" | "Windows" — Windows gets a generated password per VM
    ssh_username: str = "azureuser"  # admin username on Windows
    ssh_public_key: str = ""   # required for Linux (endpoint enforces)
    workgroup: str             # written as `workgroup` resource tag on all VMs
    # Marketplace image metadata (optional, used if present)
    image_publisher: Optional[str] = None
    image_offer: Optional[str] = None
    image_sku: Optional[str] = None
    image_version: Optional[str] = None


class AzureDeployResponse(BaseModel):
    job_id: str
    vm_name: str
    message: str = "Deployment started"


class AzureBulkDeployResponse(BaseModel):
    jobs: List[AzureDeployResponse]


# ── Image capture ─────────────────────────────────────────────────────────────

class AzureCreateImageRequest(BaseModel):
    name: str
    description: str = ""
    generalize: bool = False   # True = deallocate+generalize (VM unusable after)
