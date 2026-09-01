"""The per-POV spend cap: accrual arithmetic and what to do when it is reached.

The auto-delete timer answers "how long may this POV live?". This answers the question an
operator on their own cloud account actually loses sleep over: **"how much may it cost?"**
A clock is a poor proxy — the same fortnight is twenty dollars or two thousand depending
on what the template asked for, and the second only becomes visible on an invoice weeks
later.

**The number is ACCRUED, not read off a bill.** Every reconcile pass adds
``rate now × time since the last pass`` to a running total on the row. That is deliberate
and it is the whole reason this can work at all:

- **A bill lags a day.** AWS Cost Explorer, Azure Cost Management and a GCP BigQuery
  export all report yesterday. A cap that reacts a day late has already let the money go —
  it would report a runaway rather than stop one. Accrual reacts within one sweep.
- **A bill costs money to read.** Cost Explorer bills per request and is itself untaggable;
  this codebase has already had that line reach 45% of an account.
- **It works the same on every cloud**, because it is arithmetic over things the reconcile
  pass already fetched. No new API, no new permission, no per-cloud billing export.

What it buys in exchange is that it is an **estimate at list price** — no Savings Plans,
reservations, credits, free tier, data transfer or snapshots. Everything user-facing says
so. It errs high, which is the safe direction for a cap.

**Reaching the cap suspends; it never destroys.** Suspending is reversible in one click,
which is what lets this feature exist without the auto-delete timer's two arming clocks
and dry-run mode. The worst outcome is a POV somebody has to start again, and the default
action is `warn` regardless — the "master switch alone changes nothing" brake this codebase
applies to anything that acts on its own.

Pure: stdlib only, no I/O, no app imports beyond config. ``pov_reconcile`` supplies the
rate and enqueues the job.
"""
from __future__ import annotations

from datetime import datetime, timezone

# What to do when the cap is reached. `warn` is the default everywhere: the number is an
# estimate, and an estimate that suspends a live customer demo on its first outing would
# be the last time anybody trusted it.
ACTION_WARN = "warn"
ACTION_SUSPEND = "suspend"
VALID_ACTIONS = (ACTION_WARN, ACTION_SUSPEND)

# A cap below this is refused. Not a policy floor — an arithmetic one: a POV of any size
# crosses a dollar within the hour, so a smaller cap describes a POV that suspends
# immediately, which is a mistake rather than a preference.
MIN_CAP_USD = 1.0

# The default warning threshold, as a percentage of the cap. Configurable, clamped.
DEFAULT_WARN_PERCENT = 80
MIN_WARN_PERCENT = 10
MAX_WARN_PERCENT = 99

# A single accrual step is capped at this many hours. It exists for one case: a dashboard
# that was down for days comes back and applies the CURRENT rate across the whole gap,
# which it cannot know was right — the POV may have been suspended for most of it. Capping
# the step keeps a restart from inventing a bill big enough to trip every cap at once.
# Deliberately generous, because a POV that really did run through the gap really did cost
# that money and understating it is the unsafe direction.
MAX_ACCRUAL_HOURS = 24.0


class SpendError(ValueError):
    """A spend cap could not be understood. The message names the remedy."""


def _aware(value):
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def warn_percent(configured: int | None = None) -> int:
    """The warning threshold, clamped into a range where it means something.

    Below 10% every POV warns immediately and the warning stops being read; above 99% the
    warning and the cap arrive together, which is not a warning.
    """
    try:
        value = int(configured if configured is not None else DEFAULT_WARN_PERCENT)
    except (TypeError, ValueError):
        return DEFAULT_WARN_PERCENT
    return max(MIN_WARN_PERCENT, min(MAX_WARN_PERCENT, value))


def normalize_action(value: str) -> str:
    """The configured action, defaulting to the harmless one.

    An unrecognised value resolves to ``warn`` rather than raising, matching
    ``feature_flags.install_profile``: this is read on a sweep, and a typo in one config
    row must not stop every POV being accrued. Falling back to `warn` can only ever make
    the feature do LESS.
    """
    raw = (value or "").strip().lower()
    return raw if raw in VALID_ACTIONS else ACTION_WARN


