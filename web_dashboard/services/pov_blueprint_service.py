"""POV blueprints: a saved recipe for a kind of POV, so building one is a choice not eleven.

The POV create form asks for a template, a name, a workgroup, a project, a broker VM name,
an idle timeout and three tenant references. For a given *kind* of POV — a Password Safe
evaluation, a PRA-only demo — every one of those except the name has the same right answer
every time. A blueprint records one correct set under a name.

Two rules keep this from becoming a second way to provision a POV:

**A blueprint supplies defaults; the request still wins.** ``apply_to`` fills in only the
fields a ``ProvisionRequest`` left blank. Nothing here creates an environment, and there is
exactly one provision job before and after this module exists. A blueprint that forked the
flow would be a second place for the provision rules to drift, and the rules it would drift
from are the ones about orphaned environments.

**Nothing here auto-runs, and no secret is stored.** It is tempting to have a blueprint
chain the Gateway, Resource Broker and wire-up straight off a provision. It cannot: both
installs run *through the broker agent*, which does not exist until minutes after the
environment comes up, and both need a credential that is per-tenant rather than per-recipe
— a PRA deploy key and the Password Safe installer key. A blueprint that carried those
would spread two secrets across every recipe an SE ever saved, to save one paste. So a
blueprint pre-fills the non-secret configuration onto the new POV — the Gateway name, the
Resource Broker's VM, zone and asset — and the panels open ready to press. The person
pressing the button is looking at the panel anyway.

**There is no customer field**, for the reason ``PovEnvironment`` gives at length: a
blueprint is a shape of POV, not a customer's POV.
"""
from __future__ import annotations

import logging
import re

from sqlalchemy.orm import Session

from ..database import PovBlueprint
from . import bt_tenant_service, lab_platforms

logger = logging.getLogger(__name__)

# The same shape api/pov constrains a POV name to. A blueprint name reaches job output and
# the POV page's dropdown, and it is the other place someone would otherwise type a
# customer name.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")


class BlueprintError(Exception):
    """A blueprint could not be saved or applied. The message names the remedy."""


def normalize_name(name: str) -> str:
    value = (name or "").strip().lower()
    if not _NAME_RE.match(value):
        raise BlueprintError(
            "name must be 2-63 characters of lowercase letters, digits and hyphens, "
            "starting with a letter or digit")
    return value


def get(db: Session, blueprint_id: str) -> PovBlueprint | None:
    return db.query(PovBlueprint).filter(PovBlueprint.id == blueprint_id).first()


def list_all(db: Session) -> list[PovBlueprint]:
    return db.query(PovBlueprint).order_by(PovBlueprint.name.asc()).all()


def _validate(db: Session, *, platform: str, template_id: str, name: str,
              pra_tenant_id: str, ps_tenant_id: str, entitle_tenant_id: str,
              exclude_id: str = "") -> tuple[str, str, dict]:
    """Everything that can be refused before a row is written.

    Tenant ids go through ``bt_tenant_service.validate_selection`` — the same function the
    POV create endpoint uses — so a blueprint cannot store a selection the provision would
    later reject. Storing one that fails only at provision time would move a form error into
    a job, which is the exact thing ``validate_selection`` exists to prevent.
    """
    platform = lab_platforms.normalize(platform)
    slug = normalize_name(name)
    if not (template_id or "").strip():
        raise BlueprintError("a template id is required")

    clash = db.query(PovBlueprint).filter(PovBlueprint.name == slug)
    if exclude_id:
        clash = clash.filter(PovBlueprint.id != exclude_id)
    if clash.first():
        raise BlueprintError(f"a blueprint named {slug!r} already exists")

    try:
        tenants = bt_tenant_service.validate_selection(
            db, pra_tenant_id=pra_tenant_id, ps_tenant_id=ps_tenant_id,
            entitle_tenant_id=entitle_tenant_id)
    except bt_tenant_service.BTTenantError as exc:
        raise BlueprintError(str(exc)) from exc
    return platform, slug, tenants




def _spend_cap(value) -> float | None:
    """The blueprint's spend cap, validated.

    Validated at SAVE, like the schedule and the tenant ids beside it: a stored cap that
    only failed when a POV was provisioned from it would move a form error into somebody
    else's provision job, weeks later.
    """
    from . import pov_spend
    try:
        return pov_spend.validate_cap(value)
    except pov_spend.SpendError as exc:
        raise BlueprintError(str(exc)) from None


def _schedule_fields(suspend_at: str, resume_at: str, tz_name: str, days: str) -> dict:
    """The four schedule columns, validated.

    Validated at SAVE, like the tenant ids above and for the same reason: a stored
    schedule that only fails when the sweep reads it would move a form error into a
    background job, three weeks later, against a POV that is already running.
    """
    from . import pov_schedule
    try:
        return pov_schedule.validate(suspend_at=suspend_at, resume_at=resume_at,
                                     tz_name=tz_name, days=days)
    except pov_schedule.ScheduleError as exc:
        raise BlueprintError(str(exc)) from None


