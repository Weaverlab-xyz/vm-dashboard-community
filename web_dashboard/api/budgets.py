"""Push the configured spend budget into the cloud, so it alerts without this dashboard.

`cost_monthly_budget` and the per-cloud keys are evaluated only by
`cost_service.evaluate_budget` — here, in this process, against figures this process
fetched. When the dashboard is down nothing watches the spend at all, which is exactly
when an unattended sandbox runs one up. A budget living in the provider keeps watching.

**The push is an action an operator takes, never a side effect of saving a number.**
Creating billing configuration in somebody's cloud account because they typed into a
Settings form is the surprise this design exists to avoid — the same reasoning that kept a
reclaim button off the unattributed cost list. So the number lives in Settings and the
push lives here, behind a request that says what it will do first.

**GET before POST is the point.** The read reports what is in the cloud beside what the
dashboard would set, field by field, so the operator can look before pressing a button
that edits their billing account.

**Nothing here deletes a budget.** Clearing the configured limit stops the dashboard
managing the number; it does not tear a budget somebody may be relying on out of their
account. `provider_budget.assert_writable` is the other half: a budget this dashboard did
not name is never written to either.

**AWS and Azure. Not GCP**, and the reason is concrete rather than effort: a GCP billing
budget is scoped to a BILLING ACCOUNT, an id held nowhere in this app — it knows only
`gcp_project_id` and the BigQuery export table — the `google-cloud-billing-budgets`
package is not a dependency, and the cost audit recorded the API as not enabled on the
account it examined. That is a conversation with an operator, not a commit.

(An earlier version of this note claimed Azure required action groups. It does not:
Consumption budget notifications take `contactEmails`, a plain list of addresses.
`contactGroups` and `contactRoles` are alternatives. The claim would have sent the next
reader looking for plumbing they do not need.)
"""
import logging
from datetime import date

from fastapi import APIRouter, Depends, HTTPException

from ..database import User
from ..services import aws_service, azure_service, provider_budget
from .auth import require_explicit_permission

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/budgets", tags=["budgets"])


def _cfg(key: str, fallback=""):
    from ..config import settings
    from ..services import config_service
    return config_service.get(key) or getattr(settings, key, fallback)


def _month_start() -> str:
    """The first of the current month, which is when a monthly budget's period begins.

    Sent only on CREATE. Azure requires a start date and refuses one in the past, but
    re-dating an existing budget on every push would restart its accumulated period and
    silence an alert that had already fired.
    """
    today = date.today()
    return date(today.year, today.month, 1).isoformat()


def _wanted(cloud: str, currency: str) -> dict:
    """The budget the dashboard would set. Raises ``BudgetError`` with a reason."""
    limit = _cfg(f"cost_budget_{cloud}") or _cfg("cost_monthly_budget")
    return provider_budget.desired(
        cloud, limit,
        currency=currency,
        emails=_cfg("cost_budget_notify_emails"),
        threshold=_cfg("cost_budget_alert_percent"),
    )


# Per-cloud wiring, so the read and the push are written once. A third cloud is an entry
# here, not a third copy of the orchestration — the move `vdesktop_service._SEAT_BACKENDS`
# made, for the same reason. A cloud absent from this map has no route at all rather than
# a route that 500s.
_CLOUDS = {
    "aws": {
        # AWS budgets are denominated in the currency given at creation; the dashboard
        # sets USD because Cost Explorer reports UnblendedCost in the account currency and
        # this app has no conversion anywhere.
        "currency": "USD",
        "scope_id": lambda: aws_service.account_id(),
        "scope_key": "account_id",
        # The scope is threaded through: AWS budgets are addressed by account id, which
        # the GET and PUT both need. Azure re-resolves its subscription from the same
        # credentials internally and ignores it.
        "get": lambda scope, name, currency: aws_service.get_budget(scope, name),
        "put": lambda scope, want, exists, start: aws_service.put_budget(
            scope, want, exists),
    },
    "azure": {
        # Consumption budgets use the subscription's own billing currency. The dashboard
        # cannot set it and the API does not echo it, so it is not a managed field — see
        # `azure_service.get_budget`'s currency_hint.
        "currency": "",
        "scope_id": lambda: azure_service.subscription_id(),
        "scope_key": "subscription_id",
        "get": lambda scope, name, currency: azure_service.get_budget(name, currency),
        "put": lambda scope, want, exists, start: azure_service.put_budget(
            want, "" if exists else start),
    },
}


