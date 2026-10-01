"""PRA <-> Password Safe for one POV: is the integration live, and are this POV's
credentials usable from its jump items?

The wire-up puts each guest into **both** products independently -- a PRA jump item, and a
Password Safe managed system + account -- and never connects the two. Connecting them is
PRA's **Password Safe integration**, which the Configuration API cannot create (see
``pra_ps_vault_api``). So this does the two things that ARE possible:

  * :func:`check` -- ask PRA which Password Safe accounts it can see, and match them to this
    POV's managed systems by name (``pov_wireup.ps_system_name``). Zero matches with guests
    onboarded is the signal that the integration is not configured yet, and the step then
    shows the checklist instead of a button that would do nothing.
  * :func:`link` -- for each matched account, grant it to the POV's vendor Group Policy
    (inject only) and associate it with that guest's jump item, so a session started from
    the jump item is offered the Password Safe credential.

Both are live calls behind a button. The results are stored as COUNTS on the row so the
setup ladder can read them without a network call -- the same rule ``pov_gateway.describe``
follows -- and judged by the count, never by whether a call returned.
"""
from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy.orm import Session

from ..database import PovEnvironment, PovEnvironmentVM
from . import pov_gateway, pra_ps_vault_api
from .pov_wireup import ps_system_name
from .pra_tenant_api import PRATenantError

logger = logging.getLogger(__name__)

# What the PRA admin does in /login, because the API has no endpoint for it. Shown when a
# check finds nothing, and in the docs.
CHECKLIST = (
    "In PRA /login, configure the Password Safe integration (the customer's PRA admin does "
    "this once per appliance).",
    "Point it at the same Password Safe tenant this POV onboards into.",
    "Make sure the POV's managed accounts are visible to the integration's API user, and "
    "wait for PRA to sync them.",
    "Press Check again.",
)


class LinkError(Exception):
    """A refusal carrying the remedy."""


def _onboarded(db: Session, env: PovEnvironment) -> list[PovEnvironmentVM]:
    return (db.query(PovEnvironmentVM)
              .filter(PovEnvironmentVM.environment_id == env.id,
                      PovEnvironmentVM.ps_managed_system_id.isnot(None)).all())


async def _match(db: Session, env: PovEnvironment):
    """(tenant, [(vm, account), ...], onboarded_count)."""
    if not env.pra_tenant_id:
        raise LinkError("this POV is not wired into a PRA tenant.")
    vms = _onboarded(db, env)
    if not vms:
        raise LinkError("no guest of this POV is onboarded into Password Safe yet. Run "
                        "the wire-up with a Password Safe tenant first.")
    try:
        tenant = pov_gateway.pra_tenant(db, env)
    except pov_gateway.GatewayInstallError as exc:
        raise LinkError(str(exc)) from None
    by_name = {ps_system_name(env, vm).casefold(): vm for vm in vms}
    pairs = []
    for acct in await pra_ps_vault_api.list_ps_accounts(tenant):
        vm = by_name.get(acct["system"].casefold())
        if vm is not None:
            pairs.append((vm, acct))
    return tenant, pairs, len(vms)


def _record(db: Session, env: PovEnvironment, *, matched: int,
            linked: int | None = None) -> None:
    env.pra_ps_matched_count = matched
    if linked is not None:
        env.pra_ps_linked_count = linked
    env.pra_ps_checked_at = datetime.utcnow()
    db.commit()


async def check(db: Session, env: PovEnvironment) -> dict:
    """Which of this POV's Password Safe accounts PRA can see. Stores the count."""
    try:
        _tenant, pairs, onboarded = await _match(db, env)
    except PRATenantError as exc:
        raise LinkError(str(exc)) from None
    _record(db, env, matched=len(pairs))
    out = {"matched": len(pairs), "onboarded": onboarded,
           "accounts": [{"vm": vm.name, "account": a["name"], "system": a["system"]}
                        for vm, a in pairs]}
    if not pairs:
        out["detail"] = ("PRA sees none of this POV's Password Safe accounts, so the "
                         "Password Safe integration is not configured or has not synced.")
        out["checklist"] = list(CHECKLIST)
    else:
        out["detail"] = f"PRA sees {len(pairs)} of {onboarded} onboarded account(s)."
    return out


async def link(db: Session, env: PovEnvironment) -> dict:
    """Grant each matched account to the vendor policy and its guest's jump item.

    Idempotent: an account already granted, or already associated, is left alone. Refuses
    when PRA sees nothing, rather than reporting a link of zero as done.
    """
    try:
        tenant, pairs, onboarded = await _match(db, env)
        if not pairs:
            _record(db, env, matched=0)
            raise LinkError(
                "PRA sees none of this POV's Password Safe accounts, so there is nothing "
                "to link. Configure the Password Safe integration in PRA /login, then "
                "press Check.")

        granted_already = set()
        if env.pra_vendor_policy_id:
            granted_already = await pra_ps_vault_api.policy_account_ids(
                tenant, env.pra_vendor_policy_id)

        lines, linked = [], 0
        for vm, acct in pairs:
            jump_type = pra_ps_vault_api.JUMP_TYPE_FOR_OS.get(vm.guest_os or "")
            result = "no jump item"
            if vm.pra_jump_id and jump_type:
                result = await pra_ps_vault_api.associate_with_jump_item(
                    tenant, acct["id"], int(vm.pra_jump_id), jump_type)
            if env.pra_vendor_policy_id and acct["id"] not in granted_already:
                await pra_ps_vault_api.grant_to_policy(
                    tenant, env.pra_vendor_policy_id, acct["id"])
            if result in ("added", "already"):
                linked += 1
            lines.append(f"{vm.name}: {acct['name']} — {result}")
    except PRATenantError as exc:
        raise LinkError(str(exc)) from None

    _record(db, env, matched=len(pairs), linked=linked)
    if not env.pra_vendor_policy_id:
        lines.append("No vendor group yet, so no Group Policy grant was made. Create the "
                     "vendor group and press Link again to grant it.")
    logger.info("POV %s: linked %d of %d Password Safe account(s) in PRA", env.id, linked,
                len(pairs))
    return {"matched": len(pairs), "linked": linked, "onboarded": onboarded,
            "lines": lines}


def describe(env: PovEnvironment) -> dict:
    """The stored result for one row. No network calls."""
    return {
        "pra_ps_matched_count": int(env.pra_ps_matched_count or 0),
        "pra_ps_linked_count": int(env.pra_ps_linked_count or 0),
        "pra_ps_checked": env.pra_ps_checked_at is not None,
        "pra_ps_checked_at": (env.pra_ps_checked_at.isoformat()
                              if env.pra_ps_checked_at else ""),
    }
