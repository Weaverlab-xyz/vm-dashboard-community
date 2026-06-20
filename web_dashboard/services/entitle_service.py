"""
Entitle machine-identity JIT integration (cloud_identity_service backend).

The Entitle approval-gate flow that used to live here has been removed. What
remains talks to Entitle's public access-request API on behalf of a synthetic
machine identity so the dashboard can issue short-TTL, auto-approved
cloud-credential elevations for its own privileged operations. See
docs/design/cloud-identity-jit.md §6.1 (prod repo).

Configuration (read from config_service first, then settings.*):
  entitle_api_url    e.g. https://api.entitle.io/v1
  entitle_api_token  bearer token for the Entitle workspace
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


class EntitleError(Exception):
    """Raised when an Entitle API call fails or is misconfigured."""


def _cfg(key: str, fallback: str = "") -> str:
    """Read from config_service first, fall back to settings, then fallback."""
    from . import config_service
    from ..config import settings
    return config_service.get(key) or getattr(settings, key, None) or fallback


# ─── Machine-identity JIT (cloud_identity_service backend) ────────────────────
# See docs/design/cloud-identity-jit.md §6.1 (prod repo).
#
# These wrappers talk to Entitle's public access-request API
# (POST /public/v1/accessRequests + GET …/{id}). Distinct from the
# approval-gate flow above — same Entitle tenant, same bearer token,
# different endpoints + different `behalfOf` identity (a synthetic
# machine user, not the human operator).

_MACHINE_ENDPOINT = "/public/v1/accessRequests"


async def submit_machine_request(
    *,
    operation: str,
    target: dict,
    duration_minutes: int,
    payload_hash: str,
    behalf_of_email: str,
    justification: str = "Dashboard-issued machine-identity elevation",
) -> str:
    """Open a machine-identity access request. Returns the Entitle request id."""
    import httpx

    if not behalf_of_email:
        raise EntitleError(
            "entitle_machine_identity_email is not configured. "
            "Phase 1 needs the synthetic-user email to satisfy Entitle's behalfOf policy."
        )
    api_url = _cfg("entitle_api_url").rstrip("/")
    token = _cfg("entitle_api_token")
    if not api_url or not token:
        raise EntitleError("Entitle is not configured (api url or token missing).")

    body = {
        "duration": duration_minutes,
        "justification": f"{justification} (operation={operation}, payload={payload_hash[:12]})",
        "target": target,
        "behalfOf": {"email": behalf_of_email},
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{api_url}{_MACHINE_ENDPOINT}",
                json=body,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            )
    except httpx.HTTPError as exc:
        raise EntitleError(f"Entitle machine-request submit failed: {exc}") from exc
    if resp.status_code >= 400:
        raise EntitleError(
            f"Entitle rejected machine request ({resp.status_code}): {resp.text[:300]}"
        )
    try:
        parsed = resp.json()
    except ValueError as exc:
        raise EntitleError(f"Entitle response was not JSON: {resp.text[:300]}") from exc
    rid = parsed.get("id") or parsed.get("request_id")
    if not rid:
        raise EntitleError(f"Entitle response missing request id: {parsed}")
    return str(rid)


async def get_machine_request(request_id: str) -> dict:
    """Fetch the current state of a machine access request."""
    import httpx

    api_url = _cfg("entitle_api_url").rstrip("/")
    token = _cfg("entitle_api_token")
    if not api_url or not token:
        raise EntitleError("Entitle is not configured (api url or token missing).")
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{api_url}{_MACHINE_ENDPOINT}/{request_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
    except httpx.HTTPError as exc:
        raise EntitleError(f"Entitle machine-request poll failed: {exc}") from exc
    if resp.status_code == 404:
        raise EntitleError(f"Entitle request {request_id} not found (404)")
    if resp.status_code >= 400:
        raise EntitleError(
            f"Entitle poll error ({resp.status_code}): {resp.text[:300]}"
        )
    return resp.json()


# Status values per the design's state machine §6.1. Anything not in the
# terminal sets keeps the poll alive.
_PENDING_STATES = {"pending", "submitted", "in_review", "awaiting_approval", "approved_pending_grant"}
_GRANTED_STATES = {"granted", "active"}
_DENIED_STATES = {"denied", "rejected", "failed", "expired", "revoked", "cancelled", "canceled"}


def classify_machine_status(payload: dict) -> tuple[str, Optional[str]]:
    """Reduce an Entitle response to ``(category, reason)``."""
    raw = str(payload.get("status") or "").strip().lower()
    reason = payload.get("denial_reason") or payload.get("reason")
    if raw in _GRANTED_STATES:
        return ("granted", None)
    if raw in _DENIED_STATES:
        return ("denied", reason)
    return ("pending", None)
