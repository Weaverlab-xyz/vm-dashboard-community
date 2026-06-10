"""Pydantic models for virtual-desktop management (`/desktops` + `/api/desktops`).

Phase 0 of the virtual-desktop plan.
"""
from typing import Optional

from pydantic import BaseModel


class PoolCreateRequest(BaseModel):
    cloud: str                       # aws | azure | gcp
    name: str                        # pool name (unique)
    image: str                       # desktop image id/name (provisioned in Phase 1)
    size: str                        # instance size/type (used in Phase 1)
    count: int = 1                   # number of seats


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
