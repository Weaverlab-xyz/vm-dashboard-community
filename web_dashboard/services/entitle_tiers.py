"""The three approval tiers, and the durations Entitle will accept — in one place.

The tiers are the ones ``terraform/entitle_user_jit`` creates as workflows
(``auto_approve`` / ``single_approver`` / ``two_approver``), and the default durations per
tier are that module's variable defaults. Virtual-application roles and persona bundles
reuse them so a requester sees the same choices whichever surface they request through.

Which **workflow** a tier routes through is a setting, because the workflows themselves
were created by that module (or by hand) and their ids live in the tenant, not here:
``entitle_workflow_<tier>_id``. A tier with none set falls back to
``entitle_workflow_id`` — and :func:`resolve_workflows` says so, so the fallback is shown
to the operator rather than quietly routing a two-approver request through a one-step
workflow.
"""
from typing import Callable, Optional

TIERS = ("auto_approve", "single_approver", "two_approver")

TIER_LABELS = {
    "auto_approve": "Auto-approve",
    "single_approver": "One approver",
    "two_approver": "Two approvers",
}

# Entitle's fixed list (seconds), shared by integrations, resources, roles and bundles.
# The provider rejects anything else at plan time. -1 is unlimited and never offered here.
ALLOWED_DURATIONS = (1800, 3600, 10800, 21600, 43200, 57600, 86400, 259200, 604800,
                     2628000, 7884000, 15768000, 31536000, 63072000)

# terraform/entitle_user_jit/variables.tf defaults.
TIER_DURATIONS = {
    "auto_approve": (1800, 3600),
    "single_approver": (3600, 10800, 43200, 86400),
    "two_approver": (3600, 10800, 21600),
}

WORKFLOW_SETTING = {t: f"entitle_workflow_{t}_id" for t in TIERS}


def durations_up_to(ceiling: int, floor: int = 1800) -> tuple:
    """Every allowed duration in ``[floor, ceiling]``. ``ceiling`` must itself be allowed,
    so a typo (8h = 28800, which Entitle does not offer) fails loudly rather than
    silently shortening the grant."""
    if ceiling not in ALLOWED_DURATIONS:
        raise ValueError(f"{ceiling}s is not a duration Entitle accepts")
    return tuple(d for d in ALLOWED_DURATIONS if floor <= d <= ceiling)


def _default_cfg(key: str) -> str:
    from . import config_service
    from ..config import settings
    return str(config_service.get(key) or getattr(settings, key, "") or "")


def resolve_workflows(cfg: Optional[Callable[[str], str]] = None) -> dict:
    """``{tier: {"id": ..., "fallback": bool}}``. ``id`` is "" when neither the tier's own
    setting nor ``entitle_workflow_id`` is set."""
    cfg = cfg or _default_cfg
    default = (cfg("entitle_workflow_id") or "").strip()
    out = {}
    for tier in TIERS:
        own = (cfg(WORKFLOW_SETTING[tier]) or "").strip()
        out[tier] = {"id": own or default, "fallback": not own}
    return out
