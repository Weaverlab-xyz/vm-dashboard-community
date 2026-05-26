"""
Cloud-identity JIT sweeper — Phase 4a.

Once an hour (configurable) the dashboard's view of "granted machine-
identity elevations" is reconciled against Entitle's view. Drift comes
from three places:

  1. **Late revoke.** Entitle (or its agent) has revoked the grant
     cloud-side but the dashboard's ``entitle_activations`` row still
     reads ``status='granted'``. We flip it to ``revoked`` so audit
     queries don't lie.
  2. **Stale grant past TTL.** The row's ``expires_at`` is in the past
     but the row still reads ``status='granted'``. We flip it to
     ``revoked`` and set ``revoked_at``.
  3. **Vanished request.** Entitle returns 404 / missing for the
     request id — agent-side drift. Reported as an orphan; row is
     flipped to ``failed`` with a descriptive ``denial_reason`` so
     the operator sees something other than a silently-stuck row.

The sweeper itself never *creates* cloud-side IAM bindings — it only
reads. Phase 4b (Azure) and 4c (GCP) extend the per-cloud
reconciliation strategies on top of this foundation.

Gates:
  - ``cloud_identity_gate_enabled`` — master switch. Off → sweeper
    no-ops.
  - ``cloud_identity_sweep_enabled`` — sweeper-specific switch
    (default True). Lets operators temporarily disable sweeps without
    touching the master gate.
  - ``cloud_identity_sweep_interval_minutes`` — loop cadence
    (default 60).

Public surface:
    sweep_once(db) -> dict
        One full pass. Returns ``{started_at, ended_at, processed,
        reconciled, orphans, by_cloud}``.
    sweep_aws(db) -> dict
        AWS-specific reconciliation pass. Phase 4a's only live cloud;
        4b/4c add azure + gcp.
    get_last_sweep_result() -> dict
        The most recent sweep summary, persisted in app_config as
        ``cloud_identity_last_sweep`` so the /api/cloud-identity/orphans
        endpoint can serve the cached view without re-running the
        sweep on demand.

Background loop: lives in main.py's lifespan; calls sweep_once()
every interval. The service itself is sync-iterable so callers (admin
forced re-run, future test runners) can drive it without async glue.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

from ..database import EntitleActivation

logger = logging.getLogger(__name__)


_LAST_SWEEP_KEY = "cloud_identity_last_sweep"
# Rows older than this (by requested_at) are out of scope — operator
# already saw any drift in earlier sweep cycles. Keeps the sweep cheap
# on long-running deployments.
_SWEEP_LOOKBACK_HOURS = 24


def _cs():
    from . import config_service
    return config_service


def _is_enabled() -> bool:
    cs = _cs()
    if not cs.get_bool("cloud_identity_gate_enabled", default=False):
        return False
    return cs.get_bool("cloud_identity_sweep_enabled", default=True)


def sweep_interval_seconds() -> int:
    """Loop cadence the background task should sleep between passes."""
    cs = _cs()
    minutes = max(1, int(cs.get("cloud_identity_sweep_interval_minutes", "") or 60))
    return minutes * 60


def get_last_sweep_result() -> dict:
    cs = _cs()
    raw = cs.get(_LAST_SWEEP_KEY, "") or ""
    if not raw:
        return {"never_run": True}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"corrupted": True, "raw_preview": raw[:200]}


def _persist_result(result: dict) -> None:
    """Best-effort save of the last sweep result. Errors are logged but
    don't propagate — the sweep itself already did the reconciliation."""
    try:
        _cs().set(_LAST_SWEEP_KEY, json.dumps(result, sort_keys=True, default=str))
    except Exception:
        logger.exception("failed to persist cloud_identity_last_sweep")


# ── Per-cloud reconciliation ─────────────────────────────────────────────────

def sweep_aws(db: Session) -> dict:
    """Reconcile AWS rows against Entitle's view.

    For each ``entitle_activations`` row with ``cloud='aws'`` and
    ``status='granted'`` (and updated in the last ``_SWEEP_LOOKBACK_HOURS``),
    poll Entitle and reconcile the local row. Returns a per-cloud
    summary used by the top-level sweeper.
    """
    return _sweep_one_cloud(db, cloud="aws")


def sweep_gcp(db: Session) -> dict:
    """Reconcile GCP rows against Entitle's view — Phase 4c.

    GCP's grant mechanism is agent-driven ``setIamPolicy`` (per design
    §5.3 / §6.7) so drift is actionable in the same way AWS drift is:
    if Entitle says the grant was revoked but the dashboard's row still
    reads ``granted``, an operator needs to know. Structurally
    identical to :func:`sweep_aws` — they share ``_sweep_one_cloud``.

    Future enhancement: an optional cross-check via the GCP IAM
    ``getIamPolicy`` API at the project level, filtering bindings to
    the synthetic machine identity's service-account email. Catches
    the case where Entitle's view and the actual IAM policy disagree.
    Deferred for the same reason as Azure ARM cross-check: marginal
    coverage over Entitle-side reconciliation.
    """
    return _sweep_one_cloud(db, cloud="gcp")


