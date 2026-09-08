"""Pydantic models for virtual-desktop management (`/desktops` + `/api/desktops`).

Phase 0 of the virtual-desktop plan.
"""
from typing import Optional

from pydantic import BaseModel


class PoolCreateRequest(BaseModel):
    cloud: str                       # aws | azure | gcp
    name: str                        # pool name (unique)
    count: int = 1                   # number of seats
    # Generic (record-only clouds); optional.
    image: Optional[str] = None
    size: Optional[str] = None

    # ── Azure deploy spec (Phase 1; required when cloud == "azure") ──
    location: Optional[str] = None
    resource_group: Optional[str] = None
    vm_size: Optional[str] = None
    image_id: Optional[str] = None          # full ARM id of a gallery/managed image
    image_publisher: Optional[str] = None   # OR a marketplace image
    image_offer: Optional[str] = None
    image_sku: Optional[str] = None
    image_version: Optional[str] = None
    subnet_id: Optional[str] = None
    nsg_ids: list[str] = []
    create_public_ip: bool = False          # desktops are private + brokered
    os_type: str = "Linux"                  # "Linux" | "Windows" — Windows seats get generated passwords
    trusted_launch: bool = False            # Win 11 / Trusted-Launch gallery images (SecurityProfile + Windows_Client)
    ssh_username: str = "azureuser"         # admin username on Windows
    ssh_public_key: Optional[str] = None     # client-provided (as the Azure deploy form does); Linux only

    # ── AWS deploy spec (required when cloud == "aws") ──
    # `subnet_id`, `os_type` and `ssh_public_key` above are shared with Azure. AWS pools
    # are Linux-only, so `ssh_public_key` is not optional in practice — the service's
    # validator says so rather than the model, which keeps the refusal in one place with
    # the reason attached.
    region: Optional[str] = None            # falls back to the configured aws_region
    ami_id: Optional[str] = None            # OR `image` above
    instance_type: Optional[str] = None     # OR `size` above
    security_group_ids: list[str] = []
    iam_instance_profile: Optional[str] = None   # e.g. for SSM access

    # ── GCP deploy spec (required when cloud == "gcp") ──
    # `os_type` and `ssh_public_key` above are shared. GCP pools are Linux-only.
    project_id: Optional[str] = None        # falls back to the configured gcp_project
    zone: Optional[str] = None              # falls back to the configured gcp_zone
    machine_type: Optional[str] = None      # OR `size` above
    image_self_link: Optional[str] = None   # OR `image` above
    subnetwork: Optional[str] = None
    create_external_ip: bool = False        # desktops are private + brokered
    disk_size_gb: int = 20
    network_tags: list[str] = []


class PoolScaleRequest(BaseModel):
    count: int                       # desired seat count


class VirtualDesktopInfo(BaseModel):
    id: str
    cloud: str
    pool_name: str
    kind: str
    vm_resource_id: Optional[str] = None
    status: str
    assigned_user: Optional[str] = None
    pra_jump_id: Optional[str] = None
    created_by: Optional[str] = None
    created_at: str


class PoolSummary(BaseModel):
    pool_name: str
    cloud: str
    kind: str
    count: int
    statuses: dict[str, int]         # status -> seat count
