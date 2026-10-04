"""Service accounts: workload principals that authenticate with OAuth 2.0 client credentials.

A PAT is a person's credential lent to a machine. It is a static bearer secret, it lives
as long as somebody remembers to set, and the principal behind it is a `users` row that
looks exactly like a human -- password, sign-in page, and an empty permission map that
means UNRESTRICTED. This module is the workload-shaped alternative:

  * **The principal** is a `users` row with ``is_service_account`` set. It has no
    password, cannot sign in interactively (``api/auth.login`` and the OAuth callbacks
    refuse it), is never an administrator (``User.is_effective_admin`` answers False
    whatever the columns say), and an empty permission map reads as NOTHING rather than
    everything (``User.effective_permissions_dict``). It is still a `users` row so every
    RBAC check, workgroup scope and access role in the dashboard applies to it unchanged.

  * **The credential** is an :class:`~web_dashboard.database.OAuthClient`: a public
    ``client_id`` and a secret that is shown once and stored as a hash. The secret is
    never presented to the API. It is exchanged at ``POST /api/oauth/token``
    (RFC 6749 section 4.4) for an access token that lives minutes.

  * **The access token** is the dashboard's own JWT with ``type: "workload"``, the
    ``client_id`` it was issued to, and an optional ``scope`` that can only narrow what
    the account holds (``database.narrow_to_token_scope``). ``resolve_workload_token``
    re-reads the client on every request, so deactivating it ends every outstanding
    token immediately rather than at expiry.

What this deliberately does NOT do yet: accept access tokens from an external IdP, or a
SPIFFE JWT-SVID as the client assertion in place of a secret. Both slot in at
``api/auth.resolve_bearer`` and the token endpoint without changing the principal, and
docs/access/service-accounts.md records them as the next phases.
"""
import hashlib
import hmac
import logging
import secrets
import uuid
from datetime import datetime, timedelta
from typing import Optional, Tuple

from jose import jwt
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

CLIENT_ID_PREFIX = "vmsa_"
SECRET_PREFIX = "vmss_"
TOKEN_TYPE = "workload"
AUTH_PROVIDER = "service"

# Access-token lifetime. Short because nothing revokes a JWT except the client check in
# `resolve_workload_token` -- and the shorter it is, the less that check is carrying.
DEFAULT_TOKEN_TTL_SECONDS = 900
MIN_TOKEN_TTL_SECONDS = 60
MAX_TOKEN_TTL_SECONDS = 3600

# Client-secret lifetime. Required, like the agent cell's PAT expiry and for the same
# reason: a workload secret with no end date is the thing this module replaces.
DEFAULT_SECRET_DAYS = 90
MAX_SECRET_DAYS = 365

# How long the previous secret keeps working after a rotation, so a fleet can pick up
# the new one without an outage.
DEFAULT_ROTATION_GRACE_MINUTES = 60
MAX_ROTATION_GRACE_MINUTES = 7 * 24 * 60


class ServiceAccountError(Exception):
    """An invalid service-account request. The message is rendered into a 400 and names
    the remedy, the contract ``agentcell_service.AgentCellError`` states."""


# ── Hashing and minting ──────────────────────────────────────────────────────

