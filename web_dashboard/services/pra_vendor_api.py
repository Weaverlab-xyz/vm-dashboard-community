"""The PRA Configuration API objects a POV needs to let a third party in.

``pra_tenant_api`` owns the tenant-scoped handshake and the Gateway read. This module is
its second half: the four object types PRA Vendor Onboarding is built out of — Jump Group,
Jump Item Role, Group Policy, Vendor Group (and its users) — spoken to a
``bt_tenant_service.Tenant`` rather than to the install's singletons.

**One token function, still.** ``get_token`` is imported from ``pra_tenant_api`` rather
than written again, for the reason that module's docstring gives: a handshake that drifts
from the one the real work makes turns a green Verify into a lie.

**Why REST and not Terraform.** The ``beyondtrust/sra`` provider has an ``sra_jump_group``
resource but none for Group Policy or Vendor, so half of this lifecycle would have to be
REST anyway. Splitting four objects that are created, referenced and deleted in one
ordered chain across two toolchains is how an orphan happens, so all four are REST.

**Field names here are the appliance's, checked against its own spec** —
``openapi/bt-pra-configuration.openapi.yaml`` in BeyondTrust's ``terraform-provider-sra``
repository, which is the same document an appliance serves at
``/api/config/v1/openapi.yaml``. Each path constant below carries the schema name so the
next person can find it. Three things in that spec shape this module and are not guessable
from the product UI:

* ``POST /vendor/{id}/user`` answers **200 with no body**, so a created user's id has to be
  recovered by re-reading the group's user list. That list is paginated, which is why
  :func:`_paged` exists and why nothing here reads page 1 and stops.
* ``user_added_notification_enabled`` and ``user_expired_notification_enabled`` **default
  to true** and require at least one ``administrator_ids``/``team_ids`` entry, so a vendor
  group created with neither and the defaults left alone is a 422. They are always sent.
* The **Email Domain Allow List and the self-registration portal are not in the API**.
  They are ``/login`` settings. Nothing here can create one, and a caller that wants the
  portal link reads it off the tenant as a string somebody pasted.

**No retries**, matching every other PRA path in this codebase. And no caught exception's
text ever reaches a user-facing message — see ``bt_tenant_verify._http_reason`` for why.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from .pra_tenant_api import PRATenantError, get_token

logger = logging.getLogger(__name__)

# A config-API call made from a page somebody is waiting on, not from a provision.
_TIMEOUT_S = 20.0

# The Config API paths, with the OpenAPI schema each one carries.
_JUMP_GROUP_PATH = "/api/config/v1/jump-group"          # JumpGroup
_JUMP_ITEM_ROLE_PATH = "/api/config/v1/jump-item-role"  # JumpItemRole
_GROUP_POLICY_PATH = "/api/config/v1/group-policy"      # GroupPolicy
_VENDOR_PATH = "/api/config/v1/vendor"                  # Vendor / VendorUser

# The appliance's own ceilings, so a refusal names the limit rather than letting PRA
# answer 422 with a field path an SE has never seen.
NAME_MAX = 255            # JumpGroup.name, GroupPolicy.name, Vendor.name
CODE_NAME_MAX = 64        # CodeName, pattern ^[a-zA-Z0-9_\-]+$
USERNAME_MAX = 64         # VendorUser.username
EXPIRATION_MIN = 1        # Vendor.account_expiration
EXPIRATION_MAX = 365

# "User's Default" / "Set on Jump Items" in a GroupPolicyJumpGroup membership. Zero is a
# real value in that schema, not a missing one.
INHERIT = 0


# ── the wire ─────────────────────────────────────────────────────────────────

async def _request(tenant, method: str, path: str, *,
                   json: dict | None = None,
                   params: dict | None = None,
                   allow_404: bool = False) -> tuple[int, Any, httpx.Headers]:
    """One authenticated Config API call. Returns ``(status, body, headers)``.

    ``allow_404`` is for deletes, where "already gone" is success — the same tolerance
    ``pra_api_service.delete_protocol_tunnel_jump`` shows, and for the same reason: a
    teardown that fails because somebody tidied up by hand in the appliance leaves this
    dashboard holding an id forever.
    """
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S,
                                     headers={"Accept": "application/json"}) as client:
            token = await get_token(client, tenant)
            resp = await client.request(
                method, f"{tenant.api_base}{path}",
                json=json, params=params,
                headers={"Authorization": f"Bearer {token}"})
    except PRATenantError:
        raise
    except Exception as exc:  # noqa: BLE001
        # The exception is logged and never carried outward: its text can hold the URL,
        # and a transport error's str() is not a sentence anybody can act on.
        logger.warning("PRA %s %s for tenant %s failed", method, path, tenant.name,
                       exc_info=True)
        raise PRATenantError(
            f"could not reach PRA at {tenant.api_base} ({type(exc).__name__}) — check the "
            f"hostname, DNS and any firewall.") from None

    if resp.status_code == 404 and allow_404:
        return resp.status_code, None, resp.headers
    _raise_for_status(tenant, method, path, resp)
    body: Any = None
    if resp.status_code != 204 and resp.content:
        try:
            body = resp.json()
        except ValueError:
            body = None
    return resp.status_code, body, resp.headers


def _raise_for_status(tenant, method: str, path: str, resp: httpx.Response) -> None:
    """Turn a refusal into a sentence naming the likely cause.

    The 404-on-/vendor case is the one worth separating. Vendor Onboarding is a newer
    Config API surface than Jump Groups, and an appliance that does not serve it answers
    exactly like one where the object is missing — so without this, "your PRA is too old"
    and "that vendor group was deleted" read identically.
    """
    if resp.status_code < 400:
        return
    if resp.status_code == 404 and path.startswith(_VENDOR_PATH):
        raise PRATenantError(
            f"PRA at {tenant.api_base} does not serve the vendor API. Vendor Onboarding "
            f"needs a newer appliance, or this API account has not been given permission "
            f"to it (Management > API Configuration).")
    if resp.status_code == 404:
        raise PRATenantError(
            f"PRA has no object at {path} on tenant {tenant.name!r} (404). It was deleted "
            f"in the appliance, or never created.")
    if resp.status_code in (401, 403):
        raise PRATenantError(
            f"PRA refused {method} {path} on tenant {tenant.name!r} "
            f"({resp.status_code}). The API account needs Configuration API permission "
            f"covering Jump Groups, Group Policies and Vendors.")
    # 422 carries the field-level reason and it is the appliance's own words about the
    # request WE built, not an exception's — so it is the one body worth showing.
    detail = _detail(resp)
    raise PRATenantError(
        f"PRA refused {method} {path} on tenant {tenant.name!r} "
        f"({resp.status_code}){detail}")


def _detail(resp: httpx.Response) -> str:
    """The appliance's own explanation, trimmed. ``ErrorMessageResponse.message`` when the
    body is shaped, the first 300 characters otherwise."""
    try:
        body = resp.json()
    except ValueError:
        return f": {resp.text[:300]}" if resp.text else ""
    if isinstance(body, dict):
        msg = body.get("message") or body.get("error") or ""
        if msg:
            return f": {msg}"
        errors = body.get("errors")
        if errors:
            return f": {str(errors)[:300]}"
    return f": {str(body)[:300]}" if body else ""


async def _paged(tenant, path: str, *, params: dict | None = None) -> list[dict]:
    """Every page of a paginated collection.

    PRA paginates with ``X-BT-Pagination-Last-Page``. Reading page 1 and stopping is the
    bug this function exists to prevent: a vendor group with more than one page of users
    would silently lose the ones this dashboard is trying to find by username, and the
    symptom is "the login was created but the dashboard says it does not exist".
    """
    out: list[dict] = []
    page = 1
    while True:
        q = dict(params or {})
        q["current_page"] = page
        _status, body, headers = await _request(tenant, "GET", path, params=q)
        if isinstance(body, list):
            out.extend(it for it in body if isinstance(it, dict))
        try:
            last = int(headers.get("X-BT-Pagination-Last-Page") or page)
        except ValueError:
            last = page
        if page >= last or page >= 100:   # 100 pages is a runaway, not a big appliance
            return out
        page += 1


def _id(raw: Any) -> str:
    """An appliance-assigned id, as text. PRA hands back ints; every column that holds one
    in this codebase is a string, and converting in one place is what keeps a comparison
    from silently being ``"7" == 7``."""
    if isinstance(raw, dict):
        raw = raw.get("id")
    return "" if raw is None else str(raw)


# ── Jump Groups ──────────────────────────────────────────────────────────────

async def find_jump_group(tenant, name: str) -> dict | None:
    """One Jump Group by exact name, or None.

    The ``name`` query parameter filters server-side, but the match is re-checked here:
    a filter that is a substring match on some version would otherwise hand back a
    neighbour, and a Jump Group is what scopes a vendor's access.
    """
    wanted = (name or "").strip()
    if not wanted:
        return None
    rows = await _paged(tenant, _JUMP_GROUP_PATH, params={"name": wanted})
    for row in rows:
        if str(row.get("name") or "") == wanted:
            return row
    return None


async def create_jump_group(tenant, *, name: str, code_name: str,
                            comments: str = "") -> dict:
    """Create a Jump Group. Returns the appliance's row, with its id."""
    payload: dict[str, Any] = {"name": name[:NAME_MAX], "code_name": code_name[:CODE_NAME_MAX]}
    if comments:
        payload["comments"] = comments[:4096]
    _status, body, _h = await _request(tenant, "POST", _JUMP_GROUP_PATH, json=payload)
    return body if isinstance(body, dict) else {}


