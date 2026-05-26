"""
Cloud-identity JIT admin API — Phase 3.

Surface that powers the Settings → Integrations → Entitle → Machine
identity panel. The operator uses it to:

  1. Edit the ``cloud_identity_matrix`` JSON blob (operation key →
     Entitle bundle/role id).
  2. Flip per-cloud opt-in flags (cloud_identity_<cloud>_enabled) so
     they can promote AWS, then Azure, then GCP one at a time per
     §8.2–8.4 of the design.
  3. Flip the master ``cloud_identity_gate_enabled`` kill switch.
  4. Set the machine-identity behalfOf email + duration ceiling +
     poll interval — without leaving the Entitle panel.

All endpoints are admin-only. The matrix endpoint validates JSON
client-side before the PATCH, but also re-validates server-side so a
hand-rolled curl with garbage doesn't poison the config.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import User, get_db
from .auth import require_admin

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/cloud-identity", tags=["cloud-identity"])


_PER_CLOUD_FLAGS = ("aws", "azure", "gcp")
_MATRIX_KEY = "cloud_identity_matrix"
_MASTER_GATE = "cloud_identity_gate_enabled"
_MACHINE_EMAIL = "entitle_machine_identity_email"
_TTL_CEILING = "machine_ttl_ceiling_minutes"
_POLL_INTERVAL = "entitle_machine_poll_interval_ms"


class FlagsUpdate(BaseModel):
    """Master gate + per-cloud + machine-identity tuning."""
    gate_enabled: Optional[bool] = None
    aws_enabled: Optional[bool] = None
    azure_enabled: Optional[bool] = None
    gcp_enabled: Optional[bool] = None
    machine_identity_email: Optional[str] = None
    ttl_ceiling_minutes: Optional[int] = None
    poll_interval_ms: Optional[int] = None


class MatrixUpdate(BaseModel):
    """Whole-matrix replace. Body is the JSON object verbatim."""
    matrix: dict


def _cs():
    from ..services import config_service
    return config_service


@router.get("/matrix")
async def get_matrix(
    current_user: User = Depends(require_admin),
):
    """Return current matrix + per-cloud flags + machine tuning.

    Single endpoint so the Settings UI doesn't have to chain three
    GETs on first paint.
    """
    cs = _cs()
    raw = cs.get(_MATRIX_KEY, "") or ""
    try:
        matrix = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        matrix = {}
        logger.warning("cloud_identity_matrix is not valid JSON; returning empty to caller")
    return {
        "matrix": matrix,
        "matrix_raw": raw,
        "gate_enabled": cs.get_bool(_MASTER_GATE, default=False),
        "flags": {
            cloud: cs.get_bool(f"cloud_identity_{cloud}_enabled", default=False)
            for cloud in _PER_CLOUD_FLAGS
        },
        "machine": {
            "identity_email": cs.get(_MACHINE_EMAIL, "") or "",
            "ttl_ceiling_minutes": int(cs.get(_TTL_CEILING, "") or 60),
            "poll_interval_ms": int(cs.get(_POLL_INTERVAL, "") or 400),
        },
    }


@router.patch("/matrix")
async def update_matrix(
    payload: MatrixUpdate,
    current_user: User = Depends(require_admin),
):
    """Replace the matrix with the supplied dict.

    Validates each entry is an operation-key string mapped to a dict
    carrying at least one of ``bundle_id`` / ``role_id``. Anything else
    is rejected at the door so we don't silently store nonsense the
    submitter will then fail to call into.
    """
    matrix = payload.matrix
    if not isinstance(matrix, dict):
        raise HTTPException(status_code=400, detail="matrix must be a JSON object")
    for op, target in matrix.items():
        if not isinstance(op, str) or ":" not in op:
            raise HTTPException(
                status_code=400,
                detail=f"operation key {op!r} must be a string like 'aws:ec2:deploy'",
            )
        if not isinstance(target, dict):
            raise HTTPException(
                status_code=400,
                detail=f"target for {op!r} must be a JSON object",
            )
        if not any(k in target for k in ("bundle_id", "role_id")):
            raise HTTPException(
                status_code=400,
                detail=f"target for {op!r} must include 'bundle_id' or 'role_id'",
            )

    cs = _cs()
    cs.set(_MATRIX_KEY, json.dumps(matrix, sort_keys=True))
    logger.info(
        "cloud_identity_matrix updated by %s: %d entries",
        current_user.username, len(matrix),
    )
    return {"matrix": matrix, "count": len(matrix)}


@router.patch("/flags")
async def update_flags(
    payload: FlagsUpdate,
    current_user: User = Depends(require_admin),
):
    """Update any subset of the master gate / per-cloud flags / machine
    tuning fields. Omitted fields are left alone."""
    cs = _cs()
    changed: dict[str, str] = {}

    if payload.gate_enabled is not None:
        cs.set(_MASTER_GATE, "1" if payload.gate_enabled else "0")
        changed[_MASTER_GATE] = str(payload.gate_enabled)
    for cloud, val in (
        ("aws", payload.aws_enabled),
        ("azure", payload.azure_enabled),
        ("gcp", payload.gcp_enabled),
    ):
        if val is not None:
            key = f"cloud_identity_{cloud}_enabled"
            cs.set(key, "1" if val else "0")
            changed[key] = str(val)
    if payload.machine_identity_email is not None:
        cs.set(_MACHINE_EMAIL, payload.machine_identity_email.strip())
        changed[_MACHINE_EMAIL] = "(set)" if payload.machine_identity_email else "(cleared)"
    if payload.ttl_ceiling_minutes is not None:
        if payload.ttl_ceiling_minutes < 1 or payload.ttl_ceiling_minutes > 1440:
            raise HTTPException(status_code=400, detail="ttl_ceiling_minutes must be 1..1440")
        cs.set(_TTL_CEILING, str(payload.ttl_ceiling_minutes))
        changed[_TTL_CEILING] = str(payload.ttl_ceiling_minutes)
    if payload.poll_interval_ms is not None:
        if payload.poll_interval_ms < 100 or payload.poll_interval_ms > 5000:
            raise HTTPException(status_code=400, detail="poll_interval_ms must be 100..5000")
        cs.set(_POLL_INTERVAL, str(payload.poll_interval_ms))
        changed[_POLL_INTERVAL] = str(payload.poll_interval_ms)

    logger.info(
        "cloud_identity flags updated by %s: %s",
        current_user.username, list(changed.keys()),
    )
    return {"changed": changed, "count": len(changed)}
