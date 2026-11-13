"""Custom demo cells: blueprint authoring now; launch, lifecycle and teardown build on it.

A Cell Builder writes a BLUEPRINT -- an ordered list of items, each a VM (``count`` of
them), a Kubernetes cluster or a cloud database, each on its own cloud and region. A
Demo Presenter launches a CELL from it. This module is the single writer of the four
tables in ``database/models/demo_cell.py``.

**What an item's spec is.** The cloud's own create request -- ``GCPDeployRequest``,
``DeployRequest`` (AWS), ``AzureDeployRequest``, ``ClusterProvisionRequest``,
``cloud_databases.ProvisionRequest`` -- minus the fields the cell supplies itself: the
resource's name and its workgroup. ``validate_item`` builds that request model with the
cell's fields filled in, so a blueprint is refused for exactly what the cloud's own form
would refuse, and is re-checked at launch because a model can tighten between the two.

**What a spec may not carry** (``_FORBIDDEN``). Fields that point a deploy at a
credential or an integration target by NAME -- an SSH key secret, a PRA credential ref, a
Jumpoint or Asset Group override, a Password Safe vault account group, a managed
directory. A blueprint is authored once and launched by other people, so a reference
baked into it would let its author choose which secret every later launch uses. Those
resolve from configuration at launch, exactly as a plain deploy does without an override.
Scheduling fields go too: a CELL is booked as a whole, never an item.

Service-to-service imports are function-local, the local idiom (see
``k8s_service``): every module-level import here is one more symbol each test stub of
this module's importers would have to grow.
"""
import logging
import re
import uuid
from typing import Iterable, List, Optional

from sqlalchemy.orm import Session

from ..database import CellBlueprint, CellBlueprintItem
from ..utils.clock import utcnow

logger = logging.getLogger(__name__)

KINDS = ("vm", "k8s", "database")
CLOUDS = ("aws", "azure", "gcp")
MAX_ITEMS = 20
MAX_VM_COUNT = 10

LABEL_RE = re.compile(r"^[a-z][a-z0-9-]{0,19}$")

# Fields a blueprint spec may not set. See the module docstring for why.
_FORBIDDEN = frozenset({
    # credential / integration-target references
    "ssh_key_secret_override", "pra_credential_ref", "docker_deploy_key_ref",
    "jump_group", "jumpoint_name", "vault_account_group_id", "ad_directory_id",
    # the cell books itself as a whole
    "run_at", "run_timezone", "change_window_id", "ttl_hours",
    "suspend_at_local", "resume_at_local", "schedule_timezone", "schedule_days",
    # the cell supplies these
    "workgroup", "count",
})
# The name field each request model calls its resource's name -- the cell supplies it.
_NAME_FIELD = {
    ("vm", "gcp"): "instance_name", ("vm", "aws"): "instance_name",
    ("vm", "azure"): "vm_name", ("k8s", None): "name", ("database", None): "name",
}
# Where a VM request takes the item's region, when it takes one at all. GCE takes a ZONE,
# which stays in the spec; the item's region is then descriptive.
_VM_REGION_FIELD = {"aws": "region", "azure": "location"}


class CellError(ValueError):
    """A refusal the API turns into a 4xx with this message."""


# ── Item validation ───────────────────────────────────────────────────────────

def _request_model(kind: str, cloud: str):
    if kind == "vm":
        if cloud == "gcp":
            from ..models.gcp import GCPDeployRequest
            return GCPDeployRequest
        if cloud == "aws":
            from ..models.aws import DeployRequest
            return DeployRequest
        from ..models.azure import AzureDeployRequest
        return AzureDeployRequest
    if kind == "k8s":
        from ..models.k8s import ClusterProvisionRequest
        return ClusterProvisionRequest
    # Defined in the router module; imported lazily so this service stays importable
    # without FastAPI's router graph.
    from ..api.cloud_databases import ProvisionRequest
    return ProvisionRequest


def name_field(kind: str, cloud: str) -> str:
    return _NAME_FIELD.get((kind, cloud)) or _NAME_FIELD[(kind, None)]


def build_request(kind: str, cloud: str, region: str, spec: dict, *, name: str,
                  workgroup: str):
    """The cloud's own request model for one resource of this item. Raises CellError.

    The ONE place an item becomes a request, used both to validate a blueprint and, at
    launch, to build what each provisioning path is handed.
    """
    model = _request_model(kind, cloud)
    fields = dict(spec or {})
    fields[name_field(kind, cloud)] = name
    fields["workgroup"] = workgroup
    if kind == "vm":
        rf = _VM_REGION_FIELD.get(cloud)
        if rf and region and not fields.get(rf):
            fields[rf] = region
    else:
        fields["cloud"] = cloud
        fields["region"] = region
    try:
        return model(**fields)
    except Exception as exc:  # noqa: BLE001 — pydantic.ValidationError, worded below
        raise CellError(f"{kind} on {cloud}: {_short_validation(exc)}") from exc