async def delete_jump_group(tenant, jump_group_id: str) -> None:
    await _request(tenant, "DELETE", f"{_JUMP_GROUP_PATH}/{jump_group_id}", allow_404=True)


# ── Jump Item Roles ──────────────────────────────────────────────────────────

async def find_jump_item_role(tenant, name: str) -> dict | None:
    """One Jump Item Role by exact name, or None. Read-only — the dashboard picks one, it
    never authors one, because a role is a permission model the customer owns."""
    wanted = (name or "").strip()
    if not wanted:
        return None
    for row in await _paged(tenant, _JUMP_ITEM_ROLE_PATH):
        if str(row.get("name") or "") == wanted:
            return row
    return None


# ── Group Policies ───────────────────────────────────────────────────────────

async def create_group_policy(tenant, *, name: str, perms: dict[str, bool],
                              default_jump_item_role_id: int = 1) -> dict:
    """Create a Group Policy that grants sessions and nothing else.

    ``access_perm_status="defined"`` is what makes the ``perm_*`` fields apply at all —
    left at its ``not_defined`` default the policy is created, looks right in the
    appliance, and grants nothing. Not ``"final"``: that would also override policies of
    lower priority the customer already has, which is not this dashboard's call to make.

    Every permission not named in ``perms`` keeps its schema default of ``false``. The
    ``GroupPolicy`` schema has no administrative-privilege fields at all, which is what
    makes a policy built here legal as a Vendor Group's ``default_policy`` — PRA refuses
    one that grants admin.
    """
    payload: dict[str, Any] = {
        "name": name[:NAME_MAX],
        "access_perm_status": "defined",
        "perm_access_allowed": True,
        "default_jump_item_role_id": int(default_jump_item_role_id or 1),
    }
    payload.update({k: bool(v) for k, v in perms.items()})
    _status, body, _h = await _request(tenant, "POST", _GROUP_POLICY_PATH, json=payload)
    return body if isinstance(body, dict) else {}


