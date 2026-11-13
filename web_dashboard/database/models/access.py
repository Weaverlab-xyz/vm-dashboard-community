"""Access models: OAuth group mappings, access roles, workgroups, overrides, approvals, Entitle activations."""
import json
import uuid
from sqlalchemy import Column, String, Integer, DateTime, Boolean, Text, ForeignKey, UniqueConstraint
from sqlalchemy.orm import relationship

from ...utils.clock import utcnow
from .._engine import Base


class OAuthGroupMapping(Base):
    """Maps an Entra ID group Object ID to a dashboard workgroup name."""
    __tablename__ = "oauth_group_mappings"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    entra_group_id = Column(String(255), unique=True, nullable=False, index=True)
    display_name = Column(String(200), nullable=False)   # friendly label shown in the UI
    workgroup = Column(String(100), nullable=False)       # must match a key in settings.workgroups
    # Default permissions for auto-created users from this group. NULL = all permissions.
    default_permissions = Column(Text, nullable=True)

    # The focus this group confers, and the tie-break when a user matches several
    # mappings. Lowest priority number wins; NULL persona means this mapping expresses no
    # opinion, which is what lets a broad catch-all group grant a workgroup without also
    # dictating a focus. Ties and absent priorities fall back to the mapping's
    # display_name so the outcome is always deterministic rather than row-order dependent.
    persona = Column(String(32), nullable=True)
    persona_priority = Column(Integer, nullable=True)
    # The access role every member of this group is granted, unioned into their
    # `session_permissions` at login alongside `default_permissions` above. NULL = this
    # mapping confers no role, which is every mapping that predates the feature.
    #
    # Only an id, and deliberately no materialised copy of the map -- unlike `users`. The
    # column it feeds is rebuilt from scratch on EVERY login, so there is nothing to keep
    # in step and nothing to reconcile; the login path reads the role directly. The flip
    # side is that a role edit reaches group members at their next sign-in rather than
    # immediately, which is exactly how `default_permissions` has always behaved.
    # Plain column, no ForeignKey -- see the note on `users.role_id` for both reasons. This
    # one is not in the cycle, but an assignment that is constrained on one table and not
    # the other is a distinction no reader would guess and no test could hold.
    role_id = Column(String(36), nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)


class AccessRole(Base):
    """A named, reusable permission map that a user or a group mapping is assigned.

    `AccessRole` and not `Role` because three unrelated `role` columns already exist in this
    module -- `EntitleActivation.role` (an IAM policy name), `SpireLab.k8s_workload_role` and
    `PovCloudTemplateVM.role` ("broker"|"target"). Only this one is about dashboard RBAC.

    **`permissions` here inverts the meaning it has on `User`, and that is the single most
    important thing to know before editing this model.** On a user, NULL/`{}` means
    UNRESTRICTED. On a role it means the role grants NOTHING -- a role is a positive
    statement about access, so "no map" cannot sensibly mean "all access", and an
    unrestricted role would silently hand every assignee every scope added in future. The
    create endpoint therefore REQUIRES the field rather than defaulting it, and the Roles
    tab does not offer the grid's "Full access (unrestricted)" escape hatch at all.
    "Everything" is spelled with the admin flag, which is auditable.

    `slug` is the stable handle the built-in seeds are keyed by; it is derived from `name`,
    so a duplicate name yields a duplicate slug and a 409 without a second constraint.
    `is_builtin` can be a real Boolean with a default because this is a NEW table made by
    `create_all` -- the `BOOLEAN DEFAULT 0` trap only bites an `ALTER TABLE` in the
    legacy list (schema_legacy.py), where SQLite accepts an integer default on a boolean
    and PostgreSQL rejects it, silently, inside its own savepoint.
    """
    __tablename__ = "access_roles"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    slug = Column(String(64), unique=True, nullable=False, index=True)
    name = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    permissions = Column(Text, nullable=True)
    is_builtin = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)
    created_by_user_id = Column(String(36), ForeignKey("users.id", ondelete="SET NULL"),
                                nullable=True)

    @property
    def permissions_dict(self) -> dict:
        """The map this role grants. Empty means it grants nothing -- see the class
        docstring for why that is the opposite of `User.permissions_dict`."""
        if not self.permissions:
            return {}
        try:
            return json.loads(self.permissions)
        except Exception:
            return {}

    @permissions_dict.setter
    def permissions_dict(self, value: dict):
        self.permissions = json.dumps(value) if value else None


