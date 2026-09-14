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

**Two auth modes, and the second one stores nothing.** ``wlc_auth_mode`` is
either ``pat`` (a stored Personal Access Token) or ``entra`` (this container's
own Azure managed identity, trusted by a **Workload Identity** registered in
Pathfinder). The second removes the last standing credential this feature
needed. See the Auth section below.

**The API version is a header, not a path.** ``bt-secrets-api-version`` is
mandatory; omit it and requests fail in a way that reads like an auth problem.
The default matches the shipping Terraform provider's ``DefaultAPIVersion``.

The path grammar mirrors the provider's ``BuildPath``::

    /site/{site-id}/secrets[/{path-version}]{endpoint}

with an optional ``?folder=`` query for anything addressed by folder + name.

**Confirmed against a live site, 2026-08-21.** This was the one part that could not be
settled from the provider, which manages configuration and never calls ``generate``; the
vendor wiki documented two incompatible shapes. A real issuance from
``POST /dynamic/{name}/generate?folder={folder}`` returned the credential nested under a
``secret`` object in camelCase, with ``accessKeyId`` / ``secretAccessKey`` /
``sessionToken`` / ``leaseId`` / ``expiration`` plus ``credentialType`` and ``type``. The
requested TTL of 3600 came back intact, so AWS's one-hour role-chaining limit does not
bite at that value even though this is a three-hop chain. See
``tests/test_workload_credentials.py`` for the recorded payload.
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
    message instead of an HTTP error per call site. What "required" means depends
    on the auth mode — see :func:`_missing`.
    """
    if not _enabled():
        return False
    return not _missing()


def _missing() -> list:
    """Which required settings are blank, for the error message.

    **Auth-mode aware.** A message naming ``wlc_pat`` on an install running on a
    workload identity sends an operator hunting for a token they are deliberately
    not holding, which is the exact confusion this mode exists to end.
    """
    out = []
    if not _cfg("wlc_site_id"):
        out.append("wlc_site_id")
    if auth_mode() == AUTH_MODE_ENTRA:
        if not _cfg("wlc_service_name"):
            out.append("wlc_service_name")
        if not _cfg("wlc_entra_resource"):
            out.append("wlc_entra_resource")
    elif not _cfg("wlc_pat"):
        out.append("wlc_pat")
    return out


def missing_settings() -> list:
    """:func:`_missing`, for callers outside this module.

    Its three callers each named ``wlc_pat`` in a literal of their own, which is
    wrong the moment an install authenticates with a workload identity instead —
    and a "set wlc_pat" message is the worst possible advice there. They ask here
    now.
    """
    return _missing()


# ── Auth ──────────────────────────────────────────────────────────────────────
#
# Two ways to present this dashboard to Workload Credentials, and the second one
# is why this section is long.
#
# ``pat``    A Personal Access Token minted in Pathfinder and stored encrypted in
#            ``app_config``. Long-lived, and the one standing credential this
#            feature never removed: WC collapsed three cloud keys into one
#            platform token rather than into nothing.
#
# ``entra``  **Nothing stored at all.** The container's own Azure managed
#            identity produces a short-lived Entra token at call time, and
#            Pathfinder accepts it because a **Workload Identity** registered
#            there names that identity's issuer and service-principal object id.
#            There is no secret in ``app_config``, none in the deployment
#            template, and nothing to rotate.
#
# **Registering the trust is a GUI action in Pathfinder and has no client here,
# on purpose.** Administration → Workload Identities takes the issuer, the
# constraint on ``sub`` and the site. A dashboard that could register its own
# trust would be holding a credential that creates credentials, which is the
# thing this mode exists to get rid of. The registration's **Service Name** is
# the only part that comes back here: it travels on every request as
# ``X-BT-Service-Name``, telling the platform which registration to evaluate the
# token against.
#
# Pathfinder registers three issuer categories — GitHub Actions, Azure Entra ID
# and a Custom IDP with explicit claim conditions. Only the Azure one is wired
# here, because the thing being authenticated is an Azure-hosted container. The
# other two describe workloads that are not this process (a CI job, a third-party
# IdP) and would need a token source this code has no business owning.

AUTH_MODE_PAT = "pat"
AUTH_MODE_ENTRA = "entra"
VALID_AUTH_MODES = (AUTH_MODE_PAT, AUTH_MODE_ENTRA)

# Azure's link-local instance-metadata endpoint. The FALLBACK, not the default:
# Container Apps and App Service inject a per-replica ``IDENTITY_ENDPOINT`` plus
# an ``IDENTITY_HEADER`` secret instead, and 169.254.169.254 is not reachable
# from a Container App at all. Preferring the injected pair is what makes this
# work on the runtime the reference install actually uses.
_IMDS_TOKEN_URL = "http://169.254.169.254/metadata/identity/oauth2/token"
_IDENTITY_API_VERSION = "2019-08-01"

# Re-fetch this long before the platform's stated expiry. Entra tokens run about
# an hour; five minutes of margin covers a slow call that starts just under the
# wire. Unlike a dynamic secret, fetching one of these is FREE and unmetered, so
# the margin can be generous — nothing here is billed.
_TOKEN_MARGIN_SECONDS = 300

# Process-local, and that is correct here where it would be wrong for a lease.
# ``workload_credential_lease`` lives in the database because each issuance is
# BILLED and three processes must not buy three credentials. A managed-identity
# token costs nothing, so a per-process memo is just a cache; the worst a second
# process can do is fetch its own copy.
_token_cache: dict = {"key": "", "token": "", "expires_at": 0.0}


def auth_mode() -> str:
    """``pat`` or ``entra``; anything unrecognised reads as ``pat``.

    Falling back to the stored-token path rather than to the identity path is
    deliberate: a typo should degrade to the mode whose failure is a plain 401,
    not to one that goes looking for a metadata endpoint and reports something
    about Azure on an install that never mentioned Azure.
    """
    mode = (_cfg("wlc_auth_mode") or AUTH_MODE_PAT).strip().lower()
    return mode if mode in VALID_AUTH_MODES else AUTH_MODE_PAT


def clear_token_cache() -> None:
    """Forget the memoised Entra token.

    Called when the Workload Credentials panel is saved, for the same reason the
    lease memo is cleared there: the resource or the identity may have just
    changed, and an operator watching their own edit do nothing for up to an hour
    would reasonably conclude the mode is broken.
    """
    _token_cache.update({"key": "", "token": "", "expires_at": 0.0})


def build_identity_request(resource: str, client_id: str = "",
                           env: Optional[dict] = None) -> tuple:
    """``(url, headers, params)`` for the platform's token endpoint. Pure.

    ``IDENTITY_ENDPOINT`` + ``IDENTITY_HEADER`` when the runtime injects them
    (Container Apps, App Service), otherwise IMDS. ``client_id`` selects a
    **user-assigned** identity and is omitted for a system-assigned one — sending
    it blank is not the same thing, it asks for an identity with no client id and
    fails.
    """
    import os
    env = os.environ if env is None else env
    params = {"api-version": _IDENTITY_API_VERSION, "resource": resource}
    if client_id:
        params["client_id"] = client_id
    endpoint = (env.get("IDENTITY_ENDPOINT") or "").strip()
    header = (env.get("IDENTITY_HEADER") or "").strip()
    if endpoint and header:
        return endpoint, {"X-IDENTITY-HEADER": header}, params
    return _IMDS_TOKEN_URL, {"Metadata": "true"}, params


def parse_identity_token(payload: Any, now_epoch: float) -> tuple:
    """``(token, expires_at_epoch)`` from a managed-identity token response. Pure.

    ``expires_on`` is an absolute epoch **as a string** from IMDS and Container
    Apps; ``expires_in`` is a relative fallback. An unreadable expiry becomes
    ``now`` rather than an error, so the token is used once and re-fetched next
    call — the cache is an optimisation and must never be the reason a request
    fails.
    """
    if not isinstance(payload, dict):
        raise WorkloadCredentialsError(
            "managed identity returned "
            f"{type(payload).__name__}, expected a JSON object")
    token = payload.get("access_token") or payload.get("accessToken") or ""
    if not token:
        raise WorkloadCredentialsError(
            "managed identity response carried no access_token")
    expires_at = now_epoch
    raw = _first(payload, "expires_on", "expiresOn")
    if raw is not None:
        try:
            expires_at = float(str(raw).strip())
        except (TypeError, ValueError):
            expires_at = now_epoch
    else:
        raw_in = _first(payload, "expires_in", "expiresIn")
        try:
            expires_at = now_epoch + float(str(raw_in).strip())
        except (TypeError, ValueError):
            expires_at = now_epoch
    return str(token), expires_at


def identity_error_message(status_code: int, body: Any) -> str:
    """A message for a failed token fetch that names the likely cause.

    The platform's own wording here is thin (``identity_not_found``), and the
    cause is nearly always one of two deployment facts rather than anything in
    this app: no identity assigned to the container, or a resource the tenant
    will not issue a token for. Saying so beats echoing the body.
    """
    detail = ""
    if isinstance(body, dict):
        detail = str(_first(body, "error_description", "Message", "message",
                            "error") or "")
    elif isinstance(body, str):
        detail = body[:200]
    hint = ""
    if status_code in (400, 404):
        hint = (" — check that a managed identity is assigned to this container "
                "and that wlc_entra_resource is an App ID URI the tenant will "
                "issue for")
    suffix = f": {detail}" if detail else ""
    return (f"could not get a managed identity token (HTTP {status_code})"
            f"{hint}{suffix}")


def _entra_token() -> str:
    """A bearer token for this container's own identity, memoised until expiry."""
    import time

    import httpx

    resource = _cfg("wlc_entra_resource")
    if not resource:
        raise WorkloadCredentialsError(
            "Workload identity auth needs wlc_entra_resource — the App ID URI "
            "the token is requested for, which becomes its `aud` claim")
    client_id = _cfg("wlc_entra_client_id")

    # Keyed on what the token is FOR. Without this, changing the resource or
    # switching identities keeps serving a token minted for the old one, and the
    # failure lands at BeyondTrust as an opaque 401.
    key = f"{resource}|{client_id}"
    now = time.time()
    if (_token_cache.get("key") == key and _token_cache.get("token")
            and _token_cache.get("expires_at", 0.0) > now):
        return str(_token_cache["token"])

    url, headers, params = build_identity_request(resource, client_id)
    try:
        with httpx.Client(timeout=_TIMEOUT_SECONDS) as client:
            resp = client.get(url, headers=headers, params=params)
    except httpx.HTTPError as exc:
        raise WorkloadCredentialsError(
            "no managed identity endpoint reachable — a workload identity needs "
            f"one assigned to this container: {exc}") from exc

    if resp.status_code >= 400:
        try:
            parsed = resp.json()
        except ValueError:
            parsed = resp.text
        raise WorkloadCredentialsError(
            identity_error_message(resp.status_code, parsed))
    try:
        payload = resp.json()
    except ValueError as exc:
        raise WorkloadCredentialsError(
            "managed identity endpoint returned non-JSON "
            f"(HTTP {resp.status_code})") from exc

    token, expires_at = parse_identity_token(payload, now)
    _token_cache.update({"key": key, "token": token,
                         "expires_at": expires_at - _TOKEN_MARGIN_SECONDS})
    return token