def _short_validation(exc) -> str:
    from pydantic import ValidationError
    if isinstance(exc, ValidationError):
        try:
            parts = []
            for e in exc.errors()[:4]:
                loc = ".".join(str(x) for x in e.get("loc", ()))
                parts.append(f"{loc}: {e.get('msg', 'invalid')}")
            return "; ".join(parts) or str(exc)
        except Exception:  # noqa: BLE001
            pass
    return str(exc)


def validate_item(item: dict) -> dict:
    """Check one blueprint item and return its stored form. Raises CellError.

    ``item`` is ``{kind, cloud, region, label, count, spec}``. The returned ``spec`` keeps
    only the keys the author sent, normalised through the request model, so a later change
    to a model's DEFAULT reaches every blueprint instead of being frozen into it.
    """
    kind = (item.get("kind") or "").strip().lower()
    cloud = (item.get("cloud") or "").strip().lower()
    region = (item.get("region") or "").strip()
    label = (item.get("label") or "").strip().lower()
    spec = item.get("spec") or {}
    if kind not in KINDS:
        raise CellError(f"Unknown item kind {kind!r}: use one of {', '.join(KINDS)}.")
    if cloud not in CLOUDS:
        raise CellError(f"Unknown cloud {cloud!r}: use one of {', '.join(CLOUDS)}.")
    if not LABEL_RE.match(label):
        raise CellError(
            f"Label {label!r} must be 1-20 characters: lowercase letters, digits and "
            "hyphens, starting with a letter. It becomes part of each resource's name.")
    if not isinstance(spec, dict):
        raise CellError(f"{label}: spec must be an object.")
    bad = sorted(set(spec) & _FORBIDDEN | ({name_field(kind, cloud)} & set(spec)))
    if bad:
        raise CellError(
            f"{label}: a blueprint cannot set {', '.join(bad)}. Names and the workstream come "
            "from the cell; credential and integration overrides resolve from "
            "configuration at launch; the cell is scheduled as a whole.")
    if kind != "vm" and not region:
        raise CellError(f"{label}: a {kind} item needs a region.")
    try:
        raw = item.get("count")
        count = 1 if raw in (None, "") else int(raw)
    except (TypeError, ValueError):
        raise CellError(f"{label}: count must be a whole number.")
    if kind != "vm" and count != 1:
        raise CellError(f"{label}: only VM items take a count; each {kind} item is one.")
    if not 1 <= count <= MAX_VM_COUNT:
        raise CellError(f"{label}: count must be 1-{MAX_VM_COUNT}.")
    if kind == "database":
        from . import cloud_database_service
        engine = (spec.get("engine") or "").strip().lower()
        if engine not in cloud_database_service.VALID_ENGINES:
            raise CellError(f"{label}: engine must be one of "
                            f"{', '.join(sorted(cloud_database_service.VALID_ENGINES))}.")

    req = build_request(kind, cloud, region, spec, name=f"validate-{label}",
                        workgroup="validate")
    dumped = req.model_dump()
    clean = {k: dumped[k] for k in spec if k in dumped}
    unknown = sorted(set(spec) - set(dumped))
    if unknown:
        raise CellError(f"{label}: unknown field(s) for a {kind} on {cloud}: "
                        f"{', '.join(unknown)}.")
    return {"kind": kind, "cloud": cloud, "region": region, "label": label,
            "count": count, "spec": clean}


def validate_items(items) -> List[dict]:
    if not isinstance(items, list) or not items:
        raise CellError("A blueprint needs at least one item.")
    if len(items) > MAX_ITEMS:
        raise CellError(f"A blueprint holds at most {MAX_ITEMS} items.")
    out = [validate_item(i if isinstance(i, dict) else {}) for i in items]
    labels = [i["label"] for i in out]
    dupes = sorted({x for x in labels if labels.count(x) > 1})
    if dupes:
        raise CellError(f"Item labels must be unique; repeated: {', '.join(dupes)}.")
    return out


def validate_schedule(fields: dict) -> dict:
    """The suspend schedule in its stored form (``suspend_schedule.validate``)."""
    from . import suspend_schedule
    try:
        return suspend_schedule.validate(
            suspend_at=fields.get("suspend_at_local") or "",
            resume_at=fields.get("resume_at_local") or "",
            tz_name=fields.get("schedule_timezone") or "",
            days=fields.get("schedule_days") or "")
    except suspend_schedule.ScheduleError as exc:
        raise CellError(f"Suspend schedule: {exc}") from exc


def validate_ttl(ttl) -> Optional[float]:
    if ttl in (None, ""):
        return None
    try:
        hours = float(ttl)
    except (TypeError, ValueError):
        raise CellError("ttl_hours must be a number of hours.")
    if not 0 < hours <= 720:
        raise CellError("ttl_hours must be more than 0 and at most 720 (30 days).")
    return hours


