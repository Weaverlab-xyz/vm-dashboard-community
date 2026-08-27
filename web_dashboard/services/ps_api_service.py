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


# A per-call override of the Password Safe credentials, for callers that must not use the
# install's singletons. Keyed by the CONFIG names so a caller building one reads the same
# words it would have set in Settings — the same shape
# ``terraform_pra_service.TENANT_KEYS`` uses for PRA.
#
# It exists for the POV feature, where "which Password Safe tenant?" has as many answers as
# there are customers and the singleton is the wrong one by default.
TENANT_KEYS = ("pscli_api_url", "pscli_client_id", "pscli_client_secret")


def tenant_creds(api_url: str, client_id: str, client_secret: str) -> dict:
    """The override dict the functions below accept. A convenience, and one spelling."""
    return {"pscli_api_url": api_url, "pscli_client_id": client_id,
            "pscli_client_secret": client_secret}


def _tcfg(key: str, tenant=None) -> str:
    """One config read, from the tenant override when there is one.

    A **partial** override is refused rather than merged with the singleton: reading one
    customer's URL with another's client id is the silent cross-tenant mistake the whole
    registry exists to prevent, and it would present as an authentication error nobody can
    place.
    """
    if tenant:
        if not all(str(tenant.get(k) or "").strip() for k in TENANT_KEYS):
            raise PSApiError(
                "a Password Safe tenant override was supplied with only part of its "
                "credentials (URL, client id and client secret are all required). "
                "Refusing rather than falling back to the configured tenant.")
        return str(tenant.get(key) or "").strip()
    return _cfg(key)


def _base_url(tenant=None) -> str:
    """Normalize pscli_api_url to the public-API base. ps-cli configs store
    either the bare host or the full /BeyondTrust/api/public/v3 path — accept both."""
    host = _tcfg("pscli_api_url", tenant).rstrip("/")
    if not host:
        raise PSApiError("pscli_api_url is not configured")
    if not host.lower().startswith("http"):
        host = f"https://{host}"
    if "/beyondtrust/api/public/" not in host.lower():
        host = f"{host}/BeyondTrust/api/public/v3"
    return host


def _client(tenant=None) -> httpx.AsyncClient:
    # Trailing slash so relative paths join under .../public/v3/.
    return httpx.AsyncClient(
        base_url=f"{_base_url(tenant)}/",
        headers={"Accept": "application/json"},
        timeout=30.0,
    )


