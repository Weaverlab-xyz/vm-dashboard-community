"""Pydantic models for the agent demo cell.

Cloud-agnostic, unlike the network cell: the worker attaches to a Linux VM over SSH, so
the only thing that differs between clouds is how that VM was created — which is
somebody else's job by the time this runs.
"""
from typing import List, Optional
from pydantic import BaseModel, Field

from ..services.agentcell_service import DEFAULT_PAT_HOURS, MAX_PAT_HOURS


class AgentCellCreateRequest(BaseModel):
    """Install one non-human principal onto a host this dashboard already deployed.

    There is no image and no VM here. The cell attaches, mints an authorization, records
    what was issued, and installs a worker — see ``services/agentcell_service`` for why
    each of those is the shape it is.
    """
    name: str = Field(min_length=1, max_length=120)
    # A NAME OR AN IP, re-derived against this dashboard's deploy rows by
    # spire_lab_service.resolve_host. The caller supplies a proposal, not an address.
    host_ref: str = Field(min_length=1)
    cloud: str = "gcp"
    # The SPIRE lab whose trust domain attests the worker. Its trust domain becomes the
    # worker's SPIFFE ID; without one the worker would log `unattested`.
    spire_lab_id: str = Field(min_length=1)
    # The user the agent's token is minted against. This is the agent's blast radius:
    # every MCP tool applies this user's RBAC, so the cell refuses an administrator.
    pat_user_id: str = Field(min_length=1)
    # Always bounded. The model allows a non-expiring PAT; this cell does not, because a
    # non-human principal whose authorization never ends is what the demo argues against.
    pat_hours: int = Field(default=DEFAULT_PAT_HOURS, ge=1, le=MAX_PAT_HOURS)
    workgroup: Optional[str] = None


class AgentCellCreateResponse(BaseModel):
    id: str
    name: str
    spiffe_id: str = ""
    pat_name: str = ""
    pat_expires_at: str = ""
    # Shown ONCE, exactly as api/tokens.create_token shows it, and never stored on the
    # row. The operator carries it into the install playbook; after this response nothing
    # can recover it.
    token: str = ""
    message: str = ""
    notes: List[str] = []


class AgentCellLinkRequest(BaseModel):
    """Make an agent answerable for one Workload Lab credential.

    **Whether this is a consumption depends on the tab.** ``cloud`` gives the worker
    nothing — that tab returns its credential to nobody. ``kubernetes`` lets the agent
    *request* the token, because the worker reaches Password Safe holding nothing. See
    ``services/agentcell_service.LINKABLE_MECHANISMS`` and ``SPENDABLE_MECHANISMS``.
    """
    mechanism: str = Field(min_length=1,
                           description="a Workload Lab tab name: 'cloud' or 'kubernetes'")
    credential_id: str = Field(min_length=1, description="that tab's own row id")


class AgentCellEpisodeRequest(BaseModel):
    """Ask for one bounded window of cluster access.

    ``duration_minutes`` is how long Password Safe holds the request, which is also how
    long the account's concurrent slot stays occupied if nobody approves — so it is
    clamped rather than honoured. See ``agentcell_service.episode_duration_problem``.
    """
    duration_minutes: int = Field(
        default=15, description="clamped to 5–60; the window, not the token's TTL")


class AgentCellEpisodeResponse(BaseModel):
    """What came back from asking. Never the credential.

    ``state`` is the interesting field and ``waiting`` is the interesting value: an agent
    that cannot authorise its own access to a cluster is the whole argument, so the state
    that says so is reported rather than inferred from an absence.
    """
    id: str
    state: str
    request_id: str = ""
    summary: str = ""
    notes: list = []


class AgentCellLinkResponse(BaseModel):
    id: str
    mechanism: str = ""
    credential_id: str = ""
    summary: str = ""
    notes: List[str] = []


class AgentCellInfo(BaseModel):
    id: str
    name: str = ""
    status: str = ""
    created_by: str = ""
    created_at: str = ""
    workgroup: str = ""
    host_name: str = ""
    cloud: str = ""
    private_ip: str = ""
    trust_domain: str = ""
    spiffe_id: str = ""
    pat_name: str = ""
    pat_expires_at: str = ""
    pat_revoked_at: str = ""
    # Both playbooks ran. A cell with only one is half-wired in a way the page should say
    # out loud rather than leave to a log line.
    wired: bool = False
    stages_done: List[str] = []
    # What this agent is answerable for in the Workload Lab, and the state of that
    # credential. `link_summary` renders an expired lease as the mechanism working rather
    # than as a fault, which is the distinction workload_cloud_service.lease_state exists
    # to preserve.
    linked_mechanism: str = ""
    linked_credential_id: str = ""
    linked_summary: str = ""


class AgentCellListResponse(BaseModel):
    agents: List[AgentCellInfo]
