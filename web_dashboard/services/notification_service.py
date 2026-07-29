"""Outbound notification delivery: the outbox, the drain loop, and endpoint storage.

    emit(db, event)                    [any process, sync]  ── INSERT one row per endpoint
                                                                     │
                                          notification_deliveries ◄──┘
                                                                     │
    jobs_worker.main() ─ gather ─ drain_loop()  [worker]  ── claim → POST → sent | retry

**Emit sites only INSERT.** No network call, no rendering that can raise, nothing that
takes longer than a row write. That is the strongest available form of "a notification
can never fail a job", and it is asserted statically by ``tests/test_notify_wiring.py``
rather than left as a convention.

**``UNIQUE(dedupe_key)`` is the cross-process coordination.** The app runs under
``gunicorn -w 2`` and the worker at ``replicas: 3``, so an in-process dedupe set would
be worthless. ``job_service.log_audit`` already absorbs an ``IntegrityError`` on a
unique index the same way.

Delivery is **at-least-once**: a worker killed after the POST but before the ``sent``
write re-sends when the row is reclaimed. A duplicate alert beats silence.

The drain loop is a peer of the job runner, not a job type. Each worker runs one job at
a time, so putting delivery in the job queue would park an alert behind a 30-minute
terraform apply — and park the apply behind a stalled POST.
"""
import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..database import NotificationDelivery, NotificationEndpoint
from . import notify_policy, notify_transports

logger = logging.getLogger(__name__)

# Actor recorded on notification-initiated audit entries. A non-login name, so it stays
# separable in an audit query — same reasoning as expiry_reaper.REAPER_ACTOR.
NOTIFIER_ACTOR = "notifier"

_LAST_SCAN_KEY = "notify_last_scan_at"

# Terminal states — a row in one of these is never claimed again.
_TERMINAL = ("sent", "failed", "dry_run", "suppressed")


def _cs():
    from . import config_service
    return config_service


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ── Endpoint storage ─────────────────────────────────────────────────────────
#
# URL and secret are Fernet-encrypted at rest with the same key as app_config, because
# a Slack or Teams webhook URL *is* a bearer credential: whoever holds it can post to
# the channel. They are never returned by the API and never written to a log.

def list_endpoints(db: Session, *, enabled_only: bool = False) -> list:
    q = db.query(NotificationEndpoint)
    if enabled_only:
        q = q.filter(NotificationEndpoint.enabled.is_(True))
    return q.order_by(NotificationEndpoint.created_at).all()


def get_endpoint(db: Session, endpoint_id: str) -> Optional[NotificationEndpoint]:
    return (db.query(NotificationEndpoint)
            .filter(NotificationEndpoint.id == endpoint_id).first())


def endpoint_url(ep: NotificationEndpoint) -> str:
    return _cs().decrypt_value(ep.url or "")


def endpoint_secret(ep: NotificationEndpoint) -> str:
    """The HMAC key, resolving an external-vault reference if that is what is stored.

    Storing ``aws_sm://…`` / ``azure_kv://…`` / ``bt_safe://…`` here works for free
    because config_service already owns the resolution — the operator gets the same
    vault story as every other secret in the app.
    """
    raw = _cs().decrypt_value(ep.secret or "")
    if not raw:
        return ""
    cs = _cs()
    try:
        if cs.is_reference(raw):
            return cs.resolve_reference(raw) or ""
    except Exception:
        logger.warning("could not resolve the vault reference on notification endpoint %s",
                       ep.id, exc_info=True)
        return ""
    return raw


def endpoint_public(ep: NotificationEndpoint) -> dict:
    """The shape the API returns. Carries a scheme+host hint instead of the URL, and
    never the secret — only whether one is set."""
    return {
        "id": ep.id,
        "name": ep.name,
        "fmt": ep.fmt,
        "url_hint": notify_transports.redact_url(endpoint_url(ep)),
        "has_secret": bool(ep.secret),
        "enabled": bool(ep.enabled),
        "event_types": ep.event_types or "",
        "created_at": ep.created_at.isoformat() if ep.created_at else None,
        "created_by": ep.created_by,
        "last_success_at": ep.last_success_at.isoformat() if ep.last_success_at else None,
        "last_error": ep.last_error,
    }


