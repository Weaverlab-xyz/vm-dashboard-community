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
from ..database import Job, User, VMWorkgroupOverride, Workgroup

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}[a-z0-9]$")

# Job statuses where a row still holds a workgroup reference that blocks deletion.
_NON_TERMINAL_JOB_STATUSES = ("pending", "running")


class WorkgroupError(ValueError):
    """Raised for service-layer validation failures (404/409/400 at the API edge)."""


class WorkgroupAccessError(WorkgroupError):
    """The workgroup exists, but this user is not a member of it.

    A subclass so a caller that only cares "was the input bad?" still catches it, while
    an API that wants to answer 403 rather than 400 can tell the two apart. Catch this
    one FIRST where both are handled.
    """


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


def canonical_or_none(db: Session, name: Optional[str]) -> Optional[str]:
    """The canonical lowercase name for ``name``, or ``None`` when it is blank.

    The one validator for "a workgroup a resource is being tagged into". Blank is a
    legitimate answer, not an error: a cloud database or K8s cluster may be created
    without a workgroup, in which case it stays creator-scoped. An unknown name is an
    error, because silently dropping it would leave the operator looking at a row they
    believe they shared.

    Returns the stored form deliberately. The name is canonical lowercase in the
    ``workgroups`` table, and everything downstream that compares a workgroup --
    ``inventory_service.row_visible_to``, ``expiry_policy.exempt_workgroups`` -- compares
    lowercase, so a row tagged "Hydra" would be invisible to the workgroup it names.
    """
    if not (name or "").strip():
        return None
    wg = get(db, name)
    if not wg:
        raise WorkgroupError(f"Unknown workgroup '{name}'.")
    return wg.name


def resolve_for_tagging(db: Session, name: Optional[str], *, user=None) -> Optional[str]:
    """The canonical workgroup name to store on a resource, or ``None`` to leave it
    untagged. The one authorization point for "tag this resource into that workgroup".

    Wraps :func:`canonical_or_none` with a membership check, because tagging is not a
    label -- it hands the resource to a set of people. Two failure modes, deliberately
    distinguishable: an unknown name is a :class:`WorkgroupError` (bad input), one the
    user is not in is a :class:`WorkgroupAccessError` (refused).

    The membership check matters even though it looks like it only protects other
    people's resources: the workgroup branch of ``visible_to`` OUTRANKS the creator
    branch, so a user tagging their own database into a workgroup they are not in would
    immediately lose sight of it, with no way back short of an admin retag. Refusing is
    the kinder answer.

    ``user=None`` skips the check, for a caller that has no user to speak of (a seeder,
    a background reconcile). Passing the user is what an HTTP route must do.
    """
    canonical = canonical_or_none(db, name)
    if canonical is None or user is None:
        return canonical
    # is_effective_admin, not is_admin: these two feature areas key on the effective
    # flag everywhere else, and api/mcp_server.py is explicit that the two rules must
    # not be collapsed in the wrong direction.
    if getattr(user, "is_effective_admin", False):
        return canonical
    if canonical not in [w.lower() for w in user.workgroups_list]:
        raise WorkgroupAccessError(
            f"You are not a member of workgroup '{canonical}', so you cannot assign a "
            f"resource to it.")
    return canonical


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

    # Every on-prem VM tagged into this workgroup, across ALL providers — deliberately
    # not filtered to one. This replaced a guard that counted `vm_state_cache` rows, the
    # local VMX scan, which is the one VM source that no longer exists; the overrides it
    # never covered are the ones that matter. `VMWorkgroupOverride.workgroup` is a
    # ForeignKey with ondelete="CASCADE" and nothing enables PRAGMA foreign_keys, so
    # without this the delete silently destroys every override on PostgreSQL and leaves
    # dangling rows on SQLite — and the operator's next clue is a hypervisor page that
    # has quietly gone admin-only.
    vm_refs = (db.query(VMWorkgroupOverride.vm_id)
               .filter(VMWorkgroupOverride.workgroup == canonical).count())
    if vm_refs:
        raise WorkgroupError(
            f"Cannot delete workgroup '{canonical}': {vm_refs} VM(s) are tagged into it. "
            f"Clear or reassign them on their hypervisor page first.")

    # Cloud databases and K8s clusters tagged into this workgroup. The argument for
    # guarding is NOT the one above: these are bare VARCHAR columns with no ForeignKey,
    # so nothing is destroyed or left dangling by the delete. It is operational instead
    # -- the name simply stops resolving, every tagged row silently reverts to
    # "untagged = creator-only", and the operator's first clue is a /databases or /k8s
    # page that has quietly gone admin-only for a team that could use it yesterday.
    #
    # Imported here rather than added to the module-top `from ..database import` line,
    # which is not about circularity -- that import already exists. It keeps this
    # module's import-time surface small on purpose: several test modules stub
    # web_dashboard.database with only the symbols their subject names, and every symbol
    # added up there is one more that every such stub has to grow. Two callers already
    # import workgroup_service lazily for exactly that reason.
    from ..database import CloudDatabase, K8sCluster
    for model, label, page in ((CloudDatabase, "database", "/databases"),
                               (K8sCluster, "Kubernetes cluster", "/k8s")):
        refs = db.query(model.id).filter(model.workgroup == canonical).count()
        if refs:
            raise WorkgroupError(
                f"Cannot delete workgroup '{canonical}': {refs} {label}(s) are tagged "
                f"into it. Reassign or delete them on {page} first.")

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
