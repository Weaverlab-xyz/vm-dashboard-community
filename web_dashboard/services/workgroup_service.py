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
from ..utils.clock import utcnow

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
            f"Invalid workstream name '{name}'. Must be 2–64 chars, lowercase letters/digits/hyphens, "
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
        raise WorkgroupError(f"Unknown workstream '{name}'.")
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
            f"You are not a member of workstream '{canonical}', so you cannot assign a "
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
        raise WorkgroupError(f"Workstream '{canonical}' already exists.")

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
        created_at=utcnow(),
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
    change_window_id: Optional[str] = None,
    require_change_window: Optional[bool] = None,
) -> Workgroup:
    """Update mutable fields. `name` is immutable in v1.

    ``change_window_id`` / ``require_change_window`` constrain every gated change
    against this workgroup to a maintenance window — see
    ``admission_service._enforce_change_window``. Each is ``None`` for "leave alone",
    so an existing caller that passes neither is unaffected. Passing an empty string
    for the window clears it.
    """
    wg = get(db, name)
    if not wg:
        raise WorkgroupError(f"Workstream '{name}' not found.")
    if display_name is not None:
        if not display_name.strip():
            raise WorkgroupError("display_name cannot be empty.")
        wg.display_name = display_name.strip()
    if description is not None:
        wg.description = description.strip() or None
    if local_vm_path is not None:
        wg.local_vm_path = local_vm_path.strip() or None
    if change_window_id is not None:
        wg.change_window_id = change_window_id.strip() or None
    if require_change_window is not None:
        # `or None` so False is stored as NULL, matching the column's "NULL means as
        # today" contract and keeping an un-constrained workgroup indistinguishable
        # from one that predates the feature.
        wg.require_change_window = bool(require_change_window) or None
    # Refuse the half-configured state outright rather than storing something whose
    # behaviour nobody can predict from the row: `admission_service` fails CLOSED on a
    # workgroup that requires a window it cannot resolve, so "required, but no window"
    # would silently block every change against it.
    if wg.require_change_window is True and not wg.change_window_id:
        raise WorkgroupError(
            "Pick a change window before requiring one — otherwise every change "
            "against this workstream would be refused with nothing to book it into.")
    db.commit()
    db.refresh(wg)
    return wg


def delete(db: Session, name: str) -> None:
    """Delete a workgroup. Refuses if referenced anywhere or if is_default=True."""
    wg = get(db, name)
    if not wg:
        raise WorkgroupError(f"Workstream '{name}' not found.")
    if wg.is_default:
        raise WorkgroupError("Default workstream cannot be deleted.")

    canonical = wg.name

    # Reference checks. Iterating users in Python (not LIKE) avoids false positives
    # where one name is a substring of another (e.g., 'hydra' inside 'hydra-staging').
    user_refs = [u.username for u in db.query(User).all()
                 if canonical in [w.lower() for w in u.workgroups_list]]
    if user_refs:
        raise WorkgroupError(
            f"Cannot delete workstream '{canonical}': still assigned to {len(user_refs)} user(s): "
            f"{', '.join(user_refs[:5])}{'...' if len(user_refs) > 5 else ''}"
        )

    active_jobs = (
        db.query(Job.id)
        .filter(Job.workgroup == canonical, Job.status.in_(_NON_TERMINAL_JOB_STATUSES))
        .count()
    )
    if active_jobs:
        raise WorkgroupError(f"Cannot delete workstream '{canonical}': {active_jobs} active job(s) reference it.")

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
            f"Cannot delete workstream '{canonical}': {vm_refs} VM(s) are tagged into it. "
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
                f"Cannot delete workstream '{canonical}': {refs} {label}(s) are tagged "
                f"into it. Reassign or delete them on {page} first.")

    # Stored assets scoped to it: deleting the workstream would leave them visible to
    # no one but administrators, with nothing on the Storage page saying why.
    from . import asset_scope_service
    assets = asset_scope_service.count_for(db, canonical)
    if assets:
        raise WorkgroupError(
            f"Cannot delete workstream '{canonical}': {assets} stored asset(s) are scoped "
            f"to it. Make them global or scope them elsewhere on /storage first.")

    db.delete(wg)
    db.commit()


def assign_user(db: Session, name: str, user: User) -> None:
    canonical = _normalize(name)
    if not exists(db, canonical):
        raise WorkgroupError(f"Workstream '{name}' not found.")
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
        raise WorkgroupError(f"Unknown workstream(s): {', '.join(unknown)}")


