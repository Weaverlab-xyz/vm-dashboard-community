"""
User management API endpoints (admin only).
All routes require the authenticated user to have is_admin=True.
"""
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import User, Fido2Credential, PersonalAccessToken, get_db, get_password_hash
from ..models.user import UserResponse
from ..services import role_service
from .auth import (PERMISSION_SCOPES, get_current_user, require_admin,
                   validate_permissions_payload)
from .tokens import _generate_raw, hash_pat, TokenCreateResponse

router = APIRouter(prefix="/api/users", tags=["users"])


# ── Pydantic models ────────────────────────────────────────────────────────────

class UserCreateRequest(BaseModel):
    username: str
    password: str
    full_name: Optional[str] = None
    email: Optional[str] = None
    workgroups: List[str] = []
    is_admin: bool = False
    # Same three-state meaning as the PATCH below, and the reason it is here at all:
    # omitting it stored NULL, and NULL means UNRESTRICTED in `has_permission`. Every user
    # the admin UI created therefore held every non-admin permission in the dashboard until
    # somebody re-opened them in Edit. The UI now always sends this; None is kept as "no
    # permissions given" for API callers that predate the field, so they are unchanged.
    permissions: Optional[dict] = None
    # Which POVs the new user may reach. Same column, same meaning as on update -- [] or
    # omitted is every POV their `pov` scope allows.
    pov_env_ids: Optional[List[str]] = None
    # The access role to assign. Omitted = no role, which is the pre-roles behaviour and
    # leaves `permissions` above as the only statement about this user's access.
    role_id: Optional[str] = None


class UserUpdateRequest(BaseModel):
    full_name: Optional[str] = None
    email: Optional[str] = None
    workgroups: Optional[List[str]] = None
    is_active: Optional[bool] = None
    is_admin: Optional[bool] = None
    password: Optional[str] = None   # supply to reset password
    permissions: Optional[dict] = None  # None = no change; {} = clear (full access); dict = set specific perms
    # Which POVs this user may reach. None = no change; [] = clear the narrowing (every
    # POV their `pov` scope allows); a list = exactly those. Separate from `permissions`
    # because it is an OBJECT list, not a level set -- the scope says what they may do,
    # this says which environments they may do it to.
    pov_env_ids: Optional[List[str]] = None
    # Which role's material the dashboard leads with for this user. NOT a permission --
    # services/personas may only reorder and surface. "" clears the assignment and hands
    # the user back to their OIDC group's focus, or the instance default; None leaves it
    # alone, like every field above.
    persona: Optional[str] = None
    # The access role this user holds. Three-state, the same contract `persona` above has
    # and for the same reason: None = no change, "" = clear the assignment, an id = set it.
    # Without the empty-string case an admin could assign a role and never remove one.
    role_id: Optional[str] = None


class UserTokenItem(BaseModel):
    id: str
    name: str
    created_at: datetime
    expires_at: Optional[datetime] = None
    last_used_at: Optional[datetime] = None
    is_active: bool


# ── List users ─────────────────────────────────────────────────────────────────

# POV accessors are not listed or edited here, and both halves matter.
#
# Not LISTED because this page is about the people who run this dashboard, and an accessor
# is a prospect's ephemeral login that belongs to a POV: it is created from the POV's
# Access tab, revoked there, and reaped with the environment. A list mixing the two invites
# an admin to "tidy up" a login the POV is still using.
#
# Not EDITED because a PATCH here is an escalation path — `is_admin: true` on an accessor
# row, or clearing `accessor_env_id`, turns a confined prospect into an operator with two
# keystrokes and no audit line naming what happened. So every mutation route refuses one
# and says where the real control is.
_NOT_AN_ACCESSOR = User.accessor_env_id.is_(None)


def _nothing_granted() -> dict:
    """Every scope, no levels: a restricted map that grants nothing.

    NOT ``{}``. An empty map is stored as NULL, and NULL means UNRESTRICTED in
    ``has_permission`` -- every section in the dashboard. This is the only way to write
    "no access yet" that the server reads as no access.
    """
    return {scope: [] for scope in PERMISSION_SCOPES}


def _admin_here(user: User) -> bool:
    """Administrator by the two things this page controls: the Admin flag, or the
    Administrator role. Session and Entitle grants are not set here, so a request on this
    page cannot remove them and they are left out of the comparison."""
    return bool(user.is_admin) or bool(user.role_permissions_dict.get("is_admin"))


