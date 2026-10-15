"""
Entitle machine-identity JIT integration (cloud_identity_service backend).

The Entitle approval-gate flow that used to live here has been removed. What
remains talks to Entitle's public access-request API on behalf of a synthetic
machine identity so the dashboard can issue short-TTL, auto-approved
cloud-credential elevations for its own privileged operations. See
docs/design/cloud-identity-jit.md §6.1 (prod repo).

Configuration (read from config_service first, then settings.*):
  entitle_api_url    REGIONAL, e.g. https://api.us.entitle.io/v1
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


def _auth() -> tuple:
    """``(base url, auth headers)``: the stored token, or the platform identity.

    Both modes go through ``entitle_public_api.auth``, whose base is scheme + host
    only. The native branch used to take the FULL ``entitle_api_url`` — whose
    shipped default carries ``/v1`` — and append ``/public/v1/…`` to it, so every
    token-mode request went to ``/v1/public/v1/accessRequests``.
    """
    from . import entitle_public_api
    try:
        return entitle_public_api.auth()
    except entitle_public_api.EntitleApiError as exc:
        raise EntitleError(str(exc)) from exc


def request_target(target: dict) -> dict:
    """The access-request ``target`` in the shape Entitle's public API takes.

    ``{"type": "role", "role": {"id": …}}`` or ``{"type": "bundle", "bundle": {"id": …}}``
    (docs.beyondtrust.com/entitle/reference/accessrequests_create). The operation
    matrix and its callers write the short ``{"bundle_id": …}`` / ``{"role_id": …}``
    form, which Entitle rejects; an already-shaped target passes through.
    """
    target = dict(target or {})
    if target.get("type") in ("role", "bundle"):
        return target
    if target.get("bundle_id"):
        return {"type": "bundle", "bundle": {"id": str(target["bundle_id"])}}
    if target.get("role_id"):
        return {"type": "role", "role": {"id": str(target["role_id"])}}
    raise EntitleError(
        f"Entitle target {target!r} names neither a bundle_id nor a role_id")


def _unwrap(payload):
    """The request object out of a response. Entitle wraps it as ``{"result": …}``
    — a one-element list on create — and the bare object is accepted too."""
    if isinstance(payload, dict) and "result" in payload:
        payload = payload["result"]
    if isinstance(payload, list):
        payload = payload[0] if payload else {}
    return payload if isinstance(payload, dict) else {}


async def submit_machine_request(
    *,
    operation: str,
    target: dict,
    duration_minutes: int,
    payload_hash: str,
    behalf_of_email: str,
    justification: str = "Dashboard-issued machine-identity elevation",
) -> str:
    """Open a machine-identity access request. Returns the Entitle request id.

    ``duration`` is SECONDS on the wire, and must be one of the durations the target
    role's or bundle's workflow allows. ``behalfOf`` is ``{"id": …}``, where Entitle
    accepts a UUID, an email or an EUID — not an ``email`` key.
    """
    import httpx

    if not behalf_of_email:
        raise EntitleError(
            "entitle_machine_identity_email is not configured. "
            "Phase 1 needs the synthetic-user email to satisfy Entitle's behalfOf policy."
        )
    api_url, auth = _auth()

    body = {
        "duration": int(duration_minutes) * 60,
        "justification": f"{justification} (operation={operation}, payload={payload_hash[:12]})",
        "target": request_target(target),
        "behalfOf": {"id": behalf_of_email},
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{api_url}{_MACHINE_ENDPOINT}",
                json=body,
                headers={**auth, "Content-Type": "application/json"},
            )
    except httpx.HTTPError as exc:
        raise EntitleError(f"Entitle machine-request submit failed: {exc}") from exc
    if resp.status_code >= 400:
        raise EntitleError(
            f"Entitle rejected machine request ({resp.status_code}): {resp.text[:300]}"
        )
    try:
        parsed = _unwrap(resp.json())
    except ValueError as exc:
        raise EntitleError(f"Entitle response was not JSON: {resp.text[:300]}") from exc
    rid = parsed.get("id") or parsed.get("request_id") or parsed.get("number")
    if not rid:
        raise EntitleError(f"Entitle response missing request id: {parsed}")
    return str(rid)


async def get_machine_request(request_id: str) -> dict:
    """Fetch the current state of a machine access request (unwrapped)."""
    import httpx

    api_url, auth = _auth()
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{api_url}{_MACHINE_ENDPOINT}/{request_id}",
                headers=auth,
            )
    except httpx.HTTPError as exc:
        raise EntitleError(f"Entitle machine-request poll failed: {exc}") from exc
    if resp.status_code == 404:
        raise EntitleError(f"Entitle request {request_id} not found (404)")
    if resp.status_code >= 400:
        raise EntitleError(
            f"Entitle poll error ({resp.status_code}): {resp.text[:300]}"
        )
    try:
        return _unwrap(resp.json())
    except ValueError as exc:
        raise EntitleError(f"Entitle poll response was not JSON: {resp.text[:300]}") from exc


async def find_user(email: str) -> Optional[dict]:
    """The Entitle user ``email`` names, or None — the machine identity's existence
    check for Test connection. A read: it requests nothing and grants nothing."""
    from . import entitle_public_api
    try:
        users = await _get_paged("/public/v1/users", {"search": email})
    except entitle_public_api.EntitleApiError as exc:
        raise EntitleError(str(exc)) from exc
    want = email.strip().lower()
    for user in users:
        if str(user.get("email") or "").strip().lower() == want:
            return user
    return None


async def _get_paged(path: str, params: dict) -> list:
    from . import entitle_public_api
    return await entitle_public_api.get_paged(path, params, what="users", max_pages=1)


# Status values per the design's state machine §6.1. Anything not in the
# terminal sets keeps the poll alive.
# Entitle's own values (accessrequests_show): approved, cancelled, failed, granted,
# permissionInProgress, rejected, revoked, waitingForIT, waitingForApproval. Compared
# lowercased; "approved" is NOT granted — the permission is still being applied.
_PENDING_STATES = {"pending", "submitted", "in_review", "awaiting_approval", "approved_pending_grant",
                   "approved", "permissioninprogress", "waitingforit", "waitingforapproval"}
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