# ── Deploy defaults (the workstream profile) ──────────────────────────────────
#
# What a workstream's VMs get unless the deploy form says otherwise: whether they go into
# PRA and where, and the integration and Windows identity choices the form pre-fills.
# Held on the workstream's own row (``workgroups.deploy_defaults``, JSON), so they go
# when it does. Not app_config: its key is the primary key, so one key can hold one
# value for the whole install, never one per workstream.
#
# An ALLOWLIST, not a pass-through: anyone in a workstream may edit it (api/workgroups.py
# _require_steward), so nothing here may stand in for a global setting or a secret.
# Binding a secret to a workstream needs an administrator and is not done here.

PROFILE_KEYS = {
    # key: (type, allowed values or None)
    "pra_enabled": ("bool", None),
    "jump_group": ("str", None),
    "jumpoint_name": ("str", None),
    "register_in_entitle": ("bool", None),
    "register_in_passwordsafe": ("bool", None),
    "enable_rdp": ("bool", None),
    "entra_join_mode": ("str", ("", "arc", "hybrid")),
}


def _read_defaults(wg: Optional[Workgroup]) -> dict:
    # getattr: a stand-in workstream object (tests stub get(), and so may a caller) that
    # predates the column reads as "sets nothing", never as an AttributeError mid-deploy.
    stored = getattr(wg, "deploy_defaults", None) if wg is not None else None
    if not stored:
        return {}
    try:
        raw = json.loads(stored)
    except (ValueError, TypeError):
        return {}
    # Only known keys of the right type survive a read, so a row edited by hand cannot
    # smuggle a value past the checks set_profile makes.
    out = {}
    for key, (kind, allowed) in PROFILE_KEYS.items():
        if key not in raw:
            continue
        v = raw[key]
        if kind == "bool" and isinstance(v, bool):
            out[key] = v
        elif kind == "str" and isinstance(v, str) and (allowed is None or v in allowed):
            out[key] = v
    return out


def profile(name: Optional[str], db: Optional[Session] = None) -> dict:
    """A workstream's deploy defaults: ``{key: value}`` for each key it sets. A key it
    leaves unset is absent, which every reader takes as "use the global default" — never
    as False. Unknown or missing workstream → ``{}``.

    Opens its own session when given none: the deploy runners ask this mid-job, where the
    session they hold belongs to the job."""
    wg_name = _normalize(name or "")
    if not wg_name:
        return {}
    if db is not None:
        return _read_defaults(get(db, wg_name))
    from ..database import SessionLocal
    own = SessionLocal()
    try:
        return _read_defaults(get(own, wg_name))
    finally:
        own.close()


def set_profile(db: Session, name: str, values: dict) -> dict:
    """Change the given keys of a workstream's defaults. ``None`` — or a blank name —
    clears a key back to the global default; a key outside PROFILE_KEYS is refused and
    nothing is written. Returns the new profile."""
    wg = get(db, name)
    if wg is None:
        raise WorkgroupError(f"Workstream '{name}' not found.")
    unknown = sorted(set(values) - set(PROFILE_KEYS))
    if unknown:
        raise WorkgroupError(f"Not a workstream default: {', '.join(unknown)}")
    current = _read_defaults(wg)
    for key, value in values.items():
        kind, allowed = PROFILE_KEYS[key]
        if value is None:
            current.pop(key, None)
        elif kind == "bool":
            if not isinstance(value, bool):
                raise WorkgroupError(f"'{key}' must be true, false or null")
            current[key] = value
        else:
            text = str(value).strip()
            if allowed is not None and text not in allowed:
                raise WorkgroupError(
                    f"'{key}' must be one of: {', '.join(repr(a) for a in allowed)}")
            if len(text) > 255:
                raise WorkgroupError(f"'{key}' is too long")
            if not text and allowed is None:
                current.pop(key, None)
            else:
                current[key] = text
    wg.deploy_defaults = json.dumps(current, sort_keys=True) if current else None
    db.commit()
    return dict(current)


# ── Secret bindings (administrator only) ──────────────────────────────────────
#
# References — a secret's name or an aws_sm:// style ref, never its value — that a
# workstream's deploys use: the SSH key pair secret on each cloud, and the PRA credential
# a jump is provisioned with. Bound by an administrator; a member could otherwise point
# their team's deploys at any secret the dashboard's cloud identity can read.
#
# Once one is bound, a deploy into the workstream that names none gets it, and a
# non-admin may not name a different one (apply_secret_bindings). A workstream with
# nothing bound behaves exactly as before.

