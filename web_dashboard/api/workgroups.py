"""
Workgroup CRUD API.

Workgroups scope RBAC and cloud-resource visibility (via AWS `Workgroup` tag,
Azure/GCP `workgroup` tag/label). Names are canonical lowercase; the
``display_name`` field preserves the original casing for UI rendering.
"""
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..database import User, get_db
from ..services import workgroup_service
from ..services.workgroup_service import WorkgroupError
from .auth import get_current_user, has_explicit_permission, require_admin, require_permission

router = APIRouter(prefix="/api/workgroups", tags=["workgroups"])


# ── Schemas ───────────────────────────────────────────────────────────────────

class WorkgroupCreate(BaseModel):
    name: str = Field(..., min_length=2, max_length=64)
    display_name: str = Field(..., min_length=1, max_length=200)
    description: Optional[str] = None
    local_vm_path: Optional[str] = None
    is_default: bool = False


class WorkgroupUpdate(BaseModel):
    display_name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    description: Optional[str] = None
    local_vm_path: Optional[str] = None
    # Constrain every gated change against this workgroup to a maintenance window.
    # None means "leave alone", so an existing caller is unaffected; "" clears the
    # window. See admission_service._enforce_change_window.
    change_window_id: Optional[str] = None
    require_change_window: Optional[bool] = None


class WorkgroupResponse(BaseModel):
    id: str
    name: str
    display_name: str
    description: Optional[str] = None
    local_vm_path: Optional[str] = None
    is_default: bool
    member_count: int
    change_window_id: Optional[str] = None
    require_change_window: bool = False
    # Resolved for display so the Workgroups tab can name the window without a
    # second call, and can say so when the window it points at is gone.
    change_window_name: Optional[str] = None

    class Config:
        from_attributes = True


class WorkgroupDetailResponse(WorkgroupResponse):
    members: List[str]  # usernames


class MemberAssignRequest(BaseModel):
    username: str


# ── Helpers ───────────────────────────────────────────────────────────────────

def _to_response(db: Session, wg) -> WorkgroupResponse:
    return WorkgroupResponse(
        id=wg.id,
        name=wg.name,
        display_name=wg.display_name,
        description=wg.description,
        local_vm_path=wg.local_vm_path,
        is_default=bool(wg.is_default),
        member_count=len(workgroup_service.members(db, wg.name)),
        change_window_id=wg.change_window_id,
        require_change_window=wg.require_change_window is True,
        change_window_name=_window_name(db, wg.change_window_id),
    )


def _window_name(db, window_id):
    """The window's name for display, or None if it is unset or has been deleted.

    None for a deleted one is deliberate rather than an error: the Workgroups tab
    renders `require_change_window` with no name as "misconfigured", which is exactly
    what it is — and what `admission_service` refuses on.
    """
    if not window_id:
        return None
    try:
        from ..database import ChangeWindow
        row = db.query(ChangeWindow).filter(ChangeWindow.id == window_id).first()
        return row.name if row else None
    except Exception:  # noqa: BLE001 — a label must never break the list
        return None


def _raise_from(err: WorkgroupError, default_code: int = 400) -> None:
    msg = str(err)
    code = default_code
    if "not found" in msg.lower():
        code = 404
    elif "already exists" in msg.lower() or "cannot be deleted" in msg.lower() or "still assigned" in msg.lower():
        code = 409
    raise HTTPException(status_code=code, detail=msg)


def _require_steward(db: Session, user: User, name: str):
    """The workstream ``name``, if ``user`` may change it; 403 otherwise (404 if it is gone).

    ``workgroups:write`` is a grantable scope, so on its own it must not reach every
    workstream: a holder could add themselves to anyone's and see, power and destroy what
    is tagged there. So besides an administrator, only someone already in the workstream,
    or the person who created it, may edit it or change who is in it.
    """
    wg = workgroup_service.get(db, name)
    if not wg:
        raise HTTPException(status_code=404, detail=f"Workstream '{name}' not found")
    if getattr(user, "is_effective_admin", False):
        return wg
    mine = {w.lower() for w in (user.workgroups_list or [])}
    if wg.name.lower() in mine or (wg.created_by_user_id and wg.created_by_user_id == user.id):
        return wg
    raise HTTPException(
        status_code=403,
        detail=f"You can change only workstreams you belong to or created; '{wg.name}' is "
               "neither. An administrator can.")


def _audit(db: Session, actor: User, action: str, **details) -> None:
    from ..services import job_service
    job_service.log_audit(db, actor.username, action, details=details)


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("", response_model=List[WorkgroupResponse])
def list_workgroups(
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("workgroups", "read")),
):
    return [_to_response(db, w) for w in workgroup_service.list_all(db)]