def create_endpoint(db: Session, *, name: str, url: str, fmt: str = "custom",
                    secret: str = "", event_types: str = "",
                    enabled: bool = True, actor: str = "") -> NotificationEndpoint:
    cs = _cs()
    ep = NotificationEndpoint(
        name=(name or "").strip()[:100] or "endpoint",
        url=cs.encrypt_value((url or "").strip()),
        fmt=fmt if fmt in notify_transports.FORMATS else notify_transports.DEFAULT_FORMAT,
        secret=cs.encrypt_value(secret.strip()) if (secret or "").strip() else None,
        event_types=(event_types or "").strip() or None,
        enabled=bool(enabled),
        created_by=actor or None,
    )
    db.add(ep)
    db.commit()
    db.refresh(ep)
    return ep


def update_endpoint(db: Session, endpoint_id: str, **fields) -> Optional[NotificationEndpoint]:
    """Patch an endpoint. Only keys actually present are touched, so a toggle-only
    call cannot blank the URL or the secret — the same contract
    ``api/setup.patch_feature_config`` gives the feature panels."""
    ep = get_endpoint(db, endpoint_id)
    if ep is None:
        return None
    cs = _cs()
    if "name" in fields:
        ep.name = (fields["name"] or "").strip()[:100] or ep.name
    if "url" in fields and (fields["url"] or "").strip():
        ep.url = cs.encrypt_value(fields["url"].strip())
    if "fmt" in fields and fields["fmt"] in notify_transports.FORMATS:
        ep.fmt = fields["fmt"]
    if "secret" in fields:
        val = (fields["secret"] or "").strip()
        # A blank secret clears it; the UI sends the field only when the operator
        # actually typed something, so this can't be triggered by a toggle.
        ep.secret = cs.encrypt_value(val) if val else None
    if "event_types" in fields:
        ep.event_types = (fields["event_types"] or "").strip() or None
    if "enabled" in fields:
        ep.enabled = bool(fields["enabled"])
    db.commit()
    db.refresh(ep)
    return ep


def delete_endpoint(db: Session, endpoint_id: str) -> bool:
    ep = get_endpoint(db, endpoint_id)
    if ep is None:
        return False
    db.delete(ep)
    db.commit()
    return True


# ── Emit ─────────────────────────────────────────────────────────────────────

def emit(db: Session, event) -> int:
    """Queue ``event`` to every endpoint that wants it.

    Returns the number of rows that will actually be acted on — a suppressed row does
    not count. That distinction is load-bearing for the auto-delete warning, which uses
    a non-zero return to decide whether to burn its once-only ``expiry_warned_at``
    latch: a warning dropped by storm control must be re-offered on the next sweep, not
    marked as delivered.

    Synchronous and INSERT-only, because the callers are synchronous functions deep in
    service code (``job_service.set_failed`` has 221 call sites and no reliable event
    loop). Never raises — see :func:`emit_safe`, which is what call sites should use.
    """
    if not notify_policy.enabled():
        return 0

    endpoints = list_endpoints(db, enabled_only=True)
    if not endpoints:
        return 0

    subject, body = notify_policy.render(event)
    suppress = _over_queue_limit(db)
    queued = suppressed = 0

    for ep in endpoints:
        filt = notify_policy.parse_event_types(ep.event_types or "")
        if not notify_policy.should_notify(event, endpoint_event_types=filt):
            continue
        if suppress:
            status = "suppressed"
        elif notify_policy.dry_run():
            status = "dry_run"
        else:
            status = "pending"
        if not _insert(db, event, ep, subject, body, status):
            continue
        if status == "suppressed":
            suppressed += 1
        else:
            queued += 1

    if suppressed:
        _audit_storm(db, suppressed)
    return queued


def emit_safe(db: Session, event) -> int:
    """:func:`emit`, with the guarantee the call sites depend on: it cannot raise, and
    it cannot leave the caller's session dirty.

    Every emit site sits after the commit of the thing it is reporting, so a rollback
    here can never undo that write. This mirrors how the reaper treats ``log_audit`` —
    the record of what happened is best-effort relative to the thing that happened.
    """
    try:
        return emit(db, event)
    except Exception:                                  # noqa: BLE001
        logger.warning("could not queue a %s notification", getattr(event, "event_type", "?"),
                       exc_info=True)
        try:
            db.rollback()
        except Exception:                              # pragma: no cover - defensive
            pass
        return 0


