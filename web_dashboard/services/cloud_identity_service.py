"""
Machine-identity JIT elevation submitter.

This module is the runtime entry point for the cloud-identity JIT design
(see docs/design/cloud-identity-jit.md). Every cloud SDK write path will
eventually wrap its calls in ``async with elevate(cloud, operation, ...)``;
the context manager either:

  - **Gate off** (``cloud_identity_gate_enabled`` is False — the Phase 0
    default): yields immediately. No Entitle round-trip, no IAM change,
    no audit row. The dashboard's baseline cloud credentials carry their
    standing privileges as today. **This is what ships in Phase 0.**

  - **Gate on** (Phase 1+): submits an Entitle access request via
    ``services.entitle_service.submit_machine_request()``, polls for
    ``granted``, yields once the cloud-side IAM is in place, and waits
    for Entitle's natural TTL revoke afterwards. Failures fail loudly
    (no silent fall-back to the baseline credential — that's the bug
    class this whole design exists to prevent).

The Phase 0 implementation is deliberately a no-op when the flag is off
so callers can adopt the wrapper now and the behaviour change goes live
only when the operator flips the flag and registers the Entitle vault /
operation matrix.

Operation matrix
----------------
The mapping from dashboard operations (``aws:ec2:deploy``) to Entitle
``resource`` ids (and per-cloud IAM roles) lives in the
``cloud_identity_matrix`` config blob (see §6.3 of the design). Phase 0
ships an empty default; Phase 5 lets an admin edit it from
``Settings → Integrations → Entitle → Machine roles``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator, Literal, Optional

from ..config import settings

logger = logging.getLogger(__name__)

CloudName = Literal["aws", "azure", "gcp"]


class CloudIdentityError(Exception):
    """Raised when an elevation cannot be obtained.

    The dashboard surface (cloud SDK call sites) should let this propagate
    rather than catching and falling back to the baseline credential. The
    whole point of the gate is to fail closed when policy denies or the
    approval service is unreachable.
    """


@dataclass
class ElevationHandle:
    """Represents an active machine-identity elevation.

    Phase 0 fields are minimal. Phase 1 will add cloud-side correlation
    (session tag for AWS, correlation-request-id header for Azure,
    user-agent suffix for GCP) so cloud audit logs join cleanly to the
    Entitle approval id.
    """
    cloud: CloudName
    operation: str
    duration_minutes: int
    payload_hash: str
    requester_user_id: Optional[str]
    # Populated only when the gate is ON and the request was granted:
    entitle_request_id: Optional[str] = None
    expires_at: Optional[datetime] = None
    activation_row_id: Optional[str] = None
    # True when the gate was off and this handle is a dormant no-op:
    is_noop: bool = False
    extra: dict = field(default_factory=dict)

    @property
    def correlation_tag(self) -> str:
        """The string we'd attach to cloud-side calls so the cloud's audit
        log joins to the Entitle activation row. Empty for no-op handles."""
        if self.is_noop or not self.entitle_request_id:
            return ""
        return f"entitle:{self.entitle_request_id}"


def _is_gate_enabled() -> bool:
    """Check the master kill-switch from runtime config.

    Reads via config_service so an operator can flip the flag without a
    restart. config_service caches for 5s across workers, so a flip
    propagates fast but not instantly.
    """
    try:
        from . import config_service
        return config_service.get_bool("cloud_identity_gate_enabled", default=False)
    except Exception:
        # Defensive: if config_service blows up for any reason, treat the
        # gate as OFF so we don't accidentally block the dashboard.
        logger.exception("cloud_identity_gate_enabled lookup failed; defaulting to off")
        return False


def get_operation_matrix() -> dict:
    """Return the configured operation→role matrix as a dict.

    Phase 0 returns an empty dict; Phase 3 added an admin UI under
    Settings → Integrations → Entitle → Machine identity that writes
    here.
    """
    try:
        from . import config_service
        raw = config_service.get("cloud_identity_matrix", "")
        return json.loads(raw) if raw else {}
    except (json.JSONDecodeError, ValueError):
        logger.warning("cloud_identity_matrix is not valid JSON; treating as empty")
        return {}
    except Exception:
        return {}


def is_cloud_enabled(cloud: CloudName) -> bool:
    """Return the per-cloud opt-in flag for cloud-identity JIT.

    Phase 3 of the design (§8.2–8.4 — AWS first, then Azure, then GCP)
    promotes one cloud at a time. Each cloud has its own enable flag:

      cloud_identity_aws_enabled
      cloud_identity_azure_enabled
      cloud_identity_gcp_enabled

    Defaults False so a freshly-flipped master gate (cloud_identity_-
    gate_enabled=True) doesn't immediately route every cloud through
    Entitle before the operator has confirmed each one. ``elevate()``
    treats "cloud disabled" the same as "gate off": yields a no-op
    handle so baseline creds are used for that operation.
    """
    try:
        from . import config_service
        return config_service.get_bool(f"cloud_identity_{cloud}_enabled", default=False)
    except Exception:
        return False


@asynccontextmanager
async def elevate(
    cloud: CloudName,
    operation: str,
    *,
    duration_minutes: int = 15,
    payload_hash: str,
    requester_user_id: Optional[str] = None,
    workgroup: Optional[str] = None,
    tenant_id: Optional[str] = None,
    target: Optional[dict] = None,
    gated: bool = True,
    reuse_min_remaining_minutes: Optional[int] = None,
) -> AsyncIterator[ElevationHandle]:
    """Context manager wrapping a privileged cloud SDK call.

    Phase 0 behaviour (gate OFF, the default):
        Yields a no-op ElevationHandle. The caller proceeds with the
        baseline credential as today. No Entitle round-trip, no DB row.

    Phase 1+ behaviour (gate ON):
        1. Validate ``operation`` against the operation matrix; raise
           CloudIdentityError if not present (fail closed).
        2. Validate ``duration_minutes`` ≤ ``machine_ttl_ceiling_minutes``;
           raise if exceeded (would be denied by Entitle anyway).
        3. Submit Entitle access request via entitle_service.
        4. Insert ``EntitleActivation`` row with status=pending and the
           resolved tenant_id (Phase 2c of multi-tenancy).
        5. Poll Entitle until status reaches a terminal state.
        6. On granted: update row, yield, then on exit fire-and-forget
           the release call.
        7. On denied/failed: update row, fire alert sink, raise.

    Args:
      cloud:            "aws" | "azure" | "gcp"
      operation:        e.g. "aws:ec2:deploy"
      duration_minutes: requested TTL; capped at machine_ttl_ceiling
      payload_hash:     SHA-256 of the originating request body — binds
                        the elevation to that specific payload so a
                        granted activation can't be re-used for a
                        different action
      requester_user_id: dashboard user who triggered the operation
      workgroup:        optional workgroup context (prod multi-tenant);
                        None for community / single-tenant
      tenant_id:        optional tenant slug for prod multi-tenancy.
                        When set, the EntitleActivation row records
                        which tenant the elevation belongs to so the
                        audit trail + cross-tenant webhook check
                        (Phase 4) work correctly. None preserves
                        pre-multi-tenancy behaviour.
      target:           the Entitle role/bundle to request, overriding
                        the operation matrix. For a caller that owns its
                        own setting (the POV cloud provider's GCP target).
      gated:            False for a caller with its OWN switch: skips the
                        master gate and per-cloud flags, which belong to
                        the cloud consoles' deploy paths, so turning on
                        one feature does not silently route the other
                        through Entitle.
      reuse_min_remaining_minutes:
                        when set, a grant for the same cloud + operation
                        that is still live for at least this long is
                        reused instead of filing another request. Grants
                        end by TTL (nothing here revokes), so the grant a
                        previous call obtained is still the machine's;
                        re-requesting it only adds latency and a second
                        approval record, and Entitle may refuse a request
                        for a role the account already holds.
    """
    if gated and not _is_gate_enabled():
        # Phase 0 path: dormant no-op. Caller proceeds with baseline creds.
        handle = ElevationHandle(
            cloud=cloud,
            operation=operation,
            duration_minutes=duration_minutes,
            payload_hash=payload_hash,
            requester_user_id=requester_user_id,
            is_noop=True,
        )
        logger.debug(
            "cloud_identity.elevate(%s, %s) [gate off — no-op]",
            cloud, operation,
        )
        yield handle
        return

    # Phase 3 per-cloud opt-in: even with the master gate on, a cloud
    # whose flag is False still no-ops so the operator can promote one
    # cloud at a time (AWS → Azure → GCP per §8.2–8.4 of the design).
    if gated and not is_cloud_enabled(cloud):
        handle = ElevationHandle(
            cloud=cloud,
            operation=operation,
            duration_minutes=duration_minutes,
            payload_hash=payload_hash,
            requester_user_id=requester_user_id,
            is_noop=True,
        )
        logger.debug(
            "cloud_identity.elevate(%s, %s) [cloud disabled — no-op]",
            cloud, operation,
        )
        yield handle
        return

    # ── Phase 1+ path: real elevation via Entitle ─────────────────────────
    # Imports kept local so Phase 0 (gate off) costs zero entitle_service
    # dependency overhead — the no-op path above never touches httpx.
    from . import entitle_service

    # 1. Validate operation against the configured matrix. Empty matrix +
    #    gate on is a deployment error — fail closed.
    if target is None:
        target = get_operation_matrix().get(operation)
    if not target:
        raise CloudIdentityError(
            f"operation '{operation}' is not in cloud_identity_matrix; refusing to "
            "elevate. Populate the matrix in Settings → Integrations → Entitle."
        )

    # 2. Cap requested duration at the ceiling. Entitle would reject anyway;
    #    do it locally so the audit row records the operator's intent.
    ceiling = max(1, _cfg_int("machine_ttl_ceiling_minutes", 60))
    if duration_minutes > ceiling:
        logger.info(
            "elevate: clamping duration %s -> ceiling %s for %s",
            duration_minutes, ceiling, operation,
        )
        duration_minutes = ceiling

    # 2b. A live grant for the same cloud + operation covers this call.
    if reuse_min_remaining_minutes is not None:
        reused = _live_activation(cloud, operation, reuse_min_remaining_minutes)
        if reused is not None:
            logger.info(
                "cloud_identity.elevate reusing grant: cloud=%s op=%s request=%s expires=%s",
                cloud, operation, reused["entitle_request_id"], reused["expires_at"])
            yield ElevationHandle(
                cloud=cloud,
                operation=operation,
                duration_minutes=duration_minutes,
                payload_hash=payload_hash,
                requester_user_id=requester_user_id,
                entitle_request_id=reused["entitle_request_id"],
                expires_at=reused["expires_at"],
                activation_row_id=reused["id"],
                is_noop=False,
                extra={"reused": True},
            )
            return

    # 3. Create activation row (pending) BEFORE submitting so a crash mid-
    #    submit still leaves an audit trail.
    activation_row_id = _new_activation_row(
        cloud=cloud,
        operation=operation,
        payload_hash=payload_hash,
        requester_user_id=requester_user_id,
        duration_minutes=duration_minutes,
        tenant_id=tenant_id,
    )

    behalf_email = _cfg_str("entitle_machine_identity_email")
    if not behalf_email:
        _update_activation(activation_row_id, status="failed", denial_reason="machine identity email not configured")
        raise CloudIdentityError(
            "entitle_machine_identity_email is empty; cannot submit a "
            "behalfOf request. Configure the synthetic machine user and retry."
        )

    # 4. Submit to Entitle. Translate every error to a CloudIdentityError
    #    so callers have one exception type to handle.
    try:
        request_id = await entitle_service.submit_machine_request(
            operation=operation,
            target=target,
            duration_minutes=duration_minutes,
            payload_hash=payload_hash,
            behalf_of_email=behalf_email,
        )
    except entitle_service.EntitleError as exc:
        _update_activation(activation_row_id, status="failed", denial_reason=str(exc)[:500])
        raise CloudIdentityError(f"Entitle submit failed: {exc}") from exc

    _update_activation(activation_row_id, entitle_request_id=request_id, status="pending")

    # 5. Poll for terminal state. Cap the total wait at the requested
    #    duration — if Entitle hasn't decided by then the request is moot.
    poll_ms = max(100, _cfg_int("entitle_machine_poll_interval_ms", 400))
    deadline = asyncio.get_event_loop().time() + (duration_minutes * 60)
    granted_payload: Optional[dict] = None
    while asyncio.get_event_loop().time() < deadline:
        try:
            payload = await entitle_service.get_machine_request(request_id)
        except entitle_service.EntitleError as exc:
            _update_activation(activation_row_id, status="failed", denial_reason=f"poll error: {exc}"[:500])
            raise CloudIdentityError(f"Entitle poll failed: {exc}") from exc
        category, reason = entitle_service.classify_machine_status(payload)
        if category == "granted":
            granted_payload = payload
            break
        if category == "denied":
            _update_activation(activation_row_id, status="denied", denial_reason=reason)
            raise CloudIdentityError(
                f"Entitle denied request {request_id} (status={payload.get('status')}): {reason or '(no reason)'}"
            )
        await asyncio.sleep(poll_ms / 1000.0)
    if granted_payload is None:
        _update_activation(activation_row_id, status="timeout", denial_reason=f"no terminal state within {duration_minutes}m")
        raise CloudIdentityError(
            f"Entitle did not grant request {request_id} within {duration_minutes} minutes"
        )

    # 6. Resolve grant timestamps + flip row to granted.
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    granted_at = (_parse_dt(granted_payload.get("granted_at"))
                  or _parse_dt(granted_payload.get("grantedAt")) or now)
    # Entitle's request object does not reliably carry an expiry. Without one a
    # granted row could never be reused, and the sweeper could not tell a live grant
    # from an expired one — so fall back to the duration requested, counted from
    # NOW, which can only under-state how long the grant has left.
    expires_at = (_parse_dt(granted_payload.get("expires_at"))
                  or _parse_dt(granted_payload.get("expiresAt"))
                  or now + timedelta(minutes=duration_minutes))
    _update_activation(activation_row_id, status="granted", granted_at=granted_at, expires_at=expires_at)

    handle = ElevationHandle(
        cloud=cloud,
        operation=operation,
        duration_minutes=duration_minutes,
        payload_hash=payload_hash,
        requester_user_id=requester_user_id,
        entitle_request_id=request_id,
        expires_at=expires_at,
        activation_row_id=activation_row_id,
        is_noop=False,
        extra={k: v for k, v in granted_payload.items() if k not in {"id", "status", "granted_at", "expires_at"}},
    )
    logger.info(
        "cloud_identity.elevate granted: cloud=%s op=%s request=%s expires=%s",
        cloud, operation, request_id, expires_at,
    )

    try:
        yield handle
    finally:
        # Best-effort: mark the row complete. We do NOT call DELETE on the
        # Entitle request — revocation is via Entitle's TTL sweep (the
        # agent-revoke sweeper described in §6.7). Marking the row
        # "completed" tells the future sweeper task this activation has
        # served its purpose; mismatches between row-completed and Entitle-
        # still-active drive the audit reconciliation report.
        try:
            _update_activation(activation_row_id, status="completed")
        except Exception:
            logger.exception("failed to mark activation %s completed (non-fatal)", activation_row_id)


# ── Audit-row helpers (Phase 1+ writes through these) ────────────────────────
# Defined now so the table schema and the row-creation API are pinned even
# while the elevate() path is still a no-op.

def _new_activation_row(
    *,
    cloud: CloudName,
    operation: str,
    payload_hash: str,
    requester_user_id: Optional[str],
    duration_minutes: int,
    tenant_id: Optional[str] = None,
):
    """Insert an EntitleActivation row in 'pending' state and return its id.

    Phase 0 callers don't reach this — elevate() short-circuits before any
    DB write. Defined now so the row-shape contract is locked in.

    tenant_id (Phase 2c of multi-tenancy) records which tenant the
    elevation belongs to. Phase 4 will validate this matches the
    webhook's resolving tenant before flipping status to granted.
    """
    from ..database import SessionLocal, EntitleActivation
    row = EntitleActivation(
        id=str(uuid.uuid4()),
        cloud=cloud,
        operation=operation,
        payload_hash=payload_hash,
        requester_user_id=requester_user_id,
        tenant_id=tenant_id,
        status="pending",
        requested_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    db = SessionLocal()
    try:
        db.add(row)
        db.commit()
        return row.id
    finally:
        db.close()


def _update_activation(activation_id: str, **fields) -> None:
    """Patch an EntitleActivation row by id.

    Only known columns are written; unknown kwargs are silently dropped so
    callers can pass through extra metadata without a tight coupling to
    the schema.
    """
    if not activation_id:
        return
    from ..database import SessionLocal, EntitleActivation
    allowed = {
        "status", "entitle_request_id", "entitle_policy_id", "auto_approved",
        "denial_reason", "granted_at", "expires_at", "revoked_at",
    }
    update = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not update:
        return
    db = SessionLocal()
    try:
        db.query(EntitleActivation).filter(EntitleActivation.id == activation_id).update(update)
        db.commit()
    finally:
        db.close()


def _cfg_str(key: str) -> str:
    """config_service first, then settings — what the Settings panels write."""
    try:
        from . import config_service
        value = config_service.get(key)
    except Exception:  # noqa: BLE001
        value = ""
    return str(value or getattr(settings, key, "") or "").strip()


def _cfg_int(key: str, default: int) -> int:
    try:
        return int(_cfg_str(key) or default)
    except (TypeError, ValueError):
        return default


def _live_activation(cloud: str, operation: str, min_remaining_minutes: int) -> Optional[dict]:
    """The newest granted (or granted-then-completed) activation for ``cloud`` +
    ``operation`` that is live for at least ``min_remaining_minutes`` more."""
    from ..database import SessionLocal, EntitleActivation
    cutoff = (datetime.now(timezone.utc).replace(tzinfo=None)
              + timedelta(minutes=max(0, int(min_remaining_minutes))))
    db = SessionLocal()
    try:
        row = (db.query(EntitleActivation)
               .filter(EntitleActivation.cloud == cloud,
                       EntitleActivation.operation == operation,
                       EntitleActivation.status.in_(("granted", "completed")),
                       EntitleActivation.entitle_request_id.isnot(None),
                       EntitleActivation.expires_at.isnot(None),
                       EntitleActivation.expires_at > cutoff)
               .order_by(EntitleActivation.expires_at.desc())
               .first())
        if row is None:
            return None
        return {"id": row.id, "entitle_request_id": row.entitle_request_id,
                "expires_at": row.expires_at}
    finally:
        db.close()


def _parse_dt(value) -> Optional[datetime]:
    """Best-effort ISO-8601 parser for Entitle response timestamps.

    Returns naive UTC (matching the timestamp columns on EntitleActivation).
    None on parse failure or missing input.
    """
    if not value:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    try:
        # Handle trailing Z which fromisoformat doesn't accept on older Pythons.
        s = str(value).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt
    except (TypeError, ValueError):
        return None
