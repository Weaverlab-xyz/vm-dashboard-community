"""
Authentication API endpoints.
JWT-based login/logout with bcrypt password hashing.
Supports FIDO2/WebAuthn MFA (second factor) and Azure AD OAuth login.
"""
import hashlib
import uuid
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from jose import JWTError, jwt
from sqlalchemy.orm import Session

from ..config import settings
from ..database import User, Fido2Credential, PersonalAccessToken, OAuthGroupMapping, Approval, get_db, verify_password
from ..models.user import (
    TokenResponse,
    TokenData,
    UserResponse,
    PreAuthResponse,
    MfaLoginRequest,
    Fido2AuthBeginResponse,
)
from ..services.fido2_service import (
    fido2_server,
    store_fido2_challenge,
    fetch_fido2_challenge,
    store_oauth_state,
    verify_and_consume_oauth_state,
    b64url_encode,
    b64url_decode,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")

_PRE_AUTH_EXPIRE_MINUTES = 2  # short-lived, password-verified-only token
_PAT_PREFIX = "vmcli_"


def _get_user_from_pat(raw_token: str, db: Session) -> User:
    """Resolve a PAT string to its owner, updating last_used_at."""
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    pat = (
        db.query(PersonalAccessToken)
        .filter(
            PersonalAccessToken.token_hash == token_hash,
            PersonalAccessToken.is_active == True,
        )
        .first()
    )
    if not pat:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or revoked token")
    if pat.expires_at and pat.expires_at < datetime.utcnow():
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expired")
    pat.last_used_at = datetime.utcnow()
    db.commit()
    user = db.query(User).filter(User.id == pat.user_id, User.is_active == True).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found or inactive")
    return user


# ── Token helpers ─────────────────────────────────────────────────────────────

def create_access_token(data: dict) -> str:
    payload = data.copy()
    expire = datetime.utcnow() + timedelta(minutes=settings.access_token_expire_minutes)
    payload.update({"exp": expire, "type": "access"})
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def _create_pre_auth_token(user_id: str) -> str:
    """Short-lived token issued after password check; authorises the FIDO2 step only."""
    expire = datetime.utcnow() + timedelta(minutes=_PRE_AUTH_EXPIRE_MINUTES)
    payload = {"sub": user_id, "type": "pre_auth", "exp": expire}
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def _decode_pre_auth_token(token: str) -> str:
    """Return the user_id from a pre_auth token or raise 401."""
    try:
        payload = jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
        if payload.get("type") != "pre_auth":
            raise ValueError("wrong token type")
        return payload["sub"]
    except Exception:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired MFA token")


def decode_token(token: str) -> TokenData:
    try:
        payload = jwt.decode(
            token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm]
        )
        # Reject pre_auth tokens from being used as full access tokens
        if payload.get("type") == "pre_auth":
            raise JWTError("pre_auth token cannot be used as access token")
        username: Optional[str] = payload.get("sub")
        if username is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
        return TokenData(username=username)
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )


# ── Dependencies ──────────────────────────────────────────────────────────────

async def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> User:
    """FastAPI dependency: accept either a JWT or a vmcli_ PAT."""
    if token.startswith(_PAT_PREFIX):
        return _get_user_from_pat(token, db)
    token_data = decode_token(token)
    user = db.query(User).filter(User.username == token_data.username).first()
    if not user or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or inactive",
        )
    return user


def require_admin(current_user: User = Depends(get_current_user)) -> User:
    """FastAPI dependency: raises 403 if the current user is not an admin."""
    if not current_user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    return current_user


# ── Permission constants ───────────────────────────────────────────────────────

PERMISSION_SCOPES = ["vms", "aws", "azure", "gcp", "images", "containers", "config_mgmt", "jobs"]
PERMISSION_LEVELS = ["read", "write", "delete"]


def can_audit_jobs(user: User) -> bool:
    """True if the user may view all jobs (admin or has jobs:read permission)."""
    if user.is_admin:
        return True
    perms = user.permissions_dict
    return "read" in perms.get("jobs", [])


