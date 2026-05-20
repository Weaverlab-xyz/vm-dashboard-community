"""
Entitle approval workflow integration.

`api.auth.require_approval(action)` gates an endpoint behind an Entitle
request — on first call it opens a request via this module's
`create_request()`, returns 202 with a poll URL, and the frontend re-issues
the call with `X-Entitle-Approval-Id` once the approver hits Approve.

This module deliberately stays small:
  - canonical_payload_hash: stable SHA-256 of the request body so an
    approver's decision binds to *that* exact payload (operator can't get
    approval for `read secret A` then swap to `read secret B`).
  - create_request: POST to Entitle's request endpoint, return a ticket.
  - verify_webhook: HMAC-SHA256 verify of the inbound webhook so an
    arbitrary internet caller can't flip an approval to `approved`.

Configuration (read from config_service first, then settings.*):
  entitle_api_url            e.g. https://api.entitle.io/v1
  entitle_api_token          bearer token for the Entitle workspace
  entitle_webhook_secret     HMAC shared secret (also set in Entitle's
                             webhook config so signatures match)
  entitle_default_ttl_minutes  TTL for created requests; 15 by default

For the dashboard host to receive webhooks, Entitle must be able to reach
the public ingress (we do NOT poll Entitle from inside; the webhook is the
one channel that flips `Approval.status` from `pending` to `approved`).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)


class EntitleError(Exception):
    """Raised when an Entitle API call fails or is misconfigured."""


@dataclass
class EntitleTicket:
    """Returned by `create_request`. Threaded into the dashboard's Approval
    row so the frontend can poll and the webhook can correlate."""
    request_id: str
    expires_at: datetime


def _cfg(key: str, fallback: str = "") -> str:
    """Read from config_service first, fall back to settings, then fallback."""
    from . import config_service
    from ..config import settings
    return config_service.get(key) or getattr(settings, key, None) or fallback


def _cfg_int(key: str, fallback: int) -> int:
    raw = _cfg(key, "")
    try:
        return int(raw) if raw else fallback
    except (TypeError, ValueError):
        return fallback


def canonical_payload_hash(body: bytes | str | None) -> str:
    """Return a SHA-256 hex digest of a stable serialization of `body`.

    The approver's decision binds to the exact payload that was approved:
    if the operator swaps the body between request and retry, the
    `payload_hash` comparison in `require_approval` fails. JSON bodies are
    re-serialized with sorted keys so harmless whitespace differences
    don't invalidate an otherwise-identical retry. Non-JSON bodies are
    hashed verbatim.
    """
    if body is None:
        body = b""
    if isinstance(body, str):
        body = body.encode("utf-8")
    if not body:
        return hashlib.sha256(b"").hexdigest()
    try:
        parsed = json.loads(body.decode("utf-8"))
        normalized = json.dumps(parsed, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(normalized).hexdigest()
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return hashlib.sha256(body).hexdigest()


async def create_request(action: str, username: str, payload_hash: str) -> EntitleTicket:
    """Create an Entitle approval request and return its ticket.

    Implemented with httpx so we don't pin sync vs async. Errors surface as
    `EntitleError` so `require_approval` can map them to 503 (operator-
    actionable: "approval service unavailable").
    """
    import httpx

    api_url = _cfg("entitle_api_url").rstrip("/")
    token   = _cfg("entitle_api_token")
    ttl     = _cfg_int("entitle_default_ttl_minutes", 15)
    if not api_url:
        raise EntitleError(
            "Entitle is not configured (entitle_api_url missing). Disable the "
            "approval_gate_enabled flag or fill in the Entitle settings."
        )
    if not token:
        raise EntitleError("Entitle API token is not configured.")

    body = {
        "action":       action,
        "username":     username,
        "payload_hash": payload_hash,
        "ttl_minutes":  ttl,
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{api_url}/requests",
                json=body,
                headers={"Authorization": f"Bearer {token}"},
            )
    except httpx.HTTPError as exc:
        raise EntitleError(f"HTTP failure calling Entitle: {exc}") from exc

    if resp.status_code >= 400:
        raise EntitleError(
            f"Entitle returned {resp.status_code}: {resp.text[:300]}"
        )

    try:
        data = resp.json()
    except ValueError as exc:
        raise EntitleError(f"Entitle response was not JSON: {resp.text[:300]}") from exc

    request_id = data.get("request_id") or data.get("id") or ""
    if not request_id:
        raise EntitleError(f"Entitle response missing request_id: {data}")
    expires_iso = data.get("expires_at")
    if expires_iso:
        try:
            expires_at = datetime.fromisoformat(expires_iso.replace("Z", "+00:00"))
        except ValueError:
            expires_at = datetime.now(timezone.utc) + timedelta(minutes=ttl)
    else:
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=ttl)
    logger.info("Entitle request created: action=%s user=%s id=%s", action, username, request_id)
    return EntitleTicket(request_id=request_id, expires_at=expires_at)


def verify_webhook(payload: bytes, signature: Optional[str]) -> bool:
    """Constant-time verify an inbound Entitle webhook signature.

    Entitle signs the body with HMAC-SHA256 using the shared secret; the
    signature comes in as a header (typically `X-Entitle-Signature`,
    formatted as `sha256=<hex>`). Returns False on any malformed input so
    callers can 401 / 403 the request.
    """
    if not signature:
        return False
    secret = _cfg("entitle_webhook_secret")
    if not secret:
        # Misconfigured rather than malicious — refuse to accept *any*
        # webhook until the operator sets a secret.
        logger.warning("Entitle webhook rejected: entitle_webhook_secret not configured.")
        return False
    if signature.startswith("sha256="):
        signature = signature[len("sha256="):]
    expected = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