def sweep_azure(db: Session) -> dict:
    """Reconcile Azure rows against Entitle's view — Phase 4b.

    Azure's role assignments self-expire via ``endDateTime`` (set when
    Entitle adds the binding), so cloud-side drift is rare — the
    Entitle-side reconciliation handled by ``_sweep_one_cloud`` is the
    load-bearing check. We mark the per-cloud summary with
    ``self_expiry_trusted=True`` so the orphans endpoint can tell
    operators that any Azure drift surfaced here is informational and
    Azure is expected to have already cleaned up the role assignment
    server-side.

    Future enhancement: an optional cross-check via the ARM
    ``role_assignments`` API to catch the rare case where Entitle
    thinks a grant is active but Azure has revoked the role on its
    own. Deferred — adds an azure-mgmt-authorization dependency for
    marginal coverage over Entitle-side reconciliation.
    """
    summary = _sweep_one_cloud(db, cloud="azure")
    summary["self_expiry_trusted"] = True
    summary["note"] = (
        "Azure role assignments self-expire via endDateTime; this sweep "
        "reconciles dashboard rows against Entitle's view only. Drift "
        "here is informational."
    )
    return summary


def _sweep_one_cloud(db: Session, *, cloud: str) -> dict:
    """Shared per-cloud reconciliation. Phase 4b/4c plug into this
    by calling it with ``cloud='azure'`` or ``cloud='gcp'``."""
    cutoff = datetime.utcnow() - timedelta(hours=_SWEEP_LOOKBACK_HOURS)
    rows = (
        db.query(EntitleActivation)
        .filter(
            EntitleActivation.cloud == cloud,
            EntitleActivation.status == "granted",
            EntitleActivation.requested_at >= cutoff,
        )
        .all()
    )

    processed = 0
    reconciled_revoked = 0
    reconciled_failed = 0
    reconciled_past_ttl = 0
    orphans: list[dict] = []

    # Import inside the function so the sweeper module imports cleanly
    # even when entitle creds aren't configured (the import path runs
    # config_service which can resolve cleanly without secrets).
    import asyncio
    from . import entitle_service

    async def _poll(rid: str) -> Optional[dict]:
        try:
            return await entitle_service.get_machine_request(rid)
        except entitle_service.EntitleError as exc:
            # 404 / not-found surfaces as a string; capture for orphan reporting.
            msg = str(exc).lower()
            if "not found" in msg or "404" in msg:
                return None
            raise

    for row in rows:
        processed += 1
        rid = row.entitle_request_id or ""
        if not rid:
            # Row never got past submit; not a sweep concern. Phase 1's
            # elevate() should already have marked these as failed but
            # be defensive.
            row.status = "failed"
            row.denial_reason = (row.denial_reason or "") + " | sweeper: missing entitle_request_id"
            reconciled_failed += 1
            continue

        now = datetime.utcnow()
        # First: check the local expires_at — Entitle's TTL self-expiry
        # might not have flipped status to revoked yet, but the local
        # clock can tell us the elevation is mathematically past its
        # window. Mark as revoked locally; Entitle will catch up.
        if row.expires_at and row.expires_at <= now:
            row.status = "revoked"
            row.revoked_at = now
            row.denial_reason = (row.denial_reason or "") + " | sweeper: past local TTL"
            reconciled_past_ttl += 1
            continue

        try:
            payload = asyncio.run(_poll(rid))
        except Exception as exc:
            # Entitle errored mid-poll. Don't mutate the row — re-try
            # next sweep. Record the orphan so the operator can triage.
            orphans.append({
                "row_id": row.id,
                "cloud": row.cloud,
                "operation": row.operation,
                "entitle_request_id": rid,
                "kind": "entitle_poll_error",
                "detail": str(exc)[:200],
            })
            continue

        if payload is None:
            # Entitle has no record of this request id. Either an agent
            # bug or the request was hard-deleted; either way the row
            # is orphaned. Flip locally to failed so callers stop
            # treating it as live.
            row.status = "failed"
            row.denial_reason = (row.denial_reason or "") + " | sweeper: entitle 404 / not found"
            orphans.append({
                "row_id": row.id,
                "cloud": row.cloud,
                "operation": row.operation,
                "entitle_request_id": rid,
                "kind": "entitle_unknown_request",
                "detail": "Entitle returned 404 for this request id",
            })
            reconciled_failed += 1
            continue

        category, reason = entitle_service.classify_machine_status(payload)
        if category == "granted":
            # Healthy — Entitle agrees with the dashboard.
            continue
        if category == "denied":
            # Entitle (or its agent) revoked / denied / expired. Flip
            # locally to keep the audit trail honest.
            row.status = "revoked"
            row.revoked_at = now
            row.denial_reason = (
                (row.denial_reason or "")
                + f" | sweeper: entitle status={payload.get('status')} reason={reason or '(none)'}"
            )
            reconciled_revoked += 1
            continue
        # category == 'pending' on a row we already marked granted is
        # weird — log but don't reconcile. The next sweep will pick it
        # up if it sticks.
        orphans.append({
            "row_id": row.id,
            "cloud": row.cloud,
            "operation": row.operation,
            "entitle_request_id": rid,
            "kind": "entitle_status_drift",
            "detail": f"local=granted entitle={payload.get('status')}",
        })

    db.commit()
    return {
        "cloud": cloud,
        "processed": processed,
        "reconciled_revoked": reconciled_revoked,
        "reconciled_past_ttl": reconciled_past_ttl,
        "reconciled_failed": reconciled_failed,
        "orphans": orphans,
    }


