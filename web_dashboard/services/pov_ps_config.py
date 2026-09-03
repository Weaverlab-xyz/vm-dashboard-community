"""Reading a POV's Password Safe tenant back against the runbook that filled it.

The runbook a Password Safe POV is driven from — BeyondTrust's Skytap POC step-by-step —
spends its first ninety minutes on setup steps 5b to 11, all of it clicked into the
Password Safe console by the customer's own administrator. Then twenty use cases assume it
was done right.

**This dashboard cannot do that work, and the reason is the API rather than a choice.**
Taken from BeyondTrust's own endpoint tables:

    step 5   resource zones, resource brokers   no documented endpoint at all
    step 6   password policies                  read-only
    step 6   access policies                    read, plus POST AccessPolicies/Test
    step 9   discovery credentials              no documented endpoint
    step 10  discovery scans                    no documented endpoint; UI only
    step 11  directory queries                  no documented endpoint
    UC1-10   Smart Rules                        create is FilterAssetAttribute alone

So there is no "bootstrap this tenant" to write. What there is, and what an SE actually
loses time to, is the other half of the same problem: **finding out what the admin
finished.** Today that is a screen-share and a lot of "can you click Configuration for me".

Two things follow, and they are the whole module:

  * **A readiness read.** One signed-in pass over the eight collections the API does
    serve, each reported against the runbook step that creates it. Per-collection
    tri-state, because "your Password Safe does not serve that endpoint" and "this read
    failed" send an operator to different places.
  * **Re-processing a Smart Rule.** ``POST SmartRules/{id}/Process`` is the one runbook
    action the API exposes, and it happens to be the one a demo repeats: use case 1 is
    watching a rule onboard the discovered systems, and use case 18 is that again after
    two guests and a batch of AD users arrive. Neither needs a rule created.

**Nothing here writes to the customer's tenant.** The read is a read and the process
re-runs a rule the customer already made, which is what makes both safe to put behind a
button on a POV somebody else configured.
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from ..database import PovEnvironment
from . import bt_tenant_service, ps_api_service

logger = logging.getLogger(__name__)


class PsConfigError(Exception):
    """A refusal carrying the remedy, not just the cause."""


# Each collection the API serves, the runbook step that creates it, and what an operator
# does about an empty one. Ordered as the runbook goes, so reading the panel is reading the
# document.
#
# `expect` is what "done" looks like, and it is deliberately a MINIMUM COUNT rather than a
# predicate on the rows: the runbook's names are per-customer (`btpoc.upm.academy`,
# `PSDirBrowser`, `ServiceDesk_Users`) and a POV that renamed them has still done the step.
# Matching on names would report a correctly-configured tenant as incomplete.
_ITEMS = (
    ("workgroups", "Step 5 — Resource Zone", 1,
     "Create the resource zone; its workgroup is added with it."),
    ("access_policies", "Step 6 — Access Policies", 2,
     "The runbook wants two: 24x7-Auto and 24x7-Manual."),
    ("password_policies", "Step 6 — Password Policies", 2,
     "The runbook wants two, one per guest OS."),
    ("functional_accounts", "Step 7 — Functional Accounts", 2,
     "One per guest OS at least, plus the AD directory account."),
    ("directories", "Step 8 — AD Managed System", 1,
     "The directory managed system, without which AD accounts cannot be managed."),
    ("managed_systems", "Steps 8-10 / UC1 — Managed Systems", 1,
     "Systems arrive from the discovery scan and the onboarding Smart Rule."),
    ("user_groups", "Step 11 — User Groups", 2,
     "The requestor and approver groups the approval use cases need."),
    ("smart_rules", "UC1-10 — Smart Rules", 1,
     "The automation engine. Created in the console; this dashboard can re-run them."),
)

# The steps with no endpoint to read, reported so the panel accounts for the whole runbook
# rather than for the part that happens to be visible. A step absent from a checklist reads
# as a step nobody thought about.
UNVERIFIABLE = (
    ("Step 5 — Resource Broker", "no API for resource zones or brokers; check "
                                 "Configure Zones in the console"),
    ("Step 9 — Discovery Credentials", "no API; check Discovery Management"),
    ("Step 10 — Discovery Scan", "no API; scans are run from the console"),
    ("Step 11 — Directory Queries", "no API; check Role Based Access"),
)


def _tenant(db: Session, env: PovEnvironment):
    """This POV's Password Safe tenant, or a refusal naming what is missing."""
    if not env.ps_tenant_id:
        raise PsConfigError(
            "this POV names no Password Safe tenant, so there is nothing to read. "
            "Choose one in the Tenants column.")
    try:
        return bt_tenant_service.resolve(db, "password_safe", env.ps_tenant_id)
    except bt_tenant_service.BTTenantError as exc:
        raise PsConfigError(
            f"this POV's Password Safe tenant could not be resolved: {exc}") from None