def require_permission(scope: str, level: str):
    """
    Returns a FastAPI dependency that checks the user has the specified
    permission (scope:level).  Admins always pass.  Users whose permissions
    column is NULL are treated as having full access (backward compatible).
    """
    async def _check(current_user: User = Depends(get_current_user)) -> User:
        if current_user.is_admin:
            return current_user
        perms = current_user.permissions_dict  # {} if NULL → full access
        if not perms:
            return current_user  # NULL = unrestricted (existing users unaffected)
        if level not in perms.get(scope, []):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Requires '{scope}:{level}' permission.",
            )
        return current_user
    return _check


def require_workgroup_access(workgroup: str):
    """
    Returns a FastAPI dependency that verifies the current user
    has access to the specified workgroup.
    """
    async def _check(current_user: User = Depends(get_current_user)):
        if workgroup not in current_user.workgroups_list:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied to workgroup: {workgroup}",
            )
        return current_user
    return _check


def require_approval(action: str):
    """
    Returns a FastAPI dependency that gates the endpoint behind an Entitle
    approval workflow.

    First call (no ``X-Entitle-Approval-Id`` header) opens an Entitle
    request, persists an :class:`~web_dashboard.database.Approval` row, and
    raises ``202 Accepted`` with ``{approval_id, poll_url, expires_at}``.
    The frontend polls ``GET /api/approvals/{id}`` and, once status is
    ``approved``, retries the original call with the header.

    Retry call: verifies ``(user_id, action, payload_hash, status==approved,
    not expired, not consumed)`` then marks the row ``consumed`` so the
    approval can't be replayed.

    The master kill-switch ``settings.approval_gate_enabled`` makes this
    dependency a no-op — used for emergency bypass during incidents.
    """
    from ..services import entitle_service  # local import: avoid load-time cycle

    async def _check(
        request: Request,
        x_entitle_approval_id: Optional[str] = Header(default=None),
        current_user: User = Depends(get_current_user),
        db: Session = Depends(get_db),
    ) -> User:
        if not settings.approval_gate_enabled:
            return current_user

        body = await request.body()
        payload_hash = entitle_service.canonical_payload_hash(body)

        if not x_entitle_approval_id:
            try:
                ticket = await entitle_service.create_request(
                    action, current_user.username, payload_hash
                )
            except entitle_service.EntitleError as exc:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=f"Approval service unavailable: {exc}",
                )
            row = Approval(
                entitle_request_id=ticket.request_id,
                action=action,
                user_id=current_user.id,
                payload_hash=payload_hash,
                status="pending",
                expires_at=ticket.expires_at,
            )
            db.add(row)
            db.commit()
            db.refresh(row)
            raise HTTPException(
                status_code=status.HTTP_202_ACCEPTED,
                detail={
                    "approval_id": row.id,
                    "poll_url": f"/api/approvals/{row.id}",
                    "expires_at": row.expires_at.isoformat(),
                    "action": action,
                },
            )

        approval = (
            db.query(Approval).filter(Approval.id == x_entitle_approval_id).first()
        )
        if not approval:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"error_code": "unknown", "message": "Approval not found."},
            )
        if approval.user_id != current_user.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"error_code": "wrong_user", "message": "Approval belongs to another user."},
            )
        if approval.action != action:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"error_code": "wrong_action", "message": "Approval is for a different action."},
            )
        if approval.payload_hash != payload_hash:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"error_code": "payload_mismatch", "message": "Request body changed since approval was granted."},
            )
        if approval.status == "consumed":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"error_code": "consumed", "message": "Approval has already been used."},
            )
        if approval.status == "denied":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"error_code": "denied", "message": approval.denial_reason or "Approval was denied."},
            )
        # Auto-expire if past TTL but the webhook hasn't fired yet.
        if approval.status == "expired" or approval.expires_at < datetime.utcnow():
            if approval.status != "expired":
                approval.status = "expired"
                db.commit()
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"error_code": "expired", "message": "Approval has expired."},
            )
        if approval.status == "pending":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"error_code": "pending", "message": "Approval is still pending."},
            )
        if approval.status != "approved":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"error_code": "unknown", "message": f"Unexpected approval status: {approval.status}"},
            )

        approval.status = "consumed"
        approval.consumed_at = datetime.utcnow()
        db.commit()
        return current_user

    return _check