# ── Blueprint CRUD ────────────────────────────────────────────────────────────

def _visible(bp: CellBlueprint, accessible: Optional[Iterable[str]]) -> bool:
    if accessible is None:
        return True
    return not bp.workgroup or bp.workgroup.lower() in set(accessible)


def items_of(db: Session, blueprint_id: str) -> List[CellBlueprintItem]:
    return (db.query(CellBlueprintItem)
            .filter(CellBlueprintItem.blueprint_id == blueprint_id)
            .order_by(CellBlueprintItem.sort_order).all())


def serialize(db: Session, bp: CellBlueprint, *, items: bool = True) -> dict:
    out = {
        "id": bp.id, "name": bp.name, "description": bp.description or "",
        "workgroup": bp.workgroup or "", "created_by": bp.created_by or "",
        "created_at": bp.created_at.isoformat() if bp.created_at else "",
        "updated_at": bp.updated_at.isoformat() if bp.updated_at else "",
        "ttl_hours": bp.ttl_hours,
        "suspend_at_local": bp.suspend_at_local or "",
        "resume_at_local": bp.resume_at_local or "",
        "schedule_timezone": bp.schedule_timezone or "",
        "schedule_days": bp.schedule_days or "",
    }
    rows = items_of(db, bp.id)
    out["item_count"] = len(rows)
    out["summary"] = [f"{r.count}x {r.kind}" if r.count > 1 else r.kind for r in rows]
    if items:
        out["items"] = [{"kind": r.kind, "cloud": r.cloud, "region": r.region or "",
                         "label": r.label, "count": r.count, "spec": r.spec_dict}
                        for r in rows]
    return out


def list_blueprints(db: Session, accessible: Optional[Iterable[str]]) -> List[dict]:
    rows = db.query(CellBlueprint).order_by(CellBlueprint.name).all()
    acc = None if accessible is None else {a.lower() for a in accessible}
    return [serialize(db, bp, items=False) for bp in rows if _visible(bp, acc)]


def get_blueprint(db: Session, blueprint_id: str,
                  accessible: Optional[Iterable[str]]) -> Optional[CellBlueprint]:
    """The blueprint, or None when it is missing OR outside the caller's workgroups --
    the two answer the same 404 (docs: ownership guards answer 404)."""
    bp = db.query(CellBlueprint).filter(CellBlueprint.id == blueprint_id).first()
    acc = None if accessible is None else {a.lower() for a in accessible}
    if bp is None or not _visible(bp, acc):
        return None
    return bp


def _write_items(db: Session, bp: CellBlueprint, items: List[dict]) -> None:
    for old in items_of(db, bp.id):
        db.delete(old)
    for pos, it in enumerate(items):
        row = CellBlueprintItem(id=str(uuid.uuid4()), blueprint_id=bp.id, sort_order=pos,
                                kind=it["kind"], cloud=it["cloud"],
                                region=it["region"] or None, label=it["label"],
                                count=it["count"])
        row.spec_dict = it["spec"]
        db.add(row)


def _apply_fields(bp: CellBlueprint, data: dict) -> None:
    if "name" in data:
        name = (data.get("name") or "").strip()
        if not name or len(name) > 120:
            raise CellError("A blueprint needs a name of at most 120 characters.")
        bp.name = name
    if "description" in data:
        bp.description = (data.get("description") or "").strip() or None
    if "ttl_hours" in data:
        bp.ttl_hours = validate_ttl(data.get("ttl_hours"))
    sched_keys = ("suspend_at_local", "resume_at_local", "schedule_timezone", "schedule_days")
    if any(k in data for k in sched_keys):
        merged = {k: data.get(k, getattr(bp, k)) for k in sched_keys}
        for k, v in validate_schedule(merged).items():
            setattr(bp, k, v)


def create_blueprint(db: Session, data: dict, *, created_by: str,
                     workgroup: Optional[str]) -> CellBlueprint:
    items = validate_items(data.get("items"))
    bp = CellBlueprint(id=str(uuid.uuid4()), created_by=created_by,
                       workgroup=(workgroup or "").strip().lower() or None, name="")
    _apply_fields(bp, {"name": data.get("name"), **{k: v for k, v in data.items()
                                                    if k not in ("name", "items")}})
    db.add(bp)
    _write_items(db, bp, items)
    db.commit()
    db.refresh(bp)
    return bp


def update_blueprint(db: Session, bp: CellBlueprint, data: dict) -> CellBlueprint:
    """Partial update. ``items``, when sent, replaces the whole list. Cells already
    launched hold their own snapshot, so nothing running changes."""
    items = validate_items(data["items"]) if "items" in data else None
    _apply_fields(bp, {k: v for k, v in data.items() if k != "items"})
    if items is not None:
        _write_items(db, bp, items)
    bp.updated_at = utcnow()
    db.commit()
    db.refresh(bp)
    return bp


