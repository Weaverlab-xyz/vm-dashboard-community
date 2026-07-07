"""
BeyondTrust Password Safe public-API client (REST, httpx).

Complements btapi_service (which wraps the ps-cli binary for secret/credential
RETRIEVAL): ps-cli has no functional-account commands, so the cloud-database
feature talks to the Password Safe public API directly for the few writes it
needs. Reuses the same OAuth client the ps-cli integration is configured with:

  pscli_api_url       - Password Safe URL, e.g. https://tenant.ps.beyondtrustcloud.com
  pscli_client_id     - OAuth2 client-credentials pair
  pscli_client_secret

Tenant prerequisite: the OAuth client's linked BeyondInsight user needs
Password Safe API access plus account-management (functional accounts)
permission — without it these calls return 401/403 and callers log a warning
(everything here is best-effort from the caller's perspective).
"""
import logging

import httpx

logger = logging.getLogger(__name__)


class PSApiError(Exception):
    """Raised when a Password Safe API call fails."""


# Password Safe platform name per dashboard engine. mysql / sqlserver fan out
# with the other engines later.
_PLATFORM_BY_ENGINE = {
    "postgres": "PostgreSQL",
    "mysql": "MySQL",
    "sqlserver": "SQL Server",
}


def _cfg(key: str) -> str:
    try:
        from . import config_service
        val = config_service.get(key)
        if val:
            return val
    except Exception:
        pass
    from ..config import settings
    return getattr(settings, key, "") or ""


def configured() -> bool:
    return all(_cfg(k) for k in ("pscli_api_url", "pscli_client_id", "pscli_client_secret"))


def _base_url() -> str:
    """Normalize pscli_api_url to the public-API base. ps-cli configs store
    either the bare host or the full /BeyondTrust/api/public/v3 path — accept both."""
    host = _cfg("pscli_api_url").rstrip("/")
    if not host:
        raise PSApiError("pscli_api_url is not configured")
    if not host.lower().startswith("http"):
        host = f"https://{host}"
    if "/beyondtrust/api/public/" not in host.lower():
        host = f"{host}/BeyondTrust/api/public/v3"
    return host


def _client() -> httpx.AsyncClient:
    # Trailing slash so relative paths join under .../public/v3/.
    return httpx.AsyncClient(
        base_url=f"{_base_url()}/",
        headers={"Accept": "application/json"},
        timeout=30.0,
    )


