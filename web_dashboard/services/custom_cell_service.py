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
    errors = getattr(exc, "errors", None)
    if callable(errors):
        try:
            parts = []
            for e in errors()[:4]:
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
            f"{label}: a blueprint cannot set {', '.join(bad)}. Names and the workgroup come "
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
