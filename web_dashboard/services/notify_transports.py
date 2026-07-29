"""Payload shapes and the outbound POST for notifications.

Three formats over one transport. There is no ``Transport`` base class and no
registry — a dict of three functions is the whole abstraction, and the day a fourth
format appears it is one more function and one more dict entry.

  * ``slack``  — an incoming webhook. Wants ``{"text": ...}``; anything else is refused.
  * ``teams``  — a Power Automate **Workflows** webhook. Office 365 connectors inside
    Teams were permanently disabled in May 2026, so the legacy ``MessageCard`` shape is
    dead and is deliberately not implemented. Workflows wants the Bot Framework message
    envelope carrying an Adaptive Card.
  * ``custom`` — a stable, HMAC-signed JSON envelope. This is also how email gets
    delivered: point it at a Power Automate Flow (or any automation platform) and let
    that fan out to a mailbox. There is deliberately no SMTP client in this codebase.

No DB access here, and no config reads beyond what ``notify_policy`` exposes, so the
payload builders unit-test without a database or an event loop.
"""
import hashlib
import hmac
import json
import logging
import os
import time

import httpx

from . import notify_policy

logger = logging.getLogger(__name__)

FORMATS = ("custom", "slack", "teams")
DEFAULT_FORMAT = "custom"

# Adaptive Card schema version. 1.4 is the safe floor across current Teams surfaces —
# newer features render as blank blocks on clients that don't understand them.
_ADAPTIVE_CARD_VERSION = "1.4"

_SIGNATURE_HEADER = "X-Dashboard-Signature"


class NotifyError(Exception):
    """A delivery attempt failed. Carries the text an operator needs to diagnose it —
    the drain loop stores ``str(exc)`` verbatim on the delivery row, because for this
    feature the exception message *is* the diagnosis."""


class RateLimited(NotifyError):
    """The remote asked us to slow down. Separate from a plain failure so the drain
    loop can schedule the retry from ``Retry-After`` instead of its own backoff — a
    429 is not evidence the endpoint is broken."""

    def __init__(self, message: str, retry_after: int = 0):
        super().__init__(message)
        self.retry_after = retry_after


# ── TLS / proxy ──────────────────────────────────────────────────────────────

def _verify():
    """The CA bundle to verify against.

    ``httpx`` builds its SSL context from ``certifi`` and does **not** read
    ``SSL_CERT_FILE`` / ``REQUESTS_CA_BUNDLE`` the way ``requests`` does — yet this
    image sets both (Dockerfile) and ``docker-compose.corp-ca.yml`` mounts the host
    bundle over the container's. Behind a TLS-inspecting proxy that mismatch means
    every Slack/Teams POST fails ``CERTIFICATE_VERIFY_FAILED`` while terraform and
    boto3 in the same container work fine, which is a miserable thing to debug.

    Resolving the bundle explicitly also makes the behaviour independent of which
    httpx minor is installed.
    """
    for var in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"):
        path = os.environ.get(var) or ""
        if path and os.path.isfile(path):
            return path
    return True


# ── Payload builders ─────────────────────────────────────────────────────────