def delete_blueprint(db: Session, bp: CellBlueprint) -> None:
    for row in items_of(db, bp.id):
        db.delete(row)
    db.delete(bp)
    db.commit()


# ══ Cells: launch, provision, roll-up, teardown ═══════════════════════════════
#
# A cell owns no infrastructure of its own. Each member is created by calling the CLOUD'S
# OWN create handler -- ``api/gcp.deploy_instance``, ``api/aws.deploy_ami``,
# ``api/azure.deploy_vm``, ``api/k8s.provision_cluster``,
# ``api/cloud_databases.provision_database`` -- with the launcher as the actor, the way
# ``api/demo._destroy_vm`` calls the cloud's destroy handler. Workgroup membership,
# secret bindings, the admission policy gate, change-window approval and the job
# metadata each runner expects all come with it, unchanged; the only thing skipped is the
# route's own `<cloud>:write` dependency, which the cell route replaces with `demo:write`
# (api/demo_gates: the presenter's grant is the cell, not the cloud).
#
# Teardown is each member's own: the destroy job ``expiry_policy.build_destroy_metadata``
# builds for a VM (the same one the reaper and demo_schedule create) and
# ``start_decommission`` for a cluster or a database.
#
# Timers: the CELL's ``expires_at`` is the end of the demo, acted on by ``sweep`` (run by
# the change-window sweep, as ``demo_schedule.sweep`` is) as the presenter. Every member
# also carries its own ``expires_at`` set to that end plus ``BACKSTOP`` -- overwriting the
# kind's own default, which could otherwise reap a member of a three-day cell after one --
# so if the cell row were ever lost the ordinary auto-delete reaper still collects it.

from datetime import timedelta  # noqa: E402

from ..database import CustomCell, CustomCellMember  # noqa: E402

BACKSTOP = timedelta(hours=24)
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,23}$")
LIVE = ("scheduled", "provisioning", "active", "suspended", "failed", "destroying")
_VM_JOB_TYPES = {"gcp": "gce_deploy", "aws": "ec2_deploy", "azure": "azure_deploy"}


def _now():
    return utcnow()


def cell_visible(cell: CustomCell, accessible: Optional[Iterable[str]]) -> bool:
    if accessible is None:
        return True
    return (cell.workgroup or "").lower() in {a.lower() for a in accessible}


def get_cell(db: Session, cell_id: str,
             accessible: Optional[Iterable[str]]) -> Optional[CustomCell]:
    cell = db.query(CustomCell).filter(CustomCell.id == cell_id).first()
    if cell is None or not cell_visible(cell, accessible):
        return None
    return cell


def members_of(db: Session, cell_id: str) -> List[CustomCellMember]:
    return (db.query(CustomCellMember).filter(CustomCellMember.cell_id == cell_id)
            .order_by(CustomCellMember.created_at).all())


def _resource_names(cell_name: str, item: dict) -> List[str]:
    if item["kind"] == "vm":
        return [f"{cell_name}-{item['label']}-{n + 1}" for n in range(item["count"])]
    return [f"{cell_name}-{item['label']}"]


def plan_end(*, start, ttl_hours, blueprint_ttl, is_admin: bool, now=None):
    """When the cell ends. Counted from the booked START (demo_schedule.plan's rule): a
    presenter asking for 8 hours means 8 hours after it was due, not after a slow build."""
    from . import expiry_policy
    hours = ttl_hours or blueprint_ttl or expiry_policy.default_hours()
    hours = expiry_policy.clamp_hours(hours, is_admin=is_admin, now=now)
    return expiry_policy.expiry_from_now(hours, now=start or now)


