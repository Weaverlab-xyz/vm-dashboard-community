"""What budget the dashboard would set in the cloud, and whether it may touch one.

``cost_monthly_budget`` and the four ``cost_budget_<cloud>`` keys are evaluated only by
``cost_service.evaluate_budget`` — in this process, against figures this process fetched.
So when the dashboard is down nothing is watching the spend at all, which is exactly when
an unattended sandbox runs one up. A budget in the provider keeps watching regardless.

Pure policy: no SDK, no database, no clock. The decisions are testable on plain values and
the calls live in ``aws_service``, the same split ``expiry_policy``, ``spend_policy``,
``retry_policy`` and ``preflight`` already keep.

**The name prefix is the only ownership marker there is.** AWS budgets carry no tags —
there is no field to stamp "the dashboard made this". So a deterministic
``vm-dashboard-``-prefixed name is what separates a budget this created from one the
customer's finance team created, and anything not matching that prefix is never written to.
That is a weaker guarantee than a tag and it is stated here rather than assumed: a human
who names their own budget ``vm-dashboard-monthly`` will have it adopted, and nothing can
detect that.

**Nothing here deletes.** A limit of ``0`` means the dashboard stops managing the number,
not that a budget somebody may be relying on should be torn out of their billing account.
The reapers refuse to touch what they did not create, and this refuses to remove what it
cannot prove it owns.
"""
from __future__ import annotations

# Every budget this writes carries the prefix, and only budgets carrying it are ever
# updated. Changing it orphans every budget already pushed — they keep working and keep
# alerting, but this stops recognising them, so a rename is a migration and not a tweak.
NAME_PREFIX = "vm-dashboard-"

# Percent of the limit at which the provider should notify. Clamped rather than validated
# into an error: a nonsensical threshold should not stop a budget existing.
DEFAULT_ALERT_PERCENT = 80
MIN_ALERT_PERCENT = 1
MAX_ALERT_PERCENT = 100


class BudgetError(Exception):
    """Raised when a push cannot proceed, with a reason meant for an operator."""


def budget_name(cloud: str, scope: str = "monthly") -> str:
    """The deterministic name for this dashboard's budget on a cloud.

    Deterministic so a second push updates the first rather than creating a pile of
    near-identical budgets, which is what a timestamp or a uuid in the name would do.
    """
    return f"{NAME_PREFIX}{(cloud or '').lower()}-{(scope or 'monthly').lower()}"


def owned(name: str) -> bool:
    """Whether this dashboard may write to a budget of this name. See the module docstring
    for why a prefix is doing a tag's job."""
    return (name or "").startswith(NAME_PREFIX)


def alert_percent(configured=None) -> int:
    """The notify threshold, clamped into a range where it means something."""
    try:
        value = int(configured if configured is not None else DEFAULT_ALERT_PERCENT)
    except (TypeError, ValueError):
        return DEFAULT_ALERT_PERCENT
    return max(MIN_ALERT_PERCENT, min(MAX_ALERT_PERCENT, value))


def parse_emails(raw) -> list:
    """The addresses the CLOUD will notify, from a comma or space separated string.

    Deliberately not validated beyond "contains an @": the provider validates them and
    rejects the call with its own message, which is more accurate than anything guessed
    here and reaches the operator intact.
    """
    if isinstance(raw, (list, tuple)):
        parts = list(raw)
    else:
        parts = str(raw or "").replace(";", ",").replace(" ", ",").split(",")
    return [p.strip() for p in parts if p and "@" in str(p)]


def desired(cloud: str, limit, currency: str = "USD", emails=None,
            threshold=None, scope: str = "monthly") -> dict:
    """The budget the dashboard would set. Raises ``BudgetError`` when it would be useless.

    Refused rather than pushed when there is nobody to tell. A budget with no subscriber
    is a row in a billing console that alerts no one — which is the exact failure this
    feature exists to fix, so creating one would be worse than doing nothing.
    """
    try:
        amount = float(limit or 0)
    except (TypeError, ValueError):
        amount = 0.0
    if amount <= 0:
        raise BudgetError(
            "No budget is configured for this cloud. Set one in Settings → Cloud Costs "
            "first; clearing it here does not remove a budget already in the cloud.")
    to = parse_emails(emails)
    if not to:
        raise BudgetError(
            "Set at least one notification email. The whole point of a budget in the "
            "provider is that it alerts when this dashboard is not running, so it needs "
            "an address that does not go through here.")
    return {
        "name": budget_name(cloud, scope),
        "limit": round(amount, 2),
        "currency": (currency or "USD").upper(),
        "time_unit": "MONTHLY",
        "threshold_percent": alert_percent(threshold),
        "emails": to,
    }


def diff(existing, want: dict) -> dict:
    """What a push would change, so the caller can report instead of guessing.

    ``existing`` is ``None`` when the cloud has no such budget. Compared field by field
    rather than by equality so the answer names WHICH value moved — an operator deciding
    whether to press a button that edits their billing account deserves that much.
    """
    if not existing:
        return {"action": "create", "changes": {}, "name": want["name"]}
    changes = {}
    for field in ("limit", "currency", "time_unit", "threshold_percent"):
        before, after = existing.get(field), want.get(field)
        if field == "limit":
            before = round(float(before or 0), 2)
            after = round(float(after or 0), 2)
        if before != after:
            changes[field] = {"from": before, "to": after}
    before_emails = sorted(existing.get("emails") or [])
    after_emails = sorted(want.get("emails") or [])
    if before_emails != after_emails:
        changes["emails"] = {"from": before_emails, "to": after_emails}
    return {"action": "update" if changes else "unchanged",
            "changes": changes, "name": want["name"]}


def assert_writable(name: str) -> None:
    """Refuse to write to a budget this dashboard did not name.

    The one guard standing between a push and somebody's finance-owned budget, so it
    raises rather than returning a bool nobody checks.
    """
    if not owned(name):
        raise BudgetError(
            f"'{name}' was not created by this dashboard (its budgets are named "
            f"'{NAME_PREFIX}…'), so it will not be modified. Rename or remove it in the "
            "cloud console if you want the dashboard to manage a budget here.")
