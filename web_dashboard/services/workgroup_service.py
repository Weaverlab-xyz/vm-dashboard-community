"""
Workgroup CRUD + seed logic.

Workgroups scope RBAC and cloud-resource visibility. The static
``settings.workgroups`` dict in config.py is the bootstrap-seed source on
first boot only; at runtime everything reads from the ``workgroups`` table.

Names are canonical lowercase (regex enforced) so the string can be written
verbatim into AWS instance tags, Azure resource tags, and GCP labels — all
of which have casing/character constraints tighter than the dashboard UI.
Lookups are case-insensitive so historical TitleCase strings in
``users.workgroups`` and ``oauth_group_mappings.workgroup`` keep resolving
without a data migration.
"""
import json
import re
import uuid
from datetime import datetime
from typing import List, Optional

from sqlalchemy.orm import Session

from ..config import settings
from ..database import Job, User, VMStateCache, Workgroup

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}[a-z0-9]$")

# Job statuses where a row still holds a workgroup reference that blocks deletion.
_NON_TERMINAL_JOB_STATUSES = ("pending", "running")


class WorkgroupError(ValueError):
    """Raised for service-layer validation failures (404/409/400 at the API edge)."""


def _normalize(name: str) -> str:
    return (name or "").strip().lower()


def _validate_name(name: str) -> str:
    n = _normalize(name)
    if not NAME_RE.match(n):
        raise WorkgroupError(
            f"Invalid workgroup name '{name}'. Must be 2–64 chars, lowercase letters/digits/hyphens, "
            "start and end with alphanumeric."
        )
    return n


# ── Queries ───────────────────────────────────────────────────────────────────

def list_all(db: Session) -> List[Workgroup]:
    return db.query(Workgroup).order_by(Workgroup.name).all()


def list_names(db: Session) -> List[str]:
    return [w.name for w in db.query(Workgroup.name).order_by(Workgroup.name).all()]


def get(db: Session, name: str) -> Optional[Workgroup]:
    """Case-insensitive lookup."""
    return db.query(Workgroup).filter(Workgroup.name == _normalize(name)).first()


def exists(db: Session, name: str) -> bool:
    return db.query(Workgroup.id).filter(Workgroup.name == _normalize(name)).first() is not None


def members(db: Session, name: str) -> List[User]:
    """Return all users that include this workgroup in their list."""
    canonical = _normalize(name)
    out: List[User] = []
    for u in db.query(User).all():
        if canonical in [w.lower() for w in u.workgroups_list]:
            out.append(u)
    return out


# ── Mutations ─────────────────────────────────────────────────────────────────

def create(
    db: Session,
    *,
    name: str,
    display_name: str,
    description: Optional[str] = None,
    local_vm_path: Optional[str] = None,
    is_default: bool = False,
    created_by_user_id: Optional[str] = None,
) -> Workgroup:
    canonical = _validate_name(name)
    if not display_name or not display_name.strip():
        raise WorkgroupError("display_name is required.")
    if exists(db, canonical):
        raise WorkgroupError(f"Workgroup '{canonical}' already exists.")

    if is_default:
        # Only one default allowed; clear any prior default.
        for w in db.query(Workgroup).filter(Workgroup.is_default == True).all():
            w.is_default = False

    wg = Workgroup(
        id=str(uuid.uuid4()),
        name=canonical,
        display_name=display_name.strip(),
        description=(description or None),
        local_vm_path=(local_vm_path or None),
        is_default=bool(is_default),
        created_at=datetime.utcnow(),
        created_by_user_id=created_by_user_id,
    )
    db.add(wg)
    db.commit()
    db.refresh(wg)
    return wg


def update(
    db: Session,
    name: str,
    *,
    display_name: Optional[str] = None,
    description: Optional[str] = None,
    local_vm_path: Optional[str] = None,
) -> Workgroup:
    """Update mutable fields. `name` is immutable in v1."""
    wg = get(db, name)
    if not wg:
        raise WorkgroupError(f"Workgroup '{name}' not found.")
    if display_name is not None:
        if not display_name.strip():
            raise WorkgroupError("display_name cannot be empty.")
        wg.display_name = display_name.strip()
    if description is not None:
        wg.description = description.strip() or None
    if local_vm_path is not None:
        wg.local_vm_path = local_vm_path.strip() or None
    db.commit()
    db.refresh(wg)
    return wg