class UserAccessRole(Base):
    """One access role a user holds directly. A user may hold several.

    The record of WHICH roles, for the editor, the assignee lists and the delete guard.
    It is never read to decide access: `users.role_permissions` stays the one materialised
    copy -- now the UNION of every role here -- because `effective_permissions_dict` is
    read on a DETACHED User (`api/mcp_server`), where a query or relationship would raise.
    `users.role_id` stays too, as the first-assigned role and the "holds a role" marker
    the `_ROLE_UNRESOLVED` guard reads.

    `services/role_service.apply_roles_to_user` is the only writer of this table and of
    those two columns, and writes all three together.

    No ForeignKeys, for the reasons on `users.role_id` (identity.py): a constraint that
    exists on some installs and not others, tested on a SQLite that enforces none. The
    integrity rules live in role_service: `delete` strips the role from every holder and
    `reconcile` drops rows whose role or user is gone.
    """
    __tablename__ = "user_access_roles"
    __table_args__ = (UniqueConstraint("user_id", "role_id", name="uq_user_access_role"),)

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(36), nullable=False, index=True)
    role_id = Column(String(36), nullable=False, index=True)
    # Ordering only: the earliest row is the one mirrored into `users.role_id`.
    position = Column(Integer, nullable=False, default=0)
    assigned_at = Column(DateTime, default=utcnow, nullable=False)


class Workgroup(Base):
    """User-managed workgroup: scopes RBAC + cloud resource visibility.

    `name` is canonical lowercase (regex enforced in service layer) so it can be
    written verbatim into AWS instance tags (`Workgroup=<name>`), Azure resource
    tags (`workgroup=<name>`), and GCP labels (`workgroup=<name>`) — all of which
    have casing/character restrictions tighter than the dashboard UI.

    Lookups in `workgroup_service` are case-insensitive so existing TitleCase
    strings in `users.workgroups` and `oauth_group_mappings.workgroup` keep
    resolving without a data migration. `display_name` preserves the original
    casing for UI rendering.
    """
    __tablename__ = "workgroups"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(64), unique=True, nullable=False, index=True)
    display_name = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    local_vm_path = Column(Text, nullable=True)  # UNC path for VMware local VMs; null in community
    is_default = Column(Boolean, default=False, nullable=False, index=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    created_by_user_id = Column(String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    # ── Required change window (services/admission_service._enforce_change_window) ──
    # "Nothing in this workgroup may be changed outside Prod Weekend." Unlike the
    # per-run picker, which an operator chooses, this one cannot be bypassed by
    # clicking Run now — the gate every mutating endpoint already calls refuses and
    # offers to book instead.
    #
    # BOTH NULL is what every existing row backfills to, and NULL means "as today":
    # no constraint, no behaviour change. A workgroup becomes constrained only when an
    # administrator both picks a window and ticks the requirement, so enabling the
    # feature on a live estate blocks nothing by construction rather than by a guard.
    #
    # A plain id rather than a ForeignKey, for the same reason `Job.change_window_id`
    # is: an administrator tidying up the maintenance calendar must never cascade into
    # a workgroup's configuration. A window that no longer exists reads as "misconfigured
    # — pick another", which is what the Workgroups tab shows.
    change_window_id = Column(String(36), nullable=True)
    # Bare BOOLEAN, NO DEFAULT, deliberately. PostgreSQL has rejected a defaulted ADD
    # COLUMN in this tree's migration list before and the savepoint swallowed it
    # silently — see the `users.is_admin` note. NULL reads as False everywhere.
    require_change_window = Column(Boolean, nullable=True)
    # The workstream's deploy defaults (services/workgroup_service.PROFILE_KEYS), as a
    # JSON object holding only the keys it sets. NULL = sets none: every VM it deploys
    # takes the global defaults, which is every workstream that predates the column.
    deploy_defaults = Column(Text, nullable=True)
    # Secret REFERENCES bound to the workstream by an administrator
    # (services/workgroup_service.SECRET_CLOUDS): the SSH key secret per cloud and a PRA
    # credential. Never a value. Kept apart from deploy_defaults because members edit
    # those and only an administrator may edit these. NULL = none bound.
    secret_bindings = Column(Text, nullable=True)
    # Config-Management assets an administrator chose to run on every VM deployed into
    # the workstream, in order (services/post_deploy_service): a JSON list of
    # {asset, asset_backend}. Administrator-only like secret_bindings, because each one
    # is code run on every new machine. NULL = none.
    post_deploy_runs = Column(Text, nullable=True)
    # Password Safe attributes every managed system onboarded from the workstream carries
    # (services/ps_workstream_attrs), so a customer's Smart Rule can filter on
    # "Workstream = X": a JSON object {"<AttributeType name>": "<value>"}. Administrator-
    # only like post_deploy_runs, because it writes into a customer's PAM tenant. NULL =
    # none, which is every workstream that predates the column.
    ps_attributes = Column(Text, nullable=True)
    # The subnet / security groups its deploy forms start from, per cloud and region
    # (services/workgroup_service.NETWORK_KEYS): {cloud: {region: {key: value}}}. A form
    # convenience, never applied by the server. NULL = none.
    network_defaults = Column(Text, nullable=True)


class StorageAssetScope(Base):
    """A stored asset an administrator has limited to one workstream
    (services/asset_scope_service).

    Assets live in buckets, not here, so this is a row per scoped asset keyed the way
    the Storage page names one: backend + name. No row = global, which is every asset
    that predates the table — creating it scopes nothing. A plain workgroup string, no
    ForeignKey, for the reason the cloud-database column has none; deleting a
    workstream that still scopes assets is refused instead (workgroup_service.delete).
    """
    __tablename__ = "storage_asset_scopes"

    backend = Column(String(32), primary_key=True)
    name = Column(String(255), primary_key=True)
    workgroup = Column(String(64), nullable=False, index=True)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)
    updated_by = Column(String(100), nullable=True)


class VMWorkgroupOverride(Base):
    """Workgroup assignments for VMs the dashboard didn't deploy itself.

    Cloud and Proxmox/Nutanix dashboard-driven deploys already record
    `workgroup` on the corresponding Job row, but VMs that pre-existed on the
    on-prem hypervisor — or live on a hypervisor with no deploy flow at all
    (Hyper-V, vSphere, XCP-ng) — have no Job to hang a workgroup off of.
    An admin assigns those via the bulk-assign action on the provider page,
    which writes a row here.

    `vm_id` is normalized per-provider in each *_service module's
    _override_key() helper: Proxmox uses "<node>/<vmid>", everything else
    uses the VM's native uuid/moref.
    """
    __tablename__ = "vm_workgroup_overrides"

    id = Column(Integer, primary_key=True)
    provider = Column(String(20), nullable=False, index=True)   # proxmox|nutanix|hyperv|vsphere|xcpng
    vm_id = Column(String(128), nullable=False)
    workgroup = Column(String(64), ForeignKey("workgroups.name", ondelete="CASCADE"), nullable=False, index=True)
    created_by = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    __table_args__ = (UniqueConstraint("provider", "vm_id", name="uq_vm_workgroup_override"),)


class Approval(Base):
    """Entitle-brokered approval workflow state for gated endpoints.

    A row is created when a user calls a gated endpoint without an approval
    header; Entitle webhook moves status pending→approved/denied; the dep
    consumes the row on the user's retry call.
    """
    __tablename__ = "approvals"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    entitle_request_id = Column(String(255), nullable=False, unique=True, index=True)
    action = Column(String(100), nullable=False, index=True)
    user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    payload_hash = Column(String(64), nullable=False)
    # status values: pending | approved | denied | expired | consumed
    status = Column(String(20), nullable=False, default="pending", index=True)
    requested_at = Column(DateTime, default=utcnow, nullable=False)
    approved_at = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=False)
    consumed_at = Column(DateTime, nullable=True)
    denial_reason = Column(Text, nullable=True)

    # principal_kind: "user" for human-facing approvals (the original flow),
    # "machine" for cloud-identity JIT elevations issued by the dashboard
    # on its own behalf. Webhook handler is shared; this column lets policy
    # routing fork on identity type.
    principal_kind = Column(String(16), nullable=False, default="user", index=True)

    user = relationship("User")


