"""
Encrypted key-value configuration store backed by the `app_config` DB table.

Values are Fernet-encrypted with a key derived from JWT_SECRET_KEY so secrets
at rest are protected even if someone gets direct DB access.

An in-memory cache (populated on first access, updated on write) keeps
service calls fast after the initial DB round-trip.

External backend references: after a secrets migration the DB value for a
secret key may be a reference string with a backend prefix, e.g.
  aws_sm://dashboard/epml_pat
  azure_kv://epml-pat
  gcp_sm://dashboard-epml-pat
  bt_safe://Dashboard/epml_pat

get() detects these prefixes and resolves them transparently.  Resolved values
are cached in _ext_cache for EXT_CACHE_TTL seconds so that every service call
doesn't hit the external API.
"""
import base64
import hashlib
import logging
import threading
import time
from datetime import datetime
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

_cache: dict = {}
_cache_lock = threading.Lock()
_cache_loaded: bool = False
_cache_loaded_at: float = 0.0
_setup_complete: Optional[bool] = None

# Time-to-live for the in-memory config cache. Bounds how long another gunicorn
# worker can keep serving stale values after this process writes new ones
# (config_service.set_many only invalidates the local worker's cache).
_CACHE_TTL_SECONDS = 5.0

# External-backend value cache: key → (resolved_value, expiry_ts)
_ext_cache: dict[str, tuple[str, float]] = {}
_ext_cache_lock = threading.Lock()
EXT_CACHE_TTL = 300  # seconds

# Prefix → backend identifier (must match secrets_backend_service dispatch keys)
_EXT_PREFIXES: dict[str, str] = {
    "aws_sm://":   "aws_sm",
    "azure_kv://": "azure_kv",
    "gcp_sm://":   "gcp_sm",
    "bt_safe://":  "bt_secrets_safe",
}


# ── Encryption ────────────────────────────────────────────────────────────────

def _fernet() -> Fernet:
    from ..config import settings
    raw = settings.jwt_secret_key.encode()
    derived = base64.urlsafe_b64encode(hashlib.sha256(raw).digest())
    return Fernet(derived)


