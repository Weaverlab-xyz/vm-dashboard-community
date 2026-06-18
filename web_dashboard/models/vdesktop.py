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