def create(db: Session, *, platform: str, name: str, template_id: str,
           template_name: str = "", description: str = "", project_id: str = "",
           broker_vm_name: str = "", suspend_on_idle_seconds: int | None = None,
           pra_tenant_id: str = "", ps_tenant_id: str = "", entitle_tenant_id: str = "",
           gateway_name: str = "", rb_windows_vm_name: str = "", rb_zone: str = "",
           rb_asset: str = "", expiry_hours: int | None = None,
           workgroup: str = "", created_by: str = "",
           source_build_id: str = "", suspend_at_local: str = "",
           resume_at_local: str = "", schedule_timezone: str = "",
           schedule_days: str = "",
           spend_cap_usd: float | None = None) -> PovBlueprint:
    platform, slug, tenants = _validate(
        db, platform=platform, template_id=template_id, name=name,
        pra_tenant_id=pra_tenant_id, ps_tenant_id=ps_tenant_id,
        entitle_tenant_id=entitle_tenant_id)

    row = PovBlueprint(
        platform=platform, name=slug, description=description or None,
        template_id=str(template_id).strip(), template_name=template_name or None,
        project_id=(project_id or "").strip() or None,
        source_build_id=source_build_id or None,
        broker_vm_name=(broker_vm_name or "").strip() or None,
        suspend_on_idle_seconds=suspend_on_idle_seconds,
        gateway_name=(gateway_name or "").strip() or None,
        rb_windows_vm_name=(rb_windows_vm_name or "").strip() or None,
        rb_zone=(rb_zone or "").strip() or None,
        rb_asset=(rb_asset or "").strip() or None,
        expiry_hours=expiry_hours,
        spend_cap_usd=_spend_cap(spend_cap_usd),
        **_schedule_fields(suspend_at_local, resume_at_local, schedule_timezone,
                           schedule_days),
        workgroup=(workgroup or "").strip() or None,
        created_by=created_by or None,
        **tenants,
    )
    db.add(row)
    db.commit()
    return row


# Fields `update` will take from a payload. Listed rather than derived from the model so a
# column added later is an explicit decision to expose, not an accident — `id`,
# `created_by` and `created_at` are exactly the ones that must never be settable this way.
_UPDATABLE = (
    "description", "template_id", "template_name", "project_id", "broker_vm_name",
    "suspend_on_idle_seconds", "gateway_name", "rb_windows_vm_name", "rb_zone",
    "rb_asset", "expiry_hours", "workgroup",
    # The suspend schedule a POV from this blueprint starts with, for a platform
    # with no idle timer of its own.
    "suspend_at_local", "resume_at_local", "schedule_timezone", "schedule_days",
    # The spend cap a POV from this blueprint starts with.
    "spend_cap_usd",
)
_BLANKABLE = ("description", "project_id", "broker_vm_name", "template_name",
              "gateway_name", "rb_windows_vm_name", "rb_zone", "rb_asset", "workgroup",
              "suspend_at_local", "resume_at_local", "schedule_timezone",
              "schedule_days")


def update(db: Session, row: PovBlueprint, payload: dict) -> PovBlueprint:
    """Apply a partial update. Only keys present in ``payload`` are touched."""
    # The schedule is four fields that only mean anything together, so it is
    # validated as a unit before the field-by-field loop below touches any of them.
    if any(k in payload for k in ("suspend_at_local", "resume_at_local",
                                  "schedule_timezone", "schedule_days")):
        payload = dict(payload)
        payload.update(_schedule_fields(
            payload.get("suspend_at_local", row.suspend_at_local) or "",
            payload.get("resume_at_local", row.resume_at_local) or "",
            payload.get("schedule_timezone", row.schedule_timezone) or "",
            payload.get("schedule_days", row.schedule_days) or ""))
    name = payload.get("name")
    platform, slug, tenants = _validate(
        db,
        platform=payload.get("platform") or row.platform,
        template_id=payload.get("template_id") or row.template_id,
        name=name if name is not None else row.name,
        pra_tenant_id=payload.get("pra_tenant_id", row.pra_tenant_id) or "",
        ps_tenant_id=payload.get("ps_tenant_id", row.ps_tenant_id) or "",
        entitle_tenant_id=payload.get("entitle_tenant_id", row.entitle_tenant_id) or "",
        exclude_id=row.id)

    row.platform = platform
    row.name = slug
    for field in _UPDATABLE:
        if field not in payload:
            continue
        value = payload[field]
        if field in _BLANKABLE:
            # "" means clear it. A blankable field that stored "" instead of NULL would
            # read as set-to-empty everywhere downstream, which is a different thing from
            # not set.
            setattr(row, field, (str(value or "").strip() or None))
        else:
            setattr(row, field, value)
    for column, value in tenants.items():
        setattr(row, column, value or None)
    db.commit()
    return row


def delete(db: Session, row: PovBlueprint) -> None:
    """Delete a blueprint.

    Unlike a tenant, nothing holds a live reference to one: a POV copies the blueprint's
    values at provision time and never reads it again, so deleting one cannot strand a
    running environment. ``source_build_id`` on a build is provenance, not a foreign key.
    """
    db.delete(row)
    db.commit()


