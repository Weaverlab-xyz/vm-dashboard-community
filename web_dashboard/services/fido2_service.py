"""
FIDO2 / WebAuthn service — server initialization, ceremony-state storage, and
options-to-dict serialization for the browser.
"""
import base64
import json
import logging
import uuid
from datetime import datetime, timedelta
from typing import Optional

from fido2.server import Fido2Server
from fido2.utils import websafe_encode
from fido2.webauthn import PublicKeyCredentialRpEntity
from sqlalchemy.orm import Session

from ..config import settings
from ..database import EphemeralState

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# FIDO2 Server singleton
# ---------------------------------------------------------------------------

_rp = PublicKeyCredentialRpEntity(id=settings.webauthn_rp_id, name=settings.webauthn_rp_name)
fido2_server = Fido2Server(_rp)


# ---------------------------------------------------------------------------
# Ceremony-state storage
#
# In the database, not in this process. Every value here is written by one request
# and read by a different one — FIDO2 begin/complete, OAuth login/callback — and the
# app runs `gunicorn -w 2` with no session affinity in front of it, so the two legs
# land on the same worker only by luck. This was a module-level dict under a
# `threading.Lock` until 2026-08: thread-safe, and process-local, which meant the
# callback leg hit a worker with no record of the state about half the time and the
# user got `/login?error=invalid_state` for no reason they could act on.
#
# See the EphemeralState docstring in database.py for the rest of the reasoning,
# including why the DELETE is the lock.
# ---------------------------------------------------------------------------

_CHALLENGE_TTL = 120          # seconds — time for user to complete the ceremony
_OAUTH_STATE_TTL = 300        # seconds — time for OAuth redirect round-trip
_FIDO2_KEY_PREFIX = "vmcli:fido2:challenge:"
_OAUTH_STATE_PREFIX = "vmcli:oauth:state:"


def _store_set(db: Session, key: str, ttl: int, value: str, label: str) -> None:
    """Write one state row, replacing any existing row with the same key.

    ``label`` is a fixed description of which ceremony this is, for the log line.
    It is a caller-supplied constant rather than anything derived from ``key`` or
    ``value``, so no part of a live challenge, CSRF state or PKCE verifier can reach
    the log even by accident — see the note on _store_getdel.

    Never raises: the caller is mid-ceremony and a 500 here would be indistinguishable
    from the provider being down. A failed write surfaces as an invalid state on the
    second leg, which is the same outcome as before and at least gets logged.
    """
    try:
        db.query(EphemeralState).filter(EphemeralState.key == key).delete(
            synchronize_session=False)
        db.add(EphemeralState(
            key=key,
            value=value,
            expires_at=datetime.utcnow() + timedelta(seconds=ttl),
        ))
        db.commit()
        _sweep(db)
    except Exception:  # noqa: BLE001
        logger.warning("ephemeral state: could not store %s", label, exc_info=True)
        try:
            db.rollback()
        except Exception:  # pragma: no cover — defensive
            pass


def _store_getdel(db: Session, key: str, label: str) -> Optional[str]:
    """Read a state value and delete it in one shot. Returns None if it never
    existed, has expired, or has already been consumed.

    The DELETE's rowcount is the lock. Reading first and deleting second is safe
    because rows here are write-once — the value cannot change between the two
    statements — and only the caller whose DELETE matched a row is allowed to act on
    what it read. That is what keeps a replayed OAuth `state` from being accepted
    twice by two workers at once.

    Nothing here logs ``key`` or ``value``, not even a truncated form. The key embeds
    the live CSRF state and the value the PKCE verifier, and a log line is exactly the
    wrong place for either — a sanitiser that happens to strip the secret today is one
    refactor away from not doing so, and is not worth defending in review.
    """
    try:
        row = (
            db.query(EphemeralState.value, EphemeralState.expires_at)
            .filter(EphemeralState.key == key)
            .first()
        )
        if row is None:
            return None
        claimed = db.query(EphemeralState).filter(
            EphemeralState.key == key).delete(synchronize_session=False)
        db.commit()
        if not claimed:
            return None          # a concurrent consumer won the race
        if row.expires_at is None or datetime.utcnow() > row.expires_at:
            return None          # consumed either way — expiry is not a retry
        return row.value
    except Exception:  # noqa: BLE001
        logger.warning("ephemeral state: could not consume %s", label, exc_info=True)
        try:
            db.rollback()
        except Exception:  # pragma: no cover — defensive
            pass
        return None


def _sweep(db: Session, now: Optional[datetime] = None) -> int:
    """Delete expired rows. Opportunistic, on the write path — the table only grows
    when ceremonies start, so that is exactly when it needs tidying, and it needs no
    timer process (same reasoning as login_guard._sweep).

    This is also a real leak fix, not just tidiness: the dict this replaced evicted
    only on read, so every abandoned ceremony — a closed SSO tab, a cancelled
    touch prompt — stayed resident for the life of the worker.
    """
    try:
        deleted = db.query(EphemeralState).filter(
            EphemeralState.expires_at < (now or datetime.utcnow())
        ).delete(synchronize_session=False)
        db.commit()
        return deleted
    except Exception:  # noqa: BLE001
        try:
            db.rollback()
        except Exception:  # pragma: no cover — defensive
            pass
        return 0


