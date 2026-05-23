"""
Encrypted key-value configuration store backed by the `app_config` DB table.

Values are Fernet-encrypted with a key derived from JWT_SECRET_KEY so secrets
at rest are protected even if someone gets direct DB access.

An in-memory cache (populated on first access, updated on write) keeps
service calls fast after the initial DB round-trip.

External backend references: after a secrets migration the DB value for a
secret key may be a reference string with a backend prefix. Two shapes are
recognised:

Legacy (single-vault, the historical default):
  aws_sm://dashboard/epml_pat
  azure_kv://epml-pat
  gcp_sm://dashboard-epml-pat
  bt_safe://Dashboard/epml_pat

Multi-vault (used when an operator has registered named vaults in the
`secret_vaults` table):
  azure_kv://primary/epml-pat
  azure_kv://tenant-alpha-eu/epml-pat
  aws_sm://dev-account/dashboard/epml_pat

The multi-vault parser only treats the first slash-segment as a vault id
if it matches a registered ``SecretVault.id`` for that backend. Otherwise
the whole reference is passed through to the read function as-is — so
legacy refs like ``aws_sm://dashboard/epml_pat`` keep working even when
the secret_vaults table is non-empty.

get() detects these prefixes and resolves them transparently. Resolved
values are cached in _ext_cache for EXT_CACHE_TTL seconds so every service
call doesn't hit the external API.

Workgroup scoping: get() accepts an optional ``workgroup`` arg. When given,
lookup prefers a (key, workgroup) row, then falls back to (key, NULL).
Community installs leave the column NULL on every row and pass workgroup=None;
the column exists for schema parity with the prod multi-tenant deployment.
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
    """Populate the in-memory cache keyed on (key, workgroup_or_None).

    Workgroup is stored as None in the dict when the DB row's workgroup
    column is NULL. Lookups in get() check (key, workgroup) first, fall
    back to (key, None).
    """
    global _cache, _cache_loaded, _cache_loaded_at
    from ..database import SessionLocal, AppConfig
    db = SessionLocal()
    try:
        rows = db.query(AppConfig).all()
        loaded: dict = {}
        for row in rows:
            wg = getattr(row, "workgroup", None)
            loaded[(row.key, wg)] = _decrypt(row.value) if row.value else ""
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

def _is_registered_vault(vault_id: str, backend: str) -> bool:
    """True if a SecretVault row exists for (id=vault_id, backend=backend).

    Used by the multi-vault parser to decide whether the first path segment
    is a vault id or part of the secret name. Cheap: indexed lookup, no
    network. Returns False on any error so a missing/empty table makes the
    parser fall through to legacy single-vault behaviour.
    """
    try:
        from ..database import SessionLocal, SecretVault
        db = SessionLocal()
        try:
            return db.query(SecretVault.id).filter(
                SecretVault.id == vault_id,
                SecretVault.backend == backend,
            ).first() is not None
        finally:
            db.close()
    except Exception:
        return False


def _parse_ref(raw: str, backend: str) -> tuple[str | None, str]:
    """Split a reference into (vault_id, secret_ref).

    Returns (None, secret_ref) when the reference uses the legacy
    single-vault shape (no slash, or first segment isn't a registered
    vault). Returns (vault_id, secret_ref) when the first slash-segment
    matches a registered vault for the given backend.
    """
    for prefix, b in _EXT_PREFIXES.items():
        if backend == b:
            stripped = raw[len(prefix):]
            break
    else:
        return None, raw

    if "/" not in stripped:
        return None, stripped

    first, rest = stripped.split("/", 1)
    if _is_registered_vault(first, backend):
        return first, rest
    return None, stripped


def _resolve_external(raw: str, workgroup: str | None = None) -> str:
    """If raw is an external backend reference, fetch and cache the real value.

    The workgroup arg is reserved for future per-workgroup default-vault
    resolution; today it only flows through for telemetry / cache-key
    separation."""
    for prefix, backend in _EXT_PREFIXES.items():
        if raw.startswith(prefix):
            vault_id, ref = _parse_ref(raw, backend)
            now = time.monotonic()
            cache_key = (raw, vault_id, workgroup)
            with _ext_cache_lock:
                cached = _ext_cache.get(cache_key)
                if cached and cached[1] > now:
                    return cached[0]
            try:
                from .secrets_backend_service import read_sync
                value = read_sync(backend, ref, vault_id=vault_id)
                with _ext_cache_lock:
                    _ext_cache[cache_key] = (value, now + EXT_CACHE_TTL)
                return value
            except Exception as exc:
                logger.error("Failed to resolve external secret %s: %s", raw[:60], exc)
                return ""
    return raw


# ── Public API ────────────────────────────────────────────────────────────────

def get(key: str, default: str = "", workgroup: str | None = None) -> str:
    """Return the stored plaintext value for key, or default if not set.

    Lookup priority:
      1. (key, workgroup) row when workgroup is given
      2. (key, None) — the global row
      3. default

    Transparently resolves external backend references (aws_sm://, azure_kv://,
    gcp_sm://, bt_safe://) — callers see only the actual secret value.

    Community callers should pass workgroup=None (the default); the multi-
    workgroup priority is a prod-multi-tenant feature.
    """
    _ensure_loaded()
    with _cache_lock:
        raw = None
        if workgroup is not None:
            raw = _cache.get((key, workgroup))
        if raw is None:
            raw = _cache.get((key, None), default)
    return _resolve_external(raw, workgroup=workgroup)


def get_bool(key: str, default: bool = False) -> bool:
    """Return a config flag as bool. Stored as '1'/'0'; env-var fallback via settings."""
    val = get(key)
    if val:
        return val == "1"
    # Fall back to settings (env var)
    from ..config import settings
    return bool(getattr(settings, key, default))


def set(key: str, value: str, workgroup: str | None = None) -> None:
    """Encrypt and persist a single value; update in-memory cache.

    When workgroup is None, writes/updates the global row. When given,
    writes/updates the workgroup-scoped row, leaving the global one alone.
    """
    from ..database import SessionLocal, AppConfig
    db = SessionLocal()
    try:
        encrypted = _encrypt(value) if value else ""
        q = db.query(AppConfig).filter(AppConfig.key == key)
        if workgroup is None:
            existing = q.filter(AppConfig.workgroup.is_(None)).first()
        else:
            existing = q.filter(AppConfig.workgroup == workgroup).first()
        if existing:
            existing.value = encrypted
            existing.updated_at = datetime.utcnow()
        else:
            db.add(AppConfig(key=key, value=encrypted, workgroup=workgroup, updated_at=datetime.utcnow()))
        db.commit()
        with _cache_lock:
            _cache[(key, workgroup)] = value
    finally:
        db.close()


def set_many(pairs: dict, workgroup: str | None = None) -> None:
    """Encrypt and persist multiple values in one transaction; update cache.

    All pairs land in the same workgroup row-set; call repeatedly with
    different workgroup args if you need to write across scopes.
    """
    from ..database import SessionLocal, AppConfig
    db = SessionLocal()
    try:
        for key, value in pairs.items():
            encrypted = _encrypt(value) if value else ""
            q = db.query(AppConfig).filter(AppConfig.key == key)
            if workgroup is None:
                existing = q.filter(AppConfig.workgroup.is_(None)).first()
            else:
                existing = q.filter(AppConfig.workgroup == workgroup).first()
            if existing:
                existing.value = encrypted
                existing.updated_at = datetime.utcnow()
            else:
                db.add(AppConfig(key=key, value=encrypted, workgroup=workgroup, updated_at=datetime.utcnow()))
        db.commit()
        with _cache_lock:
            for k, v in pairs.items():
                _cache[(k, workgroup)] = v
    finally:
        db.close()


def delete(key: str, workgroup: str | None = None) -> None:
    """Remove a key from app_config (and the in-memory cache). No-op if the
    row isn't present. Used by the Secrets page CRUD when the active backend
    is `database`."""
    from ..database import SessionLocal, AppConfig
    db = SessionLocal()
    try:
        q = db.query(AppConfig).filter(AppConfig.key == key)
        if workgroup is None:
            existing = q.filter(AppConfig.workgroup.is_(None)).first()
        else:
            existing = q.filter(AppConfig.workgroup == workgroup).first()
        if existing:
            db.delete(existing)
            db.commit()
        with _cache_lock:
            _cache.pop((key, workgroup), None)
    finally:
        db.close()


def list_all() -> list[dict]:
    """Return every app_config row as {key, workgroup, updated_at}. Values
    are NOT returned — call get(key, workgroup=...) for individual decrypted
    reads. Used by the Secrets page to enumerate database-backed secrets."""
    from ..database import SessionLocal, AppConfig
    db = SessionLocal()
    try:
        rows = db.query(AppConfig).order_by(AppConfig.key, AppConfig.workgroup).all()
        return [
            {
                "key":        row.key,
                "workgroup":  getattr(row, "workgroup", None),
                "updated_at": row.updated_at.isoformat() if row.updated_at else "",
            }
            for row in rows
        ]
    finally:
        db.close()


# ── Setup state ───────────────────────────────────────────────────────────────

def is_setup_complete() -> bool:
    """True once the setup wizard has been completed. Setup is global —
    workgroup-scoped rows are never consulted here."""
    global _setup_complete
    if _setup_complete is True:
        return True
    _ensure_loaded()
    with _cache_lock:
        result = _cache.get(("setup_complete", None)) == "1"
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
    """Return all GLOBAL config key-value pairs with secrets replaced by
    bullets. Workgroup-scoped rows are intentionally excluded — this helper
    serves the legacy settings UI which is global-only.
    """
    _ensure_loaded()
    with _cache_lock:
        return {
            key: ("••••••••" if key in _SECRET_KEYS and v else v)
            for (key, wg), v in _cache.items()
            if wg is None and key != "setup_complete"
        }
