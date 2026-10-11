"""Entitle **Virtual Applications** over a registered directory's groups.

A directory (AD, Entra ID, Okta, PingOne, …) that an operator has PINNED to its Entitle
integration (``managed_directories.entitle_integration_id``) has its groups synced into
Entitle as resources, each with a membership role. Those show up under raw group names —
``ent-aws-cloudops``, ``CN=SQL-Admins,OU=…`` — which is not what anyone should have to
search for. A Virtual Application puts a friendly role over each one:

    "AWS – Cloud Ops"  ─virtualized_role→  ent-aws-cloudops / Member  (Okta integration)

Requesting the virtual role adds the requester to the group, and whatever the group
already drives (an IAM Identity Center assignment, an Azure role, a GCP binding) does the
rest. Nothing is created in the directory, and nothing is guessed: every linked role is
checked against Entitle as belonging to the directory's pinned integration before
anything is applied.

The roles are routed through the three tiers in :mod:`entitle_tiers`, so a role's
workflow and offered durations match the dashboard's own user-JIT groups.

Apply and destroy run as an ``entitle_virtual_app`` job (one Terraform state per app,
stored on the row) — see :func:`run_job`.
"""
import asyncio
import json
import logging
import re
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from ..database import EntitleVirtualApp, ManagedDirectory
from . import entitle_public_api as api
from . import entitle_tiers

logger = logging.getLogger(__name__)

JOB_TYPE = "entitle_virtual_app"

# Directory kinds whose groups an Entitle integration can sync. The two site links are
# plumbing between networks, not directories with groups of their own.
INELIGIBLE_PROVIDERS = ("dns_link", "gcp_vyos_link")

MAX_ROLES = 100
LIVE_STATUSES = ("applying", "active", "failed", "destroying")
CLOUDS = ("aws", "azure", "gcp", "oci")

_NAME_RE = re.compile(r"^[\w .,:()&/+'\-–—]{2,50}$")
# An Entitle id. It is put into a URL path (GET /public/v1/roles/{id}), so nothing that
# could change the path or start a query string gets through.
_ID_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")


class VirtualAppError(Exception):
    """A request the operator can fix. The message is shown as-is."""


def _cfg(key: str) -> str:
    from . import config_service
    from ..config import settings
    return str(config_service.get(key) or getattr(settings, key, "") or "")


def registration_enabled() -> bool:
    from . import feature_flags
    from ..config import settings
    return feature_flags.enabled("entitle_registration_enabled",
                                 getattr(settings, "entitle_registration_enabled", False))


def refusal(row: ManagedDirectory) -> str:
    """Why a virtual application cannot be created over this directory, or ""."""
    if row.provider in INELIGIBLE_PROVIDERS:
        return "A site link has no groups of its own — use the directory it extends."
    if row.status == "deleted":
        return "This directory has been removed."
    if not row.entitle_integration_id:
        return ("Pin this directory's Entitle integration first (Entitle → Link), and let "
                "Entitle sync its groups.")
    if not registration_enabled():
        return ("Entitle resource registration is off — turn it on in Settings → "
                "Integrations → Entitle.")
    return ""


def _loads(raw, default):
    try:
        val = json.loads(raw or "")
    except (TypeError, ValueError):
        return default
    return val if isinstance(val, type(default)) else default


def to_dict(app: EntitleVirtualApp) -> dict:
    return {
        "id": app.id, "directory_id": app.directory_id, "name": app.name,
        "status": app.status, "integration_id": app.integration_id or "",
        "roles": _loads(app.roles_json, []), "error": app.error or "",
        "created_by": app.created_by or "",
        "created_at": app.created_at.isoformat() if app.created_at else None,
        "updated_at": app.updated_at.isoformat() if app.updated_at else None,
    }


def list_for_directory(db: Session, directory_id: str) -> list:
    rows = (db.query(EntitleVirtualApp)
            .filter(EntitleVirtualApp.directory_id == directory_id,
                    EntitleVirtualApp.status != "deleted")
            .order_by(EntitleVirtualApp.created_at.desc()).all())
    return [to_dict(r) for r in rows]


def live_for_directory(db: Session, directory_id: str) -> list:
    return (db.query(EntitleVirtualApp)
            .filter(EntitleVirtualApp.directory_id == directory_id,
                    EntitleVirtualApp.status.in_(LIVE_STATUSES)).all())


def active_roles(db: Session) -> list:
    """Every role on every active virtual app, with its Entitle role id, for the persona
    bundles to resolve their group component against."""
    out = []
    for app in db.query(EntitleVirtualApp).filter(EntitleVirtualApp.status == "active"):
        for r in _loads(app.roles_json, []):
            if r.get("role_id"):
                out.append({**r, "app_id": app.id, "app_name": app.name})
    return out


# ── Reading Entitle ──────────────────────────────────────────────────────────

