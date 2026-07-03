"""Action-level policy guardrails — pre-action admission control (community).

Makes a **synchronous allow/deny decision on a deploy request** by running the
Rego under ``terraform/policy/admission/`` (package ``admission.<rule_id>``)
against an action-context document, via the bundled OPA binary ([`_opa`](_opa.py)).

The gate is enforced at the **service layer** at each deploy seam — right after
request params are validated and before the job is created (the point of no
return) — because deploy params live in the request body, which a FastAPI
dependency can't see. It **fails closed**: any OPA error denies the action.

Inert by default: :func:`enforce` is a no-op unless ``admission_control_enabled``
is on *and* the action is listed in the ``admission_gated_actions`` config list.
Common caps (allowed regions, blocked instance types, a change-freeze window) are
injected as ``input.limits`` from config, so an operator can set them in Settings
without writing Rego; custom rules are added by dropping a ``.rego`` file in.

Policy convention: each ``.rego`` declares ``package admission.<rule_id>`` with a
``deny`` partial set of human-readable strings. A non-empty ``deny`` for any rule
denies the action; the strings become the caller-facing ``reasons``. (A rule may
also contribute ``needs_approval``; community has no approval gate, so that verdict
is advisory-only here — community policies should use ``deny`` for hard blocks.)
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import HTTPException

from . import _opa, config_service

logger = logging.getLogger(__name__)

# /app/terraform/policy/admission in the container (parents[2] is the app root).
_DEFAULT_ADMISSION_DIR = str(
    Path(__file__).resolve().parents[2] / "terraform" / "policy" / "admission"
)
ADMISSION_POLICY_DIR = os.environ.get("ADMISSION_POLICY_DIR", _DEFAULT_ADMISSION_DIR)


class AdmissionError(Exception):
    """Raised when admission can't be evaluated (OPA missing / errored). The
    :func:`enforce` wrapper treats this as a denial — fail closed."""


def opa_available() -> bool:
    return _opa.opa_available()


def list_rules(policy_dir: str = ADMISSION_POLICY_DIR) -> list[str]:
    """The rule_ids (rego filenames without extension) currently in-repo."""
    return _opa.list_packages(policy_dir)


def evaluate(action: str, context: Optional[dict] = None, *,
             policy_dir: str = ADMISSION_POLICY_DIR) -> dict:
    """Decide whether ``action`` is admitted given ``context``.

    ``context`` carries ``actor`` / ``request`` / ``limits``; ``action`` is merged
    in as the top-level ``input.action``. Returns ``{decision:
    'allow'|'deny'|'needs_approval', reasons: [str], approval_reasons: [str],
    rules: [str]}``. Precedence: deny > needs_approval > allow. Raises
    :class:`AdmissionError` (fail-closed) on any OPA failure.
    """
    input_doc = {"action": action, **(context or {})}
    try:
        value = _opa.eval_query(input_doc, data_dir=policy_dir, query="data.admission")
    except _opa.OpaError as exc:
        raise AdmissionError(str(exc)) from exc

    def _msgs(entries) -> list[str]:
        out: list[str] = []
        for d in entries:
            out.append(d if isinstance(d, str)
                       else (d.get("msg") if isinstance(d, dict) else str(d)))
        return out

    reasons: list[str] = []
    approval_reasons: list[str] = []
    rules: list[str] = []
    for rule_id, body in sorted(value.items()):
        deny = body.get("deny", []) if isinstance(body, dict) else []
        needs = body.get("needs_approval", []) if isinstance(body, dict) else []
        if not deny and not needs:
            continue
        rules.append(rule_id)
        reasons.extend(_msgs(deny))
        approval_reasons.extend(_msgs(needs))

    if reasons:
        decision = "deny"
    elif approval_reasons:
        decision = "needs_approval"
    else:
        decision = "allow"
    logger.info("admission action=%s decision=%s rules=%s", action, decision, rules)
    return {"decision": decision, "reasons": reasons,
            "approval_reasons": approval_reasons, "rules": rules}


# ── Community enforcement seam ──────────────────────────────────────────────────

def _enabled() -> bool:
    from ..config import settings
    return config_service.get_bool("admission_control_enabled",
                                   getattr(settings, "admission_control_enabled", False))


def _csv_or_json_list(key: str) -> list[str]:
    """A config value that may be a JSON array or a comma-separated string."""
    raw = (config_service.get(key) or "").strip()
    if not raw:
        return []
    if raw.startswith("["):
        try:
            return [str(x).strip() for x in json.loads(raw) if str(x).strip()]
        except Exception:
            return []
    return [s.strip() for s in raw.split(",") if s.strip()]


def gated_actions() -> set[str]:
    """Actions the operator has opted into gating (live from config)."""
    return set(_csv_or_json_list("admission_gated_actions"))


def _limits() -> dict:
    """Config-driven caps exposed to policies as ``input.limits`` so the common
    rules are settable from Settings without editing Rego."""
    return {
        "allowed_regions": _csv_or_json_list("admission_allowed_regions"),
        "denied_instance_types": _csv_or_json_list("admission_denied_instance_types"),
        "prod_window": _csv_or_json_list("admission_prod_window"),  # frozen weekdays, e.g. sat,sun
    }


def _now_doc(dt: datetime) -> dict:
    """Expose the current time to policies as ``input.now`` — weekday computed in
    Python (lowercase ``mon``..``sun``) so Rego needs no date math."""
    return {"iso": dt.isoformat(), "weekday": dt.strftime("%a").lower(), "hour": dt.hour}


def _audit_deny(db, actor, action: str, reasons: list[str]) -> None:
    if db is None:
        return
    try:
        from . import job_service
        job_service.log_audit(
            db, getattr(actor, "username", "system"), f"{action}:denied",
            details={"reasons": reasons},
        )
    except Exception:  # auditing must never mask the 403
        logger.warning("failed to audit admission denial for %s", action, exc_info=True)


def enforce(action: str, *, request: dict, actor=None, db=None, now=None) -> None:
    """Pre-action gate. No-op unless enabled AND ``action`` is gated. On a deny
    decision (or a fail-closed engine error) audit the denial and raise
    ``HTTPException(403, {"error": "policy", "reasons": [...]})``.

    ``request`` is the deploy params (region, size, image, name, …) exposed to
    policies as ``input.request``; ``actor`` is the current user (for audit +
    ``input.actor``); ``db`` is the request session used to write the audit row.
    """
    if not _enabled() or action not in gated_actions():
        return

    context = {
        "actor": {
            "username": getattr(actor, "username", None),
            "is_admin": bool(getattr(actor, "is_effective_admin", False)),
        },
        "request": dict(request or {}),
        "limits": _limits(),
        # Date math done here so the Rego stays timezone-free (input.now.weekday).
        "now": _now_doc(now or datetime.utcnow()),
    }

    try:
        result = evaluate(action, context)
    except AdmissionError as exc:
        # Fail closed: a broken/absent engine denies a gated action.
        logger.warning("admission fail-closed for %s: %s", action, exc)
        _audit_deny(db, actor, action, [f"policy engine unavailable: {exc}"])
        raise HTTPException(
            status_code=403,
            detail={"error": "policy",
                    "reasons": ["Action blocked: policy engine unavailable (fail-closed)."]},
        )

    if result["decision"] == "deny":
        _audit_deny(db, actor, action, result["reasons"])
        raise HTTPException(
            status_code=403,
            detail={"error": "policy", "reasons": result["reasons"]},
        )
    if result["decision"] == "needs_approval":
        # No human approval gate in community — advisory only, admit.
        logger.info("admission action=%s needs_approval (advisory; no approval gate) reasons=%s",
                    action, result["approval_reasons"])