def _insert(db: Session, event, ep, subject: str, body: str, status: str) -> bool:
    """One delivery row. Returns False when the unique dedupe key already claimed it."""
    payload = notify_transports.build(ep.fmt, event, subject, body)
    row = NotificationDelivery(
        event_type=event.event_type,
        severity=event.effective_severity(),
        resource_id=event.resource_id or None,
        resource_kind=event.resource_kind or None,
        resource_name=(event.resource_name or None),
        cloud=event.cloud or None,
        region=event.region or None,
        workgroup=event.workgroup or None,
        url=notify_policy.absolute_url(event.url) or (event.url or None),
        endpoint_id=ep.id,
        channel=ep.fmt,
        subject=subject,
        body=body,
        payload=json.dumps(payload, sort_keys=True),
        status=status,
        dedupe_key=notify_policy.dedupe_key(event, ep.id),
    )
    row.reason_dict = {"route": "global_sink", "endpoint": ep.name}
    if status == "suppressed":
        row.reason_dict = {"route": "global_sink", "endpoint": ep.name,
                           "suppressed": "queue_over_limit"}
    try:
        db.add(row)
        db.commit()
        return True
    except IntegrityError:
        # Another process already queued this exact event for this endpoint. That is the
        # constraint doing its job, not an error.
        db.rollback()
        return False


def _over_queue_limit(db: Session) -> bool:
    try:
        pending = (db.query(NotificationDelivery)
                   .filter(NotificationDelivery.status == "pending").count())
    except Exception:                                  # pragma: no cover - defensive
        return False
    return pending >= notify_policy.max_queue()


def _audit_storm(db: Session, count: int) -> None:
    """One audit entry per emit call, not per row. A hash-chain append takes an advisory
    lock, and an entry per suppressed notification would bury the entries that matter —
    the same argument expiry_reaper makes about its own sweep."""
    try:
        from . import job_service
        job_service.log_audit(db, NOTIFIER_ACTOR, "notification_storm_suppressed",
                              details={"suppressed": count,
                                       "max_queue": notify_policy.max_queue()})
    except Exception:
        logger.warning("could not audit notification_storm_suppressed", exc_info=True)


# ── Drain ────────────────────────────────────────────────────────────────────

def _reclaim_stale(db: Session) -> int:
    """Return rows abandoned in ``sending`` to ``pending``.

    A worker SIGKILLed mid-POST leaves its claim behind; without this the notification
    is lost forever. This is also where at-least-once becomes explicit — if the POST
    actually landed before the process died, the retry is a duplicate.
    """
    cutoff = _utcnow() - timedelta(seconds=notify_policy.STALE_SENDING_SECONDS)
    try:
        n = (db.query(NotificationDelivery)
             .filter(NotificationDelivery.status == "sending",
                     NotificationDelivery.claimed_at < cutoff)
             .update({NotificationDelivery.status: "pending",
                      NotificationDelivery.claimed_at: None},
                     synchronize_session=False))
        db.commit()
        if n:
            logger.warning("reclaimed %d notification deliveries abandoned mid-send", n)
        return n
    except Exception:
        logger.warning("could not reclaim stale notification deliveries", exc_info=True)
        db.rollback()
        return 0


def _claim_batch(db: Session, limit: int) -> list:
    """Claim up to ``limit`` due rows, oldest first.

    The claim is an ``UPDATE ... WHERE status='pending'`` rowcount, exactly like
    ``jobs_worker._claim_one`` — deliberately not ``SKIP LOCKED``, so the same code
    path works on SQLite in development.
    """
    now = _utcnow()
    claimed = []
    try:
        candidates = (db.query(NotificationDelivery.id)
                      .filter(NotificationDelivery.status == "pending")
                      .filter((NotificationDelivery.next_attempt_at.is_(None)) |
                              (NotificationDelivery.next_attempt_at <= now))
                      .order_by(NotificationDelivery.created_at)
                      .limit(limit).all())
    except Exception:
        logger.warning("could not list due notification deliveries", exc_info=True)
        return []

    for (row_id,) in candidates:
        try:
            won = (db.query(NotificationDelivery)
                   .filter(NotificationDelivery.id == row_id,
                           NotificationDelivery.status == "pending")
                   .update({NotificationDelivery.status: "sending",
                            NotificationDelivery.claimed_at: now,
                            NotificationDelivery.attempts:
                                NotificationDelivery.attempts + 1},
                           synchronize_session=False))
            db.commit()
            if won == 1:
                row = (db.query(NotificationDelivery)
                       .filter(NotificationDelivery.id == row_id).first())
                if row is not None:
                    claimed.append(row)
        except Exception:
            logger.warning("could not claim notification delivery %s", row_id, exc_info=True)
            db.rollback()
    return claimed


