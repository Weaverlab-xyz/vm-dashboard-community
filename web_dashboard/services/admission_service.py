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


def _enforce_approval() -> bool:
    """Whether a policy's ``needs_approval`` verdict is acted on, or just logged.

    Default FALSE, and the default is the whole point: enforcing it changes what
    happens to an action that previously proceeded. See the branch in :func:`enforce`.

    Independent of the change-window approval gate (``change_approval_required``),
    which governs a change an operator BOOKED. This one governs a verdict a POLICY
    reached, and an operator may reasonably want one without the other.
    """
    return config_service.get_bool("admission_enforce_needs_approval", False)


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


def _satisfies_window(window, scheduled: Optional[dict], at: datetime) -> bool:
    """Is this request already booked into ``window``?

    **Without this the feature is useless**: the refusal offers a window, and
    accepting the offer sends the request straight back through the same gate. If the
    gate only asked "is it Saturday yet", the booked change would be refused too and
    the operator would have no way to comply with the thing they were just told to do.

    Two ways to satisfy it, and both are checked against the RESOLVED booking rather
    than the raw form input, so there is no second place where a time is interpreted:

      * booked into this very window (`change_window_id` matches), or
      * booked for an instant that falls inside one of its occurrences — which is how
        "at a time" inside the window is accepted without naming it.
    """
    if not scheduled:
        return False
    if scheduled.get("change_window_id") and \
            scheduled["change_window_id"] == getattr(window, "id", None):
        return True
    when = scheduled.get("scheduled_for")
    if when is None:
        return False
    try:
        from . import change_window
        return change_window.occurrence_covering(window, when)
    except Exception:  # noqa: BLE001 — an unresolvable window is handled by the caller
        return False


def _enforce_change_window(action: str, request: dict, actor, db, now,
                           scheduled: Optional[dict] = None) -> None:
    """Refuse a change against a workgroup that may only be changed in its window —
    and hand back the next occurrence so the caller can offer to book it instead.

    **Why it lives in this function rather than in a Rego rule.** The window is a row
    keyed off the request's workgroup, and OPA is handed a static document; it cannot
    read the database. More decisively, a deny is not the useful answer here: the
    operator wants the change, and the only thing wrong with it is *when*. So the
    refusal carries `schedule`, and the UI turns that into one click.

    **Independent of ``admission_control_enabled``.** That flag switches the POLICY
    ENGINE on, and a maintenance window should not require running OPA. It does share
    ``gated_actions()``, which is the operator's own answer to "what counts as a
    change" — one list, not two. A consequence worth knowing: power operations are
    deliberately absent from that list (``tests/test_cloud_power``), so a window does
    not block start/stop unless an administrator adds them.

    Inert in every configuration except the one an administrator opted into: no
    workgroup on the request, no workgroup row, no window on it, or the requirement
    not ticked — all return immediately, which is every install until somebody sets
    one up.
    """
    if action not in gated_actions():
        return
    if db is None:
        return
    name = (request or {}).get("workgroup")
    if not name or not str(name).strip():
        return

    # ── Does this workgroup require a window at all? ──────────────────────────
    #
    # This lookup fails OPEN, and the split from the fail-closed block below is the
    # important part. If the query itself cannot run — most plausibly because the
    # `require_change_window` column is missing on an install whose migration was
    # skipped, which #946 showed happens SILENTLY on PostgreSQL — then we do not know
    # whether a window is required, and the overwhelmingly likely truth is that this
    # install has never used the feature. Failing closed on that would 403 every gated
    # deploy on an estate that never opted in: a far worse outcome than not enforcing
    # a constraint nobody configured.
    #
    # Once we KNOW a workgroup requires a window, everything after this point fails
    # closed, because then an administrator has asked for these changes to be gated.
    try:
        from ..database import ChangeWindow, Workgroup
        from . import change_window

        row = (db.query(Workgroup)
               .filter(Workgroup.name == str(name).strip().lower())
               .first())
        required = row is not None and row.require_change_window is True
    except Exception as exc:  # noqa: BLE001 — see above
        logger.warning(
            "change-window gate could not determine whether %r requires a window "
            "(%s); admitting. If this workgroup is meant to be gated, check that the "
            "workgroups.require_change_window migration applied.", name, exc)
        return
    if not required:
        return

    try:
        window = (db.query(ChangeWindow)
                  .filter(ChangeWindow.id == row.change_window_id).first())
        if window is None:
            # Configured to require a window that no longer exists. FAIL CLOSED, in
            # keeping with the rest of this module: an administrator asked for these
            # changes to be gated, and a deleted window is a broken gate, not an open
            # one. The message names the fix rather than just refusing.
            reasons = [f"workgroup {name!r} requires a change window, but the window it "
                       f"points at no longer exists. An administrator needs to pick "
                       f"another on the Workgroups tab."]
            _audit_deny(db, actor, action, reasons)
            raise HTTPException(status_code=403,
                                detail={"error": "change_window", "reasons": reasons})

        at = now or datetime.utcnow()
        # Already booked into this window -- accepting the offer this gate itself
        # made. Checked BEFORE "are we inside it now", because the whole point of a
        # booking is that we are not.
        if _satisfies_window(window, scheduled, at):
            return
        if change_window.occurrence_covering(window, at):
            return                      # inside the window right now: proceed

        start, end = change_window.next_occurrence(window, at)
        summary = change_window.describe(window).get("summary", "")
        reasons = [
            f"{name} may only be changed during {window.name} ({summary}). "
            f"The next window opens {start:%Y-%m-%d %H:%M} UTC."
        ]
        _audit_deny(db, actor, action, reasons)
        raise HTTPException(status_code=403, detail={
            "error": "change_window",
            "reasons": reasons,
            # What the caller needs to offer "book it instead" without a second
            # round trip or any window arithmetic of its own.
            "schedule": {
                "change_window_id": window.id,
                "window_name": window.name,
                "next_start": start.isoformat(),
                "next_end": end.isoformat(),
            },
        })
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        # A misconfigured window must not take the endpoint down. FAIL CLOSED for the
        # same reason the OPA path does: the workgroup is marked as requiring a window,
        # and admitting the change because the check itself broke would be the one
        # outcome nobody asked for.
        logger.warning("change-window gate failed for %s/%s: %s", action, name, exc)
        reasons = [f"workgroup {name!r} requires a change window and the window could "
                   f"not be evaluated ({exc}). An administrator needs to check it on "
                   f"the Workgroups tab."]
        _audit_deny(db, actor, action, reasons)
        raise HTTPException(status_code=403,
                            detail={"error": "change_window", "reasons": reasons})