async def _sign_in(client: httpx.AsyncClient) -> None:
    """OAuth2 client credentials → Bearer token, then SignAppIn to establish
    the API session (cookie retained by the client)."""
    token_resp = await client.post(
        "Auth/Connect/Token",
        data={
            "grant_type": "client_credentials",
            "client_id": _cfg("pscli_client_id"),
            "client_secret": _cfg("pscli_client_secret"),
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if token_resp.status_code != 200:
        raise PSApiError(
            f"OAuth token request failed ({token_resp.status_code}): {token_resp.text[:400]}")
    token = token_resp.json().get("access_token", "")
    if not token:
        raise PSApiError("OAuth token response contained no access_token")
    client.headers["Authorization"] = f"Bearer {token}"
    sign = await client.post("Auth/SignAppIn")
    if sign.status_code not in (200, 201):
        raise PSApiError(f"SignAppIn failed ({sign.status_code}): {sign.text[:400]}")


async def _sign_out(client: httpx.AsyncClient) -> None:
    try:
        await client.post("Auth/Signout")
    except Exception:  # best-effort — session expires on its own
        pass


async def _platform_id(client: httpx.AsyncClient, engine: str) -> int:
    platform_name = _PLATFORM_BY_ENGINE.get(engine)
    if not platform_name:
        raise PSApiError(f"no Password Safe platform mapping for engine {engine!r}")
    resp = await client.get("Platforms")
    if resp.status_code != 200:
        raise PSApiError(f"GET Platforms failed ({resp.status_code}): {resp.text[:400]}")
    for p in resp.json():
        name = str(p.get("Name") or p.get("PlatformName") or "").strip()
        if name.lower() == platform_name.lower():
            pid = p.get("PlatformID") or p.get("PlatformId") or p.get("ID")
            if pid is not None:
                return int(pid)
    raise PSApiError(f"platform {platform_name!r} not found in Password Safe")


async def _platform_name(client: httpx.AsyncClient, platform_id: int) -> str:
    """Reverse of _platform_id: PlatformID → display name. Best-effort — returns ""
    on any failure so a sanity-check lookup never blocks onboarding."""
    try:
        resp = await client.get("Platforms")
        if resp.status_code == 200:
            for p in resp.json():
                pid = p.get("PlatformID") or p.get("PlatformId") or p.get("ID")
                if pid is not None and int(pid) == int(platform_id):
                    return str(p.get("Name") or p.get("PlatformName") or "").strip()
    except Exception:  # noqa: BLE001
        pass
    return ""


async def get_functional_account(name: str) -> dict:
    """Resolve an EXISTING functional account by name → ``{id, platform_id, platform_name}``.

    The VM Password-Safe registration onboards a managed system against an
    operator-configured functional account (per cloud); the provider has no
    functional-account data source, so we read it over REST. The functional
    account's ``PlatformID`` also drives the managed system's ``platform_id``
    (and thus the management method); ``platform_name`` lets callers sanity-check it
    (e.g. SSM onboarding requires an "AWS Systems Manager" platform — guarding against
    a functional account from a different platform being configured by mistake)."""
    target = (name or "").strip()
    if not target:
        raise PSApiError("functional account name is empty")
    async with _client() as client:
        await _sign_in(client)
        try:
            resp = await client.get("FunctionalAccounts")
            if resp.status_code != 200:
                raise PSApiError(
                    f"GET FunctionalAccounts failed ({resp.status_code}): {resp.text[:400]}")
            # Accept a name or a numeric id; match AccountName (case-insensitive).
            for fa in resp.json():
                fa_id = fa.get("FunctionalAccountID") or fa.get("ID") or fa.get("Id")
                acct = str(fa.get("AccountName") or "").strip()
                if acct.lower() == target.lower() or str(fa_id) == target:
                    pid = fa.get("PlatformID") or fa.get("PlatformId")
                    if fa_id is None or pid is None:
                        break
                    return {"id": int(fa_id), "platform_id": int(pid),
                            "platform_name": await _platform_name(client, int(pid))}
            raise PSApiError(f"functional account {target!r} not found in Password Safe")
        finally:
            await _sign_out(client)


async def get_workgroup_id(name_or_id: str) -> str:
    """Resolve a workgroup name → id (string). A numeric value is passed through
    unchanged (the managed_system_by_workgroup resource takes workgroup_id as a
    string)."""
    val = (name_or_id or "").strip()
    if not val:
        raise PSApiError("workgroup is not configured")
    if val.isdigit():
        return val
    async with _client() as client:
        await _sign_in(client)
        try:
            resp = await client.get("Workgroups")
            if resp.status_code != 200:
                raise PSApiError(f"GET Workgroups failed ({resp.status_code}): {resp.text[:400]}")
            for wg in resp.json():
                if str(wg.get("Name") or "").strip().lower() == val.lower():
                    wid = wg.get("ID") or wg.get("Id") or wg.get("OrganizationID")
                    if wid is not None:
                        return str(wid)
            raise PSApiError(f"workgroup {val!r} not found in Password Safe")
        finally:
            await _sign_out(client)


async def change_managed_account_password(account_id: int) -> None:
    """Queue an immediate Password Safe credential change ("Change Password") for a
    managed account.

    Used right after SSM (AWS Systems Manager custom plugin) onboarding to mint the
    first SSH key over SSM — the plugin cannot set the initial private key at creation,
    so the key only materialises on a credential change. Auto-management would rotate it
    on schedule anyway, so the caller treats failure here as non-fatal.

    Endpoint: ``POST ManagedAccounts/{id}/Credentials/Change`` (public API v3, present
    across 21.x–24.x). The body is optional; ``Queue=false`` asks for an immediate change
    rather than queueing behind other pending change operations.
    Verify the exact shape against the tenant's API version during live testing."""
    async with _client() as client:
        await _sign_in(client)
        try:
            resp = await client.post(
                f"ManagedAccounts/{int(account_id)}/Credentials/Change",
                json={"Queue": False},
            )
            if resp.status_code not in (200, 201, 202, 204):
                raise PSApiError(
                    f"POST ManagedAccounts/{account_id}/Credentials/Change failed "
                    f"({resp.status_code}): {resp.text[:400]}")
        finally:
            await _sign_out(client)


async def create_functional_account(
    *, engine: str, account_name: str, display_name: str,
    password: str, description: str = "",
) -> int:
    """Create a Password Safe functional account and return its id.

    The (platform, domain, account name, display name) tuple must be unique
    tenant-side — display_name carries the per-database uniqueness here, since
    account_name is typically the same master username (e.g. ``dbadmin``)
    across dashboard-provisioned databases.
    """
    async with _client() as client:
        await _sign_in(client)
        try:
            pid = await _platform_id(client, engine)
            resp = await client.post("FunctionalAccounts", json={
                "PlatformID": pid,
                "AccountName": account_name,
                "DisplayName": display_name,
                "Password": password,
                "Description": description[:1000],
            })
            if resp.status_code not in (200, 201):
                raise PSApiError(
                    f"POST FunctionalAccounts failed ({resp.status_code}): {resp.text[:400]}")
            body = resp.json()
            fa_id = body.get("FunctionalAccountID") or body.get("ID") or body.get("Id")
            if fa_id is None:
                raise PSApiError(f"FunctionalAccounts response had no id: {str(body)[:400]}")
            return int(fa_id)
        finally:
            await _sign_out(client)


async def delete_functional_account(account_id: int) -> None:
    """Delete a functional account. 404 means it is already gone — fine.
    A 400/409 usually means a managed system still references it (the future
    Ansible-onboarded managed system must be off-boarded first)."""
    async with _client() as client:
        await _sign_in(client)
        try:
            resp = await client.delete(f"FunctionalAccounts/{int(account_id)}")
            if resp.status_code == 404:
                logger.info("Password Safe functional account %s already gone", account_id)
                return
            if resp.status_code not in (200, 204):
                raise PSApiError(
                    f"DELETE FunctionalAccounts/{account_id} failed "
                    f"({resp.status_code}): {resp.text[:400]}")
        finally:
            await _sign_out(client)
