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

AWS only for now. Azure's notification model is action groups rather than an address, and
GCP needs a billing-account id this app does not hold plus an API the customer must enable
— see `provider_budget` for the rest.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException

from ..database import User
from ..services import aws_service, provider_budget
from .auth import require_admin

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/budgets", tags=["budgets"])


def _cfg(key: str, fallback=""):
    from ..config import settings
    from ..services import config_service
    return config_service.get(key) or getattr(settings, key, fallback)


def _wanted() -> dict:
    """The budget the dashboard would set for AWS. Raises ``BudgetError`` with a reason."""
    limit = _cfg("cost_budget_aws") or _cfg("cost_monthly_budget")
    return provider_budget.desired(
        "aws", limit,
        currency="USD",
        emails=_cfg("cost_budget_notify_emails"),
        threshold=_cfg("cost_budget_alert_percent"),
    )


@router.get("/aws")
async def read_aws_budget(current_user: User = Depends(require_admin)) -> dict:
    """What is in the account now, beside what a push would set.

    Answers 200 even when nothing is configured yet: "you have not set this up" is the
    answer to the question, not a fault. The reason travels in `reason` so the page can
    say which of the two settings is missing rather than showing a disabled button.
    """
    try:
        want = _wanted()
    except provider_budget.BudgetError as exc:
        return {"configured": False, "reason": str(exc), "existing": None, "diff": None}

    account = await aws_service.account_id()
    existing = await aws_service.get_budget(account, want["name"])
    return {"configured": True, "reason": "", "account_id": account,
            "name": want["name"], "desired": want, "existing": existing,
            "diff": provider_budget.diff(existing, want)}


@router.post("/aws")
async def push_aws_budget(current_user: User = Depends(require_admin)) -> dict:
    """Create or update the dashboard's budget in the AWS account.

    Refuses a budget it did not name — see `provider_budget.assert_writable`. That guard
    is doing a tag's job, because AWS budgets carry no tags and a name prefix is the only
    thing separating one this created from one a finance team created.
    """
    try:
        want = _wanted()
        provider_budget.assert_writable(want["name"])
    except provider_budget.BudgetError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    account = await aws_service.account_id()
    existing = await aws_service.get_budget(account, want["name"])
    if existing and not provider_budget.owned(existing.get("name", "")):
        # Belt and braces: `want["name"]` is generated so it always carries the prefix,
        # which makes this unreachable today. It stays because the day someone makes the
        # name configurable, this is the line that refuses rather than the one that does not.
        raise HTTPException(
            status_code=409,
            detail=f"A budget named '{existing.get('name')}' exists and was not created "
                   "by this dashboard; it will not be modified.")

    action = await aws_service.put_budget(account, want, bool(existing))
    logger.info("aws budget %s: %s at %s %s (notify %d address(es) at %d%%)",
                action, want["name"], want["currency"], want["limit"],
                len(want["emails"]), want["threshold_percent"])
    return {"ok": True, "action": action, "name": want["name"],
            "account_id": account, "desired": want}
