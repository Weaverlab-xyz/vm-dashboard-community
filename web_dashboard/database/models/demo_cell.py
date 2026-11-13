"""Custom demo cells: blueprints a Cell Builder authors, and the cells launched from them.

The OT, network and agent cells each have one hard-wired shape. A custom cell is a
COMPOSITION -- whatever a demo needs (a GKE cluster, a MySQL database, two Ubuntu VMs) --
assembled from the provisioning paths those resources already have. So these tables hold
only the recipe and the membership; every member stays an ordinary row of its own kind (a
deploy ``Job``, a ``K8sCluster``, a ``CloudDatabase``) with its own Destroy, inventory and
auto-delete timer, and the cell just knows which rows are its.

The blueprint is to a cell what a POV blueprint is to a POV (``PovBlueprint``), with one
difference that is the reason this is a new table rather than more columns there: a POV
blueprint is a set of saved form DEFAULTS naming one template, while a cell blueprint IS
the resource list. Its items are frozen into the cell at launch (``blueprint_snapshot``),
so editing or deleting a blueprint can never change, or strand, a cell already running.

Plain string ids and no ForeignKeys, as elsewhere in this package: integrity is kept in
``services/custom_cell_service``, which is the only writer of all four tables.
"""
import json
import uuid

from sqlalchemy import Column, DateTime, Float, Integer, String, Text

from ...utils.clock import utcnow
from .._engine import Base


def _loads(raw, default):
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:  # noqa: BLE001
        return default


class CellBlueprint(Base):
    """A reusable recipe for a demo cell. Authored by a Cell Builder (`demo_blueprints`)."""
    __tablename__ = "cell_blueprints"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(120), nullable=False)
    description = Column(Text, nullable=True)
    # Who may see and launch it. Blank = everyone who can open the hub's blueprint list.
    workgroup = Column(String(100), nullable=True, index=True)
    created_by = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)

    # ── Defaults a launch starts from (the launcher may override each) ───────────
    # NULL ttl = the auto-delete policy's default for a cell.
    ttl_hours = Column(Float, nullable=True)
    # The recurring suspend schedule, same column names and validator as Job and
    # PovEnvironment (services/suspend_schedule.validate).
    suspend_at_local = Column(String(5), nullable=True)
    resume_at_local = Column(String(5), nullable=True)
    schedule_timezone = Column(String(64), nullable=True)
    schedule_days = Column(String(32), nullable=True)


class CellBlueprintItem(Base):
    """One resource in a blueprint: a VM (``count`` of them), a k8s cluster or a database.

    ``spec`` is the cloud's own create request minus what the cell supplies itself (the
    name and the workgroup), validated against that request model when saved and again
    at launch -- ``custom_cell_service.validate_item``.
    """
    __tablename__ = "cell_blueprint_items"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    blueprint_id = Column(String(36), nullable=False, index=True)
    sort_order = Column(Integer, nullable=False, default=0)
    kind = Column(String(16), nullable=False)          # vm | k8s | database
    cloud = Column(String(16), nullable=False)         # aws | azure | gcp
    region = Column(String(64), nullable=True)
    # Short handle used to name the member ("web" -> "<cell>-web-1").
    label = Column(String(40), nullable=False)
    count = Column(Integer, nullable=False, default=1)  # VMs only; 1 for everything else
    spec = Column(Text, nullable=True)

    @property
    def spec_dict(self) -> dict:
        return _loads(self.spec, {})

    @spec_dict.setter
    def spec_dict(self, value: dict):
        self.spec = json.dumps(value or {})


class CustomCell(Base):
    """A launched cell: the frozen recipe, its lifecycle, and its timers."""
    __tablename__ = "custom_cells"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(60), nullable=False)
    blueprint_id = Column(String(36), nullable=True)   # lineage only; may be deleted since
    blueprint_name = Column(String(120), nullable=True)
    # The items as launched: [{kind, cloud, region, label, count, spec}, ...].
    blueprint_snapshot = Column(Text, nullable=True)
    workgroup = Column(String(100), nullable=True, index=True)
    created_by = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)

    # scheduled -> provisioning -> active <-> suspended -> destroying -> destroyed,
    # or failed (members are kept so Destroy can still clean up).
    status = Column(String(20), nullable=False, default="scheduled", index=True)
    status_message = Column(Text, nullable=True)
    # When it is booked to start building (NULL = it was launched "now").
    scheduled_for = Column(DateTime, nullable=True)
    provision_job_id = Column(String(36), nullable=True)
    destroy_job_id = Column(String(36), nullable=True)

    # The end of the demo. Destroyed by the change-window sweep at this instant (as the
    # presenter), with the auto-delete reaper as the backstop on the same instant.
    expires_at = Column(DateTime, nullable=True, index=True)
    expiry_warned_at = Column(DateTime, nullable=True)
    warned_stage_minutes = Column(Integer, nullable=True)

    suspend_at_local = Column(String(5), nullable=True)
    resume_at_local = Column(String(5), nullable=True)
    schedule_timezone = Column(String(64), nullable=True)
    schedule_days = Column(String(32), nullable=True)
    schedule_last_checked_at = Column(DateTime, nullable=True)

    extra_data = Column(Text, nullable=True)

    @property
    def snapshot(self) -> list:
        return _loads(self.blueprint_snapshot, [])

    @snapshot.setter
    def snapshot(self, value: list):
        self.blueprint_snapshot = json.dumps(value or [])

    @property
    def extra(self) -> dict:
        return _loads(self.extra_data, {})

    @extra.setter
    def extra(self, value: dict):
        self.extra_data = json.dumps(value or {})


class CustomCellMember(Base):
    """One resource a cell created, pointing at the row of its own kind.

    ``ref_id`` is the deploy ``Job.id`` for a VM, ``K8sCluster.id`` for a cluster and
    ``CloudDatabase.id`` for a database -- the same ids their own pages and Destroy
    buttons use, so a member is never managed through anything but its own machinery.
    """
    __tablename__ = "custom_cell_members"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    cell_id = Column(String(36), nullable=False, index=True)
    kind = Column(String(16), nullable=False)
    cloud = Column(String(16), nullable=False)
    label = Column(String(80), nullable=False)
    ref_id = Column(String(36), nullable=True, index=True)
    job_id = Column(String(36), nullable=True)          # the provision job, for its log
    # pending | provisioning | ready | failed | destroying | destroyed
    status = Column(String(20), nullable=False, default="pending")
    power_state = Column(String(16), nullable=True)     # running | stopped
    last_error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)