def _refuse_accessor(user: User) -> None:
    if user is not None and user.accessor_env_id:
        raise HTTPException(
            status_code=409,
            detail="This is a POV accessor login. It is managed from its POV's Access "
                   "tab and removed when the POV is destroyed — edit or revoke it there.")


def _refuse_service_account_escalation(db: Session, user: User, *, is_admin=None,
                                       password=None, role_id=None) -> None:
    """A service account never becomes an administrator and never gets a password.

    ``User.is_effective_admin`` already answers False for one whatever its columns say,
    so this is not the only guard -- it is the one that stops the page from STORING a
    statement the rest of the dashboard will then silently ignore.
    """
    if user is None or not user.is_service_account:
        return
    if is_admin:
        raise HTTPException(status_code=400,
                            detail="A service account cannot be an administrator.")
    if password:
        raise HTTPException(
            status_code=400,
            detail="A service account has no password. It authenticates with an OAuth "
                   "client — manage those under its OAuth clients.")
    if role_id:
        role = _resolve_role(db, role_id)
        if role is not None and role.permissions_dict.get("is_admin"):
            raise HTTPException(
                status_code=400,
                detail="A service account cannot hold an administrator role.")


def _resolve_role(db: Session, raw: Optional[str]):
    """A role id from a request body to an `AccessRole`, or None to clear. 422 if unknown.

    Shared by create and update so the two cannot drift on what an unknown id means. A 422
    rather than silently storing it: an id with no role behind it makes
    `effective_permissions_dict` return the deny sentinel, so the user would be able to do
    nothing at all and the admin would have no row to look at.
    """
    if raw is None or not str(raw).strip():
        return None
    role = role_service.get(db, str(raw).strip())
    if role is None:
        raise HTTPException(status_code=422, detail=f"Unknown role '{raw}'")
    return role


