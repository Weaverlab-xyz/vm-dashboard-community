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


# ── inventory reads (paged) ───────────────────────────────────────────────────
#
# Everything above this line fetches one object, so it opens a client, signs in,
# does one call and signs out. An inventory read is the opposite shape: several
# collections, each of them paged, and repeating that dance per collection would
# be four Token + SignAppIn + Signout round trips for one modal open. So the
# helpers below take an already-signed-in client and the public entry point owns
# the session.

_PAGE_SIZE = 500
# 20 000 rows. A ceiling, not a target — it bounds a tenant that pages forever.
_MAX_PAGES = 40
# Inventory reads dwarf the single-object writes _client()'s 30s was sized for.
_LIST_TIMEOUT_S = 60.0


def _list_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=f"{_base_url()}/",
        headers={"Accept": "application/json"},
        timeout=_LIST_TIMEOUT_S,
    )


def _page_items(resp: httpx.Response) -> list:
    """Rows out of a list response, accepting both shapes Password Safe returns:
    a bare JSON array, and the ``{"TotalCount": n, "Data": [...]}`` envelope."""
    try:
        body = resp.json()
    except ValueError:
        return []
    if isinstance(body, dict):
        body = body.get("Data") or body.get("data") or []
    return body if isinstance(body, list) else []


def _row_key(item: dict, id_keys):
    for key in id_keys:
        val = item.get(key)
        if val not in (None, ""):
            return str(val)
    return None


async def _get_all(client: httpx.AsyncClient, path: str) -> list:
    """One GET for a collection Password Safe does not page (Platforms, Workgroups)."""
    resp = await client.get(path)
    if resp.status_code != 200:
        raise PSApiError(f"GET {path} failed ({resp.status_code}): {resp.text[:400]}")
    return _page_items(resp)


async def _get_paged(client: httpx.AsyncClient, path: str, *, id_keys,
                     params: dict = None, max_items: int = 20000) -> list:
    """Walk a paged collection with ``limit``/``offset``.

    Three stop conditions, and all three are needed:

    1. a short page — the normal end;
    2. ``max_items`` — the caller's cap;
    3. **a page that contributes no new id.** Some deployments (and some proxies
       in front of them) ignore ``offset`` and re-serve page one. Without this the
       loop runs ``_MAX_PAGES`` times and hands back forty copies of the same rows,
       which downstream reads as a tenant with forty identical databases. Rows are
       appended only when their id is new, so even the page that trips this
       contributes nothing.

    ``_MAX_PAGES`` bounds the walk regardless — including the case where no row
    carries a recognisable id and condition 3 therefore cannot fire.
    """
    out = []
    seen = set()
    for page in range(_MAX_PAGES):
        query = dict(params or {})
        query.update({"limit": _PAGE_SIZE, "offset": page * _PAGE_SIZE})
        resp = await client.get(path, params=query)
        if resp.status_code != 200:
            raise PSApiError(f"GET {path} failed ({resp.status_code}): {resp.text[:400]}")
        items = _page_items(resp)
        if not items:
            break

        added = keyed = 0
        for item in items:
            if not isinstance(item, dict):
                continue
            key = _row_key(item, id_keys)
            if key is not None:
                keyed += 1
                if key in seen:
                    continue
                seen.add(key)
            out.append(item)
            added += 1
            if len(out) >= max_items:
                return out[:max_items]

        if len(items) < _PAGE_SIZE:
            break
        if keyed and not added:
            logger.warning(
                "Password Safe %s returned no new rows at offset %d — the tenant "
                "appears to ignore offset; stopping after %d rows.",
                path, page * _PAGE_SIZE, len(out))
            break
    return out


async def _workgroup_id(client: httpx.AsyncClient, name_or_id: str) -> str:
    """In-session half of :func:`get_workgroup_id`, so a caller that already holds
    a signed-in client does not need a second session."""
    val = (name_or_id or "").strip()
    if not val:
        raise PSApiError("workgroup is not configured")
    if val.isdigit():
        return val
    for wg in await _get_all(client, "Workgroups"):
        if str(wg.get("Name") or "").strip().lower() == val.lower():
            wid = wg.get("ID") or wg.get("Id") or wg.get("OrganizationID")
            if wid is not None:
                return str(wid)
    raise PSApiError(f"workgroup {val!r} not found in Password Safe")


