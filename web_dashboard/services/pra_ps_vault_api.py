"""The PRA Vault calls behind the PRA <-> Password Safe link: read what the integration
produced, and grant it.

**What the Config API can and cannot do here**, which decides the shape of the feature:

  * It **cannot create** the Password Safe integration. There is no endpoint for the
    connection itself; the customer's PRA admin configures it in ``/login``.
  * It **can see** what that integration produced. ``GET /vault/account?type=password_safe``
    lists the accounts PRA reads from Password Safe, and ``GET /vault/account/{id}`` returns
    a ``VaultPasswordSafeAccount`` whose ``system`` is the Password Safe managed system's
    name. The LIST rows are plain ``VaultAccount`` and carry no ``system``, which is why the
    detail is read per account.
  * It **can grant** them: ``POST /group-policy/{id}/vault-account`` (``inject``) and the
    per-account Asset association, which decides which jump items an account is offered
    on.

The wire and its refusals are ``pra_vendor_api``'s, deliberately — one place turns a PRA
status into a sentence, so a 403 here reads the same as a 403 creating a vendor group.
"""
from __future__ import annotations

import logging

from .pra_tenant_api import PRATenantError
from .pra_vendor_api import _GROUP_POLICY_PATH, _paged, _request

logger = logging.getLogger(__name__)

_VAULT_ACCOUNT_PATH = "/api/config/v1/vault/account"

# The jump item types the wire-up creates, by guest OS -- see pov_wireup.wire_vm. These
# are AccountInjectableJumpItem.type values.
JUMP_TYPE_FOR_OS = {"linux": "shell_jump", "windows": "remote_rdp"}


async def list_ps_accounts(tenant) -> list[dict]:
    """Every Password Safe-sourced Vault account PRA can see, with its system name.

    Normalised to ``{id, name, username, system, workgroup}``. An account whose detail read
    fails is skipped and logged, not fatal: one account deleted between the list and the
    read must not blank the whole answer.
    """
    rows = await _paged(tenant, _VAULT_ACCOUNT_PATH,
                        params={"type": "password_safe", "per_page": 100})
    out = []
    for row in rows:
        acct_id = row.get("id")
        if acct_id is None:
            continue
        try:
            _s, body, _h = await _request(tenant, "GET", f"{_VAULT_ACCOUNT_PATH}/{acct_id}",
                                          allow_404=True)
        except PRATenantError:
            logger.info("PRA vault account %s could not be read; skipped", acct_id)
            continue
        if not isinstance(body, dict):
            continue
        out.append({
            "id": int(acct_id),
            "name": str(body.get("name") or row.get("name") or ""),
            "username": str(body.get("username") or ""),
            "system": str(body.get("system") or ""),
            "workgroup": str(body.get("workgroup") or ""),
        })
    return out


async def policy_account_ids(tenant, policy_id: str) -> set[int]:
    """The Vault account ids a Group Policy already grants."""
    _s, body, _h = await _request(tenant, "GET",
                                  f"{_GROUP_POLICY_PATH}/{policy_id}/vault-account")
    if not isinstance(body, list):
        return set()
    return {int(r["account_id"]) for r in body
            if isinstance(r, dict) and r.get("account_id") is not None}


async def grant_to_policy(tenant, policy_id: str, account_id: int) -> None:
    """Let the policy's members INJECT the account. Never ``inject_and_checkout``: a vendor
    who can check a credential out can carry it away from the session."""
    await _request(tenant, "POST", f"{_GROUP_POLICY_PATH}/{policy_id}/vault-account",
                   json={"account_id": int(account_id), "role": "inject"})


async def associate_with_jump_item(tenant, account_id: int, jump_id: int,
                                   jump_type: str) -> str:
    """Make the account offered on one jump item. Returns what happened, as a word.

    Three cases, by what the account already has:

      * ``any_jump_items`` -- it is offered everywhere already. Left alone: ``"already"``.
      * ``no_jump_items`` -- somebody deliberately excluded it. **Left alone and reported**
        (``"excluded"``): overriding a customer admin's explicit choice in their own
        appliance is not this dashboard's call.
      * ``criteria`` -- the jump item is added to its list (``"added"``), or found there
        (``"already"``).

    An account with no association of its own inherits its Account Group's, and the GET
    refuses for it. That one gets its own ``criteria`` naming this jump item (``"added"``).
    The account belongs to this POV's own managed system, so narrowing it to this POV's
    jump item is the intent rather than a side effect.
    """
    path = f"{_VAULT_ACCOUNT_PATH}/{account_id}/jump-item-association"
    item = {"id": int(jump_id), "type": jump_type}
    try:
        _s, current, _h = await _request(tenant, "GET", path)
    except PRATenantError:
        current = None
    if not isinstance(current, dict):
        await _request(tenant, "POST", path,
                       json={"filter_type": "criteria", "criteria": {}, "jump_items": [item]})
        return "added"

    kind = current.get("filter_type")
    if kind == "any_jump_items":
        return "already"
    if kind == "no_jump_items":
        return "excluded"
    have = {(int(j.get("id")), j.get("type")) for j in (current.get("jump_items") or [])
            if isinstance(j, dict) and j.get("id") is not None}
    if (int(jump_id), jump_type) in have:
        return "already"
    await _request(tenant, "POST", f"{path}/jump-item", json=item)
    return "added"
