"""Cloud identity providers as managed directories: Entra ID, Okta, PingOne.

The domain directories (AD, LDAP) are changed by Ansible through an agent; these are
SaaS APIs the dashboard calls itself, so they get a small provider registry instead of
another branch in ``directory_service``. :data:`PROVIDERS` maps a row's ``provider`` to
its module; every module has the same functions (see ``base``).

Credentials follow the directories rule: nothing on the row but a pointer.
:func:`authorize` turns the pointer into an auth header just in time —

* a vault ref (``bt_safe://`` ``aws_sm://`` ``azure_kv://`` ``gcp_sm://`` ``wlc://``)
  through ``config_service.resolve_reference``;
* ``psmanaged:{system_id, account_id}`` through a Password Safe request, the same
  contract as ``directory_service.directory_connection_vars``;
* nothing, for Entra's ``dashboard_azure`` mode.

**Header cache.** Browsing is many small calls, and without a cache each would open a
Password Safe request or hit the token endpoint. The header (an OAuth bearer, or an Okta
SSWS value) is kept in-process until shortly before it expires, keyed by the row's id
and a fingerprint of everything that decides the credential, so editing the row is a
miss. It is per-process on purpose: a gunicorn sibling missing it only signs in again.
"""
import asyncio
import hashlib
import json
import logging
import time
from typing import Optional

from . import base, entra, okta, pingone
from .base import IdPConnection, IdPError

logger = logging.getLogger(__name__)

PROVIDERS = {"entra_id": entra, "okta": okta, "pingone": pingone}

AUTH_MODES = {
    "entra_id": ("client_secret", "dashboard_azure"),
    "okta": ("ssws", "private_key_jwt"),
    "pingone": ("client_secret",),
}
# Auth modes that need no credential reference at all.
NO_SECRET_MODES = ("dashboard_azure",)

# Non-secret per-provider extras allowed in `options`. Closed and pinned by a test: the
# blob sits on a row next to a credential pointer, and an open key set is how a secret
# ends up in it by accident.
OPTION_KEYS = {
    "entra_id": (),
    "okta": ("key_id",),
    "pingone": ("token_endpoint_auth",),
}

PSMANAGED_PREFIX = "psmanaged:"
# An Okta API token has no expiry of its own; hold its header no longer than this, and
# never longer than the Password Safe request it came from.
_STATIC_HEADER_TTL = 900

_cache: dict = {}       # directory_id -> (fingerprint, header, expires_monotonic)


def vault_prefixes() -> tuple:
    from .. import config_service
    return tuple(config_service._EXT_PREFIXES)


def credential_kind(ref: str, auth_mode: str = "") -> str:
    """``vault`` | ``psmanaged`` | ``dashboard_azure`` | "" — for display; never the ref."""
    if auth_mode in NO_SECRET_MODES:
        return auth_mode
    ref = ref or ""
    if ref.startswith(PSMANAGED_PREFIX):
        return "psmanaged"
    if ref.startswith(vault_prefixes()):
        return "vault"
    return ""


def _fingerprint(row) -> str:
    parts = [row.provider, row.endpoint, row.tenant_id, row.client_id, row.auth_mode,
             row.credentials_ref, row.options, bool(row.writes_enabled)]
    return hashlib.sha256(json.dumps(parts, default=str).encode()).hexdigest()


def forget(directory_id: str) -> None:
    _cache.pop(directory_id, None)


def options_of(row) -> dict:
    try:
        out = json.loads(row.options or "{}")
    except (TypeError, ValueError):
        return {}
    allowed = OPTION_KEYS.get(row.provider, ())
    return {k: v for k, v in (out if isinstance(out, dict) else {}).items() if k in allowed}


def _page_size() -> int:
    try:
        from ..directory_service import _cfg
        return int(_cfg("directory_idp_page_size", str(base.PAGE_SIZE_DEFAULT)))
    except (TypeError, ValueError):
        return base.PAGE_SIZE_DEFAULT


async def _secret(row) -> tuple:
    """``(plaintext, max_hold_seconds)`` for the row's credential pointer."""
    if row.auth_mode in NO_SECRET_MODES:
        return "", 0
    ref = (row.credentials_ref or "").strip()
    label = PROVIDERS[row.provider].LABEL
    if ref.startswith(PSMANAGED_PREFIX):
        from .. import btapi_service
        from ..directory_service import _cfg, _managed_ref_or_none
        pin = _managed_ref_or_none(row)
        if not pin:
            raise IdPError(f"{label}: the Password Safe account reference is unreadable — "
                           f"re-register the directory")
        duration = int(_cfg("ansible_managed_request_duration_min", "60") or 60)
        try:
            _req, value = await btapi_service.get_ps_credential_with_request(
                pin["system_id"], pin["account_id"], duration_min=duration)
        except btapi_service.BTAPIError as exc:
            raise IdPError(f"{label}: the Password Safe checkout failed") from exc
        if not value:
            raise IdPError(f"{label}: Password Safe returned an empty credential")
        return value, max(60, duration * 60 - 60)
    if ref.startswith(vault_prefixes()):
        from .. import config_service
        value = await asyncio.to_thread(config_service.resolve_reference, ref, row.workgroup)
        if not value or value == ref:
            raise IdPError(f"{label}: the secret reference could not be resolved — check "
                           f"the external secret backend")
        return value, _STATIC_HEADER_TTL
    raise IdPError(f"{label}: no usable credential reference on this directory")


def connection(row, secret: str = "") -> IdPConnection:
    return IdPConnection(
        directory_id=row.id, provider=row.provider, endpoint=row.endpoint or "",
        tenant_id=row.tenant_id or "", client_id=row.client_id or "",
        auth_mode=row.auth_mode or "", secret=secret, options=options_of(row),
        page_size=_page_size(), writes_enabled=bool(row.writes_enabled))


async def authorize(row, *, fresh: bool = False) -> tuple:
    """``(module, IdPConnection-without-secret, auth_header)`` for one row."""
    module = PROVIDERS.get(row.provider)
    if module is None:
        raise IdPError(f"{row.provider!r} is not a cloud identity provider")
    fp = _fingerprint(row)
    hit = _cache.get(row.id)
    now = time.monotonic()
    if hit and not fresh and hit[0] == fp and hit[2] > now:
        return module, connection(row), hit[1]
    secret, hold = await _secret(row)
    header, expires = await module.auth_header(connection(row, secret))
    if hold:
        expires = min(expires, now + hold) if expires else now + hold
    if expires and expires > now:
        _cache[row.id] = (fp, header, expires)
    return module, connection(row), header


async def call(row, fn: str, *args, **kwargs):
    """Run provider function ``fn`` for ``row``; one re-sign-in on a 401."""
    module, conn, header = await authorize(row)
    try:
        return await getattr(module, fn)(conn, header, *args, **kwargs)
    except IdPError as exc:
        if "(HTTP 401)" not in str(exc):
            raise
    forget(row.id)
    module, conn, header = await authorize(row, fresh=True)
    return await getattr(module, fn)(conn, header, *args, **kwargs)


def label(provider: str) -> Optional[str]:
    module = PROVIDERS.get(provider)
    return module.LABEL if module else None
