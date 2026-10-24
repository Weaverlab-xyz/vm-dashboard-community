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

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

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
    "wlc://":      "wlc",
}


# ── Encryption ────────────────────────────────────────────────────────────────

def _derive(secret: str) -> Fernet:
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest()))


def _fernet() -> MultiFernet:
    """Encrypt with the primary key; decrypt with any key still in service.

    Primary is ``config_encryption_key`` when set, else the key derived from
    ``jwt_secret_key`` -- the only key there was before, which is why it stays in the list:
    rows written under it read until ``reencrypt_stored_secrets`` has moved them.
    """
    from ..config import settings
    legacy = _derive(settings.jwt_secret_key)
    if settings.config_encryption_key:
        return MultiFernet([_derive(settings.config_encryption_key), legacy])
    return MultiFernet([legacy])


def _encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def _looks_encrypted(token: str) -> bool:
    """True if ``token`` is shaped like a Fernet token: version byte 0x80, then a timestamp,
    IV, ciphertext and HMAC (57 bytes at least), urlsafe-base64 encoded."""
    if not token.startswith("gAAAAA") or len(token) < 76:
        return False
    try:
        raw = base64.urlsafe_b64decode(token.encode())
    except Exception:
        return False
    return len(raw) >= 57 and raw[0] == 0x80


_undecryptable_logged = False


def _decrypt(token: str) -> str:
    """The plaintext of ``token``, or ``token`` itself when it was never encrypted.

    A plain-text legacy row (from before encryption) reads as itself. A value that IS a
    Fernet token and will not decrypt -- the key changed -- reads as "" and is logged:
    returning the ciphertext, as this once did, handed it to callers as if it were the
    password, and they used it, displayed it, or wrote it back encrypted a second time.
    """
    global _undecryptable_logged
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        if not _looks_encrypted(token):
            return token
        if not _undecryptable_logged:
            _undecryptable_logged = True
            logger.error(
                "A stored secret is encrypted with a key this process does not have; it "
                "reads as empty. JWT_SECRET_KEY (or CONFIG_ENCRYPTION_KEY) differs from the "
                "one that wrote it. Restore the original key, or re-enter the secret.")
        return ""
    except Exception:
        return token


# Tables whose secrets live as long as the thing they configure, and so are moved to the
# primary key at boot. The short-lived ones (upload-session handles, credential leases,
# relay output) are left to expire under the legacy key.
_REENCRYPT_COLUMNS = (
    ("AppConfig", "value"),
    ("HypervisorConnection", "secret_enc"),
    ("BeyondTrustTenant", "secret_enc"),
    ("NotificationEndpoint", "url"),
    ("NotificationEndpoint", "secret"),
    ("RunnerCredentialGrant", "values_enc"),
)


def reencrypt_stored_secrets(db) -> int:
    """Re-encrypt every long-lived stored secret that only the legacy key can read.

    A no-op until ``config_encryption_key`` is set. Then each value the new key cannot
    read, but the JWT-derived one can, is rewritten under the new key; a value neither can
    read is left as it is (``_decrypt`` reports it). Returns the number of values moved.
    """
    from ..config import settings
    if not settings.config_encryption_key:
        return 0
    from .. import database
    primary = _derive(settings.config_encryption_key)
    both = _fernet()
    moved = 0
    for model_name, column in _REENCRYPT_COLUMNS:
        model = getattr(database, model_name)
        for row in db.query(model).all():
            value = getattr(row, column)
            if not value or not _looks_encrypted(value):
                continue
            try:
                primary.decrypt(value.encode())
                continue  # already under the new key
            except InvalidToken:
                pass
            try:
                setattr(row, column, both.rotate(value.encode()).decode())
                moved += 1
            except InvalidToken:
                continue
    if moved:
        db.commit()
        global _cache_loaded
        _cache_loaded = False
        logger.info("Re-encrypted %d stored secret(s) under CONFIG_ENCRYPTION_KEY.", moved)
    return moved


