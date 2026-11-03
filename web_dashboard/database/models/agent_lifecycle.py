"""The remote-agent lifecycle models: releases, rollouts, and the cloud hosts agents run on."""
import uuid
from sqlalchemy import Column, String, Integer, DateTime, Boolean, Text, ForeignKey

from ...utils.clock import utcnow
from .._engine import Base


class AgentRelease(Base):
    """A release of the remote agent image the dashboard may offer as an upgrade target.

    A version and the image DIGEST it is, nothing more. The dashboard never verifies the
    signature: the updater beside each agent does, against the identity pinned in that
    host's policy.yaml, so a wrong or forged row here can at most offer an upgrade every
    host refuses (docs/platform/spiffe/remote-agents/lifecycle.md).
    """
    __tablename__ = "agent_releases"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    version = Column(String(32), nullable=False, unique=True, index=True)
    # Where the image is: the project's repository, or a mirror's (an air-gapped site
    # mirrors the image and pins the mirror in its policy.yaml `upgrades.repository`).
    repository = Column(String(200), nullable=False, default="chrweav/dashboard-agent")
    digest = Column(String(71), nullable=False)        # "sha256:" + 64 hex
    notes = Column(Text)
    source = Column(String(16), nullable=False, default="manual")   # manual | registry
    created_at = Column(DateTime, default=utcnow, nullable=False)
    created_by = Column(String(100))


class AgentRollout(Base):
    """One release rolled out across the eligible agents, canary ring first.

    ``status``: ``canary`` (canary upgrades queued) -> ``default`` (canary all done, the
    rest queued) -> ``done``; or ``stopped`` the moment any upgrade it queued fails, with
    ``detail`` naming it. ``job_ids`` and ``skipped`` (agent -> reason) are JSON, so the
    page can say which agent got what without a join table.
    """
    __tablename__ = "agent_rollouts"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    release_id = Column(String(36), ForeignKey("agent_releases.id"), nullable=False)
    change_window_id = Column(String(36), nullable=True)   # provenance only, like Job's
    status = Column(String(16), nullable=False, default="canary")
    canary_job_ids = Column(Text)                       # JSON list
    default_job_ids = Column(Text)                      # JSON list
    skipped = Column(Text)                              # JSON {agent name: reason}
    detail = Column(Text)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    created_by = Column(String(100))
    finished_at = Column(DateTime, nullable=True)
    # Hide on the Lifecycle panel sets this rather than deleting the row: the row is the
    # only record of which agent got which job and why the others were skipped.
    hidden_at = Column(DateTime, nullable=True)


class AgentHost(Base):
    """A remote agent the dashboard deployed into a cloud, and that host's lifecycle.

    The agent itself is an ordinary ``remote_agents`` row: it enrols or attests, leases and
    is routed to exactly like one an operator built by hand. This row is the part only a
    DEPLOYED agent has — where its host runs, the policy the dashboard wrote for it, and
    when it should exist at all:

      * ``always`` — up until someone stops or deletes it;
      * ``until``  — up until ``until_at``, then stopped for good;
      * ``window`` — "persistent just in time": started ``lead_minutes`` before each
                     occurrence of a change window and stopped ``grace_minutes`` after it
                     closes.

    **Stopping destroys the host and keeps the agent.** Nothing runs and nothing is at rest
    between windows; the agent row, its Config-Management routes and the runs booked into
    the next window all survive, and each start is a fresh host with a fresh identity
    re-bound to the same agent row. ``status`` is ``parked`` in between.

    Config Management by construction: the dashboard writes this agent's ``policy.yaml``
    at launch from ``ansible_cidrs`` and ``max_concurrent``, with ``job_types:
    [agent_ansible]`` — plus ``agent_k8s`` and a ``k8s:`` block when ``k8s_enabled``
    (services/agent_host_service).
    """
    __tablename__ = "agent_hosts"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    # One host record per agent. CASCADE is for the ORM; agent_host_service deletes the
    # row explicitly before the agent, for the same SQLite reason delete_agent gives.
    agent_id = Column(String(36), ForeignKey("remote_agents.id", ondelete="CASCADE"),
                      nullable=False, unique=True, index=True)
    name = Column(String(63), nullable=False)               # the cloud resource name
    cloud = Column(String(20), nullable=False, index=True)  # aws | azure | gcp
    region = Column(String(40), nullable=False)
    zone = Column(String(40), nullable=True)                # where it actually landed (GCP)
    # The VPC / VNet / GCP network its host runs in (config_mgmt_route_service.network_of
    # form): the agent can reach only that network, so its routes are narrowed to it.
    network = Column(String(200), nullable=True)
    subnet = Column(String(400), nullable=True)             # blank = the cloud's VM subnet
    instance_size = Column(String(64), nullable=True)       # blank = the per-cloud default

    # What the dashboard writes into the agent's policy.yaml at launch.
    ansible_cidrs = Column(Text, nullable=False, default="[]")   # JSON list of ranges
    max_concurrent = Column(Integer, nullable=False, default=1)
    # Kubernetes work (agent_k8s, and k8s Config-Management plays) for the clusters that
    # name this agent. NULL is off. Takes effect at the next start: the policy is written
    # at launch. Migration 0009.
    k8s_enabled = Column(Boolean, nullable=True)

    mode = Column(String(16), nullable=False, default="always")  # always | until | window
    until_at = Column(DateTime, nullable=True)                   # UTC, mode "until"
    change_window_id = Column(String(36), nullable=True)         # mode "window"
    lead_minutes = Column(Integer, nullable=False, default=20)
    grace_minutes = Column(Integer, nullable=False, default=15)

    # provisioning | starting | running | stopping | parked | error | deleted
    status = Column(String(32), nullable=False, default="parked", index=True)
    host_id = Column(String(200), nullable=True)            # EC2 id / VM name / GCE name
    last_job_id = Column(String(36), nullable=True)         # the latest start/stop job
    error = Column(Text, nullable=True)
    # The schedule as the last sweep computed it, for the Agents page. Display only:
    # the sweep recomputes from the window every pass.
    next_start_at = Column(DateTime, nullable=True)
    next_stop_at = Column(DateTime, nullable=True)
    started_at = Column(DateTime, nullable=True)

    created_by = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)