@router.get("/{name}", response_model=WorkgroupDetailResponse)
def get_workgroup(
    name: str,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("workgroups", "read")),
):
    wg = workgroup_service.get(db, name)
    if not wg:
        raise HTTPException(status_code=404, detail=f"Workstream '{name}' not found")
    members = [u.username for u in workgroup_service.members(db, wg.name)]
    base = _to_response(db, wg)
    return WorkgroupDetailResponse(**base.model_dump(), members=members)


@router.post("", response_model=WorkgroupResponse, status_code=status.HTTP_201_CREATED)
def create_workgroup(
    payload: WorkgroupCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("workgroups", "write")),
):
    try:
        wg = workgroup_service.create(
            db,
            name=payload.name,
            display_name=payload.display_name,
            description=payload.description,
            local_vm_path=payload.local_vm_path,
            is_default=payload.is_default,
            created_by_user_id=current_user.id,
        )
    except WorkgroupError as exc:
        _raise_from(exc)
    _audit(db, current_user, "workstream_create", workstream=wg.name,
           is_default=bool(payload.is_default))
    return _to_response(db, wg)


@router.patch("/{name}", response_model=WorkgroupResponse)
def update_workgroup(
    name: str,
    payload: WorkgroupUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("workgroups", "write")),
):
    _require_steward(db, current_user, name)
    # The change-window gate is an approval control: whoever can clear it can make changes
    # outside the window it exists to enforce. So it takes the same authority as editing
    # the windows themselves, not just the right to rename a workstream.
    window_fields = {k: v for k, v in (("change_window_id", payload.change_window_id),
                                       ("require_change_window", payload.require_change_window))
                     if v is not None}
    if window_fields and not has_explicit_permission(current_user, "change_windows", "write"):
        raise HTTPException(
            status_code=403,
            detail="Changing a workstream's change window needs the 'change_windows:write' "
                   "permission or administrator.")
    try:
        wg = workgroup_service.update(
            db,
            name,
            display_name=payload.display_name,
            description=payload.description,
            local_vm_path=payload.local_vm_path,
            change_window_id=payload.change_window_id,
            require_change_window=payload.require_change_window,
        )
    except WorkgroupError as exc:
        _raise_from(exc)
    _audit(db, current_user, "workstream_update", workstream=wg.name,
           fields=sorted(k for k, v in payload.model_dump().items() if v is not None))
    return _to_response(db, wg)


@router.delete("/{name}", status_code=status.HTTP_204_NO_CONTENT)
def delete_workgroup(
    name: str,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
):
    try:
        workgroup_service.delete(db, name)
    except WorkgroupError as exc:
        _raise_from(exc)


class WorkgroupProfile(BaseModel):
    """A workstream's deploy defaults. Every field optional: on a PUT, a field left out is
    unchanged and a field sent as null goes back to the global default."""
    pra_enabled: Optional[bool] = None
    jump_group: Optional[str] = Field(None, max_length=255)
    jumpoint_name: Optional[str] = Field(None, max_length=255)
    register_in_entitle: Optional[bool] = None
    register_in_passwordsafe: Optional[bool] = None
    enable_rdp: Optional[bool] = None
    entra_join_mode: Optional[str] = Field(None, max_length=16)


@router.get("/{name}/profile", response_model=WorkgroupProfile)
def get_workgroup_profile(
    name: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """The workstream's deploy defaults — what the deploy forms pre-fill when it is
    picked. Readable by anyone who may deploy into it (its members), its creator and an
    administrator; a field it does not set is null, meaning the global default."""
    wg = _require_steward(db, current_user, name)
    return WorkgroupProfile(**workgroup_service.profile(wg.name, db))


@router.put("/{name}/profile", response_model=WorkgroupProfile)
def put_workgroup_profile(
    name: str,
    payload: WorkgroupProfile,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("workgroups", "write")),
):
    """Set the workstream's deploy defaults. Only the fields sent change; null clears one.
    The keys are a fixed list (workgroup_service.PROFILE_KEYS) and hold no secrets, which
    is why the same people who may edit the workstream may set them."""
    wg = _require_steward(db, current_user, name)
    values = payload.model_dump(exclude_unset=True)
    try:
        out = workgroup_service.set_profile(db, wg.name, values)
    except WorkgroupError as exc:
        _raise_from(exc)
    _audit(db, current_user, "workstream_profile_update", workstream=wg.name,
           fields=sorted(values))
    return WorkgroupProfile(**out)