async def _sign_in(client: httpx.AsyncClient, tenant=None) -> None:
    """OAuth2 client credentials → Bearer token, then SignAppIn to establish
    the API session (cookie retained by the client)."""
    token_resp = await client.post(
        "Auth/Connect/Token",
        data={
            "grant_type": "client_credentials",
            "client_id": _tcfg("pscli_client_id", tenant),
            "client_secret": _tcfg("pscli_client_secret", tenant),
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


async def get_functional_account(name: str, tenant=None) -> dict:
    """Resolve an EXISTING functional account by name →
    ``{id, platform_id, platform_name, account_name}``.

    The VM Password-Safe registration onboards a managed system against an
    operator-configured functional account (per cloud); the provider has no
    functional-account data source, so we read it over REST. The functional
    account's ``PlatformID`` also drives the managed system's ``platform_id``
    (and thus the management method); ``platform_name`` lets callers sanity-check it
    (e.g. SSM onboarding requires an "AWS Systems Manager" platform — guarding against
    a functional account from a different platform being configured by mistake).

    ``account_name`` is the account's own name in Password Safe, which for the GKE
    functional account IS the service-account email — i.e. the exact subject the
    Kubernetes rotator ClusterRoleBinding needs. Returning it here avoids a config key
    whose only job would be to repeat the functional account's name."""
    target = (name or "").strip()
    if not target:
        raise PSApiError("functional account name is empty")
    async with _client(tenant) as client:
        await _sign_in(client, tenant)
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
                            "platform_name": await _platform_name(client, int(pid)),
                            "account_name": acct}
            raise PSApiError(f"functional account {target!r} not found in Password Safe")
        finally:
            await _sign_out(client)


async def get_workgroup_id(name_or_id: str, tenant=None) -> str:
    """Resolve a workgroup name → id (string). A numeric value is passed through
    unchanged (the managed_system_by_workgroup resource takes workgroup_id as a
    string)."""
    val = (name_or_id or "").strip()
    if not val:
        raise PSApiError("workgroup is not configured")
    if val.isdigit():
        return val
    async with _client(tenant) as client:
        await _sign_in(client, tenant)
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
                # The 400 body here is NOT an error string like every other body in this
                # module — it is the rotation plugin's own multi-line attempt log, and the
                # only place the real reason is ever stated (a denied API-server call, a
                # missing RBAC verb, a subject that matches nothing). The usual 400-char
                # bound cuts it off inside the preamble every single time: the header,
                # cluster, token mode and API-server URL alone spend ~380 characters, so
                # what survives is the part nobody needs. Cost of getting this wrong,
                # measured live: an EKS rotation failed on a missing access entry and the
                # message stopped at "Functional account ma", sending the operator to the
                # Password Safe console to read what the dashboard already had in hand.
                raise PSApiError(
                    f"POST ManagedAccounts/{account_id}/Credentials/Change failed "
                    f"({resp.status_code}): {resp.text[:4000]}")
        finally:
            await _sign_out(client)


# ── managed-account reads + synced accounts ───────────────────────────────────
#
# change_managed_account_password above asks Password Safe to MINT a credential.
# What follows reads an account's change state without touching the credential,
# and links one account to another so Password Safe keeps their credentials
# identical for us.
#
# The dashboard does NOT copy the Kubernetes ServiceAccount token into the PRA
# Vault account. Password Safe does, natively: SyncedAccounts makes the PRA Vault
# account a subscriber of the token account, and "the managed account and all of
# its subscribers always share an identical password". An earlier version of this
# feature polled LastChangeDate and pushed the value itself; that was rebuilding a
# primitive the product already has. See services/ps_k8s_token_service.py.
#
# One consequence worth keeping in mind: because a change to EITHER account
# re-rotates the pair, nothing here may ask Password Safe to rotate on release.
# _checkin below is the enforcement point.

# Which by-id read shape this tenant supports, remembered after the first probe.
# A per-process cache under `gunicorn -w 2` is fine here — it is a hint, not state:
# each process discovers the same answer independently and a wrong guess self-corrects.
_ACCOUNT_READ_SHAPE = ""  # "" unknown | "by_id" | "scan"


def _account_state(item: dict) -> dict:
    """One ManagedAccounts row → the fields callers need, key-shape tolerant.

    ``last_change_date`` is kept as the VERBATIM string. Callers compare it as an
    opaque value and must not parse it: tenants return both ``…123Z`` and
    ``…+00:00``, and a parse that fails either never fires (nothing ever syncs) or
    fires every pass (a checkout and a PRA write every interval, forever)."""
    return {
        "account_id": _row_key(item, ("ManagedAccountID", "ManagedAccountId",
                                      "AccountId", "AccountID", "ID")),
        "account_name": str(item.get("AccountName") or "").strip(),
        "system_id": _row_key(item, ("ManagedSystemID", "ManagedSystemId",
                                     "SystemId", "SystemID")),
        "platform_id": _row_key(item, ("PlatformID", "PlatformId")),
        "last_change_date": str(item.get("LastChangeDate")
                                or item.get("lastChangeDate") or ""),
        "next_change_date": str(item.get("NextChangeDate")
                                or item.get("nextChangeDate") or ""),
    }


async def _managed_account(client: httpx.AsyncClient, account_id: int) -> dict:
    """One managed account's state, or ``{}`` when the account does not exist.

    Two read shapes exist across versions: ``GET ManagedAccounts/{id}`` on some
    builds, only the collection on others (where ManagedAccounts is request-scoped).
    Probe by id, fall back to filtering the paged collection — the read this repo has
    already proven against a live tenant — and remember which worked.

    ``{}`` means genuinely absent: by-id said no AND the account is not in the
    collection. A caller must not treat that as a transport failure, and must not
    treat a transport failure as absence."""
    global _ACCOUNT_READ_SHAPE
    aid = int(account_id)

    if _ACCOUNT_READ_SHAPE != "scan":
        resp = await client.get(f"ManagedAccounts/{aid}")
        if resp.status_code == 200:
            _ACCOUNT_READ_SHAPE = "by_id"
            body = resp.json()
            return _account_state(body) if isinstance(body, dict) else {}
        if resp.status_code not in (400, 404, 405):
            raise PSApiError(
                f"GET ManagedAccounts/{aid} failed ({resp.status_code}): {resp.text[:400]}")
        # 400/404/405 is either "no such account" or "no such route" — the scan below
        # distinguishes them, and only a scan miss is real absence.
        _ACCOUNT_READ_SHAPE = "scan"

    rows = await _get_paged(
        client, "ManagedAccounts",
        id_keys=("ManagedAccountID", "ManagedAccountId", "AccountId", "AccountID"))
    for item in rows:
        state = _account_state(item)
        if state["account_id"] and int(state["account_id"]) == aid:
            return state
    return {}


async def _system_id_for(client: httpx.AsyncClient, account_id: int) -> int:
    """The managed SYSTEM that owns this account, or 0 when it cannot be read.

    Best-effort on purpose: this only fills in a field, so a read that fails must not
    turn a checkout that would have worked into an error. A 0 reaches the request body
    and the refusal below says the id was unresolved."""
    try:
        return int((await _managed_account(client, int(account_id))).get("system_id") or 0)
    except Exception as exc:  # noqa: BLE001
        logger.debug("could not resolve the managed system for an account: %s", exc)
        return 0


async def _checkout(client: httpx.AsyncClient, account_id: int, *,
                    duration_min: int, reason: str, system_id: int = 0) -> tuple:
    """``(request_id, credential)`` for one managed account.

    ``SystemID`` is a REQUIRED field on ``POST Requests`` and Password Safe authorises
    the *pair*: the account has to be requestable **on that system**. This used to send
    a hard-coded 0, which no managed system owns, so every call 403'd with 4031 — the
    same code an ungranted Requestor role returns, which is why the tenant-side grant
    looked like the cause and fixing it changed nothing. ``btapi_service`` never had the
    bug: the ps-cli path passes ``-s-id``.

    Callers that already hold the id (the k8s registration stores it) pass it in;
    otherwise it is read from the account, which costs one round trip on a path that
    runs once per tunnel provision.

    ``ConflictOption=reuse`` returns an existing active request instead of a 409 —
    the same reason btapi_service passes ``-c-op reuse``, where a 409 body was once
    mis-parsed as a request id. It is also what keeps two passes racing the same
    cluster from failing each other.

    The refusal carries Password Safe's own body because the numeric code in it is the
    only thing that separates the causes: 4031 is the tenant-side grant (or a
    system/account pair that does not match), 4034 an unapproved request, 4035 the
    concurrent-request cap. Naming one of them unconditionally, as this did, turns a
    two-minute fix into an afternoon spent re-granting a role that was never missing."""
    try:
        sid = int(system_id or 0)
    except (TypeError, ValueError):
        sid = 0
    if not sid:
        sid = await _system_id_for(client, account_id)
    resp = await client.post("Requests", json={
        "AccessType": "View", "SystemID": sid, "AccountID": int(account_id),
        "DurationMinutes": int(duration_min), "Reason": reason,
        "ConflictOption": "reuse"})
    if resp.status_code not in (200, 201):
        raise PSApiError(
            f"Password Safe refused the credential request for account {int(account_id)} on "
            f"managed system {sid or 'UNRESOLVED (the account could not be read)'} "
            f"({resp.status_code}): {resp.text[:400]} — the code in that body says which "
            f"cause it is. 4031: the API identity needs the Requestor role and an access "
            f"policy granting View on a Smart Rule containing this account (there is no "
            f"Smart Rule API — an out-of-band prerequisite, see "
            f"docs/integrations/password-safe.md), OR the account is not API-enabled, OR it "
            f"is not requestable on that system. 4034: awaiting approval. 4035: the "
            f"account's concurrent-request cap.")
    body = resp.json()
    request_id = body if isinstance(body, int) else (
        body.get("RequestID") or body.get("RequestId") or body.get("id"))
    if not request_id:
        raise PSApiError("Password Safe returned no request id for the credential request")

    got = await client.get(f"Credentials/{int(request_id)}")
    if got.status_code != 200:
        # Check the request back in before giving up. It was created a line ago and is
        # unusable, but leaving it open held the account's concurrent-request slot for the
        # whole DurationMinutes — so a pending-approval account would trip 4035 on the
        # next attempt and report a cap problem instead of the approval it was waiting on.
        await _checkin(client, int(request_id))
        raise PSApiError(
            f"Password Safe would not release the credential ({got.status_code}) — the "
            f"request may be awaiting approval, or the access policy may not auto-release.")
    credential = got.json()
    if isinstance(credential, dict):
        credential = credential.get("Credentials") or credential.get("Password") or ""
    return int(request_id), str(credential)


async def _checkin(client: httpx.AsyncClient, request_id: int,
                   reason: str = "k8s ServiceAccount token read complete") -> None:
    """Release the request. Best-effort — it expires on its own duration anyway.

    Plain ``Checkin`` ONLY — never the rotate-on-release variant that
    btapi_service.rotate_ps_request_on_checkin flags. Under synced accounts this matters
    more than it used to, not less: a credential change on EITHER member of a synced pair
    re-rotates both, so rotate-on-release here would rotate the real cluster token every
    time the tunnel reads it, with a dead-credential window each time. A static test
    asserts that endpoint is named nowhere in this module."""
    try:
        await client.put(f"Requests/{int(request_id)}/Checkin",
                         json={"Reason": reason})
    except Exception:  # noqa: BLE001 — never log the request id (CodeQL taints it)
        logger.debug("Password Safe credential check-in was refused")


def _platform_matches(actual: str, expected: str) -> bool:
    """Every word of the configured platform name appears in the tenant's.

    Same tolerance as ps_vm_hook._platform_name_ok and for the same reason — a
    Password Safe admin can rename an imported plugin platform, and "Azure VM SSH
    Rotation" once became "Azure Waagent VM SSH Rotation" overnight and silently
    switched onboarding off. Duplicated rather than imported because ps_vm_hook
    imports this module."""
    have = (actual or "").strip().lower()
    want = [w for w in (expected or "").strip().lower().split() if w]
    return bool(have) and bool(want) and all(w in have for w in want)


def _looks_like_sa_token(value: str) -> bool:
    """A Kubernetes ServiceAccount token is a JWT in both LongLived and Bound mode:
    three dot-separated segments, no whitespace.

    This guards the one failure that would put a secret-shaped non-secret into PRA's
    vault: Password Safe can return a soft-failure STRING in the credential position
    ("It was not possible to get a credential for Request ID: 5" — the case
    btapi_service already learned to catch), and provisioning the tunnel with that
    breaks the tunnel while reporting success."""
    return (bool(value) and len(value) >= 40 and value.count(".") == 2
            and not any(c.isspace() for c in value))


async def get_managed_account_states(account_ids: list) -> dict:
    """``{account_id: state}`` for several accounts in ONE signed-in session.

    A credential-free read: it reports LastChangeDate and existence without a request or
    a checkout. Per-account helpers each own a session (Token + SignAppIn + Signout),
    which over N accounts would be 3N round trips — hence the inventory-read shape.

    An account that does not exist maps to ``{}``; the whole call raises on a
    transport or auth failure, because "Password Safe is down" must not look like
    "every account was deleted"."""
    wanted = [int(a) for a in account_ids if str(a or "").strip()]
    if not wanted:
        return {}
    out: dict = {}
    async with _list_client() as client:
        await _sign_in(client)
        try:
            for aid in wanted:
                out[str(aid)] = await _managed_account(client, aid)
        finally:
            await _sign_out(client)
    return out


async def checkout_credential(account_id: int, *, duration_min: int = 15,
                              reason: str = "", system_id: int = 0) -> str:
    """Check one managed account's credential out and RETURN it.

    The only function here that hands a plaintext credential back, and it exists for one
    reason: the PRA Vault token account is provisioned by Terraform, so the tunnel
    registration has to hold the value long enough to pass it as a sensitive ``TF_VAR``.
    Nothing else should call it. Keeping the pair in step afterwards needs no checkout at
    all — that is Password Safe's job now (``link_synced_account``).

    ``system_id`` is the managed system that owns the account — required by the request
    API (see ``_checkout``). Pass it when the caller already knows it; it is read from
    the account when omitted.

    Callers must not log it, must not put it in a job result, and must not let it reach
    Terraform state unscrubbed (terraform_pra_service._scrub_tf_state redacts ``token``
    fail-closed, which is what makes the tunnel path safe)."""
    async with _client() as client:
        await _sign_in(client)
        try:
            request_id, credential = await _checkout(
                client, int(account_id), duration_min=duration_min, system_id=system_id,
                reason=reason or "k8s ServiceAccount token read for PRA Vault")
            try:
                if not _looks_like_sa_token(credential):
                    raise PSApiError(
                        f"Password Safe returned a value that is not a ServiceAccount token "
                        f"({len(credential)} chars) — check that account {account_id} is "
                        f"managed by the 'Kubernetes Service Account Token' plugin.")
                return credential
            finally:
                await _checkin(client, request_id)
        finally:
            await _sign_out(client)


_SYNC_GRANT_HINT = (
    "The API identity needs Password Safe Account Management (Full control). If it already "
    "has that, try Role Management (Read/Write) — `ps-cli synced-accounts` documents the "
    "grant that way, which looks like an error in its help but has not been ruled out.")


async def _synced_accounts(client: httpx.AsyncClient, parent_id: int) -> list:
    """``GET ManagedAccounts/{id}/SyncedAccounts`` → the subscriber rows.

    Read-only (needs only *Read*), so this is the cheap way to confirm a link without
    touching a credential."""
    resp = await client.get(f"ManagedAccounts/{int(parent_id)}/SyncedAccounts")
    if resp.status_code == 404:
        return []
    if resp.status_code != 200:
        raise PSApiError(
            f"GET ManagedAccounts/{parent_id}/SyncedAccounts failed "
            f"({resp.status_code}): {resp.text[:400]}")
    body = resp.json()
    return [_account_state(item) for item in body if isinstance(item, dict)] \
        if isinstance(body, list) else []


async def link_synced_account(*, parent_account_id: int, synced_account_id: int,
                              expect_subscriber_platform: str = "") -> dict:
    """Make ``synced`` a subscriber of ``parent``, so Password Safe keeps them identical.

    ``POST ManagedAccounts/{id}/SyncedAccounts/{syncedAccountID}``. From then on Password
    Safe owns the sync — a credential change on the parent propagates to every
    subscriber — which is what replaces the LastChangeDate poll-and-push this module used
    to do.

    **Direction is the one thing to get right, and the API will not catch it.** Both path
    segments are plain managed-account ids, so a swapped pair links happily and then syncs
    backwards, pushing the PRA Vault account's value onto the cluster's token account.
    ``{id}`` is the PARENT ("cannot be a Synced Account"); ``{syncedAccountID}`` is the
    subordinate ("cannot be a parent Managed Account"). ps-cli names the same two
    ``-ma-id`` and ``-sa-id``.

    ``expect_subscriber_platform`` fails the call CLOSED when the subordinate is not on
    that platform — linking a Kubernetes bearer token to an account managed by some other
    plugin is the one failure here that puts a secret somewhere it does not belong, so an
    ambiguous target is refused rather than guessed at.

    Returns ``{"linked": True, "confirmed": bool}``. The confirm is a re-read of the
    subscriber list in the same session: a 200 on a POST that did not actually take is
    the one silent failure worth one extra round trip."""
    parent = int(parent_account_id)
    sub = int(synced_account_id)
    if parent == sub:
        raise PSApiError(
            f"refusing to sync managed account {parent} to itself — the parent and the "
            f"subscriber must be different accounts")

    async with _client() as client:
        await _sign_in(client)
        try:
            if expect_subscriber_platform:
                target = await _managed_account(client, sub)
                if not target:
                    raise PSApiError(
                        f"Password Safe managed account {sub} (the PRA Vault Token account) "
                        f"does not exist — re-register the cluster or clear the binding.")
                pname = await _platform_name(client, int(target["platform_id"] or 0))
                if not _platform_matches(pname, expect_subscriber_platform):
                    raise PSApiError(
                        f"managed account {sub} is on platform {pname or '(unknown)'!r}, not a "
                        f"{expect_subscriber_platform!r} platform — refusing to sync a "
                        f"Kubernetes ServiceAccount token to it.")

            resp = await client.post(
                f"ManagedAccounts/{parent}/SyncedAccounts/{sub}")
            if resp.status_code not in (200, 201, 204):
                raise PSApiError(
                    f"POST ManagedAccounts/{parent}/SyncedAccounts/{sub} failed "
                    f"({resp.status_code}): {resp.text[:400]}. {_SYNC_GRANT_HINT}")

            confirmed = any(
                str(row.get("account_id") or "") == str(sub)
                for row in await _synced_accounts(client, parent))
        finally:
            await _sign_out(client)
    return {"linked": True, "confirmed": confirmed}


async def unlink_synced_account(*, parent_account_id: int, synced_account_id: int) -> bool:
    """``DELETE ManagedAccounts/{id}/SyncedAccounts/{syncedAccountID}``.

    Tolerant of 404: deregistration runs this before off-boarding either account, and a
    link that is already gone is the desired end state, not an error."""
    parent = int(parent_account_id)
    sub = int(synced_account_id)
    async with _client() as client:
        await _sign_in(client)
        try:
            resp = await client.delete(
                f"ManagedAccounts/{parent}/SyncedAccounts/{sub}")
            if resp.status_code == 404:
                return False
            if resp.status_code not in (200, 201, 204):
                raise PSApiError(
                    f"DELETE ManagedAccounts/{parent}/SyncedAccounts/{sub} failed "
                    f"({resp.status_code}): {resp.text[:400]}. {_SYNC_GRANT_HINT}")
            return True
        finally:
            await _sign_out(client)


async def synced_account_status(*, parent_account_id: int,
                                synced_account_id: int) -> dict:
    """Whether the pair is still linked, plus both change dates — one signed-in session.

    This is what the operator-facing status reads, deliberately live rather than cached:
    an admin can unlink in the Password Safe console at any time, and a dashboard that
    kept asserting "linked" from its own registration record would be reporting a claim
    about the past. Change dates are returned VERBATIM (see ``_account_state``) and are
    for display only — nothing branches on them any more."""
    parent = int(parent_account_id)
    sub = int(synced_account_id)
    async with _client() as client:
        await _sign_in(client)
        try:
            src = await _managed_account(client, parent)
            rows = await _synced_accounts(client, parent)
            match = next((r for r in rows
                          if str(r.get("account_id") or "") == str(sub)), {})
            return {
                "linked": bool(match),
                "parent_exists": bool(src),
                "parent_last_change": src.get("last_change_date") or "",
                "parent_next_change": src.get("next_change_date") or "",
                "subscriber_last_change": match.get("last_change_date") or "",
                "subscriber_count": len(rows),
            }
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