async def read_database_inventory(*, workgroup: str = "") -> dict:
    """Every collection the database import needs, in ONE signed-in pass.

    Returns the rows **raw** — ``{"platforms", "systems", "databases", "accounts",
    "workgroup_id", "warnings"}``. Shaping, filtering and sanitising belong to
    ``ps_database_catalog``, which is pure, so the I/O half and the logic half stay
    testable apart.

    Accounts come from the flat ``ManagedAccounts`` collection rather than
    per-system ``ManagedSystems/{id}/ManagedAccounts``. That collapses N calls into
    one paged call, and — the reason that matters — it returns the accounts this
    API identity can actually *request*. That is the same permission surface the
    run-time checkout uses, so a missing Requestor role or Smart Rule shows up as a
    greyed-out row at import time instead of a 4031/403 hours later on the first
    playbook run.

    ``Databases`` and ``ManagedAccounts`` each degrade to a warning: losing the
    first costs port/instance enrichment, losing the second makes every row
    ineligible with an actionable reason. Losing ``ManagedSystems`` is fatal —
    there is nothing to show.
    """
    warnings = []
    async with _list_client() as client:
        await _sign_in(client)
        try:
            platforms = await _get_all(client, "Platforms")

            params = {}
            workgroup_id = None
            if (workgroup or "").strip():
                workgroup_id = await _workgroup_id(client, workgroup)
                # Sent as a hint, and applied again below. Whether v3 honours
                # workgroupID on this collection varies by version, and a filter
                # that silently does nothing is worse than one that costs
                # bandwidth — so correctness does not depend on the tenant.
                params["workgroupID"] = workgroup_id

            systems = await _get_paged(
                client, "ManagedSystems", params=params,
                id_keys=("ManagedSystemID", "SystemId", "SystemID"))
            if workgroup_id is not None:
                systems = [s for s in systems
                           if str(s.get("WorkgroupID") or s.get("WorkgroupId") or "")
                           == str(workgroup_id)]

            try:
                databases = await _get_paged(
                    client, "Databases",
                    id_keys=("DatabaseID", "DatabaseId", "ID"))
            except PSApiError as exc:
                logger.warning("Password Safe Databases read failed: %s", exc)
                databases = []
                warnings.append(
                    "Could not read the Databases list from Password Safe — ports "
                    "and instance names fall back to platform defaults.")

            try:
                accounts = await _get_paged(
                    client, "ManagedAccounts",
                    id_keys=("ManagedAccountID", "AccountId", "AccountID"))
            except PSApiError as exc:
                logger.warning("Password Safe ManagedAccounts read failed: %s", exc)
                accounts = []
                warnings.append(
                    "Could not read the requestable accounts from Password Safe, so "
                    "nothing can be imported. The API identity needs the Requestor "
                    "role and an access policy granting View on a Smart Rule "
                    "containing the database accounts.")

            return {"platforms": platforms, "systems": systems,
                    "databases": databases, "accounts": accounts,
                    "workgroup_id": workgroup_id, "warnings": warnings}
        finally:
            await _sign_out(client)


async def _platform_id_by_name(client: httpx.AsyncClient, platform_name: str) -> int:
    """Resolve a platform display name → PlatformID via GET Platforms. Used for
    both the built-in DB engines and the operator-installed custom plugins
    ("psql SSM Custom Plugin", "PRA Vault Username Password", …)."""
    resp = await client.get("Platforms")
    if resp.status_code != 200:
        raise PSApiError(f"GET Platforms failed ({resp.status_code}): {resp.text[:400]}")
    for p in resp.json():
        name = str(p.get("Name") or p.get("PlatformName") or "").strip()
        if name.lower() == platform_name.strip().lower():
            pid = p.get("PlatformID") or p.get("PlatformId") or p.get("ID")
            if pid is not None:
                return int(pid)
    raise PSApiError(f"platform {platform_name!r} not found in Password Safe")


async def _platform_id(client: httpx.AsyncClient, engine: str) -> int:
    platform_name = _PLATFORM_BY_ENGINE.get(engine)
    if not platform_name:
        raise PSApiError(f"no Password Safe platform mapping for engine {engine!r}")
    return await _platform_id_by_name(client, platform_name)


async def get_platform_id(name_or_id: str) -> int:
    """Resolve a Password Safe platform by display name → id (a numeric value is
    passed through). Public helper for the cloud-DB onboarding, which points at
    operator-installed custom-plugin platforms configured by name."""
    val = (name_or_id or "").strip()
    if not val:
        raise PSApiError("platform name is empty")
    if val.isdigit():
        return int(val)
    async with _client() as client:
        await _sign_in(client)
        try:
            return await _platform_id_by_name(client, val)
        finally:
            await _sign_out(client)


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
            return await _workgroup_id(client, val)
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


async def create_functional_account_on_platform(
    *, platform_id: int, account_name: str, display_name: str,
    password: str, description: str = "",
) -> int:
    """Create a functional account on an explicit platform id and return its id.

    Used by the cloud-DB Password Safe onboarding for the two custom-plugin
    functional accounts (the "{engine} SSM Custom Plugin" account whose username/
    password bundle the AWS + DB-login credentials, and the "PRA Vault Username
    Password" account holding the PRA OAuth client id/secret). The (platform,
    domain, account name, display name) tuple must be unique tenant-side — the
    caller carries per-database uniqueness in display_name."""
    async with _client() as client:
        await _sign_in(client)
        try:
            resp = await client.post("FunctionalAccounts", json={
                "PlatformID": int(platform_id),
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