async def launch(db: Session, bp: CellBlueprint, *, user, name: str, workgroup: str,
                 schedule_fields: dict, ttl_hours=None, schedule: Optional[dict] = None
                 ) -> CustomCell:
    """Book a cell from ``bp``. Builds now, or at the booked start. Raises CellError.

    Everything that can be refused without touching a cloud is refused HERE -- the items
    re-validated against today's request models, every resource's final name and
    workgroup built into its request -- so a presenter hears about it at booking time,
    not when the sweep fires at 07:00 on the morning of the demo.
    """
    from . import change_window_service
    name = (name or "").strip().lower()
    if not NAME_RE.match(name):
        raise CellError("A cell name is 1-24 characters: lowercase letters, digits and "
                        "hyphens, starting with a letter. It prefixes every resource.")
    if db.query(CustomCell).filter(CustomCell.name == name,
                                   CustomCell.status.in_(LIVE)).first():
        raise CellError(f"A live cell named {name!r} already exists.")

    items = validate_items([{"kind": r.kind, "cloud": r.cloud, "region": r.region or "",
                             "label": r.label, "count": r.count, "spec": r.spec_dict}
                            for r in items_of(db, bp.id)])
    for it in items:
        for rname in _resource_names(name, it):
            build_request(it["kind"], it["cloud"], it["region"], it["spec"], name=rname,
                          workgroup=workgroup)
    if any(it["kind"] == "k8s" for it in items):
        from . import feature_flags
        if not feature_flags.enabled("k8s_management_enabled"):
            raise CellError("This blueprint has a Kubernetes cluster, and Kubernetes "
                            "management is off on this instance.")

    sched = change_window_service.schedule_kwargs(db, **schedule_fields)
    start = sched.get("scheduled_for")
    now = _now()
    end = plan_end(start=start, ttl_hours=ttl_hours, blueprint_ttl=bp.ttl_hours,
                   is_admin=bool(getattr(user, "is_effective_admin", False)), now=now)
    suspend = validate_schedule(schedule if schedule is not None else {
        "suspend_at_local": bp.suspend_at_local, "resume_at_local": bp.resume_at_local,
        "schedule_timezone": bp.schedule_timezone, "schedule_days": bp.schedule_days})

    cell = CustomCell(id=str(uuid.uuid4()), name=name, blueprint_id=bp.id,
                      blueprint_name=bp.name, workgroup=workgroup,
                      created_by=user.username, status="scheduled",
                      scheduled_for=start, expires_at=end, **suspend)
    cell.snapshot = items
    db.add(cell)
    db.commit()
    if start is None:
        await provision(db, cell)
    return cell


async def _create_member(db: Session, user, cell: CustomCell, item: dict, rname: str):
    """Create one resource through its cloud's own handler. Returns (ref_id, job_id)."""
    req = build_request(item["kind"], item["cloud"], item["region"], item["spec"],
                        name=rname, workgroup=cell.workgroup or "")
    kind, cloud = item["kind"], item["cloud"]
    if kind == "vm":
        if cloud == "gcp":
            from ..api import gcp
            resp = await gcp.deploy_instance(payload=req, current_user=user, db=db)
        elif cloud == "aws":
            from ..api import aws
            resp = await aws.deploy_ami(req=req, db=db, current_user=user)
        else:
            from ..api import azure
            resp = await azure.deploy_vm(req=req, db=db, current_user=user)
        return resp.job_id, resp.job_id
    if kind == "k8s":
        from ..api import k8s
        out = k8s.provision_cluster(payload=req, db=db, current_user=user)
        return out["cluster_id"], out["job_id"]
    from ..api import cloud_databases
    out = await cloud_databases.provision_database(payload=req, db=db, current_user=user)
    return out["db_id"], out["job_id"]


def _stamp_member(db: Session, cell: CustomCell, member: CustomCellMember) -> None:
    """The member's own timer (the backstop), and the cell's id on a VM's deploy row.

    The cell's suspend schedule is deliberately NOT copied onto the VM: the cell's own
    sweep powers every member (see "Whole-cell power" below), and a second owner of the
    same schedule would stop each VM twice at every boundary."""
    from ..database import CloudDatabase, Job, K8sCluster
    backstop = cell.expires_at + BACKSTOP if cell.expires_at else None
    if member.kind == "vm":
        row = db.query(Job).filter(Job.id == member.ref_id).first()
        if row is None:
            return
        row.expires_at = backstop
        meta = row.metadata_dict or {}
        meta["custom_cell_id"] = cell.id
        row.metadata_dict = meta
    else:
        model = K8sCluster if member.kind == "k8s" else CloudDatabase
        row = db.query(model).filter(model.id == member.ref_id).first()
        if row is not None:
            row.expires_at = backstop
    db.commit()


async def provision(db: Session, cell: CustomCell) -> CustomCell:
    """Create every member. One member failing does not stop the rest: the cell goes
    ``failed`` with its members kept, so Destroy still cleans up what did get built."""
    from fastapi import HTTPException

    from ..database import User
    user = db.query(User).filter(User.username == cell.created_by).first()
    if user is None or not user.is_active:
        cell.status = "failed"
        cell.status_message = (f"{cell.created_by!r} can no longer sign in, so nothing was "
                               "built on their behalf.")
        db.commit()
        return cell
    cell.status = "provisioning"
    cell.status_message = None
    db.commit()
    cell_id = cell.id
    for item in cell.snapshot:
        for rname in _resource_names(cell.name, item):
            member = CustomCellMember(id=str(uuid.uuid4()), cell_id=cell_id,
                                      kind=item["kind"], cloud=item["cloud"], label=rname,
                                      status="provisioning")
            try:
                member.ref_id, member.job_id = await _create_member(db, user, cell, item,
                                                                     rname)
            except HTTPException as exc:
                db.rollback()
                member.status, member.last_error = "failed", str(exc.detail)
            except CellError as exc:
                db.rollback()
                member.status, member.last_error = "failed", str(exc)
            except Exception as exc:  # noqa: BLE001 — isolate one member
                db.rollback()
                logger.warning("custom cell %s: %s failed to start", cell_id, rname,
                               exc_info=True)
                member.status, member.last_error = "failed", f"{type(exc).__name__}: {exc}"
            cell = db.query(CustomCell).filter(CustomCell.id == cell_id).first()
            db.add(member)
            db.commit()
            if member.ref_id:
                _stamp_member(db, cell, member)
    refresh(db, cell)
    return cell


