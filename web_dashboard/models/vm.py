"""VM-related Pydantic schemas"""
from typing import Optional, List
from pydantic import BaseModel


class VMInfo(BaseModel):
    """One Workstation VM, as the page renders it.

    Every field here is something a remote agent can actually report. The reachability
    pair (`is_online`, `last_online_check_at`) and `last_seen_running_at` are gone with
    the PowerShell path that wrote them: the probe was a TCP connect made FROM THE APP
    CONTAINER, which has no route to the network these VMs live on, so it could only
    ever return "offline" for a reachable VM.
    """
    # The VMX file on the agent's host. Display-only: identity is `vm_id`, and the agent
    # reports the path as `scope`, which may be absent.
    vmx_path: str
    vm_name: str
    workgroup: str
    is_running: Optional[bool] = None
    ip_address: Optional[str] = None
    os_type: Optional[str] = None
    # Which agent synced this row, for the badge on the page. Defaults to empty, not
    # "local": there is no local source any more, so defaulting to one would put a
    # LOCAL badge on any row built without it.
    source: str = ""
    # vmrest's opaque id. This is the identity, and what a power op addresses.
    vm_id: Optional[str] = None
    # Which HypervisorConnection this row came from — a power op needs it to resolve the
    # right agent when more than one host is synced onto this page.
    connection_id: Optional[str] = None
    # When the cache row was written, so the page can tell the truth about staleness.
    synced_at: Optional[str] = None


class VMListResponse(BaseModel):
    vms: List[VMInfo]
    count: int
    cached_at: Optional[str] = None