# ── Login (step 1: password) ──────────────────────────────────────────────────

@router.post("/login")
async def login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db),
):
    """
    Authenticate with username/password.

    - If the user has no FIDO2 keys: returns a full JWT (HTTP 200).
    - If the user has FIDO2 keys registered: returns a pre_auth_token (HTTP 202)
      and the client must complete the FIDO2 step at POST /api/auth/login/mfa.
    - OAuth-only users (auth_provider != 'local') cannot use this endpoint.
    """
    user = db.query(User).filter(User.username == form_data.username).first()

    # Block OAuth-only users from password login
    if user and user.auth_provider != "local":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="This account uses Microsoft login. Use 'Sign in with Microsoft' instead.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not user or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account disabled")

    # If MFA is enforced → return pre-auth token and signal the FIDO2 step
    if user.mfa_required:
        pre_auth = _create_pre_auth_token(user.id)
        return Response(
            content=PreAuthResponse(pre_auth_token=pre_auth).model_dump_json(),
            status_code=202,
            media_type="application/json",
        )

    token = create_access_token({"sub": user.username})
    return TokenResponse(
        access_token=token,
        username=user.username,
        workgroups=user.workgroups_list,
    )


# ── Login (step 2: FIDO2 assertion) ──────────────────────────────────────────

@router.post("/login/mfa", response_model=TokenResponse)
async def login_mfa(
    body: MfaLoginRequest,
    db: Session = Depends(get_db),
):
    """
    Complete login by verifying a FIDO2 assertion.
    Requires the pre_auth_token issued in step 1.
    """
    user_id = _decode_pre_auth_token(body.pre_auth_token)
    user = db.query(User).filter(User.id == user_id, User.is_active == True).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")

    # Fetch the challenge stored during webauthn/login/begin
    challenge_token = body.assertion_response.get("challenge_token")
    if not challenge_token:
        raise HTTPException(status_code=400, detail="Missing challenge_token in assertion_response")

    stored = fetch_fido2_challenge(challenge_token)
    if not stored or stored.get("user_id") != user_id:
        raise HTTPException(status_code=400, detail="Invalid or expired FIDO2 challenge")

    # Load all active credentials for this user
    db_creds = (
        db.query(Fido2Credential)
        .filter(Fido2Credential.user_id == user_id, Fido2Credential.is_active == True)
        .all()
    )
    if not db_creds:
        raise HTTPException(status_code=400, detail="No FIDO2 keys registered for this account")

    # Reconstruct AttestedCredentialData from stored bytes
    from fido2.webauthn import AttestedCredentialData, AuthenticationResponse

    credentials = [AttestedCredentialData(bytes(c.public_key)) for c in db_creds]

    try:
        raw_assertion = {k: v for k, v in body.assertion_response.items() if k != "challenge_token"}
        # Parse response first to extract sign count before verification
        authentication = AuthenticationResponse.from_dict(raw_assertion)
        new_sign_count = authentication.response.authenticator_data.counter

        matched_cred = fido2_server.authenticate_complete(
            state=stored["state"],
            credentials=credentials,
            response=authentication,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"FIDO2 authentication failed: {exc}")

    # Update sign_count (anti-cloning measure) and last_used_at
    matched_id = bytes(matched_cred.credential_id)
    for c in db_creds:
        if bytes(c.credential_id) == matched_id:
            c.sign_count = new_sign_count
            c.last_used_at = datetime.utcnow()
            db.commit()
            break

    token = create_access_token({"sub": user.username})
    return TokenResponse(
        access_token=token,
        username=user.username,
        workgroups=user.workgroups_list,
    )


# ── FIDO2 authentication begin (challenge generation for login) ───────────────