async def delete_group_policy(tenant, policy_id: str) -> None:
    await _request(tenant, "DELETE", f"{_GROUP_POLICY_PATH}/{policy_id}", allow_404=True)


async def add_policy_jump_group(tenant, policy_id: str, jump_group_id: str, *,
                                jump_item_role_id: int = INHERIT,
                                jump_policy_id: int = INHERIT) -> None:
    """Grant a Group Policy access to one Jump Group.

    This single call is the entire scoping story: a Group Policy reaches exactly the Jump
    Groups it has a membership row for, so a vendor policy with one membership reaches one
    POV. ``INHERIT`` (0) on both ids means "User's Default" and "Set on Jump Items", which
    defers to the policy's own default role rather than pinning a second one here.
    """
    await _request(tenant, "POST", f"{_GROUP_POLICY_PATH}/{policy_id}/jump-group",
                   json={"jump_group_id": int(jump_group_id),
                         "jump_item_role_id": int(jump_item_role_id),
                         "jump_policy_id": int(jump_policy_id)})


# ── Vendor Groups ────────────────────────────────────────────────────────────

async def find_vendor(tenant, name: str) -> dict | None:
    """One Vendor Group by exact name, or None. Same re-check as Jump Groups, same reason."""
    wanted = (name or "").strip()
    if not wanted:
        return None
    for row in await _paged(tenant, _VENDOR_PATH, params={"name": wanted}):
        if str(row.get("name") or "") == wanted:
            return row
    return None