# ── Roll-up ──────────────────────────────────────────────────────────────────

def _member_state(db: Session, m: CustomCellMember) -> str:
    """provisioning | ready | failed | destroying | destroyed, read off the member's own row."""
    from ..database import CloudDatabase, Job, K8sCluster
    if not m.ref_id:
        return m.status
    if m.kind == "vm":
        job = db.query(Job).filter(Job.id == m.ref_id).first()
        if job is None:
            return "destroyed"
        meta = job.metadata_dict or {}
        if meta.get("destroyed"):
            return "destroyed"
        if m.status == "destroying":
            return "destroying"
        if job.status == "completed":
            return "ready"
        if job.status in ("failed", "cancelled"):
            return "failed"
        return "provisioning"
    model = K8sCluster if m.kind == "k8s" else CloudDatabase
    row = db.query(model).filter(model.id == m.ref_id).first()
    if row is None or row.status == "decommissioned":
        return "destroyed"
    if row.status == "decommissioning":
        return "destroying"
    if row.status == "failed":
        return "failed"
    # A stopped (or stopping/starting) cluster or database is built: the power state is
    # tracked on the member, not by pretending it is still provisioning.
    if row.status in ("registered", "managed", "available", "running", "stopped",
                      "stopping", "starting"):
        return "ready"
    return "provisioning"


def refresh(db: Session, cell: CustomCell) -> CustomCell:
    """Bring every member's status, and from them the cell's, up to date."""
    if cell.status in ("scheduled", "destroyed"):
        return cell
    members = members_of(db, cell.id)
    for m in members:
        # A member _destroy_member already settled keeps its verdict: "destroyed" on a
        # deploy that failed is a decision about that row, not something it reports.
        if m.status == "destroyed":
            continue
        state = _member_state(db, m)
        if state != m.status:
            m.status = state
    states = [m.status for m in members]
    if cell.status == "destroying":
        if all(s == "destroyed" or not m.ref_id for s, m in zip(states, members)):
            cell.status = "destroyed"
    elif "failed" in states:
        cell.status = "failed"
        cell.status_message = "; ".join(f"{m.label}: {m.last_error or 'failed'}"
                                        for m in members if m.status == "failed")[:2000]
    elif states and all(s == "ready" for s in states):
        if cell.status != "suspended":
            cell.status = "active"
    elif cell.status != "suspended":
        cell.status = "provisioning"
    db.commit()
    return cell


# ── Teardown ─────────────────────────────────────────────────────────────────

def _destroy_member(db: Session, cell: CustomCell, m: CustomCellMember, actor: str) -> None:
    """Start one member's teardown if it can start now. Idempotent; a member still being
    built is left for the next sweep -- tearing down a half-applied Terraform run is how
    state gets orphaned."""
    from ..database import Job
    from . import expiry_policy, job_service
    state = _member_state(db, m)
    if state in ("destroyed", "destroying") or not m.ref_id:
        m.status = state if m.ref_id else "destroyed"
        return
    if m.kind == "vm":
        job = db.query(Job).filter(Job.id == m.ref_id).first()
        if job.status in ("pending", "queued"):
            job_service.set_cancelled(db, job.id)       # never started: nothing to delete
            m.status = "destroyed"
            return
        if job.status in ("failed", "cancelled"):
            # The deploy never finished, so there is no instance record to destroy from
            # -- the same position the OT cell's "clear a failed cell" is in. Said on the
            # member rather than silently, because a failed apply CAN leave a half-built
            # VM behind, and the cloud console is where that shows.
            m.status = "destroyed"
            m.last_error = (m.last_error or f"deploy {job.status}") + \
                "; nothing recorded to destroy -- check the cloud console"
            return
        if job.status != "completed":
            return                                       # running: next pass
        dtype, payload = expiry_policy.build_destroy_metadata(job.job_type,
                                                              job.metadata_dict or {},
                                                              job.id)
        if dtype is None:
            m.status, m.last_error = "failed", f"cannot destroy: {payload}"
            return
        job_service.create_job(db, job_type=dtype, created_by=actor,
                               workgroup=job.workgroup, metadata=payload)
        m.status = "destroying"
        return
    if state == "provisioning":
        return
    try:
        if m.kind == "k8s":
            from . import k8s_service
            k8s_service.start_decommission(db, m.ref_id, created_by=actor)
        else:
            from . import cloud_database_service
            cloud_database_service.start_decommission(db, m.ref_id, created_by=actor)
        m.status = "destroying"
    except Exception as exc:  # noqa: BLE001 — K8sError / CloudDatabaseError
        m.last_error = str(exc)
        logger.warning("custom cell %s: could not decommission %s: %s", cell.id, m.label,
                       exc)