def _encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def _decrypt(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except (InvalidToken, Exception):
        return token  # fallback: return raw (handles plain-text legacy rows)


# ── Cache management ──────────────────────────────────────────────────────────

def _load_cache() -> None:
    global _cache, _cache_loaded, _cache_loaded_at
    from ..database import SessionLocal, AppConfig
    db = SessionLocal()
    try:
        rows = db.query(AppConfig).all()
        loaded: dict = {}
        for row in rows:
            loaded[row.key] = _decrypt(row.value) if row.value else ""
        with _cache_lock:
            _cache = loaded
            _cache_loaded = True
            _cache_loaded_at = time.monotonic()
    finally:
        db.close()


def _ensure_loaded() -> None:
    # Cross-worker freshness: even if this worker's cache is "loaded", drop it
    # after _CACHE_TTL_SECONDS so writes from a sibling gunicorn worker (whose
    # invalidate() call only affected its own memory) become visible here.
    if _cache_loaded and (time.monotonic() - _cache_loaded_at) < _CACHE_TTL_SECONDS:
        return
    _load_cache()


def invalidate() -> None:
    """Force full reload from DB on next access."""
    global _cache_loaded
    with _cache_lock:
        _cache_loaded = False
    with _ext_cache_lock:
        _ext_cache.clear()


# ── External reference resolution ─────────────────────────────────────────────

def _resolve_external(raw: str) -> str:
    """If raw is an external backend reference, fetch and cache the real value."""
    for prefix, backend in _EXT_PREFIXES.items():
        if raw.startswith(prefix):
            ref = raw[len(prefix):]
            now = time.monotonic()
            with _ext_cache_lock:
                cached = _ext_cache.get(raw)
                if cached and cached[1] > now:
                    return cached[0]
            try:
                from .secrets_backend_service import read_sync
                value = read_sync(backend, ref)
                with _ext_cache_lock:
                    _ext_cache[raw] = (value, now + EXT_CACHE_TTL)
                return value
            except Exception as exc:
                logger.error("Failed to resolve external secret %s: %s", raw[:60], exc)
                return ""
    return raw


# ── Public API ────────────────────────────────────────────────────────────────

def get(key: str, default: str = "") -> str:
    """Return the stored plaintext value for key, or default if not set.

    Transparently resolves external backend references (aws_sm://, azure_kv://,
    gcp_sm://, bt_safe://) — callers see only the actual secret value.
    """
    _ensure_loaded()
    with _cache_lock:
        raw = _cache.get(key, default)
    return _resolve_external(raw)


def get_bool(key: str, default: bool = False) -> bool:
    """Return a config flag as bool. Stored as '1'/'0'; env-var fallback via settings."""
    val = get(key)
    if val:
        return val == "1"
    # Fall back to settings (env var)
    from ..config import settings
    return bool(getattr(settings, key, default))


def set(key: str, value: str) -> None:
    """Encrypt and persist a single value; update in-memory cache."""
    from ..database import SessionLocal, AppConfig
    db = SessionLocal()
    try:
        encrypted = _encrypt(value) if value else ""
        existing = db.query(AppConfig).filter(AppConfig.key == key).first()
        if existing:
            existing.value = encrypted
            existing.updated_at = datetime.utcnow()
        else:
            db.add(AppConfig(key=key, value=encrypted, updated_at=datetime.utcnow()))
        db.commit()
        with _cache_lock:
            _cache[key] = value
    finally:
        db.close()


def set_many(pairs: dict) -> None:
    """Encrypt and persist multiple values in one transaction; update cache."""
    from ..database import SessionLocal, AppConfig
    db = SessionLocal()
    try:
        for key, value in pairs.items():
            encrypted = _encrypt(value) if value else ""
            existing = db.query(AppConfig).filter(AppConfig.key == key).first()
            if existing:
                existing.value = encrypted
                existing.updated_at = datetime.utcnow()
            else:
                db.add(AppConfig(key=key, value=encrypted, updated_at=datetime.utcnow()))
        db.commit()
        with _cache_lock:
            _cache.update(pairs)
    finally:
        db.close()


def delete(key: str) -> None:
    """Remove a key from app_config (and the in-memory cache). No-op if the
    key isn't present. Used by the Secrets page CRUD when the active backend
    is `database`."""
    from ..database import SessionLocal, AppConfig
    db = SessionLocal()
    try:
        existing = db.query(AppConfig).filter(AppConfig.key == key).first()
        if existing:
            db.delete(existing)
            db.commit()
        with _cache_lock:
            _cache.pop(key, None)
    finally:
        db.close()


def list_all() -> list[dict]:
    """Return every app_config row as {key, updated_at}. Values are NOT
    returned — call get(key) for individual decrypted reads. Used by the
    Secrets page to enumerate database-backed secrets."""
    from ..database import SessionLocal, AppConfig
    db = SessionLocal()
    try:
        rows = db.query(AppConfig).order_by(AppConfig.key).all()
        return [
            {
                "key":        row.key,
                "updated_at": row.updated_at.isoformat() if row.updated_at else "",
            }
            for row in rows
        ]
    finally:
        db.close()


# ── Setup state ───────────────────────────────────────────────────────────────

def is_setup_complete() -> bool:
    """True once the setup wizard has been completed."""
    global _setup_complete
    if _setup_complete is True:
        return True
    _ensure_loaded()
    with _cache_lock:
        result = _cache.get("setup_complete") == "1"
    if result:
        _setup_complete = True
    return result


def mark_setup_complete() -> None:
    global _setup_complete
    set("setup_complete", "1")
    _setup_complete = True


# ── UI helpers ────────────────────────────────────────────────────────────────

_SECRET_KEYS = frozenset({
    "aws_secret_access_key",
    "azure_client_secret",
    "azure_oauth_client_secret",
    "gcp_service_account_json",
})


def get_all_public() -> dict:
    """Return all config key-value pairs with secrets replaced by bullets."""
    _ensure_loaded()
    with _cache_lock:
        return {
            k: ("••••••••" if k in _SECRET_KEYS and v else v)
            for k, v in _cache.items()
            if k != "setup_complete"
        }