def enforce_secret_bindings(cloud: str, req, workgroup, user) -> None:
    """For the cloud deploy routes: fill ``req``'s SSH key secret and PRA credential from
    the workstream's bindings, or refuse (403) a non-admin who names other ones. Call it
    right after the workstream is resolved and before the override is validated or the
    request fans out — see workgroup_service.apply_secret_bindings."""
    try:
        workgroup_service.apply_secret_bindings(cloud, req, workgroup, user)
    except workgroup_service.SecretBindingDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc))


class SecretBindings(BaseModel):
    """Secret REFERENCES bound to a workstream — names and refs, never values. On a PUT
    a field left out is unchanged; in ``ssh_key_secret`` only the clouds named change,
    and null or blank unbinds."""
    ssh_key_secret: Optional[dict] = None
    pra_credential_ref: Optional[str] = Field(None, max_length=1024)


@router.get("/{name}/secret-bindings", response_model=SecretBindings)
def get_secret_bindings(
    name: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """What this workstream's deploys use, so the deploy form can say so. The names are
    no secret to the people who deploy with them; the values are never returned."""
    wg = _require_steward(db, current_user, name)
    return SecretBindings(**workgroup_service.bindings(wg.name, db))


@router.put("/{name}/secret-bindings", response_model=SecretBindings)
def put_secret_bindings(
    name: str,
    payload: SecretBindings,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Administrator only: binding a secret decides what every member's deploys use, and
    a member who could set it could point their team at any secret the dashboard reads."""
    values = payload.model_dump(exclude_unset=True)
    try:
        out = workgroup_service.set_bindings(db, name, values)
    except WorkgroupError as exc:
        _raise_from(exc)
    _audit(db, current_user, "workstream_secret_bindings_update", workstream=name.lower(),
           fields=sorted(values),
           clouds=sorted((values.get("ssh_key_secret") or {}).keys()))
    return SecretBindings(**out)


class PostDeployRun(BaseModel):
    asset: str = Field(..., min_length=1, max_length=255)
    # Blank = the active storage backend when the run is queued.
    asset_backend: str = Field("", max_length=32)


class PostDeployRuns(BaseModel):
    runs: List[PostDeployRun] = []


@router.get("/{name}/post-deploy-runs", response_model=PostDeployRuns)
def get_post_deploy_runs(
    name: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """The assets run on every VM deployed into this workstream, in order — so the
    deploy form can say what will run, and the people deploying can see it."""
    wg = _require_steward(db, current_user, name)
    return PostDeployRuns(runs=workgroup_service.post_deploy_runs(wg.name, db))


@router.put("/{name}/post-deploy-runs", response_model=PostDeployRuns)
def put_post_deploy_runs(
    name: str,
    payload: PostDeployRuns,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Administrator only: each entry is code that runs on every VM the workstream's
    members deploy, so choosing it takes the same authority as binding its secrets."""
    try:
        out = workgroup_service.set_post_deploy_runs(
            db, name, [r.model_dump() for r in payload.runs])
    except WorkgroupError as exc:
        _raise_from(exc)
    _audit(db, current_user, "workstream_post_deploy_runs_update", workstream=name.lower(),
           assets=[r["asset"] for r in out])
    return PostDeployRuns(runs=out)


@router.get("/{name}/members", response_model=List[str])
def list_members(
    name: str,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("workgroups", "read")),
):
    if not workgroup_service.exists(db, name):
        raise HTTPException(status_code=404, detail=f"Workstream '{name}' not found")
    return [u.username for u in workgroup_service.members(db, name)]


@router.post("/{name}/members/{username}", status_code=status.HTTP_204_NO_CONTENT)
def assign_member(
    name: str,
    username: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("workgroups", "write")),
):
    _require_steward(db, current_user, name)
    user = db.query(User).filter(User.username == username).first()
    if not user:
        raise HTTPException(status_code=404, detail=f"User '{username}' not found")
    try:
        workgroup_service.assign_user(db, name, user)
    except WorkgroupError as exc:
        _raise_from(exc)
    _audit(db, current_user, "workstream_member_add", workstream=name, user=username)


@router.delete("/{name}/members/{username}", status_code=status.HTTP_204_NO_CONTENT)
def unassign_member(
    name: str,
    username: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("workgroups", "write")),
):
    _require_steward(db, current_user, name)
    user = db.query(User).filter(User.username == username).first()
    if not user:
        raise HTTPException(status_code=404, detail=f"User '{username}' not found")
    workgroup_service.unassign_user(db, name, user)
    _audit(db, current_user, "workstream_member_remove", workstream=name, user=username)