def start_destroy(db: Session, cell: CustomCell, *, actor: str) -> CustomCell:
    """Tear the cell down. A cell that has not started building yet is simply cancelled."""
    if cell.status == "destroyed":
        return cell
    cell.expires_at = None                              # at-most-once, as the reaper does
    if cell.status == "scheduled":
        cell.status = "destroyed"
        cell.status_message = f"cancelled before it was built, by {actor}"
        db.commit()
        return cell
    cell.status = "destroying"
    db.commit()
    for m in members_of(db, cell.id):
        _destroy_member(db, cell, m, actor)
    db.commit()
    return refresh(db, cell)


def set_end(db: Session, cell: CustomCell, end) -> None:
    """Move the cell's end time, and every member's backstop with it."""
    from ..database import CloudDatabase, Job, K8sCluster
    cell.expires_at = end
    cell.expiry_warned_at = None
    cell.warned_stage_minutes = None
    backstop = end + BACKSTOP if end else None
    for m in members_of(db, cell.id):
        model = {"vm": Job, "k8s": K8sCluster}.get(m.kind, CloudDatabase)
        row = db.query(model).filter(model.id == m.ref_id).first() if m.ref_id else None
        if row is not None:
            row.expires_at = backstop
    db.commit()


# ── The sweep (run by services/schedule_sweeper every few minutes) ────────────

def has_work(db: Session) -> bool:
    return db.query(CustomCell.id).filter(
        CustomCell.status.in_(LIVE)).first() is not None


async def sweep(db: Session, now=None) -> list:
    """Build booked cells that are due, end cells whose time is up, refresh the rest.
    Returns a short line per action. One cell failing never stops the others."""
    now = now or _now()
    out = []
    for cell in db.query(CustomCell).filter(CustomCell.status.in_(LIVE)).all():
        try:
            if cell.status != "destroying" and cell.expires_at and cell.expires_at <= now:
                start_destroy(db, cell, actor=cell.created_by or "custom-cell")
                out.append(f"{cell.name}: ended")
            elif cell.status == "scheduled":
                if cell.scheduled_for is None or cell.scheduled_for <= now:
                    await provision(db, cell)
                    out.append(f"{cell.name}: started")
            elif cell.status == "destroying":
                for m in members_of(db, cell.id):
                    _destroy_member(db, cell, m, cell.created_by or "custom-cell")
                db.commit()
                refresh(db, cell)
            else:
                refresh(db, cell)
                if cell.status in ("active", "suspended"):
                    note = _sweep_schedule(db, cell, now)
                    if note:
                        out.append(note)
        except Exception:  # noqa: BLE001
            logger.warning("custom cell sweep: %s failed", cell.id, exc_info=True)
            db.rollback()
    return out


# ── Read model ───────────────────────────────────────────────────────────────

def serialize_cell(db: Session, cell: CustomCell, *, members: bool = True) -> dict:
    out = {
        "id": cell.id, "kind": "custom", "name": cell.name,
        "blueprint_id": cell.blueprint_id or "", "blueprint_name": cell.blueprint_name or "",
        "workgroup": cell.workgroup or "", "created_by": cell.created_by or "",
        "created_at": cell.created_at.isoformat() if cell.created_at else "",
        "status": cell.status, "status_message": cell.status_message or "",
        "scheduled_for": cell.scheduled_for.isoformat() if cell.scheduled_for else "",
        "end_at": cell.expires_at.isoformat() if cell.expires_at else "",
        "suspend_at_local": cell.suspend_at_local or "",
        "resume_at_local": cell.resume_at_local or "",
        "schedule_timezone": cell.schedule_timezone or "",
        "schedule_days": cell.schedule_days or "",
        "items": [{"kind": i["kind"], "cloud": i["cloud"], "label": i["label"],
                   "count": i["count"]} for i in cell.snapshot],
    }
    if members:
        out["members"] = [{"id": m.id, "kind": m.kind, "cloud": m.cloud, "label": m.label,
                           "ref_id": m.ref_id or "", "job_id": m.job_id or "",
                           "status": m.status, "power_state": m.power_state or "",
                           "last_error": m.last_error or ""}
                          for m in members_of(db, cell.id)]
    return out


def list_cells(db: Session, accessible: Optional[Iterable[str]], *,
               include_destroyed: bool = False) -> List[CustomCell]:
    q = db.query(CustomCell)
    if not include_destroyed:
        q = q.filter(CustomCell.status != "destroyed")
    return [c for c in q.order_by(CustomCell.created_at.desc()).all()
            if cell_visible(c, accessible)]