async def _deliver(db: Session, row: NotificationDelivery) -> bool:
    """POST one claimed row and record the outcome. Never raises."""
    ep = get_endpoint(db, row.endpoint_id) if row.endpoint_id else None
    if ep is None:
        _finish(db, row, "failed", error="its endpoint has been deleted")
        return False
    if not ep.enabled:
        _finish(db, row, "failed", error="its endpoint is disabled")
        return False

    try:
        payload = json.loads(row.payload or "{}")
    except Exception:
        _finish(db, row, "failed", error="stored payload is not valid JSON")
        return False

    url = endpoint_url(ep)
    if not url:
        _finish(db, row, "failed",
                error="the endpoint URL could not be decrypted (has JWT_SECRET_KEY changed?)")
        return False

    try:
        status_code = await notify_transports.post(
            url, ep.fmt, payload,
            secret=endpoint_secret(ep) if ep.fmt == "custom" else "",
            event_type=row.event_type, delivery_id=row.id,
            timeout=notify_policy.http_timeout_s())
    except notify_transports.RateLimited as exc:
        _retry(db, row, str(exc), delay=exc.retry_after or None)
        _note_endpoint_failure(db, ep, str(exc))
        return False
    except notify_transports.NotifyError as exc:
        _retry(db, row, str(exc))
        _note_endpoint_failure(db, ep, str(exc))
        return False
    except Exception as exc:                           # noqa: BLE001
        _retry(db, row, f"{type(exc).__name__}: {exc}")
        _note_endpoint_failure(db, ep, str(exc))
        return False

    _finish(db, row, "sent")
    _note_endpoint_success(db, ep)
    logger.info("notification %s delivered to %s (HTTP %s)",
                row.event_type, ep.name, status_code)
    return True


def _finish(db: Session, row, status: str, *, error: str = "") -> None:
    try:
        row.status = status
        row.error = (error or None) and error[:2000]
        row.sent_at = _utcnow() if status == "sent" else row.sent_at
        db.commit()
    except Exception:                                  # pragma: no cover - defensive
        logger.warning("could not record notification delivery %s as %s", row.id, status,
                       exc_info=True)
        db.rollback()


def _retry(db: Session, row, error: str, *, delay: Optional[int] = None) -> None:
    """Schedule another attempt, or give up.

    Retry state lives in ``next_attempt_at`` rather than in a loop variable precisely
    so it survives a worker restart — which is the difference between this and
    ``cost_service``'s in-request retry.
    """
    attempts = int(row.attempts or 0)
    if attempts >= notify_policy.max_attempts():
        logger.warning("notification %s to endpoint %s gave up after %d attempts: %s",
                       row.event_type, row.endpoint_id, attempts, error)
        _finish(db, row, "failed", error=error)
        return
    wait = delay if delay else notify_policy.backoff_seconds(attempts - 1)
    try:
        row.status = "pending"
        row.claimed_at = None
        row.error = error[:2000]
        row.next_attempt_at = _utcnow() + timedelta(seconds=wait)
        db.commit()
        logger.warning("notification %s to endpoint %s failed (attempt %d), retrying in %ds: %s",
                       row.event_type, row.endpoint_id, attempts, wait, error)
    except Exception:                                  # pragma: no cover - defensive
        logger.warning("could not reschedule notification delivery %s", row.id, exc_info=True)
        db.rollback()


def _note_endpoint_success(db: Session, ep) -> None:
    try:
        ep.last_success_at = _utcnow()
        ep.last_error = None
        db.commit()
    except Exception:                                  # pragma: no cover - defensive
        db.rollback()


def _note_endpoint_failure(db: Session, ep, error: str) -> None:
    try:
        ep.last_error = (error or "")[:2000]
        db.commit()
    except Exception:                                  # pragma: no cover - defensive
        db.rollback()