# ── Top-level entry ──────────────────────────────────────────────────────────

def sweep_once(db: Session) -> dict:
    """Run one reconciliation pass across every enabled cloud.

    Returns the full summary + persists it as ``cloud_identity_last_sweep``
    in app_config so the /api/cloud-identity/orphans endpoint can serve
    cached results without re-running.

    No-ops cleanly when the master gate or sweep flag is off — the
    return ``skipped`` field tells the caller why.
    """
    started_at = datetime.now(timezone.utc).replace(tzinfo=None)
    if not _is_enabled():
        result = {
            "started_at": started_at.isoformat(),
            "ended_at": started_at.isoformat(),
            "skipped": "cloud_identity_gate_enabled / cloud_identity_sweep_enabled is off",
            "by_cloud": {},
            "orphans": [],
        }
        _persist_result(result)
        return result

    cs = _cs()
    by_cloud: dict[str, dict] = {}
    all_orphans: list[dict] = []

    # Per-cloud enable check matches the Phase 3 elevate() shape — when
    # the operator has promoted a cloud (cloud_identity_<cloud>_enabled=True),
    # the sweeper reconciles it. Each cloud's sweep_* is wrapped in try/
    # except so one cloud's failure doesn't kill the other passes.
    if cs.get_bool("cloud_identity_aws_enabled", default=False):
        try:
            by_cloud["aws"] = sweep_aws(db)
            all_orphans.extend(by_cloud["aws"].get("orphans", []))
        except Exception as exc:
            logger.exception("aws sweep failed")
            by_cloud["aws"] = {"error": str(exc)[:300]}

    # Phase 4b: Azure leg.
    if cs.get_bool("cloud_identity_azure_enabled", default=False):
        try:
            by_cloud["azure"] = sweep_azure(db)
            all_orphans.extend(by_cloud["azure"].get("orphans", []))
        except Exception as exc:
            logger.exception("azure sweep failed")
            by_cloud["azure"] = {"error": str(exc)[:300]}

    # Phase 4c: GCP leg. Same shape as AWS (agent-driven revoke per
    # design §5.3 / §6.7) — drift here is actionable, not informational.
    if cs.get_bool("cloud_identity_gcp_enabled", default=False):
        try:
            by_cloud["gcp"] = sweep_gcp(db)
            all_orphans.extend(by_cloud["gcp"].get("orphans", []))
        except Exception as exc:
            logger.exception("gcp sweep failed")
            by_cloud["gcp"] = {"error": str(exc)[:300]}

    ended_at = datetime.now(timezone.utc).replace(tzinfo=None)
    processed = sum(c.get("processed", 0) for c in by_cloud.values())
    reconciled = sum(
        c.get("reconciled_revoked", 0)
        + c.get("reconciled_past_ttl", 0)
        + c.get("reconciled_failed", 0)
        for c in by_cloud.values()
    )

    result = {
        "started_at": started_at.isoformat(),
        "ended_at": ended_at.isoformat(),
        "duration_seconds": int((ended_at - started_at).total_seconds()),
        "processed": processed,
        "reconciled": reconciled,
        "orphans": all_orphans,
        "by_cloud": by_cloud,
    }
    _persist_result(result)
    logger.info(
        "cloud_identity sweep: processed=%d reconciled=%d orphans=%d clouds=%s",
        processed, reconciled, len(all_orphans), sorted(by_cloud.keys()),
    )
    return result
