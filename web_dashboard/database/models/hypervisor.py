"""Hypervisor connection, VM cache and config-management route models."""
import json
import uuid
from sqlalchemy import Column, String, Integer, DateTime, Boolean, Text, ForeignKey, UniqueConstraint

from ...utils.clock import utcnow
from .._engine import Base


class HypervisorConnection(Base):
    """One reachable hypervisor management endpoint.

    Replaces the singleton ``proxmox_host`` / ``vsphere_host`` / … config keys, which
    made "N sites x M hypervisors" inexpressible — there was exactly one of each, so a
    second vCenter, or the same product at a second site, had nowhere to live.

    Two rows exist for the same reason a `Job` row does: something has to be the record.
    Every integration resolves one of these instead of reading ``settings``, and
    ``agent_id`` is what makes a connection the dashboard *cannot dial* still usable —
    a remote agent on that network does the talking.

    Credentials: exactly one of ``secret_enc`` (Fernet, via ``config_service``) or
    ``secret_ref`` (an external backend reference) is set.

    For an **agent-bound** connection the rule is narrower than "no credential", and the
    line is worth stating precisely: **the dashboard may hold the secret, never the
    target.** ``host`` and ``username`` are always NULL on such a row and always come
    from the agent's own connections.yaml, because those are the fields that *aim* the
    connection — a dashboard able to set ``host`` could redirect the agent's
    authenticated session at an endpoint of its choosing and harvest the credential on
    first use, and one able to set ``username`` could spray a known password across
    accounts. ``agent_connection_name`` remains the whole join.

    The secret itself is optional and opt-in. Left NULL, the agent resolves the
    credential locally and a dashboard compromise yields a verb and a name — the original
    behaviour. Set, the agent may instead fetch it just-in-time for one job over its own
    signed poll channel, sealed to a per-fetch key (``services/agent_sealing``), which is
    what lets an on-prem host hold no standing hypervisor credential at all. Which of the
    two applies is decided by ``dashboard_secret`` in the *customer's* connections.yaml,
    not here: this row can offer a credential, it cannot impose one.

    A ``secret_ref`` of ``ps_account://<id>`` means the dashboard holds no password
    either — it checks one out of Password Safe per job and checks it back in.
    """
    __tablename__ = "hypervisor_connections"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    kind = Column(String(16), nullable=False, index=True)   # vsphere|proxmox|nutanix|xcpng|hyperv
    name = Column(String(64), nullable=False)               # operator label
    host = Column(String(255), nullable=False, default="")
    port = Column(Integer)
    username = Column(String(255))

    secret_enc = Column(Text)              # Fernet ciphertext (config_service.encrypt_value)
    secret_ref = Column(String(256))       # aws_sm:// | azure_kv:// | gcp_sm:// | bt_safe://
    verify_ssl = Column(Boolean, default=False, nullable=False)
    # Per-kind NON-SECRET extras, JSON: vsphere datacenter, hyperv transport/use_ssl,
    # proxmox token_id, sync_interval_minutes. Allowlisted per kind by the service.
    options = Column(Text)

    # NULL = the dashboard dials this endpoint itself (the behaviour before this table).
    agent_id = Column(String(36), ForeignKey("remote_agents.id", ondelete="SET NULL"),
                      index=True, nullable=True)
    # The name this connection has in that agent's own connections.yaml. The dashboard
    # never learns the credential; this string is the entire join between the two.
    agent_connection_name = Column(String(64))

    # Grouping and display only — routing is the agent_id FK. A site join would mean
    # "any agent here can service this", but only the agent that actually holds the
    # credential can, so it would lease cleanly and then refuse.
    site = Column(String(64), index=True)

    is_default = Column(Boolean, default=False, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    last_ok_at = Column(DateTime)
    last_error = Column(Text)
    last_sync_at = Column(DateTime)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    created_by = Column(String(100))
    updated_at = Column(DateTime, default=utcnow)

    # Unique on (kind, name), NOT on name alone: "dc1" is a reasonable label for both a
    # vSphere and a Proxmox connection. Deliberately NOT unique on (kind, host, port)
    # either — two connections to one vCenter under different service accounts (a
    # read-only sync account and a privileged deploy account) is a legitimate setup.
    #
    # `is_default` is a plain boolean with a service-enforced "at most one per kind"
    # rule rather than a partial unique index: SQLite's partial-index support is not
    # something to bet the startup path on.
    __table_args__ = (UniqueConstraint("kind", "name", name="uq_hypervisor_connection_name"),)

    @property
    def options_dict(self) -> dict:
        if not self.options:
            return {}
        try:
            value = json.loads(self.options)
            return value if isinstance(value, dict) else {}
        except Exception:  # noqa: BLE001
            return {}


class HypervisorVMCache(Base):
    """VMs an agent reported for one connection.

    Identity is ``(connection_id, vm_id)`` — the hypervisor's own opaque id. It replaced
    a ``vm_state_cache`` table keyed on a VMX path *on the dashboard host*, which had no
    meaning for a VM on a customer's vCenter and none at all once the dashboard stopped
    running on the VM host.

    A read-through cache, never a source of truth. ``synced_at`` is what makes deletion
    detectable: the last page of a sync prunes rows older than the pass that started it.
    """
    __tablename__ = "hypervisor_vm_cache"

    connection_id = Column(String(36), primary_key=True)
    vm_id = Column(String(128), primary_key=True)
    name = Column(String(256))
    power_state = Column(String(32))
    vcpus = Column(Integer)
    mem_mib = Column(Integer)
    ip_addresses = Column(Text)     # JSON list
    # Text, not String(128). For a workstation connection this carries a full Windows
    # VMX path, and one longer than 128 chars raised StringDataRightTruncation inside
    # hypervisor_sync_service._upsert on PostgreSQL — which api/agent.py swallows, so an
    # entire sync page of VMs vanished with a log line and no user-visible failure.
    scope = Column(Text)            # node / cluster / datacenter, VMX path for workstation
    vm_type = Column(String(16))
    tags = Column(Text)             # JSON list
    # The hypervisor's OWN guest-OS code (`windows9-64`), not a display label. The label
    # table lives in hypervisor_view_service.guest_os_label, on the dashboard, because an
    # agent is upgraded separately and lags: a label baked into the agent would freeze at
    # whatever build the host last pulled.
    guest_os = Column(String(64))
    synced_at = Column(DateTime, default=utcnow, nullable=False, index=True)


class ConfigMgmtRoute(Base):
    """Which agent EXECUTES a Config-Management run against an address range.

    Splits "who can reach the hypervisor's management API" from "who can reach its
    guests". Before this they were forced to be the same agent — ``api/config_mgmt.py``
    required the run's agent to be the one brokering the connection the VM was synced
    from — and that is wrong for the topology agent-bound connections exist to serve.
    VMware Workstation's ``vmrest`` binds 127.0.0.1 and has no bind-address option, so
    the brokering agent must run ON the Windows host under Docker Desktop, whose WSL2 VM
    can reach no VMware vmnet at all. The agent that can read the inventory is therefore
    structurally unable to reach the VMs in it.

    Deliberately NOT a new ``hypervisor_connections`` kind. Eleven of that table's
    columns (host, port, username, secret_enc, secret_ref, verify_ssl, options,
    is_default, last_sync_at …) are meaningless here, and a new member of
    ``VALID_KINDS`` would need a ``hypervisor_view_service`` projector for something
    with no VMs to project, a ``DEFAULT_PORTS`` entry for something with no port, and a
    ``_probe`` for something that cannot be dialled.

    **Reachability is a property of the NETWORK, not of the hypervisor** — which is why
    the key is a CIDR and not a connection id. One ``vmrest`` connection routinely
    reports guests on several vmnets at once, so a per-connection executor cannot
    express "this segment is reachable from there, that one is not".

    What it may NOT do is change WHICH address a run targets. That stays pinned to one
    the discovering agent itself reported, above the routing decision in
    ``_resolve_agent_target`` — see that function.
    """
    __tablename__ = "config_mgmt_routes"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    # CASCADE is declared for the ORM's benefit; `agent_service.delete_agent` deletes
    # these rows explicitly, for the reason its own docstring gives — SQLite does not
    # enforce foreign keys unless PRAGMA foreign_keys=ON is set per connection, and
    # nothing here sets it. A dangling route would resolve runs to an id that no longer
    # exists.
    agent_id = Column(String(36), ForeignKey("remote_agents.id", ondelete="CASCADE"),
                      nullable=False, index=True)
    # The NORMALISED network, e.g. "192.168.235.0/24" — never the operator's raw input.
    # String(64) rather than 43 (39 + "/128", the true IPv6 maximum) for headroom: the
    # StringDataRightTruncation that silently ate a whole sync page in
    # HypervisorVMCache.scope is not worth re-running for four characters.
    cidr = Column(String(64), nullable=False)
    label = Column(String(64))      # "vmnet1 host-only" — display only
    is_active = Column(Boolean, default=True, nullable=False)
    # Opt-in: this range also decides which agent executes a run against a
    # dashboard-deployed CLOUD VM whose private address it covers, instead of the remote
    # worker. NULLABLE, and NULL means False: schema_migrations 0001 adds it as a bare
    # BOOLEAN with no DEFAULT (see approval_required on Job for why), so every route that
    # predates it keeps routing hypervisor guests only and an upgrade moves no cloud VM.
    # Superseded by `scope` (migration 0003). Kept so old rows keep reading; never read or
    # written now — the API derives the old meaning from `scope`.
    include_cloud_vms = Column(Boolean, nullable=True, default=False)
    # Which targets this route may govern: "onprem" (hypervisor guests) or one cloud's
    # dashboard-deployed VMs. An address alone does not name a machine — every AWS
    # region's default VPC is 172.31.0.0/16 — so a lookup only considers routes in the
    # target's own scope. See config_mgmt_route_service.
    scope = Column(String(16), nullable=False, default="onprem", server_default="onprem")
    # Narrowers, "" for none (never NULL: NULLs are distinct in a unique index). A region
    # for a cloud scope; a hypervisor connection id for onprem. The connection narrows
    # WHICH guests are covered, never who executes: `agent_id` stays the executing agent,
    # independent of the agent brokering that connection.
    scope_region = Column(String(64), nullable=False, default="", server_default="")
    scope_connection_id = Column(String(36), nullable=False, default="", server_default="")
    # Cloud scopes: one workstream's VMs (the one each was DEPLOYED in). It stands for that
    # workstream's network — two teams' VPCs in one region often share a range — and is a
    # tenancy line: one team's agent is never handed another team's VM or its key.
    scope_workstream = Column(String(100), nullable=False, default="", server_default="")
    # Cloud scopes: one network — an AWS VPC id, an Azure VNet ARM id or a GCP network —
    # read from the deploy job (config_mgmt_route_service.network_of). The exact answer
    # that region and workstream approximate. Migration 0004.
    scope_network = Column(String(200), nullable=False, default="", server_default="")
    # Cloud scopes only: when the agent is not online (and, for an agent the dashboard
    # deployed, the run is not booked into a time its host will be up), run on the remote
    # worker instead of refusing. NULL means off — migration 0007 adds a bare BOOLEAN, so
    # no existing route changes behaviour. See api/config_mgmt._worker_fallback.
    fallback_to_worker = Column(Boolean, nullable=True, default=False)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    created_by = Column(String(100))
    updated_at = Column(DateTime, default=utcnow)

    # Unique on the normalised cidr, and this IS the tiebreak. Two agents claiming one
    # range is the single ambiguity longest-prefix matching cannot resolve, so it is
    # refused at write time with a message naming the existing row rather than settled
    # by an invisible created_at comparison. Overlapping ranges of DIFFERENT prefix
    # length are legal and the longest wins, exactly like a routing table.
    __table_args__ = (UniqueConstraint("cidr", "scope", "scope_region", "scope_connection_id",
                                       "scope_workstream", "scope_network",
                                       name="uq_config_mgmt_route_scope_net"),)