# ── FIDO2 challenge storage ─────────────────────────────────────────────────

def store_fido2_challenge(db: Session, state: dict) -> str:
    """Persist a FIDO2 state dict and return the token (UUID) that identifies it."""
    token = str(uuid.uuid4())
    _store_set(db, f"{_FIDO2_KEY_PREFIX}{token}", _CHALLENGE_TTL, json.dumps(state),
               "a FIDO2 challenge")
    return token


def fetch_fido2_challenge(db: Session, token: str) -> Optional[dict]:
    """
    Retrieve and atomically delete the challenge state identified by *token*.
    Returns None if the token has expired or never existed.
    """
    raw = _store_getdel(db, f"{_FIDO2_KEY_PREFIX}{token}", "a FIDO2 challenge")
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except ValueError:  # pragma: no cover — defensive; nothing else writes these rows
        return None


# ── OAuth state storage ─────────────────────────────────────────────────────

def store_oauth_state(db: Session, state: str, redirect_uri: str = "") -> None:
    """Store an OAuth CSRF state value alongside its redirect_uri."""
    _store_set(db, f"{_OAUTH_STATE_PREFIX}{state}", _OAUTH_STATE_TTL, redirect_uri or "1",
               "an OAuth/OIDC state")


def verify_and_consume_oauth_state(db: Session, state: str) -> Optional[str]:
    """
    Verify the OAuth state exists, atomically delete it, and return the stored
    redirect_uri.  Returns None if the state is invalid or expired.
    """
    raw = _store_getdel(db, f"{_OAUTH_STATE_PREFIX}{state}", "an OAuth/OIDC state")
    if not raw or raw == "1":
        # "1" is the legacy sentinel — state valid but no URI stored
        return "" if raw == "1" else None
    return raw


# ---------------------------------------------------------------------------
# Options serialization — convert fido2 library objects to browser-ready JSON
# ---------------------------------------------------------------------------

def creation_options_to_dict(options) -> dict:
    """
    Convert a PublicKeyCredentialCreationOptions object (from register_begin) to a
    plain JSON-serialisable dict in the format the browser's
    navigator.credentials.create() expects (the publicKey sub-object).

    fido2 >= 1.0 returns the PublicKeyCredentialCreationOptions directly
    (no longer wrapped in a CredentialCreationOptions with .public_key).
    """
    # fido2 >= 1.0: options IS the PublicKeyCredentialCreationOptions
    o = options.public_key if hasattr(options, "public_key") else options
    result = {
        "challenge": websafe_encode(o.challenge),
        "rp": {"id": o.rp.id, "name": o.rp.name},
        "user": {
            "id": websafe_encode(o.user.id),
            "name": o.user.name,
            "displayName": o.user.display_name,
        },
        "pubKeyCredParams": [
            {"type": p.type.value, "alg": int(p.alg)}
            for p in o.pub_key_cred_params
        ],
        "excludeCredentials": [
            {"type": c.type.value, "id": websafe_encode(c.id)}
            for c in (o.exclude_credentials or [])
        ],
    }
    if o.timeout is not None:
        result["timeout"] = o.timeout
    if o.authenticator_selection:
        sel = o.authenticator_selection
        auth_sel = {}
        if sel.user_verification:
            auth_sel["userVerification"] = str(sel.user_verification)
        if sel.resident_key:
            auth_sel["residentKey"] = str(sel.resident_key)
        if sel.authenticator_attachment:
            auth_sel["authenticatorAttachment"] = str(sel.authenticator_attachment)
        result["authenticatorSelection"] = auth_sel
    return result


def request_options_to_dict(options) -> dict:
    """
    Convert a PublicKeyCredentialRequestOptions object (from authenticate_begin) to a
    plain JSON-serialisable dict in the format the browser's
    navigator.credentials.get() expects (the publicKey sub-object).

    fido2 >= 1.0 returns the PublicKeyCredentialRequestOptions directly.
    """
    # fido2 >= 1.0: options IS the PublicKeyCredentialRequestOptions
    o = options.public_key if hasattr(options, "public_key") else options
    result = {
        "challenge": websafe_encode(o.challenge),
        "rpId": o.rp_id,
        "allowCredentials": [
            {"type": c.type.value, "id": websafe_encode(c.id)}
            for c in (o.allow_credentials or [])
        ],
    }
    if o.timeout is not None:
        result["timeout"] = o.timeout
    if o.user_verification:
        result["userVerification"] = str(o.user_verification)
    return result


# ---------------------------------------------------------------------------
# Binary ↔ base64url helpers
# ---------------------------------------------------------------------------

def b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64url_decode(s: str) -> bytes:
    padding = 4 - len(s) % 4
    if padding != 4:
        s += "=" * padding
    return base64.urlsafe_b64decode(s)