def _backend(cloud: str) -> dict:
    backend = _CLOUDS.get((cloud or "").lower())
    if backend is None:
        raise HTTPException(status_code=404, detail=f"No budget support for '{cloud}'.")
    return backend


@router.get("/{cloud}")
async def read_budget(cloud: str,
                      current_user: User = Depends(require_explicit_permission("costs", "read"))) -> dict:
    """What is in the account now, beside what a push would set.

    Answers 200 even when nothing is configured yet: "you have not set this up" is the
    answer to the question, not a fault. The reason travels in `reason` so the page can
    say which of the two settings is missing rather than showing a disabled button.
    """
    backend = _backend(cloud)
    try:
        want = _wanted(cloud.lower(), backend["currency"])
    except provider_budget.BudgetError as exc:
        # The wording comes from `provider_budget.REFUSALS`, addressed by the refusal's
        # code — never from `str(exc)`. Today every one of them was written by hand, so
        # nothing internal would leak; the table exists so that stays true after somebody
        # wraps a BudgetError around a provider's error. The exception itself goes to the
        # log, which is where a message with an internal detail in it belongs.
        logger.info("%s budget is not configured: %s", cloud, exc)
        return {"cloud": cloud, "configured": False,
                "reason": provider_budget.reason_for(exc.code),
                "existing": None, "diff": None}

    scope = await backend["scope_id"]()
    existing = await backend["get"](scope, want["name"], want["currency"])
    return {"cloud": cloud, "configured": True, "reason": "",
            backend["scope_key"]: scope, "name": want["name"], "desired": want,
            "existing": existing, "diff": provider_budget.diff(existing, want)}


@router.post("/{cloud}")
async def push_budget(cloud: str,
                      current_user: User = Depends(require_explicit_permission("costs", "write"))) -> dict:
    """Create or update the dashboard's budget in the cloud account.

    Refuses a budget it did not name — see `provider_budget.assert_writable`. That guard
    is doing a tag's job on AWS, whose budgets carry no tags, so a name prefix is the only
    thing separating one this created from one a finance team created.
    """
    backend = _backend(cloud)
    try:
        want = _wanted(cloud.lower(), backend["currency"])
        provider_budget.assert_writable(want["name"])
    except provider_budget.BudgetError as exc:
        # Same rule as the read: a constant out of `REFUSALS`, the exception to the log.
        logger.info("%s budget push refused: %s", cloud, exc)
        raise HTTPException(status_code=400,
                            detail=provider_budget.reason_for(exc.code))

    scope = await backend["scope_id"]()
    existing = await backend["get"](scope, want["name"], want["currency"])
    if existing and not provider_budget.owned(existing.get("name", "")):
        # Belt and braces: `want["name"]` is generated so it always carries the prefix,
        # which makes this unreachable today. It stays because the day someone makes the
        # name configurable, this is the line that refuses rather than the one that does not.
        raise HTTPException(
            status_code=409,
            detail=f"A budget named '{existing.get('name')}' exists and was not created "
                   "by this dashboard; it will not be modified.")

    await backend["put"](scope, want, bool(existing), _month_start())
    action = "updated" if existing else "created"
    logger.info("%s budget %s: %s at %s %s (notify %d address(es) at %d%%)",
                cloud, action, want["name"], want["currency"] or "account currency",
                want["limit"], len(want["emails"]), want["threshold_percent"])
    return {"ok": True, "cloud": cloud, "action": action, "name": want["name"],
            backend["scope_key"]: scope, "desired": want}