@router.get("/webauthn/login/begin", response_model=Fido2AuthBeginResponse)
async def webauthn_login_begin(
    username: str,
    db: Session = Depends(get_db),
):
    """
    Generate a WebAuthn authentication challenge for the given username.
    Called by the frontend before invoking navigator.credentials.get().
    """
    user = db.query(User).filter(User.username == username, User.is_active == True).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    db_creds = (
        db.query(Fido2Credential)
        .filter(Fido2Credential.user_id == user.id, Fido2Credential.is_active == True)
        .all()
    )
    if not db_creds:
        raise HTTPException(status_code=400, detail="No FIDO2 keys registered for this account")

    from fido2.webauthn import AttestedCredentialData
    from ..services.fido2_service import request_options_to_dict

    credentials = [AttestedCredentialData(bytes(c.public_key)) for c in db_creds]

    options, state = fido2_server.authenticate_begin(
        credentials=credentials,
        user_verification="preferred",
    )

    challenge_token = store_fido2_challenge({"state": state, "user_id": user.id})

    return Fido2AuthBeginResponse(
        challenge_token=challenge_token,
        options=request_options_to_dict(options),
    )


# ── Azure AD OAuth ────────────────────────────────────────────────────────────

def _oauth_cfg() -> tuple:
    """Return (client_id, client_secret, tenant_id) from DB config, falling back to env."""
    from ..services import config_service
    return (
        config_service.get("azure_oauth_client_id") or settings.azure_oauth_client_id,
        config_service.get("azure_oauth_client_secret") or settings.azure_oauth_client_secret,
        config_service.get("azure_oauth_tenant_id") or settings.azure_oauth_tenant_id,
    )


def _build_redirect_uri(request: Request) -> str:
    """
    Derive the OAuth callback URI from the incoming request so that the flow
    works regardless of whether the user accessed via localhost, an IP, or a
    hostname.  Falls back to the configured static value if unavailable.
    """
    try:
        base = f"{request.url.scheme}://{request.url.netloc}"
        return f"{base}/api/auth/oauth/azure/callback"
    except Exception:
        return settings.azure_oauth_redirect_uri


@router.get("/oauth/azure/login")
async def oauth_azure_login(request: Request):
    """Redirect the browser to Azure AD for OAuth login."""
    client_id, _, tenant_id = _oauth_cfg()
    if not client_id or not tenant_id:
        raise HTTPException(
            status_code=501,
            detail="Azure AD OAuth is not configured on this server.",
        )

    redirect_uri = _build_redirect_uri(request)
    state = str(uuid.uuid4())
    store_oauth_state(state, redirect_uri)

    import msal  # noqa: F401 (unused var `app` removed)
    # Build the authorization URL manually so we can include the state parameter
    auth_url = (
        f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/authorize?"
        + urlencode({
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": "openid profile email User.Read",
            "state": state,
            "response_mode": "query",
        })
    )
    return RedirectResponse(url=auth_url, status_code=302)