def encrypt_value(value: str) -> str:
    """Encrypt a secret held in a table other than ``app_config``.

    Same Fernet key as every stored config value, so there is one secret-at-rest
    story and one rotation hazard rather than two. Used by the notification
    endpoints table, whose webhook URLs are themselves bearer credentials.
    """
    return _encrypt(value or "")


def decrypt_value(token: str) -> str:
    """Inverse of :func:`encrypt_value`. Returns the input unchanged when it isn't
    decryptable, matching ``_decrypt`` — a plain-text legacy value still reads."""
    return _decrypt(token or "")


def decrypt_value_strict(token: str, ttl: Optional[int] = None) -> str:
    """Like :func:`decrypt_value`, but RAISES rather than falling back to the input.

    The fallback in ``_decrypt`` is right for reading a config row that predates
    encryption, and wrong wherever the ciphertext is what makes the value trustworthy:
    handing that reader a hand-written plain-text string gets the string back, so
    "it decrypted" stops meaning "we minted it". Callers who need Fernet's
    authentication — ``storage_chunked``'s upload-session handle is the first — use this.

    ``ttl`` is Fernet's own token age check, in seconds. Fernet stamps every token with
    its mint time, so an expiry needs no field of ours and no clock the caller has to
    trust.
    """
    f = _fernet()
    raw = (token or "").encode()
    return (f.decrypt(raw, ttl=ttl) if ttl else f.decrypt(raw)).decode()


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


def _vault_workgroup(vault_id: str, backend: str) -> str | None:
    """The workstream a registered vault is scoped to, or None (any caller may use it).
    An unreadable registry reads as unscoped, like ``_is_registered_vault``."""
    try:
        from ..database import SessionLocal, SecretVault
        db = SessionLocal()
        try:
            row = db.query(SecretVault.workgroup).filter(
                SecretVault.id == vault_id, SecretVault.backend == backend).first()
            return (row[0] or None) if row else None
        finally:
            db.close()
    except Exception:
        return None


def _vault_allows(vault_id: str | None, backend: str, workgroup: str | None) -> bool:
    """Whether a caller resolving for ``workgroup`` may read through ``vault_id``. A vault
    scoped to a workstream (secret_vault_service) resolves only for that workstream; a
    caller that names none — a global Settings key, a Config-Management run — is refused
    too, so a team's vault is never read on anyone else's behalf."""
    if not vault_id:
        return True
    scope = _vault_workgroup(vault_id, backend)
    return not scope or scope == (workgroup or "").strip().lower()


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

    ``workgroup`` is the workstream the value is resolved for. A reference into a vault
    scoped to another workstream (or to one, when the caller names none) resolves to ""
    — the same answer as any other failed read — and is logged."""
    for prefix, backend in _EXT_PREFIXES.items():
        if raw.startswith(prefix):
            vault_id, ref = _parse_ref(raw, backend)
            if not _vault_allows(vault_id, backend, workgroup):
                # Nothing from the reference is logged: CodeQL traces the vault id back
                # to the secret reference itself.
                logger.warning("Refused a secret reference into a vault scoped to "
                               "another workstream")
                return ""
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


def get_fresh(key: str, default: str = "", workgroup: str | None = None) -> str:
    """Like :func:`get`, but reads this one key straight from the DB.

    For a value ANOTHER process wrote seconds ago. get() answers from a snapshot up
    to _CACHE_TTL_SECONDS old, so a row that is definitely in app_config reads back
    as "" for up to five seconds — and the shape that hits is exactly the one that
    matters: the app mints a per-resource secret (``clouddb/<id>/admin``,
    ``cloudfn/<id>/bearer``), commits it, and creates a Job; the job runner claims
    that Job within POLL_INTERVAL (2s) and reads the secret back. The runner's
    snapshot is ALWAYS under 5s old — its supervisor pass re-reads ``worker_policy``
    through this cache every poll — so the read lands inside the stale window most
    of the time and the secret silently comes back empty.

    One indexed single-row SELECT, so it is only worth it where the value is both
    fresh and required. Everything read repeatedly (feature flags, cloud creds)
    should stay on get() and its cache.
    """
    from ..database import SessionLocal, AppConfig
    db = SessionLocal()
    try:
        rows = db.query(AppConfig).filter(AppConfig.key == key).all()
        scoped = {getattr(r, "workgroup", None): r.value for r in rows}
    finally:
        db.close()

    # Same lookup priority as get(): the workgroup row, then the global row.
    found_wg = workgroup if (workgroup is not None and workgroup in scoped) else None
    if found_wg is None and None not in scoped:
        return _resolve_external(default, workgroup=workgroup)
    stored = scoped[found_wg]
    value = _decrypt(stored) if stored else ""
    # Repair the cache under the scope the row actually came from, so a later get()
    # in the same dispatch (the tunnel broker, the Password Safe staging) doesn't go
    # back to reading the stale snapshot this call just worked around.
    with _cache_lock:
        _cache[(key, found_wg)] = value
    return _resolve_external(value, workgroup=workgroup)


def get_raw(key: str, default: str = "", workgroup: str | None = None) -> str:
    """Return the **stored** value for key *without* resolving external references.

    Unlike get(), which transparently resolves aws_sm:// / gcp_sm:// / … to the
    actual secret value, this returns the raw stored string — so callers can tell
    whether a config key holds a literal or a backend reference (and which backend).
    Used by the cloud Ansible runner to require a secret to live in a given cloud's
    store before injecting it via that provider's secret channel."""
    _ensure_loaded()
    with _cache_lock:
        raw = None
        if workgroup is not None:
            raw = _cache.get((key, workgroup))
        if raw is None:
            raw = _cache.get((key, None), default)
    return raw or ""