def _member_first(role: dict) -> tuple:
    name = role["name"].lower()
    return (0 if "member" in name else 1, role["resource_name"].lower(), name)


async def directory_roles(row: ManagedDirectory, search: str = "") -> list:
    """Roles Entitle synced from the directory's pinned integration — one per group and
    access level — with membership roles first, which is nearly always the one to front."""
    if not row.entitle_integration_id:
        raise VirtualAppError(refusal(row) or "No Entitle integration is pinned.")
    try:
        roles = await api.list_roles(integration_id=row.entitle_integration_id,
                                     search=search.strip()[:100], max_pages=3)
    except api.EntitleApiError as e:
        raise VirtualAppError(str(e)) from e
    from .entitle_persona_bundles import suggest_for_group
    for r in roles:
        r["suggested"] = suggest_for_group(r["resource_name"])
    return sorted(roles, key=_member_first)


async def _check_roles(row: ManagedDirectory, role_ids: list) -> dict:
    """``{role_id: summary}``, each confirmed to belong to the pinned integration. A role
    from anywhere else is refused by name — linking it would make a "directory" app that
    grants something the directory does not govern."""
    sem = asyncio.Semaphore(8)

    async def one(rid):
        async with sem:
            return rid, await api.get_role(rid)

    try:
        found = dict(await asyncio.gather(*(one(r) for r in dict.fromkeys(role_ids))))
    except api.EntitleApiError as e:
        raise VirtualAppError(f"Checking the roles against Entitle failed: {e}") from e
    for rid, summary in found.items():
        if summary.get("integration_id") != row.entitle_integration_id:
            where = summary.get("integration_name") or "another integration"
            raise VirtualAppError(
                f"Role {summary.get('name') or rid} belongs to {where}, not to this "
                f"directory's integration ({row.entitle_integration_name or 'pinned'}).")
    return found


# ── Validation ───────────────────────────────────────────────────────────────

def _key_for(resource: str, name: str, taken: set) -> str:
    base = re.sub(r"[^a-z0-9_]", "_", f"{resource}_{name}".lower()).strip("_")[:60] or "role"
    if base[0].isdigit():
        base = f"r_{base}"
    key, n = base, 2
    while key in taken:
        key, n = f"{base}_{n}", n + 1
    taken.add(key)
    return key


def normalize_request(name: str, roles: list) -> tuple:
    """Check the shape of a create request; returns ``(name, roles)`` cleaned. Entitle is
    not consulted here — :func:`prepare` does that."""
    name = (name or "").strip()
    if not _NAME_RE.match(name):
        raise VirtualAppError("Name the application in 2–50 letters, digits, spaces or "
                              "simple punctuation.")
    if not roles:
        raise VirtualAppError("Link at least one group role.")
    if len(roles) > MAX_ROLES:
        raise VirtualAppError(f"At most {MAX_ROLES} roles per application.")
    out, taken, seen = [], set(), set()
    for r in roles:
        rid = str(r.get("role_id") or "").strip()
        rname = str(r.get("name") or "").strip()
        resource = str(r.get("resource") or "Access").strip() or "Access"
        tier = str(r.get("tier") or "single_approver")
        if not _ID_RE.match(rid):
            raise VirtualAppError("Each role needs the Entitle role it fronts.")
        if rid in seen:
            raise VirtualAppError("The same group role is linked twice.")
        seen.add(rid)
        if not _NAME_RE.match(rname):
            raise VirtualAppError(f"Role name {rname!r} must be 2–50 characters.")
        if not _NAME_RE.match(resource):
            raise VirtualAppError(f"Grouping {resource!r} must be 2–50 characters.")
        if tier not in entitle_tiers.TIERS:
            raise VirtualAppError(f"Unknown tier {tier!r}.")
        cloud = str(r.get("cloud") or "").lower()
        out.append({
            "key": _key_for(resource, rname, taken), "name": rname, "resource": resource,
            "tier": tier, "virtualized_role_id": rid,
            "persona": str(r.get("persona") or "")[:32],
            "cloud": cloud if cloud in CLOUDS else "",
        })
    return name, out


async def prepare(row: ManagedDirectory, name: str, roles: list) -> tuple:
    """Validate fully — shape, refusal, tier workflows, and every role against Entitle.
    Returns ``(name, roles)`` with each role's underlying group name filled in."""
    reason = refusal(row)
    if reason:
        raise VirtualAppError(reason)
    name, roles = normalize_request(name, roles)
    flows = entitle_tiers.resolve_workflows(_cfg)
    for tier in {r["tier"] for r in roles}:
        if not flows[tier]["id"]:
            raise VirtualAppError(
                f"No workflow for the {entitle_tiers.TIER_LABELS[tier]} tier — set "
                f"{entitle_tiers.WORKFLOW_SETTING[tier]} or entitle_workflow_id in Settings "
                "→ Integrations → Entitle.")
    if not _cfg("entitle_owner_id"):
        raise VirtualAppError("Set the Entitle owner (entitle_owner_id) in Settings → "
                              "Integrations → Entitle.")
    found = await _check_roles(row, [r["virtualized_role_id"] for r in roles])
    for r in roles:
        s = found[r["virtualized_role_id"]]
        r["virtualized_role_name"] = s.get("name") or ""
        r["group_name"] = s.get("resource_name") or ""
    return name, roles