@router.get("/oauth/azure/callback")
async def oauth_azure_callback(
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    error_description: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Handle the OAuth callback from Azure AD."""
    if error:
        return RedirectResponse(
            url=f"/login?error=oauth_error&detail={error_description or error}",
            status_code=302,
        )

    stored_redirect_uri = verify_and_consume_oauth_state(state) if state else None
    if stored_redirect_uri is None:
        return RedirectResponse(url="/login?error=invalid_state", status_code=302)

    if not code:
        return RedirectResponse(url="/login?error=no_code", status_code=302)

    # Use the redirect URI that was sent in the authorization request (stored in state).
    # Falls back to the configured static value for legacy state entries.
    redirect_uri = stored_redirect_uri or settings.azure_oauth_redirect_uri

    import msal

    oauth_client_id, oauth_client_secret, oauth_tenant_id = _oauth_cfg()
    msal_app = msal.ConfidentialClientApplication(
        client_id=oauth_client_id,
        client_credential=oauth_client_secret,
        authority=f"https://login.microsoftonline.com/{oauth_tenant_id}",
    )
    result = msal_app.acquire_token_by_authorization_code(
        code=code,
        scopes=["User.Read"],
        redirect_uri=redirect_uri,
    )

    if "error" in result:
        return RedirectResponse(
            url=f"/login?error=token_error&detail={result.get('error_description', '')}",
            status_code=302,
        )

    id_token_claims = result.get("id_token_claims", {})
    email = (
        id_token_claims.get("email")
        or id_token_claims.get("preferred_username")
        or id_token_claims.get("upn")
    )
    oid = id_token_claims.get("oid")
    display_name = id_token_claims.get("name", "")

    if not email:
        return RedirectResponse(url="/login?error=no_email", status_code=302)

    # ── Group-to-workgroup mapping check ─────────────────────────────────────
    # Read mappings from DB (managed via /groups admin page).
    # Falls back to the .env azure_oauth_group_map if DB has no entries.
    db_mappings = db.query(OAuthGroupMapping).all()
    import json as _json
    if db_mappings:
        group_map = {m.entra_group_id: (m.workgroup, m.default_permissions) for m in db_mappings}
    else:
        # .env fallback: no default_permissions support in this path
        group_map = {gid: (wg, None) for gid, wg in settings.azure_oauth_group_map.items()}

    if group_map:
        user_group_ids = set(id_token_claims.get("groups", []))
        matched: list[tuple] = [(wg, dp) for gid, (wg, dp) in group_map.items() if gid in user_group_ids]
        if not matched:
            return RedirectResponse(url="/login?error=not_authorized", status_code=302)
        matched_workgroups = [wg for wg, _ in matched]
        # Use default_permissions from the first matched group that has them set
        matched_default_permissions = next((dp for _, dp in matched if dp is not None), None)
    else:
        matched_workgroups = None  # no mappings configured — legacy path
        matched_default_permissions = None

    # ── Find or auto-create user ──────────────────────────────────────────────
    # Look up by stable oid first, then fall back to email
    user = None
    if oid:
        user = db.query(User).filter(User.oauth_subject == oid).first()
    if not user:
        user = db.query(User).filter(User.email == email).first()

    if user:
        if not user.is_active:
            return RedirectResponse(url="/login?error=account_disabled", status_code=302)
        # Sync fields from Entra ID
        if oid and not user.oauth_subject:
            user.oauth_subject = oid
        user.auth_provider = "azure_ad"
        if display_name and not user.full_name:
            user.full_name = display_name
        # Keep workgroups in sync with current Entra group membership
        if matched_workgroups is not None:
            user.workgroups_list = matched_workgroups
        db.commit()
    elif matched_workgroups is not None:
        # Auto-create: derive a unique username from the email local-part
        base = email.split("@")[0].lower().replace(".", "_")
        username = base
        suffix = 1
        while db.query(User).filter(User.username == username).first():
            username = f"{base}_{suffix}"
            suffix += 1

        user = User(
            id=str(uuid.uuid4()),
            username=username,
            full_name=display_name,
            email=email,
            auth_provider="azure_ad",
            oauth_subject=oid,
            is_active=True,
            is_admin=False,
        )
        user.workgroups_list = matched_workgroups
        if matched_default_permissions is not None:
            user.permissions = matched_default_permissions  # already a JSON string from DB
        db.add(user)
        db.commit()
        db.refresh(user)
    else:
        # Group map not configured — require pre-existing account (legacy behaviour)
        return RedirectResponse(url="/login?error=not_registered", status_code=302)

    token = create_access_token({"sub": user.username})

    # Return JWT via URL fragment (not sent to server on redirect, safe for internal use)
    return RedirectResponse(
        url=f"/login#token={token}",
        status_code=302,
    )


# ── Profile ───────────────────────────────────────────────────────────────────

@router.get("/me", response_model=UserResponse)
async def me(current_user: User = Depends(get_current_user)):
    """Return the currently authenticated user's profile."""
    return UserResponse(
        id=current_user.id,
        username=current_user.username,
        full_name=current_user.full_name,
        email=current_user.email,
        workgroups=current_user.workgroups_list,
        is_active=current_user.is_active,
        is_admin=current_user.is_admin or False,
        auth_provider=current_user.auth_provider,
        mfa_required=current_user.mfa_required,
        permissions=current_user.permissions_dict or None,
    )
