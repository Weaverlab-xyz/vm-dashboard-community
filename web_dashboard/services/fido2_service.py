"""
FIDO2 / WebAuthn service — server initialization, in-memory challenge storage,
and options-to-dict serialization for the browser.
"""
import base64
import json
import threading
import time
import uuid
from typing import Optional

from fido2.server import Fido2Server
from fido2.utils import websafe_encode
from fido2.webauthn import PublicKeyCredentialRpEntity

from ..config import settings

# ---------------------------------------------------------------------------
# FIDO2 Server singleton
# ---------------------------------------------------------------------------

_rp = PublicKeyCredentialRpEntity(id=settings.webauthn_rp_id, name=settings.webauthn_rp_name)
fido2_server = Fido2Server(_rp)


# ---------------------------------------------------------------------------
# In-memory challenge storage
# ---------------------------------------------------------------------------

_CHALLENGE_TTL = 120          # seconds — time for user to complete the ceremony
_OAUTH_STATE_TTL = 300        # seconds — time for OAuth redirect round-trip
_FIDO2_KEY_PREFIX = "vmcli:fido2:challenge:"
_OAUTH_STATE_PREFIX = "vmcli:oauth:state:"

# In-memory store: key → (value, expires_at)
_mem_store: dict = {}
_mem_lock = threading.Lock()


def _mem_set(key: str, ttl: int, value: str) -> None:
    with _mem_lock:
        _mem_store[key] = (value, time.monotonic() + ttl)


def _mem_getdel(key: str) -> Optional[str]:
    with _mem_lock:
        entry = _mem_store.pop(key, None)
        if entry is None:
            return None
        value, expires_at = entry
        if time.monotonic() > expires_at:
            return None
        return value


# ── FIDO2 challenge storage ─────────────────────────────────────────────────

def store_fido2_challenge(state: dict) -> str:
    """Persist a FIDO2 state dict and return the token (UUID) that identifies it."""
    token = str(uuid.uuid4())
    key = f"{_FIDO2_KEY_PREFIX}{token}"
    _mem_set(key, _CHALLENGE_TTL, json.dumps(state))
    return token


def fetch_fido2_challenge(token: str) -> Optional[dict]:
    """
    Retrieve and atomically delete the challenge state identified by *token*.
    Returns None if the token has expired or never existed.
    """
    raw = _mem_getdel(f"{_FIDO2_KEY_PREFIX}{token}")
    if raw is None:
        return None
    return json.loads(raw)


# ── OAuth state storage ─────────────────────────────────────────────────────

def store_oauth_state(state: str, redirect_uri: str = "") -> None:
    """Store an OAuth CSRF state value alongside its redirect_uri."""
    _mem_set(f"{_OAUTH_STATE_PREFIX}{state}", _OAUTH_STATE_TTL, redirect_uri or "1")


def verify_and_consume_oauth_state(state: str) -> Optional[str]:
    """
    Verify the OAuth state exists, atomically delete it, and return the stored
    redirect_uri.  Returns None if the state is invalid or expired.
    """
    raw = _mem_getdel(f"{_OAUTH_STATE_PREFIX}{state}")
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
