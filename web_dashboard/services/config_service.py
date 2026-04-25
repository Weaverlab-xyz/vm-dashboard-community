"""
Encrypted key-value configuration store backed by the `app_config` DB table.

Values are Fernet-encrypted with a key derived from JWT_SECRET_KEY so secrets
at rest are protected even if someone gets direct DB access.

An in-memory cache (populated on first access, updated on write) keeps
service calls fast after the initial DB round-trip.
"""
import base64
import hashlib
import logging
import threading
from datetime import datetime
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

_cache: dict = {}
_cache_lock = threading.Lock()
_cache_loaded: bool = False
_setup_complete: Optional[bool] = None


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
    global _cache, _cache_loaded
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
    finally:
        db.close()


def _ensure_loaded() -> None:
    if not _cache_loaded:
        _load_cache()


def invalidate() -> None:
    """Force full reload from DB on next access."""
    global _cache_loaded
    with _cache_lock:
        _cache_loaded = False


# ── Public API ────────────────────────────────────────────────────────────────

def get(key: str, default: str = "") -> str:
    """Return the stored plaintext value for key, or default if not set."""
    _ensure_loaded()
    with _cache_lock:
        return _cache.get(key, default)


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