class EntitleActivation(Base):
    """Per-cloud-write machine-identity elevation issued via Entitle.

    Phase 0 of the cloud-identity JIT design ships this table empty. When
    ``cloud_identity_gate_enabled`` is False (default), ``cloud_identity_service``
    short-circuits and no rows are inserted. When the gate is on, every
    write-path cloud SDK call is preceded by an elevation request whose
    lifecycle is tracked here.

    Internal ``status`` values:
      - pending     — request submitted to Entitle, awaiting workflow
      - granted     — Entitle agent has finished granting cloud-side IAM;
                      dashboard may proceed to call the cloud SDK
      - denied      — Entitle workflow rejected the request (security alert)
      - failed      — cloud-side IAM call failed (operator alert)
      - revoked     — TTL elapsed or explicit revoke (terminal)
    """
    __tablename__ = "entitle_activations"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    cloud = Column(String(16), nullable=False, index=True)        # aws | azure | gcp
    operation = Column(String(64), nullable=False, index=True)    # e.g. "aws:ec2:deploy"
    role = Column(String(255), nullable=True)                     # IAM policy / role / binding granted
    requester_user_id = Column(String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    entitle_request_id = Column(String(255), nullable=True, unique=True, index=True)
    entitle_policy_id = Column(String(255), nullable=True)
    auto_approved = Column(Boolean, nullable=False, default=True)
    status = Column(String(20), nullable=False, default="pending", index=True)
    denial_reason = Column(Text, nullable=True)
    payload_hash = Column(String(64), nullable=False)
    # Which tenant the elevation belongs to. cloud_identity_service passes this
    # into the constructor unconditionally, so its absence made every elevation
    # raise TypeError the moment cloud_identity_gate_enabled was turned on.
    tenant_id = Column(String(64), nullable=True, index=True)
    requested_at = Column(DateTime, default=utcnow, nullable=False)
    granted_at = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=True)
    revoked_at = Column(DateTime, nullable=True)