def hash_secret(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _new_client_id() -> str:
    return CLIENT_ID_PREFIX + secrets.token_hex(12)


def _new_secret() -> str:
    return SECRET_PREFIX + secrets.token_hex(32)


def _bounded(value: Optional[int], default: int, lo: int, hi: int, what: str) -> int:
    if value is None:
        return default
    if value < lo or value > hi:
        raise ServiceAccountError(f"{what} must be between {lo} and {hi}.")
    return int(value)


# ── The principal ────────────────────────────────────────────────────────────

def create_service_account(db: Session, *, username: str, full_name: str = "",
                           workgroups=None, permissions: Optional[dict] = None):
    """Create the workload principal. Never an admin, never a password.

    ``permissions`` is stored as given -- the caller validates it -- and None or ``{}``
    leaves the column NULL, which for a service account is DENY-ALL rather than the
    unrestricted it means on a person (see ``User.effective_permissions_dict``).
    """
    from ..database import User

    username = (username or "").strip()
    if not username:
        raise ServiceAccountError("A service account needs a username.")
    if db.query(User).filter(User.username == username).first():
        raise ServiceAccountError(f"The username {username!r} is already taken.")
    user = User(
        id=str(uuid.uuid4()),
        username=username,
        full_name=full_name or username,
        hashed_password=None,
        auth_provider=AUTH_PROVIDER,
        is_active=True,
        is_admin=False,
        is_service_account=True,
    )
    user.workgroups_list = list(workgroups or [])
    if permissions:
        user.permissions_dict = permissions
    db.add(user)
    db.flush()
    return user


def service_account_problem(user) -> Optional[str]:
    """Why this row cannot hold an OAuth client, or None."""
    if user is None:
        return "No such service account."
    if not bool(getattr(user, "is_service_account", False)):
        return (f"{user.username!r} is a person's account. OAuth clients belong to "
                "service accounts only -- create one under Users → Service accounts.")
    if not bool(getattr(user, "is_active", True)):
        return f"{user.username!r} is disabled."
    return None


# ── The credential ───────────────────────────────────────────────────────────

def create_client(db: Session, user, *, name: str, secret_days: Optional[int] = None,
                  token_ttl_seconds: Optional[int] = None,
                  created_by: str = "", spiffe_id: str = "") -> Tuple[object, str]:
    """Mint an OAuth client for a service account. Returns ``(row, raw_secret)``; the raw
    secret is not stored and cannot be retrieved again.

    With ``spiffe_id`` the client authenticates with a JWT-SVID for that ID instead
    (``services/spiffe_assertion``) and the returned secret is "": one is still generated
    so the column has a value, but it is never shown and ``authenticate_client`` refuses
    secret auth for such a client anyway.
    """
    from ..database import OAuthClient
    from . import spiffe_assertion

    spiffe_id = (spiffe_id or "").strip()
    if spiffe_id:
        if not spiffe_assertion.valid_spiffe_id(spiffe_id):
            raise ServiceAccountError(f"{spiffe_id!r} is not a SPIFFE ID (spiffe://<trust-domain>/<path>).")
        # In the dashboard's OWN trust domain, remote agents' IDs and the dashboard's own are
        # reserved: the dashboard mints those, and a client bound to one would let it sign
        # in as a service account. A lab's trust domain has no such rule.
        from . import dashboard_spire
        own = dashboard_spire.registered(db)
        if own and dashboard_spire.reserved_path(spiffe_id, own.trust_domain):
            raise ServiceAccountError(
                f"{spiffe_id} is reserved in the dashboard's own trust domain (remote agents "
                f"and the dashboard itself). Use a path under "
                f"spiffe://{own.trust_domain}{dashboard_spire.WORKLOAD_PREFIX}.")
        if (db.query(OAuthClient)
                .filter(OAuthClient.spiffe_id == spiffe_id, OAuthClient.is_active == True)  # noqa: E712
                .first()):
            raise ServiceAccountError(
                f"{spiffe_id} already authenticates an active OAuth client. One identity, "
                "one client -- revoke that one first.")

    problem = service_account_problem(user)
    if problem:
        raise ServiceAccountError(problem)
    name = (name or "").strip()
    if not name:
        raise ServiceAccountError("An OAuth client needs a name.")
    days = _bounded(secret_days, DEFAULT_SECRET_DAYS, 1, MAX_SECRET_DAYS,
                    "Secret lifetime (days)")
    ttl = _bounded(token_ttl_seconds, DEFAULT_TOKEN_TTL_SECONDS, MIN_TOKEN_TTL_SECONDS,
                   MAX_TOKEN_TTL_SECONDS, "Access-token lifetime (seconds)")
    raw = _new_secret()
    row = OAuthClient(
        client_id=_new_client_id(),
        user_id=user.id,
        name=name[:100],
        secret_hash=hash_secret(raw),
        secret_expires_at=datetime.utcnow() + timedelta(days=days),
        token_ttl_seconds=ttl,
        created_by=created_by or None,
        is_active=True,
        auth_method=spiffe_assertion.AUTH_METHOD if spiffe_id else None,
        spiffe_id=spiffe_id or None,
    )
    db.add(row)
    db.flush()
    return row, ("" if spiffe_id else raw)


def rotate_client(db: Session, client, *, secret_days: Optional[int] = None,
                  grace_minutes: Optional[int] = None) -> str:
    """Issue a new secret; the old one keeps working for ``grace_minutes``."""
    if not client.is_active:
        raise ServiceAccountError("That client is revoked. Create a new one instead.")
    if client.auth_method == "spiffe_jwt":
        raise ServiceAccountError(
            "That client authenticates with a SPIFFE JWT-SVID and has no secret to rotate.")
    days = _bounded(secret_days, DEFAULT_SECRET_DAYS, 1, MAX_SECRET_DAYS,
                    "Secret lifetime (days)")
    grace = _bounded(grace_minutes, DEFAULT_ROTATION_GRACE_MINUTES, 0,
                     MAX_ROTATION_GRACE_MINUTES, "Rotation grace (minutes)")
    now = datetime.utcnow()
    raw = _new_secret()
    if grace:
        client.previous_secret_hash = client.secret_hash
        # Never past the old secret's own expiry: a rotation must not extend it.
        old_end = client.secret_expires_at
        grace_end = now + timedelta(minutes=grace)
        client.previous_expires_at = min(grace_end, old_end) if old_end else grace_end
    else:
        client.previous_secret_hash = None
        client.previous_expires_at = None
    client.secret_hash = hash_secret(raw)
    client.secret_expires_at = now + timedelta(days=days)
    db.flush()
    return raw


def authenticate_client(db: Session, client_id: str, secret: str,
                        now: Optional[datetime] = None):
    """The client row for a valid ``client_id``/secret pair, else None.

    Every failure answers the same None -- unknown client, wrong secret, expired secret,
    revoked client, disabled or non-service account -- so the token endpoint cannot be
    used to learn which client ids exist.
    """
    from ..database import OAuthClient, User

    if not client_id or not secret:
        return None
    now = now or datetime.utcnow()
    client = db.query(OAuthClient).filter(OAuthClient.client_id == client_id).first()
    if not client or not client.is_active:
        return None
    if client.auth_method == "spiffe_jwt":
        # Its secret was generated and discarded; refusing here as well means that stays
        # true even if the column were ever filled in by hand.
        return None
    presented = hash_secret(secret)
    ok = False
    if client.secret_hash and hmac.compare_digest(presented, client.secret_hash):
        ok = client.secret_expires_at is None or client.secret_expires_at > now
    elif client.previous_secret_hash and hmac.compare_digest(presented, client.previous_secret_hash):
        ok = bool(client.previous_expires_at and client.previous_expires_at > now)
    if not ok:
        return None
    user = db.query(User).filter(User.id == client.user_id).first()
    if service_account_problem(user):
        return None
    return client


# ── Scope ────────────────────────────────────────────────────────────────────

def parse_scope(raw: Optional[str]) -> Optional[dict]:
    """``"vms:read aws:write"`` → ``{"vms": ["read"], "aws": ["write"]}``; blank → None.

    Syntax only. Whether a scope or level exists is the caller's question
    (``api/oauth`` checks it against the permission catalogue), because this module must
    not import ``api``.
    """
    parts = (raw or "").split()
    if not parts:
        return None
    out: dict = {}
    for part in parts:
        scope, sep, level = part.partition(":")
        if not sep or not scope or not level:
            raise ServiceAccountError(
                f"Scope {part!r} is not of the form <scope>:<level>, e.g. vms:read.")
        levels = out.setdefault(scope, [])
        if level not in levels:
            levels.append(level)
    return {k: sorted(v) for k, v in out.items()}


def format_scope(scope: Optional[dict]) -> str:
    if not scope:
        return ""
    return " ".join(f"{s}:{lvl}" for s in sorted(scope) for lvl in sorted(scope[s]))


def granted_scope(user, requested: Optional[dict]) -> Optional[dict]:
    """The scope actually issued: ``requested`` intersected with what the account holds.

    None when nothing was requested -- the token then carries the account's full
    permissions, which for a service account is never more than an admin set explicitly.
    ``{}`` when something was requested and none of it is held; the caller refuses that
    with ``invalid_scope`` rather than issue a token that can do nothing.
    """
    if requested is None:
        return None
    held = user.effective_permissions_dict or {}
    out = {}
    for scope, levels in requested.items():
        have = held.get(scope, [])
        if not isinstance(have, list):
            continue
        keep = sorted(set(levels) & set(have))
        if keep:
            out[scope] = keep
    return out


# ── Access tokens ────────────────────────────────────────────────────────────

def issue_access_token(user, client, scope: Optional[dict]) -> Tuple[str, int]:
    """Sign a workload access token. Returns ``(jwt, expires_in_seconds)``."""
    from ..config import settings

    ttl = int(client.token_ttl_seconds or DEFAULT_TOKEN_TTL_SECONDS)
    now = datetime.utcnow()
    payload = {
        "sub": user.username,
        "type": TOKEN_TYPE,
        "client_id": client.client_id,
        "jti": uuid.uuid4().hex,
        "iat": now,
        "exp": now + timedelta(seconds=ttl),
    }
    if scope is not None:
        payload["scope"] = format_scope(scope)
    token = jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)
    return token, ttl


def resolve_workload_token(db: Session, payload: dict):
    """The service account a decoded workload token names, or None.

    Re-reads the client row, so a revoked client ends its tokens on the next request.
    The scope, if any, is attached to the instance as ``_token_scope``, which is what
    ``User.effective_permissions_dict`` narrows by -- one place, so REST, WebSocket and
    MCP all see the same answer.
    """
    from ..database import OAuthClient, User

    if payload.get("type") != TOKEN_TYPE:
        return None
    client = (db.query(OAuthClient)
              .filter(OAuthClient.client_id == payload.get("client_id", ""),
                      OAuthClient.is_active == True)  # noqa: E712
              .first())
    if not client:
        return None
    user = db.query(User).filter(User.id == client.user_id).first()
    if service_account_problem(user) or user.username != payload.get("sub"):
        return None
    scope_claim = payload.get("scope")
    try:
        user._token_scope = parse_scope(scope_claim) if scope_claim is not None else None
    except ServiceAccountError:
        return None
    if scope_claim is not None and user._token_scope is None:
        # An empty scope claim was issued as "nothing"; never let it read as "unscoped".
        user._token_scope = {}
    return user


def touch_client(db: Session, client) -> None:
    client.last_used_at = datetime.utcnow()
