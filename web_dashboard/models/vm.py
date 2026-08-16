"""VM-related Pydantic schemas"""
from typing import Optional, List
from pydantic import BaseModel


class VMInfo(BaseModel):
    vmx_path: str
    vm_name: str
    workgroup: str
    is_running: Optional[bool] = None
    ip_address: Optional[str] = None
    os_type: Optional[str] = None
    last_seen_running_at: Optional[str] = None
    is_online: Optional[bool] = None
    last_online_check_at: Optional[str] = None
    # Where this row came from. "local" is the PowerShell scan of this host; anything
    # else is the name of the remote agent that synced it. Defaulted so every existing
    # caller and every stored row keeps working unchanged.
    source: str = "local"
    # vmrest's opaque id, for agent-synced rows only. The local path addresses a VM by
    # its VMX file; vmrest addresses it by this, and a power verb needs it.
    vm_id: Optional[str] = None


class VMListResponse(BaseModel):
    vms: List[VMInfo]
    count: int
    cached_at: Optional[str] = None


class VMStartRequest(BaseModel):
    vmx_path: str
    ip_wait_timeout: int = 120


class VMStopRequest(BaseModel):
    vmx_path: str


class BulkStartRequest(BaseModel):
    workgroup: str


class BulkStopRequest(BaseModel):
    workgroup: str


class VMDecommissionRequest(BaseModel):
    vmx_path: str
    delete_folder: bool = False
    guest_password: str = ""


class VMOperationResponse(BaseModel):
    job_id: str
    status: str
    message: str


class VMOnlineCheckRequest(BaseModel):
    vmx_path: str