def _creds(tenant) -> dict:
    return ps_api_service.tenant_creds(tenant.base_url, tenant.client_id, tenant.secret)


def _count(probe: dict) -> int:
    return len(probe.get("rows") or [])


def _state_for(probe: dict, expect: int) -> str:
    """``ok`` / ``short`` / ``missing`` / ``unavailable`` / ``error``.

    ``short`` exists so a tenant with one access policy is not reported the same as one
    with none: the first is a step half done and the second is a step not started, and
    only one of them is a conversation with the customer.
    """
    state = probe.get("state")
    if state != ps_api_service.PROBE_OK:
        return state or ps_api_service.PROBE_ERROR
    found = _count(probe)
    if found == 0:
        return "missing"
    return "ok" if found >= expect else "short"


async def read(db: Session, env: PovEnvironment) -> dict:
    """The readiness payload the POV page's panel reads. One signed-in pass.

    Never raises for a collection — see ``ps_api_service.read_config_inventory``. It DOES
    raise for a POV with no tenant, or one whose tenant cannot be resolved, because those
    are about the POV rather than about Password Safe and have a different fix.
    """
    tenant = _tenant(db, env)
    inventory = await ps_api_service.read_config_inventory(_creds(tenant))

    if not inventory.get("reachable"):
        return {"tenant": tenant.name, "reachable": False,
                "detail": inventory.get("detail") or "the tenant could not be reached",
                "items": [], "unverifiable": [
                    {"step": s, "detail": d} for s, d in UNVERIFIABLE]}

    items = []
    for key, step, expect, remedy in _ITEMS:
        probe = inventory.get(key) or {}
        items.append({
            "key": key,
            "step": step,
            "state": _state_for(probe, expect),
            "found": _count(probe),
            "expected": expect,
            "remedy": remedy,
            # The probe's own words when it could not answer — never a stringified
            # exception, which could quote the tenant's response body.
            "detail": probe.get("detail") or "",
        })
    return {
        "tenant": tenant.name,
        "reachable": True,
        "detail": "",
        "items": items,
        "unverifiable": [{"step": s, "detail": d} for s, d in UNVERIFIABLE],
    }


async def smart_rules(db: Session, env: PovEnvironment) -> list:
    """This tenant's Smart Rules, shaped for the picker behind the re-process button.

    Only the four fields the panel shows. The raw rows carry an organisation id and a
    description that can quote customer naming, and a picker does not need either.
    """
    tenant = _tenant(db, env)
    inventory = await ps_api_service.read_config_inventory(_creds(tenant))
    probe = inventory.get("smart_rules") or {}
    if probe.get("state") != ps_api_service.PROBE_OK:
        raise PsConfigError(
            f"the Smart Rules could not be read from tenant {tenant.name!r}: "
            f"{probe.get('detail') or 'the read failed'}.")

    out = []
    for row in probe.get("rows") or []:
        rule_id = row.get("SmartRuleID") or row.get("ID")
        if not rule_id:
            continue
        out.append({
            "id": int(rule_id),
            "title": str(row.get("Title") or ""),
            "category": str(row.get("Category") or ""),
            "rule_type": str(row.get("RuleType") or ""),
            "last_processed": str(row.get("LastProcessedDate") or ""),
        })
    # By title, because the runbook numbers its rules ("1 - List", "5 - Map and Access")
    # and that numbering is how an SE finds the one they just talked about.
    return sorted(out, key=lambda r: r["title"].lower())


async def process(db: Session, env: PovEnvironment, rule_id) -> dict:
    """Re-run one Smart Rule. Queued, so this returns before Password Safe finishes."""
    tenant = _tenant(db, env)
    try:
        result = await ps_api_service.process_smart_rule(rule_id, _creds(tenant))
    except ps_api_service.PSApiError as exc:
        # A purpose-built refusal from this codebase, so its words are the useful part.
        raise PsConfigError(str(exc)) from None
    except Exception as exc:  # noqa: BLE001
        logger.warning("POV %s: processing Smart Rule %s failed", env.id, rule_id,
                       exc_info=True)
        raise PsConfigError(
            f"Password Safe would not process that Smart Rule "
            f"({type(exc).__name__}). Check the API identity has Smart Rule "
            f"Management, and that the rule is not read-only.") from None
    result["tenant"] = tenant.name
    return result


def describe(env: PovEnvironment) -> dict:
    """Whether the panel has anything to read — no network calls.

    Spread onto every row of the POV list, so a call here would be one per row. The live
    read is behind a button, like the Gateway check.
    """
    return {"ps_config_readable": bool(env.ps_tenant_id)}