SECRET_CLOUDS = ("aws", "azure", "gcp", "oci")
_MAX_REF = 1024


class SecretBindingDenied(WorkgroupAccessError):
    """A non-admin asked for a secret other than the one bound to their workstream."""


def _read_bindings(wg: Optional[Workgroup]) -> dict:
    out = {"ssh_key_secret": {}, "pra_credential_ref": None}
    stored = getattr(wg, "secret_bindings", None) if wg is not None else None   # see _read_defaults
    if not stored:
        return out
    try:
        raw = json.loads(stored)
    except (ValueError, TypeError):
        return out
    ssh = raw.get("ssh_key_secret") if isinstance(raw, dict) else None
    if isinstance(ssh, dict):
        out["ssh_key_secret"] = {c: v for c, v in ssh.items()
                                 if c in SECRET_CLOUDS and isinstance(v, str) and v}
    pra = raw.get("pra_credential_ref") if isinstance(raw, dict) else None
    if isinstance(pra, str) and pra:
        out["pra_credential_ref"] = pra
    return out


def bindings(name: Optional[str], db: Optional[Session] = None) -> dict:
    """``{"ssh_key_secret": {cloud: ref}, "pra_credential_ref": ref | None}`` for a
    workstream; empty when it binds nothing or does not exist."""
    wg_name = _normalize(name or "")
    if not wg_name:
        return _read_bindings(None)
    if db is not None:
        return _read_bindings(get(db, wg_name))
    from ..database import SessionLocal
    own = SessionLocal()
    try:
        return _read_bindings(get(own, wg_name))
    finally:
        own.close()