async def drain_once(db: Session) -> dict:
    """One delivery pass. Returns a small summary; never raises."""
    if not notify_policy.enabled():
        return {"skipped": "disabled"}
    _reclaim_stale(db)
    rows = _claim_batch(db, notify_policy.max_per_flush())
    sent = 0
    for row in rows:
        try:
            if await _deliver(db, row):
                sent += 1
        except Exception:                              # pragma: no cover - defensive
            logger.warning("notification delivery %s raised", row.id, exc_info=True)
    return {"claimed": len(rows), "sent": sent}


async def drain_loop() -> None:
    """Deliver queued notifications forever. Launched from ``jobs_worker.main``.

    Started unconditionally and no-ops while ``notifications_enabled`` is off, and the
    interval is re-read every pass, so flipping the Settings toggle takes effect on the
    next tick without restarting the worker — the same contract
    ``main._expiry_sweeper_loop`` documents.

    Caveat worth knowing: this shares the worker's event loop with the job runner, so a
    dispatched job that blocks the loop delays notifications. It does not lose them —
    the queue is a table.
    """
    from ..database import SessionLocal
    from . import notify_scanner

    while True:
        try:
            db = SessionLocal()
            try:
                result = await drain_once(db)
                await notify_scanner.scan_if_due(db)
                if not result.get("sent"):
                    # Prune only on a quiet pass, so housekeeping never delays an alert.
                    prune(db)
            finally:
                db.close()
        except asyncio.CancelledError:
            raise
        except Exception as exc:                       # noqa: BLE001
            logger.warning("notification drain pass failed: %s", exc)
        try:
            interval = notify_policy.flush_interval_seconds()
        except Exception:                              # pragma: no cover - defensive
            interval = 30
        await asyncio.sleep(interval)


# ── Housekeeping ─────────────────────────────────────────────────────────────

def prune(db: Session) -> int:
    """Drop delivery rows past the retention window.

    Only terminal, non-failed rows: a failed delivery is evidence and stays until an
    operator has seen it. Note ``job_logs`` grows unbounded, so this is not an existing
    convention — notifications are simply higher-volume than job output.
    """
    days = notify_policy.retention_days()
    if not days:
        return 0
    cutoff = _utcnow() - timedelta(days=days)
    try:
        n = (db.query(NotificationDelivery)
             .filter(NotificationDelivery.created_at < cutoff,
                     NotificationDelivery.status.in_(("sent", "dry_run", "suppressed")))
             .delete(synchronize_session=False))
        db.commit()
        return n
    except Exception:
        logger.warning("could not prune notification deliveries", exc_info=True)
        db.rollback()
        return 0


def mark_scanned(now: Optional[datetime] = None) -> None:
    try:
        _cs().set(_LAST_SCAN_KEY, (now or _utcnow()).isoformat())
    except Exception:
        logger.warning("could not persist %s", _LAST_SCAN_KEY, exc_info=True)


def last_scan_at() -> Optional[datetime]:
    try:
        raw = _cs().get(_LAST_SCAN_KEY, "") or ""
    except Exception:                                  # pragma: no cover - defensive
        return None
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "")).replace(tzinfo=None)
    except ValueError:
        return None


# ── Reads for the API ────────────────────────────────────────────────────────

def delivery_public(row: NotificationDelivery) -> dict:
    """Never includes the endpoint URL or secret. ``payload`` is safe — it is the body
    only, and the credential lives in the URL and the signature header."""
    return {
        "id": row.id,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "event_type": row.event_type,
        "severity": row.severity,
        "resource_id": row.resource_id,
        "resource_kind": row.resource_kind,
        "resource_name": row.resource_name,
        "cloud": row.cloud,
        "region": row.region,
        "workgroup": row.workgroup,
        "url": row.url,
        "endpoint_id": row.endpoint_id,
        "channel": row.channel,
        "subject": row.subject,
        "body": row.body,
        "reason": row.reason_dict,
        "status": row.status,
        "attempts": row.attempts,
        "next_attempt_at": row.next_attempt_at.isoformat() if row.next_attempt_at else None,
        "sent_at": row.sent_at.isoformat() if row.sent_at else None,
        "error": row.error,
    }


