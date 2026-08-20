"""BeyondTrust Workload Credentials (WC / "SMoP") client.

The dashboard's third credential posture, alongside the static keys in
``app_config`` and the Entitle machine-identity gate. WC mints **short-lived**
AWS and Azure credentials on demand, so the standing cloud secret stops existing
rather than merely being time-boxed.

Nothing here changes behaviour until an operator turns it on:
``workload_credentials_enabled`` gates the whole module and every per-cloud flag
defaults off. A community install with no BeyondTrust products never reaches
this code.

Shape notes, because two of them are easy to get wrong
------------------------------------------------------
**Synchronous, deliberately.** ``secrets_backend_service``'s dispatch tables are
sync (callers push them off the event loop with ``asyncio.to_thread``), and
``aws_service._aws_kwargs`` is sync too. An async HTTP layer would force a bridge
at both. Timeouts stay short because that thread pool is small and one slow
external call has wedged this app before.

**The API version is a header, not a path.** ``bt-secrets-api-version`` is
mandatory; omit it and requests fail in a way that reads like an auth problem.
The default matches the shipping Terraform provider's ``DefaultAPIVersion``.

The path grammar mirrors the provider's ``BuildPath``::

    /site/{site-id}/secrets[/{path-version}]{endpoint}

with an optional ``?folder=`` query for anything addressed by folder + name.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Matches the Terraform provider's client.DefaultAPIVersion. Date-based; a newer
# value changes response shapes, so it is config-overridable rather than pinned.
DEFAULT_API_VERSION = "2026-04-28"
DEFAULT_API_URL = "https://api.beyondtrust.io"

# Short on purpose — see the module docstring on the thread pool.
_TIMEOUT_SECONDS = 15.0


class WorkloadCredentialsError(Exception):
    """Any failure talking to Workload Credentials.

    Raised rather than returning an empty value. A credential fetch that fails
    quietly is indistinguishable from "this cloud is on the static tier", which
    is the most confusing state this feature could produce.
    """


# ── Config ────────────────────────────────────────────────────────────────────

def _cfg(key: str, fallback: str = "") -> str:
    """Config value with the usual DB then settings precedence."""
    try:
        from . import config_service
        val = config_service.get(key)
        if val:
            return val
    except Exception:
        pass
    try:
        from ..config import settings
        return str(getattr(settings, key, "") or fallback)
    except Exception:
        return fallback


def _enabled() -> bool:
    try:
        from . import config_service
        return config_service.get_bool("workload_credentials_enabled", default=False)
    except Exception:
        return False


def configured() -> bool:
    """True when the master flag is on and the required values are set.

    Checked before any request so a half-configured install produces one clear
    message instead of an HTTP error per call site.
    """
    if not _enabled():
        return False
    return bool(_cfg("wlc_site_id") and _cfg("wlc_pat"))


def _missing() -> list:
    """Which required settings are blank, for the error message."""
    out = []
    if not _cfg("wlc_site_id"):
        out.append("wlc_site_id")
    if not _cfg("wlc_pat"):
        out.append("wlc_pat")
    return out


# ── Pure helpers (stdlib only — unit-testable without config_service) ─────────

def build_secrets_path(site_id: str, endpoint: str, path_version: str = "") -> str:
    """The ``/site/{id}/secrets`` path for ``endpoint``.

    Mirrors the Terraform provider's ``Client.BuildPath``, including the optional
    path-version segment, so a deployment needing a pinned path version behaves
    the same here as it does in Terraform.
    """
    if not endpoint.startswith("/"):
        endpoint = "/" + endpoint
    if path_version:
        return f"/site/{site_id}/secrets/{path_version}{endpoint}"
    return f"/site/{site_id}/secrets{endpoint}"


def build_auth_path(site_id: str, endpoint: str) -> str:
    """The platform-auth path, used for workload-identity registration.

    A separate grammar from :func:`build_secrets_path` — the auth service lives
    at ``/site/{id}/platform/auth`` and takes no path version.
    """
    if not endpoint.startswith("/"):
        endpoint = "/" + endpoint
    return f"/site/{site_id}/platform/auth{endpoint}"


def _first(mapping: dict, *names: str) -> Any:
    """First present, non-empty value among ``names``."""
    for name in names:
        val = mapping.get(name)
        if val not in (None, ""):
            return val
    return None


def parse_expiration(value: Any) -> Optional[datetime]:
    """A lease expiry as naive UTC, or None if unparseable.

    Naive UTC to match every timestamp column in ``database.py``. Returns None
    rather than raising: the caller treats an unreadable expiry as "refresh now",
    which is the safe direction.
    """
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed.astimezone(timezone.utc).replace(tzinfo=None) if parsed.tzinfo else parsed


def parse_generated(payload: Any) -> dict:
    """Normalise a ``generate`` response into a flat, predictable dict.

    Returns ``{"values": {...}, "lease_id": str, "expires_at": datetime|None}``.

    Two tolerances, both taken from BeyondTrust's own GitHub Action rather than
    invented here: field names are accepted in **camelCase or PascalCase**, and
    ``leaseId`` / ``expiration`` are read from either the ``secret`` object or the
    response root. The published docs disagree with each other on both, so
    accepting the union is cheaper than betting on one and failing opaquely.
    """
    if not isinstance(payload, dict):
        raise WorkloadCredentialsError(
            f"generate returned {type(payload).__name__}, expected a JSON object")

    secret = payload.get("secret")
    if not isinstance(secret, dict):
        # Some shapes put the credential at the root. Accept that, but only when
        # it actually looks like a credential — otherwise the error below says far
        # more than a dict of metadata masquerading as one would.
        secret = payload if any(
            k in payload for k in ("accessKeyId", "AccessKeyId", "clientId", "ClientId")
        ) else {}

    lease_id = _first(secret, "leaseId", "LeaseId") or _first(payload, "leaseId", "LeaseId")
    expiration = (_first(secret, "expiration", "Expiration")
                  or _first(payload, "expiration", "Expiration"))

    access_key = _first(secret, "accessKeyId", "AccessKeyId")
    client_id = _first(secret, "clientId", "ClientId")

    if access_key:
        # AWS: the assumed-role triple.
        values = {
            "access_key_id":     access_key,
            "secret_access_key": _first(secret, "secretAccessKey", "SecretAccessKey"),
            "session_token":     _first(secret, "sessionToken", "SessionToken"),
        }
        optional = ()
    elif client_id:
        # Azure: service-principal client credentials.
        values = {
            "client_id":     client_id,
            "client_secret": _first(secret, "clientSecret", "ClientSecret"),
            "tenant_id":     _first(secret, "tenantId", "TenantId"),
            "key_id":        _first(secret, "keyId", "KeyId"),
        }
        # key_id is only needed to correlate a revoke; absence is not a failure.
        optional = ("key_id",)
    else:
        # Names only — never the values.
        raise WorkloadCredentialsError(
            "generate response contained no recognised credential fields "
            f"(saw: {', '.join(sorted(secret)) or 'nothing'})")

    absent = [k for k, v in values.items() if v in (None, "") and k not in optional]
    if absent:
        raise WorkloadCredentialsError(
            f"generate response is missing {', '.join(absent)}")

    return {
        "values":     values,
        "lease_id":   str(lease_id) if lease_id else "",
        "expires_at": parse_expiration(expiration),
    }


def refresh_due(expires_at: Optional[datetime], issued_at: Optional[datetime],
                margin_pct: int, now: Optional[datetime] = None) -> bool:
    """Whether a lease should be regenerated now.

    True once less than ``margin_pct`` of the original TTL remains. A missing or
    unparseable expiry is always due — refreshing an unknown lease costs one
    metered issuance, whereas trusting it risks every cloud call failing.

    ``margin_pct`` is clamped to 1..99 so a mis-set 0 or 100 cannot mean either
    "never refresh" or "refresh on every check" — the latter would bill per call.
    """
    if now is None:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
    if expires_at is None or now >= expires_at:
        return True
    pct = min(99, max(1, int(margin_pct or 50)))
    if issued_at is None:
        # No issue time to measure against: treat the window as an hour.
        return (expires_at - now).total_seconds() <= (3600 * pct / 100.0)
    ttl = (expires_at - issued_at).total_seconds()
    if ttl <= 0:
        return True
    return (expires_at - now).total_seconds() <= (ttl * pct / 100.0)


def static_value_from(payload: Any) -> str:
    """The stored string for a static-secret read.

    A Workload Credentials static secret is a **map** of key to value, not a
    scalar — the Terraform provider models it as ``secret_wo = { token = "..." }``
    and its ephemeral read exposes ``.secret["password"]``. So the value comes
    back re-serialised as JSON, which is also exactly what the dashboard's
    Secrets page stores for every other backend (all values are JSON by
    convention, enforced by ``validate_json_value``).
    """
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict):
        for key in ("secret", "value", "data"):
            inner = payload.get(key)
            if isinstance(inner, str):
                return inner
            if isinstance(inner, (dict, list)):
                return json.dumps(inner)
    return json.dumps(payload)


def static_write_body(value: str) -> dict:
    """The request body for creating or updating a static secret.

    The dashboard stores every secret as a JSON document and WC stores a map, so
    a JSON object maps across directly. Anything that is not an object (a bare
    string, a number, a list) is wrapped under ``value`` rather than rejected —
    the Secrets page accepts any valid JSON, and failing on a scalar would be a
    worse trade than a predictable wrapper key.
    """
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        parsed = value
    if not isinstance(parsed, dict):
        parsed = {"value": value}
    return {"secret": parsed}


def error_message_from(status_code: int, body: Any) -> str:
    """A useful message for an error response.

    WC guarantees a machine-actionable ``Code`` and human-readable ``Message`` on
    errors (backend 0.1.46 onward), so prefer those over the raw body — and cap
    the fallback, because an HTML error page would otherwise land verbatim in a
    job's ``error_message``, which is the only failure detail the UI renders.
    """
    if isinstance(body, dict):
        message = _first(body, "Message", "message", "error", "detail")
        code = _first(body, "Code", "code")
        if message:
            suffix = f", code {code}" if code else ""
            return f"Workload Credentials error (HTTP {status_code}{suffix}): {message}"
    if isinstance(body, str) and body.strip():
        return f"Workload Credentials error (HTTP {status_code}): {body[:200]}"
    return f"Workload Credentials error (HTTP {status_code})"


def is_conflict(exc: Exception) -> bool:
    """Whether an error is a 409 — the name is taken, or a ``cas`` version clash."""
    text = str(exc).lower()
    return "409" in text or "conflict" in text or "exist" in text


# ── HTTP ──────────────────────────────────────────────────────────────────────

def _headers(merge_patch: bool = False) -> dict:
    out = {
        "Authorization": f"Bearer {_cfg('wlc_pat')}",
        "bt-secrets-api-version": _cfg("wlc_api_version") or DEFAULT_API_VERSION,
        "Accept": "application/json",
    }
    if merge_patch:
        # Updates are JSON Merge Patch (RFC 7396): a null deletes a field and an
        # omitted field is left alone. These routes reject a plain
        # application/json body.
        out["Content-Type"] = "application/merge-patch+json"
    return out


def _request(method: str, endpoint: str, *, folder: str = "",
             body: Any = None, query: Optional[dict] = None) -> Any:
    """One Workload Credentials call.

    Raises :class:`WorkloadCredentialsError` on anything that is not a 2xx, using
    the server's coded message when it sends one.
    """
    if not configured():
        missing = _missing()
        detail = (" (missing: " + ", ".join(missing) + ")") if missing else \
                 " (workload_credentials_enabled is off)"
        raise WorkloadCredentialsError(
            "Workload Credentials is not configured" + detail)

    import httpx

    base = (_cfg("wlc_api_base_url") or DEFAULT_API_URL).rstrip("/")
    path = build_secrets_path(_cfg("wlc_site_id"), endpoint, _cfg("wlc_api_path_version"))
    params = dict(query or {})
    if folder:
        params["folder"] = folder

    try:
        with httpx.Client(timeout=_TIMEOUT_SECONDS) as client:
            resp = client.request(method, base + path,
                                  headers=_headers(merge_patch=(method == "PATCH")),
                                  params=params or None, json=body)
    except httpx.HTTPError as exc:
        raise WorkloadCredentialsError(f"Workload Credentials unreachable: {exc}") from exc

    if resp.status_code >= 400:
        try:
            parsed = resp.json()
        except ValueError:
            parsed = resp.text
        raise WorkloadCredentialsError(error_message_from(resp.status_code, parsed))
    if not resp.content:
        return None
    try:
        return resp.json()
    except ValueError as exc:
        raise WorkloadCredentialsError(
            f"Workload Credentials returned non-JSON (HTTP {resp.status_code})") from exc


# ── Operations ────────────────────────────────────────────────────────────────

def test_connection() -> dict:
    """Verify credentials and reachability.

    ``GET /session`` validates the current authentication, so success here means
    the PAT, site id and API version are all good — without creating anything or
    incurring a metered credential issuance.
    """
    _request("GET", "/session")
    base = (_cfg("wlc_api_base_url") or DEFAULT_API_URL).rstrip("/")
    return {"ok": True,
            "message": f"Connected to Workload Credentials at {base} (site {_cfg('wlc_site_id')})."}


def list_folders() -> list:
    data = _request("GET", "/folders")
    if isinstance(data, list):
        return data
    return (data or {}).get("folders") or []


def list_static(folder: str = "") -> list:
    data = _request("GET", "/static", folder=folder)
    if isinstance(data, list):
        return data
    for key in ("secrets", "static", "items"):
        val = (data or {}).get(key)
        if isinstance(val, list):
            return val
    return []


def read_static(name: str, folder: str = "") -> str:
    return static_value_from(_request("GET", "/static/" + name, folder=folder))


def write_static(name: str, value: str, folder: str = "") -> None:
    """Create or update a static secret.

    POST creates and 409s when the name is taken, so a conflict falls through to
    PATCH. Mirrors ``write_aws_sm``'s create-then-put shape, which is what lets
    the Secrets page behave identically across every backend.
    """
    body = static_write_body(value)
    try:
        _request("POST", "/static/" + name, folder=folder, body=body)
    except WorkloadCredentialsError as exc:
        if not is_conflict(exc):
            raise
        _request("PATCH", "/static/" + name, folder=folder, body=body)


def delete_static(name: str, folder: str = "") -> None:
    _request("DELETE", "/static/" + name, folder=folder)


def static_metadata(name: str, folder: str = "") -> dict:
    """Metadata (timestamps, tags, version) without reading the value.

    Used for staleness reporting, so the age shown is WC's own last-changed date
    rather than when the reference happened to be pasted into the dashboard.
    """
    data = _request("GET", "/static/" + name + "/metadata", folder=folder)
    return data if isinstance(data, dict) else {}


def generate(name: str, folder: str = "") -> dict:
    """Mint a credential from a dynamic secret. **This is the metered call.**

    Returns the :func:`parse_generated` shape. Every caller must cache the result
    for the lease's lifetime — pricing is per issuance, so calling this per
    request is both a cost and a rate-limit problem.
    """
    payload = _request("POST", "/dynamic/" + name + "/generate", folder=folder)
    result = parse_generated(payload)
    logger.info("WC: generated lease %s from dynamic secret %s (expires %s)",
                result["lease_id"] or "(none)", name, result["expires_at"])
    return result


def get_lease(lease_id: str) -> dict:
    data = _request("GET", "/leases/id/" + lease_id)
    return data if isinstance(data, dict) else {}


def revoke_lease(lease_id: str) -> None:
    """Release a lease early.

    Only Azure leases are revocable; AWS returns ``400 lease_not_revocable``
    because STS credentials cannot be withdrawn before they expire. That refusal
    is expected rather than an error, so it is swallowed — callers revoke
    unconditionally and let the provider decide.
    """
    if not lease_id:
        return
    try:
        _request("DELETE", "/leases/id/" + lease_id)
    except WorkloadCredentialsError as exc:
        if "not_revocable" in str(exc):
            logger.debug("WC: lease %s is not revocable (expected for AWS)", lease_id)
            return
        raise