def enforce(action: str, *, request: dict, actor=None, db=None, now=None,
            scheduled: Optional[dict] = None, approvable: bool = False) -> dict:
    """Pre-action gate. No-op unless enabled AND ``action`` is gated. On a deny
    decision (or a fail-closed engine error) audit the denial and raise
    ``HTTPException(403, {"error": "policy", "reasons": [...]})``.

    ``request`` is the deploy params (region, size, image, name, …) exposed to
    policies as ``input.request``; ``actor`` is the current user (for audit +
    ``input.actor``); ``db`` is the request session used to write the audit row.

    **Two gates, in order.** The workgroup change-window check runs first and is
    independent of ``admission_control_enabled``, because that flag is the OPA switch
    and a maintenance window must not require running a policy engine to be enforced.
    It shares ``gated_actions()`` — the operator's own list of what counts as a change
    — so there is one list to reason about rather than two.
    """
    _enforce_change_window(action, request, actor, db, now, scheduled)

    if not _enabled() or action not in gated_actions():
        return {}

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
        # Community HAS an approval gate (Job.approval_required, the /jobs approve
        # endpoint, the `change_windows:use` permission), so this verdict CAN be
        # enforced rather than logged — behind a flag, and OFF by default.
        #
        # Off by default because enforcing it is a breaking change on upgrade, and the
        # docs invited exactly the policy it would break. No shipped rule emits
        # `needs_approval`, but the guardrails doc described the verdict as available
        # and advisory, so an operator may well have written a custom rule using it as
        # a soft signal. Flipping the meaning under them would turn actions that
        # worked yesterday into 403s, with the policy unchanged. An operator who wants
        # the gate turns it on knowing what it does.
        if not _enforce_approval():
            logger.info(
                "admission action=%s needs_approval (advisory; enforcement is off — "
                "see admission_enforce_needs_approval) reasons=%s",
                action, result["approval_reasons"])
            return {}

        # `approvable=True` says "I will pass what you return into create_job". A seam
        # that does not opt in is REFUSED, deliberately: the alternative is admitting an
        # action a policy said needs a second person, which is the one outcome nobody
        # asked for. Refusing is also self-correcting — the operator sees exactly which
        # surface cannot express the verdict, instead of a policy that appears to work
        # on some pages and silently does nothing on others.
        if not approvable:
            logger.warning(
                "admission action=%s needs_approval but this seam cannot create an "
                "approval-gated job; refusing. reasons=%s",
                action, result["approval_reasons"])
            reasons = list(result["approval_reasons"]) + [
                "This action requires approval, and it cannot be requested from here "
                "yet. Raise it from a surface that supports scheduling, or have an "
                "administrator narrow the policy."]
            _audit_deny(db, actor, action, reasons)
            raise HTTPException(status_code=403,
                                detail={"error": "needs_approval", "reasons": reasons})

        logger.info("admission action=%s needs_approval reasons=%s",
                    action, result["approval_reasons"])
        _audit_needs_approval(db, actor, action, result["approval_reasons"])
        return {"approval_required": True}
    return {}


def _audit_needs_approval(db, actor, action: str, reasons: list) -> None:
    """Record that policy required approval, distinctly from a denial.

    Its own audit action rather than reusing `<action>:denied`: the change was
    ADMITTED, as a job nobody may run until a second person signs it off. Filing that
    under "denied" would make the audit trail claim something that did not happen, and
    would hide the approval that follows from anyone reading the action name.
    """
    if db is None:
        return
    try:
        from . import job_service
        job_service.log_audit(
            db, getattr(actor, "username", "system"), f"{action}:needs_approval",
            details={"reasons": list(reasons)},
        )
    except Exception:  # noqa: BLE001 — auditing must never mask the decision
        logger.warning("failed to audit approval requirement for %s", action,
                       exc_info=True)