def validate_cap(value) -> float | None:
    """A cap in USD, or None for "no cap". Anything unusable is refused."""
    if value in (None, "", 0, 0.0):
        return None
    try:
        cap = float(value)
    except (TypeError, ValueError):
        raise SpendError(f"{value!r} is not an amount; give a number of US dollars") from None
    if cap < 0:
        raise SpendError("a spend cap cannot be negative")
    if cap < MIN_CAP_USD:
        raise SpendError(
            f"a cap under ${MIN_CAP_USD:.0f} would be reached within the hour by a POV of "
            f"any size. Leave it blank for no cap.")
    return round(cap, 2)


def accrue(previous_usd, accrued_at, rate_usd_per_hour, now_utc: datetime) -> tuple:
    """``(new_total, new_accrued_at, added)`` for one sweep.

    A left-hand Riemann sum: the rate observed NOW is applied across the interval since
    the last accrual. With a ten-minute sweep the error is bounded by one interval of one
    power transition, which is smaller than the gap between list price and any real bill.

    **The first call accrues nothing.** A NULL ``accrued_at`` means "never measured", and
    an unbounded interval would bill this POV for every hour since the epoch. Same rule
    the suspend schedule's latch follows, and the same reason `expires_at IS NULL` means
    never: enabling a feature must select nothing by construction rather than by a guard.
    """
    total = float(previous_usd or 0.0)
    since = _aware(accrued_at)
    if since is None:
        return total, now_utc, 0.0
    if rate_usd_per_hour is None:
        # No price source for this cloud. The clock still moves on, so a rate that appears
        # later does not then bill for the blind period.
        return total, now_utc, 0.0
    hours = (now_utc - since).total_seconds() / 3600.0
    if hours <= 0:
        return total, since, 0.0
    hours = min(hours, MAX_ACCRUAL_HOURS)
    added = max(0.0, float(rate_usd_per_hour)) * hours
    return round(total + added, 4), now_utc, added


def state(row, *, warn_at_percent: int | None = None) -> str:
    """``""``, ``"warn"`` or ``"cap"`` — what this row's spend has newly reached.

    Returns the *unlatched* transition only: a row already warned does not warn again, and
    a row already capped does not re-suspend on every pass. Raising the cap clears both
    latches, which is what makes "give it another fifty dollars" work.
    """
    cap = getattr(row, "spend_cap_usd", None)
    if not cap or cap <= 0:
        return ""
    spent = float(getattr(row, "spend_estimate_usd", 0.0) or 0.0)
    if spent >= cap:
        return "" if getattr(row, "spend_capped_at", None) else "cap"
    threshold = cap * (warn_percent(warn_at_percent) / 100.0)
    if spent >= threshold:
        return "" if getattr(row, "spend_warned_at", None) else "warn"
    return ""


def describe(row, *, warn_at_percent: int | None = None, action: str = "") -> dict:
    """The spend cap as the POV page renders it."""
    cap = getattr(row, "spend_cap_usd", None) or 0.0
    spent = float(getattr(row, "spend_estimate_usd", 0.0) or 0.0)
    percent = int(round(spent / cap * 100)) if cap else 0
    return {
        "capped": bool(cap),
        "cap_usd": round(float(cap), 2) if cap else 0.0,
        "spent_usd": round(spent, 2),
        "percent": min(percent, 999),
        "warn_percent": warn_percent(warn_at_percent),
        "action": normalize_action(action),
        # Distinct from `capped`: one is "there is a limit", the other is "it was hit".
        "over": bool(cap and spent >= cap),
        "warned": bool(getattr(row, "spend_warned_at", None)),
        "suspended_by_cap": bool(getattr(row, "spend_capped_at", None)),
        "measured": getattr(row, "spend_accrued_at", None) is not None,
        "summary": _summary(cap, spent, normalize_action(action)),
    }


def _summary(cap: float, spent: float, action: str) -> str:
    if not cap:
        return f"${spent:,.2f} estimated so far — no cap"
    if spent >= cap:
        what = "suspended" if action == ACTION_SUSPEND else "over its cap"
        return f"${spent:,.2f} of ${cap:,.2f} — {what}"
    return f"${spent:,.2f} of ${cap:,.2f} estimated"