def _hcl_resources(roles: list) -> list:
    flows = entitle_tiers.resolve_workflows(_cfg)
    grouped: dict = {}
    for r in roles:
        grouped.setdefault(r["resource"], []).append({
            "key": r["key"], "name": r["name"],
            "virtualized_role_id": r["virtualized_role_id"],
            "workflow_id": flows[r["tier"]]["id"],
            "allowed_durations": list(entitle_tiers.TIER_DURATIONS[r["tier"]]),
        })
    return [{"name": res, "roles": rs} for res, rs in grouped.items()]


# ── Lifecycle ────────────────────────────────────────────────────────────────

def start_create(db: Session, row: ManagedDirectory, *, name: str, roles: list,
                 created_by: str) -> tuple:
    """Record the app and enqueue its apply. ``roles`` must have come from :func:`prepare`."""
    from . import job_service
    app = EntitleVirtualApp(directory_id=row.id, name=name, status="applying",
                            roles_json=json.dumps(roles), created_by=created_by)
    db.add(app)
    db.commit()
    db.refresh(app)
    job = job_service.create_job(db, job_type=JOB_TYPE, created_by=created_by,
                                 metadata={"action": "create", "app_id": app.id})
    return app, job


def start_delete(db: Session, app: EntitleVirtualApp, *, created_by: str):
    from . import job_service
    if app.status in ("applying", "destroying"):
        raise VirtualAppError(f"{app.name} is {app.status} — wait for that job to finish.")
    app.status = "destroying"
    app.error = None
    db.commit()
    return job_service.create_job(db, job_type=JOB_TYPE, created_by=created_by,
                                  metadata={"action": "delete", "app_id": app.id})


async def run_job(db: Session, *, job_id: str, meta: dict) -> None:
    """Worker entry for an ``entitle_virtual_app`` job (``action`` create | delete)."""
    from . import entitle_registration_service as reg
    from . import job_service
    from ..api.websocket import broadcast_progress
    job_service.set_running(db, job_id)
    app = db.query(EntitleVirtualApp).filter(EntitleVirtualApp.id == meta.get("app_id")).first()
    if not app:
        job_service.set_failed(db, job_id, "virtual application record not found")
        return
    action = meta.get("action") or "create"
    try:
        if action == "delete":
            await broadcast_progress(job_id, 20, f"Removing {app.name} from Entitle…")
            if app.tf_state_json:
                await reg.deregister(app.tf_state_json)
            app.status, app.tf_state_json, app.updated_at = "deleted", None, datetime.utcnow()
            db.commit()
            job_service.set_completed(db, job_id, {"action": "delete", "app_id": app.id})
            return

        roles = _loads(app.roles_json, [])
        directory = db.query(ManagedDirectory).filter(
            ManagedDirectory.id == app.directory_id).first()
        tags = ["vm-dashboard", "virtual-app"]
        if directory is not None:
            tags.append(f"directory:{directory.name}"[:60])
        await broadcast_progress(job_id, 20, f"Creating {app.name} with {len(roles)} role(s)…")
        result = await reg.register_virtual_app(name=app.name,
                                                resources=_hcl_resources(roles),
                                                tags=tuple(tags))
        ids = (result.get("outputs") or {}).get("role_ids") or {}
        for r in roles:
            r["role_id"] = str(ids.get(r["key"]) or "")
        app.roles_json = json.dumps(roles)
        app.integration_id = result.get("integration_id")
        app.tf_state_json = result.get("tf_state_json")
        app.status, app.error, app.updated_at = "active", None, datetime.utcnow()
        db.commit()
        job_service.set_completed(db, job_id, {
            "action": "create", "app_id": app.id, "integration_id": app.integration_id,
            "roles": len(roles)})
    except Exception as exc:  # noqa: BLE001 — every failure is recorded on the row
        logger.exception("entitle virtual app job failed app=%s action=%s", app.id, action)
        app.status = "failed"
        app.error = str(exc)[:4000]
        app.updated_at = datetime.utcnow()
        db.commit()
        job_service.set_failed(db, job_id, str(exc))


def get(db: Session, app_id: str) -> Optional[EntitleVirtualApp]:
    return db.query(EntitleVirtualApp).filter(EntitleVirtualApp.id == app_id).first()
