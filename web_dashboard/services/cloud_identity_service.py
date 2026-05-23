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

import json
import logging
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import AsyncIterator, Literal, Optional

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

    Phase 0 returns an empty dict; Phase 5 populates it from the
    ``cloud_identity_matrix`` config row (set via the Vaults / Machine
    roles UI).
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


@asynccontextmanager
async def elevate(
    cloud: CloudName,
    operation: str,
    *,
    duration_minutes: int = 15,
    payload_hash: str,
    requester_user_id: Optional[str] = None,
    workgroup: Optional[str] = None,
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
        4. Insert ``EntitleActivation`` row with status=pending.
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
    """
    if not _is_gate_enabled():
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

    # Phase 1+ path: real elevation. Not implemented in Phase 0; flipping
    # the gate today raises so an operator can't accidentally enable
    # half-built machinery.
    raise CloudIdentityError(
        "cloud_identity_gate_enabled is True but Phase 1 implementation "
        "(Entitle submit + poll + grant tracking) has not been built yet. "
        "Set cloud_identity_gate_enabled=false to restore baseline behaviour."
    )


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
):
    """Insert an EntitleActivation row in 'pending' state and return its id.

    Phase 0 callers don't reach this — elevate() short-circuits before any
    DB write. Defined now so the row-shape contract is locked in.
    """
    from ..database import SessionLocal, EntitleActivation
    row = EntitleActivation(
        id=str(uuid.uuid4()),
        cloud=cloud,
        operation=operation,
        payload_hash=payload_hash,
        requester_user_id=requester_user_id,
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