# ── Whole-cell power: suspend and resume every member ────────────────────────
#
# A VM is stopped by the same ``<cloud>_power`` job its Stop button and the suspend
# sweeper create (``suspend_sweeper._power_meta`` builds the identical row). A cluster or
# a database goes through ``k8s_service.start_power`` / ``cloud_database_service.
# start_power`` -- scale node pools to zero (AKS: managed stop) and stop the instance.
# Those two are looked up rather than imported: on an install without them the cell
# still suspends its VMs and says, per member, why the rest kept running.
#
# The cell's suspend schedule is driven HERE for every member, VMs included, and is NOT
# copied onto the VM deploy rows: the VM sweeper runs only behind its own flag, and two
# owners of one schedule would act twice at every boundary.

_POWER_ENTRY = {"k8s": ("k8s_service", "start_power"),
                "database": ("cloud_database_service", "start_power")}


def _start_power_fn(kind: str):
    import importlib
    mod_name, fn_name = _POWER_ENTRY[kind]
    mod = importlib.import_module(f"{__package__}.{mod_name}")
    return getattr(mod, fn_name, None)


def _power_member(db: Session, m: CustomCellMember, verb: str, actor: str,
                  *, restop: bool = False) -> None:
    from ..database import Job
    from . import job_service
    if m.kind == "vm":
        from . import suspend_sweeper
        job = db.query(Job).filter(Job.id == m.ref_id).first()
        cloud = {v: k for k, v in _VM_JOB_TYPES.items()}.get(job.job_type) if job else None
        if job is None or cloud is None:
            m.last_error = "no deploy record to power"
            return
        job_service.create_job(db, job_type=suspend_sweeper._POWER_JOB[cloud],
                               created_by=actor, workgroup=job.workgroup,
                               metadata=suspend_sweeper._power_meta(
                                   cloud, job, job.metadata_dict or {}, verb))
    else:
        fn = _start_power_fn(m.kind)
        if fn is None:
            m.last_error = (f"{m.kind} power ops are not available on this install, so it "
                            "keeps running while the cell is suspended")
            return
        try:
            if m.kind == "database":
                fn(db, m.ref_id, verb, actor, restop=restop)
            else:
                fn(db, m.ref_id, verb, actor)
        except Exception as exc:  # noqa: BLE001 — PowerRefused / PowerUnsupported / ...
            m.last_error = f"{verb}: {exc}"
            return
    m.power_state = "stopped" if verb == "stop" else "running"
    m.last_error = None


def power(db: Session, cell: CustomCell, action: str, *, actor: str) -> CustomCell:
    """Suspend (``stop``) or resume (``start``) every built member of the cell."""
    if action not in ("stop", "start"):
        raise CellError("action must be stop or start")
    refresh(db, cell)
    if action == "stop" and cell.status != "active":
        raise CellError(f"Only an active cell can be suspended; this one is {cell.status}.")
    if action == "start" and cell.status != "suspended":
        raise CellError(f"Only a suspended cell can be resumed; this one is {cell.status}.")
    for m in members_of(db, cell.id):
        if m.status == "ready" and m.ref_id:
            _power_member(db, m, action, actor)
    cell.status = "suspended" if action == "stop" else "active"
    db.commit()
    return cell


def _sweep_schedule(db: Session, cell: CustomCell, now) -> str:
    """Apply the cell's suspend schedule (``suspend_schedule.due_action``), and re-stop a
    database its cloud restarted on its own while the cell sleeps (RDS does after 7 days)."""
    from datetime import timezone

    from . import suspend_schedule
    now_utc = now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now
    action = suspend_schedule.due_action(cell, now_utc)
    cell.schedule_last_checked_at = now_utc.replace(tzinfo=None)
    db.commit()
    actor = cell.created_by or "custom-cell"
    if action == suspend_schedule.SUSPEND and cell.status == "active":
        power(db, cell, "stop", actor=actor)
        return f"{cell.name}: suspended on schedule"
    if action == suspend_schedule.RESUME and cell.status == "suspended":
        power(db, cell, "start", actor=actor)
        return f"{cell.name}: resumed on schedule"
    if cell.status == "suspended":
        _restop_databases(db, cell, actor)
    return ""


def _restop_databases(db: Session, cell: CustomCell, actor: str) -> None:
    from ..database import CloudDatabase
    try:
        from . import clouddb_power
    except ImportError:
        return
    for m in members_of(db, cell.id):
        if m.kind != "database" or not m.ref_id:
            continue
        row = db.query(CloudDatabase).filter(CloudDatabase.id == m.ref_id).first()
        if row is not None and clouddb_power.needs_restop(row):
            _power_member(db, m, "stop", actor, restop=True)
    db.commit()