def list_deliveries(db: Session, *, page: int = 1, page_size: int = 20,
                    status: str = "", channel: str = "", event_type: str = "",
                    resource_id: str = "") -> tuple:
    """→ ``(rows, total)``, newest first. Same shape as ``job_service.list_jobs``."""
    q = db.query(NotificationDelivery)
    if status:
        q = q.filter(NotificationDelivery.status == status)
    if channel:
        q = q.filter(NotificationDelivery.channel == channel)
    if event_type:
        q = q.filter(NotificationDelivery.event_type == event_type)
    if resource_id:
        q = q.filter(NotificationDelivery.resource_id == resource_id)
    total = q.count()
    page = max(1, int(page or 1))
    page_size = max(1, min(200, int(page_size or 20)))
    rows = (q.order_by(NotificationDelivery.created_at.desc())
            .offset((page - 1) * page_size).limit(page_size).all())
    return rows, total


def summary(db: Session, *, hours: int = 24) -> dict:
    """Counts by status over the recent window, for the Settings panel header."""
    since = _utcnow() - timedelta(hours=hours)
    out = {s: 0 for s in ("pending", "sending", "sent", "failed", "dry_run", "suppressed")}
    try:
        rows = (db.query(NotificationDelivery.status)
                .filter(NotificationDelivery.created_at >= since).all())
        for (st,) in rows:
            out[st] = out.get(st, 0) + 1
    except Exception:                                  # pragma: no cover - defensive
        logger.warning("could not summarise notification deliveries", exc_info=True)
    out["window_hours"] = hours
    return out


# ── Test send ────────────────────────────────────────────────────────────────

async def test_send(db: Session, endpoint_id: str, *, actor: str = "") -> dict:
    """Send one real message to one endpoint and return the verbatim outcome.

    Runs inline in the request rather than through the drain loop, and ignores dry-run:
    the entire point of the button is the immediate ``CERTIFICATE_VERIFY_FAILED`` /
    ``invalid_payload`` text. It reads the *stored* configuration, so it tests what a
    real notification would use — the same contract ``POST /api/setup/oidc/test`` has.
    """
    ep = get_endpoint(db, endpoint_id)
    if ep is None:
        return {"ok": False, "error": "no such endpoint"}

    event = notify_policy.NotificationEvent(
        event_type="notification.test",
        title=f"Test notification from the VM dashboard ({ep.name})",
        body="If you can read this, the endpoint is configured correctly.",
        severity="info",
        # A fresh bucket per press, so repeated tests each get their own row rather
        # than being swallowed by the dedupe constraint.
        dedupe_bucket=str(int(time.time())),
        fields={"Format": ep.fmt, "Endpoint": ep.name},
    )
    subject, body = notify_policy.render(event)
    payload = notify_transports.build(ep.fmt, event, subject, body)

    row = NotificationDelivery(
        event_type=event.event_type, severity="info", endpoint_id=ep.id,
        channel=ep.fmt, subject=subject, body=body,
        payload=json.dumps(payload, sort_keys=True),
        status="sending", attempts=1, claimed_at=_utcnow(),
        dedupe_key=notify_policy.dedupe_key(event, ep.id),
    )
    row.reason_dict = {"route": "test", "actor": actor or "?"}
    try:
        db.add(row)
        db.commit()
    except IntegrityError:                             # pragma: no cover - defensive
        db.rollback()

    url = endpoint_url(ep)
    if not url:
        err = "the endpoint URL could not be decrypted (has JWT_SECRET_KEY changed?)"
        _finish(db, row, "failed", error=err)
        return {"ok": False, "error": err, "endpoint": ep.name, "fmt": ep.fmt}

    try:
        status_code = await notify_transports.post(
            url, ep.fmt, payload,
            secret=endpoint_secret(ep) if ep.fmt == "custom" else "",
            event_type=event.event_type, delivery_id=row.id,
            timeout=notify_policy.http_timeout_s())
    except Exception as exc:                           # noqa: BLE001
        err = str(exc) or type(exc).__name__
        _finish(db, row, "failed", error=err)
        _note_endpoint_failure(db, ep, err)
        return {"ok": False, "error": err, "endpoint": ep.name, "fmt": ep.fmt}

    _finish(db, row, "sent")
    _note_endpoint_success(db, ep)
    return {"ok": True, "status_code": status_code, "endpoint": ep.name, "fmt": ep.fmt}