def _auth_headers() -> dict:
    """Authorization, plus whatever routes the request to an identity.

    In ``entra`` mode ``X-BT-Service-Name`` is not optional decoration: without
    it the platform holds a valid token and no statement of which registered
    Workload Identity it is supposed to satisfy.
    """
    if auth_mode() == AUTH_MODE_ENTRA:
        return {
            "Authorization": f"Bearer {_entra_token()}",
            "X-BT-Service-Name": _cfg("wlc_service_name"),
        }
    return {"Authorization": f"Bearer {_cfg('wlc_pat')}"}


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
        "bt-secrets-api-version": _cfg("wlc_api_version") or DEFAULT_API_VERSION,
        "Accept": "application/json",
    }
    # Authorization comes from the auth mode, which may mean a live call to the
    # platform's token endpoint — so it is built per request rather than held.
    out.update(_auth_headers())
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
    the site id, the API version and whatever the auth mode presents — a stored
    PAT, or this container's identity token against its registered Workload
    Identity — are all good, without creating anything or incurring a metered
    credential issuance.

    In ``entra`` mode this is also the only cheap way to tell a token-fetch
    failure (the container has no usable identity) from a rejection (the platform
    has no matching registration): the first names the metadata endpoint, the
    second is an HTTP 401 from BeyondTrust.
    """
    _request("GET", "/session")
    base = (_cfg("wlc_api_base_url") or DEFAULT_API_URL).rstrip("/")
    return {"ok": True,
            "message": f"Connected to Workload Credentials at {base} (site {_cfg('wlc_site_id')})."}


# Confirmed against a live site: collections come back as {"data": [...]}. The other
# keys are kept as fallbacks rather than removed — this API is still pre-release and may
# rename things, so tolerating that costs nothing, whereas a wrong guess here returns an
# empty list rather than an error, which is silent.
_COLLECTION_KEYS = ("data", "secrets", "static", "items", "folders")


def _collection(payload) -> list:
    """The list inside a collection response, whatever it is keyed under."""
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in _COLLECTION_KEYS:
        val = payload.get(key)
        if isinstance(val, list):
            return val
    return []


def list_folders() -> list:
    return _collection(_request("GET", "/folders"))


def list_static(folder: str = "") -> list:
    return _collection(_request("GET", "/static", folder=folder))


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
    # Log the request, never the result. Reading any field back out of `result`
    # puts a credential-bearing object on a wide-audience sink one edit away from
    # leaking, and a lease id is a correlation handle to a LIVE credential. The
    # lease id belongs in the lease row and in Workload Credentials' own audit
    # log; what an operator needs here is the issuance count, which the folder
    # and name give them.
    logger.info("WC: generated a credential from dynamic secret %s/%s",
                folder or "(root)", name)
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
            # Same reasoning as generate(): a lease id identifies a live
            # credential, so it stays out of the application log.
            logger.debug("WC: lease is not revocable (expected for AWS)")
            return
        raise