def get_opt(key: str, workgroup: str | None = None) -> str | None:
    """Return the stored value for key, or None when no row exists at all.

    get() cannot express this: its `default` argument folds "no row" and "a row
    holding an empty string" into the same answer, and get_bool() folds them
    further into the settings/env fallback. A migration that has to tell "the
    operator never touched this flag" (fall through to the config.py default)
    from "the operator deliberately set it to 0" (carry the 0 forward) needs the
    difference. See feature_flag_migration.seed_beyondtrust_split.

    Deliberately does NOT resolve external references, unlike get(): a feature
    flag is never a vault ref, and resolving one here would turn a boot-time
    seed into a network call."""
    _ensure_loaded()
    with _cache_lock:
        if workgroup is not None and (key, workgroup) in _cache:
            return _cache[(key, workgroup)]
        return _cache.get((key, None))


def is_reference(raw: str) -> bool:
    """True if raw is an external secret-backend reference (aws_sm://, azure_kv://,
    gcp_sm://, …) rather than a literal value."""
    return any((raw or "").startswith(p) for p in _EXT_PREFIXES)


def resolve_reference(raw: str, workgroup: str | None = None) -> str:
    """Resolve a raw external-backend reference string to its secret value.

    Unlike get(), which takes a stored config key, this resolves a reference the
    caller already holds (e.g. one a user typed into a form). Non-reference
    strings are returned unchanged. Returns "" if resolution fails."""
    return _resolve_external(raw, workgroup=workgroup)