def _ref(value, what: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise WorkgroupError(f"{what} must be a string or null")
    text = value.strip()
    if len(text) > _MAX_REF:
        raise WorkgroupError(f"{what} is too long")
    return text or None


def set_bindings(db: Session, name: str, values: dict) -> dict:
    """Change a workstream's bound secrets. ``ssh_key_secret`` is a per-cloud map and
    only the clouds it names change; ``pra_credential_ref`` replaces when present. A
    null or blank reference unbinds. The caller must be an administrator."""
    wg = get(db, name)
    if wg is None:
        raise WorkgroupError(f"Workstream '{name}' not found.")
    unknown = sorted(set(values) - {"ssh_key_secret", "pra_credential_ref"})
    if unknown:
        raise WorkgroupError(f"Not a bindable secret: {', '.join(unknown)}")
    current = _read_bindings(wg)
    if "ssh_key_secret" in values:
        ssh = values["ssh_key_secret"] or {}
        if not isinstance(ssh, dict):
            raise WorkgroupError("ssh_key_secret must map a cloud to a secret reference")
        bad = sorted(set(ssh) - set(SECRET_CLOUDS))
        if bad:
            raise WorkgroupError(f"Unknown cloud: {', '.join(bad)}")
        for cloud, value in ssh.items():
            ref = _ref(value, f"ssh_key_secret.{cloud}")
            if ref:
                current["ssh_key_secret"][cloud] = ref
            else:
                current["ssh_key_secret"].pop(cloud, None)
    if "pra_credential_ref" in values:
        ref = _ref(values["pra_credential_ref"], "pra_credential_ref")
        if ref:
            # A team vault resolves only for its own workstream; bound anywhere else it
            # would resolve to nothing on every deploy.
            from . import secret_vault_service
            vault, owner = secret_vault_service.vault_of(ref)
            if owner and owner != wg.name:
                raise WorkgroupError(
                    f"pra_credential_ref reads from vault '{vault}', which belongs to "
                    f"workstream '{owner}'")
        current["pra_credential_ref"] = ref
    empty = not current["ssh_key_secret"] and not current["pra_credential_ref"]
    wg.secret_bindings = None if empty else json.dumps(current, sort_keys=True)
    db.commit()
    return current


def apply_secret_bindings(cloud: str, req, workgroup: Optional[str], user) -> None:
    """Fill a deploy request's secret references from its workstream's bindings, and
    refuse a non-admin who names a different one. Mutates ``req`` in place, BEFORE the
    caller validates the override and fans out, so every child of a batch, the job
    metadata and the runner all see the reference actually used.

    Raises SecretBindingDenied for a non-admin asking for another secret than the bound
    one. Fields a request model does not have (an older model) are skipped."""
    bound = bindings(workgroup)
    is_admin = bool(getattr(user, "is_effective_admin", False))
    for field, ref, label in (
        ("ssh_key_secret_override", bound["ssh_key_secret"].get(cloud), "SSH key secret"),
        ("pra_credential_ref", bound["pra_credential_ref"], "PRA credential"),
    ):
        if not ref or not hasattr(req, field):
            continue
        asked = (getattr(req, field) or "").strip()
        if not asked:
            setattr(req, field, ref)
        elif asked != ref and not is_admin:
            raise SecretBindingDenied(
                f"This workstream's {label} is set by an administrator; leave it blank "
                "to use it, or ask an administrator to change it.")


# ── Post-deploy runs (administrator only) ─────────────────────────────────────
#
# Config-Management assets — playbooks, scripts, packages — run on every VM deployed
# into the workstream, in order, once it is up: an agent install, a hardening baseline.
# Administrator-only like the secret bindings: each entry is code that will run on every
# new machine the workstream's members deploy. services/post_deploy_service queues them.

POST_DEPLOY_MAX = 10


def _read_runs(wg: Optional[Workgroup]) -> list:
    stored = getattr(wg, "post_deploy_runs", None) if wg is not None else None   # see _read_defaults
    if not stored:
        return []
    try:
        raw = json.loads(stored)
    except (ValueError, TypeError):
        return []
    out = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        asset = item.get("asset")
        backend = item.get("asset_backend") or ""
        if isinstance(asset, str) and asset and isinstance(backend, str):
            out.append({"asset": asset, "asset_backend": backend})
    return out[:POST_DEPLOY_MAX]


def post_deploy_runs(name: Optional[str], db: Optional[Session] = None) -> list:
    """The workstream's post-deploy runs, ``[{asset, asset_backend}]`` in run order;
    ``[]`` when it has none or does not exist."""
    wg_name = _normalize(name or "")
    if not wg_name:
        return []
    if db is not None:
        return _read_runs(get(db, wg_name))
    from ..database import SessionLocal
    own = SessionLocal()
    try:
        return _read_runs(get(own, wg_name))
    finally:
        own.close()


def set_post_deploy_runs(db: Session, name: str, runs) -> list:
    """Replace a workstream's post-deploy runs. Each entry names an asset and, optionally,
    the storage backend it lives on (blank = the active backend at run time). The caller
    must be an administrator."""
    wg = get(db, name)
    if wg is None:
        raise WorkgroupError(f"Workstream '{name}' not found.")
    if not isinstance(runs, list):
        raise WorkgroupError("runs must be a list")
    if len(runs) > POST_DEPLOY_MAX:
        raise WorkgroupError(f"At most {POST_DEPLOY_MAX} post-deploy runs per workstream")
    clean = []
    for i, item in enumerate(runs):
        if not isinstance(item, dict):
            raise WorkgroupError(f"run {i + 1} must be an object with an asset")
        asset = item.get("asset")
        backend = item.get("asset_backend") or ""
        if not isinstance(asset, str) or not asset.strip():
            raise WorkgroupError(f"run {i + 1} names no asset")
        asset = asset.strip()
        if len(asset) > 255 or ".." in asset or asset.startswith("/"):
            raise WorkgroupError(f"run {i + 1}: '{asset[:60]}' is not an asset name")
        if not isinstance(backend, str) or len(backend) > 32:
            raise WorkgroupError(f"run {i + 1}: bad storage backend")
        backend = backend.strip()
        # A post-deploy run executes on every VM this workstream deploys, so it may only
        # use an asset that is global or scoped to this workstream.
        from . import asset_scope_service
        owner = asset_scope_service.scope_of(db, backend, asset)
        if owner and owner != wg.name:
            raise WorkgroupError(
                f"run {i + 1}: '{asset}' is scoped to workstream '{owner}', not this one")
        clean.append({"asset": asset, "asset_backend": backend})
    wg.post_deploy_runs = json.dumps(clean) if clean else None
    db.commit()
    return clean


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

    now = utcnow()
    # Always seed `default` first so it survives even if the dict has bad entries.
    db.add(Workgroup(
        id=str(uuid.uuid4()),
        name="default",
        display_name="Default",
        description="Default workstream. Used when no specific workstream is chosen.",
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