def delete(db: Session, name: str) -> None:
    """Delete a workgroup. Refuses if referenced anywhere or if is_default=True."""
    wg = get(db, name)
    if not wg:
        raise WorkgroupError(f"Workgroup '{name}' not found.")
    if wg.is_default:
        raise WorkgroupError("Default workgroup cannot be deleted.")

    canonical = wg.name

    # Reference checks. Iterating users in Python (not LIKE) avoids false positives
    # where one name is a substring of another (e.g., 'hydra' inside 'hydra-staging').
    user_refs = [u.username for u in db.query(User).all()
                 if canonical in [w.lower() for w in u.workgroups_list]]
    if user_refs:
        raise WorkgroupError(
            f"Cannot delete workgroup '{canonical}': still assigned to {len(user_refs)} user(s): "
            f"{', '.join(user_refs[:5])}{'...' if len(user_refs) > 5 else ''}"
        )

    active_jobs = (
        db.query(Job.id)
        .filter(Job.workgroup == canonical, Job.status.in_(_NON_TERMINAL_JOB_STATUSES))
        .count()
    )
    if active_jobs:
        raise WorkgroupError(f"Cannot delete workgroup '{canonical}': {active_jobs} active job(s) reference it.")

    vm_refs = db.query(VMStateCache.vmx_path).filter(VMStateCache.workgroup == canonical).count()
    if vm_refs:
        raise WorkgroupError(f"Cannot delete workgroup '{canonical}': {vm_refs} VM(s) in local cache reference it.")

    db.delete(wg)
    db.commit()


def assign_user(db: Session, name: str, user: User) -> None:
    canonical = _normalize(name)
    if not exists(db, canonical):
        raise WorkgroupError(f"Workgroup '{name}' not found.")
    current = [w.lower() for w in user.workgroups_list]
    if canonical not in current:
        current.append(canonical)
        user.workgroups_list = current
        db.commit()


def unassign_user(db: Session, name: str, user: User) -> None:
    canonical = _normalize(name)
    current = [w for w in user.workgroups_list if w.lower() != canonical]
    if len(current) != len(user.workgroups_list):
        user.workgroups_list = current
        db.commit()


def validate_user_workgroups(db: Session, names: List[str]) -> None:
    """Raise WorkgroupError if any name in the list doesn't exist."""
    known = {n for n in list_names(db)}
    unknown = [n for n in names if _normalize(n) not in known]
    if unknown:
        raise WorkgroupError(f"Unknown workgroup(s): {', '.join(unknown)}")


# ── Seed ──────────────────────────────────────────────────────────────────────

def seed_if_empty(db: Session) -> None:
    """Populate workgroups on first boot.

    Every install gets a `default` workgroup (is_default=True) so anyone wiring
    up integrations can deploy against it without picking a name. Dev/prod also
    get the `settings.workgroups` dict entries (Hydra, Weaverlab, …) seeded
    alongside, with their UNC paths preserved.
    """
    if db.query(Workgroup.id).first() is not None:
        return

    now = datetime.utcnow()
    # Always seed `default` first so it survives even if the dict has bad entries.
    db.add(Workgroup(
        id=str(uuid.uuid4()),
        name="default",
        display_name="Default",
        description="Default workgroup. Used when no specific workgroup is chosen.",
        is_default=True,
        created_at=now,
    ))

    src = getattr(settings, "workgroups", None) or {}
    for raw_name, path in src.items():
        canonical = _normalize(raw_name)
        if not NAME_RE.match(canonical) or canonical == "default":
            # Skip unseedable names and avoid colliding with the `default` row.
            continue
        db.add(Workgroup(
            id=str(uuid.uuid4()),
            name=canonical,
            display_name=raw_name,
            local_vm_path=path or None,
            is_default=False,
            created_at=now,
        ))
    db.commit()