@router.get("", response_model=List[UserResponse])
def list_users(
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    users = db.query(User).filter(_NOT_AN_ACCESSOR).order_by(User.username).all()
    # One lookup for the whole page rather than one per row: the list is the only place
    # where a per-user role fetch would be an N+1.
    _role_names = {r.id: r.name for r in role_service.list_all(db)}
    return [
        UserResponse(
            id=u.id,
            username=u.username,
            full_name=u.full_name,
            email=u.email,
            workgroups=u.workgroups_list,
            is_active=u.is_active,
            is_admin=u.is_admin or False,
            auth_provider=u.auth_provider,
            mfa_required=u.mfa_required,
            is_service_account=bool(u.is_service_account),
            permissions=u.permissions_dict or None,
            pov_env_ids=u.pov_env_ids_list,
            # The role's NAME as well as its id, so the list renders the assignment
            # without a second request per row.
            role_id=u.role_id or "",
            role_name=_role_names.get(u.role_id, ""),
            # The ASSIGNED value, not the resolved one: an admin editing this row needs to
            # see what is stored here, and `persona_source` tells them when the focus a
            # user actually gets comes from their group instead.
            persona=u.persona or "",
            persona_source=("user" if u.persona else "group" if u.session_persona else ""),
        )
        for u in users
    ]


# ── Create user ────────────────────────────────────────────────────────────────

@router.post("", response_model=UserResponse, status_code=201)
def create_user(
    body: UserCreateRequest,
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if db.query(User).filter(User.username == body.username).first():
        raise HTTPException(status_code=409, detail="Username already exists")
    user = User(
        username=body.username,
        hashed_password=get_password_hash(body.password),
        full_name=body.full_name,
        email=body.email,
        is_active=True,
        is_admin=body.is_admin,
    )
    user.workgroups_list = body.workgroups
    if body.permissions is not None:
        # Validated, never stored raw -- the same treatment the PATCH path gives it, for
        # the same reason: an unknown scope persisted here is invisible to the grid and
        # permanent. An empty map still means unrestricted; the UI expresses "restricted,
        # nothing granted" as every scope present with an empty level list, which is a
        # non-empty map and so a strict allowlist.
        validate_permissions_payload(body.permissions)
        user.permissions_dict = body.permissions if body.permissions else None
    elif body.is_admin:
        # The form sends no map for an administrator, because the flag bypasses it. Stored
        # as NULL that would mean UNRESTRICTED, silently, the day the flag came off -- so an
        # administrator is created with nothing granted underneath instead.
        user.permissions_dict = _nothing_granted()
    if body.pov_env_ids is not None:
        user.pov_env_ids_list = body.pov_env_ids
    if body.role_id is not None:
        # Assigned through role_service, which is the single writer of the two role columns
        # and always writes them together -- an id without its materialised copy is a user
        # who can do nothing, by way of the deny sentinel in database.py.
        role_service.apply_role_to_user(db, user, _resolve_role(db, body.role_id))
    db.add(user)
    db.commit()
    db.refresh(user)
    return UserResponse(
        id=user.id,
        username=user.username,
        full_name=user.full_name,
        email=user.email,
        workgroups=user.workgroups_list,
        is_active=user.is_active,
        is_admin=user.is_admin or False,
        auth_provider=user.auth_provider,
        mfa_required=user.mfa_required,
        permissions=user.permissions_dict or None,
        pov_env_ids=user.pov_env_ids_list,
        role_id=user.role_id or "",
        role_name=(role_service.get(db, user.role_id).name if user.role_id else ""),
    )


# ── Update user ────────────────────────────────────────────────────────────────

@router.patch("/{user_id}", response_model=UserResponse)
def update_user(
    user_id: str,
    body: UserUpdateRequest,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    _refuse_accessor(user)
    _refuse_service_account_escalation(db, user, is_admin=body.is_admin,
                                       password=body.password, role_id=body.role_id)
    # Prevent admins from removing their own admin flag
    if user.id == admin.id and body.is_admin is False:
        raise HTTPException(status_code=400, detail="Cannot remove your own admin privilege")
    was_admin = _admin_here(user)

    if body.full_name is not None:
        user.full_name = body.full_name
    if body.email is not None:
        user.email = body.email
    if body.workgroups is not None:
        user.workgroups_list = body.workgroups
    if body.is_active is not None:
        user.is_active = body.is_active
    if body.is_admin is not None:
        user.is_admin = body.is_admin
    if body.password:
        user.hashed_password = get_password_hash(body.password)
    if body.persona is not None:
        # Validated against the registry, never stored raw: a free-text column here would
        # be a focus that resolves to nothing and reads as "unset" with no way to tell why.
        # Writes `persona`, never `session_persona` -- that one belongs to the login path.
        #
        # Refused on an instance with no focus axis rather than stored inert: a value
        # nothing resolves would start being honoured the day that instance was
        # reconfigured to an estate one. Clearing is allowed on both profiles, matching
        # groups.py::_valid_persona and the masked-flag rule in api/setup.py.
        from ..services import feature_flags, personas
        want = (body.persona or "").strip().lower()
        if want and not personas.applies():
            raise HTTPException(
                status_code=409,
                detail=f"A focus cannot be assigned on {feature_flags.profile_noun()}.")
        if want and want not in personas.VALID_PERSONAS:
            raise HTTPException(status_code=422, detail=f"Unknown persona '{want}'")
        user.persona = want or None
    if body.permissions is not None:
        # Validated against the catalog, never stored raw -- the same treatment `persona`
        # gets above, and for the same reason. Before this, an unknown scope was persisted
        # verbatim, then round-tripped on every subsequent save by a grid that only renders
        # keys it knows: invisible, permanent, and still granting if any route was ever
        # gated on that string. Note api/entitle_rest.py has validated its own input since
        # day one; it was only the human admin path that did not.
        validate_permissions_payload(body.permissions)
        # Empty dict {} clears restrictions (full access); non-empty dict sets specific perms
        user.permissions_dict = body.permissions if body.permissions else None
    if body.role_id is not None:
        # Refuse an admin changing their OWN role, mirroring the is_admin guard above and
        # for a strictly stronger reason: a role can be what confers their admin, so
        # swapping it can lock them out of this very page with no way back short of the
        # database. Another administrator can do it.
        if user.id == admin.id:
            raise HTTPException(
                status_code=400,
                detail="Cannot change your own role — ask another administrator.")
        # Same refusal `pov_env_ids` gets below, for the same reason: an accessor is
        # confined by a path allowlist no permission can widen, so a role on that row would
        # be a second, contradictory statement about access.
        _refuse_accessor(user)
        # "" clears, an id sets. role_service writes both columns together.
        role_service.apply_role_to_user(db, user, _resolve_role(db, body.role_id))
    if body.pov_env_ids is not None:
        # An accessor is already bound to exactly one POV by accessor_env_id, and confined
        # by a path allowlist that no permission can widen. A second, contradictory POV
        # list on the same row would be a lie in the database -- refuse rather than store
        # something whose meaning depends on which guard reads it first.
        _refuse_accessor(user)
        user.pov_env_ids_list = body.pov_env_ids

    # Losing administrator -- the flag cleared, or the Administrator role removed -- leaves
    # the user with NOTHING until access is granted by a role or by permissions. Unless
    # this same request sends permissions, whatever map sat under the admin is replaced:
    # a NULL one would read as unrestricted, and an old explicit one is a grant nobody has
    # looked at since the admin flag started bypassing it. A role assigned in the same
    # request still grants, because roles and the per-user map are unioned.
    if was_admin and not _admin_here(user) and body.permissions is None:
        user.permissions_dict = _nothing_granted()

    db.commit()
    db.refresh(user)
    return UserResponse(
        id=user.id,
        username=user.username,
        full_name=user.full_name,
        email=user.email,
        workgroups=user.workgroups_list,
        is_active=user.is_active,
        is_admin=user.is_admin or False,
        auth_provider=user.auth_provider,
        mfa_required=user.mfa_required,
        is_service_account=bool(user.is_service_account),
        permissions=user.permissions_dict or None,
        pov_env_ids=user.pov_env_ids_list,
        role_id=user.role_id or "",
        role_name=(role_service.get(db, user.role_id).name if user.role_id else ""),
    )


# ── Deactivate user ────────────────────────────────────────────────────────────

@router.delete("/{user_id}", status_code=200)
def deactivate_user(
    user_id: str,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if user_id == admin.id:
        raise HTTPException(status_code=400, detail="Cannot deactivate your own account")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    # Refused rather than allowed-because-it-is-safe: deactivating here would stop the
    # login while its PovAccessor row went on saying the POV has a live accessor, so the
    # Access tab would show access that does not work. Revoke on that tab does both, and
    # one writer for a lifecycle is the whole reason this stays refused.
    _refuse_accessor(user)
    user.is_active = False
    db.commit()
    return {"detail": "User deactivated"}


# ── Permanently delete user ─────────────────────────────────────────────────────

@router.delete("/{user_id}/permanent", status_code=200)
def delete_user_permanent(
    user_id: str,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Permanently remove a user and all associated tokens and FIDO2 credentials."""
    if user_id == admin.id:
        raise HTTPException(status_code=400, detail="Cannot delete your own account")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    # Same refusal as the deactivate above, plus a sharper one: deleting the row here
    # leaves the PovAccessor binding pointing at a user that no longer exists, which is
    # exactly the orphan `revoke` is written to avoid.
    _refuse_accessor(user)
    db.delete(user)
    db.commit()
    return {"detail": "User permanently deleted"}


# ── List a user's PATs (admin view) ────────────────────────────────────────────

@router.get("/{user_id}/tokens", response_model=List[UserTokenItem])
def list_user_tokens(
    user_id: str,
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if not db.query(User).filter(User.id == user_id).first():
        raise HTTPException(status_code=404, detail="User not found")
    pats = (
        db.query(PersonalAccessToken)
        .filter(PersonalAccessToken.user_id == user_id)
        .order_by(PersonalAccessToken.created_at.desc())
        .all()
    )
    return [
        UserTokenItem(
            id=p.id,
            name=p.name,
            created_at=p.created_at,
            expires_at=p.expires_at,
            last_used_at=p.last_used_at,
            is_active=p.is_active,
        )
        for p in pats
    ]


# ── Create a PAT for any user (admin) ─────────────────────────────────────────

class AdminCreateTokenRequest(BaseModel):
    name: str
    expires_days: Optional[int] = None


@router.post("/{user_id}/tokens", response_model=TokenCreateResponse, status_code=201)
def create_user_token(
    user_id: str,
    body: AdminCreateTokenRequest,
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Create a PAT on behalf of any user. Raw token shown once — store it immediately."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    # A PAT is a credential that outlives the browser session. An accessor is confined by
    # a path allowlist that a PAT would still be subject to — but minting one for a
    # prospect is a standing credential into this dashboard, which is the thing this whole
    # feature is arranged not to create.
    _refuse_accessor(user)
    from datetime import timedelta
    raw = _generate_raw()
    expires_at = (
        datetime.utcnow() + timedelta(days=body.expires_days)
        if body.expires_days
        else None
    )
    pat = PersonalAccessToken(
        user_id=user_id,
        name=body.name,
        token_hash=hash_pat(raw),
        expires_at=expires_at,
    )
    db.add(pat)
    db.commit()
    db.refresh(pat)
    return TokenCreateResponse(
        id=pat.id,
        name=pat.name,
        token=raw,
        created_at=pat.created_at,
        expires_at=pat.expires_at,
    )


# ── Revoke any user's PAT (admin) ──────────────────────────────────────────────

@router.delete("/{user_id}/tokens/{token_id}", status_code=200)
def revoke_user_token(
    user_id: str,
    token_id: str,
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    pat = (
        db.query(PersonalAccessToken)
        .filter(
            PersonalAccessToken.id == token_id,
            PersonalAccessToken.user_id == user_id,
        )
        .first()
    )
    if not pat:
        raise HTTPException(status_code=404, detail="Token not found")
    pat.is_active = False
    db.commit()
    return {"detail": "Token revoked"}


# ── FIDO2 summary per user (admin view) ────────────────────────────────────────

@router.get("/{user_id}/fido2", response_model=List[dict])
def list_user_fido2(
    user_id: str,
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if not db.query(User).filter(User.id == user_id).first():
        raise HTTPException(status_code=404, detail="User not found")
    creds = (
        db.query(Fido2Credential)
        .filter(Fido2Credential.user_id == user_id)
        .all()
    )
    return [
        {
            "id": c.id,
            "device_name": c.device_name,
            "created_at": c.created_at.isoformat(),
            "last_used_at": c.last_used_at.isoformat() if c.last_used_at else None,
            "is_active": c.is_active,
        }
        for c in creds
    ]


# ── Service accounts and their OAuth clients ──────────────────────────────────
# A workload principal: see services/service_accounts for the design. Every route here is
# require_admin and audit-logged, and a client secret is returned exactly once.

class ServiceAccountCreateRequest(BaseModel):
    username: str
    full_name: Optional[str] = None
    workgroups: List[str] = []
    # None or {} both mean NOTHING GRANTED for a service account -- the inverse of a
    # person, where they mean unrestricted. Grant with a map or a role.
    permissions: Optional[dict] = None
    role_id: Optional[str] = None


class OAuthClientCreateRequest(BaseModel):
    name: str
    secret_days: Optional[int] = None
    token_ttl_seconds: Optional[int] = None
    # Set = the client authenticates with a JWT-SVID for this SPIFFE ID and has no secret.
    spiffe_id: Optional[str] = None


class OAuthClientRotateRequest(BaseModel):
    secret_days: Optional[int] = None
    grace_minutes: Optional[int] = None


class OAuthClientItem(BaseModel):
    id: str
    client_id: str
    name: str
    created_at: datetime
    created_by: Optional[str] = None
    secret_expires_at: Optional[datetime] = None
    previous_expires_at: Optional[datetime] = None
    token_ttl_seconds: int
    last_used_at: Optional[datetime] = None
    is_active: bool
    auth_method: str = "secret"
    spiffe_id: Optional[str] = None


class OAuthClientSecretResponse(OAuthClientItem):
    client_secret: str        # shown ONCE -- store it now
    token_endpoint: str = "/api/oauth/token"


def _client_item(c) -> dict:
    return dict(
        id=c.id, client_id=c.client_id, name=c.name, created_at=c.created_at,
        created_by=c.created_by, secret_expires_at=c.secret_expires_at,
        previous_expires_at=c.previous_expires_at,
        token_ttl_seconds=c.token_ttl_seconds or 0, last_used_at=c.last_used_at,
        is_active=bool(c.is_active),
        auth_method=c.auth_method or "secret", spiffe_id=c.spiffe_id,
    )


def _service_account_or_404(db: Session, user_id: str) -> User:
    from ..services import service_accounts
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    problem = service_accounts.service_account_problem(user)
    if problem:
        raise HTTPException(status_code=400, detail=problem)
    return user


def _client_or_404(db: Session, user_id: str, client_row_id: str):
    from ..database import OAuthClient
    client = (db.query(OAuthClient)
              .filter(OAuthClient.id == client_row_id, OAuthClient.user_id == user_id)
              .first())
    if not client:
        raise HTTPException(status_code=404, detail="OAuth client not found")
    return client


@router.post("/service-accounts", response_model=UserResponse, status_code=201)
def create_service_account(
    body: ServiceAccountCreateRequest,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Create a workload principal. It holds nothing until a map or a role grants it."""
    from ..services import job_service, service_accounts

    if body.permissions:
        validate_permissions_payload(body.permissions)
    role = _resolve_role(db, body.role_id)
    if role is not None and role.permissions_dict.get("is_admin"):
        raise HTTPException(status_code=400,
                            detail="A service account cannot hold an administrator role.")
    try:
        user = service_accounts.create_service_account(
            db, username=body.username, full_name=body.full_name or "",
            workgroups=body.workgroups, permissions=body.permissions or None)
    except service_accounts.ServiceAccountError as exc:
        raise HTTPException(status_code=409 if "taken" in str(exc) else 400, detail=str(exc))
    if role is not None:
        role_service.apply_role_to_user(db, user, role)
    db.commit()
    db.refresh(user)
    job_service.log_audit(db, admin.username, "service_account.create",
                          details={"service_account": user.username,
                                   "role": role.name if role else ""})
    return UserResponse(
        id=user.id, username=user.username, full_name=user.full_name, email=user.email,
        workgroups=user.workgroups_list, is_active=user.is_active, is_admin=False,
        auth_provider=user.auth_provider, mfa_required=False, is_service_account=True,
        permissions=user.permissions_dict or None, pov_env_ids=[],
        role_id=user.role_id or "", role_name=role.name if role else "",
    )


@router.get("/{user_id}/oauth-clients", response_model=List[OAuthClientItem])
def list_oauth_clients(
    user_id: str,
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    from ..database import OAuthClient
    if not db.query(User).filter(User.id == user_id).first():
        raise HTTPException(status_code=404, detail="User not found")
    rows = (db.query(OAuthClient).filter(OAuthClient.user_id == user_id)
            .order_by(OAuthClient.created_at.desc()).all())
    return [OAuthClientItem(**_client_item(c)) for c in rows]


@router.post("/{user_id}/oauth-clients", response_model=OAuthClientSecretResponse,
             status_code=201)
def create_oauth_client(
    user_id: str,
    body: OAuthClientCreateRequest,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Mint a client_credentials client. The secret is returned once and never again."""
    from ..services import job_service, service_accounts
    user = _service_account_or_404(db, user_id)
    try:
        client, raw = service_accounts.create_client(
            db, user, name=body.name, secret_days=body.secret_days,
            token_ttl_seconds=body.token_ttl_seconds, created_by=admin.username,
            spiffe_id=body.spiffe_id or "")
    except service_accounts.ServiceAccountError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    db.commit()
    db.refresh(client)
    job_service.log_audit(db, admin.username, "service_account.client_create",
                          details={"service_account": user.username,
                                   "client_id": client.client_id, "name": client.name,
                                   "spiffe_id": client.spiffe_id or ""})
    return OAuthClientSecretResponse(**_client_item(client), client_secret=raw)


@router.post("/{user_id}/oauth-clients/{client_row_id}/rotate",
             response_model=OAuthClientSecretResponse)
def rotate_oauth_client(
    user_id: str,
    client_row_id: str,
    body: OAuthClientRotateRequest,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Issue a new secret. The previous one keeps working for ``grace_minutes``."""
    from ..services import job_service, service_accounts
    user = _service_account_or_404(db, user_id)
    client = _client_or_404(db, user_id, client_row_id)
    try:
        raw = service_accounts.rotate_client(db, client, secret_days=body.secret_days,
                                             grace_minutes=body.grace_minutes)
    except service_accounts.ServiceAccountError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    db.commit()
    db.refresh(client)
    job_service.log_audit(db, admin.username, "service_account.client_rotate",
                          details={"service_account": user.username,
                                   "client_id": client.client_id})
    return OAuthClientSecretResponse(**_client_item(client), client_secret=raw)


@router.delete("/{user_id}/oauth-clients/{client_row_id}", status_code=200)
def revoke_oauth_client(
    user_id: str,
    client_row_id: str,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Revoke a client. Every access token it issued stops working on its next use."""
    from ..services import job_service
    client = _client_or_404(db, user_id, client_row_id)
    client.is_active = False
    db.commit()
    job_service.log_audit(db, admin.username, "service_account.client_revoke",
                          details={"user_id": user_id, "client_id": client.client_id})
    return {"detail": "OAuth client revoked"}


# ── External IdP identities mapped to a service account ───────────────────────
# services/external_workload verifies the IdP's token; these rows say which service
# account a verified (issuer, sub) acts as. Keyed on `sub`, never `azp` -- see the model.

class ExternalIdentityCreateRequest(BaseModel):
    subject: str
    name: str
    issuer: Optional[str] = None           # blank = the configured workload issuer
    expected_client: Optional[str] = None  # optional azp/appid/client_id/cid check


class ExternalIdentityItem(BaseModel):
    id: str
    issuer: str
    subject: str
    expected_client: Optional[str] = None
    name: str
    created_at: datetime
    created_by: Optional[str] = None
    last_used_at: Optional[datetime] = None
    is_active: bool


def _external_item(r) -> ExternalIdentityItem:
    return ExternalIdentityItem(
        id=r.id, issuer=r.issuer, subject=r.subject, expected_client=r.expected_client,
        name=r.name, created_at=r.created_at, created_by=r.created_by,
        last_used_at=r.last_used_at, is_active=bool(r.is_active))


@router.get("/{user_id}/external-identities", response_model=List[ExternalIdentityItem])
def list_external_identities(
    user_id: str,
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    from ..database import ExternalWorkloadIdentity
    if not db.query(User).filter(User.id == user_id).first():
        raise HTTPException(status_code=404, detail="User not found")
    rows = (db.query(ExternalWorkloadIdentity)
            .filter(ExternalWorkloadIdentity.user_id == user_id)
            .order_by(ExternalWorkloadIdentity.created_at.desc()).all())
    return [_external_item(r) for r in rows]


@router.post("/{user_id}/external-identities", response_model=ExternalIdentityItem,
             status_code=201)
def create_external_identity(
    user_id: str,
    body: ExternalIdentityCreateRequest,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Let an IdP identity act as this service account."""
    from ..database import ExternalWorkloadIdentity
    from ..services import external_workload, job_service
    user = _service_account_or_404(db, user_id)
    subject = (body.subject or "").strip()
    name = (body.name or "").strip()
    issuer = (body.issuer or "").strip() or external_workload.issuer()
    if not subject or not name:
        raise HTTPException(status_code=400, detail="A subject and a name are required.")
    if not issuer:
        raise HTTPException(
            status_code=400,
            detail="No issuer given and none configured. Set the workload issuer (or the "
                   "SSO issuer) under Settings → Single sign-on first.")
    if issuer not in external_workload.accepted_issuers():
        raise HTTPException(
            status_code=400,
            detail=f"{issuer!r} is not an issuer this dashboard accepts workload tokens "
                   "from. Add it under Settings → Single sign-on → Workload tokens.")
    if (db.query(ExternalWorkloadIdentity)
            .filter(ExternalWorkloadIdentity.issuer == issuer,
                    ExternalWorkloadIdentity.subject == subject).first()):
        raise HTTPException(status_code=409,
                            detail="That issuer and subject are already mapped.")
    row = ExternalWorkloadIdentity(
        user_id=user.id, issuer=issuer, subject=subject[:255], name=name[:100],
        expected_client=(body.expected_client or "").strip()[:255] or None,
        created_by=admin.username, is_active=True)
    db.add(row)
    db.commit()
    db.refresh(row)
    job_service.log_audit(db, admin.username, "service_account.external_identity_create",
                          details={"service_account": user.username, "issuer": issuer,
                                   "subject": subject})
    return _external_item(row)


@router.delete("/{user_id}/external-identities/{identity_id}", status_code=200)
def revoke_external_identity(
    user_id: str,
    identity_id: str,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Stop accepting this IdP identity. Its next request is refused.

    Deleted rather than flagged: the unique (issuer, subject) key would otherwise leave a
    dead row that blocks mapping the same identity again. The audit log keeps the record.
    """
    from ..database import ExternalWorkloadIdentity
    from ..services import job_service
    row = (db.query(ExternalWorkloadIdentity)
           .filter(ExternalWorkloadIdentity.id == identity_id,
                   ExternalWorkloadIdentity.user_id == user_id).first())
    if not row:
        raise HTTPException(status_code=404, detail="External identity not found")
    details = {"user_id": user_id, "issuer": row.issuer, "subject": row.subject}
    db.delete(row)
    db.commit()
    job_service.log_audit(db, admin.username, "service_account.external_identity_revoke",
                          details=details)
    return {"detail": "External identity revoked"}