def describe_reference(raw: str, workgroup: str | None = None):
    """Best-effort last-changed timestamp for an external-backend reference.

    Returns a naive-UTC ``datetime`` (the vault's own last-changed / last-rotated
    date) for an ``aws_sm://`` / ``azure_kv://`` / ``gcp_sm://`` / ``bt_safe://``
    ref, or ``None`` for a non-reference or when the backend can't report it.
    Cached alongside resolved values (same TTL) so staleness checks don't hammer
    the vault. Never raises — the backend describe path fails soft to ``None``.
    """
    for prefix, backend in _EXT_PREFIXES.items():
        if raw.startswith(prefix):
            vault_id, ref = _parse_ref(raw, backend)
            if not _vault_allows(vault_id, backend, workgroup):
                return None
            now = time.monotonic()
            cache_key = ("__desc__", raw, vault_id, workgroup)
            with _ext_cache_lock:
                cached = _ext_cache.get(cache_key)
                if cached and cached[1] > now:
                    return cached[0]
            from .secrets_backend_service import describe_sync
            dt = describe_sync(backend, ref, vault_id=vault_id)
            with _ext_cache_lock:
                _ext_cache[cache_key] = (dt, now + EXT_CACHE_TTL)
            return dt
    return None


def get_bool(key: str, default: bool = False) -> bool:
    """Return a config flag as bool. Stored as '1'/'0' (legacy rows may hold
    'True'/'False' from pre-normalization _write_feature); env-var fallback via settings."""
    val = get(key)
    if val:
        return val.strip().lower() in ("1", "true", "yes", "on")
    # Fall back to settings (env var)
    from ..config import settings
    return bool(getattr(settings, key, default))


def set(key: str, value: str, workgroup: str | None = None) -> None:
    """Encrypt and persist a single value; update in-memory cache.

    When workgroup is None, writes/updates the global row. When given,
    writes/updates the workgroup-scoped row, leaving the global one alone.
    """
    _write({key: value}, workgroup)


def set_many(pairs: dict, workgroup: str | None = None) -> None:
    """Encrypt and persist multiple values in one transaction; update cache.

    All pairs land in the same workgroup row-set; call repeatedly with
    different workgroup args if you need to write across scopes.
    """
    _write(pairs, workgroup)


def _write(pairs: dict, workgroup: str | None) -> None:
    """Update each row, or insert it. One retry when an insert loses to another process.

    Two processes can both find a key absent -- the image's two gunicorn workers each run
    the background sweepers, which record their last result here -- and the slower
    insert fails on the primary key. The retry finds the row the other one wrote and
    updates it: last write wins, as it does whenever both rows already exist.
    """
    from sqlalchemy.exc import IntegrityError
    from ..database import SessionLocal, AppConfig
    for attempt in (1, 2):
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
                    existing.updated_at = utcnow()
                else:
                    db.add(AppConfig(key=key, value=encrypted, workgroup=workgroup,
                                     updated_at=utcnow()))
            db.commit()
        except IntegrityError:
            db.rollback()
            if attempt == 2:
                raise
            continue
        finally:
            db.close()
        with _cache_lock:
            for k, v in pairs.items():
                _cache[(k, workgroup)] = v
        return


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

# Every key whose value is a credential. One list for the whole app -- see
# secret_hygiene.SECRET_KEYS for why it replaced four.
from .secret_hygiene import BACKEND_PREFIXES as _VAULT_PREFIXES  # noqa: E402
from .secret_hygiene import SECRET_KEYS as _SECRET_KEYS  # noqa: E402
from ..utils.clock import utcnow

#: What get_all_public() puts in place of a stored secret.
MASK = "••••••••"


def _is_vault_ref(value) -> bool:
    """A pointer into an external vault (``azure_kv://…``) rather than the secret itself.
    Not sensitive, and it is exactly what a config migration should carry, so it is shown."""
    return isinstance(value, str) and any(
        p and value.startswith(p) for p in _VAULT_PREFIXES.values())


def get_all_public() -> dict:
    """Return all GLOBAL config key-value pairs with secrets replaced by
    bullets. Workgroup-scoped rows are intentionally excluded — this helper
    serves the legacy settings UI which is global-only.
    """
    _ensure_loaded()
    with _cache_lock:
        return {
            key: (MASK if key in _SECRET_KEYS and v and not _is_vault_ref(v) else v)
            for (key, wg), v in _cache.items()
            if wg is None and key != "setup_complete"
        }