def build_custom(event, subject: str, body: str) -> dict:
    """The generic envelope. This is the feature's public contract — receivers parse
    it, so reshaping it casually breaks integrations we cannot see. Version it in
    docs/notifications.md before changing anything here."""
    return {
        "version": 1,
        "event": event.event_type,
        "severity": event.effective_severity(),
        "subject": subject,
        "body": body,
        "resource": {
            "id": event.resource_id,
            "kind": event.resource_kind,
            "name": event.resource_name,
            "cloud": event.cloud,
            "region": event.region,
            "workgroup": event.workgroup,
        },
        "fields": dict(event.fields or {}),
        "url": notify_policy.absolute_url(event.url),
        # When the event happened, not when it was delivered: the payload is built once
        # at emit time and those exact bytes are what get signed and posted, possibly
        # after a retry. Claiming a send time we don't yet know would be a lie the
        # receiver can't detect.
        "occurred_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def build_slack(event, subject: str, body: str) -> dict:
    """Slack incoming webhook. ``text`` is required — it is what shows in the
    notification popup and in clients that don't render blocks."""
    payload = {"text": subject}
    blocks = [{"type": "section",
               "text": {"type": "mrkdwn", "text": f"*{_mrkdwn(subject)}*"}}]
    if body.strip():
        blocks.append({"type": "section",
                       "text": {"type": "mrkdwn", "text": _mrkdwn(body)[:2900]}})
    link = notify_policy.absolute_url(event.url)
    if link:
        blocks.append({"type": "context", "elements": [
            {"type": "mrkdwn", "text": f"<{link}|Open in dashboard>"}]})
    payload["blocks"] = blocks
    return payload


def build_teams(event, subject: str, body: str) -> dict:
    """Teams via a Power Automate Workflows webhook.

    The ``type: "message"`` + ``attachments[].contentType`` envelope is mandatory —
    posting a bare Adaptive Card is the single most common reason these silently
    render as an empty post.
    """
    card_body = [
        {"type": "TextBlock", "size": "Medium", "weight": "Bolder",
         "text": subject, "wrap": True},
    ]
    if body.strip():
        card_body.append({"type": "TextBlock", "text": body, "wrap": True})
    facts = [{"title": k, "value": str(v)} for k, v in notify_policy.event_facts(event)]
    if facts:
        card_body.append({"type": "FactSet", "facts": facts})

    content = {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": _ADAPTIVE_CARD_VERSION,
        "body": card_body,
    }
    link = notify_policy.absolute_url(event.url)
    if link:
        # Action.OpenUrl requires an absolute URL; an empty one rejects the whole
        # card, so with no base_url configured the action is omitted entirely.
        content["actions"] = [{"type": "Action.OpenUrl",
                               "title": "Open in dashboard", "url": link}]

    return {"type": "message", "attachments": [{
        "contentType": "application/vnd.microsoft.card.adaptive",
        "contentUrl": None,
        "content": content,
    }]}


BUILDERS = {
    "custom": build_custom,
    "slack":  build_slack,
    "teams":  build_teams,
}


def build(fmt: str, event, subject: str, body: str) -> dict:
    return BUILDERS.get(fmt or DEFAULT_FORMAT, build_custom)(event, subject, body)


def _mrkdwn(text: str) -> str:
    """Escape the three characters Slack treats as markup control chars. Slack's
    mrkdwn has no general escape, so this is the documented substitution."""
    return (str(text or "").replace("&", "&amp;")
            .replace("<", "&lt;").replace(">", "&gt;"))


# ── Sending ──────────────────────────────────────────────────────────────────

def serialize(payload: dict) -> bytes:
    """The exact bytes that go on the wire.

    Signing a separately-serialised body and then posting with ``json=`` is the
    classic HMAC bug — the receiver verifies against bytes we never sent and every
    signature check fails. Callers sign *this* and post it with ``content=``.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign(secret: str, timestamp: str, raw: bytes) -> str:
    """``sha256=<hex>`` over ``"<ts>." + body``. Including the timestamp in the signed
    string gives the receiver replay protection for free."""
    mac = hmac.new(secret.encode("utf-8"), f"{timestamp}.".encode("utf-8") + raw,
                   hashlib.sha256)
    return f"sha256={mac.hexdigest()}"


def headers_for(fmt: str, payload: dict, raw: bytes, *, secret: str,
                event_type: str, delivery_id: str) -> dict:
    hdrs = {"Content-Type": "application/json"}
    if fmt != "custom":
        return hdrs
    ts = str(int(time.time()))
    hdrs.update({
        "X-Dashboard-Event": event_type,
        "X-Dashboard-Delivery": delivery_id,
        "X-Dashboard-Timestamp": ts,
    })
    if secret:
        hdrs[_SIGNATURE_HEADER] = sign(secret, ts, raw)
    return hdrs


def check_response(fmt: str, status_code: int, text: str) -> None:
    """Raise :class:`NotifyError` unless the remote really accepted the message.

    Two per-platform quirks live here rather than at the call site:

      * **Slack answers 200 with an error body.** ``invalid_payload`` / ``no_service``
        come back as HTTP 200 with a plain-text body, and treating that as success is
        exactly how a webhook integration ends up silently doing nothing for weeks.
      * **Teams/Power Automate answers 202, not 200.** Accepting only 200 would mark
        every successful Teams post as failed and retry it four times.
    """
    if status_code == 429:
        raise RateLimited(f"rate limited (HTTP 429): {(text or '').strip()[:200]}")
    if not (200 <= status_code < 300):
        raise NotifyError(f"HTTP {status_code}: {(text or '').strip()[:300]}")
    if fmt == "slack" and (text or "").strip().lower() != "ok":
        raise NotifyError(f"Slack rejected the payload: {(text or '').strip()[:200]}")


async def post(url: str, fmt: str, payload: dict, *, secret: str = "",
               event_type: str = "", delivery_id: str = "",
               timeout: int = 10) -> int:
    """POST one payload and return the HTTP status code.

    Raises :class:`NotifyError` (or :class:`RateLimited`) on anything that isn't a
    real acceptance. Never logs the URL or the secret — a Slack/Teams webhook URL is
    itself a bearer credential.
    """
    raw = serialize(payload)
    hdrs = headers_for(fmt, payload, raw, secret=secret,
                       event_type=event_type, delivery_id=delivery_id)
    try:
        async with httpx.AsyncClient(timeout=float(timeout), verify=_verify(),
                                     trust_env=True, follow_redirects=False) as client:
            # content=, not json=: the signature above is over these exact bytes.
            resp = await client.post(url, content=raw, headers=hdrs)
    except httpx.HTTPError as exc:
        raise NotifyError(f"{type(exc).__name__}: {exc}") from exc

    try:
        text = resp.text
    except Exception:                                  # pragma: no cover - defensive
        text = ""
    try:
        check_response(fmt, resp.status_code, text)
    except RateLimited as exc:
        exc.retry_after = notify_policy.retry_after_seconds(
            resp.headers.get("Retry-After"), default=notify_policy.backoff_seconds(0))
        raise
    return resp.status_code


def redact_url(url: str) -> str:
    """A scheme+host hint for the API and the UI. The path and query of a Slack or
    Teams webhook URL are the credential, so they never leave the process."""
    raw = (url or "").strip()
    if not raw:
        return ""
    try:
        from urllib.parse import urlsplit
        parts = urlsplit(raw)
        if not parts.scheme or not parts.netloc:
            return "…"
        return f"{parts.scheme}://{parts.netloc}/…"
    except Exception:                                  # pragma: no cover - defensive
        return "…"