async def create_vendor(tenant, *, name: str, policy_id: str, account_expiration: int,
                        deletion_days_after_expiration: int | None = 1,
                        administrator_ids: list[int] | None = None,
                        network_restrictions: list[str] | None = None) -> dict:
    """Create a Vendor Group whose users inherit ``policy_id``.

    ``account_expiration`` is clamped to the schema's own 1-365 here rather than trusted
    from the caller, because the caller's number came from a POV's remaining days and a
    POV that expired an hour ago would otherwise send 0 and get a 422 about a field an SE
    never typed.

    The three notification/approval flags are sent EXPLICITLY, always. Two of them default
    to true in the schema and require at least one administrator or team to be set, so
    omitting them on a group with no administrators is a guaranteed 422 — and one whose
    message is about notifications, which is not what the operator was doing.
    """
    admins = [int(a) for a in (administrator_ids or [])][:10]
    payload: dict[str, Any] = {
        "name": name[:NAME_MAX],
        "default_policy": int(policy_id),
        "account_expiration": max(EXPIRATION_MIN, min(EXPIRATION_MAX,
                                                      int(account_expiration))),
        "user_added_notification_enabled": bool(admins),
        "user_expired_notification_enabled": bool(admins),
        "user_approval_enabled": False,
    }
    if deletion_days_after_expiration is not None:
        payload["deletion_days_after_expiration"] = max(
            EXPIRATION_MIN, min(EXPIRATION_MAX, int(deletion_days_after_expiration)))
    if admins:
        payload["administrator_ids"] = admins
    if network_restrictions:
        payload["network_restrictions"] = list(network_restrictions)[:128]
    _status, body, _h = await _request(tenant, "POST", _VENDOR_PATH, json=payload)
    return body if isinstance(body, dict) else {}


async def get_vendor(tenant, vendor_id: str) -> dict | None:
    """One Vendor Group by id, or None when the appliance no longer has it."""
    _status, body, _h = await _request(tenant, "GET", f"{_VENDOR_PATH}/{vendor_id}",
                                       allow_404=True)
    return body if isinstance(body, dict) else None


async def delete_vendor(tenant, vendor_id: str) -> None:
    """Delete a Vendor Group. Per the spec this deletes its users too."""
    await _request(tenant, "DELETE", f"{_VENDOR_PATH}/{vendor_id}", allow_404=True)


# ── Vendor users ─────────────────────────────────────────────────────────────

async def list_vendor_users(tenant, vendor_id: str) -> list[dict]:
    """Every user in a Vendor Group, across every page."""
    return await _paged(tenant, f"{_VENDOR_PATH}/{vendor_id}/user")


async def create_vendor_user(tenant, vendor_id: str, *, username: str, password: str,
                             email: str = "", display_name: str = "") -> str:
    """Add a vendor user and return the id the appliance gave it.

    The re-read is not defensive coding. ``POST /vendor/{id}/user`` is documented as
    answering **200 with no body**, so the id genuinely is not in the response and the
    only way to learn it is to look the username back up. A user created but not found
    afterwards is reported as such rather than recorded with a blank id — a row that
    cannot be deleted later is worse than a failed create, because the account exists
    either way.
    """
    payload: dict[str, Any] = {
        "username": username[:USERNAME_MAX],
        "password": password,
        "enabled": True,
        # PRA has no "send an invite" call; the dashboard hands over the password, so the
        # one thing it can insist on is that the vendor replaces it immediately.
        "password_reset_next_login": True,
    }
    if email:
        payload["email_address"] = email[:256]
    if display_name:
        payload["public_display_name"] = display_name[:64]
    await _request(tenant, "POST", f"{_VENDOR_PATH}/{vendor_id}/user", json=payload)

    for row in await list_vendor_users(tenant, vendor_id):
        if str(row.get("username") or "") == username[:USERNAME_MAX]:
            return _id(row)
    raise PRATenantError(
        f"PRA accepted the vendor user {username!r} but does not list it in vendor group "
        f"{vendor_id}. Check the group in the appliance before creating another.")


async def delete_vendor_user(tenant, vendor_id: str, user_id: str) -> None:
    await _request(tenant, "DELETE", f"{_VENDOR_PATH}/{vendor_id}/user/{user_id}",
                   allow_404=True)