def apply_to(row: PovBlueprint, payload) -> dict:
    """The provision fields this blueprint supplies, for those the request left blank.

    Returns a dict of overrides for the caller to apply. **The request wins field by
    field** — a blueprint is a set of defaults, and an SE who typed a broker VM name into
    the form meant it. Returning overrides rather than mutating the payload keeps that
    precedence visible at the one call site instead of buried here.

    ``name`` and ``workgroup`` are deliberately absent: a POV's name is per-POV by
    definition, and its workgroup decides RBAC and the expiry exempt list, which is not
    something a saved recipe should be able to set silently.
    """
    out: dict = {}

    def _fill(field: str, value):
        current = getattr(payload, field, None)
        if value in (None, ""):
            return
        # 0 is a real answer for suspend_on_idle_seconds ("disable it"), but the form
        # default is also 0, so it cannot be told from "unset" — the blueprint fills it.
        if current in (None, "", 0):
            out[field] = value

    _fill("template_id", row.template_id)
    _fill("project_id", row.project_id)
    _fill("broker_vm_name", row.broker_vm_name)
    _fill("suspend_on_idle_seconds", row.suspend_on_idle_seconds)
    _fill("pra_tenant_id", row.pra_tenant_id)
    _fill("ps_tenant_id", row.ps_tenant_id)
    _fill("entitle_tenant_id", row.entitle_tenant_id)
    return out


def prefill_environment(db: Session, row: PovBlueprint | None, env) -> list[str]:
    """Copy the blueprint's non-secret install configuration onto a new POV.

    Returns what was set, for the provision response. Nothing is *run* — see the module
    docstring. This only means that when the operator later opens the Gateway or Resource
    Broker panel, the fields that are the same for every POV of this kind are already
    filled and only the secret is left to paste.

    A field the POV already carries is never overwritten: the create form wins over the
    recipe here for the same reason it does in ``apply_to``.
    """
    if row is None:
        return []
    filled: list[str] = []

    if row.gateway_name and not (env.gateway_name or "").strip():
        env.gateway_name = row.gateway_name
        filled.append("Gateway name")
        db.commit()

    # The suspend schedule, copied onto the POV rather than returned as an `apply_to`
    # override. It belongs here for the same reason the Gateway name does: it is
    # configuration the POV carries, not a field of the provision REQUEST — nothing in
    # `ProvisionRequest` names it, so there is no request value for a default to lose to.
    #
    # The evaluation latch is deliberately NOT copied. It is per-environment state, and a
    # new POV must start with it NULL so its first sweep records the time and acts on
    # nothing.
    if row.suspend_at_local and not (env.suspend_at_local or "").strip():
        env.suspend_at_local = row.suspend_at_local
        env.resume_at_local = row.resume_at_local
        env.schedule_timezone = row.schedule_timezone
        env.schedule_days = row.schedule_days
        env.schedule_last_checked_at = None
        filled.append("Suspend schedule")
        db.commit()

    # The Resource Broker's three non-secret fields live in the POV's metadata, behind the
    # service that owns them — writing metadata_dict from here would be a second writer for
    # keys pov_resource_broker names privately.
    if row.rb_windows_vm_name or row.rb_zone or row.rb_asset:
        from . import pov_resource_broker

        def _fill(candidate: str, current: str):
            """The blueprint's value, or None to leave what is there alone.

            ``configure`` reads None as "do not touch", so this is the don't-overwrite rule
            expressed once instead of three times."""
            return candidate if (candidate and not current) else None

        pov_resource_broker.configure(
            db, env,
            vm_name=_fill(row.rb_windows_vm_name,
                          pov_resource_broker.stored_rb_vm_name(env)),
            zone_name=_fill(row.rb_zone, pov_resource_broker.zone(env)),
            asset_key=_fill(row.rb_asset, pov_resource_broker.asset(env)))
        filled.append("Resource Broker staging")
    return filled


def serialize(row: PovBlueprint) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "description": row.description or "",
        "platform": row.platform,
        "template_id": row.template_id or "",
        "template_name": row.template_name or "",
        "project_id": row.project_id or "",
        "source_build_id": row.source_build_id or "",
        "broker_vm_name": row.broker_vm_name or "",
        "suspend_on_idle_seconds": row.suspend_on_idle_seconds or 0,
        "pra_tenant_id": row.pra_tenant_id or "",
        "ps_tenant_id": row.ps_tenant_id or "",
        "entitle_tenant_id": row.entitle_tenant_id or "",
        "gateway_name": row.gateway_name or "",
        "rb_windows_vm_name": row.rb_windows_vm_name or "",
        "rb_zone": row.rb_zone or "",
        "rb_asset": row.rb_asset or "",
        "expiry_hours": row.expiry_hours or 0,
        "suspend_at_local": row.suspend_at_local or "",
        "resume_at_local": row.resume_at_local or "",
        "schedule_timezone": row.schedule_timezone or "",
        "schedule_days": row.schedule_days or "",
        "spend_cap_usd": row.spend_cap_usd or 0.0,
        "workgroup": row.workgroup or "",
        "created_by": row.created_by or "",
        "created_at": row.created_at.isoformat() if row.created_at else "",
    }
